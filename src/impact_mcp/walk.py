"""Project file discovery: ignore dirs, .gitignore, symlink/junction loops."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

PY_EXTS = (".py",)
JS_EXTS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
CODE_EXTS = PY_EXTS + JS_EXTS

ALWAYS_IGNORED_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env", "virtualenv",
        "dist", "build", "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", "site-packages", ".next", ".nuxt", ".turbo", ".cache",
        "coverage", ".idea", ".vscode", ".eggs",
    }
)  # fmt: skip

MAX_FILE_BYTES = 2_000_000
_ROOT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg")


def _glob_to_regex(pat: str) -> str:
    out: list[str] = []
    i = 0
    n = len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pat[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pat.find("]", i + 2)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pat[i + 1 : j]
                if body[:1] == "!":
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(pat[i]))
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


@dataclass(frozen=True)
class _Rule:
    rx: re.Pattern[str]
    negate: bool
    dir_only: bool


def parse_gitignore(text: str) -> tuple[_Rule, ...]:
    rules: list[_Rule] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip() or line.startswith("#"):
            continue
        line = line.rstrip(" ") if not line.endswith("\\ ") else line
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        anchored = "/" in line
        line = line.lstrip("/")
        body = _glob_to_regex(line)
        rx = re.compile(("^" if anchored else "^(?:.*/)?") + body + "$")
        rules.append(_Rule(rx, negate, dir_only))
    return tuple(rules)


@dataclass
class Scan:
    root: str
    files: dict[str, tuple[int, int]] = field(default_factory=dict)  # code files
    all_files: set[str] = field(default_factory=set)
    tsconfigs: list[str] = field(default_factory=list)
    package_jsons: list[str] = field(default_factory=list)
    pyproject_dirs: list[str] = field(default_factory=list)  # dirs with pyproject/setup.*
    pyprojects: list[str] = field(default_factory=list)  # pyproject.toml rel paths
    pnpm_workspace: list[str] = field(default_factory=list)
    symlinks_skipped: int = 0
    gitignored: int = 0
    ignored_dirs: int = 0
    too_large: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


def _rel_join(rel: str, name: str) -> str:
    return f"{rel}/{name}" if rel else name


def scan_tree(root: str) -> Scan:
    root = os.path.realpath(root)
    scan = Scan(root=root)
    seen_real: set[str] = set()
    # stack of (abs dir, rel dir, ((base_rel, rules), ...))
    stack: list[tuple[str, str, tuple[tuple[str, tuple[_Rule, ...]], ...]]] = [(root, "", ())]
    while stack:
        d, rel, ruleset = stack.pop()
        real = os.path.normcase(os.path.realpath(d))
        if real in seen_real:
            scan.symlinks_skipped += 1
            continue
        seen_real.add(real)
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError as e:
            scan.unreadable.append(f"{rel or '.'}: {e.strerror or e}")
            continue
        gi = next((e for e in entries if e.name == ".gitignore" and e.is_file()), None)
        if gi is not None:
            try:
                with open(gi.path, encoding="utf-8", errors="replace") as fh:
                    rules = parse_gitignore(fh.read())
                if rules:
                    ruleset = (*ruleset, (rel, rules))
            except OSError as e:
                scan.unreadable.append(f"{_rel_join(rel, '.gitignore')}: {e.strerror or e}")

        def ignored(name: str, is_dir: bool, _rs: tuple[tuple[str, tuple[_Rule, ...]], ...] = ruleset, _rel: str = rel) -> bool:
            full = _rel_join(_rel, name)
            res = False
            for base, rules in _rs:
                sub = full[len(base) + 1 :] if base else full
                for r in rules:
                    if r.dir_only and not is_dir:
                        continue
                    if r.rx.match(sub):
                        res = not r.negate
            return res

        for ent in entries:
            name = ent.name
            erel = _rel_join(rel, name)
            try:
                is_link = ent.is_symlink() or bool(getattr(ent, "is_junction", lambda: False)())
                is_dir = ent.is_dir(follow_symlinks=False)
                is_file = ent.is_file(follow_symlinks=False)
            except OSError as ex:
                scan.unreadable.append(f"{erel}: {ex.strerror or ex}")
                continue
            if is_link:
                scan.symlinks_skipped += 1
                continue
            if is_dir:
                if name in ALWAYS_IGNORED_DIRS or name.endswith(".egg-info"):
                    scan.ignored_dirs += 1
                    continue
                if ignored(name, True):
                    scan.ignored_dirs += 1
                    continue
                stack.append((ent.path, erel, ruleset))
                continue
            if not is_file:
                continue
            if ignored(name, False):
                scan.gitignored += 1
                continue
            scan.all_files.add(erel)
            if name in ("tsconfig.json", "jsconfig.json"):
                scan.tsconfigs.append(erel)
            elif name == "package.json":
                scan.package_jsons.append(erel)
            elif name == "pnpm-workspace.yaml":
                scan.pnpm_workspace.append(erel)
            if name in _ROOT_MARKERS:
                scan.pyproject_dirs.append(rel)
            if name == "pyproject.toml":
                scan.pyprojects.append(erel)
            if name.endswith(CODE_EXTS):
                try:
                    st = ent.stat(follow_symlinks=False)
                except OSError as ex:
                    scan.unreadable.append(f"{erel}: {ex.strerror or ex}")
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    scan.too_large.append(erel)
                    continue
                scan.files[erel] = (st.st_mtime_ns, st.st_size)
    return scan
