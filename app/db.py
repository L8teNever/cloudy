import ipaddress
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


def _is_ip(val: str | None) -> bool:
    if not val:
        return False
    try:
        ipaddress.ip_address(val)
        return True
    except ValueError:
        return False


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

            -- Welches Gerät (MAC) über welchen WARP-Account ins Internet geht.
            CREATE TABLE IF NOT EXISTS device_warp (
                mac TEXT PRIMARY KEY,
                slot TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS warp_labels (
                slot TEXT PRIMARY KEY,
                label TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS whitelist (
                domain TEXT PRIMARY KEY,
                added INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS redirects (
                source TEXT PRIMARY KEY,
                target TEXT NOT NULL,
                created_at INTEGER NOT NULL
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


def device_warp() -> dict[str, str]:
    """MAC -> WARP-Slot"""
    with closing(_connect()) as conn:
        return {r["mac"]: r["slot"] for r in conn.execute("SELECT mac, slot FROM device_warp")}


def set_device_warp(mac: str, slot: str | None) -> None:
    with closing(_connect()) as conn:
        if slot:
            conn.execute(
                "INSERT INTO device_warp (mac, slot) VALUES (?, ?) ON CONFLICT (mac) DO UPDATE SET slot = excluded.slot",
                (mac, slot),
            )
        else:
            conn.execute("DELETE FROM device_warp WHERE mac = ?", (mac,))
        conn.commit()


def get_setting(key: str, default: str = "") -> str:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with closing(_connect()) as conn:
        if value:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        else:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()


def warp_labels() -> dict[str, str]:
    with closing(_connect()) as conn:
        return {r["slot"]: r["label"] for r in conn.execute("SELECT slot, label FROM warp_labels")}


def set_warp_label(slot: str, label: str) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO warp_labels (slot, label) VALUES (?, ?) ON CONFLICT (slot) DO UPDATE SET label = excluded.label",
            (slot, label),
        )
        conn.commit()


def whitelist() -> list[str]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT domain FROM whitelist ORDER BY domain ASC").fetchall()
    return [r["domain"] for r in rows]


def add_whitelist(domain: str) -> None:
    domain = domain.strip().lower().rstrip(".")
    if not domain:
        return
    now = int(time.time())
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO whitelist (domain, added) VALUES (?, ?)",
            (domain, now),
        )
        conn.commit()


def remove_whitelist(domain: str) -> None:
    domain = domain.strip().lower().rstrip(".")
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM whitelist WHERE domain = ?", (domain,))
        conn.commit()


def known_ips() -> list[str]:
    """Alle Geräte, die der Pi kennt (DHCP, DNS-Anfragen, Datenverkehr)."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT ip FROM devices UNION SELECT ip FROM domain_stats UNION SELECT ip FROM traffic"
        ).fetchall()
    return [r["ip"] for r in rows]


def current_sites() -> dict[str, dict]:
    """Für jedes Gerät die zuletzt aufgerufene Domain/Seite mit Zeitstempel und optionaler Ziel-IP."""
    with closing(_connect()) as conn:
        q_rows = conn.execute(
            """
            SELECT ip, domain, ts FROM (
                SELECT ip, domain, ts, ROW_NUMBER() OVER (PARTITION BY ip ORDER BY id DESC) AS rn
                FROM queries
            ) WHERE rn = 1
            """
        ).fetchall()
        t_rows = conn.execute(
            """
            SELECT ip, target, last_seen FROM (
                SELECT ip, target, last_seen, ROW_NUMBER() OVER (PARTITION BY ip ORDER BY last_seen DESC) AS rn
                FROM traffic
            ) WHERE rn = 1
            """
        ).fetchall()
    result = {}
    for r in q_rows:
        result[r["ip"]] = {"domain": r["domain"], "ip": None, "ts": r["ts"]}

    for r in t_rows:
        dev_ip = r["ip"]
        target = r["target"]
        ts = r["last_seen"]
        target_is_ip = _is_ip(target)

        if dev_ip not in result:
            result[dev_ip] = {
                "domain": target,
                "ip": target if target_is_ip else None,
                "ts": ts,
            }
        else:
            prev = result[dev_ip]
            if target_is_ip:
                # Target im Datenverkehr ist eine IP (z. B. 142.250.1.1):
                # Wir behalten die Domain aus der DNS-Anfrage, merken uns die Ziel-IP
                result[dev_ip] = {
                    "domain": prev["domain"],
                    "ip": target,
                    "ts": max(prev["ts"], ts),
                }
            else:
                # Target ist bereits ein Domainname
                if ts >= prev["ts"]:
                    result[dev_ip] = {
                        "domain": target,
                        "ip": None,
                        "ts": ts,
                    }
    return result


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
    sites = current_sites()
    return [{**dict(r), "current_site": sites.get(r["ip"])} for r in rows]


def device(ip: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT ip, mac, hostname FROM devices WHERE ip = ?", (ip,)).fetchone()
    res = dict(row) if row else {"ip": ip, "mac": None, "hostname": None}
    sites = current_sites()
    res["current_site"] = sites.get(ip)
    return res


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

def redirects() -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT source, target, created_at FROM redirects ORDER BY source ASC").fetchall()
        return [dict(r) for r in rows]


def add_redirect(source: str, target: str) -> None:
    source = source.strip().lower().lstrip("http://").lstrip("https://").rstrip("/")
    target = target.strip()
    if not source or not target:
        return
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO redirects (source, target, created_at) VALUES (?, ?, ?)",
            (source, target, int(time.time())),
        )
        conn.commit()


def remove_redirect(source: str) -> None:
    source = source.strip().lower().lstrip("http://").lstrip("https://").rstrip("/")
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM redirects WHERE source = ?", (source,))
        conn.commit()


def redirect_map() -> dict[str, str]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT source, target FROM redirects").fetchall()
        return {r["source"].lower(): r["target"] for r in rows}

