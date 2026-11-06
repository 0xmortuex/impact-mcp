"""Exact-edge tests on a realistic multi-package Python project."""

import pytest
from conftest import edge_set, write_tree

from impact_mcp import queries as q
from impact_mcp.graph import resolve_target, sccs, symbol_users
from impact_mcp.project import ImpactError, build_project

FILES = {
    "pyproject.toml": '[project]\nname = "demo"\n[project.scripts]\ndemo = "app.cli:main"\n',
    "src/app/__init__.py": "from .core import Engine\nfrom . import util\n__all__ = ['Engine', 'util']\n",
    "src/app/core.py": (
        "from .util import helper\nfrom app.models import Model\nimport app.util as u\n"
        "class Engine:\n    def run(self):\n        u.helper()\n        return helper() or Model\n"
    ),
    "src/app/util.py": "def helper():\n    return 1\n\n\ndef other():\n    return 2\n",
    "src/app/models.py": (
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .core import Engine\n"
        "class Model:\n    pass\n"
    ),
    "src/app/cli.py": (
        "import argparse\nimport importlib\nimport yaml\nfrom app import Engine\n"
        "from app.util import other, helper\n"
        "def main():\n    importlib.import_module(argparse.__name__ + 'x')\n    importlib.import_module('app.plugins.a')\n"
        "if __name__ == '__main__':\n    main()\n"
    ),
    "src/app/__main__.py": "from app.cli import main\nmain()\n",
    "src/app/plugins/__init__.py": "",
    "src/app/plugins/a.py": "try:\n    import ujson\nexcept ImportError:\n    ujson = None\nfrom ..util import helper\n",
    "src/ns/pkg/mod.py": "def f():\n    return 1\n",
    "src/app/uses_ns.py": "from ns.pkg import mod\nmod.f()\n",
    "src/app/broken.py": "def (\n",
    "src/app/missing.py": "from app.nothing import x\nimport os, sys\n",
    "src/app/lazy.py": "def go():\n    from app.core import Engine\n    return Engine\n",
    "tests/test_core.py": "from app.core import Engine\n",
    "tests/test_util.py": "import app.util\napp.util.helper()\n",
    "tests/helpers.py": "VALUE = 1\n",
    "tests/test_sibling.py": "import helpers\n",
}


@pytest.fixture
def proj(tmp_path):
    return build_project(write_tree(tmp_path, FILES))


def test_exact_edges(proj):
    assert edge_set(proj) == {
        ("src/app/__init__.py", "src/app/core.py"),
        ("src/app/__init__.py", "src/app/util.py"),
        ("src/app/core.py", "src/app/util.py"),
        ("src/app/core.py", "src/app/models.py"),
        ("src/app/models.py", "src/app/core.py"),
        ("src/app/cli.py", "src/app/__init__.py"),
        ("src/app/cli.py", "src/app/util.py"),
        ("src/app/cli.py", "src/app/plugins/a.py"),
        ("src/app/__main__.py", "src/app/cli.py"),
        ("src/app/plugins/a.py", "src/app/util.py"),
        ("src/app/uses_ns.py", "src/ns/pkg/mod.py"),
        ("src/app/lazy.py", "src/app/core.py"),
        ("tests/test_core.py", "src/app/core.py"),
        ("tests/test_util.py", "src/app/util.py"),
        ("tests/test_sibling.py", "tests/helpers.py"),
    }


def test_honesty_counts(proj):
    assert [u["spec"] for u in proj.unresolved] == ["app.nothing"]
    assert len(proj.dynamic) == 1 and "import_module" in proj.dynamic[0]["what"]
    assert [e["file"] for e in proj.syntax_errors] == ["src/app/broken.py"]
    assert "yaml" in proj.external and "ujson" in proj.external
    assert "os" in proj.stdlib and "argparse" in proj.stdlib


def test_flags(proj):
    e = {(x.src, x.dst): x for x in proj.edges}
    assert "type_only" in e[("src/app/models.py", "src/app/core.py")].flags
    assert "lazy" in e[("src/app/lazy.py", "src/app/core.py")].flags
    assert "dynamic" in e[("src/app/cli.py", "src/app/plugins/a.py")].flags
    assert "conditional" not in e[("src/app/plugins/a.py", "src/app/util.py")].flags


def test_entry_points_and_tests(proj):
    assert set(proj.entry_points) == {"src/app/cli.py", "src/app/__main__.py"}
    assert proj.tests == {"tests/test_core.py", "tests/test_util.py", "tests/helpers.py", "tests/test_sibling.py"}


