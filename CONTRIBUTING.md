# Contributing to CrawlTrove

Thanks for improving CrawlTrove. Keep changes focused: fix one problem, include
the smallest useful test, and avoid committing generated scrape artifacts.

## Set up

Use Python 3.11 from the repository root:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The browser runtime is included in Docker. Install Playwright Chromium only if
you need to exercise browser-tier scraping directly on the host:

```bash
.venv/bin/python -m playwright install chromium
```

## Make a change

1. Open an issue for substantial behavior changes or security-sensitive work.
2. Create a branch from `main`; do not commit directly to `main`.
3. Follow the existing module boundaries and reuse existing helpers.
4. Add or update the smallest test that would catch a regression.
5. Run the full test suite before opening a pull request.

There is no project-specific formatter. Match the surrounding Python style and
keep public API fields backward compatible unless the pull request clearly
documents a breaking change.

## Tests

```bash
.venv/bin/python -m pytest
```

Most tests are hermetic. Database tests create and reset a dedicated test
database when Postgres is reachable; otherwise they skip. Override their local
connection with `TEST_PG_ADMIN_DSN` and `TEST_DB_NAME`.

Never point tests at production data or a production database.

## Pull requests

Include:

- the user-visible problem and the chosen fix;
- verification commands and results;
- migration notes for changed API fields, environment variables, or storage;
- screenshots only when the dashboard changed visibly.

By contributing, you agree that your contribution is licensed under the
repository's MIT License.
