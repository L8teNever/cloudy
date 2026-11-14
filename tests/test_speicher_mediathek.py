"""Tests für Streaming-Mediathek, Anbieter-Metadaten, Konvertierung und Schnell-TV („Jetzt schauen“)."""
import json
import os
import struct
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "speicher"))

from speicher import (  # noqa: E402
    auth,
    backup,
    dateien,
    mediathek,
    mediathek_db,
    mediathek_providers,
    medien,
    pool,
)
from speicher import freigaben as fg  # noqa: E402


def _box(typ: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def _valid_mp4(size: int = 1024) -> bytes:
    return _box(b"ftyp", b"isom") + _box(b"moov", _box(b"mvhd", b"\x00" * 20)) + _box(b"mdat", b"\x00" * size)


def _frag_mp4(size: int = 1024) -> bytes:
    return (
        _box(b"ftyp", b"isom")
        + _box(b"moov", _box(b"mvhd", b"\x00" * 20) + _box(b"mvex", b"\x00" * 8))
        + _box(b"moof", b"\x00" * 16)
        + _box(b"mdat", b"\x00" * size)
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MOUNT_ROOT", str(tmp_path / "mnt"))
    monkeypatch.setenv("SAMBA_CONF_DIR", str(tmp_path / "samba"))
    auth._fails.clear()
    root = pool.paths()["pool"]
    (root / "Familie" / "Urlaub").mkdir(parents=True)
    (root / "Familie" / "Urlaub" / "strand.jpg").write_bytes(b"\xff\xd8\xff\xe0foto1")
    (root / "Familie" / "Urlaub" / "berg.png").write_bytes(b"\x89PNG\r\n\x1a\nfoto2")
    (root / "Familie" / "clip.mp4").write_bytes(_valid_mp4(512))
    (root / "Privat").mkdir()
    (root / "Privat" / "geheim.jpg").write_bytes(b"\xff\xd8secret")
    (root / "Privat" / "geheim.mp4").write_bytes(_valid_mp4(512))
    monkeypatch.setattr(pool, "mounts", lambda: {str(root): "fuse.mergerfs"})
    monkeypatch.setattr(fg, "geraete", lambda max_age=30: ([fg.Geraet("pc", "PC", "testclient")], ""))
    e = fg.Einstellungen(
        benutzer=["simon", "mama"],
        freigaben=[
            fg.Freigabe("Familie", "Familie", standard="offen"),
            fg.Freigabe("Privat", "Privat", standard="passwort", benutzer=["simon"]),
            fg.Freigabe("Versteckt", "Privat", standard="versteckt"),
        ],
    )
    e.save()
    fg.set_web_password("simon", "geheim123")
    fg.set_web_password("mama", "mama1234")
    return root


@pytest.fixture()
def web(env):
    from fastapi.testclient import TestClient

    with TestClient(dateien.app) as client:
        client.get("/")
        yield client


def _csrf(web) -> str:
    return dateien.csrf_token(web.cookies.get(dateien.COOKIE))


def test_episode_filename_parser():
    assert mediathek_providers.parse_episode_from_filename("Dark.S02E05.1080p.mkv") == (2, 5)
    assert mediathek_providers.parse_episode_from_filename("Breaking Bad 1x03.mp4") == (1, 3)
    assert mediathek_providers.parse_episode_from_filename("Anime Staffel 3 Folge 12.mkv") == (3, 12)
    assert mediathek_providers.parse_episode_from_filename("Frieren E07.mp4") == (None, 7)
    assert mediathek_providers.parse_episode_from_filename("Inception (2010).mp4") == (None, None)


def test_target_paths_und_sanitization(env):
    movie = {"type": "movie", "name": "Spider-Man: No Way Home?", "year": 2021}
    rel, folder = mediathek.streaming_target_rel(movie)
    assert rel == "Streaming/Filme/Spider-Man No Way Home (2021)/Spider-Man No Way Home (2021).mp4"
    assert folder == "Streaming/Filme/Spider-Man No Way Home (2021)"

    show = {
        "type": "show",
        "name": "Dark",
        "year": 2017,
        "episodes": [{"season": 1, "number": 2, "title": "Lügen"}],
    }
    srel, sfolder = mediathek.streaming_target_rel(show, season=1, episode=2)
    assert srel == "Streaming/Serien/Dark (2017)/Staffel 1/Dark - S01E02 - Lügen.mp4"
    assert sfolder == "Streaming/Serien/Dark (2017)/Staffel 1"


def test_providers_suche_und_details_de(monkeypatch):
    def fake_get_json(url, headers=None, body=None):
        if "/search/movie" in url:
            return {"results": [{"id": 27205, "title": "Inception", "release_date": "2010-07-16", "poster_path": "/p.jpg", "overview": "Träume", "vote_average": 8.4, "vote_count": 30000, "popularity": 90}]}
        if "/search/tv" in url:
            return {"results": []}
        if "/movie/27205" in url:
            return {
                "id": 27205, "title": "Inception", "release_date": "2010-07-16", "runtime": 148,
                "genres": [{"name": "Sci-Fi"}], "overview": "Ein Dieb stiehlt Geheimnisse.",
                "watch/providers": {"results": {"DE": {
                    "link": "https://www.themoviedb.org/movie/27205/watch?locale=DE",
                    "flatrate": [{"provider_name": "Netflix", "logo_path": "/nf.png"}],
                    "rent": [{"provider_name": "Amazon Video", "logo_path": "/amz.png"}],
                }}},
                "credits": {"cast": [{"name": "Leonardo DiCaprio", "character": "Cobb"}]},
                "videos": {"results": []},
            }
        if "tvmaze.com" in url:
            return []
        if "graphql.anilist.co" in url:
            return {"data": {"Page": {"media": []}}}
        return {}

    monkeypatch.setattr(mediathek_providers, "_get_json", fake_get_json)
    res = mediathek_providers.search("Inception", ["tmdb", "tvmaze", "anilist"], tmdb_key="testkey")
    assert len(res["results"]) == 1
    assert res["results"][0]["ref"] == "tmdbmovie:27205"

    det = mediathek_providers.full("tmdbmovie:27205", tmdb_key="testkey")
    assert det["watch"]["link"].endswith("locale=DE")
    assert any(p["name"] == "Netflix" and p["type"] == "Streamen" for p in det["watch"]["list"])
    assert any(p["name"] == "Amazon Video" and p["type"] == "Leihen" for p in det["watch"]["list"])
    assert mediathek_providers.is_allowed_image_url("https://image.tmdb.org/t/p/w500/p.jpg")
    assert not mediathek_providers.is_allowed_image_url("http://127.0.0.1/secret.png")


def test_streaming_upload_konvertierung_und_rohdatei_loeschung(web, env, monkeypatch):
    csrf = _csrf(web)
    monkeypatch.setattr(mediathek, "probe_video", lambda p: {"duration": 120, "video_codec": "h264", "audio_codec": "aac", "pix_fmt": "yuv420p"})

    # 1. Sauberes MP4 direkt bereit
    r = web.put(
        "/api/mediathek/hochladen?name=Mein+Film&type=movie&year=2024&dateiname=film.mp4",
        content=_valid_mp4(1024),
        headers={"X-CSRF": csrf},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "bereit"
    target = env / "Streaming" / "Filme" / "Mein Film (2024)" / "Mein Film (2024).mp4"
    assert target.is_file()
    assert not list(target.parent.glob("*.raw-*"))

    # Range-Streaming funktioniert
    sr = web.get(f"/api/mediathek/stream/{d['file_id']}", headers={"Range": "bytes=0-3"})
    assert sr.status_code == 206 and len(sr.content) == 4

    # 2. MKV-Upload -> zunächst .raw-*, danach Konvertierung löscht Rohdatei
    monkeypatch.setattr(mediathek.shutil, "which", lambda cmd: "/usr/bin/ffmpeg" if cmd == "ffmpeg" else None)

    def fake_ffmpeg(cmd, dst):
        dst.write_bytes(_valid_mp4(2048))
        return True, ""

    monkeypatch.setattr(mediathek, "_run_ffmpeg_cmd", fake_ffmpeg)
    monkeypatch.setattr(medien, "scan_anstossen", lambda: None)

    r2 = web.put(
        "/api/mediathek/hochladen?name=Meine+Serie&type=show&staffel=1&folge=2&folgentitel=Pilot&dateiname=S01E02.mkv",
        content=b"\x1a\x45\xdf\xa3mkv-rohdaten",
        headers={"X-CSRF": csrf},
    )
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["status"] == "umwandlung"
    ep_target = env / "Streaming" / "Serien" / "Meine Serie" / "Staffel 1" / "Meine Serie - S01E02 - Pilot.mp4"
    assert not ep_target.exists()
    assert len(list(ep_target.parent.glob("*.raw-*.mkv"))) == 1

    # Queue abarbeiten -> .mp4 liegt vor, .raw-*.mkv ist gelöscht!
    assert mediathek.process_queue() == 1
    assert ep_target.is_file()
    assert not list(ep_target.parent.glob("*.raw-*"))
    assert mediathek_db.get_file(d2["file_id"])["status"] == "bereit"


def test_tv_staging_kopiert_und_raeumt_ordner_auf(web, env, monkeypatch):
    csrf = _csrf(web)
    monkeypatch.setattr(mediathek, "probe_video", lambda p: {"duration": 90, "video_codec": "h264", "audio_codec": "aac", "pix_fmt": "yuv420p"})

    # Film hochladen
    up = web.put(
        "/api/mediathek/hochladen?name=Testfilm&type=movie&year=2025&dateiname=t.mp4",
        content=_valid_mp4(1024),
        headers={"X-CSRF": csrf},
    ).json()
    orig = env / up["rel_pfad"]
    assert orig.is_file()

    # In „Jetzt schauen“ stagen
    st = web.post(f"/api/tv/stage-streaming/{up['file_id']}", headers={"X-CSRF": csrf})
    assert st.status_code == 200
    tv_item = st.json()["item"]
    # Warten bis Background-Kopie fertig
    for _ in range(40):
        cur = mediathek_db.get_tv_item(tv_item["id"])
        if cur["status"] == "bereit":
            break
        time.sleep(0.05)
    assert cur["status"] == "bereit"

    tv_dir = env / "Jetzt schauen"
    tv_file = env / cur["ziel_pfad"]
    assert tv_dir.is_dir() and tv_file.is_file()
    assert orig.is_file()  # Original bleibt unberührt

    # minidlna nimmt „Jetzt schauen“ mit auf, solange der Ordner existiert
    conf = fg.minidlna_conf(fg.Einstellungen.load())
    assert "Jetzt schauen" in conf

    # Entfernen -> Kopie und leerer „Jetzt schauen“-Ordner verschwinden, Original bleibt!
    rm = web.delete(f"/api/tv/item/{tv_item['id']}", headers={"X-CSRF": csrf})
    assert rm.status_code == 200
    assert not tv_file.exists()
    assert not tv_dir.exists()
    assert orig.is_file()


def test_mehrere_bilder_laengere_haltedauer_und_ablauf(web, env):
    csrf = _csrf(web)
    r = web.post(
        "/api/tv/stage-images",
        json={"pool_rels": ["Familie/Urlaub/strand.jpg", "Familie/Urlaub/berg.png"], "ablauf_tage": 7},
        headers={"X-CSRF": csrf},
    )
    assert r.status_code == 200 and r.json()["count"] == 2
    assert (env / "Jetzt schauen" / "strand.jpg").is_file()
    assert (env / "Jetzt schauen" / "berg.png").is_file()

    # Video mit kurzem Ablauf (1 Std) dazu
    sv = web.post("/api/tv/stage-video", json={"pool_rel": "Familie/clip.mp4", "ablauf_stunden": 1}, headers={"X-CSRF": csrf})
    assert sv.status_code == 200
    vid_id = sv.json()["item"]["id"]
    for _ in range(40):
        if mediathek_db.get_tv_item(vid_id)["status"] == "bereit":
            break
        time.sleep(0.05)

    # Nach 2 Stunden: Video läuft ab, die 2 Bilder bleiben noch 7 Tage erhalten!
    removed = mediathek.cleanup_expired(now=time.time() + 2 * 3600)
    assert removed == 1
    assert not (env / "Jetzt schauen" / "clip.mp4").exists()
    assert (env / "Jetzt schauen" / "strand.jpg").is_file()
    assert (env / "Jetzt schauen" / "berg.png").is_file()
    assert (env / "Familie" / "clip.mp4").is_file()  # Original bleibt

    # Nach 8 Tagen: Bilder laufen ab -> Ordner wird automatisch gelöscht
    removed2 = mediathek.cleanup_expired(now=time.time() + 8 * 86400)
    assert removed2 == 2
    assert not (env / "Jetzt schauen").exists()


def test_medien_berechtigungen_und_backup_ausschluss(web, env):
    csrf = _csrf(web)
    # Nicht angemeldeter Benutzer sieht Privat-Dateien nicht in /api/medien/liste
    lst = web.get("/api/medien/liste").json()
    rels = {x["pool_rel"] for x in lst["items"]}
    assert "Familie/Urlaub/strand.jpg" in rels
    assert "Privat/geheim.jpg" not in rels

    # Direkter Zugriff auf Privat-Dateien über /api/medien/datei oder /api/tv/* wird blockiert
    assert web.get("/api/medien/datei?rel=Privat/geheim.jpg").status_code == 403
    assert web.post("/api/tv/stage-video", json={"pool_rel": "Privat/geheim.mp4"}, headers={"X-CSRF": csrf}).status_code == 403
    assert web.post("/api/tv/stage-images", json={"pool_rels": ["Privat/geheim.jpg"]}, headers={"X-CSRF": csrf}).status_code == 403

    # Backup schließt „Jetzt schauen“ und .raw-* Dateien aus
    web.post("/api/tv/stage-images", json={"pool_rels": ["Familie/Urlaub/strand.jpg"]}, headers={"X-CSRF": csrf})
    (env / "Streaming" / ".film.raw-1234.mkv").parent.mkdir(parents=True, exist_ok=True)
    (env / "Streaming" / ".film.raw-1234.mkv").write_bytes(b"roh")
    changed = backup.changed_files(env, 0.0)
    assert not any(p.startswith("Jetzt schauen") for p in changed)
    assert not any(".raw-" in p for p in changed)
    assert "Familie/Urlaub/strand.jpg" in changed


def test_seiten_render_und_csp(web, env):
    for path in ("/streaming", "/medien"):
        r = web.get(path)
        assert r.status_code == 200
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert "Jetzt schauen" in r.text or "Streaming" in r.text


def test_medien_upload_einstellungen_und_loeschen(web, env, monkeypatch):
    csrf = _csrf(web)
    monkeypatch.setattr(mediathek, "probe_video", lambda p: {"duration": 30, "video_codec": "h264", "audio_codec": "aac", "pix_fmt": "yuv420p"})

    # Bild über /api/medien/hochladen hochladen + direkt für TV stagen
    ri = web.put("/api/medien/hochladen?dateiname=urlaub.jpg&ziel=tv&ablauf_tage=14", content=b"\xff\xd8\xff\xe0bild", headers={"X-CSRF": csrf})
    assert ri.status_code == 200
    di = ri.json()
    assert di["kind"] == "image" and (env / di["pool_rel"]).is_file()
    assert (env / di["tv_item"]["ziel_pfad"]).is_file()

    # Video über /api/medien/hochladen hochladen + direkt für TV stagen
    rv = web.put("/api/medien/hochladen?dateiname=party.mp4&ziel=tv", content=_valid_mp4(512), headers={"X-CSRF": csrf})
    assert rv.status_code == 200 and rv.json()["status"] == "bereit"
    assert (env / rv.json()["pool_rel"]).is_file()

    # Einstellungen lesen & speichern (inkl. TMDB-Key-Maskierung)
    rs = web.post("/api/mediathek/einstellungen", json={
        "tmdb_key": "abc123456",
        "video_ablauf_stunden": 4,
        "bild_ablauf_tage": 10,
        "quellen": ["tmdb", "anilist"],
    }, headers={"X-CSRF": csrf})
    assert rs.status_code == 200
    gs = web.get("/api/mediathek/einstellungen").json()
    assert gs["has_tmdb_key"] is True and gs["tmdb_key_hinweis"] == "••••3456"
    assert gs["video_ablauf_stunden"] == 4 and gs["bild_ablauf_tage"] == 10

    # Mediathek-Datei hochladen & über DELETE /api/mediathek/datei/{id} löschen
    up = web.put(
        "/api/mediathek/hochladen?name=Loeschfilm&type=movie&year=2020&dateiname=l.mp4",
        content=_valid_mp4(512),
        headers={"X-CSRF": csrf},
    ).json()
    assert (env / up["rel_pfad"]).is_file()
    dl = web.delete(f"/api/mediathek/datei/{up['file_id']}", headers={"X-CSRF": csrf})
    assert dl.status_code == 200
    assert not (env / up["rel_pfad"]).exists()
    assert mediathek_db.get_file(up["file_id"]) is None



def test_filme_serien_trennen_zieht_alte_titel_um(env):
    from speicher import mediathek_db

    alt = env / "Streaming" / "Alter Film (2020)"
    alt.mkdir(parents=True)
    (alt / "Alter Film (2020).mp4").write_bytes(b"x")
    tid = mediathek_db.upsert_title({"ref": "tmdb:movie:1", "provider": "tmdb", "type": "movie", "name": "Alter Film", "year": 2020})
    fid = mediathek_db.register_file(tid, "Streaming/Alter Film (2020)/Alter Film (2020).mp4")
    assert mediathek.filme_serien_trennen() == 1
    assert (env / "Streaming" / "Filme" / "Alter Film (2020)" / "Alter Film (2020).mp4").is_file()
    assert mediathek_db.get_file(fid)["rel_pfad"] == "Streaming/Filme/Alter Film (2020)/Alter Film (2020).mp4"
    assert mediathek.filme_serien_trennen() == 0  # schon erledigt
