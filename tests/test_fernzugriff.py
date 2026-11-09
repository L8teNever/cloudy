import base64
import json
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from app import db, fernzugriff as fz, localdomain
from app.main import app


class FakeCF:
    """Kleiner Nachbau der Cloudflare-API (nur was cloudy braucht)."""

    def __init__(self, reject_long_session=False):
        self.calls = []
        self.dns = {"r0": {"id": "r0", "type": "A", "name": "www.kulbarts.com", "content": "1.2.3.4"}}
        self.apps, self.policies, self.tunnels = {}, {}, {}
        self.reject_long_session = reject_long_session
        self.ingress = None

    def __call__(self, url, data, headers, method):
        path = url.removeprefix(localdomain.CF_API)
        body = json.loads(data) if data else None
        self.calls.append((method, path, body))
        ok = lambda r: (200, {}, json.dumps({"success": True, "result": r}).encode())  # noqa: E731
        err = lambda m: (400, {}, json.dumps({"success": False, "errors": [{"message": m}]}).encode())  # noqa: E731
        base, _, query = path.partition("?")
        if base == "/zones":
            return ok([{"id": "z1"}] if query == "name=kulbarts.com" else [])
        if base == "/zones/z1":
            return ok({"id": "z1", "account": {"id": "acc"}})
        if base == "/zones/z1/dns_records":
            if method == "GET":
                name = query.removeprefix("name=")
                return ok([r for r in self.dns.values() if r["name"] == name])
            rid = f"r{len(self.dns) + 1}"
            self.dns[rid] = {"id": rid, **body}
            return ok(self.dns[rid])
        if base.startswith("/zones/z1/dns_records/") and method == "DELETE":
            self.dns.pop(base.rsplit("/", 1)[1], None)
            return ok({})
        if base == "/accounts/acc/cfd_tunnel":
            if method == "GET":
                return ok(list(self.tunnels.values()))
            self.tunnels["t1"] = {"id": "t1", "name": body["name"]}
            return ok(self.tunnels["t1"])
        if base == "/accounts/acc/cfd_tunnel/t1":
            return ok(self.tunnels["t1"])
        if base == "/accounts/acc/cfd_tunnel/t1/token":
            return ok("TUNNELTOKEN")
        if base == "/accounts/acc/cfd_tunnel/t1/configurations":
            self.ingress = body["config"]["ingress"]
            return ok({})
        if base == "/accounts/acc/access/apps" and method == "POST":
            if self.reject_long_session and body["session_duration"] != "730h":
                return err("invalid session_duration")
            self.apps["a1"] = {**body, "id": "a1", "aud": "aud123"}
            return ok(self.apps["a1"])
        if base == "/accounts/acc/access/apps/a1":
            if method == "DELETE":
                self.apps.pop("a1", None)
                return ok({})
            self.apps["a1"].update(body)
            return ok(self.apps["a1"])
        if base == "/accounts/acc/access/apps/a1/policies":
            if method == "GET":
                return ok(list(self.policies.values()))
            self.policies["p1"] = {**body, "id": "p1"}
            return ok(self.policies["p1"])
        if base == "/accounts/acc/access/apps/a1/policies/p1":
            self.policies["p1"].update(body)
            return ok(self.policies["p1"])
        if base == "/accounts/acc/access/organizations":
            return ok({"auth_domain": "team.cloudflareaccess.com"})
        return err(f"unbekannt {method} {path}")


def _settings():
    return app.state.settings


