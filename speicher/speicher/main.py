"""Webseite des Speicher-Dienstes: Platten zuordnen, Pool, Puffer-SSD und Backup nach Google Drive."""
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import auth, backup, config, disks, jobs, pool
from .config import Config

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
_stop = threading.Event()


def fmt_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return (f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}").replace(".", ",")
        n /= 1000
    return ""


def fmt_time(ts) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else "noch nie"


templates.env.filters["bytes"] = fmt_bytes
templates.env.filters["zeit"] = fmt_time


# ---------- Planer ----------

def scheduler() -> None:
    while not _stop.wait(30):
        try:
            cfg = Config.load()
            now = time.time()
            if jobs.busy():
                continue
            if cfg.backup_an and backup.drive_eingerichtet() and backup.backup_due(now, cfg.backup_zeit, cfg.letztes_backup_start, cfg.letzter_versuch):
                jobs.run("Backup nach Google Drive", backup.backup)
            elif cfg.puffer and now - cfg.letzte_kopie >= max(1, cfg.puffer_minuten) * 60:
                jobs.run("Kopie auf Puffer-SSD", backup.puffer_kopie)
            elif int(now) % 300 < 30:
                # Platte wieder da (z. B. nach USB-Wackler)? Dann einhängen.
                st = pool.status(cfg)
                if any(not h.mounted and h.device for h in st["hdds"]) or (st["puffer"] and not st["puffer"].mounted and st["puffer"].device):
                    pool.mount_all(cfg)
        except Exception as exc:  # noqa: BLE001
            jobs.log(None, f"Planer: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if "PYTEST_CURRENT_TEST" not in os.environ:
        try:
            await run_in_threadpool(pool.mount_all)
        except Exception as exc:  # noqa: BLE001
            jobs.log(None, f"Einhängen beim Start: {exc}")
        threading.Thread(target=scheduler, name="planer", daemon=True).start()
    yield
    _stop.set()
    if "PYTEST_CURRENT_TEST" not in os.environ:
        pool.unmount_pool()  # sonst bleibt auf dem Host ein toter FUSE-Eintrag zurück


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------- Sicherheit ----------

PUBLIC = {"/login", "/einrichten", "/healthz"}


@app.middleware("http")
async def security(request: Request, call_next):
    path = request.url.path
    session = request.cookies.get(auth.COOKIE, "")
    logged_in = auth.valid_session(session)
    request.state.session = session if logged_in else ""
    if path not in PUBLIC:
        if not auth.password_set():
            return RedirectResponse("/einrichten", 303)
        if not logged_in:
            if path.startswith("/api/"):
                return JSONResponse({"fehler": "nicht angemeldet"}, 401)
            return RedirectResponse("/login", 303)
    if request.method == "POST":
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return Response("Fremde Herkunft", 403)
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
    )
    return response


async def _form(request: Request) -> dict:
    form = await request.form()
    if not auth.csrf_ok(request.state.session, str(form.get("csrf", ""))):
        raise PermissionError("Sitzung abgelaufen – bitte Seite neu laden")
    return {k: str(v) for k, v in form.items()}


def _back(msg: str = "", fehler: str = "", to: str = "/") -> RedirectResponse:
    q = f"?ok={quote(msg)}" if msg else (f"?fehler={quote(fehler)}" if fehler else "")
    return RedirectResponse(to + q, 303)


