#!/bin/sh
# WireGuard-Tunnel (z. B. Proton VPN). Wartet auf die .conf aus dem Dashboard (/cloudy/wg.conf), baut den Tunnel auf
# und leitet den Verkehr, der von den Geräten hier ankommt, durch den Tunnel. Zustand -> /cloudy/status
set -u
DIR=/cloudy
IF=wg0
mkdir -p "$DIR"

status() { printf '%s\n' "$1" | head -n1 > "$DIR/status.tmp" && mv "$DIR/status.tmp" "$DIR/status"; }
v6state() { printf '%s\n' "$1" > "$DIR/ipv6.tmp" && mv "$DIR/ipv6.tmp" "$DIR/ipv6"; }

# Kill-Switch im Container (IPv4 & IPv6): Pakete der Geräte dürfen NUR in den Tunnel, nie an ihm vorbei ins Internet.
iptables -P FORWARD DROP
iptables -A FORWARD -i eth0 -o "$IF" -j ACCEPT
iptables -A FORWARD -i "$IF" -o eth0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -t nat -A POSTROUTING -o "$IF" -j MASQUERADE
# Tunnel hat eine kleinere MTU -> TCP-Paketgröße anpassen, sonst hängen manche Seiten.
iptables -t mangle -A FORWARD -o "$IF" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu

ip6tables -P FORWARD DROP 2>/dev/null || true
ip6tables -A FORWARD -i eth0 -o "$IF" -j ACCEPT 2>/dev/null || true
ip6tables -A FORWARD -i "$IF" -o eth0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
ip6tables -t nat -A POSTROUTING -o "$IF" -j MASQUERADE 2>/dev/null || true
ip6tables -t mangle -A FORWARD -o "$IF" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null || true

tunnel_down() {
  wg-quick down "$IF" >/dev/null 2>&1
  ip rule del iif eth0 lookup 100 priority 100 2>/dev/null || true
  ip route flush table 100 2>/dev/null || true
  ip -6 rule del iif eth0 lookup 100 priority 100 2>/dev/null || true
  ip -6 route flush table 100 2>/dev/null || true
}

tunnel_up() {
  # Nur Adresse und Schlüssel übernehmen. Befehle in der Datei (PostUp ...), DNS und Routen setzen wir selbst.
  mkdir -p /etc/wireguard
  tr -d '\r' < "$DIR/wg.conf" \
    | sed -E '/^[[:space:]]*(PostUp|PreUp|PostDown|PreDown|SaveConfig|DNS|Table)[[:space:]]*=/Id' \
    | sed '/^\[Interface\]/I a Table = off' > /etc/wireguard/$IF.conf
  chmod 600 /etc/wireguard/$IF.conf
  tunnel_down
  if ! out="$(wg-quick up "$IF" 2>&1)"; then
    # Kein WireGuard im Kernel -> Programm-Variante
    tunnel_down
    out="$(WG_QUICK_USERSPACE_IMPLEMENTATION=wireguard-go wg-quick up "$IF" 2>&1)" || { echo "vpn: $out"; return 1; }
  fi
  peer="$(wg show "$IF" peers | head -n1)"
  [ -n "$peer" ] && wg set "$IF" peer "$peer" persistent-keepalive 25
  ip route replace default dev "$IF" table 100
  ip rule add iif eth0 lookup 100 priority 100 2>/dev/null || true
  # IPv6-Schutz: Blockiert IPv6, falls der WireGuard-Tunnel nur IPv4 hat,
  # damit keine Pakete zurück zur Fritzbox ins deutsche Heimnetz lecken.
  ip -6 route replace unreachable default table 100 2>/dev/null || true
  if ip -6 addr show dev "$IF" 2>/dev/null | grep -q "inet6"; then
    ip -6 route replace default dev "$IF" table 100 2>/dev/null || true
  fi
  ip -6 rule add iif eth0 lookup 100 priority 100 2>/dev/null || true
}

trap 'tunnel_down; exit 0' TERM INT

applied=""
while true; do
  if [ ! -s "$DIR/wg.conf" ]; then
    if [ -n "$applied" ]; then tunnel_down; applied=""; fi
    status "Nicht angemeldet"
    sleep 5 & wait $!
    continue
  fi

  want="$(sha256sum "$DIR/wg.conf" | cut -c1-16)"
  if [ "$want" != "$applied" ]; then
    echo "vpn: baue Tunnel auf"
    status "Verbindung wird aufgebaut"
    if tunnel_up; then
      applied="$want"
    else
      status "Fehler: Tunnel konnte nicht gestartet werden (cloudy logs vpn1)"
      sleep 10 & wait $!
      continue
    fi
  fi

  hs="$(wg show "$IF" latest-handshakes 2>/dev/null | awk '{print $2}' | head -n1)"
  now="$(date +%s)"
  if [ -n "$hs" ] && [ "$hs" -gt 0 ] && [ $((now - hs)) -lt 180 ]; then
    status "Connected"
    # IPv6 nur, wenn die WireGuard-Konfiguration auch eine IPv6-Adresse hat (sonst bleibt "keine Route").
    if ip -6 addr show dev "$IF" 2>/dev/null | grep -q "inet6"; then v6state ok; else v6state nein; fi
  else
    status "Getrennt – kein Kontakt zum VPN-Server"
    v6state nein
  fi
  sleep 5 & wait $!
done
