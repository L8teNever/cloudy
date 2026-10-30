#!/bin/sh
# Wartet auf den Token aus dem Dashboard, meldet sich als WARP Connector an und leitet weitergeleiteten
# Verkehr (von den Geräten, die cloudy hierher schickt) durch den Tunnel. Zustand -> /cloudy/status
set -u
DIR=/cloudy
mkdir -p "$DIR/lib" /run/dbus
[ "$(cat /proc/sys/net/ipv6/conf/default/disable_ipv6 2>/dev/null)" = 1 ]   && echo "warp: WARNUNG – IPv6 ist im Container aus, WARP startet so nicht (docker-compose.yml aktualisieren)"
svc_pid=""

status() { printf '%s\n' "$1" | head -n1 > "$DIR/status.tmp" && mv "$DIR/status.tmp" "$DIR/status"; }
warp() { warp-cli --accept-tos "$@"; }

# Absender der Geräte auf die WARP-Adresse umschreiben (IPv4 & IPv6), sonst findet die Antwort nicht zurück.
iptables -t nat -C POSTROUTING -o CloudflareWARP -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -o CloudflareWARP -j MASQUERADE
iptables -t mangle -C FORWARD -o CloudflareWARP -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null \
  || iptables -t mangle -A FORWARD -o CloudflareWARP -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu

ip6tables -t nat -C POSTROUTING -o CloudflareWARP -j MASQUERADE 2>/dev/null \
  || ip6tables -t nat -A POSTROUTING -o CloudflareWARP -j MASQUERADE 2>/dev/null || true
ip6tables -t mangle -C FORWARD -o CloudflareWARP -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null \
  || ip6tables -t mangle -A FORWARD -o CloudflareWARP -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null || true

# IPv6 der Geräte nur durchlassen, wenn der Tunnel es nachweislich kann (v6_probe). Bis dahin: sofort "keine Route",
# damit die Geräte ohne Wartezeit auf IPv4 ausweichen (statt in ein Loch zu senden).
v6state() { printf '%s\n' "$1" > "$DIR/ipv6.tmp" && mv "$DIR/ipv6.tmp" "$DIR/ipv6"; }
v6_block() {
  ip6tables -C FORWARD -i eth0 -j REJECT --reject-with icmp6-no-route 2>/dev/null \
    || ip6tables -I FORWARD 1 -i eth0 -j REJECT --reject-with icmp6-no-route 2>/dev/null || true
}
v6_probe() {
  # Klartext-HTTP an eine IPv6-Adresse von Cloudflare: jede Antwort (auch eine Weiterleitung) heißt, IPv6 geht durch.
  if curl -6 -s -m 8 -o /dev/null "http://[2606:4700:4700::1111]/" 2>/dev/null; then
    while ip6tables -D FORWARD -i eth0 -j REJECT --reject-with icmp6-no-route 2>/dev/null; do :; done
    v6state ok
  else
    v6_block
    v6state nein
  fi
}
v6_block
v6state nein

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

n=0
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

  full="$(warp status 2>&1)"
  s="$(printf '%s\n' "$full" | sed -n 's/^Status update: //p' | head -n1)"
  reason="$(printf '%s\n' "$full" | sed -n 's/^Reason: //p' | head -n1)"
  case "$s" in
    Connected*)
      status "Connected"
      # IPv6-Prüfung beim ersten Mal und danach etwa alle 30 Sekunden.
      n=$((n + 1))
      [ $((n % 6)) -eq 1 ] && v6_probe
      ;;
    Disconnected*)
      status "Getrennt${reason:+ ($reason)} – verbinde neu"
      v6state nein; n=0
      warp connect >/dev/null 2>&1
      ;;
    "") status "Unbekannt: $(warp status 2>&1 | tr '\n' ' ' | cut -c1-120)"; v6state nein; n=0 ;;
    *) status "$s"; v6state nein; n=0 ;;
  esac
  sleep 5 & wait $!
done
