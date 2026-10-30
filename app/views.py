"""Seiten (Übersicht, Geräte, Statistik, Schutz, VPN, FRITZ!Box, Einstellungen) und ihre Live-Stücke unter /teil/<name>.

Jede Seite rendert beim ersten Aufruf alles. Danach holt das Skript in base.html nur die Stücke mit data-live
regelmäßig neu – die Seite wird nie komplett neu geladen.
"""

import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import access, blocklist, charts, db, fritz, stats, warp
from .settings import Settings

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
basic = HTTPBasic(auto_error=False)
router = APIRouter()


def fmt_bytes(n: int) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return f"{n:.1f} TB".replace(".", ",")


def fmt_count(n: int) -> str:
    return f"{int(n or 0):,}".replace(",", ".")


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


def fmt_time(ts: int) -> str:
    return time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(ts))


templates.env.filters["bytes"] = fmt_bytes
templates.env.filters["count"] = fmt_count
templates.env.filters["ago"] = fmt_ago
templates.env.globals.update(fmt_time=fmt_time, charts=charts, fmt_bytes=fmt_bytes, fmt_count=fmt_count)


def require_password(credentials: HTTPBasicCredentials | None = Depends(basic)) -> None:
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        return
    ok = credentials is not None and secrets.compare_digest(credentials.password.encode(), password.encode())
    if not ok:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


def back(request: Request, default: str = "/", **params: str) -> RedirectResponse:
    """Nach einem Formular zurück zur Seite, von der es kam (ohne JavaScript)."""
    url = request.headers.get("referer") or default
    if params:
        url = url.split("?")[0] + "?" + urlencode(params)
    return RedirectResponse(url, status_code=303)


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


def device_name(row: dict) -> str:
    return row.get("hostname") or row.get("ip") or "unbekannt"


# --------------------------------------------------------------------------- Kontexte der Live-Stücke

def ctx_status(request: Request) -> dict:
    s: Settings = request.app.state.settings
    warp_default = db.get_setting(warp.DEFAULT_KEY)
    vpn_in_use = bool(warp_default) or any(v != warp.DIRECT for v in db.device_warp().values())
    return {
        "settings": s,
        "blocklist_enabled": blocklist.is_enabled(),
        "blocklist_paused_until": blocklist.paused_until(),
        # Mit IPV6_ENABLED stört eine Fritzbox mit Router Advertisement immer (zwei IPv6-Router im Heimnetz).
        "ipv6_bypass": s.gateway_enabled and (vpn_in_use or s.ipv6_enabled) and warp.ipv6_bypass(s.lan_interface),
        "ipv6_tunnels": (
            [slot for slot, _ in s.warp_slots if warp.read_ipv6_ok(s, slot)] if s.gateway_enabled and s.ipv6_enabled else []
        ),
    }


def ctx_kpis(request: Request) -> dict:
    r = stats.get_range("heute")
    return {
        "online": online_ips(request),
        "totals": stats.totals(r),
        "series": stats.timeseries(r),
        "range": r,
    }


def ctx_jetzt(request: Request) -> dict:
    online = online_ips(request)
    devices = db.devices()
    active = [d for d in devices if d["ip"] in online and (d.get("current_site") or {}).get("domain")]
    return {"active_devices": active, "online": online}


def ctx_aufrufe(request: Request, ip: str | None = None) -> dict:
    return {"recent": db.recent(40 if not ip else 100, ip), "ip": ip}


def _warp_ctx(request: Request) -> dict:
    s: Settings = request.app.state.settings
    profiles = warp_profiles(s)
    return {
        "warp": profiles,
        "warp_names": {w["slot"]: w["label"] for w in profiles},
        "warp_by_mac": db.device_warp(),
        "warp_default": db.get_setting(warp.DEFAULT_KEY),
    }


