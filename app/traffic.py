"""Gateway-Modus: Pi leitet den Verkehr der Geräte weiter (NAT) und misst ihn pro Gerät und Domain."""

import ipaddress
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass

from . import db
from .settings import Settings

NAT_CHAIN = "CLOUDY-NAT"
FWD_CHAIN = "CLOUDY-FWD"

# Mit log-queries=extra: "<serial> <client>/<port> query[A] name from client"
QUERY_SERIAL_RE = re.compile(r" (?P<serial>\d+) (?P<client>[0-9A-Fa-f:.]+)/\d+ query\[\w+\] (?P<domain>\S+) from ")
# "<serial> <client>/<port> reply|cached name is 1.2.3.4"
REPLY_RE = re.compile(
    r" (?P<serial>\d+) (?P<client>[0-9A-Fa-f:.]+)/\d+ (?:reply|cached) (?P<name>\S+) is (?P<addr>\S+)"
)


def _bounded_set(d: OrderedDict, key, value, limit: int) -> None:
    d[key] = value
    d.move_to_end(key)
    while len(d) > limit:
        d.popitem(last=False)


class DomainMap:
    """Merkt sich aus den DNS-Antworten, welche Ziel-IP zu welcher Domain gehört (pro Gerät)."""

    def __init__(self, limit: int = 100_000):
        self.limit = limit
        self._questions: OrderedDict[str, str] = OrderedDict()
        self._by_client: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._by_addr: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def feed(self, line: str) -> None:
        m = QUERY_SERIAL_RE.search(line)
        if m:
            with self._lock:
                _bounded_set(self._questions, m.group("serial"), m.group("domain").lower().rstrip("."), 10_000)
            return
        m = REPLY_RE.search(line)
        if not m:
            return
        try:
            addr = str(ipaddress.ip_address(m.group("addr")))
        except ValueError:  # <CNAME>, NXDOMAIN, NODATA ...
            return
        with self._lock:
            # Gefragt wurde z. B. www.youtube.com, die IP gehört zu youtube-ui.l.google.com -> die Frage zählt.
            domain = self._questions.get(m.group("serial")) or m.group("name").lower().rstrip(".")
            _bounded_set(self._by_client, (m.group("client"), addr), domain, self.limit)
            _bounded_set(self._by_addr, addr, domain, self.limit)

    def lookup(self, client: str, addr: str) -> str | None:
        with self._lock:
            return self._by_client.get((client, addr)) or self._by_addr.get(addr)


@dataclass(frozen=True)
class Conn:
    id: str
    proto: str
    src: str
    dst: str
    dport: str | None
    bytes_up: int
    bytes_down: int


def parse_conntrack(line: str) -> Conn | None:
    """Eine Zeile aus `conntrack -L/-E -o extended,id`. Erstes src/dst/bytes = Gerät -> Internet, zweites = Antwort."""
    orig: dict[str, str] = {}
    reply: dict[str, str] = {}
    conn_id = None
    proto = None
    for tok in line.split():
        if tok in ("tcp", "udp", "icmp") and proto is None:
            proto = tok
        key, sep, value = tok.partition("=")
        if not sep:
            continue
        if key == "id":
            conn_id = value
        elif key in ("src", "dst", "dport", "bytes"):
            (reply if key in orig else orig).setdefault(key, value)
    if not conn_id or "src" not in orig or "dst" not in orig:
        return None
    return Conn(
        id=conn_id,
        proto=proto or "?",
        src=orig["src"],
        dst=orig["dst"],
        dport=orig.get("dport"),
        bytes_up=int(orig.get("bytes", 0)),
        bytes_down=int(reply.get("bytes", 0)),
    )


class TrafficMeter:
    def __init__(self, settings: Settings, domains: DomainMap, stale_seconds: int = 600):
        self.lan = settings.lan_network
        self.pi_ip = settings.pi_ip
        self.domains = domains
        self.stale_seconds = stale_seconds
        self._last: dict[str, tuple[int, int, float]] = {}  # id -> (up, down, zuletzt gesehen)
        self._pending: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
        self._lock = threading.Lock()

    def _relevant(self, c: Conn) -> bool:
        try:
            src, dst = ipaddress.ip_address(c.src), ipaddress.ip_address(c.dst)
        except ValueError:
            return False
        return src in self.lan and dst not in self.lan and c.src != self.pi_ip and not dst.is_multicast

    def observe(self, c: Conn, final: bool = False, now: float | None = None) -> None:
        if not self._relevant(c):
            return
        now = now if now is not None else time.time()
        with self._lock:
            prev = self._last.pop(c.id, None) if final else self._last.get(c.id)
            is_new = prev is None
            up = max(0, c.bytes_up - (prev[0] if prev else 0))
            down = max(0, c.bytes_down - (prev[1] if prev else 0))
            if not final:
                self._last[c.id] = (c.bytes_up, c.bytes_down, now)
            if not (up or down or is_new):
                return
            label = self.domains.lookup(c.src, c.dst) or c.dst
            entry = self._pending[(c.src, label)]
            entry[0] += up
            entry[1] += down
            entry[2] += 1 if is_new else 0
            entry[3] = int(now)

    def flush(self) -> None:
        now = time.time()
        with self._lock:
            pending, self._pending = self._pending, defaultdict(lambda: [0, 0, 0, 0])
            # Verbindungen, deren Ende wir verpasst haben, irgendwann vergessen.
            self._last = {k: v for k, v in self._last.items() if now - v[2] < self.stale_seconds}
        db.add_traffic([(ip, label, up, down, conns, ts) for (ip, label), (up, down, conns, ts) in pending.items()])


