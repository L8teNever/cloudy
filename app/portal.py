"""Geräte-Portal: eigene kleine Webseite (eigener Port), von unterwegs über den Cloudflare Tunnel erreichbar.

Cloudflare Access (mit WARP-Anmeldung) schützt die Seite. Das Portal erkennt, welches Gerät aufruft, und lässt
nur für genau dieses Gerät einstellen, ob es über VPN/WARP oder normal über die Fritzbox ins Internet geht.
Es ist eine eigene FastAPI-App – vom Admin-Dashboard kann man hier nichts erreichen.
"""

import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import access, db, views, warp
from .settings import Settings

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

BROWSER_COOKIE = "cloudy_portal"
CSRF_COOKIE = "cloudy_portal_csrf"
HEX_RE = re.compile(r"^[0-9a-f]{32}$")
MAC_RE = warp.MAC_RE

portal = FastAPI(title="cloudy-portal", docs_url=None, redoc_url=None, openapi_url=None)


def _settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        settings = request.app.state.settings = Settings.from_env()
    return settings


def _deny(title: str, text: str) -> HTMLResponse:
    body = (
        '<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f"<title>cloudy – {title}</title><body style=\"font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem\">"
        f"<h1>🔒 {title}</h1><p>{text}</p></body></html>"
    )
    return HTMLResponse(body, status_code=403)


@portal.middleware("http")
async def guard(request: Request, call_next):
    """Nur über den Tunnel, nur mit gültiger Cloudflare-Anmeldung für die Portal-App (eigener AUD)."""
    if request.url.path == "/healthz":
        return PlainTextResponse("ok")
    settings = _settings(request)
    host = request.client.host if request.client else ""
    if host != settings.tunnel_ip and os.environ.get("PORTAL_ALLOW_LAN", "0") != "1":
        return _deny("Nur über Cloudflare", "Das Geräte-Portal ist nur über die Fernzugriffs-Adresse erreichbar.")
    team, aud = await run_in_threadpool(access.portal_config)
    token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization") or ""
    claims = await run_in_threadpool(access.verify_claims, token, team, aud) if team and aud and token else None
    if claims is None:
        return _deny(
            "Nur mit Cloudflare-Anmeldung",
            "Das Geräte-Portal braucht eine Anmeldung über Cloudflare Zero Trust. "
            "Ist sie eingerichtet (Einstellungen → Geräte-Portal: AUD der Portal-App), Seite neu laden.",
        )
    request.state.token = token
    request.state.team = team
    request.state.claims = claims
    return await call_next(request)


# --------------------------------------------------------------------------- Gerät erkennen

def _norm_mac(text: str | None) -> str:
    mac = (text or "").strip().lower().replace("-", ":")
    return mac if MAC_RE.match(mac) else ""


def _known_devices() -> dict[str, dict]:
    return {d["mac"]: d for d in db.devices() if d.get("mac")}


def _name_suggestion(cf_name: str, known: dict[str, dict]) -> dict | None:
    """Ein Gerät im Heimnetz mit demselben Namen – nur, wenn es genau eins ist (Namen sind nicht eindeutig)."""
    wanted = (cf_name or "").strip().lower()
    if not wanted:
        return None
    hits = [
        d for d in known.values()
        if wanted in {(d.get("hostname") or "").lower(), (d.get("lease_name") or "").lower(), (d.get("fritz_name") or "").lower()}
    ]
    return hits[0] if len(hits) == 1 else None


def _resolve(token: str, team: str, claims: dict, browser_id: str) -> dict:
    """Wer ruft auf und welches Gerät im Heimnetz ist das? (blockierend – im Threadpool aufrufen)"""
    ident = access.identity(token, team, claims)
    device_id = ident["device_id"]
    key = device_id or f"browser:{browser_id}"
    out = {"key": key, "email": ident["email"], "cf_name": ident["name"], "has_device_id": bool(device_id),
           "mac": db.portal_mac_for(key), "suggestion": None}
    known = _known_devices()
    if out["mac"] and out["mac"] not in known:
        # Zugeordnetes Gerät kennt cloudy (noch) nicht – trotzdem erlaubt, nur ohne Anzeigedaten.
        pass
    if not out["mac"] and device_id:
        info = access.cloudflare_device(device_id) or {}
        out["cf_name"] = out["cf_name"] or str(info.get("name") or "")
        mac = _norm_mac(info.get("mac_address"))
        if mac and mac in known:
            db.portal_link(key, mac, out["email"], out["cf_name"])
            out["mac"] = mac
    if not out["mac"]:
        out["suggestion"] = _name_suggestion(out["cf_name"], known)
    if out["mac"]:
        db.portal_touch(key)
    return out


