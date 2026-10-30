import os
import re
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

from . import access, blocklist, certs, db, fritz, localdomain, portal as portal_app, views, warp
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

    # Geräte-Portal (eigene Seite, eigener Port) – nur erreichbar über den Cloudflare Tunnel.
    portal_server = None
    if settings.portal_port and "PYTEST_CURRENT_TEST" not in os.environ:
        try:
            portal_app.portal.state.settings = settings
            portal_app.portal.state.presence = presence
            portal_server = uvicorn.Server(
                uvicorn.Config(portal_app.portal, host="0.0.0.0", port=settings.portal_port, lifespan="off", log_level="warning")
            )

            def _run_portal():
                try:
                    portal_server.run()
                except BaseException as exc:  # noqa: BLE001 – das Portal darf das Dashboard nie verhindern
                    print(f"cloudy: Geräte-Portal beendet: {exc}", flush=True)

            threading.Thread(target=_run_portal, daemon=True).start()
            print(f"cloudy: Geräte-Portal an (Port {settings.portal_port})", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"cloudy: Geräte-Portal nicht gestartet: {exc}", flush=True)

    https_server = None
    if settings.https_port:
        try:
            certfile, keyfile = ensure_certs(settings)
            def redirect_cert(name: str, _s=settings):
                own = localdomain.sni_pair(_s, name)
                if own:
                    return own
                rule = find_rule(name, db.redirect_map())
                return certs.ensure_domain_cert(_s, rule[0]) if rule else None

            https_server = start_https_server(app, certfile, keyfile, settings.https_port, sni_cert=redirect_cert)
            print(f"cloudy: HTTPS an (Port {settings.https_port})", flush=True)
        except Exception as exc:  # noqa: BLE001 – ohne HTTPS läuft das Dashboard weiter über HTTP
            print(f"cloudy: HTTPS nicht verfügbar: {exc}", flush=True)
    renew_stop = threading.Event()
    if "PYTEST_CURRENT_TEST" not in os.environ:
        threading.Thread(target=localdomain.renew_loop, args=(settings, renew_stop), daemon=True).start()
    yield
    renew_stop.set()
    if portal_server:
        portal_server.should_exit = True
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
    own_domain = await run_in_threadpool(localdomain.domain)
    dash_domain = await run_in_threadpool(localdomain.dashboard_domain)
    if dash_domain and raw_host == dash_domain and request.url.scheme == "http" and not from_tunnel(request):
        # Dashboard unter der eigenen Domain: Passwort und Daten nur verschlüsselt, sobald das Zertifikat da ist.
        if settings and settings.https_port and await run_in_threadpool(localdomain.sni_pair, settings, raw_host):
            port = "" if settings.https_port == 443 else f":{settings.https_port}"
            return RedirectResponse(f"https://{dash_domain}{port}{request.url.path}" + (f"?{request.url.query}" if request.url.query else ""), status_code=302)
    if own_domain and raw_host == own_domain:
        # Eigene Domain nur im Heimnetz (z. B. main.kulbarts.com): zeigt ausschließlich „Mein Gerät“.
        path = request.url.path
        if from_tunnel(request):
            return Response("Nur im Heimnetz.", status_code=404)
        if request.url.scheme == "http" and settings and settings.https_port and await run_in_threadpool(localdomain.sni_pair, settings, raw_host):
            port = "" if settings.https_port == 443 else f":{settings.https_port}"
            return RedirectResponse(f"https://{own_domain}{port}/mein", status_code=302)
        if path == "/":
            return RedirectResponse("/mein", status_code=302)
        if path not in ("/healthz", "/favicon.ico", "/mein") and not path.startswith("/mein/"):
            return Response("Nicht gefunden.", status_code=404)
        return await call_next(request)
    own_hosts = {"localhost", "127.0.0.1"}
    own_hosts.update(d for d in (own_domain, dash_domain) if d)
    if settings:
        own_hosts.add(settings.pi_ip.lower())
        if settings.local_name:
            own_hosts.add(settings.local_name.lower())
            short = settings.local_name.split(".")[0].lower()
            if short:
                own_hosts.add(short)

    if raw_host and raw_host not in own_hosts and not from_tunnel(request):
        rule = find_rule(raw_host, await run_in_threadpool(db.redirect_map))
        if rule:
            return RedirectResponse(rule[1], status_code=302)

    return await call_next(request)


