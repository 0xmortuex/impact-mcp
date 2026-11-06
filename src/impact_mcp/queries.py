"""The seven queries. Each takes a project root and returns plain text for an agent."""

from __future__ import annotations

from collections import Counter, defaultdict

from .graph import Reach, reach, resolve_target, sccs, shortest_cycle, symbol_known, symbol_users
from .project import ImpactError, Project, build_project
from .resolve import Edge

SOFT_FLAGS = ("type_only", "lazy", "conditional", "dynamic")


def confidence(proj: Project) -> str:
    n_py = sum(1 for f in proj.scan.files if f.endswith(".py"))
    n_js = len(proj.scan.files) - n_py
    parts = [
        f"{len(proj.scan.files)} files ({n_py} py, {n_js} js/ts)",
        f"{len(proj.edges)} resolved internal edges",
        f"external packages not tracked: {len(proj.external)} (+{len(proj.stdlib)} stdlib/builtin)",
        f"UNRESOLVED project-looking imports: {len(proj.unresolved)}",
        f"dynamic non-literal imports: {len(proj.dynamic)}",
        f"syntax-error files: {len(proj.syntax_errors)}",
    ]
    lines = ["Graph confidence: " + " | ".join(parts)]
    notes = []
    if proj.syntax_errors:
        notes.append("files that failed to parse have NO outgoing edges: " + ", ".join(e["file"] for e in proj.syntax_errors[:5]))
    if proj.ambiguous:
        notes.append(f"{proj.ambiguous} Python imports matched several files (nearest chosen)")
    if proj.case_mismatches:
        notes.append(f"{proj.case_mismatches} imports matched only case-insensitively")
    notes.extend(proj.warnings[:5])
    if len(proj.warnings) > 5:
        notes.append(f"... {len(proj.warnings) - 5} more warnings")
    lines.extend("  note: " + n for n in notes)
    return "\n".join(lines)


def _tag(proj: Project, f: str) -> str:
    t = []
    if f in proj.tests:
        t.append("test")
    if f in proj.entry_points:
        t.append("entry")
    return f" [{', '.join(t)}]" if t else ""


def _edge_desc(e: Edge) -> str:
    bits = []
    if e.names:
        bits.append(", ".join(e.names[:6]) + ("..." if len(e.names) > 6 else ""))
    if e.star:
        bits.append("*" if not e.names else "+ whole-module/wildcard")
    kind = {"from": "from-import", "import": "import", "export": "re-export", "require": "require()", "dyn": "import()"}[e.kind]
    soft = [f for f in e.flags if f in SOFT_FLAGS]
    s = f"{kind}" + (f" {{{'; '.join(bits)}}}" if bits else "")
    if soft:
        s += " (" + ", ".join(soft) + ")"
    return s


def _load(root: str) -> Project:
    return build_project(root)


def _fmt_levels(proj: Project, found: dict[str, Reach], direction: str, limit: int) -> list[str]:
    by_depth: dict[int, list[str]] = defaultdict(list)
    for f, r in found.items():
        by_depth[r.depth].append(f)
    lines: list[str] = []
    shown = 0
    for d in sorted(by_depth):
        files = sorted(by_depth[d])
        lines.append(f"depth {d} ({len(files)}):")
        for f in files:
            if shown >= limit:
                break
            r = found[f]
            if d == 1:
                where = f"line {r.edge.line}: {_edge_desc(r.edge)}"
            else:
                where = f"via {r.via}"
            lines.append(f"  {f}{_tag(proj, f)}  -- {where}")
            shown += 1
        if shown >= limit:
            break
    hidden = len(found) - shown
    if hidden > 0:
        lines.append(f"... {hidden} more not shown (raise `limit` to see them)")
    return lines


def _hidden_hints(proj: Project, target: str) -> list[str]:
    stem = target.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if stem in ("__init__", "index"):
        parts = target.split("/")
        stem = parts[-2] if len(parts) > 1 else stem
    hits = [
        u for u in proj.unresolved
        if u["spec"].replace("/", ".").split(" import ")[0].rstrip(".").rsplit(".", 1)[-1] == stem
    ]  # fmt: skip
    out = []
    if hits:
        out.append(f"{len(hits)} unresolved import(s) end in '{stem}' and might really point here:")
        out.extend(f"  {u['file']}:{u['line']}  {u['spec']}  ({u['reason']})" for u in hits[:5])
    if proj.dynamic:
        out.append(
            f"{len(proj.dynamic)} dynamic non-literal import/require call(s) exist in the project; "
            "any could load this file at runtime (see project_map)."
        )
    return out


