"""Auslastung des Servers messen und entscheiden, wann Hintergrundarbeit laufen darf.

Hintergrundarbeit = Videos für Fernseher umpacken und die Kopie auf die Puffer-SSD.
Regel (Freigaben → Dienst):
  "immer"        sofort, aber mit niedrigster Priorität
  "leerlauf"     nur, wenn niemand Dateien überträgt (Netzlaufwerk/Fernseher ruhig); sonst Pause
  "zeitfenster"  nur zwischen fenster_von und fenster_bis (z. B. nachts)

Gemessen wird alle paar Sekunden aus /proc (der Container läuft privilegiert im Host-Netz, sieht also den
ganzen Server): CPU, Arbeitsspeicher, Netzwerk, Auslastung jeder Platte und wie viel Samba/miniDLNA übertragen.
"""
import collections
import os
import shutil
import signal
import threading
import time
from datetime import datetime
from pathlib import Path

INTERVALL = 3.0
RUHIG_MB_S = 1.0  # darunter gilt das Netzlaufwerk als ruhig
RUHIG_SEKUNDEN = 60  # so lange muss es ruhig sein, bevor Hintergrundarbeit (wieder) startet
PUFFER_MAX_WARTEN = 2 * 3600  # Sicherheitskopie wartet höchstens so lange auf Ruhe

_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_prev: dict = {}
_werte: dict = {}
_uebertrag = collections.deque(maxlen=int(RUHIG_SEKUNDEN / INTERVALL) + 1)  # (zeit, MB/s Samba+DLNA)
_pausiert_pid: int | None = None


def niedrig(cmd: list[str]) -> list[str]:
    """Befehl mit niedrigster CPU- und Platten-Priorität starten (Explorer & Co. haben Vorrang)."""
    pre: list[str] = []
    if shutil.which("nice"):
        pre += ["nice", "-n", "19"]
    if shutil.which("ionice"):
        pre += ["ionice", "-c", "3"]
    return pre + cmd


# ---------- Messen ----------

def _cpu() -> tuple[int, int]:
    with open("/proc/stat") as fh:
        teile = [int(x) for x in fh.readline().split()[1:]]
    idle = teile[3] + (teile[4] if len(teile) > 4 else 0)
    return sum(teile), idle


def _ram() -> tuple[int, int]:
    info = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0]) * 1024
    return info.get("MemTotal", 0), info.get("MemAvailable", 0)


def _netz() -> int:
    """Bytes (rein+raus) aller echten Netzwerkkarten."""
    total = 0
    for dev in Path("/sys/class/net").iterdir():
        if not (dev / "device").exists():
            continue  # virtuell (docker, veth, Tunnel, lo)
        try:
            total += int((dev / "statistics" / "rx_bytes").read_text()) + int((dev / "statistics" / "tx_bytes").read_text())
        except (OSError, ValueError):
            pass
    return total


def _platten() -> dict[str, tuple[int, str]]:
    """{sda: (io_ticks_ms, Modell)} für echte Platten."""
    out = {}
    try:
        with open("/proc/diskstats") as fh:
            stats = {p[2]: int(p[12]) for p in (line.split() for line in fh) if len(p) > 12}
    except OSError:
        return out
    for name, ticks in stats.items():
        if not (Path("/sys/block") / name / "device").exists():
            continue  # Partitionen, loop, dm
        try:
            modell = (Path("/sys/block") / name / "device" / "model").read_text().strip()
        except OSError:
            modell = ""
        out[name] = (ticks, modell)
    return out


def _dienst_bytes() -> int:
    """Gelesene+geschriebene Bytes aller smbd- und minidlnad-Prozesse (= Übertragungen an Geräte)."""
    total = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            name = Path(f"/proc/{pid}/comm").read_text().strip()
            if not (name.startswith("smbd") or name == "minidlnad"):
                continue
            for line in Path(f"/proc/{pid}/io").read_text().splitlines():
                k, v = line.split(":")
                if k in ("rchar", "wchar"):
                    total += int(v)
        except (OSError, ValueError):
            continue
    return total


