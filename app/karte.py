"""3D-Karte vom Haus: Fernseher und PC-Monitore zeigen an/aus je nachdem, ob das zugeordnete Gerät online ist.

Die Karte liegt in static/karte/ (index.html, karte.css, karte.js, cloudy.js, three.js lokal unter vendor/). cloudy merkt sich pro Bildschirm auf der Karte
ein Gerät aus der Geräteliste (per MAC, sonst per IP) und liefert unter /karte/status, welche davon gerade im
Netz sind. Bildschirme ohne Zuordnung behalten ihren Zustand aus der Karte.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from . import db

KARTE_DIR = Path(__file__).resolve().parent / "static" / "karte"
ROOM_RE = re.compile(r"^[a-z0-9-]{1,40}$")  # Raum-Adresse wie „simonszimmer“ (welcher Raum das ist, weiß cloudy.js)


def valid_path(path: str) -> bool:
    """„raum“ oder „raum/menü“ (z. B. simonszimmer/rollladen-fenster) – höchstens zwei Teile."""
    parts = path.strip("/").split("/")
    return 1 <= len(parts) <= 2 and all(ROOM_RE.match(p) for p in parts)

ASSIGN_KEY = "karte_geraete"  # JSON {bildschirm_id: "mac" oder "ip:1.2.3.4"}

# Bildschirme, die in static/karte/karte.js per registerScreen(...) angelegt sind (gleiche IDs).
SCREENS: list[tuple[str, str]] = [
    ("tv-simon", "Fernseher (Simons Zimmer)"),
    ("mon-simon-1", "Monitor 1 (Simons Zimmer)"),
    ("mon-simon-2", "Monitor 2 (Simons Zimmer)"),
    ("mon-arbeit", "PC-Monitor (Arbeitszimmer)"),
    ("tv-martin", "Fernseher (Martins Zimmer)"),
    ("mon-martin", "PC-Monitor (Martins Zimmer)"),
    ("tv-wohnzimmer", "Fernseher (Wohnzimmer)"),
    ("tv-melanie", "Fernseher (Melanies Zimmer)"),
]
SCREEN_IDS = {sid for sid, _ in SCREENS}


def device_key(d: dict) -> str:
    """Stabiler Schlüssel eines Geräts: MAC, ohne MAC die IP."""
    mac = (d.get("mac") or "").strip().lower()
    return mac or (f"ip:{d['ip']}" if d.get("ip") else "")


def assignments() -> dict[str, str]:
    try:
        raw = json.loads(db.get_setting(ASSIGN_KEY) or "{}")
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: str(v) for k, v in raw.items() if k in SCREEN_IDS and v}


def save_assignments(data: dict[str, str]) -> None:
    clean = {k: v.strip().lower() for k, v in data.items() if k in SCREEN_IDS and v and v.strip()}
    db.set_setting(ASSIGN_KEY, json.dumps(clean, sort_keys=True) if clean else "")


def is_online(d: dict, online_ips: frozenset[str]) -> bool:
    """Online laut ARP-Abfrage des Pi; Geräte, die nur die FRITZ!Box kennt, laut FRITZ!Box."""
    if d.get("ip") and d["ip"] in online_ips:
        return True
    return bool(d.get("fritz_only") and d.get("fritz_active"))


def status(online_ips: frozenset[str], devices: list[dict] | None = None) -> dict[str, bool]:
    """{bildschirm_id: an?} für alle zugeordneten Bildschirme."""
    assign = assignments()
    if not assign:
        return {}
    by_key = {device_key(d): d for d in (devices if devices is not None else db.devices())}
    out = {}
    for sid, key in assign.items():
        d = by_key.get(key)
        if d is None and key.startswith("ip:"):
            d = {"ip": key[3:]}
        out[sid] = bool(d) and is_online(d, online_ips)
    return out


@lru_cache(maxsize=1)
def _version() -> str:
    """Kurzer Hash über die Karten-Dateien, damit der Browser nach einem Update neu lädt."""
    h = hashlib.sha1()
    for name in ("index.html", "karte.css", "karte.js", "cloudy.css", "cloudy.js", "m3.css"):
        h.update((KARTE_DIR / name).read_bytes())
    return h.hexdigest()[:10]


@lru_cache(maxsize=4)
def page_html(base: str) -> str:
    """index.html mit Basis-Pfad ("" auf der eigenen Domain, sonst "/karte") und Datei-Version."""
    html = (KARTE_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace("__BASE__", base).replace("__V__", _version())


# --------------------------------------------------------------------------- Internet-Route pro Bildschirm

def _profiles(settings) -> list[dict]:
    """WARP- und VPN-Profile für das Menü auf der Karte (DNS-Profile leiten keinen Verkehr um)."""
    from . import views  # erst hier: views importiert viel

    return [
        {"slot": p["slot"], "kind": p["kind"], "label": p["label"], "ready": bool(p["has_token"])}
        for p in views.warp_profiles(settings, dns=False)
    ]


def internet(settings, devices: list[dict] | None = None) -> dict:
    """Was die Karte fürs Internet-Menü braucht: Profile, Standard und pro zugeordnetem Bildschirm die Route."""
    from . import policy, warp

    profiles = _profiles(settings)
    names = {p["slot"]: p["label"] for p in profiles}
    default = db.get_setting(warp.DEFAULT_KEY)
    if default and default not in names:  # z. B. DNS-Profil als Standard
        names[default] = (db.warp_labels().get(default) or default)
    by_key = {device_key(d): d for d in (devices if devices is not None else db.devices())}
    device_map = db.device_warp()
    by_mac = db.groups_by_mac()
    screens = {}
    for sid, key in assignments().items():
        d = by_key.get(key) or {}
        mac = key if warp.MAC_RE.match(key) else ""
        name = (d.get("hostname") or "").strip() or d.get("ip") or key.removeprefix("ip:")
        if not mac:
            screens[sid] = {"device": name, "mac": "", "choice": "", "label": "", "locked": "", "kind": ""}
            continue
        eff = policy.resolve(mac, device_map, by_mac)
        slot = eff.slot or default
        screens[sid] = {
            "device": name,
            "mac": mac,
            "choice": device_map.get(mac, ""),  # "" = Standard, "direct" = normal
            "label": policy.label_for(eff, names, default),
            "kind": "" if not slot or slot == warp.DIRECT else warp.kind(slot),
            "locked": eff.group_name if eff.locked else "",
            "group": eff.group_name if eff.source == "group" else "",
        }
    return {
        "profiles": profiles,
        "default": default,
        "default_label": policy.label_for(policy.Effective("", "default"), names, default),
        "screens": screens,
    }


def set_internet(settings, screen_id: str, slot: str) -> str:
    """Internet-Route für das Gerät hinter einem Bildschirm setzen. Rückgabe: Fehlertext oder ""."""
    from . import policy, warp

    key = assignments().get(screen_id, "")
    if not key:
        return "Diesem Bildschirm ist kein Gerät zugeordnet."
    if not warp.MAC_RE.match(key):
        return "Das Gerät hat keine MAC-Adresse – die Route lässt sich nur per MAC umstellen."
    slot = (slot or "").strip().lower()
    if slot and slot != warp.DIRECT:
        if slot not in {name for name, _ in settings.warp_slots}:
            return "Unbekanntes Profil."
    locked = policy.locked_for(key)
    if locked:
        return f"Festgelegt durch Gruppe „{locked.group_name}“."
    if slot and slot != warp.DIRECT:
        warp.allow_device(slot, key)  # wie im Dashboard: ausdrückliche Zuweisung gibt das Profil frei
    db.set_device_warp(key, slot or None)
    return ""
