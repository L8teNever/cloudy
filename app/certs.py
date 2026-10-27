"""Lokale Zertifizierungsstelle (CA) und HTTPS-Zertifikat für das Dashboard im Heimnetz."""

import base64
import datetime
import ipaddress
import os
import sys
import threading
import uuid

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .settings import Settings

CA_DAYS = 3650
# iOS/macOS akzeptieren für selbst installierte CAs maximal 825 Tage.
SERVER_DAYS = 825
RENEW_BEFORE_DAYS = 30


def certs_dir(s: Settings) -> str:
    return os.path.join(s.data_dir, "certs")


def ca_cert_path(s: Settings) -> str:
    return os.path.join(certs_dir(s), "cloudy-ca.crt")


def _paths(s: Settings) -> dict[str, str]:
    d = certs_dir(s)
    return {
        "ca_crt": ca_cert_path(s),
        "ca_key": os.path.join(d, "cloudy-ca.key"),
        "crt": os.path.join(d, "server.crt"),
        "key": os.path.join(d, "server.key"),
    }


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _load_cert(path: str) -> x509.Certificate | None:
    try:
        with open(path, "rb") as f:
            return x509.load_pem_x509_certificate(f.read())
    except (FileNotFoundError, ValueError):
        return None


def _load_key(path: str):
    try:
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    except (FileNotFoundError, ValueError):
        return None


def _write_key(path: str, key) -> None:
    data = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _write_cert(path: str, cert: x509.Certificate) -> None:
    with open(path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def _expiring(cert: x509.Certificate) -> bool:
    return cert.not_valid_after_utc - _now() < datetime.timedelta(days=RENEW_BEFORE_DAYS)


def wanted_names(s: Settings) -> tuple[set[str], set[str]]:
    dns = {s.local_name} if s.local_name else set()
    return dns, {s.pi_ip}


def cert_names(cert: x509.Certificate) -> tuple[set[str], set[str]]:
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return (
        set(san.get_values_for_type(x509.DNSName)),
        {str(ip) for ip in san.get_values_for_type(x509.IPAddress)},
    )


def _make_ca() -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cloudy lokale CA")])
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _make_server_cert(s: Settings, ca_key, ca_cert: x509.Certificate) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    dns, ips = wanted_names(s)
    sans = [x509.DNSName(n) for n in sorted(dns)] + [x509.IPAddress(ipaddress.ip_address(i)) for i in sorted(ips)]
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, s.local_name or s.pi_ip)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=SERVER_DAYS))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=False, crl_sign=False,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def ensure_certs(s: Settings) -> tuple[str, str]:
    """Legt CA und Server-Zertifikat an bzw. erneuert sie. Gibt (Zertifikat, Schlüssel) für den Server zurück."""
    p = _paths(s)
    os.makedirs(certs_dir(s), exist_ok=True)

    ca_cert, ca_key = _load_cert(p["ca_crt"]), _load_key(p["ca_key"])
    new_ca = ca_cert is None or ca_key is None or _expiring(ca_cert)
    if new_ca:
        ca_key, ca_cert = _make_ca()
        _write_key(p["ca_key"], ca_key)
        _write_cert(p["ca_crt"], ca_cert)

    cert, key = _load_cert(p["crt"]), _load_key(p["key"])
    renew = new_ca or cert is None or key is None or _expiring(cert) or cert_names(cert) != wanted_names(s)
    if renew:
        key, cert = _make_server_cert(s, ca_key, ca_cert)
        _write_key(p["key"], key)
        _write_cert(p["crt"], cert)
    return p["crt"], p["key"]


def make_mobileconfig(s: Settings) -> bytes:
    """Generiert ein Apple .mobileconfig Profil fuer iOS/iPadOS/macOS."""
    path = ca_cert_path(s)
    with open(path, "rb") as f:
        cert_pem = f.read()
    cert = x509.load_pem_x509_certificate(cert_pem)
    cert_der_b64 = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode("ascii")
    payload_uuid = str(uuid.uuid4())
    profile_uuid = str(uuid.uuid4())
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>PayloadDisplayName</key>
    <string>cloudy Lokale CA</string>
    <key>PayloadDescription</key>
    <string>Vertrauenswuerdiges Root-Zertifikat fuer cloudy im Heimnetz</string>
    <key>PayloadIdentifier</key>
    <string>lan.cloudy.ca</string>
    <key>PayloadOrganization</key>
    <string>cloudy</string>
    <key>PayloadRemovalDisallowed</key>
    <false/>
    <key>PayloadType</key>
    <string>Configuration</string>
    <key>PayloadUUID</key>
    <string>{profile_uuid}</string>
    <key>PayloadVersion</key>
    <integer>1</integer>
    <key>PayloadContent</key>
    <array>
        <dict>
            <key>PayloadCertificateFileName</key>
            <string>cloudy-ca.crt</string>
            <key>PayloadContent</key>
            <data>
{cert_der_b64}
            </data>
            <key>PayloadDescription</key>
            <string>Root-Zertifikat fuer cloudy</string>
            <key>PayloadDisplayName</key>
            <string>cloudy Root CA</string>
            <key>PayloadIdentifier</key>
            <string>com.apple.security.root.{payload_uuid}</string>
            <key>PayloadType</key>
            <string>com.apple.security.root</string>
            <key>PayloadUUID</key>
            <string>{payload_uuid}</string>
            <key>PayloadVersion</key>
            <integer>1</integer>
        </dict>
    </array>
</dict>
</plist>
"""
    return xml.encode("utf-8")


def make_windows_bat(s: Settings) -> str:
    """Generiert ein Windows-Batch-Skript zur 1-Klick-Installation des CA-Zertifikats."""
    path = ca_cert_path(s)
    with open(path, "r", encoding="utf-8") as f:
        pem = f.read().strip()
    return f"""@echo off
chcp 65001 >nul
echo ========================================================
echo   cloudy CA - Automatischer Zertifikats-Import
echo ========================================================
echo.
(
{chr(10).join(f'echo {line}' for line in pem.splitlines())}
) > "%TEMP%\\cloudy-ca.crt"

echo Installiere Root-Zertifikat in vertrauenswuerdige Stammzertifizierungsstellen...
certutil -addstore -f "Root" "%TEMP%\\cloudy-ca.crt"
del "%TEMP%\\cloudy-ca.crt" 2>nul
echo.
echo [OK] Zertifikat erfolgreich installiert!
echo Browser (Chrome, Edge, Firefox) neu starten.
pause
"""


def start_https_server(app, certfile: str, keyfile: str, port: int) -> uvicorn.Server:
    """Zweiter Webserver (HTTPS) neben dem normalen. Gleiche App, aber ohne eigenen Start/Stop (lifespan aus)."""
    config = uvicorn.Config(
        app, host="0.0.0.0", port=port, ssl_certfile=certfile, ssl_keyfile=keyfile,
        lifespan="off", log_level="warning",
    )
    server = uvicorn.Server(config)

    def run() -> None:
        try:
            server.run()
        except Exception as exc:  # noqa: BLE001 – HTTPS darf das normale Dashboard nie verhindern
            print(f"cloudy: HTTPS-Server beendet: {exc}", file=sys.stderr, flush=True)

    threading.Thread(target=run, daemon=True).start()
    return server
