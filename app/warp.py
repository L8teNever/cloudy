"""WARP: Verkehr einzelner Geräte durch einen Cloudflare-WARP-Tunnel schicken.

Pro Cloudflare-Account läuft ein eigener Container (warp1, warp2 …, siehe docker-compose.yml) im Docker-Netz
"cloudy-warp". Das Dashboard legt dessen Token in data/warp/<slot>/token ab, der Container meldet sich damit als
WARP Connector an und schreibt seinen Zustand nach data/warp/<slot>/status.

Hier auf dem Pi (Gateway-Modus): Pakete zugeordneter Geräte (nach MAC-Adresse) bekommen eine Markierung, jede
Markierung hat eine eigene Routing-Tabelle, deren Standardroute auf den WARP-Container zeigt. Ist der Tunnel weg,
zeigt sie auf die Fritzbox (oder, mit WARP_KILLSWITCH=1, ins Leere).
"""

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from . import db
from .settings import Settings
from .traffic import FWD_CHAIN, _run, _write_sysctl

MANGLE_CHAIN = "CLOUDY-WARP"
TABLE_BASE = 200
MARK_BASE = 0x100
RULE_PRIORITY_BASE = 1000
STATUS_MAX_AGE = 60  # Sekunden; der Container schreibt alle paar Sekunden
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def mark(index: int) -> str:
    return hex(MARK_BASE + index)


def slot_dir(s: Settings, slot: str) -> str:
    return os.path.join(s.warp_dir, slot)


def save_token(s: Settings, slot: str, token: str) -> None:
    os.makedirs(slot_dir(s, slot), exist_ok=True)
    path = os.path.join(slot_dir(s, slot), "token")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token.strip() + "\n")
    os.replace(tmp, path)


def remove_token(s: Settings, slot: str) -> None:
    try:
        os.remove(os.path.join(slot_dir(s, slot), "token"))
    except FileNotFoundError:
        pass


def has_token(s: Settings, slot: str) -> bool:
    path = os.path.join(slot_dir(s, slot), "token")
    return os.path.exists(path) and os.path.getsize(path) > 0


@dataclass(frozen=True)
class SlotStatus:
    connected: bool
    text: str


def read_status(s: Settings, slot: str, now: float | None = None) -> SlotStatus:
    path = os.path.join(slot_dir(s, slot), "status")
    now = now if now is not None else time.time()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.readline().strip()
        age = now - os.path.getmtime(path)
    except FileNotFoundError:
        return SlotStatus(False, "WARP-Container läuft nicht (cloudy update ausführen)")
    if age > STATUS_MAX_AGE:
        return SlotStatus(False, "WARP-Container antwortet nicht")
    if text == "Connected":
        return SlotStatus(True, "verbunden")
    return SlotStatus(False, text or "unbekannt")


@dataclass(frozen=True)
class Plan:
    mangle_rules: tuple[tuple[str, ...], ...]
    routes: tuple[tuple[str, ...], ...]  # pro Slot: Argumente für "ip route replace ... table N"


def build_plan(s: Settings, assignments: dict[str, str], connected: dict[str, bool]) -> Plan:
    """assignments: MAC -> Slot. connected: Slot -> Tunnel verbunden?"""
    lan = str(s.lan_network)
    rules = []
    for index, (slot, _) in enumerate(s.warp_slots, start=1):
        for mac in sorted(m for m, sl in assignments.items() if sl == slot and MAC_RE.match(m)):
            rules.append((
                "-i", s.lan_interface, "-m", "mac", "--mac-source", mac,
                "!", "-d", lan, "-j", "MARK", "--set-mark", mark(index),
            ))
    routes = []
    for index, (slot, ip) in enumerate(s.warp_slots, start=1):
        table = str(TABLE_BASE + index)
        if connected.get(slot):
            routes.append(("default", "via", ip, "table", table))
        elif s.warp_killswitch:
            routes.append(("unreachable", "default", "table", table))
        else:
            routes.append(("default", "via", s.router_ip, "dev", s.lan_interface, "table", table))
    return Plan(tuple(rules), tuple(routes))


def route_device(ip: str) -> str | None:
    """Über welche Schnittstelle erreicht der Pi den WARP-Container (auf dem Pi: die Docker-Bridge)?"""
    out = subprocess.run(["ip", "-o", "route", "get", ip], capture_output=True, text=True).stdout
    m = re.search(r" dev (\S+)", out)
    return m.group(1) if m else None