def _set_cookies(request: Request, response, browser_id: str, csrf: str) -> None:
    secure = request.headers.get("x-forwarded-proto", "").lower() == "https"
    opts = {"httponly": True, "secure": secure, "samesite": "lax", "max_age": 365 * 86400, "path": "/"}
    response.set_cookie(BROWSER_COOKIE, browser_id, **opts)
    response.set_cookie(CSRF_COOKIE, csrf, **opts)


def _cookie_ids(request: Request) -> tuple[str, str]:
    browser_id = request.cookies.get(BROWSER_COOKIE, "")
    csrf = request.cookies.get(CSRF_COOKIE, "")
    return (
        browser_id if HEX_RE.match(browser_id) else secrets.token_hex(16),
        csrf if HEX_RE.match(csrf) else secrets.token_hex(16),
    )


def _csrf_ok(request: Request, form_token: str) -> bool:
    """Schutz gegen fremde Seiten, die ein Formular an das Portal schicken."""
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not (HEX_RE.match(cookie) and secrets.compare_digest(cookie, form_token or "")):
        return False
    origin = request.headers.get("origin")
    if origin and origin != "null":
        host = request.headers.get("host", "")
        if urlsplit(origin).netloc.lower() != host.lower():
            return False
    return True


# --------------------------------------------------------------------------- Seiten

@portal.get("/", response_class=HTMLResponse)
async def home(request: Request, ok: int | None = None, fehler: str | None = None):
    settings = _settings(request)
    browser_id, csrf = _cookie_ids(request)
    who = await run_in_threadpool(_resolve, request.state.token, request.state.team, request.state.claims, browser_id)
    known = await run_in_threadpool(_known_devices)
    device = known.get(who["mac"]) if who["mac"] else None
    code = None
    if not who["mac"]:
        code = await run_in_threadpool(db.pairing_create, who["key"], who["email"], who["cf_name"])
    presence = getattr(request.app.state, "presence", None)
    online = presence.online if presence else frozenset()
    profiles = await run_in_threadpool(views.warp_profiles, settings)
    warp_default = await run_in_threadpool(db.get_setting, warp.DEFAULT_KEY)
    current = (await run_in_threadpool(db.device_warp)).get(who["mac"] or "", "")
    response = templates.TemplateResponse(
        request,
        "portal/seite.html",
        {
            "title": "Mein Gerät",
            "settings": settings,
            "who": who,
            "linked": bool(who["mac"]),
            "device": device,
            "is_online": bool(device and (device.get("ip") in online or device.get("fritz_active"))),
            "code": code,
            "warp": profiles,
            "warp_names": {w["slot"]: w["label"] for w in profiles},
            "warp_default": warp_default,
            "current": current,
            "csrf": csrf,
            "ok": bool(ok),
            "fehler": fehler,
        },
    )
    _set_cookies(request, response, browser_id, csrf)
    return response


async def _mac_of_caller(request: Request) -> tuple[dict, str]:
    browser_id, _ = _cookie_ids(request)
    who = await run_in_threadpool(_resolve, request.state.token, request.state.team, request.state.claims, browser_id)
    return who, who["mac"] or ""


@portal.post("/vpn")
async def set_vpn(request: Request, slot: str = Form(""), csrf: str = Form("")):
    """VPN/WARP nur für das Gerät umstellen, das zu dieser Anmeldung gehört."""
    if not _csrf_ok(request, csrf):
        return RedirectResponse("/?fehler=Sitzung+abgelaufen%2C+bitte+nochmal+versuchen.", status_code=303)
    _, mac = await _mac_of_caller(request)
    if not mac:
        return RedirectResponse("/?fehler=Dieses+Ger%C3%A4t+ist+noch+nicht+zugeordnet.", status_code=303)
    settings = _settings(request)
    if slot and slot != warp.DIRECT and slot not in {name for name, _ in settings.warp_slots}:
        return PlainTextResponse("Unbekanntes Profil", status_code=400)
    await run_in_threadpool(db.set_device_warp, mac, slot or None)
    return RedirectResponse("/?ok=1", status_code=303)


@portal.post("/bestaetigen")
async def confirm_suggestion(request: Request, csrf: str = Form("")):
    """„Ja, das bin ich“ für den Namensvorschlag. Der Vorschlag wird hier neu berechnet, nicht aus dem Formular gelesen."""
    if not _csrf_ok(request, csrf):
        return RedirectResponse("/?fehler=Sitzung+abgelaufen%2C+bitte+nochmal+versuchen.", status_code=303)
    who, mac = await _mac_of_caller(request)
    suggestion = who.get("suggestion")
    if not mac and suggestion:
        await run_in_threadpool(db.portal_link, who["key"], suggestion["mac"], who["email"], who["cf_name"])
    return RedirectResponse("/", status_code=303)
