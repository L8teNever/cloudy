"""Dateien im Browser: Freigaben ansehen, öffnen, herunterladen, hochladen, umbenennen, löschen.

Läuft als zweite Webseite im selben Container (DATEIEN_PORT, Standard 8092) und hält sich an dieselben Regeln
wie das Netzlaufwerk: pro Freigabe und Gerät offen, mit Passwort oder unsichtbar. Das Gerät wird an seiner
IP erkannt (Geräteliste aus cloudy), angemeldet wird mit den Benutzern aus „Freigaben“.
"""
import asyncio
import hashlib
import hmac
import mimetypes
import os
import secrets
import shutil
import stat
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config, freigaben, mediathek, mediathek_db, mediathek_providers, medien, pakete, pool
from .freigaben import Freigabe

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.globals["static_v"] = config.static_version()
COOKIE = "speicher_dateien"
MAX_AGE = 30 * 86400
CHUNK = 1024 * 1024
VERBOTEN = set('/\\\0')
# Im Browser direkt anzeigen; alles andere wird heruntergeladen. HTML/SVG nie aktiv (sonst Skripte im Kontext der Seite).
INLINE = ("image/", "video/", "audio/", "text/plain", "application/pdf")
AKTIV = ("text/html", "image/svg+xml", "application/xhtml+xml", "text/xml", "application/xml")


def _fmt_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return (f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}").replace(".", ",")
        n /= 1000
    return ""


templates.env.filters["bytes"] = _fmt_bytes
templates.env.filters["zeit"] = lambda ts: time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else ""

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


