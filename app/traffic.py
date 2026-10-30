"""Gateway-Modus: Pi leitet den Verkehr der Geräte weiter (NAT) und misst ihn pro Gerät und Domain."""

import ipaddress
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass

from . import db, ipv6
from .settings import Settings

NAT_CHAIN = "CLOUDY-NAT"
DNS_CHAIN = "CLOUDY-DNS"
FWD_CHAIN = "CLOUDY-FWD"
ISOLATE_CHAIN = "CLOUDY-ISOLATE"

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
        # IPv6: nur das eigene Heimnetz des Pi (ohne IPV6_ENABLED gibt es keinen IPv6-Verkehr der Geräte über den Pi).
        self.lan6 = ipv6.prefix(settings) if settings.ipv6_enabled else None
        self.pi_ip6 = str(ipv6.pi_address(self.lan6)) if self.lan6 else None
        self._last: dict[str, tuple[int, int, float]] = {}  # id -> (up, down, zuletzt gesehen)
        self._done6: OrderedDict[str, None] = OrderedDict()  # beendete IPv6-Verbindungen (Ereignisse können doppelt kommen)
        self._pending: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
        self._lock = threading.Lock()

    def _relevant(self, c: Conn) -> bool:
        try:
            src, dst = ipaddress.ip_address(c.src), ipaddress.ip_address(c.dst)
        except ValueError:
            return False
        if src.version == 6:
            return (
                self.lan6 is not None
                and src in self.lan6
                and dst not in self.lan6
                and c.src != self.pi_ip6
                and not dst.is_multicast
                and not dst.is_link_local
            )
        return src in self.lan and dst not in self.lan and c.src != self.pi_ip and not dst.is_multicast

    def observe(self, c: Conn, final: bool = False, now: float | None = None) -> None:
        if not self._relevant(c):
            return
        # Geräte werden über ihre IPv4-Adresse geführt; eine IPv6-Verbindung gehört zum selben Gerät.
        owner = ipv6.to_ipv4(c.src) if ":" in c.src else c.src
        if owner is None:
            return
        now = now if now is not None else time.time()
        with self._lock:
            if final and ":" in c.src:
                if c.id in self._done6:
                    return
                _bounded_set(self._done6, c.id, None, 20_000)
            prev = self._last.pop(c.id, None) if final else self._last.get(c.id)
            is_new = prev is None
            up = max(0, c.bytes_up - (prev[0] if prev else 0))
            down = max(0, c.bytes_down - (prev[1] if prev else 0))
            if not final:
                self._last[c.id] = (c.bytes_up, c.bytes_down, now)
            if not (up or down or is_new):
                return
            label = self.domains.lookup(c.src, c.dst) or self.domains.lookup(owner, c.dst) or c.dst
            entry = self._pending[(owner, label)]
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


def dns_force_rules(s: Settings) -> list[list[str]]:
    """Leitet DNS (UDP/TCP 53) von Geräten hinter dem Pi auf dnsmasq um – auch bei fest eingestelltem 8.8.8.8 usw.

    Greift nur für Traffic, der den Pi als Gateway nutzt. Geräte mit set:direkt (Fritzbox als Router)
    und DNS zu einer LAN-Adresse (z. B. Fritzbox) gehen L2 am Pi vorbei und sind davon nicht betroffen.
    """
    iface = s.lan_interface
    pi = s.pi_ip
    return [
        ["iptables", "-t", "nat", "-A", DNS_CHAIN, "-i", iface, "-p", "udp", "--dport", "53", "!", "-d", pi, "-j", "REDIRECT", "--to-ports", "53"],
        ["iptables", "-t", "nat", "-A", DNS_CHAIN, "-i", iface, "-p", "tcp", "--dport", "53", "!", "-d", pi, "-j", "REDIRECT", "--to-ports", "53"],
    ]


def gateway_rules_v6(s: Settings) -> list[list[str]]:
    """IPv6 (nur mit IPV6_ENABLED): Wer keinen Tunnel hat, bekommt sofort "keine Route" und weicht auf IPv4 aus.

    Markierte Pakete (zugeordnetes Gerät) gehen zum WARP-/VPN-Container und verlassen den Pi nie über das Heimnetz.
    """
    net = str(ipv6.prefix(s))
    lan = s.lan_interface
    return [
        ["ip6tables", "-A", FWD_CHAIN, "-i", lan, "-o", lan, "-m", "mark", "--mark", "0x0", "!", "-d", net,
         "-j", "REJECT", "--reject-with", "icmp6-no-route"],
    ]


