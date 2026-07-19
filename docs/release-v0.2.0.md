# CrawlTrove v0.2.0

CrawlTrove v0.2.0 adds an MCP integration and improves reliability for
self-hosted deployments.

## Highlights

- Run CrawlTrove as a stdio MCP server backed by an existing CrawlTrove HTTP
  service. The adapter exposes scrape, web search, hybrid search, crawl start,
  and crawl-status tools.
- Forward either HTTP Basic credentials or `X-API-Key` authentication from the
  MCP adapter.
- Repair root-owned data volumes in root-started container deployments such as
  Railway before dropping privileges to the runtime user.
- Refresh the public project documentation and repository hygiene.

## Upgrade notes

- Rebuild the container image after updating the source checkout.
- The MCP adapter uses a separate dependency set:

  ```bash
  python3.11 -m venv .venv
  .venv/bin/python -m pip install -r requirements-mcp.txt
  ```

- Set `CRAWLTROVE_BASE_URL` for the running service. If authentication is
  enabled, set `CRAWLTROVE_API_KEY` or the `CRAWLTROVE_USER` and
  `CRAWLTROVE_PASSWORD` pair.

No scraped third-party documentation or generated corpus data is included in
this release.