def dependents(root: str, path_or_module: str, depth: int = 1, limit: int = 100) -> str:
    proj = _load(root)
    target = resolve_target(proj, path_or_module)
    depth = max(1, min(depth, 50))
    found = reach(proj, target, "in", depth)
    head = f"Dependents of {target}{_tag(proj, target)} (importers, transitive up to depth {depth}): {len(found)} file(s)"
    lines = [head]
    if found:
        lines.extend(_fmt_levels(proj, found, "in", limit))
    else:
        lines.append("Nothing in the project imports this file." + (" It is an entry point." if target in proj.entry_points else ""))
    lines.extend(_hidden_hints(proj, target))
    lines.append(confidence(proj))
    return "\n".join(lines)


def dependencies(root: str, path_or_module: str, depth: int = 1, limit: int = 100) -> str:
    proj = _load(root)
    target = resolve_target(proj, path_or_module)
    depth = max(1, min(depth, 50))
    found = reach(proj, target, "out", depth)
    lines = [f"Dependencies of {target}{_tag(proj, target)} (imports, transitive up to depth {depth}): {len(found)} file(s)"]
    if found:
        lines.extend(_fmt_levels(proj, found, "out", limit))
    else:
        lines.append("This file imports nothing from inside the project.")
    mine_unres = [u for u in proj.unresolved if u["file"] == target]
    if mine_unres:
        lines.append(f"UNRESOLVED imports in {target} (project-looking but no file found):")
        lines.extend(f"  line {u['line']}: {u['spec']}  ({u['reason']})" for u in mine_unres)
    mine_dyn = [d for d in proj.dynamic if d["file"] == target]
    if mine_dyn:
        lines.append(f"DYNAMIC imports in {target} (target unknown, not in the graph):")
        lines.extend(f"  line {d['ln']}: {d['what']}" for d in mine_dyn)
    if target in proj.parsed and proj.parsed[target].get("err"):
        lines.append(f"{target} failed to parse ({proj.parsed[target]['err']}); its imports are unknown.")
    lines.append(confidence(proj))
    return "\n".join(lines)


def _risk(proj: Project, n_trans: int, entries: list[str], in_cycle: bool, n_direct: int) -> tuple[str, list[str]]:
    total = max(1, len(proj.scan.files))
    share = n_trans / total
    reasons = [f"{n_trans} transitive dependent file(s) = {share:.0%} of {total} files"]
    level = "LOW"
    if n_trans >= 25 or share >= 0.15:
        level = "HIGH"
    elif n_trans >= 5 or share >= 0.05:
        level = "MEDIUM"
    if entries:
        reasons.append(f"{len(entries)} entry point(s) are downstream (a break can surface at startup)")
        if level == "LOW" and n_trans >= 2:
            level = "MEDIUM"
    if in_cycle:
        reasons.append("file is part of an import cycle (changes can loop back)")
    if n_direct == 0:
        reasons.append("nothing imports it (check entry/dynamic use before deleting)")
    return level, reasons


