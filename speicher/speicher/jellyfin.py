"""Jellyfin (Medienserver im Stack) automatisch einrichten.

Jellyfin spielt Videos auf Fernsehern, Handys und im Browser ab und wandelt nur beim Abspielen und nur bei Bedarf
um – die Dateien selbst bleiben unverändert. Der Speicher erledigt die Einrichtung von selbst:
  - Ersteinrichtung auf Deutsch, Administrator „admin“ mit zufälligem Passwort (steht auf der Freigaben-Seite)
  - Mediatheken: Filme und Serien aus der Mediathek, dazu jede Freigabe mit „Auf Fernsehern zeigen“ als Heimvideos
  - Benutzer: jeder Speicher-Benutzer bekommt ein Jellyfin-Konto mit demselben Passwort, sobald es gesetzt/geändert wird,
    und erscheint als Kachel auf der Anmeldeseite (Profil antippen statt Namen tippen; admin bleibt versteckt)
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
        if not f.dlna or f.name.lower() in namen or _im_streaming(f.ordner, base):
            continue  # Streaming-Ordner kommt schon als Filme/Serien mit Cover und Beschreibung
        namen.add(f.name.lower())
        out.append({"name": f.name, "typ": "homevideos", "pfad": f"{root}/{f.ordner}" if f.ordner else root})
    return out


def _im_streaming(ordner: str, base: str) -> bool:
    """Liegt der Ordner (relativ zum Pool) im Streaming-Ordner oder ist es dieser selbst?"""
    o, b = (ordner or "").strip("/").lower(), base.strip("/").lower()
    return bool(o) and (o == b or o.startswith(b + "/"))


def _doppelte_streaming_mediatheken(token: str, ordner: list[dict]) -> list[str]:
    """Heimvideo-Mediatheken, die in den Streaming-Ordner zeigen, entfernen: Dort zeigt Jellyfin nur Ordner ohne
    Cover und Beschreibung, und dieselben Serien/Filme stehen schon richtig unter „Serien“/„Filme“.
    Nur die Mediathek in Jellyfin wird entfernt, die Dateien bleiben."""
    from . import mediathek_db

    root = medien_pfad().rstrip("/")
    base = (mediathek_db.Einstellungen.load().streaming_ordner or "Streaming").strip("/")
    weg = []
    for v in ordner:
        orte = v.get("Locations") or []
        if (v.get("CollectionType") or "").lower() != "homevideos" or not orte:
            continue
        rel = [o.rstrip("/")[len(root) + 1:] if o.rstrip("/").startswith(root + "/") else None for o in orte]
        if all(r is not None and _im_streaming(r, base) for r in rel):
            q = urllib.parse.urlencode({"name": v.get("Name") or "", "refreshLibrary": "false"})
            _req("DELETE", f"/Library/VirtualFolders?{q}", token=token, timeout=60)
            jobs.log(None, f"Jellyfin: doppelte Mediathek „{v.get('Name')}“ entfernt (steht schon unter Filme/Serien)")
            weg.append((v.get("Name") or "").lower())
    return weg


def mediatheken_abgleichen(token: str) -> int:
    """Fehlende Mediatheken anlegen (vorhandene bleiben, auch selbst angelegte). Gibt die Zahl neuer zurück."""
    ordner = _req("GET", "/Library/VirtualFolders", token=token) or []
    weg = set(_doppelte_streaming_mediatheken(token, ordner))
    vorhanden = {(v.get("Name") or "").lower() for v in ordner} - weg
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


GPU_HERSTELLER = {"0x8086": "intel", "0x1002": "amd", "0x10de": "nvidia"}


def gpu() -> str:
    """Hersteller der Grafikeinheit für Jellyfin ("intel", "amd", "nvidia" oder "")."""
    if not os.path.exists("/dev/dri/renderD128"):
        return ""
    try:
        with open("/sys/class/drm/renderD128/device/vendor") as fh:
            return GPU_HERSTELLER.get(fh.read().strip().lower(), "")
    except OSError:
        return ""


def hardware_einrichten(token: str) -> str:
    """Einmalig: Umrechnen über die Grafikeinheit (VA-API) einschalten, wenn Intel/AMD da ist und in Jellyfin
    noch nichts eingestellt wurde. Vorsichtig nur H.264/MPEG-2 per Grafik entschlüsseln (kann jede solche Grafik);
    HEVC/AV1 kann man in Jellyfin selbst dazuschalten, wenn die Grafik neu genug ist."""
    if zugang().get("hardware"):
        return zugang()["hardware"]
    hersteller = gpu()
    if hersteller not in ("intel", "amd"):
        _zugang_speichern(hardware="keine" if not hersteller else f"{hersteller} (nicht automatisch)")
        return zugang()["hardware"]
    enc = _req("GET", "/System/Configuration/encoding", token=token) or {}
    if enc.get("HardwareAccelerationType") not in (None, "", "none"):
        _zugang_speichern(hardware=f"schon eingestellt ({enc.get('HardwareAccelerationType')})")
        return zugang()["hardware"]
    enc.update({
        "HardwareAccelerationType": "vaapi",
        "VaapiDevice": "/dev/dri/renderD128",
        "EnableHardwareEncoding": True,
        "HardwareDecodingCodecs": ["h264", "mpeg2video"],
        "EnableThrottling": True,  # nicht weiter vorausrechnen als nötig – spart Leistung
    })
    _req("POST", "/System/Configuration/encoding", enc, token=token)
    jobs.log(None, f"Jellyfin: Umrechnen über die Grafikeinheit ({hersteller}, VA-API) eingeschaltet")
    _zugang_speichern(hardware=f"{hersteller} (VA-API)")
    return zugang()["hardware"]


TEST_GERAET = "cloudy-umrechnen-test"


def _testvideo(token: str):
    """(Item-Id, MediaSource-Id) eines echten Videos auf der Platte – keine .strm (die hängen am Internet)."""
    q = urllib.parse.urlencode({"Recursive": "true", "IncludeItemTypes": "Movie,Episode,Video", "Fields": "Path,MediaSources",
                                "Limit": "100", "IsMissing": "false"})
    for it in (_req("GET", f"/Items?{q}", token=token, timeout=30) or {}).get("Items") or []:
        if (it.get("Path") or "").lower().endswith(".strm"):
            continue
        for ms in it.get("MediaSources") or []:
            if ms.get("IsRemote") or (ms.get("Protocol") or "File") != "File":
                continue
            if any(st.get("Type") == "Video" for st in ms.get("MediaStreams") or []):
                return it["Id"], ms["Id"]
    return None


def umrechnen_testen(token: str, video=None):
    """Lässt Jellyfin ein Stück Video wirklich umrechnen (H.264 klein, kein Kopieren) – genau das, was Fernseher und
    Browser brauchen, wenn sie die Datei nicht direkt abspielen können. True/False, None = kein Video zum Testen."""
    video = video or _testvideo(token)
    if not video:
        return None
    item, quelle = video
    sitzung = secrets.token_hex(8)
    q = urllib.parse.urlencode({
        "MediaSourceId": quelle, "Static": "false", "VideoCodec": "h264", "AudioCodec": "aac", "MaxWidth": "640",
        "MaxHeight": "360", "VideoBitrate": "500000", "AudioBitrate": "64000", "AudioChannels": "2",
        "AllowVideoStreamCopy": "false", "AllowAudioStreamCopy": "false", "DeviceId": TEST_GERAET, "PlaySessionId": sitzung,
    })
    req = urllib.request.Request(f"{url()}/Videos/{item}/stream.ts?{q}")
    auth = CLIENT + f', Token="{token}"'
    req.add_header("Authorization", auth)
    req.add_header("X-Emby-Authorization", auth)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    menge = 0
    try:
        with opener.open(req, timeout=90) as resp:
            ende = time.time() + 90
            while menge < 256 * 1024 and time.time() < ende:
                stueck = resp.read(16384)
                if not stueck:
                    break
                menge += len(stueck)
    except (urllib.error.URLError, OSError):
        pass
    finally:
        try:
            _req("DELETE", "/Videos/ActiveEncodings?" + urllib.parse.urlencode({"deviceId": TEST_GERAET, "playSessionId": sitzung}),
                 token=token)
        except JellyfinFehler:
            pass
    return menge >= 32 * 1024


def hardware_pruefen(token: str) -> None:
    """Einmalig nach dem Einschalten: Klappt das Umrechnen über die Grafikeinheit wirklich? Wenn nicht, bricht in
    Jellyfin jede Wiedergabe ab, die umgerechnet werden muss („Dieser Client ist nicht mit dem Medium kompatibel …“) –
    besonders in Fernseh-Browsern, die vieles nicht direkt abspielen. Dann auf den Prozessor zurückschalten."""
    z = zugang()
    if z.get("hardware_geprueft") or not str(z.get("hardware", "")).endswith("(VA-API)"):
        return
    video = _testvideo(token)
    if not video:
        return  # noch kein Video da – beim nächsten Abgleich wieder
    if umrechnen_testen(token, video):
        _zugang_speichern(hardware_geprueft=True)
        jobs.log(None, "Jellyfin: Umrechnen über die Grafikeinheit getestet – klappt")
        return
    enc = _req("GET", "/System/Configuration/encoding", token=token) or {}
    vorher = dict(enc)
    enc.update({"HardwareAccelerationType": "none", "EnableHardwareEncoding": False, "HardwareDecodingCodecs": []})
    _req("POST", "/System/Configuration/encoding", enc, token=token)
    if umrechnen_testen(token, video):
        _zugang_speichern(hardware="keine", hardware_geprueft=True,
                          hardware_hinweis=f"Die Grafikeinheit ({z['hardware'].split()[0]}) konnte nicht umrechnen – jetzt rechnet der Prozessor.")
        jobs.log(None, "Jellyfin: Umrechnen über die Grafikeinheit klappte nicht – auf den Prozessor umgestellt")
        return
    # Auch der Prozessor schafft es nicht: Grafik-Einstellung lassen, Fehler zeigen.
    _req("POST", "/System/Configuration/encoding", vorher, token=token)
    _zugang_speichern(hardware_geprueft=True,
                      hardware_hinweis="Umrechnen klappt in Jellyfin gerade gar nicht – Protokolle unter Dashboard → Protokolle (FFmpeg.Transcode).")
    jobs.log(None, "Jellyfin: Umrechnen klappt weder über die Grafikeinheit noch über den Prozessor")


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
        anmeldeseite_abgleichen(token)
    except JellyfinFehler as exc:
        jobs.log(None, f"Jellyfin-Benutzer {name}: {exc}")


def anmeldeseite_abgleichen(token: str) -> None:
    """Benutzer als Kacheln auf der Jellyfin-Anmeldeseite zeigen (wie Profile bei Netflix), admin bleibt versteckt.

    Jellyfin legt neue Benutzer versteckt an – dann muss man den Namen jedes Mal eintippen. So wählt man am Fernseher
    nur noch sein Profil aus (und gibt höchstens das Passwort ein)."""
    for u in _req("GET", "/Users", token=token) or []:
        policy = u.get("Policy") or {}
        versteckt = (u.get("Name") or "").lower() == ADMIN
        if policy and bool(policy.get("IsHidden")) != versteckt:
            policy["IsHidden"] = versteckt
            _req("POST", f"/Users/{u['Id']}/Policy", policy, token=token)


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
        token = _token()
        hardware_einrichten(token)
        hardware_pruefen(token)
        anmeldeseite_abgleichen(token)
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
    s.update(admin=z.get("admin", ADMIN) if z.get("passwort") else "", passwort=z.get("passwort", ""), port=port(),
             hardware=z.get("hardware", ""), hardware_hinweis=z.get("hardware_hinweis", ""))
    return s
