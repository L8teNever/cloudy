import json
import socket
import ssl
import subprocess
import time
from dataclasses import replace

import pytest
from cryptography import x509
from fastapi.testclient import TestClient

from app import db, warp
from app.certs import ca_cert_path, ensure_certs, ensure_domain_cert, start_https_server
from app.dnsmasq import build_config, sync_redirects_hosts
from app.main import app
from app.redirects import dnsmasq_lines, find_rule, normalize_source, normalize_target
from app.settings import Settings


def test_normalize_source():
    assert normalize_source("tiktok.com") == "tiktok.com"
    assert normalize_source("https://www.TikTok.com/video?x=1") == "tiktok.com"
    assert normalize_source("http://fritzbox/") == "fritzbox"
    assert normalize_source("sub.example.org.") == "sub.example.org"
    for bad in ("", "a b.com", "http://", "-x.com", "x..com"):
        with pytest.raises(ValueError):
            normalize_source(bad)


def test_normalize_target():
    assert normalize_target("wikipedia.org") == "https://wikipedia.org"
    assert normalize_target("http://192.168.178.1:8080/x") == "http://192.168.178.1:8080/x"
    for bad in ("", "ftp://x.de", "https://"):
        with pytest.raises(ValueError):
            normalize_target(bad)


def test_find_rule_matches_subdomains_only():
    rules = {"tiktok.com": "https://a", "fritzbox": "http://b"}
    assert find_rule("m.tiktok.com", rules) == ("tiktok.com", "https://a")
    assert find_rule("TIKTOK.COM.", rules) == ("tiktok.com", "https://a")
    assert find_rule("fritzbox", rules) == ("fritzbox", "http://b")
    assert find_rule("nottiktok.com", rules) is None
    assert find_rule("tiktok.com.evil.de", rules) is None


def test_dnsmasq_config_uses_address_for_redirects():
    s = Settings.from_env()
    assert dnsmasq_lines(["tiktok.com", "tiktok.com", "fritzbox"], "192.168.178.3") == [
        "address=/fritzbox/192.168.178.3",
        "address=/tiktok.com/192.168.178.3",
    ]
    db.add_redirect("tiktok.com", "wikipedia.org")
    sync_redirects_hosts(s)
    assert "address=/tiktok.com/192.168.178.2" in open(s.redirects_path, encoding="utf-8").read()
    assert f"conf-file={s.redirects_path}" in build_config(s)


def test_domain_cert_covers_subdomains_and_chains_to_ca():
    s = Settings.from_env()
    crt, _ = ensure_domain_cert(s, "tiktok.com")
    cert = x509.load_pem_x509_certificate(open(crt, "rb").read())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert set(san.get_values_for_type(x509.DNSName)) == {"tiktok.com", "*.tiktok.com"}
    ca = x509.load_pem_x509_certificate(open(ca_cert_path(s), "rb").read())
    cert.verify_directly_issued_by(ca)
    first = open(crt, "rb").read()
    ensure_domain_cert(s, "tiktok.com")
    assert open(crt, "rb").read() == first  # wird wiederverwendet, nicht neu erzeugt


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _https_get(port: int, cafile: str, server_name: str) -> bytes:
    ctx = ssl.create_default_context(cafile=cafile)
    for _ in range(50):
        try:
            raw = socket.create_connection(("127.0.0.1", port), timeout=2)
            break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.fail("HTTPS-Server startet nicht")
    with ctx.wrap_socket(raw, server_hostname=server_name) as tls:
        tls.sendall(f"GET / HTTP/1.1\r\nHost: {server_name}\r\nConnection: close\r\n\r\n".encode())
        data = b""
        while chunk := tls.recv(4096):
            data += chunk
    return data


def test_https_redirect_with_matching_certificate():
    """Echte TLS-Verbindung zu m.tiktok.com: Zertifikat muss passen (gegen die cloudy-CA geprüft) und weiterleiten."""
    s = Settings.from_env()
    crt, key = ensure_certs(s)
    db.add_redirect("tiktok.com", "wikipedia.org")
    port = _free_port()

    def sni(name):
        rule = find_rule(name, db.redirect_map())
        return ensure_domain_cert(s, rule[0]) if rule else None

    with TestClient(app):
        server = start_https_server(app, crt, key, port, sni_cert=sni)
        try:
            data = _https_get(port, ca_cert_path(s), "m.tiktok.com")
            assert b"302 Found" in data and b"location: https://wikipedia.org" in data.lower()
            # Das Dashboard-Zertifikat für cloudy.lan funktioniert weiterhin.
            assert b"200 OK" in _https_get(port, ca_cert_path(s), "cloudy.lan")
        finally:
            server.should_exit = True


def test_ipv6_bypass_detection(monkeypatch):
    def fake_run(out):
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr="")

    ra = json.dumps([{"dst": "default", "gateway": "fe80::3ea6:2fff:feac:2414", "dev": "eth0", "protocol": "ra"}])
    monkeypatch.setattr(warp.subprocess, "run", fake_run(ra))
    assert warp.ipv6_bypass("eth0") is True
    monkeypatch.setattr(warp.subprocess, "run", fake_run("[]"))
    assert warp.ipv6_bypass("eth0") is False


def test_dashboard_warns_about_ipv6_bypass(client, monkeypatch):
    monkeypatch.setattr(warp, "ipv6_bypass", lambda iface: True)
    client.app.state.settings = replace(client.app.state.settings, gateway_enabled=True)
    assert "IPv6 läuft am VPN vorbei" not in client.get("/").text  # kein VPN zugeordnet -> keine Warnung
    db.set_setting(warp.DEFAULT_KEY, "vpn1")
    assert "IPv6 läuft am VPN vorbei" in client.get("/").text
