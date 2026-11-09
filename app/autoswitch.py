"""Auto-Wechsel: Ruft ein Gerät eine bestimmte Seite auf, wechselt es still im Hintergrund aufs VPN.

Ablauf (ohne Anzeige, nichts auf den Geräten installieren):
1. Das Gerät fragt per DNS z. B. aniworld.to an.
2. Festes Profil: Die Seite läuft per Seiten-Regel (ipset) schon beim ersten Aufruf übers VPN, zusätzlich
   wird das ganze Gerät aufs Profil gestellt (CDNs, Nachlade-Domains).
   „auto“: im Hintergrund wird das erste verbundene VPN gesucht, das die Seite erreicht.
3. Nach der Wartezeit ohne Aufruf: zurück auf Standard.

Eine Wechselseite gibt es nur noch über eigene Wechsel-Domains (z. B. per Cloudflare), denn bei HTTPS/HSTS-Seiten
kann der Pi ohne installiertes Zertifikat keine Seite anzeigen.
"""

from __future__ import annotations

import http.client
import json
import re
import secrets
import socket
import ssl
import struct
import threading
import time

from . import db, dienste, policy, warp, zertwechsel

RULES_KEY = "auto_rules"  # JSON: {"domain": "slot"| "auto", …}
MINUTES_KEY = "auto_minutes"
STATE_KEY = "auto_state"  # JSON: {mac: {"slot", "seen", "since", "host"?}}
NORMAL_KEY = "auto_normal_slot"  # "" = Standard (Fritzbox), "direct", oder Slot
PASS_KEY = "auto_pass"  # JSON: {domain: {"slot", "prev"}} – Seiten-Regel für „auto“/normal aktiv
AUTO = "auto"
DEFAULT_MINUTES = 5
MIN_MINUTES, MAX_MINUTES = 1, 240
TICK = 15.0
RULES_TTL = 10.0
SEEN_STEP = 5
SETTLE = 5.0  # kurz warten, bis Markierung/DNS greifen; die Wechselseite zeigt sowieso ≥5 s
PROBE_TTL = 45.0  # Ergebnis „Seite über Slot erreichbar?“ so lange merken
PROBE_TIMEOUT = 8
PROBE_DNS = ("1.1.1.1", "8.8.8.8")
PROBE_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
CERT_FALLBACK = 20.0  # Gerät mit Zertifikat: kommt die Wechselseite nicht (z. B. IPv6), still wechseln
_rules_gen = 0  # steigt bei jeder Regel-Änderung → Cache im AutoSwitcher verwerfen


# --------------------------------------------------------------------------- Einstellungen


def load_rules() -> dict[str, str]:
    try:
        raw = json.loads(db.get_setting(RULES_KEY) or "{}")
    except ValueError:
        return {}
    return {str(d): str(s) for d, s in raw.items()} if isinstance(raw, dict) else {}


def _bump_rules() -> None:
    global _rules_gen
    _rules_gen += 1


def set_rule(domain: str, slot: str) -> None:
    rules = load_rules()
    rules[domain] = slot
    db.set_setting(RULES_KEY, json.dumps(rules, sort_keys=True))
    _bump_rules()


def remove_rule(domain: str) -> None:
    rules = load_rules()
    domain = domain.strip().lower()
    rules.pop(domain, None)
    db.set_setting(RULES_KEY, json.dumps(rules, sort_keys=True) if rules else "")
    clear_pass(domain)
    _bump_rules()


def purge_slot(slot: str) -> None:
    """Regeln für ein gelöschtes Profil entfernen (auto-Regeln bleiben)."""
    if slot == AUTO:
        return
    rules = {d: s for d, s in load_rules().items() if s != slot}
    db.set_setting(RULES_KEY, json.dumps(rules, sort_keys=True) if rules else "")
    _bump_rules()
    for domain, info in list(pass_map().items()):
        if info.get("slot") == slot:
            clear_pass(domain)


def minutes() -> int:
    try:
        return min(MAX_MINUTES, max(MIN_MINUTES, int(db.get_setting(MINUTES_KEY) or DEFAULT_MINUTES)))
    except ValueError:
        return DEFAULT_MINUTES