def ctx_geraete(request: Request) -> dict:
    s: Settings = request.app.state.settings
    devices = db.devices()
    try:
        fritz_data = fritz.get_all_fritz_data(s)
        fritz_conns = fritz_data.get("device_connections", {})
        for d in devices:
            mac = (d.get("mac") or "").strip().lower()
            if mac in fritz_conns:
                d["connection"] = fritz_conns[mac]
    except Exception:
        pass
    return {
        "settings": s,
        "devices": devices,
        "online": online_ips(request),
        "isolated": db.isolated_macs(),
        **_warp_ctx(request),
    }


def ctx_geraet_kopf(request: Request, ip: str) -> dict:
    s: Settings = request.app.state.settings
    device = db.device(ip) or {"ip": ip}
    mac = (device.get("mac") or "").strip().lower()
    if mac:
        try:
            device["connection"] = fritz.get_device_connection(s, mac)
        except Exception:
            pass
    current = db.current_sites().get(ip)
    return {
        "device": device,
        "current": current,
        "online": online_ips(request),
        **_warp_ctx(request),
    }


def ctx_geraet_statistik(request: Request, ip: str, zeitraum: str | None = None) -> dict:
    r = stats.get_range(zeitraum)
    return {
        "ip": ip,
        "range": r,
        "totals": stats.totals(r, ip),
        "series": stats.timeseries(r, ip),
        "targets": stats.top_targets(r, ip, 12),
        "domains": stats.top_domains(r, ip, 12),
    }


def ctx_statistik(request: Request, zeitraum: str | None = None) -> dict:
    r = stats.get_range(zeitraum)
    return {
        "range": r,
        "totals": stats.totals(r),
        "series": stats.timeseries(r),
        "usage": stats.device_usage(r, 15),
        "device_hits": stats.device_hits(r, 15),
        "targets": stats.top_targets(r, None, 15),
        "domains": stats.top_domains(r, None, 15),
    }


def ctx_schutz(request: Request) -> dict:
    return {
        "blocklist_enabled": blocklist.is_enabled(),
        "blocklist_paused_until": blocklist.paused_until(),
        "whitelist": db.whitelist(),
        "list_info": blocklist.list_info(),
        "blocked": blocklist.recent_blocked(),
    }


def ctx_weiterleitungen(request: Request) -> dict:
    return {"redirects": db.redirects()}


def ctx_vpn(request: Request) -> dict:
    return {"settings": request.app.state.settings, **_warp_ctx(request)}


def ctx_fritzbox(request: Request, fehler: str | None = None, erfolg: str | None = None) -> dict:
    s: Settings = request.app.state.settings
    return {
        "fritz": fritz.get_all_fritz_data(s),
        "routers": fritz.get_routers(s),
        "settings": s,
        "fehler": fehler,
        "erfolg": erfolg,
    }


PARTS = {
    "status": ctx_status,
    "kpis": ctx_kpis,
    "jetzt": ctx_jetzt,
    "aufrufe": ctx_aufrufe,
    "geraete": ctx_geraete,
    "geraet_kopf": ctx_geraet_kopf,
    "geraet_statistik": ctx_geraet_statistik,
    "statistik": ctx_statistik,
    "schutz": ctx_schutz,
    "weiterleitungen": ctx_weiterleitungen,
    "vpn": ctx_vpn,
    "fritzbox": ctx_fritzbox,
}
PARAMS = {"ip", "zeitraum", "fehler", "erfolg"}


async def _build(request: Request, names: list[str], **params) -> dict:
    ctx: dict = {}
    for name in names:
        fn = PARTS[name]
        wanted = {k: v for k, v in params.items() if k in fn.__code__.co_varnames}
        ctx.update(await run_in_threadpool(fn, request, **wanted))
    return ctx


async def page(request: Request, template: str, title: str, active: str, parts: list[str], **params) -> HTMLResponse:
    ctx = await _build(request, ["status", *parts], **params)
    ctx.update(title=title, active=active, params=params, settings=request.app.state.settings,
               live=lambda name, **q: f"/teil/{name}" + (f"?{urlencode({k: v for k, v in q.items() if v})}" if q else ""))
    return templates.TemplateResponse(request, template, ctx)


