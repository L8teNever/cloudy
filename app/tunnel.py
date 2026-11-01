"""Von unterwegs nach Hause: cloudy als WireGuard-Server.

Ein Handy oder Laptop verbindet sich unterwegs per WireGuard mit dem Pi (QR-Code scannen, fertig). Der ganze
Verkehr läuft dann über zu Hause – wie im WLAN: cloudy-DNS (Werbeblocker, Sperren, lokale Namen), Zugriff auf alle
Geräte im Heimnetz, VPN-Profile und Auto-Wechsel.

- Schnittstelle cloudy-wg im Host-Netz, Netz 10.77.0.0/24 (Pi = 10.77.0.1).
- Jedes Tunnel-Gerät erscheint in cloudy als eigenes Gerät mit einer festen Ersatz-MAC (02:77:67:00:00:NN),
  damit Profile, Gruppen und Auto-Wechsel wie bei echten Geräten funktionieren.
- Die Fritzbox leitet den UDP-Port an den Pi weiter (cloudy richtet das per TR-064 selbst ein).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from . import adblock, db, fritz
from .settings import Settings

KEY = "tunnel"  # JSON: {"enabled", "port", "endpoint", "private_key", "peers": [{name, ip, private_key}]}
IFACE = "cloudy-wg"
PREFIX = "10.77.0."
SERVER_IP = PREFIX + "1"
NET = PREFIX + "0/24"
DEFAULT_PORT = 51820
MAC_PREFIX = "02:77:67:00:00:"
NAME_MAX = 40


# --------------------------------------------------------------------------- Schlüssel


def new_private_key() -> str:
    raw = X25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return base64.b64encode(raw).decode()


def public_key(private_key: str) -> str:
    key = X25519PrivateKey.from_private_bytes(base64.b64decode(private_key))
    return base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


# --------------------------------------------------------------------------- Einstellungen


def load() -> dict:
    try:
        cfg = json.loads(db.get_setting(KEY) or "{}")
    except ValueError:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("enabled", False)
    cfg.setdefault("port", DEFAULT_PORT)
    cfg.setdefault("endpoint", "")
    cfg.setdefault("peers", [])
    if not cfg.get("private_key"):
        cfg["private_key"] = new_private_key()
        save(cfg)
    return cfg


def save(cfg: dict) -> None:
    db.set_setting(KEY, json.dumps(cfg, sort_keys=True))


def enabled() -> bool:
    return bool(load()["enabled"])


def peer_mac(ip: str) -> str:
    return MAC_PREFIX + f"{int(ip.rsplit('.', 1)[1]):02x}"


def peers() -> list[dict]:
    """Tunnel-Geräte inkl. öffentlichem Schlüssel und Ersatz-MAC."""
    return [{**p, "mac": peer_mac(p["ip"]), "public_key": public_key(p["private_key"])} for p in load()["peers"]]


def peer(ip: str) -> dict | None:
    return next((p for p in peers() if p["ip"] == ip), None)


def mac_ips() -> dict[str, str]:
    """Ersatz-MAC → Tunnel-IP (für die Gateway-Markierungen)."""
    return {p["mac"]: p["ip"] for p in peers()}


def add_peer(name: str) -> dict:
    cfg = load()
    used = {p["ip"] for p in cfg["peers"]}
    ip = next((PREFIX + str(n) for n in range(2, 255) if PREFIX + str(n) not in used), None)
    if not ip:
        raise ValueError("Keine freie Tunnel-Adresse mehr (maximal 253 Geräte)")
    name = " ".join((name or "").split())[:NAME_MAX] or f"Unterwegs {ip.rsplit('.', 1)[1]}"
    cfg["peers"].append({"name": name, "ip": ip, "private_key": new_private_key()})
    save(cfg)
    db.upsert_devices([(ip, peer_mac(ip), name)])
    return peer(ip)  # type: ignore[return-value]


def remove_peer(ip: str) -> bool:
    cfg = load()
    before = len(cfg["peers"])
    cfg["peers"] = [p for p in cfg["peers"] if p["ip"] != ip]
    save(cfg)
    return len(cfg["peers"]) != before


def update(enabled_: bool | None = None, port: int | None = None, endpoint: str | None = None) -> dict:
    cfg = load()
    if enabled_ is not None:
        cfg["enabled"] = bool(enabled_)
    if port is not None:
        cfg["port"] = min(65535, max(1024, int(port)))
    if endpoint is not None:
        cfg["endpoint"] = endpoint.strip().lower().removeprefix("https://").removeprefix("http://").strip("/")[:200]
    save(cfg)
    return cfg


# --------------------------------------------------------------------------- Konfigurationen


def server_config(cfg: dict) -> str:
    lines = ["[Interface]", f"PrivateKey = {cfg['private_key']}", f"ListenPort = {cfg['port']}"]
    for p in cfg["peers"]:
        lines += ["", "[Peer]", f"PublicKey = {public_key(p['private_key'])}", f"AllowedIPs = {p['ip']}/32"]
    return "\n".join(lines) + "\n"


def client_config(p: dict, endpoint: str) -> str:
    cfg = load()
    host = endpoint or "DEINE-ADRESSE.myfritz.net"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6
    return "\n".join([
        "[Interface]",
        f"PrivateKey = {p['private_key']}",
        f"Address = {p['ip']}/32",
        f"DNS = {SERVER_IP}",
        "",
        "[Peer]",
        f"PublicKey = {public_key(cfg['private_key'])}",
        f"Endpoint = {host}:{cfg['port']}",
        # Alles über zu Hause – auch IPv6, damit nichts am Tunnel vorbeigeht.
        "AllowedIPs = 0.0.0.0/0, ::/0",
        "PersistentKeepalive = 25",
    ]) + "\n"


def qr_svg(text: str) -> str:
    import segno

    return segno.make(text, error="m").svg_inline(scale=5, dark="#111", light="#fff")


# --------------------------------------------------------------------------- Fritzbox


def _router(settings: Settings):
    r = fritz.get_routers(settings)[0]
    return fritz._connect(r["ip"], r.get("user", ""), r.get("password", ""), timeout=5)


def detect_endpoint(settings: Settings) -> str:
    """MyFRITZ-Adresse oder die öffentliche IPv4 der Fritzbox – oder \"\"."""
    try:
        fc = _router(settings)
    except Exception:  # noqa: BLE001
        return ""
    try:
        info = fc.call_action("X_AVM-DE_MyFritz1", "GetInfo")
        if info.get("NewEnabled") and info.get("NewDynDNSName"):
            return str(info["NewDynDNSName"]).lower()
    except Exception:  # noqa: BLE001
        pass
    for service in ("WANIPConnection1", "WANPPPConnection1"):
        try:
            ip = fc.call_action(service, "GetExternalIPAddress").get("NewExternalIPAddress") or ""
            if ip and ip != "0.0.0.0":
                return ip
        except Exception:  # noqa: BLE001
            continue
    return ""


