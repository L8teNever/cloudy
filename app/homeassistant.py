"""Home Assistant für die 3D-Karte: Rollläden, Licht, LEDs, PC und Steckdose echt steuern.

Verbindung per URL + langlebigem Zugriffstoken (Home Assistant → Profil → Sicherheit). Pro Gerät auf der Karte
merkt sich cloudy eine Entität (z. B. cover.rollladen_simon). Das Token verlässt cloudy nie – die Karte spricht
nur mit /karte/ha, cloudy reicht die Befehle an die REST-API von Home Assistant weiter.

Rollläden: Auf der Karte heißt 0 % offen und 100 % zu, in Home Assistant ist position 100 offen – wird umgerechnet.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request

from . import db

URL_KEY = "ha_url"
TOKEN_KEY = "ha_token"
MAP_KEY = "karte_ha"  # JSON {karten_geraet: entity_id}

ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Geräte, die in static/karte/karte.js angelegt sind: (id, Bezeichnung, Art)
DEVICES: list[tuple[str, str, str]] = [
    ("shutter-simon-win", "Rollladen Fenster (Simons Zimmer)", "cover"),
    ("shutter-simon-door", "Rollladen Balkontür (Simons Zimmer)", "cover"),
    ("shutter-arbeit", "Rollladen (Arbeitszimmer)", "cover"),
    ("shutter-martin", "Rollladen (Martins Zimmer)", "cover"),
    ("shutter-schlaf", "Rollladen (Schlafzimmer)", "cover"),
    ("shutter-melanie-door", "Rollladen Balkontür (Melanies Zimmer)", "cover"),
    ("shutter-melanie-win", "Rollladen Fenster (Melanies Zimmer)", "cover"),
    ("light-simon", "Deckenlicht (Simons Zimmer)", "light"),
    ("led-simon", "Tisch-LED (Simons Zimmer)", "led"),
    ("pc-simon", "PC (Simons Zimmer)", "switch"),
    ("light-schlaf", "Deckenlicht (Schlafzimmer)", "light"),
    ("led-schlaf", "Decken-LED (Schlafzimmer)", "led"),
    ("plug-kueche", "Steckdose (Küche)", "switch"),
    ("led-flur", "LED-Band (Flur)", "light"),
]
DEVICE_KIND = {d: k for d, _, k in DEVICES}

# Welche Home-Assistant-Bereiche pro Art zuerst vorgeschlagen werden (auswählen lässt sich jede Entität)
DOMAINS = {
    "cover": ("cover",),
    "light": ("light", "switch", "input_boolean"),
    "led": ("light",),
    "switch": ("switch", "input_boolean", "light"),
}
KIND_TITLES = {"cover": "Rollläden", "light": "Licht", "led": "LEDs", "switch": "PC & Steckdosen"}

SWITCH_DOMAINS = ("light", "switch", "input_boolean")  # haben eigene turn_on/turn_off, sonst der allgemeine Dienst
COVER_SET_POSITION = 4  # CoverEntityFeature.SET_POSITION
COLOR_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}


class HAError(Exception):
    """Fehler mit verständlichem Text für die Oberfläche."""


# --------------------------------------------------------------------------- Einstellungen

def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url.endswith("/api"):
        url = url[:-4]
    if url and not re.match(r"^https?://[^\s/]+(/[^\s]*)?$", url):
        raise HAError("Die Adresse muss mit http:// oder https:// anfangen, z. B. http://192.168.178.20:8123.")
    return url


def config() -> tuple[str, str]:
    return db.get_setting(URL_KEY), db.get_setting(TOKEN_KEY)


def configured() -> bool:
    url, token = config()
    return bool(url and token)


def save_config(url: str, token: str) -> None:
    """URL setzen; Token nur ersetzen, wenn ein neues eingegeben wurde (leer = bisheriges behalten)."""
    db.set_setting(URL_KEY, normalize_url(url))
    token = (token or "").strip()
    if token:
        db.set_setting(TOKEN_KEY, token)


def forget() -> None:
    db.set_setting(URL_KEY, "")
    db.set_setting(TOKEN_KEY, "")


def mapping() -> dict[str, str]:
    try:
        raw = json.loads(db.get_setting(MAP_KEY) or "{}")
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: str(v) for k, v in raw.items() if k in DEVICE_KIND and isinstance(v, str) and ENTITY_RE.match(v)}


CONFIRM_KEY = "karte_bestaetigen"  # JSON [karten_geraet, ...]: vor dem Ausschalten auf der Karte nachfragen


def confirm_devices() -> list[str]:
    try:
        raw = json.loads(db.get_setting(CONFIRM_KEY) or "[]")
    except ValueError:
        return []
    return sorted(d for d in raw if d in DEVICE_KIND and DEVICE_KIND[d] != "cover") if isinstance(raw, list) else []


def save_confirm(devices) -> None:
    clean = sorted({d for d in devices if d in DEVICE_KIND and DEVICE_KIND[d] != "cover"})
    db.set_setting(CONFIRM_KEY, json.dumps(clean) if clean else "")


NIGHT_KEY = "karte_nacht"  # JSON {"from": "22:00", "to": "07:00"}: in dieser Zeit fragt die Karte vor jedem Schalten nach
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def night() -> dict | None:
    try:
        raw = json.loads(db.get_setting(NIGHT_KEY) or "null")
    except ValueError:
        return None
    if isinstance(raw, dict) and TIME_RE.match(str(raw.get("from"))) and TIME_RE.match(str(raw.get("to"))):
        return {"from": raw["from"], "to": raw["to"]}
    return None


def save_night(on: bool, start: str, end: str) -> None:
    start, end = (start or "").strip(), (end or "").strip()
    if not on:
        db.set_setting(NIGHT_KEY, "")
        return
    if not (TIME_RE.match(start) and TIME_RE.match(end)) or start == end:
        raise HAError("Nachtzeit: Bitte zwei verschiedene Uhrzeiten wie 22:00 und 07:00 angeben.")
    db.set_setting(NIGHT_KEY, json.dumps({"from": start, "to": end}))


def save_mapping(data: dict[str, str]) -> None:
    clean = {}
    for dev, ent in data.items():
        ent = (ent or "").strip().lower()
        if dev in DEVICE_KIND and ENTITY_RE.match(ent):
            clean[dev] = ent
    db.set_setting(MAP_KEY, json.dumps(clean, sort_keys=True) if clean else "")


# --------------------------------------------------------------------------- REST-API

def _call(method: str, path: str, body: dict | None = None, timeout: float = 6.0):
    url, token = config()
    if not url or not token:
        raise HAError("Home Assistant ist nicht verbunden.")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{url}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise HAError("Home Assistant lehnt den Zugriffsschlüssel ab (401).") from exc
        if exc.code == 404:
            raise HAError("Home Assistant kennt diese Entität oder diesen Dienst nicht (404).") from exc
        raise HAError(f"Home Assistant meldet Fehler {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HAError("Home Assistant ist nicht erreichbar.") from exc
    try:
        return json.loads(raw) if raw else None
    except ValueError as exc:
        raise HAError("Unerwartete Antwort von Home Assistant.") from exc


def check() -> str:
    """Verbindung testen. Rückgabe: Fehlertext oder ""."""
    try:
        _call("GET", "/api/")
    except HAError as exc:
        return str(exc)
    return ""


def states() -> list[dict]:
    data = _call("GET", "/api/states")
    return data if isinstance(data, list) else []


# Raum und Labels gibt es nicht über /api/states, nur über die Template-API. Labels können an der Entität oder am Gerät hängen.
_META_TEMPLATE = """[{%- for s in states -%}
{%- set d = device_id(s.entity_id) -%}
{%- set own = labels(s.entity_id) + (labels(d) if d else []) -%}
{{- [s.entity_id, area_name(s.entity_id), own | unique | map('label_name') | select | list] | to_json -}}
{{- ',' if not loop.last -}}
{%- endfor -%}]"""


def entity_meta() -> dict[str, tuple[str, list[str]]]:
    """{entity_id: (Raum, Labels)}. Leer, wenn Home Assistant das nicht liefert (z. B. sehr alte Version)."""
    try:
        rows = _call("POST", "/api/template", {"template": _META_TEMPLATE}, timeout=15.0)
    except HAError:
        return {}
    if not isinstance(rows, list):
        return {}
    out = {}
    for row in rows:
        if isinstance(row, list) and len(row) == 3:
            out[str(row[0])] = (str(row[1] or ""), sorted(str(x) for x in row[2] or []))
    return out


def all_entities(all_states: list[dict], meta: dict | None = None) -> list[dict]:
    """Alle Entitäten mit Name, Art, Zustand, Raum und Labels; nach Raum sortiert, ohne Raum zuletzt."""
    meta = meta or {}
    out = []
    for s in all_states:
        eid = str(s.get("entity_id") or "")
        if not ENTITY_RE.match(eid):
            continue
        area, labels = meta.get(eid, ("", []))
        out.append({
            "id": eid, "domain": eid.split(".")[0], "name": str((s.get("attributes") or {}).get("friendly_name") or eid),
            "state": str(s.get("state") or ""), "room": area, "labels": labels,
        })
    out.sort(key=lambda e: (not e["room"], e["room"].lower(), e["domain"], e["name"].lower()))
    return out


def _hex(rgb) -> str:
    try:
        r, g, b = (max(0, min(255, int(v))) for v in rgb[:3])
    except (TypeError, ValueError):
        return ""
    return f"#{r:02x}{g:02x}{b:02x}"


def device_state(kind: str, s: dict | None) -> dict:
    """Zustand einer Entität so, wie die Karte ihn braucht."""
    if not s:
        return {"available": False}
    state = str(s.get("state") or "")
    attrs = s.get("attributes") or {}
    out = {"available": state not in ("unavailable", "unknown", ""), "state": state,
           "name": str(attrs.get("friendly_name") or s.get("entity_id") or "")}
    if kind == "cover":
        pos = attrs.get("current_position")
        if isinstance(pos, (int, float)):
            out["percent"] = 100 - max(0, min(100, round(pos)))
        elif state in ("open", "closed", "on", "off"):  # „on“ = offen, falls ein Schalter den Rollladen steuert
            out["percent"] = 0 if state in ("open", "on") else 100
        out["moving"] = state in ("opening", "closing")
        if out["moving"]:
            out["direction"] = state
        out["positionable"] = bool(int(attrs.get("supported_features") or 0) & COVER_SET_POSITION)
    else:
        out["on"] = state in ("on", "open")
        if kind == "led":
            bri = attrs.get("brightness")
            if isinstance(bri, (int, float)):
                out["brightness"] = max(1, min(100, round(bri / 255 * 100)))
            if attrs.get("rgb_color"):
                out["color"] = _hex(attrs["rgb_color"])
            modes = set(attrs.get("supported_color_modes") or [])
            if modes:  # Farben/Helligkeit nur, wenn Home Assistant das für die Entität meldet
                out["color_ok"] = bool(modes & COLOR_MODES)
                out["dimmable"] = modes != {"onoff"}
    return out


def karte_state() -> dict:
    """{"configured": bool, "devices": {karten_geraet: zustand}, "error": ""} für die Karte."""
    mp = mapping()
    if not configured() or not mp:
        return {"configured": configured(), "devices": {}, "error": "", "timers": timers(), "confirm": confirm_devices(), "night": night()}
    try:
        by_id = {s.get("entity_id"): s for s in states()}
    except HAError as exc:
        return {"configured": True, "devices": {}, "error": str(exc), "timers": timers(), "confirm": confirm_devices(), "night": night()}
    devices = {}
    for dev, ent in mp.items():
        st = device_state(DEVICE_KIND[dev], by_id.get(ent))
        st["entity"] = ent
        devices[dev] = st
    return {"configured": True, "devices": devices, "error": "", "timers": timers(), "confirm": confirm_devices(), "night": night()}


def _service(domain: str, service: str, data: dict) -> None:
    _call("POST", f"/api/services/{domain}/{service}", data)


def _switch(domain: str, on: bool, data: dict) -> None:
    """Ein- oder ausschalten; alles außer Licht und Schaltern über den allgemeinen Home-Assistant-Dienst."""
    _service(domain if domain in SWITCH_DOMAINS else "homeassistant", "turn_on" if on else "turn_off", data)


def command(device: str, action: str, value=None) -> None:
    """Befehl von der Karte ausführen. Wirft HAError mit verständlichem Text."""
    kind = DEVICE_KIND.get(device)
    if not kind:
        raise HAError("Unbekanntes Gerät.")
    ent = mapping().get(device)
    if not ent:
        raise HAError("Diesem Gerät ist in cloudy keine Home-Assistant-Entität zugeordnet.")
    domain = ent.split(".")[0]
    target = {"entity_id": ent}

    if kind == "cover":
        if action == "stop":
            if domain == "cover":
                _service("cover", "stop_cover", target)
            return
        if action != "position":
            raise HAError("Unbekannter Befehl.")
        try:
            pct = max(0, min(100, int(round(float(value)))))
        except (TypeError, ValueError) as exc:
            raise HAError("Ungültige Position.") from exc
        if domain != "cover":  # z. B. ein Schalter: an = offen
            _switch(domain, pct < 50, target)
            return
        cur = next((s for s in states() if s.get("entity_id") == ent), None)
        features = int(((cur or {}).get("attributes") or {}).get("supported_features") or 0)
        if features & COVER_SET_POSITION:
            _service("cover", "set_cover_position", {**target, "position": 100 - pct})
        else:  # Rollladen kann nur auf/zu
            _service("cover", "open_cover" if pct < 50 else "close_cover", target)
        return

    if action == "off":
        _switch(domain, False, target)
        return
    if action != "on":
        raise HAError("Unbekannter Befehl.")
    data = dict(target)
    if kind == "led" and domain == "light" and isinstance(value, dict):
        try:
            bri = value.get("brightness")
            if bri is not None:
                data["brightness_pct"] = max(1, min(100, int(round(float(bri)))))
        except (TypeError, ValueError):
            pass
        color = value.get("color")
        if isinstance(color, str) and HEX_RE.match(color):
            cur = next((s for s in states() if s.get("entity_id") == ent), None)
            modes = set(((cur or {}).get("attributes") or {}).get("supported_color_modes") or [])
            if modes & COLOR_MODES or not modes:
                data["rgb_color"] = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
            else:
                _switch(domain, True, data)
                raise HAError("Diese LED kann laut Home Assistant keine Farben, nur an/aus und Helligkeit.")
    elif domain != "light" and isinstance(value, dict) and value.get("color"):
        _switch(domain, True, data)
        raise HAError(f"„{ent}“ ist in Home Assistant ein Schalter, kein Licht – Farben gehen damit nicht. "
                      "Ordne unter Einstellungen → 3D-Karte die light.-Entität der LED zu.")
    _switch(domain, True, data)


# --------------------------------------------------------------------------- Zeitplan (einmalig an → nach X Minuten aus)

TIMER_KEY = "karte_timer"  # JSON {geraet: {"start": epoch, "end": epoch, "phase": "wait"|"on"}}
_timer_lock = threading.Lock()


def _load_timers() -> dict:
    try:
        raw = json.loads(db.get_setting(TIMER_KEY) or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in raw.items() if k in DEVICE_KIND and isinstance(v, dict)} if isinstance(raw, dict) else {}


def _save_timers(data: dict) -> None:
    db.set_setting(TIMER_KEY, json.dumps(data, sort_keys=True) if data else "")


def timers() -> dict:
    return _load_timers()


def set_timer(device: str, start: float, minutes: float, now: float | None = None) -> dict:
    """Einmal-Zeitplan: um start einschalten, nach minutes wieder aus. Ersetzt einen bestehenden."""
    now = time.time() if now is None else now
    if DEVICE_KIND.get(device) not in ("switch", "light", "led"):
        raise HAError("Für dieses Gerät gibt es keinen Zeitplan.")
    if device not in mapping():
        raise HAError("Diesem Gerät ist in cloudy keine Home-Assistant-Entität zugeordnet.")
    try:
        start, minutes = float(start), float(minutes)
    except (TypeError, ValueError) as exc:
        raise HAError("Ungültige Zeit.") from exc
    if not 1 <= minutes <= 24 * 60:
        raise HAError("Die Dauer muss zwischen 1 Minute und 24 Stunden liegen.")
    if not now - 120 <= start <= now + 2 * 86400:
        raise HAError("Die Startzeit muss in den nächsten 2 Tagen liegen.")
    with _timer_lock:
        data = _load_timers()
        data[device] = {"start": int(start), "end": int(start + minutes * 60), "phase": "wait"}
        _save_timers(data)
    timer_tick(now)
    return timers()


def cancel_timer(device: str) -> dict:
    """Zeitplan löschen; läuft er gerade (Gerät an), wird das Gerät ausgeschaltet."""
    with _timer_lock:
        data = _load_timers()
        entry = data.pop(device, None)
        _save_timers(data)
    if entry and entry.get("phase") == "on":
        try:
            command(device, "off")
        except HAError:
            pass
    return timers()


def timer_tick(now: float | None = None) -> None:
    """Fällige Zeitpläne ausführen: einschalten, nach Ablauf ausschalten und den Plan entfernen."""
    now = time.time() if now is None else now
    with _timer_lock:
        data = _load_timers()
        changed = False
        for dev, t in list(data.items()):
            start, end = float(t.get("start") or 0), float(t.get("end") or 0)
            try:
                if now >= end:
                    if t.get("phase") == "on":
                        command(dev, "off")
                    del data[dev]
                    changed = True
                elif now >= start and t.get("phase") != "on":
                    command(dev, "on")
                    t["phase"] = "on"
                    changed = True
            except HAError as exc:  # Home Assistant gerade weg -> beim nächsten Durchlauf noch mal
                print(f"cloudy: Zeitplan {dev}: {exc}", flush=True)
        if changed:
            _save_timers(data)


def timer_loop(stop: threading.Event, interval: float = 15.0) -> None:
    while not stop.wait(interval):
        if _load_timers():
            try:
                timer_tick()
            except Exception as exc:  # noqa: BLE001 – Schleife darf nie sterben
                print(f"cloudy: Zeitplan-Fehler: {exc}", flush=True)
