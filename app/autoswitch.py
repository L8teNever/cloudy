"""Auto-Wechsel: Ruft ein Gerät eine bestimmte Seite auf (z. B. aniworld.com), wechselt das ganze Gerät automatisch auf ein
WARP-/VPN-Profil. Fragt es X Minuten lang keine dieser Seiten mehr ab, geht es zurück auf das, was vorher eingestellt war
(eigenes Profil oder „Standard“).

Auslöser sind die DNS-Anfragen des Geräts (der Collector meldet jede Anfrage). Zugehörige Domains eines Dienstes
(siehe dienste.py) zählen mit. Gruppen mit festgelegtem Profil und manuelle Änderungen werden respektiert.
"""

from __future__ import annotations

import json
import threading
import time

from . import db, dienste, policy, warp

RULES_KEY = "auto_rules"  # JSON: {"domain": "slot", …}
MINUTES_KEY = "auto_minutes"
STATE_KEY = "auto_state"  # JSON: {mac: {"slot", "prev", "seen"}} – übersteht einen Neustart
DEFAULT_MINUTES = 5
MIN_MINUTES, MAX_MINUTES = 1, 240
TICK = 15.0  # Sekunden zwischen den Prüfungen auf „lange nichts mehr“
RULES_TTL = 10.0  # Regeln so lange im Speicher halten
SEEN_STEP = 5  # „zuletzt gesehen“ höchstens alle paar Sekunden neu speichern


# --------------------------------------------------------------------------- Einstellungen


def load_rules() -> dict[str, str]:
    try:
        raw = json.loads(db.get_setting(RULES_KEY) or "{}")
    except ValueError:
        return {}
    return {str(d): str(s) for d, s in raw.items()} if isinstance(raw, dict) else {}


def set_rule(domain: str, slot: str) -> None:
    rules = load_rules()
    rules[domain] = slot
    db.set_setting(RULES_KEY, json.dumps(rules, sort_keys=True))


def remove_rule(domain: str) -> None:
    rules = load_rules()
    rules.pop(domain.strip().lower(), None)
    db.set_setting(RULES_KEY, json.dumps(rules, sort_keys=True) if rules else "")


def purge_slot(slot: str) -> None:
    """Regeln für ein gelöschtes Profil entfernen."""
    rules = {d: s for d, s in load_rules().items() if s != slot}
    db.set_setting(RULES_KEY, json.dumps(rules, sort_keys=True) if rules else "")


def minutes() -> int:
    try:
        return min(MAX_MINUTES, max(MIN_MINUTES, int(db.get_setting(MINUTES_KEY) or DEFAULT_MINUTES)))
    except ValueError:
        return DEFAULT_MINUTES


def set_minutes(value: int) -> int:
    value = min(MAX_MINUTES, max(MIN_MINUTES, int(value)))
    db.set_setting(MINUTES_KEY, str(value))
    return value


def _covers(rule: str, domain: str) -> bool:
    return domain == rule or domain.endswith("." + rule)


# --------------------------------------------------------------------------- Laufzeit


class AutoSwitcher:
    def __init__(self, tick: float = TICK):
        self.tick_seconds = tick
        self._lock = threading.Lock()
        self._rules: list[tuple[str, str]] = []  # (Domain, Slot) inkl. zugehöriger Domains
        self._rules_at = 0.0
        self._ip_mac: dict[str, tuple[float, str]] = {}
        self._stopping = threading.Event()
        try:
            state = json.loads(db.get_setting(STATE_KEY) or "{}")
        except ValueError:
            state = {}
        self.state: dict[str, dict] = state if isinstance(state, dict) else {}

    # -- Regeln
    def _current_rules(self, now: float) -> list[tuple[str, str]]:
        if now - self._rules_at > RULES_TTL:
            expanded: list[tuple[str, str]] = []
            for domain, slot in load_rules().items():
                expanded.append((domain, slot))
                expanded += [(extra, slot) for extra in dienste.companions(domain)]
            self._rules = expanded
            self._rules_at = now
        return self._rules

    def match(self, domain: str, now: float | None = None) -> str:
        """Profil, auf das diese Seite wechselt – oder ""."""
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

    # -- Anfragen
    def on_query(self, ip: str, domain: str, now: float | None = None) -> None:
        """Wird für jede DNS-Anfrage aufgerufen (muss schnell sein)."""
        now = now if now is not None else time.time()
        slot = self.match(domain, now)
        if not slot:
            return
        mac = self._mac_for(ip, now)
        if not mac or not warp.MAC_RE.match(mac):
            return
        with self._lock:
            st = self.state.get(mac)
            if st and st["slot"] == slot:
                if now - st["seen"] >= SEEN_STEP:
                    st["seen"] = now
                    self._save()
                return
            if policy.locked_for(mac):
                return  # Gruppe legt das Profil fest
            current = db.device_warp().get(mac, "")
            if st is None and current == slot:
                return  # steht ohnehin schon so (vom Admin) – nicht anfassen, nicht zurückwechseln
            if not warp.may_use(mac, slot):
                return
            prev = st["prev"] if st else current  # beim Wechsel zwischen zwei Regeln den Ursprung behalten
            db.set_device_warp(mac, slot)
            self.state[mac] = {"slot": slot, "prev": prev, "seen": now}
            self._save()

    # -- Zurückwechseln
    def tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        limit = minutes() * 60
        with self._lock:
            if not self.state:
                return
            current = db.device_warp()
            changed = False
            for mac, st in list(self.state.items()):
                if current.get(mac, "") != st["slot"]:
                    del self.state[mac]  # jemand hat das Profil inzwischen selbst geändert – in Ruhe lassen
                    changed = True
                elif now - st["seen"] >= limit:
                    db.set_device_warp(mac, st.get("prev") or None)
                    del self.state[mac]
                    changed = True
            if changed:
                self._save()

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