def from_tunnel(request: Request) -> bool:
    """Kommt die Anfrage über den Cloudflare Tunnel (Fernzugriff) statt aus dem Heimnetz?"""
    settings = getattr(request.app.state, "settings", None)
    host = request.client.host if request.client else ""
    return bool(settings and settings.tunnel_ip and host == settings.tunnel_ip)


def _tunnel_denied(title: str, text: str) -> HTMLResponse:
    body = (
        '<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f"<title>cloudy – {title}</title><body style=\"font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem\">"
        f"<h1>🔒 {title}</h1><p>{text}</p></body></html>"
    )
    return HTMLResponse(body, status_code=403)


# Wird nach redirect_middleware registriert und läuft damit zuerst.
@app.middleware("http")
async def tunnel_guard(request: Request, call_next):
    """Fernzugriff: Nur mit gültiger Cloudflare-Access-Anmeldung, sonst nichts – auch wenn bei Cloudflare
    die Access-App fehlt oder falsch eingestellt ist."""
    if not from_tunnel(request):
        return await call_next(request)
    path = request.url.path
    if path == "/mein" or path.startswith("/mein/"):
        return _tunnel_denied("Nur im Heimnetz", "„Mein Gerät“ erkennt dein Gerät im WLAN und geht deshalb nur zu Hause.")
    team, aud = await run_in_threadpool(access.config)
    token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization") or ""
    email = await run_in_threadpool(access.verify, token, team, aud) if team and aud and token else None
    if not email:
        return _tunnel_denied(
            "Nur mit Cloudflare-Anmeldung",
            "Der Fernzugriff auf cloudy braucht eine Anmeldung über Cloudflare Zero Trust. "
            "Ist sie eingerichtet (Einstellungen → Fernzugriff: Team-Domain und AUD), Seite neu laden.",
        )
    request.state.access_email = email
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


def client_ip(request: Request) -> str:
    """IP des aufrufenden Geräts im Heimnetz (ohne Proxy – cloudy läuft direkt auf dem Pi)."""
    host = (request.client.host if request.client else "") or ""
    return host.strip()


app.include_router(views.router)


@app.get("/mein", response_class=HTMLResponse)
async def mein_geraet(request: Request, ok: int | None = None, fehler: str | None = None):
    """Öffentliche Self-Service-Seite: nur das eigene Gerät, erkannt über die Client-IP."""
    ip = client_ip(request)
    device = await run_in_threadpool(db.device, ip) if ip else None
    settings = request.app.state.settings
    profiles = await run_in_threadpool(warp_profiles, settings)
    warp_by_mac = await run_in_threadpool(db.device_warp)
    warp_default = await run_in_threadpool(db.get_setting, warp.DEFAULT_KEY)
    labels = {w["slot"]: w["label"] for w in profiles}
    online = online_ips(request)
    return templates.TemplateResponse(
        request,
        "seiten/mein.html",
        {
            "title": "Mein Gerät",
            "ip": ip,
            "device": device,
            "online": online,
            "warp": profiles,
            "warp_names": labels,
            "warp_by_mac": warp_by_mac,
            "warp_default": warp_default,
            "settings": settings,
            "ok": bool(ok),
            "fehler": fehler,
        },
    )


@app.post("/mein/koppeln")
async def mein_koppeln(request: Request, code: str = Form("")):
    """Kopplungscode vom Geräte-Portal einlösen: das Portal-Gerät gehört dann zu diesem Gerät (MAC aus Client-IP)."""
    ip = client_ip(request)
    device = await run_in_threadpool(db.device, ip) if ip else None
    mac = ((device or {}).get("mac") or "").strip().lower()
    if not warp.MAC_RE.match(mac):
        return RedirectResponse(f"/mein?fehler={quote('Gerät noch nicht erkannt – bitte kurz warten.')}", status_code=303)
    if not await run_in_threadpool(db.pairing_redeem, code, mac):
        return RedirectResponse(f"/mein?fehler={quote('Code ungültig oder abgelaufen.')}", status_code=303)
    return RedirectResponse("/mein?ok=1", status_code=303)


