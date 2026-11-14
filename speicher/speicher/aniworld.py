"""Anime von AniWorld suchen und fertig benannt für Jellyfin in die Mediathek laden.

Ablauf wie im Addon NINA-AniWorld, nur auf dem Server:
  Suche (/ajax/search) → Serienseite (Staffeln, Cover, Jahr) → Staffelseite (Folgen) → Folgenseite
  (Hoster pro Sprache) → /redirect/<id> → VOE-Seite → Stream-Adresse entschlüsseln → ffmpeg lädt nach
  Streaming/Serien/<Titel (Jahr)>/Staffel N/<Titel> - SxxEyy - <Folgentitel>.mp4 → Mediathek + Jellyfin.

Die Seiten holt ein echter Chromium im eigenen Container (Fernsteuerung über CDP, wie bei Redswipe):
BROWSER_CDP=http://127.0.0.1:9222. Ist er nicht erreichbar, geht es direkt per HTTP weiter.
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import config, jobs, mediathek, mediathek_db, mediathek_providers

BASE = "https://aniworld.to"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TIMEOUT = 25
PARALLEL = max(1, min(4, int(os.environ.get("ANIME_PARALLEL", "2") or 2)))
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,150}$")


class AniFehler(Exception):
    pass


# ---------- Seiten holen: Chromium (CDP) oder direkt ----------

def _cdp_base() -> str:
    return os.environ.get("BROWSER_CDP", "").strip().rstrip("/")


def browser_status() -> dict:
    b = _cdp_base()
    if not b:
        return {"an": False, "text": "Kein Browser eingerichtet – direkt"}
    try:
        with _opener().open(urllib.request.Request(f"{b}/json/version"), timeout=3) as r:
            v = json.loads(r.read() or b"{}")
        return {"an": True, "text": v.get("Browser", "Chromium")}
    except Exception:  # noqa: BLE001
        return {"an": False, "text": "Browser nicht erreichbar – direkt"}


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _Browser:
    """Ein Tab im Chromium: öffnen, warten bis die Seite steht, JavaScript auswerten, schließen."""

    _slots = threading.Semaphore(3)

    def __init__(self, url: str):
        self.url = url
        self.base = _cdp_base()
        self.tab = ""
        self.ws = None
        self.n = 0

    def __enter__(self):
        from websockets.sync.client import connect

        self._slots.acquire()
        try:
            req = urllib.request.Request(f"{self.base}/json/new?{urllib.parse.quote(self.url, safe='')}", method="PUT")
            with _opener().open(req, timeout=10) as r:
                t = json.loads(r.read())
            self.tab = t["id"]
            ws_url = f"ws://{urllib.parse.urlparse(self.base).netloc}/devtools/page/{self.tab}"
            self.ws = connect(ws_url, max_size=64 * 1024 * 1024, open_timeout=10)
        except Exception:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        try:
            if self.ws:
                self.ws.close()
        except Exception:  # noqa: BLE001
            pass
        if self.tab:
            try:
                _opener().open(urllib.request.Request(f"{self.base}/json/close/{self.tab}"), timeout=5).close()
            except Exception:  # noqa: BLE001
                pass
        self._slots.release()

    def call(self, method: str, params: dict, timeout: float = 30):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        ende = time.monotonic() + timeout
        while True:
            rest = ende - time.monotonic()
            if rest <= 0:
                raise AniFehler("Der Browser hat zu lange gebraucht")
            msg = json.loads(self.ws.recv(timeout=rest))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise AniFehler(f"Browser: {msg['error'].get('message', 'Fehler')}")
                return msg.get("result") or {}

    def eval(self, expr: str, timeout: float = 30):
        res = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True}, timeout)
        if res.get("exceptionDetails"):
            raise AniFehler("Fehler auf der Seite: " + str(res["exceptionDetails"].get("text", ""))[:200])
        return (res.get("result") or {}).get("value")

    def warte(self, bis: str = "true", timeout: float = 35) -> dict:
        """Wartet, bis die Seite fertig ist, keine Schutzseite (DDoS-Guard/Cloudflare) mehr zeigt und `bis` gilt."""
        expr = ("JSON.stringify({h:location.href,s:document.readyState,t:document.title,"
                f"ok:(()=>{{try{{return !!({bis})}}catch(e){{return false}}}})()}})")
        ende = time.monotonic() + timeout
        letzte = {}
        while time.monotonic() < ende:
            try:
                letzte = json.loads(self.eval(expr, 10) or "{}")
            except (AniFehler, ValueError):
                letzte = {}
            schutz = re.search(r"just a moment|ddos-guard|checking your browser|bitte warten", letzte.get("t", ""), re.I)
            if letzte.get("s") == "complete" and letzte.get("h", "").startswith("http") and not schutz and letzte.get("ok"):
                return letzte
            time.sleep(0.4)
        raise AniFehler("Seite hat im Browser nicht rechtzeitig geladen" + (" (Schutzseite)" if letzte.get("t") else ""))


def _direkt(url: str, data: bytes | None = None, headers: dict | None = None) -> tuple[str, str]:
    h = {"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with _opener().open(req, timeout=TIMEOUT) as r:
            return r.read(8 * 1024 * 1024).decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as exc:
        raise AniFehler(f"{urllib.parse.urlparse(url).hostname} antwortet mit {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AniFehler(f"{urllib.parse.urlparse(url).hostname} nicht erreichbar: {exc}") from exc


def seite(url: str, bis: str = "true") -> tuple[str, str]:
    """(HTML, endgültige Adresse) – bevorzugt über den Browser."""
    if _cdp_base():
        try:
            with _Browser(url) as b:
                info = b.warte(bis)
                return b.eval("document.documentElement.outerHTML") or "", info.get("h") or url
        except Exception:  # noqa: BLE001 – Browser weg oder Seite hängt: direkt noch einmal versuchen
            pass
    return _direkt(url)


# ---------- Suche & Seiten lesen ----------

def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def suche(q: str) -> list[dict]:
    q = (q or "").strip()[:100]
    if len(q) < 2:
        return []
    raw = None
    if _cdp_base():
        try:
            with _Browser(BASE + "/") as b:
                b.warte("document.body")
                raw = b.eval(
                    "fetch('/ajax/search',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',"
                    "'X-Requested-With':'XMLHttpRequest'},body:'keyword='+encodeURIComponent(" + json.dumps(q) + ")}).then(r=>r.text())")
        except Exception:  # noqa: BLE001
            raw = None
    if raw is None:
        raw, _ = _direkt(BASE + "/ajax/search", urllib.parse.urlencode({"keyword": q}).encode(),
                         {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    try:
        items = json.loads(raw)
    except ValueError as exc:
        raise AniFehler("AniWorld hat keine Suchergebnisse geliefert") from exc
    out, seen = [], set()
    for it in items if isinstance(items, list) else []:
        m = re.match(r"^/anime/stream/([a-z0-9-]+)/?$", str(it.get("link") or ""))
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        out.append({"slug": m.group(1), "titel": _text(it.get("title")), "beschreibung": _text(it.get("description"))})
    return out


_cache: dict[tuple, tuple[float, object]] = {}


def _gemerkt(key: tuple, fn, ttl: float = 900):
    """Serien- und Staffelseiten 15 Minuten merken (Cover-Nachladen + Download holen sie sonst mehrfach)."""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    wert = fn()
    if len(_cache) > 300:
        _cache.clear()
    _cache[key] = (time.time(), wert)
    return wert


def _check_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise AniFehler("Ungültiger Anime")
    return slug


def serie(slug: str) -> dict:
    slug = _check_slug(slug)
    return _gemerkt(("serie", slug), lambda: _serie(slug))


def _serie(slug: str) -> dict:
    doc, _ = seite(f"{BASE}/anime/stream/{slug}", "document.querySelector('.series-title')")
    m = re.search(r'<div class="series-title">\s*<h1[^>]*>(.*?)</h1>', doc, re.S)
    titel = _text(re.sub(r"<small.*?</small>", "", m.group(1), flags=re.S)) if m else slug.replace("-", " ").title()
    jahr = re.search(r'itemprop="startDate"[^>]*>.*?\b((?:19|20)\d{2})\b', doc, re.S)
    cover = re.search(r'<div class="seriesCoverBox">\s*<img[^>]*?(?:data-src|src)="(/public/img/cover/[^"]+)"', doc)
    besch = re.search(r'data-full-description="([^"]*)"', doc)
    hinten = re.search(r'class="backdrop"[^>]*background-image:\s*url\(([^)"\']+)\)', doc)
    genres = [_text(g) for g in re.findall(r'<a[^>]*class="genreButton[^"]*"[^>]*>(.*?)</a>', doc, re.S)]
    staffeln = []
    for href, nr, film in re.findall(rf'href="(/anime/stream/{re.escape(slug)}/(?:staffel-(\d+)|(filme)))"', doc):
        key = 0 if film else int(nr)
        if any(s["nr"] == key for s in staffeln):
            continue
        staffeln.append({"nr": key, "film": bool(film), "name": "Filme" if film else f"Staffel {nr}"})
    staffeln.sort(key=lambda s: (s["film"], s["nr"]))
    if not staffeln:
        staffeln = [{"nr": 1, "film": False, "name": "Staffel 1"}]
    return {
        "slug": slug, "titel": titel, "jahr": int(jahr.group(1)) if jahr else None,
        "cover": BASE + cover.group(1).replace("_150x225", "_220x330") if cover else "",
        "beschreibung": html.unescape(besch.group(1)).strip() if besch else "",
        "hintergrund": urllib.parse.urljoin(BASE, hinten.group(1).strip()) if hinten else "",
        "genres": [g for g in dict.fromkeys(genres) if g and g.lower() not in ("ger", "gersub", "eng", "engsub")][:8], "staffeln": staffeln,
    }


SPRACHEN = {"1": "Deutsch", "2": "Eng-Sub", "3": "Ger-Sub"}
FLAGGEN = {"german": "1", "japanese-english": "2", "japanese-german": "3", "english": "2"}


def staffel(slug: str, nr: int) -> list[dict]:
    slug = _check_slug(slug)
    return _gemerkt(("staffel", slug, int(nr)), lambda: _staffel(slug, int(nr)))


def _staffel(slug: str, nr: int) -> list[dict]:
    pfad = f"/anime/stream/{slug}/filme" if nr == 0 else f"/anime/stream/{slug}/staffel-{int(nr)}"
    doc, _ = seite(BASE + pfad, "document.querySelector('table.seasonEpisodesList') || document.querySelector('.series-title')")
    tbody = re.search(r'<table class="seasonEpisodesList".*?</table>', doc, re.S)
    folgen = []
    for zeile in re.findall(r"<tr[^>]*>(.*?)</tr>", tbody.group(0) if tbody else "", re.S):
        a = re.search(r'href="(/anime/stream/[^"]+/(?:staffel-(\d+)/episode-(\d+)|filme/film-(\d+)))"', zeile)
        if not a:
            continue
        de = re.search(r'class="seasonEpisodeTitle".*?<strong>(.*?)</strong>', zeile, re.S)
        en = re.search(r'class="seasonEpisodeTitle".*?<span>(.*?)</span>', zeile, re.S)
        flaggen = re.findall(r'/public/img/([a-z-]+)\.svg', zeile)
        num = int(a.group(3) or a.group(4))
        if any(f["nr"] == num for f in folgen):
            continue
        titel = _text(de.group(1)) if de else ""
        titel_en = re.sub(r"\s*\[(Episode|Film)\s*\d+\]\s*$", "", _text(en.group(1)) if en else "")
        folgen.append({
            "nr": num, "pfad": a.group(1), "titel": titel or titel_en,
            "sprachen": sorted({FLAGGEN[f] for f in flaggen if f in FLAGGEN}),
        })
    return folgen


def _hoster(doc: str) -> list[dict]:
    out = []
    for li in re.findall(r"<li[^>]*data-lang-key[^>]*>.*?</li>", doc, re.S):
        key = re.search(r'data-lang-key="(\d+)"', li)
        ziel = re.search(r'data-link-target="([^"]+)"', li)
        name = re.search(r"<h4>(.*?)</h4>", li, re.S)
        if key and ziel:
            out.append({"sprache": key.group(1), "ziel": urllib.parse.urljoin(BASE, ziel.group(1)), "name": _text(name.group(1)) if name else ""})
    return out


# ---------- VOE (und einfache Hoster) → Stream-Adresse ----------

def voe_entschluesseln(data: str) -> dict:
    """Wie decodeVoeString im Addon: ROT13 → Trenner raus → Base64 → Zeichen −3 → umdrehen → Base64 → JSON."""
    s = data.translate(str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
        "NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyzabcdefghijklm"))
    for d in ("@$", "^^", "~@", "%?", "*~", "!!", "#&"):
        s = s.replace(d, "_")
    s = s.replace("_", "")
    b = base64.b64decode(s + "=" * (-len(s) % 4)).decode("latin-1")
    shifted = "".join(chr(ord(c) - 3) for c in b)[::-1]
    return json.loads(base64.b64decode(shifted + "=" * (-len(shifted) % 4)).decode("utf-8"))


def stream_aus_hoster(redirect_url: str) -> dict:
    """{m3u8, mp4, referer} aus einem Hoster-Link (/redirect/<id>)."""
    doc, final = seite(redirect_url, "location.hostname.indexOf('aniworld')<0")
    js = re.search(r"window\.location\.href\s*=\s*['\"](https://[^'\"]+)['\"]", doc)
    if js and "application/json" not in doc:
        doc, final = seite(js.group(1), "document.querySelector('script[type=\"application/json\"]') || document.querySelector('video')")
    j = re.search(r'<script type="application/json">\s*\["([\s\S]*?)"\]\s*</script>', doc)
    if j:
        try:
            d = voe_entschluesseln(j.group(1))
            if d.get("source") or d.get("direct_access_url"):
                return {"m3u8": d.get("source") or "", "mp4": d.get("direct_access_url") or "", "referer": final}
        except (ValueError, UnicodeDecodeError, KeyError):
            pass
    hls = re.search(r"['\"](https://[^'\"\s]+?\.m3u8[^'\"\s]*)['\"]", doc)
    mp4 = re.search(r"['\"](https://[^'\"\s]+?\.mp4[^'\"\s]*)['\"]", doc)
    if hls or mp4:
        return {"m3u8": html.unescape(hls.group(1)) if hls else "", "mp4": html.unescape(mp4.group(1)) if mp4 else "", "referer": final}
    raise AniFehler("Kein Video-Stream beim Hoster gefunden")


def folge_stream(pfad: str, sprache: str) -> dict:
    if not re.match(r"^/anime/stream/[a-z0-9-]+/(staffel-\d+/episode-\d+|filme/film-\d+)$", pfad or ""):
        raise AniFehler("Ungültige Folge")
    doc, _ = seite(BASE + pfad, "document.querySelector('li[data-lang-key]') || document.querySelector('.series-title')")
    alle = _hoster(doc)
    meine = [h for h in alle if h["sprache"] == str(sprache)]
    if not meine:
        raise AniFehler(f"Nicht auf {SPRACHEN.get(str(sprache), 'dieser Sprache')} verfügbar")
    meine.sort(key=lambda h: (0 if "voe" in h["name"].lower() else 1))
    letzter = "Kein Hoster lieferte einen Stream"
    for h in meine:
        try:
            s = stream_aus_hoster(h["ziel"])
            s["hoster"] = h["name"]
            return s
        except AniFehler as exc:
            letzter = f"{h['name']}: {exc}"
    raise AniFehler(letzter)


# ---------- Warteschlange ----------

_lock = threading.Lock()
_jobs: list[dict] = []
_procs: dict[str, subprocess.Popen] = {}
_gestartet = False
_jellyfin_faellig = False


def _datei() -> Path:
    return config.data_dir() / "anime_downloads.json"


def _speichern() -> None:
    try:
        tmp = _datei().with_suffix(".tmp")
        tmp.write_text(json.dumps(_jobs, ensure_ascii=False))
        os.replace(tmp, _datei())
    except OSError:
        pass


def _laden() -> None:
    try:
        daten = json.loads(_datei().read_text())
    except (OSError, ValueError):
        return
    for j in daten if isinstance(daten, list) else []:
        if j.get("status") == "laeuft":  # nach Neustart von vorn
            j.update(status="wartet", fortschritt=0, meldung="")
        _jobs.append(j)


def start() -> None:
    global _gestartet
    with _lock:
        if _gestartet:
            return
        _gestartet = True
        _laden()
    for i in range(PARALLEL):
        threading.Thread(target=_worker, name=f"anime-{i}", daemon=True).start()


def liste() -> list[dict]:
    with _lock:
        return [dict(j) for j in _jobs]


def ziel_rel(j: dict) -> str:
    base = (mediathek_db.Einstellungen.load().streaming_ordner or "Streaming").strip("/")
    name = mediathek_providers.sanitize_name(j["titel"])
    ordner = f"{name} ({j['jahr']})" if j.get("jahr") else name
    s, e = int(j["staffel"]), int(j["folge"])
    t = mediathek_providers.sanitize_name(j.get("folgentitel") or "", "")
    datei = f"{name} - S{s:02d}E{e:02d}" + (f" - {t[:80]}" if t else "") + ".mp4"
    return f"{base}/{mediathek.SERIEN}/{ordner}/Staffel {s}/{datei}"


def hinzufuegen(info: dict, folgen: list[dict], sprache: str) -> int:
    """info = serie(); folgen = [{staffel, nr, pfad, titel}] – legt fehlende Folgen in die Warteschlange."""
    sprache = str(sprache)
    if sprache not in SPRACHEN:
        raise AniFehler("Unbekannte Sprache")
    neu = 0
    with _lock:
        offen = {(j["pfad"], j["sprache"]) for j in _jobs if j["status"] in ("wartet", "laeuft")}
        for f in folgen:
            pfad = str(f.get("pfad") or "")
            if not re.match(rf"^/anime/stream/{re.escape(info['slug'])}/(staffel-\d+/episode-\d+|filme/film-\d+)$", pfad):
                continue
            if (pfad, sprache) in offen:
                continue
            _jobs.append({
                "id": uuid.uuid4().hex[:12], "slug": info["slug"], "titel": info["titel"], "jahr": info.get("jahr"),
                "cover": info.get("cover", ""), "hintergrund": info.get("hintergrund", ""), "beschreibung": (info.get("beschreibung") or "")[:1500],
                "genres": info.get("genres") or [], "staffel": int(f.get("staffel") or 0), "folge": int(f.get("nr") or 0),
                "folgentitel": str(f.get("titel") or "")[:150], "pfad": pfad, "sprache": sprache,
                "status": "wartet", "fortschritt": 0, "bytes": 0, "meldung": "", "erstellt": time.time(),
            })
            neu += 1
        _speichern()
    return neu


def abbrechen(job_id: str) -> bool:
    with _lock:
        for j in _jobs:
            if j["id"] == job_id and j["status"] in ("wartet", "laeuft", "fehler"):
                j["status"] = "abgebrochen"
                j["meldung"] = "Abgebrochen"
                p = _procs.get(job_id)
                if p:
                    p.kill()
                _speichern()
                return True
    return False


def wiederholen(job_id: str = "") -> int:
    n = 0
    with _lock:
        for j in _jobs:
            if (not job_id or j["id"] == job_id) and j["status"] in ("fehler", "abgebrochen"):
                j.update(status="wartet", fortschritt=0, meldung="")
                n += 1
        _speichern()
    return n


def aufraeumen() -> int:
    with _lock:
        vorher = len(_jobs)
        _jobs[:] = [j for j in _jobs if j["status"] in ("wartet", "laeuft")]
        _speichern()
        return vorher - len(_jobs)


def jellyfin_spaeter() -> None:
    """Jellyfin neu einlesen lassen, sobald keine Downloads mehr laufen (z. B. nach dem Löschen)."""
    global _jellyfin_faellig
    _jellyfin_faellig = True


def jellyfin_jetzt(pfad: Path) -> None:
    """Live-Folgen sind sofort fertig: die Mediathek gleich einlesen, nicht erst wenn die Downloads durch sind."""
    def los() -> None:
        try:
            from . import jellyfin
            if jellyfin.mediathek_einlesen(str(pfad)):
                jobs.log(None, "Jellyfin liest die neuen Anime ein")
                return
        except Exception:  # noqa: BLE001 – dann wie bisher nach den Downloads
            pass
        jellyfin_spaeter()
    threading.Thread(target=los, name="jellyfin-einlesen", daemon=True).start()


def _set(j: dict, **werte) -> None:
    with _lock:
        j.update(werte)


def _naechster() -> dict | None:
    with _lock:
        for j in _jobs:
            if j["status"] == "wartet":
                j.update(status="laeuft", meldung="Suche Stream …", fortschritt=0)
                _speichern()
                return j
    return None


def _worker() -> None:
    global _jellyfin_faellig
    while True:
        j = _naechster()
        if not j:
            if _jellyfin_faellig and not any(x["status"] in ("wartet", "laeuft") for x in liste()):
                _jellyfin_faellig = False
                _jellyfin_neu_einlesen()
            time.sleep(2)
            continue
        try:
            fertig = _laden_job(j)
            if j["status"] == "laeuft":
                _set(j, status="fertig" if fertig else "vorhanden", fortschritt=1,
                     meldung=j.get("meldung") if fertig else "Schon in der Mediathek")
            if fertig:
                _jellyfin_faellig = True
        except Exception as exc:  # noqa: BLE001 – ein Fehler darf die Warteschlange nicht stoppen
            if j["status"] == "laeuft":
                _set(j, status="fehler", meldung=str(exc)[:300])
                jobs.log(None, f"Anime-Download fehlgeschlagen ({j['titel']} S{j['staffel']:02d}E{j['folge']:02d}): {exc}")
        finally:
            with _lock:
                _procs.pop(j["id"], None)
                _speichern()


def _laden_job(j: dict) -> bool:
    if not mediathek.pool_mounted():
        raise AniFehler("Der Speicher ist gerade nicht eingehängt")
    rel = ziel_rel(j)
    dst = mediathek.safe_pool_path(rel)
    if dst.is_file() and dst.stat().st_size > 1024 * 1024:
        dst.with_suffix(".strm").unlink(missing_ok=True)  # Live-Version nicht doppelt in Jellyfin
        _meta(j, dst)
        _eintragen(j, rel, dst)
        return False
    stream = folge_stream(j["pfad"], j["sprache"])
    if j["status"] != "laeuft":
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    mediathek._chown_tree(dst.parent, mediathek.pool_root())
    tmp = dst.with_name(dst.name + ".part")
    fehler = ""
    for url in [u for u in (stream.get("m3u8"), stream.get("mp4")) if u]:
        _set(j, meldung=f"Lädt über {stream.get('hoster') or 'Hoster'} …")
        fehler = _ffmpeg(j, url, stream.get("referer") or BASE, tmp)
        if not fehler or j["status"] != "laeuft":
            break
    if j["status"] != "laeuft":
        tmp.unlink(missing_ok=True)
        return False
    if fehler:
        tmp.unlink(missing_ok=True)
        raise AniFehler(fehler)
    os.replace(tmp, dst)
    mediathek._besitzer(dst)
    dst.with_suffix(".strm").unlink(missing_ok=True)  # war vorher live in Jellyfin → jetzt die Datei
    _meta(j, dst)
    _eintragen(j, rel, dst)
    _set(j, meldung=f"Fertig · {dst.stat().st_size / 1e6:,.0f} MB".replace(",", "."))
    jobs.log(None, f"Anime geladen: {dst.name}")
    return True


def _ffmpeg(j: dict, url: str, referer: str, tmp: Path) -> str:
    """Lädt den Stream ohne Umwandeln als MP4. Gibt '' oder eine Fehlermeldung zurück."""
    tmp.unlink(missing_ok=True)
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-y", "-loglevel", "info",
        "-user_agent", UA, "-headers", f"Referer: {referer}\r\n",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
        "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart",
        "-f", "mp4", "-progress", "pipe:1", "-nostats", str(tmp),
    ]
    if url.lower().split("?")[0].endswith(".mp4"):
        cmd = [c for c in cmd if c not in ("-bsf:a", "aac_adtstoasc")]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
                             preexec_fn=lambda: os.nice(10))
    except OSError as exc:
        return f"ffmpeg nicht startbar: {exc}"
    with _lock:
        _procs[j["id"]] = p
    dauer = [0.0]
    log: list[str] = []

    def stderr_lesen():
        for zeile in p.stderr:
            m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", zeile)
            if m and not dauer[0]:
                dauer[0] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            log.append(zeile.strip())
            del log[:-20]

    t = threading.Thread(target=stderr_lesen, daemon=True)
    t.start()
    for zeile in p.stdout:
        k, _, v = zeile.strip().partition("=")
        if k == "out_time_us" and v.isdigit() and dauer[0]:
            _set(j, fortschritt=min(0.99, int(v) / 1e6 / dauer[0]))
        elif k == "total_size" and v.isdigit():
            _set(j, bytes=int(v))
    p.wait()
    t.join(timeout=2)
    if p.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 512 * 1024:
        return ""
    fehler = next((z for z in reversed(log) if z and not z.startswith(("Press", "frame="))), "")
    return f"Download fehlgeschlagen: {fehler[:200] or 'ffmpeg Code ' + str(p.returncode)}"


def _meta(j: dict, dst: Path) -> None:
    """Infos + Cover für Jellyfin neben die Folge legen (siehe anime_meta.py)."""
    from . import anime_meta
    info = {"slug": j["slug"], "titel": j["titel"], "jahr": j.get("jahr"), "cover": j.get("cover", ""),
            "hintergrund": j.get("hintergrund", ""),
            "beschreibung": j.get("beschreibung", ""), "genres": j.get("genres") or []}
    anime_meta.fuer_folge(dst, info, int(j["staffel"]), int(j["folge"]), j.get("folgentitel", ""))


def _eintragen(j: dict, rel: str, dst: Path) -> None:
    """In die Mediathek (Streaming-Seite) eintragen; Umwandeln nur, wenn das Format nicht passt."""
    titel_id = mediathek_db.upsert_title({
        "ref": f"aniworld:{j['slug']}", "provider": "aniworld", "type": "show", "anime": True,
        "name": j["titel"], "year": j.get("jahr"), "poster": j.get("cover", ""),
        "summary": j.get("beschreibung", ""), "genres": j.get("genres") or [],
        "url": f"{BASE}/anime/stream/{j['slug']}",
    })
    fid = mediathek_db.register_file(titel_id, rel, staffel=j["staffel"], folge=j["folge"],
                                     folgentitel=j.get("folgentitel", ""), status="umwandlung", groesse=dst.stat().st_size)
    mediathek.finalize_uploaded_streaming_file(fid)


def _jellyfin_neu_einlesen() -> None:
    """Nur die Mediathek „Serien“ einlesen. Kein /Library/Refresh: Das bricht einen laufenden Scan ab und fängt von
    vorn an – kamen Downloads nacheinander fertig, wurde Jellyfin so nie fertig (nur ein Teil der Folgen da)."""
    global _jellyfin_faellig
    try:
        from . import jellyfin
        base = (mediathek_db.Einstellungen.load().streaming_ordner or "Streaming").strip("/")
        if jellyfin.mediathek_einlesen(str(mediathek.safe_pool_path(f"{base}/{mediathek.SERIEN}"))):
            jobs.log(None, "Jellyfin liest die neuen Anime ein")
            return
        token = jellyfin._token()
        if jellyfin.scan_laeuft(token):
            _jellyfin_faellig = True  # der laufende Scan findet sie vielleicht schon; sonst gleich danach
            return
        jellyfin._req("POST", "/Library/Refresh", token=token)
        jobs.log(None, "Jellyfin liest die neuen Anime ein")
    except Exception:  # noqa: BLE001 – Jellyfin findet sie sonst beim nächsten Scan
        pass
