import ipaddress
import os
import secrets
import socket
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import db

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
basic = HTTPBasic(auto_error=False)

# Diese Pfade gehören zum Dashboard selbst und werden nicht protokolliert.
UNLOGGED_PATHS = {"/", "/api/logs", "/healthz", "/favicon.ico"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="cloudy", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def client_ip(request: Request) -> str:
    if os.environ.get("TRUST_PROXY") == "1":
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unbekannt"


@lru_cache(maxsize=1024)
def resolve_hostname(ip: str) -> str | None:
    # Hostnamen gibt es nur im Heimnetz (Fritzbox-DNS); öffentliche IPs nicht auflösen.
    try:
        if not ipaddress.ip_address(ip).is_private:
            return None
        socket.setdefaulttimeout(1.0)
        return socket.gethostbyaddr(ip)[0]
    except (ValueError, OSError):
        return None


def require_password(credentials: HTTPBasicCredentials | None = Depends(basic)) -> None:
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        return
    ok = credentials is not None and secrets.compare_digest(
        credentials.password.encode(), password.encode()
    )
    if not ok:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


@app.middleware("http")
async def log_request(request: Request, call_next):
    if request.url.path not in UNLOGGED_PATHS:
        ip = client_ip(request)
        hostname = await run_in_threadpool(resolve_hostname, ip)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        await run_in_threadpool(
            db.insert, ip, hostname, request.method, target, request.headers.get("user-agent")
        )
    return await call_next(request)


def fmt_time(ts: int) -> str:
    return time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(ts))


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_password)])
async def dashboard(request: Request):
    devices = await run_in_threadpool(db.devices)
    recent = await run_in_threadpool(db.recent, 100)
    return templates.TemplateResponse(
        request, "index.html", {"devices": devices, "recent": recent, "fmt_time": fmt_time}
    )


@app.get("/api/logs", dependencies=[Depends(require_password)])
async def api_logs():
    return JSONResponse(
        {
            "devices": await run_in_threadpool(db.devices),
            "recent": await run_in_threadpool(db.recent, 100),
        }
    )


@app.api_route(
    "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
)
async def catch_all(path: str):
    return HTMLResponse("<!doctype html><title>cloudy</title><p>OK</p>")
