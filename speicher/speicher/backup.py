"""Sicherheitskopie auf die Puffer-SSD und Backup nach Google Drive (rclone, NICHT verschlüsselt).

In Drive liegen ganz normale Dateien – im Browser ansehen und herunterladen geht jederzeit, ohne Schlüssel:
  <ordner>/aktuell/              genaues Abbild des Pools
  <ordner>/alt/<JJJJ-MM-TT_HH-MM>/  was beim Backup zu dieser Zeit geändert oder gelöscht wurde (alte Fassung)

Ablauf:
  - Alle paar Minuten: Dateien, die seit dem letzten erfolgreichen Backup neu oder geändert sind,
    vom Pool nach puffer/sicherung kopieren (gleiche Ordnerstruktur).
  - Zur Backup-Zeit: Startzeit merken, Pool mit "rclone sync --backup-dir" nach Drive, alte Versionsordner löschen.
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
ALT_FORMAT = "%Y-%m-%d_%H-%M"


class BackupError(RuntimeError):
    pass


# ---------- Pfade und Umgebung ----------

def rclone_conf() -> Path:
    return data_dir() / "rclone.conf"


def sicherung_dir() -> Path:
    return pool.paths()["puffer"] / "sicherung"


def drive_base(cfg: Config) -> str:
    ordner = (cfg.drive_ordner or "cloudy-backup").strip("/ ")
    return f"{REMOTE}:{ordner}"


def env(cfg: Config) -> dict:
    return {**os.environ, "RCLONE_CONFIG": str(rclone_conf())}


def drive_eingerichtet() -> bool:
    return rclone_conf().exists()


RCLONE_OPTS = ["--transfers", "4", "--checkers", "8", "--skip-links", "--stats", "1m", "--stats-one-line",
               "--stats-log-level", "NOTICE", "--log-level", "NOTICE", "--retries", "5", "--low-level-retries", "20"]


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
    res = _run(job, ["rclone", "mkdir", f"{drive_base(cfg)}/aktuell"], cfg, timeout=120, quiet=True)
    if res.returncode != 0:
        raise BackupError(f"Ordner in Drive anlegen fehlgeschlagen: {_last_line(res)}")
    jobs.log(job, f"Google Drive ist erreichbar, Backup-Ordner: {cfg.drive_ordner}")


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


def alte_ordner_zum_loeschen(namen: list[str], now: float, tage: int) -> list[str]:
    """Versionsordner unter alt/, die älter als 'tage' sind. Unbekannte Namen werden nie angefasst."""
    weg = []
    for name in namen:
        name = name.strip("/")
        try:
            ts = datetime.strptime(name, ALT_FORMAT).timestamp()
        except ValueError:
            continue
        if now - ts > tage * 86400:
            weg.append(name)
    return sorted(weg)


def naechstes_backup(now: float, zeit: str) -> float:
    try:
        hour, minute = (int(x) for x in zeit.split(":"))
    except ValueError:
        hour, minute = 0, 0
    now_dt = datetime.fromtimestamp(now)
    slot = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot <= now_dt:
        slot += timedelta(days=1)
    return slot.timestamp()


def backup(job) -> None:
    cfg = Config.load()
    if not drive_eingerichtet():
        raise BackupError("Google Drive ist noch nicht verbunden")
    st = pool.status(cfg)
    missing = [h.name for h in st["hdds"] if not h.mounted]
    if missing:
        # Sonst würde das Abbild in Drive die Dateien der fehlenden Platte als "gelöscht" behandeln.
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

    base = drive_base(cfg)
    alt = f"{base}/alt/{time.strftime(ALT_FORMAT, time.localtime(start))}"
    res = _run(job, [
        "rclone", "sync", f"{pool.paths()['pool']}/", f"{base}/aktuell",
        "--backup-dir", alt,  # Geänderte/gelöschte Dateien wandern hierhin statt verloren zu gehen
        "--create-empty-src-dirs",
        "--exclude", "/.mergerfs", "--exclude", "/wiederhergestellt/**",
        *RCLONE_OPTS,
    ], cfg)
    if res.returncode != 0:
        config.update(letzter_fehler=_last_line(res))
        raise BackupError(f"Backup fehlgeschlagen: {_last_line(res)}")

    # Alte Versionsordner aufräumen
    res = _run(job, ["rclone", "lsf", "--dirs-only", f"{base}/alt"], cfg, timeout=300, quiet=True)
    if res.returncode == 0:
        for name in alte_ordner_zum_loeschen(res.stdout.splitlines(), time.time(), max(1, cfg.behalten_tage)):
            r = _run(job, ["rclone", "purge", f"{base}/alt/{name}"], cfg, timeout=1800, quiet=True)
            jobs.log(job, f"Alte Versionen vom {name} gelöscht" if r.returncode == 0 else f"Löschen von alt/{name} fehlgeschlagen")

    config.update(letztes_backup_start=start, letztes_backup_ende=time.time(), letzter_fehler="")
    if cfg.puffer:
        puffer_aufraeumen(job, start)


def staende() -> list[dict]:
    """Was man zurückholen kann: der aktuelle Stand und die Versionsordner (neueste zuerst)."""
    if not drive_eingerichtet():
        return []
    cfg = Config.load()
    items = [{"id": "aktuell", "titel": "Aktueller Stand", "info": "alles, wie beim letzten Backup"}]
    res = subprocess.run(["rclone", "lsf", "--dirs-only", f"{drive_base(cfg)}/alt"], capture_output=True, text=True, env=env(cfg), timeout=300)
    if res.returncode == 0:
        for name in sorted((n.strip("/") for n in res.stdout.splitlines()), reverse=True):
            try:
                ts = datetime.strptime(name, ALT_FORMAT)
            except ValueError:
                continue
            items.append({"id": f"alt/{name}", "titel": ts.strftime("Alte Versionen vom %d.%m.%Y %H:%M"),
                          "info": "Dateien, die bei diesem Backup geändert oder gelöscht wurden (alte Fassung)"})
    elif "directory not found" not in (res.stderr + res.stdout).lower():
        raise BackupError((res.stderr.strip().splitlines() or ["Drive nicht lesbar"])[-1])
    return items


def restore(job, stand: str, include: str = "") -> None:
    if stand != "aktuell":
        try:
            datetime.strptime(stand.removeprefix("alt/"), ALT_FORMAT)
            assert stand.startswith("alt/")
        except (ValueError, AssertionError):
            raise BackupError("Ungültiger Stand") from None
    cfg = Config.load()
    if not pool.status(cfg)["pool"].mounted:
        raise BackupError("Pool ist nicht eingehängt")
    include = include.strip().strip("/")
    if include and (".." in include.split("/") or "\n" in include):
        raise BackupError("Ungültiger Pfad")
    name = stand.replace("/", "_")
    target = pool.paths()["pool"] / "wiederhergestellt" / f"{time.strftime('%Y-%m-%d_%H-%M')}_{name}"
    src = f"{drive_base(cfg)}/{stand}"
    cmd = "copy"
    if include:
        src += f"/{include}"
        target = target / include
        res = _run(job, ["rclone", "lsjson", "--stat", src], cfg, timeout=120, quiet=True)
        if res.returncode != 0:
            raise BackupError(f"„{include}“ gibt es in diesem Stand nicht")
        if not json.loads(res.stdout or "{}").get("IsDir"):
            cmd = "copyto"  # einzelne Datei
    (target.parent if cmd == "copyto" else target).mkdir(parents=True, exist_ok=True)
    res = _run(job, ["rclone", cmd, src, str(target), *RCLONE_OPTS], cfg)
    if res.returncode != 0:
        raise BackupError(f"Wiederherstellen fehlgeschlagen: {_last_line(res)}")
    jobs.log(job, f"Wiederhergestellt nach {target}")
