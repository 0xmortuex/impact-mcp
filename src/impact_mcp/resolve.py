"""Resolve parsed import references to project files.

Everything here works on the set of files found by the scanner; nothing is imported or
executed and the real ``sys.path`` / ``node_modules`` are never consulted. Imports that
point outside the project are counted as *external*; imports that look project-local but
cannot be matched to a file are recorded as *unresolved*.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .walk import JS_EXTS, Scan


@dataclass(slots=True)
class Edge:
    src: str
    dst: str
    line: int
    kind: str  # import | from | export | require | dyn
    names: tuple[str, ...] = ()
    star: bool = False
    flags: tuple[str, ...] = ()
    rename: tuple[tuple[str, str], ...] = ()  # (original name, exported name) for `export {a as b} from`

    @property
    def reexport(self) -> bool:
        return self.kind == "export"


@dataclass
class Resolved:
    edges: list[Edge] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    external: Counter[str] = field(default_factory=Counter)
    stdlib: Counter[str] = field(default_factory=Counter)
    assets: int = 0
    warnings: list[str] = field(default_factory=list)
    case_mismatches: int = 0
    ambiguous: int = 0


class FileLookup:
    """Exact lookup with a case-insensitive fallback on case-insensitive platforms."""

    def __init__(self, files: set[str]) -> None:
        self.files = files
        self.lower: dict[str, str] | None = None
        if sys.platform in ("win32", "darwin"):
            self.lower = {f.lower(): f for f in files}
        self.case_hits = 0

    def get(self, p: str) -> str | None:
        if p in self.files:
            return p
        if self.lower is not None:
            r = self.lower.get(p.lower())
            if r is not None:
                self.case_hits += 1
                return r
        return None


def _join(a: str, b: str) -> str:
    return f"{a}/{b}" if a and b else (a or b)


def _dirname(p: str) -> str:
    return p.rpartition("/")[0]


# --------------------------------------------------------------------------- Python


class PyResolver:
    def __init__(self, scan: Scan, pyfiles: set[str], code: FileLookup) -> None:
        self.code = code
        self.pyfiles = pyfiles
        dirs: set[str] = set()
        for f in pyfiles:
            d = _dirname(f)
            while d and d not in dirs:
                dirs.add(d)
                d = _dirname(d)
        roots: set[str] = {""}
        for d in scan.pyproject_dirs:
            roots.add(d)
            if _join(d, "src") in dirs:
                roots.add(_join(d, "src"))
        for d in dirs:
            if d.rpartition("/")[2] == "src":
                roots.add(d)
        init_dirs = {_dirname(f) for f in pyfiles if f.rpartition("/")[2] == "__init__.py"}
        for d in init_dirs:
            if d and _dirname(d) not in init_dirs:
                roots.add(_dirname(d))
        self.roots = sorted(roots)
        self.names: dict[str, list[str]] = {}
        self.dirnames: set[str] = set()
        self.tops: set[str] = set()
        for f in sorted(pyfiles):
            for r in self.roots:
                if r and not f.startswith(r + "/"):
                    continue
                sub = f[len(r) + 1 :] if r else f
                parts = sub[:-3].split("/")
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if not parts or not all(p.isidentifier() for p in parts):
                    continue
                name = ".".join(parts)
                lst = self.names.setdefault(name, [])
                if f not in lst:
                    lst.append(f)
                self.tops.add(parts[0])
                for k in range(1, len(parts)):
                    self.dirnames.add(".".join(parts[:k]))
        self.stdlib: frozenset[str] = getattr(sys, "stdlib_module_names", frozenset())

    def module_name(self, rel: str) -> str | None:
        """Best dotted name for a file (shortest, i.e. from the deepest source root)."""
        best: str | None = None
        for name, files in self.names.items():
            if rel in files and (best is None or len(name) < len(best)):
                best = name
        return best

    def _pick(self, importer: str, cands: list[str], res: Resolved) -> str:
        if len(cands) == 1:
            return cands[0]
        res.ambiguous += 1
        imp = importer.split("/")[:-1]

        def score(c: str) -> int:
            n = 0
            for a, b in zip(imp, c.split("/")[:-1], strict=False):
                if a != b:
                    break
                n += 1
            return n

        return max(cands, key=score)

    def _abs(self, importer: str, name: str, res: Resolved) -> str | None:
        cands = self.names.get(name)
        if cands:
            return self._pick(importer, cands, res)
        # script-directory behaviour: sibling modules are importable from a file's own dir
        base = _join(_dirname(importer), name.replace(".", "/"))
        return self.code.get(base + ".py") or self.code.get(base + "/__init__.py")

    def _rel_target(self, importer: str, level: int, module: str) -> tuple[str, str | None] | None:
        d = _dirname(importer)
        for _ in range(level - 1):
            if not d:
                return None
            d = _dirname(d)
        p = _join(d, module.replace(".", "/")) if module else d
        f = self.code.get(p + ".py") if module else None
        if f is None:
            f = self.code.get(_join(p, "__init__.py"))
        return p, f

    def _classify_missing(self, importer: str, line: int, spec: str, res: Resolved) -> None:
        top = spec.split(".")[0]
        if spec in self.dirnames:
            return  # namespace package: legal, nothing to point at
        if top in self.tops:
            res.unresolved.append(
                {"file": importer, "line": line, "spec": spec,
                 "reason": "top-level name matches a project package but no such module file"}
            )  # fmt: skip
        elif top in self.stdlib:
            res.stdlib[top] += 1
        else:
            res.external[top] += 1

    def resolve(self, importer: str, parsed: dict[str, Any], res: Resolved) -> None:
        for ref in parsed["refs"]:
            line = ref["ln"]
            flags = tuple(ref["fl"])
            if ref["k"] == "import":
                f = self._abs(importer, ref["m"], res)
                if f is None:
                    self._classify_missing(importer, line, ref["m"], res)
                    continue
                res.edges.append(
                    Edge(importer, f, line, "import", tuple(ref.get("attrs", {}).get("", ())), False, (*flags, "ns"))
                )
                continue
            level, module = ref["lv"], ref["m"]
            base_file: str | None
            if level == 0:
                base_file = self._abs(importer, module, res) if module else None
                base_pkg_name: str | None = module
                rel_dir = None
            else:
                tgt = self._rel_target(importer, level, module)
                if tgt is None:
                    res.unresolved.append(
                        {"file": importer, "line": line, "spec": "." * level + module,
                         "reason": "relative import climbs above the project root"}
                    )  # fmt: skip
                    continue
                rel_dir, base_file = tgt
                base_pkg_name = None
            direct: list[str] = []
            star = False
            attrs: dict[str, list[str]] = ref.get("attrs", {})
            found_any = False
            for name, _as in ref["names"]:
                if name == "*":
                    star = True
                    continue
                sub: str | None
                if rel_dir is not None:
                    p = _join(rel_dir, name)
                    sub = self.code.get(p + ".py") or self.code.get(_join(p, "__init__.py"))
                else:
                    full = f"{base_pkg_name}.{name}" if base_pkg_name else name
                    sub = None
                    if full in self.names:
                        sub = self._pick(importer, self.names[full], res)
                if sub is not None and sub != base_file:
                    found_any = True
                    res.edges.append(Edge(importer, sub, line, "from", tuple(attrs.get(name, ())), False, (*flags, "ns")))
                else:
                    direct.append(name)
            if base_file is not None and (direct or star or not ref["names"]):
                found_any = True
                res.edges.append(Edge(importer, base_file, line, "from", tuple(direct), star, flags))
            elif base_file is None and direct:
                spec = "." * level + module
                if level == 0:
                    if module in self.dirnames or module in self.names:
                        res.unresolved.append(
                            {"file": importer, "line": line, "spec": f"{spec} import {', '.join(direct)}",
                             "reason": "namespace package has no such submodule"}
                        )  # fmt: skip
                    else:
                        self._classify_missing(importer, line, module, res)
                else:
                    res.unresolved.append(
                        {"file": importer, "line": line, "spec": f"{spec} import {', '.join(direct)}",
                         "reason": "relative import target not found"}
                    )  # fmt: skip
            elif base_file is None and not found_any and not ref["names"]:
                self._classify_missing(importer, line, module, res)


# --------------------------------------------------------------------------- JS/TS

_TS_SWAP = {
    ".js": (".ts", ".tsx"),
    ".jsx": (".tsx",),
    ".mjs": (".mts",),
    ".cjs": (".cts",),
}
_PROBE_EXTS_TS = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")
_PROBE_EXTS_JS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".d.ts", ".mts", ".cts")
_CONDITIONS = ("import", "module", "default", "require", "node", "types")
_BUILD_DIRS = ("dist", "build", "lib", "out")
_ALIAS_LOOKING = re.compile(r"^(@/|~/|~$|\$lib|#)")


def _strip_jsonc(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _export_targets(val: Any) -> list[str]:
    if isinstance(val, str):
        return [val]
    if isinstance(val, list):
        return [t for v in val for t in _export_targets(v)]
    if isinstance(val, dict):
        out: list[str] = []
        for key in _CONDITIONS:
            if key in val:
                out.extend(_export_targets(val[key]))
        return out
    return []


class JsResolver:
    def __init__(self, scan: Scan, code: FileLookup, allf: FileLookup, res: Resolved) -> None:
        self.scan = scan
        self.code = code
        self.allf = allf
        self.res = res
        self.pkg_json: dict[str, dict[str, Any]] = {}  # dir -> package.json content
        self.pkgs: dict[str, str] = {}  # package name -> dir
        self.cfgs: dict[str, dict[str, Any]] = {}  # dir -> tsconfig summary
        self._cfg_cache: dict[str, dict[str, Any] | None] = {}
        self._load_package_jsons()
        for rel in scan.tsconfigs:
            cfg = self._load_tsconfig(rel, 0)
            if cfg is not None:
                self.cfgs[_dirname(rel)] = cfg

    # -- config loading
    def _read_json(self, rel: str, jsonc: bool = False) -> Any:
        path = os.path.join(self.scan.root, *rel.split("/"))
        try:
            with open(path, encoding="utf-8-sig") as fh:
                text = fh.read()
            return json.loads(_strip_jsonc(text) if jsonc else text)
        except (OSError, ValueError) as e:
            self.res.warnings.append(f"could not read {rel}: {e}")
            return None

    def _load_package_jsons(self) -> None:
        for rel in self.scan.package_jsons:
            data = self._read_json(rel)
            if not isinstance(data, dict):
                continue
            d = _dirname(rel)
            self.pkg_json[d] = data
            name = data.get("name")
            if isinstance(name, str) and name:
                self.pkgs.setdefault(name, d)

    def _load_tsconfig(self, rel: str, depth: int) -> dict[str, Any] | None:
        data = self._read_json(rel, jsonc=True)
        if not isinstance(data, dict):
            return None
        d = _dirname(rel)
        merged: dict[str, Any] = {"baseUrl": None, "paths": {}, "paths_dir": d}
        ext = data.get("extends")
        for e in ext if isinstance(ext, list) else ([ext] if isinstance(ext, str) else []):
            if depth >= 5:
                self.res.warnings.append(f"{rel}: tsconfig extends chain deeper than 5, rest ignored")
                break
            if not (e.startswith(".") or e.startswith("/")):
                self.res.warnings.append(f"{rel}: extends package {e!r} is not followed (paths/baseUrl from it are unknown)")
                continue
            target = posixpath.normpath(_join(d, e))
            cand = target if target.endswith(".json") else target + ".json"
            found = self.allf.get(cand) or self.allf.get(target)
            if found is None:
                self.res.warnings.append(f"{rel}: extends {e!r} not found inside the project")
                continue
            base = self._load_tsconfig(found, depth + 1)
            if base is not None:
                merged = base
        co = data.get("compilerOptions")
        if isinstance(co, dict):
            bu = co.get("baseUrl")
            if isinstance(bu, str):
                merged["baseUrl"] = posixpath.normpath(_join(d, bu))
                if merged["baseUrl"] == ".":
                    merged["baseUrl"] = ""
            paths = co.get("paths")
            if isinstance(paths, dict):
                merged["paths"] = {k: [t for t in v if isinstance(t, str)] for k, v in paths.items() if isinstance(v, list)}
                merged["paths_dir"] = d
        return merged

    def config_for(self, importer: str) -> dict[str, Any] | None:
        d = _dirname(importer)
        start = d
        if start in self._cfg_cache:
            return self._cfg_cache[start]
        found: dict[str, Any] | None = None
        while True:
            if d in self.cfgs:
                found = self.cfgs[d]
                break
            if not d:
                break
            d = _dirname(d)
        self._cfg_cache[start] = found
        return found

    # -- path probing
    def resolve_path(self, p: str, ts_first: bool = True) -> str | None:
        p = p.rstrip("/") if p != "/" else p
        hit = self.code.get(p)
        if hit:
            return hit
        base, ext = posixpath.splitext(p)
        for e2 in _TS_SWAP.get(ext, ()):
            hit = self.code.get(base + e2)
            if hit:
                return hit
        for e in _PROBE_EXTS_TS if ts_first else _PROBE_EXTS_JS:
            hit = self.code.get(p + e)
            if hit:
                return hit
        pj = self.pkg_json.get(p)
        if pj is not None and p:
            entry = self.pkg_entry(p)
            if entry:
                return entry
        for e in _PROBE_EXTS_TS if ts_first else _PROBE_EXTS_JS:
            hit = self.code.get(_join(p, "index" + e))
            if hit:
                return hit
        return None

    def _try_target(self, pkgdir: str, target: str) -> str | None:
        t = posixpath.normpath(_join(pkgdir, target))
        hit = self._probe_no_pkg(t)
        if hit:
            return hit
        parts = target.lstrip("./").split("/")
        if parts and parts[0] in _BUILD_DIRS:
            alt = _join(pkgdir, "src/" + "/".join(parts[1:]))
            return self._probe_no_pkg(posixpath.normpath(alt))
        return None

    def _probe_no_pkg(self, p: str) -> str | None:
        hit = self.code.get(p)
        if hit:
            return hit
        base, ext = posixpath.splitext(p)
        for e2 in _TS_SWAP.get(ext, ()):
            hit = self.code.get(base + e2)
            if hit:
                return hit
        if ext in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"):
            return None
        for e in _PROBE_EXTS_TS:
            hit = self.code.get(p + e)
            if hit:
                return hit
        for e in _PROBE_EXTS_TS:
            hit = self.code.get(_join(p, "index" + e))
            if hit:
                return hit
        return None

    def package_targets(self, pkgdir: str) -> list[str]:
        pj = self.pkg_json.get(pkgdir, {})
        out: list[str] = []
        exports = pj.get("exports")
        if isinstance(exports, dict) and any(k.startswith(".") for k in exports):
            out.extend(_export_targets(exports.get(".")))
        elif exports is not None:
            out.extend(_export_targets(exports))
        for key in ("module", "main", "types", "source"):
            v = pj.get(key)
            if isinstance(v, str):
                out.append(v)
        return out

    def pkg_entry(self, pkgdir: str) -> str | None:
        for t in self.package_targets(pkgdir):
            hit = self._try_target(pkgdir, t)
            if hit:
                return hit
        for guess in ("src/index", "index"):
            hit = self._probe_no_pkg(_join(pkgdir, guess))
            if hit:
                return hit
        return None

    def _workspace(self, spec: str) -> tuple[bool, str | None]:
        best: str | None = None
        for name in self.pkgs:
            if (spec == name or spec.startswith(name + "/")) and (best is None or len(name) > len(best)):
                best = name
        if best is None:
            return False, None
        d = self.pkgs[best]
        sub = spec[len(best) + 1 :]
        if not sub:
            return True, self.pkg_entry(d)
        exports = self.pkg_json.get(d, {}).get("exports")
        if isinstance(exports, dict):
            key = "./" + sub
            if key in exports:
                for t in _export_targets(exports[key]):
                    hit = self._try_target(d, t)
                    if hit:
                        return True, hit
            for k, v in exports.items():
                if k.endswith("*") and key.startswith(k[:-1]):
                    star = key[len(k) - 1 :]
                    for t in _export_targets(v):
                        hit = self._try_target(d, t.replace("*", star))
                        if hit:
                            return True, hit
        hit = self.resolve_path(_join(d, sub)) or self.resolve_path(_join(d, "src/" + sub))
        return True, hit

    def _alias(self, importer: str, spec: str) -> tuple[bool, str | None]:
        cfg = self.config_for(importer)
        if cfg is None:
            return False, None
        paths: dict[str, list[str]] = cfg["paths"]
        base = cfg["baseUrl"] if cfg["baseUrl"] is not None else cfg["paths_dir"]
        best: tuple[int, str, str] | None = None
        for pat in paths:
            if "*" in pat:
                pre, _, suf = pat.partition("*")
                if spec.startswith(pre) and spec.endswith(suf) and len(spec) >= len(pre) + len(suf):
                    if best is None or len(pre) > best[0]:
                        best = (len(pre), pat, spec[len(pre) : len(spec) - len(suf)])
            elif pat == spec:
                best = (10**6, pat, "")
        if best is not None:
            _, pat, star = best
            for tgt in paths[pat]:
                p = posixpath.normpath(_join(base, tgt.replace("*", star)))
                hit = self.resolve_path(p)
                if hit:
                    return True, hit
            return True, None
        if cfg["baseUrl"] is not None:
            hit = self.resolve_path(posixpath.normpath(_join(cfg["baseUrl"], spec)))
            if hit:
                return True, hit
        return False, None

    # -- main entry
    def resolve_spec(self, importer: str, spec: str) -> tuple[str | None, str]:
        """Return (file, status); status in ok|asset|external|unresolved:<why>."""
        if "?" in spec and not spec.startswith("#"):
            spec = spec.split("?", 1)[0]  # bundler query strings such as ./a.js?raw
        if spec.startswith(("./", "../")) or spec in (".", ".."):
            p = posixpath.normpath(_join(_dirname(importer), spec))
            if p == ".":
                p = ""
            if p.startswith(".."):
                return None, "unresolved:path escapes the project root"
            hit = self.resolve_path(p)
            if hit:
                return hit, "ok"
            if self.allf.get(p):
                return None, "asset"
            return None, "unresolved:relative path matches no file"
        if spec.startswith("/"):
            return None, "unresolved:absolute path specifier"
        if spec.startswith("node:") or spec.startswith("data:") or "://" in spec:
            return None, "external"
        if spec.startswith("#"):
            return None, "unresolved:package.json 'imports' (#) maps are not supported"
        matched, hit = self._alias(importer, spec)
        if matched:
            if hit:
                return hit, "ok"
            return None, "unresolved:tsconfig alias matched but no target file in project"
        matched, hit = self._workspace(spec)
        if matched:
            if hit:
                return hit, "ok"
            return None, "unresolved:workspace package matched but entry file not found in project"
        if _ALIAS_LOOKING.match(spec):
            return None, "unresolved:looks like a path alias but no tsconfig paths entry matches"
        return None, "external"

    def resolve(self, importer: str, parsed: dict[str, Any], res: Resolved) -> None:
        for ref in parsed["refs"]:
            spec = ref["s"]
            f, status = self.resolve_spec(importer, spec)
            if status == "ok" and f is not None:
                flags = tuple(ref["fl"])
                if ref["k"] == "dyn":
                    flags = (*flags, "dynamic")
                if ref.get("ns"):
                    flags = (*flags, "ns")
                rename = tuple(sorted(ref.get("rename", {}).items()))
                res.edges.append(
                    Edge(importer, f, ref["ln"], ref["k"], tuple(ref["names"]), bool(ref.get("star")), flags, rename)
                )
            elif status == "asset":
                res.assets += 1
            elif status == "external":
                if spec.startswith("node:") or spec in _NODE_BUILTINS:
                    res.stdlib[spec.removeprefix("node:")] += 1
                else:
                    parts = spec.split("/")
                    name = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
                    res.external[name] += 1
            else:
                res.unresolved.append(
                    {"file": importer, "line": ref["ln"], "spec": spec, "reason": status.removeprefix("unresolved:")}
                )


_NODE_BUILTINS = frozenset(
    "assert async_hooks buffer child_process cluster console constants crypto dgram dns domain events fs "
    "http http2 https inspector module net os path perf_hooks process punycode querystring readline repl "
    "stream string_decoder sys timers tls tty url util v8 vm wasi worker_threads zlib".split()
)  # fmt: skip

JS_EXT_SET = frozenset(JS_EXTS)
