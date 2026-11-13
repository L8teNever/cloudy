"""SQLite-Datenbank und Einstellungen für Streaming-Mediathek & Schnell-TV."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config, freigaben, pool
from .config import data_dir

_DB_LOCK = threading.RLock()


@dataclass
class Einstellungen:
    tmdb_key: str = ""
    streaming_ordner: str = "Streaming"
    medien_ordner: str = "Medien"
    temp_ordner: str = "Jetzt schauen"
    video_ablauf_stunden: int = 6
    bild_ablauf_tage: int = 7
    auto_entfernen_nach_player: bool = False
    quellen: list[str] = field(default_factory=lambda: ["tmdb", "tvmaze", "anilist"])

    @classmethod
    def load(cls) -> "Einstellungen":
        try:
            raw = json.loads((data_dir() / "mediathek.json").read_text())
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        self.streaming_ordner = freigaben.check_ordner(self.streaming_ordner or "Streaming") or "Streaming"
        self.medien_ordner = freigaben.check_ordner(self.medien_ordner or "Medien") or "Medien"
        self.temp_ordner = freigaben.check_ordner(self.temp_ordner or "Jetzt schauen") or "Jetzt schauen"
        self.video_ablauf_stunden = max(0, min(720, int(self.video_ablauf_stunden or 0)))
        self.bild_ablauf_tage = max(0, min(365, int(self.bild_ablauf_tage or 0)))
        valid_q = [q for q in (self.quellen or []) if q in ("tmdb", "tvmaze", "anilist")]
        self.quellen = valid_q or ["tmdb", "tvmaze", "anilist"]
        config.write_private(data_dir() / "mediathek.json", json.dumps(asdict(self), indent=2, ensure_ascii=False))

    def effective_tmdb_key(self) -> str:
        return (self.tmdb_key or os.environ.get("TMDB_API_KEY", "")).strip()


def db_path() -> Path:
    return data_dir() / "mediathek.db"


def _connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS titel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref TEXT UNIQUE NOT NULL,
            provider TEXT NOT NULL,
            typ TEXT NOT NULL,
            anime INTEGER NOT NULL DEFAULT 0,
            name TEXT NOT NULL,
            jahr INTEGER,
            poster TEXT NOT NULL DEFAULT '',
            backdrop TEXT NOT NULL DEFAULT '',
            zusammenfassung TEXT NOT NULL DEFAULT '',
            genres_json TEXT NOT NULL DEFAULT '[]',
            watch_json TEXT NOT NULL DEFAULT '{}',
            meta_json TEXT NOT NULL DEFAULT '{}',
            erstellt REAL NOT NULL,
            aktualisiert REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dateien (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titel_id INTEGER NOT NULL REFERENCES titel(id) ON DELETE CASCADE,
            staffel INTEGER,
            folge INTEGER,
            folgentitel TEXT NOT NULL DEFAULT '',
            rel_pfad TEXT UNIQUE NOT NULL,
            roh_pfad TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'bereit',
            fehler TEXT NOT NULL DEFAULT '',
            groesse INTEGER NOT NULL DEFAULT 0,
            dauer INTEGER,
            video_codec TEXT NOT NULL DEFAULT '',
            audio_codec TEXT NOT NULL DEFAULT '',
            hinzugefuegt REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_dateien_titel ON dateien(titel_id, staffel, folge);
        CREATE INDEX IF NOT EXISTS idx_dateien_status ON dateien(status);

        CREATE TABLE IF NOT EXISTS schnell_tv (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            art TEXT NOT NULL,
            quelle_pfad TEXT NOT NULL,
            ziel_pfad TEXT UNIQUE NOT NULL,
            titel TEXT NOT NULL DEFAULT '',
            datei_id INTEGER REFERENCES dateien(id) ON DELETE SET NULL,
            gruppe TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'bereit',
            fortschritt INTEGER NOT NULL DEFAULT 100,
            fehler TEXT NOT NULL DEFAULT '',
            groesse INTEGER NOT NULL DEFAULT 0,
            erstellt REAL NOT NULL,
            ablauf_am REAL
        );

        CREATE INDEX IF NOT EXISTS idx_schnell_tv_ablauf ON schnell_tv(ablauf_am);
        """
    )


