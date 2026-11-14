"""Videos automatisch für Fernseher (DLNA) und Browser optimieren.

Viele Download-Tools speichern MP4-Videos als fragmentierte Datei (lauter moof/mdat-Blöcke
mit Gesamtdauer 0 im Header) oder legen das Inhaltsverzeichnis (moov) ans Ende.
PCs spielen das ab, aber Smart-TVs über DLNA brechen oft nach wenigen Sekunden ab.

Dieses Modul erkennt solche MP4/M4V-Dateien blitzschnell anhand der Top-Level-Boxen
und packt sie mit ``ffmpeg -c copy -movflags +faststart`` verlustfrei um – erst in
eine versteckte Zwischendatei im selben Ordner, dann atomar an den alten Platz.
"""
import collections
import os
import secrets
import shutil
import signal
import struct
import subprocess
import threading
import time
from pathlib import Path

from . import freigaben, jobs, last, pool

ENDUNGEN = {".mp4", ".m4v"}
RUHEZEIT = 15.0  # Sekunden nach letzter Änderung warten (Samba-Upload sicher fertig)
SCAN_INTERVALL = 30.0
MAX_CACHE = 5000
MAX_MOOV_CHECK = 32 * 1024 * 1024

_lock = threading.RLock()
_stop = threading.Event()
_wake = threading.Event()
_thread: threading.Thread | None = None
_proc: subprocess.Popen | None = None
_sofort: collections.deque[Path] = collections.deque()
_scan_jetzt = True
_geprueft: dict[str, tuple[int, int, str]] = {}
_ffmpeg_fehlt_gemeldet = False
_state: dict = {"aktuell": "", "optimiert": 0, "fehler": "", "art": "", "pausiert": "", "wartet": ""}


def _read_box(fh, offset: int, limit: int) -> tuple[int, bytes, int] | None:
    """Liest Größe, Typ und Header-Länge einer MP4-Box bei 'offset'."""
    if offset + 8 > limit:
        return None
    fh.seek(offset)
    hdr = fh.read(8)
    if len(hdr) < 8:
        return None
    size, box_type = struct.unpack(">I4s", hdr)
    hdr_len = 8
    if size == 1:
        if offset + 16 > limit:
            return None
        ext = fh.read(8)
        if len(ext) < 8:
            return None
        size = struct.unpack(">Q", ext)[0]
        hdr_len = 16
    elif size == 0:
        size = limit - offset
    if size < hdr_len or offset + size > limit:
        return None
    return size, box_type, hdr_len


def _hat_mvex(fh, moov_offset: int, moov_size: int, hdr_len: int) -> bool:
    """Prüft innerhalb eines moov-Atoms, ob ein mvex-Block (fragmentiertes MP4) drinsteckt."""
    pos = moov_offset + hdr_len
    end = moov_offset + moov_size
    for _ in range(1000):
        if pos >= end:
            break
        box = _read_box(fh, pos, end)
        if not box:
            break
        c_size, c_type, _ = box
        if c_type == b"mvex":
            return True
        pos += c_size
    return False


def analyse_mp4(path: Path) -> str:
    """Prüft die Top-Level-Boxen einer MP4-Datei ohne sie komplett einzulesen.

    Rückgabe:
      'ok'          – normales MP4 mit moov vor mdat (fernseh- und browser-tauglich)
      'fragmented'  – fragmentiertes MP4 (moof oder mvex) → umpacken nötig
      'moov_hinten' – moov liegt hinter mdat (faststart fehlt) → umpacken sinnvoll
      'unbekannt'   – keine gültige/vollständige MP4-Datei → nicht anfassen
    """
    try:
        with path.open("rb") as fh:
            file_size = os.fstat(fh.fileno()).st_size
            if file_size < 16:
                return "unbekannt"
            offset = 0
            saw_ftyp = False
            moov_pos: int | None = None
            mdat_pos: int | None = None
            moov_has_mvex = False
            for _ in range(100_000):
                if offset >= file_size:
                    break
                box = _read_box(fh, offset, file_size)
                if not box:
                    return "unbekannt"
                size, box_type, hdr_len = box
                if offset == 0 and box_type not in (b"ftyp", b"free", b"skip", b"wide"):
                    return "unbekannt"
                if box_type == b"ftyp":
                    saw_ftyp = True
                elif box_type == b"moof":
                    return "fragmented" if saw_ftyp else "unbekannt"
                elif box_type == b"moov":
                    if moov_pos is None:
                        moov_pos = offset
                    if size <= MAX_MOOV_CHECK and _hat_mvex(fh, offset, size, hdr_len):
                        moov_has_mvex = True
                elif box_type == b"mdat":
                    if mdat_pos is None:
                        mdat_pos = offset
                offset += size
    except OSError:
        return "unbekannt"
    if not saw_ftyp or moov_pos is None or mdat_pos is None:
        return "unbekannt"
    if moov_has_mvex:
        return "fragmented"
    if moov_pos > mdat_pos:
        return "moov_hinten"
    return "ok"


