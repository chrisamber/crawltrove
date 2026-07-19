import anyio

from crawltrove_mcp import server
from crawltrove_mcp.client import CrawlTroveError


def test_core_tools_are_registered():
    tools = anyio.run(server.mcp.list_tools)
    assert {tool.name for tool in tools} == {
        "scrape", "search_web", "search", "start_crawl", "get_crawl",
    }


def test_tool_errors_are_returned_as_data(monkeypatch):
    class FailingClient:
        def search_web(self, query, *, limit):
            raise CrawlTroveError("service unavailable", kind="connection")

    monkeypatch.setattr(server, "_client", FailingClient())

    assert server.search_web("query") == {
        "error": "service unavailable", "kind": "connection", "status": None,
    }