@contextmanager
def db():
    with _DB_LOCK:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _row_to_title(row: sqlite3.Row) -> dict:
    meta = _json_loads(row["meta_json"], {})
    watch = _json_loads(row["watch_json"], {})
    genres = _json_loads(row["genres_json"], [])
    return {
        "id": row["id"],
        "ref": row["ref"],
        "provider": row["provider"],
        "type": row["typ"],
        "anime": bool(row["anime"]),
        "name": row["name"],
        "year": row["jahr"],
        "poster": row["poster"],
        "backdrop": row["backdrop"],
        "summary": row["zusammenfassung"],
        "genres": genres,
        "watch": watch,
        "meta": meta,
        "erstellt": row["erstellt"],
        "aktualisiert": row["aktualisiert"],
    }


def _row_to_file(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "titel_id": row["titel_id"],
        "staffel": row["staffel"],
        "folge": row["folge"],
        "folgentitel": row["folgentitel"],
        "rel_pfad": row["rel_pfad"],
        "roh_pfad": row["roh_pfad"],
        "status": row["status"],
        "fehler": row["fehler"],
        "groesse": row["groesse"],
        "dauer": row["dauer"],
        "video_codec": row["video_codec"],
        "audio_codec": row["audio_codec"],
        "hinzugefuegt": row["hinzugefuegt"],
    }


def _row_to_tv(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "art": row["art"],
        "quelle_pfad": row["quelle_pfad"],
        "ziel_pfad": row["ziel_pfad"],
        "titel": row["titel"],
        "datei_id": row["datei_id"],
        "gruppe": row["gruppe"],
        "status": row["status"],
        "fortschritt": row["fortschritt"],
        "fehler": row["fehler"],
        "groesse": row["groesse"],
        "erstellt": row["erstellt"],
        "ablauf_am": row["ablauf_am"],
    }


def _json_loads(raw: str, default):
    try:
        return json.loads(raw) if raw else default
    except ValueError:
        return default