def _ist_offen(path: Path) -> bool:
    """True, wenn gerade ein anderer Prozess (z. B. Samba oder Browser) die Datei offen hat."""
    try:
        target_stat = path.stat()
    except OSError:
        return False
    target_str = str(path)
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = target_str
    pool_prefix = str(pool.paths()["root"])
    my_pid = str(os.getpid())
    try:
        pids = os.listdir("/proc")
    except OSError:
        return False
    for pid in pids:
        if not pid.isdigit() or pid == my_pid:
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(f"{fd_dir}/{fd}")
                if link == target_str or link == resolved:
                    return True
                if pool_prefix and link.startswith(pool_prefix):
                    st = os.stat(f"{fd_dir}/{fd}")
                    if st.st_ino == target_stat.st_ino and st.st_dev == target_stat.st_dev:
                        return True
            except OSError:
                continue
    return False


def _cache_set(key: str, val: tuple[int, int, str]) -> None:
    with _lock:
        if len(_geprueft) >= MAX_CACHE:
            _geprueft.clear()
        _geprueft[key] = val


def _besitzer(path: Path) -> None:
    try:
        os.chown(path, int(os.environ.get("SPEICHER_UID", "1000")), int(os.environ.get("SPEICHER_GID", "1000")))
    except (OSError, ValueError):
        pass


