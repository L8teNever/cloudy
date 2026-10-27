import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import db, warp
from .certs import ca_cert_path, ensure_certs, start_https_server
from .dnsmasq import Collector, DnsmasqRunner
from .presence import PresenceMonitor
from .settings import Settings
from .traffic import DomainMap, TrafficMeter, TrafficRunner, setup_gateway

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
basic = HTTPBasic(auto_error=False)


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return f"{n:.1f} TB".replace(".", ",")


templates.env.filters["bytes"] = fmt_bytes


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    db.init()
    runner = traffic_runner = warp_router = presence = None
    domain_map = DomainMap()
    if settings.dnsmasq_enabled and settings.gateway_enabled:
        if setup_gateway(settings):
            traffic_runner = TrafficRunner(TrafficMeter(settings, domain_map), settings.traffic_poll_seconds)
            traffic_runner.start()
        else:
            # Ohne funktionierende Weiterleitung den Pi NICHT als Gateway verteilen, sonst ist das Heimnetz offline.
            print("cloudy: Gateway konnte nicht eingerichtet werden – Geräte bleiben bei der Fritzbox", flush=True)
            settings = replace(settings, gateway_enabled=False)
    if settings.gateway_enabled and settings.warp_slots:
        warp_router = warp.WarpRouter(settings)
        warp_router.start()
    elif settings.dnsmasq_enabled:
        try:
            warp.cleanup()
        except OSError:
            pass
    app.state.settings = settings
    if settings.dnsmasq_enabled:
        presence = PresenceMonitor(settings)
        presence.start()
    app.state.presence = presence
    if settings.dnsmasq_enabled:
        runner = DnsmasqRunner(settings, Collector(domain_map=domain_map))
        runner.start()
    https_server = None
    if settings.https_port:
        try:
            certfile, keyfile = ensure_certs(settings)
            https_server = start_https_server(app, certfile, keyfile, settings.https_port)
            print(f"cloudy: HTTPS an (Port {settings.https_port})", flush=True)
        except Exception as exc:  # noqa: BLE001 – ohne HTTPS läuft das Dashboard weiter über HTTP
            print(f"cloudy: HTTPS nicht verfügbar: {exc}", flush=True)
    yield
    if https_server:
        https_server.should_exit = True
    if runner:
        runner.stop()
    if traffic_runner:
        traffic_runner.stop()
    if warp_router:
        warp_router.stop()
    if presence:
        presence.stop()


app = FastAPI(title="cloudy", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def require_password(credentials: HTTPBasicCredentials | None = Depends(basic)) -> None:
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        return
    ok = credentials is not None and secrets.compare_digest(
        credentials.password.encode(), password.encode()
    )
    if not ok:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


def fmt_time(ts: int) -> str:
    return time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(ts))


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/ca.crt")
async def ca_certificate(request: Request):
    """Öffentliches CA-Zertifikat zum Installieren auf den Geräten (enthält keinen geheimen Schlüssel)."""
    path = ca_cert_path(request.app.state.settings)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="HTTPS ist nicht eingeschaltet")
    return FileResponse(path, media_type="application/x-x509-ca-cert", filename="cloudy-ca.crt")


def warp_profiles(settings: Settings) -> list[dict]:
    labels = db.warp_labels()
    count: dict[str, int] = {}
    profiles = []
    for slot, _ in settings.warp_slots:
        k = warp.kind(slot)
        count[k] = count.get(k, 0) + 1
        profiles.append((slot, k, f"{'VPN' if k == 'vpn' else 'WARP'} {count[k]}"))
    return [
        {
            "slot": slot,
            "kind": k,
            "label": labels.get(slot) or default_label,
            "has_token": warp.has_token(settings, slot),
            "status": warp.read_status(settings, slot),
        }
        for slot, k, default_label in profiles
    ]