def cleanup() -> None:
    """Gateway aus: alte Markierungen aus einem früheren Lauf entfernen (die Regeln liegen im Host-Netz)."""
    _run(["iptables", "-t", "mangle", "-F", MANGLE_CHAIN])


class WarpRouter:
    def __init__(self, settings: Settings, interval: float = 5.0):
        self.s = settings
        self.interval = interval
        self._stopping = threading.Event()
        self._applied_rules: tuple | None = None
        self._applied_routes: dict[int, tuple] = {}
        self._forwarded: set[str] = set()

    def setup(self) -> bool:
        ok = _run(["iptables", "-t", "mangle", "-N", MANGLE_CHAIN]) or _run(
            ["iptables", "-t", "mangle", "-L", MANGLE_CHAIN, "-n"]
        )
        ok &= _run(["iptables", "-t", "mangle", "-F", MANGLE_CHAIN])
        if not _run(["iptables", "-t", "mangle", "-C", "PREROUTING", "-j", MANGLE_CHAIN]):
            ok &= _run(["iptables", "-t", "mangle", "-I", "PREROUTING", "1", "-j", MANGLE_CHAIN])
        for index, _ in enumerate(self.s.warp_slots, start=1):
            prio, table = str(RULE_PRIORITY_BASE + index), str(TABLE_BASE + index)
            while _run(["ip", "rule", "del", "priority", prio]):
                pass
            ok &= _run(["ip", "rule", "add", "fwmark", mark(index), "table", table, "priority", prio])
        # Antworten kommen über die Docker-Bridge zurück, obwohl der Absender "im Internet" liegt.
        _write_sysctl("/proc/sys/net/ipv4/conf/all/rp_filter", "2")
        return ok

    def _ensure_forwarding(self) -> None:
        """Docker blockt Weiterleitung zwischen LAN und Bridge -> in der Gateway-Kette freigeben."""
        for _, ip in self.s.warp_slots:
            dev = route_device(ip)
            if not dev or dev == self.s.lan_interface or dev in self._forwarded:
                continue
            _write_sysctl(f"/proc/sys/net/ipv4/conf/{dev}/rp_filter", "2")
            lan = self.s.lan_interface
            if _run(["iptables", "-A", FWD_CHAIN, "-i", lan, "-o", dev, "-j", "ACCEPT"]) and _run(
                ["iptables", "-A", FWD_CHAIN, "-i", dev, "-o", lan, "-j", "ACCEPT"]
            ):
                self._forwarded.add(dev)

    def apply(self, plan: Plan) -> None:
        self._ensure_forwarding()
        if plan.mangle_rules != self._applied_rules:
            _run(["iptables", "-t", "mangle", "-F", MANGLE_CHAIN])
            for rule in plan.mangle_rules:
                _run(["iptables", "-t", "mangle", "-A", MANGLE_CHAIN, *rule])
            self._applied_rules = plan.mangle_rules
        for index, route in enumerate(plan.routes):
            if self._applied_routes.get(index) != route and _run(["ip", "route", "replace", *route]):
                print(f"cloudy: WARP {self.s.warp_slots[index][0]}: {' '.join(route)}", flush=True)
                self._applied_routes[index] = route

    def tick(self) -> None:
        connected = {slot: read_status(self.s, slot).connected for slot, _ in self.s.warp_slots}
        self.apply(build_plan(self.s, db.device_warp(), connected))

    def start(self) -> None:
        try:
            ok = self.setup()
        except Exception as exc:  # noqa: BLE001 – ohne WARP laufen DNS/Gateway trotzdem weiter
            print(f"cloudy: WARP-Routing nicht verfügbar: {exc}", file=sys.stderr, flush=True)
            return
        if not ok:
            print("cloudy: WARP-Routing konnte nicht vollständig eingerichtet werden", file=sys.stderr, flush=True)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 – WARP darf den Dienst nie stoppen
                print(f"cloudy: Fehler beim WARP-Routing: {exc}", file=sys.stderr, flush=True)
            if self._stopping.wait(self.interval):
                return

    def stop(self) -> None:
        self._stopping.set()
