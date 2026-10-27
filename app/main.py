import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import db
from .certs import ca_cert_path, ensure_certs, start_https_server
from .dnsmasq import Collector, DnsmasqRunner
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
    runner = traffic_runner = None
    domain_map = DomainMap()
    if settings.dnsmasq_enabled and settings.gateway_enabled:
        if setup_gateway(settings):
            traffic_runner = TrafficRunner(TrafficMeter(settings, domain_map), settings.traffic_poll_seconds)
            traffic_runner.start()
        else:
            # Ohne funktionierende Weiterleitung den Pi NICHT als Gateway verteilen, sonst ist das Heimnetz offline.
            print("cloudy: Gateway konnte nicht eingerichtet werden – Geräte bleiben bei der Fritzbox", flush=True)
            settings = replace(settings, gateway_enabled=False)
    app.state.settings = settings
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


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_password)])
async def dashboard(request: Request, ip: str | None = None):
    context = {"settings": request.app.state.settings, "fmt_time": fmt_time, "ip": ip}
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
async def api_devices():
    return await run_in_threadpool(db.devices)


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
