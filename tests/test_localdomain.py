import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app import db, localdomain
from app.dnsmasq import sync_redirects_hosts
from app.settings import Settings


def test_normalize_domain():
    assert localdomain.normalize_domain("https://Main.Kulbarts.com/") == "main.kulbarts.com"
    for bad in ("cloudy.lan", "foo", "a b.com", ""):
        with pytest.raises(ValueError):
            localdomain.normalize_domain(bad)


def test_dns_txt_value_is_sha256_length():
    key = ec.generate_private_key(ec.SECP256R1())
    value = localdomain.dns_txt_value("tok", key)
    assert len(base64.urlsafe_b64decode(value + "==")) == 32


def test_dns_line_written_to_dnsmasq_conf(client):
    s = Settings.from_env()
    db.set_setting(localdomain.DOMAIN_KEY, "main.kulbarts.com")
    sync_redirects_hosts(s)
    assert f"address=/main.kulbarts.com/{s.pi_ip}" in open(s.redirects_path, encoding="utf-8").read()


def test_domain_only_shows_mein(client):
    db.set_setting(localdomain.DOMAIN_KEY, "main.kulbarts.com")
    h = {"host": "main.kulbarts.com"}
    assert client.get("/", headers=h, follow_redirects=False).headers["location"] == "/mein"
    assert client.get("/mein", headers=h).status_code == 200
    assert client.get("/einstellungen", headers=h).status_code == 404
    assert client.get("/geraete", headers=h).status_code == 404


def test_settings_form_saves_and_rejects(client, monkeypatch):
    monkeypatch.setattr(localdomain, "start_in_background", lambda s: None)
    assert client.post("/lokale-domain", data={"name": "cloudy.lan"}).status_code == 400
    r = client.post("/lokale-domain", data={"name": "main.kulbarts.com", "cf_token": "abc"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.get_setting(localdomain.TOKEN_KEY) == "abc"
    assert "main.kulbarts.com" in client.get("/einstellungen").text


class FakeAcme(localdomain.Acme):
    def __init__(self, key):  # kein Netzwerk
        self.key = key

    def account(self, email=""):
        pass

    def issue(self, name, cf, **kw):
        return "CHAIN", "KEY"


def test_issue_now_writes_files_and_status(client):
    s = Settings.from_env()
    db.set_setting(localdomain.DOMAIN_KEY, "main.kulbarts.com")
    db.set_setting(localdomain.TOKEN_KEY, "t")
    localdomain.issue_now(s, acme_factory=FakeAcme)
    crt, key = localdomain.paths(s)
    assert open(crt).read() == "CHAIN" and open(key).read() == "KEY"
    assert localdomain.status()["state"] == "ok"
    assert localdomain.sni_pair(s, "Main.Kulbarts.com") == (crt, key)
    assert localdomain.sni_pair(s, "andere.de") is None


def test_issue_error_is_stored(client):
    class Boom(FakeAcme):
        def issue(self, *a, **k):
            raise localdomain.AcmeError("kaputt")

    s = Settings.from_env()
    db.set_setting(localdomain.DOMAIN_KEY, "main.kulbarts.com")
    db.set_setting(localdomain.TOKEN_KEY, "t")
    localdomain.issue_now(s, acme_factory=Boom)
    st = localdomain.status()
    assert st["state"] == "fehler" and "kaputt" in st["text"]


def test_full_acme_flow_with_fake_servers():
    """Ganzer Ablauf gegen einen nachgebauten ACME-Server und eine nachgebaute Cloudflare-API."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    calls = []
    state = {}

    def b64d(s):
        return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))

    def http(url, data=None, headers=None, method=None):
        calls.append((method, url))
        if url.startswith(localdomain.CF_API):
            if "/zones?name=" in url:
                ok = url.endswith("kulbarts.com")
                return 200, {}, json.dumps({"success": True, "result": [{"id": "Z"}] if ok else []}).encode()
            if method == "POST":
                state["txt"] = json.loads(data)["content"]
                return 200, {}, json.dumps({"success": True, "result": {"id": "R"}}).encode()
            return 200, {}, json.dumps({"success": True, "result": {}}).encode()
        base = "https://acme.test"
        if url.endswith("/directory"):
            d = {k: f"{base}/{k}" for k in ("newNonce", "newAccount", "newOrder")}
            return 200, {}, json.dumps(d).encode()
        h = {"Replay-Nonce": "n"}
        if method == "HEAD":
            return 200, h, b""
        body = json.loads(data)
        payload = b64d(body["payload"]) if body["payload"] else None
        if url.endswith("/newAccount"):
            h["Location"] = f"{base}/acct/1"
            return 201, h, b"{}"
        if url.endswith("/newOrder"):
            h["Location"] = f"{base}/order/1"
            return 201, h, json.dumps({"authorizations": [f"{base}/authz/1"], "finalize": f"{base}/fin"}).encode()
        if url.endswith("/authz/1"):
            ch = [{"type": "dns-01", "token": "TOKEN", "url": f"{base}/chall/1"}]
            status = "valid" if state.get("answered") else "pending"
            return 200, h, json.dumps({"status": status, "challenges": ch}).encode()
        if url.endswith("/chall/1"):
            state["answered"] = True
            return 200, h, b"{}"
        if url.endswith("/fin"):
            state["csr"] = payload["csr"]
            return 200, h, b"{}"
        if url.endswith("/order/1"):
            return 200, h, json.dumps({"status": "valid", "certificate": f"{base}/cert"}).encode()
        if url.endswith("/cert"):
            return 200, h, b"PEMCHAIN"
        raise AssertionError(url)

    key = ec.generate_private_key(ec.SECP256R1())
    acme = localdomain.Acme(key, directory="https://acme.test/directory", http=http)
    acme.account("a@b.de")
    chain, key_pem = acme.issue("main.kulbarts.com", localdomain.Cloudflare("tok", http=http), dns_wait=0, wait=0)
    assert chain == "PEMCHAIN"
    assert state["txt"] == localdomain.dns_txt_value("TOKEN", key)
    assert ("DELETE", f"{localdomain.CF_API}/zones/Z/dns_records/R") in calls
    csr = x509.load_der_x509_csr(base64.urlsafe_b64decode(state["csr"] + "=="))
    san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["main.kulbarts.com"]
    serialization.load_pem_private_key(key_pem.encode(), password=None)
