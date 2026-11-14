"""Anime-Seite als eigene Webseite (ANIME_PORT, Standard 8093): AniWorld durchsuchen und Folgen in die Mediathek
laden – Jellyfin zeigt sie unter „Serien“. Die eigentliche Arbeit macht aniworld.py.

Nur im Heimnetz (cloudy reicht die eigene Domain nur lokal durch, kein Cloudflare-Fernzugriff). Zugriff wie bei
„Streaming“ in den Dateien: Regeln der Freigabe für den Streaming-Ordner pro Gerät, Anmeldung mit den Benutzern aus
„Freigaben“ (gleiches Cookie wie die Dateien-Seite). Unter „Geladen“ stehen die fertigen Anime zum Abspielen und Löschen.

/live/… ist für Jellyfin (Anime live ohne Download, siehe anime_live.py): ohne Anmeldung, aber jede Adresse signiert.
"""
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import anime_katalog, anime_live, aniworld, auth, config, dateien, freigaben, mediathek, mediathek_db
from .dateien import Fehler

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.globals["static_v"] = config.static_version()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.middleware("http")
async def security(request: Request, call_next):
    if request.url.path.startswith("/live/"):
        # Jellyfin holt hier die Live-Streams: keine Sitzung, keine Geräte-Regeln (die Adressen sind signiert).
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Cache-Control"] = "no-store"
        return response
    sess = dateien.read_session(request.cookies.get(dateien.COOKIE))
    neu = None
    if not sess:
        neu = dateien.make_session()
        sess = ("", neu)
    request.state.benutzer, request.state.session = sess
    e = freigaben.Einstellungen.load()
    if e.geraet_login and not request.state.benutzer:
        key = await run_in_threadpool(freigaben.key_fuer_ip, dateien._ip(request))
        request.state.benutzer = e.geraet_benutzer(key)
    request.state.recht = e.recht(request.state.benutzer) if request.state.benutzer else "voll"
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return Response("Fremde Herkunft", 403)
        if neu:
            return _antwort(request, Fehler(403, "Sitzung abgelaufen – bitte Seite neu laden"))
    response = await call_next(request)
    if neu:
        dateien._set_cookie(response, neu)
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if not request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("Content-Security-Policy", (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; font-src 'self'; img-src 'self' data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'self'"))
    return response


def _page(request: Request, vorlage: str, status: int = 200, **ctx) -> HTMLResponse:
    ctx.update(request=request, benutzer=request.state.benutzer, basis="anime_base.html",
               app_name="Anime", csrf=dateien.csrf_token(request.state.session),
               ok=ctx.get("ok") or "", fehler=ctx.get("fehler") or "")
    return templates.TemplateResponse(request, vorlage, ctx, status_code=status)


def _antwort(request: Request, exc: Fehler) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"fehler": exc.text}, exc.status)
    return _page(request, "dateien_fehler.html", status=exc.status, text=exc.text)


@app.exception_handler(Fehler)
async def _fehler_handler(request: Request, exc: Fehler):
    return _antwort(request, exc)


def _zugriff(request: Request, write: bool = False) -> bool:
    cfg = mediathek_db.Einstellungen.load()
    return dateien._check_mediathek_access(request, cfg.streaming_ordner, write=write)


# ---------- Seiten ----------

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def start(request: Request):
    try:
        can_write = await run_in_threadpool(_zugriff, request)
    except Fehler as exc:
        if exc.status == 401:
            return RedirectResponse("/login", 303)
        raise
    aniworld.start()
    anime_live.reparieren_im_hintergrund()
    return _page(request, "anime.html", can_write=can_write, pool_da=mediathek.pool_mounted(),
                 cfg=mediathek_db.Einstellungen.load())


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return _page(request, "dateien_login.html", weiter="/")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    dateien._csrf(request, str(form.get("csrf", "")))
    name, pw = str(form.get("name", "")).strip(), str(form.get("passwort", ""))
    ip = dateien._ip(request) or "?"
    if auth.throttled("dateien:" + ip):
        return _page(request, "dateien_login.html", weiter="/", name=name, fehler="Zu viele Versuche – in 10 Minuten nochmal")
    if not await run_in_threadpool(freigaben.check_login, name, pw):
        auth.failed("dateien:" + ip)
        return _page(request, "dateien_login.html", weiter="/", name=name, fehler="Benutzer oder Passwort falsch")
    resp = RedirectResponse("/", 303)
    dateien._set_cookie(resp, dateien.make_session(freigaben.benutzer_fuer(name)))
    return resp


