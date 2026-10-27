#!/usr/bin/env bash
# Richtet cloudy auf dem Raspberry Pi ein und startet es.
# Aufruf: curl -fsSL https://raw.githubusercontent.com/l8tenever/cloudy/main/scripts/setup-pi.sh | bash
# Andere Werte z. B. so: curl ... | PI_IP=192.168.178.3 bash
set -euo pipefail

REPO="${CLOUDY_REPO:-l8tenever/cloudy}"
BRANCH="${CLOUDY_BRANCH:-main}"
DIR="${CLOUDY_DIR:-$HOME/cloudy}"
RAW="https://raw.githubusercontent.com/$REPO/$BRANCH"

PI_IP="${PI_IP:-192.168.178.2}"
ROUTER_IP="${ROUTER_IP:-192.168.178.1}"
UPSTREAM_DNS="${UPSTREAM_DNS:-172.64.36.1}"
IFACE="${LAN_INTERFACE:-eth0}"

# --- 1. Docker ---
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker wird installiert ..."
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER" || true
fi
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"

# --- 2. Dateien ---
mkdir -p "$DIR/data"
cd "$DIR"
curl -fsSL "$RAW/docker-compose.yml" -o docker-compose.yml
if [ ! -f .env ]; then
  curl -fsSL "$RAW/.env.example" -o .env
  sed -i \
    -e "s|^PI_IP=.*|PI_IP=$PI_IP|" \
    -e "s|^ROUTER_IP=.*|ROUTER_IP=$ROUTER_IP|" \
    -e "s|^UPSTREAM_DNS=.*|UPSTREAM_DNS=$UPSTREAM_DNS|" \
    -e "s|^LAN_INTERFACE=.*|LAN_INTERFACE=$IFACE|" \
    .env
fi

# --- 3. Befehl "cloudy" installieren ---
curl -fsSL "$RAW/scripts/cloudy" | sed "s|__CLOUDY_DIR__|$DIR|" > /tmp/cloudy-cli
sudo install -m 755 /tmp/cloudy-cli /usr/local/bin/cloudy
rm -f /tmp/cloudy-cli

# --- 4. Starten ---
$DOCKER compose pull
$DOCKER compose up -d

# --- 5. Feste IP für den Pi ---
CURRENT_IP="$(ip -4 -o addr show "$IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)"
CHANGE_IP=0
if [ "$CURRENT_IP" != "$PI_IP" ]; then
  if ping -c1 -W1 "$PI_IP" >/dev/null 2>&1; then
    echo "FEHLER: $PI_IP wird schon von einem anderen Gerät benutzt. Mit PI_IP=... eine freie IP angeben." >&2
    exit 1
  fi
  if ! command -v nmcli >/dev/null 2>&1; then
    echo "FEHLER: nmcli fehlt – bitte die feste IP $PI_IP von Hand einstellen." >&2
    exit 1
  fi
  CON="$(nmcli -g GENERAL.CONNECTION device show "$IFACE")"
  sudo nmcli con mod "$CON" \
    ipv4.method manual \
    ipv4.addresses "$PI_IP/24" \
    ipv4.gateway "$ROUTER_IP" \
    ipv4.dns "${UPSTREAM_DNS//,/ }" \
    ipv4.ignore-auto-dns yes
  CHANGE_IP=1
fi

echo
echo "================================================================"
echo " cloudy läuft. Dashboard: http://$PI_IP:${DASHBOARD_PORT:-8080}"
echo " DNS ist an, IP-Verteilung (DHCP) ist noch AUS."
echo
echo " Nächste Schritte stehen in der README:"
echo "   https://github.com/$REPO#readme"
echo " Befehle: cloudy update | cloudy dhcp an | cloudy dhcp aus | cloudy status"
echo "================================================================"

if [ "$CHANGE_IP" = 1 ]; then
  echo
  echo "Der Pi wechselt jetzt auf die feste IP $PI_IP. Die SSH-Verbindung bricht ab."
  echo "Danach neu verbinden mit:  ssh $USER@$PI_IP"
  # Im Hintergrund, damit es auch durchläuft, wenn SSH abbricht.
  sudo nohup sh -c "sleep 2; nmcli con up '$CON'" >/dev/null 2>&1 &
fi
