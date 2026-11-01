"""Auto-Wechsel: Pi zeigt Wechselseite per HTTP (wie Cloudflare-DNS-URL-Redirect)."""

from app import autoswitch, db, warp
from app.dnsmasq import sync_redirects_hosts
from app.settings import Settings


def test_auto_rule_dns_hijack_for_bounce(client):
    autoswitch.set_rule("aniworld.to", autoswitch.AUTO)
    assert autoswitch.covers_host("www.aniworld.to")
    assert "aniworld.to" in autoswitch.hijack_domains()
    s = Settings.from_env()
    sync_redirects_hosts(s)
    assert f"address=/aniworld.to/{s.pi_ip}" in open(s.redirects_path, encoding="utf-8").read()


def test_bounce_url():
    assert autoswitch.switch_bounce_url("aniworld.to", ["cloudy-vpn.kulbarts.com"], https=False) == (
        "http://cloudy-vpn.kulbarts.com/?cf_request_host=aniworld.to"
    )
    assert autoswitch.switch_bounce_url("aniworld.to", local_name="cloudy.lan") == (
        "http://cloudy.lan/vpn-wechsel?cf_request_host=aniworld.to"
    )
    assert autoswitch.switch_bounce_url("aniworld.to", pi_ip="192.168.178.50") == (
        "http://192.168.178.50/vpn-wechsel?cf_request_host=aniworld.to"
    )
    assert autoswitch.switch_bounce_url(
        "aniworld.to", local_name="cloudy.lan", target_url="http://aniworld.to/anime/stream?ep=1"
    ) == (
        "http://cloudy.lan/vpn-wechsel?cf_request_host=aniworld.to&target_url=http%3A%2F%2Faniworld.to%2Fanime%2Fstream%3Fep%3D1"
    )
    assert autoswitch.switch_bounce_url("x") is None


def test_pass_removes_hijack(client):
    autoswitch.set_rule("aniworld.to", "vpn1")
    assert autoswitch.set_pass("aniworld.to", "vpn1")
    assert "aniworld.to" not in autoswitch.hijack_domains()
    assert db.site_routes().get("aniworld.to") == "vpn1"
    assert autoswitch.clear_pass("aniworld.to")
    assert "aniworld.to" in autoswitch.hijack_domains()


def test_pi_redirects_to_wechsel_page(client):
    """Wie Cloudflare: HTTP 307 auf die Wechselseite, Seite wird nicht unter dem Ziel-Host umgeschrieben."""
    autoswitch.set_rule("aniworld.to", autoswitch.AUTO)
    r = client.get("/", headers={"host": "aniworld.to"}, follow_redirects=False)
    assert r.status_code == 307
    assert "cf_request_host=aniworld.to" in r.headers["location"]
    assert r.headers["location"].startswith("http://")
    assert "aniworld.to/?" not in r.headers["location"]
    assert "/vpn-wechsel" in r.headers["location"] or "cloudy" in r.headers["location"]


def test_pi_redirects_preserves_path_and_query(client):
    """Originale URL von davor inklusive Pfad und Query bleibt für die Wechselseite erhalten."""
    autoswitch.set_rule("aniworld.to", autoswitch.AUTO)
    r = client.get("/anime/stream?ep=42", headers={"host": "aniworld.to"}, follow_redirects=False)
    assert r.status_code == 307
    loc = r.headers["location"]
    assert "cf_request_host=aniworld.to" in loc
    assert "target_url=http%3A%2F%2Faniworld.to%2Fanime%2Fstream%3Fep%3D42" in loc


def test_vpn_wechsel_page_target_url(client):
    """Wechselseite übernimmt target_url in das Script und auf den Button."""
    target = "http://aniworld.to/anime/stream?ep=42"
    r = client.get(f"/vpn-wechsel?cf_request_host=aniworld.to&target_url={target}")
    assert r.status_code == 200
    assert target in r.text


def test_dns_query_does_not_switch_vpn(client):
    mac = "aa:bb:cc:dd:ee:03"
    db.upsert_devices([("testclient", mac, "phone")])
    autoswitch.set_rule("aniworld.to", "vpn1")
    sw = autoswitch.AutoSwitcher(settings=client.app.state.settings)
    sw.on_query("testclient", "aniworld.to")
    assert mac not in db.device_warp()


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
    assert "Wechselseite" in page and "HTTP 307" in page
