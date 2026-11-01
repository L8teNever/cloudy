"""Wechselseite für Geräte mit installiertem cloudy-Zertifikat.

Geräte ohne Zertifikat wechseln still (siehe autoswitch). Hat ein Gerät das cloudy-Zertifikat, zeigt der Pi bei
Auto-Wechsel-Seiten kurz die Wechselseite – nur dort, aller andere Verkehr läuft normal durch:

1. dnsmasq trägt die IPs der Auto-Wechsel-Seiten ins ipset cloudy_auto ein (nur die Regel-Domains selbst).
2. iptables leitet HTTPS dieser Geräte zu diesen IPs auf eine kleine Weiche auf dem Pi um.
3. Die Weiche liest den Namen aus dem TLS-Hallo (SNI): Auto-Wechsel-Seite → cloudy-HTTPS (Zertifikat von der
   cloudy-CA, Wechselseite); alles andere (z. B. eine andere Seite auf derselben CDN-IP) → unverändert zum Ziel.
4. Ist das VPN bereit, nimmt cloudy das Gerät aus der Umleitung, die Wechselseite leitet zur echten Seite weiter.

Neue Seiten brauchen kein neues Zertifikat auf den Geräten – der Pi stellt pro Seite selbst eins aus.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import struct
import subprocess
import sys
import threading

from . import db, warp

CERT_KEY = "auto_cert_macs"  # JSON-Liste: Geräte, die der cloudy-CA vertrauen
SET = "cloudy_auto"
CHAIN = "CLOUDY_AUTO"
CHECK_HOST = "cloudy-zert.internal"  # nur für das Prüfbild – hier hat nie jemand eine Warnung weggeklickt
PROXY_PORT = int(os.environ.get("AUTO_PROXY_PORT", "8444"))
SO_ORIGINAL_DST = 80
HELLO_TIMEOUT = 10.0

_peers: dict[int, str] = {}  # lokaler Port der Weiche → echte Geräte-IP (für client_ip)
_peers_lock = threading.Lock()


# --------------------------------------------------------------------------- Geräte mit Zertifikat


def cert_macs() -> set[str]:
    try:
        raw = json.loads(db.get_setting(CERT_KEY) or "[]")
    except ValueError:
        return set()
    return {str(m).lower() for m in raw} if isinstance(raw, list) else set()


def set_cert(mac: str, on: bool) -> bool:
    """Gerät als „hat Zertifikat“ merken oder vergessen. True, wenn sich etwas geändert hat."""
    mac = (mac or "").strip().lower()
    if not warp.MAC_RE.match(mac):
        return False
    macs = cert_macs()
    if (mac in macs) == on:
        return False
    macs = macs | {mac} if on else macs - {mac}
    db.set_setting(CERT_KEY, json.dumps(sorted(macs)) if macs else "")
    return True


# --------------------------------------------------------------------------- iptables


def _run(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def create_set() -> None:
    _run(["ipset", "create", "-exist", SET, "hash:ip", "timeout", "86400"])


def intercept_rules(macs: list[str]) -> list[list[str]]:
    return [
        ["-m", "mac", "--mac-source", mac, "-p", "tcp", "--dport", "443", "-j", "REDIRECT", "--to-ports", str(PROXY_PORT)]
        for mac in macs
    ]


def apply_intercept(s, macs: list[str]) -> None:
    """Nur IPv4: Über IPv6 gibt es keine Umleitung – dann wechselt das Gerät nach kurzer Zeit still."""
    create_set()
    _run(["iptables", "-t", "nat", "-N", CHAIN])
    _run(["iptables", "-t", "nat", "-F", CHAIN])
    for rule in intercept_rules(macs):
        if not _run(["iptables", "-t", "nat", "-A", CHAIN, *rule]):
            print(f"cloudy: Wechselseite: iptables-Regel nicht angelegt: {' '.join(rule)}", file=sys.stderr, flush=True)
    jump = ["-i", s.lan_interface, "-p", "tcp", "--dport", "443", "-m", "set", "--match-set", SET, "dst", "-j", CHAIN]
    if not _run(["iptables", "-t", "nat", "-C", "PREROUTING", *jump]):
        _run(["iptables", "-t", "nat", "-I", "PREROUTING", "1", *jump])


# --------------------------------------------------------------------------- Weiche (SNI)


def parse_sni(data: bytes) -> str | None:
    """Servername aus einem TLS-ClientHello (erster Datensatz) – oder None."""
    try:
        if len(data) < 5 or data[0] != 0x16:
            return None
        body = data[5:5 + int.from_bytes(data[3:5], "big")]
        if not body or body[0] != 1:
            return None
        i = 4 + 2 + 32  # Handshake-Kopf, Version, Zufall
        i += 1 + body[i]  # Session-ID
        i += 2 + int.from_bytes(body[i:i + 2], "big")  # Cipher-Suites
        i += 1 + body[i]  # Kompression
        end = min(len(body), i + 2 + int.from_bytes(body[i:i + 2], "big"))
        i += 2
        while i + 4 <= end:
            kind, length = int.from_bytes(body[i:i + 2], "big"), int.from_bytes(body[i + 2:i + 4], "big")
            i += 4
            if kind == 0:  # server_name
                j = i + 2
                while j + 3 <= i + length:
                    name_type, name_len = body[j], int.from_bytes(body[j + 1:j + 3], "big")
                    j += 3
                    if name_type == 0:
                        return body[j:j + name_len].decode("ascii", "ignore").lower().rstrip(".") or None
                    j += name_len
                return None
            i += length
    except IndexError:
        return None
    return None


def _read_hello(sock: socket.socket) -> bytes:
    data = b""
    while len(data) < 5:
        chunk = sock.recv(4096)
        if not chunk:
            return data
        data += chunk
    want = 5 + int.from_bytes(data[3:5], "big")
    while len(data) < want:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _original_dst(sock: socket.socket) -> tuple[str, int] | None:
    try:
        raw = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
    except OSError:
        return None
    return socket.inet_ntoa(raw[4:8]), struct.unpack("!H", raw[2:4])[0]


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def peer_ip(port: int) -> str | None:
    with _peers_lock:
        return _peers.get(port)


class _Handler(socketserver.BaseRequestHandler):
    server: "_Server"

    def handle(self) -> None:
        client = self.request
        client.settimeout(HELLO_TIMEOUT)
        try:
            hello = _read_hello(client)
        except OSError:
            return
        name = parse_sni(hello)
        local = bool(name and self.server.covers(name))
        target = ("127.0.0.1", self.server.https_port) if local else _original_dst(client)
        if not target or not hello:
            return
        try:
            upstream = socket.create_connection(target, timeout=HELLO_TIMEOUT)
        except OSError:
            return
        port = upstream.getsockname()[1]
        if local:
            with _peers_lock:
                _peers[port] = self.client_address[0]
        try:
            client.settimeout(None)
            upstream.settimeout(None)
            upstream.sendall(hello)
            back = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
            back.start()
            _pipe(client, upstream)
            back.join()
        finally:
            if local:
                with _peers_lock:
                    _peers.pop(port, None)
            upstream.close()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, https_port: int, covers):
        self.https_port = https_port
        self.covers = covers
        super().__init__(("0.0.0.0", port), _Handler)


def start_proxy(https_port: int, covers) -> _Server:
    server = _Server(PROXY_PORT, https_port, covers)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