@app.post("/mein/warp")
async def mein_set_warp(request: Request, slot: str = Form("")):
    """VPN/WARP nur für das Gerät umstellen, das gerade die Seite aufruft (MAC aus Client-IP)."""
    ip = client_ip(request)
    device = await run_in_threadpool(db.device, ip) if ip else None
    mac = (device or {}).get("mac") or ""
    mac = mac.strip().lower()
    if not warp.MAC_RE.match(mac):
        return RedirectResponse(
            f"/mein?fehler={quote('Gerät noch nicht erkannt – bitte kurz warten oder WLAN neu verbinden.')}",
            status_code=303,
        )
    if slot and slot != warp.DIRECT:
        _check_slot(request, slot)
    await run_in_threadpool(db.set_device_warp, mac, slot or None)
    return RedirectResponse("/mein?ok=1", status_code=303)


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


@app.post("/tunnel/login", dependencies=[Depends(require_password)])
async def tunnel_login(request: Request, token: str = Form(""), team: str = Form(""), aud: str = Form("")):
    """Fernzugriff einrichten: Tunnel-Token (leer = behalten) und Access-Daten (Team-Domain, AUD)."""
    settings = request.app.state.settings
    token = token.strip()
    try:
        team_n = access.normalize_team(team) if team.strip() else ""
        aud_n = access.normalize_aud(aud) if aud.strip() else ""
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if token and (len(token) > 8192 or any(ch.isspace() for ch in token)):
        raise HTTPException(status_code=400, detail="Ungültiger Token")
    if team_n:
        await run_in_threadpool(db.set_setting, access.TEAM_KEY, team_n)
    if aud_n:
        await run_in_threadpool(db.set_setting, access.AUD_KEY, aud_n)
    if token:
        await run_in_threadpool(warp.save_token, settings, access.SLOT, token)
    return RedirectResponse("/einstellungen#fernzugriff", status_code=303)


@app.post("/tunnel/logout", dependencies=[Depends(require_password)])
async def tunnel_logout(request: Request):
    """Tunnel aus: Token löschen (der Container beendet cloudflared). Team/AUD bleiben gespeichert."""
    await run_in_threadpool(warp.remove_token, request.app.state.settings, access.SLOT)
    return RedirectResponse("/einstellungen#fernzugriff", status_code=303)


@app.post("/lokale-domain", dependencies=[Depends(require_password)])
async def lokale_domain_set(
    request: Request, name: str = Form(""), dashboard: str = Form(""), cf_token: str = Form(""), email: str = Form("")
):
    """Eigene Domains nur im Heimnetz einrichten (Dashboard und/oder „Mein Gerät“) und das Let's-Encrypt-Zertifikat holen."""
    try:
        name = localdomain.normalize_domain(name) if name.strip() else ""
        dashboard = localdomain.normalize_domain(dashboard) if dashboard.strip() else ""
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if name and name == dashboard:
        raise HTTPException(status_code=400, detail="Dashboard und „Mein Gerät“ brauchen verschiedene Namen")
    cf_token = cf_token.strip()
    if cf_token and (len(cf_token) > 512 or any(ch.isspace() for ch in cf_token)):
        raise HTTPException(status_code=400, detail="Ungültiger API-Token")
    email = email.strip()
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="Ungültige E-Mail-Adresse")
    await run_in_threadpool(db.set_setting, localdomain.DOMAIN_KEY, name)
    await run_in_threadpool(db.set_setting, localdomain.DASH_KEY, dashboard)
    await run_in_threadpool(db.set_setting, localdomain.EMAIL_KEY, email)
    if cf_token:
        await run_in_threadpool(db.set_setting, localdomain.TOKEN_KEY, cf_token)
    if not name and not dashboard:
        await run_in_threadpool(db.set_setting, localdomain.TOKEN_KEY, "")
    await run_in_threadpool(_apply_redirects, request)  # schreibt auch die DNS-Zeilen für die Domains
    if name or dashboard:
        localdomain.start_in_background(request.app.state.settings)
    return RedirectResponse("/einstellungen#lokale-domain", status_code=303)


