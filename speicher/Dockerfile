# syntax=docker/dockerfile:1
# cloudy Speicher: Platten-Pool (mergerfs), Puffer-SSD, Backup nach Google Drive (rclone, unverschlüsselt),
# Netzlaufwerk (Samba + wsdd) und Medienserver für Fernseher (minidlna).
FROM python:3.12-slim-bookworm

ARG TARGETARCH
ARG RCLONE_VERSION=1.68.2

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        mergerfs fuse3 util-linux e2fsprogs xfsprogs gdisk parted rsync smartmontools ca-certificates curl unzip \
        samba samba-vfs-modules wsdd minidlna ffmpeg passwd fonts-dejavu-core \
    && arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && curl -fsSL -o /tmp/rclone.zip "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${arch}.zip" \
    && unzip -j /tmp/rclone.zip "*/rclone" -d /usr/local/bin \
    && chmod +x /usr/local/bin/rclone \
    && rm -f /tmp/rclone.zip \
    && apt-get purge -y curl unzip && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /etc/samba/smb.conf \
    && rclone version | head -n1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    MOUNT_ROOT=/mnt/cloudy \
    HOST_PROC=/host/proc \
    SPEICHER_PORT=8091 \
    DATEIEN_PORT=8092 \
    ANIME_PORT=8093

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY speicher ./speicher
# Material 3: Google Sans Flex + Material Symbols Rounded (beide OFL/Apache, lokal ausgeliefert – kein Google-CDN nötig).
ADD --chmod=644 "https://github.com/google/fonts/raw/main/ofl/googlesansflex/GoogleSansFlex%5BROND,opsz,slnt,wdth,wght%5D.ttf" ./speicher/static/fonts/GoogleSansFlex.ttf
ADD --chmod=644 "https://github.com/google/material-design-icons/raw/master/variablefont/MaterialSymbolsRounded%5BFILL,GRAD,opsz,wght%5D.woff2" ./speicher/static/fonts/MaterialSymbolsRounded.woff2

VOLUME /data
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"SPEICHER_PORT\"]}/healthz', timeout=2)"

# Läuft als root: Platten einhängen, formatieren und FUSE (mergerfs) gehen nur so.
# Zwei Webseiten: Verwaltung (SPEICHER_PORT) und Dateien im Browser (DATEIEN_PORT).
CMD ["python", "-m", "speicher.run"]