class Fehler(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status, self.text = status, text


# ---------- Sitzung: Cookie "benutzer.zeit.zufall.signatur" (benutzer leer = nicht angemeldet) ----------

def _key() -> bytes:
    return hashlib.sha256(b"dateien:" + auth._key()).digest()


def _sign(payload: str) -> str:
    return hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def make_session(benutzer: str = "") -> str:
    payload = f"{benutzer}.{int(time.time())}.{secrets.token_urlsafe(12)}"
    return f"{payload}.{_sign(payload)}"


def read_session(value: str | None) -> tuple[str, str] | None:
    """(benutzer, cookie) oder None, wenn ungültig/abgelaufen."""
    if not value or value.count(".") != 3:
        return None
    benutzer, ts, nonce, sig = value.split(".")
    if not hmac.compare_digest(sig, _sign(f"{benutzer}.{ts}.{nonce}")):
        return None
    try:
        if time.time() - int(ts) > MAX_AGE:
            return None
    except ValueError:
        return None
    if benutzer and benutzer not in freigaben.Einstellungen.load().benutzer:
        benutzer = ""  # Benutzer inzwischen gelöscht
    return benutzer, value


def csrf_token(session: str) -> str:
    return hmac.new(_key(), b"csrf:" + session.encode(), hashlib.sha256).hexdigest()


def _set_cookie(resp: Response, value: str) -> None:
    resp.set_cookie(COOKIE, value, max_age=MAX_AGE, httponly=True, samesite="lax")


@app.middleware("http")
async def security(request: Request, call_next):
    sess = read_session(request.cookies.get(COOKIE))
    neu = None
    if not sess:
        neu = make_session()
        sess = ("", neu)
    request.state.benutzer, request.state.session = sess
    request.state.geraet_benutzer = ""
    e = freigaben.Einstellungen.load()
    if e.geraet_login:
        key = await run_in_threadpool(freigaben.key_fuer_ip, _ip(request))
        request.state.geraet_benutzer = e.geraet_benutzer(key)
        if not request.state.benutzer:
            # Angemeldetes Gerät: gilt wie angemeldet, auch ohne Cookie
            request.state.benutzer = request.state.geraet_benutzer
    request.state.recht = e.recht(request.state.benutzer) if request.state.benutzer else "voll"
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return Response("Fremde Herkunft", 403)
        if neu:  # ohne gültiges Cookie gibt es auch kein gültiges CSRF-Token
            return _antwort(request, Fehler(403, "Sitzung abgelaufen – bitte Seite neu laden"))
    try:
        response = await call_next(request)
    except Fehler as exc:
        response = _antwort(request, exc)
    if neu:
        _set_cookie(response, neu)
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if not request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("Content-Security-Policy", (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; font-src 'self'; img-src 'self' data:; "
        "media-src 'self'; frame-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'self'"
    ))
    return response


def _antwort(request: Request, exc: Fehler) -> Response:
    if request.url.path.startswith("/api/") or request.method == "PUT":
        return JSONResponse({"fehler": exc.text}, exc.status)
    return _page(request, "dateien_fehler.html", status=exc.status, text=exc.text)


@app.exception_handler(Fehler)
async def _fehler_handler(request: Request, exc: Fehler):
    return _antwort(request, exc)


def _page(request: Request, vorlage: str, status: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("nav", "ordner")
    ctx.update(
        request=request,
        benutzer=request.state.benutzer,
        recht=request.state.recht,
        recht_text=freigaben.RECHTE_TEXT,
        csrf=csrf_token(request.state.session),
        ok=ctx.get("ok") or request.query_params.get("ok", ""),
        fehler=ctx.get("fehler") or request.query_params.get("fehler", ""),
    )
    return templates.TemplateResponse(request, vorlage, ctx, status_code=status)


def _csrf(request: Request, token: str) -> None:
    if not token or not hmac.compare_digest(csrf_token(request.state.session), token):
        raise Fehler(403, "Sitzung abgelaufen – bitte Seite neu laden")


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


# ---------- Zugriff ----------

@dataclass
class Zugang:
    f: Freigabe
    root: Path
    schreiben: bool  # neue Dateien/Ordner anlegen
    aendern: bool = True  # umbenennen, löschen, ersetzen


def sichtbar(request: Request) -> list[tuple[Freigabe, bool]]:
    """Freigaben, die dieses Gerät sieht: (Freigabe, darf rein)."""
    e = freigaben.Einstellungen.load()
    if not e.an:
        return []
    key = freigaben.key_fuer_ip(_ip(request))
    out = []
    for f in e.freigaben:
        regel = freigaben.regel_fuer(f, key)
        if request.state.benutzer and f.rechte.get(request.state.benutzer) == "gesperrt":
            continue  # ausgesperrt: Freigabe gar nicht zeigen
        if regel != "versteckt":
            out.append((f, freigaben.darf(f, regel, request.state.benutzer, e.benutzer)))
    return out


def zugang(request: Request, name: str) -> Zugang:
    e = freigaben.Einstellungen.load()
    try:
        f = e.get(name)
    except freigaben.FreigabeError:
        f = None
    regel = freigaben.regel_fuer(f, freigaben.key_fuer_ip(_ip(request))) if f else "versteckt"
    if not e.an or regel == "versteckt" or (f and request.state.benutzer and f.rechte.get(request.state.benutzer) == "gesperrt"):
        raise Fehler(404, "Diesen Ordner gibt es nicht")
    if not freigaben.darf(f, regel, request.state.benutzer, e.benutzer):
        if request.state.benutzer:
            raise Fehler(403, f"{request.state.benutzer} darf nicht in „{f.name}“")
        raise Fehler(401, "Für diesen Ordner musst du dich anmelden")
    if str(pool.paths()["pool"]) not in pool.mounts():
        raise Fehler(503, "Der Speicher ist gerade nicht eingehängt")
    recht = e.recht_fuer(f, request.state.benutzer) if request.state.benutzer else "voll"
    schreiben = not f.nur_lesen and recht in ("voll", "erstellen")
    return Zugang(f, freigaben.share_path(f), schreiben, schreiben and recht == "voll")


def check_name(name: str) -> str:
    name = name.strip()
    if not name or name in (".", "..") or any(c in VERBOTEN for c in name) or len(name.encode()) > 255:
        raise Fehler(400, "Ungültiger Name")
    if name == ".mergerfs":
        raise Fehler(400, "Dieser Name ist reserviert")
    return name


def ziel(z: Zugang, rel: str) -> Path:
    """Pfad in der Freigabe auflösen – nie außerhalb (auch nicht über Symlinks)."""
    parts = [p for p in rel.split("/") if p]
    if any(p in (".", "..") or "\0" in p or "\\" in p for p in parts):
        raise Fehler(400, "Ungültiger Pfad")
    root = z.root.resolve()
    path = (root.joinpath(*parts)).resolve()
    if path != root and root not in path.parents:
        raise Fehler(403, "Ungültiger Pfad")
    if parts and parts[0] == ".mergerfs" and z.root == pool.paths()["pool"]:
        raise Fehler(404, "Nicht gefunden")
    return path


def _besitzer(path: Path) -> None:
    """Neue Dateien gehören wie beim Netzlaufwerk dem Speicher-Benutzer."""
    try:
        os.chown(path, int(os.environ.get("SPEICHER_UID", "1000")), int(os.environ.get("SPEICHER_GID", "1000")))
    except (OSError, ValueError):
        pass


def _url(name: str, rel: str = "") -> str:
    return "/d/" + quote(name) + ("/" + quote(rel.strip("/")) if rel.strip("/") else "")


def _zurueck(name: str, rel: str, ok: str = "", fehler: str = "") -> RedirectResponse:
    q = f"?ok={quote(ok)}" if ok else (f"?fehler={quote(fehler)}" if fehler else "")
    return RedirectResponse(_url(name, rel) + q, 303)


# ---------- Seiten ----------

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def start(request: Request):
    e = freigaben.Einstellungen.load()
    liste = await run_in_threadpool(sichtbar, request)
    gesperrt = any(not darf for _, darf in liste)
    return _page(request, "dateien_start.html", liste=liste, gesperrt=gesperrt, hat_benutzer=bool(e.benutzer),
                 pool_da=str(pool.paths()["pool"]) in pool.mounts())


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, weiter: str = "/"):
    return _page(request, "dateien_login.html", weiter=_sicher(weiter))


def _sicher(weiter: str) -> str:
    return weiter if weiter.startswith(("/d/", "/?", "/streaming", "/medien", "/geraet")) or weiter == "/" else "/"


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    _csrf(request, str(form.get("csrf", "")))
    name, pw, weiter = str(form.get("name", "")).strip(), str(form.get("passwort", "")), _sicher(str(form.get("weiter", "/")))
    ip = _ip(request) or "?"
    if auth.throttled("dateien:" + ip):
        return _page(request, "dateien_login.html", weiter=weiter, name=name, fehler="Zu viele Versuche – in 10 Minuten nochmal")
    if not await run_in_threadpool(freigaben.check_login, name, pw):
        auth.failed("dateien:" + ip)
        fehler = "Benutzer oder Passwort falsch"
        user = freigaben.benutzer_fuer(name)
        if user and not freigaben.web_login_moeglich(user):
            fehler = "Für diesen Benutzer einmal auf der Speicher-Seite das Passwort neu setzen"
        return _page(request, "dateien_login.html", weiter=weiter, name=name, fehler=fehler)
    resp = RedirectResponse(weiter, 303)
    _set_cookie(resp, make_session(freigaben.benutzer_fuer(name)))
    return resp


# ---------- Gerät anmelden (Ordner „Anmelden“ im Netzlaufwerk führt hierher) ----------

def _geraet_info(request: Request) -> dict:
    e = freigaben.Einstellungen.load()
    ip = _ip(request)
    key = freigaben.key_fuer_ip(ip)
    name = next((g.name for g in freigaben.geraete()[0] if g.key == key), "") if key else ""
    user = e.geraet_benutzer(key)
    return {"ip": ip, "geraet_name": name or ip, "geraet_user": user, "geraet_recht": e.recht(user) if user else "voll",
            "hat_benutzer": bool(e.benutzer)}


@app.get("/geraet", response_class=HTMLResponse)
async def geraet_seite(request: Request):
    info = await run_in_threadpool(_geraet_info, request)
    return _page(request, "dateien_geraet.html", nav="geraet", **info)


@app.post("/geraet")
async def geraet_anmelden(request: Request):
    form = await request.form()
    _csrf(request, str(form.get("csrf", "")))
    name, pw = str(form.get("name", "")).strip(), str(form.get("passwort", ""))
    ip = _ip(request) or "?"
    info = await run_in_threadpool(_geraet_info, request)
    if auth.throttled("dateien:" + ip):
        return _page(request, "dateien_geraet.html", nav="geraet", name=name, fehler="Zu viele Versuche – in 10 Minuten nochmal", **info)
    if not await run_in_threadpool(freigaben.check_login, name, pw):
        auth.failed("dateien:" + ip)
        return _page(request, "dateien_geraet.html", nav="geraet", name=name, fehler="Benutzer oder Passwort falsch", **info)
    user = freigaben.benutzer_fuer(name)
    try:
        await run_in_threadpool(freigaben.geraet_anmelden, ip, user)
        await run_in_threadpool(freigaben.dienste.abgleichen)
    except freigaben.FreigabeError as exc:
        return _page(request, "dateien_geraet.html", nav="geraet", name=name, fehler=str(exc), **info)
    resp = RedirectResponse("/geraet?ok=" + quote(f"Dieses Gerät ist jetzt als {user} angemeldet"), 303)
    _set_cookie(resp, make_session(user))
    return resp


@app.post("/geraet/abmelden")
async def geraet_abmelden(request: Request):
    form = await request.form()
    _csrf(request, str(form.get("csrf", "")))
    key = await run_in_threadpool(freigaben.key_fuer_ip, _ip(request))
    if key:
        await run_in_threadpool(freigaben.geraet_abmelden, key)
        await run_in_threadpool(freigaben.dienste.abgleichen)
    resp = RedirectResponse("/geraet?ok=" + quote("Gerät abgemeldet"), 303)
    _set_cookie(resp, make_session())
    return resp


@app.post("/logout")
async def logout(request: Request):
    form = await request.form()
    _csrf(request, str(form.get("csrf", "")))
    resp = RedirectResponse("/", 303)
    _set_cookie(resp, make_session())
    return resp


@dataclass
class Eintrag:
    name: str
    ordner: bool
    groesse: int
    geaendert: float
    icon: str


def _icon(name: str, ordner: bool) -> str:
    if ordner:
        return "folder"
    typ = mimetypes.guess_type(name)[0] or ""
    if typ.startswith("image/"):
        return "image"
    if typ.startswith("video/"):
        return "movie"
    if typ.startswith("audio/"):
        return "music_note"
    if typ == "application/pdf":
        return "picture_as_pdf"
    if typ.startswith("text/"):
        return "description"
    if name.lower().endswith((".zip", ".rar", ".7z", ".tar", ".gz")):
        return "folder_zip"
    return "draft"


_TEMP_TAGS = (".hochladen-", ".tvfix-", ".raw-", ".tvcopy-", ".conv-", ".mp4tmp-")


def auflisten(path: Path, ist_pool_wurzel: bool) -> list[Eintrag]:
    out = []
    with os.scandir(path) as it:
        for entry in it:
            if ist_pool_wurzel and entry.name == ".mergerfs":
                continue
            if entry.name.startswith(".") and any(tag in entry.name for tag in _TEMP_TAGS):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            ordner = stat.S_ISDIR(st.st_mode)
            out.append(Eintrag(entry.name, ordner, 0 if ordner else st.st_size, st.st_mtime, _icon(entry.name, ordner)))
    return sorted(out, key=lambda x: (not x.ordner, x.name.casefold()))


def _medientyp(path: Path) -> str:
    typ = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if typ in AKTIV:
        return "text/plain"
    return typ


@app.get("/d/{name}", response_class=HTMLResponse)
@app.get("/d/{name}/{rel:path}")
async def ansehen(request: Request, name: str, rel: str = ""):
    try:
        z = zugang(request, name)
    except Fehler as exc:
        if exc.status == 401:
            return RedirectResponse("/login?weiter=" + quote(_url(name, rel)), 303)
        raise
    path = ziel(z, rel)
    if path.is_dir():
        if request.query_params.get("zip") == "1":
            return _zip(path, path.name if rel else z.f.name)
        eintraege = await run_in_threadpool(auflisten, path, z.root == pool.paths()["pool"] and path == z.root.resolve())
        teile = [p for p in rel.split("/") if p]
        pfad = [(teile[i], "/".join(teile[: i + 1])) for i in range(len(teile))]
        return _page(request, "dateien_ordner.html", f=z.f, rel="/".join(teile), pfad=pfad, eintraege=eintraege,
                     schreiben=z.schreiben, aendern=z.aendern, ist_paket=pakete.ist_paket, url=_url, oben=_url(name, "/".join(teile[:-1])) if teile else "/")
    if not path.is_file():
        raise Fehler(404, "Nicht gefunden")
    typ = _medientyp(path)
    inline = request.query_params.get("dl") != "1" and typ.startswith(INLINE)
    resp = FileResponse(path, media_type=typ, filename=path.name, content_disposition_type="inline" if inline else "attachment")
    resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox"
    resp.headers["Cache-Control"] = "private, no-cache"
    return resp


def _zip(path: Path, name: str) -> StreamingResponse:
    """Ordner als ZIP herunterladen – wird beim Senden erzeugt (kein Zwischenspeicher, unkomprimiert)."""

    class Puffer:
        def __init__(self):
            self.teile: list[bytes] = []
            self.pos = 0

        def write(self, b):
            self.teile.append(bytes(b))
            self.pos += len(b)
            return len(b)

        def tell(self):
            return self.pos

        def flush(self):
            pass

        def leeren(self) -> bytes:
            data, self.teile = b"".join(self.teile), []
            return data

    def erzeugen():
        buf = Puffer()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames.sort()
                for fn in sorted(filenames):
                    full = Path(dirpath) / fn
                    if full.is_symlink() or not full.is_file() or fn == ".mergerfs":
                        continue
                    if fn.startswith(".") and any(tag in fn for tag in _TEMP_TAGS):
                        continue
                    arc = str(full.relative_to(path))
                    try:
                        zi = zipfile.ZipInfo.from_file(full, arc)
                        with open(full, "rb") as src, zf.open(zi, "w", force_zip64=True) as dst:
                            while chunk := src.read(CHUNK):
                                dst.write(chunk)
                                yield buf.leeren()
                    except OSError:
                        continue
                    yield buf.leeren()
        yield buf.leeren()

    return StreamingResponse(erzeugen(), media_type="application/zip", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name + '.zip')}",
    })


