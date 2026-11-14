# Digitaler Browser für den Speicher (Anime-Seite): ein echter Chromium ohne Bildschirm, ferngesteuert über CDP.
# Holt die AniWorld- und Hoster-Seiten wie ein normaler Browser (Schutzseiten, Weiterleitungen per JavaScript).
# Lauscht nur auf 127.0.0.1:9222 (Host-Netz) – von außen nicht erreichbar.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-liberation ca-certificates tini tinyproxy iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 browser \
    && mkdir /profil && chown browser /profil

COPY start.sh proxy.sh /
RUN chmod +x /start.sh /proxy.sh

VOLUME /profil
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD ["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/${CDP_PORT:-9222}"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/start.sh"]
