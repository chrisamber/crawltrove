#!/usr/bin/env bash
#
# backup_db.sh — pg_dump the Postgres index to a gzipped file under
# data/backups/, with N-day retention (Epic 2, E2.S3).
#
# The database is an additive index; the files under data/ remain the source of
# truth (see backup_data.sh). This snapshot makes point-in-time restore of the
# queryable index cheap.
#
#   DATABASE_URL        required — the DSN to dump (same one the app uses)
#   BACKUP_DIR          default <repo>/data/backups
#   BACKUP_RETAIN_DAYS  default 14  (delete db dumps older than this)
#   BACKUP_S3_URI       optional — also `aws s3 cp` the dump there (needs aws CLI)
#
# Restore:
#   gunzip -c data/backups/db-YYYYmmddTHHMMSS.sql.gz | psql "$DATABASE_URL"
# (restore into a fresh/empty database; the dump is a plain-SQL schema+data dump.)
#
# Cron example (daily at 03:30):
#   30 3 * * *  cd /path/to/web-scraper && DATABASE_URL=... scripts/backup_db.sh
set -euo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "backup_db.sh: DATABASE_URL is not set — nothing to back up." >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$REPO_ROOT/data/backups}"
RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-14}"
STAMP="$(date +%Y%m%dT%H%M%S)"
OUT="$BACKUP_DIR/db-$STAMP.sql.gz"

mkdir -p "$BACKUP_DIR"
echo "backup_db.sh: dumping -> $OUT"
pg_dump --no-owner --no-privileges "$DATABASE_URL" | gzip -9 > "$OUT"
echo "backup_db.sh: wrote $(du -h "$OUT" | cut -f1)"

# Optional offsite copy (same seam as backup_data.sh).
if [[ -n "${BACKUP_S3_URI:-}" ]] && command -v aws >/dev/null 2>&1; then
  echo "backup_db.sh: uploading to $BACKUP_S3_URI"
  aws s3 cp "$OUT" "$BACKUP_S3_URI/"
fi

# Retention: drop dumps older than RETAIN_DAYS.
find "$BACKUP_DIR" -maxdepth 1 -name 'db-*.sql.gz' -type f -mtime "+$RETAIN_DAYS" -print -delete
echo "backup_db.sh: done."
