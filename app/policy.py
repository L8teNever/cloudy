"""Wirksames „Internet über“ aus Gerätegruppen und Einzel-Einstellung.

Reihenfolge: festgelegte Gruppe (oben zuerst) → Geräte-Einstellung → weiche Gruppe → globaler Standard.
"""

from dataclasses import dataclass

from . import db, warp


@dataclass(frozen=True)
class Effective:
    """slot leer = globaler Standard (warp_default). source: locked|device|group|default."""

    slot: str
    source: str
    group_id: int | None = None
    group_name: str = ""
    locked: bool = False


def resolve(mac: str, device_map: dict[str, str] | None = None, by_mac: dict[str, list[dict]] | None = None) -> Effective:
    mac = (mac or "").strip().lower()
    if not mac:
        return Effective("", "default")
    device_map = db.device_warp() if device_map is None else device_map
    by_mac = db.groups_by_mac() if by_mac is None else by_mac
    groups = by_mac.get(mac) or []
    for g in groups:
        if g["locked"] and g["slot"]:
            return Effective(g["slot"], "locked", g["id"], g["name"], True)
    if mac in device_map:
        return Effective(device_map[mac], "device")
    for g in groups:
        if g["slot"]:
            return Effective(g["slot"], "group", g["id"], g["name"], False)
    return Effective("", "default")


def assignments(device_map: dict[str, str] | None = None, by_mac: dict[str, list[dict]] | None = None) -> dict[str, str]:
    """MAC → Slot für die Firewall (nur Geräte mit explizitem Profil, nicht „Standard“)."""
    device_map = db.device_warp() if device_map is None else device_map
    by_mac = db.groups_by_mac() if by_mac is None else by_mac
    out: dict[str, str] = {}
    for mac in set(device_map) | set(by_mac):
        eff = resolve(mac, device_map, by_mac)
        if eff.slot:
            out[mac] = eff.slot
    return out


def locked_for(mac: str, by_mac: dict[str, list[dict]] | None = None) -> Effective | None:
    """Festgelegte Gruppe für dieses Gerät, sonst None."""
    eff = resolve(mac, by_mac=by_mac)
    return eff if eff.locked else None


def label_for(eff: Effective, warp_names: dict[str, str], default_slot: str = "") -> str:
    """Kurzer Text für die UI, z. B. „normal (Fritzbox)“ oder Profilname."""
    slot = eff.slot or default_slot
    if not slot or slot == warp.DIRECT:
        return "normal (Fritzbox)"
    return warp_names.get(slot, slot)
