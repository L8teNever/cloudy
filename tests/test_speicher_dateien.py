"""Tests für die Dateien-Seite des Speichers (speicher/speicher/dateien.py): Regeln pro Gerät, Anmeldung, Hoch-/Herunterladen."""
import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "speicher"))

from speicher import auth, dateien, pool  # noqa: E402
from speicher import freigaben as fg  # noqa: E402


@pytest.fixture()
def umgebung(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MOUNT_ROOT", str(tmp_path / "mnt"))
    monkeypatch.setenv("SAMBA_CONF_DIR", str(tmp_path / "samba"))
    auth._fails.clear()
    root = pool.paths()["pool"]
    (root / "Familie" / "Fotos").mkdir(parents=True)
    (root / "Familie" / "hallo.txt").write_text("Hallo Welt")
    (root / "Familie" / "Fotos" / "bild.jpg").write_bytes(b"\xff\xd8bild")
    (root / "Familie" / "seite.html").write_text("<script>alert(1)</script>")
    (root / "Filme").mkdir()
    (root / "Privat").mkdir()
    (root / "Privat" / "geheim.txt").write_text("psst")
    (tmp_path / "draussen.txt").write_text("darf keiner sehen")
    monkeypatch.setattr(pool, "mounts", lambda: {str(root): "fuse.mergerfs"})
    geraete = [fg.Geraet("pc", "PC", "testclient")]  # TestClient kommt von der „IP“ testclient
    monkeypatch.setattr(fg, "geraete", lambda max_age=30: (geraete, ""))
    e = fg.Einstellungen(benutzer=["simon", "mama"], freigaben=[
        fg.Freigabe("Familie", "Familie", standard="offen"),
        fg.Freigabe("Filme", "Filme", standard="offen", nur_lesen=True),
        fg.Freigabe("Privat", "Privat", standard="passwort", benutzer=["simon"]),
        fg.Freigabe("Versteckt", "Privat", standard="versteckt"),
    ])
    e.save()
    fg.set_web_password("simon", "geheim123")
    fg.set_web_password("mama", "mama1234")
    return root


@pytest.fixture()
def web(umgebung):
    from fastapi.testclient import TestClient

    with TestClient(dateien.app) as client:
        client.get("/")  # Sitzungs-Cookie holen
        yield client


def _csrf(web) -> str:
    return dateien.csrf_token(web.cookies.get(dateien.COOKIE))


def _login(web, name="simon", pw="geheim123"):
    return web.post("/login", data={"csrf": _csrf(web), "name": name, "passwort": pw, "weiter": "/"}, follow_redirects=False)


def test_start_zeigt_nur_sichtbare(web):
    page = web.get("/").text
    assert "Familie" in page and "Filme" in page and "Privat" in page
    assert "Versteckt" not in page
    assert "Anmelden zum Öffnen" in page  # Privat nur mit Passwort


def test_offen_auflisten_oeffnen_herunterladen(web):
    page = web.get("/d/Familie")
    assert page.status_code == 200 and "hallo.txt" in page.text and "Fotos" in page.text
    r = web.get("/d/Familie/hallo.txt")
    assert r.text == "Hallo Welt" and r.headers["content-disposition"].startswith("inline")
    assert "sandbox" in r.headers["content-security-policy"]
    r = web.get("/d/Familie/hallo.txt?dl=1")
    assert r.headers["content-disposition"].startswith("attachment")
    r = web.get("/d/Familie/hallo.txt", headers={"Range": "bytes=0-4"})
    assert r.status_code == 206 and r.text == "Hallo"
    assert web.get("/d/Familie/Fotos").status_code == 200


def test_html_wird_nicht_ausgefuehrt(web):
    r = web.get("/d/Familie/seite.html")
    assert r.headers["content-type"].startswith("text/plain")


def test_passwort_freigabe_braucht_anmeldung(web):
    r = web.get("/d/Privat", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?weiter=")
    assert web.get("/d/Privat/geheim.txt", follow_redirects=False).status_code == 303
    assert "falsch" in _login(web, pw="falsch").text
    r = _login(web)
    assert r.status_code == 303
    assert web.get("/d/Privat/geheim.txt").text == "psst"


def test_falscher_benutzer_darf_nicht(web):
    _login(web, "mama", "mama1234")
    assert web.get("/d/Privat").status_code == 403
    assert web.get("/d/Familie").status_code == 200  # offen geht für alle


def test_versteckt_und_regel_pro_geraet(web, umgebung):
    assert web.get("/d/Versteckt").status_code == 404
    assert web.get("/d/Gibtsnicht").status_code == 404
    e = fg.Einstellungen.load()
    e.get("Familie").geraete = {"pc": "versteckt"}
    e.get("Versteckt").geraete = {"pc": "offen"}
    e.save()
    assert web.get("/d/Familie").status_code == 404
    assert web.get("/d/Versteckt/geheim.txt").text == "psst"


def test_kein_ausbruch_aus_der_freigabe(web, umgebung):
    os.symlink(umgebung.parent.parent / "draussen.txt", umgebung / "Familie" / "link.txt")
    for url in ("/d/Familie/../Privat/geheim.txt", "/d/Familie/%2e%2e/Privat/geheim.txt", "/d/Familie/link.txt",
                "/d/Familie/Fotos/..%2f..%2fPrivat/geheim.txt"):
        r = web.get(url, follow_redirects=False)
        assert r.status_code in (303, 400, 403, 404), url  # 303 = Anmeldung (der Client macht aus /../ schon /d/Privat)
        assert "psst" not in r.text and "darf keiner" not in r.text


def test_hochladen(web, umgebung):
    csrf = _csrf(web)
    r = web.put("/api/hochladen/Familie/neu.txt", content=b"neu", headers={"X-CSRF": csrf})
    assert r.status_code == 200 and (umgebung / "Familie" / "neu.txt").read_bytes() == b"neu"
    r = web.put("/api/hochladen/Familie/neu.txt", content=b"x", headers={"X-CSRF": csrf})
    assert r.status_code == 409
    r = web.put("/api/hochladen/Familie/neu.txt?ersetzen=1", content=b"zwei", headers={"X-CSRF": csrf})
    assert r.status_code == 200 and (umgebung / "Familie" / "neu.txt").read_bytes() == b"zwei"
    r = web.put("/api/hochladen/Familie/Urlaub/2026/a.jpg", content=b"a", headers={"X-CSRF": csrf})  # ganzer Ordner
    assert r.status_code == 200 and (umgebung / "Familie" / "Urlaub" / "2026" / "a.jpg").exists()
    assert web.put("/api/hochladen/Familie/x.txt", content=b"x").status_code == 403  # ohne CSRF
    assert web.put("/api/hochladen/Filme/x.txt", content=b"x", headers={"X-CSRF": csrf}).status_code == 403  # nur lesen
    assert web.put("/api/hochladen/Privat/x.txt", content=b"x", headers={"X-CSRF": csrf}).status_code == 401
    assert web.put("/api/hochladen/Familie/..%2f..%2fboese.txt", content=b"x", headers={"X-CSRF": csrf}).status_code in (400, 403)
    assert not list((umgebung / "Familie").glob(".*hochladen*"))


def test_ordner_umbenennen_loeschen(web, umgebung):
    csrf = _csrf(web)
    r = web.post("/aktion/Familie/ordner", data={"csrf": csrf, "pfad": "", "neu": "Neu"}, follow_redirects=False)
    assert "ok=" in r.headers["location"] and (umgebung / "Familie" / "Neu").is_dir()
    r = web.post("/aktion/Familie/umbenennen", data={"csrf": csrf, "pfad": "hallo.txt", "neu": "Hi.txt"}, follow_redirects=False)
    assert "ok=" in r.headers["location"] and (umgebung / "Familie" / "Hi.txt").exists()
    r = web.post("/aktion/Familie/umbenennen", data={"csrf": csrf, "pfad": "Hi.txt", "neu": "../x"}, follow_redirects=False)
    assert "fehler=" in r.headers["location"]
    r = web.post("/aktion/Familie/loeschen", data={"csrf": csrf, "pfad": "Fotos"}, follow_redirects=False)
    assert "ok=" in r.headers["location"] and not (umgebung / "Familie" / "Fotos").exists()
    r = web.post("/aktion/Familie/loeschen", data={"csrf": csrf, "pfad": ""}, follow_redirects=False)
    assert "fehler=" in r.headers["location"] and (umgebung / "Familie").is_dir()
    assert web.post("/aktion/Familie/loeschen", data={"csrf": "falsch", "pfad": "Hi.txt"}).status_code == 403
    assert (umgebung / "Familie" / "Hi.txt").exists()


def test_ordner_als_zip(web):
    r = web.get("/d/Familie?zip=1")
    assert r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert sorted(names) == ["Fotos/bild.jpg", "hallo.txt", "seite.html"]


def test_web_passwort_wird_mit_benutzer_entfernt(umgebung):
    assert fg.check_login("simon", "geheim123") and not fg.check_login("simon", "falsch")
    assert not fg.check_login("niemand", "geheim123")
    fg.set_web_password("simon", None)
    assert not fg.check_login("simon", "geheim123")


# ---------- Geräte-Anmeldung und Rechte ----------

@pytest.fixture()
def handy(umgebung, monkeypatch):
    """Ein Gerät mit echter IPv4 (für die Geräte-Anmeldung), Samba-Abgleich ohne Dienste."""
    geraete = [fg.Geraet("aa:bb", "Handy", "192.168.178.50")]
    monkeypatch.setattr(fg, "geraete", lambda max_age=30: (geraete, ""))
    monkeypatch.setattr(dateien, "_ip", lambda request: "192.168.178.50")
    monkeypatch.setattr(fg.dienste, "abgleichen", lambda: fg.write_configs())
    return umgebung


def test_geraet_anmelden_gibt_samba_rechte(handy, web):
    fg.set_recht("simon", "erstellen")
    r = web.post("/geraet", data={"csrf": _csrf(web), "name": "simon", "passwort": "geheim123"}, follow_redirects=False)
    assert r.status_code == 303
    e = fg.Einstellungen.load()
    assert e.geraet_login["aa:bb"]["benutzer"] == "simon"
    conf = (Path(os.environ["SAMBA_CONF_DIR"]) / "hosts" / "192.168.178.50.conf").read_text()
    privat = conf.split("[Privat]")[1].split("\n[")[0]
    assert "guest ok = yes" in privat and "worm" in privat  # ohne Passwort, nur erstellen
    assert "[Anmelden]" in conf
    html = (Path(os.environ["SAMBA_CONF_DIR"]) / "anmelden" / fg.ANMELDE_DATEI).read_text()
    assert "/geraet" in html
    # Ohne Cookie zählt das Gerät als angemeldet
    web.cookies.clear()
    web.get("/")
    assert web.get("/d/Privat").status_code == 200
    # Nur erstellen: anlegen ja, löschen nein
    ok = web.put("/api/hochladen/Privat/neu.txt", content=b"x", headers={"x-csrf": _csrf(web)})
    assert ok.status_code == 200
    r = web.post("/aktion/Privat/loeschen", data={"csrf": _csrf(web), "pfad": "geheim.txt"}, follow_redirects=False)
    assert "fehler=" in r.headers["location"] and (handy / "Privat" / "geheim.txt").exists()
    # Abmelden entfernt die Geräte-Regel
    web.post("/geraet/abmelden", data={"csrf": _csrf(web)})
    assert "aa:bb" not in fg.Einstellungen.load().geraet_login


def test_nur_lesen_recht(umgebung, web):
    fg.set_recht("mama", "lesen")
    _login(web, "mama", "mama1234")
    r = web.put("/api/hochladen/Familie/x.txt", content=b"x", headers={"x-csrf": _csrf(web)})
    assert r.status_code == 403
    conf = fg.render(fg.Einstellungen.load(), None)
    assert "read list = mama" in conf.split("[Familie]")[1].split("\n[")[0]


def test_rechte_pro_freigabe_und_sperren(umgebung, web):
    fg.set_freigabe_rechte("Familie", {"mama": "gesperrt", "simon": "lesen"})
    e = fg.Einstellungen.load()
    familie = fg.render(e, None).split("[Familie]")[1].split("\n[")[0]
    assert "invalid users = mama" in familie and "read list = simon" in familie
    _login(web, "mama", "mama1234")
    assert "Familie" not in web.get("/").text
    assert web.get("/d/Familie").status_code == 404
    _login(web)
    r = web.put("/api/hochladen/Familie/x.txt", content=b"x", headers={"x-csrf": _csrf(web)})
    assert r.status_code == 403
    fg.set_freigabe_rechte("Familie", {"simon": ""})  # wieder wie überall
    assert "simon" not in fg.Einstellungen.load().get("Familie").rechte


# ---------- Pakete: viele Dateien auf einmal, Entpacken ----------

def _tar(dateien_: dict[str, bytes]) -> bytes:
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, data in dateien_.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_paket_hochladen_entpackt_direkt(umgebung, web):
    data = _tar({"Welt/region/r.0.0.mca": b"a" * 5000, "Welt/level.dat": b"lvl", "hallo.txt": b"neu", "../boese.txt": b"x"})
    r = web.put("/api/paket/Familie/Spiele", content=data, headers={"x-csrf": _csrf(web)})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] == 3 and j["uebersprungen"] == [] and len(j["fehler"]) == 1
    assert (umgebung / "Familie" / "Spiele" / "Welt" / "region" / "r.0.0.mca").read_bytes() == b"a" * 5000
    assert not (umgebung / "Familie" / "boese.txt").exists()
    # Nochmal: alles schon da -> übersprungen, mit ersetzen=1 überschrieben
    r = web.put("/api/paket/Familie/Spiele", content=_tar({"hallo.txt": b"zwei"}), headers={"x-csrf": _csrf(web)})
    assert r.json()["uebersprungen"] == ["hallo.txt"]
    r = web.put("/api/paket/Familie/Spiele?ersetzen=1", content=_tar({"hallo.txt": b"zwei"}), headers={"x-csrf": _csrf(web)})
    assert r.json()["ok"] == 1 and (umgebung / "Familie" / "Spiele" / "hallo.txt").read_bytes() == b"zwei"
    # Nur lesen: abgelehnt
    assert web.put("/api/paket/Filme", content=data, headers={"x-csrf": _csrf(web)}).status_code == 403


def test_zip_entpacken_im_ordner(umgebung, web):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a/b.txt", "drin")
        zf.writestr("../../raus.txt", "nein")
    (umgebung / "Familie" / "paket.zip").write_bytes(buf.getvalue())
    assert "Entpacken" in web.get("/d/Familie").text
    r = web.post("/aktion/Familie/entpacken", data={"csrf": _csrf(web), "pfad": "paket.zip"}, follow_redirects=False)
    assert "fehler=" in r.headers["location"]  # der böse Eintrag wird gemeldet
    assert (umgebung / "Familie" / "paket" / "a" / "b.txt").read_text() == "drin"
    assert not (umgebung / "raus.txt").exists() and not (umgebung.parent / "raus.txt").exists()
