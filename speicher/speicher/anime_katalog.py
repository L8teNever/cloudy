"""Anime-Katalog für Jellyfin: jeder AniWorld-Anime als Platzhalter, damit die Jellyfin-Suche alle findet.

Streaming/Katalog/<Titel>/tvshow.nfo (+ poster.jpg)          → Titel, Beschreibung, Cover (kommen nach und nach)
Streaming/Katalog/<Titel>/Staffel 1/<Titel> - S01E01.strm     → Folge „▶ Zu Jellyfin hinzufügen“

Drückt man in Jellyfin auf diese Folge, ruft Jellyfin /katalog/<slug>/<sig>.mp4 auf. Dort legen wir den Anime
live unter „Serien“ bzw. „Filme“ an (anime_live.anlegen) und spielen einen kurzen Hinweis-Clip ab. Danach
verschwindet der Platzhalter. Alles von AniWorld geht wie sonst durch den VPN-Proxy (aniworld._opener).
Die Mediathek „Anime-Katalog“ holt keine Infos aus dem Internet (jellyfin.gewuenschte_mediatheken): nur unsere .nfo.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import anime_live, anime_meta, aniworld, config, jobs, mediathek, mediathek_db, mediathek_providers

KATALOG = "Katalog"
NAME = "Anime-Katalog"
ALLE = 3600  # neue Anime auf AniWorld stündlich aufnehmen (eine Seite, billig)
PAUSE = 2.0  # zwischen zwei Anime beim Info-Holen (AniWorld nicht überfahren)
LINK_RE = re.compile(r'<a\b[^>]*?href="/anime/stream/([a-z0-9-]+)"[^>]*>(.*?)</a>', re.S)
UNSICHTBAR = re.compile("[​-‏⁠﻿]")

_lock = threading.Lock()
_gestartet = False
_im_gange: set[str] = set()  # slugs, die gerade hinzugefügt werden


# ---------- Ablage ----------

def ordner() -> Path:
    base = (mediathek_db.Einstellungen.load().streaming_ordner or "Streaming").strip("/")
    return mediathek.safe_pool_path(f"{base}/{KATALOG}")


def _zustand_datei() -> Path:
    return config.data_dir() / "anime_katalog.json"


def _laden() -> dict:
    try:
        z = json.loads(_zustand_datei().read_text())
        if isinstance(z, dict) and isinstance(z.get("items"), dict):
            return z
    except (OSError, ValueError):
        pass
    return {"items": {}, "bereit": False}


def _speichern(z: dict) -> None:
    try:
        tmp = _zustand_datei().with_suffix(".tmp")
        tmp.write_text(json.dumps(z, ensure_ascii=False))
        os.replace(tmp, _zustand_datei())
    except OSError:
        pass


def bereit() -> bool:
    """Steht der Katalog (Platzhalter angelegt)? Erst dann legt Jellyfin die Mediathek an."""
    return bool(_laden().get("bereit"))


def status() -> dict:
    z = _laden()
    items = z["items"]
    return {"bereit": bool(z.get("bereit")), "anzahl": len(items), "mit_infos": sum(1 for v in items.values() if v.get("meta")),
            "stand": z.get("stand", 0)}


# ---------- AniWorld-Liste ----------

def alle_anime() -> list[tuple[str, str]]:
    """[(slug, Titel)] – AniWorld listet alle Anime auf einer Seite."""
    doc, _ = aniworld.seite(aniworld.BASE + "/animes", "document.querySelectorAll('a[href^=\"/anime/stream/\"]').length > 100")
    out: dict[str, str] = {}
    for slug, inner in LINK_RE.findall(doc):
        titel = UNSICHTBAR.sub("", aniworld._text(re.sub(r"<[^>]+>", "", inner))).strip()
        if titel and slug not in out and aniworld.SLUG_RE.match(slug):
            out[slug] = titel
    return sorted(out.items(), key=lambda x: x[1].lower())


def _vorhanden() -> set[str]:
    """Slugs der Anime, die schon unter Serien/Filme stehen (live oder geladen): steht in deren .nfo."""
    base = (mediathek_db.Einstellungen.load().streaming_ordner or "Streaming").strip("/")
    slugs: set[str] = set()
    for unter, tiefe in ((mediathek.SERIEN, "*/tvshow.nfo"), (mediathek.FILME, "*/*.nfo")):
        root = mediathek.safe_pool_path(f"{base}/{unter}")
        if not root.is_dir():
            continue
        for p in root.glob(tiefe):
            try:
                slugs.update(re.findall(r'type="aniworld">([a-z0-9-]+)<', p.read_text(errors="replace")))
            except OSError:
                continue
    return slugs


# ---------- Platzhalter schreiben / entfernen ----------

def _url(slug: str) -> str:
    return f"{anime_live.basis()}/katalog/{slug}/{anime_live._sig('katalog|' + slug)}.mp4"


def _nfo_serie(slug: str, titel: str, info: dict | None) -> str:
    info = info or {}
    plot = (info.get("beschreibung") or "").strip()
    hinweis = "▶ Folge 1 abspielen: der Anime wird zu Jellyfin hinzugefügt (steht gleich unter Serien/Filme)."
    plot = f"{plot}\n\n{hinweis}" if plot else hinweis
    genres = "".join(f"  <genre>{anime_meta._x(g)}</genre>\n" for g in info.get("genres") or [])
    jahr = f"  <year>{int(info['jahr'])}</year>\n" if info.get("jahr") else ""
    return ('<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<tvshow>\n'
            f"  <title>{anime_meta._x(titel)}</title>\n  <plot>{anime_meta._x(plot)}</plot>\n{jahr}{genres}"
            "  <tag>Katalog</tag>\n"
            f"  <uniqueid type=\"aniworld-katalog\">{anime_meta._x(slug)}</uniqueid>\n</tvshow>\n")


def _platzhalter(ziel: Path, name: str, slug: str, titel: str) -> None:
    """Ordner, tvshow.nfo, season.nfo, Folge (.strm + .nfo). Vorhandene Dateien bleiben (Cover/Infos kommen später)."""
    anime_meta._schreiben(ziel / "tvshow.nfo", _nfo_serie(slug, titel, None).encode())
    st = ziel / "Staffel 1"
    anime_meta.staffel(st, 1)
    strm = st / f"{name} - S01E01.strm"
    anime_meta._schreiben(strm, (_url(slug) + "\n").encode(), ersetzen=True)
    anime_meta.folge(strm, "▶ Zu Jellyfin hinzufügen", 1, 1)


def _weg(ziel: Path) -> None:
    shutil.rmtree(ziel, ignore_errors=True)


def _name(titel: str, slug: str, benutzt: set[str]) -> str:
    n = mediathek_providers.sanitize_name(titel, slug)
    if n.lower() in benutzt:
        n = f"{n} ({slug})"[:150]
    benutzt.add(n.lower())
    return n


def aufbauen() -> dict:
    """Katalog mit AniWorld abgleichen: neue Anime als Platzhalter, schon vorhandene/verschwundene entfernen."""
    if not mediathek.pool_mounted():
        raise aniworld.AniFehler("Der Speicher ist gerade nicht eingehängt")
    liste = alle_anime()
    if len(liste) < 100:
        raise aniworld.AniFehler(f"AniWorld-Liste zu kurz ({len(liste)}) – Seite nicht richtig geladen")
    wurzel = ordner()
    wurzel.mkdir(parents=True, exist_ok=True)
    mediathek._chown_tree(wurzel, mediathek.pool_root())
    da = _vorhanden()
    with _lock:
        z = _laden()
        items = z["items"]
        benutzt = {v["ordner"].lower() for v in items.values()}
        neu = weg = 0
        soll = {slug for slug, _ in liste} - da
        for slug in list(items):
            if slug not in soll:  # schon hinzugefügt oder von AniWorld verschwunden
                _weg(wurzel / items[slug]["ordner"])
                del items[slug]
                weg += 1
        for slug, titel in liste:
            if slug in items:  # Adresse des Servers kann sich geändert haben
                strm = wurzel / items[slug]["ordner"] / "Staffel 1" / f"{items[slug]['ordner']} - S01E01.strm"
                try:
                    if strm.is_file() and strm.read_text() != _url(slug) + "\n":
                        strm.write_text(_url(slug) + "\n")
                except OSError:
                    pass
                continue
            if slug in da:
                continue
            name = _name(titel, slug, benutzt)
            try:
                _platzhalter(wurzel / name, name, slug, titel)
            except OSError as exc:
                jobs.log(None, f"Anime-Katalog: {name}: {exc}"[:300])
                continue
            items[slug] = {"ordner": name, "titel": titel, "meta": False}
            neu += 1
        z.update(bereit=True, stand=int(time.time()))
        _speichern(z)
    if neu or weg:
        jobs.log(None, f"Anime-Katalog: {neu} neu, {weg} entfernt, {len(items)} insgesamt")
    return {"neu": neu, "weg": weg, "gesamt": len(items)}


# ---------- Infos und Cover nachtragen (langsam, im Hintergrund) ----------

def infos_nachtragen() -> int:
    """Für Anime ohne Infos: Beschreibung, Jahr, Genres, Cover von AniWorld holen. Gibt die Zahl erledigter zurück."""
    fertig = fehler = 0
    wurzel = ordner()
    while True:
        with _lock:
            z = _laden()
            offen = [(s, v) for s, v in z["items"].items() if not v.get("meta")]
        if not offen:
            break
        slug, v = offen[0]
        try:
            info = aniworld._serie(slug)  # frisch, nicht im Zwischenspeicher halten (2500 Einträge)
            ziel = wurzel / v["ordner"]
            if ziel.is_dir():
                anime_meta._schreiben(ziel / "tvshow.nfo", _nfo_serie(slug, v["titel"], info).encode(), ersetzen=True)
                if not (ziel / "poster.jpg").exists() and info.get("cover"):
                    bild = anime_meta._cover(info["cover"])
                    if bild:
                        anime_meta._schreiben(ziel / "poster.jpg", bild)
            with _lock:
                z = _laden()
                if slug in z["items"]:
                    z["items"][slug]["meta"] = True
                    z["items"][slug]["film"] = bool(info.get("film"))
                    _speichern(z)
            fertig += 1
            fehler = 0
            if fertig % 100 == 0:
                _jellyfin_einlesen()
        except aniworld.AniFehler as exc:
            fehler += 1
            jobs.log(None, f"Anime-Katalog: Infos zu {slug}: {exc}"[:300])
            with _lock:  # nicht für immer hängen bleiben: dieser Anime bekommt später noch einen Versuch
                z = _laden()
                if slug in z["items"]:
                    z["items"][slug]["meta"] = z["items"][slug].get("versuche", 0) >= 2
                    z["items"][slug]["versuche"] = z["items"][slug].get("versuche", 0) + 1
                    _speichern(z)
            if fehler >= 8:
                break  # AniWorld/VPN gerade nicht erreichbar: später weiter
            time.sleep(10)
        time.sleep(PAUSE)
    if fertig:
        _jellyfin_einlesen()
    return fertig


def _jellyfin_einlesen() -> None:
    try:
        from . import jellyfin
        if not jellyfin.mediathek_einlesen(str(ordner())):
            jellyfin.abgleichen()  # Mediathek gibt es noch nicht → anlegen
    except Exception:  # noqa: BLE001 – Jellyfin gerade nicht da: nächstes Mal
        pass


def aktualisieren() -> dict:
    """Katalog mit AniWorld abgleichen (neue Anime als Platzhalter, schon hinzugefügte weg); Jellyfin nur bei Änderung neu einlesen."""
    erg = aufbauen()
    if erg["neu"] or erg["weg"]:
        _jellyfin_einlesen()
    return erg


def start() -> None:
    """Hintergrund: Katalog stündlich mit AniWorld abgleichen (neue Anime sind so nach spätestens einer Stunde in der
    Jellyfin-Suche); getrennt davon Infos und Cover nachtragen, sobald es Anime ohne gibt."""
    global _gestartet
    if _gestartet:
        return
    _gestartet = True

    def abgleich() -> None:
        time.sleep(3 * 60)
        while True:
            try:
                aktualisieren()
            except Exception as exc:  # noqa: BLE001
                jobs.log(None, f"Anime-Katalog: {exc}"[:300])
            time.sleep(ALLE if bereit() else 15 * 60)

    def infos() -> None:
        time.sleep(5 * 60)
        while True:
            try:
                if bereit():
                    infos_nachtragen()
            except Exception as exc:  # noqa: BLE001
                jobs.log(None, f"Anime-Katalog: Infos: {exc}"[:300])
            time.sleep(10 * 60)

    threading.Thread(target=abgleich, name="anime-katalog", daemon=True).start()
    threading.Thread(target=infos, name="anime-katalog-infos", daemon=True).start()


# ---------- Hinzufügen per Klick in Jellyfin ----------

def hinweis_clip() -> Path:
    """Kurzer Hinweis-Clip („Wird hinzugefügt …“), einmal erzeugt und dann immer derselbe."""
    p = config.data_dir() / "anime_katalog_hinweis.mp4"
    if p.is_file() and p.stat().st_size > 1000:
        return p
    tmp = p.with_name("hinweis.tmp.mp4")
    schrift = next((f for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",) if Path(f).is_file()), "")
    text = "Wird zu Jellyfin hinzugefuegt|Gleich unter Serien oder Filme (Mediathek neu laden)"
    filter_ = "format=yuv420p"
    if schrift:
        z1, z2 = text.split("|")
        filter_ = (f"drawtext=fontfile={schrift}:text='{z1}':fontcolor=white:fontsize=56:x=(w-text_w)/2:y=h/2-70,"
                   f"drawtext=fontfile={schrift}:text='{z2}':fontcolor=0xbbbbbb:fontsize=30:x=(w-text_w)/2:y=h/2+20,format=yuv420p")
    for vf in dict.fromkeys((filter_, "format=yuv420p")):
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "color=c=0x14202e:s=1280x720:r=25:d=6",
               "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "6", "-vf", vf,
               "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(tmp)]
        try:
            if subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0 and tmp.is_file():
                os.replace(tmp, p)
                return p
        except (OSError, subprocess.SubprocessError):
            pass
    raise aniworld.AniFehler("Hinweis-Clip konnte nicht erzeugt werden (ffmpeg)")


def _sprache(slug: str) -> str:
    """Deutsch, sonst Ger-Sub, sonst Eng-Sub – nach der ersten Folge der ersten Staffel."""
    try:
        info = aniworld.serie(slug)
        st = info["staffeln"][0]["nr"]
        folgen = aniworld.staffel(slug, st)
        verfuegbar = set(folgen[0]["sprachen"]) if folgen else set()
    except (aniworld.AniFehler, KeyError, IndexError):
        verfuegbar = set()
    return next((s for s in ("1", "3", "2") if s in verfuegbar), "1")


def pruefen(slug: str, sig: str) -> str:
    if not anime_live._ok("katalog|" + slug, sig) or not aniworld.SLUG_RE.match(slug):
        raise anime_live.LiveFehler(403, "Ungültige Adresse")
    return slug


def hinzufuegen(slug: str) -> None:
    """Anime live unter Serien/Filme anlegen und den Platzhalter entfernen (läuft im Hintergrund, nur einmal je Anime)."""
    with _lock:
        if slug in _im_gange:
            return
        _im_gange.add(slug)

    def los() -> None:
        try:
            info = aniworld.serie(slug)
            folgen = []
            for s in info["staffeln"]:
                folgen += [{**f, "staffel": s["nr"]} for f in aniworld.staffel(slug, s["nr"])]
            if not folgen:
                raise aniworld.AniFehler("Keine Folgen gefunden")
            erg = anime_live.anlegen(info, folgen, _sprache(slug))
            jobs.log(None, f"Anime-Katalog: „{info['titel']}“ hinzugefügt ({erg['neu']} Folgen)")
            with _lock:
                z = _laden()
                v = z["items"].pop(slug, None)
                _speichern(z)
            if v:
                _weg(ordner() / v["ordner"])
                _jellyfin_einlesen()
        except Exception as exc:  # noqa: BLE001
            jobs.log(None, f"Anime-Katalog: Hinzufügen von {slug} fehlgeschlagen: {exc}"[:300])
        finally:
            with _lock:
                _im_gange.discard(slug)

    threading.Thread(target=los, name="anime-katalog-add", daemon=True).start()
