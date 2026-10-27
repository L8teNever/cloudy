import socket
import ssl
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.certs import ca_cert_path, cert_names, ensure_certs, start_https_server
from app.main import app
from app.settings import Settings
from cryptography import x509


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_certs_created_and_names(tmp_path):
    s = Settings.from_env()
    crt, key = ensure_certs(s)
    cert = x509.load_pem_x509_certificate(open(crt, "rb").read())
    assert cert_names(cert) == ({"cloudy.lan"}, {"192.168.178.2"})
    ca = x509.load_pem_x509_certificate(open(ca_cert_path(s), "rb").read())
    assert cert.issuer == ca.subject
    cert.verify_directly_issued_by(ca)


def test_certs_are_reused_and_renewed_on_name_change(monkeypatch):
    crt, _ = ensure_certs(Settings.from_env())
    first = open(crt, "rb").read()
    ensure_certs(Settings.from_env())
    assert open(crt, "rb").read() == first  # unverändert -> nicht neu erzeugt

    monkeypatch.setenv("PI_IP", "192.168.178.3")
    ensure_certs(Settings.from_env())
    cert = x509.load_pem_x509_certificate(open(crt, "rb").read())
    assert cert_names(cert) == ({"cloudy.lan"}, {"192.168.178.3"})


def test_ca_download_route(client):
    assert client.get("/ca.crt").status_code == 404  # HTTPS aus -> keine CA
    ensure_certs(Settings.from_env())
    r = client.get("/ca.crt")
    assert r.status_code == 200
    assert b"BEGIN CERTIFICATE" in r.content and b"PRIVATE KEY" not in r.content

    # Apple .mobileconfig
    r_mc = client.get("/ca.mobileconfig")
    assert r_mc.status_code == 200
    assert "com.apple.security.root" in r_mc.text
    assert "cloudy-ca.mobileconfig" in r_mc.headers.get("content-disposition", "")

    # Windows .bat
    r_bat = client.get("/install-ca.bat")
    assert r_bat.status_code == 200
    assert "certutil -addstore" in r_bat.text


def test_real_https_connection_validates_against_ca():
    s = Settings.from_env()
    crt, key = ensure_certs(s)
    port = _free_port()
    with TestClient(app):  # Startet die App (Datenbank usw.); der HTTPS-Server nutzt dieselbe App.
        server = start_https_server(app, crt, key, port)
        try:
            ctx = ssl.create_default_context(cafile=ca_cert_path(s))
            for _ in range(50):
                try:
                    raw = socket.create_connection(("127.0.0.1", port), timeout=1)
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                pytest.fail("HTTPS-Server startet nicht")
            with ctx.wrap_socket(raw, server_hostname="cloudy.lan") as tls:
                tls.sendall(b"GET /healthz HTTP/1.1\r\nHost: cloudy.lan\r\nConnection: close\r\n\r\n")
                data = b""
                while chunk := tls.recv(4096):
                    data += chunk
            assert b"200 OK" in data and b'"status":"ok"' in data
        finally:
            server.should_exit = True
