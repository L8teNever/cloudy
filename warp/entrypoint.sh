#!/bin/sh
# Wartet auf den Token aus dem Dashboard, meldet sich als WARP Connector an und leitet weitergeleiteten
# Verkehr (von den Geräten, die cloudy hierher schickt) durch den Tunnel. Zustand -> /cloudy/status
set -u
DIR=/cloudy
mkdir -p "$DIR/lib" /run/dbus
svc_pid=""

status() { printf '%s\n' "$1" | head -n1 > "$DIR/status.tmp" && mv "$DIR/status.tmp" "$DIR/status"; }
warp() { warp-cli --accept-tos "$@"; }

# Absender der Geräte auf die WARP-Adresse umschreiben, sonst findet die Antwort nicht zurück.
iptables -t nat -C POSTROUTING -o CloudflareWARP -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -o CloudflareWARP -j MASQUERADE
# Tunnel hat eine kleinere MTU -> TCP-Paketgröße anpassen, sonst hängen manche Seiten.
iptables -t mangle -C FORWARD -o CloudflareWARP -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null \
  || iptables -t mangle -A FORWARD -o CloudflareWARP -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu

start_svc() {
  if [ -n "$svc_pid" ] && kill -0 "$svc_pid" 2>/dev/null; then return 0; fi
  rm -f /run/dbus/pid
  dbus-daemon --config-file=/usr/share/dbus-1/system.conf 2>/dev/null
  warp-svc --accept-tos &
  svc_pid=$!
  i=0
  until warp status >/dev/null 2>&1; do
    i=$((i + 1)); [ "$i" -ge 30 ] && return 1
    sleep 1
  done
}

stop_svc() {
  [ -n "$svc_pid" ] && kill "$svc_pid" 2>/dev/null && wait "$svc_pid" 2>/dev/null
  svc_pid=""
}

trap 'stop_svc; exit 0' TERM INT

while true; do
  token=""
  [ -s "$DIR/token" ] && token="$(tr -d ' \r\n' < "$DIR/token")"
  have="$(cat "$DIR/lib/cloudy-enrolled" 2>/dev/null)"

  if [ -z "$token" ]; then
    # Im Dashboard abgemeldet -> Gerät bei Cloudflare austragen.
    if [ -n "$have" ] && start_svc; then
      warp disconnect >/dev/null 2>&1
      warp registration delete >/dev/null 2>&1
      rm -f "$DIR/lib/cloudy-enrolled" "$DIR/failed"
      echo "warp: abgemeldet"
    fi
    stop_svc
    status "Nicht angemeldet"
    sleep 5 & wait $!
    continue
  fi

  want="$(printf %s "$token" | sha256sum | cut -c1-16)"
  if ! start_svc; then
    status "Fehler: WARP-Dienst startet nicht"
    stop_svc
    sleep 10 & wait $!
    continue
  fi

  if [ "$want" != "$have" ]; then
    if [ "$(cat "$DIR/failed" 2>/dev/null)" = "$want" ]; then
      sleep 5 & wait $!   # Token wurde schon abgelehnt -> auf einen neuen warten
      continue
    fi
    echo "warp: melde mit neuem Token an"
    status "Anmeldung läuft"
    warp registration delete >/dev/null 2>&1
    if out="$(warp connector new "$token" 2>&1)"; then
      echo "$want" > "$DIR/lib/cloudy-enrolled"
      rm -f "$DIR/failed"
      warp connect >/dev/null 2>&1
    else
      echo "$want" > "$DIR/failed"
      echo "warp: Anmeldung fehlgeschlagen: $out"
      status "Fehler: Anmeldung abgelehnt ($(echo "$out" | tr '\n' ' ' | cut -c1-120))"
      continue
    fi
  fi

  s="$(warp status 2>&1 | sed -n 's/^Status update: //p' | head -n1)"
  case "$s" in
    Connected*) status "Connected" ;;
    Disconnected*)
      status "Getrennt – verbinde neu"
      warp connect >/dev/null 2>&1
      ;;
    "") status "Unbekannt: $(warp status 2>&1 | tr '\n' ' ' | cut -c1-120)" ;;
    *) status "$s" ;;
  esac
  sleep 5 & wait $!
done