def test_cycle(proj):
    comps = sccs(proj)
    assert comps == [["src/app/core.py", "src/app/models.py"]]
    out = q.find_cycles(proj.root)
    assert "src/app/core.py -> src/app/models.py -> src/app/core.py" in out
    assert "type_only" in out


def test_dependents_transitive(proj):
    out = q.dependents(proj.root, "app.util", depth=3)
    assert "depth 1" in out and "src/app/core.py" in out
    assert "tests/test_core.py" in out
    assert "Graph confidence" in out and "UNRESOLVED project-looking imports: 1" in out


def test_module_name_and_path_forms(proj, tmp_path):
    assert resolve_target(proj, "app.core") == "src/app/core.py"
    assert resolve_target(proj, "src\\app\\core.py") == "src/app/core.py"
    assert resolve_target(proj, "app") == "src/app/__init__.py"
    assert resolve_target(proj, str(tmp_path / "src" / "app" / "util.py")) == "src/app/util.py"
    with pytest.raises(ImpactError, match="Did you mean"):
        resolve_target(proj, "src/app/cor.py")


def test_sandbox_rejects_outside(proj, tmp_path):
    with pytest.raises(ImpactError, match="outside the sandbox"):
        resolve_target(proj, str(tmp_path.parent))
    with pytest.raises(ImpactError, match="escapes"):
        resolve_target(proj, "../x.py")


def test_symbol_users_reexport_and_attr(proj):
    uses = symbol_users(proj, "src/app/core.py", "Engine")
    by = {(u.file, u.how) for u in uses}
    assert ("src/app/__init__.py", "imported by name") in by
    assert ("tests/test_core.py", "imported by name") in by
    assert any(u.file == "src/app/cli.py" and u.chain == ("src/app/__init__.py",) for u in uses)
    helper = symbol_users(proj, "src/app/util.py", "helper")
    kinds = {(u.file, u.how) for u in helper}
    assert ("src/app/core.py", "imported by name") in kinds
    assert ("src/app/core.py", "attribute access on the imported module") in kinds
    assert ("tests/test_util.py", "attribute access on the imported module") in kinds
    other = {u.file for u in symbol_users(proj, "src/app/util.py", "other")}
    assert other == {"src/app/cli.py"}


def test_impact_report(proj):
    out = q.impact(proj.root, "src/app/util.py")
    assert "Risk: " in out
    assert "tests/test_core.py" in out and "tests/test_util.py" in out
    assert "src/app/cli.py  (console script in pyproject.toml)" in out
    out2 = q.impact(proj.root, "src/app/util.py", symbol="other")
    assert "Direct users of the symbol: 1 file" in out2
    out3 = q.impact(proj.root, "src/app/util.py", symbol="nope")
    assert "WARNING" in out3


def test_orphans_and_hubs(proj):
    out = q.orphans_and_hubs(proj.root)
    orphans, hubs = out.split("Most depended-on")
    assert "src/app/broken.py" in orphans and "src/app/missing.py" in orphans
    assert "src/app/lazy.py" in orphans
    assert "src/app/cli.py" not in orphans
    assert "src/app/util.py" in hubs


def test_project_map(proj):
    out = q.project_map(proj.root)
    assert "app.nothing" in out and "broken.py" in out


def test_cache_hits_and_invalidation(tmp_path):
    root = write_tree(tmp_path, {"a.py": "import b\n", "b.py": "x = 1\n"})
    p1 = build_project(root)
    assert p1.cache_misses == 2
    write_tree(tmp_path, {"c.py": "import a\n"})
    p2 = build_project(root)
    assert (p2.cache_hits, p2.cache_misses) == (2, 1)
    assert ("c.py", "a.py") in edge_set(p2)
    write_tree(tmp_path, {"a.py": "import b\nimport c\n"})
    p3 = build_project(root)
    assert p3.cache_misses == 1 and ("a.py", "c.py") in edge_set(p3)


def test_relative_beyond_root_and_star(tmp_path):
    root = write_tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from ... import zzz\nfrom .b import *\n",
            "pkg/b.py": "__all__ = ['one']\none = 1\ntwo = 2\n",
            "pkg/c.py": "from pkg.b import *\none\n",
        },
    )
    p = build_project(root)
    assert any("climbs above" in u["reason"] for u in p.unresolved)
    users = symbol_users(p, "pkg/b.py", "one")
    assert {u.file for u in users} == {"pkg/a.py", "pkg/c.py"}
    assert symbol_users(p, "pkg/b.py", "two") == []
