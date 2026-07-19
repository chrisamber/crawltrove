# CrawlTrove v0.2.0 release checklist

Release type: public source release from `origin/main`.

Scope: stdio MCP integration, authenticated MCP requests, public-repository
hygiene, and deployment reliability changes since `v0.1.0`.

Do not call v0.2.0 **shipped** until the GitHub release is public and the final
checks below pass. Run commands from the repository root unless noted.

## 1. Publication boundary

- [x] Work is on `release/v0.2`, not `main`.
- [x] The branch was created from current `origin/main`.
- [x] Only the audited MCP and public-hygiene changes were carried onto the
  release branch.
- [x] No scraped third-party documentation, generated corpus, database, or
  downloaded archive is tracked.
- [x] Re-run the redacted tracked-tree and publish-history secret scans without
  printing any suspected value.
- [x] Confirm repository visibility, description, topics, license, README,
  contribution guidance, security policy, and default branch.
- [x] Confirm the publish diff and commit subjects contain no private workflow
  or process residue.

## 2. Release notes and version

- [x] Set `app/VERSION` to `0.2.0`.
- [x] Document the stdio MCP adapter and its separate
  `requirements-mcp.txt` dependency set.
- [x] Document Basic and `X-API-Key` authentication forwarding.
- [x] Limit deployment claims to the verified root-started volume repair path.
- [x] Avoid unsupported security, readiness, or publication claims.

## 3. Sequential local verification

Do not run these concurrently; they share the checkout, pytest cache, Docker
state, and data volumes.

- [x] Focused MCP and authentication suite: `20 passed`.
- [x] Install the supported Python 3.11 dependencies in a clean environment and
  run `pip check`.
- [x] Run the full suite from a cleared cache: `455 passed, 29 skipped`.

  ```bash
  .venv/bin/python -m pytest --cache-clear -q
  ```

- [x] Validate the Compose configuration:

  ```bash
  docker compose config --quiet
  ```

- [x] Build the supported runtime from fresh layers:

  ```bash
  docker compose build --no-cache
  ```

- [x] Start an isolated stack and verify both containers and the HTTP service:

  ```bash
  PORT=18000 docker compose -p crawltrove-v02-push-verify \
    up -d --wait --wait-timeout 120
  docker compose -p crawltrove-v02-push-verify ps --status running
  curl --fail --show-error --silent http://127.0.0.1:18000/api/health
  ```

  The response must report `status=healthy`, `service=crawltrove`, `db=up`, and
  `version=0.2.0`.

- [x] Stop the isolated stack and delete only its fresh verification volumes:

  ```bash
  docker compose -p crawltrove-v02-push-verify down --volumes
  ```

- [x] Confirm `git diff --check` passes and the worktree is clean.

## 4. Pull request gate

- [ ] Push `release/v0.2` to `origin` and open a pull request to `main`.
- [ ] Confirm the PR diff contains only the intended v0.2.0 scope.
- [ ] Require PR CI to pass, including PostgreSQL tests, Compose validation,
  Docker build, artifact migration, running-container proof, and health curl.
- [ ] Resolve every review comment and confirm the worktree remains clean.
- [ ] Squash-merge the approved PR and record the resulting `main` commit SHA as
  `RELEASE_SHA`.

## 5. Tag and publish

- [ ] Verify `RELEASE_SHA` is current `origin/main`, `app/VERSION` is `0.2.0`,
  and post-merge `main` CI passed.
- [ ] With explicit authorization, create a draft `v0.2.0` GitHub release
  targeting the exact `RELEASE_SHA` and using `docs/release-v0.2.0.md`.
- [ ] Confirm the draft target, title, notes, and non-prerelease status.
- [ ] With explicit authorization, publish the draft.
- [ ] Confirm the public tag and release target `RELEASE_SHA`, then run one
  fresh-install and container health smoke from the published source archive.
