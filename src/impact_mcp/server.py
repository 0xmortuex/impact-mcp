"""MCP server: read-only import-graph impact analysis for Python and JS/TS projects."""

from __future__ import annotations

import argparse
import functools
import os
from collections.abc import Callable
from typing import Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import queries as q
from .project import ImpactError

mcp = MCPServer(
    "impact",
    instructions=(
        "Static import-graph analysis of ONE project directory (set by --dir; read-only, offline). "
        "Use it BEFORE editing to answer 'what depends on this file/symbol and what could break': "
        "impact(path, symbol?) gives blast radius, tests to run and entry points affected; "
        "dependents / dependencies walk the graph; symbol_users finds who uses one name; "
        "find_cycles and orphans_and_hubs audit structure; project_map is the overview. "
        "Every result ends with a 'Graph confidence' line: unresolved or dynamic imports mean the "
        "graph can miss dependents, so treat results as a lower bound when those counts are non-zero."
    ),
)

_root: str = os.getcwd()

F = TypeVar("F", bound=Callable[..., Any])


def tool(fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ImpactError as e:
            raise ToolError(str(e)) from e

    mcp.tool()(wrapper)
    return fn


@tool
def project_map() -> str:
    """Overview of the project: languages, module count, Python packages, guessed entry points
    (pyproject scripts, __main__.py, package.json bin/main/exports), import-cycle count,
    unresolved/dynamic imports, and syntax-error files. Start here."""
    return q.project_map(_root)


@tool
def dependents(path_or_module: str, depth: int = 3, limit: int = 100) -> str:
    """Who imports this file? Transitive importers grouped by depth, with the import line,
    imported names, and flags (type-only / lazy / conditional). `path_or_module` is a
    project-relative path (src/app/util.py, packages/ui/src/Button.tsx), an extensionless JS
    path, or a dotted Python module (app.util). Depth 1 = direct importers only."""
    return q.dependents(_root, path_or_module, depth, limit)


@tool
def dependencies(path_or_module: str, depth: int = 1, limit: int = 100) -> str:
    """What does this file import from inside the project (transitively up to `depth`)?
    Also lists this file's UNRESOLVED and DYNAMIC imports explicitly."""
    return q.dependencies(_root, path_or_module, depth, limit)


@tool
def impact(path: str, symbol: str | None = None, limit: int = 40) -> str:
    """Blast radius of changing a file (or one symbol in it): direct and transitive dependents,
    the tests that reach it through imports (run these), entry points affected, and a LOW/MEDIUM/HIGH
    risk summary with reasons. With `symbol`, direct users are narrowed to files that import or
    access that name (following re-exports); the rest is an upper bound."""
    return q.impact(_root, path, symbol, limit)


@tool
def find_cycles(limit: int = 10) -> str:
    """Import cycles: strongly connected components (Tarjan), each with its shortest cycle
    spelled out file by file, and whether the cycle is softened by lazy/type-only imports."""
    return q.find_cycles(_root, limit)


@tool
def orphans_and_hubs(limit: int = 15) -> str:
    """Dead-code candidates (files nothing imports that are not entry points, tests, __init__.py
    or configs - with caveats) and the most depended-on files (direct importers / transitive dependents)."""
    return q.orphans_and_hubs(_root, limit)


@tool
def symbol_users(module: str, symbol: str, limit: int = 100) -> str:
    """Who uses `symbol` from `module`? Best effort and labeled as such: from-imports, named
    imports, attribute access on the module alias (mod.name), wildcard imports, and re-export chains
    (__init__.py, __all__, export {x} from). getattr()/dynamic use is invisible."""
    return q.symbol_users_report(_root, module, symbol, limit)


def main() -> None:
    global _root
    ap = argparse.ArgumentParser(prog="impact-mcp", description="Read-only import-graph impact analysis MCP server.")
    ap.add_argument("--dir", default=os.environ.get("IMPACT_MCP_DIR") or os.getcwd(),
                    help="project directory to analyze (the sandbox; default: current directory)")  # fmt: skip
    args = ap.parse_args()
    root = os.path.realpath(args.dir)
    if not os.path.isdir(root):
        ap.error(f"--dir {args.dir!r} is not a directory")
    _root = root
    mcp.run()


if __name__ == "__main__":
    main()