def set_minutes(value: int) -> int:
    value = min(MAX_MINUTES, max(MIN_MINUTES, int(value)))
    db.set_setting(MINUTES_KEY, str(value))
    return value


def normal_slot() -> str:
    """Profil für „über normales Internet schauen“: "" = Standard, direct, oder Slot-Name."""
    return (db.get_setting(NORMAL_KEY) or "").strip().lower()


def set_normal_slot(slot: str) -> str:
    slot = (slot or "").strip().lower()
    if slot in (warp.STANDARD,):
        slot = ""
    db.set_setting(NORMAL_KEY, slot)
    return slot


def _covers(rule: str, domain: str) -> bool:
    return domain == rule or domain.endswith("." + rule)


def pass_map() -> dict[str, dict]:
    try:
        raw = json.loads(db.get_setting(PASS_KEY) or "{}")
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for domain, info in raw.items():
        if isinstance(info, dict) and info.get("slot") is not None:
            out[str(domain)] = {"slot": str(info["slot"]), "prev": info.get("prev")}
        elif isinstance(info, str):
            out[str(domain)] = {"slot": info, "prev": None}
    return out


def _save_pass(data: dict[str, dict]) -> None:
    db.set_setting(PASS_KEY, json.dumps(data, sort_keys=True) if data else "")


def set_pass(domain: str, slot: str) -> bool:
    """Seiten-Regel für domain setzen. True wenn sich etwas geändert hat."""
    domain = domain.strip().lower()
    slot = (slot or warp.DIRECT).strip().lower() or warp.DIRECT
    current = pass_map()
    prev_routes = db.site_routes()
    if domain in current and current[domain].get("slot") == slot:
        if prev_routes.get(domain) == slot:
            return False
    prev = current[domain]["prev"] if domain in current else prev_routes.get(domain)
    if domain not in current:
        prev = prev_routes.get(domain)
    current[domain] = {"slot": slot, "prev": prev}
    _save_pass(current)
    db.set_site_route(domain, slot)
    return True


def clear_pass(domain: str) -> bool:
    domain = domain.strip().lower()
    current = pass_map()
    info = current.pop(domain, None)
    if info is None:
        return False
    _save_pass(current)
    prev = info.get("prev")
    if prev:
        db.set_site_route(domain, prev)
    else:
        # Nur entfernen, wenn wir die Regel gesetzt haben (prev war None / fehlte).
        if db.site_routes().get(domain) == info.get("slot"):
            db.remove_site_route(domain)
    return True


def managed_domains() -> list[str]:
    return sorted(load_rules())


def fixed_routes() -> dict[str, str]:
    """Regeln mit festem Profil als Seiten-Regeln – greifen schon beim ersten DNS-Aufruf (ipset)."""
    return {d: s for d, s in load_rules().items() if s and s != AUTO}


def match_rule(domain: str) -> tuple[str, str] | None:
    """(Regel-Domain, Slot) oder None. Slot kann „auto“ sein."""
    domain = (domain or "").lower().rstrip(".")
    if not domain:
        return None
    for rule, slot in load_rules().items():
        if _covers(rule, domain):
            return rule, slot
        for extra in dienste.companions(rule):
            if _covers(extra, domain):
                return rule, slot
    return None


def covers_host(host: str) -> bool:
    return match_rule(host) is not None


def is_passed(host: str) -> bool:
    """Seiten-Regel aus dem Auto-Wechsel aktiv für diesen Host?"""
    host = (host or "").lower().rstrip(".")
    for domain in pass_map():
        if _covers(domain, host):
            return True
    return False


def rule_apex(host: str) -> str:
    """Die konfigurierte Basis-Domain zu diesem Host (für Zertifikat/Pass)."""
    hit = match_rule(host)
    return hit[0] if hit else (host or "").lower().rstrip(".")


# --------------------------------------------------------------------------- VPN-Erreichbarkeit


def vpn_slots(settings) -> list[str]:
    """Nur WireGuard-VPN-Slots (vpn1 …), in der Reihenfolge der Einstellungen."""
    return [name for name, _ in settings.warp_slots if warp.kind(name) == "vpn"]


