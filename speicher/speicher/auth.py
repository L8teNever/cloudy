"""Anmeldung: eigenes Passwort (scrypt), signiertes Sitzungs-Cookie, CSRF-Schutz."""
import base64
import hashlib
import hmac
import os
import secrets
import time

from . import config

COOKIE = "speicher_sitzung"
MAX_AGE = 7 * 86400
_fails: dict[str, list[float]] = {}


def _key() -> bytes:
    return config.secret("sitzung-schluessel", create=True).encode()


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def password_set() -> bool:
    return bool(os.environ.get("SPEICHER_PASSWORD")) or (config.data_dir() / "passwort").exists()


def set_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("Mindestens 8 Zeichen")
    salt = secrets.token_bytes(16)
    value = base64.b64encode(salt).decode() + "$" + base64.b64encode(_hash(password, salt)).decode()
    config.write_private(config.data_dir() / "passwort", value)


def check_password(password: str) -> bool:
    env = os.environ.get("SPEICHER_PASSWORD", "")
    if env:
        return hmac.compare_digest(password.encode(), env.encode())
    try:
        salt_b64, hash_b64 = (config.data_dir() / "passwort").read_text().strip().split("$")
    except (OSError, ValueError):
        return False
    return hmac.compare_digest(_hash(password, base64.b64decode(salt_b64)), base64.b64decode(hash_b64))


def throttled(ip: str) -> bool:
    """Mehr als 5 Fehlversuche in 10 Minuten -> gesperrt."""
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 600]
    return len(_fails[ip]) >= 5


def failed(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())


def make_session() -> str:
    payload = f"{int(time.time())}.{secrets.token_urlsafe(12)}"
    sig = hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def valid_session(value: str | None) -> bool:
    if not value or value.count(".") != 2:
        return False
    ts, nonce, sig = value.split(".")
    expected = hmac.new(_key(), f"{ts}.{nonce}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        return time.time() - int(ts) < MAX_AGE
    except ValueError:
        return False


def csrf_token(session: str) -> str:
    return hmac.new(_key(), b"csrf:" + session.encode(), hashlib.sha256).hexdigest()


def csrf_ok(session: str, token: str) -> bool:
    return bool(session and token) and hmac.compare_digest(csrf_token(session), token)