def test_einrichten_und_lokal(client, tmp_path, monkeypatch):
    monkeypatch.setattr(fz, "apply", lambda settings, cf=None, _orig=fz.apply: _orig(settings, cf) if cf else {})
    monkeypatch.setattr(localdomain, "start_in_background", lambda s: None)
    db.set_setting(localdomain.TOKEN_KEY, "cftoken")
    localdomain.set_hub_domains(["www.kulbarts.com"])
    r = client.post("/fernzugriff", data={"fern_host": ["www.kulbarts.com"], "fern_seite": ["karte"],
                                          "fern_emails": "A@x.de\nb@y.de", "fern_tage": "330"}, follow_redirects=False)
    assert r.status_code == 303
    # Name gehört jetzt zur Karte (auch im Heimnetz), nicht mehr zum Hub
    assert localdomain.karte_domains() == ["www.kulbarts.com"] and localdomain.hub_domains() == []
    cfg = fz.load()
    assert cfg["emails"] == ["a@x.de", "b@y.de"] and cfg["days"] == 330

    cf = FakeCF()
    cfg = fz.apply(_settings(), localdomain.Cloudflare("cftoken", http=cf))
    assert cfg["state"] == "fehler" and "schon einen DNS-Eintrag" in cfg["text"]  # fremder A-Eintrag bleibt
    fz.set_config([("www.kulbarts.com", "karte")], ["a@x.de", "b@y.de"], 330, ["www.kulbarts.com"])
    cfg = fz.apply(_settings(), localdomain.Cloudflare("cftoken", http=cf))
    assert cfg["state"] == "ok", cfg["text"]
    assert [r["type"] for r in cf.dns.values()] == ["CNAME"]
    rec = next(iter(cf.dns.values()))
    assert rec["content"] == "t1.cfargotunnel.com" and rec["proxied"]
    assert cf.ingress[0]["hostname"] == "www.kulbarts.com" and cf.ingress[-1] == {"service": "http_status:404"}
    assert cf.apps["a1"]["session_duration"] == "7920h"
    assert cf.policies["p1"]["include"] == [{"email": {"email": "a@x.de"}}, {"email": {"email": "b@y.de"}}]
    assert cfg["aud"] == "aud123" and cfg["team"] == "team.cloudflareaccess.com"
    assert (tmp_path / "warp" / "tunnel" / "token").read_text().strip() == "TUNNELTOKEN"

    # Abschalten: DNS-Eintrag und App weg, Tunnel-Token leer
    fz.set_config([], [], 330, [])
    cfg = fz.apply(_settings(), localdomain.Cloudflare("cftoken", http=cf))
    assert cf.dns == {} and cf.apps == {} and "Aus" in cfg["text"]
    assert (tmp_path / "warp" / "tunnel" / "token").read_text().strip() == ""


def test_lange_anmeldung_abgelehnt():
    db.set_setting(localdomain.TOKEN_KEY, "cftoken")
    fz.set_config([("karte.kulbarts.com", "karte")], ["a@x.de"], 330, [])
    cf = FakeCF(reject_long_session=True)
    with TestClient(app):
        cfg = fz.apply(app.state.settings, localdomain.Cloudflare("cftoken", http=cf))
    assert cfg["state"] == "ok" and "30 Tage" in cfg["text"] and cfg["hours"] == 730


def test_eingaben_pruefen(client):
    r = client.post("/fernzugriff", data={"fern_host": ["karte.kulbarts.com"], "fern_seite": ["karte"], "fern_emails": ""},
                    follow_redirects=False)
    assert "fern_fehler" in r.headers["location"]
    r = client.post("/fernzugriff", data={"fern_host": ["karte.kulbarts.com"], "fern_seite": ["karte"],
                                          "fern_emails": "kaputt"}, follow_redirects=False)
    assert "fern_fehler" in r.headers["location"] and fz.hosts() == {}


def _jwt(key, claims):
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    head, body = b({"alg": "RS256", "kid": "k1"}), b(claims)
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def test_tunnel_nur_mit_anmeldung(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(fz, "fetch_keys", lambda team: {"k1": key.public_key()})
    fz._keys.clear()
    fz.set_config([("karte.kulbarts.com", "karte")], ["a@x.de"], 330, [])
    cfg = fz.load()
    cfg.update(team="team.cloudflareaccess.com", aud="aud123")
    fz.save(cfg)
    now = time.time()
    good = _jwt(key, {"aud": ["aud123"], "iss": "https://team.cloudflareaccess.com", "email": "a@x.de", "exp": now + 600})
    stranger = _jwt(key, {"aud": ["aud123"], "iss": "https://team.cloudflareaccess.com", "email": "z@x.de", "exp": now + 600})
    with TestClient(app, client=(fz.TUNNEL_IP, 5000)) as remote:
        h = {"host": "karte.kulbarts.com"}
        assert remote.get("/", headers=h).status_code == 403
        assert remote.get("/", headers={**h, "cf-access-jwt-assertion": stranger}).status_code == 403
        r = remote.get("/", headers={**h, "cf-access-jwt-assertion": good}, follow_redirects=False)
        assert r.status_code == 200  # kein Umweg über https://…:Heim-Port
        assert remote.get("/", headers={"host": "cloudy.lan", "cf-access-jwt-assertion": good}).status_code == 403
        assert remote.get("/mein", headers={**h, "cf-access-jwt-assertion": good}).status_code == 403
    with TestClient(app) as local:  # zu Hause: ohne Anmeldung
        assert local.get("/", headers={"host": "karte.kulbarts.com"}).status_code == 200
