"""Dateien im Browser: Freigaben ansehen, öffnen, herunterladen, hochladen, umbenennen, löschen.

Läuft als zweite Webseite im selben Container (DATEIEN_PORT, Standard 8092) und hält sich an dieselben Regeln
wie das Netzlaufwerk: pro Freigabe und Gerät offen, mit Passwort oder unsichtbar. Das Gerät wird an seiner
IP erkannt (Geräteliste aus cloudy), angemeldet wird mit den Benutzern aus „Freigaben“.
"""
import hashlib
import hmac
import mimetypes
import os
import secrets
import shutil
import stat
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

from . import auth, freigaben, pool
from .freigaben import Freigabe

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
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
    ctx.update(
        request=request,
        benutzer=request.state.benutzer,
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
    schreiben: bool


def sichtbar(request: Request) -> list[tuple[Freigabe, bool]]:
    """Freigaben, die dieses Gerät sieht: (Freigabe, darf rein)."""
    e = freigaben.Einstellungen.load()
    if not e.an:
        return []
    key = freigaben.key_fuer_ip(_ip(request))
    out = []
    for f in e.freigaben:
        regel = freigaben.regel_fuer(f, key)
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
    if not e.an or regel == "versteckt":
        raise Fehler(404, "Diesen Ordner gibt es nicht")
    if not freigaben.darf(f, regel, request.state.benutzer, e.benutzer):
        if request.state.benutzer:
            raise Fehler(403, f"{request.state.benutzer} darf nicht in „{f.name}“")
        raise Fehler(401, "Für diesen Ordner musst du dich anmelden")
    if str(pool.paths()["pool"]) not in pool.mounts():
        raise Fehler(503, "Der Speicher ist gerade nicht eingehängt")
    return Zugang(f, freigaben.share_path(f), not f.nur_lesen)


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
    return weiter if weiter.startswith(("/d/", "/?")) or weiter == "/" else "/"


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


def auflisten(path: Path, ist_pool_wurzel: bool) -> list[Eintrag]:
    out = []
    with os.scandir(path) as it:
        for entry in it:
            if ist_pool_wurzel and entry.name == ".mergerfs":
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
                     schreiben=z.schreiben, url=_url, oben=_url(name, "/".join(teile[:-1])) if teile else "/")
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
    if path.exists() and request.query_params.get("ersetzen") != "1":
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
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise Fehler(507 if exc.errno == 28 else 500, "Kein Platz mehr" if exc.errno == 28 else f"Ging nicht: {exc.strerror}") from exc
    except BaseException:
        tmp.unlink(missing_ok=True)  # Abbruch im Browser
        raise
    return {"ok": True, "name": dateiname}
