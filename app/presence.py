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
_NUD_STATES = {"REACHABLE", "STALE", "DELAY", "PROBE", "FAILED", "INCOMPLETE", "PERMANENT", "NOARP", "NONE"}


def parse_neigh(text: str) -> dict[str, str]:
    """Ausgabe von `ip -4 neigh show` -> {ip: Zustand}. Ohne MAC-Adresse zählt der Eintrag als FAILED."""
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        ip = parts[0]
        if "lladdr" not in parts:
            result[ip] = "FAILED"
            continue
        state = "FAILED"
        for part in reversed(parts):
            if part in _NUD_STATES:
                state = part
                break
        result[ip] = state
    return result


def _ip(*args: str) -> str:
    try:
        return subprocess.run(["ip", *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _flush_ips(ips: list[str], dev: str) -> None:
    if not ips:
        return
    dev_arg = ["dev", dev] if dev else []
    dev_str = f" dev {dev}" if dev else ""
    lines = "".join(f"neigh flush to {ip}{dev_str}\n" for ip in ips)
    try:
        res = subprocess.run(
            ["ip", "-batch", "-"], input=lines, capture_output=True, text=True, timeout=5
        )
        if res.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    for ip in ips:
        _ip("neigh", "flush", "to", ip, *dev_arg)


class PresenceMonitor:
    def __init__(self, settings: Settings, interval: float = 8.0, settle_seconds: float = 1.2):
        self.s = settings
        self.interval = interval
        self.settle = settle_seconds
        self.online: frozenset[str] = frozenset()
        self._stopping = threading.Event()
        self._lock = threading.Lock()

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
        dev_args = ["dev", self.s.lan_interface] if self.s.lan_interface else []
        return parse_neigh(_ip("-4", "neigh", "show", *dev_args))

    def tick(self) -> None:
        if not self._lock.acquire(blocking=False):
            return
        try:
            ips = self._candidates()
            if not ips:
                self.online = frozenset()
                return

            # Alle bekannten Geräte abfragen: vorherige ARP-Einträge im Kernel löschen,
            # damit veraltete Einträge (z. B. Handy hat WLAN ausgemacht) nicht fälschlich
            # als REACHABLE weiterlaufen. Wer jetzt antwortet, ist wirklich da.
            _flush_ips(ips, self.s.lan_interface)

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setblocking(False)
                for ip in ips:
                    try:
                        sock.sendto(b"", (ip, 9))
                    except OSError:
                        pass

            if self.settle > 0:
                time.sleep(self.settle)
            states = self._neigh()
            self.online = frozenset(ip for ip in ips if states.get(ip) in ("REACHABLE", "PERMANENT"))
        finally:
            self._lock.release()

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
