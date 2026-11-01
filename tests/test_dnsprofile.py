import socket
import struct
import time

import pytest

from app import db, dnsprofile, warp
from app.settings import Settings

MAC = "aa:bb:cc:dd:ee:01"
MAC2 = "aa:bb:cc:dd:ee:02"
MAC3 = "aa:bb:cc:dd:ee:03"


def test_normalize_url_accepts_cloudflare_forms():
    full = "https://abc123.cloudflare-gateway.com/dns-query"
    assert dnsprofile.normalize_url(full) == full
    assert dnsprofile.normalize_url("  abc123.cloudflare-gateway.com ") == full
    assert dnsprofile.normalize_url("abc123") == full
    assert dnsprofile.normalize_url("") == ""
    for bad in ("http://abc.cloudflare-gateway.com/dns-query", "https://user:pw@x.com/q", "https://-bad-/x"):
        with pytest.raises(ValueError):
            dnsprofile.normalize_url(bad)


def test_profiles_add_set_remove(tmp_path):
    d = str(tmp_path)
    assert dnsprofile.add(d) == "dns1"
    assert dnsprofile.add(d) == "dns2"
    dnsprofile.set_url(d, "dns2", "https://x.cloudflare-gateway.com/dns-query")
    assert dnsprofile.get_url(d, "dns2").startswith("https://x.")
    dnsprofile.remove(d, "dns1")
    assert dnsprofile.names(d) == ["dns2"]
    assert dnsprofile.add(d) == "dns1"  # freie Nummer wird wieder vergeben
    with pytest.raises(ValueError):
        dnsprofile.remove(d, "dns9")


def test_build_config_replaces_only_upstream():
    main = "\n".join([
        "interface=eth0", "server=1.1.1.1", "server=9.9.9.9", "server=/fritz.box/192.168.178.1",
        "dhcp-range=1,2", "enable-ra", "address=/cloudy.lan/192.168.178.2",
    ])
    conf = dnsprofile.build_config(main, "dns3").splitlines()
    assert "server=1.1.1.1" not in conf and "server=9.9.9.9" not in conf
    assert "server=/fritz.box/192.168.178.1" in conf and "address=/cloudy.lan/192.168.178.2" in conf
    assert not any(line.startswith(("dhcp-", "enable-ra")) for line in conf)
    assert f"port={dnsprofile.PORT_BASE + 3}" in conf
    assert f"server=127.0.0.1#{dnsprofile.DOH_PORT_BASE + 3}" in conf


def test_targets_and_rules():
    assign = {MAC: "dns1", MAC2: "warp1", MAC3: "dns2", "kaputt": "dns1"}
    per_mac, others, default = dnsprofile.targets(assign, "dns1", {}, ready={"dns1"})
    # dns2 läuft nicht -> Gerät bleibt beim normalen DNS (und bekommt auch nicht das Standard-DNS-Profil)
    assert per_mac == {MAC: "dns1"}
    assert others == [MAC2, MAC3]
    assert default == "dns1"
    rules = dnsprofile.rules(per_mac, others, default, {})
    port = str(dnsprofile.PORT_BASE + 1)
    assert rules[0] == ["-m", "mac", "--mac-source", MAC, "-j", "REDIRECT", "--to-ports", port]
    assert ["-m", "mac", "--mac-source", MAC2, "-j", "RETURN"] in rules
    assert rules[-1] == ["-j", "REDIRECT", "--to-ports", port]  # alle anderen: Standard

    # Kein DNS-Standard -> keine RETURN-Regeln und kein Sammel-Redirect
    assert dnsprofile.rules({MAC: "dns1"}, [MAC2], "", {}) == [rules[0]]
    # Eingeschränkter Standard: nur freigegebene Geräte
    limited = dnsprofile.rules({}, [], "dns1", {"dns1": {MAC3}})
    assert limited == [["-m", "mac", "--mac-source", MAC3, "-j", "REDIRECT", "--to-ports", port]]
    # Nicht freigegebenes Gerät mit DNS-Profil -> keine Umleitung
    per_mac, _, _ = dnsprofile.targets({MAC: "dns1"}, "", {"dns1": {MAC2}}, ready={"dns1"})
    assert per_mac == {}


def test_redirect_rules_get_protocol():
    rules = [["-m", "mac", "--mac-source", MAC, "-j", "REDIRECT", "--to-ports", "5361"],
             ["-m", "mac", "--mac-source", MAC2, "-j", "RETURN"]]
    out = dnsprofile.expand_protocols(rules)
    assert out[0][:2] == ["-p", "udp"] and out[1][:2] == ["-p", "tcp"]
    assert out[2] == rules[1]


