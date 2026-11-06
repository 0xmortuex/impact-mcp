# Changelog

## 0.1.0

First release.

- Python import graph from the stdlib `ast`: `import`, `from` (absolute and relative), submodule
  imports, `__init__.py` re-exports, `__all__`, `if TYPE_CHECKING`, try/except and function-level
  imports (flagged), namespace packages, `src/` layouts, literal `importlib.import_module("x")`.
  Non-literal dynamic imports are reported as dynamic, never guessed.
- JS/TS import graph from a hand-written tokenizer: ESM `import` / `export ... from` / `import type`,
  `require()`, `import()` with a literal, `import x = require()`, tsconfig `extends` / `baseUrl` / `paths`,
  workspace packages from package.json, extension probing (including `.js` -> `.ts`), `index` resolution.
- Seven tools: `project_map`, `dependents`, `dependencies`, `impact`, `find_cycles`,
  `orphans_and_hubs`, `symbol_users`.
- Every result ends with a graph-confidence line (unresolved / dynamic / syntax-error counts).
- Parse cache keyed on mtime+size outside the repo; honors `.gitignore`; symlink and junction loops are not followed.