def endpoint(settings: Settings | None = None) -> str:
    """Eingetragene Adresse, sonst die zuletzt erkannte (detect=True fragt die Fritzbox neu)."""
    cfg = load()
    return cfg["endpoint"] or cfg.get("detected", "")


def refresh_endpoint(settings: Settings) -> str:
    found = detect_endpoint(settings)
    if found:
        cfg = load()
        cfg["detected"] = found
        save(cfg)
    return endpoint()


def ensure_port_forward(settings: Settings) -> tuple[bool, str]:
    """UDP-Port an den Pi weiterleiten lassen (TR-064 AddPortMapping)."""
    port = int(load()["port"])
    try:
        fc = _router(settings)
    except Exception as exc:  # noqa: BLE001
        return False, f"Fritzbox nicht erreichbar: {exc}"
    last = ""
    for service in ("WANIPConnection1", "WANPPPConnection1"):
        try:
            fc.call_action(
                service, "AddPortMapping",
                NewRemoteHost="", NewExternalPort=port, NewProtocol="UDP", NewInternalPort=port,
                NewInternalClient=settings.pi_ip, NewEnabled=True,
                NewPortMappingDescription="cloudy WireGuard", NewLeaseDuration=0,
            )
            return True, f"Fritzbox leitet UDP {port} an den Pi ({settings.pi_ip}) weiter."
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
    return False, (
        f"Portfreigabe ging nicht ({last}). In der Fritzbox unter Internet → Freigaben beim Pi "
        f"„Selbstständige Portfreigaben erlauben“ einschalten oder selbst UDP {port} → {settings.pi_ip} anlegen."
    )


