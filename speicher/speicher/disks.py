"""Platten erkennen, Systemplatte schützen und Platten für den Speicher formatieren."""
import json
import os
import subprocess
import time
from dataclasses import dataclass, field

from .config import mount_root

SUPPORTED_FS = ("ext4", "xfs", "btrfs")
HOST_PROC = os.environ.get("HOST_PROC", "/host/proc")


class DiskError(RuntimeError):
    pass


def run(cmd: list[str], timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise DiskError(f"{cmd[0]} fehlt im Container") from exc
    except subprocess.TimeoutExpired as exc:
        raise DiskError(f"{cmd[0]} hat zu lange gebraucht") from exc
    if check and res.returncode != 0:
        msg = (res.stderr or res.stdout).strip().splitlines()
        raise DiskError(f"{cmd[0]}: {msg[-1] if msg else 'Fehler ' + str(res.returncode)}")
    return res


@dataclass
class Partition:
    path: str
    size: int
    fstype: str = ""
    uuid: str = ""
    label: str = ""


@dataclass
class Disk:
    name: str
    path: str
    size: int
    model: str = ""
    serial: str = ""
    ssd: bool = False
    tran: str = ""
    partitions: list[Partition] = field(default_factory=list)
    system: bool = False  # vom Host benutzt (System, Swap, gemountet) -> nie anfassen
    system_grund: str = ""

    @property
    def art(self) -> str:
        if self.tran == "nvme" or self.name.startswith("nvme"):
            return "NVMe-SSD"
        return "SSD" if self.ssd else "HDD"

    @property
    def dateisystem(self) -> Partition | None:
        """Die eine nutzbare Partition (oder das Dateisystem direkt auf der Platte), sonst None."""
        usable = [p for p in self.partitions if p.fstype in SUPPORTED_FS and p.uuid]
        return usable[0] if len(usable) == 1 else None

    @property
    def uuids(self) -> set[str]:
        return {p.uuid for p in self.partitions if p.uuid}


def _flag(value) -> bool:
    return value in (True, 1, "1", "true")


def host_used() -> tuple[set[str], set[str]]:
    """Was der Host selbst benutzt: (maj:min-Nummern, Gerätepfade). Unsere eigenen Mounts zählen nicht."""
    majmin: set[str] = set()
    paths: set[str] = set()
    ours = str(mount_root())
    try:
        with open(f"{HOST_PROC}/1/mountinfo") as fh:
            for line in fh:
                left, _, right = line.partition(" - ")
                parts = left.split()
                if len(parts) < 5:
                    continue
                target = parts[4]
                if target == ours or target.startswith(ours + "/"):
                    continue
                majmin.add(parts[2])
                src = right.split()
                if len(src) >= 2 and src[1].startswith("/dev/"):
                    paths.add(src[1])
    except OSError:
        pass
    try:
        with open(f"{HOST_PROC}/swaps") as fh:
            for line in fh.readlines()[1:]:
                if line.split():
                    paths.add(line.split()[0])
    except OSError:
        pass
    return majmin, paths


def parse_lsblk(data: dict, used_majmin: set[str], used_paths: set[str]) -> list[Disk]:
    disks: list[Disk] = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk" or not int(dev.get("size") or 0):
            continue
        if (dev.get("name") or "").startswith(("zram", "loop", "sr", "ram")):
            continue
        disk = Disk(
            name=dev.get("name", ""),
            path=dev.get("path") or f"/dev/{dev.get('name')}",
            size=int(dev.get("size") or 0),
            model=(dev.get("model") or "").strip(),
            serial=(dev.get("serial") or "").strip(),
            ssd=not _flag(dev.get("rota")),
            tran=dev.get("tran") or "",
        )
        if dev.get("fstype"):
            disk.partitions.append(Partition(disk.path, disk.size, dev.get("fstype") or "", dev.get("uuid") or "", dev.get("label") or ""))

        def walk(node, top: bool):
            mm = node.get("maj:min", "")
            path = node.get("path") or ""
            if mm in used_majmin or path in used_paths:
                disk.system = True
                disk.system_grund = disk.system_grund or f"{path or node.get('name')} wird vom Server benutzt"
            if node.get("fstype") in ("LVM2_member", "linux_raid_member", "crypto_LUKS", "zfs_member", "swap"):
                disk.system = True
                disk.system_grund = disk.system_grund or f"{path} ist {node.get('fstype')}"
            for child in node.get("children") or []:
                if top and child.get("type") == "part":
                    disk.partitions.append(Partition(
                        child.get("path") or "", int(child.get("size") or 0),
                        child.get("fstype") or "", child.get("uuid") or "", child.get("label") or "",
                    ))
                walk(child, False)

        walk(dev, True)
        disks.append(disk)
    return disks


def list_disks() -> list[Disk]:
    res = run(["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,TYPE,MODEL,SERIAL,ROTA,TRAN,FSTYPE,LABEL,UUID,MAJ:MIN"])
    majmin, paths = host_used()
    return parse_lsblk(json.loads(res.stdout or "{}"), majmin, paths)


def find_disk(path: str) -> Disk:
    for disk in list_disks():
        if disk.path == path:
            return disk
    raise DiskError(f"{path} nicht gefunden")


def device_for_uuid(uuid: str) -> str:
    res = run(["blkid", "-U", uuid], check=False)
    return res.stdout.strip() if res.returncode == 0 else ""


def _partition_path(disk_path: str) -> str:
    return disk_path + ("p1" if disk_path[-1].isdigit() else "1")


def format_disk(path: str, label: str, reserve_percent: int = 0) -> str:
    """Löscht die Platte komplett, legt eine ext4-Partition an und gibt deren UUID zurück."""
    disk = find_disk(path)
    if disk.system:
        raise DiskError(f"{path} gehört zum System und wird nicht formatiert ({disk.system_grund})")
    for part in disk.partitions:
        run(["umount", part.path], check=False)
    run(["wipefs", "-a", "-f", path])
    run(["sgdisk", "--zap-all", path])
    run(["sgdisk", "-n", "1:0:0", "-t", "1:8300", "-c", f"1:{label}", path])
    run(["partprobe", path], check=False)
    run(["blockdev", "--rereadpt", path], check=False)
    part = _partition_path(path)
    for _ in range(50):
        if os.path.exists(part):
            break
        time.sleep(0.2)
    else:
        raise DiskError(f"Partition {part} erscheint nicht (ist /dev in den Container gemountet?)")
    run(["mkfs.ext4", "-F", "-q", "-L", label, "-m", str(reserve_percent), "-E", "lazy_itable_init=1", part], timeout=1800)
    uuid = run(["blkid", "-s", "UUID", "-o", "value", part]).stdout.strip()
    if not uuid:
        raise DiskError("Keine UUID nach dem Formatieren")
    return uuid


def smart_health(path: str) -> str:
    """'ok', 'FEHLER' oder '' (unbekannt, z. B. USB-Gehäuse ohne SMART)."""
    res = run(["smartctl", "-H", "-n", "standby", path], timeout=20, check=False)
    out = res.stdout
    if res.returncode & 2 or "STANDBY" in out.upper():
        return ""
    if "PASSED" in out or ": OK" in out:
        return "ok"
    if "FAILED" in out:
        return "FEHLER"
    return ""
