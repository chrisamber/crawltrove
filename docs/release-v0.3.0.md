# CrawlTrove v0.3.0

CrawlTrove v0.3.0 adds a local Next.js operator GUI to the supported Docker
runtime. The browser interface and API remain part of one self-hosted service at
`http://localhost:8000`.

## Highlights

- Start bounded site crawls or single-page scrapes from the Crawl workspace.
- Inspect persisted runs, page metadata, errors, and captured artifacts.
- Browse saved Markdown and JSON documents without leaving the dashboard.
- Search and inspect corpus records, provenance, quality tiers, and targets.
- Resume active crawl polling after a dashboard refresh and ignore stale or
  cancelled browser requests.
- Use responsive desktop inspectors, mobile dialogs, loading/error states, and
  keyboard-accessible navigation.

## Runtime and development

- Docker builds the Next.js static export and copies it into the FastAPI image;
  the final container does not run Node.js.
- The dashboard uses existing same-origin API and artifact routes, preserving
  the current authentication, CORS, URL-safety, and loopback defaults.
- Frontend unit tests, TypeScript checks, the production Docker build, and a
  served-asset smoke test run in CI.
- Frontend development uses the Next.js `FASTAPI_URL` rewrite to proxy a local
  FastAPI server without requiring browser CORS changes.

## Upgrade notes

Rebuild the image after updating the source checkout:

```bash
docker compose up --build
```

Then open <http://localhost:8000>. No API, configuration, database, or artifact
storage migration is required. The previous static dashboard remains a fallback
for source checkouts where the Next.js export has not been built.
