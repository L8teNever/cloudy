"""Sicherheitskopie auf die Puffer-SSD und verschlüsseltes Backup nach Google Drive (restic über rclone).

Ablauf:
  - Alle paar Minuten: Dateien, die seit dem letzten erfolgreichen Backup neu oder geändert sind,
    vom Pool nach puffer/sicherung kopieren (gleiche Ordnerstruktur).
  - Zur Backup-Zeit: Startzeit merken, Pool mit restic nach Drive sichern, alte Stände ausdünnen.
  - Nur wenn das geklappt hat: alles auf dem Puffer löschen, was VOR dem Backup-Start kopiert wurde.
"""
import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config, jobs, pool
from .config import Config, data_dir

REMOTE = "cloudy-drive"
HOST = "cloudy"


class BackupError(RuntimeError):
    pass


# ---------- Pfade und Umgebung ----------

def rclone_conf() -> Path:
    return data_dir() / "rclone.conf"


def password_file() -> Path:
    return data_dir() / "restic-passwort"


def sicherung_dir() -> Path:
    return pool.paths()["puffer"] / "sicherung"


def repository(cfg: Config) -> str:
    ordner = (cfg.drive_ordner or "cloudy-backup").strip("/ ")
    return f"rclone:{REMOTE}:{ordner}"


def env(cfg: Config) -> dict:
    cache = data_dir() / "cache"
    cache.mkdir(exist_ok=True)
    return {
        **os.environ,
        "RESTIC_REPOSITORY": repository(cfg),
        "RESTIC_PASSWORD_FILE": str(password_file()),
        "RESTIC_CACHE_DIR": str(cache / "restic"),
        "RCLONE_CONFIG": str(rclone_conf()),
        "RESTIC_PROGRESS_FPS": "0.0167",  # ca. eine Fortschrittszeile pro Minute
    }


def drive_eingerichtet() -> bool:
    return rclone_conf().exists() and password_file().exists()


def write_rclone(token_json: str, client_id: str = "", client_secret: str = "") -> None:
    token_json = token_json.strip()
    # "rclone authorize drive" gibt den Token mit Rahmen aus – nur das JSON-Objekt übernehmen.
    if "{" in token_json:
        token_json = token_json[token_json.index("{"): token_json.rindex("}") + 1]
    try:
        token = json.loads(token_json)
    except ValueError as exc:
        raise BackupError("Das ist kein gültiger Token (JSON von „rclone authorize drive“ einfügen)") from exc
    if not isinstance(token, dict) or not token.get("refresh_token"):
        raise BackupError("Im Token fehlt der refresh_token – bitte den kompletten Text einfügen")
    lines = [f"[{REMOTE}]", "type = drive", "scope = drive"]
    for key, value in (("client_id", client_id), ("client_secret", client_secret)):
        value = value.strip()
        if value:
            if any(c in value for c in "\r\n"):
                raise BackupError(f"{key} ist ungültig")
            lines.append(f"{key} = {value}")
    lines.append(f"token = {json.dumps(token, separators=(',', ':'))}")
    config.write_private(rclone_conf(), "\n".join(lines) + "\n")
    config.secret("restic-passwort", create=True)


# ---------- Befehle ----------

