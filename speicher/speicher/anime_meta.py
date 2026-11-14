"""Infos und Cover für Jellyfin neben die Anime-Folgen legen (live wie heruntergeladen).

Jellyfin liest von sich aus:
  <Serie>/tvshow.nfo + poster.jpg      → Titel, Jahr, Beschreibung, Genres, Cover der Serie
  <Serie>/backdrop.jpg                 → breites Hintergrundbild (AniWorld-Banner)
  <Serie>/Staffel N/season.nfo         → Staffelnummer/-name
  <Serie>/Staffel N/<Folge>.nfo        → Folgentitel, Staffel/Folge (gleicher Name wie .strm/.mp4)
Filme (siehe aniworld._film_art) liegen unter Filme/<Titel (Jahr)>/:
  <Titel (Jahr)>.nfo (<movie>) + poster.jpg + backdrop.jpg
Ohne diese Dateien kennt Jellyfin bei .strm nichts außer dem Ordnernamen (und zeigt nur ein Ordner-Symbol).
Bilder/Beschreibungen, die hier fehlen, ergänzt Jellyfin weiter aus dem Internet (TMDB), weil nichts gesperrt ist.
"""
from __future__ import annotations

import re
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

from . import aniworld, mediathek

META = ("tvshow.nfo", "season.nfo", "poster.jpg", "folder.jpg", "backdrop.jpg")
VIDEO = (".strm", ".mp4", ".mkv")
FOLGE_RE = re.compile(r" - S(\d+)E(\d+)(?: - (.*))?$")


def _x(s) -> str:
    return escape(str(s or ""))


def _schreiben(p: Path, inhalt: bytes, ersetzen: bool = False) -> None:
    if p.exists() and not ersetzen:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(inhalt)
    tmp.replace(p)
    mediathek._besitzer(p)


def _cover(url: str, groesser: bool = True) -> bytes:
    """Bild von AniWorld (beim Cover erst ohne Größenangabe versuchen, sonst die Vorschaugröße)."""
    for u in dict.fromkeys(([re.sub(r"_\d+x\d+(?=\.\w+$)", "", url)] if groesser else []) + [url]):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": aniworld.UA, "Referer": aniworld.BASE})
            with aniworld._opener().open(req, timeout=aniworld.TIMEOUT) as r:
                data = r.read(5 * 1024 * 1024)
            if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[8:12] == b"WEBP":
                return data
        except Exception:  # noqa: BLE001 – ohne Cover holt Jellyfin eins aus dem Internet
            continue
    return b""


def serie(ordner: Path, info: dict) -> None:
    """tvshow.nfo + poster.jpg (einmal; vorhandene, z. B. selbst angepasste, bleiben)."""
    if not (ordner / "tvshow.nfo").exists():
        genres = "".join(f"  <genre>{_x(g)}</genre>\n" for g in info.get("genres") or [])
        jahr = f"  <year>{int(info['jahr'])}</year>\n  <premiered>{int(info['jahr'])}-01-01</premiered>\n" if info.get("jahr") else ""
        nfo = (
            '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<tvshow>\n'
            f"  <title>{_x(info.get('titel'))}</title>\n"
            f"  <plot>{_x(info.get('beschreibung'))}</plot>\n{jahr}{genres}"
            "  <tag>Anime</tag>\n"
            f"  <uniqueid type=\"aniworld\">{_x(info.get('slug'))}</uniqueid>\n"
            "</tvshow>\n"
        )
        _schreiben(ordner / "tvshow.nfo", nfo.encode())
    if not (ordner / "poster.jpg").exists() and info.get("cover"):
        bild = _cover(info["cover"])
        if bild:
            _schreiben(ordner / "poster.jpg", bild)
    if not (ordner / "backdrop.jpg").exists() and info.get("hintergrund"):
        bild = _cover(info["hintergrund"], groesser=False)
        if bild:
            _schreiben(ordner / "backdrop.jpg", bild)


