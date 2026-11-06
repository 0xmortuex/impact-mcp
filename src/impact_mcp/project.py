"""Build the dependency graph of a project directory (read-only)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .jsscan import parse_js
from .pyscan import parse_py
from .resolve import Edge, FileLookup, JsResolver, PyResolver, Resolved
from .walk import PY_EXTS, Scan, scan_tree

CACHE_VERSION = 3


class ImpactError(Exception):
    """An expected, user-actionable failure (shown to the agent verbatim)."""


def cache_dir() -> str:
    env = os.environ.get("IMPACT_MCP_CACHE_DIR")
    if env:
        return env
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "impact-mcp")


def is_test_file(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    if name.endswith(PY_EXTS):
        return (
            name.startswith("test_")
            or name.endswith("_test.py")
            or name == "conftest.py"
            or any(p in ("tests", "test", "__tests__") for p in parts[:-1])
        )
    return bool(re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", name)) or any(
        p in ("__tests__", "tests", "test") for p in parts[:-1]
    )


_SCRIPT_RE = re.compile(r'^\s*[\w.\-]+\s*=\s*["\']([\w.]+)(?::[\w.]+)?["\']', re.M)


def _pyproject_scripts(text: str) -> list[str]:
    """Module names from [project.scripts] / [tool.poetry.scripts] (a tiny, purpose-built reader)."""
    mods: list[str] = []
    section = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            section = s.strip("[] ")
            continue
        if section in ("project.scripts", "tool.poetry.scripts"):
            m = _SCRIPT_RE.match(line)
            if m:
                mods.append(m.group(1))
    return mods


@dataclass
class Project:
    root: str
    scan: Scan
    parsed: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    out: dict[str, list[Edge]] = field(default_factory=dict)
    inn: dict[str, list[Edge]] = field(default_factory=dict)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    dynamic: list[dict[str, Any]] = field(default_factory=list)
    syntax_errors: list[dict[str, Any]] = field(default_factory=list)
    external: Counter[str] = field(default_factory=Counter)
    stdlib: Counter[str] = field(default_factory=Counter)
    assets: int = 0
    warnings: list[str] = field(default_factory=list)
    entry_points: dict[str, str] = field(default_factory=dict)
    tests: set[str] = field(default_factory=set)
    packages: list[str] = field(default_factory=list)
    case_mismatches: int = 0
    ambiguous: int = 0
    py: PyResolver | None = None
    cache_hits: int = 0
    cache_misses: int = 0

    @property
    def files(self) -> list[str]:
        return sorted(self.scan.files)

    def lang(self, rel: str) -> str:
        return "py" if rel.endswith(PY_EXTS) else "js"


def _cache_path(root: str) -> str:
    h = hashlib.sha1(os.path.normcase(root).encode("utf-8")).hexdigest()[:16]
    return os.path.join(cache_dir(), f"{h}.json")


def _load_cache(path: str, warnings: list[str]) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        warnings.append(f"cache file {path} unreadable ({e}); re-parsing everything")
        return {}
    if not isinstance(data, dict) or data.get("v") != CACHE_VERSION:
        return {}
    entries = data.get("e")
    return entries if isinstance(entries, dict) else {}


def _save_cache(path: str, entries: dict[str, Any], warnings: list[str]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"v": CACHE_VERSION, "e": entries}, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as e:
        warnings.append(f"could not write cache {path}: {e} (results are still correct, just not cached)")


def _read(root: str, rel: str) -> bytes | OSError:
    try:
        with open(os.path.join(root, *rel.split("/")), "rb") as fh:
            return fh.read()
    except OSError as e:
        return e


def _parse_bytes(rel: str, data: bytes) -> dict[str, Any]:
    if rel.endswith(PY_EXTS):
        return parse_py(data)
    return parse_js(data.decode("utf-8", errors="replace"))


def _fingerprint(scan: Scan) -> str:
    h = hashlib.sha1()
    for rel in sorted(scan.files):
        mt, sz = scan.files[rel]
        h.update(f"{rel}|{mt}|{sz}\n".encode())
    for rel in sorted([*scan.tsconfigs, *scan.package_jsons, *scan.pyprojects, *scan.pnpm_workspace]):
        try:
            st = os.stat(os.path.join(scan.root, *rel.split("/")))
            h.update(f"{rel}|{st.st_mtime_ns}|{st.st_size}\n".encode())
        except OSError:
            h.update(f"{rel}|gone\n".encode())
    h.update(f"{len(scan.all_files)}".encode())
    return h.hexdigest()


_MEMO: dict[str, tuple[str, Project]] = {}


def build_project(root: str) -> Project:
    root = os.path.realpath(root)
    if not os.path.isdir(root):
        raise ImpactError(f"project directory does not exist: {root}")
    scan = scan_tree(root)
    fp = _fingerprint(scan)
    memo = _MEMO.get(root)
    if memo is not None and memo[0] == fp:
        return memo[1]
    proj = Project(root=root, scan=scan)
    proj.warnings.extend(f"unreadable: {u}" for u in scan.unreadable)

    cpath = _cache_path(root)
    cached = _load_cache(cpath, proj.warnings)
    fresh: dict[str, Any] = {}
    misses: list[str] = []
    for rel, (mt, sz) in scan.files.items():
        ent = cached.get(rel)
        if isinstance(ent, list) and len(ent) == 3 and ent[0] == mt and ent[1] == sz:
            proj.parsed[rel] = ent[2]
            fresh[rel] = ent
            proj.cache_hits += 1
        else:
            misses.append(rel)
    # Reading is I/O bound (slow on Windows with antivirus): read in threads, parse here.
    with ThreadPoolExecutor(max_workers=8) as pool:
        blobs = list(pool.map(lambda r: _read(root, r), misses))
    for rel, blob in zip(misses, blobs, strict=True):
        if isinstance(blob, OSError):
            proj.warnings.append(f"cannot read {rel}: {blob.strerror or blob}")
            continue
        parsed = _parse_bytes(rel, blob)
        proj.cache_misses += 1
        mt, sz = scan.files[rel]
        fresh[rel] = [mt, sz, parsed]
        proj.parsed[rel] = parsed
    if proj.cache_misses or len(fresh) != len(cached):
        _save_cache(cpath, fresh, proj.warnings)

    for rel, parsed in proj.parsed.items():
        if parsed.get("err"):
            proj.syntax_errors.append({"file": rel, "line": parsed.get("err_line"), "error": parsed["err"]})
        for d in parsed.get("dynamic", []):
            proj.dynamic.append({"file": rel, **d})

    code = FileLookup(set(scan.files))
    allf = FileLookup(set(scan.all_files))
    res = Resolved()
    pyfiles = {f for f in scan.files if f.endswith(PY_EXTS)}
    py = PyResolver(scan, pyfiles, code)
    proj.py = py
    js = JsResolver(scan, code, allf, res)
    for rel in sorted(proj.parsed):
        parsed = proj.parsed[rel]
        if parsed.get("err"):
            continue
        if rel in pyfiles:
            py.resolve(rel, parsed, res)
        else:
            js.resolve(rel, parsed, res)

    seen: set[tuple[str, str, int, str, tuple[str, ...]]] = set()
    for e in res.edges:
        if e.src == e.dst:
            continue
        key = (e.src, e.dst, e.line, e.kind, e.names)
        if key in seen:
            continue
        seen.add(key)
        proj.edges.append(e)
        proj.out.setdefault(e.src, []).append(e)
        proj.inn.setdefault(e.dst, []).append(e)
    proj.unresolved = res.unresolved
    proj.external = res.external
    proj.stdlib = res.stdlib
    proj.assets = res.assets
    proj.warnings.extend(res.warnings)
    proj.case_mismatches = code.case_hits
    proj.ambiguous = res.ambiguous
    proj.tests = {f for f in scan.files if is_test_file(f)}
    _find_entry_points(proj, py, js, pyfiles)
    pk = {_pkg_of(f) for f in pyfiles if f.endswith("__init__.py")}
    proj.packages = sorted(p for p in pk if p)
    if scan.symlinks_skipped:
        proj.warnings.append(f"{scan.symlinks_skipped} symlink/junction/loop directories or files were not followed")
    if scan.too_large:
        proj.warnings.append(f"{len(scan.too_large)} files over 2 MB skipped (likely bundles): {', '.join(scan.too_large[:3])}")
    _MEMO[root] = (fp, proj)
    return proj


def _pkg_of(init_rel: str) -> str:
    return init_rel.rpartition("/")[0]


def _find_entry_points(proj: Project, py: PyResolver, js: JsResolver, pyfiles: set[str]) -> None:
    eps = proj.entry_points
    for rel in sorted(pyfiles):
        name = rel.rpartition("/")[2]
        if name == "__main__.py":
            eps[rel] = "__main__.py (python -m)"
        elif proj.parsed.get(rel, {}).get("main") and rel not in proj.tests:
            eps.setdefault(rel, "script with `if __name__ == ...` guard")
        elif name in ("setup.py", "manage.py", "wsgi.py", "asgi.py"):
            eps.setdefault(rel, f"{name} (conventional entry)")
    for ppath in proj.scan.pyprojects:
        try:
            with open(os.path.join(proj.root, *ppath.split("/")), encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            proj.warnings.append(f"cannot read {ppath}: {e.strerror or e}")
            continue
        for mod in _pyproject_scripts(text):
            cands = py.names.get(mod)
            if cands:
                eps[py._pick(ppath, cands, Resolved())] = f"console script in {ppath}"
    for d, pj in sorted(js.pkg_json.items()):
        pj_rel = (d + "/" if d else "") + "package.json"
        bins: list[str] = []
        b = pj.get("bin")
        if isinstance(b, str):
            bins.append(b)
        elif isinstance(b, dict):
            bins.extend(v for v in b.values() if isinstance(v, str))
        for t in bins:
            hit = js._try_target(d, t)
            if hit:
                eps[hit] = f"bin in {pj_rel}"
        for t in js.package_targets(d):
            hit = js._try_target(d, t)
            if hit:
                eps.setdefault(hit, f"main/module/exports in {pj_rel}")
        if (
            not js.package_targets(d)
            and "name" in pj
            and (hit := js.pkg_entry(d))
        ):
            eps.setdefault(hit, f"inferred index of package {pj['name']!r} ({pj_rel})")
    for rel in sorted(proj.scan.files):
        if not rel.endswith(PY_EXTS) and re.search(r"(^|/)[\w.-]+\.config\.[cm]?[jt]s$", rel):
            eps.setdefault(rel, "tool config file")
    for t in list(eps):
        if t in proj.tests:
            del eps[t]
