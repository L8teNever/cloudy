"""Eigene Domains für den Speicher-Container (Verwaltung, „Dateien im Browser“, Anime) und Jellyfin – nur im Heimnetz.

cloudy beantwortet die Namen im Heimnetz per DNS mit seiner IP, hält das Let's-Encrypt-Zertifikat und reicht jede
Anfrage unverändert an den Speicher weiter (Host-Header bleibt, damit dessen Herkunfts-Prüfung passt). Die echte
Geräte-IP geht als X-Forwarded-For mit: Der Speicher vertraut dem nur von 127.0.0.1 und erkennt daran das Gerät
für die Regeln pro Freigabe. Hoch- und Herunterladen laufen gestreamt, also auch für große Dateien.
"""

import os
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx
from fastapi import Request
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

HOP = {"date", "server", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
       "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-real-ip", "forwarded"}

_client: httpx.AsyncClient | None = None


def target(art: str) -> str:
    """Basis-URL: SPEICHER_URL/DATEIEN_URL/ANIME_URL/JELLYFIN_URL oder 127.0.0.1 mit dem Port aus der .env."""
    if art == "anime":
        return (os.environ.get("ANIME_URL") or f"http://127.0.0.1:{os.environ.get('ANIME_PORT') or '8093'}").rstrip("/")
    if art == "jellyfin":
        return (os.environ.get("JELLYFIN_URL") or "http://127.0.0.1:8096").rstrip("/")
    if art == "dateien":
        return (os.environ.get("DATEIEN_URL") or f"http://127.0.0.1:{os.environ.get('DATEIEN_PORT') or '8092'}").rstrip("/")
    return (os.environ.get("SPEICHER_URL") or f"http://127.0.0.1:{os.environ.get('SPEICHER_PORT') or '8091'}").rstrip("/")


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # Kein Lese-Timeout: Downloads und ZIPs können lange laufen. Keine Proxys aus der Umgebung.
        # Wichtig: keine Cookies merken – sonst bekäme jedes Gerät die Anmeldung des ersten (geteilter Client).
        _client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=None, write=None, pool=10), trust_env=False,
                                    follow_redirects=False, cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])))
    return _client


def forward_headers(request: Request) -> list[tuple[str, str]]:
    headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP and k.lower() != "content-length"]
    if request.headers.get("content-length"):
        headers.append(("content-length", request.headers["content-length"]))
    headers.append(("x-forwarded-for", request.client.host if request.client else ""))
    headers.append(("x-forwarded-proto", request.url.scheme))
    return headers


async def forward(request: Request, art: str) -> Response:
    url = target(art) + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    has_body = request.method not in ("GET", "HEAD", "OPTIONS")
    req = client().build_request(request.method, url, headers=forward_headers(request),
                                 content=request.stream() if has_body else None)
    try:
        resp = await client().send(req, stream=True)
    except httpx.HTTPError:
        name, box = {"dateien": ("Dateien", "cloudy-speicher"), "anime": ("Anime", "cloudy-speicher"),
                     "jellyfin": ("Jellyfin", "cloudy-jellyfin")}.get(art, ("Speicher", "cloudy-speicher"))
        return Response(f"{name} ist gerade nicht erreichbar (läuft der Container {box}?).", status_code=502,
                        media_type="text/plain; charset=utf-8")
    headers = [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in HOP]
    out = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, background=BackgroundTask(resp.aclose))
    out.raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers]
    return out
