"""WARP/VPN: Verkehr einzelner Geräte durch einen Cloudflare-WARP- oder WireGuard-Tunnel (z. B. Proton VPN) schicken.

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
DIRECT = "direct"  # Gerät ausdrücklich ohne Tunnel (auch wenn ein Standard eingestellt ist)
DEFAULT_KEY = "warp_default"
TABLE_BASE = 200
MARK_BASE = 0x100
RULE_PRIORITY_BASE = 1000
STATUS_MAX_AGE = 60  # Sekunden; der Container schreibt alle paar Sekunden
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def mark(index: int) -> str:
    return hex(MARK_BASE + index)


def slot_dir(s: Settings, slot: str) -> str:
    return os.path.join(s.warp_dir, slot)


def kind(slot: str) -> str:
    """"vpn" für WireGuard-Profile (vpn1, vpn2 ...), sonst "warp"."""
    return "vpn" if slot.startswith("vpn") else "warp"


def secret_name(slot: str) -> str:
    return "wg.conf" if kind(slot) == "vpn" else "token"


def _write_secret(s: Settings, slot: str, text: str) -> None:
    os.makedirs(slot_dir(s, slot), exist_ok=True)
    path = os.path.join(slot_dir(s, slot), secret_name(slot))
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def save_token(s: Settings, slot: str, token: str) -> None:
    _write_secret(s, slot, token.strip() + "\n")


def check_wireguard_config(text: str) -> str:
    """Prüft eine WireGuard-.conf (z. B. von Proton VPN) und gibt sie bereinigt zurück. ValueError bei Fehlern."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > 8192 or "\0" in text:
        raise ValueError("Datei zu groß oder ungültig")
    sections, keys = set(), set()
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("["):
            sections.add(line.lower())
        elif "=" in line and not line.startswith("#"):
            keys.add(line.split("=", 1)[0].strip().lower())
    if "[interface]" not in sections or "[peer]" not in sections:
        raise ValueError("Das ist keine WireGuard-Konfiguration ([Interface] und [Peer] fehlen)")
    for needed in ("privatekey", "publickey", "endpoint", "address"):
        if needed not in keys:
            raise ValueError(f"In der Konfiguration fehlt {needed}")
    return text + "\n"


def save_vpn_config(s: Settings, slot: str, text: str) -> None:
    _write_secret(s, slot, check_wireguard_config(text))


def remove_token(s: Settings, slot: str) -> None:
    try:
        os.remove(os.path.join(slot_dir(s, slot), secret_name(slot)))
    except FileNotFoundError:
        pass


def has_token(s: Settings, slot: str) -> bool:
    path = os.path.join(slot_dir(s, slot), secret_name(slot))
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
        return SlotStatus(False, "Container läuft nicht (cloudy update ausführen)")
    if age > STATUS_MAX_AGE:
        return SlotStatus(False, "Container antwortet nicht")
    if text == "Connected":
        return SlotStatus(True, "verbunden")
    return SlotStatus(False, text or "unbekannt")


@dataclass(frozen=True)
class Plan:
    mangle_rules: tuple[tuple[str, ...], ...]
    routes: tuple[tuple[str, ...], ...]  # pro Slot: Argumente für "ip route replace ... table N"