# --------------------------------------------------------------------------- Pi


def _run(cmd: list[str], input_: str | None = None) -> bool:
    try:
        return subprocess.run(cmd, input=input_, text=True, capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _ensure_rule(table: str, chain: str, rule: list[str], first: bool = True) -> None:
    base = ["iptables", "-t", table]
    if not _run([*base, "-C", chain, *rule]):
        _run([*base, "-I", chain, "1", *rule] if first else [*base, "-A", chain, *rule])


def _sync_devices() -> None:
    db.upsert_devices([(p["ip"], peer_mac(p["ip"]), p["name"]) for p in load()["peers"]])


def apply(settings: Settings) -> str:
    """Schnittstelle und Regeln passend zur Einstellung herstellen. Gibt einen Status-Text zurück."""
    cfg = load()
    if not cfg["enabled"]:
        _run(["ip", "link", "del", IFACE])
        return "aus"
    if not _run(["ip", "link", "show", IFACE]) and not _run(["ip", "link", "add", IFACE, "type", "wireguard"]):
        return "WireGuard fehlt im Kernel des Pi (sudo apt install wireguard, dann neu starten)"
    path = os.path.join(settings.data_dir, "tunnel-wg.conf")
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write(server_config(cfg))
    if not _run(["wg", "syncconf", IFACE, path]):
        return "wg-Programm fehlt oder Konfiguration ungültig"
    _run(["ip", "address", "replace", f"{SERVER_IP}/24", "dev", IFACE])
    _run(["ip", "link", "set", IFACE, "up"])
    try:
        with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
            f.write("1")
    except OSError:
        pass
    # Docker setzt FORWARD auf DROP: Tunnel-Verkehr ausdrücklich erlauben (raus und Antworten zurück).
    _ensure_rule("filter", "FORWARD", ["-i", IFACE, "-j", "ACCEPT"])
    _ensure_rule("filter", "FORWARD", ["-o", IFACE, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"])
    # Ins Heimnetz/Internet mit der Adresse des Pi – Fritzbox und Geräte kennen 10.77.0.x nicht.
    _ensure_rule("nat", "POSTROUTING", ["-s", NET, "-o", settings.lan_interface, "-j", "MASQUERADE"], first=False)
    _ensure_rule("mangle", "FORWARD", [
        "-i", IFACE, "-p", "tcp", "--tcp-flags", "SYN,RST", "SYN", "-j", "TCPMSS", "--clamp-mss-to-pmtu",
    ])
    # Werbeblocker wie im WLAN: DNS aus dem Tunnel durch dieselbe Kette.
    for proto in ("udp", "tcp"):
        _ensure_rule("nat", "PREROUTING", ["-i", IFACE, "-p", proto, "--dport", "53", "-j", adblock.CHAIN])
    _sync_devices()
    return "an"


def handshakes() -> dict[str, int]:
    """Öffentlicher Schlüssel → Zeitpunkt der letzten Verbindung (0 = noch nie)."""
    try:
        out = subprocess.run(["wg", "show", IFACE, "latest-handshakes"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    result = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            result[parts[0]] = int(parts[1])
    return result


def online(public: str, shakes: dict[str, int], now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    return now - shakes.get(public, 0) < 180


def start(settings: Settings) -> None:
    try:
        state = apply(settings)
        if state != "aus":
            print(f"cloudy: Tunnel von unterwegs: {state}", flush=True)
    except Exception as exc:  # noqa: BLE001 – der Tunnel darf cloudy nie stoppen
        print(f"cloudy: Tunnel von unterwegs nicht verfügbar: {exc}", file=sys.stderr, flush=True)
