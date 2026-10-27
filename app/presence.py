"""Welche Geräte sind gerade im Netz? Der Pi fragt die bekannten Geräte per ARP ab (klappt auch bei Handys,
die auf Ping nicht antworten) und merkt sich, wer antwortet."""

import ipaddress
import socket
import subprocess
import sys
import threading
import time

from . import db
from .settings import Settings

MAX_DEVICES = 512


def parse_neigh(text: str) -> dict[str, str]:
    """Ausgabe von `ip -4 neigh show` -> {ip: Zustand}. Ohne MAC-Adresse zählt der Eintrag als FAILED."""
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        result[parts[0]] = parts[-1] if "lladdr" in parts else "FAILED"
    return result


def _ip(*args: str) -> str:
    try:
        return subprocess.run(["ip", *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


class PresenceMonitor:
    def __init__(self, settings: Settings, interval: float = 30.0, settle_seconds: float = 2.5):
        self.s = settings
        self.interval = interval
        self.settle = settle_seconds
        self.online: frozenset[str] = frozenset()
        self._stopping = threading.Event()

    def _candidates(self) -> list[str]:
        lan = self.s.lan_network
        skip = {self.s.pi_ip, self.s.router_ip}
        ips = []
        for ip in db.known_ips():
            try:
                if ipaddress.ip_address(ip) in lan and ip not in skip:
                    ips.append(ip)
            except ValueError:
                continue
        return ips[:MAX_DEVICES]

    def _neigh(self) -> dict[str, str]:
        return parse_neigh(_ip("-4", "neigh", "show", "dev", self.s.lan_interface))

    def tick(self) -> None:
        ips = self._candidates()
        online = {ip for ip, state in self._neigh().items() if state == "REACHABLE" and ip in ips}
        unsure = [ip for ip in ips if ip not in online]
        if unsure:
            # Alten Eintrag löschen und neu per ARP fragen: nur wer jetzt antwortet, ist wirklich da.
            for ip in unsure:
                _ip("neigh", "flush", "to", ip, "dev", self.s.lan_interface)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setblocking(False)
                for ip in unsure:
                    try:
                        sock.sendto(b"", (ip, 9))
                    except OSError:
                        pass
            time.sleep(self.settle)
            states = self._neigh()
            online |= {ip for ip in unsure if states.get(ip) == "REACHABLE"}
        self.online = frozenset(online)

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 – die Anzeige darf den Dienst nie stoppen
                print(f"cloudy: Fehler bei der Online-Anzeige: {exc}", file=sys.stderr, flush=True)
            if self._stopping.wait(self.interval):
                return

    def stop(self) -> None:
        self._stopping.set()
