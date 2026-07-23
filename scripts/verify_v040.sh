#!/usr/bin/env bash
# Sequential v0.4 verification.  The trap never removes named Compose volumes.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
COMPOSE_STARTED=0
PG_STARTED=0
PG_TMP=""
PG_PORT="${VERIFY_PG_PORT:-55432}"
AUDIT_TMP=""

run() {
  printf '+ '
  printf '%s ' "$@"
  printf '\n'
  "$@"
}

cleanup() {
  status=$?
  if [ "$COMPOSE_STARTED" -eq 1 ]; then
    if [ "$status" -ne 0 ]; then
      docker compose logs --no-color || true
    fi
    docker compose down || true
  fi
  if [ "$PG_STARTED" -eq 1 ]; then
    pg_ctl -D "$PG_TMP/data" -m fast -w stop || true
  fi
  case "$PG_TMP" in
    /tmp/crawltrove-v040-pg.*)
      if [ -d "$PG_TMP" ]; then
        rm -r -- "$PG_TMP" || true
      fi
      ;;
  esac
  case "$AUDIT_TMP" in
    /tmp/crawltrove-v040-audit.*)
      if [ -d "$AUDIT_TMP" ]; then
        rm -r -- "$AUDIT_TMP" || true
      fi
      ;;
  esac
  exit "$status"
}
trap cleanup EXIT INT TERM

cd "$ROOT"
test -x "$PYTHON" || { echo "missing $PYTHON; create the supported virtualenv first" >&2; exit 1; }
test -d apps/app/node_modules || { echo "missing apps/app/node_modules" >&2; exit 1; }
run "$PYTHON" -c "import bandit, mypy, pip_audit, ruff"
run docker info

# Do not permit a shell, CI runner, or local .env to redirect verification to
# a deployment database. Verification creates its own loopback-only cluster
# unless the operator supplies a separately named, loopback-only override.
unset DATABASE_URL TEST_DATABASE_URL TEST_PG_ADMIN_DSN TEST_DB_NAME REQUIRE_TEST_DATABASE
TEST_DB="crawltrove_v040_verify_test"
if [ -n "${VERIFY_TEST_PG_ADMIN_DSN:-}" ]; then
  TEST_PG_ADMIN_DSN="$VERIFY_TEST_PG_ADMIN_DSN"
  VERIFY_DSN_TO_CHECK="$TEST_PG_ADMIN_DSN" "$PYTHON" -c \
    "import os; from urllib.parse import unquote,urlsplit; p=urlsplit(os.environ['VERIFY_DSN_TO_CHECK']); name=unquote(p.path.lstrip('/').split('?',1)[0]); assert p.hostname in {'localhost','127.0.0.1','::1'}; assert name != '$TEST_DB'"
else
  command -v initdb >/dev/null || { echo "initdb is required" >&2; exit 1; }
  command -v pg_ctl >/dev/null || { echo "pg_ctl is required" >&2; exit 1; }
  PG_TMP="$(mktemp -d /tmp/crawltrove-v040-pg.XXXXXX)"
  run initdb -D "$PG_TMP/data" -A trust --no-locale
  run pg_ctl -D "$PG_TMP/data" -o "-h 127.0.0.1 -p $PG_PORT" -w start
  PG_STARTED=1
  TEST_PG_ADMIN_DSN="postgresql://127.0.0.1:$PG_PORT/postgres"
fi
TEST_DATABASE_URL="$(TEST_PG_ADMIN_DSN="$TEST_PG_ADMIN_DSN" "$PYTHON" -c \
  "import os; from urllib.parse import urlsplit,urlunsplit; p=urlsplit(os.environ['TEST_PG_ADMIN_DSN']); print(urlunsplit((p.scheme,p.netloc,'/$TEST_DB',p.query,p.fragment)))")"
export TEST_PG_ADMIN_DSN TEST_DATABASE_URL
export TEST_DB_NAME="$TEST_DB" REQUIRE_TEST_DATABASE=1
export V040_LOAD_ADMIN_DSN="$TEST_PG_ADMIN_DSN"
export V040_MIGRATION_ADMIN_DSN="$TEST_PG_ADMIN_DSN"

run "$PYTHON" -m pytest --cache-clear -q
run "$PYTHON" scripts/check_migration_compat.py
run "$PYTHON" -m ruff check .
run "$PYTHON" -m mypy --follow-imports=skip --explicit-package-bases \
  app/crawl/types.py app/crawl/config.py \
  app/acquisition/providers.py app/acquisition/registry.py \
  app/acquisition/sessions.py app/acquisition/owned_session.py \
  app/artifacts/base.py app/worker_config.py app/metrics.py \
  scripts/check_migration_compat.py
run "$PYTHON" -m bandit -q -lll -r app -x tests
AUDIT_TMP="$(mktemp -d /tmp/crawltrove-v040-audit.XXXXXX)"
run "$PYTHON" -m pip install --quiet --upgrade --target "$AUDIT_TMP" \
  -r requirements.txt
run "$PYTHON" -m pip_audit --strict --path "$AUDIT_TMP"

(
  cd apps/app
  test -d node_modules
  run pnpm test
  run pnpm check
  run pnpm build
)

run docker compose config --quiet
run docker compose build
COMPOSE_STARTED=1
run docker compose up -d --wait --wait-timeout 180
run docker compose ps --status running
run curl -fsS http://127.0.0.1:8000/health/ready
run curl -fsS http://127.0.0.1:8000/
run "$PYTHON" tests/load_crawl_queue.py
