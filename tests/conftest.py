import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("IMPACT_MCP_CACHE_DIR", str(tmp_path_factory.mktemp("cache")))


def write_tree(root, files: dict[str, str]) -> str:
    """Write files (UTF-8, LF) under root and return it as a string."""
    for rel, text in files.items():
        p = os.path.join(str(root), *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    return str(root)


def edge_set(proj):
    return {(e.src, e.dst) for e in proj.edges}
