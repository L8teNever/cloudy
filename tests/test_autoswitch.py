"""Auto-Wechsel: Gerät wechselt still beim DNS-Aufruf aufs VPN."""

from app import autoswitch, db, warp
from app.dnsmasq import sync_redirects_hosts
from app.settings import Settings


def test_fixed_rule_is_site_route_without_hijack(client, monkeypatch):
    """Kein DNS auf den Pi mehr – die Seite läuft per ipset gleich übers VPN."""
    import app.dnsmasq as dm

    monkeypatch.setenv("GATEWAY_ENABLED", "1")
    monkeypatch.setattr(dm, "ipset_supported", lambda: True)
    autoswitch.set_rule("aniworld.to", "vpn1")
    autoswitch.set_rule("s.to", autoswitch.AUTO)
    assert autoswitch.covers_host("www.aniworld.to")
    assert autoswitch.fixed_routes() == {"aniworld.to": "vpn1"}
    s = Settings.from_env()
    sync_redirects_hosts(s)
    conf = open(s.redirects_path, encoding="utf-8").read()
    assert f"address=/aniworld.to/{s.pi_ip}" not in conf
    assert "ipset=/aniworld.to/" in conf
    assert "/s.to/" not in conf


def test_pass_sets_site_route(client):
    autoswitch.set_rule("aniworld.to", "vpn1")
    assert autoswitch.set_pass("aniworld.to", "vpn1")
    assert autoswitch.is_passed("aniworld.to")
    assert db.site_routes().get("aniworld.to") == "vpn1"
    assert autoswitch.clear_pass("aniworld.to")
    assert "aniworld.to" not in db.site_routes()


def test_target_host_not_redirected(client):
    """Die Ziel-Domain wird nicht mehr auf den Pi umgebogen – keine 307 zur Wechselseite."""
    autoswitch.set_rule("aniworld.to", autoswitch.AUTO)
    r = client.get("/", headers={"host": "aniworld.to"}, follow_redirects=False)
    assert r.status_code != 307


def test_vpn_wechsel_page_target_url(client):
    """Wechselseite übernimmt target_url in das Script und auf den Button."""
    target = "http://aniworld.to/anime/stream?ep=42"
    r = client.get(f"/vpn-wechsel?cf_request_host=aniworld.to&target_url={target}")
    assert r.status_code == 200
    assert target in r.text


def test_dns_query_switches_vpn_silently(client, monkeypatch):
    mac = "aa:bb:cc:dd:ee:03"
    db.upsert_devices([("testclient", mac, "phone")])
    autoswitch.set_rule("aniworld.to", "vpn1")
    sw = autoswitch.AutoSwitcher(settings=client.app.state.settings)
    monkeypatch.setattr(warp, "may_use", lambda m, slot: True)
    sw.on_query("testclient", "www.aniworld.to")
    assert db.device_warp().get(mac) == "vpn1"
    assert sw.state[mac]["host"] == "aniworld.to"
    # Nach der Wartezeit ohne Aufruf zurück auf Standard
    sw.tick(now=sw.state[mac]["seen"] + autoswitch.minutes() * 60 + 1)
    assert mac not in db.device_warp()


def test_dns_query_auto_picks_reachable_vpn(client, monkeypatch):
    mac = "aa:bb:cc:dd:ee:04"
    db.upsert_devices([("testclient", mac, "tv")])
    autoswitch.set_rule("aniworld.to", autoswitch.AUTO)
    sw = autoswitch.AutoSwitcher(settings=client.app.state.settings)
    sw.async_auto = False
    dns = []
    sw.on_dns_change = lambda: dns.append(1)
    monkeypatch.setattr(warp, "may_use", lambda m, slot: True)
    monkeypatch.setattr(warp, "read_status", lambda s, slot, now=None: warp.SlotStatus(True, "verbunden"))
    monkeypatch.setattr(autoswitch, "vpn_slots", lambda s: ["vpn1", "vpn2"])
    monkeypatch.setattr(autoswitch, "probe_via", lambda s, slot, host, timeout=8: slot == "vpn2")
    sw.on_query("testclient", "aniworld.to")
    assert db.device_warp().get(mac) == "vpn2"
    assert db.site_routes().get("aniworld.to") == "vpn2" and dns


def test_auto_picks_vpn_and_normal_fallback(client, monkeypatch):
    mac = "aa:bb:cc:dd:ee:02"
    db.upsert_devices([("testclient", mac, "tablet")])
    autoswitch.set_rule("aniworld.to", autoswitch.AUTO)
    autoswitch.set_normal_slot(warp.DIRECT)
    settings = client.app.state.settings
    sw = autoswitch.AutoSwitcher(settings=settings)
    client.app.state.autoswitcher = sw
    dns = []
    sw.on_dns_change = lambda: dns.append(1)
    monkeypatch.setattr(warp, "read_status", lambda s, slot, now=None: warp.SlotStatus(True, "verbunden"))
    monkeypatch.setattr(autoswitch, "vpn_slots", lambda s: ["vpn1", "vpn2"])
    monkeypatch.setattr(autoswitch, "probe_via", lambda s, slot, host, timeout=8: slot == "vpn2")

    first = sw.status_for("testclient", "aniworld.to")
    assert first["state"] == "wait" and first["slot"] == "vpn2"
    sw.state[mac]["since"] -= 60
    assert sw.status_for("testclient", "aniworld.to")["state"] == "ready"
    assert autoswitch.is_passed("aniworld.to") and dns

    monkeypatch.setattr(autoswitch, "probe_via", lambda s, slot, host, timeout=8: False)
    sw2 = autoswitch.AutoSwitcher(settings=settings)
    client.app.state.autoswitcher = sw2
    db.set_device_warp(mac, None)
    autoswitch.clear_pass("aniworld.to")
    autoswitch.set_rule("s.to", autoswitch.AUTO)
    assert sw2.status_for("testclient", "s.to")["state"] == "none_found"
    assert sw2.allow_normal("testclient", "s.to")["state"] == "ready"
    client.app.state.autoswitcher = None


def test_normal_setting_form(client):
    assert client.post("/auto-wechsel/normal", data={"slot": "direct"}, follow_redirects=False).status_code == 303
    assert client.post("/auto-wechsel/add", data={"domain": "example.com", "slot": "auto"}, follow_redirects=False).status_code == 303
    page = client.get("/vpn").text
    assert "still im Hintergrund" in page
