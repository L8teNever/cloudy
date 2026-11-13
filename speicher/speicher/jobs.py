"""Ein Auftrag zur Zeit (Kopie, Backup, Wiederherstellung) mit Protokoll für die Webseite."""
import collections
import threading
import time
import traceback
from dataclasses import dataclass, field

from .config import data_dir


@dataclass
class Job:
    name: str
    start: float = field(default_factory=time.time)
    ende: float = 0.0
    ok: bool | None = None
    zeilen: collections.deque = field(default_factory=lambda: collections.deque(maxlen=400))


_lock = threading.Lock()
_current: Job | None = None
_history: collections.deque = collections.deque(maxlen=20)


def log(job: Job | None, text: str) -> None:
    line = f"{time.strftime('%d.%m. %H:%M:%S')} {text.rstrip()}"
    if job is not None:
        job.zeilen.append(line)
    try:
        path = data_dir() / "speicher.log"
        if path.exists() and path.stat().st_size > 2_000_000:
            path.replace(path.with_suffix(".log.1"))
        with path.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def current() -> Job | None:
    return _current


def history() -> list[Job]:
    return list(reversed(_history))


def busy() -> bool:
    return _current is not None


def run(name: str, func, *args, wait: bool = False) -> bool:
    """Startet func(job, *args) im Hintergrund. False, wenn schon etwas läuft."""
    global _current
    with _lock:
        if _current is not None:
            return False
        job = _current = Job(name)

    def worker():
        global _current
        log(job, f"Start: {name}")
        try:
            func(job, *args)
            job.ok = True
            log(job, f"Fertig: {name}")
        except Exception as exc:  # noqa: BLE001 – Fehler gehören ins Protokoll, nicht in den Thread-Tod
            job.ok = False
            log(job, f"Fehler: {exc}")
            if not isinstance(exc, RuntimeError):
                log(None, traceback.format_exc())
        finally:
            job.ende = time.time()
            with _lock:
                _history.append(job)
                _current = None

    thread = threading.Thread(target=worker, name=f"job-{name}", daemon=True)
    thread.start()
    if wait:
        thread.join()
    return True
