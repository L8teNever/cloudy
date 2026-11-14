"""Live-Ansicht des digitalen Browsers (Chromium, CDP): zugucken, klicken, tippen – z. B. für Schutzseiten.

Die Seite (templates/anime_browser.html + static/anime_browser.js) spricht über einen WebSocket mit dem Server. Der
reicht nur erlaubte CDP-Befehle an einen eigenen Tab weiter und die Bilder (Screencast) zurück. Der Tab nutzt das
Profil des Browsers: Cookies von gelösten Schutzseiten gelten danach auch für die automatischen Abrufe. Wie alles
andere geht auch dieser Browser nur über den VPN-Proxy (browser/start.sh)."""
from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request

from fastapi.concurrency import run_in_threadpool

from . import aniworld

ERLAUBT = {
    "Page.enable", "Page.navigate", "Page.reload", "Page.startScreencast", "Page.stopScreencast", "Page.screencastFrameAck",
    "Page.getNavigationHistory", "Page.navigateToHistoryEntry", "Page.bringToFront",
    "Input.dispatchMouseEvent", "Input.dispatchKeyEvent", "Input.insertText",
    "Emulation.setDeviceMetricsOverride", "Runtime.evaluate",
}
# Runtime.evaluate nur zum Auslesen von Adresse und Titel (kein freies JavaScript).
AUSDRUCK_OK = {"location.href", "document.title"}
_tab = {"id": ""}


def _cdp(pfad: str, methode: str = "GET"):
    base = aniworld._cdp_base()
    with aniworld._opener().open(urllib.request.Request(base + pfad, method=methode), timeout=10) as r:
        return json.loads(r.read() or b"null")


def _tab_holen() -> str:
    """Eigener, dauerhafter Tab für die Live-Ansicht (neu, wenn er weg ist)."""
    if _tab["id"]:
        try:
            if any(t.get("id") == _tab["id"] for t in _cdp("/json/list") or []):
                return _tab["id"]
        except Exception:  # noqa: BLE001
            pass
    t = _cdp("/json/new?" + urllib.parse.quote("about:blank", safe=""), "PUT")
    _tab["id"] = t["id"]
    return _tab["id"]


def _erlaubt(m: dict) -> bool:
    if m.get("method") not in ERLAUBT or not isinstance(m.get("params", {}), dict):
        return False
    if m["method"] == "Runtime.evaluate":
        return str((m.get("params") or {}).get("expression", "")).strip() in AUSDRUCK_OK
    if m["method"] == "Page.navigate":
        return urllib.parse.urlparse(str((m.get("params") or {}).get("url", ""))).scheme in ("http", "https")
    return True


async def relay(ws) -> None:
    import websockets

    try:
        tab = await run_in_threadpool(_tab_holen)
        url = f"ws://{urllib.parse.urlparse(aniworld._cdp_base()).netloc}/devtools/page/{tab}"
        cdp = await websockets.connect(url, max_size=64 * 1024 * 1024, open_timeout=10)
    except Exception as exc:  # noqa: BLE001
        await ws.send_text(json.dumps({"fehler": f"Browser nicht erreichbar: {exc}"[:200]}))
        await ws.close()
        return

    async def hoch() -> None:
        while True:
            try:
                m = json.loads(await ws.receive_text())
            except ValueError:
                continue
            if not isinstance(m, dict) or not _erlaubt(m):
                await ws.send_text(json.dumps({"id": m.get("id") if isinstance(m, dict) else None, "error": {"message": "nicht erlaubt"}}))
                continue
            await cdp.send(json.dumps({"id": m.get("id"), "method": m["method"], "params": m.get("params") or {}}))

    async def runter() -> None:
        async for text in cdp:
            await ws.send_text(text if isinstance(text, str) else text.decode())

    tasks = [asyncio.create_task(hoch()), asyncio.create_task(runter())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        try:
            await cdp.send(json.dumps({"id": 1, "method": "Page.stopScreencast"}))
        except Exception:  # noqa: BLE001
            pass
        await cdp.close()
