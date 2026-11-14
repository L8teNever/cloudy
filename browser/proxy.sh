#!/bin/sh
# VPN-Proxy für die Anime-Seite: ein kleiner HTTP-Proxy (tinyproxy), dessen gesamter Verkehr über den
# VPN-Container (GATEWAY, Standard vpn1) in den Tunnel geht. Der VPN-Container lässt nur Pakete in den Tunnel
# (Kill-Switch) – ist der Tunnel weg, kommt keine Verbindung zustande, nie ein Umweg am VPN vorbei.
set -u
GW="${VPN_GATEWAY:-172.29.86.21}"
# Standardroute und DNS durch den VPN-Container (sonst würde DNS am Tunnel vorbei fragen).
ip route replace default via "$GW" || { echo "proxy: Route über $GW nicht setzbar"; exit 1; }
ip -6 route del default 2>/dev/null || true
printf 'nameserver 1.1.1.1\nnameserver 9.9.9.9\n' > /etc/resolv.conf
cat > /tmp/tinyproxy.conf <<EOC
User nobody
Group nogroup
Port 8888
Listen 0.0.0.0
Timeout 600
MaxClients 64
Allow 0.0.0.0/0
DisableViaHeader Yes
ConnectPort 443
ConnectPort 80
ConnectPort 8443
EOC
exec tinyproxy -d -c /tmp/tinyproxy.conf