def build_plan(
    s: Settings, assignments: dict[str, str], connected: dict[str, bool], default_slot: str = ""
) -> Plan:
    """assignments: MAC -> Slot (oder "direct"). connected: Slot -> Tunnel verbunden? default_slot: für alle anderen."""
    lan = str(s.lan_network)
    index_of = {slot: i for i, (slot, _) in enumerate(s.warp_slots, start=1)}
    rules = []
    for mac in sorted(m for m in assignments if MAC_RE.match(m)):
        slot = assignments[mac]
        if slot == DIRECT:
            # Nicht markieren und die Standard-Regel unten überspringen.
            rules.append(("-i", s.lan_interface, "-m", "mac", "--mac-source", mac, "-j", "RETURN"))
        elif slot in index_of:
            rules.append((
                "-i", s.lan_interface, "-m", "mac", "--mac-source", mac,
                "!", "-d", lan, "-j", "MARK", "--set-mark", mark(index_of[slot]),
            ))
    if default_slot in index_of:
        # Alle Geräte, die noch keine Markierung haben.
        rules.append((
            "-i", s.lan_interface, "-m", "mark", "--mark", "0x0",
            "!", "-d", lan, "-j", "MARK", "--set-mark", mark(index_of[default_slot]),
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
    _run(["ip6tables", "-t", "mangle", "-F", MANGLE_CHAIN])


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

        _run(["ip6tables", "-t", "mangle", "-N", MANGLE_CHAIN])
        _run(["ip6tables", "-t", "mangle", "-F", MANGLE_CHAIN])
        if not _run(["ip6tables", "-t", "mangle", "-C", "PREROUTING", "-j", MANGLE_CHAIN]):
            _run(["ip6tables", "-t", "mangle", "-I", "PREROUTING", "1", "-j", MANGLE_CHAIN])

        for index, _ in enumerate(self.s.warp_slots, start=1):
            prio, table = str(RULE_PRIORITY_BASE + index), str(TABLE_BASE + index)
            while _run(["ip", "rule", "del", "priority", prio]):
                pass
            ok &= _run(["ip", "rule", "add", "fwmark", mark(index), "table", table, "priority", prio])
            while _run(["ip", "-6", "rule", "del", "priority", prio]):
                pass
            _run(["ip", "-6", "rule", "add", "fwmark", mark(index), "table", table, "priority", prio])

        # Antworten kommen über die Docker-Bridge zurück, obwohl der Absender "im Internet" liegt.
        _write_sysctl("/proc/sys/net/ipv4/conf/all/rp_filter", "2")
        _write_sysctl("/proc/sys/net/ipv6/conf/all/forwarding", "1")
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
                _run(["ip6tables", "-A", FWD_CHAIN, "-i", lan, "-o", dev, "-j", "ACCEPT"])
                _run(["ip6tables", "-A", FWD_CHAIN, "-i", dev, "-o", lan, "-j", "ACCEPT"])
                self._forwarded.add(dev)

    def apply(self, plan: Plan) -> None:
        self._ensure_forwarding()
        if plan.mangle_rules != self._applied_rules:
            _run(["iptables", "-t", "mangle", "-F", MANGLE_CHAIN])
            _run(["ip6tables", "-t", "mangle", "-F", MANGLE_CHAIN])
            for rule in plan.mangle_rules:
                _run(["iptables", "-t", "mangle", "-A", MANGLE_CHAIN, *rule])
                v6_rule = [r if r != str(self.s.lan_network) else "fe80::/10" for r in rule]
                _run(["ip6tables", "-t", "mangle", "-A", MANGLE_CHAIN, *v6_rule])
            self._applied_rules = plan.mangle_rules
        for index, route in enumerate(plan.routes):
            if self._applied_routes.get(index) != route and _run(["ip", "route", "replace", *route]):
                print(f"cloudy: WARP {self.s.warp_slots[index][0]}: {' '.join(route)}", flush=True)
                self._applied_routes[index] = route
                last_byte = self.s.warp_slots[index][1].split(".")[-1]
                v6_via = f"fd29:86::{last_byte}"
                table = route[-1]
                if "via" in route:
                    _run(["ip", "-6", "route", "replace", "default", "via", v6_via, "table", table])
                elif "unreachable" in route:
                    _run(["ip", "-6", "route", "replace", "unreachable", "default", "table", table])
                else:
                    _run(["ip", "-6", "route", "replace", "default", "dev", self.s.lan_interface, "table", table])

    def tick(self) -> None:
        connected = {slot: read_status(self.s, slot).connected for slot, _ in self.s.warp_slots}
        self.apply(build_plan(self.s, db.device_warp(), connected, db.get_setting(DEFAULT_KEY)))

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
