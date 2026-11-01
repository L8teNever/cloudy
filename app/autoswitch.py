"""Auto-Wechsel: Ruft ein Gerät eine bestimmte Seite auf (z. B. aniworld.com), wechselt das ganze Gerät automatisch
aufs VPN.

Zwei Arten von Regeln:
- festes Profil (warp1, vpn2 …): wie bisher – Gerät wechselt auf dieses Profil
- „auto“: nur echte VPN-Profile (vpn1, vpn2 …), die gerade verbunden sind; cloudy prüft der Reihe nach, ob die
  Seite darüber erreichbar ist, und nimmt das erste passende. Schlägt keines an, kann der Nutzer bestätigt über
  „normales Internet“ (einstellbares Profil, z. B. Fritzbox/WARP) weitergehen.

Lokal (ohne Cloudflare): Die Domains zeigen per DNS auf den Pi und landen auf der VPN-Wechselseite. Sobald ein
Profil greift, wird die DNS-Umleitung für diese Domain ausgesetzt und eine Seiten-Regel gesetzt, damit der
Browser die echte Seite über das VPN erreicht. Nach der Wartezeit ohne Aufruf: zurück auf Standard.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time

from . import db, dienste, policy, warp

RULES_KEY = "auto_rules"  # JSON: {"domain": "slot"| "auto", …}
MINUTES_KEY = "auto_minutes"
STATE_KEY = "auto_state"  # JSON: {mac: {"slot", "seen", "since", "host"?}}
NORMAL_KEY = "auto_normal_slot"  # "" = Standard (Fritzbox), "direct", oder Slot
PASS_KEY = "auto_pass"  # JSON: {domain: {"slot", "prev"}} – DNS-Hijack aus, site_route aktiv
AUTO = "auto"
DEFAULT_MINUTES = 5
MIN_MINUTES, MAX_MINUTES = 1, 240
TICK = 15.0
RULES_TTL = 10.0
SEEN_STEP = 5
SETTLE = 5.0  # kurz warten, bis Markierung/DNS greifen; die Wechselseite zeigt sowieso ≥5 s
PROBE_TTL = 45.0  # Ergebnis „Seite über Slot erreichbar?“ so lange merken
PROBE_TIMEOUT = 8
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
    """DNS-Hijack für domain aussetzen und Seiten-Regel setzen. True wenn sich etwas geändert hat."""
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


def hijack_domains() -> list[str]:
    """Domains, die lokal auf den Pi zeigen (Wechselseite) – nicht die mit aktivem Durchlass."""
    passed = set(pass_map())
    return [d for d in managed_domains() if d not in passed]


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
    """DNS-Durchlass aktiv (kein Hijack mehr) für diesen Host?"""
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


def container_pid(slot: str) -> int | None:
    name = f"cloudy-{slot}"
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Pid}}", name],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        pid = int((out.stdout or "").strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def probe_via(settings, slot: str, host: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """Erreicht man https://host/ über das Netzwerk dieses VPN-Containers?"""
    host = (host or "").lower().rstrip(".")
    if not host or not slot:
        return False
    pid = container_pid(slot)
    if not pid:
        return False
    # Nur Netz-Namespace des Containers – Python bleibt das vom Host (cloudy-Image).
    code = (
        "import sys,urllib.request\n"
        f"req=urllib.request.Request('https://{host}/',method='HEAD')\n"
        "try:\n"
        f"  urllib.request.urlopen(req,timeout={int(timeout)})\n"
        "  sys.exit(0)\n"
        "except Exception:\n"
        "  try:\n"
        f"    urllib.request.urlopen(urllib.request.Request('https://{host}/'),timeout={int(timeout)})\n"
        "    sys.exit(0)\n"
        "  except Exception:\n"
        "    sys.exit(1)\n"
    )
    try:
        r = subprocess.run(
            ["nsenter", "-t", str(pid), "-n", "python3", "-c", code],
            capture_output=True, timeout=timeout + 4,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# --------------------------------------------------------------------------- Laufzeit


class AutoSwitcher:
    def __init__(self, tick: float = TICK, settings=None):
        self.tick_seconds = tick
        self.settings = settings
        self.on_dns_change = None  # wird gesetzt, wenn DNS-Hijack/Seiten-Regel neu geschrieben werden muss
        self._lock = threading.RLock()
        self._rules: list[tuple[str, str]] = []
        self._rules_at = 0.0
        self._rules_gen = -1
        self._ip_mac: dict[str, tuple[float, str]] = {}
        self._probe_cache: dict[tuple[str, str], tuple[float, bool]] = {}
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

    def _dns_changed(self) -> None:
        cb = self.on_dns_change
        if cb:
            try:
                cb()
                # Kurz warten, damit dnsmasq die Hijack-Zeilen weg hat, bevor der Browser neu auflöst.
                time.sleep(0.4)
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
        """DNS-Anfrage: festes Profil sofort umschalten; „auto“ erst auf der Wechselseite (Probe)."""
        now = now if now is not None else time.time()
        slot = self.match(domain, now)
        if not slot:
            return
        mac = self._mac_for(ip, now)
        if not mac or not warp.MAC_RE.match(mac):
            return
        with self._lock:
            st = self.state.get(mac)
            if st and (st["slot"] == slot or slot == AUTO):
                self._touch(mac, now)
                return
            if slot == AUTO:
                return  # Auswahl passiert in status_for / allow_normal
            if policy.locked_for(mac):
                return
            current = db.device_warp().get(mac, "")
            if st is None and current == slot:
                return
            if not warp.may_use(mac, slot):
                return
            # VPN schon anwärmen, DNS-Hijack aber behalten – sonst lädt die echte Seite
            # ohne Wechselseite (zweite DNS-Antwort ohne address=/…/Pi).
            db.set_device_warp(mac, slot)
            apex = rule_apex(domain) or domain.lower().rstrip(".")
            self.state[mac] = {"slot": slot, "seen": now, "since": now, "host": apex}
            self._save()

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
                # VPN schon an und Seite erreichbar → ready (Wechselseite zeigt trotzdem 5-s-Countdown)
                current = db.device_warp().get(mac, "")
                if current and warp.kind(current) == "vpn" and warp.may_use(mac, current):
                    if self.settings and warp.read_status(self.settings, current, now).connected:
                        if self._probe(current, host, now):
                            self.state[mac] = {
                                "slot": current, "seen": now, "since": now - SETTLE, "host": apex,
                            }
                            self._save()
                            if set_pass(apex, current):
                                self._dns_changed()
                            return {"state": "ready", "slot": current}

                picked = self._pick_vpn(mac, host, now)
                if not picked:
                    return {"state": "none_found", "slot": ""}
                db.set_device_warp(mac, picked)
                self.state[mac] = {"slot": picked, "seen": now, "since": now, "host": apex}
                self._save()
                return {"state": "wait", "slot": picked}

            # Festes Profil – Durchlass erst bei ready (nach SETTLE), nicht schon bei wait
            self.on_query(ip, host, now)
            if db.device_warp().get(mac, "") != slot:
                return {"state": "blocked", "slot": slot}
            since = self.state.get(mac, {}).get("since", 0)
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