@app.post("/lokale-domain/neu", dependencies=[Depends(require_password)])
async def lokale_domain_neu(request: Request):
    localdomain.start_in_background(request.app.state.settings)
    return RedirectResponse("/einstellungen#lokale-domain", status_code=303)


@app.post("/portal/config", dependencies=[Depends(require_password)])
async def portal_config(aud: str = Form(""), cf_token: str = Form(""), cf_account: str = Form("")):
    """Geräte-Portal einrichten: AUD der Portal-Access-App, optional Cloudflare-API (Zero Trust: Read) für die Geräte-Zuordnung."""
    try:
        aud_n = access.normalize_aud(aud) if aud.strip() else ""
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    account = cf_account.strip().lower()
    if account and not re.match(r"^[0-9a-f]{32}$", account):
        raise HTTPException(status_code=400, detail="Account-ID ungültig (32 Hex-Zeichen)")
    cf_token = cf_token.strip()
    if cf_token and (len(cf_token) > 512 or any(ch.isspace() for ch in cf_token)):
        raise HTTPException(status_code=400, detail="Ungültiger API-Token")
    if aud_n:
        await run_in_threadpool(db.set_setting, access.PORTAL_AUD_KEY, aud_n)
    if account:
        await run_in_threadpool(db.set_setting, access.CF_ACCOUNT_KEY, account)
    if cf_token:
        await run_in_threadpool(db.set_setting, access.CF_TOKEN_KEY, cf_token)
    return RedirectResponse("/einstellungen#portal", status_code=303)


@app.post("/portal/pair", dependencies=[Depends(require_password)])
async def portal_pair(code: str = Form(""), mac: str = Form("")):
    """Kopplungscode im Dashboard einlösen und einem Gerät zuordnen."""
    mac = mac.strip().lower()
    if not warp.MAC_RE.match(mac):
        raise HTTPException(status_code=400, detail="Ungültige MAC-Adresse")
    if not await run_in_threadpool(db.pairing_redeem, code, mac):
        return RedirectResponse(f"/einstellungen?fehler={quote('Code ungültig oder abgelaufen.')}#portal", status_code=303)
    return RedirectResponse("/einstellungen#portal", status_code=303)


@app.post("/portal/unlink", dependencies=[Depends(require_password)])
async def portal_unlink(cf_device_id: str = Form(...)):
    await run_in_threadpool(db.portal_unlink, cf_device_id)
    return RedirectResponse("/einstellungen#portal", status_code=303)


@app.post("/devices/warp", dependencies=[Depends(require_password)])
async def device_set_warp(request: Request, mac: str = Form(...), slot: str = Form(""), ip: str = Form("")):
    mac = mac.strip().lower()
    if not warp.MAC_RE.match(mac):
        raise HTTPException(status_code=400, detail="Ungültige MAC-Adresse")
    if slot and slot != warp.DIRECT:
        _check_slot(request, slot)
    await run_in_threadpool(db.set_device_warp, mac, slot or None)
    return RedirectResponse(f"/?ip={quote(ip)}" if ip else "/", status_code=303)


@app.post("/devices/isolate", dependencies=[Depends(require_password)])
async def device_set_isolate(request: Request, mac: str = Form(...), isolated: int = Form(0), ip: str = Form("")):
    mac = mac.strip().lower()
    if not warp.MAC_RE.match(mac):
        raise HTTPException(status_code=400, detail="Ungültige MAC-Adresse")
    await run_in_threadpool(db.set_isolated, mac, bool(isolated))
    settings = request.app.state.settings
    if settings.gateway_enabled:
        from . import traffic
        await run_in_threadpool(traffic.apply_isolation, settings)
    return RedirectResponse(f"/geraete/{quote(ip)}" if ip else "/geraete", status_code=303)


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


