import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cloudy.db"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DNSMASQ_ENABLED", "0")
    monkeypatch.setenv("HTTPS_PORT", "0")
    for name in ("DASHBOARD_PASSWORD", "MAX_ROWS", "DHCP_ENABLED", "GATEWAY_ENABLED", "UPSTREAM_DNS",
                 "WARP_SLOTS", "WARP_KILLSWITCH", "IPV6_ENABLED", "IPV6_PREFIX", "TUNNEL_IP"):
        monkeypatch.delenv(name, raising=False)
    db.init()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
