"""Tunnel von unterwegs: cloudy als WireGuard-Server."""

from app import db, dnsmasq, tunnel, warp
from app.settings import Settings


def test_keys_and_peer_config():
    priv = tunnel.new_private_key()
    assert len(priv) == 44 and tunnel.public_key(priv) == tunnel.public_key(priv) != priv

    tunnel.update(True, 51999, "")
    p = tunnel.add_peer("  Handy   Max ")
    assert p["name"] == "Handy Max" and p["ip"] == "10.77.0.2" and p["mac"] == "02:77:67:00:00:02"
    assert tunnel.add_peer("")["ip"] == "10.77.0.3"
    assert db.device("10.77.0.2")["mac"] == "02:77:67:00:00:02"

    conf = tunnel.client_config(p, "abc.myfritz.net")
    assert f"PrivateKey = {p['private_key']}" in conf
    assert "Endpoint = abc.myfritz.net:51999" in conf
    assert "DNS = 10.77.0.1" in conf and "AllowedIPs = 0.0.0.0/0, ::/0" in conf
    assert tunnel.public_key(tunnel.load()["private_key"]) in conf
    assert "Endpoint = [2001:db8::1]:51999" in tunnel.client_config(p, "2001:db8::1")
    assert tunnel.qr_svg(conf).startswith("<svg")

    server = tunnel.server_config(tunnel.load())
    assert "ListenPort = 51999" in server and f"PublicKey = {p['public_key']}" in server
    assert "AllowedIPs = 10.77.0.2/32" in server and "AllowedIPs = 10.77.0.3/32" in server

    assert tunnel.remove_peer("10.77.0.2") and not tunnel.peer("10.77.0.2")
    assert tunnel.add_peer("Neu")["ip"] == "10.77.0.2"  # frei gewordene Adresse wird wieder vergeben


def test_gateway_marks_mirrored_for_tunnel():
    s = Settings.from_env()
    mac = "02:77:67:00:00:05"
    plan = warp.build_plan(
        s, {mac: "vpn1", "aa:bb:cc:dd:ee:01": "warp1"}, {"vpn1": True, "warp1": True}, "warp2",
        tunnel=(tunnel.IFACE, {mac: "10.77.0.5"}),
    )
    assert ("-i", tunnel.IFACE, "-s", "10.77.0.5", "!", "-d", "192.168.178.0/24",
            "-j", "MARK", "--set-mark", "0x103") in plan.mangle_rules
    # Geräte ohne Tunnel-IP tauchen nicht auf, Standard-Profil gilt auch im Tunnel
    assert not any(r[:2] == ("-i", tunnel.IFACE) and "aa:bb:cc:dd:ee:01" in r for r in plan.mangle_rules)
    assert ("-i", tunnel.IFACE, "-m", "mark", "--mark", "0x0", "!", "-d", "192.168.178.0/24",
            "-j", "MARK", "--set-mark", "0x102") in plan.mangle_rules
    assert not any(r[:2] == ("-i", tunnel.IFACE) for r in warp.build_plan(s, {mac: "vpn1"}, {}).mangle_rules)


def test_dnsmasq_listens_in_tunnel():
    s = Settings.from_env()
    assert f"interface={tunnel.IFACE}" not in dnsmasq.build_config(s)
    tunnel.update(True)
    assert f"interface={tunnel.IFACE}" in dnsmasq.build_config(s)


def test_tunnel_pages(client, monkeypatch):
    monkeypatch.setattr(tunnel, "apply", lambda s: "an")
    monkeypatch.setattr(tunnel, "detect_endpoint", lambda s: "abc.myfritz.net")
    monkeypatch.setattr(tunnel, "ensure_port_forward", lambda s: (True, "Fritzbox leitet UDP 51820 weiter."))
    monkeypatch.setattr(tunnel, "handshakes", lambda: {})

    r = client.post("/tunnel/einstellungen", data={"enabled": "1", "port": "51820"}, follow_redirects=False)
    assert r.status_code == 303 and "erfolg=" in r.headers["location"] and tunnel.enabled()
    assert tunnel.endpoint() == "abc.myfritz.net"
    assert "Von unterwegs nach Hause" in client.get("/vpn").text

    r = client.post("/tunnel/geraete", data={"name": "Handy"}, follow_redirects=False)
    assert r.headers["location"] == "/tunnel/geraet/10.77.0.2"
    page = client.get("/tunnel/geraet/10.77.0.2")
    assert page.status_code == 200 and "<svg" in page.text and "Handy" in page.text
    conf = client.get("/tunnel/geraet/10.77.0.2/conf")
    assert "Endpoint = abc.myfritz.net:51820" in conf.text
    assert "cloudy-Handy.conf" in conf.headers["content-disposition"]
    assert "noch nie verbunden" in client.get("/vpn").text

    assert client.post("/tunnel/geraet/10.77.0.2/entfernen", follow_redirects=False).status_code == 303
    assert client.get("/tunnel/geraet/10.77.0.2").status_code == 404

    monkeypatch.setattr(tunnel, "apply", lambda s: "WireGuard fehlt im Kernel des Pi")
    r = client.post("/tunnel/einstellungen", data={"enabled": "1", "port": "51820"}, follow_redirects=False)
    assert "fehler=" in r.headers["location"]
