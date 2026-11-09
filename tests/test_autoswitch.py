"""Auto-Wechsel: Gerät wechselt still beim DNS-Aufruf aufs VPN."""

import struct
import time
import types

import pytest

from app import autoswitch, db, warp, zertwechsel
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
    sw.async_auto = False
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
    sw2.async_auto = False
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


def _hello(name: str) -> bytes:
    """Minimales TLS-ClientHello mit SNI."""
    host = name.encode()
    sni = (len(host) + 3).to_bytes(2, "big") + b"\x00" + len(host).to_bytes(2, "big") + host
    ext = b"\x00\x00" + len(sni).to_bytes(2, "big") + sni
    body = b"\x03\x03" + b"\x00" * 32 + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00" + len(ext).to_bytes(2, "big") + ext
    hs = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs


def test_parse_sni():
    assert zertwechsel.parse_sni(_hello("www.aniworld.to")) == "www.aniworld.to"
    assert zertwechsel.parse_sni(b"GET / HTTP/1.1\r\n") is None
    assert zertwechsel.parse_sni(_hello("x.de")[:20]) is None


def test_cert_device_sees_page_then_released(client, monkeypatch):
    mac = "aa:bb:cc:dd:ee:05"
    db.upsert_devices([("testclient", mac, "laptop")])
    autoswitch.set_rule("aniworld.to", "vpn1")
    monkeypatch.setattr(warp, "may_use", lambda m, slot: True)
    monkeypatch.setattr(warp, "read_status", lambda s, slot, now=None: warp.SlotStatus(True, "verbunden"))
    sw = autoswitch.AutoSwitcher(settings=client.app.state.settings)
    client.app.state.autoswitcher = sw
    intercepted = []
    sw.on_intercept = lambda macs: intercepted.append(macs)

    # Prüfbild über den Prüf-Namen per HTTPS → Gerät hat das Zertifikat → wird umgeleitet
    r = client.get(f"https://{zertwechsel.CHECK_HOST}/zertifikat/check.gif")
    assert r.status_code == 200 and mac in zertwechsel.cert_macs()
    assert intercepted[-1] == [mac]

    # DNS allein schaltet nicht – die Wechselseite soll erscheinen
    sw.on_query("testclient", "aniworld.to")
    assert mac not in db.device_warp()

    # Über die Weiche: Wechselseite unter aniworld.to, ursprüngliche URL bleibt erhalten
    page = client.get("https://aniworld.to/anime/folge?ep=3")
    assert page.status_code == 200 and page.headers["connection"] == "close"
    assert "https://aniworld.to/anime/folge?ep=3" in page.text

    # Wechselseite pollt → VPN an → fertig → Gerät raus aus der Umleitung
    assert client.get("https://aniworld.to/vpn-wechsel/status?host=aniworld.to").json()["state"] == "wait"
    assert db.device_warp().get(mac) == "vpn1"
    sw.state[mac]["since"] -= 60
    assert client.get("https://aniworld.to/vpn-wechsel/status?host=aniworld.to").json()["state"] == "ready"
    assert intercepted[-1] == []

    # Nach der Wartezeit zurück auf Standard → wieder umleiten
    sw.tick(now=sw.state[mac]["seen"] + autoswitch.minutes() * 60 + 1)
    assert intercepted[-1] == [mac]

    # Prüfbild lädt nicht mehr → Zertifikat weg → wieder still wechseln
    client.post("/zertifikat/fehlt")
    assert mac not in zertwechsel.cert_macs() and intercepted[-1] == []
    client.app.state.autoswitcher = None


def test_cert_device_fallback_switches_silently(client, monkeypatch):
    mac = "aa:bb:cc:dd:ee:06"
    db.upsert_devices([("testclient", mac, "laptop")])
    autoswitch.set_rule("aniworld.to", "vpn1")
    zertwechsel.set_cert(mac, True)
    monkeypatch.setattr(warp, "may_use", lambda m, slot: True)
    sw = autoswitch.AutoSwitcher(settings=client.app.state.settings)
    intercepted = []
    sw.on_intercept = lambda macs: intercepted.append(macs)
    monkeypatch.setattr(autoswitch, "CERT_FALLBACK", 3600)
    sw.on_query("testclient", "aniworld.to")
    assert mac not in db.device_warp()
    sw.fallback("testclient", "aniworld.to")  # Wechselseite kam nicht (z. B. IPv6)
    assert db.device_warp().get(mac) == "vpn1"
    assert intercepted[-1] == []


def test_check_pixel_needs_check_host(client):
    db.upsert_devices([("testclient", "aa:bb:cc:dd:ee:07", "laptop")])
    client.get("/zertifikat/check.gif")
    assert "aa:bb:cc:dd:ee:07" not in zertwechsel.cert_macs()


