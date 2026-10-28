import os
import secrets
import socket
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import blocklist, certs, db, warp
from .certs import ca_cert_path, ensure_certs, start_https_server
from .dnsmasq import Collector, DnsmasqRunner
from .presence import PresenceMonitor
from .redirects import find_rule
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


def fmt_ago(ts: int | None, now: float | None = None) -> str:
    if not ts:
        return ""
    if now is None:
        now = time.time()
    diff = max(0, int(now - ts))
    if diff < 5:
        return "gerade eben"
    if diff < 60:
        return f"vor {diff}s"
    if diff < 3600:
        return f"vor {diff // 60}m"
    if diff < 86400:
        return f"vor {diff // 3600}h"
    return f"vor {diff // 86400}d"


templates.env.filters["bytes"] = fmt_bytes
templates.env.filters["ago"] = fmt_ago


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
    app.state.runner = runner

    # HTTP auf Standard-Port 80 anbieten, damit Mobilgeräte und Browser ohne :8080 verbinden können.
    http_80_server = None
    dashboard_port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    if dashboard_port != 80 and "PYTEST_CURRENT_TEST" not in os.environ:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
                _s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                _s.bind(("0.0.0.0", 80))
            http_80_config = uvicorn.Config(
                app, host="0.0.0.0", port=80, lifespan="off", log_level="warning"
            )
            http_80_server = uvicorn.Server(http_80_config)

            def _run_80():
                try:
                    http_80_server.run()
                except BaseException as exc:  # noqa: BLE001
                    print(f"cloudy: Port 80 Server beendet: {exc}", flush=True)

            threading.Thread(target=_run_80, daemon=True).start()
            print("cloudy: HTTP auch auf Standard-Port 80 an", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"cloudy: Port 80 nicht gebunden: {exc}", flush=True)

    https_server = None
    if settings.https_port:
        try:
            certfile, keyfile = ensure_certs(settings)
            def redirect_cert(name: str, _s=settings):
                rule = find_rule(name, db.redirect_map())
                return certs.ensure_domain_cert(_s, rule[0]) if rule else None

            https_server = start_https_server(app, certfile, keyfile, settings.https_port, sni_cert=redirect_cert)
            print(f"cloudy: HTTPS an (Port {settings.https_port})", flush=True)
        except Exception as exc:  # noqa: BLE001 – ohne HTTPS läuft das Dashboard weiter über HTTP
            print(f"cloudy: HTTPS nicht verfügbar: {exc}", flush=True)
    yield
    if https_server:
        https_server.should_exit = True
    if http_80_server:
        http_80_server.should_exit = True
    if runner:
        runner.stop()
    if traffic_runner:
        traffic_runner.stop()
    if warp_router:
        warp_router.stop()
    if presence:
        presence.stop()


app = FastAPI(title="cloudy", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def redirect_middleware(request: Request, call_next):
    raw_host = request.headers.get("host", "").split(":")[0].strip().lower()
    settings = getattr(request.app.state, "settings", None)
    own_hosts = {"localhost", "127.0.0.1"}
    if settings:
        own_hosts.add(settings.pi_ip.lower())
        if settings.local_name:
            own_hosts.add(settings.local_name.lower())
            short = settings.local_name.split(".")[0].lower()
            if short:
                own_hosts.add(short)

    if raw_host and raw_host not in own_hosts:
        rule = find_rule(raw_host, await run_in_threadpool(db.redirect_map))
        if rule:
            return RedirectResponse(rule[1], status_code=302)

    return await call_next(request)


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


@app.get("/ca.mobileconfig")
async def ca_mobileconfig(request: Request):
    """Apple Configuration Profile für iOS/iPadOS/macOS für 1-Klick-Installation des Zertifikats."""
    settings = request.app.state.settings
    path = ca_cert_path(settings)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="HTTPS ist nicht eingeschaltet")
    content = await run_in_threadpool(certs.make_mobileconfig, settings)
    return Response(
        content=content,
        media_type="application/x-apple-aspen-config",
        headers={"Content-Disposition": 'attachment; filename="cloudy-ca.mobileconfig"'},
    )