def upsert_title(meta: dict) -> int:
    """Speichert oder aktualisiert einen Titel aus mediathek_providers.full()/details()."""
    now = time.time()
    ref = str(meta["ref"])
    provider = str(meta.get("provider") or ref.partition(":")[0])
    typ = "movie" if meta.get("type") == "movie" else "show"
    anime = 1 if meta.get("anime") else 0
    name = str(meta.get("name") or "Unbenannt")
    jahr = meta.get("year")
    poster = str(meta.get("poster") or "")
    backdrop = str(meta.get("backdrop") or "")
    summary = str(meta.get("summary") or "")
    genres_json = json.dumps(meta.get("genres") or [], ensure_ascii=False)
    watch_json = json.dumps(meta.get("watch") or {}, ensure_ascii=False)
    slim_meta = {
        k: meta.get(k)
        for k in ("altNames", "network", "status", "score", "totalEpisodes", "seasons", "runtime", "url", "ratings", "trailer", "cast", "episodes")
        if meta.get(k) is not None
    }
    meta_json = json.dumps(slim_meta, ensure_ascii=False)

    with db() as conn:
        existing = conn.execute("SELECT id, watch_json, meta_json FROM titel WHERE ref = ?", (ref,)).fetchone()
        if existing:
            if not meta.get("watch") and existing["watch_json"] != "{}":
                watch_json = existing["watch_json"]
            if not slim_meta.get("episodes") and existing["meta_json"] != "{}":
                old_meta = _json_loads(existing["meta_json"], {})
                for k in ("episodes", "cast", "ratings", "trailer", "seasons"):
                    if not slim_meta.get(k) and old_meta.get(k):
                        slim_meta[k] = old_meta[k]
                meta_json = json.dumps(slim_meta, ensure_ascii=False)
            conn.execute(
                """UPDATE titel SET provider=?, typ=?, anime=?, name=?, jahr=?, poster=?, backdrop=?,
                   zusammenfassung=?, genres_json=?, watch_json=?, meta_json=?, aktualisiert=? WHERE id=?""",
                (provider, typ, anime, name, jahr, poster, backdrop, summary, genres_json, watch_json, meta_json, now, existing["id"]),
            )
            return int(existing["id"])
        cur = conn.execute(
            """INSERT INTO titel (ref, provider, typ, anime, name, jahr, poster, backdrop,
               zusammenfassung, genres_json, watch_json, meta_json, erstellt, aktualisiert)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ref, provider, typ, anime, name, jahr, poster, backdrop, summary, genres_json, watch_json, meta_json, now, now),
        )
        return int(cur.lastrowid)


def get_title_by_ref(ref: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM titel WHERE ref = ?", (ref,)).fetchone()
        if not row:
            return None
        files = conn.execute(
            "SELECT * FROM dateien WHERE titel_id = ? ORDER BY COALESCE(staffel, 0), COALESCE(folge, 0), id",
            (row["id"],),
        ).fetchall()
    out = _row_to_title(row)
    out["dateien"] = [_row_to_file(f) for f in files]
    return out


def get_title_by_id(titel_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM titel WHERE id = ?", (titel_id,)).fetchone()
        if not row:
            return None
        files = conn.execute(
            "SELECT * FROM dateien WHERE titel_id = ? ORDER BY COALESCE(staffel, 0), COALESCE(folge, 0), id",
            (row["id"],),
        ).fetchall()
    out = _row_to_title(row)
    out["dateien"] = [_row_to_file(f) for f in files]
    return out


def register_file(
    titel_id: int,
    rel_pfad: str,
    *,
    staffel: int | None = None,
    folge: int | None = None,
    folgentitel: str = "",
    roh_pfad: str = "",
    status: str = "bereit",
    groesse: int = 0,
    dauer: int | None = None,
    video_codec: str = "",
    audio_codec: str = "",
) -> int:
    now = time.time()
    with db() as conn:
        existing = conn.execute("SELECT id FROM dateien WHERE rel_pfad = ?", (rel_pfad,)).fetchone()
        if existing:
            conn.execute(
                """UPDATE dateien SET titel_id=?, staffel=?, folge=?, folgentitel=?, roh_pfad=?, status=?,
                   fehler='', groesse=?, dauer=?, video_codec=?, audio_codec=?, hinzugefuegt=? WHERE id=?""",
                (titel_id, staffel, folge, folgentitel, roh_pfad, status, groesse, dauer, video_codec, audio_codec, now, existing["id"]),
            )
            return int(existing["id"])
        cur = conn.execute(
            """INSERT INTO dateien (titel_id, staffel, folge, folgentitel, rel_pfad, roh_pfad, status,
               groesse, dauer, video_codec, audio_codec, hinzugefuegt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (titel_id, staffel, folge, folgentitel, rel_pfad, roh_pfad, status, groesse, dauer, video_codec, audio_codec, now),
        )
        return int(cur.lastrowid)


def update_file(file_id: int, **fields) -> None:
    allowed = {"status", "fehler", "groesse", "dauer", "video_codec", "audio_codec", "roh_pfad", "rel_pfad", "staffel", "folge", "folgentitel"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    cols = ", ".join(f"{k} = ?" for k in updates)
    vals = [*updates.values(), file_id]
    with db() as conn:
        conn.execute(f"UPDATE dateien SET {cols} WHERE id = ?", vals)


def get_file(file_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM dateien WHERE id = ?", (file_id,)).fetchone()
        if not row:
            return None
        titel = conn.execute("SELECT * FROM titel WHERE id = ?", (row["titel_id"],)).fetchone()
    d = _row_to_file(row)
    d["titel"] = _row_to_title(titel) if titel else None
    return d


def delete_file_record(file_id: int) -> None:
    with db() as conn:
        row = conn.execute("SELECT titel_id FROM dateien WHERE id = ?", (file_id,)).fetchone()
        conn.execute("DELETE FROM dateien WHERE id = ?", (file_id,))
        if row:
            cnt = conn.execute("SELECT COUNT(*) FROM dateien WHERE titel_id = ?", (row["titel_id"],)).fetchone()[0]
            if cnt == 0:
                conn.execute("DELETE FROM titel WHERE id = ?", (row["titel_id"],))


def pending_files() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM dateien WHERE status IN ('umwandlung', 'hochladend') ORDER BY id").fetchall()
    return [_row_to_file(r) for r in rows]


def list_local_library() -> list[dict]:
    """Alle Titel mit mindestens einer zugeordneten Datei."""
    with db() as conn:
        t_rows = conn.execute(
            """SELECT t.*, COUNT(d.id) AS file_count, MAX(d.hinzugefuegt) AS last_added
               FROM titel t JOIN dateien d ON d.titel_id = t.id
               GROUP BY t.id
               ORDER BY last_added DESC, t.name COLLATE NOCASE"""
        ).fetchall()
        f_rows = conn.execute("SELECT * FROM dateien ORDER BY COALESCE(staffel, 0), COALESCE(folge, 0), id").fetchall()
    by_title: dict[int, list[dict]] = {}
    for fr in f_rows:
        by_title.setdefault(fr["titel_id"], []).append(_row_to_file(fr))
    out = []
    for tr in t_rows:
        item = _row_to_title(tr)
        item["dateien"] = by_title.get(tr["id"], [])
        item["ready_count"] = sum(1 for f in item["dateien"] if f["status"] == "bereit")
        item["busy_count"] = sum(1 for f in item["dateien"] if f["status"] in ("hochladend", "umwandlung"))
        out.append(item)
    return out


def local_availability_map(refs: list[str] | None = None) -> dict[str, dict]:
    """Map ref -> {ready_count, busy_count, total_count} für Suchergebnis-Badges."""
    with db() as conn:
        if refs:
            placeholders = ",".join("?" for _ in refs)
            rows = conn.execute(
                f"""SELECT t.ref,
                           SUM(CASE WHEN d.status = 'bereit' THEN 1 ELSE 0 END) AS ready_count,
                           SUM(CASE WHEN d.status IN ('hochladend', 'umwandlung') THEN 1 ELSE 0 END) AS busy_count,
                           COUNT(d.id) AS total_count
                    FROM titel t JOIN dateien d ON d.titel_id = t.id
                    WHERE t.ref IN ({placeholders})
                    GROUP BY t.ref""",
                refs,
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT t.ref,
                          SUM(CASE WHEN d.status = 'bereit' THEN 1 ELSE 0 END) AS ready_count,
                          SUM(CASE WHEN d.status IN ('hochladend', 'umwandlung') THEN 1 ELSE 0 END) AS busy_count,
                          COUNT(d.id) AS total_count
                   FROM titel t JOIN dateien d ON d.titel_id = t.id
                   GROUP BY t.ref"""
            ).fetchall()
    return {
        r["ref"]: {
            "ready_count": int(r["ready_count"] or 0),
            "busy_count": int(r["busy_count"] or 0),
            "total_count": int(r["total_count"] or 0),
        }
        for r in rows
    }


def sync_disk(pool_root: Path) -> int:
    """Entfernt DB-Einträge für Dateien, die auf der Platte nicht mehr existieren (nur wenn Pool eingehängt)."""
    if str(pool_root) not in pool.mounts():
        return 0
    removed = 0
    with db() as conn:
        rows = conn.execute("SELECT id, rel_pfad, roh_pfad, status, groesse FROM dateien").fetchall()
        for r in rows:
            rel_p = pool_root / r["rel_pfad"]
            roh_p = (pool_root / r["roh_pfad"]) if r["roh_pfad"] else None
            if r["status"] == "bereit" and not rel_p.is_file():
                conn.execute("DELETE FROM dateien WHERE id = ?", (r["id"],))
                removed += 1
            elif r["status"] in ("umwandlung", "fehler"):
                has_any = rel_p.is_file() or (roh_p is not None and roh_p.is_file())
                if not has_any:
                    conn.execute("DELETE FROM dateien WHERE id = ?", (r["id"],))
                    removed += 1
            elif r["status"] == "bereit" and rel_p.is_file():
                try:
                    sz = rel_p.stat().st_size
                    if sz != r["groesse"]:
                        conn.execute("UPDATE dateien SET groesse = ? WHERE id = ?", (sz, r["id"]))
                except OSError:
                    pass
        conn.execute("DELETE FROM titel WHERE id NOT IN (SELECT DISTINCT titel_id FROM dateien)")

        tv_rows = conn.execute("SELECT id, ziel_pfad, status FROM schnell_tv").fetchall()
        for r in tv_rows:
            if r["status"] == "bereit" and not (pool_root / r["ziel_pfad"]).is_file():
                conn.execute("DELETE FROM schnell_tv WHERE id = ?", (r["id"],))
    return removed


# ---------- Schnell-TV (Jetzt schauen) ----------

def upsert_tv_item(
    *,
    art: str,
    quelle_pfad: str,
    ziel_pfad: str,
    titel: str,
    datei_id: int | None = None,
    gruppe: str = "",
    status: str = "kopiere",
    fortschritt: int = 0,
    groesse: int = 0,
    ablauf_am: float | None = None,
) -> int:
    now = time.time()
    with db() as conn:
        existing = conn.execute("SELECT id FROM schnell_tv WHERE ziel_pfad = ?", (ziel_pfad,)).fetchone()
        if existing:
            conn.execute(
                """UPDATE schnell_tv SET art=?, quelle_pfad=?, titel=?, datei_id=?, gruppe=?, status=?,
                   fortschritt=?, fehler='', groesse=?, erstellt=?, ablauf_am=? WHERE id=?""",
                (art, quelle_pfad, titel, datei_id, gruppe, status, fortschritt, groesse, now, ablauf_am, existing["id"]),
            )
            return int(existing["id"])
        cur = conn.execute(
            """INSERT INTO schnell_tv (art, quelle_pfad, ziel_pfad, titel, datei_id, gruppe, status, fortschritt, groesse, erstellt, ablauf_am)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (art, quelle_pfad, ziel_pfad, titel, datei_id, gruppe, status, fortschritt, groesse, now, ablauf_am),
        )
        return int(cur.lastrowid)


def update_tv_item(item_id: int, **fields) -> None:
    allowed = {"status", "fortschritt", "fehler", "groesse", "ablauf_am"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    cols = ", ".join(f"{k} = ?" for k in updates)
    with db() as conn:
        conn.execute(f"UPDATE schnell_tv SET {cols} WHERE id = ?", [*updates.values(), item_id])


def get_tv_item(item_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM schnell_tv WHERE id = ?", (item_id,)).fetchone()
        return _row_to_tv(row) if row else None


def list_tv_items() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM schnell_tv ORDER BY erstellt DESC, id DESC").fetchall()
    return [_row_to_tv(r) for r in rows]


def delete_tv_item(item_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM schnell_tv WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM schnell_tv WHERE id = ?", (item_id,))
        return _row_to_tv(row)


def expired_tv_items(now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM schnell_tv WHERE ablauf_am IS NOT NULL AND ablauf_am <= ? AND status IN ('bereit', 'fehler')",
            (now,),
        ).fetchall()
    return [_row_to_tv(r) for r in rows]
