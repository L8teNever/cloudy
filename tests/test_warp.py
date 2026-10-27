import os
import time

from app import db, warp
from app.settings import Settings

MAC = "aa:bb:cc:dd:ee:01"


def test_plan_marks_assigned_devices_and_routes_per_tunnel(monkeypatch):
    s = Settings.from_env()
    assert s.warp_slots == [("warp1", "172.29.86.11"), ("warp2", "172.29.86.12"),
                            ("vpn1", "172.29.86.21"), ("vpn2", "172.29.86.22")]
    plan = warp.build_plan(s, {MAC: "warp2", "kaputt": "warp1", "aa:bb:cc:dd:ee:09": "weg"},
                           {"warp1": True, "warp2": False})
    assert plan.mangle_rules == ((
        "-i", "eth0", "-m", "mac", "--mac-source", MAC, "!", "-d", "192.168.178.0/24",
        "-j", "MARK", "--set-mark", "0x102",
    ),)
    assert plan.routes[:2] == (
        ("default", "via", "172.29.86.11", "table", "201"),
        # Tunnel weg -> über die Fritzbox
        ("default", "via", "192.168.178.1", "dev", "eth0", "table", "202"),
    )
    assert plan.routes[2] == ("default", "via", "192.168.178.1", "dev", "eth0", "table", "203")

    monkeypatch.setenv("WARP_KILLSWITCH", "1")
    plan = warp.build_plan(Settings.from_env(), {}, {"warp1": False})
    assert plan.routes[0] == ("unreachable", "default", "table", "201")


def test_read_status(tmp_path):
    s = Settings.from_env()
    assert not warp.read_status(s, "warp1").connected
    os.makedirs(warp.slot_dir(s, "warp1"))
    path = os.path.join(warp.slot_dir(s, "warp1"), "status")
    with open(path, "w") as f:
        f.write("Connected\n")
    assert warp.read_status(s, "warp1") == warp.SlotStatus(True, "verbunden")
    # Container hängt -> alter Status zählt nicht.
    assert not warp.read_status(s, "warp1", now=time.time() + 120).connected


def test_token_saved_privately_and_removed():
    s = Settings.from_env()
    assert not warp.has_token(s, "warp1")
    warp.save_token(s, "warp1", "  abc.def  ")
    path = os.path.join(warp.slot_dir(s, "warp1"), "token")
    assert open(path).read() == "abc.def\n"
    if os.name != "nt":
        assert os.stat(path).st_mode & 0o777 == 0o600
    assert warp.has_token(s, "warp1")
    warp.remove_token(s, "warp1")
    assert not warp.has_token(s, "warp1")


def test_dashboard_login_and_assign(client):
    db.upsert_devices([("192.168.178.25", MAC, "handy")])
    db.add_traffic([("192.168.178.25", "example.com", 1, 1, 1, int(time.time()))])

    r = client.post("/warp/warp1/login", data={"label": "Account Max", "token": "eyJhIjoi"}, follow_redirects=False)
    assert r.status_code == 303
    s = Settings.from_env()
    assert warp.has_token(s, "warp1")
    assert "Account Max" in client.get("/").text

    assert client.post("/warp/warp1/login", data={"token": "mit leerzeichen"}).status_code == 400
    assert client.post("/warp/gibtsnicht/login", data={"token": "x"}).status_code == 404

    r = client.post("/devices/warp", data={"mac": MAC.upper(), "slot": "warp1", "ip": "192.168.178.25"},
                    follow_redirects=False)
    assert r.headers["location"] == "/?ip=192.168.178.25"
    assert db.device_warp() == {MAC: "warp1"}
    detail = client.get("/", params={"ip": "192.168.178.25"}).text
    assert '<option value="warp1" selected>' in detail

    assert client.post("/devices/warp", data={"mac": "nope", "slot": "warp1"}).status_code == 400
    client.post("/devices/warp", data={"mac": MAC, "slot": ""})
    assert db.device_warp() == {}

    client.post("/warp/warp1/logout")
    assert not warp.has_token(s, "warp1")


WG_CONF = (
    "[Interface]\r\nPrivateKey = AAAA\r\nAddress = 10.2.0.2/32\r\nDNS = 10.2.0.1\r\n"
    "[Peer]\r\nPublicKey = BBBB\r\nAllowedIPs = 0.0.0.0/0\r\nEndpoint = 1.2.3.4:51820\r\n"
)


