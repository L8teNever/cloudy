"""Eigene echte Domains nur im Heimnetz mit Let's-Encrypt-Zertifikat.

Zwei Namen sind einstellbar: einer für das ganze Dashboard (statt cloudy.lan, z. B. cloudy.kulbarts.com) und einer
nur für „Mein Gerät“ (/mein, z. B. main.kulbarts.com). Beide bekommen zusammen ein Zertifikat.
Auch Domain-Weiterleitungen, deren Quelle in einer Cloudflare-Zone des Tokens liegt (z. B. pimmel.kulbarts.com),
landen im selben Zertifikat und laufen so ohne Warnung und ohne lokale CA auf dem Gerät. Alle anderen Quellen
(tiktok.com …) bleiben bei der lokalen cloudy-CA.

So funktioniert es:
1. DNS: dnsmasq beantwortet die Domains im Heimnetz mit der IP des Pi. Von außen führt nichts hierher
   (bei Cloudflare darf es für die Domain keinen öffentlichen DNS-Eintrag zur Heimadresse geben).
2. Zertifikat: Let's Encrypt per DNS-Challenge (ACME DNS-01). Dafür legt cloudy kurz einen TXT-Eintrag
   `_acme-challenge.<domain>` über die Cloudflare-API an. Port 80/443 müssen nicht aus dem Internet erreichbar sein.
3. Der Cloudflare-API-Token steht nur in der Datenbank (Dashboard → Einstellungen), nie im Repo oder in .env.
   Er bleibt gespeichert, bis man ihn ausdrücklich löscht.
Auf den Geräten muss nichts installiert werden: Browser vertrauen Let's Encrypt von sich aus.
"""

import base64
import datetime
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.x509.oid import NameOID

from . import db
from .settings import Settings

DOMAIN_KEY = "local_domain"  # nur „Mein Gerät“ (/mein)
DASH_KEY = "local_domain_dashboard"  # das ganze Dashboard (wie cloudy.lan)
TOKEN_KEY = "local_domain_cf_token"
EMAIL_KEY = "local_domain_email"
STATUS_KEY = "local_domain_status"
SKIPPED_KEY = "local_domain_skipped"  # Weiterleitungs-Quellen ohne passende Zone beim Token (eine pro Zeile)
DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
CF_API = "https://api.cloudflare.com/client/v4"
RENEW_BEFORE_DAYS = 30
CHECK_EVERY = 12 * 3600
RETRY_AFTER_ERROR = 3600
DNS_WAIT = 15  # Sekunden, bis der TXT-Eintrag überall sichtbar ist

HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

_issue_lock = threading.Lock()


class AcmeError(Exception):
    pass


class NoZone(AcmeError):
    """Die Domain liegt in keiner Cloudflare-Zone, auf die der Token zugreifen darf."""


def normalize_domain(text: str) -> str:
    """'https://Main.Kulbarts.com/mein' -> 'main.kulbarts.com'; ValueError, wenn das keine echte Domain ist."""
    host = text.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0].rstrip(".")
    if not HOST_RE.match(host) or host.endswith((".lan", ".local", ".home.arpa")):
        raise ValueError("Bitte eine echte Domain eintragen, die dir gehört (z. B. main.kulbarts.com)")
    return host


def _setting_hosts(key: str) -> list[str]:
    """Gespeicherte Hosts (eine Domain pro Zeile; alte Einzelwerte bleiben gültig)."""
    raw = db.get_setting(key)
    if not raw:
        return []
    out: list[str] = []
    for line in raw.replace(",", "\n").splitlines():
        host = line.strip().lower().split("/")[0].rstrip(".")
        if host and host not in out:
            out.append(host)
    return out


def _save_hosts(key: str, hosts: list[str]) -> None:
    db.set_setting(key, "\n".join(hosts))


def mein_domains() -> list[str]:
    """Domains nur für „Mein Gerät“ (/mein) – Aufruf der Domain landet auf /mein."""
    return _setting_hosts(DOMAIN_KEY)


def dashboard_domains() -> list[str]:
    """Domains für das ganze Dashboard (wie cloudy.lan)."""
    return _setting_hosts(DASH_KEY)


def domain() -> str:
    """Erste Mein-Gerät-Domain (Kompatibilität)."""
    return (mein_domains() or [""])[0]


def dashboard_domain() -> str:
    """Erste Dashboard-Domain (Kompatibilität)."""
    return (dashboard_domains() or [""])[0]


def set_mein_domains(hosts: list[str]) -> None:
    _save_hosts(DOMAIN_KEY, hosts)


