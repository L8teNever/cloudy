"""Webseite des Speicher-Dienstes: Platten zuordnen, Pool, Puffer-SSD und Backup nach Google Drive."""
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, backup, config, disks, freigaben, jobs, medien, pool
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


def fmt_rel(ts, now: float | None = None) -> str:
    """'gerade eben', 'vor 5 Min.', 'heute, 00:00', 'gestern, 23:10', 'morgen, 00:00', sonst Datum."""
    if not ts:
        return "noch nie"
    now = time.time() if now is None else now
    diff = now - ts
    if 0 <= diff < 60:
        return "gerade eben"
    if 0 <= diff < 3600:
        return f"vor {int(diff // 60)} Min."
    day = (datetime.fromtimestamp(ts).date() - datetime.fromtimestamp(now).date()).days
    hm = time.strftime("%H:%M", time.localtime(ts))
    return {0: f"heute, {hm}", -1: f"gestern, {hm}", 1: f"morgen, {hm}"}.get(day, time.strftime("%d.%m.%Y, %H:%M", time.localtime(ts)))


def pct(used, total) -> int:
    return max(0, min(100, round(used / total * 100))) if total else 0


templates.env.filters["bytes"] = fmt_bytes
templates.env.filters["zeit"] = fmt_time
templates.env.filters["wann"] = fmt_rel
templates.env.globals["pct"] = pct
templates.env.globals["dateien_port"] = int(os.environ.get("DATEIEN_PORT", "8092") or 0)
templates.env.globals["puffer_paths"] = lambda rollen: [p for p, r in rollen.items() if r == "puffer"]


# ---------- Planer ----------

def scheduler() -> None:
    letzter_abgleich = 0.0
    while not _stop.wait(30):
        if time.time() - letzter_abgleich > 55:
            letzter_abgleich = time.time()
            try:
                freigaben.dienste.abgleichen()  # neue Geräte-IPs aus cloudy, abgestürzte Dienste
            except Exception as exc:  # noqa: BLE001
                jobs.log(None, f"Freigaben: {exc}")
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
        try:
            await run_in_threadpool(freigaben.dienste.abgleichen)
        except Exception as exc:  # noqa: BLE001
            jobs.log(None, f"Freigaben beim Start: {exc}")
        threading.Thread(target=scheduler, name="planer", daemon=True).start()
        medien.start()
    yield
    _stop.set()
    if "PYTEST_CURRENT_TEST" not in os.environ:
        medien.stop()
        freigaben.dienste.stop_all()
        pool.unmount_pool()  # sonst bleibt auf dem Host ein toter FUSE-Eintrag zurück


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


# ---------- Sicherheit ----------

PUBLIC = {"/login", "/einrichten", "/healthz"}
NAV = [
    ("/", "Übersicht", "home_storage"),
    ("/platten", "Platten", "hard_drive"),
    ("/freigaben", "Freigaben", "folder_shared"),
    ("/backup", "Backup", "cloud_upload"),
    ("/wiederherstellen", "Zurückholen", "history"),
    ("/einstellungen", "Einstellungen", "settings"),
]
NAV_PATHS = {href for href, _, _ in NAV}


@app.middleware("http")
async def security(request: Request, call_next):
    path = request.url.path
    session = request.cookies.get(auth.COOKIE, "")
    logged_in = auth.valid_session(session)
    request.state.session = session if logged_in else ""
    if path not in PUBLIC and not path.startswith("/static/"):
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
    if not path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; font-src 'self'; img-src 'self' data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


async def _form(request: Request) -> dict:
    form = await request.form()
    if not auth.csrf_ok(request.state.session, str(form.get("csrf", ""))):
        raise PermissionError("Sitzung abgelaufen – bitte Seite neu laden")
    request.state.form = form
    return {k: str(v) for k, v in form.items()}


def _back(msg: str = "", fehler: str = "", to: str = "/") -> RedirectResponse:
    if not (to in NAV_PATHS or re.fullmatch(r"/freigaben/[A-Za-z0-9%._~-]+", to)):
        to = "/"
    q = f"?ok={quote(msg)}" if msg else (f"?fehler={quote(fehler)}" if fehler else "")
    return RedirectResponse(to + q, 303)


