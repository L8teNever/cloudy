import time
from dataclasses import replace

import pytest

from app import assistent, autoswitch, db, ordnung, policy


def test_custom_name_and_view_groups(client):
    db.upsert_devices([("192.168.178.50", "aa:bb:cc:dd:ee:01", "android-8f3a"), ("192.168.178.51", "aa:bb:cc:dd:ee:02", "lg-tv")])
    r = client.post("/geraete/name", data={"key": "aa:bb:cc:dd:ee:01", "name": "  Simons  Handy ", "back": "/geraete"},
                    follow_redirects=False)
    assert r.status_code == 303
    d = next(x for x in db.devices() if x["mac"] == "aa:bb:cc:dd:ee:01")
    assert d["hostname"] == "Simons Handy" and d["original_name"] == "android-8f3a"
    assert db.device("192.168.178.50")["hostname"] == "Simons Handy"

    client.post("/uebersichtsgruppen", data={"name": "Fernseher"})
    g = ordnung.groups()[0]
    client.post("/geraete/uebersichtsgruppe", data={"key": "aa:bb:cc:dd:ee:02", "gruppe": g["id"]})
    page = client.get("/geraete").text
    assert "Fernseher" in page and "Ohne Gruppe" in page and "Simons Handy" in page
    assert page.index("Fernseher") < page.index("Ohne Gruppe")
    assert "Übersichtsgruppen" in client.get("/gruppen").text
    client.post(f"/uebersichtsgruppen/{g['id']}", data={"aktion": "speichern", "name": "TV", "color": db.GROUP_COLORS[2]})
    assert ordnung.groups()[0]["name"] == "TV"
    client.post(f"/uebersichtsgruppen/{g['id']}", data={"aktion": "loeschen"})
    assert ordnung.groups() == []

    client.post("/geraete/name", data={"key": "aa:bb:cc:dd:ee:01", "name": ""})
    assert db.device("192.168.178.50")["hostname"] == "android-8f3a"


def test_assistent_chat_with_tools(client, monkeypatch):
    db.upsert_devices([("192.168.178.60", "aa:bb:cc:dd:ee:10", "lg-webos-tv")])
    assert "noch kein Schlüssel" in client.get("/assistent").text
    assert client.post("/assistent/chat", json={"messages": [{"role": "user", "text": "hi"}]}).status_code == 400
    client.post("/assistent/einstellungen", data={"key": "AIzaTEST", "model": "gemini-2.5-flash"})
    assert assistent.config() == ("AIzaTEST", "gemini-2.5-flash")
    assert "AIzaTEST" not in client.get("/assistent").text

    sent = []
    replies = iter([
        {"candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": "gruppe_anlegen", "args": {"name": "Fernseher"}}, "thoughtSignature": "sig"}]}}]},
        None,  # wird unten mit der Gruppen-ID gebaut
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "Fertig, der TV ist in **Fernseher**."}]}}]},
    ])

    def fake_request(method, path, body=None, timeout=60.0):
        sent.append((path, body))
        nxt = next(replies)
        if nxt is None:
            gid = ordnung.groups()[0]["id"]
            nxt = {"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": "geraete_zuordnen", "args": {"gruppe_id": gid, "schluessel": ["aa:bb:cc:dd:ee:10", "quatsch"]}}},
                {"functionCall": {"name": "geraet_umbenennen", "args": {"schluessel": "aa:bb:cc:dd:ee:10", "name": "Fernseher Wohnzimmer"}}},
            ]}}]}
        return nxt

    monkeypatch.setattr(assistent, "_request", fake_request)
    r = client.post("/assistent/chat", json={"messages": [{"role": "user", "text": "Sortier meine Geräte"}]}).json()
    assert r["ok"] and "Fernseher" in r["reply"]
    assert len(r["changes"]) == 3
    assert ordnung.group_of()["aa:bb:cc:dd:ee:10"]["name"] == "Fernseher"
    assert db.device("192.168.178.60")["hostname"] == "Fernseher Wohnzimmer"
    # Modell-Antwort mit thoughtSignature unverändert zurückgeschickt, Ergebnisse als functionResponse
    second = sent[1][1]["contents"]
    assert second[1]["parts"][0]["thoughtSignature"] == "sig"
    assert second[2]["parts"][0]["functionResponse"]["name"] == "gruppe_anlegen"
    unknown = sent[2][1]["contents"][-1]["parts"][0]["functionResponse"]["response"]
    assert unknown["unbekannt"] == ["quatsch"]
    assert "gemini-2.5-flash:generateContent" in sent[0][0]


