# WireGuard-Tunnel (z. B. Proton VPN) für ein Gerät-zu-VPN-Profil. Die .conf kommt aus dem cloudy-Dashboard (data/warp/<slot>/wg.conf).
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends wireguard-tools wireguard-go iproute2 iptables procps \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME /cloudy
CMD ["/entrypoint.sh"]