def dns_force_rules_v6(s: Settings) -> list[list[str]]:
    """Wie dns_force_rules, für DNS über IPv6 (Geräte mit fest eingestelltem IPv6-Resolver)."""
    pi6 = str(ipv6.pi_address(ipv6.prefix(s)))
    return [
        ["ip6tables", "-t", "nat", "-A", DNS_CHAIN, "-i", s.lan_interface, "-p", proto, "--dport", "53",
         "!", "-d", pi6, "-j", "REDIRECT", "--to-ports", "53"]
        for proto in ("udp", "tcp")
    ]


def apply_isolation(s: Settings) -> bool:
    """Isoliert Geräte: Sie dürfen nur ins Internet und zum Pi-DNS, aber nichts im lokalen Heimnetz erreichen."""
    _run(["iptables", "-N", ISOLATE_CHAIN])
    _run(["iptables", "-F", ISOLATE_CHAIN])
    if not _run(["iptables", "-C", FWD_CHAIN, "-j", ISOLATE_CHAIN]):
        _run(["iptables", "-I", FWD_CHAIN, "1", "-j", ISOLATE_CHAIN])

    macs = db.isolated_macs()
    for mac in macs:
        # DNS zum Pi erlauben
        _run(["iptables", "-A", ISOLATE_CHAIN, "-m", "mac", "--mac-source", mac, "-d", s.pi_ip, "-p", "udp", "--dport", "53", "-j", "ACCEPT"])
        _run(["iptables", "-A", ISOLATE_CHAIN, "-m", "mac", "--mac-source", mac, "-d", s.pi_ip, "-p", "tcp", "--dport", "53", "-j", "ACCEPT"])
        # Zugriff auf lokale private Netzwerke verbieten
        _run(["iptables", "-A", ISOLATE_CHAIN, "-m", "mac", "--mac-source", mac, "-d", "10.0.0.0/8", "-j", "DROP"])
        _run(["iptables", "-A", ISOLATE_CHAIN, "-m", "mac", "--mac-source", mac, "-d", "172.16.0.0/12", "-j", "DROP"])
        _run(["iptables", "-A", ISOLATE_CHAIN, "-m", "mac", "--mac-source", mac, "-d", "192.168.0.0/16", "-j", "DROP"])
    return True


