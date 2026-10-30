"""IPv6 im Heimnetz (Schalter IPV6_ENABLED, nur zusammen mit dem Gateway-Modus).

Voraussetzung: Die Fritzbox verteilt kein IPv6 mehr an die Geräte ("Router Advertisement im LAN aktiv" aus).
Dann übernimmt der Pi: Er verteilt per Router Advertisement ein eigenes privates Netz (ULA, fd00::/8) und ist
Standard-Router dafür. Geräte, die einen WARP-/VPN-Tunnel zugeordnet haben, schicken ihr IPv6 durch den Container
(der Container schreibt die Absender auf seine Tunnel-Adresse um). Alle anderen bekommen ein sofortiges "keine Route"
und weichen auf IPv4 aus – es gibt hinter der Fritzbox kein IPv6-Internet, das wir ohne Router Advertisement nutzen
könnten.
"""

import ipaddress
import os
import secrets
import subprocess
import sys
import threading
import time

from . import db
from .settings import Settings

PREFIX_FILE = "ipv6-prefix"
NEIGHBOR_TTL = 30  # Sekunden, wie lange die Nachbartabelle (IPv6 -> MAC) wiederverwendet wird
DOCKER_V6_PREFIX = "fd29:86::"  # Netz "cloudy-warp" in docker-compose.yml


def generate_prefix() -> ipaddress.IPv6Network:
    """Zufälliges ULA-/64: fdXX:XXXX:XXXX:1::/64 (RFC 4193)."""
    raw = b"\xfd" + secrets.token_bytes(5) + b"\x00\x01" + bytes(8)
    return ipaddress.IPv6Network((int.from_bytes(raw, "big"), 64))


ULA = ipaddress.IPv6Network("fc00::/7")


def _valid(net: ipaddress.IPv6Network) -> bool:
    return net.prefixlen == 64 and net.subnet_of(ULA)


def prefix(s: Settings) -> ipaddress.IPv6Network:
    """Das /64 des Heimnetzes. IPV6_PREFIX aus der .env, sonst eins, das einmal erzeugt und gespeichert wird."""
    if s.ipv6_prefix:
        try:
            net = ipaddress.IPv6Network(s.ipv6_prefix, strict=False)
            if _valid(net):
                return net
        except ValueError:
            pass
        print(f"cloudy: IPV6_PREFIX={s.ipv6_prefix} ist kein gültiges privates /64 – nehme ein eigenes", file=sys.stderr, flush=True)
    path = os.path.join(s.data_dir, PREFIX_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            net = ipaddress.IPv6Network(f.read().strip(), strict=False)
        if _valid(net):
            return net
    except (OSError, ValueError):
        pass
    net = generate_prefix()
    os.makedirs(s.data_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(net) + "\n")
    os.replace(tmp, path)
    return net


def pi_address(net: ipaddress.IPv6Network) -> ipaddress.IPv6Address:
    return net.network_address + 1


def slot_address(slot_ip: str) -> str:
    """IPv6-Adresse eines WARP-/VPN-Containers im Netz cloudy-warp: 172.29.86.11 -> fd29:86::11 (siehe docker-compose.yml)."""
    return DOCKER_V6_PREFIX + slot_ip.rsplit(".", 1)[-1]


def dnsmasq_lines(s: Settings) -> list[str]:
    """Router Advertisement: Geräte bilden aus dem Präfix selbst ihre Adresse (SLAAC), der Pi ist Router und DNS."""
    net = prefix(s)
    return [
        "enable-ra",
        # "high": Geräte nehmen den Pi als Router, selbst wenn noch ein anderer Router IPv6 ankündigt.
        f"ra-param={s.lan_interface},high,60,1800",
        f"dhcp-range={net.network_address},ra-only,{net.prefixlen},12h",
        f"dhcp-option=option6:dns-server,[{pi_address(net)}]",
    ]


def _run(cmd: list[str]) -> bool:
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _write(path: str, value: str) -> bool:
    try:
        with open(path, "w") as f:
            f.write(value)
        return True
    except OSError as exc:
        print(f"cloudy: konnte {path} nicht setzen: {exc}", file=sys.stderr, flush=True)
        return False


def ensure_lan(s: Settings) -> bool:
    """Pi-Adresse im Heimnetz setzen und Weiterleitung einschalten. Mehrfach aufrufbar."""
    net = prefix(s)
    iface = s.lan_interface
    ok = _write("/proc/sys/net/ipv6/conf/all/forwarding", "1")
    # Mit forwarding=1 würde der Pi sonst keine Router Advertisements mehr annehmen. 2 = trotzdem annehmen
    # (so erkennt das Dashboard eine Fritzbox, die noch IPv6 verteilt).
    _write(f"/proc/sys/net/ipv6/conf/{iface}/accept_ra", "2")
    # Eigene Adresse ohne Ablaufzeit, ohne Duplikat-Prüfung (der Pi ist der einzige, der .::1 benutzt).
    ok &= _run([
        "ip", "-6", "addr", "replace", f"{pi_address(net)}/{net.prefixlen}", "dev", iface,
        "nodad", "preferred_lft", "forever", "valid_lft", "forever",
    ])
    return ok


# --------------------------------------------------------------------------- Geräte-Zuordnung (IPv6 -> IPv4)

_neighbors: dict[str, str] = {}
_neighbors_at = 0.0
_neighbors_lock = threading.Lock()


def parse_neighbors(text: str) -> dict[str, str]:
    """Ausgabe von `ip -6 neigh show`: 'fd12::5 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE' -> {'fd12::5': 'aa:bb:...'}"""
    table: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 5 and "lladdr" in parts:
            try:
                addr = str(ipaddress.IPv6Address(parts[0].split("%")[0]))
            except ValueError:
                continue
            table[addr] = parts[parts.index("lladdr") + 1].lower()
    return table


def _read_neighbors() -> dict[str, str]:
    try:
        out = subprocess.run(["ip", "-6", "neigh", "show"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_neighbors(out)


def to_ipv4(addr: str) -> str | None:
    """Die IPv4-Adresse des Geräts, das hinter einer IPv6-Adresse steckt (über die MAC). IPv4 bleibt unverändert."""
    if ":" not in addr:
        return addr
    global _neighbors, _neighbors_at
    try:
        key = str(ipaddress.IPv6Address(addr.split("%")[0]))
    except ValueError:
        return None
    with _neighbors_lock:
        now = time.time()
        age = now - _neighbors_at
        # Neu einlesen, wenn die Tabelle alt ist oder die Adresse fehlt (dann aber höchstens alle paar Sekunden).
        if age > NEIGHBOR_TTL or (key not in _neighbors and age > 3):
            fresh = _read_neighbors()
            # Alte Einträge behalten (Geräte mit wechselnden Adressen), aber nicht unbegrenzt wachsen lassen.
            merged = {**_neighbors, **fresh}
            _neighbors = merged if len(merged) < 5000 else fresh
            _neighbors_at = now
        mac = _neighbors.get(key)
    return db.ip_for_mac(mac) if mac else None
