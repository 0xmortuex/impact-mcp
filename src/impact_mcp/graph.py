"""Graph algorithms over a built Project: traversal, cycles, symbol users."""

from __future__ import annotations

import difflib
import os
from collections import deque
from dataclasses import dataclass

from .project import ImpactError, Project
from .resolve import Edge


def normalize_query(proj: Project, query: str) -> str:
    q = query.strip().strip('"').strip("'")
    if not q:
        raise ImpactError("empty path_or_module; pass a project-relative path (src/app/util.py) or a dotted module (app.util)")
    if os.path.isabs(q) or (len(q) > 1 and q[1] == ":"):
        real = os.path.realpath(q)
        root = proj.root
        if os.path.normcase(real) != os.path.normcase(root) and not os.path.normcase(real).startswith(os.path.normcase(root) + os.sep):
            raise ImpactError(f"{query!r} is outside the sandbox directory {root}")
        q = os.path.relpath(real, root)
    q = q.replace("\\", "/")
    while q.startswith("./"):
        q = q[2:]
    return q


def resolve_target(proj: Project, query: str) -> str:
    """Map a path / module name / extensionless JS path to one scanned file."""
    q = normalize_query(proj, query)
    files = proj.scan.files
    if q.startswith("../") or q == "..":
        raise ImpactError(f"{query!r} escapes the sandbox directory {proj.root}")
    if q in files:
        return q
    lower = {f.lower(): f for f in files}
    if q.lower() in lower:
        return lower[q.lower()]
    for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"):
        if q + ext in files:
            return q + ext
    for idx in ("__init__.py", "index.ts", "index.tsx", "index.js", "index.jsx"):
        if f"{q}/{idx}" in files:
            return f"{q}/{idx}"
    if proj.py is not None and q in proj.py.names:
        cands = proj.py.names[q]
        if len(cands) == 1:
            return cands[0]
        raise ImpactError(f"module {q!r} is ambiguous across source roots: {', '.join(sorted(cands))}; pass a path")
    suffix = [f for f in files if f.endswith("/" + q) or f.rsplit(".", 1)[0].endswith("/" + q)]
    if len(suffix) == 1:
        return suffix[0]
    if len(suffix) > 1:
        raise ImpactError(f"{query!r} matches several files: {', '.join(sorted(suffix)[:8])}; pass a longer path")
    near = difflib.get_close_matches(q, list(files), n=5, cutoff=0.5)
    hint = f" Did you mean: {', '.join(near)}?" if near else ""
    raise ImpactError(
        f"no scanned file or module matches {query!r}.{hint} "
        "(only .py/.js/.jsx/.ts/.tsx/.mjs/.cjs/.mts/.cts files outside ignored dirs are in the graph)"
    )


@dataclass
class Reach:
    depth: int
    via: str  # the previously visited file this one connects through
    edge: Edge


def reach(proj: Project, start: str | list[str], direction: str, max_depth: int) -> dict[str, Reach]:
    """BFS. direction 'in' = dependents (who imports me), 'out' = dependencies."""
    starts = [start] if isinstance(start, str) else list(start)
    adj = proj.inn if direction == "in" else proj.out
    seen: dict[str, Reach] = {}
    q: deque[tuple[str, int]] = deque((s, 0) for s in starts)
    visited = set(starts)
    while q:
        cur, d = q.popleft()
        if d >= max_depth:
            continue
        for e in adj.get(cur, ()):
            nxt = e.src if direction == "in" else e.dst
            if nxt in visited:
                continue
            visited.add(nxt)
            seen[nxt] = Reach(d + 1, cur, e)
            q.append((nxt, d + 1))
    return seen