def _page(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.update(
        request=request,
        csrf=auth.csrf_token(request.state.session) if request.state.session else "",
        ok=request.query_params.get("ok", ""),
        fehler=ctx.get("fehler") or request.query_params.get("fehler", ""),
    )
    return templates.TemplateResponse(request, name, ctx)


# ---------- Anmeldung ----------

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/einrichten", response_class=HTMLResponse)
async def einrichten_form(request: Request):
    if auth.password_set():
        return RedirectResponse("/login", 303)
    return _page(request, "login.html", erst=True)


@app.post("/einrichten")
async def einrichten(request: Request, passwort: str = Form(...), passwort2: str = Form(...)):
    if auth.password_set():
        return RedirectResponse("/login", 303)
    if passwort != passwort2:
        return _page(request, "login.html", erst=True, fehler="Die Passwörter sind nicht gleich")
    try:
        auth.set_password(passwort)
    except ValueError as exc:
        return _page(request, "login.html", erst=True, fehler=str(exc))
    return _login_response()


def _login_response() -> RedirectResponse:
    resp = RedirectResponse("/", 303)
    resp.set_cookie(auth.COOKIE, auth.make_session(), max_age=auth.MAX_AGE, httponly=True, samesite="strict")
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if not auth.password_set():
        return RedirectResponse("/einrichten", 303)
    return _page(request, "login.html", erst=False)


@app.post("/login")
async def login(request: Request, passwort: str = Form(...)):
    ip = request.client.host if request.client else "?"
    if auth.throttled(ip):
        return _page(request, "login.html", erst=False, fehler="Zu viele Versuche – in 10 Minuten nochmal")
    if not await run_in_threadpool(auth.check_password, passwort):
        auth.failed(ip)
        return _page(request, "login.html", erst=False, fehler="Falsches Passwort")
    return _login_response()


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# ---------- Übersicht ----------

def _overview() -> dict:
    cfg = Config.load()
    fehler = ""
    try:
        alle = disks.list_disks()
    except disks.DiskError as exc:
        alle, fehler = [], str(exc)
    rollen = {}
    for disk in alle:
        uu = disk.uuids
        if cfg.puffer and cfg.puffer in uu:
            rollen[disk.path] = "puffer"
        else:
            for i, u in enumerate(cfg.pool):
                if u in uu:
                    rollen[disk.path] = f"hdd{i + 1}"
    health = {d.path: disks.smart_health(d.path) for d in alle}
    return {
        "cfg": cfg, "disks": alle, "rollen": rollen, "health": health, "st": pool.status(cfg),
        "drive": backup.drive_eingerichtet(), "disk_fehler": fehler, "mount_root": str(config.mount_root()),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    data = await run_in_threadpool(_overview)
    return _page(request, "index.html", job=jobs.current(), verlauf=jobs.history()[:8], **data)


@app.get("/api/job")
async def api_job():
    job = jobs.current() or (jobs.history()[0] if jobs.history() else None)
    if not job:
        return {"laeuft": False}
    return {"laeuft": jobs.current() is not None, "name": job.name, "ok": job.ok, "zeilen": list(job.zeilen)[-60:]}


# ---------- Platten ----------

def _assign(path: str, rolle: str, formatieren: bool, bestaetigung: str) -> str:
    if jobs.busy():
        raise disks.DiskError("Gerade läuft ein Auftrag – bitte warten, bis er fertig ist")
    disk = disks.find_disk(path)
    if disk.system:
        raise disks.DiskError(f"{disk.path} gehört zum System und ist gesperrt")
    cfg = Config.load()
    uuids = disk.uuids
    new_pool = [u for u in cfg.pool if u not in uuids]
    new_puffer = "" if cfg.puffer in uuids else cfg.puffer
    if rolle == "keine":
        with pool.lock:
            pool.unmount_all()
            cfg = config.update(pool=new_pool, puffer=new_puffer)
            pool.mount_all(cfg)
        return f"{disk.name} wird nicht mehr benutzt (Daten darauf bleiben erhalten)"
    if rolle not in ("pool", "puffer"):
        raise disks.DiskError("Unbekannte Aufgabe")
    if rolle == "puffer" and new_puffer:
        raise disks.DiskError("Es gibt schon eine Puffer-SSD – die erst auf „nicht benutzen“ stellen")

    with pool.lock:
        pool.unmount_all()
        try:
            if formatieren:
                if bestaetigung.strip() != disk.name:
                    raise disks.DiskError(f"Zum Formatieren genau „{disk.name}“ eintippen")
                label = "cloudy-puffer" if rolle == "puffer" else f"cloudy-hdd{len(new_pool) + 1}"
                uuid = disks.format_disk(disk.path, label, reserve_percent=0 if rolle == "pool" else 1)
                jobs.log(None, f"{disk.path} ({disk.model}) formatiert als {label}")
            else:
                fs = disk.dateisystem
                if not fs:
                    raise disks.DiskError(f"{disk.name} hat kein eindeutiges ext4/xfs/btrfs-Dateisystem – bitte formatieren")
                uuid = fs.uuid
            if rolle == "pool":
                cfg = config.update(pool=new_pool + [uuid], puffer=new_puffer)
            else:
                cfg = config.update(pool=new_pool, puffer=uuid, puffer_seit=time.time())
        finally:
            result = pool.mount_all()
    if result["pool_fehler"]:
        raise disks.DiskError(result["pool_fehler"])
    return f"{disk.name} ist jetzt {'Puffer-SSD' if rolle == 'puffer' else 'im Pool'}"


@app.post("/platte")
async def platte(request: Request):
    try:
        f = await _form(request)
        msg = await run_in_threadpool(_assign, f.get("path", ""), f.get("rolle", ""), f.get("formatieren") == "1", f.get("bestaetigung", ""))
    except (disks.DiskError, PermissionError) as exc:
        return _back(fehler=str(exc))
    return _back(msg)


# ---------- Einstellungen ----------

def _int(value: str, low: int, high: int, default: int) -> int:
    try:
        return min(high, max(low, int(value)))
    except (TypeError, ValueError):
        return default


@app.post("/einstellungen")
async def einstellungen(request: Request):
    try:
        f = await _form(request)
    except PermissionError as exc:
        return _back(fehler=str(exc))
    cfg = Config.load()
    zeit = f.get("backup_zeit", "00:00")
    try:
        h, m = (int(x) for x in zeit.split(":"))
        assert 0 <= h < 24 and 0 <= m < 60
        zeit = f"{h:02d}:{m:02d}"
    except (ValueError, AssertionError):
        return _back(fehler="Uhrzeit bitte als HH:MM")
    ordner = f.get("drive_ordner", cfg.drive_ordner).strip().strip("/")
    if not ordner or any(c in ordner for c in "\\:\n\r") or ".." in ordner.split("/"):
        return _back(fehler="Ungültiger Drive-Ordner")
    neu_policy = f.get("create_policy", cfg.create_policy)
    changes = dict(
        create_policy=neu_policy if neu_policy in ("epmfs", "mfs", "lfs", "ff") else "epmfs",
        minfree_gb=_int(f.get("minfree_gb"), 1, 500, cfg.minfree_gb),
        puffer_minuten=_int(f.get("puffer_minuten"), 1, 240, cfg.puffer_minuten),
        backup_an=f.get("backup_an") == "1",
        backup_zeit=zeit,
        drive_ordner=ordner,
        behalten_taeglich=_int(f.get("behalten_taeglich"), 1, 365, cfg.behalten_taeglich),
        behalten_woechentlich=_int(f.get("behalten_woechentlich"), 0, 520, cfg.behalten_woechentlich),
        behalten_monatlich=_int(f.get("behalten_monatlich"), 0, 240, cfg.behalten_monatlich),
    )
    remount = changes["create_policy"] != cfg.create_policy or changes["minfree_gb"] != cfg.minfree_gb
    cfg = config.update(**changes)
    if remount and not jobs.busy():
        def _re():
            with pool.lock:
                pool.unmount_pool()
                pool.mount_all(cfg)
        await run_in_threadpool(_re)
    return _back("Gespeichert")


@app.post("/drive")
async def drive(request: Request):
    try:
        f = await _form(request)
        backup.write_rclone(f.get("token", ""), f.get("client_id", ""), f.get("client_secret", ""))
    except (backup.BackupError, PermissionError) as exc:
        return _back(fehler=str(exc))
    if not jobs.run("Google Drive prüfen und Ablage anlegen", backup.drive_testen_und_init):
        return _back(fehler="Gespeichert, aber gerade läuft ein Auftrag – danach „Drive prüfen“ drücken")
    return _back("Drive gespeichert – Prüfung läuft unten im Protokoll")


AKTIONEN = {
    "backup": ("Backup nach Google Drive", backup.backup),
    "kopie": ("Kopie auf Puffer-SSD", backup.puffer_kopie),
    "zurueckspielen": ("Puffer in den Pool zurückspielen", backup.puffer_zurueckspielen),
    "drive": ("Google Drive prüfen und Ablage anlegen", backup.drive_testen_und_init),
    "einhaengen": ("Platten neu einhängen", lambda job: pool.mount_all()),
}


@app.post("/aktion/{name}")
async def aktion(request: Request, name: str):
    try:
        await _form(request)
    except PermissionError as exc:
        return _back(fehler=str(exc))
    if name not in AKTIONEN:
        return _back(fehler="Unbekannte Aktion")
    title, func = AKTIONEN[name]
    if not jobs.run(title, func):
        return _back(fehler="Es läuft schon ein Auftrag")
    return _back(f"{title} gestartet")


# ---------- Wiederherstellen & Schlüssel ----------

@app.get("/staende", response_class=HTMLResponse)
async def staende(request: Request):
    fehler = ""
    try:
        items = await run_in_threadpool(backup.snapshots)
    except Exception as exc:  # noqa: BLE001
        items, fehler = [], str(exc)
    return _page(request, "staende.html", staende=items, fehler=fehler)


@app.post("/wiederherstellen")
async def wiederherstellen(request: Request):
    try:
        f = await _form(request)
    except PermissionError as exc:
        return _back(fehler=str(exc))
    sid = f.get("id", "")
    if not jobs.run("Wiederherstellen aus Google Drive", backup.restore, sid, f.get("pfad", "")):
        return _back(fehler="Es läuft schon ein Auftrag")
    return _back("Wiederherstellung läuft – Ziel: pool/wiederhergestellt/")


@app.get("/schluessel", response_class=HTMLResponse)
async def schluessel_form(request: Request):
    return _page(request, "schluessel.html", schluessel="")


@app.post("/schluessel", response_class=HTMLResponse)
async def schluessel(request: Request):
    try:
        f = await _form(request)
    except PermissionError as exc:
        return _page(request, "schluessel.html", schluessel="", fehler=str(exc))
    ip = request.client.host if request.client else "?"
    if auth.throttled(ip) or not await run_in_threadpool(auth.check_password, f.get("passwort", "")):
        auth.failed(ip)
        return _page(request, "schluessel.html", schluessel="", fehler="Falsches Passwort")
    return _page(request, "schluessel.html", schluessel=config.secret("restic-passwort"), repo=backup.repository(Config.load()))
