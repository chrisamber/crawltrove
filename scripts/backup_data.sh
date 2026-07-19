#!/usr/bin/env bash
#
# backup_data.sh — tar+gzip the on-disk artifacts (the source of truth) under
# data/backups/, with N-day retention.
#
# Bundles the scrape/crawl/run artifacts AND the dedup index so a restore brings
# back both the corpus outputs and the near-dup state. Excludes data/backups
# itself (no recursive nesting).
#
#   DATA_DIR            default <repo>/data
#   BACKUP_DIR          default $DATA_DIR/backups
#   BACKUP_RETAIN_DAYS  default 14
#
# Restore:
#   tar -xzf data/backups/data-YYYYmmddTHHMMSS.tar.gz -C "$DATA_DIR"
#
# Optional offsite (Railway bucket / S3-compatible), if the AWS CLI + creds are
# present and BACKUP_S3_URI is set:
#   aws s3 cp <archive> "$BACKUP_S3_URI/"
#
# Cron example (daily at 03:45):
#   45 3 * * *  cd /path/to/crawltrove && scripts/backup_data.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${DATA_DIR:-$REPO_ROOT/data}"
BACKUP_DIR="${BACKUP_DIR:-$DATA_DIR/backups}"
RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-14}"
STAMP="$(date +%Y%m%dT%H%M%S)"
OUT="$BACKUP_DIR/data-$STAMP.tar.gz"

if [[ ! -d "$DATA_DIR" ]]; then
  echo "backup_data.sh: DATA_DIR '$DATA_DIR' does not exist — nothing to back up." >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
echo "backup_data.sh: archiving $DATA_DIR -> $OUT"
# Archive the artifact subdirs that exist; never recurse into backups/.
INCLUDE=()
for sub in scrapes crawls runs index; do
  [[ -d "$DATA_DIR/$sub" ]] && INCLUDE+=("$sub")
done
if [[ ${#INCLUDE[@]} -eq 0 ]]; then
  echo "backup_data.sh: no artifact subdirs yet — skipping." >&2
  exit 0
fi
tar -czf "$OUT" -C "$DATA_DIR" "${INCLUDE[@]}"
echo "backup_data.sh: wrote $(du -h "$OUT" | cut -f1)"

# Optional offsite copy.
if [[ -n "${BACKUP_S3_URI:-}" ]] && command -v aws >/dev/null 2>&1; then
  echo "backup_data.sh: uploading to $BACKUP_S3_URI"
  aws s3 cp "$OUT" "$BACKUP_S3_URI/"
fi

# Retention: drop archives older than RETAIN_DAYS.
find "$BACKUP_DIR" -maxdepth 1 -name 'data-*.tar.gz' -type f -mtime "+$RETAIN_DAYS" -print -delete
echo "backup_data.sh: done."
