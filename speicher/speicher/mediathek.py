"""Logik für Streaming-Mediathek, Video-Umwandlung und Schnell-TV („Jetzt schauen“)."""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import freigaben, jobs, mediathek_db, mediathek_providers, medien, pool
from .mediathek_db import Einstellungen

VIDEO_EXTS = {".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mov", ".ts", ".m2ts", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif", ".avif"}
CHUNK = 1024 * 1024

_conv_lock = threading.Lock()
_tv_threads: dict[int, threading.Thread] = {}


def _besitzer(path: Path) -> None:
    try:
        os.chown(path, int(os.environ.get("SPEICHER_UID", "1000")), int(os.environ.get("SPEICHER_GID", "1000")))
    except (OSError, ValueError):
        pass


def _chown_tree(path: Path, stop_at: Path) -> None:
    p = path
    stop_res = stop_at.resolve()
    while True:
        _besitzer(p)
        if p.resolve() == stop_res or p == p.parent:
            break
        p = p.parent


def pool_root() -> Path:
    return pool.paths()["pool"]


def pool_mounted() -> bool:
    root = pool_root()
    return str(root) in pool.mounts()


def safe_pool_path(rel_pfad: str) -> Path:
    """Löst einen relativen Pfad im Pool sicher auf (kein Ausbruch, kein Symlink-Escape)."""
    parts = [p for p in str(rel_pfad or "").replace("\\", "/").split("/") if p]
    if not parts or any(p in (".", "..") or "\0" in p for p in parts):
        raise ValueError("Ungültiger Pfad")
    if parts[0] == ".mergerfs":
        raise ValueError("Reservierter Pfad")
    root = pool_root().resolve()
    target = root.joinpath(*parts).resolve()
    if target != root and root not in target.parents:
        raise ValueError("Pfad außerhalb des Pools")
    return target


FILME = "Filme"
SERIEN = "Serien"


def streaming_target_rel(
    meta: dict,
    season: int | None = None,
    episode: int | None = None,
    episode_title: str = "",
) -> tuple[str, str]:
    """Berechnet (rel_mp4_pfad, ordner_rel_pfad) im Pool für Film, Serie oder Anime."""
    cfg = Einstellungen.load()
    base = cfg.streaming_ordner or "Streaming"
    title_clean = mediathek_providers.sanitize_name(meta.get("name") or "Unbenannt")
    year = meta.get("year")
    folder_title = f"{title_clean} ({year})" if year else title_clean
    if meta.get("type") == "movie":
        folder_rel = f"{base}/{FILME}/{folder_title}"
        return f"{folder_rel}/{folder_title}.mp4", folder_rel

    s = max(1, int(season or 1))
    ep = max(1, int(episode or 1))
    ep_t = (episode_title or "").strip()
    if not ep_t:
        for item in (meta.get("episodes") or (meta.get("meta") or {}).get("episodes") or []):
            if item.get("season") == s and item.get("number") == ep and item.get("title"):
                ep_t = str(item["title"])
                break
    ep_clean = mediathek_providers.sanitize_name(ep_t, "") if ep_t else ""
    if ep_clean and ep_clean.lower() not in (f"folge {ep}", f"episode {ep}"):
        fname = f"{title_clean} - S{s:02d}E{ep:02d} - {ep_clean}.mp4"
    else:
        fname = f"{title_clean} - S{s:02d}E{ep:02d}.mp4"
    folder_rel = f"{base}/{SERIEN}/{folder_title}/Staffel {s}"
    return f"{folder_rel}/{fname}", folder_rel


def filme_serien_trennen() -> int:
    """Alte Ablage Streaming/<Titel> in Streaming/Filme/<Titel> bzw. Streaming/Serien/<Titel> umziehen
    (Jellyfin braucht Filme und Serien in getrennten Ordnern). Gibt die Zahl umgezogener Titel zurück."""
    if not pool_mounted():
        return 0
    base = (Einstellungen.load().streaming_ordner or "Streaming").strip("/")
    root = pool_root()
    umgezogen = 0
    with mediathek_db.db() as conn:
        rows = conn.execute(
            "SELECT d.id, d.rel_pfad, d.roh_pfad, d.status, t.typ FROM dateien d JOIN titel t ON t.id = d.titel_id").fetchall()
        ordner: dict[str, str] = {}  # alter Titelordner -> Unterordner
        beschaeftigt: set[str] = set()
        for r in rows:
            teile = r["rel_pfad"].split("/")
            if len(teile) < 3 or teile[0] != base or teile[1] in (FILME, SERIEN):
                continue
            ordner[teile[1]] = FILME if r["typ"] == "movie" else SERIEN
            if r["status"] == "umwandlung":
                beschaeftigt.add(teile[1])
        for titel, unter in ordner.items():
            if titel in beschaeftigt:
                continue  # nach der Umwandlung beim nächsten Mal
            alt, neu = root / base / titel, root / base / unter / titel
            if not alt.is_dir() or neu.exists():
                continue
            neu.parent.mkdir(parents=True, exist_ok=True)
            _besitzer(neu.parent)
            os.rename(alt, neu)
            alt_pre, neu_pre = f"{base}/{titel}/", f"{base}/{unter}/{titel}/"
            for spalte in ("rel_pfad", "roh_pfad"):
                conn.execute(
                    f"UPDATE dateien SET {spalte} = ? || substr({spalte}, ?) WHERE {spalte} LIKE ? ESCAPE '\\'",
                    (neu_pre, len(alt_pre) + 1, alt_pre.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"))
            umgezogen += 1
    return umgezogen


def personal_video_target_rel(filename: str) -> str:
    cfg = Einstellungen.load()
    base = cfg.medien_ordner or "Medien"
    month = time.strftime("%Y-%m")
    stem = mediathek_providers.sanitize_name(Path(filename).stem, "Video")
    return f"{base}/Videos/{month}/{stem}.mp4"


def personal_image_target_rel(filename: str) -> str:
    cfg = Einstellungen.load()
    base = cfg.medien_ordner or "Medien"
    month = time.strftime("%Y-%m")
    ext = Path(filename).suffix.lower()
    if ext not in IMAGE_EXTS:
        ext = ".jpg"
    stem = mediathek_providers.sanitize_name(Path(filename).stem, "Bild")
    return f"{base}/Bilder/{month}/{stem}{ext}"


def unique_target_path(rel_pfad: str, replace: bool = False) -> Path:
    target = safe_pool_path(rel_pfad)
    if replace or not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for i in range(2, 1000):
        cand = target.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    return target.with_name(f"{stem}-{secrets.token_hex(3)}{suffix}")


# ---------- Video-Analyse & Umwandlung ----------

def probe_video(path: Path) -> dict:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not path.is_file():
        return {"duration": None, "video_codec": "", "audio_codec": "", "pix_fmt": ""}
    cmd = [
        ffprobe, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            return {"duration": None, "video_codec": "", "audio_codec": "", "pix_fmt": ""}
        data = json.loads(res.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"duration": None, "video_codec": "", "audio_codec": "", "pix_fmt": ""}

    v_codec = ""
    a_codec = ""
    pix_fmt = ""
    for st in data.get("streams") or []:
        if st.get("codec_type") == "video" and not v_codec:
            v_codec = (st.get("codec_name") or "").lower()
            pix_fmt = (st.get("pix_fmt") or "").lower()
        elif st.get("codec_type") == "audio" and not a_codec:
            a_codec = (st.get("codec_name") or "").lower()
    dur_raw = (data.get("format") or {}).get("duration")
    try:
        duration = int(round(float(dur_raw))) if dur_raw is not None else None
    except (TypeError, ValueError):
        duration = None
    return {"duration": duration, "video_codec": v_codec, "audio_codec": a_codec, "pix_fmt": pix_fmt}


def needs_conversion(path: Path, info: dict | None = None) -> bool:
    """True, wenn Datei umgepackt oder nach H.264/AAC MP4 umgewandelt werden muss."""
    ext = path.suffix.lower()
    if ext not in (".mp4", ".m4v"):
        return True
    if medien.analyse_mp4(path) != "ok":
        return True
    if info is None:
        info = probe_video(path)
    vc = info.get("video_codec") or ""
    ac = info.get("audio_codec") or ""
    # Browser & Smart-TVs spielen H.264 + AAC/MP3 zuverlässig; exotische Codecs umwandeln
    if vc and vc not in ("h264", "hevc"):
        return True
    if ac and ac not in ("aac", "mp3"):
        return True
    return False


def _run_ffmpeg_cmd(cmd: list[str], dst: Path) -> tuple[bool, str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    except subprocess.TimeoutExpired:
        dst.unlink(missing_ok=True)
        return False, "ffmpeg hat zu lange gebraucht"
    except OSError as exc:
        dst.unlink(missing_ok=True)
        return False, str(exc)
    if res.returncode == 0:
        return True, ""
    dst.unlink(missing_ok=True)
    lines = [l.strip() for l in (res.stderr or "").splitlines() if l.strip()]
    return False, (lines[-1][:200] if lines else f"Code {res.returncode}")


def convert_to_mp4(src: Path, dst_mp4: Path, delete_src: bool = True) -> tuple[bool, str, dict]:
    """Wandelt beliebige Videodatei in TV-/Browser-taugliches Faststart-MP4 um.

    Bei delete_src=True wird die alte Rohdatei nach erfolgreicher Umwandlung gelöscht.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False, "ffmpeg fehlt im Container", {}
    if not src.is_file():
        return False, "Quelldatei fehlt", {}

    info = probe_video(src)
    vc = info.get("video_codec") or ""
    ac = info.get("audio_codec") or ""
    pix = info.get("pix_fmt") or ""

    # Wenn schon perfektes MP4: ggf. nur kopieren/verschieben
    if not needs_conversion(src, info):
        if src.resolve() != dst_mp4.resolve():
            dst_mp4.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst_mp4.parent / f".{dst_mp4.stem[:150]}.mp4tmp-{secrets.token_hex(4)}.mp4"
            try:
                if delete_src:
                    _besitzer(src)
                    os.replace(src, dst_mp4)
                else:
                    shutil.copyfile(src, tmp)
                    _besitzer(tmp)
                    os.replace(tmp, dst_mp4)
            finally:
                tmp.unlink(missing_ok=True)
        return True, "", info

    dst_mp4.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst_mp4.parent / f".{dst_mp4.stem[:150]}.conv-{secrets.token_hex(4)}.mp4"

    attempts: list[list[str]] = []
    h264_ok = vc == "h264" and (not pix or pix in ("yuv420p", "yuvj420p"))
    if h264_ok and ac in ("aac", "mp3", ""):
        attempts.append([
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
            "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp),
        ])
    if h264_ok:
        attempts.append([
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
            "-movflags", "+faststart", "-f", "mp4", str(tmp),
        ])
    attempts.append([
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-movflags", "+faststart", "-f", "mp4", str(tmp),
    ])

    last_err = ""
    ok = False
    try:
        for cmd in attempts:
            ok, last_err = _run_ffmpeg_cmd(cmd, tmp)
            if ok and tmp.is_file() and tmp.stat().st_size >= 512 and medien.analyse_mp4(tmp) == "ok":
                break
            ok = False
            tmp.unlink(missing_ok=True)
        if not ok:
            return False, last_err or "Umwandlung fehlgeschlagen", {}

        _besitzer(tmp)
        os.replace(tmp, dst_mp4)
        new_info = probe_video(dst_mp4)
        if delete_src and src.resolve() != dst_mp4.resolve():
            src.unlink(missing_ok=True)
        return True, "", new_info
    finally:
        tmp.unlink(missing_ok=True)


def finalize_uploaded_streaming_file(file_id: int) -> None:
    """Prüft hochgeladene Mediathek-Datei direkt oder wandelt sie in den Hintergrund-Status um."""
    item = mediathek_db.get_file(file_id)
    if not item:
        return
    root = pool_root()
    roh = safe_pool_path(item["roh_pfad"]) if item.get("roh_pfad") else safe_pool_path(item["rel_pfad"])
    dst = safe_pool_path(item["rel_pfad"])
    info = probe_video(roh)
    if not needs_conversion(roh, info) and roh.resolve() == dst.resolve():
        mediathek_db.update_file(
            file_id,
            status="bereit",
            roh_pfad="",
            groesse=dst.stat().st_size,
            dauer=info.get("duration"),
            video_codec=info.get("video_codec") or "",
            audio_codec=info.get("audio_codec") or "",
        )
        return
    mediathek_db.update_file(file_id, status="umwandlung")
    medien.scan_anstossen()


def process_queue() -> int:
    """Verarbeitet wartende Umwandlungen in mediathek.db (wird von medien._worker aufgerufen)."""
    if not pool_mounted():
        return 0
    if not _conv_lock.acquire(blocking=False):
        return 0
    done = 0
    try:
        root = pool_root()
        for item in mediathek_db.pending_files():
            if item["status"] != "umwandlung":
                continue
            try:
                dst = safe_pool_path(item["rel_pfad"])
                roh = safe_pool_path(item["roh_pfad"]) if item.get("roh_pfad") else dst
            except ValueError as exc:
                mediathek_db.update_file(item["id"], status="fehler", fehler=str(exc))
                continue
            if not roh.is_file():
                if dst.is_file() and medien.analyse_mp4(dst) == "ok":
                    info = probe_video(dst)
                    mediathek_db.update_file(
                        item["id"], status="bereit", roh_pfad="", groesse=dst.stat().st_size,
                        dauer=info.get("duration"), video_codec=info.get("video_codec") or "", audio_codec=info.get("audio_codec") or "",
                    )
                    done += 1
                else:
                    mediathek_db.update_file(item["id"], status="fehler", fehler="Rohdatei fehlt")
                continue

            ok, err, info = convert_to_mp4(roh, dst, delete_src=True)
            if ok:
                mediathek_db.update_file(
                    item["id"],
                    status="bereit",
                    fehler="",
                    roh_pfad="",
                    groesse=dst.stat().st_size,
                    dauer=info.get("duration"),
                    video_codec=info.get("video_codec") or "",
                    audio_codec=info.get("audio_codec") or "",
                )
                jobs.log(None, f"Mediathek fertig umgewandelt: {dst.name}")
                done += 1
            else:
                mediathek_db.update_file(item["id"], status="fehler", fehler=err)
                jobs.log(None, f"Mediathek-Umwandlung fehlgeschlagen ({roh.name}): {err}")
        if done:
            try:
                mediathek_db.sync_disk(root)
            except OSError:
                pass
        return done
    finally:
        _conv_lock.release()


# ---------- Jetzt schauen (Temporärer Pool-Ordner für TV) ----------

def temp_dir() -> Path:
    cfg = Einstellungen.load()
    return safe_pool_path(cfg.temp_ordner or "Jetzt schauen")


def ensure_temp_dir() -> Path:
    d = temp_dir()
    created = not d.is_dir()
    d.mkdir(parents=True, exist_ok=True)
    _besitzer(d)
    if created:
        _refresh_dlna_async()
    return d


def cleanup_temp_dir_if_empty() -> bool:
    """Löscht den Ordner „Jetzt schauen“, sobald keine Dateien mehr darin liegen."""
    try:
        d = temp_dir()
    except ValueError:
        return False
    if not d.is_dir():
        return False
    try:
        with os.scandir(d) as it:
            if any(True for _ in it):
                return False
        d.rmdir()
        _refresh_dlna_async()
        return True
    except OSError:
        return False


def _refresh_dlna_async() -> None:
    def _run():
        try:
            freigaben.dienste.abgleichen()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_run, name="dlna-sync", daemon=True).start()


def _copy_chunked(src: Path, dst: Path, item_id: int) -> None:
    total = max(1, src.stat().st_size)
    copied = 0
    tmp = dst.parent / f".{dst.name[:160]}.tvcopy-{secrets.token_hex(4)}"
    last_pct = -1
    try:
        with src.open("rb") as rf, tmp.open("wb") as wf:
            while buf := rf.read(CHUNK):
                wf.write(buf)
                copied += len(buf)
                pct = min(99, int(copied * 100 / total))
                if pct != last_pct and (pct - last_pct >= 5 or pct == 99):
                    last_pct = pct
                    mediathek_db.update_tv_item(item_id, fortschritt=pct)
        _besitzer(tmp)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def _unique_flat_tv_dest(tdir: Path, filename: str) -> Path:
    clean = mediathek_providers.sanitize_name(Path(filename).stem, "Datei") + Path(filename).suffix.lower()
    cand = tdir / clean
    if not cand.exists():
        return cand
    stem = cand.stem
    ext = cand.suffix
    for i in range(2, 1000):
        c2 = tdir / f"{stem} ({i}){ext}"
        if not c2.exists():
            return c2
    return tdir / f"{stem}-{secrets.token_hex(3)}{ext}"


def stage_video(
    src_rel: str,
    *,
    art: str = "video",
    titel: str = "",
    datei_id: int | None = None,
    ablauf_stunden: int | None = None,
    wait: bool = False,
) -> dict:
    """Kopiert ein Video als TV-taugliche MP4 in den Ordner „Jetzt schauen“ (Original bleibt unberührt)."""
    if not pool_mounted():
        raise RuntimeError("Pool ist nicht eingehängt")
    src = safe_pool_path(src_rel)
    if not src.is_file():
        raise FileNotFoundError("Quelldatei nicht gefunden")

    cfg = Einstellungen.load()
    tdir = ensure_temp_dir()
    base_name = mediathek_providers.sanitize_name(titel or src.stem, src.stem) + ".mp4"
    # Wenn schon gleiche Quelldatei aktiv im TV-Ordner liegt, wiederverwenden
    for existing in mediathek_db.list_tv_items():
        if existing["quelle_pfad"] == src_rel and existing["status"] in ("kopiere", "umwandlung", "bereit"):
            epath = safe_pool_path(existing["ziel_pfad"])
            if epath.is_file() or existing["status"] != "bereit":
                return existing

    dest = _unique_flat_tv_dest(tdir, base_name)
    ziel_rel = str(dest.relative_to(pool_root()))
    hours = cfg.video_ablauf_stunden if ablauf_stunden is None else ablauf_stunden
    ablauf_am = time.time() + hours * 3600 if hours and hours > 0 else None

    needs_conv = needs_conversion(src)
    init_status = "umwandlung" if needs_conv else "kopiere"
    item_id = mediathek_db.upsert_tv_item(
        art=art,
        quelle_pfad=src_rel,
        ziel_pfad=ziel_rel,
        titel=titel or src.stem,
        datei_id=datei_id,
        gruppe=titel or src.stem,
        status=init_status,
        fortschritt=5,
        groesse=src.stat().st_size,
        ablauf_am=ablauf_am,
    )

    def _job():
        try:
            if needs_conv:
                ok, err, _ = convert_to_mp4(src, dest, delete_src=False)
                if not ok:
                    mediathek_db.update_tv_item(item_id, status="fehler", fehler=err)
                    cleanup_temp_dir_if_empty()
                    return
            else:
                _copy_chunked(src, dest, item_id)
            mediathek_db.update_tv_item(item_id, status="bereit", fortschritt=100, groesse=dest.stat().st_size)
        except Exception as exc:  # noqa: BLE001
            mediathek_db.update_tv_item(item_id, status="fehler", fehler=str(exc))
            cleanup_temp_dir_if_empty()

    if wait:
        _job()
    else:
        t = threading.Thread(target=_job, name=f"tv-stage-{item_id}", daemon=True)
        _tv_threads[item_id] = t
        t.start()
    return mediathek_db.get_tv_item(item_id) or {}


def stage_images(
    src_rels: list[str],
    *,
    gruppe: str = "",
    ablauf_tage: int | None = None,
) -> list[dict]:
    """Kopiert mehrere ausgewählte Bilder direkt nach „Jetzt schauen“ (längere Haltedauer)."""
    if not pool_mounted():
        raise RuntimeError("Pool ist nicht eingehängt")
    if not src_rels:
        return []
    cfg = Einstellungen.load()
    tdir = ensure_temp_dir()
    days = cfg.bild_ablauf_tage if ablauf_tage is None else ablauf_tage
    ablauf_am = time.time() + days * 86400 if days and days > 0 else None
    batch_name = gruppe or time.strftime("Bilder %d.%m. %H:%M")

    existing_by_src = {x["quelle_pfad"]: x for x in mediathek_db.list_tv_items() if x["art"] == "bild"}
    out: list[dict] = []
    for rel in src_rels:
        src = safe_pool_path(rel)
        if not src.is_file() or src.suffix.lower() not in IMAGE_EXTS:
            continue
        prev = existing_by_src.get(rel)
        if prev and safe_pool_path(prev["ziel_pfad"]).is_file():
            mediathek_db.update_tv_item(prev["id"], ablauf_am=ablauf_am)
            out.append(mediathek_db.get_tv_item(prev["id"]) or prev)
            continue
        dest = _unique_flat_tv_dest(tdir, src.name)
        tmp = tdir / f".{dest.name[:160]}.tvcopy-{secrets.token_hex(4)}"
        try:
            shutil.copyfile(src, tmp)
            _besitzer(tmp)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        ziel_rel = str(dest.relative_to(pool_root()))
        item_id = mediathek_db.upsert_tv_item(
            art="bild",
            quelle_pfad=rel,
            ziel_pfad=ziel_rel,
            titel=src.name,
            gruppe=batch_name,
            status="bereit",
            fortschritt=100,
            groesse=dest.stat().st_size,
            ablauf_am=ablauf_am,
        )
        item = mediathek_db.get_tv_item(item_id)
        if item:
            out.append(item)
    return out


def remove_staged(item_id: int) -> bool:
    item = mediathek_db.delete_tv_item(item_id)
    if not item:
        return False
    try:
        target = safe_pool_path(item["ziel_pfad"])
        target.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass
    cleanup_temp_dir_if_empty()
    return True


def clear_staged(art: str | None = None) -> int:
    count = 0
    for item in mediathek_db.list_tv_items():
        if art and item["art"] != art:
            continue
        if remove_staged(item["id"]):
            count += 1
    cleanup_temp_dir_if_empty()
    return count


def cleanup_expired(now: float | None = None) -> int:
    if not pool_mounted():
        return 0
    removed = 0
    for item in mediathek_db.expired_tv_items(now):
        if remove_staged(item["id"]):
            removed += 1
    if not mediathek_db.list_tv_items():
        cleanup_temp_dir_if_empty()
    return removed


# ---------- Persönliche Medien durchsuchen (nur erlaubte Freigaben) ----------

def list_personal_media(
    shares: list[tuple[freigaben.Freigabe, Path]],
    kind: str = "all",
    share_filter: str = "",
    limit: int = 300,
) -> list[dict]:
    """Listet Videos und Bilder aus freigegebenen Ordnern (neueste zuerst)."""
    if not pool_mounted():
        return []
    cfg = Einstellungen.load()
    root = pool_root().resolve()
    skip_top = {cfg.temp_ordner, "wiederhergestellt", ".mergerfs"}
    streaming_top = cfg.streaming_ordner

    items: list[dict] = []
    seen_real: set[str] = set()
    for f, share_root in shares:
        if share_filter and f.name.lower() != share_filter.lower():
            continue
        if not share_root.is_dir():
            continue
        s_res = share_root.resolve()
        stack = [s_res]
        while stack:
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    entries = list(it)
            except OSError:
                continue
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if cur == root and entry.name in skip_top:
                    continue
                if cur == root and entry.name == streaming_top and f.ordner == "":
                    # Im Gesamtpool-Share nicht die Filme/Serien unter persönliche Videos mischen
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    ext = Path(entry.name).suffix.lower()
                    if ext in VIDEO_EXTS:
                        media_kind = "video"
                    elif ext in IMAGE_EXTS:
                        media_kind = "image"
                    else:
                        continue
                    if kind in ("video", "image") and media_kind != kind:
                        continue
                    ep = Path(entry.path).resolve()
                    real_key = str(ep)
                    if real_key in seen_real:
                        continue
                    seen_real.add(real_key)
                    st = entry.stat(follow_symlinks=False)
                    pool_rel = str(ep.relative_to(root))
                    share_rel = str(ep.relative_to(s_res))
                    items.append({
                        "share": f.name,
                        "share_rel": share_rel,
                        "pool_rel": pool_rel,
                        "name": entry.name,
                        "kind": media_kind,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "mime": mimetypes.guess_type(entry.name)[0] or ("video/mp4" if media_kind == "video" else "image/jpeg"),
                    })
                except (OSError, ValueError):
                    continue
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:limit]
