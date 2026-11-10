#!/usr/bin/env bash
# Database backups, run by the "backup" service in docker-compose.prod.yml.
#   backup.sh loop            nightly (BACKUP_INTERVAL_HOURS), keeping BACKUP_KEEP_DAYS of dumps
#   backup.sh once            one backup now
#   backup.sh restore FILE    restore FILE into the live database (then run "cli migrate")
# Encryption keys are NOT in these files, by design: back up deploy/secrets/ separately.
set -euo pipefail
export PGPASSWORD="$(cat /run/secrets/postgres_password)"
HOST="${PGHOST:-db}" USER_="${PGUSER:-postgres}" DB="${PGDATABASE:-invoices}"

backup_once() {
  local file="/backups/invoices-$(date -u +%Y%m%dT%H%M%SZ).dump"
  pg_dump --format=custom -h "$HOST" -U "$USER_" -d "$DB" -f "$file.partial"
  mv "$file.partial" "$file"
  find /backups -name 'invoices-*.dump' -mtime +"${BACKUP_KEEP_DAYS:-14}" -delete
  echo "backup written: $file ($(du -h "$file" | cut -f1))"
}

case "${1:-loop}" in
  once) backup_once ;;
  loop)
    while true; do
      backup_once || echo "BACKUP FAILED at $(date -u)" >&2
      sleep $(( ${BACKUP_INTERVAL_HOURS:-24} * 3600 ))
    done ;;
  restore)
    file="${2:?usage: backup.sh restore /backups/FILE.dump}"
    pg_restore --clean --if-exists --no-owner --no-privileges -h "$HOST" -U "$USER_" -d "$DB" "$file"
    echo "restored $file; now run: docker compose -f docker-compose.prod.yml exec app python -m invoice_detector.cli migrate" ;;
  *) echo "usage: backup.sh once|loop|restore FILE" >&2; exit 1 ;;
esac