def test_wireguard_config_check():
    assert "\r" not in warp.check_wireguard_config(WG_CONF)
    for bad in ("hallo", WG_CONF.replace("PrivateKey", "Foo"), WG_CONF.replace("[Peer]", ""), "x" * 9000):
        try:
            warp.check_wireguard_config(bad)
        except ValueError:
            continue
        raise AssertionError(bad[:20])


def test_dashboard_vpn_config(client):
    s = Settings.from_env()
    assert warp.kind("vpn1") == "vpn" and warp.kind("warp1") == "warp"
    r = client.post("/warp/vpn1/login", data={"label": "Proton", "config": WG_CONF}, follow_redirects=False)
    assert r.status_code == 303
    path = os.path.join(warp.slot_dir(s, "vpn1"), "wg.conf")
    assert "PrivateKey = AAAA" in open(path).read()
    page = client.get("/").text
    assert "Proton" in page and "WireGuard" in page
    assert client.post("/warp/vpn1/login", data={"config": "quatsch"}).status_code == 400
    assert not warp.has_token(s, "warp1")
    client.post("/warp/vpn1/logout")
    assert not warp.has_token(s, "vpn1")


def test_presence_parse_and_dashboard(client):
    from types import SimpleNamespace

    from app.presence import parse_neigh

    out = (
        "192.168.178.25 lladdr aa:bb:cc:dd:ee:01 REACHABLE\n"
        "192.168.178.26 lladdr aa:bb:cc:dd:ee:02 STALE\n"
        "192.168.178.27  FAILED\n"
        "192.168.178.28  INCOMPLETE\n"
    )
    assert parse_neigh(out) == {
        "192.168.178.25": "REACHABLE", "192.168.178.26": "STALE",
        "192.168.178.27": "FAILED", "192.168.178.28": "FAILED",
    }
    now = int(time.time())
    db.add_traffic([("192.168.178.25", "a.de", 1, 1, 1, now), ("192.168.178.26", "b.de", 1, 1, 1, now)])
    assert set(db.known_ips()) == {"192.168.178.25", "192.168.178.26"}
    client.app.state.presence = SimpleNamespace(online=frozenset({"192.168.178.25"}))
    page = client.get("/").text
    assert "(1 online)" in page and "● online" in page
    api = {d["ip"]: d["online"] for d in client.get("/api/devices").json()}
    assert api == {"192.168.178.25": True, "192.168.178.26": False}


def test_plan_default_and_direct():
    s = Settings.from_env()
    plan = warp.build_plan(s, {MAC: "direct", "aa:bb:cc:dd:ee:02": "warp1"}, {"warp1": True, "vpn1": True}, "vpn1")
    assert plan.mangle_rules == (
        ("-i", "eth0", "-m", "mac", "--mac-source", MAC, "-j", "RETURN"),
        ("-i", "eth0", "-m", "mac", "--mac-source", "aa:bb:cc:dd:ee:02", "!", "-d", "192.168.178.0/24",
         "-j", "MARK", "--set-mark", "0x101"),
        ("-i", "eth0", "-m", "mark", "--mark", "0x0", "!", "-d", "192.168.178.0/24",
         "-j", "MARK", "--set-mark", "0x103"),
    )
    assert warp.build_plan(s, {}, {}, "gibtsnicht").mangle_rules == ()


def test_dashboard_default_and_direct(client):
    db.upsert_devices([("192.168.178.25", MAC, "handy")])
    db.add_traffic([("192.168.178.25", "example.com", 1, 1, 1, int(time.time()))])
    assert client.post("/warp/default", data={"slot": "vpn1"}, follow_redirects=False).status_code == 303
    assert db.get_setting(warp.DEFAULT_KEY) == "vpn1"
    page = client.get("/").text
    assert '<option value="vpn1" selected>' in page and "Standard (VPN 1)" in page
    assert client.post("/warp/default", data={"slot": "quatsch"}).status_code == 404

    client.post("/devices/warp", data={"mac": MAC, "slot": "direct"})
    assert db.device_warp() == {MAC: "direct"}
    assert '<option value="direct" selected>' in client.get("/").text
    client.post("/devices/warp", data={"mac": MAC, "slot": ""})
    assert db.device_warp() == {}

    client.post("/warp/default", data={"slot": ""})
    assert db.get_setting(warp.DEFAULT_KEY) == ""
