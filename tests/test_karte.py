from app import db, karte, localdomain


def test_karte_page_and_status(client):
    r = client.get("/karte")
    assert r.status_code == 200 and 'name="karte-base" content="/karte"' in r.text
    assert "__V__" not in r.text and "cdnjs" not in r.text
    assert client.get("/karte/simonszimmer/").status_code == 200
    assert client.get("/karte/Quatsch!").status_code == 404
    assert client.get("/karte/simonszimmer/rollladen-fenster/").status_code == 200
    assert client.get("/karte/a/b/c").status_code == 404
    for f in ("karte.css", "karte.js", "cloudy.js", "vendor/three.min.js", "vendor/OrbitControls.js"):
        assert client.get(f"/static/karte/{f}").status_code == 200
    assert "/karte/status" in client.get("/static/karte/cloudy.js").text
    st = client.get("/karte/status").json()
    assert st["screens"] == {} and st["internet"]["screens"] == {}


def test_karte_assign_and_online(client):
    db.upsert_devices([("192.168.178.50", "aa:bb:cc:dd:ee:ff", "fernseher")])
    page = client.get("/karte/einstellungen")
    assert page.status_code == 200 and "fernseher" in page.text
    r = client.post("/karte/einstellungen", data={"geraet_tv-simon": "aa:bb:cc:dd:ee:ff", "geraet_quatsch": "x"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert karte.assignments() == {"tv-simon": "aa:bb:cc:dd:ee:ff"}
    assert karte.status(frozenset()) == {"tv-simon": False}
    assert karte.status(frozenset({"192.168.178.50"})) == {"tv-simon": True}


def test_karte_domain(client):
    r = client.post("/lokale-domain", data={"karte": "haus.kulbarts.com"}, follow_redirects=False)
    assert r.status_code == 303
    assert localdomain.karte_domains() == ["haus.kulbarts.com"]
    h = {"host": "haus.kulbarts.com"}
    start = client.get("/", headers=h)
    assert start.status_code == 200 and 'name="karte-base" content=""' in start.text
    room = client.get("/simonszimmer/", headers=h)
    assert room.status_code == 200 and 'name="karte-base" content=""' in room.text
    assert client.get("/static/karte/karte.js", headers=h).status_code == 200
    assert client.get("/karte/einstellungen", headers=h).status_code == 404
    assert client.get("/karte/status", headers=h).status_code == 200
    assert client.get("/einstellungen", headers=h).status_code == 404
    assert client.get("/simonszimmer/internet/", headers=h).status_code == 200
    assert client.get("/a/b/c", headers=h).status_code == 404
    assert client.get("/einstellungen/x", headers=h).status_code == 404
    assert client.get("/karte/simonszimmer", headers=h, follow_redirects=False).headers["location"] == "/simonszimmer/"
    assert client.post("/lokale-domain", data={"karte": "a.kulbarts.com", "hub": "a.kulbarts.com"}).status_code == 400


def test_karte_internet_menu(client):
    mac = "aa:bb:cc:dd:ee:01"
    db.upsert_devices([("192.168.178.60", mac, "wohnzimmer-tv")])
    karte.save_assignments({"tv-wohnzimmer": mac, "tv-simon": "ip:192.168.178.99"})
    info = client.get("/karte/status").json()["internet"]
    assert {p["slot"] for p in info["profiles"]} >= {"warp1", "vpn1"}
    tv = info["screens"]["tv-wohnzimmer"]
    assert tv["device"] == "wohnzimmer-tv" and tv["choice"] == "" and tv["kind"] == ""

    r = client.post("/karte/internet", json={"screen": "tv-wohnzimmer", "slot": "vpn1"})
    assert r.status_code == 200 and r.json()["ok"]
    assert db.device_warp()[mac] == "vpn1"
    assert r.json()["internet"]["screens"]["tv-wohnzimmer"]["kind"] == "vpn"

    assert client.post("/karte/internet", json={"screen": "tv-wohnzimmer", "slot": "direct"}).json()["ok"]
    assert db.device_warp()[mac] == "direct"
    assert client.post("/karte/internet", json={"screen": "tv-wohnzimmer", "slot": ""}).json()["ok"]
    assert mac not in db.device_warp()

    bad = client.post("/karte/internet", json={"screen": "tv-wohnzimmer", "slot": "vpn99"})
    assert bad.status_code == 400 and "Profil" in bad.json()["error"]
    assert client.post("/karte/internet", json={"screen": "tv-simon", "slot": "warp1"}).status_code == 400
    assert client.post("/karte/internet", json={"screen": "tv-martin", "slot": "warp1"}).status_code == 400


def test_karte_internet_needs_password(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "geheim")
    karte.save_assignments({"tv-wohnzimmer": "aa:bb:cc:dd:ee:02"})
    assert client.get("/karte/status").status_code == 200
    assert client.post("/karte/internet", json={"screen": "tv-wohnzimmer", "slot": "warp1"}).status_code == 401
    r = client.post("/karte/internet", json={"screen": "tv-wohnzimmer", "slot": "warp1"}, auth=("x", "geheim"))
    assert r.status_code == 200


def test_karte_internet_locked_group(client):
    mac = "aa:bb:cc:dd:ee:03"
    karte.save_assignments({"tv-melanie": mac})
    gid = db.create_group("Kinder", slot="warp1", locked=True)
    db.set_group_members(gid, [mac])
    tv = client.get("/karte/status").json()["internet"]["screens"]["tv-melanie"]
    assert tv["locked"] == "Kinder" and tv["kind"] == "warp"
    r = client.post("/karte/internet", json={"screen": "tv-melanie", "slot": "direct"})
    assert r.status_code == 400 and "Kinder" in r.json()["error"]