def messen() -> dict:
    """Einmal messen (Raten aus dem Abstand zur letzten Messung)."""
    jetzt = time.monotonic()
    try:
        cpu_total, cpu_idle = _cpu()
    except OSError:
        cpu_total = cpu_idle = 0
    try:
        ram_total, ram_frei = _ram()
    except (OSError, ValueError):
        ram_total = ram_frei = 0
    netz = _netz()
    platten = _platten()
    dienst = _dienst_bytes()
    with _lock:
        p = _prev.copy()
        _prev.update(t=jetzt, cpu_total=cpu_total, cpu_idle=cpu_idle, netz=netz, platten={k: v[0] for k, v in platten.items()}, dienst=dienst)
    dt = jetzt - p.get("t", jetzt)
    werte = {
        "zeit": time.time(),
        "cpu": 0.0, "ram_total": ram_total, "ram_benutzt": max(0, ram_total - ram_frei),
        "netz_mb_s": 0.0, "dienst_mb_s": 0.0, "platten": [],
        "kerne": os.cpu_count() or 1, "load": os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0,
    }
    if dt > 0:
        d_total = cpu_total - p.get("cpu_total", cpu_total)
        d_idle = cpu_idle - p.get("cpu_idle", cpu_idle)
        werte["cpu"] = round(max(0.0, min(100.0, 100.0 * (1 - d_idle / d_total))) if d_total > 0 else 0.0, 1)
        werte["netz_mb_s"] = round(max(0, netz - p.get("netz", netz)) / dt / 1e6, 1)
        werte["dienst_mb_s"] = round(max(0, dienst - p.get("dienst", dienst)) / dt / 1e6, 1)
        alt = p.get("platten", {})
        for name, (ticks, modell) in sorted(platten.items()):
            busy = max(0, ticks - alt.get(name, ticks)) / (dt * 1000) * 100
            werte["platten"].append({"name": name, "modell": modell, "busy": round(min(100.0, busy))})
    return werte


# ---------- Regel ----------

def _hm(text: str) -> int:
    try:
        h, m = text.split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return 0


def im_fenster(von: str, bis: str, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    t, a, b = now.hour * 60 + now.minute, _hm(von), _hm(bis)
    if a == b:
        return True
    return a <= t < b if a < b else (t >= a or t < b)


def ruhig() -> bool:
    """Seit RUHIG_SEKUNDEN überträgt niemand nennenswert über Netzlaufwerk/Fernseher."""
    with _lock:
        werte = list(_uebertrag)
    if not werte:
        return True
    return all(mb < RUHIG_MB_S for _, mb in werte)


def erlaubt(e=None, now: datetime | None = None) -> tuple[bool, str]:
    """(darf Hintergrundarbeit jetzt laufen?, Grund wenn nicht)."""
    if e is None:
        from . import freigaben
        e = freigaben.Einstellungen.load()
    regel = getattr(e, "hintergrund", "immer")
    if regel == "zeitfenster" and not im_fenster(e.fenster_von, e.fenster_bis, now):
        return False, f"wartet aufs Zeitfenster {e.fenster_von}–{e.fenster_bis} Uhr"
    if regel == "leerlauf" and not ruhig():
        return False, "pausiert – gerade werden Dateien übertragen"
    return True, ""


def puffer_erlaubt(e, letzte_kopie: float) -> bool:
    """Sicherheitskopie: nur auf Ruhe warten (nicht aufs Zeitfenster), höchstens PUFFER_MAX_WARTEN."""
    if getattr(e, "hintergrund", "immer") == "immer" or ruhig():
        return True
    return time.time() - letzte_kopie > PUFFER_MAX_WARTEN


# ---------- Hintergrund: messen und laufendes Umpacken pausieren ----------

def _ffmpeg_steuern() -> None:
    """Läuft gerade Umpacken aus dem Scan und ist es nicht erlaubt -> anhalten (SIGSTOP), sonst weiter."""
    global _pausiert_pid
    from . import medien

    pid, art = medien.laufend()
    if pid is None:
        _pausiert_pid = None
        medien.set_pausiert("")
        return
    ok, grund = erlaubt() if art == "scan" else (True, "")
    try:
        if not ok and _pausiert_pid != pid:
            os.killpg(pid, signal.SIGSTOP)
            _pausiert_pid = pid
            medien.set_pausiert(grund)
        elif ok and _pausiert_pid == pid:
            os.killpg(pid, signal.SIGCONT)
            _pausiert_pid = None
            medien.set_pausiert("")
        elif not ok:
            medien.set_pausiert(grund)
    except OSError:
        _pausiert_pid = None


def _loop() -> None:
    while not _stop.is_set():
        try:
            w = messen()
            with _lock:
                _werte.clear()
                _werte.update(w)
                _uebertrag.append((w["zeit"], w["dienst_mb_s"]))
            _ffmpeg_steuern()
        except Exception:  # noqa: BLE001 – Messen darf nie den Dienst stören
            pass
        _stop.wait(INTERVALL)


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="last", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()


def werte() -> dict:
    with _lock:
        return dict(_werte)
