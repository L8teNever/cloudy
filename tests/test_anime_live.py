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