def online_ips(request: Request) -> frozenset[str]:
    presence = getattr(request.app.state, "presence", None)
    return presence.online if presence else frozenset()


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_password)])
async def dashboard(request: Request, ip: str | None = None):
    settings = request.app.state.settings
    context = {"settings": settings, "fmt_time": fmt_time, "ip": ip}
    context["warp"] = await run_in_threadpool(warp_profiles, settings)
    context["online"] = online_ips(request)
    context["warp_default"] = await run_in_threadpool(db.get_setting, warp.DEFAULT_KEY)
    context["warp_names"] = {w["slot"]: w["label"] for w in context["warp"]}
    context["warp_by_mac"] = await run_in_threadpool(db.device_warp)
    if ip:
        context["device"] = await run_in_threadpool(db.device, ip)
        context["domains"] = await run_in_threadpool(db.top_domains, ip)
        context["traffic"] = await run_in_threadpool(db.traffic_for, ip)
        context["recent"] = await run_in_threadpool(db.recent, 200, ip)
    else:
        context["devices"] = await run_in_threadpool(db.devices)
        context["recent"] = await run_in_threadpool(db.recent, 100)
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/api/devices", dependencies=[Depends(require_password)])
async def api_devices(request: Request):
    online = online_ips(request)
    devices = await run_in_threadpool(db.devices)
    return [{**d, "online": d["ip"] in online} for d in devices]


@app.get("/api/devices/{ip}", dependencies=[Depends(require_password)])
async def api_device(ip: str):
    return {
        "device": await run_in_threadpool(db.device, ip),
        "domains": await run_in_threadpool(db.top_domains, ip),
        "traffic": await run_in_threadpool(db.traffic_for, ip),
    }


@app.get("/api/recent", dependencies=[Depends(require_password)])
async def api_recent(limit: int = 100, ip: str | None = None):
    return await run_in_threadpool(db.recent, min(limit, 1000), ip)


def _check_slot(request: Request, slot: str) -> Settings:
    settings = request.app.state.settings
    if slot not in {name for name, _ in settings.warp_slots}:
        raise HTTPException(status_code=404, detail="Unbekanntes WARP-Profil")
    return settings


@app.post("/warp/{slot}/login", dependencies=[Depends(require_password)])
async def warp_login(
    request: Request, slot: str, token: str = Form(""), config: str = Form(""), label: str = Form("")
):
    settings = _check_slot(request, slot)
    token = token.strip()
    if warp.kind(slot) == "vpn":
        if config.strip():
            try:
                await run_in_threadpool(warp.save_vpn_config, settings, slot, config)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        token = ""
    if label.strip():
        await run_in_threadpool(db.set_warp_label, slot, label.strip()[:40])
    if token:
        if len(token) > 8192 or any(ch.isspace() for ch in token):
            raise HTTPException(status_code=400, detail="Ungültiger Token")
        await run_in_threadpool(warp.save_token, settings, slot, token)
    return RedirectResponse("/#warp", status_code=303)


@app.post("/warp/{slot}/logout", dependencies=[Depends(require_password)])
async def warp_logout(request: Request, slot: str):
    settings = _check_slot(request, slot)
    await run_in_threadpool(warp.remove_token, settings, slot)
    return RedirectResponse("/#warp", status_code=303)


@app.post("/devices/warp", dependencies=[Depends(require_password)])
async def device_set_warp(request: Request, mac: str = Form(...), slot: str = Form(""), ip: str = Form("")):
    mac = mac.strip().lower()
    if not warp.MAC_RE.match(mac):
        raise HTTPException(status_code=400, detail="Ungültige MAC-Adresse")
    if slot and slot != warp.DIRECT:
        _check_slot(request, slot)
    await run_in_threadpool(db.set_device_warp, mac, slot or None)
    return RedirectResponse(f"/?ip={quote(ip)}" if ip else "/", status_code=303)


@app.post("/warp/default", dependencies=[Depends(require_password)])
async def warp_set_default(request: Request, slot: str = Form("")):
    """Wohin alle Geräte gehen, die keine eigene Einstellung haben ("" = normal über die Fritzbox)."""
    if slot:
        _check_slot(request, slot)
    await run_in_threadpool(db.set_setting, warp.DEFAULT_KEY, slot)
    return RedirectResponse("/", status_code=303)