def sccs(proj: Project) -> list[list[str]]:
    """Tarjan's algorithm, iterative. Returns components that form a cycle (size>1 or self loop)."""
    nodes = sorted(proj.scan.files)
    succ = {n: sorted({e.dst for e in proj.out.get(n, ())}) for n in nodes}
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on.add(v)
            recursed = False
            nbrs = succ[v]
            while i < len(nbrs):
                w = nbrs[i]
                i += 1
                if w not in index:
                    work.append((v, i))
                    work.append((w, 0))
                    recursed = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if recursed:
                continue
            if low[v] == index[v]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    return out


def shortest_cycle(proj: Project, comp: list[str], max_starts: int = 200) -> list[Edge]:
    """Shortest directed cycle inside a strongly connected component (list of edges)."""
    members = set(comp)
    best: list[Edge] | None = None
    for start in comp[:max_starts]:
        prev: dict[str, Edge] = {}
        q: deque[str] = deque([start])
        seen = {start}
        found: Edge | None = None
        while q and found is None:
            cur = q.popleft()
            for e in proj.out.get(cur, ()):
                if e.dst not in members:
                    continue
                if e.dst == start:
                    found = e
                    break
                if e.dst not in seen:
                    seen.add(e.dst)
                    prev[e.dst] = e
                    q.append(e.dst)
        if found is None:
            continue
        path = [found]
        cur = found.src
        while cur != start:
            e = prev[cur]
            path.append(e)
            cur = e.src
        path.reverse()
        if best is None or len(path) < len(best):
            best = path
            if len(best) == 2:
                break
    assert best is not None
    return best


# ---------------------------------------------------------------- symbol users


@dataclass
class Use:
    file: str
    line: int
    how: str
    chain: tuple[str, ...]  # re-export hops from the target to the file that was directly imported


def _provides(proj: Project, f: str, sym: str) -> bool:
    p = proj.parsed.get(f, {})
    if p.get("lang") == "py":
        names = p.get("all")
        if names is not None:
            return sym in names or (p.get("all_partial", False) and sym in p.get("defs", ()))
        return sym in p.get("defs", ()) and not sym.startswith("_")
    return sym in p.get("exports", ())


def symbol_known(proj: Project, f: str, sym: str) -> bool:
    p = proj.parsed.get(f, {})
    if p.get("lang") == "py":
        return sym in p.get("defs", ()) or sym in (p.get("all") or ())
    return sym in p.get("exports", ()) or any(sym in e.names and e.reexport for e in proj.out.get(f, ()))


def symbol_users(proj: Project, target: str, sym: str) -> list[Use]:
    uses: list[Use] = []
    seen: set[tuple[str, str]] = set()

    def walk(tgt: str, name: str, chain: tuple[str, ...]) -> None:
        if (tgt, name) in seen:
            return
        seen.add((tgt, name))
        for e in proj.inn.get(tgt, ()):
            how: str | None = None
            if name in e.names:
                if e.reexport:
                    how = "re-exported by name"
                elif "ns" in e.flags:
                    how = "attribute access on the imported module"
                elif e.kind in ("require", "dyn"):
                    how = f"{e.kind}() destructuring / property access"
                else:
                    how = "imported by name"
            elif e.star:
                if e.reexport:
                    how = "re-exported through `export *`"
                elif e.src.endswith(".py"):
                    if _provides(proj, tgt, name):
                        how = "wildcard import (may use it; not verified)"
                else:
                    how = "namespace/whole-module use not attributable (may use it; not verified)"
            if how is None:
                continue
            uses.append(Use(e.src, e.line, how, chain))
            reexported = False
            new = name
            if e.reexport:
                reexported = name in e.names or e.star
                new = dict(e.rename).get(name, name)
            elif e.src.endswith(".py") and e.kind == "from" and "ns" not in e.flags and (name in e.names or e.star):
                reexported = e.src.endswith("__init__.py") or _provides_all(proj, e.src, name)
            if reexported:
                walk(e.src, new, (*chain, e.src))

    walk(target, sym, ())
    return uses


def _provides_all(proj: Project, f: str, sym: str) -> bool:
    names = proj.parsed.get(f, {}).get("all")
    return names is not None and sym in names