def _run(cmd: list[str]) -> bool:
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _write_sysctl(path: str, value: str) -> bool:
    try:
        with open(path, "w") as f:
            f.write(value)
        return True
    except OSError as exc:
        print(f"cloudy: konnte {path} nicht setzen: {exc}", file=sys.stderr, flush=True)
        return False


def gateway_rules(s: Settings) -> list[list[str]]:
    lan = str(s.lan_network)
    return [
        ["iptables", "-t", "nat", "-A", NAT_CHAIN, "-s", lan, "!", "-d", lan, "-o", s.lan_interface, "-j", "MASQUERADE"],
        ["iptables", "-A", FWD_CHAIN, "-i", s.lan_interface, "-o", s.lan_interface, "-j", "ACCEPT"],
    ]


def setup_gateway(s: Settings) -> bool:
    """Weiterleitung + NAT einschalten. Gibt False zurück, wenn es nicht geklappt hat."""
    ok = _write_sysctl("/proc/sys/net/ipv4/ip_forward", "1")
    # Der Pi soll die IPv6-Ankündigungen der Fritzbox weiter sehen (auch wenn Docker IPv6-Forwarding anschaltet),
    # damit das Dashboard warnen kann, wenn IPv6 am Pi/VPN vorbeiläuft.
    _write_sysctl(f"/proc/sys/net/ipv6/conf/{s.lan_interface}/accept_ra", "2")
    # Sonst sagt der Pi den Geräten "geh direkt zur Fritzbox" (ICMP-Redirect) und der Verkehr läuft vorbei.
    for name in ("all", "default", s.lan_interface):
        _write_sysctl(f"/proc/sys/net/ipv4/conf/{name}/send_redirects", "0")

    for table, chain in (("nat", NAT_CHAIN), ("filter", FWD_CHAIN)):
        _run(["iptables", "-t", table, "-N", chain])
        _run(["ip6tables", "-t", table, "-N", chain])
        ok &= _run(["iptables", "-t", table, "-F", chain])
        _run(["ip6tables", "-t", table, "-F", chain])
    for rule in gateway_rules(s):
        ok &= _run(rule)
    _run(["ip6tables", "-A", FWD_CHAIN, "-i", s.lan_interface, "-o", s.lan_interface, "-j", "ACCEPT"])

    # Docker setzt FORWARD auf DROP; DOCKER-USER wird vorher durchlaufen.
    jumps = [("nat", "POSTROUTING", NAT_CHAIN)]
    fwd_parent = "DOCKER-USER" if _run(["iptables", "-L", "DOCKER-USER", "-n"]) else "FORWARD"
    jumps.append(("filter", fwd_parent, FWD_CHAIN))
    for table, parent, chain in jumps:
        if not _run(["iptables", "-t", table, "-C", parent, "-j", chain]):
            ok &= _run(["iptables", "-t", table, "-I", parent, "1", "-j", chain])

    fwd_v6 = "DOCKER-USER" if _run(["ip6tables", "-L", "DOCKER-USER", "-n"]) else "FORWARD"
    if not _run(["ip6tables", "-t", "filter", "-C", fwd_v6, "-j", FWD_CHAIN]):
        _run(["ip6tables", "-t", "filter", "-I", fwd_v6, "1", "-j", FWD_CHAIN])

    # Byte-Zähler pro Verbindung (das Modul ist erst nach der NAT-Regel geladen).
    _write_sysctl("/proc/sys/net/netfilter/nf_conntrack_acct", "1")
    return ok

class TrafficRunner:
    def __init__(self, meter: TrafficMeter, poll_seconds: float):
        self.meter = meter
        self.poll_seconds = poll_seconds
        self._stopping = threading.Event()
        self._events: subprocess.Popen | None = None

    def start(self) -> None:
        try:
            self._events = subprocess.Popen(
                ["conntrack", "-E", "-e", "DESTROY", "-o", "extended,id"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            threading.Thread(target=self._read_events, daemon=True).start()
        except OSError as exc:
            print(f"cloudy: conntrack-Events nicht verfügbar: {exc}", file=sys.stderr, flush=True)
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _read_events(self) -> None:
        assert self._events and self._events.stdout
        for line in self._events.stdout:
            c = parse_conntrack(line)
            if c:
                self.meter.observe(c, final=True)

    def _poll_loop(self) -> None:
        while not self._stopping.wait(self.poll_seconds):
            try:
                out = subprocess.run(
                    ["conntrack", "-L", "-o", "extended,id"], capture_output=True, text=True, timeout=30
                ).stdout
                for line in out.splitlines():
                    c = parse_conntrack(line)
                    if c:
                        self.meter.observe(c)
                self.meter.flush()
            except Exception as exc:  # noqa: BLE001 – Messung darf den Dienst nie stoppen
                print(f"cloudy: Fehler bei der Verkehrsmessung: {exc}", file=sys.stderr, flush=True)

    def stop(self) -> None:
        self._stopping.set()
        if self._events and self._events.poll() is None:
            self._events.terminate()
        self.meter.flush()
