"""Startet dnsmasq (DNS + optional DHCP), liest dessen Log mit und schreibt die Anfragen in die Datenbank."""

import os
import re
import subprocess
import sys
import threading
import time

from . import db
from .settings import Settings

QUERY_RE = re.compile(r"query\[(?P<qtype>[A-Za-z0-9]+)\] (?P<domain>\S+) from (?P<ip>\S+)")
IGNORED_SUFFIXES = (".in-addr.arpa", ".ip6.arpa", ".lan", ".local", ".home.arpa")
IGNORED_IPS = {"127.0.0.1", "::1"}
# Diese Log-Zeilen kommen bei jeder Anfrage und würden die Container-Logs fluten.
NOISY = (" query[", " forwarded ", " reply ", " cached ", " config ", " validation ")

# Schaltet "sicheres DNS" in Firefox und iCloud Private Relay im Heimnetz ab,
# damit diese Geräte normal über den Pi fragen.
BYPASS_DOMAINS = ("use-application-dns.net", "mask.icloud.com", "mask-h2.icloud.com")

HOSTS_HEADER = """\
# Feste IP-Adressen für einzelne Geräte (wie "immer die gleiche IP" in der Fritzbox).
# Eine Zeile pro Gerät:  MAC-Adresse,IP-Adresse,Name
# Beispiel:
# aa:bb:cc:dd:ee:ff,192.168.178.50,drucker
#
# Im Gateway-Modus: "set:direkt" -> Gerät geht NICHT über den Pi, sondern direkt zur Fritzbox
# (nötig für Geräte mit Portfreigabe, z. B. NAS):
# aa:bb:cc:dd:ee:01,set:direkt,192.168.178.60,nas
"""

LOCAL_NAMES_HEADER = """\
# Eigene Namen fürs Heimnetz. Eine Zeile pro Name:  IP-Adresse  Name
# Beispiel:
# 192.168.178.60  nas.lan
# 192.168.178.50  drucker.lan
# Nach dem Ändern: cloudy restart
"""


def parse_query(line: str) -> tuple[str, str, str] | None:
    """Gibt (qtype, domain, ip) zurück oder None, wenn die Zeile keine interessante Anfrage ist."""
    m = QUERY_RE.search(line)
    if not m:
        return None
    qtype = m.group("qtype").upper()
    domain = m.group("domain").lower().rstrip(".")
    ip = m.group("ip")
    if qtype == "PTR" or ip in IGNORED_IPS or not domain or "." not in domain:
        return None
    if domain.endswith(IGNORED_SUFFIXES):
        return None
    return qtype, domain, ip


def build_config(s: Settings) -> str:
    lines = [
        f"interface={s.lan_interface}",
        "bind-dynamic",
        "no-resolv",
        "no-hosts",
        "user=root",
        "domain-needed",
        "bogus-priv",
        "cache-size=10000",
        # "extra": jede Zeile mit Anfrage-Nummer, damit Antwort-IPs der gefragten Domain zugeordnet werden können.
        "log-queries=extra",
        "log-async=50",
        "log-facility=-",
        *(f"server={u}" for u in s.upstream_dns),
        f"server=/fritz.box/{s.router_ip}",
        *(f"local=/{d}/" for d in BYPASS_DOMAINS),
        "domain=lan",
        "local=/lan/",
        f"addn-hosts={s.local_names_path}",
    ]
    if s.local_name:
        # Name des Dashboards im Heimnetz, z. B. http://cloudy.lan
        lines.append(f"address=/{s.local_name}/{s.pi_ip}")
    if s.gateway_enabled:
        # Nur IPv4-Adressen ausliefern: IPv6 würde direkt über die Fritzbox und am Pi vorbei laufen.
        lines.append("filter-AAAA")
    if s.dhcp_enabled:
        if s.gateway_enabled:
            # Geräte mit "set:direkt" in dhcp-hosts.conf (z. B. mit Portfreigabe) gehen weiter direkt zur Fritzbox.
            router = [
                f"dhcp-option=tag:!direkt,option:router,{s.pi_ip}",
                f"dhcp-option=tag:direkt,option:router,{s.router_ip}",
            ]
        else:
            router = [f"dhcp-option=option:router,{s.router_ip}"]
        lines += [
            f"dhcp-range={s.dhcp_start},{s.dhcp_end},{s.netmask},{s.dhcp_lease}",
            *router,
            f"dhcp-option=option:dns-server,{s.pi_ip}",
            "dhcp-authoritative",
            f"dhcp-leasefile={s.leases_path}",
            f"dhcp-hostsfile={s.hosts_path}",
        ]
    return "\n".join(lines) + "\n"


