# Repository Guidelines

## Project Structure & Module Organization

`app/` contains the FastAPI service. Keep route handlers in `app/routes/`, database code in `app/db/`, document extractors in `app/documents/`, and offline corpus logic in `app/corpus/`. The stdio MCP adapter lives in `crawltrove_mcp/`. Tests mirror these areas under `tests/`, with corpus-specific coverage in `tests/corpus/`. Use `scripts/` for data collection and maintenance tools, `eval/` for retrieval evaluations, and `docs/assets/` for README images. Do not commit generated scrape output from `data/` or `tmp/`.

## Build, Test, and Development Commands

- `python3.11 -m venv .venv && .venv/bin/python -m pip install -r requirements-dev.txt` creates the supported development environment.
- `.venv/bin/python -m pytest` runs the complete test suite; add `-q` for CI-like output or pass one test path while iterating.
- `docker compose config --quiet` validates Compose configuration.
- `docker compose up --build` builds and starts the supported runtime at `http://localhost:8000`; verify it with `curl -fsS http://localhost:8000/api/health`.
- `docker compose down` stops services without deleting named volumes.

## Coding Style & Naming Conventions

Use four-space indentation and conventional Python naming: `snake_case` for modules, functions, and variables; `PascalCase` for classes; `UPPER_SNAKE_CASE` for constants. Add type hints where surrounding code uses them. There is no project-specific formatter, so match nearby style, reuse existing helpers, and preserve public API fields unless a breaking change is explicitly documented.

## Testing Guidelines

Pytest discovers `tests/test_*.py` and `tests/corpus/test_*.py`; name tests `test_<behavior>`. Add the smallest regression test that proves the change. Database tests use a dedicated Postgres database when available and otherwise skip. Never point `TEST_PG_ADMIN_DSN`, `TEST_DATABASE_URL`, or `TEST_DB_NAME` at production. No fixed coverage threshold is enforced, but CI runs the full suite.

## Commit & Pull Request Guidelines

Follow the existing short, imperative commit style, such as `Fix public version and Railway startup`. Work on a branch; never commit directly to `main`. Keep each change focused. Pull requests should explain the user-visible problem and fix, link relevant issues, list verification commands and results, note API/configuration/storage migrations, and include screenshots only for visible dashboard changes.

## Security & Configuration

Copy `.env.example` for local overrides and never commit credentials. Keep the default loopback bind unless authentication is configured. Do not enable `ALLOW_PRIVATE_NETWORKS` for public or untrusted deployments; report vulnerabilities through `SECURITY.md`.
