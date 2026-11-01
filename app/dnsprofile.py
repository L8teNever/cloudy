"""DNS-Profile: Cloudflare Zero Trust „DNS-Standorte“ als eigene Auswahl neben WARP- und VPN-Profilen.

Ein DNS-Profil (dns1, dns2 …) schickt nur die DNS-Anfragen eines Geräts an einen Cloudflare-Gateway-Standort
(DoH-Adresse wie https://abc123.cloudflare-gateway.com/dns-query). Der übrige Verkehr bleibt normal (direkt über
die Fritzbox) – Cloudflare filtert dann nach den DNS-Richtlinien dieses Standorts.

Ablauf: Gerät -> iptables leitet Port 53 nach MAC auf einen eigenen dnsmasq pro Profil um (Port 5360+n, gleiche
Einstellungen wie der Haupt-dnsmasq: lokale Namen, Weiterleitungen, Seiten-Regeln und Statistik funktionieren
weiter) -> dieser fragt eine kleine DoH-Weiterleitung auf 127.0.0.1 (Port 5380+n) -> HTTPS zu Cloudflare.

Läuft der dnsmasq eines Profils nicht, gibt es für dieses Profil keine Umleitung – DNS fällt nie ganz aus.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from . import db, policy, warp
from .settings import Settings

PROFILES_FILE = "dns_profiles.json"
NAME_RE = re.compile(r"^dns([1-9][0-9]?)$")
MAX_PROFILES = 20
PORT_BASE = 5360  # dnsmasq pro Profil
DOH_PORT_BASE = 5380  # DoH-Weiterleitung pro Profil (nur 127.0.0.1)
CHAIN = "CLOUDY_DNSPROF"
STATUS_MAX_AGE = 90  # Sekunden; der Manager schreibt alle paar Sekunden
UDP_MAX = 1232  # mehr schickt dnsmasq per UDP nicht – größere Antworten gehen über TCP
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


def is_dns(slot: str) -> bool:
    return bool(slot and NAME_RE.match(slot))


def index(name: str) -> int:
    m = NAME_RE.match(name)
    if not m:
        raise ValueError(f"Kein DNS-Profil: {name}")
    return int(m.group(1))


def dnsmasq_port(name: str) -> int:
    return PORT_BASE + index(name)


def doh_port(name: str) -> int:
    return DOH_PORT_BASE + index(name)


# --------------------------------------------------------------------------- Liste der Profile


def _file(data_dir: str) -> str:
    return os.path.join(data_dir, PROFILES_FILE)


def load(data_dir: str) -> list[dict]:
    """[{"name": "dns1", "url": "https://….cloudflare-gateway.com/dns-query"}, …] – nach Nummer sortiert."""
    try:
        with open(_file(data_dir), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for p in raw if isinstance(raw, list) else []:
        name = str((p or {}).get("name", ""))
        if NAME_RE.match(name):
            out.append({"name": name, "url": str(p.get("url") or "")})
    return sorted(out, key=lambda p: index(p["name"]))


def save(data_dir: str, profiles: list[dict]) -> None:
    os.makedirs(data_dir, exist_ok=True)
    tmp = _file(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2)
    os.replace(tmp, _file(data_dir))


def names(data_dir: str) -> list[str]:
    return [p["name"] for p in load(data_dir)]


def get_url(data_dir: str, name: str) -> str:
    return next((p["url"] for p in load(data_dir) if p["name"] == name), "")


def add(data_dir: str) -> str:
    profiles = load(data_dir)
    used = {index(p["name"]) for p in profiles}
    n = next((i for i in range(1, MAX_PROFILES + 1) if i not in used), None)
    if n is None:
        raise ValueError(f"Maximal {MAX_PROFILES} DNS-Profile")
    name = f"dns{n}"
    save(data_dir, [*profiles, {"name": name, "url": ""}])
    return name


def remove(data_dir: str, name: str) -> None:
    profiles = load(data_dir)
    if name not in {p["name"] for p in profiles}:
        raise ValueError("Unbekanntes DNS-Profil")
    save(data_dir, [p for p in profiles if p["name"] != name])


def set_url(data_dir: str, name: str, url: str) -> None:
    profiles = load(data_dir)
    if name not in {p["name"] for p in profiles}:
        raise ValueError("Unbekanntes DNS-Profil")
    save(data_dir, [{**p, "url": url} if p["name"] == name else p for p in profiles])


def normalize_url(text: str) -> str:
    """DoH-Adresse aus dem Cloudflare-Dashboard prüfen. Erlaubt auch nur den Hostnamen oder nur die Standort-ID:
    "abc123" -> https://abc123.cloudflare-gateway.com/dns-query. Leer -> "" (Profil ausgeschaltet)."""
    text = (text or "").strip()
    if not text:
        return ""
    if "://" not in text:
        host, _, path = text.partition("/")
        if "." not in host:
            host = f"{host}.cloudflare-gateway.com"
        text = f"https://{host}/{path or 'dns-query'}"
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not HOST_RE.match(host) or parts.username or parts.password:
        raise ValueError("Bitte die DoH-Adresse des Standorts eintragen (https://….cloudflare-gateway.com/dns-query)")
    port = f":{parts.port}" if parts.port else ""
    path = parts.path or "/dns-query"
    return f"https://{host}{port}{path}" + (f"?{parts.query}" if parts.query else "")


