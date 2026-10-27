import os
import time

from app import db, warp
from app.settings import Settings

MAC = "aa:bb:cc:dd:ee:01"


def test_plan_marks_assigned_devices_and_routes_per_tunnel(monkeypatch):
    s = Settings.from_env()
    assert s.warp_slots == [("warp1", "172.29.86.11"), ("warp2", "172.29.86.12")]
    plan = warp.build_plan(s, {MAC: "warp2", "kaputt": "warp1", "aa:bb:cc:dd:ee:09": "weg"},
                           {"warp1": True, "warp2": False})
    assert plan.mangle_rules == ((
        "-i", "eth0", "-m", "mac", "--mac-source", MAC, "!", "-d", "192.168.178.0/24",
        "-j", "MARK", "--set-mark", "0x102",
    ),)
    assert plan.routes == (
        ("default", "via", "172.29.86.11", "table", "201"),
        # Tunnel weg -> über die Fritzbox
        ("default", "via", "192.168.178.1", "dev", "eth0", "table", "202"),
    )

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
