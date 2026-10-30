# Cloudflare Tunnel fürs cloudy-Dashboard (Fernzugriff, z. B. https://cloudy.kulbarts.com).
# Der Token kommt aus dem cloudy-Dashboard (data/warp/tunnel/token).
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
        | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-main.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared bookworm main" \
        > /etc/apt/sources.list.d/cloudflared.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends cloudflared \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME /cloudy
CMD ["/entrypoint.sh"]
