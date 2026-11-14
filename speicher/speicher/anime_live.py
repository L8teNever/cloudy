"""Anime live in Jellyfin – ohne Herunterladen.

Statt einer MP4 legt die Anime-Seite pro Folge eine kleine .strm-Datei an (gleicher Name und Ordner wie der Download,
also Streaming/Serien/<Titel (Jahr)>/Staffel N/<Titel> - SxxEyy - <Folgentitel>.strm). Darin steht nur eine Adresse
zur Anime-Seite:  http://<Pi>:<ANIME_PORT>/live/<slug>/<staffel>/<folge>/<sprache>/<signatur>.m3u8

Erst wenn jemand in Jellyfin auf Play drückt, sucht die Anime-Seite den Stream bei AniWorld/VOE (Adressen dort laufen
nach kurzer Zeit ab und brauchen Referer/User-Agent) und reicht die HLS-Playlist umgeschrieben durch: Jede Teil-Adresse
zeigt wieder auf /live/p/…, der Speicher holt die Stücke mit den richtigen Headern. Die Pfade enden auf .m3u8/.ts/…,
weil das ffmpeg in Jellyfin HLS-Teile ohne bekannte Endung ablehnt.

Alle Adressen sind mit einem geheimen Schlüssel (data/anime_live.key) signiert – kein offener Proxy. Ist die Folge
später heruntergeladen, löscht der Download die .strm (aniworld._laden_job).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from . import anime_meta, aniworld, config, freigaben, jobs, mediathek, mediathek_db

CACHE_TTL = 20 * 60  # so lange gilt eine gefundene Stream-Adresse
SEG_EXT = {"ts", "m4s", "mp4", "m4v", "aac", "m4a", "mp3", "vtt", "m3u8", "mkv", "mpegts"}
ZEILE_RE = re.compile(r"^http://[^/\s]+/live/([a-z0-9-]+)/(\d+)/(\d+)/(\d)/([0-9a-f]{32})\.m3u8\s*$")

_key: bytes | None = None
_cache: dict[tuple, tuple[float, dict]] = {}
_locks: dict[tuple, threading.Lock] = {}
_lock = threading.Lock()


class LiveFehler(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status, self.text = status, text


# ---------- Signatur ----------

def _schluessel() -> bytes:
    global _key
    if _key is None:
        f = config.data_dir() / "anime_live.key"
        try:
            _key = bytes.fromhex(f.read_text().strip())
        except (OSError, ValueError):
            _key = secrets.token_bytes(32)
            f.write_text(_key.hex())
            os.chmod(f, 0o600)
    return _key


def _sig(text: str) -> str:
    return hmac.new(_schluessel(), text.encode(), hashlib.sha256).hexdigest()[:32]


def _ok(text: str, sig: str) -> bool:
    return hmac.compare_digest(_sig(text), sig or "")


# ---------- Adressen ----------

def basis() -> str:
    """So erreichen Jellyfin (und Apps, die direkt abspielen) die Anime-Seite im Heimnetz."""
    port = os.environ.get("ANIME_PORT", "8093") or "8093"
    return f"http://{freigaben.server_ip()}:{port}"


def folge_url(slug: str, staffel: int, folge: int, sprache: str) -> str:
    teil = f"{slug}/{int(staffel)}/{int(folge)}/{sprache}"
    return f"{basis()}/live/{teil}/{_sig('folge|' + teil)}.m3u8"


def _proxy_pfad(url: str, referer: str, ext: str = "") -> str:
    """Root-relative Adresse für ein Stück der Playlist (Jellyfins ffmpeg löst sie gegen die .m3u8 auf)."""
    if not ext:
        endung = urllib.parse.urlparse(url).path.rsplit(".", 1)
        ext = endung[1].lower() if len(endung) == 2 and endung[1].lower() in SEG_EXT else "ts"
    token = base64.urlsafe_b64encode(json.dumps([url, referer]).encode()).decode().rstrip("=")
    return f"/live/p/{_sig('p|' + token)}/{token}.{ext}"


def proxy_ziel(sig: str, token: str) -> tuple[str, str]:
    if not _ok("p|" + token, sig):
        raise LiveFehler(403, "Ungültige Adresse")
    try:
        url, ref = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (ValueError, TypeError) as exc:
        raise LiveFehler(400, "Ungültige Adresse") from exc
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise LiveFehler(400, "Ungültige Adresse")
    return url, ref


# ---------- Stream finden (mit Zwischenspeicher) ----------

def _pfad(slug: str, staffel: int, folge: int) -> str:
    if staffel == 0:
        return f"/anime/stream/{slug}/filme/film-{folge}"
    return f"/anime/stream/{slug}/staffel-{staffel}/episode-{folge}"


def stream(slug: str, staffel: int, folge: int, sprache: str, sig: str, neu: bool = False) -> dict:
    teil = f"{slug}/{staffel}/{folge}/{sprache}"
    if not _ok("folge|" + teil, sig) or not aniworld.SLUG_RE.match(slug):
        raise LiveFehler(403, "Ungültige Adresse")
    key = (slug, staffel, folge, sprache)
    with _lock:
        lk = _locks.setdefault(key, threading.Lock())
    with lk:  # Jellyfin fragt beim Start oft doppelt (Prüfen + Abspielen) – nur einmal suchen
        hit = _cache.get(key)
        if hit and not neu and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        try:
            s = aniworld.folge_stream(_pfad(slug, staffel, folge), sprache)
        except aniworld.AniFehler as exc:
            raise LiveFehler(502, str(exc)) from exc
        with _lock:
            for k in [k for k, (t, _) in _cache.items() if time.time() - t > CACHE_TTL]:
                _cache.pop(k, None)
            _cache[key] = (time.time(), s)
        return s


# ---------- Holen & Umschreiben ----------

def oeffnen(url: str, referer: str, bereich: str = ""):
    """urllib-Antwort vom Hoster (mit Referer/User-Agent wie im Browser)."""
    p = urllib.parse.urlparse(referer or aniworld.BASE)
    h = {"User-Agent": aniworld.UA, "Referer": referer or aniworld.BASE, "Origin": f"{p.scheme}://{p.netloc}",
         "Accept": "*/*"}
    if bereich:
        h["Range"] = bereich
    return aniworld._opener(True).open(urllib.request.Request(url, headers=h), timeout=aniworld.TIMEOUT)


def ist_playlist(url: str, ctype: str, anfang: bytes = b"") -> bool:
    return ("mpegurl" in (ctype or "").lower() or urllib.parse.urlparse(url).path.lower().endswith(".m3u8")
            or anfang.lstrip().startswith(b"#EXTM3U"))


def umschreiben(text: str, basis_url: str, referer: str) -> str:
    """Jede Adresse in der Playlist (Zeilen und URI="…") durch den Speicher leiten."""
    def ersetzen(uri: str) -> str:
        return _proxy_pfad(urllib.parse.urljoin(basis_url, uri.strip()), referer)

    out = []
    for zeile in text.splitlines():
        s = zeile.strip()
        if not s:
            continue
        if s.startswith("#"):
            out.append(re.sub(r'URI="([^"]+)"', lambda m: f'URI="{ersetzen(m.group(1))}"', s))
        else:
            out.append(ersetzen(s))
    return "\n".join(out) + "\n"


def playlist(slug: str, staffel: int, folge: int, sprache: str, sig: str) -> tuple[str, str]:
    """('m3u8', Text) oder ('weiter', Proxy-Pfad zur MP4)."""
    for versuch in range(2):
        s = stream(slug, staffel, folge, sprache, sig, neu=versuch > 0)
        ref = s.get("referer") or aniworld.BASE
        if not s.get("m3u8"):
            return "weiter", _proxy_pfad(s["mp4"], ref, "mp4")
        try:
            with oeffnen(s["m3u8"], ref) as r:
                text = r.read(4 * 1024 * 1024).decode("utf-8", "replace")
                final = r.geturl()
            return "m3u8", umschreiben(text, final, ref)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404, 410) and versuch == 0:
                continue  # Adresse abgelaufen → neu suchen
            if s.get("mp4"):
                return "weiter", _proxy_pfad(s["mp4"], ref, "mp4")
            raise LiveFehler(502, f"Hoster antwortet mit {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LiveFehler(502, f"Hoster nicht erreichbar: {exc}") from exc
    raise LiveFehler(502, "Kein Stream")


# ---------- .strm-Dateien ----------

def _serien_ordner() -> Path:
    base = (mediathek_db.Einstellungen.load().streaming_ordner or "Streaming").strip("/")
    return mediathek.safe_pool_path(f"{base}/{mediathek.SERIEN}")


def _filme_ordner() -> Path:
    base = (mediathek_db.Einstellungen.load().streaming_ordner or "Streaming").strip("/")
    return mediathek.safe_pool_path(f"{base}/{mediathek.FILME}")


def anlegen(info: dict, folgen: list[dict], sprache: str, einlesen: bool = True) -> dict:
    """info = aniworld.serie(); folgen = [{staffel, nr, titel, …}] → .strm je Folge (fertige Downloads bleiben)."""
    if str(sprache) not in aniworld.SPRACHEN:
        raise aniworld.AniFehler("Unbekannte Sprache")
    if not mediathek.pool_mounted():
        raise aniworld.AniFehler("Der Speicher ist gerade nicht eingehängt")
    neu = vorhanden = 0
    for f in folgen:
        j = {"titel": info["titel"], "jahr": info.get("jahr"), "staffel": int(f["staffel"]), "folge": int(f["nr"]),
             "folgentitel": str(f.get("titel") or "")[:150], "film": info.get("film") or ""}
        mp4 = mediathek.safe_pool_path(aniworld.ziel_rel(j))
        strm = mp4.with_suffix(".strm")
        inhalt = folge_url(info["slug"], j["staffel"], j["folge"], str(sprache)) + "\n"
        if mp4.is_file() or (strm.is_file() and strm.read_text(errors="replace") == inhalt):
            anime_meta.fuer_folge(mp4 if mp4.is_file() else strm, info, j["staffel"], j["folge"], j["folgentitel"], film=j["film"])
            vorhanden += 1
            continue
        strm.parent.mkdir(parents=True, exist_ok=True)
        mediathek._chown_tree(strm.parent, mediathek.pool_root())
        strm.write_text(inhalt)
        mediathek._besitzer(strm)
        anime_meta.fuer_folge(strm, info, j["staffel"], j["folge"], j["folgentitel"], film=j["film"])
        neu += 1
    if neu and einlesen:
        aniworld.jellyfin_jetzt(_filme_ordner() if info.get("film") else _serien_ordner())
    return {"neu": neu, "vorhanden": vorhanden}


def _live_dateien(nur_serien: bool = False):
    for root in [_serien_ordner()] if nur_serien else [_serien_ordner(), _filme_ordner()]:
        if not root.is_dir():
            continue
        for p in root.rglob("*.strm"):
            try:
                if p.is_file() and p.stat().st_size < 1024 and "/live/" in p.read_text(errors="replace"):
                    yield root, p
            except OSError:
                continue


def liste() -> list[dict]:
    """Serien und Filme mit Live-Folgen: [{ordner, folgen, staffeln, film}]."""
    if not mediathek.pool_mounted():
        return []
    filme = _filme_ordner()
    gruppen: dict[str, dict] = {}
    for root, p in _live_dateien():
        rel = p.relative_to(root).parts
        g = gruppen.setdefault(rel[0], {"ordner": rel[0], "folgen": 0, "staffeln": set(), "film": root == filme})
        g["folgen"] += 1
        if len(rel) > 2:
            g["staffeln"].add(rel[1])
    out = sorted(gruppen.values(), key=lambda g: g["ordner"].lower())
    for g in out:
        g["staffeln"] = sorted(g["staffeln"], key=lambda s: (len(s), s))
    return out


def entfernen(ordner: str) -> int:
    """Alle Live-Folgen einer Serie löschen (heruntergeladene Dateien bleiben)."""
    if not ordner or "/" in ordner or "\\" in ordner or ordner in (".", ".."):
        raise aniworld.AniFehler("Ungültiger Ordner")
    root = next((r for r in (_serien_ordner(), _filme_ordner()) if (r / ordner).is_dir()), _serien_ordner())
    ziel = (root / ordner).resolve()
    if ziel.parent != root.resolve() or not ziel.is_dir():
        raise aniworld.AniFehler("Ordner nicht gefunden")
    n = 0
    for _, p in list(_live_dateien()):
        if ziel in p.resolve().parents:
            p.unlink(missing_ok=True)
            anime_meta.nach_loeschen(p)
            n += 1
    for d in sorted([d for d in ziel.rglob("*") if d.is_dir()], key=lambda d: len(d.parts), reverse=True) + [ziel]:
        try:
            d.rmdir()  # nur leere Ordner
        except OSError:
            pass
    if n:
        aniworld.jellyfin_jetzt(root)
    return n


# ---------- Neue Folgen automatisch ----------

NEU_ALLE = 3600  # so oft bei AniWorld nach neuen Folgen der Live-Serien schauen
_neu_lock = threading.Lock()
_neu_gestartet = False


def _abos() -> dict[Path, dict]:
    """Live-Serien aus den .strm lesen: Serienordner → {slug, sprachen, folgen: {staffel: höchste Folge}}.
    Mehr braucht es nicht – in jeder .strm stehen Anime, Staffel, Folge und Sprache."""
    out: dict[Path, dict] = {}
    for root, p in _live_dateien(nur_serien=True):
        try:
            m = ZEILE_RE.match(p.read_text(errors="replace").strip())
        except OSError:
            continue
        if not m:
            continue
        a = out.setdefault(root / p.relative_to(root).parts[0], {"slug": m.group(1), "sprachen": Counter(), "folgen": {}})
        a["sprachen"][m.group(4)] += 1
        s, f = int(m.group(2)), int(m.group(3))
        a["folgen"][s] = max(a["folgen"].get(s, 0), f)
    return out


def _neue_fuer(ordner: Path, a: dict) -> list[dict]:
    """Folgen nach der höchsten vorhandenen je Staffel, dazu neue Staffeln nach der höchsten. Ältere Staffeln, die
    man nicht ausgewählt hat, und einzeln gelöschte Folgen kommen nicht wieder. Gibt es eine Folge noch nicht in der
    Sprache (z. B. Synchro kommt später), hört die Staffel dort auf und wird beim nächsten Mal weiter geprüft."""
    info = aniworld._serie(aniworld._check_slug(a["slug"]))  # frisch, nicht aus dem 15-Minuten-Merker
    soll = Path(aniworld.ziel_rel({"titel": info["titel"], "jahr": info.get("jahr"), "staffel": 1, "folge": 1}))
    if soll.parent.parent.name != ordner.name:
        return []  # Titel/Jahr bei AniWorld geändert – lieber nichts in einen zweiten Ordner legen
    sprache = a["sprachen"].most_common(1)[0][0]
    hoechste = max(a["folgen"])
    wahl = []
    for st in info["staffeln"]:
        if st["film"] or st["nr"] < 1:
            continue
        bis = a["folgen"].get(st["nr"])
        if bis is None and st["nr"] < hoechste:
            continue
        for f in aniworld._staffel(info["slug"], st["nr"]):
            if f["nr"] <= (bis or 0):
                continue
            if f.get("sprachen") and sprache not in f["sprachen"]:
                break
            wahl.append({**f, "staffel": st["nr"]})
    return [{**f, "sprache": sprache, "info": info} for f in wahl]


def neue_folgen() -> int:
    """Bei allen Live-Serien neue Folgen von AniWorld anlegen. Gibt die Zahl neuer Folgen zurück."""
    if not mediathek.pool_mounted():
        return 0
    with _neu_lock:
        n = 0
        for ordner, a in _abos().items():
            try:
                wahl = _neue_fuer(ordner, a)
                if not wahl:
                    continue
                r = anlegen(wahl[0]["info"], wahl, wahl[0]["sprache"], einlesen=False)
            except Exception as exc:  # noqa: BLE001 – eine Serie darf die anderen nicht aufhalten
                jobs.log(None, f"Anime live: neue Folgen für „{ordner.name}“ nicht geprüft: {exc}"[:300])
                continue
            if r["neu"]:
                n += r["neu"]
                jobs.log(None, f"Anime live: {r['neu']} neue Folge{'' if r['neu'] == 1 else 'n'} von „{ordner.name}“")
        if n:
            aniworld.jellyfin_jetzt(_serien_ordner())
        return n


def neue_folgen_start() -> None:
    """Hintergrund: kurz nach dem Start und dann alle NEU_ALLE Sekunden nach neuen Folgen schauen."""
    global _neu_gestartet
    if _neu_gestartet:
        return
    _neu_gestartet = True

    def schleife() -> None:
        time.sleep(10 * 60)  # erst einhängen und Jellyfin hochfahren lassen
        try:
            reparieren()
        except Exception as exc:  # noqa: BLE001
            jobs.log(None, f"Anime live: Aufräumen nach dem Start: {exc}"[:300])
        while True:
            try:
                neue_folgen()
            except Exception as exc:  # noqa: BLE001
                jobs.log(None, f"Anime live: Suche nach neuen Folgen: {exc}"[:300])
            time.sleep(NEU_ALLE)

    threading.Thread(target=schleife, name="anime-neue-folgen", daemon=True).start()


_repariert = False


def reparieren() -> None:
    """Einmal nach dem Start: hat der Pi eine neue IP oder der Port sich geändert, die .strm-Adressen anpassen."""
    global _repariert
    if _repariert or not mediathek.pool_mounted():
        return
    _repariert = True
    jetzt = basis()
    ohne_infos: dict[Path, str] = {}  # Serienordner → slug
    try:
        for root, p in _live_dateien():
            alt = p.read_text(errors="replace").strip()
            m = ZEILE_RE.match(alt)
            if m and not alt.startswith(jetzt + "/"):
                p.write_text(jetzt + alt[alt.index("/live/"):] + "\n")
            if m and not p.with_suffix(".nfo").exists() and root == _serien_ordner():
                ohne_infos.setdefault(root / p.relative_to(root).parts[0], m.group(1))
    except OSError:
        pass
    # Live-Serien von vor den Infodateien: Cover/Beschreibung von AniWorld holen und .nfo nachtragen
    n = 0
    for ordner, slug in ohne_infos.items():
        try:
            n += anime_meta.nachtragen(ordner, aniworld.serie(slug))
        except Exception:  # noqa: BLE001 – beim nächsten Start wieder
            continue
    try:
        n += filme_umziehen()
    except Exception as exc:  # noqa: BLE001
        jobs.log(None, f"Anime: Filme umziehen: {exc}"[:300])
    if n:
        aniworld.jellyfin_spaeter()


def filme_umziehen() -> int:
    """Anime-Filme, die noch als Serie unter Serien/ liegen (vor der Film-Erkennung angelegt, z. B. „Your Name.“),
    nach Filme/<Titel (Jahr)>/ verschieben – live (.strm) wie heruntergeladen. Nur Ordner mit genau einer Folge
    und unserer tvshow.nfo; ob es ein Film ist, sagt AniWorld (aniworld._film_art)."""
    root = _serien_ordner()
    if not mediathek.pool_mounted() or not root.is_dir():
        return 0
    n = 0
    for ordner in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            m = re.search(r'<uniqueid type="aniworld">([a-z0-9-]+)</uniqueid>', (ordner / "tvshow.nfo").read_text(errors="replace"))
            videos = [p for p in ordner.rglob("*") if p.suffix.lower() in anime_meta.VIDEO and p.is_file()]
        except OSError:
            continue
        if not m or len(videos) != 1:
            continue
        try:
            info = aniworld.serie(m.group(1))
        except Exception:  # noqa: BLE001 – beim nächsten Mal
            continue
        if not info.get("film"):
            continue
        alt = videos[0]
        f = anime_meta.FOLGE_RE.search(alt.stem)
        j = {"titel": info["titel"], "jahr": info.get("jahr"), "staffel": int(f.group(1)) if f else 1,
             "folge": int(f.group(2)) if f else 1, "folgentitel": (f.group(3) or "").strip() if f else "", "film": info["film"]}
        rel_neu = str(Path(aniworld.ziel_rel(j)).with_suffix(alt.suffix))
        neu = mediathek.safe_pool_path(rel_neu)
        if neu.exists():
            continue
        neu.parent.mkdir(parents=True, exist_ok=True)
        mediathek._chown_tree(neu.parent, mediathek.pool_root())
        try:
            os.rename(alt, neu)
        except OSError:
            shutil.move(str(alt), str(neu))  # mergerfs: anderer Zweig
        anime_meta.fuer_folge(neu, info, j["staffel"], j["folge"], j["folgentitel"], film=info["film"])
        anime_meta.nach_loeschen(alt)  # alte Infodateien und leere Ordner weg
        rel_alt = str(alt.relative_to(mediathek.pool_root()))
        with mediathek_db.db() as conn:
            conn.execute("UPDATE dateien SET rel_pfad = ?, staffel = NULL, folge = NULL, folgentitel = '' WHERE rel_pfad = ?",
                         (rel_neu, rel_alt))
            conn.execute("UPDATE titel SET typ = 'movie' WHERE ref = ?", (f"aniworld:{info['slug']}",))
        jobs.log(None, f"Anime: „{ordner.name}“ ist ein Film – nach {mediathek.FILME} verschoben")
        n += 1
    return n


def reparieren_im_hintergrund() -> None:
    if not _repariert:
        threading.Thread(target=reparieren, name="anime-live-reparieren", daemon=True).start()
