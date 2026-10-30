"""WARP/VPN: Verkehr einzelner Geräte durch einen Cloudflare-WARP- oder WireGuard-Tunnel (z. B. Proton VPN) schicken.

Pro Cloudflare-Account läuft ein eigener Container (warp1, warp2 …, siehe docker-compose.yml) im Docker-Netz
"cloudy-warp". Das Dashboard legt dessen Token in data/warp/<slot>/token ab, der Container meldet sich damit als
WARP Connector an und schreibt seinen Zustand nach data/warp/<slot>/status.

Hier auf dem Pi (Gateway-Modus): Pakete zugeordneter Geräte (nach MAC-Adresse) bekommen eine Markierung, jede
Markierung hat eine eigene Routing-Tabelle, deren Standardroute auf den WARP-Container zeigt. Ist der Tunnel weg,
zeigt sie auf die Fritzbox (oder, mit WARP_KILLSWITCH=1, ins Leere).

Seiten-Regeln: Einzelne Domains laufen für alle Geräte durch ein festes Profil, egal was beim Gerät eingestellt ist.
dnsmasq trägt die IPs dieser Domains beim Auflösen in ein ipset pro Profil ein, die Markierung kommt dann vor
den Geräte-Regeln (und beendet die Kette).
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from . import db, ipv6
from .settings import Settings
from .traffic import FWD_CHAIN, _run, _write_sysctl

MANGLE_CHAIN = "CLOUDY-WARP"
DIRECT = "direct"  # Gerät ausdrücklich ohne Tunnel (auch wenn ein Standard eingestellt ist)
DEFAULT_KEY = "warp_default"
TABLE_BASE = 200
MARK_BASE = 0x100
RULE_PRIORITY_BASE = 1000
STATUS_MAX_AGE = 60  # Sekunden; der Container schreibt alle paar Sekunden
IPV6_MAX_AGE = 120  # Der Container prüft IPv6 etwa alle 30 Sekunden
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def mark(index: int) -> str:
    return hex(MARK_BASE + index)


MEIN_KEY = "mein_choices"
STANDARD = "standard"  # Auswahl „Standard“ (kein eigenes Profil für das Gerät)


def mein_allowed() -> set[str] | None:
    """Was andere Nutzer auf „Mein Gerät“ auswählen dürfen: None = alles, sonst Menge aus "standard", "direct" und Slots."""
    value = db.get_setting(MEIN_KEY).strip()
    if value in ("", "all"):
        return None
    return {v for v in value.split(",") if v and v != "none"}


def save_mein_allowed(choices: list[str], valid: set[str]) -> None:
    picked = sorted(set(choices) & valid)
    db.set_setting(MEIN_KEY, "all" if picked and set(picked) == valid else (",".join(picked) or "none"))


def mein_choice_ok(slot: str, current: str) -> bool:
    """Darf der Nutzer diese Auswahl treffen? Die aktuell gesetzte bleibt immer erlaubt (sonst wäre „Übernehmen“ gesperrt)."""
    allowed = mein_allowed()
    return allowed is None or (slot or STANDARD) in allowed or slot == current


def site_set(index: int, v6: bool = False) -> str:
    """Name des ipset, in das dnsmasq die IPs der Seiten für dieses Profil einträgt."""
    return f"cloudy_s{index}" + ("_6" if v6 else "")


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


def read_ipv6_ok(s: Settings, slot: str, now: float | None = None) -> bool:
    """Kann der Tunnel dieses Containers IPv6? (Der Container prüft das selbst und schreibt "ok" nach <slot>/ipv6.)"""
    path = os.path.join(slot_dir(s, slot), "ipv6")
    now = now if now is not None else time.time()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.readline().strip()
        age = now - os.path.getmtime(path)
    except FileNotFoundError:
        return False
    return text == "ok" and age <= IPV6_MAX_AGE


@dataclass(frozen=True)
class Plan:
    mangle_rules: tuple[tuple[str, ...], ...]
    routes: tuple[tuple[str, ...], ...]  # pro Slot: Argumente für "ip route replace ... table N"
    mangle_rules6: tuple[tuple[str, ...], ...] = ()  # wie mangle_rules, für ip6tables (nur mit IPV6_ENABLED)
    routes6: tuple[tuple[str, ...], ...] = ()  # wie routes, für "ip -6 route replace"


def _mark_rules(
    s: Settings, assignments: dict[str, str], default_slot: str, index_of: dict[str, int], local_net: str,
    site_routes: dict[str, str] | None = None, v6: bool = False,
) -> tuple[tuple[str, ...], ...]:
    """Markierungen nach MAC. local_net: Ziele im eigenen Heimnetz (IPv4 oder IPv6) werden nie getunnelt."""
    rules = []
    # Seiten-Regeln zuerst und mit RETURN beenden: sie gelten für alle Geräte (auch für „direkt“).
    for slot in sorted({sl for sl in (site_routes or {}).values() if sl in index_of}, key=index_of.get):
        rules.append((
            "-i", s.lan_interface, "-m", "set", "--match-set", site_set(index_of[slot], v6), "dst",
            "!", "-d", local_net, "-j", "MARK", "--set-mark", mark(index_of[slot]),
        ))
        rules.append(("-i", s.lan_interface, "-m", "mark", "--mark", mark(index_of[slot]), "-j", "RETURN"))
    for mac in sorted(m for m in assignments if MAC_RE.match(m)):
        slot = assignments[mac]
        if slot == DIRECT:
            # Nicht markieren und die Standard-Regel unten überspringen.
            rules.append(("-i", s.lan_interface, "-m", "mac", "--mac-source", mac, "-j", "RETURN"))
        elif slot in index_of:
            rules.append((
                "-i", s.lan_interface, "-m", "mac", "--mac-source", mac,
                "!", "-d", local_net, "-j", "MARK", "--set-mark", mark(index_of[slot]),
            ))
    if default_slot in index_of:
        # Alle Geräte, die noch keine Markierung haben.
        rules.append((
            "-i", s.lan_interface, "-m", "mark", "--mark", "0x0",
            "!", "-d", local_net, "-j", "MARK", "--set-mark", mark(index_of[default_slot]),
        ))
    return tuple(rules)


def build_plan(
    s: Settings,
    assignments: dict[str, str],
    connected: dict[str, bool],
    default_slot: str = "",
    ipv6_ok: dict[str, bool] | None = None,
    site_routes: dict[str, str] | None = None,
) -> Plan:
    """assignments: MAC -> Slot (oder "direct"). connected: Slot -> Tunnel verbunden? default_slot: für alle anderen.
    ipv6_ok: Slot -> Tunnel kann IPv6 (nur mit IPV6_ENABLED)."""
    index_of = {slot: i for i, (slot, _) in enumerate(s.warp_slots, start=1)}
    rules = _mark_rules(s, assignments, default_slot, index_of, str(s.lan_network), site_routes)
    routes = []
    for index, (slot, ip) in enumerate(s.warp_slots, start=1):
        table = str(TABLE_BASE + index)
        if connected.get(slot):
            routes.append(("default", "via", ip, "table", table))
        elif s.warp_killswitch:
            routes.append(("unreachable", "default", "table", table))
        else:
            routes.append(("default", "via", s.router_ip, "dev", s.lan_interface, "table", table))
    if not s.ipv6_enabled:
        return Plan(rules, tuple(routes))
    rules6 = _mark_rules(s, assignments, default_slot, index_of, str(ipv6.prefix(s)), site_routes, v6=True)
    routes6 = []
    for index, (slot, ip) in enumerate(s.warp_slots, start=1):
        table = str(TABLE_BASE + index)
        if connected.get(slot) and (ipv6_ok or {}).get(slot):
            routes6.append(("default", "via", ipv6.slot_address(ip), "table", table))
        else:
            # Kein IPv6 im Tunnel (oder Tunnel weg): nie über die Fritzbox, sondern sofort "keine Route".
            routes6.append(("unreachable", "default", "table", table))
    return Plan(rules, tuple(routes), rules6, tuple(routes6))


def route_device(ip: str) -> str | None:
    """Über welche Schnittstelle erreicht der Pi den WARP-Container (auf dem Pi: die Docker-Bridge)?"""
    out = subprocess.run(["ip", "-o", "route", "get", ip], capture_output=True, text=True).stdout
    m = re.search(r" dev (\S+)", out)
    return m.group(1) if m else None


def cleanup() -> None:
    """Gateway aus: alte Markierungen aus einem früheren Lauf entfernen (die Regeln liegen im Host-Netz)."""
    _run(["iptables", "-t", "mangle", "-F", MANGLE_CHAIN])
    _cleanup_ipv6(9)


def create_site_sets(s: Settings) -> None:
    """ipsets für die Seiten-Regeln anlegen (gibt es sie schon, bleibt der Inhalt). Einträge laufen nach 24 h ab."""
    for index, _ in enumerate(s.warp_slots, start=1):
        _run(["ipset", "create", "-exist", site_set(index), "hash:ip", "timeout", "86400"])
        if s.ipv6_enabled:
            _run(["ipset", "create", "-exist", site_set(index, True), "hash:ip", "family", "inet6", "timeout", "86400"])


def _cleanup_ipv6(slots: int) -> None:
    _run(["ip6tables", "-t", "mangle", "-D", "PREROUTING", "-j", MANGLE_CHAIN])
    _run(["ip6tables", "-t", "mangle", "-F", MANGLE_CHAIN])
    _run(["ip6tables", "-t", "mangle", "-X", MANGLE_CHAIN])
    for index in range(1, slots + 1):
        while _run(["ip", "-6", "rule", "del", "priority", str(RULE_PRIORITY_BASE + index)]):
            pass
    while _run(["ip6tables", "-D", FWD_CHAIN, "-m", "mark", "!", "--mark", "0x0", "-j", "DROP"]):
        pass


def ipv6_bypass(iface: str) -> bool:
    """True, wenn im Heimnetz ein IPv6-Router (die Fritzbox) aktiv ist. Dann schicken die Geräte IPv6 direkt
    dorthin – am Pi und damit am VPN vorbei. Erkennt das an der IPv6-Standardroute, die der Pi selbst lernt."""
    try:
        out = subprocess.run(
            ["ip", "-6", "-j", "route", "show", "default", "dev", iface], capture_output=True, text=True, timeout=5
        ).stdout
        routes = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return any(r.get("protocol") == "ra" or str(r.get("gateway", "")).startswith("fe80:") for r in routes)


class WarpRouter:
    def __init__(self, settings: Settings, interval: float = 5.0):
        self.s = settings
        self.interval = interval
        self._stopping = threading.Event()
        self._applied_rules: tuple | None = None
        self._applied_routes: dict[int, tuple] = {}
        self._applied_rules6: tuple | None = None
        self._applied_routes6: dict[int, tuple] = {}
        self._ticks = 0
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

        create_site_sets(self.s)
        # Alte Regeln aus einem früheren Lauf entfernen; mit IPV6_ENABLED werden sie gleich neu aufgebaut.
        _cleanup_ipv6(len(self.s.warp_slots))
        if self.s.ipv6_enabled:
            _run(["ip6tables", "-t", "mangle", "-N", MANGLE_CHAIN])
            _run(["ip6tables", "-t", "mangle", "-F", MANGLE_CHAIN])
            if not _run(["ip6tables", "-t", "mangle", "-C", "PREROUTING", "-j", MANGLE_CHAIN]):
                _run(["ip6tables", "-t", "mangle", "-I", "PREROUTING", "1", "-j", MANGLE_CHAIN])
            for index, _ in enumerate(self.s.warp_slots, start=1):
                prio, table = str(RULE_PRIORITY_BASE + index), str(TABLE_BASE + index)
                _run(["ip", "-6", "rule", "add", "fwmark", mark(index), "table", table, "priority", prio])
            _write_sysctl("/proc/sys/net/ipv6/conf/all/forwarding", "1")
        # Antworten kommen über die Docker-Bridge zurück, obwohl der Absender "im Internet" liegt.
        _write_sysctl("/proc/sys/net/ipv4/conf/all/rp_filter", "2")
        return ok

    def _ensure_forwarding(self) -> None:
        """Docker blockt Weiterleitung zwischen LAN und Bridge -> in der Gateway-Kette freigeben."""
        if self.s.warp_killswitch:
            if not _run(["iptables", "-C", FWD_CHAIN, "-m", "mark", "!", "--mark", "0x0", "-o", self.s.lan_interface, "-j", "DROP"]):
                _run(["iptables", "-I", FWD_CHAIN, "1", "-m", "mark", "!", "--mark", "0x0", "-o", self.s.lan_interface, "-j", "DROP"])
        for _, ip in self.s.warp_slots:
            dev = route_device(ip)
            if not dev or dev == self.s.lan_interface or dev in self._forwarded:
                continue
            _write_sysctl(f"/proc/sys/net/ipv4/conf/{dev}/rp_filter", "2")
            lan = self.s.lan_interface
            if _run(["iptables", "-A", FWD_CHAIN, "-i", lan, "-o", dev, "-j", "ACCEPT"]) and _run(
                ["iptables", "-A", FWD_CHAIN, "-i", dev, "-o", lan, "-j", "ACCEPT"]
            ):
                if self.s.ipv6_enabled:
                    _run(["ip6tables", "-A", FWD_CHAIN, "-i", lan, "-o", dev, "-j", "ACCEPT"])
                    _run(["ip6tables", "-A", FWD_CHAIN, "-i", dev, "-o", lan, "-j", "ACCEPT"])
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
        if not self.s.ipv6_enabled:
            return
        if plan.mangle_rules6 != self._applied_rules6:
            _run(["ip6tables", "-t", "mangle", "-F", MANGLE_CHAIN])
            for rule in plan.mangle_rules6:
                _run(["ip6tables", "-t", "mangle", "-A", MANGLE_CHAIN, *rule])
            self._applied_rules6 = plan.mangle_rules6
        for index, route in enumerate(plan.routes6):
            if self._applied_routes6.get(index) != route and _run(["ip", "-6", "route", "replace", *route]):
                print(f"cloudy: WARP {self.s.warp_slots[index][0]} IPv6: {' '.join(route)}", flush=True)
                self._applied_routes6[index] = route

    def tick(self) -> None:
        connected = {slot: read_status(self.s, slot).connected for slot, _ in self.s.warp_slots}
        ipv6_ok = None
        if self.s.ipv6_enabled:
            ipv6_ok = {slot: read_ipv6_ok(self.s, slot) for slot, _ in self.s.warp_slots}
            # Adresse des Pi im Heimnetz gelegentlich erneuern (z. B. nach einem Neustart des Netzwerks).
            self._ticks += 1
            if self._ticks % 12 == 1:
                ipv6.ensure_lan(self.s)
        self.apply(build_plan(self.s, db.device_warp(), connected, db.get_setting(DEFAULT_KEY), ipv6_ok, db.site_routes()))

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
