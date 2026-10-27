import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import db
from .dnsmasq import Collector, DnsmasqRunner
from .settings import Settings

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
basic = HTTPBasic(auto_error=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    app.state.settings = settings
    db.init()
    runner = None
    if settings.dnsmasq_enabled:
        runner = DnsmasqRunner(settings, Collector())
        runner.start()
    yield
    if runner:
        runner.stop()


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


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_password)])
async def dashboard(request: Request, ip: str | None = None):
    context = {"settings": request.app.state.settings, "fmt_time": fmt_time, "ip": ip}
    if ip:
        context["device"] = await run_in_threadpool(db.device, ip)
        context["domains"] = await run_in_threadpool(db.top_domains, ip)
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
    }


@app.get("/api/recent", dependencies=[Depends(require_password)])
async def api_recent(limit: int = 100, ip: str | None = None):
    return await run_in_threadpool(db.recent, min(limit, 1000), ip)
