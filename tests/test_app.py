import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "access.db"))
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("MAX_ROWS", raising=False)
    with TestClient(app) as c:
        yield c


def test_request_is_logged_and_shown(client):
    assert client.get("/test?x=1", headers={"user-agent": "pytest-agent"}).status_code == 200
    logs = client.get("/api/logs").json()
    assert logs["recent"][0]["path"] == "/test?x=1"
    assert logs["recent"][0]["method"] == "GET"
    assert logs["devices"][0]["hits"] == 1
    assert logs["devices"][0]["user_agent"] == "pytest-agent"

    html = client.get("/").text
    assert "/test?x=1" in html
    assert "pytest-agent" in html


def test_dashboard_requests_are_not_logged(client):
    client.get("/")
    client.get("/api/logs")
    client.get("/healthz")
    assert client.get("/api/logs").json()["recent"] == []


def test_devices_grouped_by_ip(client):
    for _ in range(3):
        client.get("/a")
    devices = client.get("/api/logs").json()["devices"]
    assert len(devices) == 1
    assert devices[0]["hits"] == 3


def test_pruning(client, monkeypatch):
    monkeypatch.setenv("MAX_ROWS", "5")
    for i in range(12):
        client.get(f"/p{i}")
    recent = db.recent(100)
    assert len(recent) == 5
    assert recent[0]["path"] == "/p11"


def test_forwarded_for_only_when_trusted(client, monkeypatch):
    client.get("/x", headers={"x-forwarded-for": "203.0.113.7"})
    assert client.get("/api/logs").json()["recent"][0]["ip"] != "203.0.113.7"

    monkeypatch.setenv("TRUST_PROXY", "1")
    client.get("/y", headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"})
    assert client.get("/api/logs").json()["recent"][0]["ip"] == "203.0.113.7"


def test_optional_password(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "geheim")
    assert client.get("/").status_code == 401
    assert client.get("/api/logs").status_code == 401
    assert client.get("/", auth=("x", "falsch")).status_code == 401
    assert client.get("/", auth=("x", "geheim")).status_code == 200
    # Andere Pfade bleiben öffentlich.
    assert client.get("/foo").status_code == 200