# --------------------------------------------------------------------------- FRITZ!Box Aktionen

@app.post("/fritzbox/config", dependencies=[Depends(require_password)])
async def fritzbox_config(
    request: Request,
    r1_name: str = Form("FRITZ!Box 1"),
    r1_ip: str = Form("192.168.178.1"),
    r1_user: str = Form(""),
    r1_password: str = Form(""),
    r2_name: str = Form("FRITZ!Box 2"),
    r2_ip: str = Form(""),
    r2_user: str = Form(""),
    r2_password: str = Form(""),
    same_login: str = Form(""),
):
    routers = [
        {
            "name": r1_name.strip() or "FRITZ!Box 1",
            "ip": r1_ip.strip() or "192.168.178.1",
            "user": r1_user.strip(),
            "password": r1_password.strip(),
        }
    ]
    if same_login:
        r2_user, r2_password = r1_user, r1_password
    if r2_ip.strip():
        routers.append(
            {
                "name": r2_name.strip() or "FRITZ!Box 2",
                "ip": r2_ip.strip(),
                "user": r2_user.strip(),
                "password": r2_password.strip(),
            }
        )
    await run_in_threadpool(fritz.save_routers, routers)
    await run_in_threadpool(fritz.refresh_now, request.app.state.settings)
    return RedirectResponse(f"/fritzbox?erfolg={quote('FRITZ!Box-Zugangsdaten erfolgreich gespeichert.')}", status_code=303)


@app.post("/fritzbox/retry", dependencies=[Depends(require_password)])
async def fritzbox_retry(request: Request):
    data = await run_in_threadpool(fritz.refresh_now, request.app.state.settings)
    offline = [b["name"] for b in data["boxes"] if not b["online"]]
    if offline:
        return RedirectResponse(f"/fritzbox?fehler={quote('Weiterhin nicht erreichbar: ' + ', '.join(offline))}", status_code=303)
    return RedirectResponse(f"/fritzbox?erfolg={quote('Alle FRITZ!Boxen sind erreichbar.')}", status_code=303)


@app.post("/fritzbox/guest-wlan", dependencies=[Depends(require_password)])
async def fritzbox_guest_wlan(request: Request, router_index: int = Form(0), enable: str = Form("1")):
    settings = request.app.state.settings
    ok, msg = await run_in_threadpool(fritz.toggle_guest_wlan, settings, router_index, enable == "1")
    param = f"erfolg={quote(msg)}" if ok else f"fehler={quote(msg)}"
    return RedirectResponse(f"/fritzbox?{param}", status_code=303)


@app.post("/fritzbox/wlan", dependencies=[Depends(require_password)])
async def fritzbox_wlan(request: Request, router_index: int = Form(0), service_id: int = Form(1), enable: str = Form("1")):
    settings = request.app.state.settings
    ok, msg = await run_in_threadpool(fritz.toggle_wlan, settings, router_index, service_id, enable == "1")
    param = f"erfolg={quote(msg)}" if ok else f"fehler={quote(msg)}"
    return RedirectResponse(f"/fritzbox?{param}", status_code=303)


@app.post("/fritzbox/reconnect", dependencies=[Depends(require_password)])
async def fritzbox_reconnect(request: Request, router_index: int = Form(0)):
    settings = request.app.state.settings
    ok, msg = await run_in_threadpool(fritz.reconnect_internet, settings, router_index)
    param = f"erfolg={quote(msg)}" if ok else f"fehler={quote(msg)}"
    return RedirectResponse(f"/fritzbox?{param}", status_code=303)


@app.post("/fritzbox/reboot", dependencies=[Depends(require_password)])
async def fritzbox_reboot(request: Request, router_index: int = Form(0)):
    settings = request.app.state.settings
    ok, msg = await run_in_threadpool(fritz.reboot_fritzbox, settings, router_index)
    param = f"erfolg={quote(msg)}" if ok else f"fehler={quote(msg)}"
    return RedirectResponse(f"/fritzbox?{param}", status_code=303)
