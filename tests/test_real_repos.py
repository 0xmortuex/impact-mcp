"""Read-only runs against real repositories.

Set IMPACT_TEST_REPOS to a list of directories (separated by os.pathsep). Skipped when unset.
The repositories are only read; the test also proves nothing under them changed.
"""

import os

import pytest

from impact_mcp import queries as q
from impact_mcp.project import build_project

REPOS = [p for p in os.environ.get("IMPACT_TEST_REPOS", "").split(os.pathsep) if p]


def _snapshot(proj):
    return {f: v for f, v in proj.scan.files.items()}


@pytest.mark.skipif(not REPOS, reason="IMPACT_TEST_REPOS not set (list of local repos to analyze read-only)")
@pytest.mark.parametrize("repo", REPOS or ["-"])
def test_real_repo(repo):
    if not os.path.isdir(repo):
        pytest.skip(f"{repo} does not exist on this machine")
    proj = build_project(repo)
    before = _snapshot(proj)
    files = set(proj.scan.files)
    assert files, "repo has no .py/.js/.ts files"
    for e in proj.edges:
        assert e.src in files and e.dst in files and e.src != e.dst
    assert "Graph confidence" in q.project_map(repo)
    assert "Import cycles" in q.find_cycles(repo)
    assert "Dead-code candidates" in q.orphans_and_hubs(repo)
    if proj.inn:
        hub = max(proj.inn, key=lambda f: len(proj.inn[f]))
        assert hub in q.dependents(repo, hub, depth=2) or "Dependents of" in q.dependents(repo, hub, depth=2)
        assert "Risk:" in q.impact(repo, hub)
    again = build_project(repo)
    assert _snapshot(again) == before, "repository changed while being analyzed"
