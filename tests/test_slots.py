"""Dynamische WARP-/VPN-Profile."""

from app import db, slots, warp
from app.settings import Settings


def test_ensure_defaults_and_compose(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    got = slots.ensure(str(tmp_path))
    assert [s.name for s in got] == ["warp1", "warp2", "vpn1", "vpn2"]
    text = open(tmp_path / slots.COMPOSE_FILE, encoding="utf-8").read()
    assert "cloudy-warp1" in text and "172.29.86.11" in text
    assert "cloudy-vpn1" in text and "image: ghcr.io/l8tenever/cloudy:vpn" in text


def test_add_and_remove_slots(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    slots.ensure(str(tmp_path))
    w3 = slots.add(str(tmp_path), "warp")
    assert w3.name == "warp3" and w3.ip == "172.29.86.13"
    v3 = slots.add(str(tmp_path), "vpn")
    assert v3.name == "vpn3" and v3.ip == "172.29.86.23"
    names = [s.name for s in slots.load(str(tmp_path))]
    assert names == ["warp1", "warp2", "warp3", "vpn1", "vpn2", "vpn3"]
    slots.remove(str(tmp_path), "warp2")
    assert "warp2" not in [s.name for s in slots.load(str(tmp_path))]
    assert "warp3:" in open(tmp_path / slots.COMPOSE_FILE, encoding="utf-8").read()


def test_settings_reads_slots_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("WARP_SLOTS", raising=False)
    slots.ensure(str(tmp_path))
    slots.add(str(tmp_path), "warp")
    s = Settings.from_env()
    assert ("warp3", "172.29.86.13") in s.warp_slots


def test_dashboard_add_remove(client, monkeypatch, tmp_path):
    monkeypatch.setattr(slots, "try_apply", lambda data_dir: "Bitte cloudy slots")
    r = client.post("/warp/neu", data={"kind": "warp"}, follow_redirects=False)
    assert r.status_code == 303
    assert "warp3" in r.headers["location"]
    s = Settings.from_env()
    assert any(name == "warp3" for name, _ in s.warp_slots) or "warp3" in [
        x.name for x in slots.load(s.data_dir)
    ]
    page = client.get("/vpn").text
    assert "warp3" in page and "+ WARP-Profil" in page

    db.set_device_warp("aa:bb:cc:dd:ee:01", "warp3")
    r = client.post("/warp/warp3/loeschen", follow_redirects=False)
    assert r.status_code == 303
    assert "warp3" not in [x.name for x in slots.load(Settings.from_env().data_dir)]
    assert db.device_warp().get("aa:bb:cc:dd:ee:01") is None
