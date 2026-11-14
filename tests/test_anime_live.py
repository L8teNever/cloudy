"""Live-Anime: neue Folgen automatisch (anime_live._abos / _neue_fuer)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "speicher"))

from speicher import anime_live, aniworld  # noqa: E402


def _strm(root: Path, ordner: str, s: int, f: int, sprache: str = "1"):
    p = root / ordner / f"Staffel {s}" / f"X - S{s:02d}E{f:02d}.strm"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"http://192.168.178.3:8093/live/x-anime/{s}/{f}/{sprache}/{'a' * 32}.m3u8\n")


def test_neue_folgen_nur_nach_der_hoechsten(tmp_path, monkeypatch):
    root = tmp_path / "Serien"
    for f in (1, 2, 3):
        _strm(root, "X (2020)", 2, f)
    _strm(root, "X (2020)", 2, 2, "3")  # gleiche Folge doppelt in anderer Sprache zählt nicht als Mehrheit
    monkeypatch.setattr(anime_live, "_serien_ordner", lambda: root)
    monkeypatch.setattr(aniworld, "ziel_rel", lambda j: f"S/Serien/X ({j['jahr']})/Staffel {j['staffel']}/a.mp4")
    monkeypatch.setattr(aniworld, "_serie", lambda slug: {
        "slug": slug, "titel": "X", "jahr": 2020,
        "staffeln": [{"nr": 1, "film": False}, {"nr": 2, "film": False}, {"nr": 3, "film": False}, {"nr": 0, "film": True}]})
    folgen = {
        1: [{"nr": n, "titel": "", "sprachen": ["1"]} for n in (1, 2, 3, 4)],
        2: [{"nr": n, "titel": "", "sprachen": ["1"]} for n in (1, 2, 3, 4, 5)] + [{"nr": 6, "titel": "", "sprachen": ["3"]},
                                                                               {"nr": 7, "titel": "", "sprachen": ["1"]}],
        3: [{"nr": 1, "titel": "", "sprachen": []}],
    }
    monkeypatch.setattr(aniworld, "_staffel", lambda slug, nr: folgen[nr])

    abos = anime_live._abos()
    a = abos[root / "X (2020)"]
    assert a["slug"] == "x-anime" and a["folgen"] == {2: 3}
    neu = anime_live._neue_fuer(root / "X (2020)", a)
    # Staffel 1 nicht gewählt → bleibt weg; Staffel 2 ab E4, stoppt vor E6 (noch nicht auf Deutsch); Staffel 3 neu
    assert [(f["staffel"], f["nr"]) for f in neu] == [(2, 4), (2, 5), (3, 1)]
    assert all(f["sprache"] == "1" for f in neu)

    # Titel bei AniWorld geändert → kein zweiter Ordner
    monkeypatch.setattr(aniworld, "ziel_rel", lambda j: "S/Serien/Y (2020)/Staffel 1/a.mp4")
    assert anime_live._neue_fuer(root / "X (2020)", a) == []


def test_film_erkennung(monkeypatch):
    staffeln = {
        "your-name": ([{"nr": 1, "film": False}], {1: [{"nr": 1, "movie": True}]}),
        "ova": ([{"nr": 1, "film": False}], {1: [{"nr": 1, "movie": False}]}),
        "reihe": ([{"nr": 0, "film": True}], {0: [{"nr": 1, "movie": False}, {"nr": 2, "movie": False}]}),
        "ein-film": ([{"nr": 0, "film": True}], {0: [{"nr": 1, "movie": False}]}),
        "serie-mit-film": ([{"nr": 1, "film": False}, {"nr": 0, "film": True}], {1: [{"nr": 1, "movie": True}]}),
    }
    for slug, (st, folgen) in staffeln.items():
        monkeypatch.setattr(aniworld, "staffel", lambda s, nr, f=folgen: f[nr])
        erwartet = {"your-name": "einzeln", "ova": "", "reihe": "mehrere", "ein-film": "einzeln", "serie-mit-film": ""}[slug]
        assert aniworld._film_art(slug, st) == erwartet, slug


def test_film_ziel_und_umzug(tmp_path, monkeypatch):
    from speicher import anime_meta, mediathek, mediathek_db, mediathek_providers

    monkeypatch.setattr(mediathek_db.Einstellungen, "load", classmethod(lambda cls: type("E", (), {"streaming_ordner": "Streaming"})()))
    monkeypatch.setattr(mediathek_providers, "sanitize_name", lambda s, d="x": s)
    j = {"titel": "Your Name.", "jahr": 2016, "staffel": 1, "folge": 1, "folgentitel": "Gestern", "film": "einzeln"}
    assert aniworld.ziel_rel(j) == "Streaming/Filme/Your Name. (2016)/Your Name. (2016).mp4"
    j.update(film="mehrere", folgentitel="Film Zwei", folge=2)
    assert aniworld.ziel_rel(j) == "Streaming/Filme/Film Zwei/Film Zwei.mp4"

    # Umzug einer alten Live-„Serie“, die eigentlich ein Film ist
    pool = tmp_path / "pool"
    alt_ordner = pool / "Streaming/Serien/Your Name. (2016)"
    (alt_ordner / "Staffel 1").mkdir(parents=True)
    (alt_ordner / "tvshow.nfo").write_text('<tvshow><uniqueid type="aniworld">your-name</uniqueid></tvshow>')
    (alt_ordner / "poster.jpg").write_bytes(b"x")
    strm = alt_ordner / "Staffel 1/Your Name. - S01E01 - Gestern.strm"
    strm.write_text("http://h/live/your-name/1/1/1/" + "a" * 32 + ".m3u8\n")
    (alt_ordner / "Staffel 1/season.nfo").write_text("<season/>")
    strm.with_suffix(".nfo").write_text("<episodedetails/>")
    monkeypatch.setattr(mediathek, "pool_mounted", lambda: True)
    monkeypatch.setattr(mediathek, "pool_root", lambda: pool)
    monkeypatch.setattr(mediathek, "safe_pool_path", lambda rel: pool / rel)
    monkeypatch.setattr(mediathek, "_chown_tree", lambda *a: None)
    monkeypatch.setattr(mediathek, "_besitzer", lambda *a: None)
    monkeypatch.setattr(anime_meta, "_cover", lambda *a, **k: b"")
    monkeypatch.setattr(aniworld, "serie", lambda slug: {"slug": slug, "titel": "Your Name.", "jahr": 2016, "film": "einzeln"})
    monkeypatch.setattr(mediathek_db, "db", lambda: __import__("contextlib").nullcontext(type("C", (), {"execute": lambda *a: None})()))
    monkeypatch.setattr(anime_live.jobs, "log", lambda *a: None)

    assert anime_live.filme_umziehen() == 1
    neu = pool / "Streaming/Filme/Your Name. (2016)/Your Name. (2016).strm"
    assert neu.read_text().startswith("http://h/live/your-name/1/1/1/")
    assert "<movie>" in neu.with_suffix(".nfo").read_text() and "<year>2016</year>" in neu.with_suffix(".nfo").read_text()
    assert not alt_ordner.exists()
    assert [g["film"] for g in anime_live.liste()] == [True]


def test_vpn_proxy_nur_extern(monkeypatch):
    """Mit ANIME_PROXY geht alles im Internet durch den Proxy, der Browser (lokal, CDP) nie."""
    monkeypatch.setenv("ANIME_PROXY", "http://127.0.0.1:8094")
    extern = {type(h).__name__: h for h in aniworld._opener(True).handlers}["ProxyHandler"]
    assert extern.proxies == {"http": "http://127.0.0.1:8094", "https": "http://127.0.0.1:8094"}
    # lokal / ohne Proxy: gar kein Proxy-Handler (auch keiner aus der Umgebung)
    assert "ProxyHandler" not in {type(h).__name__ for h in aniworld._opener().handlers}
    monkeypatch.delenv("ANIME_PROXY")
    assert "ProxyHandler" not in {type(h).__name__ for h in aniworld._opener(True).handlers}


def test_katalog_aufbauen_und_hinzufuegen(tmp_path, monkeypatch):
    from speicher import anime_katalog, anime_meta, mediathek

    wurzel = tmp_path / "Katalog"
    monkeypatch.setattr(anime_katalog, "ordner", lambda: wurzel)
    monkeypatch.setattr(anime_katalog, "_zustand_datei", lambda: tmp_path / "z.json")
    monkeypatch.setattr(anime_katalog, "_vorhanden", lambda: {"schon-da"})
    monkeypatch.setattr(anime_katalog, "_jellyfin_einlesen", lambda: None)
    monkeypatch.setattr(anime_katalog, "alle_anime", lambda: [(f"a-{i}", f"Anime {i}") for i in range(150)] + [("schon-da", "Da")])
    monkeypatch.setattr(mediathek, "pool_mounted", lambda: True)
    monkeypatch.setattr(mediathek, "pool_root", lambda: tmp_path)
    monkeypatch.setattr(mediathek, "_chown_tree", lambda *a: None)
    monkeypatch.setattr(mediathek, "_besitzer", lambda *a: None)
    monkeypatch.setattr(anime_live, "basis", lambda: "http://h:8093")

    assert not anime_katalog.bereit()
    assert anime_katalog.aufbauen() == {"neu": 150, "weg": 0, "gesamt": 150}
    assert anime_katalog.bereit()
    assert not (wurzel / "Da").exists()
    strm = (wurzel / "Anime 7" / "Staffel 1" / "Anime 7 - S01E01.strm").read_text()
    assert strm.startswith("http://h:8093/katalog/a-7/") and strm.strip().endswith(".mp4")
    sig = strm.strip().rsplit("/", 1)[1][:-4]
    assert anime_katalog.pruefen("a-7", sig) == "a-7"
    assert "Katalog" in (wurzel / "Anime 7" / "tvshow.nfo").read_text()

    # Anime wurde hinzugefügt (steht jetzt unter Serien) → Platzhalter verschwindet beim nächsten Abgleich
    monkeypatch.setattr(anime_katalog, "_vorhanden", lambda: {"a-7", "schon-da"})
    assert anime_katalog.aufbauen() == {"neu": 0, "weg": 1, "gesamt": 149}
    assert not (wurzel / "Anime 7").exists()

    # Infos nachtragen
    monkeypatch.setattr(anime_katalog, "PAUSE", 0)
    monkeypatch.setattr(aniworld, "_serie", lambda slug: {"slug": slug, "titel": "x", "beschreibung": "Beschreibung!", "jahr": 2001, "genres": ["Action"], "cover": ""})
    assert anime_katalog.infos_nachtragen() == 149
    nfo = (wurzel / "Anime 8" / "tvshow.nfo").read_text()
    assert "Beschreibung!" in nfo and "<year>2001</year>" in nfo and anime_katalog.status()["mit_infos"] == 149
