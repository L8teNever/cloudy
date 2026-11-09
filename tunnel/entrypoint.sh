#!/bin/sh
# Wartet auf den Tunnel-Token aus dem Dashboard und hält "cloudflared tunnel run" am Laufen.
# Neuer Token -> Neustart, Token gelöscht -> Tunnel aus. Zustand -> /cloudy/status
set -u
DIR=/cloudy
METRICS=127.0.0.1:20241
mkdir -p "$DIR"
pid=""
have=""

status() { printf '%s\n' "$1" | head -n1 > "$DIR/status.tmp" && mv "$DIR/status.tmp" "$DIR/status"; }

stop_tunnel() {
  [ -n "$pid" ] && kill "$pid" 2>/dev/null && wait "$pid" 2>/dev/null
  pid=""
}

trap 'stop_tunnel; exit 0' TERM INT

while true; do
  token=""
  [ -s "$DIR/token" ] && token="$(tr -d ' \r\n' < "$DIR/token")"
  # Übergang: früher stand der Token als CLOUDFLARE_TUNNEL_TOKEN in der .env.
  [ -z "$token" ] && token="$(printf %s "${TUNNEL_TOKEN:-}" | tr -d ' \r\n')"

  if [ -z "$token" ]; then
    if [ -n "$pid" ]; then stop_tunnel; echo "tunnel: aus"; fi
    have=""
    status "Nicht eingerichtet"
    sleep 5 & wait $!
    continue
  fi

  want="$(printf %s "$token" | sha256sum | cut -c1-16)"
  if [ "$want" != "$have" ] || ! kill -0 "$pid" 2>/dev/null; then
    stop_tunnel
    [ "$want" != "$have" ] && echo "tunnel: starte mit neuem Token"
    status "Verbindet …"
    TUNNEL_TOKEN="$token" cloudflared tunnel --no-autoupdate --metrics "$METRICS" run > "$DIR/cloudflared.log" 2>&1 &
    pid=$!
    have="$want"
    sleep 5 & wait $!
  fi

  if curl -fs -m 3 "http://$METRICS/ready" >/dev/null 2>&1; then
    status "Connected"
  elif ! kill -0 "$pid" 2>/dev/null; then
    # cloudflared beendet (z. B. ungültiger Token): letzte Fehlerzeile anzeigen, gleich neu versuchen.
    err="$(grep -iE 'error|ERR|invalid|unauthor' "$DIR/cloudflared.log" 2>/dev/null | tail -n1 | cut -c1-160)"
    status "Fehler: ${err:-cloudflared beendet} (cloudy logs tunnel)"
    cat "$DIR/cloudflared.log" 2>/dev/null | tail -n 5
    sleep 10 & wait $!
    continue
  else
    status "Verbindet …"
  fi
  sleep 5 & wait $!
done