# ---------- Ändern ----------

async def _aktion(request: Request, name: str) -> tuple[Zugang, dict]:
    form = await request.form()
    _csrf(request, str(form.get("csrf", "")))
    z = zugang(request, name)
    if not z.schreiben:
        raise Fehler(403, f"„{z.f.name}“ ist nur zum Lesen freigegeben")
    return z, {k: str(v) for k, v in form.items()}


@app.post("/aktion/{name}/ordner")
async def neuer_ordner(request: Request, name: str):
    z, f = await _aktion(request, name)
    rel = f.get("pfad", "")
    try:
        neu = ziel(z, rel) / check_name(f.get("neu", ""))
        neu.mkdir()
        _besitzer(neu)
    except FileExistsError:
        return _zurueck(name, rel, fehler="Gibt es schon")
    except Fehler as exc:
        return _zurueck(name, rel, fehler=exc.text)
    except OSError as exc:
        return _zurueck(name, rel, fehler=f"Ging nicht: {exc.strerror}")
    return _zurueck(name, rel, ok=f"Ordner „{neu.name}“ angelegt")


@app.post("/aktion/{name}/umbenennen")
async def umbenennen(request: Request, name: str):
    z, f = await _aktion(request, name)
    rel = f.get("pfad", "")
    ordner = "/".join(rel.split("/")[:-1])
    if not z.aendern:
        return _zurueck(name, ordner, fehler="Du darfst hier nur Neues anlegen, nichts umbenennen")
    try:
        alt = ziel(z, rel)
        if alt == z.root.resolve():
            raise Fehler(400, "Die Freigabe selbst kann man hier nicht umbenennen")
        neu = alt.parent / check_name(f.get("neu", ""))
        if neu.exists() and neu.name.casefold() != alt.name.casefold():
            raise Fehler(409, f"„{neu.name}“ gibt es schon")
        alt.rename(neu)
    except Fehler as exc:
        return _zurueck(name, ordner, fehler=exc.text)
    except OSError as exc:
        return _zurueck(name, ordner, fehler=f"Ging nicht: {exc.strerror}")
    return _zurueck(name, ordner, ok=f"Umbenannt in „{neu.name}“")


@app.post("/aktion/{name}/loeschen")
async def loeschen(request: Request, name: str):
    z, f = await _aktion(request, name)
    rel = f.get("pfad", "")
    ordner = "/".join(rel.split("/")[:-1])
    if not z.aendern:
        return _zurueck(name, ordner, fehler="Du darfst hier nur Neues anlegen, nichts löschen")
    try:
        path = ziel(z, rel)
        if path == z.root.resolve():
            raise Fehler(400, "Die Freigabe selbst kann man hier nicht löschen")
        if path.is_dir() and not path.is_symlink():
            await run_in_threadpool(shutil.rmtree, path)
        else:
            path.unlink()
    except Fehler as exc:
        return _zurueck(name, ordner, fehler=exc.text)
    except OSError as exc:
        return _zurueck(name, ordner, fehler=f"Ging nicht: {exc.strerror}")
    return _zurueck(name, ordner, ok=f"„{path.name}“ gelöscht")