@app.get("/install-ca.bat")
async def ca_install_bat(request: Request):
    """Windows-Batch-Skript zur 1-Klick-Installation des CA-Zertifikats."""
    settings = request.app.state.settings
    path = ca_cert_path(settings)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="HTTPS ist nicht eingeschaltet")
    content = await run_in_threadpool(certs.make_windows_bat, settings)
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="install-cloudy-ca.bat"'},
    )


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
async def dashboard(request: Request, ip: str | None = None, fehler: str | None = None):
    settings = request.app.state.settings
    context = {"settings": settings, "fmt_time": fmt_time, "fmt_ago": fmt_ago, "ip": ip, "fehler": fehler}
    context["warp"] = await run_in_threadpool(warp_profiles, settings)
    context["online"] = online_ips(request)
    context["warp_default"] = await run_in_threadpool(db.get_setting, warp.DEFAULT_KEY)
    context["ipv6_bypass"] = settings.gateway_enabled and await run_in_threadpool(warp.ipv6_bypass, settings.lan_interface)
    context["vpn_in_use"] = bool(context["warp_default"]) or any(
        slot != warp.DIRECT for slot in (await run_in_threadpool(db.device_warp)).values()
    )
    context["warp_names"] = {w["slot"]: w["label"] for w in context["warp"]}
    context["warp_by_mac"] = await run_in_threadpool(db.device_warp)
    context["blocklist_enabled"] = await run_in_threadpool(blocklist.is_enabled)
    context["blocklist_paused_until"] = await run_in_threadpool(blocklist.paused_until)
    context["whitelist"] = await run_in_threadpool(db.whitelist)
    context["redirects"] = await run_in_threadpool(db.redirects)
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
async def api_devices(request: Request, refresh: bool = False):
    presence = getattr(request.app.state, "presence", None)
    if refresh and presence and hasattr(presence, "tick"):
        await run_in_threadpool(presence.tick)
    online = online_ips(request)
    devices = await run_in_threadpool(db.devices)
    return [{**d, "online": d["ip"] in online} for d in devices]


@app.post("/api/presence/refresh", dependencies=[Depends(require_password)])
async def api_presence_refresh(request: Request):
    presence = getattr(request.app.state, "presence", None)
    if presence and hasattr(presence, "tick"):
        await run_in_threadpool(presence.tick)
        return {"online": sorted(presence.online)}
    return {"online": []}


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


def _reload_dnsmasq(request: Request) -> None:
    runner = getattr(request.app.state, "runner", None)
    if runner and hasattr(runner, "reload_hosts"):
        runner.reload_hosts()


@app.post("/blocklist/toggle", dependencies=[Depends(require_password)])
async def blocklist_toggle(request: Request):
    current = blocklist.is_enabled()
    blocklist.set_enabled(not current)
    if current:
        blocklist.resume()
    await run_in_threadpool(blocklist.generate_blocklist, request.app.state.settings)
    _reload_dnsmasq(request)
    return RedirectResponse("/#blocklist", status_code=303)


@app.post("/blocklist/pause", dependencies=[Depends(require_password)])
async def blocklist_pause(request: Request, minutes: int = Form(15)):
    blocklist.pause_for_seconds(max(1, min(minutes, 1440)) * 60)
    await run_in_threadpool(blocklist.generate_blocklist, request.app.state.settings)
    _reload_dnsmasq(request)
    return RedirectResponse("/#blocklist", status_code=303)


@app.post("/blocklist/resume", dependencies=[Depends(require_password)])
async def blocklist_resume(request: Request):
    blocklist.resume()
    await run_in_threadpool(blocklist.generate_blocklist, request.app.state.settings)
    _reload_dnsmasq(request)
    return RedirectResponse("/#blocklist", status_code=303)


@app.post("/blocklist/whitelist/add", dependencies=[Depends(require_password)])
async def blocklist_whitelist_add(request: Request, domain: str = Form(...)):
    await run_in_threadpool(db.add_whitelist, domain)
    await run_in_threadpool(blocklist.generate_blocklist, request.app.state.settings)
    _reload_dnsmasq(request)
    return RedirectResponse("/#blocklist", status_code=303)


@app.post("/blocklist/whitelist/remove", dependencies=[Depends(require_password)])
async def blocklist_whitelist_remove(request: Request, domain: str = Form(...)):
    await run_in_threadpool(db.remove_whitelist, domain)
    await run_in_threadpool(blocklist.generate_blocklist, request.app.state.settings)
    _reload_dnsmasq(request)
    return RedirectResponse("/#blocklist", status_code=303)

def _apply_redirects(request: Request) -> None:
    from .dnsmasq import sync_redirects_hosts

    sync_redirects_hosts(request.app.state.settings)
    runner = getattr(request.app.state, "runner", None)
    if runner and hasattr(runner, "restart"):
        runner.restart()


@app.post("/redirects/add", dependencies=[Depends(require_password)])
async def redirects_add(request: Request, source: str = Form(...), target: str = Form(...)):
    try:
        await run_in_threadpool(db.add_redirect, source, target)
    except ValueError as exc:
        return RedirectResponse(f"/?fehler={quote(str(exc))}#redirects", status_code=303)
    await run_in_threadpool(_apply_redirects, request)
    return RedirectResponse("/#redirects", status_code=303)


@app.post("/redirects/remove", dependencies=[Depends(require_password)])
async def redirects_remove(request: Request, source: str = Form(...)):
    await run_in_threadpool(db.remove_redirect, source)
    await run_in_threadpool(_apply_redirects, request)
    return RedirectResponse("/#redirects", status_code=303)
