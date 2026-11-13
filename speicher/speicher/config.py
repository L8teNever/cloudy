"""Einstellungen und Geheimnisse des Speicher-Dienstes (alles unter DATA_DIR, Geheimnisse mit 0600)."""
import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

_lock = threading.Lock()


def data_dir() -> Path:
    path = Path(os.environ.get("DATA_DIR", "/data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def mount_root() -> Path:
    """Hier hängen Platten und Pool. Ist per rshared mit dem Host verbunden, damit andere Container sie sehen."""
    return Path(os.environ.get("MOUNT_ROOT", "/mnt/cloudy"))


@dataclass
class Config:
    # Dateisystem-UUIDs der Pool-Platten (Reihenfolge = hdd1, hdd2, …) und der Puffer-SSD.
    pool: list[str] = field(default_factory=list)
    puffer: str = ""
    # mergerfs: epmfs hält Ordner auf einer Platte zusammen, mfs verteilt nach freiem Platz.
    create_policy: str = "epmfs"
    minfree_gb: int = 20
    # Puffer: alle N Minuten Neues aus dem Pool auf die SSD kopieren.
    puffer_minuten: int = 10
    # Backup nach Google Drive
    backup_an: bool = False
    backup_zeit: str = "00:00"
    drive_ordner: str = "cloudy-backup"
    behalten_taeglich: int = 7
    behalten_woechentlich: int = 4
    behalten_monatlich: int = 12
    # Zustand
    letztes_backup_start: float = 0.0  # Startzeit des letzten ERFOLGREICHEN Backups
    letztes_backup_ende: float = 0.0
    letzter_fehler: str = ""
    letzter_prune: float = 0.0
    letzter_versuch: float = 0.0  # letzter Backup-Start (auch fehlgeschlagen)
    puffer_seit: float = 0.0  # ab wann der Puffer kopiert (vor dem ersten Backup nicht den ganzen Pool kopieren)
    letzte_kopie: float = 0.0
    kopie_fehler: str = ""

    @classmethod
    def load(cls) -> "Config":
        path = data_dir() / "config.json"
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        with _lock:
            write_private(data_dir() / "config.json", json.dumps(asdict(self), indent=2))


def update(**changes) -> Config:
    """Laden, ändern, speichern – unter Sperre, damit Planer und Webseite sich nicht überschreiben."""
    with _lock:
        cfg = Config.load()
        for key, value in changes.items():
            if key not in Config.__dataclass_fields__:
                raise KeyError(key)
            setattr(cfg, key, value)
        write_private(data_dir() / "config.json", json.dumps(asdict(cfg), indent=2))
        return cfg


def write_private(path: Path, text: str) -> None:
    """Atomar und nur für root lesbar schreiben."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def secret(name: str, create: bool = False) -> str:
    """Geheimnis aus DATA_DIR/<name> lesen (optional einmalig zufällig erzeugen)."""
    path = data_dir() / name
    try:
        return path.read_text().strip()
    except OSError:
        if not create:
            return ""
    value = secrets.token_urlsafe(32)
    write_private(path, value)
    return value