def test_assistent_models(client, monkeypatch):
    client.post("/assistent/einstellungen", data={"key": "k"})
    monkeypatch.setattr(assistent, "_request", lambda *a, **k: {"models": [
        {"name": "models/gemini-2.5-pro", "displayName": "Gemini 2.5 Pro", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
    ]})
    assert client.get("/assistent/modelle").json()["models"] == [{"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"}]
    assert "fehler=" in client.post("/assistent/einstellungen", data={"model": "../x"}, follow_redirects=False).headers["location"]


def _ctx(client, applied=None):
    s = replace(client.app.state.settings, warp_slots=[("warp1", "10"), ("vpn1", "11")])
    return assistent.Ctx(frozenset(), s, (lambda: applied.append(1)) if applied is not None else None)


def test_assistent_profile_und_gruppen(client):
    mac = "aa:bb:cc:dd:ee:20"
    db.upsert_devices([("192.168.178.70", mac, "laptop")])
    ctx = _ctx(client)
    assert assistent.run_tool("geraete_profil_setzen", {"schluessel": [mac], "profil": "gibtsnicht"}, ctx)[0]["fehler"]
    res, change = assistent.run_tool("geraete_profil_setzen", {"schluessel": [mac], "profil": "vpn1"}, ctx)
    assert res["gesetzt"] == ["laptop"] and change and db.device_warp()[mac] == "vpn1"
    assistent.run_tool("geraete_profil_setzen", {"schluessel": [mac], "profil": "standard"}, ctx)
    assert mac not in db.device_warp()

    gid = assistent.run_tool("regelgruppe_anlegen", {"name": "Immer VPN", "profil": "vpn1", "festgelegt": True}, ctx)[0]["id"]
    assistent.run_tool("regelgruppe_geraete", {"id": gid, "hinzufuegen": [mac]}, ctx)
    assert policy.resolve(mac).slot == "vpn1" and policy.resolve(mac).locked
    # Festgelegte Gruppe schlägt die Einzel-Einstellung – wird nicht still überschrieben
    res = assistent.run_tool("geraete_profil_setzen", {"schluessel": [mac], "profil": "warp1"}, ctx)[0]
    assert not res["ok"] and "Immer VPN" in res["festgelegt_durch_gruppe"][0] and mac not in db.device_warp()
    # Nicht genannte Felder bleiben
    assistent.run_tool("regelgruppe_aendern", {"id": gid, "profil": "warp1"}, ctx)
    g = db.group(gid)
    assert g["slot"] == "warp1" and g["locked"] and g["name"] == "Immer VPN"
    assistent.run_tool("regelgruppe_geraete", {"id": gid, "entfernen": [mac]}, ctx)
    assert db.group_members(gid) == set()


def test_assistent_auto_wechsel_laedt_dns_einmal(client, monkeypatch):
    applied = []
    ctx = _ctx(client, applied)
    calls = iter([
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "auto_wechsel_setzen", "args": {"domains": ["https://www.Aniworld.to/x", "kaputt_*x"], "profil": "vpn1"}}},
            {"functionCall": {"name": "auto_wechsel_setzen", "args": {"domains": ["s.to"], "profil": "auto"}}}]}}]},
        assistent.AssistError("Gemini ist gerade nicht erreichbar."),
    ])

    def fake_request(*a, **k):
        nxt = next(calls)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(assistent, "_request", fake_request)
    with pytest.raises(assistent.AssistError):
        assistent.chat([{"role": "user", "text": "Regeln bitte"}], ctx)
    assert autoswitch.load_rules() == {"aniworld.to": "vpn1", "s.to": "auto"}
    assert applied == [1]  # einmal und auch dann, wenn Gemini danach ausfällt

    assert "fehler" in assistent.run_tool("auto_wechsel_setzen", {"domains": ["a.de"], "profil": "direct"}, ctx)[0]
    res = assistent.run_tool("auto_wechsel_entfernen", {"domains": ["aniworld.to", "nix.de"]}, ctx)[0]
    assert res["entfernt"] == ["aniworld.to"] and res["nicht_vorhanden"] == ["nix.de"]


def test_assistent_geraet_details(client):
    now = int(time.time())
    db.upsert_devices([("192.168.178.80", "02:bb:cc:dd:ee:30", "android-77"), ("192.168.178.81", "aa:bb:cc:dd:ee:31", "anderes")])
    db.add_queries([(now, "192.168.178.80", "gateway.icloud.com", "A"), (now, "192.168.178.81", "geheim.de", "A")])
    db.add_traffic([("192.168.178.80", "gateway.icloud.com", 1000, 5_000_000, 1, now)])
    ctx = _ctx(client)
    row = next(r for r in assistent.run_tool("geraete_liste", {}, ctx)[0]["geraete"] if r["ip"] == "192.168.178.80")
    assert row["mac_zufaellig"] is True and row["profil"] == "standard"

    rep = assistent.run_tool("geraet_details", {"geraet": "android"}, ctx)[0]
    assert rep["ip"] == "192.168.178.80"
    assert [d["domain"] for d in rep["domains_top"]] == ["gateway.icloud.com"]  # nur dieses Gerät
    assert rep["letzte_aufrufe"][0]["domain"] == "gateway.icloud.com"
    assert rep["ziele_top"][0]["runter_mb"] == 5.0
    assert rep["aktivitaet"]["tage"][0]["aktive_stunden"] == 1

    # Gerät ohne IP darf nicht die Zahlen des ganzen Netzes bekommen
    empty = assistent._device_report({"ip": "", "name": "x"}, "7t")
    assert "domains_top" not in empty and empty["hinweis"]