def _run(job, cmd: list[str], cfg: Config, timeout: int | None = None, quiet: bool = False) -> subprocess.CompletedProcess:
    """Befehl ausführen, Ausgabe zeilenweise ins Protokoll."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env(cfg))
    out: list[str] = []
    start = time.time()
    assert proc.stdout is not None
    for line in proc.stdout:
        out.append(line)
        if not quiet and line.strip():
            jobs.log(job, line.strip()[:300])
        if timeout and time.time() - start > timeout:
            proc.kill()
            raise BackupError(f"{cmd[0]} hat zu lange gebraucht")
    proc.wait()
    return subprocess.CompletedProcess(cmd, proc.returncode, "".join(out), "")


def _last_line(res: subprocess.CompletedProcess) -> str:
    lines = [l for l in res.stdout.strip().splitlines() if l.strip()]
    return lines[-1][:300] if lines else f"Fehler {res.returncode}"


def drive_testen_und_init(job) -> None:
    cfg = Config.load()
    if not drive_eingerichtet():
        raise BackupError("Google Drive ist noch nicht verbunden")
    res = _run(job, ["rclone", "lsd", f"{REMOTE}:", "--max-depth", "1"], cfg, timeout=120, quiet=True)
    if res.returncode != 0:
        raise BackupError(f"Google Drive antwortet nicht: {_last_line(res)}")
    jobs.log(job, "Google Drive ist erreichbar")
    res = _run(job, ["restic", "cat", "config"], cfg, timeout=300, quiet=True)
    if res.returncode == 0:
        jobs.log(job, f"Backup-Ablage {repository(cfg)} gibt es schon – passt das Passwort, wird sie weiterbenutzt")
        return
    if res.returncode not in (10,) and "does not exist" not in res.stdout and "unable to open config" not in res.stdout:
        raise BackupError(f"Backup-Ablage nicht lesbar: {_last_line(res)}")
    res = _run(job, ["restic", "init"], cfg, timeout=600)
    if res.returncode != 0:
        raise BackupError(f"Backup-Ablage anlegen fehlgeschlagen: {_last_line(res)}")
    jobs.log(job, "Neue, verschlüsselte Backup-Ablage in Google Drive angelegt")


# ---------- Puffer (Sicherheitskopie auf der SSD) ----------

def changed_files(root: Path, since: float) -> list[str]:
    """Relative Pfade aller Dateien unter root, die seit 'since' neu oder geändert sind.

    ctime zählt mit: Kopiert man alte Fotos in den Pool, behalten sie ihr altes Änderungsdatum (mtime),
    bekommen aber eine neue ctime – sonst würden sie übersehen.
    """
    result: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if entry.name == ".mergerfs" and current == root:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) or entry.is_symlink():
                    st = entry.stat(follow_symlinks=False)
                    if max(st.st_mtime, st.st_ctime) > since:
                        result.append(os.path.relpath(entry.path, root))
            except OSError:
                continue
    result.sort()
    return result


def kopie_marker(cfg: Config) -> float:
    return max(cfg.letztes_backup_start, cfg.puffer_seit)


def puffer_kopie(job) -> None:
    cfg = Config.load()
    if not cfg.puffer:
        return
    st = pool.status(cfg)
    if not st["pool"].mounted:
        raise BackupError("Pool ist nicht eingehängt")
    if not st["puffer"] or not st["puffer"].mounted:
        config.update(kopie_fehler="Puffer-SSD ist nicht eingehängt")
        raise BackupError("Puffer-SSD ist nicht eingehängt")
    start = time.time()
    src = pool.paths()["pool"]
    files = changed_files(src, kopie_marker(cfg))
    dst = sicherung_dir()
    dst.mkdir(parents=True, exist_ok=True)
    fehler = ""
    if files:
        res = subprocess.run(
            ["rsync", "-a", "--from0", "--files-from=-", "--partial", "--inplace", f"{src}/", f"{dst}/"],
            input="\0".join(files) + "\0", capture_output=True, text=True,
        )
        if res.returncode not in (0, 24):  # 24 = Datei verschwand während der Kopie
            lines = res.stderr.strip().splitlines()
            fehler = f"Kopie unvollständig: {lines[-1] if lines else res.returncode}"
            if "No space left" in res.stderr:
                fehler = "Puffer-SSD ist voll – nicht alles Neue ist doppelt gesichert"
            jobs.log(job, fehler)
        jobs.log(job, f"{len(files)} neue/geänderte Dateien auf dem Puffer gesichert")
    config.update(letzte_kopie=start, kopie_fehler=fehler)


def puffer_aufraeumen(job, backup_start: float) -> int:
    """Löscht Kopien, die vor dem Backup-Start auf den Puffer kamen – sie stecken jetzt im Drive-Backup."""
    root = sicherung_dir()
    if not root.is_dir():
        return 0
    removed = 0
    for current, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            path = os.path.join(current, name)
            try:
                # ctime der KOPIE = wann sie auf den Puffer kam (rsync -a übernimmt nur die mtime).
                if os.lstat(path).st_ctime < backup_start:
                    os.unlink(path)
                    removed += 1
            except OSError:
                pass
        if current != str(root):
            try:
                os.rmdir(current)  # nur wenn leer
            except OSError:
                pass
    jobs.log(job, f"Puffer aufgeräumt: {removed} Dateien sind jetzt in Google Drive gesichert")
    return removed


def puffer_zurueckspielen(job) -> None:
    """Nach einem Plattentausch: alles vom Puffer in den Pool, ohne Vorhandenes zu überschreiben."""
    st = pool.status()
    if not st["pool"].mounted or not st["puffer"] or not st["puffer"].mounted:
        raise BackupError("Pool und Puffer müssen eingehängt sein")
    res = _run(job, ["rsync", "-a", "--ignore-existing", "--stats", f"{sicherung_dir()}/", f"{pool.paths()['pool']}/"], Config.load(), quiet=True)
    if res.returncode not in (0, 24):
        raise BackupError(f"Zurückspielen fehlgeschlagen: {_last_line(res)}")
    jobs.log(job, "Puffer in den Pool zurückgespielt (vorhandene Dateien unverändert)")


# ---------- Backup ----------

def backup_due(now: float, zeit: str, last_success_start: float, last_attempt: float) -> bool:
    """Ist das tägliche Backup fällig? Nach einem Fehler wird stündlich neu versucht."""
    try:
        hour, minute = (int(x) for x in zeit.split(":"))
    except ValueError:
        hour, minute = 0, 0
    now_dt = datetime.fromtimestamp(now)
    slot = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot > now_dt:
        slot -= timedelta(days=1)
    slot_ts = slot.timestamp()
    if last_success_start >= slot_ts:
        return False
    if last_attempt >= slot_ts and now - last_attempt < 3600:
        return False
    return True


def backup(job) -> None:
    cfg = Config.load()
    if not drive_eingerichtet():
        raise BackupError("Google Drive ist noch nicht verbunden")
    st = pool.status(cfg)
    missing = [h.name for h in st["hdds"] if not h.mounted]
    if missing:
        # Sonst würde ein Backup ohne die Dateien der fehlenden Platte entstehen und alte Stände verdrängen.
        raise BackupError(f"Backup ausgelassen: {', '.join(missing)} fehlt – erst Platte tauschen und zurückspielen")
    if not st["pool"].mounted:
        raise BackupError("Pool ist nicht eingehängt")

    start = time.time()
    config.update(letzter_versuch=start)
    if cfg.puffer:
        try:
            puffer_kopie(job)  # Letzten Stand noch auf den Puffer, bevor die Startmarke gesetzt wird
        except BackupError as exc:
            jobs.log(job, f"Hinweis: {exc}")
        start = time.time()

    res = _run(job, [
        "restic", "backup", str(pool.paths()["pool"]),
        "--host", HOST, "--tag", "pool", "--exclude-caches", "--exclude", ".mergerfs",
    ], cfg)
    if res.returncode == 3:
        jobs.log(job, "Einige Dateien waren nicht lesbar – der Rest ist gesichert")
    elif res.returncode != 0:
        config.update(letzter_fehler=_last_line(res))
        raise BackupError(f"Backup fehlgeschlagen: {_last_line(res)}")

    keep = [
        "--keep-daily", str(max(1, cfg.behalten_taeglich)),
        "--keep-weekly", str(max(0, cfg.behalten_woechentlich)),
        "--keep-monthly", str(max(0, cfg.behalten_monatlich)),
    ]
    prune = time.time() - cfg.letzter_prune > 7 * 86400
    res = _run(job, ["restic", "forget", "--host", HOST, "--tag", "pool", *keep, *(["--prune"] if prune else [])], cfg, quiet=True)
    if res.returncode != 0:
        jobs.log(job, f"Alte Stände ausdünnen hat nicht geklappt: {_last_line(res)}")
    elif prune:
        config.update(letzter_prune=time.time())
        jobs.log(job, "Alte Stände ausgedünnt und Platz in Drive freigegeben")

    config.update(letztes_backup_start=start, letztes_backup_ende=time.time(), letzter_fehler="")
    if cfg.puffer:
        puffer_aufraeumen(job, start)


def snapshots() -> list[dict]:
    if not drive_eingerichtet():
        return []
    cfg = Config.load()
    res = subprocess.run(["restic", "snapshots", "--json", "--host", HOST], capture_output=True, text=True, env=env(cfg), timeout=300)
    if res.returncode != 0:
        raise BackupError((res.stderr.strip().splitlines() or ["restic snapshots fehlgeschlagen"])[-1])
    items = json.loads(res.stdout or "[]")
    items.sort(key=lambda s: s.get("time", ""), reverse=True)
    return items


def restore(job, snapshot_id: str, include: str = "") -> None:
    if not snapshot_id.replace("-", "").isalnum() and snapshot_id != "latest":
        raise BackupError("Ungültiger Stand")
    cfg = Config.load()
    if not pool.status(cfg)["pool"].mounted:
        raise BackupError("Pool ist nicht eingehängt")
    target = pool.paths()["pool"] / "wiederhergestellt" / f"{time.strftime('%Y-%m-%d_%H-%M')}_{snapshot_id[:8]}"
    target.mkdir(parents=True, exist_ok=True)
    cmd = ["restic", "restore", f"{snapshot_id}:{pool.paths()['pool']}", "--target", str(target)]
    include = include.strip().strip("/")
    if include:
        if ".." in include.split("/"):
            raise BackupError("Ungültiger Pfad")
        cmd += ["--include", "/" + include]
    res = _run(job, cmd, cfg)
    if res.returncode != 0:
        raise BackupError(f"Wiederherstellen fehlgeschlagen: {_last_line(res)}")
    jobs.log(job, f"Wiederhergestellt nach {target}")
