"""Platten einhängen und mit mergerfs zu einem Pool zusammenfassen.

Aufbau unter MOUNT_ROOT (Standard /mnt/cloudy, per rshared auch auf dem Host sichtbar):
  platten/hdd1, hdd2, …   die einzelnen Pool-Platten (normales ext4, jede Datei liegt ganz auf einer Platte)
  puffer                  die SSD mit der Sicherheitskopie bis zum nächsten Backup
  pool                    alle HDDs als ein Ordner (mergerfs) – den benutzen Freigaben und andere Container
"""
import errno
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from . import disks
from .config import Config, mount_root

lock = threading.RLock()


@dataclass
class Branch:
    name: str
    uuid: str
    target: str
    device: str = ""
    mounted: bool = False
    size: int = 0
    free: int = 0
    fehler: str = ""


def _unescape(path: str) -> str:
    return path.encode().decode("unicode_escape") if "\\" in path else path


def mounts() -> dict[str, str]:
    """Eingehängte Ziele im Container -> Dateisystemtyp."""
    out: dict[str, str] = {}
    try:
        with open("/proc/self/mountinfo") as fh:
            for line in fh:
                left, _, right = line.partition(" - ")
                parts = left.split()
                if len(parts) >= 5:
                    out[_unescape(parts[4])] = right.split()[0] if right else ""
    except OSError:
        pass
    return out


def paths() -> dict[str, Path]:
    root = mount_root()
    return {"root": root, "platten": root / "platten", "puffer": root / "puffer", "pool": root / "pool"}


def _guard(directory: Path) -> None:
    """Leeren Mount-Ordner schreibgeschützt machen: Ist die Platte weg, landet nichts aus Versehen auf der Systemplatte."""
    directory.mkdir(parents=True, exist_ok=True)
    if str(directory) not in mounts():
        disks.run(["chattr", "+i", str(directory)], check=False)


def _alive(target: str) -> bool:
    try:
        os.stat(target)
        return True
    except OSError as exc:
        return exc.errno not in (errno.ENOTCONN, errno.EIO)


def _usage(branch: Branch) -> None:
    try:
        st = os.statvfs(branch.target)
        branch.size = st.f_blocks * st.f_frsize
        branch.free = st.f_bavail * st.f_frsize
    except OSError as exc:
        branch.fehler = str(exc)


def _mount_fs(uuid: str, target: Path, name: str) -> Branch:
    branch = Branch(name=name, uuid=uuid, target=str(target))
    _guard(target)
    branch.device = disks.device_for_uuid(uuid)
    current = mounts()
    if str(target) in current and _alive(str(target)):
        branch.mounted = True
    elif not branch.device:
        branch.fehler = "Platte nicht gefunden"
    else:
        if str(target) in current:
            disks.run(["umount", "-l", str(target)], check=False)
        res = disks.run(["mount", "-o", "noatime", f"UUID={uuid}", str(target)], check=False)
        if res.returncode == 0:
            branch.mounted = True
        else:
            branch.fehler = (res.stderr.strip() or "mount fehlgeschlagen").splitlines()[-1]
    if branch.mounted:
        _usage(branch)
    return branch


def mergerfs_options(cfg: Config) -> str:
    policy = cfg.create_policy if cfg.create_policy in ("epmfs", "mfs", "ff", "lfs") else "epmfs"
    return ",".join([
        "defaults", "allow_other", "noatime",
        f"category.create={policy}",
        "moveonenospc=true",
        f"minfreespace={max(1, int(cfg.minfree_gb))}G",
        "cache.files=off", "dropcacheonclose=true",
        "fsname=cloudy-pool",
    ])


def _pool_branches() -> list[str]:
    """Aktuelle Zweige des laufenden mergerfs (über die Steuerdatei), leer wenn unbekannt."""
    try:
        raw = os.getxattr(str(paths()["pool"] / ".mergerfs"), "user.mergerfs.srcmounts").decode()
    except OSError:
        return []
    return [b for b in raw.split(":") if b]


def mount_all(cfg: Config | None = None) -> dict:
    """Alles einhängen, was eingestellt ist. Fehlende Platten werden übersprungen – der Rest läuft weiter."""
    cfg = cfg or Config.load()
    p = paths()
    with lock:
        for key in ("root", "platten"):
            p[key].mkdir(parents=True, exist_ok=True)
        hdds = [_mount_fs(uuid, p["platten"] / f"hdd{i + 1}", f"hdd{i + 1}") for i, uuid in enumerate(cfg.pool)]
        puffer = _mount_fs(cfg.puffer, p["puffer"], "puffer") if cfg.puffer else None

        pool = str(p["pool"])
        live = [h.target for h in hdds if h.mounted]
        current = mounts()
        pool_fehler = ""
        if pool in current and (not _alive(pool) or _pool_branches() != live):
            disks.run(["umount", "-l", pool], check=False)
            current = mounts()
        if live and pool not in current:
            _guard(p["pool"])
            res = disks.run(["mergerfs", "-o", mergerfs_options(cfg), ":".join(live), pool], check=False)
            if res.returncode != 0:
                pool_fehler = (res.stderr.strip() or "mergerfs fehlgeschlagen").splitlines()[-1]
        elif not live:
            pool_fehler = "Keine Pool-Platte eingehängt" if cfg.pool else ""
        return {"hdds": hdds, "puffer": puffer, "pool_fehler": pool_fehler}


def unmount_pool() -> None:
    pool = str(paths()["pool"])
    if pool in mounts():
        disks.run(["umount", "-l", pool], check=False)


def unmount(target: str) -> None:
    if target not in mounts():
        return
    if _alive(target):
        disks.run(["umount", target], check=False)
    if target in mounts():
        disks.run(["umount", "-l", target], check=False)


def unmount_all() -> None:
    """Pool, Puffer und alle Platten aushängen (vor dem Umbau der Zuordnung)."""
    p = paths()
    with lock:
        unmount(str(p["pool"]))
        unmount(str(p["puffer"]))
        if p["platten"].is_dir():
            for child in sorted(p["platten"].iterdir()):
                unmount(str(child))


def status(cfg: Config | None = None) -> dict:
    cfg = cfg or Config.load()
    p = paths()
    current = mounts()
    hdds = []
    for i, uuid in enumerate(cfg.pool):
        b = Branch(f"hdd{i + 1}", uuid, str(p["platten"] / f"hdd{i + 1}"))
        b.mounted = b.target in current and _alive(b.target)
        b.device = disks.device_for_uuid(uuid)
        if b.mounted:
            _usage(b)
        elif not b.device:
            b.fehler = "Platte nicht gefunden"
        hdds.append(b)
    puffer = None
    if cfg.puffer:
        puffer = Branch("puffer", cfg.puffer, str(p["puffer"]))
        puffer.mounted = puffer.target in current and _alive(puffer.target)
        puffer.device = disks.device_for_uuid(cfg.puffer)
        if puffer.mounted:
            _usage(puffer)
    pool = Branch("pool", "", str(p["pool"]))
    pool.mounted = pool.target in current and _alive(pool.target)
    if pool.mounted:
        _usage(pool)
    return {"hdds": hdds, "puffer": puffer, "pool": pool}
