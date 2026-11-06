"""MCP stdio smoke test: spawns the real server over stdio on a real project directory."""

import os
import sys

import pytest
from conftest import write_tree

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

EXPECTED_TOOLS = {
    "project_map",
    "dependents",
    "dependencies",
    "impact",
    "find_cycles",
    "orphans_and_hubs",
    "symbol_users",
}

FILES = {
    "pkg/__init__.py": "",
    "pkg/a.py": "from pkg.b import thing\n",
    "pkg/b.py": "def thing():\n    return 1\n",
    "tests/test_a.py": "from pkg.a import thing\n",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _params(root: str, tmp_path) -> StdioServerParameters:
    env = {**os.environ, "IMPACT_MCP_CACHE_DIR": str(tmp_path / "cache")}
    return StdioServerParameters(command=sys.executable, args=["-m", "impact_mcp", "--dir", root], env=env)


@pytest.mark.anyio
async def test_tools_and_a_session_over_stdio(tmp_path):
    root = write_tree(tmp_path / "proj", FILES)
    async with stdio_client(_params(root, tmp_path)) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "impact"
        names = {t.name for t in (await session.list_tools()).tools}
        assert names == EXPECTED_TOOLS, (names - EXPECTED_TOOLS, EXPECTED_TOOLS - names)

        res = await session.call_tool("impact", {"path": "pkg/b.py"})
        text = res.content[0].text
        assert not res.is_error and "tests/test_a.py" in text and "Graph confidence" in text
        res = await session.call_tool("dependents", {"path_or_module": "pkg.b", "depth": 1})
        assert "pkg/a.py" in res.content[0].text
        res = await session.call_tool("symbol_users", {"module": "pkg.b", "symbol": "thing"})
        assert "pkg/a.py:1" in res.content[0].text
        res = await session.call_tool("project_map", {})
        assert "python 4" in res.content[0].text


@pytest.mark.anyio
async def test_instructive_errors_reach_the_agent(tmp_path):
    root = write_tree(tmp_path / "proj", FILES)
    outside = write_tree(tmp_path / "outside", {"x.py": ""})
    async with stdio_client(_params(root, tmp_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        res = await session.call_tool("dependents", {"path_or_module": "pkg/zzz.py"})
        assert res.is_error and "no scanned file or module matches" in res.content[0].text
        res = await session.call_tool("impact", {"path": os.path.join(outside, "x.py")})
        assert res.is_error and "outside the sandbox" in res.content[0].text
