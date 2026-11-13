# cloudy Speicher: Platten-Pool (mergerfs), Puffer-SSD und verschlüsseltes Backup nach Google Drive (restic + rclone).
FROM python:3.12-slim-bookworm

ARG TARGETARCH
ARG RESTIC_VERSION=0.17.3
ARG RCLONE_VERSION=1.68.2

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        mergerfs fuse3 util-linux e2fsprogs xfsprogs gdisk parted rsync smartmontools ca-certificates curl bzip2 unzip \
    && arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && curl -fsSL "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_${arch}.bz2" \
        | bunzip2 > /usr/local/bin/restic \
    && chmod +x /usr/local/bin/restic \
    && curl -fsSL -o /tmp/rclone.zip "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${arch}.zip" \
    && unzip -j /tmp/rclone.zip "*/rclone" -d /usr/local/bin \
    && chmod +x /usr/local/bin/rclone \
    && rm -f /tmp/rclone.zip \
    && apt-get purge -y curl bzip2 unzip && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && restic version && rclone version | head -n1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    MOUNT_ROOT=/mnt/cloudy \
    HOST_PROC=/host/proc \
    SPEICHER_PORT=8091

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY speicher ./speicher

VOLUME /data
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"SPEICHER_PORT\"]}/healthz', timeout=2)"

# Läuft als root: Platten einhängen, formatieren und FUSE (mergerfs) gehen nur so.
CMD ["sh", "-c", "exec uvicorn speicher.main:app --host 0.0.0.0 --port ${SPEICHER_PORT} --proxy-headers --no-server-header"]
