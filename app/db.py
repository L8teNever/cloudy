import os
import sqlite3
import time
from contextlib import closing


def db_path() -> str:
    return os.environ.get("DB_PATH", "/data/access.db")


def max_rows() -> int:
    return int(os.environ.get("MAX_ROWS", "10000"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    os.makedirs(os.path.dirname(db_path()) or ".", exist_ok=True)
    with closing(_connect()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS access_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                ip TEXT NOT NULL,
                hostname TEXT,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                user_agent TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_access_log_ip ON access_log (ip)")
        conn.commit()


def insert(ip: str, hostname: str | None, method: str, path: str, user_agent: str | None) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO access_log (ts, ip, hostname, method, path, user_agent) VALUES (?, ?, ?, ?, ?, ?)",
            (int(time.time()), ip, hostname, method, path[:500], (user_agent or "")[:300]),
        )
        # Nur die letzten MAX_ROWS Einträge behalten.
        conn.execute(
            "DELETE FROM access_log WHERE id <= (SELECT MAX(id) FROM access_log) - ?",
            (max_rows(),),
        )
        conn.commit()


def devices() -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT a.ip AS ip,
                   MAX(a.hostname) AS hostname,
                   COUNT(*) AS hits,
                   MAX(a.ts) AS last_seen,
                   (SELECT b.user_agent FROM access_log b WHERE b.ip = a.ip ORDER BY b.id DESC LIMIT 1) AS user_agent
            FROM access_log a
            GROUP BY a.ip
            ORDER BY last_seen DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def recent(limit: int = 100) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT ts, ip, hostname, method, path, user_agent FROM access_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
