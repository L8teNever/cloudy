"""Jellyfin (Medienserver im Stack) automatisch einrichten.

Jellyfin spielt Videos auf Fernsehern, Handys und im Browser ab und wandelt nur beim Abspielen und nur bei Bedarf
um – die Dateien selbst bleiben unverändert. Der Speicher erledigt die Einrichtung von selbst:
  - Ersteinrichtung auf Deutsch, Administrator „admin“ mit zufälligem Passwort (steht auf der Freigaben-Seite)
  - Mediatheken: Filme und Serien aus der Mediathek, dazu jede Freigabe mit „Auf Fernsehern zeigen“ als Heimvideos
  - Benutzer: jeder Speicher-Benutzer bekommt ein Jellyfin-Konto mit demselben Passwort, sobald es gesetzt/geändert wird
Jellyfin sieht den Pool nur lesend (/mnt/cloudy, siehe docker-compose.yml).
"""
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, freigaben, jobs, pool
from .config import data_dir

ADMIN = "admin"
CLIENT = 'MediaBrowser Client="cloudy Speicher", Device="speicher", DeviceId="cloudy-speicher", Version="1.0"'
_lock = threading.Lock()  # nur für _state (kurz)
_arbeit = threading.Lock()  # ein Abgleich zur Zeit
_state: dict = {"erreichbar": False, "eingerichtet": False, "fehler": "", "version": "", "geprueft": 0.0}


class JellyfinFehler(RuntimeError):
    pass


def url() -> str:
    return (os.environ.get("JELLYFIN_URL") or "http://127.0.0.1:8096").rstrip("/")


def port() -> int:
    try:
        return urllib.parse.urlparse(url()).port or 8096
    except ValueError:
        return 8096


def medien_pfad() -> str:
    """So sieht Jellyfin den Pool (gleicher Pfad wie auf dem Host)."""
    return str(pool.paths()["pool"])


# ---------- Zugangsdaten ----------

def _datei():
    return data_dir() / "jellyfin.json"


def zugang() -> dict:
    try:
        return json.loads(_datei().read_text())
    except (OSError, ValueError):
        return {}


def _zugang_speichern(**werte) -> None:
    d = zugang()
    d.update(werte)
    config.write_private(_datei(), json.dumps(d, indent=2))


# ---------- HTTP ----------

