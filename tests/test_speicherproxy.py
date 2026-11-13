"""Tests für die Weiterleitung eigener Domains an den Speicher-Container (app/speicherproxy.py)."""
import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import localdomain, speicherproxy


@pytest.fixture()
def upstream(monkeypatch):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(url=str(req.url), method=req.method, headers=dict(req.headers), body=req.read())
        if req.url.path == "/weg":
            return httpx.Response(303, headers=[("location", "/login"), ("set-cookie", "a=1"), ("set-cookie", "b=2")],
                                  stream=httpx.ByteStream(b""))
        return httpx.Response(206, stream=httpx.ByteStream(b"Hallo"),  # wie ein echter Server: noch nicht gelesen
                              headers={"content-range": "bytes 0-4/10", "content-type": "text/plain", "content-length": "5"})

    monkeypatch.setattr(speicherproxy, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("DATEIEN_PORT", "18092")
    monkeypatch.delenv("DATEIEN_URL", raising=False)
    app = FastAPI()

    @app.api_route("/{p:path}", methods=["GET", "PUT", "POST"])
    async def alles(request: Request, p: str):
        return await speicherproxy.forward(request, "dateien")

    return TestClient(app, base_url="https://dateien.kulbarts.com"), seen


def test_reicht_durch_mit_echter_ip_und_host(upstream):
    web, seen = upstream
    r = web.get("/d/Familie/a.txt?dl=1", headers={"Range": "bytes=0-4", "X-Forwarded-For": "6.6.6.6"})
    assert r.status_code == 206 and r.text == "Hallo" and r.headers["content-range"] == "bytes 0-4/10"
    assert seen["url"] == "http://127.0.0.1:18092/d/Familie/a.txt?dl=1"
    assert seen["headers"]["host"] == "dateien.kulbarts.com"
    assert seen["headers"]["x-forwarded-for"] == "testclient"  # vorgetäuschte IP fliegt raus
    assert seen["headers"]["x-forwarded-proto"] == "https"
    assert seen["headers"]["range"] == "bytes=0-4"


def test_hochladen_und_cookies(upstream):
    web, seen = upstream
    web.put("/api/hochladen/Familie/x.bin", content=b"x" * 5000, headers={"X-CSRF": "t"})
    assert seen["method"] == "PUT" and seen["body"] == b"x" * 5000 and seen["headers"]["x-csrf"] == "t"
    r = web.get("/weg", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert r.headers.get_list("set-cookie") == ["a=1", "b=2"]


def test_nicht_erreichbar(monkeypatch):
    def kaputt(req):
        raise httpx.ConnectError("weg")

    monkeypatch.setattr(speicherproxy, "_client", httpx.AsyncClient(transport=httpx.MockTransport(kaputt)))
    app = FastAPI()

    @app.get("/")
    async def start(request: Request):
        return await speicherproxy.forward(request, "speicher")

    r = TestClient(app).get("/")
    assert r.status_code == 502 and "nicht erreichbar" in r.text


def test_domains_bekommen_dns_und_zertifikat(monkeypatch):
    store = {}
    monkeypatch.setattr(localdomain.db, "get_setting", lambda k: store.get(k, ""))
    monkeypatch.setattr(localdomain.db, "set_setting", lambda k, v: store.__setitem__(k, v))
    localdomain.set_speicher_domains(["speicher.kulbarts.com"])
    localdomain.set_dateien_domains(["dateien.kulbarts.com"])
    assert {"speicher.kulbarts.com", "dateien.kulbarts.com"} <= set(localdomain.domains())
    assert localdomain.dateien_domains() == ["dateien.kulbarts.com"]


def test_merkt_sich_keine_cookies(monkeypatch):
    """Der Client wird von allen Geräten geteilt – Cookies dürfen nie von einem Gerät zum nächsten wandern."""
    seen = []

    def handler(req):
        seen.append(req.headers.get("cookie"))
        return httpx.Response(200, headers=[("set-cookie", "sitzung=geheim; Path=/")], stream=httpx.ByteStream(b"ok"))

    echt = httpx.AsyncClient
    monkeypatch.setattr(speicherproxy.httpx, "AsyncClient", lambda **kw: echt(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(speicherproxy, "_client", None)
    app = FastAPI()

    @app.get("/")
    async def start(request: Request):
        return await speicherproxy.forward(request, "dateien")

    web = TestClient(app)
    assert web.get("/").headers["set-cookie"].startswith("sitzung=geheim")
    TestClient(app).get("/")  # anderes Gerät ohne Cookie
    assert seen == [None, None]