def set_dashboard_domains(hosts: list[str]) -> None:
    _save_hosts(DASH_KEY, hosts)


def domains() -> list[str]:
    """Alle eingestellten Domains (ohne Doppelte). Sie bekommen zusammen ein Zertifikat."""
    return list(dict.fromkeys(dashboard_domains() + mein_domains()))


def skipped() -> set[str]:
    return {n for n in db.get_setting(SKIPPED_KEY).split("\n") if n}


def reset_skipped() -> None:
    db.set_setting(SKIPPED_KEY, "")


def redirect_names() -> list[str]:
    """Quellen der Domain-Weiterleitungen, für die ein echtes Zertifikat versucht wird (nur echte Domains)."""
    own, skip = set(domains()), skipped()
    return [n for n in sorted(db.redirect_map()) if n not in own and n not in skip and HOST_RE.match(n)]


def cert_names() -> list[str]:
    """Alle Namen, die ins gemeinsame Zertifikat sollen (Let's Encrypt erlaubt höchstens 100)."""
    return list(dict.fromkeys(domains() + redirect_names()))[:100]


def paths(s: Settings) -> tuple[str, str]:
    d = os.path.join(s.data_dir, "certs")
    return os.path.join(d, "lokale-domain.crt"), os.path.join(d, "lokale-domain.key")


def account_key_path(s: Settings) -> str:
    return os.path.join(s.data_dir, "certs", "acme-konto.key")


def dnsmasq_lines(names: list[str], pi_ip: str) -> list[str]:
    return [f"address=/{n}/{pi_ip}" for n in names]


_cert_cache: dict[str, tuple[float, dict | None]] = {}


