import time

from app import blocklist, charts, db, stats
from app.settings import Settings


def test_stats_and_charts():
    now = int(time.time())
    db.upsert_devices([("192.168.178.20", "aa:bb:cc:dd:ee:20", "fernseher")])
    db.add_queries([(now, "192.168.178.20", "netflix.com", "A")])
    db.add_traffic([("192.168.178.20", "netflix.com", 2000, 10000, 2, now)])

    r_heute = stats.get_range("heute", now=now)
    assert r_heute.key == "heute"
    assert r_heute.hourly is True

    tot = stats.totals(r_heute)
    assert tot["bytes_down"] >= 10000
    assert tot["bytes_up"] >= 2000
    assert tot["hits"] >= 1

    usage = stats.device_usage(r_heute)
    assert len(usage) >= 1
    assert usage[0]["ip"] == "192.168.178.20"

    series = stats.timeseries(r_heute, now=now)
    assert len(series) > 0

    # Diagramme rendern ohne Fehler
    col_html = charts.columns(series, charts.TRAFFIC, lambda n: f"{n} B")
    assert "cols" in str(col_html)

    hbar_html = charts.hbars(usage, charts.TRAFFIC, lambda n: f"{n} B", "device")
    assert "hbars" in str(hbar_html) and "fernseher" in str(hbar_html)


def test_isolation_endpoint(client):
    mac = "aa:bb:cc:dd:ee:99"
    assert mac not in db.isolated_macs()

    res = client.post("/devices/isolate", data={"mac": mac, "isolated": 1}, follow_redirects=False)
    assert res.status_code == 303
    assert mac in db.isolated_macs()

    res = client.post("/devices/isolate", data={"mac": mac, "isolated": 0}, follow_redirects=False)
    assert res.status_code == 303
    assert mac not in db.isolated_macs()


def test_pages_and_partials(client):
    now = int(time.time())
    db.upsert_devices([("192.168.178.44", "aa:bb:cc:dd:ee:44", "tablet")])
    db.add_queries([(now, "192.168.178.44", "wikipedia.org", "A")])

    for route in ["/", "/geraete", "/geraete/192.168.178.44", "/statistik", "/schutz", "/vpn", "/einstellungen"]:
        res = client.get(route)
        assert res.status_code == 200, f"Route {route} failed: {res.status_code}"

    # Einzelne Live-Teile
    for part in ["status", "kpis", "jetzt", "aufrufe", "geraete", "statistik", "schutz", "weiterleitungen", "vpn"]:
        res = client.get(f"/teil/{part}")
        assert res.status_code == 200, f"Teil {part} failed: {res.status_code}"

    # Gerät-Teile mit Parametern
    res = client.get("/teil/geraet_kopf?ip=192.168.178.44")
    assert res.status_code == 200
    assert "tablet" in res.text

    res = client.get("/teil/geraet_statistik?ip=192.168.178.44&zeitraum=heute")
    assert res.status_code == 200

    # Unbekanntes Teil gibt 404
    assert client.get("/teil/unbekannt").status_code == 404


def test_cookie_blocklist_info_and_recent():
    info = blocklist.list_info()
    assert info["count"] > 20
    assert "Cookie-Consent" in info["source"]

    recent = blocklist.recent_blocked()
    assert isinstance(recent, list)


def test_mein_self_service(client):
    # TestClient meldet sich als Host "testclient"
    res = client.get("/mein")
    assert res.status_code == 200
    assert "Gerät noch nicht erkannt" in res.text

    db.upsert_devices([("testclient", "aa:bb:cc:dd:ee:55", "mein-handy")])
    res = client.get("/mein")
    assert res.status_code == 200
    assert "mein-handy" in res.text
    assert "Internet für dieses Gerät" in res.text
    assert "/geraete" not in res.text  # keine Admin-Navigation

    res = client.post("/mein/warp", data={"slot": "direct"}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/mein?ok=1"
    assert db.device_warp().get("aa:bb:cc:dd:ee:55") == "direct"

    # Fremde MAC darf nicht per Formular untergeschoben werden – Endpoint nimmt nur Client-IP
    db.upsert_devices([("192.168.178.99", "aa:bb:cc:dd:ee:99", "fremd")])
    res = client.post("/mein/warp", data={"slot": "warp1"}, follow_redirects=False)
    assert res.status_code == 303
    assert db.device_warp().get("aa:bb:cc:dd:ee:99") is None
    assert db.device_warp().get("aa:bb:cc:dd:ee:55") == "warp1"
