"""Feste IP-Adressen: Ein Gerät (MAC) bekommt per DHCP immer dieselbe Adresse.

Einzeln pro Gerät oder für alle: Ist „alle“ an, wird die aktuelle Adresse jedes Geräts mit cloudy-DHCP festgehalten.
cloudy schreibt die Einträge in eine eigene Datei; dhcp-hosts.conf bleibt für Einträge von Hand (z. B. set:direkt).
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import threading

from . import db

MAP_KEY = "feste_ips"  # JSON {mac: ip}
ALL_KEY = "feste_ips_alle"  # "1" = jedes Gerät behält seine Adresse
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
HEADER = "# cloudy: feste IP-Adressen (automatisch erzeugt, nicht von Hand ändern - dafür ist dhcp-hosts.conf da)\n"
_lock = threading.RLock()


def path(s) -> str:
    return os.path.join(s.data_dir, "dhcp-feste-ips.conf")


def mapping() -> dict[str, str]:
    try:
        raw = json.loads(db.get_setting(MAP_KEY) or "{}")
    except ValueError:
        return {}
    return {str(m): str(i) for m, i in raw.items()} if isinstance(raw, dict) else {}


def _save(data: dict[str, str]) -> None:
    db.set_setting(MAP_KEY, json.dumps(data, sort_keys=True) if data else "")


def all_on() -> bool:
    return db.get_setting(ALL_KEY) == "1"


def write(s) -> bool:
    """Datei für dnsmasq schreiben. True, wenn sich etwas geändert hat."""
    text = HEADER + "".join(f"{m},{i}\n" for m, i in sorted(mapping().items()))
    target = path(s)
    try:
        with open(target, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    os.makedirs(s.data_dir, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, target)
    return True


def _hand_entries(s) -> tuple[set[str], set[str]]:
    """MACs und IPs aus der von Hand gepflegten dhcp-hosts.conf."""
    macs, ips = set(), set()
    try:
        with open(s.hosts_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return macs, ips
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        for part in (p.strip().lower() for p in line.split(",")):
            if MAC_RE.match(part):
                macs.add(part)
            else:
                try:
                    ips.add(str(ipaddress.IPv4Address(part)))
                except ValueError:
                    pass
    return macs, ips


def _bad_address(s, ip: ipaddress.IPv4Address) -> str:
    net = s.lan_network
    if ip not in net:
        return f"Die Adresse liegt nicht im Heimnetz ({net})."
    if ip in (net.network_address, net.broadcast_address):
        return "Diese Adresse ist im Netz reserviert."
    if str(ip) in (s.pi_ip, s.router_ip):
        return "Das ist die Adresse vom Pi oder von der FRITZ!Box."
    return ""


def set_ip(s, mac: str, ip_text: str) -> str:
    """Feste Adresse für ein Gerät. Rückgabe: Fehlertext oder ""."""
    from .dnsmasq import read_leases

    mac = (mac or "").strip().lower()
    if not s.dhcp_enabled:
        return "Feste Adressen gehen nur, wenn cloudy das DHCP macht."
    if not MAC_RE.match(mac):
        return "Das Gerät hat keine MAC-Adresse."
    try:
        ip = ipaddress.IPv4Address((ip_text or "").strip())
    except ValueError:
        return "Das ist keine gültige IP-Adresse."
    err = _bad_address(s, ip)
    if err:
        return err
    text = str(ip)
    hand_macs, hand_ips = _hand_entries(s)
    if mac in hand_macs:
        return "Dieses Gerät steht von Hand in dhcp-hosts.conf."
    if text in hand_ips:
        return "Diese Adresse steht von Hand in dhcp-hosts.conf."
    with _lock:
        data = mapping()
        if any(i == text and m != mac for m, i in data.items()):
            return "Diese Adresse ist schon für ein anderes Gerät festgelegt."
        if any(i == text and m != mac for i, m, _ in read_leases(s.leases_path)):
            return "Diese Adresse benutzt gerade ein anderes Gerät."
        if any(d.get("fritz_active") and d.get("ip") == text and d.get("mac") != mac for d in db.devices()):
            return "Diese Adresse benutzt gerade ein anderes Gerät."
        data[mac] = text
        _save(data)
    return ""


def remove(mac: str) -> None:
    with _lock:
        data = mapping()
        if data.pop((mac or "").strip().lower(), None):
            _save(data)


def clear() -> None:
    """Alle festen Adressen aufheben und nichts mehr automatisch festhalten."""
    with _lock:
        _save({})
        db.set_setting(ALL_KEY, "")


def set_all(on: bool) -> None:
    db.set_setting(ALL_KEY, "1" if on else "")


def _pool_free_limit(s, data: dict[str, str]) -> int:
    """Wie viele Adressen im DHCP-Bereich noch festgehalten werden dürfen (der Rest bleibt für neue Geräte)."""
    start, end = int(ipaddress.IPv4Address(s.dhcp_start)), int(ipaddress.IPv4Address(s.dhcp_end))
    size = end - start + 1
    used = sum(1 for i in data.values() if start <= int(ipaddress.IPv4Address(i)) <= end)
    return size - max(8, size // 10) - used


def pin_leases(s, leases: list[tuple[str, str, str | None]]) -> bool:
    """„Alle“ an: aktuelle Adressen festhalten. True, wenn dnsmasq neu laden muss."""
    if not s.dhcp_enabled:
        return False
    changed = False
    with _lock:
        if all_on():
            data = mapping()
            taken = set(data.values())
            hand_macs, hand_ips = _hand_entries(s)
            room = _pool_free_limit(s, data)
            for ip_text, mac, _ in leases:
                if room <= 0:
                    break
                if mac in data or mac in hand_macs or ip_text in taken or ip_text in hand_ips or not MAC_RE.match(mac):
                    continue
                try:
                    ip = ipaddress.IPv4Address(ip_text)
                except ValueError:
                    continue
                if _bad_address(s, ip):
                    continue
                data[mac] = ip_text
                taken.add(ip_text)
                room -= 1
                changed = True
            if changed:
                _save(data)
        return write(s) or changed