def _page(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.update(
        request=request,
        nav=NAV,
        csrf=auth.csrf_token(request.state.session) if request.state.session else "",
        ok=request.query_params.get("ok", ""),
        fehler=ctx.get("fehler") or request.query_params.get("fehler", ""),
        job=jobs.current(),
        verlauf=jobs.history()[:10],
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

def _status() -> dict:
    cfg = Config.load()
    st = pool.status(cfg)
    return {
        "cfg": cfg, "st": st, "drive": backup.drive_eingerichtet(), "mount_root": str(config.mount_root()),
        "naechstes": backup.naechstes_backup(time.time(), cfg.backup_zeit) if cfg.backup_an else 0,
        "warnungen": _warnungen(cfg, st),
    }


def _warnungen(cfg: Config, st: dict) -> list[str]:
    out = [f"{h.name} fehlt – ihre Dateien sind gerade nicht im Pool" for h in st["hdds"] if not h.mounted]
    if st["puffer"] and not st["puffer"].mounted:
        out.append("Die Puffer-SSD ist nicht eingehängt")
    if cfg.kopie_fehler:
        out.append(cfg.kopie_fehler)
    if cfg.letzter_fehler:
        out.append(f"Letztes Backup fehlgeschlagen: {cfg.letzter_fehler}")
    elif cfg.backup_an and cfg.letztes_backup_ende and time.time() - cfg.letztes_backup_ende > 3 * 86400:
        out.append("Das letzte erfolgreiche Backup ist älter als 3 Tage")
    return out


def _disks() -> dict:
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
    return {"disks": alle, "rollen": rollen, "health": health, "disk_fehler": fehler}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return _page(request, "uebersicht.html", active="/", **await run_in_threadpool(_status))


@app.get("/platten", response_class=HTMLResponse)
async def platten(request: Request):
    data = await run_in_threadpool(lambda: {**_status(), **_disks()})
    return _page(request, "platten.html", active="/platten", **data)


@app.get("/backup", response_class=HTMLResponse)
async def backup_seite(request: Request):
    return _page(request, "backup.html", active="/backup", **await run_in_threadpool(_status))


@app.get("/einstellungen", response_class=HTMLResponse)
async def einstellungen_seite(request: Request):
    return _page(request, "einstellungen.html", active="/einstellungen", port=os.environ.get("SPEICHER_PORT", "8091"), **await run_in_threadpool(_status))


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
        return _back(fehler=str(exc), to="/platten")
    return _back(msg, to="/platten")


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
    to = f.get("zurueck", "/einstellungen")
    cfg = Config.load()
    changes: dict = {}
    if "backup_zeit" in f:
        try:
            h, m = (int(x) for x in f["backup_zeit"].split(":"))
            assert 0 <= h < 24 and 0 <= m < 60
        except (ValueError, AssertionError):
            return _back(fehler="Uhrzeit bitte als HH:MM", to=to)
        changes["backup_zeit"] = f"{h:02d}:{m:02d}"
    if "drive_ordner" in f:
        ordner = f["drive_ordner"].strip().strip("/")
        if not ordner or any(c in ordner for c in "\\:\n\r") or ".." in ordner.split("/"):
            return _back(fehler="Ungültiger Drive-Ordner", to=to)
        changes["drive_ordner"] = ordner
    if "create_policy" in f:
        changes["create_policy"] = f["create_policy"] if f["create_policy"] in ("epmfs", "mfs", "lfs", "ff") else "epmfs"
    for key, low, high in (("minfree_gb", 1, 500), ("puffer_minuten", 1, 240), ("behalten_tage", 1, 3650)):
        if key in f:
            changes[key] = _int(f[key], low, high, getattr(cfg, key))
    if "backup_an" in f:  # Formular schickt verstecktes 0 und beim Haken 1 (der letzte Wert gewinnt)
        changes["backup_an"] = f["backup_an"] == "1" and backup.drive_eingerichtet()
    remount = any(k in changes and changes[k] != getattr(cfg, k) for k in ("create_policy", "minfree_gb"))
    cfg = config.update(**changes)
    if remount and not jobs.busy():
        def _re():
            with pool.lock:
                pool.unmount_pool()
                pool.mount_all(cfg)
        await run_in_threadpool(_re)
    return _back("Gespeichert", to=to)


@app.post("/drive")
async def drive(request: Request):
    try:
        f = await _form(request)
        backup.write_rclone(f.get("token", ""), f.get("client_id", ""), f.get("client_secret", ""))
    except (backup.BackupError, PermissionError) as exc:
        return _back(fehler=str(exc), to="/backup")
    if not jobs.run("Google Drive prüfen", backup.drive_testen_und_init):
        return _back(fehler="Gespeichert, aber gerade läuft ein Auftrag – danach „Drive prüfen“ drücken", to="/backup")
    return _back("Drive verbunden – wird gerade geprüft", to="/backup")


AKTIONEN = {
    "backup": ("Backup nach Google Drive", backup.backup),
    "kopie": ("Kopie auf Puffer-SSD", backup.puffer_kopie),
    "zurueckspielen": ("Puffer in den Pool zurückspielen", backup.puffer_zurueckspielen),
    "drive": ("Google Drive prüfen", backup.drive_testen_und_init),
    "einhaengen": ("Platten neu einhängen", lambda job: pool.mount_all()),
}


@app.post("/aktion/{name}")
async def aktion(request: Request, name: str):
    try:
        f = await _form(request)
    except PermissionError as exc:
        return _back(fehler=str(exc))
    to = f.get("zurueck", "/")
    if name not in AKTIONEN:
        return _back(fehler="Unbekannte Aktion", to=to)
    title, func = AKTIONEN[name]
    if not jobs.run(title, func):
        return _back(fehler="Es läuft schon ein Auftrag", to=to)
    return _back(f"{title} gestartet", to=to)


# ---------- Wiederherstellen ----------

@app.get("/wiederherstellen", response_class=HTMLResponse)
async def staende(request: Request):
    fehler = ""
    try:
        items = await run_in_threadpool(backup.staende)
    except Exception as exc:  # noqa: BLE001
        items, fehler = [], str(exc)
    return _page(request, "staende.html", active="/wiederherstellen", staende=items, fehler=fehler, drive=backup.drive_eingerichtet())


@app.post("/wiederherstellen")
async def wiederherstellen(request: Request):
    try:
        f = await _form(request)
    except PermissionError as exc:
        return _back(fehler=str(exc), to="/wiederherstellen")
    sid = f.get("id", "")
    if not jobs.run("Wiederherstellen aus Google Drive", backup.restore, sid, f.get("pfad", "")):
        return _back(fehler="Es läuft schon ein Auftrag", to="/wiederherstellen")
    return _back("Wird zurückgeholt nach pool/wiederhergestellt/", to="/wiederherstellen")


# ---------- Freigaben (Netzlaufwerk + Fernseher) ----------

def _server_ip() -> str:
    """Eigene IP im Heimnetz (für die Anleitung „so verbindest du dich“)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return "<server-ip>"


def _freigaben_data() -> dict:
    e = freigaben.Einstellungen.load()
    geraete, fehler = freigaben.geraete()
    d = freigaben.dienste
    return {
        "fe": e, "geraete": geraete, "cloudy_fehler": fehler, "server_ip": _server_ip(),
        "smb_laeuft": d.laeuft("smbd"), "dlna_laeuft": d.laeuft("minidlna"), "dienst_fehler": d.fehler,
        "medien": medien.status(), "regel_text": freigaben.REGEL_TEXT, "pool_da": pool.status()["pool"].mounted,
        "recht_text": freigaben.RECHTE_TEXT, "anmelde_url": freigaben.anmelde_url(e),
        "anmelde_freigabe": freigaben.ANMELDE_FREIGABE, "anmelde_datei": freigaben.ANMELDE_DATEI,
    }


async def _apply() -> None:
    await run_in_threadpool(freigaben.dienste.abgleichen)


def _share_url(name: str) -> str:
    return "/freigaben/" + quote(name, safe="")


@app.get("/freigaben", response_class=HTMLResponse)
async def freigaben_seite(request: Request):
    data = await run_in_threadpool(lambda: {**_status(), **_freigaben_data()})
    return _page(request, "freigaben.html", active="/freigaben", **data)


@app.post("/freigaben/einstellungen")
async def freigaben_einstellungen(request: Request):
    try:
        f = await _form(request)
        e = freigaben.Einstellungen.load()
        e.an = f.get("an") == "1"
        e.dlna_an = f.get("dlna_an") == "1"
        e.videos_optimieren = f.get("videos_optimieren") == "1"
        e.anmelde_ordner = f.get("anmelde_ordner") == "1"
        adresse = f.get("anmelde_adresse", e.anmelde_adresse).strip().rstrip("/")
        if adresse and not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d{1,5})?", adresse):
            raise freigaben.FreigabeError("Adresse der Dateien-Seite: z. B. http://192.168.178.2:8092 oder https://dateien.example.com")
        e.anmelde_adresse = adresse
        name = re.sub(r"[^A-Za-z0-9-]", "", f.get("netzwerkname", e.netzwerkname)).upper()[:15]
        if not name:
            raise freigaben.FreigabeError("Netzwerkname: Buchstaben, Zahlen und -")
        alt = e.netzwerkname
        e.netzwerkname = name
        e.dlna_name = " ".join(f.get("dlna_name", e.dlna_name).split())[:48] or "cloudy Speicher"
        e.save()
        if alt != name:
            await run_in_threadpool(freigaben.dienste.neustart_wsdd)
        await _apply()
        if e.videos_optimieren:
            medien.scan_anstossen()
    except (freigaben.FreigabeError, PermissionError) as exc:
        return _back(fehler=str(exc), to="/freigaben")
    return _back("Gespeichert", to="/freigaben")


@app.post("/freigaben/neu")
async def freigabe_neu(request: Request):
    try:
        f = await _form(request)
        name = freigaben.check_name(f.get("name", ""))
        ordner = freigaben.check_ordner(f.get("ordner", ""))
        standard = f.get("standard", "offen")
        if standard not in freigaben.REGELN:
            raise freigaben.FreigabeError("Unbekannte Regel")
        with freigaben._lock:
            e = freigaben.Einstellungen.load()
            if any(x.name.lower() == name.lower() for x in e.freigaben):
                raise freigaben.FreigabeError(f"„{name}“ gibt es schon")
            e.freigaben.append(freigaben.Freigabe(name=name, ordner=ordner, standard=standard, dlna=f.get("dlna") == "1"))
            e.save()
        await _apply()
    except (freigaben.FreigabeError, PermissionError) as exc:
        return _back(fehler=str(exc), to="/freigaben")
    return _back(f"Freigabe „{name}“ angelegt", to=_share_url(name))


@app.get("/freigaben/{name}", response_class=HTMLResponse)
async def freigabe_seite(request: Request, name: str):
    data = await run_in_threadpool(lambda: {**_status(), **_freigaben_data()})
    try:
        share = data["fe"].get(name)
    except freigaben.FreigabeError as exc:
        return _back(fehler=str(exc), to="/freigaben")
    return _page(request, "freigabe.html", active="/freigaben", share=share, share_url=_share_url(share.name), **data)


@app.post("/freigaben/{name}")
async def freigabe_speichern(request: Request, name: str):
    try:
        f = await _form(request)
        with freigaben._lock:
            e = freigaben.Einstellungen.load()
            share = e.get(name)
            neu_name = freigaben.check_name(f.get("name", share.name))
            if neu_name.lower() != share.name.lower() and any(x.name.lower() == neu_name.lower() for x in e.freigaben):
                raise freigaben.FreigabeError(f"„{neu_name}“ gibt es schon")
            share.name = neu_name
            share.ordner = freigaben.check_ordner(f.get("ordner", share.ordner))
            share.nur_lesen = f.get("nur_lesen") == "1"
            share.dlna = f.get("dlna") == "1"
            if f.get("standard") in freigaben.REGELN:
                share.standard = f["standard"]
            gewaehlt = [u for u in request.state.form.getlist("benutzer") if u in e.benutzer]
            if e.benutzer and f.get("alle_benutzer") != "1" and not gewaehlt:
                raise freigaben.FreigabeError("Wähle mindestens einen Benutzer oder „Alle Benutzer“")
            share.benutzer = [] if f.get("alle_benutzer") == "1" or set(gewaehlt) == set(e.benutzer) else gewaehlt
            e.save()
        await _apply()
    except (freigaben.FreigabeError, PermissionError) as exc:
        return _back(fehler=str(exc), to=_share_url(name))
    return _back("Gespeichert", to=_share_url(neu_name))


@app.post("/freigaben/{name}/geraete")
async def freigabe_geraete(request: Request, name: str):
    try:
        f = await _form(request)
        with freigaben._lock:
            e = freigaben.Einstellungen.load()
            share = e.get(name)
            for key, value in f.items():
                if not key.startswith("regel:"):
                    continue
                dev = key[6:]
                if value in freigaben.REGELN:
                    share.geraete[dev] = value
                else:
                    share.geraete.pop(dev, None)
            e.save()
        await _apply()
    except (freigaben.FreigabeError, PermissionError) as exc:
        return _back(fehler=str(exc), to=_share_url(name))
    return _back("Geräte-Regeln gespeichert", to=_share_url(name))


@app.post("/freigaben/{name}/rechte")
async def freigabe_rechte(request: Request, name: str):
    try:
        f = await _form(request)
        werte = {k[6:]: v for k, v in f.items() if k.startswith("recht:")}
        await run_in_threadpool(freigaben.set_freigabe_rechte, name, werte)
        await _apply()
    except (freigaben.FreigabeError, PermissionError) as exc:
        return _back(fehler=str(exc), to=_share_url(name))
    return _back("Rechte gespeichert", to=_share_url(name))


@app.post("/freigaben/{name}/loeschen")
async def freigabe_loeschen(request: Request, name: str):
    try:
        await _form(request)
        with freigaben._lock:
            e = freigaben.Einstellungen.load()
            share = e.get(name)
            e.freigaben.remove(share)
            e.save()
        await _apply()
    except (freigaben.FreigabeError, PermissionError) as exc:
        return _back(fehler=str(exc), to="/freigaben")
    return _back(f"Freigabe „{name}“ entfernt – die Dateien bleiben im Pool", to="/freigaben")


@app.post("/benutzer/neu")
async def benutzer_neu(request: Request):
    try:
        f = await _form(request)
        name = await run_in_threadpool(freigaben.add_user, f.get("name", ""), f.get("passwort", ""), f.get("windows", ""))
        await _apply()
    except (freigaben.FreigabeError, PermissionError) as exc:
        return _back(fehler=str(exc), to="/freigaben")
    return _back(f"Benutzer {name} angelegt", to="/freigaben")


@app.post("/benutzer/{name}/windows")
async def benutzer_windows(request: Request, name: str):
    try:
        f = await _form(request)
        await run_in_threadpool(freigaben.set_windows_names, name, f.get("windows", ""))
        await _apply()
    except (freigaben.FreigabeError, PermissionError) as exc:
        return _back(fehler=str(exc), to="/freigaben")
    return _back(f"Windows-Anmeldung von {name} gespeichert", to="/freigaben")


@app.post("/benutzer/{name}/passwort")
async def benutzer_passwort(request: Request, name: str):
    try:
        f = await _form(request)
        await run_in_threadpool(freigaben.change_password, name, f.get("passwort", ""))
    except (freigaben.FreigabeError, PermissionError) as exc:
        return _back(fehler=str(exc), to="/freigaben")
    return _back(f"Passwort von {name} geändert", to="/freigaben")


@app.post("/benutzer/{name}/rechte")
async def benutzer_rechte(request: Request, name: str):
    try:
        f = await _form(request)
        await run_in_threadpool(freigaben.set_recht, name, f.get("recht", ""))
        await _apply()
    except (freigaben.FreigabeError, PermissionError) as exc:
        return _back(fehler=str(exc), to="/freigaben")
    return _back(f"Rechte von {name}: {freigaben.RECHTE_TEXT[f['recht']]}", to="/freigaben")


@app.post("/geraete/abmelden")
async def geraet_abmelden(request: Request):
    try:
        f = await _form(request)
        await run_in_threadpool(freigaben.geraet_abmelden, f.get("key", ""))
        await _apply()
    except (freigaben.FreigabeError, PermissionError) as exc:
        return _back(fehler=str(exc), to="/freigaben")
    return _back("Gerät abgemeldet", to="/freigaben")


@app.post("/benutzer/{name}/loeschen")
async def benutzer_loeschen(request: Request, name: str):
    try:
        await _form(request)
        await run_in_threadpool(freigaben.delete_user, name)
        await _apply()
    except (freigaben.FreigabeError, PermissionError) as exc:
        return _back(fehler=str(exc), to="/freigaben")
    return _back(f"Benutzer {name} gelöscht", to="/freigaben")


@app.post("/geraete/neu")
async def geraet_neu(request: Request):
    f: dict = {}
    try:
        f = await _form(request)
        ip = f.get("ip", "").strip()
        if not freigaben.IPV4_RE.fullmatch(ip):
            raise freigaben.FreigabeError("Bitte eine IPv4-Adresse wie 192.168.178.50")
        name = " ".join(f.get("name", "").split())[:60] or ip
        with freigaben._lock:
            e = freigaben.Einstellungen.load()
            e.eigene_geraete[f"ip:{ip}"] = name
            e.save()
        freigaben._cache["t"] = 0
        await _apply()
    except (freigaben.FreigabeError, PermissionError) as exc:
        return _back(fehler=str(exc), to=f.get("zurueck", "/freigaben"))
    return _back(f"{name} hinzugefügt", to=f.get("zurueck", "/freigaben"))
