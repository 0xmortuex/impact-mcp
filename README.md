# impact-mcp

**Ask "what depends on this file, and what breaks if I change it?" and get an answer that tells you how much to trust it.**

<!-- mcp-name: io.github.0xmortuex/impact-mcp -->

impact-mcp is an [MCP](https://modelcontextprotocol.io) server that builds the import graph of a
Python and/or JavaScript/TypeScript project and answers blast-radius questions: who imports this
file, who uses this one symbol, which tests reach it, which entry points are downstream, where the
cycles are, what looks dead.

**What is different from the other code-graph MCP servers** (codegraph-mcp, code-impact-mcp,
ts-review-graph, dep-graph-guard and friends):

- **Zero extra dependencies and fully offline.** Only `mcp`. Python is read with the stdlib `ast`; JS/TS
  with a small hand-written tokenizer. No Node, no TypeScript compiler, no ts-morph, no SQLite, no
  network, nothing executed, nothing written inside your repo.
- **It tells you what it could not see.** Every answer ends with a `Graph confidence` line: resolved
  edges, external packages, **unresolved project-looking imports, dynamic non-literal imports,
  syntax-error files**. Dynamic `importlib` / `import(x)` / `require(x)` is reported as UNRESOLVED, never
  guessed. `impact` also warns when unresolved imports end in the name of the file you are about to change.
- **Python and JS/TS resolution that matches how projects are really laid out:** `src/` layouts,
  namespace packages, `__init__.py` re-exports and `__all__`, `TYPE_CHECKING`, tsconfig `extends` /
  `baseUrl` / `paths`, monorepo workspace packages, `.js` -> `.ts` ESM style imports, `index.ts`
  directories, `export * from`, `import type`.
- **Symbol-level answers that follow re-exports:** `symbol_users("pkg/core.py", "Engine")` also finds the
  file that imports `Engine` from the package `__init__.py` that re-exports it.

## What it looks like

```
> impact path=mort/lexer.py
Impact of changing mort/lexer.py
Direct dependents: 2; transitive: 10; max depth: 4
  mort/lsp.py  -- line 10: from-import {Lexer}
  mortc.py [entry]  -- line 36: from-import {Lexer}
Tests that reach it through imports (4) - a superset to run, not proof of coverage:
  kernel/test_exec.py
  tests/test_mort.py
  ...
Entry points affected (4):
  mortc.py  (console script in pyproject.toml)
  ...
Risk: HIGH
  - 10 transitive dependent file(s) = 31% of 32 files
  - 4 entry point(s) are downstream (a break can surface at startup)
Graph confidence: 32 files (32 py, 0 js/ts) | 52 resolved internal edges | external packages not tracked: 4 (+37 stdlib/builtin) | UNRESOLVED project-looking imports: 0 | dynamic non-literal imports: 0 | syntax-error files: 0
```

## Install

You need [uv](https://docs.astral.sh/uv/). Point `--dir` at the project you want analyzed.

**Claude Code**

```
claude mcp add impact -- uvx impact-mcp --dir /path/to/your/project
```

**Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "impact": {
      "command": "uvx",
      "args": ["impact-mcp", "--dir", "C:\\path\\to\\your\\project"]
    }
  }
}
```

`--dir` defaults to the current directory (or `IMPACT_MCP_DIR`). One server instance analyzes one project.

## Tools

| Tool | What it answers |
|---|---|
| `project_map` | Languages, module count, packages, guessed entry points, cycle count, unresolved/dynamic imports. Start here. |
| `dependents(path_or_module, depth=3, limit=100)` | Who imports this file, transitively, grouped by depth, with the import line, names and flags. |
| `dependencies(path_or_module, depth=1, limit=100)` | What this file imports from the project; lists its unresolved and dynamic imports. |
| `impact(path, symbol=None, limit=40)` | Blast radius: dependents, tests that reach it, entry points affected, LOW/MEDIUM/HIGH risk with reasons. With `symbol`, the direct users are narrowed to that name. |
| `find_cycles(limit=10)` | Strongly connected components (Tarjan) with the shortest cycle written out edge by edge; notes lazy/type-only edges that soften a cycle. |
| `orphans_and_hubs(limit=15)` | Dead-code candidates (with caveats) and the most depended-on files. |
| `symbol_users(module, symbol, limit=100)` | Who imports or accesses one name, including through re-exports. Best effort, labeled as such. |

`path_or_module` accepts a project-relative path (`src/app/util.py`, `packages/ui/src/Button.tsx`), a
dotted Python module (`app.util`), an absolute path inside the project, or an extensionless JS path.

## Try asking

- "I'm about to change `src/billing/invoice.py`. What could break and which tests should I run?"
- "Who uses `parse_config` from `app.config`?"
- "Are there import cycles in this repo? Show me the shortest one."
- "What are the most depended-on files here? Which files look dead?"
- "Which entry points are affected if I rename `Button` in `packages/ui/src/Button.tsx`?"
- "Give me an overview of this project and how much I can trust the import graph."

## What it cannot resolve (read this)

The graph is static. These are counted in the confidence line where they are detectable, and the
`impact` and `dependents` add hints when they could be hiding a dependent of the file you asked about.

Python
- `importlib.import_module(name)` / `__import__(name)` with a non-literal argument, `exec`, plugin
  discovery by entry points or string names, `getattr` on modules. A literal `import_module("pkg.mod")` is resolved and flagged `dynamic`.
- Implicit parent-package execution is not modeled: `import a.b.c` is an edge to `a/b/c.py` only, not also to `a/__init__.py` and `a/b/__init__.py`.
- Which directories act as import roots is inferred (project root, `src/`, dirs with `pyproject.toml`/`setup.py`, and the dir above each top-level package); a custom `PYTHONPATH` or `sys.path` edit is invisible.
- `from x import *` is an edge; which names it brings in is taken from `__all__` / top-level definitions.
- Re-exports renamed with `as` inside `__init__.py` are not followed by symbol tracking.

JavaScript / TypeScript
- A tokenizer, not a parser. Known weak spots: JSX text containing an apostrophe can desync the rest of that line; regex-vs-division is heuristic.
- `require(x)` / `import(x)` with a non-literal argument, `require.context`, bundler magic (`import.meta.glob`), Next.js/Nuxt file-system routing.
- tsconfig: `extends` to an npm package is not followed (warned); `references`/`include` are ignored; the nearest tsconfig.json wins.
- package.json `exports` is read only for the `.` entry and simple subpaths (conditions: import, module, default, require, node, types); `imports` (`#alias`) maps are reported as unresolved.
- Workspace packages whose entry is a build output (`dist/`) resolve to `src/` equivalents if the source file exists; otherwise the import counts as unresolved.
- Namespace-import and `require` attribute tracking (`ns.foo`) is by name only; a namespace passed around as a value marks the edge as "may use anything".
- Only `.js .jsx .mjs .cjs .ts .tsx .mts .cts` files are in the graph; `.vue`, `.svelte`, `.astro` are not.
- Files over 2 MB (bundles) are skipped and reported.