def test_dns_profile_device_is_not_tunneled():
    """Ein DNS-Profil leitet keinen Verkehr um – auch nicht über einen Standard-Tunnel."""
    s = Settings.from_env()
    plan = warp.build_plan(s, {MAC: "dns1"}, {"warp1": True}, "warp1")
    flat = [" ".join(r) for r in plan.mangle_rules]
    assert any(MAC in r and r.endswith("RETURN") for r in flat)
    assert not any(MAC in r and "MARK" in r for r in flat)


def test_status_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = Settings.from_env()
    assert not dnsprofile.read_status(s, "dns1").connected
    dnsprofile.write_status(s, "dns1", "Connected")
    assert dnsprofile.read_status(s, "dns1").connected
    old = dnsprofile.read_status(s, "dns1", now=time.time() + 1000)
    assert not old.connected and "läuft nicht" in old.text


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_forwarder_udp_tcp_and_truncation(monkeypatch):
    fwd = dnsprofile.DohForwarder("https://x.cloudflare-gateway.com/dns-query", _free_port())
    big = b"\x12\x34\x81\x80" + b"\x00\x01\x00\x01\x00\x00\x00\x00" + b"x" * 2000
    answers = {b"small": b"\x12\x34" + b"\x81\x80" + b"\x00" * 8 + b"ok", b"big": big}
    monkeypatch.setattr(fwd, "resolve", lambda q: answers[q[12:].split(b"|")[0]])
    fwd.start()
    try:
        head = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            u.settimeout(3)
            u.sendto(head + b"small", ("127.0.0.1", fwd.port))
            assert u.recvfrom(4096)[0].endswith(b"ok")
            u.sendto(head + b"big", ("127.0.0.1", fwd.port))
            data = u.recvfrom(4096)[0]
            assert len(data) <= dnsprofile.UDP_MAX
            assert struct.unpack("!H", data[2:4])[0] & 0x0200  # TC-Bit -> dnsmasq fragt per TCP
        with socket.create_connection(("127.0.0.1", fwd.port), timeout=3) as t:
            q = head + b"big"
            t.sendall(struct.pack("!H", len(q)) + q)
            n = struct.unpack("!H", t.recv(2, socket.MSG_WAITALL))[0]
            assert t.recv(n, socket.MSG_WAITALL) == big
    finally:
        fwd.stop()


def test_forwarder_status_texts():
    fwd = dnsprofile.DohForwarder("https://x.cloudflare-gateway.com/dns-query", 0)
    assert fwd.status() == "verbindet …"
    fwd.last_error, fwd.last_error_at = "timed out", time.time()
    assert fwd.status().startswith("Fehler")
    fwd.last_ok = time.time() + 1
    assert fwd.status() == "Connected"


def test_dashboard_dns_profile_flow(client):
    r = client.post("/warp/neu", data={"kind": "dns"}, follow_redirects=False)
    assert r.status_code == 303 and "dns1" in r.headers["location"]
    s = client.app.state.settings
    assert dnsprofile.names(s.data_dir) == ["dns1"]

    assert client.post("/warp/dns1/login", data={"token": "http://nein"}).status_code == 400
    client.post("/warp/dns1/login", data={"token": "abc123", "label": "Familienfilter"})
    assert dnsprofile.get_url(s.data_dir, "dns1") == "https://abc123.cloudflare-gateway.com/dns-query"

    page = client.get("/vpn").text
    assert "Familienfilter" in page and "+ DNS-Profil" in page and "Cloudflare DNS-Standort" in page

    # Wie ein VPN-Profil zuweisbar: Gerät, Standard, Gruppe, Mein-Gerät-Auswahl
    assert client.post("/devices/warp", data={"mac": MAC, "slot": "dns1"}).status_code == 200
    assert db.device_warp()[MAC] == "dns1"
    assert client.post("/warp/default", data={"slot": "dns1"}, follow_redirects=False).status_code == 303
    client.post("/mein-auswahl", data={"choice": ["direct", "dns1"]})
    assert "dns1" in warp.mein_allowed()

    # Seiten-Regeln brauchen einen Tunnel
    assert client.post("/seiten-routen/add", data={"domain": "example.com", "slot": "dns1"}).status_code == 400

    client.post("/warp/dns1/logout")
    assert dnsprofile.get_url(s.data_dir, "dns1") == ""

    client.post("/warp/dns1/loeschen")
    assert dnsprofile.names(s.data_dir) == []
    assert MAC not in db.device_warp()
    assert db.get_setting(warp.DEFAULT_KEY) == ""
    assert client.post("/warp/dns1/login", data={"token": "abc"}).status_code == 404