@app.put("/api/hochladen/{name}/{rel:path}")
async def hochladen(request: Request, name: str, rel: str):
    """Eine Datei hochladen. Der Inhalt kommt direkt als Body und wird ohne Zwischenspeicher geschrieben."""
    _csrf(request, request.headers.get("x-csrf", ""))
    z = zugang(request, name)
    if not z.schreiben:
        raise Fehler(403, f"„{z.f.name}“ ist nur zum Lesen freigegeben")
    teile = [p for p in rel.split("/") if p]
    if not teile:
        raise Fehler(400, "Dateiname fehlt")
    dateiname = check_name(teile[-1])
    ordner = ziel(z, "/".join(teile[:-1]))
    # Unterordner beim Hochladen ganzer Ordner anlegen
    if not ordner.is_dir():
        ordner.mkdir(parents=True, exist_ok=True)
        p = ordner
        while p != z.root.resolve() and z.root.resolve() in p.parents:
            _besitzer(p)
            p = p.parent
    path = ordner / dateiname
    if path.exists() and (request.query_params.get("ersetzen") != "1" or not z.aendern):
        raise Fehler(409, f"„{dateiname}“ gibt es schon")
    if path.is_dir():
        raise Fehler(409, f"„{dateiname}“ ist ein Ordner")
    tmp = ordner / f".{dateiname[:200]}.hochladen-{secrets.token_hex(4)}"
    try:
        fh = await run_in_threadpool(open, tmp, "wb")
        try:
            async for chunk in request.stream():
                if chunk:
                    await run_in_threadpool(fh.write, chunk)
        finally:
            await run_in_threadpool(fh.close)
        _besitzer(tmp)
        os.replace(tmp, path)
        medien.anmelden(path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise Fehler(507 if exc.errno == 28 else 500, "Kein Platz mehr" if exc.errno == 28 else f"Ging nicht: {exc.strerror}") from exc
    except BaseException:
        tmp.unlink(missing_ok=True)  # Abbruch im Browser
        raise
    return {"ok": True, "name": dateiname}


@app.put("/api/paket/{name}")
@app.put("/api/paket/{name}/{rel:path}")
async def paket_hochladen(request: Request, name: str, rel: str = ""):
    """Viele Dateien als ein TAR-Paket (packt der Browser) – wird beim Empfang direkt in den Ordner entpackt."""
    _csrf(request, request.headers.get("x-csrf", ""))
    z = zugang(request, name)
    if not z.schreiben:
        raise Fehler(403, f"„{z.f.name}“ ist nur zum Lesen freigegeben")
    ersetzen = request.query_params.get("ersetzen") == "1"
    if ersetzen and not z.aendern:
        raise Fehler(403, "Du darfst hier nur Neues anlegen, nichts ersetzen")
    ordner = ziel(z, rel)
    if not ordner.is_dir():
        await run_in_threadpool(lambda: ordner.mkdir(parents=True, exist_ok=True))
        p = ordner
        while p != z.root.resolve() and z.root.resolve() in p.parents:
            _besitzer(p)
            p = p.parent
    schreiber = pakete.Schreiber(z.root, ordner, ersetzen, _besitzer, medien.anmelden)
    rohr = pakete.Rohr()
    loop = asyncio.get_running_loop()
    arbeit = loop.run_in_executor(None, pakete.tar_entpacken, rohr, schreiber)

    def _rein(chunk) -> bool:
        """Chunk in die Warteschlange; False, wenn das Entpacken schon vorbei ist (Fehler)."""
        while not arbeit.done():
            try:
                rohr.q.put(chunk, timeout=0.5)
                return True
            except Exception:  # noqa: BLE001 – queue.Full
                continue
        return False

    try:
        async for chunk in request.stream():
            if chunk and not await run_in_threadpool(_rein, chunk):
                break
    except BaseException:
        await run_in_threadpool(_rein, None)
        try:
            await arbeit
        except Exception:  # noqa: BLE001
            pass
        raise
    await run_in_threadpool(_rein, None)
    try:
        erg = await arbeit
    except pakete.PaketFehler as exc:
        raise Fehler(400, str(exc)) from exc
    except OSError as exc:
        raise Fehler(507 if exc.errno == 28 else 500, "Kein Platz mehr" if exc.errno == 28 else f"Ging nicht: {exc.strerror}") from exc
    return {"ok": erg.ok, "uebersprungen": erg.uebersprungen, "fehler": erg.fehler[:50]}


@app.post("/aktion/{name}/entpacken")
async def entpacken(request: Request, name: str):
    z, f = await _aktion(request, name)
    rel = f.get("pfad", "")
    ordner = "/".join(rel.split("/")[:-1])
    try:
        path = ziel(z, rel)
        if not path.is_file() or not pakete.ist_paket(path.name):
            raise Fehler(400, "Das ist kein ZIP- oder TAR-Paket")
        basis = pakete.ordner_name(path.name)
        dest = path.parent / basis
        n = 2
        while dest.exists():
            dest = path.parent / f"{basis} ({n})"
            n += 1
        dest.mkdir()
        _besitzer(dest)
        schreiber = pakete.Schreiber(z.root, dest, False, _besitzer, medien.anmelden)
        erg = await run_in_threadpool(pakete.entpacken, path, schreiber)
    except Fehler as exc:
        return _zurueck(name, ordner, fehler=exc.text)
    except pakete.PaketFehler as exc:
        try:
            dest.rmdir()
        except (OSError, UnboundLocalError):
            pass
        return _zurueck(name, ordner, fehler=str(exc))
    except OSError as exc:
        return _zurueck(name, ordner, fehler=f"Ging nicht: {exc.strerror}")
    text = f"{erg.ok} Dateien nach „{dest.name}“ entpackt"
    if erg.fehler:
        return _zurueck(name, ordner, fehler=text + f", {len(erg.fehler)} nicht: " + " · ".join(erg.fehler[:3]))
    return _zurueck(name, ordner, ok=text)


# ---------- Zugriffsschutz für Streaming & Medien ----------

def _allowed_shares(request: Request, write_only: bool = False) -> list[tuple[Freigabe, Path]]:
    e = freigaben.Einstellungen.load()
    if not e.an:
        return []
    key = freigaben.key_fuer_ip(_ip(request))
    out: list[tuple[Freigabe, Path]] = []
    for f in e.freigaben:
        regel = freigaben.regel_fuer(f, key)
        if regel == "versteckt":
            continue
        if not freigaben.darf(f, regel, request.state.benutzer, e.benutzer):
            continue
        if write_only and (f.nur_lesen or _recht(request, e, f) not in ("voll", "erstellen")):
            continue
        out.append((f, freigaben.share_path(f)))
    return out


def _recht(request: Request, e, f: Freigabe) -> str:
    return e.recht_fuer(f, request.state.benutzer) if request.state.benutzer else "voll"


def _bestes_recht(request: Request, e, shares: list[Freigabe]) -> str:
    rs = {_recht(request, e, f) for f in shares if not f.nur_lesen}
    return next((r for r in ("voll", "erstellen") if r in rs), "lesen")


def _check_mediathek_access(
    request: Request,
    folder_rel: str = "",
    *,
    write: bool = False,
    require_pool: bool = False,
) -> bool:
    """Prüft Zugriff anhand der Freigabe-Regeln. Gibt True zurück, wenn Schreibrecht besteht."""
    e = freigaben.Einstellungen.load()
    if not e.an:
        raise Fehler(404, "Freigaben sind ausgeschaltet")
    key = freigaben.key_fuer_ip(_ip(request))
    folder_norm = folder_rel.strip("/")

    covering: list[tuple[Freigabe, str]] = []
    for f in e.freigaben:
        fo = (f.ordner or "").strip("/")
        if fo == "" or (folder_norm and (folder_norm == fo or folder_norm.startswith(fo + "/"))):
            covering.append((f, freigaben.regel_fuer(f, key)))

    if covering:
        visible = [(f, r) for f, r in covering if r != "versteckt"]
        if not visible:
            raise Fehler(404, "Für dieses Gerät nicht freigegeben")
        allowed = [f for f, r in visible if freigaben.darf(f, r, request.state.benutzer, e.benutzer)]
        if not allowed:
            if not request.state.benutzer:
                raise Fehler(401, "Für diesen Bereich musst du dich anmelden")
            raise Fehler(403, f"{request.state.benutzer} darf nicht auf diesen Bereich zugreifen")
        can_write = any(not f.nur_lesen and _recht(request, e, f) in ("voll", "erstellen") for f in allowed)
        request.state.bereich_recht = _bestes_recht(request, e, allowed)
        if write and not can_write:
            raise Fehler(403, "Nur zum Lesen freigegeben")
    elif e.freigaben:
        vis = sichtbar(request)
        allowed_shares = [f for f, ok in vis if ok]
        if not allowed_shares:
            if vis and not request.state.benutzer:
                raise Fehler(401, "Bitte anmelden")
            raise Fehler(403 if vis else 404, "Für dieses Gerät nicht freigegeben")
        can_write = any(not f.nur_lesen and _recht(request, e, f) in ("voll", "erstellen") for f in allowed_shares)
        request.state.bereich_recht = _bestes_recht(request, e, allowed_shares)
        if write and not can_write:
            raise Fehler(403, "Nur zum Lesen freigegeben")
    else:
        if e.benutzer and not request.state.benutzer:
            raise Fehler(401, "Bitte anmelden")
        can_write = True

    if require_pool and not mediathek.pool_mounted():
        raise Fehler(503, "Der Speicher ist gerade nicht eingehängt")
    return can_write


def _ensure_pool_rel_allowed(request: Request, pool_rel: str, *, allow_temp: bool = False) -> Path:
    try:
        target = mediathek.safe_pool_path(pool_rel)
    except ValueError as exc:
        raise Fehler(400, str(exc)) from exc
    if not target.is_file():
        raise Fehler(404, "Datei nicht gefunden")
    cfg = mediathek_db.Einstellungen.load()
    if allow_temp:
        tdir = mediathek.temp_dir().resolve()
        if tdir in target.parents:
            _check_mediathek_access(request, cfg.temp_ordner, require_pool=True)
            return target
    for _, share_root in _allowed_shares(request):
        sr = share_root.resolve()
        if target == sr or sr in target.parents:
            return target
    raise Fehler(403, "Kein Zugriff auf diese Datei")


# ---------- Seiten: Streaming & Medien ----------

@app.get("/streaming", response_class=HTMLResponse)
async def streaming_page(request: Request):
    cfg = mediathek_db.Einstellungen.load()
    try:
        can_write = await run_in_threadpool(_check_mediathek_access, request, cfg.streaming_ordner, write=False, require_pool=False)
    except Fehler as exc:
        if exc.status == 401:
            return RedirectResponse("/login?weiter=" + quote("/streaming"), 303)
        raise
    fe = freigaben.Einstellungen.load()
    return _page(
        request,
        "streaming.html",
        nav="streaming",
        can_write=can_write,
        pool_da=mediathek.pool_mounted(),
        cfg=cfg,
        has_tmdb_key=bool(cfg.effective_tmdb_key()),
        dlna_an=fe.dlna_an,
        dlna_name=fe.dlna_name,
    )


@app.get("/medien", response_class=HTMLResponse)
async def medien_page(request: Request):
    cfg = mediathek_db.Einstellungen.load()
    try:
        can_write = await run_in_threadpool(_check_mediathek_access, request, cfg.medien_ordner, write=False, require_pool=False)
    except Fehler as exc:
        if exc.status == 401:
            return RedirectResponse("/login?weiter=" + quote("/medien"), 303)
        raise
    fe = freigaben.Einstellungen.load()
    return _page(
        request,
        "medien.html",
        nav="medien",
        can_write=can_write,
        pool_da=mediathek.pool_mounted(),
        cfg=cfg,
        dlna_an=fe.dlna_an,
        dlna_name=fe.dlna_name,
    )


# ---------- API: Streaming & Mediathek ----------

@app.get("/api/mediathek/bibliothek")
async def api_mediathek_bibliothek(request: Request):
    cfg = mediathek_db.Einstellungen.load()
    await run_in_threadpool(_check_mediathek_access, request, cfg.streaming_ordner)

    def _load():
        if mediathek.pool_mounted():
            mediathek_db.sync_disk(mediathek.pool_root())
        return {
            "items": mediathek_db.list_local_library(),
            "tv": mediathek_db.list_tv_items(),
            "pool_da": mediathek.pool_mounted(),
            "streaming_ordner": cfg.streaming_ordner,
            "temp_ordner": cfg.temp_ordner,
        }

    return await run_in_threadpool(_load)


@app.get("/api/mediathek/suche")
async def api_mediathek_suche(request: Request, q: str = ""):
    cfg = mediathek_db.Einstellungen.load()
    await run_in_threadpool(_check_mediathek_access, request, cfg.streaming_ordner)
    query = q.strip()
    if not query:
        return {"results": [], "errors": [], "local": []}

    def _run_search():
        res = mediathek_providers.search(query, cfg.quellen, cfg.effective_tmdb_key())
        refs = [r["ref"] for r in res["results"] if r.get("ref")]
        avail = mediathek_db.local_availability_map(refs) if refs else {}
        for r in res["results"]:
            r["local"] = avail.get(r["ref"], {"ready_count": 0, "busy_count": 0, "total_count": 0})
        nq = mediathek_providers.norm(query)
        local_hits = [
            t for t in mediathek_db.list_local_library()
            if nq in mediathek_providers.norm(t.get("name", ""))
        ]
        res["local"] = local_hits
        return res

    return await run_in_threadpool(_run_search)


@app.get("/api/mediathek/details")
async def api_mediathek_details(request: Request, ref: str = ""):
    cfg = mediathek_db.Einstellungen.load()
    await run_in_threadpool(_check_mediathek_access, request, cfg.streaming_ordner)
    if not ref or ":" not in ref:
        raise Fehler(400, "Ungültige Referenz")

    def _load():
        local = mediathek_db.get_title_by_ref(ref)
        online = None
        err = ""
        try:
            online = mediathek_providers.full(ref, cfg.effective_tmdb_key())
            if local:
                # Cached Metadaten still aktualisieren, falls Title schon in DB
                mediathek_db.upsert_title(online)
                local = mediathek_db.get_title_by_ref(ref)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
        if not online and not local:
            raise Fehler(404, err or "Titel nicht gefunden")
        if not online and local:
            meta_extra = local.get("meta") or {}
            online = {
                "ref": local["ref"],
                "provider": local["provider"],
                "type": local["type"],
                "anime": local["anime"],
                "name": local["name"],
                "year": local["year"],
                "poster": local["poster"],
                "backdrop": local["backdrop"],
                "summary": local["summary"],
                "genres": local["genres"],
                "watch": local["watch"],
                **meta_extra,
            }
        return {"item": online, "local": local, "error": err}

    return await run_in_threadpool(_load)


@app.get("/api/mediathek/staffel")
async def api_mediathek_staffel(request: Request, ref: str = "", s: int = 1):
    cfg = mediathek_db.Einstellungen.load()
    await run_in_threadpool(_check_mediathek_access, request, cfg.streaming_ordner)
    if not ref or ":" not in ref:
        raise Fehler(400, "Ungültige Referenz")
    try:
        eps = await run_in_threadpool(mediathek_providers.season_episodes, ref, max(1, s), cfg.effective_tmdb_key())
    except Exception as exc:  # noqa: BLE001
        raise Fehler(502, str(exc)) from exc
    return {"episodes": eps}


@app.get("/api/mediathek/bild")
async def api_mediathek_bild(request: Request, url: str = ""):
    if not mediathek_providers.is_allowed_image_url(url):
        raise Fehler(400, "Bildquelle nicht erlaubt")
    res = await run_in_threadpool(mediathek_providers.fetch_cached_image, url)
    if not res:
        raise Fehler(404, "Bild nicht verfügbar")
    data, ctype = res
    return Response(content=data, media_type=ctype, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/mediathek/einstellungen")
async def api_mediathek_einstellungen_get(request: Request):
    await run_in_threadpool(_check_mediathek_access, request, "")
    cfg = mediathek_db.Einstellungen.load()
    key = cfg.effective_tmdb_key()
    return {
        "has_tmdb_key": bool(key),
        "tmdb_key_hinweis": ("••••" + key[-4:]) if len(key) >= 4 else ("••••" if key else ""),
        "tmdb_env": bool(os.environ.get("TMDB_API_KEY")),
        "streaming_ordner": cfg.streaming_ordner,
        "medien_ordner": cfg.medien_ordner,
        "temp_ordner": cfg.temp_ordner,
        "video_ablauf_stunden": cfg.video_ablauf_stunden,
        "bild_ablauf_tage": cfg.bild_ablauf_tage,
        "auto_entfernen_nach_player": cfg.auto_entfernen_nach_player,
        "quellen": cfg.quellen,
    }


@app.post("/api/mediathek/einstellungen")
async def api_mediathek_einstellungen_post(request: Request):
    _csrf(request, request.headers.get("x-csrf", ""))
    await run_in_threadpool(_check_mediathek_access, request, "", write=True)
    body = await request.json()
    cfg = mediathek_db.Einstellungen.load()
    if "tmdb_key" in body:
        val = str(body.get("tmdb_key") or "").strip()
        if val != "__KEEP__":
            cfg.tmdb_key = val
    for field in ("streaming_ordner", "medien_ordner", "temp_ordner"):
        if field in body and str(body[field] or "").strip():
            try:
                setattr(cfg, field, freigaben.check_ordner(str(body[field])))
            except freigaben.FreigabeError as exc:
                raise Fehler(400, str(exc)) from exc
    if "video_ablauf_stunden" in body:
        cfg.video_ablauf_stunden = max(0, min(720, int(body["video_ablauf_stunden"] or 0)))
    if "bild_ablauf_tage" in body:
        cfg.bild_ablauf_tage = max(0, min(365, int(body["bild_ablauf_tage"] or 0)))
    if "auto_entfernen_nach_player" in body:
        cfg.auto_entfernen_nach_player = bool(body["auto_entfernen_nach_player"])
    if "quellen" in body and isinstance(body["quellen"], list):
        q = [str(x) for x in body["quellen"] if x in ("tmdb", "tvmaze", "anilist")]
        if q:
            cfg.quellen = q
    await run_in_threadpool(cfg.save)
    return {"ok": True}


@app.put("/api/mediathek/hochladen")
async def api_mediathek_hochladen(request: Request):
    """Streamt ein Video in die Mediathek und wandelt Nicht-MP4/fragmentierte Videos im Hintergrund um."""
    _csrf(request, request.headers.get("x-csrf", ""))
    cfg = mediathek_db.Einstellungen.load()
    await run_in_threadpool(_check_mediathek_access, request, cfg.streaming_ordner, write=True, require_pool=True)

    qp = request.query_params
    ref = str(qp.get("ref", "")).strip()
    dateiname = check_name(str(qp.get("dateiname", "")).strip() or "video.mp4")
    ext = Path(dateiname).suffix.lower()
    if ext not in mediathek.VIDEO_EXTS:
        raise Fehler(400, f"Dateiformat „{ext or 'ohne Endung'}“ ist kein unterstütztes Video")

    fallback_name = str(qp.get("name", "")).strip() or Path(dateiname).stem
    typ = "movie" if qp.get("type") == "movie" else "show"
    year_raw = qp.get("year")
    year = int(year_raw) if year_raw and str(year_raw).isdigit() else None
    staffel = int(qp["staffel"]) if qp.get("staffel") and str(qp["staffel"]).isdigit() else None
    folge = int(qp["folge"]) if qp.get("folge") and str(qp["folge"]).isdigit() else None
    folgentitel = str(qp.get("folgentitel", "")).strip()
    ersetzen = qp.get("ersetzen") == "1"

    def _prepare():
        meta = None
        if ref and ":" in ref:
            existing = mediathek_db.get_title_by_ref(ref)
            if existing and (existing.get("meta") or {}).get("episodes"):
                meta = {**existing, **(existing.get("meta") or {})}
            else:
                try:
                    meta = mediathek_providers.full(ref, cfg.effective_tmdb_key())
                except Exception:  # noqa: BLE001
                    meta = existing
        if not meta:
            local_ref = ref or f"local:{mediathek_providers.norm(fallback_name)}:{year or ''}:{typ}"
            meta = {
                "ref": local_ref,
                "provider": local_ref.partition(":")[0],
                "type": typ,
                "anime": False,
                "name": fallback_name,
                "year": year,
                "poster": "",
                "backdrop": "",
                "summary": "",
                "genres": [],
                "watch": {},
            }
        titel_id = mediathek_db.upsert_title(meta)
        rel_pfad, _ = mediathek.streaming_target_rel(meta, staffel, folge, folgentitel)
        target_mp4 = mediathek.safe_pool_path(rel_pfad)
        if target_mp4.exists() and not ersetzen:
            raise Fehler(409, f"„{target_mp4.name}“ gibt es schon")
        target_mp4.parent.mkdir(parents=True, exist_ok=True)
        mediathek._chown_tree(target_mp4.parent, mediathek.pool_root())
        raw_path = target_mp4.parent / f".{target_mp4.stem[:140]}.raw-{secrets.token_hex(4)}{ext}"
        raw_rel = str(raw_path.relative_to(mediathek.pool_root()))
        return titel_id, rel_pfad, target_mp4, raw_path, raw_rel

    titel_id, rel_pfad, target_mp4, raw_path, raw_rel = await run_in_threadpool(_prepare)

    try:
        fh = await run_in_threadpool(open, raw_path, "wb")
        try:
            async for chunk in request.stream():
                if chunk:
                    await run_in_threadpool(fh.write, chunk)
        finally:
            await run_in_threadpool(fh.close)
        _besitzer(raw_path)

        def _register():
            info = mediathek.probe_video(raw_path)
            if not mediathek.needs_conversion(raw_path, info):
                os.replace(raw_path, target_mp4)
                fid = mediathek_db.register_file(
                    titel_id,
                    rel_pfad,
                    staffel=staffel if typ != "movie" else None,
                    folge=folge if typ != "movie" else None,
                    folgentitel=folgentitel,
                    roh_pfad="",
                    status="bereit",
                    groesse=target_mp4.stat().st_size,
                    dauer=info.get("duration"),
                    video_codec=info.get("video_codec") or "",
                    audio_codec=info.get("audio_codec") or "",
                )
                return fid, "bereit"
            fid = mediathek_db.register_file(
                titel_id,
                rel_pfad,
                staffel=staffel if typ != "movie" else None,
                folge=folge if typ != "movie" else None,
                folgentitel=folgentitel,
                roh_pfad=raw_rel,
                status="umwandlung",
                groesse=raw_path.stat().st_size,
                dauer=info.get("duration"),
                video_codec=info.get("video_codec") or "",
                audio_codec=info.get("audio_codec") or "",
            )
            medien.scan_anstossen()
            return fid, "umwandlung"

        file_id, status = await run_in_threadpool(_register)
    except OSError as exc:
        raw_path.unlink(missing_ok=True)
        raise Fehler(507 if exc.errno == 28 else 500, "Kein Platz mehr" if exc.errno == 28 else f"Ging nicht: {exc.strerror}") from exc
    except BaseException:
        raw_path.unlink(missing_ok=True)
        raise

    return {"ok": True, "file_id": file_id, "titel_id": titel_id, "rel_pfad": rel_pfad, "status": status}


@app.get("/api/mediathek/stream/{file_id}")
async def api_mediathek_stream(request: Request, file_id: int):
    cfg = mediathek_db.Einstellungen.load()
    await run_in_threadpool(_check_mediathek_access, request, cfg.streaming_ordner, require_pool=True)
    item = await run_in_threadpool(mediathek_db.get_file, file_id)
    if not item:
        raise Fehler(404, "Datei nicht gefunden")
    path = mediathek.safe_pool_path(item["rel_pfad"])
    if not path.is_file():
        raise Fehler(404, "Videodatei fehlt auf dem Speicher")
    resp = FileResponse(path, media_type="video/mp4", filename=path.name, content_disposition_type="inline")
    resp.headers["Cache-Control"] = "private, no-cache"
    return resp


@app.delete("/api/mediathek/datei/{file_id}")
async def api_mediathek_delete_file(request: Request, file_id: int):
    _csrf(request, request.headers.get("x-csrf", ""))
    cfg = mediathek_db.Einstellungen.load()
    await run_in_threadpool(_check_mediathek_access, request, cfg.streaming_ordner, write=True, require_pool=True)
    if getattr(request.state, "bereich_recht", "voll") != "voll":
        raise Fehler(403, f"{request.state.benutzer} darf hier nichts löschen")

    def _delete():
        item = mediathek_db.get_file(file_id)
        if not item:
            raise Fehler(404, "Eintrag nicht gefunden")
        for tv in mediathek_db.list_tv_items():
            if tv.get("datei_id") == file_id:
                mediathek.remove_staged(tv["id"])
        for rel in (item.get("rel_pfad"), item.get("roh_pfad")):
            if rel:
                try:
                    p = mediathek.safe_pool_path(rel)
                    p.unlink(missing_ok=True)
                    # Leere Staffel-/Titelordner bis Streaming-Basis aufräumen
                    base = mediathek.safe_pool_path(cfg.streaming_ordner).resolve()
                    parent = p.parent.resolve()
                    while parent != base and base in parent.parents:
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent
                except (ValueError, OSError):
                    pass
        mediathek_db.delete_file_record(file_id)

    await run_in_threadpool(_delete)
    return {"ok": True}


# ---------- API: Schnell-TV („Jetzt schauen“) ----------

@app.get("/api/tv/status")
async def api_tv_status(request: Request):
    cfg = mediathek_db.Einstellungen.load()
    await run_in_threadpool(_check_mediathek_access, request, cfg.temp_ordner)
    fe = freigaben.Einstellungen.load()
    items = await run_in_threadpool(mediathek_db.list_tv_items)
    return {
        "items": items,
        "temp_ordner": cfg.temp_ordner,
        "temp_exists": mediathek.pool_mounted() and mediathek.temp_dir().is_dir(),
        "dlna_an": fe.dlna_an,
        "dlna_name": fe.dlna_name,
        "video_ablauf_stunden": cfg.video_ablauf_stunden,
        "bild_ablauf_tage": cfg.bild_ablauf_tage,
    }


@app.post("/api/tv/stage-streaming/{file_id}")
async def api_tv_stage_streaming(request: Request, file_id: int):
    _csrf(request, request.headers.get("x-csrf", ""))
    cfg = mediathek_db.Einstellungen.load()
    await run_in_threadpool(_check_mediathek_access, request, cfg.streaming_ordner, require_pool=True)

    def _stage():
        f = mediathek_db.get_file(file_id)
        if not f:
            raise Fehler(404, "Video nicht gefunden")
        if f["status"] != "bereit":
            raise Fehler(409, "Video wird noch umgewandelt")
        title_info = f.get("titel") or {}
        if title_info.get("type") == "show" and f.get("staffel") and f.get("folge"):
            tname = f"{title_info.get('name', 'Serie')} - S{f['staffel']:02d}E{f['folge']:02d}"
            if f.get("folgentitel"):
                tname += f" - {f['folgentitel']}"
        else:
            tname = title_info.get("name") or Path(f["rel_pfad"]).stem
            if title_info.get("year") and f"({title_info['year']})" not in tname:
                tname += f" ({title_info['year']})"
        return mediathek.stage_video(f["rel_pfad"], art="streaming", titel=tname, datei_id=file_id)

    item = await run_in_threadpool(_stage)
    return {"ok": True, "item": item}


@app.post("/api/tv/stage-video")
async def api_tv_stage_video(request: Request):
    _csrf(request, request.headers.get("x-csrf", ""))
    body = await request.json()
    pool_rel = str(body.get("pool_rel") or "").strip()
    if not pool_rel:
        raise Fehler(400, "Pfad fehlt")
    await run_in_threadpool(_ensure_pool_rel_allowed, request, pool_rel)
    titel = str(body.get("titel") or "").strip()
    stunden = body.get("ablauf_stunden")
    stunden_val = int(stunden) if stunden is not None else None
    item = await run_in_threadpool(mediathek.stage_video, pool_rel, art="video", titel=titel, ablauf_stunden=stunden_val)
    return {"ok": True, "item": item}


@app.post("/api/tv/stage-images")
async def api_tv_stage_images(request: Request):
    _csrf(request, request.headers.get("x-csrf", ""))
    body = await request.json()
    rels = body.get("pool_rels") or []
    if not isinstance(rels, list) or not rels:
        raise Fehler(400, "Keine Bilder ausgewählt")
    rels = [str(r) for r in rels[:500]]

    def _stage_all():
        for r in rels:
            _ensure_pool_rel_allowed(request, r)
        tage = body.get("ablauf_tage")
        tage_val = int(tage) if tage is not None else None
        gruppe = str(body.get("gruppe") or "").strip()
        return mediathek.stage_images(rels, gruppe=gruppe, ablauf_tage=tage_val)

    items = await run_in_threadpool(_stage_all)
    return {"ok": True, "count": len(items), "items": items}


@app.delete("/api/tv/item/{item_id}")
async def api_tv_delete_item(request: Request, item_id: int):
    _csrf(request, request.headers.get("x-csrf", ""))
    cfg = mediathek_db.Einstellungen.load()
    await run_in_threadpool(_check_mediathek_access, request, cfg.temp_ordner, require_pool=True)
    ok = await run_in_threadpool(mediathek.remove_staged, item_id)
    if not ok:
        raise Fehler(404, "Eintrag nicht gefunden")
    return {"ok": True, "temp_exists": mediathek.temp_dir().is_dir()}


@app.post("/api/tv/clear")
async def api_tv_clear(request: Request):
    _csrf(request, request.headers.get("x-csrf", ""))
    cfg = mediathek_db.Einstellungen.load()
    await run_in_threadpool(_check_mediathek_access, request, cfg.temp_ordner, require_pool=True)
    art = None
    try:
        body = await request.json()
        if isinstance(body, dict) and body.get("art") in ("streaming", "video", "bild"):
            art = body["art"]
    except Exception:  # noqa: BLE001
        pass
    removed = await run_in_threadpool(mediathek.clear_staged, art)
    return {"ok": True, "removed": removed, "temp_exists": mediathek.temp_dir().is_dir()}


# ---------- API: Persönliche Medien (/medien) ----------

@app.get("/api/medien/liste")
async def api_medien_liste(request: Request, kind: str = "all", share: str = ""):
    cfg = mediathek_db.Einstellungen.load()
    await run_in_threadpool(_check_mediathek_access, request, cfg.medien_ordner)

    def _load():
        shares = _allowed_shares(request)
        items = mediathek.list_personal_media(shares, kind=kind, share_filter=share)
        return {
            "items": items,
            "shares": [f.name for f, _ in shares],
            "tv": mediathek_db.list_tv_items(),
            "pool_da": mediathek.pool_mounted(),
        }

    return await run_in_threadpool(_load)


@app.get("/api/medien/datei")
async def api_medien_datei(request: Request, rel: str = ""):
    if not rel:
        raise Fehler(400, "Pfad fehlt")
    path = await run_in_threadpool(lambda: _ensure_pool_rel_allowed(request, rel, allow_temp=True))
    typ = _medientyp(path)
    resp = FileResponse(path, media_type=typ, filename=path.name, content_disposition_type="inline")
    resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; media-src 'self'; sandbox"
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.put("/api/medien/hochladen")
async def api_medien_hochladen(request: Request):
    """Lädt ein persönliches Video oder Foto hoch und stellt es auf Wunsch sofort in „Jetzt schauen“ bereit."""
    _csrf(request, request.headers.get("x-csrf", ""))
    cfg = mediathek_db.Einstellungen.load()
    await run_in_threadpool(_check_mediathek_access, request, cfg.medien_ordner, write=True, require_pool=True)

    qp = request.query_params
    dateiname = check_name(str(qp.get("dateiname", "")).strip() or "datei")
    ext = Path(dateiname).suffix.lower()
    is_video = ext in mediathek.VIDEO_EXTS
    is_image = ext in mediathek.IMAGE_EXTS
    if not is_video and not is_image:
        raise Fehler(400, f"Dateityp „{ext or 'ohne Endung'}“ wird nicht unterstützt")

    ziel_tv = qp.get("ziel", "tv") == "tv"
    tage = int(qp["ablauf_tage"]) if qp.get("ablauf_tage") and str(qp["ablauf_tage"]).isdigit() else None
    gruppe = str(qp.get("gruppe") or "").strip()

    def _prep():
        if is_video:
            rel_base = mediathek.personal_video_target_rel(dateiname)
            dst = mediathek.unique_target_path(rel_base)
            dst.parent.mkdir(parents=True, exist_ok=True)
            mediathek._chown_tree(dst.parent, mediathek.pool_root())
            raw = dst.parent / f".{dst.stem[:140]}.raw-{secrets.token_hex(4)}{ext}"
            return dst, raw
        rel_base = mediathek.personal_image_target_rel(dateiname)
        dst = mediathek.unique_target_path(rel_base)
        dst.parent.mkdir(parents=True, exist_ok=True)
        mediathek._chown_tree(dst.parent, mediathek.pool_root())
        raw = dst.parent / f".{dst.stem[:140]}.hochladen-{secrets.token_hex(4)}{ext}"
        return dst, raw

    dst, raw = await run_in_threadpool(_prep)
    try:
        fh = await run_in_threadpool(open, raw, "wb")
        try:
            async for chunk in request.stream():
                if chunk:
                    await run_in_threadpool(fh.write, chunk)
        finally:
            await run_in_threadpool(fh.close)
        _besitzer(raw)

        def _finish():
            rel_dst = str(dst.relative_to(mediathek.pool_root()))
            if is_image:
                os.replace(raw, dst)
                tv_items = mediathek.stage_images([rel_dst], gruppe=gruppe, ablauf_tage=tage) if ziel_tv else []
                return {"ok": True, "kind": "image", "pool_rel": rel_dst, "tv_item": tv_items[0] if tv_items else None}

            # Video: wenn bereits TV-taugliches MP4, direkt umbenennen + stagen; sonst im Hintergrund umwandeln & stagen
            info = mediathek.probe_video(raw)
            if not mediathek.needs_conversion(raw, info):
                os.replace(raw, dst)
                tv_item = mediathek.stage_video(rel_dst, art="video", titel=dst.stem) if ziel_tv else None
                return {"ok": True, "kind": "video", "pool_rel": rel_dst, "status": "bereit", "tv_item": tv_item}

            tv_id = None
            if ziel_tv:
                tdir = mediathek.ensure_temp_dir()
                tv_dest = mediathek._unique_flat_tv_dest(tdir, dst.name)
                tv_rel = str(tv_dest.relative_to(mediathek.pool_root()))
                hours = cfg.video_ablauf_stunden
                ablauf_am = time.time() + hours * 3600 if hours > 0 else None
                tv_id = mediathek_db.upsert_tv_item(
                    art="video",
                    quelle_pfad=rel_dst,
                    ziel_pfad=tv_rel,
                    titel=dst.stem,
                    gruppe=dst.stem,
                    status="umwandlung",
                    fortschritt=5,
                    groesse=raw.stat().st_size,
                    ablauf_am=ablauf_am,
                )

            def _bg():
                ok, err, _ = mediathek.convert_to_mp4(raw, dst, delete_src=True)
                if not ok:
                    if tv_id is not None:
                        mediathek_db.update_tv_item(tv_id, status="fehler", fehler=err)
                        mediathek.cleanup_temp_dir_if_empty()
                    jobs.log(None, f"Medien-Video Umwandlung fehlgeschlagen ({raw.name}): {err}")
                    return
                if tv_id is not None:
                    tv_row = mediathek_db.get_tv_item(tv_id)
                    if tv_row:
                        tv_target = mediathek.safe_pool_path(tv_row["ziel_pfad"])
                        mediathek_db.update_tv_item(tv_id, status="kopiere", fortschritt=50)
                        mediathek._copy_chunked(dst, tv_target, tv_id)
                        mediathek_db.update_tv_item(tv_id, status="bereit", fortschritt=100, groesse=tv_target.stat().st_size)

            threading.Thread(target=_bg, name="medien-vid", daemon=True).start()
            return {
                "ok": True,
                "kind": "video",
                "pool_rel": rel_dst,
                "status": "umwandlung",
                "tv_item": mediathek_db.get_tv_item(tv_id) if tv_id is not None else None,
            }

        return await run_in_threadpool(_finish)
    except OSError as exc:
        raw.unlink(missing_ok=True)
        raise Fehler(507 if exc.errno == 28 else 500, "Kein Platz mehr" if exc.errno == 28 else f"Ging nicht: {exc.strerror}") from exc
    except BaseException:
        raw.unlink(missing_ok=True)
        raise

