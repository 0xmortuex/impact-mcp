"""Exact-edge tests on a TypeScript monorepo with aliases, workspaces and re-exports."""

import pytest
from conftest import edge_set, write_tree

from impact_mcp import queries as q
from impact_mcp.graph import sccs, symbol_users
from impact_mcp.jsscan import parse_js
from impact_mcp.project import build_project

S = "packages/shared/src"
A = "packages/app/src"

FILES = {
    "package.json": '{"name": "root", "private": true, "workspaces": ["packages/*"]}',
    "tsconfig.json": (
        '{\n  // comment\n  "compilerOptions": {\n    "baseUrl": ".",\n'
        '    "paths": { "@app/*": ["packages/app/src/*"], "@shared": ["packages/shared/src/index.ts"], },\n  },\n}\n'
    ),
    "packages/shared/package.json": '{"name": "@acme/shared", "main": "dist/index.js", "types": "dist/index.d.ts"}',
    f"{S}/index.ts": 'export * from "./math";\nexport { fmt as format } from "./fmt";\nexport type { Opts } from "./types";\n',
    f"{S}/math.ts": "export const add = (a: number, b: number) => a + b;\n",
    f"{S}/fmt.ts": "export function fmt(x: unknown) { return String(x); }\n",
    f"{S}/types.ts": "export interface Opts { a: number }\n",
    "packages/app/package.json": '{"name": "@acme/app", "bin": {"acme": "src/cli.ts"}}',
    "packages/app/tsconfig.json": '{"extends": "../../tsconfig.json"}',
    f"{A}/cli.ts": (
        'import { add, format } from "@acme/shared";\nimport type { Opts } from "@acme/shared";\n'
        'import { Button } from "./ui/Button";\nimport "./styles.css";\nimport * as path from "node:path";\n'
        'import React from "react";\nconsole.log(add(1, 2), format(1), Button, path, React);\n'
    ),
    f"{A}/ui/Button.tsx": (
        'import { fmt } from "../../../shared/src/fmt.js";\nimport { helper } from "@app/lib/helper";\n'
        "export const Button = () => <b>{fmt(helper())}</b>;\n"
    ),
    f"{A}/ui/index.ts": 'export { Button } from "./Button";\n',
    f"{A}/lib/helper.ts": 'import { Button } from "../ui";\nexport function helper() { return Button; }\n',
    f"{A}/lazy.ts": 'export async function go(name: string) {\n  const m = await import("./lib/helper");\n  return [m, await import(name)];\n}\n',
    f"{A}/legacy.js": 'const { add } = require("@acme/shared");\nconst h = require(\'./lib/helper\');\nh.helper();\nmodule.exports.x = add;\n',
    f"{A}/lib/helper.test.ts": 'import { helper } from "./helper";\n',
    f"{A}/styles.css": "body {}\n",
    f"{A}/unknown.ts": 'import x from "@/nothing";\nimport y from "./nope";\n',
    f"{A}/comments.ts": (
        '// import a from "./lib/helper"\n/* import b from "./lib/helper" */\n'
        "const s = 'import c from \"./lib/helper\"';\nconst t = `import(\"./lib/helper\") ${1}`;\nexport default s + t;\n"
    ),
    "node_modules/dep/index.js": 'require("../../packages/app/src/lib/helper");\n',
}


@pytest.fixture
def proj(tmp_path):
    return build_project(write_tree(tmp_path, FILES))


def test_exact_edges(proj):
    assert edge_set(proj) == {
        (f"{S}/index.ts", f"{S}/math.ts"),
        (f"{S}/index.ts", f"{S}/fmt.ts"),
        (f"{S}/index.ts", f"{S}/types.ts"),
        (f"{A}/cli.ts", f"{S}/index.ts"),
        (f"{A}/cli.ts", f"{A}/ui/Button.tsx"),
        (f"{A}/ui/Button.tsx", f"{S}/fmt.ts"),
        (f"{A}/ui/Button.tsx", f"{A}/lib/helper.ts"),
        (f"{A}/ui/index.ts", f"{A}/ui/Button.tsx"),
        (f"{A}/lib/helper.ts", f"{A}/ui/index.ts"),
        (f"{A}/lazy.ts", f"{A}/lib/helper.ts"),
        (f"{A}/legacy.js", f"{S}/index.ts"),
        (f"{A}/legacy.js", f"{A}/lib/helper.ts"),
        (f"{A}/lib/helper.test.ts", f"{A}/lib/helper.ts"),
    }