def film_infos(video: Path, info: dict, titel: str) -> None:
    """<Film>.nfo + poster.jpg/backdrop.jpg im Filmordner (einmal; vorhandene bleiben)."""
    ordner = video.parent
    genres = "".join(f"  <genre>{_x(g)}</genre>\n" for g in info.get("genres") or [])
    jahr = f"  <year>{int(info['jahr'])}</year>\n" if info.get("jahr") and not titel else ""
    _schreiben(video.with_suffix(".nfo"), (
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<movie>\n'
        f"  <title>{_x(titel or info.get('titel'))}</title>\n"
        f"  <plot>{_x(info.get('beschreibung'))}</plot>\n{jahr}{genres}"
        "  <tag>Anime</tag>\n"
        f"  <uniqueid type=\"aniworld\">{_x(info.get('slug'))}</uniqueid>\n"
        "</movie>\n").encode())
    if not (ordner / "poster.jpg").exists() and info.get("cover"):
        bild = _cover(info["cover"])
        if bild:
            _schreiben(ordner / "poster.jpg", bild)
    if not (ordner / "backdrop.jpg").exists() and info.get("hintergrund"):
        bild = _cover(info["hintergrund"], groesser=False)
        if bild:
            _schreiben(ordner / "backdrop.jpg", bild)


def staffel(ordner: Path, nr: int) -> None:
    name = "Filme" if nr == 0 else f"Staffel {nr}"
    _schreiben(ordner / "season.nfo", (
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<season>\n'
        f"  <title>{name}</title>\n  <seasonnumber>{nr}</seasonnumber>\n</season>\n").encode())


def folge(video: Path, titel: str, nr_staffel: int, nr_folge: int) -> None:
    _schreiben(video.with_suffix(".nfo"), (
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<episodedetails>\n'
        f"  <title>{_x(titel or f'Folge {nr_folge}')}</title>\n"
        f"  <season>{nr_staffel}</season>\n  <episode>{nr_folge}</episode>\n"
        "</episodedetails>\n").encode())


def fuer_folge(video: Path, info: dict, nr_staffel: int, nr_folge: int, titel: str, film: str = "") -> None:
    """Alles für eine Folge (video = Pfad der .strm/.mp4 unter <Serie>/Staffel N/) oder einen Film
    (film = "einzeln"/"mehrere", video unter Filme/<Name>/). Fehler sind egal."""
    try:
        if film:
            film_infos(video, info, titel if film == "mehrere" else "")
            return
        serie(video.parent.parent, info)
        staffel(video.parent, nr_staffel)
        folge(video, titel, nr_staffel, nr_folge)
    except OSError:
        pass


def nachtragen(serien_ordner: Path, info: dict) -> int:
    """Für schon vorhandene Folgen (ohne .nfo) Infos aus dem Dateinamen nachtragen. Gibt die Zahl neuer zurück."""
    n = 0
    for p in serien_ordner.rglob("*"):
        if p.suffix.lower() not in VIDEO or not p.is_file() or p.with_suffix(".nfo").exists():
            continue
        m = FOLGE_RE.search(p.stem)
        if m:
            fuer_folge(p, info, int(m.group(1)), int(m.group(2)), (m.group(3) or "").strip())
            n += 1
    if n == 0:
        try:
            serie(serien_ordner, info)
        except OSError:
            pass
    return n


def nach_loeschen(video: Path) -> None:
    """Nach dem Löschen einer Folge: ihre .nfo weg; Staffel- und Serienordner ohne Folgen samt Infodateien löschen.
    Nur bei Anime-Ordnern (dort liegt eine tvshow.nfo von uns) – andere Serien fasst das nicht an.
    Bei Anime-Filmen: .nfo, Cover und den leeren Filmordner löschen."""
    try:
        nfo = video.with_suffix(".nfo").read_text(errors="replace")
    except OSError:
        nfo = ""
    if "<movie>" in nfo and 'type="aniworld"' in nfo:
        video.with_suffix(".nfo").unlink(missing_ok=True)
        try:
            if any(p.name not in META for p in video.parent.iterdir()):
                return
            for name in META:
                (video.parent / name).unlink(missing_ok=True)
            video.parent.rmdir()
        except OSError:
            pass
        return
    try:
        if 'type="aniworld"' not in (video.parent.parent / "tvshow.nfo").read_text(errors="replace"):
            return
    except OSError:
        return
    video.with_suffix(".nfo").unlink(missing_ok=True)
    for ordner in (video.parent, video.parent.parent):
        try:
            rest = [p for p in ordner.iterdir() if p.name not in META]
        except OSError:
            return
        if rest:
            return
        for name in META:
            (ordner / name).unlink(missing_ok=True)
        try:
            ordner.rmdir()
        except OSError:
            return
