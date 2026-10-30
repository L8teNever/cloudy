"""Werbeblocker per DNS – pro Gerät abschaltbar, standardmäßig für alle an.

dnsmasq kann keine Listen pro Gerät. Darum läuft ein zweiter dnsmasq (nur DNS, Port 5354) mit derselben
Konfiguration plus Werbeliste. Eine iptables-Regel leitet die DNS-Anfragen (Port 53) aller Geräte dorthin um –
außer bei Geräten, die den Werbeblocker ausgeschaltet haben. Das gilt auch mit WARP/VPN, weil DNS immer am Pi
landet. Läuft der zweite dnsmasq nicht, wird die Umleitung sofort entfernt, damit DNS nie ausfällt.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

from . import db
from .settings import Settings

PORT = 5354
CHAIN = "CLOUDY_ADBLOCK"
ENABLED_KEY = "adblock_enabled"  # global, Standard an
OFF_KEY = "adblock_off"  # MACs, die ihn ausgeschaltet haben
HOSTS_FILE = "adblock.hosts"
CONF_FILE = "dnsmasq-adblock.conf"
LIST_URL = os.environ.get("ADBLOCK_LIST_URL", "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts")
REFRESH_SECONDS = 24 * 3600
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
DOMAIN_RE = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)+$")

# Falls die große Liste (noch) nicht geladen werden kann.
FALLBACK = [
    "doubleclick.net", "googleadservices.com", "googlesyndication.com", "pagead2.googlesyndication.com",
    "adservice.google.com", "ad.doubleclick.net", "static.doubleclick.net", "adnxs.com", "ads.yahoo.com",
    "amazon-adsystem.com", "taboola.com", "outbrain.com", "criteo.com", "criteo.net", "pubmatic.com",
    "rubiconproject.com", "openx.net", "adform.net", "smartadserver.com", "moatads.com", "scorecardresearch.com",
    "adsrvr.org", "casalemedia.com", "media.net", "yieldlab.net", "zemanta.com", "ads.linkedin.com",
    "an.facebook.com", "ads.tiktok.com", "app-measurement.com",
]


def hosts_path(s: Settings) -> str:
    return os.path.join(s.data_dir, HOSTS_FILE)


def conf_path(s: Settings) -> str:
    return os.path.join(s.data_dir, CONF_FILE)


def is_enabled() -> bool:
    return db.get_setting(ENABLED_KEY, "1") not in ("0", "false", "aus", "off")


def set_enabled(on: bool) -> None:
    db.set_setting(ENABLED_KEY, "1" if on else "0")


def off_macs() -> set[str]:
    return {m for m in db.get_setting(OFF_KEY).split(",") if MAC_RE.match(m)}


def device_on(mac: str) -> bool:
    return (mac or "").strip().lower() not in off_macs()


def set_device(mac: str, on: bool) -> None:
    mac = (mac or "").strip().lower()
    if not MAC_RE.match(mac):
        raise ValueError("Gerät nicht erkannt")
    macs = off_macs()
    if on:
        macs.discard(mac)
    else:
        macs.add(mac)
    db.set_setting(OFF_KEY, ",".join(sorted(macs)))


# --------------------------------------------------------------------------- Liste


def parse_hosts(text: str, whitelist: set[str]) -> list[str]:
    """Domains aus einer Hosts-Datei ("0.0.0.0 ads.example.com")."""
    out = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        parts = line.split()
        if len(parts) < 2 or parts[0] not in ("0.0.0.0", "127.0.0.1"):
            continue
        for name in parts[1:]:
            name = name.lower().rstrip(".")
            if DOMAIN_RE.match(name) and re.search("[a-z]", name) and name not in whitelist and name != "localhost.localdomain":
                out.add(name)
    return sorted(out)


def write_hosts(s: Settings, domains: list[str]) -> int:
    path = hosts_path(s)
    os.makedirs(s.data_dir, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write("# cloudy Werbeblocker\n")
        for d in domains:
            f.write(f"0.0.0.0 {d}\n")
    os.replace(path + ".tmp", path)
    return len(domains)


def drop_whitelisted(s: Settings) -> None:
    """Neue Whitelist-Einträge sofort aus der Werbeliste nehmen (nicht erst beim nächsten Download)."""
    whitelist = set(db.whitelist())
    try:
        with open(hosts_path(s), encoding="utf-8") as f:
            domains = [line.split()[1] for line in f if line.startswith("0.0.0.0 ")]
    except OSError:
        return
    kept = [d for d in domains if d not in whitelist]
    if len(kept) != len(domains):
        write_hosts(s, kept)


def ensure_hosts(s: Settings) -> None:
    """Ohne Datei startet der zweite dnsmasq nicht -> notfalls die kleine eingebaute Liste."""
    if not os.path.exists(hosts_path(s)):
        whitelist = set(db.whitelist())
        write_hosts(s, sorted(d for d in FALLBACK if d not in whitelist))


def download(s: Settings) -> int:
    req = urllib.request.Request(LIST_URL, headers={"User-Agent": "cloudy"})
    with urllib.request.urlopen(req, timeout=60) as res:
        text = res.read().decode("utf-8", errors="replace")
    domains = parse_hosts(text, set(db.whitelist()))
    if len(domains) < 1000:
        raise ValueError(f"Liste zu klein ({len(domains)})")
    return write_hosts(s, domains)


def list_age(s: Settings) -> float | None:
    try:
        return time.time() - os.path.getmtime(hosts_path(s))
    except OSError:
        return None


def count(s: Settings) -> int:
    try:
        with open(hosts_path(s), encoding="utf-8") as f:
            return sum(1 for line in f if line.startswith("0.0.0.0 "))
    except OSError:
        return 0


# --------------------------------------------------------------------------- Konfiguration & Umleitung


def build_config(main_config: str, s: Settings) -> str:
    """Gleiche DNS-Einstellungen wie der Haupt-dnsmasq, aber ohne DHCP/RA, eigener Port, plus Werbeliste."""
    skip = ("dhcp-", "enable-ra", "ra-param")
    lines = [line for line in main_config.splitlines() if line and not line.startswith(skip)]
    lines += [f"port={PORT}", "pid-file=", f"addn-hosts={hosts_path(s)}"]
    return "\n".join(lines) + "\n"


def _run(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def rules(s: Settings, off: set[str]) -> list[list[str]]:
    """Inhalt der Kette: ausgeschaltete Geräte überspringen, alle anderen auf Port 5354."""
    out = []
    for mac in sorted(off):
        out.append(["-m", "mac", "--mac-source", mac, "-j", "RETURN"])
    for proto in ("udp", "tcp"):
        out.append(["-p", proto, "--dport", "53", "-j", "REDIRECT", "--to-ports", str(PORT)])
    return out


def apply_rules(s: Settings, active: bool) -> None:
    """active=False (zweiter dnsmasq läuft nicht oder global aus) -> Kette leeren, DNS geht normal an Port 53."""
    for tool in ("iptables", "ip6tables"):
        if tool == "ip6tables" and not s.ipv6_enabled:
            clear(tool)
            continue
        _run([tool, "-t", "nat", "-N", CHAIN])
        _run([tool, "-t", "nat", "-F", CHAIN])
        if active and is_enabled():
            for rule in rules(s, off_macs()):
                _run([tool, "-t", "nat", "-A", CHAIN, *rule])
        for proto in ("udp", "tcp"):
            match = ["-i", s.lan_interface, "-p", proto, "--dport", "53", "-j", CHAIN]
            if not _run([tool, "-t", "nat", "-C", "PREROUTING", *match]):
                # Ganz nach vorn, vor DNS-Force (das sonst auf Port 53 umleiten würde).
                _run([tool, "-t", "nat", "-I", "PREROUTING", "1", *match])


def clear(tool: str = "iptables") -> None:
    _run([tool, "-t", "nat", "-F", CHAIN])


class AdblockDns:
    """Zweiter dnsmasq. Log-Zeilen gehen an dieselbe Statistik wie beim Haupt-dnsmasq."""

    def __init__(self, s: Settings, main_config: str, feed):
        self.s = s
        self.main_config = main_config
        self.feed = feed
        self.proc: subprocess.Popen | None = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        ensure_hosts(self.s)
        with open(conf_path(self.s), "w", encoding="utf-8") as f:
            f.write(build_config(self.main_config, self.s))
        self._spawn()
        threading.Thread(target=self._refresh_loop, daemon=True).start()

    def _spawn(self) -> None:
        with self._lock:
            if self._stopping.is_set():
                return
            try:
                proc = subprocess.Popen(
                    ["dnsmasq", "--keep-in-foreground", f"--conf-file={conf_path(self.s)}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1,
                )
            except OSError as exc:
                print(f"cloudy: Werbeblocker-DNS startet nicht: {exc}", file=sys.stderr, flush=True)
                return
            self.proc = proc
        threading.Thread(target=self._read_log, args=(proc,), daemon=True).start()
        # Kurz warten, ob er wirklich läuft, erst dann umleiten.
        time.sleep(1)
        if proc.poll() is None:
            apply_rules(self.s, True)
            print(f"cloudy: Werbeblocker an ({count(self.s)} Domains, Port {PORT})", flush=True)

    def _read_log(self, proc: subprocess.Popen) -> None:
        assert proc.stderr
        for line in proc.stderr:
            self.feed(line)
        code = proc.wait()
        if proc is self.proc and not self._stopping.is_set():
            apply_rules(self.s, False)  # DNS darf nie ausfallen
            print(f"cloudy: Werbeblocker-DNS beendet (Code {code}) – Umleitung aus, neuer Versuch in 15 s",
                  file=sys.stderr, flush=True)
            if not self._stopping.wait(15):
                self._spawn()

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def update_rules(self) -> None:
        apply_rules(self.s, self.running)

    def restart(self) -> None:
        with self._lock:
            old, self.proc = self.proc, None
        if old and old.poll() is None:
            old.terminate()
            try:
                old.wait(timeout=5)
            except subprocess.TimeoutExpired:
                old.kill()
        self._spawn()

    def reload_hosts(self) -> None:
        if self.running:
            try:
                self.proc.send_signal(1)  # SIGHUP: addn-hosts neu lesen
            except (OSError, ValueError):
                pass

    def refresh_list(self) -> None:
        try:
            n = download(self.s)
            print(f"cloudy: Werbeliste aktualisiert ({n} Domains)", flush=True)
            self.reload_hosts()
        except Exception as exc:  # noqa: BLE001 – ohne neue Liste läuft die alte weiter
            print(f"cloudy: Werbeliste nicht geladen: {exc}", file=sys.stderr, flush=True)

    def _refresh_loop(self) -> None:
        age = list_age(self.s)
        if age is None or age > REFRESH_SECONDS or count(self.s) < 1000:
            self.refresh_list()
        while not self._stopping.wait(3600):
            age = list_age(self.s)
            if age is None or age > REFRESH_SECONDS:
                self.refresh_list()

    def stop(self) -> None:
        self._stopping.set()
        apply_rules(self.s, False)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