# --------------------------------------------------------------------------- Seiten

@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_password)])
async def uebersicht(request: Request, ip: str | None = None):
    if ip:  # alte Links /?ip=... weiterhin unterstützen
        return RedirectResponse(f"/geraete/{quote(ip)}", status_code=307)
    return await page(request, "seiten/uebersicht.html", "Übersicht", "/", ["kpis", "jetzt", "geraete", "vpn", "aufrufe", "weiterleitungen"])


@router.get("/geraete", response_class=HTMLResponse, dependencies=[Depends(require_password)])
async def geraete(request: Request):
    return await page(request, "seiten/geraete.html", "Geräte", "/geraete", ["geraete"])


@router.get("/geraete/{ip}", response_class=HTMLResponse, dependencies=[Depends(require_password)])
async def geraet(request: Request, ip: str, zeitraum: str | None = None):
    ctx_parts = ["geraet_kopf", "geraet_statistik", "aufrufe"]
    resp = await page(request, "seiten/geraet.html", "Gerät", "/geraete", ctx_parts, ip=ip, zeitraum=zeitraum)
    return resp


@router.get("/statistik", response_class=HTMLResponse, dependencies=[Depends(require_password)])
async def statistik(request: Request, zeitraum: str | None = None):
    return await page(request, "seiten/statistik.html", "Statistik", "/statistik", ["statistik"], zeitraum=zeitraum)


@router.get("/schutz", response_class=HTMLResponse, dependencies=[Depends(require_password)])
async def schutz(request: Request):
    return await page(request, "seiten/schutz.html", "Schutz", "/schutz", ["schutz", "weiterleitungen"])


@router.get("/vpn", response_class=HTMLResponse, dependencies=[Depends(require_password)])
async def vpn(request: Request):
    return await page(request, "seiten/vpn.html", "VPN", "/vpn", ["vpn"])


@router.get("/fritzbox", response_class=HTMLResponse, dependencies=[Depends(require_password)])
async def fritzbox_page(request: Request, fehler: str | None = None, erfolg: str | None = None):
    return await page(request, "seiten/fritzbox.html", "FRITZ!Box", "/fritzbox", ["fritzbox"], fehler=fehler, erfolg=erfolg)


def tunnel_ctx(request: Request) -> dict:
    """Fernzugriff (Cloudflare Tunnel) für die Einstellungsseite."""
    s: Settings = request.app.state.settings
    team, aud = access.config()
    gateway = s.tunnel_ip.rsplit(".", 1)[0] + ".1"  # Pi aus Sicht des Tunnel-Containers
    return {
        "has_token": warp.has_token(s, access.SLOT),
        "status": warp.read_status(s, access.SLOT),
        "team": team,
        "team_default": team or access.DEFAULT_TEAM,
        "aud": aud,
        "target": f"http://{gateway}:{os.environ.get('DASHBOARD_PORT', '8080')}",
        "secured": bool(team and aud),
        "email": getattr(request.state, "access_email", None),
    }


@router.get("/einstellungen", response_class=HTMLResponse, dependencies=[Depends(require_password)])
async def einstellungen(request: Request):
    tunnel = await run_in_threadpool(tunnel_ctx, request)
    return await page(request, "seiten/einstellungen.html", "Einstellungen", "/einstellungen", [], tunnel=tunnel)


@router.get("/teil/{name}", response_class=HTMLResponse, dependencies=[Depends(require_password)])
async def teil(request: Request, name: str):
    if name not in PARTS:
        raise HTTPException(status_code=404)
    params = {k: v for k, v in request.query_params.items() if k in PARAMS}
    ctx = await _build(request, [name], **params)
    ctx.update(settings=request.app.state.settings, params=params)
    return templates.TemplateResponse(request, f"teile/{name}.html", ctx)
