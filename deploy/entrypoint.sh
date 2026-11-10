#!/bin/sh
# Container start-up: bring the database schema and security policies up to date (when an
# owner connection is configured), then serve. migrate is idempotent, so this is safe on
# every restart, and it also checks the encryption keys match the data.
set -e
if [ -n "${DATABASE_ADMIN_URL}${DATABASE_ADMIN_URL_FILE}" ]; then
  python -m invoice_detector.cli migrate
fi
# --proxy-headers: trust X-Forwarded-For/-Proto from the reverse proxy, so rate limits see
# real client addresses and HTTPS is detected. Only trust addresses that can reach the app
# directly; in docker-compose.prod.yml only the Caddy proxy can.
exec uvicorn invoice_detector.api:create_app --factory --host 0.0.0.0 --port 8000 --no-server-header \
  --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