Both
- Symlinks and junctions are not followed (loop safe); skipped counts are reported.
- On Windows and macOS (case-insensitive file systems) an import that only matches by case resolves and is counted.
- Dead-code candidates are candidates: frameworks, external consumers of a library and non-code tooling can use files the graph cannot see.

## Performance

Parsed results are cached by file mtime and size in a cache directory outside your repo
(`%LOCALAPPDATA%\impact-mcp` on Windows, `$XDG_CACHE_HOME/impact-mcp` or `~/.cache/impact-mcp` elsewhere;
override with `IMPACT_MCP_CACHE_DIR`). A cold scan of a 2,700-file JS repo takes a few seconds; a warm scan is
dominated by the directory walk. File reading is threaded; `.gitignore` files (nested too), `.git`,
`node_modules`, `venv`, `.venv`, `dist`, `build`, `__pycache__` and similar directories are skipped.

## Security

- **Read-only.** Source files are opened for reading and parsed; nothing is imported, executed, installed or written inside the project. The only write is the parse cache, in your user cache directory.
- **Sandboxed.** All paths are resolved against `--dir`; absolute paths or `..` that leave it are rejected. Symlinks are not followed.
- **Offline.** No network access of any kind.
- Output contains file paths, line numbers, and import names from your code, which go to whatever AI client you connected.

## Develop

```
uv sync --extra test
uv run pytest
uv run --with ruff==0.16.0 ruff check src tests
uv run --with mypy==2.3.0 mypy --strict src
```

Set `IMPACT_TEST_REPOS` (paths separated by `;` on Windows, `:` elsewhere) to also run read-only checks against your own repositories.

There is a from-scratch tutorial that builds a minimal version of this server:
[impact-mcp-the-hard-way](https://github.com/0xmortuex/impact-mcp-the-hard-way).

MIT licensed.