def _req(method: str, path: str, body=None, token: str = "", timeout: float = 15):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url() + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    auth = CLIENT + (f', Token="{token}"' if token else "")
    req.add_header("Authorization", auth)
    req.add_header("X-Emby-Authorization", auth)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise JellyfinFehler(f"{method} {path}: HTTP {exc.code} {exc.read()[:200]!r}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise JellyfinFehler(f"Jellyfin nicht erreichbar ({url()}): {exc}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def info() -> dict:
    return _req("GET", "/System/Info/Public", timeout=5) or {}


def anmelden(name: str, passwort: str) -> str:
    res = _req("POST", "/Users/AuthenticateByName", {"Username": name, "Pw": passwort})
    return (res or {}).get("AccessToken", "")


def _token() -> str:
    z = zugang()
    if not z.get("passwort"):
        raise JellyfinFehler("Kein Administrator-Zugang gespeichert")
    return anmelden(z.get("admin", ADMIN), z["passwort"])


# ---------- Einrichten ----------

def ersteinrichtung() -> None:
    """Assistent beim allerersten Start von Jellyfin durchlaufen (Deutsch, Administrator)."""
    pw = zugang().get("passwort") or secrets.token_urlsafe(12)
    _zugang_speichern(admin=ADMIN, passwort=pw)
    _req("POST", "/Startup/Configuration",
         {"UICulture": "de", "MetadataCountryCode": "DE", "PreferredMetadataLanguage": "de"})
    _req("GET", "/Startup/User")
    _req("POST", "/Startup/User", {"Name": ADMIN, "Password": pw})
    _req("POST", "/Startup/RemoteAccess", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
    _req("POST", "/Startup/Complete")
    jobs.log(None, "Jellyfin eingerichtet (Administrator: admin)")


def gewuenschte_mediatheken() -> list[dict]:
    """[{name, typ, pfad}] – Filme/Serien aus der Mediathek und Freigaben fürs Fernsehen."""
    from . import mediathek, mediathek_db

    root = medien_pfad()
    base = (mediathek_db.Einstellungen.load().streaming_ordner or "Streaming").strip("/")
    out = [
        {"name": "Filme", "typ": "movies", "pfad": f"{root}/{base}/{mediathek.FILME}"},
        {"name": "Serien", "typ": "tvshows", "pfad": f"{root}/{base}/{mediathek.SERIEN}"},
    ]
    e = freigaben.Einstellungen.load()
    namen = {"filme", "serien"}
    for f in e.freigaben:
        if not f.dlna or f.name.lower() in namen:
            continue
        namen.add(f.name.lower())
        out.append({"name": f.name, "typ": "homevideos", "pfad": f"{root}/{f.ordner}" if f.ordner else root})
    return out


def mediatheken_abgleichen(token: str) -> int:
    """Fehlende Mediatheken anlegen (vorhandene bleiben, auch selbst angelegte). Gibt die Zahl neuer zurück."""
    vorhanden = {(v.get("Name") or "").lower() for v in (_req("GET", "/Library/VirtualFolders", token=token) or [])}
    neu = 0
    for m in gewuenschte_mediatheken():
        if m["name"].lower() in vorhanden:
            continue
        if not os.path.isdir(m["pfad"]):
            os.makedirs(m["pfad"], exist_ok=True)
            try:
                os.chown(m["pfad"], int(os.environ.get("SPEICHER_UID", "1000")), int(os.environ.get("SPEICHER_GID", "1000")))
            except (OSError, ValueError):
                pass
        q = urllib.parse.urlencode({"name": m["name"], "collectionType": m["typ"], "paths": m["pfad"], "refreshLibrary": "true"})
        _req("POST", f"/Library/VirtualFolders?{q}", {"LibraryOptions": {
            "PreferredMetadataLanguage": "de", "MetadataCountryCode": "DE", "EnableRealtimeMonitor": True,
            "EnableChapterImageExtraction": False, "ExtractChapterImagesDuringLibraryScan": False,
            "SaveLocalMetadata": False,  # nichts in die Ordner schreiben (Pool ist für Jellyfin ohnehin nur lesbar)
        }}, token=token, timeout=60)
        jobs.log(None, f"Jellyfin: Mediathek „{m['name']}“ angelegt")
        neu += 1
    return neu


def benutzer_setzen(name: str, passwort: str) -> None:
    """Jellyfin-Konto für einen Speicher-Benutzer anlegen bzw. sein Passwort setzen (still, wenn Jellyfin fehlt)."""
    if not zugang().get("passwort"):
        return
    try:
        token = _token()
        users = {(u.get("Name") or "").lower(): u for u in (_req("GET", "/Users", token=token) or [])}
        u = users.get(name.lower())
        if u is None:
            u = _req("POST", "/Users/New", {"Name": name, "Password": passwort}, token=token) or {}
            jobs.log(None, f"Jellyfin: Benutzer {name} angelegt")
        else:
            _req("POST", f"/Users/{u['Id']}/Password", {"CurrentPw": "", "NewPw": passwort, "ResetPassword": False}, token=token)
    except JellyfinFehler as exc:
        jobs.log(None, f"Jellyfin-Benutzer {name}: {exc}")


def benutzer_loeschen(name: str) -> None:
    if not zugang().get("passwort"):
        return
    try:
        token = _token()
        for u in _req("GET", "/Users", token=token) or []:
            if (u.get("Name") or "").lower() == name.lower() and name.lower() != ADMIN:
                _req("DELETE", f"/Users/{u['Id']}", token=token)
    except JellyfinFehler as exc:
        jobs.log(None, f"Jellyfin-Benutzer {name} löschen: {exc}")


def abgleichen() -> None:
    """Regelmäßig: Jellyfin da? Einmalig einrichten, Mediatheken ergänzen. Fehler landen im Status, nie als Ausnahme."""
    if not _arbeit.acquire(blocking=False):
        return
    try:
        _abgleichen()
    finally:
        _arbeit.release()


def _setze(**werte) -> None:
    with _lock:
        _state.update(**werte)


def _abgleichen() -> None:
    try:
        i = info()
    except JellyfinFehler:
        _setze(erreichbar=False, eingerichtet=False, fehler="", geprueft=time.time())
        return
    _setze(erreichbar=True, version=i.get("Version", ""), geprueft=time.time())
    try:
        if not i.get("StartupWizardCompleted"):
            ersteinrichtung()
        if not zugang().get("passwort"):
            _setze(eingerichtet=False, fehler="Jellyfin wurde schon von Hand eingerichtet – Zugang hier unbekannt")
            return
        if pool.paths()["pool"].is_dir() and str(pool.paths()["pool"]) in pool.mounts():
            from . import mediathek
            mediathek.filme_serien_trennen()
            mediatheken_abgleichen(_token())
        if not zugang().get("umpacken_aus"):
            # Einmalig: Ab jetzt wandelt Jellyfin beim Abspielen um – nicht mehr alle Videos vorab umpacken.
            # (Wer es danach wieder einschaltet, behält es eingeschaltet.)
            e = freigaben.Einstellungen.load()
            if e.videos_optimieren:
                e.videos_optimieren = False
                e.save()
                jobs.log(None, "Jellyfin ist da: Videos auf den Platten werden nicht mehr vorab umgepackt")
            _zugang_speichern(umpacken_aus=True)
        _setze(eingerichtet=True, fehler="")
    except (JellyfinFehler, OSError) as exc:
        _setze(fehler=str(exc)[:300])


def status() -> dict:
    with _lock:
        s = dict(_state)
    z = zugang()
    s.update(admin=z.get("admin", ADMIN) if z.get("passwort") else "", passwort=z.get("passwort", ""), port=port())
    return s
