import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app import db, localdomain
from app.dnsmasq import sync_redirects_hosts
from app.settings import Settings


def test_normalize_domain():
    assert localdomain.normalize_domain("https://Main.Kulbarts.com/") == "main.kulbarts.com"
    assert localdomain.normalize_domain("main.kulbarts.com/mein") == "main.kulbarts.com"
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


def test_dashboard_domain_shows_everything_and_is_in_dns(client):
    s = Settings.from_env()
    db.set_setting(localdomain.DASH_KEY, "cloudy.kulbarts.com")
    db.set_setting(localdomain.DOMAIN_KEY, "main.kulbarts.com")
    sync_redirects_hosts(s)
    conf = open(s.redirects_path, encoding="utf-8").read()
    assert f"address=/cloudy.kulbarts.com/{s.pi_ip}" in conf and f"address=/main.kulbarts.com/{s.pi_ip}" in conf
    h = {"host": "cloudy.kulbarts.com"}
    assert client.get("/", headers=h, follow_redirects=False).status_code == 200
    assert client.get("/einstellungen", headers=h).status_code == 200
    assert client.get("/einstellungen", headers={"host": "main.kulbarts.com"}).status_code == 404


def test_same_name_for_both_is_rejected(client, monkeypatch):
    monkeypatch.setattr(localdomain, "start_in_background", lambda s: None)
    r = client.post("/lokale-domain", data={"name": "a.kulbarts.com", "dashboard": "a.kulbarts.com"})
    assert r.status_code == 400


def test_hub_domain_shows_hub(client):
    s = Settings.from_env()
    db.set_setting(localdomain.HUB_KEY, "home.kulbarts.com")
    sync_redirects_hosts(s)
    assert f"address=/home.kulbarts.com/{s.pi_ip}" in open(s.redirects_path, encoding="utf-8").read()
    h = {"host": "home.kulbarts.com"}
    assert client.get("/", headers=h, follow_redirects=False).headers["location"] == "/hub"
    assert client.get("/hub", headers=h).status_code == 200
    assert client.get("/hub/statistik", headers=h).status_code == 200
    page = client.get("/hub/statistik", headers=h).text
    assert "Nur Anzeige" in page
    assert "/geraete/" not in page
    assert client.get("/mein", headers=h).status_code == 200
    assert client.get("/einstellungen", headers=h).status_code == 404
    assert client.get("/geraete", headers=h).status_code == 404


def test_same_name_hub_rejected(client, monkeypatch):
    monkeypatch.setattr(localdomain, "start_in_background", lambda s: None)
    r = client.post("/lokale-domain", data={"hub": "a.kulbarts.com", "dashboard": "a.kulbarts.com"})
    assert r.status_code == 400


