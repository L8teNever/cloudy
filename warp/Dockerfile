# Ein Cloudflare-WARP-Connector pro Container. Der Token kommt aus dem cloudy-Dashboard (data/warp/<slot>/token).
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg iptables iproute2 dbus \
    && curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
        | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ bookworm main" \
        > /etc/apt/sources.list.d/cloudflare-client.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends cloudflare-warp \
    && rm -rf /var/lib/apt/lists/* \
    # Anmeldung im Volume speichern, damit sie Updates und Neustarts übersteht.
    && rm -rf /var/lib/cloudflare-warp && ln -s /cloudy/lib /var/lib/cloudflare-warp

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME /cloudy
CMD ["/entrypoint.sh"]
