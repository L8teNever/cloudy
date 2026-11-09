"""Von außen erreichbar: ausgewählte cloudy-Seiten über Cloudflare Tunnel, nur mit Zero-Trust-Anmeldung.

Derselbe Name (z. B. karte.kulbarts.com) wird je nach Ort anders aufgelöst:
- Im Heimnetz beantwortet cloudy (dnsmasq) den Namen mit der IP des Pi -> direkt, ohne Anmeldung.
- Von außen zeigt der öffentliche DNS-Eintrag bei Cloudflare auf den Tunnel -> Cloudflare Access fragt nach der
  E-Mail (Einmal-Code), nur erlaubte Adressen kommen durch, die Anmeldung hält so lange wie eingestellt.

cloudy richtet alles selbst über die Cloudflare-API ein (Token aus „Eigene Domains“): Tunnel, Weiterleitung im
Tunnel, DNS-Einträge, Access-App mit Regel. Der Tunnel-Token landet in data/warp/tunnel/token, den Rest macht der
Container cloudy-tunnel. Zusätzlich prüft cloudy selbst bei jeder Tunnel-Anfrage das Access-Token (JWT) – ist bei
Cloudflare etwas falsch eingestellt, bleibt die Seite trotzdem zu.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from . import db, localdomain, warp
from .settings import Settings

KEY = "fernzugriff"  # JSON, siehe load()
SLOT = "tunnel"  # Token-Datei wie bei WARP: data/warp/tunnel/token
TUNNEL_IP = "172.29.88.10"  # Absender der Tunnel-Anfragen (Netz cloudy-tunnel in docker-compose.yml)
GATEWAY_IP = "172.29.88.1"  # so erreicht der Tunnel-Container cloudy im Host-Netz
TUNNEL_NAME = "cloudy"
APP_NAME = "cloudy – von außen"
DEFAULT_DAYS = 330
MAX_DAYS = 365
FALLBACK_HOURS = 730  # falls Cloudflare längere Anmeldungen ablehnt: ein Monat
LEEWAY = 60
CERTS_TTL = 3600

# Welche cloudy-Seite unter dem Namen erscheint
PAGES = {
    "karte": "3D-Karte",
    "dashboard": "Ganzes Dashboard (wie cloudy.lan)",
    "hub": "Hub (Startseite mit Links)",
}
_LOCAL = {  # Seite -> (lesen, speichern) der lokalen Domain-Liste
    "karte": (localdomain.karte_domains, localdomain.set_karte_domains),
    "dashboard": (localdomain.dashboard_domains, localdomain.set_dashboard_domains),
    "hub": (localdomain.hub_domains, localdomain.set_hub_domains),
}
_OTHER_LOCAL = (  # Listen, in denen ein Name nicht gleichzeitig stehen darf
    (localdomain.mein_domains, localdomain.set_mein_domains),
    (localdomain.block_domains, localdomain.set_block_domains),
    (localdomain.switch_domains, localdomain.set_switch_domains),
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NEEDED = ("Konto → Cloudflare Tunnel → Bearbeiten, Konto → Access: Apps und Richtlinien → Bearbeiten, "
          "Konto → Access: Organisationen, Identitätsanbieter und Gruppen → Lesen, Zone → DNS → Bearbeiten")

_lock = threading.Lock()
_step = threading.local()  # welcher Schritt gerade läuft (für verständliche Fehler)

STEP_RIGHTS = {
    "Tunnel": "Konto → Cloudflare Tunnel → Bearbeiten",
    "DNS": "Zone → DNS → Bearbeiten (und Zone → Zone → Lesen)",
    "Zero-Trust-Anmeldung": "Konto → Access: Apps und Richtlinien → Bearbeiten",
    "Zero-Trust-Team": "Konto → Access: Organisationen, Identitätsanbieter und Gruppen → Lesen",
    "Konto": "Zone → Zone → Lesen",
}


class FernError(Exception):
    """Fehler mit verständlichem Text für die Oberfläche."""


# --------------------------------------------------------------------------- Einstellungen

def load() -> dict:
    """{hosts: {name: seite}, emails: [...], days, replace: [...], account, tunnel_id, app_id, aud, team,
    records: {name: dns_id}, state: "ok"|"fehler"|"", text, hours}"""
    try:
        raw = json.loads(db.get_setting(KEY) or "{}")
    except ValueError:
        raw = {}
    cfg = raw if isinstance(raw, dict) else {}
    cfg["hosts"] = {h: p for h, p in (cfg.get("hosts") or {}).items() if p in PAGES}
    cfg["emails"] = [e for e in cfg.get("emails") or [] if isinstance(e, str)]
    cfg["records"] = dict(cfg.get("records") or {})
    cfg["replace"] = list(cfg.get("replace") or [])
    try:
        cfg["days"] = max(1, min(MAX_DAYS, int(cfg.get("days") or DEFAULT_DAYS)))
    except (TypeError, ValueError):
        cfg["days"] = DEFAULT_DAYS
    return cfg


def save(cfg: dict) -> None:
    db.set_setting(KEY, json.dumps(cfg, sort_keys=True))


def hosts() -> dict[str, str]:
    return load()["hosts"]


def parse_emails(text: str) -> list[str]:
    out = []
    for part in re.split(r"[\s,;]+", text or ""):
        e = part.strip().lower()
        if not e:
            continue
        if not EMAIL_RE.match(e):
            raise FernError(f"„{part}“ ist keine gültige E-Mail-Adresse.")
        if e not in out:
            out.append(e)
    return out


def set_config(entries: list[tuple[str, str]], emails: list[str], days, replace: list[str]) -> dict:
    """Namen + Seite, erlaubte E-Mails und Anmeldedauer speichern. Die Namen landen auch in den lokalen Domains,
    damit sie zu Hause direkt (ohne Anmeldung) gehen."""
    clean: dict[str, str] = {}
    for host, page in entries:
        if not host.strip():
            continue
        if page not in PAGES:
            raise FernError("Unbekannte Seite.")
        try:
            name = localdomain.normalize_domain(host)
        except ValueError as exc:
            raise FernError(str(exc)) from exc
        if name in clean and clean[name] != page:
            raise FernError(f"{name} steht doppelt mit verschiedenen Seiten.")
        clean[name] = page
    if clean and not emails:
        raise FernError("Trag mindestens eine E-Mail-Adresse ein, die reindarf.")
    try:
        days = int(days)
    except (TypeError, ValueError) as exc:
        raise FernError("Die Anmeldedauer muss eine Zahl (Tage) sein.") from exc
    if not 1 <= days <= MAX_DAYS:
        raise FernError(f"Die Anmeldedauer muss zwischen 1 und {MAX_DAYS} Tagen liegen.")
    with _lock:
        cfg = load()
        cfg.update(hosts=clean, emails=emails, days=days, replace=sorted(set(replace) & set(clean)))
        save(cfg)
        _sync_local(clean)
    return cfg


def sync_local() -> None:
    _sync_local(hosts())


def _sync_local(clean: dict[str, str]) -> None:
    """Jeder Name steht in genau der lokalen Liste seiner Seite (Karte, Dashboard oder Hub)."""
    for page, (get, put) in _LOCAL.items():
        cur = get()
        want = [h for h in cur if clean.get(h, page) == page]
        want += [h for h, p in clean.items() if p == page and h not in want]
        if want != cur:
            put(want)
    for get, put in _OTHER_LOCAL:
        cur = get()
        want = [h for h in cur if h not in clean]
        if want != cur:
            put(want)


# --------------------------------------------------------------------------- Cloudflare einrichten

def _api(cf: localdomain.Cloudflare, method: str, path: str, body: dict | None = None):
    try:
        return cf._call(method, path, body)  # noqa: SLF001 – gleiche Verbindung wie für die Zertifikate
    except localdomain.AcmeError as exc:
        text = str(exc)
        step = getattr(_step, "name", "")
        if any(w in text.lower() for w in ("authentication", "not authorized", "unauthorized", "forbidden", "permission", "10000")):
            need = STEP_RIGHTS.get(step, NEEDED)
            raise FernError(f"Schritt „{step or 'Cloudflare'}“: dem Cloudflare-Token fehlt das Recht {need}. "
                            "Unter dash.cloudflare.com → Mein Profil → API-Tokens den Token bearbeiten, Recht ergänzen, "
                            "dann hier noch mal speichern.") from exc
        raise FernError(f"Schritt „{step}“: {text}" if step else text) from exc


def _port() -> int:
    try:
        return int(os.environ.get("DASHBOARD_PORT", "8080") or 8080)
    except ValueError:
        return 8080


def apply(settings: Settings, cf: localdomain.Cloudflare | None = None) -> dict:
    """Cloudflare an die Einstellungen anpassen. Ergebnis/Fehler steht danach in load()["state"/"text"]."""
    with _lock:
        cfg = load()
        _step.name = ""
        try:
            text = _apply(settings, cfg, cf)
            cfg.update(state="ok" if cfg["hosts"] else "", text=text, at=int(time.time()))
        except FernError as exc:
            cfg.update(state="fehler", text=str(exc), at=int(time.time()))
        save(cfg)
        return cfg


def _apply(settings: Settings, cfg: dict, cf) -> str:
    token = db.get_setting(localdomain.TOKEN_KEY)
    if not token:
        if not cfg["hosts"]:
            return ""
        raise FernError("Kein Cloudflare-API-Token gespeichert (oben bei „Eigene Domains im Heimnetz“).")
    cf = cf or localdomain.Cloudflare(token)
    names = sorted(cfg["hosts"])
    if not names and not cfg.get("account"):
        return ""  # nie eingerichtet, nichts aufzuräumen

    # Konto über die Zone des ersten Namens (bzw. eines alten DNS-Eintrags) finden
    _step.name = "Konto"
    if not cfg.get("account"):
        probe = names[0]
        try:
            zone = cf.zone_id(probe)
        except localdomain.AcmeError as exc:
            raise FernError(f"{probe}: {exc}") from exc
        cfg["account"] = _api(cf, "GET", f"/zones/{zone}")["account"]["id"]
    acc = cfg["account"]

    # 1) Tunnel (einmal anlegen, Token für den Container ablegen)
    _step.name = "Tunnel"
    tid = cfg.get("tunnel_id") or ""
    if tid:
        try:
            if _api(cf, "GET", f"/accounts/{acc}/cfd_tunnel/{tid}").get("deleted_at"):
                tid = ""
        except FernError:  # bei Cloudflare gelöscht
            tid = ""
    if not tid and names:
        found = _api(cf, "GET", f"/accounts/{acc}/cfd_tunnel?name={TUNNEL_NAME}&is_deleted=false")
        tid = found[0]["id"] if found else _api(cf, "POST", f"/accounts/{acc}/cfd_tunnel",
                                                 {"name": TUNNEL_NAME, "config_src": "cloudflare"})["id"]
    cfg["tunnel_id"] = tid
    if tid:
        tok = _api(cf, "GET", f"/accounts/{acc}/cfd_tunnel/{tid}/token")
        if names:
            warp.save_token(settings, SLOT, str(tok))
        # 2) Weiterleitung im Tunnel: jeder Name -> cloudy (unterscheidet die Seite am Namen)
        ingress = [{"hostname": h, "service": f"http://{GATEWAY_IP}:{_port()}"} for h in names]
        ingress.append({"service": "http_status:404"})
        _api(cf, "PUT", f"/accounts/{acc}/cfd_tunnel/{tid}/configurations", {"config": {"ingress": ingress}})

    # 3) DNS: Name -> <tunnel>.cfargotunnel.com; nicht mehr gewünschte eigene Einträge löschen
    _step.name = "DNS"
    target = f"{tid}.cfargotunnel.com"
    for host in list(cfg["records"]):
        if host in cfg["hosts"]:
            continue
        zone = cf.zone_id(host)
        for rec in _api(cf, "GET", f"/zones/{zone}/dns_records?name={host}"):
            if rec.get("type") == "CNAME" and rec.get("content", "").endswith(".cfargotunnel.com"):
                _api(cf, "DELETE", f"/zones/{zone}/dns_records/{rec['id']}")
        del cfg["records"][host]
    for host in names:
        zone = cf.zone_id(host)
        recs = _api(cf, "GET", f"/zones/{zone}/dns_records?name={host}")
        ours = [r for r in recs if r.get("type") == "CNAME" and r.get("content") == target]
        others = [r for r in recs if r not in ours and r.get("type") in ("A", "AAAA", "CNAME")]
        if others:
            if host not in cfg["replace"]:
                desc = ", ".join(f"{r['type']} → {r.get('content', '')}" for r in others)
                raise FernError(f"{host} hat bei Cloudflare schon einen DNS-Eintrag ({desc}). Haken bei „vorhandenen "
                                "Eintrag ersetzen“ setzen oder einen anderen Namen nehmen.")
            for r in others:
                _api(cf, "DELETE", f"/zones/{zone}/dns_records/{r['id']}")
        if ours:
            cfg["records"][host] = ours[0]["id"]
        else:
            cfg["records"][host] = _api(cf, "POST", f"/zones/{zone}/dns_records", {
                "type": "CNAME", "name": host, "content": target, "proxied": True, "ttl": 1,
                "comment": "cloudy: von außen über Cloudflare Tunnel",
            })["id"]
    cfg["replace"] = []

    # 4) Zero Trust: eine Access-App für alle Namen, Regel = erlaubte E-Mails, Anmeldung hält <days> Tage
    _step.name = "Zero-Trust-Anmeldung"
    if not names:
        if cfg.get("app_id"):
            try:
                _api(cf, "DELETE", f"/accounts/{acc}/access/apps/{cfg['app_id']}")
            except FernError:
                pass
        cfg.update(app_id="", aud="")
        if tid:
            warp.save_token(settings, SLOT, "")
        return "Aus – nichts ist von außen erreichbar."
    hours = cfg["days"] * 24
    app_body = {
        "name": APP_NAME, "type": "self_hosted", "domain": names[0],
        "destinations": [{"type": "public", "uri": h} for h in names],
        "session_duration": f"{hours}h", "app_launcher_visible": False,
    }
    note = ""
    app = _save_app(cf, acc, cfg, app_body)
    if app is None:  # Cloudflare lehnt die lange Dauer ab -> ein Monat
        hours = FALLBACK_HOURS
        app_body["session_duration"] = f"{hours}h"
        app = _save_app(cf, acc, cfg, app_body, fallback=False)
        note = f" Cloudflare erlaubt keine {cfg['days']} Tage – Anmeldung hält jetzt 30 Tage."
    cfg.update(app_id=app["id"], aud=app.get("aud", ""), hours=hours)
    policy = {
        "name": "Erlaubte E-Mails", "decision": "allow", "precedence": 1,
        "include": [{"email": {"email": e}} for e in cfg["emails"]],
        "session_duration": f"{hours}h",
    }
    existing = _api(cf, "GET", f"/accounts/{acc}/access/apps/{app['id']}/policies")
    if existing:
        _api(cf, "PUT", f"/accounts/{acc}/access/apps/{app['id']}/policies/{existing[0]['id']}", policy)
        for extra in existing[1:]:  # nur unsere eine Regel, damit niemand anders durchkommt
            _api(cf, "DELETE", f"/accounts/{acc}/access/apps/{app['id']}/policies/{extra['id']}")
    else:
        _api(cf, "POST", f"/accounts/{acc}/access/apps/{app['id']}/policies", policy)
    if not cfg.get("team"):
        _step.name = "Zero-Trust-Team"
        cfg["team"] = _api(cf, "GET", f"/accounts/{acc}/access/organizations").get("auth_domain", "")
    return f"Eingerichtet: {', '.join(names)}.{note}"


def _save_app(cf, acc: str, cfg: dict, body: dict, fallback: bool = True):
    try:
        if cfg.get("app_id"):
            try:
                return _api(cf, "PUT", f"/accounts/{acc}/access/apps/{cfg['app_id']}", body)
            except FernError as exc:
                if "not found" not in str(exc).lower() and "12130" not in str(exc):
                    raise
                cfg["app_id"] = ""
        return _api(cf, "POST", f"/accounts/{acc}/access/apps", body)
    except FernError as exc:
        if fallback and "session" in str(exc).lower():
            return None
        raise


def apply_in_background(settings: Settings) -> None:
    threading.Thread(target=apply, args=(settings,), daemon=True).start()


def tunnel_status(settings: Settings) -> str:
    try:
        with open(os.path.join(warp.slot_dir(settings, SLOT), "status"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


# --------------------------------------------------------------------------- Access-Token prüfen

_keys: dict[str, tuple[float, dict]] = {}
_keys_lock = threading.Lock()
_step = threading.local()  # welcher Schritt gerade läuft (für verständliche Fehler)

STEP_RIGHTS = {
    "Tunnel": "Konto → Cloudflare Tunnel → Bearbeiten",
    "DNS": "Zone → DNS → Bearbeiten (und Zone → Zone → Lesen)",
    "Zero-Trust-Anmeldung": "Konto → Access: Apps und Richtlinien → Bearbeiten",
    "Zero-Trust-Team": "Konto → Access: Organisationen, Identitätsanbieter und Gruppen → Lesen",
    "Konto": "Zone → Zone → Lesen",
}


def _b64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def fetch_keys(team: str) -> dict:
    with urllib.request.urlopen(f"https://{team}/cdn-cgi/access/certs", timeout=5) as resp:
        data = json.loads(resp.read())
    keys = {}
    for k in data.get("keys", []):
        if k.get("kty") == "RSA" and k.get("kid") and k.get("n") and k.get("e"):
            numbers = rsa.RSAPublicNumbers(int.from_bytes(_b64(k["e"]), "big"), int.from_bytes(_b64(k["n"]), "big"))
            keys[k["kid"]] = numbers.public_key()
    return keys


def _key(team: str, kid: str, now: float):
    with _keys_lock:
        loaded, keys = _keys.get(team, (0.0, {}))
        if now - loaded > CERTS_TTL or (kid not in keys and now - loaded > 60):
            try:
                keys = fetch_keys(team)
                _keys[team] = (now, keys)
            except (OSError, ValueError) as exc:
                print(f"cloudy: Access-Schlüssel von {team} nicht ladbar: {exc}", flush=True)
        return keys.get(kid)


def verify(token: str, team: str, aud: str, now: float | None = None) -> str | None:
    """E-Mail aus einem gültigen Access-Token (Signatur, AUD, Aussteller, Zeit), sonst None."""
    now = time.time() if now is None else now
    try:
        head_b64, body_b64, sig_b64 = token.strip().split(".")
        header = json.loads(_b64(head_b64))
        claims = json.loads(_b64(body_b64))
        signature = _b64(sig_b64)
    except (ValueError, TypeError):
        return None
    if header.get("alg") != "RS256" or not isinstance(claims, dict):
        return None
    key = _key(team, str(header.get("kid", "")), now)
    if key is None:
        return None
    try:
        key.verify(signature, f"{head_b64}.{body_b64}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return None
    auds = claims.get("aud")
    auds = [auds] if isinstance(auds, str) else (auds or [])
    if aud not in auds or claims.get("iss") != f"https://{team}":
        return None
    try:
        if float(claims.get("exp", 0)) < now - LEEWAY or float(claims.get("nbf", 0)) > now + LEEWAY:
            return None
    except (TypeError, ValueError):
        return None
    return str(claims.get("email") or "angemeldet")


def check_request(host: str, token: str) -> tuple[bool, str]:
    """Darf eine Tunnel-Anfrage für host durch? (ok, E-Mail oder Grund)"""
    cfg = load()
    if host not in cfg["hosts"]:
        return False, "Diese Adresse ist nicht für den Zugriff von außen freigegeben."
    if not (cfg.get("team") and cfg.get("aud")):
        return False, "Die Zero-Trust-Anmeldung ist noch nicht fertig eingerichtet."
    email = verify(token, cfg["team"], cfg["aud"]) if token else None
    if not email:
        return False, "Bitte über die Cloudflare-Anmeldung aufrufen (Seite neu laden)."
    if cfg["emails"] and email.lower() not in cfg["emails"]:
        return False, f"{email} ist nicht freigegeben."
    return True, email