def impact(root: str, path: str, symbol: str | None = None, limit: int = 40) -> str:
    proj = _load(root)
    target = resolve_target(proj, path)
    lines: list[str] = []
    if symbol:
        known = symbol_known(proj, target, symbol)
        uses = symbol_users(proj, target, symbol)
        seeds = sorted({u.file for u in uses})
        closure = reach(proj, seeds, "in", 50) if seeds else {}
        for s in seeds:
            closure.pop(s, None)
        all_dep = set(seeds) | set(closure)
        lines.append(f"Impact of changing `{symbol}` in {target}")
        if not known:
            lines.append(
                f"WARNING: `{symbol}` was not found among {target}'s top-level definitions/exports; "
                "it may be defined dynamically, or the name is misspelled."
            )
        lines.append(f"Direct users of the symbol: {len(seeds)} file(s) (via from-imports, re-exports, attribute access; best effort)")
        for u in sorted(uses, key=lambda u: (u.file, u.line))[:limit]:
            via = f" via re-export chain {' -> '.join(u.chain)}" if u.chain else ""
            lines.append(f"  {u.file}:{u.line}  {u.how}{via}{_tag(proj, u.file)}")
        lines.append(
            f"Files that import those users (upper bound; they only break if the users' own API changes): {len(closure)}"
        )
    else:
        closure = reach(proj, target, "in", 50)
        all_dep = set(closure)
        direct = [f for f, r in closure.items() if r.depth == 1]
        lines.append(f"Impact of changing {target}{_tag(proj, target)}")
        lines.append(f"Direct dependents: {len(direct)}; transitive: {len(closure)}; max depth: {max((r.depth for r in closure.values()), default=0)}")
        for f in sorted(direct)[:limit]:
            lines.append(f"  {f}{_tag(proj, f)}  -- line {closure[f].edge.line}: {_edge_desc(closure[f].edge)}")
        if len(direct) > limit:
            lines.append(f"  ... {len(direct) - limit} more direct dependents")
    tests = sorted(f for f in all_dep if f in proj.tests)
    if target in proj.tests:
        tests = [target, *tests]
    lines.append(f"Tests that reach it through imports ({len(tests)}) - a superset to run, not proof of coverage:")
    lines.extend(f"  {t}" for t in tests[:limit])
    if len(tests) > limit:
        lines.append(f"  ... {len(tests) - limit} more")
    if not tests:
        lines.append("  none found (no test file imports this module, directly or transitively)")
    entries = sorted(f for f in all_dep if f in proj.entry_points)
    if target in proj.entry_points:
        entries = [target, *entries]
    lines.append(f"Entry points affected ({len(entries)}):")
    lines.extend(f"  {f}  ({proj.entry_points[f]})" for f in entries[:limit])
    in_cycle = any(target in c for c in sccs(proj))
    n_direct = len(proj.inn.get(target, ()))
    level, reasons = _risk(proj, len(all_dep), entries, in_cycle, n_direct)
    lines.append(f"Risk: {level}")
    lines.extend(f"  - {r}" for r in reasons)
    lines.extend(_hidden_hints(proj, target))
    lines.append(confidence(proj))
    return "\n".join(lines)


def find_cycles(root: str, limit: int = 10) -> str:
    proj = _load(root)
    comps = sorted(sccs(proj), key=lambda c: (-len(c), c[0]))
    lines = [f"Import cycles (strongly connected components): {len(comps)}"]
    for comp in comps[:limit]:
        cyc = shortest_cycle(proj, comp)
        path = " -> ".join([cyc[0].src, *(e.dst for e in cyc)])
        lines.append(f"- {len(comp)} files in the component; shortest cycle ({len(cyc)} edges):")
        lines.append(f"    {path}")
        soft = sorted({f for e in cyc for f in e.flags if f in SOFT_FLAGS})
        if soft:
            lines.append(f"    cycle includes {', '.join(soft)} import(s): it may be harmless at runtime (lazy, guarded or type-only)")
        for e in cyc:
            lines.append(f"      {e.src}:{e.line} -> {e.dst}  {_edge_desc(e)}")
        if len(comp) > len(cyc):
            lines.append(f"    other members: {', '.join(f for f in comp if f not in {e.src for e in cyc})[:300]}")
    if len(comps) > limit:
        lines.append(f"... {len(comps) - limit} more components (raise `limit`)")
    if not comps:
        lines.append("No cycles found among resolved edges.")
    lines.append(confidence(proj))
    return "\n".join(lines)


_NOT_ORPHAN_NAMES = ("__init__.py", "conftest.py", "setup.py")


def orphans_and_hubs(root: str, limit: int = 15) -> str:
    proj = _load(root)
    orphans = [
        f for f in proj.files
        if f not in proj.inn and f not in proj.entry_points and f not in proj.tests
        and f.rsplit("/", 1)[-1] not in _NOT_ORPHAN_NAMES and not f.endswith(".d.ts")
    ]  # fmt: skip
    lines = [f"Dead-code candidates: {len(orphans)} file(s) that nothing in the project imports (and are not entry points, tests, __init__.py or configs)"]
    lines.extend(f"  {f}  ({len(proj.out.get(f, ()))} imports)" for f in orphans[:limit])
    if len(orphans) > limit:
        lines.append(f"  ... {len(orphans) - limit} more (raise `limit`)")
    lines.append(
        "Caveats: a file may still be used by framework conventions (file-system routing, plugin discovery, "
        "settings strings), by dynamic imports, by non-Python/JS tooling, or by external consumers of a library. "
        f"{len(proj.dynamic)} dynamic import(s) and {len(proj.unresolved)} unresolved import(s) in this project could hide real users."
    )
    indeg = Counter({f: len({e.src for e in es}) for f, es in proj.inn.items()})
    lines.append("")
    lines.append("Most depended-on files (direct importers / transitive dependents):")
    for f, n in indeg.most_common(limit):
        trans = len(reach(proj, f, "in", 50))
        lines.append(f"  {f}{_tag(proj, f)}  {n} / {trans}")
    if not indeg:
        lines.append("  (no internal imports found)")
    lines.append(confidence(proj))
    return "\n".join(lines)


