"""Feste IP-Adressen per DHCP: einzeln und für alle."""

import pytest

from app import db, festeip
from app.dnsmasq import build_config
from app.settings import Settings


@pytest.fixture
def s(monkeypatch):
    monkeypatch.setenv("DHCP_ENABLED", "1")
    monkeypatch.setenv("DNSMASQ_ENABLED", "1")
    return Settings.from_env()


def lines(s):
    with open(festeip.path(s), encoding="utf-8") as f:
        return [x.strip() for x in f if not x.startswith("#")]


def test_config_reads_own_file_and_keeps_hand_file(s):
    conf = build_config(s)
    assert f"dhcp-hostsfile={s.hosts_path}" in conf and f"dhcp-hostsfile={festeip.path(s)}" in conf


def test_set_validates_and_writes(s):
    mac = "aa:bb:cc:dd:ee:01"
    assert festeip.set_ip(s, mac, "192.168.178.50") == ""
    assert festeip.write(s) and lines(s) == [f"{mac},192.168.178.50"]
    assert not festeip.write(s)  # unverändert: dnsmasq muss nicht neu laden
    assert "gültige" in festeip.set_ip(s, mac, "quatsch")
    assert "Heimnetz" in festeip.set_ip(s, mac, "10.0.0.5")
    assert "Pi" in festeip.set_ip(s, mac, s.pi_ip) and "Pi" in festeip.set_ip(s, mac, s.router_ip)
    assert "MAC" in festeip.set_ip(s, "", "192.168.178.51")
    assert "anderes Gerät" in festeip.set_ip(s, "aa:bb:cc:dd:ee:02", "192.168.178.50")
    festeip.remove(mac)
    assert festeip.mapping() == {}


def test_respects_active_lease_and_hand_file(s):
    with open(s.leases_path, "w", encoding="utf-8") as f:
        f.write("1999999999 aa:bb:cc:dd:ee:09 192.168.178.60 tv *\n")
    assert "benutzt gerade" in festeip.set_ip(s, "aa:bb:cc:dd:ee:01", "192.168.178.60")
    assert festeip.set_ip(s, "aa:bb:cc:dd:ee:09", "192.168.178.60") == ""  # das eigene Gerät darf
    with open(s.hosts_path, "w", encoding="utf-8") as f:
        f.write("# aa:bb:cc:dd:ee:03,192.168.178.1\naa:bb:cc:dd:ee:05,set:direkt,192.168.178.70,nas\n")
    assert "Hand" in festeip.set_ip(s, "aa:bb:cc:dd:ee:05", "192.168.178.71")
    assert "Hand" in festeip.set_ip(s, "aa:bb:cc:dd:ee:06", "192.168.178.70")
    assert festeip.set_ip(s, "aa:bb:cc:dd:ee:03", "192.168.178.72") == ""  # Kommentarzeile zählt nicht


def test_all_pins_current_leases_only_when_on(s):
    leases = [("192.168.178.60", "aa:bb:cc:dd:ee:01", "a"), ("192.168.178.61", "aa:bb:cc:dd:ee:02", None)]
    assert not festeip.pin_leases(s, leases) or festeip.mapping() == {}
    festeip.set_all(True)
    assert festeip.pin_leases(s, leases)
    assert festeip.mapping() == {"aa:bb:cc:dd:ee:01": "192.168.178.60", "aa:bb:cc:dd:ee:02": "192.168.178.61"}
    # Nächster Lauf: neue Lease desselben Geräts ändert die feste Adresse nicht
    assert not festeip.pin_leases(s, [("192.168.178.99", "aa:bb:cc:dd:ee:01", "a")])
    assert festeip.mapping()["aa:bb:cc:dd:ee:01"] == "192.168.178.60"


def test_all_keeps_room_for_new_devices(s, monkeypatch):
    festeip.set_all(True)
    start = int(s.dhcp_start.split(".")[-1])
    monkeypatch.setattr(festeip, "_pool_free_limit", lambda st, d: 2)
    leases = [(f"192.168.178.{start + i}", f"aa:bb:cc:dd:ee:{i:02x}", None) for i in range(5)]
    festeip.pin_leases(s, leases)
    assert len(festeip.mapping()) == 2


def test_routes(client, s):
    client.app.state.settings = s
    db.upsert_devices([("192.168.178.80", "aa:bb:cc:dd:ee:80", "drucker")])
    page = client.get("/geraete/192.168.178.80").text
    assert 'action="/geraete/feste-ip"' in page and "Immer diese IP" in page
    r = client.post("/geraete/feste-ip", data={"mac": "aa:bb:cc:dd:ee:80", "ip": "192.168.178.81", "back": "/geraete/192.168.178.80"}, follow_redirects=False)
    assert r.status_code == 303 and "fehler" not in r.headers["location"]
    assert festeip.mapping() == {"aa:bb:cc:dd:ee:80": "192.168.178.81"} and lines(s) == ["aa:bb:cc:dd:ee:80,192.168.178.81"]
    assert "beim nächsten Verbinden" in client.get("/geraete/192.168.178.80").text and "📌 fest" in client.get("/geraete").text
    bad = client.post("/geraete/feste-ip", data={"mac": "aa:bb:cc:dd:ee:80", "ip": "x", "back": "/geraete"}, follow_redirects=False)
    assert "fehler=" in bad.headers["location"]
    client.post("/geraete/feste-ip", data={"mac": "aa:bb:cc:dd:ee:80", "aktion": "aufheben"})
    assert festeip.mapping() == {}

    with open(s.leases_path, "w", encoding="utf-8") as f:
        f.write("1999999999 aa:bb:cc:dd:ee:81 192.168.178.82 tv *\n")
    client.post("/geraete/feste-ips-alle", data={"wert": "1"})
    assert festeip.all_on() and festeip.mapping() == {"aa:bb:cc:dd:ee:81": "192.168.178.82"}
    client.post("/geraete/feste-ips-alle", data={"aktion": "aufheben"})
    assert not festeip.all_on() and festeip.mapping() == {} and lines(s) == []
