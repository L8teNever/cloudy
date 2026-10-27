import os
import sqlite3
import time
from contextlib import closing


def db_path() -> str:
    return os.environ.get("DB_PATH", "/data/cloudy.db")


def max_rows() -> int:
    return int(os.environ.get("MAX_ROWS", "50000"))


def retention_days() -> int:
    return int(os.environ.get("RETENTION_DAYS", "30"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    os.makedirs(os.path.dirname(db_path()) or ".", exist_ok=True)
    with closing(_connect()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                ip TEXT NOT NULL,
                domain TEXT NOT NULL,
                qtype TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_queries_ip ON queries (ip, id);

            CREATE TABLE IF NOT EXISTS domain_stats (
                ip TEXT NOT NULL,
                domain TEXT NOT NULL,
                hits INTEGER NOT NULL,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                PRIMARY KEY (ip, domain)
            );

            CREATE TABLE IF NOT EXISTS traffic (
                ip TEXT NOT NULL,
                target TEXT NOT NULL,
                bytes_up INTEGER NOT NULL,
                bytes_down INTEGER NOT NULL,
                conns INTEGER NOT NULL,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                PRIMARY KEY (ip, target)
            );

            CREATE TABLE IF NOT EXISTS devices (
                ip TEXT PRIMARY KEY,
                mac TEXT,
                hostname TEXT,
                updated INTEGER NOT NULL
            );
            """
        )
        conn.commit()


def add_queries(rows: list[tuple[int, str, str, str]]) -> None:
    """rows: (ts, ip, domain, qtype)"""
    if not rows:
        return
    with closing(_connect()) as conn:
        conn.executemany("INSERT INTO queries (ts, ip, domain, qtype) VALUES (?, ?, ?, ?)", rows)
        conn.executemany(
            """
            INSERT INTO domain_stats (ip, domain, hits, first_seen, last_seen) VALUES (?, ?, 1, ?, ?)
            ON CONFLICT (ip, domain) DO UPDATE SET hits = hits + 1, last_seen = excluded.last_seen
            """,
            [(ip, domain, ts, ts) for ts, ip, domain, _ in rows],
        )
        # Nur die letzten MAX_ROWS Einzel-Aufrufe und Statistiken der letzten RETENTION_DAYS Tage behalten.
        conn.execute(
            "DELETE FROM queries WHERE id <= (SELECT MAX(id) FROM queries) - ?", (max_rows(),)
        )
        conn.execute(
            "DELETE FROM domain_stats WHERE last_seen < ?",
            (int(time.time()) - retention_days() * 86400,),
        )
        conn.commit()


def add_traffic(rows: list[tuple[str, str, int, int, int, int]]) -> None:
    """rows: (ip, ziel (Domain oder IP), bytes_up, bytes_down, neue Verbindungen, ts)"""
    if not rows:
        return
    with closing(_connect()) as conn:
        conn.executemany(
            """
            INSERT INTO traffic (ip, target, bytes_up, bytes_down, conns, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (ip, target) DO UPDATE SET
                bytes_up = bytes_up + excluded.bytes_up,
                bytes_down = bytes_down + excluded.bytes_down,
                conns = conns + excluded.conns,
                last_seen = excluded.last_seen
            """,
            [(ip, target, up, down, conns, ts, ts) for ip, target, up, down, conns, ts in rows],
        )
        conn.execute(
            "DELETE FROM traffic WHERE last_seen < ?", (int(time.time()) - retention_days() * 86400,)
        )
        conn.commit()


def traffic_for(ip: str, limit: int = 200) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT target, bytes_up, bytes_down, conns, first_seen, last_seen FROM traffic
            WHERE ip = ? ORDER BY bytes_up + bytes_down DESC LIMIT ?
            """,
            (ip, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_devices(devices: list[tuple[str, str, str | None]]) -> None:
    """devices: (ip, mac, hostname) aus der DHCP-Lease-Datei"""
    if not devices:
        return
    now = int(time.time())
    with closing(_connect()) as conn:
        conn.executemany(
            """
            INSERT INTO devices (ip, mac, hostname, updated) VALUES (?, ?, ?, ?)
            ON CONFLICT (ip) DO UPDATE SET
                mac = excluded.mac,
                hostname = COALESCE(excluded.hostname, devices.hostname),
                updated = excluded.updated
            """,
            [(ip, mac, hostname, now) for ip, mac, hostname in devices],
        )
        conn.commit()


def devices() -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            WITH ips AS (SELECT ip FROM domain_stats UNION SELECT ip FROM traffic),
                 dns AS (SELECT ip, SUM(hits) AS hits, COUNT(*) AS domains, MAX(last_seen) AS ls
                         FROM domain_stats GROUP BY ip),
                 tr AS (SELECT ip, SUM(bytes_up) AS up, SUM(bytes_down) AS down, MAX(last_seen) AS ls
                        FROM traffic GROUP BY ip)
            SELECT ips.ip AS ip, d.mac AS mac, d.hostname AS hostname,
                   COALESCE(dns.hits, 0) AS hits, COALESCE(dns.domains, 0) AS domains,
                   COALESCE(tr.up, 0) AS bytes_up, COALESCE(tr.down, 0) AS bytes_down,
                   MAX(COALESCE(dns.ls, 0), COALESCE(tr.ls, 0)) AS last_seen
            FROM ips
            LEFT JOIN dns ON dns.ip = ips.ip
            LEFT JOIN tr ON tr.ip = ips.ip
            LEFT JOIN devices d ON d.ip = ips.ip
            ORDER BY last_seen DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def device(ip: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT ip, mac, hostname FROM devices WHERE ip = ?", (ip,)).fetchone()
    return dict(row) if row else {"ip": ip, "mac": None, "hostname": None}


def top_domains(ip: str, limit: int = 200) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT domain, hits, first_seen, last_seen FROM domain_stats
            WHERE ip = ? ORDER BY hits DESC, last_seen DESC LIMIT ?
            """,
            (ip, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def recent(limit: int = 100, ip: str | None = None) -> list[dict]:
    where, params = ("WHERE q.ip = ?", [ip]) if ip else ("", [])
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"""
            SELECT q.ts, q.ip, q.domain, q.qtype, d.hostname
            FROM queries q LEFT JOIN devices d ON d.ip = q.ip
            {where}
            ORDER BY q.id DESC LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]
