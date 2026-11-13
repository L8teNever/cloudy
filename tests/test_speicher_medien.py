"""Tests für automatische MP4-Optimierung (speicher/speicher/medien.py)."""
import os
import struct
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "speicher"))

from speicher import auth, dateien, medien, pool  # noqa: E402
from speicher import freigaben as fg  # noqa: E402


def _box(typ: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def _box64(typ: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4sQ", 1, typ, 16 + len(payload)) + payload


@pytest.fixture(autouse=True)
def medien_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MOUNT_ROOT", str(tmp_path / "mnt"))
    monkeypatch.setenv("SAMBA_CONF_DIR", str(tmp_path / "samba"))
    auth._fails.clear()
    medien._geprueft.clear()
    medien._sofort.clear()
    medien._ffmpeg_fehlt_gemeldet = False
    medien._state.update(aktuell="", optimiert=0, fehler="")


def test_ist_offen_erkennt_fremden_prozess(tmp_path):
    import subprocess

    datei = tmp_path / "offen.mp4"
    datei.write_bytes(b"1234")
    assert not medien._ist_offen(datei)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; fh = open(sys.argv[1], 'rb'); print('ready', flush=True); time.sleep(10)", str(datei)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert medien._ist_offen(datei)
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    assert not medien._ist_offen(datei)


def test_analyse_mp4_erkennt_alle_formen(tmp_path):
    ok = tmp_path / "ok.mp4"
    ok.write_bytes(_box(b"ftyp", b"isom") + _box(b"moov", _box(b"mvhd", b"\x00" * 20)) + _box(b"mdat", b"\x00" * 32))
    assert medien.analyse_mp4(ok) == "ok"

    hinten = tmp_path / "hinten.mp4"
    hinten.write_bytes(_box(b"ftyp", b"isom") + _box(b"mdat", b"\x00" * 32) + _box(b"moov", _box(b"mvhd", b"\x00" * 20)))
    assert medien.analyse_mp4(hinten) == "moov_hinten"

    frag = tmp_path / "frag.mp4"
    frag.write_bytes(
        _box(b"ftyp", b"isom")
        + _box(b"moov", _box(b"mvhd", b"\x00" * 20) + _box(b"mvex", b"\x00" * 8))
        + _box(b"moof", b"\x00" * 16)
        + _box(b"mdat", b"\x00" * 32)
    )
    assert medien.analyse_mp4(frag) == "fragmented"

    mvex = tmp_path / "mvex.mp4"
    mvex.write_bytes(_box(b"ftyp", b"isom") + _box(b"moov", _box(b"mvex", b"\x00" * 8)) + _box(b"mdat", b"\x00" * 32))
    assert medien.analyse_mp4(mvex) == "fragmented"

    gross = tmp_path / "gross64.mp4"
    gross.write_bytes(_box(b"ftyp", b"isom") + _box(b"moov", _box(b"mvhd", b"\x00" * 20)) + _box64(b"mdat", b"\x00" * 32))
    assert medien.analyse_mp4(gross) == "ok"

    kaputt = tmp_path / "kaputt.mp4"
    kaputt.write_bytes(_box(b"ftyp", b"isom") + struct.pack(">I4s", 99999, b"mdat") + b"\x00" * 16)
    assert medien.analyse_mp4(kaputt) == "unbekannt"

    fremd = tmp_path / "fremd.mp4"
    fremd.write_bytes(b"\x1a\x45\xdf\xa3matroska-nicht-mp4")
    assert medien.analyse_mp4(fremd) == "unbekannt"


def test_optimieren_wartet_auf_ruhezeit_und_offene_datei(tmp_path, monkeypatch):
    frag = tmp_path / "film.mp4"
    frag.write_bytes(_box(b"ftyp", b"isom") + _box(b"moov") + _box(b"moof", b"\x00" * 16) + _box(b"mdat", b"\x00" * 512))
    monkeypatch.setattr(medien.shutil, "which", lambda cmd: "/usr/bin/ffmpeg")

    calls = []
    def fake_ffmpeg(ffmpeg, src, dst):
        calls.append(src)
        dst.write_bytes(_box(b"ftyp", b"isom") + _box(b"moov", _box(b"mvhd", b"\x00" * 20)) + _box(b"mdat", b"\x00" * 512))
        return True, ""
    monkeypatch.setattr(medien, "_run_ffmpeg", fake_ffmpeg)

    # Gerade erst geändert -> noch nicht anfassen (Samba könnte noch schreiben)
    assert not medien.optimieren(frag, sofort=False, now=time.time())
    assert calls == []

    # Datei noch von einem Prozess geöffnet -> nicht anfassen
    monkeypatch.setattr(medien, "_ist_offen", lambda p: True)
    assert not medien.optimieren(frag, sofort=True)
    assert calls == []

    # Jetzt frei -> wird umgepackt und ersetzt, danach als 'ok' im Cache
    monkeypatch.setattr(medien, "_ist_offen", lambda p: False)
    assert medien.optimieren(frag, sofort=False, now=time.time() + 30)
    assert len(calls) == 1
    assert medien.analyse_mp4(frag) == "ok"
    assert medien.status()["optimiert"] == 1
    assert not list(tmp_path.glob("*.tvfix-*"))

    # Zweiter Lauf fasst die fertige Datei nicht mehr an
    assert not medien.optimieren(frag, sofort=True)
    assert len(calls) == 1


def test_optimieren_ueberschreibt_nicht_bei_aenderung_waehrend_ffmpeg(tmp_path, monkeypatch):
    frag = tmp_path / "aendert_sich.mp4"
    frag.write_bytes(_box(b"ftyp", b"isom") + _box(b"moov") + _box(b"moof", b"\x00" * 16) + _box(b"mdat", b"\x00" * 512))
    monkeypatch.setattr(medien.shutil, "which", lambda cmd: "/usr/bin/ffmpeg")

    def fake_ffmpeg(ffmpeg, src, dst):
        dst.write_bytes(_box(b"ftyp", b"isom") + _box(b"moov", _box(b"mvhd", b"\x00" * 20)) + _box(b"mdat", b"\x00" * 512))
        # Während ffmpeg läuft, überschreibt der Nutzer die Quelldatei
        src.write_bytes(_box(b"ftyp", b"isom") + _box(b"moov") + _box(b"mdat", b"NEU" * 300))
        return True, ""

    monkeypatch.setattr(medien, "_run_ffmpeg", fake_ffmpeg)
    assert not medien.optimieren(frag, sofort=True)
    assert b"NEU" in frag.read_bytes()
    assert not list(tmp_path.glob("*.tvfix-*"))


def test_optimieren_verwirft_ungueltige_ausgabe(tmp_path, monkeypatch):
    frag = tmp_path / "kaputt.mp4"
    orig = _box(b"ftyp", b"isom") + _box(b"moov") + _box(b"moof", b"\x00" * 16) + _box(b"mdat", b"\x00" * 512)
    frag.write_bytes(orig)
    monkeypatch.setattr(medien.shutil, "which", lambda cmd: "/usr/bin/ffmpeg")
    monkeypatch.setattr(medien, "_run_ffmpeg", lambda f, s, d: (d.write_bytes(b"zu klein"), (True, ""))[1])

    assert not medien.optimieren(frag, sofort=True)
    assert frag.read_bytes() == orig
    assert "ungültig" in medien.status()["fehler"] or "kein gültiges MP4" in medien.status()["fehler"]


def test_scan_und_ziel_ordner(tmp_path, monkeypatch):
    root = pool.paths()["pool"]
    (root / "Familie" / "Unter").mkdir(parents=True)
    (root / "wiederhergestellt").mkdir(parents=True)
    (root / ".mergerfs").write_text("")
    monkeypatch.setattr(pool, "mounts", lambda: {str(root): "fuse.mergerfs"})
    monkeypatch.setattr(medien.shutil, "which", lambda cmd: "/usr/bin/ffmpeg")

    e = fg.Einstellungen(freigaben=[
        fg.Freigabe("Familie", "Familie", dlna=True),
        fg.Freigabe("Unter", "Familie/Unter"),
    ])
    e.save()
    assert medien.ziel_ordner() == [root / "Familie"]

    frag = root / "Familie" / "Unter" / "video.m4v"
    frag.write_bytes(_box(b"ftyp", b"M4V ") + _box(b"moov") + _box(b"moof", b"\x00" * 16) + _box(b"mdat", b"\x00" * 512))
    restored = root / "wiederhergestellt" / "alt.mp4"
    restored.write_bytes(frag.read_bytes())
    temp = root / "Familie" / ".video.tvfix-1234.mp4"
    temp.write_bytes(frag.read_bytes())

    old = time.time() - 60
    os.utime(frag, (old, old))
    # ctime lässt sich nicht zurücksetzen -> RUHEZEIT für diesen Test auf 0 setzen
    monkeypatch.setattr(medien, "RUHEZEIT", 0.0)
    monkeypatch.setattr(
        medien, "_run_ffmpeg",
        lambda f, s, d: (d.write_bytes(_box(b"ftyp", b"isom") + _box(b"moov") + _box(b"mdat", b"\x00" * 512)), (True, ""))[1],
    )
    assert medien.scan() == 1
    assert medien.analyse_mp4(frag) == "ok"
    assert medien.analyse_mp4(restored) == "fragmented"  # wiederhergestellt bleibt unberührt


def test_dateien_upload_meldet_mp4_an_und_versteckt_tempdateien(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    root = pool.paths()["pool"]
    (root / "Familie").mkdir(parents=True)
    (root / "Familie" / ".video.tvfix-abcd.mp4").write_bytes(b"tmp")
    (root / "Familie" / ".other.hochladen-abcd").write_bytes(b"tmp")
    monkeypatch.setattr(pool, "mounts", lambda: {str(root): "fuse.mergerfs"})
    monkeypatch.setattr(fg, "geraete", lambda max_age=30: ([], ""))
    fg.Einstellungen(freigaben=[fg.Freigabe("Familie", "Familie", standard="offen")]).save()

    gemeldet = []
    monkeypatch.setattr(medien, "anmelden", lambda p: gemeldet.append(p))

    with TestClient(dateien.app) as client:
        client.get("/")
        csrf = dateien.csrf_token(client.cookies.get(dateien.COOKIE))
        listing = client.get("/d/Familie").text
        assert "tvfix" not in listing and "hochladen-" not in listing

        r = client.put("/api/hochladen/Familie/clip.mp4", content=b"mp4data", headers={"X-CSRF": csrf})
        assert r.status_code == 200
        assert gemeldet == [root / "Familie" / "clip.mp4"]


def test_web_einstellung_videos_optimieren(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from speicher import main

    monkeypatch.setattr(fg.dienste, "abgleichen", lambda: None)
    monkeypatch.setattr(main, "_status", lambda: {
        "cfg": main.Config(), "drive": False, "mount_root": "/mnt/cloudy", "naechstes": 0, "warnungen": [],
        "st": {"hdds": [], "puffer": None, "pool": pool.Branch("pool", "", "/mnt/cloudy/pool")},
    })
    with TestClient(main.app, base_url="http://speicher.lan") as web:
        web.post("/einrichten", data={"passwort": "geheim123", "passwort2": "geheim123"})
        csrf = auth.csrf_token(web.cookies.get(auth.COOKIE))
        r = web.post("/freigaben/einstellungen", data={
            "csrf": csrf, "an": "1", "dlna_an": "1", "netzwerkname": "SPEICHER", "dlna_name": "TV",
        }, follow_redirects=False)
        assert "ok=" in r.headers["location"]
        assert fg.Einstellungen.load().videos_optimieren is False
        web.post("/freigaben/einstellungen", data={
            "csrf": csrf, "an": "1", "dlna_an": "1", "videos_optimieren": "1",
            "netzwerkname": "SPEICHER", "dlna_name": "TV",
        })
        assert fg.Einstellungen.load().videos_optimieren is True
