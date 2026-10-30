"""Fernzugriff über Cloudflare Tunnel: Access-Token-Prüfung, Sperre ohne Anmeldung, Einrichtung im Dashboard."""

import base64
import json
import os
import stat
import sys
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from app import access, db, warp
from app.main import app
from app.settings import Settings

TEAM = "l8tenever.cloudflareaccess.com"
AUD = "26f4b29835b82d63bb68cb0bc0c5089b20f1e50f593e9bab1e1c9f4073ce0179"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_token(key=KEY, kid="k1", aud=AUD, iss=f"https://{TEAM}", exp_in=600, email="l8tenever@gmail.com") -> str:
    header = _b64(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    now = int(time.time())
    body = _b64(json.dumps({"aud": [aud], "iss": iss, "exp": now + exp_in, "iat": now, "email": email}).encode())
    sig = key.sign(f"{header}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{body}.{_b64(sig)}"


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    access._keys.clear()
    calls = []

    def fake_fetch(team):
        calls.append(team)
        return {"k1": KEY.public_key()}

    monkeypatch.setattr(access, "fetch_keys", fake_fetch)
    return calls


@pytest.fixture
def tunnel_client(monkeypatch):
    # Im TestClient heißt der Absender "testclient" -> so tun, als käme alles über den Tunnel.
    monkeypatch.setenv("TUNNEL_IP", "testclient")
    with TestClient(app) as c:
        yield c


def configure():
    db.set_setting(access.TEAM_KEY, TEAM)
    db.set_setting(access.AUD_KEY, AUD)


def test_valid_token():
    assert access.verify(make_token(), TEAM, AUD) == "l8tenever@gmail.com"


@pytest.mark.parametrize("token", [
    make_token(exp_in=-600),  # abgelaufen
    make_token(aud="0" * 64),  # andere App
    make_token(iss="https://fremd.cloudflareaccess.com"),  # anderes Team
    make_token(key=OTHER_KEY),  # falsche Signatur
    make_token(kid="unbekannt"),  # Schlüssel gibt es nicht
    "kein.jwt",
    "",
])
def test_invalid_tokens(token):
    assert access.verify(token, TEAM, AUD) is None


def test_keys_are_cached(keys):
    for _ in range(3):
        assert access.verify(make_token(), TEAM, AUD)
    assert keys == [TEAM]


def test_normalize():
    assert access.normalize_team("https://L8teNever.cloudflareaccess.com/") == TEAM
    assert access.normalize_team("l8tenever") == TEAM
    with pytest.raises(ValueError):
        access.normalize_team("evil.example.com")
    assert access.normalize_aud(AUD.upper()) == AUD
    with pytest.raises(ValueError):
        access.normalize_aud("abc")


def test_tunnel_without_setup_is_locked(tunnel_client):
    r = tunnel_client.get("/", headers={"Cf-Access-Jwt-Assertion": make_token()})
    assert r.status_code == 403 and "Cloudflare-Anmeldung" in r.text


def test_tunnel_without_token_is_locked(tunnel_client):
    configure()
    r = tunnel_client.get("/")
    assert r.status_code == 403


def test_tunnel_with_valid_login(tunnel_client):
    configure()
    r = tunnel_client.get("/", headers={"Cf-Access-Jwt-Assertion": make_token()})
    assert r.status_code == 200
    # Cookie geht genauso (Browser schickt CF_Authorization mit)
    tunnel_client.cookies.set("CF_Authorization", make_token())
    assert tunnel_client.get("/einstellungen").status_code == 200


def test_tunnel_bad_token_is_locked(tunnel_client):
    configure()
    r = tunnel_client.get("/api/devices", headers={"Cf-Access-Jwt-Assertion": make_token(key=OTHER_KEY)})
    assert r.status_code == 403


def test_mein_only_at_home(tunnel_client):
    configure()
    r = tunnel_client.get("/mein", headers={"Cf-Access-Jwt-Assertion": make_token()})
    assert r.status_code == 403 and "Heimnetz" in r.text
    r = tunnel_client.post("/mein/warp", data={"slot": ""}, headers={"Cf-Access-Jwt-Assertion": make_token()})
    assert r.status_code == 403


def test_home_network_unchanged(client):
    assert client.get("/").status_code == 200
    assert client.get("/mein").status_code == 200


def test_setup_in_dashboard(client):
    r = client.post("/tunnel/login", data={"token": "eyJhIjoiYiJ9", "team": "l8tenever", "aud": AUD},
                    follow_redirects=False)
    assert r.status_code == 303
    s = Settings.from_env()
    assert warp.has_token(s, access.SLOT)
    path = os.path.join(warp.slot_dir(s, access.SLOT), "token")
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert access.config() == (TEAM, AUD)
    page = client.get("/einstellungen").text
    assert "Fernzugriff" in page and "http://172.29.88.1:" in page and "geprüft" in page

    # Leerer Token behält den alten, ungültige Eingaben werden abgelehnt.
    assert client.post("/tunnel/login", data={"token": "", "team": "", "aud": ""}).status_code == 200
    assert warp.has_token(s, access.SLOT)
    assert client.post("/tunnel/login", data={"aud": "kaputt"}, follow_redirects=False).status_code == 400
    assert client.post("/tunnel/login", data={"token": "mit leerzeichen"}, follow_redirects=False).status_code == 400

    assert client.post("/tunnel/logout", follow_redirects=False).status_code == 303
    assert not warp.has_token(s, access.SLOT)