def _slot_mark(settings, slot: str) -> int | None:
    """Markierung, mit der Pakete in die Routing-Tabelle dieses Profils laufen (wie bei den Geräten)."""
    for index, (name, _) in enumerate(settings.warp_slots, start=1):
        if name == slot:
            return int(warp.mark(index), 16)
    return None


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("Probe-Zeit abgelaufen")
    return left


def _marked_socket(kind: int, mark: int, timeout: float) -> socket.socket:
    sock = socket.socket(socket.AF_INET, kind)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, mark)
    sock.settimeout(timeout)
    return sock


def _dns_query(host: str, txid: int) -> bytes:
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in host.split(".")) + b"\0"
    return struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)


def _skip_name(data: bytes, pos: int) -> int:
    while True:
        length = data[pos]
        if length & 0xC0 == 0xC0:
            return pos + 2
        pos += 1 + length
        if length == 0:
            return pos


def _parse_a(data: bytes, txid: int) -> list[str]:
    """IPv4-Adressen aus einer DNS-Antwort (leer bei Fehler oder fremder Antwort)."""
    try:
        rid, flags, questions, answers = struct.unpack(">HHHH", data[:8])
        if rid != txid or flags & 0x000F:
            return []
        pos = 12
        for _ in range(questions):
            pos = _skip_name(data, pos) + 4
        out = []
        for _ in range(answers):
            pos = _skip_name(data, pos)
            rtype, rclass, _ttl, length = struct.unpack(">HHIH", data[pos:pos + 10])
            pos += 10
            if rtype == 1 and rclass == 1 and length == 4:
                out.append(socket.inet_ntoa(data[pos:pos + 4]))
            pos += length
        return out
    except (struct.error, IndexError, OSError):
        return []


def _resolve_via(mark: int, host: str, deadline: float) -> list[str]:
    """Namen durch den Tunnel auflösen – so wie ihn das VPN sieht, nicht wie der Provider im Heimnetz."""
    for server in PROBE_DNS:
        txid = secrets.randbelow(0x10000)
        try:
            with _marked_socket(socket.SOCK_DGRAM, mark, min(2.5, _remaining(deadline))) as sock:
                sock.connect((server, 53))
                sock.send(_dns_query(host, txid))
                ips = _parse_a(sock.recv(1500), txid)
        except OSError:
            continue
        if ips:
            return ips
    # Tunnel lässt kein fremdes DNS durch: normal auflösen. Eine gefälschte Sperr-Adresse fällt bei der TLS-Prüfung auf.
    try:
        _remaining(deadline)
        return socket.gethostbyname_ex(host)[2]
    except OSError:
        return []


def site_answered(status: int, headers) -> bool:
    """Hat die Seite wirklich geantwortet? 403/429/451/5xx = über dieses VPN gesperrt oder kaputt."""
    if headers.get("cf-mitigated"):  # Browser-Prüfung von Cloudflare: das schafft der Browser des Geräts
        return True
    return status < 500 and status not in (403, 429, 451)


def _https_status(mark: int, ip: str, host: str, deadline: float) -> tuple[int, http.client.HTTPMessage]:
    context = ssl.create_default_context()

    class Conn(http.client.HTTPSConnection):
        def connect(self):
            raw = _marked_socket(socket.SOCK_STREAM, mark, _remaining(deadline))
            try:
                raw.connect((ip, 443))
                self.sock = context.wrap_socket(raw, server_hostname=host)
            except Exception:
                raw.close()
                raise

    conn = Conn(host, 443, timeout=_remaining(deadline))
    try:
        conn.request("GET", "/", headers={"User-Agent": PROBE_AGENT, "Accept": "text/html,*/*", "Connection": "close"})
        resp = conn.getresponse()
        return resp.status, resp.headers
    finally:
        conn.close()


