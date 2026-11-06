import os
import subprocess
import sys
import time

import pytest
from conftest import edge_set, write_tree

from impact_mcp.graph import resolve_target
from impact_mcp.project import build_project
from impact_mcp.walk import parse_gitignore, scan_tree


def test_gitignore_and_default_ignores(tmp_path):
    root = write_tree(
        tmp_path,
        {
            ".gitignore": "generated/\n*.gen.py\n!keep.gen.py\n/top_only.py\nsub/local_*.py\n# comment\n",
            "a.py": "import b\n",
            "b.py": "",
            "generated/x.py": "import a\n",
            "foo.gen.py": "",
            "keep.gen.py": "",
            "top_only.py": "",
            "nested/top_only.py": "",
            "sub/local_one.py": "",
            "sub/other.py": "",
            "sub/.gitignore": "other.py\n",
            "node_modules/m/index.js": "",
            "venv/lib/x.py": "",
            ".venv/lib/x.py": "",
            "dist/x.js": "",
            "build/x.js": "",
            "__pycache__/x.py": "",
            ".git/hooks/x.py": "",
        },
    )
    scan = scan_tree(root)
    assert sorted(scan.files) == ["a.py", "b.py", "keep.gen.py", "nested/top_only.py"]
    assert scan.gitignored >= 4


def test_gitignore_glob_forms():
    rules = parse_gitignore("**/cache/\nlogs/**\ndocs/*.tmp\n")
    assert rules[0].rx.match("a/b/cache") and rules[0].dir_only
    assert rules[1].rx.match("logs/a/b")
    assert rules[2].rx.match("docs/x.tmp") and not rules[2].rx.match("docs/a/x.tmp")


def test_symlink_loop_is_not_followed(tmp_path):
    root = write_tree(tmp_path, {"pkg/a.py": "import pkg.b\n", "pkg/b.py": ""})
    link = os.path.join(root, "pkg", "loop")
    try:
        os.symlink(root, link, target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        if sys.platform != "win32":
            raise
        # unprivileged Windows: make a junction (a real directory loop) instead
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, root], capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip(f"cannot create symlink or junction here: {e}; {r.stderr}")
    started = time.time()
    p = build_project(root)
    assert time.time() - started < 20
    assert sorted(p.scan.files) == ["pkg/a.py", "pkg/b.py"]
    assert p.scan.symlinks_skipped >= 1
    assert ("pkg/a.py", "pkg/b.py") in edge_set(p)


def test_syntax_errors_are_reported_not_fatal(tmp_path):
    root = write_tree(
        tmp_path,
        {"ok.py": "import bad\n", "bad.py": "def (:\n", "nul.py": "x = 1\n", "weird.ts": "import {{{ from 'x'\n"},
    )
    with open(os.path.join(root, "nul.py"), "wb") as fh:
        fh.write(b"x = 1\x00\n")
    p = build_project(root)
    assert {e["file"] for e in p.syntax_errors} == {"bad.py", "nul.py"}
    assert ("ok.py", "bad.py") in edge_set(p)


def test_case_insensitive_fallback_is_flagged(tmp_path):
    root = write_tree(tmp_path, {"Src/Mod.ts": "export const a = 1;\n", "use.ts": "import { a } from './src/mod';\n"})
    p = build_project(root)
    if sys.platform in ("win32", "darwin"):
        assert ("use.ts", "Src/Mod.ts") in edge_set(p)
        assert p.case_mismatches == 1
    else:
        assert edge_set(p) == set()
        assert [u["spec"] for u in p.unresolved] == ["./src/mod"]


def test_utf8_bom_and_non_ascii(tmp_path):
    root = tmp_path
    (root / "uni.py").write_bytes("﻿# café ☃\nimport other\n".encode())
    (root / "other.py").write_text("", encoding="utf-8")
    (root / "j.ts").write_bytes("﻿import {x} from './k';\n// ☃\n".encode())
    (root / "k.ts").write_text("export const x = 1;\n", encoding="utf-8")
    p = build_project(str(root))
    assert edge_set(p) == {("uni.py", "other.py"), ("j.ts", "k.ts")}


def test_path_lookup_ambiguity_is_an_error(tmp_path):
    from impact_mcp.project import ImpactError

    root = write_tree(tmp_path, {"a/util.py": "", "b/util.py": ""})
    p = build_project(root)
    with pytest.raises(ImpactError, match="matches several files"):
        resolve_target(p, "util.py")
    assert resolve_target(p, "a/util.py") == "a/util.py"


def test_large_generated_repo_is_fast_and_cached(tmp_path):
    files: dict[str, str] = {}
    n_pkgs, per = 40, 60
    for p in range(n_pkgs):
        files[f"src/pkg{p}/__init__.py"] = ""
        for m in range(per):
            nxt = (m + 1) % per
            lines = [f"from pkg{p}.m{nxt} import f{nxt}", "import os"]
            if p:
                lines.append(f"from pkg{p - 1}.m{m} import f{m}")
            lines.append(f"def f{m}():\n    return {m}")
            files[f"src/pkg{p}/m{m}.py"] = "\n".join(lines) + "\n"
    for i in range(300):
        files[f"web/c{i}.ts"] = f"import {{ x }} from './c{(i + 1) % 300}';\nexport const x = {i};\n"
    root = write_tree(tmp_path, files)
    t0 = time.time()
    p1 = build_project(root)
    cold = time.time() - t0
    assert len(p1.scan.files) == n_pkgs * (per + 1) + 300
    assert len(p1.edges) == n_pkgs * per + (n_pkgs - 1) * per + 300
    assert p1.cache_misses == len(p1.scan.files)
    from impact_mcp import project as pj

    pj._MEMO.clear()
    t0 = time.time()
    p2 = build_project(root)
    warm = time.time() - t0
    assert p2.cache_hits == len(p2.scan.files) and p2.cache_misses == 0
    assert cold < 60 and warm < cold * 1.5 + 1
    assert len(p2.edges) == len(p1.edges)
