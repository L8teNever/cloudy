"""Übersichtsgruppen: Geräte nur für die Anzeige ordnen (z. B. „Fernseher“, „Simons Zimmer“).

Anders als die Gruppen unter /gruppen ändern sie nichts am Netz – keine Internet-Route, keine Sperren.
Jedes Gerät steckt in höchstens einer Übersichtsgruppe, die Geräteliste zeigt sie als Abschnitte.
Gespeichert als JSON in den Einstellungen: [{"id", "name", "color", "members": [geräte_schlüssel]}].
"""

from __future__ import annotations

import json
import secrets

from . import db

KEY = "uebersicht_gruppen"
COLORS = db.GROUP_COLORS


def groups() -> list[dict]:
    try:
        raw = json.loads(db.get_setting(KEY) or "[]")
    except ValueError:
        return []
    out = []
    for g in raw if isinstance(raw, list) else []:
        if isinstance(g, dict) and g.get("id") and g.get("name"):
            out.append({
                "id": str(g["id"]),
                "name": str(g["name"]),
                "color": g.get("color") if g.get("color") in COLORS else COLORS[0],
                "members": [str(m) for m in g.get("members") or []],
            })
    return out


def _save(gs: list[dict]) -> None:
    db.set_setting(KEY, json.dumps(gs, ensure_ascii=False) if gs else "")


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())[:40]
    if not name:
        raise ValueError("Name fehlt")
    return name


def find(group_id: str) -> dict | None:
    return next((g for g in groups() if g["id"] == str(group_id)), None)


def create(name: str, color: str = "") -> dict:
    gs = groups()
    name = _clean_name(name)
    same = next((g for g in gs if g["name"].lower() == name.lower()), None)
    if same:
        return same
    g = {"id": secrets.token_hex(3), "name": name,
         "color": color if color in COLORS else COLORS[len(gs) % len(COLORS)], "members": []}
    gs.append(g)
    _save(gs)
    return g


def update(group_id: str, name: str | None = None, color: str | None = None) -> None:
    gs = groups()
    g = next((x for x in gs if x["id"] == str(group_id)), None)
    if not g:
        raise ValueError("Gruppe nicht gefunden")
    if name is not None:
        g["name"] = _clean_name(name)
    if color:
        g["color"] = color if color in COLORS else g["color"]
    _save(gs)


def delete(group_id: str) -> None:
    gs = [g for g in groups() if g["id"] != str(group_id)]
    _save(gs)


def move(group_id: str, direction: int) -> None:
    gs = groups()
    i = next((n for n, g in enumerate(gs) if g["id"] == str(group_id)), None)
    j = None if i is None else i + (1 if direction > 0 else -1)
    if i is None or j < 0 or j >= len(gs):
        return
    gs[i], gs[j] = gs[j], gs[i]
    _save(gs)


def assign(keys: list[str], group_id: str | None) -> None:
    """Geräte in eine Gruppe legen (aus anderen raus). group_id leer/None = aus allen Gruppen nehmen."""
    keys = [k.strip().lower() for k in keys if k and k.strip()]
    gs = groups()
    if group_id and not any(g["id"] == str(group_id) for g in gs):
        raise ValueError("Gruppe nicht gefunden")
    for g in gs:
        g["members"] = [m for m in g["members"] if m not in keys]
        if g["id"] == str(group_id):
            g["members"] += keys
    _save(gs)


def group_of() -> dict[str, dict]:
    """Geräte-Schlüssel -> Gruppe."""
    return {m: g for g in groups() for m in g["members"]}


def sections(devices: list[dict]) -> list[dict]:
    """Geräteliste in Abschnitte: jede Gruppe (in Reihenfolge, auch leer), dann „Ohne Gruppe“."""
    gs = groups()
    by_key = group_of()
    buckets: dict[str, list[dict]] = {g["id"]: [] for g in gs}
    rest = []
    for d in devices:
        g = by_key.get(db.device_key(d))
        (buckets[g["id"]] if g else rest).append(d)
    out = [{"group": g, "devices": buckets[g["id"]]} for g in gs]
    if rest or not gs:
        out.append({"group": None, "devices": rest})
    return out