def test_auto_domains_in_intercept_set(client, monkeypatch):
    import app.dnsmasq as dm

    monkeypatch.setenv("GATEWAY_ENABLED", "1")
    monkeypatch.setenv("HTTPS_PORT", "443")
    monkeypatch.setattr(dm, "ipset_supported", lambda: True)
    autoswitch.set_rule("s.to", autoswitch.AUTO)
    s = Settings.from_env()
    sync_redirects_hosts(s)
    conf = open(s.redirects_path, encoding="utf-8").read()
    assert f"ipset=/s.to/{zertwechsel.SET}" in conf
    assert f"address=/{zertwechsel.CHECK_HOST}/{s.pi_ip}" in conf


def test_auto_search_does_not_block_and_reports_probing(client, monkeypatch):
    """Die Suche läuft im Hintergrund: Wechselseite sieht „probing“, danach das zweite VPN."""
    import threading

    mac = "aa:bb:cc:dd:ee:21"
    db.upsert_devices([("testclient", mac, "tv")])
    autoswitch.set_rule("aniworld.to", autoswitch.AUTO)
    sw = autoswitch.AutoSwitcher(settings=client.app.state.settings)
    go, tried = threading.Event(), []

    def probe(s, slot, host, timeout=8):
        tried.append((slot, host))
        go.wait(5)
        return slot == "vpn2"

    monkeypatch.setattr(warp, "read_status", lambda s, slot, now=None: warp.SlotStatus(True, "verbunden"))
    monkeypatch.setattr(autoswitch, "vpn_slots", lambda s: ["vpn1", "vpn2"])
    monkeypatch.setattr(autoswitch, "probe_via", probe)
    assert sw.status_for("testclient", "cdn.aniworld.to")["state"] == "probing"
    assert sw.status_for("testclient", "cdn.aniworld.to")["state"] == "probing"
    go.set()
    for _ in range(200):
        if mac not in sw._picking:
            break
        time.sleep(0.01)
    assert tried == [("vpn1", "aniworld.to"), ("vpn2", "aniworld.to")]  # immer die Hauptseite, in Reihenfolge
    assert sw.status_for("testclient", "aniworld.to") == {"state": "wait", "slot": "vpn2"}


def test_auto_takes_next_vpn_when_current_drops(client, monkeypatch):
    mac = "aa:bb:cc:dd:ee:22"
    db.upsert_devices([("testclient", mac, "tv")])
    autoswitch.set_rule("aniworld.to", autoswitch.AUTO)
    sw = autoswitch.AutoSwitcher(settings=client.app.state.settings)
    sw.async_auto = False
    up = {"vpn1": True, "vpn2": True}
    monkeypatch.setattr(warp, "read_status", lambda s, slot, now=None: warp.SlotStatus(up.get(slot, False), ""))
    monkeypatch.setattr(autoswitch, "vpn_slots", lambda s: ["vpn1", "vpn2"])
    monkeypatch.setattr(autoswitch, "probe_via", lambda s, slot, host, timeout=8: True)
    assert sw.status_for("testclient", "aniworld.to")["slot"] == "vpn1"
    up["vpn1"] = False
    assert sw.status_for("testclient", "aniworld.to")["slot"] == "vpn2"
    assert db.device_warp()[mac] == "vpn2"


def test_probe_rejects_bad_hosts_and_needs_gateway(monkeypatch):
    s = types.SimpleNamespace(gateway_enabled=True, warp_slots=[("warp1", "a"), ("vpn1", "b")])
    monkeypatch.setattr(autoswitch, "_resolve_via", lambda *a: pytest.fail("darf nicht auflösen"))
    assert not autoswitch.probe_via(s, "vpn1", "x');import os;#.aniworld.to")
    assert not autoswitch.probe_via(s, "vpn9", "aniworld.to")
    assert not autoswitch.probe_via(types.SimpleNamespace(gateway_enabled=False, warp_slots=s.warp_slots), "vpn1", "aniworld.to")
    assert autoswitch._slot_mark(s, "vpn1") == 0x102  # dieselbe Markierung wie Plan/ip rule


@pytest.mark.parametrize("status,headers,ok", [
    (200, {}, True), (301, {}, True), (404, {}, True), (403, {}, False), (451, {}, False), (429, {}, False),
    (503, {}, False), (403, {"cf-mitigated": "challenge"}, True),
])
def test_site_answered(status, headers, ok):
    assert autoswitch.site_answered(status, headers) is ok


def test_dns_answer_parsing():
    query = autoswitch._dns_query("aniworld.to", 0x1234)
    assert b"\x08aniworld\x02to\x00" in query
    answer = struct.pack(">HHHHHH", 0x1234, 0x8180, 1, 2, 0, 0) + query[12:]
    answer += b"\xc0\x0c" + struct.pack(">HHIH", 5, 1, 60, 2) + b"\xc0\x0c"  # CNAME wird übersprungen
    answer += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes([1, 2, 3, 4])
    assert autoswitch._parse_a(answer, 0x1234) == ["1.2.3.4"]
    assert autoswitch._parse_a(answer, 0x9999) == []
    assert autoswitch._parse_a(answer[:20], 0x1234) == []
