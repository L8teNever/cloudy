"""Dynamische WARP-/VPN-Profile: Liste in data/warp_slots.json, Compose-Overlay, optional Docker anwenden.

Die festen vier Profile aus der alten compose-Datei sind der Startzustand. Weitere Profile (warp3…, vpn3…)
werden im Dashboard angelegt; Löschen entfernt sie aus der Liste und aus dem Overlay.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

SLOTS_FILE = "warp_slots.json"
COMPOSE_FILE = "docker-compose.slots.yml"
DEFAULTS = (
    ("warp1", "172.29.86.11", "fd29:86::11"),
    ("warp2", "172.29.86.12", "fd29:86::12"),
    ("vpn1", "172.29.86.21", "fd29:86::21"),
    ("vpn2", "172.29.86.22", "fd29:86::22"),
)
SLOT_RE = re.compile(r"^(warp|vpn)([1-9][0-9]?)$")
MAX_PER_KIND = 20


@dataclass(frozen=True)
class Slot:
    name: str
    ip: str
    ip6: str

    @property
    def kind(self) -> str:
        return "vpn" if self.name.startswith("vpn") else "warp"


def _path(data_dir: str, name: str) -> str:
    return os.path.join(data_dir, name)


def _parse_env_slots() -> list[Slot]:
    raw = os.environ.get("WARP_SLOTS", "")
    if not raw.strip():
        return [Slot(n, ip4, ip6) for n, ip4, ip6 in DEFAULTS]
    out: list[Slot] = []
    for part in raw.split(","):
        name, _, ip = part.partition("=")
        name, ip = name.strip(), ip.strip()
        if not name or not ip:
            continue
        m = SLOT_RE.match(name)
        if not m:
            continue
        n = int(m.group(2))
        base = 10 if m.group(1) == "warp" else 20
        ip6 = f"fd29:86::{base + n}"
        out.append(Slot(name, ip, ip6))
    return out or [Slot(n, ip4, ip6) for n, ip4, ip6 in DEFAULTS]


def load(data_dir: str) -> list[Slot]:
    path = _path(data_dir, SLOTS_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        slots = _parse_env_slots()
        save(data_dir, slots)
        return slots
    out: list[Slot] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        ip = str(item.get("ip", "")).strip()
        ip6 = str(item.get("ip6", "")).strip()
        if SLOT_RE.match(name) and ip:
            if not ip6:
                m = SLOT_RE.match(name)
                base = 10 if m.group(1) == "warp" else 20
                ip6 = f"fd29:86::{base + int(m.group(2))}"
            out.append(Slot(name, ip, ip6))
    if not out:
        out = _parse_env_slots()
        save(data_dir, out)
    return out


def save(data_dir: str, slots: list[Slot]) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = _path(data_dir, SLOTS_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([{"name": s.name, "ip": s.ip, "ip6": s.ip6} for s in slots], f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def as_pairs(slots: list[Slot]) -> list[tuple[str, str]]:
    return [(s.name, s.ip) for s in slots]


def _next_index(slots: list[Slot], kind: str) -> int:
    used = {int(SLOT_RE.match(s.name).group(2)) for s in slots if s.name.startswith(kind) and SLOT_RE.match(s.name)}
    for i in range(1, MAX_PER_KIND + 1):
        if i not in used:
            return i
    raise ValueError(f"Maximal {MAX_PER_KIND} {kind.upper()}-Profile")


def add(data_dir: str, kind: str) -> Slot:
    kind = "vpn" if kind == "vpn" else "warp"
    slots = load(data_dir)
    n = _next_index(slots, kind)
    base = 10 if kind == "warp" else 20
    slot = Slot(f"{kind}{n}", f"172.29.86.{base + n}", f"fd29:86::{base + n}")
    slots.append(slot)
    slots.sort(key=lambda s: (0 if s.kind == "warp" else 1, int(SLOT_RE.match(s.name).group(2))))
    save(data_dir, slots)
    write_compose(data_dir, slots)
    return slot


def remove(data_dir: str, name: str) -> None:
    name = (name or "").strip()
    if not SLOT_RE.match(name):
        raise ValueError("Ungültiges Profil")
    before = load(data_dir)
    if name not in {s.name for s in before}:
        raise ValueError("Profil nicht gefunden")
    if len(before) <= 1:
        raise ValueError("Mindestens ein Profil muss bleiben")
    slots = [s for s in before if s.name != name]
    save(data_dir, slots)
    write_compose(data_dir, slots)


def write_compose(data_dir: str, slots: list[Slot] | None = None) -> str:
    """Schreibt data/docker-compose.slots.yml. Rückgabe: Pfad."""
    slots = load(data_dir) if slots is None else slots
    lines = [
        "# Automatisch von cloudy erzeugt – nicht von Hand bearbeiten.",
        "# Wird von „cloudy slots“ / „cloudy update“ mit docker-compose.yml zusammengeführt.",
        "services:",
    ]
    for s in slots:
        image = "ghcr.io/l8tenever/cloudy:vpn" if s.kind == "vpn" else "ghcr.io/l8tenever/cloudy:warp"
        lines += [
            f"  {s.name}:",
            f"    image: {image}",
            f"    container_name: cloudy-{s.name}",
            "    restart: unless-stopped",
            "    cap_add:",
            "      - NET_ADMIN",
            "    devices:",
            "      - /dev/net/tun",
            "    sysctls:",
            '      net.ipv4.ip_forward: "1"',
            '      net.ipv4.conf.all.src_valid_mark: "1"',
            '      net.ipv4.conf.all.rp_filter: "2"',
            '      net.ipv6.conf.all.disable_ipv6: "0"',
            '      net.ipv6.conf.default.disable_ipv6: "0"',
            '      net.ipv6.conf.all.forwarding: "1"',
            "    environment:",
            "      TZ: Europe/Berlin",
            "    volumes:",
            f"      - ./data/warp/{s.name}:/cloudy",
            "    networks:",
            "      cloudy-warp:",
            f"        ipv4_address: {s.ip}",
            f"        ipv6_address: {s.ip6}",
            "",
        ]
    path = _path(data_dir, COMPOSE_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    os.replace(tmp, path)
    return path


def ensure(data_dir: str) -> list[Slot]:
    """Datei + Compose anlegen (Start / Tests)."""
    slots = load(data_dir)
    write_compose(data_dir, slots)
    return slots


def try_apply(data_dir: str) -> str:
    """Compose-Datei schreiben. Container startet der Host mit „cloudy slots“ (Docker-Socket bleibt draußen)."""
    write_compose(data_dir)
    return "Gespeichert. Auf dem Pi jetzt „cloudy slots“ ausführen, damit die Container starten oder verschwinden."
