"""Netzlaufwerk (Samba) für PCs, Handys und Android-TVs und DLNA für Fernseher.

Regeln pro Freigabe:
  - "standard" gilt für alle Geräte ohne eigene Regel: offen (ohne Passwort), passwort oder versteckt.
  - "geraete" überschreibt das pro Gerät (Schlüssel = MAC aus cloudy, sonst "ip:1.2.3.4").

Umsetzung: Samba lädt pro Verbindung eine eigene Konfiguration für die IP des Geräts
(config file = hosts/%I.conf). Darin fehlen versteckte Freigaben ganz, offene haben "guest ok" (Gast oder jeder angemeldete Benutzer),
Passwort-Freigaben "valid users". Geräte ohne eigene Datei bekommen die Standard-Konfiguration.
Die IPs kommen aus cloudy (Geräteliste) und werden regelmäßig abgeglichen.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config, pool
from .config import data_dir

REGELN = ("offen", "passwort", "versteckt")
REGEL_TEXT = {"offen": "Ohne Passwort", "passwort": "Mit Passwort", "versteckt": "Unsichtbar"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")
IPV4_RE = re.compile(r"(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}")
USER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
RESERVED = {"global", "homes", "printers", "print$", "ipc$"}
FORCE_USER = "speicher"
_lock = threading.RLock()


class FreigabeError(RuntimeError):
    pass


@dataclass
class Freigabe:
    name: str
    ordner: str = ""  # relativ zum Pool, "" = ganzer Pool
    nur_lesen: bool = False
    dlna: bool = False
    standard: str = "offen"
    geraete: dict[str, str] = field(default_factory=dict)
    benutzer: list[str] = field(default_factory=list)  # leer = alle Benutzer


@dataclass
class Einstellungen:
    an: bool = True
    netzwerkname: str = "SPEICHER"
    arbeitsgruppe: str = "WORKGROUP"
    dlna_an: bool = True
    dlna_name: str = "cloudy Speicher"
    freigaben: list[Freigabe] = field(default_factory=list)
    benutzer: list[str] = field(default_factory=list)
    # Windows-Anmeldenamen pro Benutzer: Windows meldet sich damit von selbst an (Microsoft-Konto = E-Mail).
    windows: dict[str, list[str]] = field(default_factory=dict)
    # Eigene Geräte, falls cloudy ein Gerät nicht kennt: {"ip:1.2.3.4": "Name"}
    eigene_geraete: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Einstellungen":
        try:
            raw = json.loads((data_dir() / "freigaben.json").read_text())
        except (OSError, ValueError):
            return cls()
        shares = [Freigabe(**{k: v for k, v in f.items() if k in Freigabe.__dataclass_fields__}) for f in raw.pop("freigaben", [])]
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(freigaben=shares, **known)

    def save(self) -> None:
        config.write_private(data_dir() / "freigaben.json", json.dumps(asdict(self), indent=2, ensure_ascii=False))

    def get(self, name: str) -> Freigabe:
        for f in self.freigaben:
            if f.name.lower() == name.lower():
                return f
        raise FreigabeError(f"Freigabe „{name}“ gibt es nicht")


# ---------- Prüfen ----------

def check_name(name: str) -> str:
    name = " ".join(name.split())
    if not NAME_RE.match(name) or name.lower() in RESERVED:
        raise FreigabeError("Name: Buchstaben, Zahlen, Leerzeichen, - _ . (max. 40 Zeichen)")
    return name


def check_ordner(ordner: str) -> str:
    ordner = ordner.strip().strip("/")
    parts = [p for p in ordner.split("/") if p]
    if any(p in (".", "..") for p in parts) or any(c in ordner for c in "\\\n\r\0%"):
        raise FreigabeError("Ungültiger Ordner")
    return "/".join(parts)


def check_user(name: str) -> str:
    name = name.strip().lower()
    if not USER_RE.match(name) or name in ("root", FORCE_USER, "nobody"):
        raise FreigabeError("Benutzername: Kleinbuchstaben, Zahlen, - und _ (max. 32), mit Buchstabe vorne")
    return name


WINDOWS_RE = re.compile(r"^(?! )[^\t\r\n\0\"'=#;,\\/\[\]:|<>+*?%]{1,104}(?<! )$")


def check_windows_name(name: str) -> str:
    """'DESKTOP-ABC\\Simon' -> 'simon', 'MicrosoftAccount\\a@b.de' -> 'a@b.de'. Windows schickt den Namen ohne Rechner."""
    name = " ".join(name.strip().strip('"').split("\\")[-1].split()).lower()
    if not WINDOWS_RE.match(name):
        raise FreigabeError("Windows-Name: dein Windows-Benutzername oder die E-Mail deines Microsoft-Kontos")
    return name


def parse_windows_names(text: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[\n,;]+", text or ""):
        if part.strip():
            n = check_windows_name(part)
            if n not in out:
                out.append(n)
    return out


def name_aus_windows(windows_name: str) -> str:
    """Vorschlag für den Benutzernamen: 'Simon.K@outlook.de' -> 'simon-k'."""
    base = re.sub(r"[^a-z0-9_-]+", "-", windows_name.split("@")[0].lower()).strip("-_")
    base = re.sub(r"^[^a-z]+", "", base)[:32].strip("-_")
    return base or "benutzer"


def benutzer_fuer(name: str, e: "Einstellungen | None" = None) -> str:
    """Anmeldename (Benutzer oder Windows-Name) -> Benutzer, sonst ''."""
    e = e or Einstellungen.load()
    name = " ".join(name.strip().split("\\")[-1].split()).lower()
    if name in e.benutzer:
        return name
    for user, names in e.windows.items():
        if name in names and user in e.benutzer:
            return user
    return ""


def check_password(pw: str) -> str:
    if len(pw) < 6:
        raise FreigabeError("Passwort: mindestens 6 Zeichen")
    if any(c in pw for c in "\r\n\0"):
        raise FreigabeError("Passwort enthält ungültige Zeichen")
    return pw


# ---------- Geräte aus cloudy ----------

@dataclass
class Geraet:
    key: str
    name: str
    ip: str
    mac: str = ""
    online: bool = False
    eigen: bool = False


_cache: dict = {"t": 0.0, "geraete": [], "fehler": ""}


def cloudy_url() -> str:
    return os.environ.get("CLOUDY_URL") or f"http://127.0.0.1:{os.environ.get('CLOUDY_PORT', '80')}"


def geraete(max_age: float = 30) -> tuple[list[Geraet], str]:
    """Geräteliste aus cloudy plus eigene Einträge. (Liste, Fehlertext)."""
    with _lock:
        if time.time() - _cache["t"] < max_age:
            return _merge(_cache["geraete"]), _cache["fehler"]
        fehler = ""
        found: list[Geraet] = []
        req = urllib.request.Request(cloudy_url().rstrip("/") + "/api/devices")
        pw = os.environ.get("CLOUDY_PASSWORD", "")
        if pw:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"cloudy:{pw}".encode()).decode())
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            found = parse_cloudy(data)
        except Exception as exc:  # noqa: BLE001 – cloudy darf fehlen, dann nur eigene Geräte
            fehler = f"cloudy nicht erreichbar ({cloudy_url()}): {exc}"
            found = _cache["geraete"]  # letzter Stand bleibt gültig
        _cache.update(t=time.time(), geraete=found, fehler=fehler)
        return _merge(found), fehler


def parse_cloudy(data: list) -> list[Geraet]:
    out: dict[str, Geraet] = {}
    for d in data if isinstance(data, list) else []:
        ip = (d.get("ip") or "").strip()
        mac = (d.get("mac") or "").strip().lower()
        if not IPV4_RE.fullmatch(ip):  # nur IPv4 (Samba sieht im Heimnetz IPv4); schützt auch den Dateinamen
            continue
        key = mac or f"ip:{ip}"
        name = d.get("hostname") or d.get("lease_name") or ip
        if key not in out:
            out[key] = Geraet(key=key, name=str(name)[:60], ip=ip, mac=mac, online=bool(d.get("online")))
    return sorted(out.values(), key=lambda g: (not g.online, g.name.lower()))


def _merge(found: list[Geraet]) -> list[Geraet]:
    eigene = Einstellungen.load().eigene_geraete
    ips = {g.ip for g in found}
    extra = [Geraet(key=k, name=n, ip=k[3:], eigen=True) for k, n in eigene.items()
             if k.startswith("ip:") and IPV4_RE.fullmatch(k[3:]) and k[3:] not in ips]
    return list(found) + extra


# ---------- Samba-Konfiguration ----------

def samba_dir() -> Path:
    path = Path(os.environ.get("SAMBA_CONF_DIR", "/etc/samba"))
    (path / "hosts").mkdir(parents=True, exist_ok=True)
    return path


def _esc(value: str) -> str:
    return value.replace("\n", " ").replace("\r", " ")


def global_section(e: Einstellungen, main: bool) -> str:
    state = data_dir() / "samba"
    lines = [
        "[global]",
        f"  netbios name = {_esc(e.netzwerkname)[:15]}",
        f"  workgroup = {_esc(e.arbeitsgruppe)[:15]}",
        "  server string = cloudy Speicher",
        "  server role = standalone server",
        "  security = user",
        "  map to guest = Bad User",
        f"  guest account = {FORCE_USER}",
        f"  passdb backend = tdbsam:{state}/passdb.tdb",
        f"  private dir = {state}",
        "  server min protocol = SMB2_10",
        # NetBIOS-Namensdienst (nmbd) bleibt an: Windows löst „\\SPEICHER“ sonst nicht auf,
        # weil der Name nicht der Hostname des Servers ist und wsdd nur ankündigt, nicht auflöst.
        "  smb ports = 445",
        "  dns proxy = no",
        "  load printers = no",
        "  printing = bsd",
        "  printcap name = /dev/null",
        "  disable spoolss = yes",
        "  log level = 1",
        "  logging = file",
        "  max log size = 1000",
        "  log file = /var/log/samba/%m.log",
        "  unix extensions = no",
        "  wide links = no",
        "  follow symlinks = yes",
        "  vfs objects = catia fruit streams_xattr",
        "  fruit:metadata = stream",
        "  fruit:model = MacSamba",
        "  fruit:nfs_aces = no",
        "  ea support = yes",
        "  idmap config * : backend = tdb",
        "  idmap config * : range = 3000-7999",
        "  usershare path =",
        # Windows-Anmeldenamen (z. B. Microsoft-Konto) -> Benutzer, damit Windows sich von selbst anmeldet.
        f"  username map = {samba_dir()}/benutzer.map",
    ]
    if main:
        # Pro Gerät eine eigene Konfiguration (fehlt die Datei, gilt diese hier).
        lines.append(f"  config file = {samba_dir()}/hosts/%I.conf")
    return "\n".join(lines) + "\n"


def share_section(f: Freigabe, regel: str, alle_benutzer: list[str]) -> str:
    if regel == "versteckt":
        return ""
    path = share_path(f)
    lines = [
        f"[{f.name}]",
        f"  path = {path}",
        "  browseable = yes",
        f"  read only = {'yes' if f.nur_lesen else 'no'}",
        f"  force user = {FORCE_USER}",
        f"  force group = {FORCE_USER}",
        "  create mask = 0664",
        "  directory mask = 0775",
    ]
    if regel == "offen":
        # Kein "guest only": Windows 11 lässt Gast-Zugriff oft nicht zu, dann meldet man sich
        # mit einem angelegten Benutzer an und kommt damit auch in die offenen Ordner.
        lines += ["  guest ok = yes"]
    else:
        users = [u for u in (f.benutzer or alle_benutzer) if u in alle_benutzer]
        lines += ["  guest ok = no", f"  valid users = {' '.join(users) if users else 'nobody-darf-rein'}"]
    return "\n".join(lines) + "\n"


def render(e: Einstellungen, key: str | None) -> str:
    """Komplette smb.conf – für ein Gerät (key) oder die Standard-Konfiguration (key=None)."""
    parts = [global_section(e, main=key is None)]
    for f in e.freigaben:
        regel = regel_fuer(f, key)
        sec = share_section(f, regel, e.benutzer)
        if sec:
            parts.append(sec)
    return "\n".join(parts)


def username_map(e: Einstellungen) -> str:
    """Samba-Zuordnung: 'simon = "simon@outlook.de" "simon-pc"'. Leer, wenn es keine Windows-Namen gibt."""
    lines = []
    for user in e.benutzer:
        names = []
        for n in e.windows.get(user, []):
            if n != user:
                names.append(n)
            if "@" in n:  # Microsoft-Konto: Windows schickt oft „MicrosoftAccount\<E-Mail>“ (mit Samba geprüft)
                names.append(f"MicrosoftAccount\\{n}")
        if names:
            lines.append(f"{user} = " + " ".join(f'"{n}"' for n in names))
    return "\n".join(lines) + ("\n" if lines else "")


def write_configs(e: Einstellungen | None = None) -> bool:
    """smb.conf + hosts/<ip>.conf schreiben. True, wenn sich etwas geändert hat."""
    e = e or Einstellungen.load()
    d = samba_dir()
    (data_dir() / "samba").mkdir(mode=0o700, exist_ok=True)  # Passwörter (passdb/secrets) bleiben im Daten-Ordner
    wanted: dict[Path, str] = {d / "smb.conf": render(e, None), d / "benutzer.map": username_map(e)}
    devices, _ = geraete()
    # Nur für Geräte mit eigener Regel eine Extra-Datei (sonst gilt ohnehin der Standard).
    keys_with_rules = {k for f in e.freigaben for k in f.geraete}
    for g in devices:
        if g.key in keys_with_rules and g.ip:
            wanted[d / "hosts" / f"{g.ip}.conf"] = render(e, g.key)
    changed = False
    for path, text in wanted.items():
        try:
            if path.read_text() == text:
                continue
        except OSError:
            pass
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, path)
        changed = True
    for old in (d / "hosts").glob("*.conf"):
        if old not in wanted:
            old.unlink(missing_ok=True)
            changed = True
    return changed


def ensure_share_dirs(e: Einstellungen) -> None:
    root = pool.paths()["pool"]
    if str(root) not in pool.mounts():
        return  # ohne Pool nichts anlegen (sonst landet es auf der Systemplatte)
    for f in e.freigaben:
        if f.ordner:
            target = root / f.ordner
            target.mkdir(parents=True, exist_ok=True)
            shutil.chown(target, FORCE_USER, FORCE_USER)


# ---------- Benutzer ----------

def _run(cmd: list[str], stdin: str = "") -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise FreigabeError(f"{cmd[0]} fehlt im Container") from exc


def ensure_unix_users(e: Einstellungen) -> None:
    """Samba braucht zu jedem Benutzer einen Linux-Benutzer im Container (wird bei jedem Start neu angelegt)."""
    uid = os.environ.get("SPEICHER_UID", "1000")
    gid = os.environ.get("SPEICHER_GID", "1000")
    if _run(["getent", "group", FORCE_USER]).returncode != 0:
        _run(["groupadd", "-o", "-g", gid, FORCE_USER])
    if _run(["id", FORCE_USER]).returncode != 0:
        _run(["useradd", "-o", "-M", "-u", uid, "-g", FORCE_USER, "-s", "/usr/sbin/nologin", "-d", "/nonexistent", FORCE_USER])
    for name in e.benutzer:
        if _run(["id", name]).returncode != 0:
            _run(["useradd", "-M", "-N", "-g", FORCE_USER, "-s", "/usr/sbin/nologin", "-d", "/nonexistent", name])


def ensure_force_user_passdb() -> None:
    """Der Datei-Besitzer (speicher) muss in Sambas Benutzerliste stehen, sonst dürfen Gäste nichts schreiben
    (das Gast-Token passt sonst nicht zum Besitzer der Ordner). Anmelden kann man sich damit nicht: gesperrt."""
    conf = str(samba_dir() / "smb.conf")
    res = _run(["pdbedit", "-s", conf, "-L"])
    if any(line.split(":")[0] == FORCE_USER for line in res.stdout.splitlines()):
        return
    pw = secrets.token_urlsafe(24)
    _run(["smbpasswd", "-c", conf, "-s", "-a", FORCE_USER], f"{pw}\n{pw}\n")
    _run(["smbpasswd", "-c", conf, "-d", FORCE_USER])


def set_user_password(name: str, password: str, neu: bool) -> None:
    conf = str(samba_dir() / "smb.conf")
    res = _run(["smbpasswd", "-c", conf, "-s", *(["-a"] if neu else []), name], f"{password}\n{password}\n")
    if res.returncode != 0:
        raise FreigabeError(f"Passwort setzen fehlgeschlagen: {(res.stderr or res.stdout).strip()[-200:]}")
    _run(["smbpasswd", "-c", conf, "-e", name])
    set_web_password(name, password)


# ---------- Anmeldung im Browser (Dateien-Seite) ----------
# Samba speichert nur NT-Hashes, die man nicht sinnvoll prüfen kann. Deshalb zusätzlich ein scrypt-Hash pro Benutzer.

def _web_file() -> Path:
    return data_dir() / "benutzer-web.json"


def _web_hashes() -> dict[str, str]:
    try:
        return json.loads(_web_file().read_text())
    except (OSError, ValueError):
        return {}


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def set_web_password(name: str, password: str | None) -> None:
    """Passwort-Hash speichern (None = Benutzer entfernen)."""
    with _lock:
        hashes = _web_hashes()
        if password is None:
            hashes.pop(name, None)
        else:
            salt = secrets.token_bytes(16)
            hashes[name] = base64.b64encode(salt).decode() + "$" + base64.b64encode(_scrypt(password, salt)).decode()
        config.write_private(_web_file(), json.dumps(hashes, indent=2))


def web_login_moeglich(name: str) -> bool:
    return name in _web_hashes()


def check_login(name: str, password: str) -> bool:
    """Anmeldung im Browser – mit Benutzer oder Windows-Namen."""
    name = benutzer_fuer(name)
    if not name:
        _scrypt(password, b"0" * 16)  # gleiche Laufzeit wie ein echter Versuch
        return False
    try:
        salt_b64, hash_b64 = _web_hashes()[name].split("$")
    except (KeyError, ValueError):
        return False
    return hmac.compare_digest(_scrypt(password, base64.b64decode(salt_b64)), base64.b64decode(hash_b64))


def key_fuer_ip(ip: str) -> str | None:
    """Gerät (MAC aus cloudy oder ip:…) zu einer IP – wie Samba es über hosts/<ip>.conf macht."""
    devices, _ = geraete()
    for g in devices:
        if g.ip == ip:
            return g.key
    return None


def regel_fuer(f: Freigabe, key: str | None) -> str:
    return f.geraete.get(key, f.standard) if key else f.standard


def darf(f: Freigabe, regel: str, benutzer: str, alle_benutzer: list[str]) -> bool:
    """Darf jemand (benutzer = "" für nicht angemeldet) in die Freigabe – gleiche Logik wie share_section."""
    if regel == "versteckt":
        return False
    if regel == "offen":
        return True
    erlaubt = [u for u in (f.benutzer or alle_benutzer) if u in alle_benutzer]
    return bool(benutzer) and benutzer in erlaubt


def share_path(f: Freigabe) -> Path:
    root = pool.paths()["pool"]
    return root / f.ordner if f.ordner else root


def _check_windows_frei(e: Einstellungen, user: str, names: list[str]) -> None:
    for n in names:
        other = benutzer_fuer(n, e)
        if other and other != user:
            raise FreigabeError(f"„{n}“ gehört schon zu {other}")


def add_user(name: str, password: str, windows: str = "") -> str:
    names = parse_windows_names(windows)
    if not name.strip() and names:
        name = name_aus_windows(names[0])
    name, password = check_user(name), check_password(password)
    with _lock:
        e = Einstellungen.load()
        if name in e.benutzer or benutzer_fuer(name, e):
            raise FreigabeError(f"Benutzer {name} gibt es schon")
        _check_windows_frei(e, name, names)
        e.benutzer.append(name)
        if names:
            e.windows[name] = names
        ensure_unix_users(e)
        write_configs(e)
        set_user_password(name, password, neu=True)
        e.save()
        write_configs(e)
    return name


def set_windows_names(name: str, windows: str) -> None:
    names = parse_windows_names(windows)
    with _lock:
        e = Einstellungen.load()
        if name not in e.benutzer:
            raise FreigabeError("Unbekannter Benutzer")
        _check_windows_frei(e, name, names)
        if names:
            e.windows[name] = names
        else:
            e.windows.pop(name, None)
        e.save()
        write_configs(e)


def change_password(name: str, password: str) -> None:
    password = check_password(password)
    e = Einstellungen.load()
    if name not in e.benutzer:
        raise FreigabeError("Unbekannter Benutzer")
    ensure_unix_users(e)
    set_user_password(name, password, neu=False)


def delete_user(name: str) -> None:
    with _lock:
        e = Einstellungen.load()
        if name not in e.benutzer:
            raise FreigabeError("Unbekannter Benutzer")
        e.benutzer.remove(name)
        e.windows.pop(name, None)
        for f in e.freigaben:
            if name in f.benutzer:
                f.benutzer.remove(name)
        e.save()
        write_configs(e)  # Windows-Zuordnung sofort entfernen
        _run(["smbpasswd", "-c", str(samba_dir() / "smb.conf"), "-x", name])
        _run(["userdel", name])
        set_web_password(name, None)


# ---------- DLNA (Fernseher) ----------

def minidlna_conf(e: Einstellungen) -> str:
    root = pool.paths()["pool"]
    dirs = [root / f.ordner if f.ordner else root for f in e.freigaben if f.dlna]
    cache = data_dir() / "minidlna"
    lines = [
        f"friendly_name={_esc(e.dlna_name)[:48]}",
        "port=8200",
        f"db_dir={cache}",
        f"log_dir={cache}",
        "inotify=yes",
        "notify_interval=60",
        "strict_dlna=no",
        "album_art_names=Cover.jpg/cover.jpg/Folder.jpg/folder.jpg/AlbumArt.jpg",
        "root_container=B",
    ]
    lines += [f"media_dir={d}" for d in dirs]
    return "\n".join(lines) + "\n"


# ---------- Dienste (smbd, nmbd, wsdd, minidlna) ----------

class Dienste:
    """Startet und überwacht die Netzwerk-Dienste im Container."""

    def __init__(self) -> None:
        self.procs: dict[str, subprocess.Popen] = {}
        self.fehler: dict[str, str] = {}
        self._dlna_conf = ""

    def _start(self, name: str, cmd: list[str]) -> None:
        log = open(f"/var/log/speicher-{name}.log", "a")  # noqa: SIM115 – bleibt offen, solange der Dienst läuft
        try:
            self.procs[name] = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            self.fehler.pop(name, None)
        except FileNotFoundError:
            self.fehler[name] = f"{cmd[0]} fehlt"

    def _stop(self, name: str) -> None:
        proc = self.procs.pop(name, None)
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()

    def laeuft(self, name: str) -> bool:
        proc = self.procs.get(name)
        return bool(proc and proc.poll() is None)

    def abgleichen(self) -> None:
        """Konfiguration schreiben und Dienste an den Soll-Zustand anpassen (idempotent)."""
        with _lock:
            e = Einstellungen.load()
            pool_da = str(pool.paths()["pool"]) in pool.mounts()
            ensure_unix_users(e)
            if pool_da:
                ensure_share_dirs(e)
            changed = write_configs(e)
            ensure_force_user_passdb()
            Path("/var/log/samba").mkdir(parents=True, exist_ok=True)
            (data_dir() / "samba").mkdir(exist_ok=True)

            smb_soll = e.an and bool(e.freigaben) and pool_da
            if smb_soll and not self.laeuft("smbd"):
                self._start("smbd", ["smbd", "--foreground", "--no-process-group", "-s", str(samba_dir() / "smb.conf")])
            elif not smb_soll:
                self._stop("smbd")
            elif changed:
                _run(["smbcontrol", "-s", str(samba_dir() / "smb.conf"), "smbd", "reload-config"])

            if smb_soll and not self.laeuft("wsdd"):
                self._start("wsdd", ["wsdd", "--shortlog", "-n", e.netzwerkname[:15], "-w", e.arbeitsgruppe[:15]])
            elif not smb_soll:
                self._stop("wsdd")

            # nmbd beantwortet „Wer ist SPEICHER?“ im Heimnetz (NetBIOS, UDP 137/138).
            if smb_soll and not self.laeuft("nmbd"):
                self._start("nmbd", ["nmbd", "--foreground", "--no-process-group", "-s", str(samba_dir() / "smb.conf")])
            elif not smb_soll:
                self._stop("nmbd")

            dlna_soll = e.dlna_an and pool_da and any(f.dlna for f in e.freigaben)
            conf = minidlna_conf(e)
            if dlna_soll:
                (data_dir() / "minidlna").mkdir(exist_ok=True)
                path = data_dir() / "minidlna" / "minidlna.conf"
                if conf != self._dlna_conf or not self.laeuft("minidlna"):
                    self._stop("minidlna")
                    path.write_text(conf)
                    self._start("minidlna", ["minidlnad", "-S", "-f", str(path), "-P", "/run/minidlna.pid", "-r"])
                    self._dlna_conf = conf
            else:
                self._stop("minidlna")
                self._dlna_conf = ""

    def neustart_wsdd(self) -> None:
        """Nach Namensänderung: Ankündigung (wsdd) und Namensdienst (nmbd) neu starten."""
        self._stop("wsdd")
        self._stop("nmbd")
        self.abgleichen()

    def stop_all(self) -> None:
        for name in list(self.procs):
            self._stop(name)


dienste = Dienste()