@app.post("/logout")
async def logout(request: Request):
    form = await request.form()
    dateien._csrf(request, str(form.get("csrf", "")))
    resp = RedirectResponse("/", 303)
    dateien._set_cookie(resp, dateien.make_session())
    return resp


# ---------- API ----------

async def _anime(request: Request, fn, *args, write: bool = False):
    await run_in_threadpool(_zugriff, request, write)
    try:
        return await run_in_threadpool(fn, *args)
    except aniworld.AniFehler as exc:
        raise Fehler(502, str(exc)) from exc


@app.get("/api/anime/suche")
async def api_suche(request: Request, q: str = ""):
    return {"items": await _anime(request, aniworld.suche, q)}


@app.get("/api/anime/serie")
async def api_serie(request: Request, slug: str = ""):
    return await _anime(request, aniworld.serie, slug)


@app.get("/api/anime/staffel")
async def api_staffel(request: Request, slug: str = "", nr: int = 1):
    return {"folgen": await _anime(request, aniworld.staffel, slug, max(0, nr))}


@app.get("/api/anime/status")
async def api_status(request: Request):
    aniworld.start()
    await run_in_threadpool(_zugriff, request)
    return {"jobs": aniworld.liste(), "browser": await run_in_threadpool(aniworld.browser_status),
            "pool_da": mediathek.pool_mounted()}