def setup_gateway(s: Settings) -> bool:
    """Weiterleitung + NAT + DNS-Force einschalten. Gibt False zurück, wenn es nicht geklappt hat."""
    ok = _write_sysctl("/proc/sys/net/ipv4/ip_forward", "1")
    # Der Pi soll die IPv6-Ankündigungen der Fritzbox weiter sehen (auch wenn Docker IPv6-Forwarding anschaltet),
    # damit das Dashboard warnen kann, wenn IPv6 am Pi/VPN vorbeiläuft.
    _write_sysctl(f"/proc/sys/net/ipv6/conf/{s.lan_interface}/accept_ra", "2")
    # Sonst sagt der Pi den Geräten "geh direkt zur Fritzbox" (ICMP-Redirect) und der Verkehr läuft vorbei.
    for name in ("all", "default", s.lan_interface):
        _write_sysctl(f"/proc/sys/net/ipv4/conf/{name}/send_redirects", "0")

    for table, chain in (("nat", NAT_CHAIN), ("nat", DNS_CHAIN), ("filter", FWD_CHAIN)):
        _run(["iptables", "-t", table, "-N", chain])
        if chain != DNS_CHAIN:
            _run(["ip6tables", "-t", table, "-N", chain])
        ok &= _run(["iptables", "-t", table, "-F", chain])
        if chain != DNS_CHAIN:
            _run(["ip6tables", "-t", table, "-F", chain])
    for rule in gateway_rules(s):
        ok &= _run(rule)
    for rule in dns_force_rules(s):
        ok &= _run(rule)
    if s.ipv6_enabled:
        # IPv6-Fehler dürfen das IPv4-Gateway nie lahmlegen: eigener Rückgabewert, nur eine Meldung.
        if not ipv6.ensure_lan(s):
            print("cloudy: IPv6-Adresse im Heimnetz konnte nicht gesetzt werden (IPV6_ENABLED)", file=sys.stderr, flush=True)
        for rule in gateway_rules_v6(s):
            _run(rule)
        _run(["ip6tables", "-t", "nat", "-N", DNS_CHAIN])
        _run(["ip6tables", "-t", "nat", "-F", DNS_CHAIN])
        for rule in dns_force_rules_v6(s):
            _run(rule)
        if not _run(["ip6tables", "-t", "nat", "-C", "PREROUTING", "-j", DNS_CHAIN]):
            _run(["ip6tables", "-t", "nat", "-I", "PREROUTING", "1", "-j", DNS_CHAIN])
    else:
        # Reste aus einem früheren Lauf mit IPV6_ENABLED=1 entfernen.
        while _run(["ip6tables", "-t", "nat", "-D", "PREROUTING", "-j", DNS_CHAIN]):
            pass
        _run(["ip6tables", "-t", "nat", "-F", DNS_CHAIN])
        _run(["ip6tables", "-t", "nat", "-X", DNS_CHAIN])
    _run(["ip6tables", "-A", FWD_CHAIN, "-i", s.lan_interface, "-o", s.lan_interface, "-j", "ACCEPT"])

    # Docker setzt FORWARD auf DROP; DOCKER-USER wird vorher durchlaufen.
    jumps = [("nat", "POSTROUTING", NAT_CHAIN), ("nat", "PREROUTING", DNS_CHAIN)]
    fwd_parent = "DOCKER-USER" if _run(["iptables", "-L", "DOCKER-USER", "-n"]) else "FORWARD"
    jumps.append(("filter", fwd_parent, FWD_CHAIN))
    for table, parent, chain in jumps:
        if not _run(["iptables", "-t", table, "-C", parent, "-j", chain]):
            ok &= _run(["iptables", "-t", table, "-I", parent, "1", "-j", chain])

    fwd_v6 = "DOCKER-USER" if _run(["ip6tables", "-L", "DOCKER-USER", "-n"]) else "FORWARD"
    if not _run(["ip6tables", "-t", "filter", "-C", fwd_v6, "-j", FWD_CHAIN]):
        _run(["ip6tables", "-t", "filter", "-I", fwd_v6, "1", "-j", FWD_CHAIN])

    # Isolationsregeln anwenden
    apply_isolation(s)

    # Byte-Zähler pro Verbindung (das Modul ist erst nach der NAT-Regel geladen).
    _write_sysctl("/proc/sys/net/netfilter/nf_conntrack_acct", "1")
    return ok


class TrafficRunner:
    def __init__(self, meter: TrafficMeter, poll_seconds: float):
        self.meter = meter
        self.poll_seconds = poll_seconds
        self._stopping = threading.Event()
        self._events: list[subprocess.Popen] = []

    @property
    def _families(self) -> list[list[str]]:
        """Zusätzliche conntrack-Argumente: IPv4 wie bisher, IPv6 nur mit IPV6_ENABLED (zweiter Aufruf mit -f ipv6)."""
        return [[], ["-f", "ipv6"]] if self.meter.lan6 is not None else [[]]

    def start(self) -> None:
        for extra in self._families:
            try:
                events = subprocess.Popen(
                    ["conntrack", "-E", "-e", "DESTROY", *extra, "-o", "extended,id"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                self._events.append(events)
                threading.Thread(target=self._read_events, args=(events,), daemon=True).start()
            except OSError as exc:
                print(f"cloudy: conntrack-Events nicht verfügbar: {exc}", file=sys.stderr, flush=True)
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _read_events(self, events: subprocess.Popen) -> None:
        assert events.stdout
        for line in events.stdout:
            c = parse_conntrack(line)
            if c:
                self.meter.observe(c, final=True)

    def _poll_loop(self) -> None:
        while not self._stopping.wait(self.poll_seconds):
            try:
                for extra in self._families:
                    out = subprocess.run(
                        ["conntrack", "-L", *extra, "-o", "extended,id"], capture_output=True, text=True, timeout=30
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
        for events in self._events:
            if events.poll() is None:
                events.terminate()
        self.meter.flush()