def symbol_users_report(root: str, module: str, symbol: str, limit: int = 100) -> str:
    proj = _load(root)
    target = resolve_target(proj, module)
    uses = symbol_users(proj, target, symbol)
    lines = [f"Users of `{symbol}` from {target} (best effort, static):"]
    if not symbol_known(proj, target, symbol):
        lines.append(
            f"WARNING: `{symbol}` is not among {target}'s top-level names/exports; results below only show who imports that name."
        )
    for u in sorted(uses, key=lambda u: (u.file, u.line))[:limit]:
        via = f"  via re-export chain: {' -> '.join(u.chain)}" if u.chain else ""
        lines.append(f"  {u.file}:{u.line}  {u.how}{via}{_tag(proj, u.file)}")
    if len(uses) > limit:
        lines.append(f"  ... {len(uses) - limit} more")
    if not uses:
        lines.append("  no static users found.")
    lines.append(
        "Method: counts from-imports/named imports, attribute access on the imported module alias "
        "(`mod.name`), wildcard imports, and follows re-exports (`__init__.py`, `__all__`, `export {x} from`). "
        "Uses through getattr(), aliases passed around as values, or dynamic imports are invisible."
    )
    lines.append(confidence(proj))
    return "\n".join(lines)


def project_map(root: str) -> str:
    proj = _load(root)
    langs = Counter("python" if f.endswith(".py") else ("typescript" if f.endswith((".ts", ".tsx", ".mts", ".cts")) else "javascript") for f in proj.scan.files)
    lines = [f"Project {proj.root}"]
    lines.append("Languages: " + (", ".join(f"{k} {v}" for k, v in langs.most_common()) or "none found"))
    lines.append(f"Modules (code files): {len(proj.scan.files)}; test files: {len(proj.tests)}")
    top = Counter(f.split("/")[0] if "/" in f else "." for f in proj.scan.files)
    lines.append("Top-level dirs: " + ", ".join(f"{k}({v})" for k, v in top.most_common(10)))
    if proj.packages:
        lines.append(f"Python packages ({len(proj.packages)}): " + ", ".join(proj.packages[:15]) + (" ..." if len(proj.packages) > 15 else ""))
    comps = sccs(proj)
    lines.append(f"Import cycles: {len(comps)}")
    lines.append(f"Entry points (guessed, {len(proj.entry_points)}):")
    for f, why in sorted(proj.entry_points.items())[:20]:
        lines.append(f"  {f}  ({why})")
    if not proj.entry_points:
        lines.append("  none detected (no console scripts, __main__.py, bin/main in package.json, or main-guard scripts)")
    if proj.unresolved:
        lines.append(f"Unresolved imports ({len(proj.unresolved)}), first 8:")
        lines.extend(f"  {u['file']}:{u['line']}  {u['spec']}  ({u['reason']})" for u in proj.unresolved[:8])
    if proj.dynamic:
        lines.append(f"Dynamic non-literal imports ({len(proj.dynamic)}), first 8:")
        lines.extend(f"  {d['file']}:{d['ln']}  {d['what']}" for d in proj.dynamic[:8])
    if proj.syntax_errors:
        lines.append(f"Files that failed to parse ({len(proj.syntax_errors)}):")
        lines.extend(f"  {e['file']}  {e['error']}" for e in proj.syntax_errors[:8])
    if proj.external:
        lines.append("Most used external packages: " + ", ".join(f"{k}({v})" for k, v in proj.external.most_common(8)))
    lines.append(
        f"Scan: {proj.scan.gitignored} .gitignore'd files and {proj.scan.ignored_dirs} ignored dirs skipped; "
        f"parsed {proj.cache_misses} files, {proj.cache_hits} from cache"
    )
    lines.append(confidence(proj))
    return "\n".join(lines)


__all__ = [
    "ImpactError", "dependencies", "dependents", "find_cycles", "impact",
    "orphans_and_hubs", "project_map", "symbol_users_report",
]  # fmt: skip
