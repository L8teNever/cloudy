FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends dnsmasq-base iptables ipset iproute2 conntrack \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    DB_PATH=/data/cloudy.db \
    DASHBOARD_PORT=8080

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Läuft als root: dnsmasq braucht Port 53 (DNS) und 67 (DHCP), der Gateway-Modus iptables/conntrack.
VOLUME /data

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"DASHBOARD_PORT\"]}/healthz', timeout=2)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${DASHBOARD_PORT}"]
