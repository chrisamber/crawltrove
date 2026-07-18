import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_stdio_server_negotiates_and_lists_tools():
    root = Path(__file__).parents[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "crawltrove_mcp"],
        cwd=root,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()

    assert {tool.name for tool in tools.tools} == {
        "scrape", "search_web", "search", "start_crawl", "get_crawl",
    }