# --------------------------------------------------------------------------- Status (Datei, wie bei WARP)


def _status_path(s: Settings, name: str) -> str:
    return os.path.join(s.data_dir, "dns", name, "status")


def write_status(s: Settings, name: str, text: str) -> None:
    path = _status_path(s, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")


def read_status(s: Settings, name: str, now: float | None = None) -> warp.SlotStatus:
    now = now if now is not None else time.time()
    path = _status_path(s, name)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.readline().strip()
        age = now - os.path.getmtime(path)
    except FileNotFoundError:
        return warp.SlotStatus(False, "startet …")
    if age > STATUS_MAX_AGE:
        return warp.SlotStatus(False, "läuft nicht – cloudy neu starten?")
    if text == "Connected":
        return warp.SlotStatus(True, "verbunden")
    return warp.SlotStatus(False, text or "unbekannt")


# --------------------------------------------------------------------------- dnsmasq pro Profil


def build_config(main_config: str, name: str) -> str:
    """Wie der Haupt-dnsmasq (ohne DHCP/RA), eigener Port, statt der normalen Upstream-Server nur Cloudflare."""
    skip = ("dhcp-", "enable-ra", "ra-param")
    lines = []
    for line in main_config.splitlines():
        if not line or line.startswith(skip):
            continue
        if line.startswith("server=") and not line.startswith("server=/"):
            continue  # normaler Upstream (z. B. 1.1.1.1) – ersetzt durch die DoH-Weiterleitung
        lines.append(line)
    lines += [f"port={dnsmasq_port(name)}", "pid-file=", f"server=127.0.0.1#{doh_port(name)}"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- Umleitung (iptables)


def targets(
    assignments: dict[str, str], default_slot: str, allow: dict[str, set[str]], ready: set[str]
) -> tuple[dict[str, str], list[str], str]:
    """Wer geht über welches DNS-Profil?

    Rückgabe: (MAC -> Profil für Geräte mit DNS-Profil, MACs mit anderem eigenem Profil, Standard-DNS-Profil oder "").
    Nur Profile aus `ready` (dnsmasq läuft), nur freigegebene Geräte.
    """
    per_mac: dict[str, str] = {}
    others: list[str] = []
    for mac, slot in assignments.items():
        if not warp.MAC_RE.match(mac):
            continue
        if is_dns(slot) and warp.may_use(mac, slot, allow):
            if slot in ready:
                per_mac[mac] = slot
            else:
                others.append(mac)  # Profil läuft nicht -> normales DNS, nicht das Standard-DNS-Profil
        elif slot and warp.may_use(mac, slot, allow):
            others.append(mac)
    default = default_slot if is_dns(default_slot) and default_slot in ready else ""
    return per_mac, sorted(others), default


def rules(per_mac: dict[str, str], others: list[str], default: str, allow: dict[str, set[str]]) -> list[list[str]]:
    """Inhalt der Kette (sie bekommt nur DNS-Pakete aus dem Heimnetz)."""
    out: list[list[str]] = []
    for mac in sorted(per_mac):
        out.append(["-m", "mac", "--mac-source", mac, "-j", "REDIRECT", "--to-ports", str(dnsmasq_port(per_mac[mac]))])
    if default:
        for mac in others:
            out.append(["-m", "mac", "--mac-source", mac, "-j", "RETURN"])
        if default in allow:
            for mac in sorted(allow[default] - set(per_mac) - set(others)):
                out.append(["-m", "mac", "--mac-source", mac, "-j", "REDIRECT", "--to-ports", str(dnsmasq_port(default))])
        else:
            out.append(["-j", "REDIRECT", "--to-ports", str(dnsmasq_port(default))])
    return out


def _run(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def _first_in_prerouting(tool: str) -> bool:
    """Steht unsere Kette ganz vorn (vor Werbeblocker und DNS-Force)? Sonst würden die zuerst umleiten."""
    try:
        out = subprocess.run([tool, "-t", "nat", "-S", "PREROUTING"], capture_output=True, text=True).stdout
    except OSError:
        return True
    first = next((ln for ln in out.splitlines() if ln.startswith("-A PREROUTING")), "")
    return f"-j {CHAIN}" in first


def apply_rules(s: Settings, chain_rules: list[list[str]]) -> None:
    for tool in ("iptables", "ip6tables"):
        if tool == "ip6tables" and not s.ipv6_enabled:
            continue
        _run([tool, "-t", "nat", "-N", CHAIN])
        _run([tool, "-t", "nat", "-F", CHAIN])
        for rule in chain_rules:
            _run([tool, "-t", "nat", "-A", CHAIN, *rule])
        ensure_jump(s, tool)


def ensure_jump(s: Settings, tool: str = "iptables") -> None:
    if _first_in_prerouting(tool):
        return
    for proto in ("tcp", "udp"):
        match = ["-i", s.lan_interface, "-p", proto, "--dport", "53", "-j", CHAIN]
        while _run([tool, "-t", "nat", "-D", "PREROUTING", *match]):
            pass
        _run([tool, "-t", "nat", "-I", "PREROUTING", "1", *match])


def clear(s: Settings) -> None:
    for tool in ("iptables", "ip6tables"):
        _run([tool, "-t", "nat", "-F", CHAIN])


# --------------------------------------------------------------------------- DoH-Weiterleitung


def _truncated(query: bytes, response: bytes) -> bytes:
    """Zu große Antwort für UDP: nur Kopf + Frage mit TC-Bit, dnsmasq fragt dann per TCP nach."""
    if len(query) < 12 or len(response) < 12:
        return response[:UDP_MAX]
    flags = struct.unpack("!H", response[2:4])[0] | 0x0200
    qdcount = struct.unpack("!H", query[4:6])[0]
    return response[:2] + struct.pack("!HHHHH", flags, qdcount, 0, 0, 0) + query[12:]


class DohForwarder:
    """Nimmt DNS auf 127.0.0.1:<port> (UDP und TCP) an und fragt per DNS-over-HTTPS (RFC 8484, POST) weiter."""

    def __init__(self, url: str, port: int, timeout: float = 5.0):
        self.url = url
        self.port = port
        self.timeout = timeout
        parts = urlsplit(url)
        self.host = parts.hostname or ""
        self.http_port = parts.port or 443
        self.path = parts.path + (f"?{parts.query}" if parts.query else "")
        self.last_ok = 0.0
        self.last_error = ""
        self.last_error_at = 0.0
        self._local = threading.local()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="doh")
        self._udp: socket.socket | None = None
        self._tcp: socketserver.ThreadingTCPServer | None = None
        self._stopping = threading.Event()

    # -- HTTPS
    def _conn(self) -> http.client.HTTPSConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = http.client.HTTPSConnection(self.host, self.http_port, timeout=self.timeout)
            self._local.conn = conn
        return conn

    def resolve(self, query: bytes) -> bytes | None:
        headers = {"Content-Type": "application/dns-message", "Accept": "application/dns-message"}
        for attempt in (1, 2):  # Verbindung kann nach Leerlauf geschlossen sein -> einmal neu verbinden
            conn = self._conn()
            try:
                conn.request("POST", self.path, body=query, headers=headers)
                res = conn.getresponse()
                body = res.read()
                if res.status != 200:
                    raise OSError(f"Cloudflare antwortet mit HTTP {res.status}")
                self.last_ok = time.time()
                return body
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                self._local.conn = None
                if attempt == 2:
                    self.last_error = str(exc) or exc.__class__.__name__
                    self.last_error_at = time.time()
        return None

    # -- Server
    def start(self) -> None:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.bind(("127.0.0.1", self.port))
        self._udp = udp
        threading.Thread(target=self._udp_loop, daemon=True).start()

        fwd = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                sock = self.request
                sock.settimeout(fwd.timeout + 2)
                try:
                    while True:
                        head = sock.recv(2, socket.MSG_WAITALL)
                        if len(head) < 2:
                            return
                        query = sock.recv(struct.unpack("!H", head)[0], socket.MSG_WAITALL)
                        answer = fwd.resolve(query)
                        if answer is None:
                            return
                        sock.sendall(struct.pack("!H", len(answer)) + answer)
                except OSError:
                    return

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._tcp = Server(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._tcp.serve_forever, daemon=True).start()

    def _udp_loop(self) -> None:
        assert self._udp
        while not self._stopping.is_set():
            try:
                query, addr = self._udp.recvfrom(4096)
            except OSError:
                return
            self._pool.submit(self._answer_udp, query, addr)

    def _answer_udp(self, query: bytes, addr) -> None:
        answer = self.resolve(query)
        if answer is None or not self._udp:
            return  # dnsmasq wiederholt bzw. meldet SERVFAIL
        if len(answer) > UDP_MAX:
            answer = _truncated(query, answer)
        try:
            self._udp.sendto(answer, addr)
        except OSError:
            pass

    def probe(self) -> None:
        """Testanfrage (A cloudflare.com), damit der Status auch ohne Verkehr stimmt."""
        q = struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
        q += b"".join(bytes([len(p)]) + p.encode() for p in ("cloudflare", "com")) + b"\0" + struct.pack("!HH", 1, 1)
        self.resolve(q)

    def status(self) -> str:
        if self.last_ok and self.last_ok >= self.last_error_at:
            return "Connected"
        if self.last_error:
            return f"Fehler: {self.last_error}"[:120]
        return "verbindet …"

    def stop(self) -> None:
        self._stopping.set()
        if self._udp:
            try:
                self._udp.close()
            except OSError:
                pass
            self._udp = None
        if self._tcp:
            self._tcp.shutdown()
            self._tcp.server_close()
            self._tcp = None
        self._pool.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------- Laufzeit


class _Instance:
    def __init__(self, name: str, url: str, fwd: DohForwarder, proc: subprocess.Popen | None):
        self.name, self.url, self.fwd, self.proc = name, url, fwd, proc

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)


class DnsProfiles:
    """Startet pro eingerichtetem DNS-Profil DoH-Weiterleitung + dnsmasq und hält die Umleitung aktuell."""

    def __init__(self, s: Settings, main_config: str, feed, interval: float = 5.0):
        self.s = s
        self.main_config = main_config
        self.feed = feed  # fn(name, line): Log-Zeilen für die Statistik
        self.interval = interval
        self.instances: dict[str, _Instance] = {}
        self._applied: list[list[str]] | None = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._ticks = 0

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 – DNS-Profile dürfen cloudy nie stoppen
                print(f"cloudy: Fehler bei DNS-Profilen: {exc}", file=sys.stderr, flush=True)
            if self._stopping.wait(self.interval):
                return

    def _conf_path(self, name: str) -> str:
        return os.path.join(self.s.data_dir, f"dnsmasq-{name}.conf")

    def _start(self, name: str, url: str) -> _Instance:
        fwd = DohForwarder(url, doh_port(name))
        fwd.start()
        with open(self._conf_path(name), "w", encoding="utf-8") as f:
            f.write(build_config(self.main_config, name))
        proc = None
        try:
            proc = subprocess.Popen(
                ["dnsmasq", "--keep-in-foreground", f"--conf-file={self._conf_path(name)}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1,
            )
            threading.Thread(target=self._read_log, args=(name, proc), daemon=True).start()
        except OSError as exc:
            print(f"cloudy: DNS-Profil {name}: dnsmasq startet nicht: {exc}", file=sys.stderr, flush=True)
        print(f"cloudy: DNS-Profil {name} an ({urlsplit(url).hostname}, Port {dnsmasq_port(name)})", flush=True)
        threading.Thread(target=fwd.probe, daemon=True).start()
        return _Instance(name, url, fwd, proc)

    def _read_log(self, name: str, proc: subprocess.Popen) -> None:
        assert proc.stderr
        for line in proc.stderr:
            self.feed(name, line)

    def _stop(self, inst: _Instance) -> None:
        inst.fwd.stop()
        if inst.proc and inst.proc.poll() is None:
            inst.proc.terminate()
            try:
                inst.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                inst.proc.kill()

    def sync(self) -> None:
        """Instanzen an die gespeicherten Profile anpassen (neu, geändert, gelöscht, abgestürzt)."""
        wanted = {p["name"]: p["url"] for p in load(self.s.data_dir) if p["url"]}
        with self._lock:
            for name, inst in list(self.instances.items()):
                if wanted.get(name) != inst.url or not inst.running:
                    self._stop(inst)
                    del self.instances[name]
            for name, url in wanted.items():
                if name not in self.instances and not self._stopping.is_set():
                    try:
                        self.instances[name] = self._start(name, url)
                    except OSError as exc:
                        print(f"cloudy: DNS-Profil {name} nicht gestartet: {exc}", file=sys.stderr, flush=True)

    def restart(self, main_config: str | None = None) -> None:
        """Nach Änderungen an der Haupt-Konfiguration (z. B. Weiterleitungen) alle dnsmasq neu starten."""
        if main_config is not None:
            self.main_config = main_config
        with self._lock:
            for inst in self.instances.values():
                self._stop(inst)
            self.instances.clear()
        self.sync()

    def reload_hosts(self) -> None:
        for inst in list(self.instances.values()):
            if inst.running:
                try:
                    inst.proc.send_signal(1)  # SIGHUP
                except (OSError, ValueError):
                    pass

    def tick(self) -> None:
        self.sync()
        self._ticks += 1
        for p in load(self.s.data_dir):
            inst = self.instances.get(p["name"])
            if not p["url"]:
                write_status(self.s, p["name"], "nicht eingerichtet")
            elif inst is None or not inst.running:
                write_status(self.s, p["name"], "dnsmasq läuft nicht")
            else:
                if self._ticks % 12 == 0 and time.time() - inst.fwd.last_ok > 50:
                    threading.Thread(target=inst.fwd.probe, daemon=True).start()
                write_status(self.s, p["name"], inst.fwd.status())
        ready = {n for n, inst in self.instances.items() if inst.running}
        names_all = names(self.s.data_dir)
        allow = warp.slot_allowlists(names_all) if names_all else {}
        per_mac, others, default = targets(policy.assignments(), db.get_setting(warp.DEFAULT_KEY), allow, ready)
        chain_rules = rules(per_mac, others, default, allow)
        if chain_rules != self._applied:
            apply_rules(self.s, chain_rules)
            self._applied = chain_rules
        elif chain_rules:
            ensure_jump(self.s)  # Werbeblocker o. Ä. hat sich davor gesetzt

    def stop(self) -> None:
        self._stopping.set()
        clear(self.s)
        with self._lock:
            for inst in self.instances.values():
                self._stop(inst)
            self.instances.clear()