def read_leases(path: str) -> list[tuple[str, str, str | None]]:
    """dnsmasq-Lease-Datei: '<ablauf> <mac> <ip> <hostname> <client-id>' -> [(ip, mac, hostname)]"""
    result = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 4 or ":" not in parts[1] or parts[0] == "duid":
                    continue
                hostname = None if parts[3] == "*" else parts[3]
                result.append((parts[2], parts[1].lower(), hostname))
    except FileNotFoundError:
        pass
    return result


class Collector:
    """Sammelt Anfragen im Speicher und schreibt sie gebündelt (schont die SD-Karte)."""

    def __init__(self, dedup_seconds: int = 60, domain_map=None):
        self.dedup_seconds = dedup_seconds
        self.domain_map = domain_map
        self._pending: list[tuple[int, str, str, str]] = []
        self._last: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def feed(self, line: str, now: int | None = None) -> bool:
        """Gibt True zurück, wenn die Zeile eine DNS-Anfrage war."""
        if self.domain_map is not None:
            self.domain_map.feed(line)
        parsed = parse_query(line)
        if parsed is None:
            return " query[" in line
        qtype, domain, ip = parsed
        now = now if now is not None else int(time.time())
        key = (ip, domain)
        with self._lock:
            # Ein Gerät fragt eine Domain oft mehrfach hintereinander (A, AAAA, HTTPS ...) -> einmal zählen.
            last = self._last.get(key)
            if last is not None and now - last < self.dedup_seconds:
                return True
            self._last[key] = now
            self._pending.append((now, ip, domain, qtype))
        return True

    def flush(self) -> None:
        with self._lock:
            rows, self._pending = self._pending, []
            if len(self._last) > 50000:
                cutoff = int(time.time()) - self.dedup_seconds
                self._last = {k: v for k, v in self._last.items() if v >= cutoff}
        db.add_queries(rows)


class DnsmasqRunner:
    def __init__(self, settings: Settings, collector: Collector, flush_interval: float = 5.0):
        self.settings = settings
        self.collector = collector
        self.flush_interval = flush_interval
        self.proc: subprocess.Popen | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        s = self.settings
        os.makedirs(s.data_dir, exist_ok=True)
        if not os.path.exists(s.hosts_path):
            with open(s.hosts_path, "w", encoding="utf-8") as f:
                f.write(HOSTS_HEADER)
        if not os.path.exists(s.local_names_path):
            with open(s.local_names_path, "w", encoding="utf-8") as f:
                f.write(LOCAL_NAMES_HEADER)
        with open(s.conf_path, "w", encoding="utf-8") as f:
            f.write(build_config(s))
        print(
            f"cloudy: DNS an, DHCP {'an' if s.dhcp_enabled else 'aus'}, "
            f"Gateway {'an' if s.gateway_enabled else 'aus'}, Upstream {', '.join(s.upstream_dns)}",
            flush=True,
        )
        self.proc = subprocess.Popen(
            ["dnsmasq", "--keep-in-foreground", f"--conf-file={s.conf_path}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read_log, daemon=True).start()
        threading.Thread(target=self._flush_loop, daemon=True).start()

    def _read_log(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            if not self.collector.feed(line) and not any(n in line for n in NOISY):
                print(line, end="", flush=True)
        code = self.proc.wait()
        if not self._stopping.is_set():
            # Ohne dnsmasq hat das Heimnetz kein DNS/DHCP -> Container beenden, Docker startet ihn neu.
            print(f"cloudy: dnsmasq beendet (Code {code}), Container wird neu gestartet", file=sys.stderr, flush=True)
            os._exit(1)

    def _flush_loop(self) -> None:
        while not self._stopping.wait(self.flush_interval):
            self._flush_once()

    def _flush_once(self) -> None:
        try:
            self.collector.flush()
            if self.settings.dhcp_enabled:
                db.upsert_devices(read_leases(self.settings.leases_path))
        except Exception as exc:  # noqa: BLE001 – Logging darf den Dienst nie stoppen
            print(f"cloudy: Fehler beim Speichern: {exc}", file=sys.stderr, flush=True)

    def stop(self) -> None:
        self._stopping.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._flush_once()


if __name__ == "__main__":
    # Gibt die erzeugte dnsmasq-Konfiguration aus (für Tests: dnsmasq --test).
    print(build_config(Settings.from_env()), end="")