def cert_info(s: Settings) -> dict | None:
    """{'names': [...], 'expires': datetime} des aktuellen Zertifikats oder None (nur bei Änderung neu gelesen)."""
    crt, _ = paths(s)
    try:
        mtime = os.path.getmtime(crt)
    except OSError:
        return None
    cached = _cert_cache.get(crt)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(crt, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        info = {"names": san.get_values_for_type(x509.DNSName), "expires": cert.not_valid_after_utc}
    except (OSError, ValueError, x509.ExtensionNotFound):
        info = None
    _cert_cache[crt] = (mtime, info)
    return info


def real_cert_hosts(s: Settings) -> set[str]:
    """Namen, für die gerade wirklich das Let's-Encrypt-Zertifikat ausgeliefert wird."""
    info = cert_info(s)
    return set(info["names"]) & set(cert_names()) if info else set()


def needs_issue(s: Settings, names: list[str]) -> bool:
    info = cert_info(s)
    if not names:
        return False
    if info is None or not set(names) <= set(info["names"]):
        return True
    left = info["expires"] - datetime.datetime.now(datetime.timezone.utc)
    return left < datetime.timedelta(days=RENEW_BEFORE_DAYS)


def sni_pair(s: Settings, host: str) -> tuple[str, str] | None:
    """Für den HTTPS-Server: Zertifikat + Schlüssel, wenn der Browser genau diese Domain aufruft."""
    host = host.lower().rstrip(".")
    if host not in domains() and host not in real_cert_hosts(s):
        return None
    crt, key = paths(s)
    return (crt, key) if os.path.exists(crt) and os.path.exists(key) else None


# --------------------------------------------------------------------------- HTTP

def _http(url: str, data: bytes | None = None, headers: dict | None = None, method: str | None = None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


# --------------------------------------------------------------------------- Cloudflare DNS

class Cloudflare:
    def __init__(self, token: str, http=_http):
        self.token = token
        self.http = http
        self._zones: dict[str, str] = {}

    def _call(self, method: str, path: str, body: dict | None = None):
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        data = json.dumps(body).encode() if body is not None else None
        status, _, raw = self.http(CF_API + path, data, headers, method)
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        if status >= 300 or not payload.get("success"):
            errors = "; ".join(e.get("message", "") for e in payload.get("errors", [])) or f"HTTP {status}"
            raise AcmeError(f"Cloudflare: {errors}")
        return payload["result"]

    def zone_id(self, name: str) -> str:
        """Die Zone (z. B. kulbarts.com) zu einem Namen (main.kulbarts.com) finden."""
        parts = name.split(".")
        for i in range(len(parts) - 1):
            zone = ".".join(parts[i:])
            if zone not in self._zones:
                result = self._call("GET", f"/zones?name={zone}")
                if not result:
                    continue
                self._zones[zone] = result[0]["id"]
            return self._zones[zone]
        raise NoZone("Cloudflare: keine Zone für diese Domain gefunden (oder Token darf sie nicht lesen)")

    def add_txt(self, zone: str, name: str, value: str) -> str:
        record = self._call("POST", f"/zones/{zone}/dns_records",
                            {"type": "TXT", "name": name, "content": value, "ttl": 60})
        return record["id"]

    def delete(self, zone: str, record_id: str) -> None:
        self._call("DELETE", f"/zones/{zone}/dns_records/{record_id}")


# --------------------------------------------------------------------------- ACME (RFC 8555)

def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwk(key: ec.EllipticCurvePrivateKey) -> dict:
    nums = key.public_key().public_numbers()
    return {"crv": "P-256", "kty": "EC", "x": b64(nums.x.to_bytes(32, "big")), "y": b64(nums.y.to_bytes(32, "big"))}


def thumbprint(key: ec.EllipticCurvePrivateKey) -> str:
    return b64(hashlib.sha256(json.dumps(jwk(key), sort_keys=True, separators=(",", ":")).encode()).digest())


def dns_txt_value(token: str, key: ec.EllipticCurvePrivateKey) -> str:
    """Inhalt des TXT-Eintrags `_acme-challenge` für die DNS-01-Prüfung."""
    return b64(hashlib.sha256(f"{token}.{thumbprint(key)}".encode()).digest())


class Acme:
    def __init__(self, account_key: ec.EllipticCurvePrivateKey, directory: str = DIRECTORY, http=_http):
        self.key = account_key
        self.http = http
        status, _, raw = http(directory)
        if status != 200:
            raise AcmeError(f"Let's Encrypt nicht erreichbar (HTTP {status})")
        self.dir = json.loads(raw)
        self.kid: str | None = None
        self.nonce: str | None = None

    def _new_nonce(self) -> str:
        _, headers, _ = self.http(self.dir["newNonce"], method="HEAD")
        return {k.lower(): v for k, v in headers.items()}["replay-nonce"]

    def post(self, url: str, payload, retries: int = 2):
        """Signierte Anfrage (payload None = „POST-as-GET“). Gibt (Status, Header, Antwort-Bytes) zurück."""
        protected = {"alg": "ES256", "nonce": self.nonce or self._new_nonce(), "url": url}
        protected["kid" if self.kid else "jwk"] = self.kid or jwk(self.key)
        body = "" if payload is None else b64(json.dumps(payload).encode())
        signing = f"{b64(json.dumps(protected).encode())}.{body}"
        der = self.key.sign(signing.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        sig = b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        data = json.dumps({"protected": b64(json.dumps(protected).encode()), "payload": body, "signature": sig}).encode()
        status, headers, raw = self.http(url, data, {"Content-Type": "application/jose+json"}, "POST")
        headers = {k.lower(): v for k, v in headers.items()}
        self.nonce = headers.get("replay-nonce")
        if status >= 400:
            err = {}
            try:
                err = json.loads(raw)
            except ValueError:
                pass
            if err.get("type", "").endswith(":badNonce") and retries:
                return self.post(url, payload, retries - 1)
            raise AcmeError(f"Let's Encrypt: {err.get('detail') or f'HTTP {status}'}")
        return status, headers, raw

    def account(self, email: str = "") -> None:
        payload = {"termsOfServiceAgreed": True}
        if email:
            payload["contact"] = [f"mailto:{email}"]
        _, headers, _ = self.post(self.dir["newAccount"], payload)
        self.kid = headers["location"]

    def poll(self, url: str, done: str, tries: int = 30, wait: float = 2) -> dict:
        for _ in range(tries):
            _, _, raw = self.post(url, None)
            obj = json.loads(raw)
            if obj["status"] == done:
                return obj
            if obj["status"] in ("invalid", "revoked", "deactivated", "expired"):
                raise AcmeError(f"Let's Encrypt hat abgelehnt: {json.dumps(obj.get('error') or obj)[:300]}")
            time.sleep(wait)
        raise AcmeError("Let's Encrypt: Zeitüberschreitung")

    def issue(self, names: list[str], cf: Cloudflare, dns_wait: float = DNS_WAIT, wait: float = 2) -> tuple[str, str]:
        """Ein Zertifikat für alle Namen holen. Gibt (Zertifikatskette PEM, privater Schlüssel PEM) zurück."""
        _, headers, raw = self.post(
            self.dir["newOrder"], {"identifiers": [{"type": "dns", "value": n} for n in names]}
        )
        order, order_url = json.loads(raw), headers["location"]
        zones = {n: cf.zone_id(n) for n in names}
        for authz_url in order["authorizations"]:
            _, _, raw = self.post(authz_url, None)
            authz = json.loads(raw)
            name = authz["identifier"]["value"]
            zone = zones[name]
            challenge = next((c for c in authz["challenges"] if c["type"] == "dns-01"), None)
            if challenge is None:
                raise AcmeError("Let's Encrypt bietet keine DNS-Prüfung an")
            record = cf.add_txt(zone, f"_acme-challenge.{name}", dns_txt_value(challenge["token"], self.key))
            try:
                time.sleep(dns_wait)
                self.post(challenge["url"], {})
                self.poll(authz_url, "valid", wait=wait)
            finally:
                try:
                    cf.delete(zone, record)
                except AcmeError:
                    pass
        key = ec.generate_private_key(ec.SECP256R1())
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
            .sign(key, hashes.SHA256())
        )
        self.post(order["finalize"], {"csr": b64(csr.public_bytes(serialization.Encoding.DER))})
        done = self.poll(order_url, "valid", wait=wait)
        _, _, chain = self.post(done["certificate"], None)
        key_pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        return chain.decode(), key_pem


# --------------------------------------------------------------------------- Ablauf im Dienst

def _account_key(s: Settings) -> ec.EllipticCurvePrivateKey:
    path = account_key_path(s)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    except (FileNotFoundError, ValueError):
        key = ec.generate_private_key(ec.SECP256R1())
        _write_private(path, key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        return key


def _write_private(path: str, data: bytes) -> None:
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


def _issue_once(s: Settings, acme_factory, token: str) -> None:
    cf = Cloudflare(token)
    own = set(domains())
    names = []
    for name in cert_names():
        if name not in own:
            try:
                cf.zone_id(name)
            except NoZone:  # gehört nicht zu dieser Cloudflare-Zone (z. B. tiktok.com): bleibt bei der lokalen CA
                db.set_setting(SKIPPED_KEY, "\n".join(sorted(skipped() | {name})))
                continue
        names.append(name)
    if not names:
        db.set_setting(STATUS_KEY, f"{int(time.time())}|ok|Nichts zu tun.")
        return
    db.set_setting(STATUS_KEY, f"{int(time.time())}|läuft|Zertifikat wird geholt …")
    acme = acme_factory(_account_key(s))
    acme.account(db.get_setting(EMAIL_KEY))
    chain, key_pem = acme.issue(names, cf)
    crt, key = paths(s)
    with open(crt, "w", encoding="utf-8") as f:
        f.write(chain)
    _write_private(key, key_pem.encode())
    db.set_setting(STATUS_KEY, f"{int(time.time())}|ok|Zertifikat geholt ({len(names)} Namen).")


def issue_now(s: Settings, acme_factory=Acme) -> None:
    """Zertifikat für alle Namen holen und ablegen. Fehler landen als Status in der Datenbank.

    Kommt währenddessen eine Weiterleitung dazu, wird danach gleich noch einmal geholt.
    """
    token = db.get_setting(TOKEN_KEY)
    if not cert_names() or not token:
        return
    if not _issue_lock.acquire(blocking=False):
        return
    try:
        for _ in range(3):
            try:
                _issue_once(s, acme_factory, token)
            except Exception as exc:  # noqa: BLE001 – Fehler anzeigen, nie den Dienst stoppen
                db.set_setting(STATUS_KEY, f"{int(time.time())}|fehler|{exc}")
                return
            if not needs_issue(s, cert_names()):
                return
    finally:
        _issue_lock.release()


def status() -> dict:
    ts, _, rest = db.get_setting(STATUS_KEY).partition("|")
    state, _, text = rest.partition("|")
    return {"time": int(ts) if ts.isdigit() else 0, "state": state, "text": text}


def start_in_background(s: Settings) -> None:
    threading.Thread(target=issue_now, args=(s,), daemon=True).start()


def renew_loop(s: Settings, stop: threading.Event) -> None:
    """Prüft regelmäßig, ob das Zertifikat bald abläuft oder fehlt, und holt ein neues."""
    while True:
        wait = CHECK_EVERY
        try:
            if cert_names() and db.get_setting(TOKEN_KEY) and needs_issue(s, cert_names()):
                issue_now(s)
                if status()["state"] == "fehler":
                    wait = RETRY_AFTER_ERROR
        except Exception as exc:  # noqa: BLE001
            print(f"cloudy: Zertifikatsprüfung fehlgeschlagen: {exc}", flush=True)
        if stop.wait(wait):
            return
