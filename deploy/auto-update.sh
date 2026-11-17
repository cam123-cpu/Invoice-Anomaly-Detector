#!/usr/bin/env bash
# Security updates for the one-server setup, safe to run unattended (weekly is a good pace).
#
#   1. takes a database backup first,
#   2. pulls the newest patched base images (PostgreSQL, Caddy) and rebuilds the app on a
#      freshly pulled Python image, which picks up operating-system and Python security fixes,
#   3. restarts only what changed, then checks the app answers.
#
# It does NOT pull new application code (no "git pull"): new features should be read and
# tested by a person first. Security fixes to the libraries the app uses arrive as Dependabot
# pull requests on GitHub; merge them there, then "git pull" and rerun this script.
#
# Install as a weekly timer (run once, as root, from the project folder):
#   sudo deploy/auto-update.sh install
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker-compose.prod.yml"

if [[ "${1:-}" == "install" ]]; then
  dir="$(pwd)"
  cat > /etc/systemd/system/invoice-update.service <<EOF
[Unit]
Description=Invoice detector security updates
After=docker.service
[Service]
Type=oneshot
WorkingDirectory=$dir
ExecStart=$dir/deploy/auto-update.sh
EOF
  cat > /etc/systemd/system/invoice-update.timer <<'EOF'
[Unit]
Description=Weekly invoice detector security updates
[Timer]
OnCalendar=Sun 03:30
RandomizedDelaySec=30m
Persistent=true
[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now invoice-update.timer
  # The server's own operating system: install security fixes automatically every day.
  if command -v apt-get >/dev/null; then
    apt-get install -y unattended-upgrades
    printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
      > /etc/apt/apt.conf.d/20auto-upgrades
  fi
  echo "Installed: weekly app/image updates (systemctl list-timers invoice-update) and daily OS security updates."
  exit 0
fi

echo "$(date -u +%FT%TZ) backing up before updating"
$COMPOSE exec -T backup bash /backup.sh once

echo "pulling patched images and rebuilding the app"
$COMPOSE pull db caddy backup
$COMPOSE build --pull app
$COMPOSE up -d

for _ in $(seq 1 30); do
  if $COMPOSE exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)" 2>/dev/null; then
    echo "updated, and the app is answering"
    docker image prune -f >/dev/null
    exit 0
  fi
  sleep 5
done
echo "the app did not come back after the update: check '$COMPOSE logs app'. The backup from before is in the backups volume." >&2
exit 1
