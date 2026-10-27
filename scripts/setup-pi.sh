#!/usr/bin/env bash
# Richtet cloudy auf dem Raspberry Pi ein und startet es.
# Aufruf: curl -fsSL https://raw.githubusercontent.com/l8tenever/cloudy/main/scripts/setup-pi.sh | bash
set -euo pipefail

REPO="${CLOUDY_REPO:-l8tenever/cloudy}"
BRANCH="${CLOUDY_BRANCH:-main}"
DIR="${CLOUDY_DIR:-$HOME/cloudy}"
RAW="https://raw.githubusercontent.com/$REPO/$BRANCH"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker wird installiert ..."
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER" || true
fi

# Falls der aktuelle Benutzer noch nicht in der docker-Gruppe ist (frisch installiert), sudo benutzen.
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  DOCKER="sudo docker"
fi

mkdir -p "$DIR/data"
cd "$DIR"

curl -fsSL "$RAW/docker-compose.yml" -o docker-compose.yml
if [ ! -f .env ]; then
  curl -fsSL "$RAW/.env.example" -o .env
fi

$DOCKER compose pull
$DOCKER compose up -d

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "cloudy läuft: http://${IP:-<pi-ip>}:8080"
echo "Updates kommen automatisch (Watchtower, alle 5 Minuten)."
