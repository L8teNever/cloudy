"""Auto-Wechsel: lokale Wechselseite, VPN-Kette und normales Internet."""

from app import autoswitch, db, warp
from app.dnsmasq import sync_redirects_hosts
from app.settings import Settings


def test_auto_rule_and_hijack_dns(client):
    autoswitch.set_rule("aniworld.com", autoswitch.AUTO)
    assert autoswitch.load_rules()["aniworld.com"] == "auto"
    assert autoswitch.covers_host("www.aniworld.com")
    assert autoswitch.rule_apex("www.aniworld.com") == "aniworld.com"
    assert "aniworld.com" in autoswitch.hijack_domains()

    s = Settings.from_env()
    sync_redirects_hosts(s)
    conf = open(s.redirects_path, encoding="utf-8").read()
    assert f"address=/aniworld.com/{s.pi_ip}" in conf


def test_pass_removes_hijack(client):
    autoswitch.set_rule("aniworld.com", "vpn1")
    assert autoswitch.set_pass("aniworld.com", "vpn1")
    assert autoswitch.is_passed("www.aniworld.com")
    assert "aniworld.com" not in autoswitch.hijack_domains()
    assert db.site_routes().get("aniworld.com") == "vpn1"

    s = Settings.from_env()
    sync_redirects_hosts(s)
    conf = open(s.redirects_path, encoding="utf-8").read()
    assert f"address=/aniworld.com/{s.pi_ip}" not in conf

    assert autoswitch.clear_pass("aniworld.com")
    assert not autoswitch.is_passed("aniworld.com")
    assert db.site_routes().get("aniworld.com") is None


def test_local_host_shows_wechsel_page(client, monkeypatch):
    monkeypatch.setattr("app.localdomain.start_in_background", lambda s: None)
    autoswitch.set_rule("aniworld.com", autoswitch.AUTO)
    page = client.get("/", headers={"host": "aniworld.com"})
    assert page.status_code == 200
    assert "aniworld.com" in page.text and "/vpn-wechsel/status" in page.text
    # Auch mit Durchlass: Wechselseite (Browser-DNS-Cache), kein Dashboard-404
    autoswitch.set_pass("aniworld.com", "vpn1")
    page2 = client.get("/", headers={"host": "aniworld.com"})
    assert page2.status_code == 200 and "/vpn-wechsel/status" in page2.text
    assert client.get("/vpn-wechsel/ping?host=aniworld.com").json()["cloudy"] is True


def test_auto_picks_vpn_and_normal_fallback(client, monkeypatch):
    mac = "aa:bb:cc:dd:ee:02"
    # TestClient erscheint als Host „testclient“
    db.upsert_devices([("testclient", mac, "tablet")])
    autoswitch.set_rule("aniworld.com", autoswitch.AUTO)
    autoswitch.set_normal_slot(warp.DIRECT)

    settings = client.app.state.settings
    sw = autoswitch.AutoSwitcher(settings=settings)
    client.app.state.autoswitcher = sw
    dns = []
    sw.on_dns_change = lambda: dns.append(1)

    monkeypatch.setattr(warp, "read_status", lambda s, slot, now=None: warp.SlotStatus(True, "verbunden"))
    monkeypatch.setattr(autoswitch, "vpn_slots", lambda s: ["vpn1", "vpn2"])
    monkeypatch.setattr(autoswitch, "probe_via", lambda s, slot, host, timeout=8: slot == "vpn2")

    first = sw.status_for("testclient", "aniworld.com")
    assert first["state"] == "wait" and first["slot"] == "vpn2"
    assert db.device_warp().get(mac) == "vpn2"
    # Während wait bleibt DNS-Hijack aktiv – sonst lädt die Seite ohne Wechselseite
    assert not autoswitch.is_passed("aniworld.com")
    assert not dns

    sw.state[mac]["since"] -= 60
    assert sw.status_for("testclient", "aniworld.com")["state"] == "ready"
    assert autoswitch.is_passed("aniworld.com")
    assert dns

    # Kein VPN erreicht die Seite → none_found, dann normales Internet
    monkeypatch.setattr(autoswitch, "probe_via", lambda s, slot, host, timeout=8: False)
    sw2 = autoswitch.AutoSwitcher(settings=settings)
    client.app.state.autoswitcher = sw2
    db.set_device_warp(mac, None)
    autoswitch.clear_pass("aniworld.com")
    assert sw2.status_for("testclient", "s.to")["state"] == "none"  # keine Regel
    autoswitch.set_rule("s.to", autoswitch.AUTO)
    assert sw2.status_for("testclient", "s.to")["state"] == "none_found"

    ok = sw2.allow_normal("testclient", "s.to")
    assert ok["state"] == "ready" and ok.get("normal")
    assert db.device_warp().get(mac) == warp.DIRECT
    assert autoswitch.is_passed("s.to")

    r = client.post("/vpn-wechsel/normal", data={"host": "s.to"})
    assert r.status_code == 200 and r.json()["state"] == "ready"

    client.app.state.autoswitcher = None


def test_normal_setting_form(client):
    r = client.post("/auto-wechsel/normal", data={"slot": "direct"}, follow_redirects=False)
    assert r.status_code == 303 and autoswitch.normal_slot() == "direct"
    r = client.post("/auto-wechsel/add", data={"domain": "example.com", "slot": "auto"}, follow_redirects=False)
    assert r.status_code == 303 and autoswitch.load_rules()["example.com"] == "auto"
    page = client.get("/vpn").text
    assert "Automatisch" in page and "Normales Internet" in page
