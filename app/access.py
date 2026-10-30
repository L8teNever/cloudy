"""Fernzugriff über Cloudflare Tunnel: Login-Prüfung mit Cloudflare Access.

Jede Anfrage, die über den Tunnel kommt, muss das von Cloudflare Access ausgestellte Token (JWT im Header
"Cf-Access-Jwt-Assertion" bzw. Cookie "CF_Authorization") mitbringen. cloudy prüft die Signatur selbst gegen die
öffentlichen Schlüssel des Teams – fehlt die Access-App bei Cloudflare oder ist sie falsch eingestellt, bleibt das
Dashboard trotzdem zu.
"""

import base64
import json
import re
import threading
import time
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from . import db

SLOT = "tunnel"  # Token liegt wie bei WARP in data/warp/tunnel/token (Helfer aus warp.py)
TEAM_KEY = "tunnel_team"
AUD_KEY = "tunnel_aud"
DEFAULT_TEAM = "l8tenever.cloudflareaccess.com"
CERTS_TTL = 3600  # Schlüssel eine Stunde zwischenspeichern
REFRESH_MIN = 60  # unbekannter Schlüssel: höchstens einmal pro Minute neu laden
LEEWAY = 60  # Sekunden Toleranz bei Uhrzeiten

TEAM_RE = re.compile(r"^[a-z0-9-]+\.cloudflareaccess\.com$")
AUD_RE = re.compile(r"^[0-9a-f]{32,128}$")

_keys: dict[str, tuple[float, dict]] = {}  # team -> (geladen um, kid -> Schlüssel)
_lock = threading.Lock()


def normalize_team(text: str) -> str:
    """'https://l8tenever.cloudflareaccess.com/' -> 'l8tenever.cloudflareaccess.com'; ValueError bei Unsinn."""
    team = text.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
    if "." not in team:
        team += ".cloudflareaccess.com"  # nur der Teamname eingegeben
    if not TEAM_RE.match(team):
        raise ValueError("Team-Domain muss so aussehen: <name>.cloudflareaccess.com")
    return team


def normalize_aud(text: str) -> str:
    aud = text.strip().lower()
    if not AUD_RE.match(aud):
        raise ValueError("AUD-Tag ungültig (lange Hex-Zeichenkette aus der Access-App)")
    return aud


def config() -> tuple[str, str]:
    """(Team-Domain, AUD) aus den Einstellungen; leer, solange nichts eingetragen ist."""
    return db.get_setting(TEAM_KEY), db.get_setting(AUD_KEY)


def _b64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def fetch_keys(team: str) -> dict:
    """Öffentliche Schlüssel des Teams (JWKS) von Cloudflare laden: kid -> RSA-Schlüssel."""
    with urllib.request.urlopen(f"https://{team}/cdn-cgi/access/certs", timeout=5) as resp:
        data = json.loads(resp.read())
    keys = {}
    for k in data.get("keys", []):
        if k.get("kty") == "RSA" and k.get("kid") and k.get("n") and k.get("e"):
            numbers = rsa.RSAPublicNumbers(int.from_bytes(_b64(k["e"]), "big"), int.from_bytes(_b64(k["n"]), "big"))
            keys[k["kid"]] = numbers.public_key()
    return keys


def _key(team: str, kid: str, now: float):
    with _lock:
        loaded, keys = _keys.get(team, (0.0, {}))
        stale = now - loaded > CERTS_TTL
        unknown = kid not in keys and now - loaded > REFRESH_MIN
        if stale or unknown:
            try:
                keys = fetch_keys(team)
                _keys[team] = (now, keys)
            except (OSError, ValueError) as exc:
                print(f"cloudy: Access-Schlüssel von {team} nicht ladbar: {exc}", flush=True)
        return keys.get(kid)


def verify(token: str, team: str, aud: str, now: float | None = None) -> str | None:
    """Gibt die angemeldete E-Mail zurück, wenn das Token gültig ist, sonst None."""
    now = now if now is not None else time.time()
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
    audiences = claims.get("aud")
    audiences = [audiences] if isinstance(audiences, str) else (audiences or [])
    if aud not in audiences or claims.get("iss") != f"https://{team}":
        return None
    try:
        if float(claims.get("exp", 0)) < now - LEEWAY or float(claims.get("nbf", 0)) > now + LEEWAY:
            return None
    except (TypeError, ValueError):
        return None
    return str(claims.get("email") or claims.get("common_name") or "angemeldet")