def test_multiple_mein_and_dashboard_domains(client, monkeypatch):
    from urllib.parse import urlencode

    monkeypatch.setattr(localdomain, "start_in_background", lambda s: None)
    body = urlencode([
        ("dashboard", "cloudy.kulbarts.com"),
        ("dashboard", "dash.kulbarts.com"),
        ("hub", "home.kulbarts.com/hub"),
        ("name", "main.kulbarts.com/mein"),
        ("name", "handy.kulbarts.com"),
        ("ha_url", "http://ha.lan:8123"),
        ("cf_token", "tok"),
    ])
    r = client.post(
        "/lokale-domain",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert localdomain.dashboard_domains() == ["cloudy.kulbarts.com", "dash.kulbarts.com"]
    assert localdomain.hub_domains() == ["home.kulbarts.com"]
    assert localdomain.mein_domains() == ["main.kulbarts.com", "handy.kulbarts.com"]
    assert localdomain.ha_url() == "http://ha.lan:8123"
    page = client.get("/einstellungen").text
    assert "handy.kulbarts.com" in page and "/mein" in page
    assert client.get("/", headers={"host": "handy.kulbarts.com"}, follow_redirects=False).headers["location"] == "/mein"
    assert client.get("/einstellungen", headers={"host": "dash.kulbarts.com"}).status_code == 200
    s = Settings.from_env()
    sync_redirects_hosts(s)
    conf = open(s.redirects_path, encoding="utf-8").read()
    assert f"address=/handy.kulbarts.com/{s.pi_ip}" in conf
    assert f"address=/dash.kulbarts.com/{s.pi_ip}" in conf


def test_cert_covers_both_names(client):
    s = Settings.from_env()
    db.set_setting(localdomain.DASH_KEY, "cloudy.kulbarts.com")
    db.set_setting(localdomain.DOMAIN_KEY, "main.kulbarts.com")
    db.set_setting(localdomain.TOKEN_KEY, "t")
    seen = {}

    class Two(FakeAcme):
        def issue(self, names, cf, **kw):
            seen["names"] = names
            return "CHAIN", "KEY"

    localdomain.issue_now(s, acme_factory=Two)
    assert seen["names"] == ["cloudy.kulbarts.com", "main.kulbarts.com"]
    crt, key = localdomain.paths(s)
    assert localdomain.sni_pair(s, "cloudy.kulbarts.com") == (crt, key)
    assert localdomain.sni_pair(s, "main.kulbarts.com") == (crt, key)


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
            ident = {"type": "dns", "value": "main.kulbarts.com"}
            return 200, h, json.dumps({"status": status, "identifier": ident, "challenges": ch}).encode()
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
    chain, key_pem = acme.issue(["main.kulbarts.com"], localdomain.Cloudflare("tok", http=http), dns_wait=0, wait=0)
    assert chain == "PEMCHAIN"
    assert state["txt"] == localdomain.dns_txt_value("TOKEN", key)
    assert ("DELETE", f"{localdomain.CF_API}/zones/Z/dns_records/R") in calls
    csr = x509.load_der_x509_csr(base64.urlsafe_b64decode(state["csr"] + "=="))
    san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["main.kulbarts.com"]
    serialization.load_pem_private_key(key_pem.encode(), password=None)


def _selfsigned(names):
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=90))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return cert.public_bytes(serialization.Encoding.PEM).decode(), key_pem.decode()


class ZoneCloudflare:
    """Nur *.kulbarts.com liegt in der Zone des Tokens."""

    def __init__(self, token):
        pass

    def zone_id(self, name):
        if not name.endswith("kulbarts.com"):
            raise localdomain.NoZone("keine Zone")
        return "Z"


def test_redirect_in_own_zone_gets_real_cert_and_others_are_skipped(client, monkeypatch):
    monkeypatch.setattr(localdomain, "Cloudflare", ZoneCloudflare)
    s = Settings.from_env()
    db.set_setting(localdomain.TOKEN_KEY, "t")
    db.add_redirect("pimmel.kulbarts.com", "https://cloudy.kulbarts.com")
    db.add_redirect("tiktok.com", "https://wikipedia.org")
    db.add_redirect("router", "http://192.168.178.1")
    seen = {}

    class Real(FakeAcme):
        def issue(self, names, cf, **kw):
            seen["names"] = names
            return _selfsigned(names)

    localdomain.issue_now(s, acme_factory=Real)
    assert seen["names"] == ["pimmel.kulbarts.com"]
    assert localdomain.skipped() == {"tiktok.com"}
    crt, key = localdomain.paths(s)
    assert localdomain.sni_pair(s, "pimmel.kulbarts.com") == (crt, key)
    assert localdomain.sni_pair(s, "tiktok.com") is None
    assert not localdomain.needs_issue(s, localdomain.cert_names())


def test_new_redirect_needs_new_cert_and_removed_one_does_not(client, monkeypatch):
    monkeypatch.setattr(localdomain, "Cloudflare", ZoneCloudflare)
    s = Settings.from_env()
    db.set_setting(localdomain.TOKEN_KEY, "t")
    db.add_redirect("a.kulbarts.com", "https://x.de")

    class Real(FakeAcme):
        def issue(self, names, cf, **kw):
            return _selfsigned(names)

    localdomain.issue_now(s, acme_factory=Real)
    assert not localdomain.needs_issue(s, localdomain.cert_names())
    db.add_redirect("b.kulbarts.com", "https://x.de")
    assert localdomain.needs_issue(s, localdomain.cert_names())
    localdomain.issue_now(s, acme_factory=Real)
    assert localdomain.sni_pair(s, "b.kulbarts.com") is not None
    db.remove_redirect("a.kulbarts.com")
    assert localdomain.sni_pair(s, "a.kulbarts.com") is None
    assert not localdomain.needs_issue(s, localdomain.cert_names())