@app.get("/api/mediathek/bild")
async def api_bild(request: Request, url: str = ""):
    """Cover über den Server (die Seite lädt keine fremden Bilder)."""
    from . import mediathek_providers
    if not mediathek_providers.is_allowed_image_url(url):
        raise Fehler(400, "Bildquelle nicht erlaubt")
    res = await run_in_threadpool(mediathek_providers.fetch_cached_image, url)
    if not res:
        raise Fehler(404, "Bild nicht gefunden")
    data, ctype = res
    return Response(data, media_type=ctype, headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/anime/laden")
async def api_laden(request: Request):
    """{slug, sprache, staffeln: [nr …]} lädt ganze Staffeln, {slug, sprache, folgen: [{staffel, nr}]} einzelne Folgen."""
    dateien._csrf(request, request.headers.get("x-csrf", ""))
    body = await request.json()
    aniworld.start()

    def _los():
        info, wahl = _auswahl(body)
        return {"neu": aniworld.hinzufuegen(info, wahl, str(body.get("sprache") or "1")), "gesamt": len(wahl)}

    return await _anime(request, _los, write=True)


@app.post("/api/anime/live")
async def api_live_anlegen(request: Request):
    """Wie /api/anime/laden, legt aber nur .strm-Dateien an: Jellyfin spielt die Folgen live von AniWorld ab."""
    dateien._csrf(request, request.headers.get("x-csrf", ""))
    body = await request.json()
    aniworld.start()

    def _los():
        info, wahl = _auswahl(body)
        return {**anime_live.anlegen(info, wahl, str(body.get("sprache") or "1")), "gesamt": len(wahl)}

    return await _anime(request, _los, write=True)


@app.get("/api/anime/live")
async def api_live_liste(request: Request):
    await run_in_threadpool(_zugriff, request)
    return {"items": await run_in_threadpool(anime_live.liste),
            "loeschen": getattr(request.state, "bereich_recht", "voll") == "voll"}


@app.post("/api/anime/live/neu")
async def api_live_neu(request: Request):
    """Jetzt bei allen Live-Serien nach neuen Folgen schauen (sonst alle 6 Stunden von selbst)."""
    dateien._csrf(request, request.headers.get("x-csrf", ""))
    return await _anime(request, lambda: {"neu": anime_live.neue_folgen()}, write=True)


@app.post("/api/anime/live/entfernen")
async def api_live_entfernen(request: Request):
    """{ordner} – alle Live-Folgen dieser Serie löschen (heruntergeladene bleiben)."""
    dateien._csrf(request, request.headers.get("x-csrf", ""))
    await run_in_threadpool(_zugriff, request, True)
    if getattr(request.state, "bereich_recht", "voll") != "voll":
        raise Fehler(403, f"{request.state.benutzer} darf hier nichts löschen")
    body = await request.json()
    return {"n": await _anime(request, anime_live.entfernen, str(body.get("ordner") or ""), write=True)}


def _auswahl(body: dict) -> tuple[dict, list[dict]]:
    """{slug, staffeln: [nr …]} ganze Staffeln oder {slug, folgen: [{staffel, nr}]} einzelne Folgen → (serie, folgen)."""
    info = aniworld.serie(str(body.get("slug") or ""))
    gueltig = {s["nr"] for s in info["staffeln"]}
    einzeln = body.get("folgen") if isinstance(body.get("folgen"), list) else []
    einzeln = [f for f in einzeln if isinstance(f, dict) and str(f.get("staffel")).isdigit() and str(f.get("nr")).isdigit()]
    ganze = {int(x) for x in (body.get("staffeln") or []) if str(x).isdigit()} & gueltig
    staffeln = ganze | ({int(f["staffel"]) for f in einzeln} & gueltig)
    wahl: list[dict] = []
    for s in sorted(staffeln):
        nur = {int(f["nr"]) for f in einzeln if int(f["staffel"]) == s}
        for f in aniworld.staffel(info["slug"], s):
            if s in ganze or f["nr"] in nur:
                wahl.append({**f, "staffel": s})
    if not wahl:
        raise aniworld.AniFehler("Keine passenden Folgen gefunden")
    return info, wahl


@app.get("/api/anime/geladen")
async def api_geladen(request: Request):
    """Fertig geladene Anime aus der Mediathek (mit Staffeln und Folgen)."""
    await run_in_threadpool(_zugriff, request)

    def _load():
        if mediathek.pool_mounted():
            mediathek_db.sync_disk(mediathek.pool_root())
        items = [t for t in mediathek_db.list_local_library() if t["provider"] == "aniworld" or t["anime"]]
        for t in items:
            t["groesse"] = sum(f.get("groesse") or 0 for f in t["dateien"])
            t.pop("meta", None)
            t.pop("watch", None)
        return {"items": items, "pool_da": mediathek.pool_mounted(),
                "loeschen": getattr(request.state, "bereich_recht", "voll") == "voll"}

    return await run_in_threadpool(_load)


@app.get("/api/mediathek/stream/{file_id}")
async def api_stream(request: Request, file_id: int):
    """Folge im Browser abspielen (gleiche Prüfung wie in den Dateien)."""
    return await dateien.api_mediathek_stream(request, file_id)


@app.delete("/api/mediathek/datei/{file_id}")
async def api_datei_loeschen(request: Request, file_id: int):
    """Folge von der Platte und aus der Mediathek löschen (nur mit vollem Recht auf den Streaming-Ordner)."""
    res = await dateien.api_mediathek_delete_file(request, file_id)
    aniworld.jellyfin_spaeter()
    return res


@app.post("/api/anime/job")
async def api_job(request: Request):
    """{aktion: abbrechen|wiederholen|aufraeumen, id}"""
    dateien._csrf(request, request.headers.get("x-csrf", ""))
    await run_in_threadpool(_zugriff, request, True)
    body = await request.json()
    aktion, jid = str(body.get("aktion") or ""), str(body.get("id") or "")
    if aktion == "abbrechen":
        return {"ok": aniworld.abbrechen(jid)}
    if aktion == "wiederholen":
        return {"ok": True, "n": aniworld.wiederholen(jid)}
    if aktion == "aufraeumen":
        return {"ok": True, "n": aniworld.aufraeumen()}
    raise Fehler(400, "Unbekannte Aktion")



# ---------- Live für Jellyfin (.strm → hier) ----------

def _live_fehler(exc: anime_live.LiveFehler) -> Response:
    return PlainTextResponse(exc.text, exc.status)


@app.api_route("/live/{slug}/{staffel}/{folge}/{sprache}/{sig}.m3u8", methods=["GET", "HEAD"])
def live_folge(slug: str, staffel: int, folge: int, sprache: str, sig: str):
    """Playlist einer Folge: Stream bei AniWorld suchen (oder aus dem Zwischenspeicher) und umgeschrieben ausgeben."""
    try:
        art, inhalt = anime_live.playlist(slug, staffel, folge, sprache, sig)
    except anime_live.LiveFehler as exc:
        return _live_fehler(exc)
    if art == "weiter":
        return RedirectResponse(inhalt, 302)
    return Response(inhalt, media_type="application/vnd.apple.mpegurl")


@app.api_route("/live/p/{sig}/{token}.{ext}", methods=["GET", "HEAD"])
def live_stueck(request: Request, sig: str, token: str, ext: str):
    """Ein Stück (Teil-Playlist, Segment, Schlüssel oder MP4) vom Hoster – mit Referer, Range wird durchgereicht."""
    try:
        url, ref = anime_live.proxy_ziel(sig, token)
    except anime_live.LiveFehler as exc:
        return _live_fehler(exc)
    try:
        r = anime_live.oeffnen(url, ref, request.headers.get("range", ""))
    except urllib.error.HTTPError as exc:
        return PlainTextResponse(f"Hoster antwortet mit {exc.code}", exc.code if 400 <= exc.code < 600 else 502)
    except (urllib.error.URLError, OSError) as exc:
        return PlainTextResponse(f"Hoster nicht erreichbar: {exc}", 502)
    ctype = r.headers.get("Content-Type", "")
    anfang = r.read(64 * 1024)
    if anime_live.ist_playlist(url, ctype, anfang[:64]):
        try:
            text = (anfang + r.read(4 * 1024 * 1024)).decode("utf-8", "replace")
            return Response(anime_live.umschreiben(text, r.geturl(), ref), media_type="application/vnd.apple.mpegurl")
        finally:
            r.close()
    headers = {"Accept-Ranges": r.headers.get("Accept-Ranges") or "bytes"}
    for h in ("Content-Length", "Content-Range"):
        if r.headers.get(h):
            headers[h] = r.headers[h]

    def stuecke():
        try:
            yield anfang
            while True:
                b = r.read(256 * 1024)
                if not b:
                    break
                yield b
        finally:
            r.close()

    typ = ctype if ctype and "html" not in ctype and "image" not in ctype else ("video/mp4" if ext == "mp4" else "video/mp2t")
    return StreamingResponse(stuecke(), status_code=r.status, media_type=typ, headers=headers)


# ---------- Anime-Katalog (Platzhalter in Jellyfin → hier hinzufügen) ----------

@app.api_route("/katalog/{slug}/{sig}.mp4", methods=["GET", "HEAD"])
def katalog_hinzufuegen(slug: str, sig: str):
    """Jellyfin spielt „▶ Zu Jellyfin hinzufügen“ ab: Anime live anlegen (im Hintergrund) und kurzen Hinweis-Clip senden."""
    try:
        anime_katalog.pruefen(slug, sig)
        clip = anime_katalog.hinweis_clip()
    except anime_live.LiveFehler as exc:
        return _live_fehler(exc)
    except aniworld.AniFehler as exc:
        return PlainTextResponse(str(exc), 500)
    anime_katalog.hinzufuegen(slug)
    return FileResponse(clip, media_type="video/mp4")


@app.get("/api/anime/katalog")
async def api_katalog(request: Request):
    await run_in_threadpool(_zugriff, request)
    return anime_katalog.status()
