"""Gerätegruppen: Ordnung + gemeinsames Internet-über, optional festgelegt."""

from app import db, policy, warp


MAC_TV = "aa:bb:cc:dd:ee:10"
MAC_PHONE = "aa:bb:cc:dd:ee:20"
MAC_TAB = "aa:bb:cc:dd:ee:30"


def test_policy_locked_beats_device_and_soft_group():
    top = db.create_group("Prioritaet", slot="vpn1", locked=True)
    tv = db.create_group("TV", slot="direct", locked=True)
    phones = db.create_group("Handys", slot="warp1", locked=False)
    db.set_group_members(tv, [MAC_TV])
    db.set_group_members(top, [MAC_TV])
    db.set_group_members(phones, [MAC_PHONE])
    db.set_device_warp(MAC_TV, "warp1")
    db.set_device_warp(MAC_PHONE, "warp2")

    # Zwei locked-Gruppen: die weiter oben (zuerst angelegt) gewinnt
    assert policy.resolve(MAC_TV).slot == "vpn1"
    assert policy.resolve(MAC_TV).group_name == "Prioritaet"
    assert policy.resolve(MAC_TV).locked

    # Handy: Geräte-Einstellung vor weicher Gruppe
    assert policy.resolve(MAC_PHONE).slot == "warp2"
    assert policy.resolve(MAC_PHONE).source == "device"

    # Handy ohne eigene Einstellung → weiche Gruppe
    db.set_device_warp(MAC_PHONE, None)
    assert policy.resolve(MAC_PHONE).slot == "warp1"
    assert policy.resolve(MAC_PHONE).source == "group"


def test_policy_soft_group_when_no_device_setting():
    gid = db.create_group("Smart Home", slot="direct", locked=False)
    db.set_group_members(gid, [MAC_TAB])
    assert policy.resolve(MAC_TAB) == policy.Effective("direct", "group", gid, "Smart Home", False)
    assert policy.assignments()[MAC_TAB] == "direct"
    db.set_device_warp(MAC_TAB, "warp1")
    assert policy.resolve(MAC_TAB).source == "device"
    assert policy.assignments()[MAC_TAB] == "warp1"


def test_create_update_members_and_pages(client):
    db.upsert_devices([("192.168.178.40", MAC_TV, "living-tv")])
    r = client.post(
        "/gruppen",
        data={"name": "TV", "color": "#3b82f6", "slot": "direct", "locked": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    groups = db.groups()
    assert len(groups) == 1 and groups[0]["slot"] == "direct" and groups[0]["locked"]
    gid = groups[0]["id"]
    assert f"/gruppen/{gid}" in r.headers["location"]

    page = client.get(f"/gruppen/{gid}")
    assert page.status_code == 200 and "living-tv" in page.text

    r = client.post(f"/gruppen/{gid}/mitglieder", data={"mac": MAC_TV}, follow_redirects=False)
    assert r.status_code == 303
    assert db.group_members(gid) == {MAC_TV}

    geraete = client.get("/geraete").text
    assert "TV" in geraete and "fest" in geraete

    assert policy.locked_for(MAC_TV) is not None

    client.post(f"/gruppen/{gid}", data={"name": "Fernseher", "color": "#22c55e", "slot": "direct", "locked": "1"})
    assert db.group(gid)["name"] == "Fernseher"

    client.post(f"/gruppen/{gid}/loeschen", follow_redirects=False)
    assert db.groups() == []


def test_mein_rejects_when_locked(client):
    from urllib.parse import unquote

    gid = db.create_group("TV", slot="direct", locked=True)
    db.set_group_members(gid, [MAC_TV])
    # TestClient meldet sich als Host "testclient".
    db.upsert_devices([("testclient", MAC_TV, "tv")])

    r = client.post("/mein/warp", data={"slot": "warp1"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Festgelegt" in unquote(r.headers["location"])
    assert db.device_warp().get(MAC_TV) is None


def test_warp_tick_uses_group_assignments(monkeypatch):
    from app import policy as pol
    from app.settings import Settings
    from app.warp import build_plan

    gid = db.create_group("TV", slot="direct", locked=True)
    db.set_group_members(gid, [MAC_TV])
    s = Settings.from_env()
    plan = build_plan(s, pol.assignments(), {"warp1": True}, "", None, {})
    assert any(MAC_TV in rule and "RETURN" in rule for rule in plan.mangle_rules)


def test_group_site_routes_only_for_members():
    from app.settings import Settings
    from app.warp import build_plan

    gid = db.create_group("TV", slot="", locked=False)
    db.set_group_members(gid, [MAC_TV])
    db.set_group_site_route(gid, "netflix.com", "vpn1")
    db.set_group_site_route(gid, "bank.de", "direct")
    assert db.group_site_routes(gid) == {"bank.de": "direct", "netflix.com": "vpn1"}
    assert db.group_site_mac_slots() == [(MAC_TV, "direct"), (MAC_TV, "vpn1")]

    s = Settings.from_env()
    plan = build_plan(s, {}, {"vpn1": True}, "", None, {}, db.group_site_mac_slots())
    assert plan.mangle_rules[0] == (
        "-i", "eth0", "-m", "mac", "--mac-source", MAC_TV,
        "-m", "set", "--match-set", "cloudy_sd", "dst", "-j", "RETURN",
    )
    assert MAC_TV in plan.mangle_rules[1] and "cloudy_s3" in plan.mangle_rules[1]
    # Ohne Gruppenregeln: keine Markierung
    assert build_plan(s, {}, {}, "", None, {}, []).mangle_rules == ()


def test_group_site_route_pages(client):
    gid = db.create_group("TV")
    r = client.post(
        f"/gruppen/{gid}/seiten/add",
        data={"domain": "https://www.Netflix.com/x", "slot": "vpn1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.group_site_routes(gid) == {"netflix.com": "vpn1"}
    page = client.get(f"/gruppen/{gid}").text
    assert "netflix.com" in page and "Seiten nur für diese Gruppe" in page
    assert "Globale Seiten-Regeln" in page

    client.post(
        "/seiten-routen/add",
        data={"domain": "bank.de", "slot": "direct", "next": f"/gruppen/{gid}#seitenrouten"},
        follow_redirects=False,
    )
    assert db.site_routes() == {"bank.de": "direct"}
    assert "bank.de" in client.get(f"/gruppen/{gid}").text

    client.post(f"/gruppen/{gid}/seiten/remove", data={"domain": "netflix.com"})
    assert db.group_site_routes(gid) == {}
    client.post(f"/gruppen/{gid}/loeschen")
    assert db.group_site_routes(gid) == {}