def test_token_stays_when_domains_are_empty_and_can_be_deleted(client, monkeypatch):
    monkeypatch.setattr(localdomain, "start_in_background", lambda s: None)
    client.post("/lokale-domain", data={"name": "main.kulbarts.com", "cf_token": "abc"})
    client.post("/lokale-domain", data={"name": "", "dashboard": ""})
    assert db.get_setting(localdomain.TOKEN_KEY) == "abc"
    client.post("/lokale-domain/token-loeschen")
    assert db.get_setting(localdomain.TOKEN_KEY) == ""


def test_tunnel_and_portal_are_gone(client):
    assert client.post("/tunnel/login", data={}).status_code in (404, 405)
    assert client.get("/einstellungen").status_code == 200
    assert "Fernzugriff" not in client.get("/einstellungen").text
    assert client.post("/mein/koppeln", data={"code": "123456"}).status_code in (404, 405)


def test_pi_ip_override_and_detection(tmp_path, monkeypatch):
    from app import settings as S

    monkeypatch.setattr(S, "detect_lan_ip", lambda router: "192.168.178.3")
    monkeypatch.setattr(S, "is_local_ip", lambda ip: ip == "192.168.178.3")
    d = str(tmp_path)
    # Falsche PI_IP aus .env -> erkannte Adresse
    assert S.resolve_pi_ip(d, "192.168.178.2", "192.168.178.1", True) == ("192.168.178.3", "erkannt", "192.168.178.3")
    # Richtige PI_IP bleibt
    assert S.resolve_pi_ip(d, "192.168.178.3", "192.168.178.1", True)[1] == "env"
    # Ohne Prüfung (Tests/Vorschau) unverändert
    assert S.resolve_pi_ip(d, "192.168.178.2", "192.168.178.1", False)[0] == "192.168.178.2"
    # Im Dashboard festgelegt hat Vorrang
    S.write_pi_ip_override(d, "192.168.178.9")
    assert S.resolve_pi_ip(d, "192.168.178.2", "192.168.178.1", True)[:2] == ("192.168.178.9", "dashboard")
    S.write_pi_ip_override(d, "")
    assert S.read_pi_ip_override(d) == ""
    import pytest

    with pytest.raises(ValueError):
        S.write_pi_ip_override(d, "kein.ip")
    # Erkannte Adresse aus fremdem Netz wird ignoriert
    monkeypatch.setattr(S, "detect_lan_ip", lambda router: "10.0.0.5")
    assert S.resolve_pi_ip(d, "192.168.178.2", "192.168.178.1", True) == ("192.168.178.2", "env", "")


def test_pi_ip_form(client, tmp_path):
    from app import settings as S

    r = client.post("/einstellungen/pi-ip", data={"ip": "192.168.178.3"}, follow_redirects=False)
    assert r.status_code == 303
    assert S.read_pi_ip_override(str(tmp_path)) == "192.168.178.3"
    r = client.post("/einstellungen/pi-ip", data={"ip": "quatsch"}, follow_redirects=False)
    assert "fehler=" in r.headers["location"]
    client.post("/einstellungen/pi-ip", data={"auto": "1"})
    assert S.read_pi_ip_override(str(tmp_path)) == ""
    assert "IP-Adresse des Pi" in client.get("/einstellungen").text


def test_env_file_first_line_wins(tmp_path, monkeypatch):
    from app import settings as S

    env = tmp_path / "cloudy.env"
    env.write_text(
        "PI_IP=192.168.178.3\nGATEWAY_ENABLED=1\n# Kommentar\nPI_IP=192.168.178.2\nGATEWAY_ENABLED=0\nNEU='x'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PI_IP", "192.168.178.2")  # so setzt Docker es bei doppelten Zeilen (letzte gewinnt)
    monkeypatch.setenv("GATEWAY_ENABLED", "0")
    monkeypatch.delenv("NEU", raising=False)
    fixed = S.apply_env_file(str(env))
    import os

    assert os.environ["PI_IP"] == "192.168.178.3"
    assert os.environ["GATEWAY_ENABLED"] == "1"
    assert os.environ["NEU"] == "x"
    assert set(fixed) == {"PI_IP", "GATEWAY_ENABLED", "NEU"}
    assert S.apply_env_file(str(tmp_path / "fehlt.env")) == []