def _run_ffmpeg(ffmpeg: str, src: Path, dst: Path) -> tuple[bool, str]:
    """Packt MP4 verlustfrei um. Erst komplett (-map 0), bei Exoten-Streams nur Video+Audio."""
    global _proc
    last_err = ""
    for map_args in (["-map", "0"], ["-map", "0:v?", "-map", "0:a?"]):
        if _stop.is_set():
            return False, "gestoppt"
        cmd = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src), *map_args, "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(dst),
        ]
        try:
            with _lock:
                _proc = subprocess.Popen(last.niedrig(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                         start_new_session=True)
                proc = _proc
            # Warten; Zeit, in der das Umpacken pausiert ist (SIGSTOP), zählt nicht mit.
            gelaufen = 0.0
            while True:
                try:
                    proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    if not _state["pausiert"]:
                        gelaufen += 5
                    if gelaufen > 3600:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except OSError:
                            proc.kill()
                        proc.wait()
                        dst.unlink(missing_ok=True)
                        return False, "ffmpeg hat zu lange gebraucht"
            stderr = proc.stderr.read() if proc.stderr else ""
        except OSError as exc:
            dst.unlink(missing_ok=True)
            return False, str(exc)
        finally:
            with _lock:
                _proc = None
        if proc.returncode == 0:
            return True, ""
        last_err = (stderr or "").strip().splitlines()[-1][:200] if stderr and stderr.strip() else f"Code {proc.returncode}"
        dst.unlink(missing_ok=True)
    return False, last_err


def optimieren(path: Path, sofort: bool = False, now: float | None = None) -> bool:
    """Prüft eine Video-Datei und packt sie bei Bedarf verlustfrei für Fernseher/Browser um.

    True, wenn die Datei erfolgreich umgepackt wurde.
    """
    global _ffmpeg_fehlt_gemeldet
    if path.suffix.lower() not in ENDUNGEN or path.name.startswith(".") or path.is_symlink():
        return False
    e = freigaben.Einstellungen.load()
    if not e.videos_optimieren:
        return False
    try:
        st = path.stat()
    except OSError:
        return False
    if not os.path.isfile(path):
        return False

    key = str(path)
    with _lock:
        cached = _geprueft.get(key)
    if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
        return False

    jetzt = time.time() if now is None else now
    age = jetzt - max(st.st_mtime, st.st_ctime)
    if not sofort and age < RUHEZEIT:
        return False
    if _ist_offen(path):
        return False

    zustand = analyse_mp4(path)
    if zustand == "ok":
        _cache_set(key, (st.st_size, st.st_mtime_ns, "ok"))
        return False
    if zustand == "unbekannt":
        if sofort or age >= 60:
            _cache_set(key, (st.st_size, st.st_mtime_ns, "unbekannt"))
        return False

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        with _lock:
            _state["fehler"] = "ffmpeg fehlt im Container"
            schon = _ffmpeg_fehlt_gemeldet
            _ffmpeg_fehlt_gemeldet = True
        if not schon:
            jobs.log(None, "Video-Optimierung übersprungen: ffmpeg fehlt im Container")
        return False

    try:
        if shutil.disk_usage(path.parent).free < st.st_size + 32 * 1024 * 1024:
            jobs.log(None, f"Video-Optimierung übersprungen (zu wenig Platz): {path.name}")
            _cache_set(key, (st.st_size, st.st_mtime_ns, "fehler"))
            return False
    except OSError:
        return False

    tmp = path.parent / f".{path.stem[:180]}.tvfix-{secrets.token_hex(4)}.mp4"
    with _lock:
        _state["aktuell"] = path.name
        _state["art"] = "sofort" if sofort else "scan"
    try:
        ok, err = _run_ffmpeg(ffmpeg, path, tmp)
        if not ok:
            if not _stop.is_set():
                with _lock:
                    _state["fehler"] = f"{path.name}: {err}"
                jobs.log(None, f"Video-Optimierung fehlgeschlagen ({path.name}): {err}")
                _cache_set(key, (st.st_size, st.st_mtime_ns, "fehler"))
            return False

        try:
            tmp_st = tmp.stat()
            now_st = path.stat()
        except OSError:
            return False
        if tmp_st.st_size < max(512, int(st.st_size * 0.5)) or analyse_mp4(tmp) != "ok":
            with _lock:
                _state["fehler"] = f"{path.name}: Umpacken brachte kein gültiges MP4"
            jobs.log(None, f"Video-Optimierung verworfen ({path.name}): Ausgabedatei ungültig")
            _cache_set(key, (st.st_size, st.st_mtime_ns, "fehler"))
            return False
        # Wurde die Datei währenddessen neu beschrieben? Dann niemals überschreiben.
        if now_st.st_size != st.st_size or now_st.st_mtime_ns != st.st_mtime_ns or _ist_offen(path):
            return False

        try:
            os.chmod(tmp, st.st_mode & 0o777)
        except OSError:
            pass
        _besitzer(tmp)
        # Erst löschen, dann umbenennen: so bekommt minidlna (inotify) sicher IN_DELETE + IN_MOVED_TO
        # und liest Videodauer und Index sauber neu ein.
        path.unlink(missing_ok=True)
        os.replace(tmp, path)
        new_st = path.stat()
        _cache_set(key, (new_st.st_size, new_st.st_mtime_ns, "ok"))
        with _lock:
            _state["optimiert"] += 1
            _state["fehler"] = ""
        jobs.log(None, f"Video für Fernseher optimiert: {path.name} ({zustand})")
        return True
    except OSError as exc:
        with _lock:
            _state["fehler"] = f"{path.name}: {exc}"
        jobs.log(None, f"Video-Optimierung fehlgeschlagen ({path.name}): {exc}")
        _cache_set(key, (st.st_size, st.st_mtime_ns, "fehler"))
        return False
    finally:
        tmp.unlink(missing_ok=True)
        with _lock:
            _state["aktuell"] = ""
            _state["art"] = ""
            _state["pausiert"] = ""


def ziel_ordner() -> list[Path]:
    """Eindeutige Wurzelordner aller Freigaben, wenn Video-Optimierung aktiv und der Pool eingehängt ist."""
    e = freigaben.Einstellungen.load()
    if not e.videos_optimieren:
        return []
    root = pool.paths()["pool"]
    if str(root) not in pool.mounts():
        return []
    kandidaten: list[Path] = []
    for f in e.freigaben:
        p = freigaben.share_path(f)
        if p == root:
            return [root]
        if p.is_dir():
            kandidaten.append(p)
    # Doppeltes Durchsuchen verschachtelter Freigaben vermeiden
    kandidaten.sort(key=lambda p: len(p.parts))
    out: list[Path] = []
    for p in kandidaten:
        if any(parent == p or parent in p.parents for parent in out):
            continue
        out.append(p)
    return out


def scan() -> int:
    """Durchsucht alle Freigaben nach nicht-faststart/fragmentierten MP4s. Gibt Anzahl der Umpackungen zurück."""
    roots = ziel_ordner()
    if not roots:
        return 0
    pool_root = pool.paths()["pool"]
    try:
        from . import mediathek_db
        temp_name = mediathek_db.Einstellungen.load().temp_ordner
    except Exception:  # noqa: BLE001
        temp_name = "Jetzt schauen"
    count = 0
    for root in roots:
        stack = [root]
        while stack and not _stop.is_set():
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = list(it)
            except OSError:
                continue
            for entry in entries:
                if _stop.is_set():
                    break
                if entry.name.startswith("."):
                    continue
                if current == pool_root and entry.name in (".mergerfs", "wiederhergestellt", temp_name):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in ENDUNGEN:
                        ok, grund = last.erlaubt()
                        if not ok:
                            with _lock:
                                _state["wartet"] = grund
                            return count  # später weiter (Scan läuft regelmäßig)
                        if optimieren(Path(entry.path), sofort=False):
                            count += 1
                except OSError:
                    continue
    return count


def anmelden(path: Path) -> None:
    """Neue Datei nach Browser-Upload sofort zur Prüfung vormerken."""
    if path.suffix.lower() not in ENDUNGEN:
        return
    if not freigaben.Einstellungen.load().videos_optimieren:
        return
    with _lock:
        if path not in _sofort:
            _sofort.append(path)
    _wake.set()


def scan_anstossen() -> None:
    global _scan_jetzt
    with _lock:
        _scan_jetzt = True
    _wake.set()


def _worker() -> None:
    global _scan_jetzt
    letzter_scan = 0.0
    while not _stop.is_set():
        while not _stop.is_set():
            with _lock:
                pfad = _sofort.popleft() if _sofort else None
            if pfad is None:
                break
            try:
                optimieren(pfad, sofort=True)
            except Exception as exc:  # noqa: BLE001
                jobs.log(None, f"Video-Optimierung: {exc}")

        if not _stop.is_set():
            try:
                from . import mediathek
                mediathek.process_queue()
                mediathek.cleanup_expired()
            except Exception as exc:  # noqa: BLE001
                jobs.log(None, f"Mediathek-Worker: {exc}")

        with _lock:
            faellig = _scan_jetzt or (time.time() - letzter_scan >= SCAN_INTERVALL)
            _scan_jetzt = False
        if faellig and not _stop.is_set():
            letzter_scan = time.time()
            with _lock:
                _state["wartet"] = ""
            try:
                scan()
            except Exception as exc:  # noqa: BLE001
                jobs.log(None, f"Video-Scan: {exc}")

        _wake.wait(SCAN_INTERVALL)
        _wake.clear()


def start() -> None:
    global _thread, _scan_jetzt
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _wake.clear()
        _scan_jetzt = True
        _thread = threading.Thread(target=_worker, name="medien", daemon=True)
        _thread.start()


def stop() -> None:
    global _thread
    _stop.set()
    _wake.set()
    with _lock:
        proc = _proc
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            os.killpg(proc.pid, signal.SIGCONT)  # falls gerade pausiert
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
    t = _thread
    if t and t.is_alive():
        t.join(timeout=5)
    _thread = None


def laufend() -> tuple[int | None, str]:
    """(PID des laufenden ffmpeg, "scan"/"sofort") oder (None, "")."""
    with _lock:
        proc = _proc
        art = _state["art"]
    if proc and proc.poll() is None:
        return proc.pid, art
    return None, ""


def set_pausiert(grund: str) -> None:
    with _lock:
        _state["pausiert"] = grund


def status() -> dict:
    with _lock:
        return {
            "pausiert": _state["pausiert"],
            "wartet": _state["wartet"],
            "aktuell": _state["aktuell"],
            "optimiert": _state["optimiert"],
            "fehler": _state["fehler"],
            "ffmpeg_da": bool(shutil.which("ffmpeg")),
        }