def test_honesty(proj):
    assert sorted(u["spec"] for u in proj.unresolved) == ["./nope", "@/nothing"]
    assert [d["what"] for d in proj.dynamic] == ["import(<non-literal>)"]
    assert proj.assets == 1
    assert "react" in proj.external and "path" in proj.stdlib
    assert not any("node_modules" in f for f in proj.scan.files)


def test_entry_points_tests_cycles(proj):
    assert f"{A}/cli.ts" in proj.entry_points and "bin" in proj.entry_points[f"{A}/cli.ts"]
    assert f"{S}/index.ts" in proj.entry_points
    assert proj.tests == {f"{A}/lib/helper.test.ts"}
    assert sccs(proj) == [sorted([f"{A}/lib/helper.ts", f"{A}/ui/index.ts", f"{A}/ui/Button.tsx"])]


def test_symbol_users_through_export_star_and_rename(proj):
    add = symbol_users(proj, f"{S}/math.ts", "add")
    files = {(u.file, u.how) for u in add}
    assert (f"{S}/index.ts", "re-exported through `export *`") in files
    assert (f"{A}/cli.ts", "imported by name") in files
    assert (f"{A}/legacy.js", "require() destructuring / property access") in files
    fmt = symbol_users(proj, f"{S}/fmt.ts", "fmt")
    assert {u.file for u in fmt} == {f"{S}/index.ts", f"{A}/ui/Button.tsx", f"{A}/cli.ts"}
    # cli.ts only reaches `fmt` through the renamed re-export `format` in index.ts
    via = [u for u in fmt if u.file == f"{A}/cli.ts"]
    assert via[0].chain == (f"{S}/index.ts",)
    assert f"{A}/cli.ts" in {u.file for u in symbol_users(proj, f"{S}/index.ts", "format")}


def test_impact_includes_tests_and_entries(proj):
    out = q.impact(proj.root, f"{A}/lib/helper.ts")
    assert f"{A}/lib/helper.test.ts" in out
    assert f"{A}/cli.ts  (bin in packages/app/package.json)" in out
    assert "part of an import cycle" in out
    assert "UNRESOLVED project-looking imports: 2" in q.dependents(proj.root, "packages/app/src/lib/helper")


def test_type_only_flag(proj):
    flags = [e.flags for e in proj.out[f"{A}/cli.ts"] if e.dst == f"{S}/index.ts"]
    assert ("type_only",) in flags


def test_scanner_ignores_strings_comments_and_regex():
    src = (
        "// import a from './no1'\n/* require('./no2') */\n"
        "const r = /import 'x'/g, d = 4 / 2;\n"
        "const t = `${require('./yes1')} text import('./no3')`;\n"
        "const o = { import: 1 }; o.import('./no4'); x.require('./no5');\n"
    )
    refs = parse_js(src)["refs"]
    assert [r["s"] for r in refs] == ["./yes1"]


def test_scanner_import_forms():
    src = (
        'import d, { a as b, type C, default as e } from "./m1";\n'
        'import type { T } from "./m2";\nimport { type U, type V } from "./m3";\n'
        'import * as ns from "./m4";\nimport "./m5";\nexport * as q from "./m6";\n'
        'import eq = require("./m7");\nexport type * from "./m8";\n'
        "ns.one(); ns?.two();\n"
    )
    r = {x["s"]: x for x in parse_js(src)["refs"]}
    assert r["./m1"]["names"] == ["default", "a", "C", "default"] and r["./m1"]["fl"] == []
    assert r["./m2"]["fl"] == ["type_only"]
    assert r["./m3"]["fl"] == ["type_only"]
    assert r["./m4"]["names"] == ["one", "two"] and r["./m4"].get("ns")
    assert r["./m5"]["fl"] == ["side_effect"]
    assert r["./m6"]["k"] == "export" and r["./m6"]["star"]
    assert r["./m7"]["k"] == "require"
    assert r["./m8"]["fl"] == ["type_only"]