def probe_via(settings, slot: str, host: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """Erreicht man https://host/ durch den Tunnel dieses Profils? (Eine Adresse pro Versuch, TLS wird geprüft.)"""
    host = (host or "").lower().rstrip(".")
    if not HOST_RE.match(host) or not settings.gateway_enabled:
        return False  # ohne Gateway fehlen die Routing-Tabellen, der Test ginge am VPN vorbei
    mark = _slot_mark(settings, slot)
    if mark is None:
        return False
    deadline = time.monotonic() + timeout
    names = [host] if host.startswith("www.") else [host, f"www.{host}"]
    try:
        for name in names:
            ips = _resolve_via(mark, name, deadline)
            if not ips:
                continue
            try:
                status, headers = _https_status(mark, ips[0], name, deadline)
            except (OSError, ssl.SSLError, http.client.HTTPException):
                continue  # keine Verbindung: www-Variante versuchen
            return site_answered(status, headers)
    except TimeoutError:
        return False
    return False


# --------------------------------------------------------------------------- Laufzeit


class AutoSwitcher:
    def __init__(self, tick: float = TICK, settings=None):
        self.tick_seconds = tick
        self.settings = settings
        self.on_dns_change = None  # wird gesetzt, wenn Seiten-Regeln neu geschrieben werden müssen
        self._lock = threading.RLock()
        self._rules: list[tuple[str, str]] = []
        self._rules_at = 0.0
        self._rules_gen = -1
        self._ip_mac: dict[str, tuple[float, str]] = {}
        self._probe_cache: dict[tuple[str, str], tuple[float, bool]] = {}
        self._picking: set[str] = set()  # MACs, für die gerade ein VPN gesucht wird
        self._no_vpn: dict[tuple[str, str], float] = {}  # (MAC, Seite) -> wann zuletzt kein VPN die Seite erreichte
        self.async_auto = True
        self.on_intercept = None  # bekommt die MACs, deren HTTPS zur Wechselseite umgeleitet wird
        self._intercept_last: list[str] | None = None
        self._waiting: set[str] = set()  # Geräte mit Zertifikat, für die der stille Notfall-Wechsel läuft
        self._stopping = threading.Event()
        try:
            state = json.loads(db.get_setting(STATE_KEY) or "{}")
        except ValueError:
            state = {}
        self.state: dict[str, dict] = state if isinstance(state, dict) else {}

    def _current_rules(self, now: float) -> list[tuple[str, str]]:
        if now - self._rules_at > RULES_TTL or self._rules_gen != _rules_gen:
            expanded: list[tuple[str, str]] = []
            for domain, slot in load_rules().items():
                expanded.append((domain, slot))
                expanded += [(extra, slot) for extra in dienste.companions(domain)]
            self._rules = expanded
            self._rules_at = now
            self._rules_gen = _rules_gen
        return self._rules

    def match(self, domain: str, now: float | None = None) -> str:
        """Profil („auto“ oder Slot) für diese Seite – oder \"\"."""
        now = now if now is not None else time.time()
        domain = (domain or "").lower().rstrip(".")
        for rule, slot in self._current_rules(now):
            if _covers(rule, domain):
                return slot
        return ""

    def _mac_for(self, ip: str, now: float) -> str:
        hit = self._ip_mac.get(ip)
        if hit and now - hit[0] < 60:
            return hit[1]
        dev = db.device(ip)
        mac = ((dev or {}).get("mac") or "").strip().lower()
        if len(self._ip_mac) > 2000:
            self._ip_mac.clear()
        self._ip_mac[ip] = (now, mac)
        return mac

    def _save(self) -> None:
        db.set_setting(STATE_KEY, json.dumps(self.state) if self.state else "")
        self.sync_intercept()

    def sync_intercept(self, force: bool = False) -> None:
        """Geräte mit Zertifikat, die gerade keine Seite freigeschaltet haben, zur Wechselseite umleiten."""
        cb = self.on_intercept
        if not cb:
            return
        with self._lock:
            wanted = sorted(m for m in zertwechsel.cert_macs() if not (self.state.get(m) or {}).get("shown"))
            if wanted == self._intercept_last and not force:
                return
            self._intercept_last = wanted
        try:
            cb(wanted)
        except Exception as exc:  # noqa: BLE001
            print(f"cloudy: Wechselseite-Umleitung: {exc}", flush=True)

    def mark_shown(self, ip: str, now: float | None = None) -> None:
        """Wechselseite fertig: Gerät aus der Umleitung nehmen, damit die echte Seite lädt."""
        now = now if now is not None else time.time()
        mac = self._mac_for(ip, now)
        with self._lock:
            st = self.state.get(mac)
            if st and not st.get("shown"):
                st["shown"] = True
                self._save()

    def _dns_changed(self) -> None:
        cb = self.on_dns_change
        if cb:
            try:
                cb()
            except Exception as exc:  # noqa: BLE001
                print(f"cloudy: Auto-Wechsel DNS-Update: {exc}", flush=True)

    def _cached_probe(self, slot: str, host: str, now: float) -> bool | None:
        hit = self._probe_cache.get((slot, host))
        if hit and now - hit[0] < PROBE_TTL:
            return hit[1]
        return None

    def _probe(self, slot: str, host: str, now: float) -> bool:
        cached = self._cached_probe(slot, host, now)
        if cached is not None:
            return cached
        settings = self.settings
        ok = bool(settings and probe_via(settings, slot, host))
        self._probe_cache[(slot, host)] = (now, ok)
        if len(self._probe_cache) > 500:
            self._probe_cache.clear()
        return ok

    def _touch(self, mac: str, now: float) -> None:
        st = self.state.get(mac)
        if st and now - st["seen"] >= SEEN_STEP:
            st["seen"] = now
            self._save()

    def on_query(self, ip: str, domain: str, now: float | None = None) -> None:
        """DNS-Anfrage für eine Auto-Wechsel-Seite: Gerät still im Hintergrund aufs VPN stellen.

        Geräte mit cloudy-Zertifikat sehen stattdessen die Wechselseite (die schaltet über status_for um);
        kommt die nicht (z. B. Aufruf über IPv6), wird nach CERT_FALLBACK Sekunden still gewechselt.
        """
        now = now if now is not None else time.time()
        slot = self.match(domain, now)
        if not slot:
            return
        mac = self._mac_for(ip, now)
        if not mac or not warp.MAC_RE.match(mac) or policy.locked_for(mac):
            return
        apex = rule_apex(domain) or domain.lower().rstrip(".")
        with self._lock:
            st = self.state.get(mac)
            if st and (st.get("host") == apex or _covers(st.get("host") or "", domain) or st["slot"] == slot):
                self._touch(mac, now)
                return
            if self.on_intercept and mac in zertwechsel.cert_macs() and not (st or {}).get("shown"):
                if mac not in self._waiting:
                    self._waiting.add(mac)
                    timer = threading.Timer(CERT_FALLBACK, self.fallback, args=(ip, domain))
                    timer.daemon = True
                    timer.start()
                return
        self._switch(mac, slot, domain, apex, now, shown=False)

    def fallback(self, ip: str, domain: str) -> None:
        """Wechselseite kam nicht – still wechseln und Gerät aus der Umleitung nehmen."""
        now = time.time()
        mac = self._mac_for(ip, now)
        slot = self.match(domain, now)
        apex = rule_apex(domain) or domain.lower().rstrip(".")
        with self._lock:
            self._waiting.discard(mac)
            st = self.state.get(mac)
            if not slot or (st and (st.get("host") == apex or st["slot"] == slot)):
                return  # Wechselseite hat übernommen
        self._switch(mac, slot, domain, apex, now, shown=True)

    def _switch(self, mac: str, slot: str, domain: str, apex: str, now: float, shown: bool) -> None:
        with self._lock:
            st = self.state.get(mac)
            if slot == AUTO:
                if mac in self._picking:
                    return
                self._picking.add(mac)
            else:
                if not warp.may_use(mac, slot):
                    return
                if st is None and db.device_warp().get(mac, "") == slot:
                    return  # schon von Hand auf diesem Profil – nicht zurückstellen
                db.set_device_warp(mac, slot)
                self.state[mac] = {"slot": slot, "seen": now, "since": now, "host": apex, "shown": shown}
                self._save()
                return
        # „auto“: VPN-Suche dauert (Probe übers VPN) – nicht im DNS-Log-Thread warten.
        if self.async_auto:
            threading.Thread(target=self._auto_switch, args=(mac, domain, apex, shown), daemon=True).start()
        else:
            self._auto_switch(mac, domain, apex, shown)

    def _auto_switch(self, mac: str, host: str, apex: str, shown: bool = False) -> None:
        try:
            now = time.time()
            host = apex or host.lower().rstrip(".")  # die Hauptseite testen, Unter-/CDN-Adressen haben oft keine Startseite
            picked = None
            current = db.device_warp().get(mac, "")
            if (
                self.settings and current and warp.kind(current) == "vpn" and warp.may_use(mac, current)
                and warp.read_status(self.settings, current, now).connected and self._probe(current, host, now)
            ):
                picked = current
            if not picked:
                picked = self._pick_vpn(mac, host, now)
            if not picked:
                print(f"cloudy: Auto-Wechsel: kein VPN erreicht {host}", flush=True)
                with self._lock:
                    if len(self._no_vpn) > 500:
                        self._no_vpn.clear()
                    self._no_vpn[(mac, apex)] = time.time()
                return
            with self._lock:
                self._no_vpn.pop((mac, apex), None)
                db.set_device_warp(mac, picked)
                # War das Gerät schon auf diesem VPN, muss nichts mehr einschwingen.
                since = now - SETTLE if picked == current else now
                self.state[mac] = {"slot": picked, "seen": now, "since": since, "host": apex, "shown": shown}
                self._save()
                changed = set_pass(apex, picked)
            if changed:
                self._dns_changed()
        except Exception as exc:  # noqa: BLE001 – darf den DNS-Log nie stoppen
            print(f"cloudy: Auto-Wechsel: {exc}", flush=True)
        finally:
            with self._lock:
                self._picking.discard(mac)

    def _pick_vpn(self, mac: str, host: str, now: float) -> str | None:
        settings = self.settings
        if not settings:
            return None
        for slot in vpn_slots(settings):
            if not warp.may_use(mac, slot):
                continue
            if not warp.read_status(settings, slot, now).connected:
                continue
            if self._probe(slot, host, now):
                return slot
        return None

    def status_for(self, ip: str, host: str, now: float | None = None) -> dict:
        """state: none | blocked | wait | ready | probing | none_found"""
        now = now if now is not None else time.time()
        host = (host or "").lower().rstrip(".")
        slot = self.match(host, now)
        if not slot:
            return {"state": "none", "slot": ""}
        mac = self._mac_for(ip, now)
        if not mac:
            return {"state": "blocked", "slot": "" if slot == AUTO else slot}
        if policy.locked_for(mac):
            return {"state": "blocked", "slot": "" if slot == AUTO else slot}

        with self._lock:
            st = self.state.get(mac)
            apex = rule_apex(host)

            # „auto“: Ist das gewählte VPN weg, neu suchen – das nächste Profil übernimmt.
            if (
                st and st.get("host") == apex and slot == AUTO and not st.get("normal") and self.settings
                and st["slot"] not in ("", warp.DIRECT, warp.STANDARD)
                and not warp.read_status(self.settings, st["slot"], now).connected
            ):
                del self.state[mac]
                self._save()
                st = None

            # Schon durchgelassen (VPN oder normales Internet) für diese Seite
            if st and st.get("host") == apex:
                self._touch(mac, now)
                chosen = st["slot"]
                since = st.get("since", 0)
                if st.get("normal"):
                    route = chosen if chosen and chosen not in ("", warp.STANDARD) else warp.DIRECT
                    if route == "":
                        route = warp.DIRECT
                    if set_pass(apex, route):
                        self._dns_changed()
                    return {"state": "ready", "slot": route, "normal": True}
                if now - since < SETTLE:
                    return {"state": "wait", "slot": chosen}
                if self.settings and chosen not in ("", warp.DIRECT, warp.STANDARD, AUTO):
                    if not warp.read_status(self.settings, chosen, now).connected:
                        return {"state": "wait", "slot": chosen}
                if set_pass(apex, chosen):
                    self._dns_changed()
                return {"state": "ready", "slot": chosen}

            if slot == AUTO:
                if mac in self._picking:
                    return {"state": "probing", "slot": ""}
                failed = self._no_vpn.get((mac, apex))
                if failed and now - failed < PROBE_TTL:
                    return {"state": "none_found", "slot": ""}
                self._picking.add(mac)
                search = True
            else:
                search = False

        if search:
            # Die Suche dauert mehrere Sekunden: außerhalb der Sperre, die Wechselseite fragt weiter nach.
            if self.async_auto:
                threading.Thread(target=self._auto_switch, args=(mac, host, apex, False), daemon=True).start()
                return {"state": "probing", "slot": ""}
            self._auto_switch(mac, host, apex, False)
            st = self.state.get(mac)
            if st and st.get("host") == apex:
                return {"state": "wait", "slot": st["slot"]}
            return {"state": "none_found", "slot": ""}

        with self._lock:
            st = self.state.get(mac)
            # Festes Profil – Wechsel erst hier (Wechselseite pollt), nicht schon bei DNS
            if not warp.may_use(mac, slot):
                return {"state": "blocked", "slot": slot}
            if not st or st.get("host") != apex or st.get("slot") != slot:
                db.set_device_warp(mac, slot)
                self.state[mac] = {"slot": slot, "seen": now, "since": now, "host": apex}
                self._save()
                return {"state": "wait", "slot": slot}
            since = st.get("since", 0)
            if now - since < SETTLE:
                return {"state": "wait", "slot": slot}
            if self.settings and not warp.read_status(self.settings, slot, now).connected:
                return {"state": "wait", "slot": slot}
            if set_pass(apex, slot):
                self._dns_changed()
            return {"state": "ready", "slot": slot}

    def allow_normal(self, ip: str, host: str, now: float | None = None) -> dict:
        """Nach Bestätigung: Gerät auf „normales Internet“ und Seite durchlassen."""
        now = now if now is not None else time.time()
        host = (host or "").lower().rstrip(".")
        if not self.match(host, now):
            return {"state": "none", "slot": ""}
        mac = self._mac_for(ip, now)
        if not mac or not warp.MAC_RE.match(mac):
            return {"state": "blocked", "slot": ""}
        if policy.locked_for(mac):
            return {"state": "blocked", "slot": ""}
        target = normal_slot()
        with self._lock:
            if target and target != warp.DIRECT:
                if not warp.may_use(mac, target):
                    return {"state": "blocked", "slot": target}
                db.set_device_warp(mac, target)
                route = target
            elif target == warp.DIRECT:
                db.set_device_warp(mac, warp.DIRECT)
                route = warp.DIRECT
            else:
                db.set_device_warp(mac, None)  # Standard / Fritzbox
                route = warp.DIRECT
            apex = rule_apex(host)
            if set_pass(apex, route):
                self._dns_changed()
            self.state[mac] = {
                "slot": target or "",
                "seen": now,
                "since": now - SETTLE,
                "host": apex,
                "normal": True,
            }
            self._save()
            return {"state": "ready", "slot": route, "normal": True}

    def tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        limit = minutes() * 60
        with self._lock:
            if not self.state and not pass_map():
                return
            current = db.device_warp()
            changed = False
            dns = False
            active_hosts = set()
            for mac, st in list(self.state.items()):
                want = st["slot"]
                have = current.get(mac, "")
                # Standard speichern wir als ""
                if want in ("", warp.STANDARD):
                    match_ok = mac not in current or have == ""
                elif want == warp.DIRECT:
                    match_ok = have == warp.DIRECT
                else:
                    match_ok = have == want
                if not match_ok and not st.get("normal"):
                    # Bei normal: have kann "" sein während slot ""
                    del self.state[mac]
                    changed = True
                    continue
                if not match_ok and st.get("normal"):
                    # manuell umgestellt
                    del self.state[mac]
                    changed = True
                    continue
                if now - st["seen"] >= limit:
                    db.set_device_warp(mac, None)
                    del self.state[mac]
                    changed = True
                else:
                    if st.get("host"):
                        active_hosts.add(st["host"])
            # Pass-Einträge ohne aktives Gerät zurücknehmen
            for domain in list(pass_map()):
                if domain not in active_hosts and not any(
                    _covers(domain, h) or _covers(h, domain) for h in active_hosts
                ):
                    if clear_pass(domain):
                        dns = True
            if changed:
                self._save()
            if dns:
                self._dns_changed()

    def _loop(self) -> None:
        while not self._stopping.wait(self.tick_seconds):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 – darf cloudy nie stoppen
                print(f"cloudy: Fehler beim Auto-Wechsel: {exc}", flush=True)

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stopping.set()
