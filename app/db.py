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

            -- Statistik: pro Stunde und Gerät bzw. pro Tag, Gerät und Ziel (für Diagramme).
            CREATE TABLE IF NOT EXISTS traffic_hourly (
                hour INTEGER NOT NULL, ip TEXT NOT NULL,
                bytes_up INTEGER NOT NULL, bytes_down INTEGER NOT NULL,
                PRIMARY KEY (hour, ip)
            );
            CREATE TABLE IF NOT EXISTS traffic_daily (
                day TEXT NOT NULL, ip TEXT NOT NULL, target TEXT NOT NULL,
                bytes_up INTEGER NOT NULL, bytes_down INTEGER NOT NULL, conns INTEGER NOT NULL,
                PRIMARY KEY (day, ip, target)
            );
            CREATE TABLE IF NOT EXISTS dns_hourly (
                hour INTEGER NOT NULL, ip TEXT NOT NULL, hits INTEGER NOT NULL,
                PRIMARY KEY (hour, ip)
            );
            CREATE TABLE IF NOT EXISTS dns_daily (
                day TEXT NOT NULL, ip TEXT NOT NULL, domain TEXT NOT NULL, hits INTEGER NOT NULL,
                PRIMARY KEY (day, ip, domain)
            );

            -- Geräte (MAC), die nur ins Internet dürfen und sonst niemanden im Heimnetz sehen.
            CREATE TABLE IF NOT EXISTS device_isolated (
                mac TEXT PRIMARY KEY,
                added INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS redirects (
                source TEXT PRIMARY KEY,
                target TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        # Alte DB: gleiches Gerät unter mehreren IPs (MAC doppelt) → zusammenführen.
        _reconcile_duplicate_macs(conn)
        conn.commit()


def hour_of(ts: int) -> int:
    return int(ts) - int(ts) % 3600


def day_of(ts: int) -> str:
    """Kalendertag in Ortszeit (TZ aus dem Container, z. B. Europe/Berlin)."""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _prune_rollups(conn: sqlite3.Connection) -> None:
    cutoff = int(time.time()) - retention_days() * 86400
    conn.execute("DELETE FROM traffic_hourly WHERE hour < ?", (cutoff,))
    conn.execute("DELETE FROM dns_hourly WHERE hour < ?", (cutoff,))
    conn.execute("DELETE FROM traffic_daily WHERE day < ?", (day_of(cutoff),))
    conn.execute("DELETE FROM dns_daily WHERE day < ?", (day_of(cutoff),))


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
        conn.executemany(
            """
            INSERT INTO dns_hourly (hour, ip, hits) VALUES (?, ?, 1)
            ON CONFLICT (hour, ip) DO UPDATE SET hits = hits + 1
            """,
            [(hour_of(ts), ip) for ts, ip, _, _ in rows],
        )
        conn.executemany(
            """
            INSERT INTO dns_daily (day, ip, domain, hits) VALUES (?, ?, ?, 1)
            ON CONFLICT (day, ip, domain) DO UPDATE SET hits = hits + 1
            """,
            [(day_of(ts), ip, domain) for ts, ip, domain, _ in rows],
        )
        _prune_rollups(conn)
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
        conn.executemany(
            """
            INSERT INTO traffic_hourly (hour, ip, bytes_up, bytes_down) VALUES (?, ?, ?, ?)
            ON CONFLICT (hour, ip) DO UPDATE SET
                bytes_up = bytes_up + excluded.bytes_up, bytes_down = bytes_down + excluded.bytes_down
            """,
            [(hour_of(ts), ip, up, down) for ip, _, up, down, _, ts in rows],
        )
        conn.executemany(
            """
            INSERT INTO traffic_daily (day, ip, target, bytes_up, bytes_down, conns) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (day, ip, target) DO UPDATE SET
                bytes_up = bytes_up + excluded.bytes_up, bytes_down = bytes_down + excluded.bytes_down,
                conns = conns + excluded.conns
            """,
            [(day_of(ts), ip, target, up, down, conns) for ip, target, up, down, conns, ts in rows],
        )
        _prune_rollups(conn)
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


def _merge_ip_into(conn: sqlite3.Connection, old_ip: str, new_ip: str) -> None:
    """Verschiebt Stats/Traffic von old_ip nach new_ip (gleiches Gerät, neue Adresse)."""
    if old_ip == new_ip:
        return

    conn.execute(
        """
        INSERT INTO domain_stats (ip, domain, hits, first_seen, last_seen)
        SELECT ?, domain, hits, first_seen, last_seen FROM domain_stats WHERE ip = ?
        ON CONFLICT (ip, domain) DO UPDATE SET
            hits = domain_stats.hits + excluded.hits,
            first_seen = MIN(domain_stats.first_seen, excluded.first_seen),
            last_seen = MAX(domain_stats.last_seen, excluded.last_seen)
        """,
        (new_ip, old_ip),
    )
    conn.execute("DELETE FROM domain_stats WHERE ip = ?", (old_ip,))

    conn.execute(
        """
        INSERT INTO traffic (ip, target, bytes_up, bytes_down, conns, first_seen, last_seen)
        SELECT ?, target, bytes_up, bytes_down, conns, first_seen, last_seen FROM traffic WHERE ip = ?
        ON CONFLICT (ip, target) DO UPDATE SET
            bytes_up = traffic.bytes_up + excluded.bytes_up,
            bytes_down = traffic.bytes_down + excluded.bytes_down,
            conns = traffic.conns + excluded.conns,
            first_seen = MIN(traffic.first_seen, excluded.first_seen),
            last_seen = MAX(traffic.last_seen, excluded.last_seen)
        """,
        (new_ip, old_ip),
    )
    conn.execute("DELETE FROM traffic WHERE ip = ?", (old_ip,))

    conn.execute("UPDATE queries SET ip = ? WHERE ip = ?", (new_ip, old_ip))

    conn.execute(
        """
        INSERT INTO dns_hourly (hour, ip, hits)
        SELECT hour, ?, hits FROM dns_hourly WHERE ip = ?
        ON CONFLICT (hour, ip) DO UPDATE SET hits = dns_hourly.hits + excluded.hits
        """,
        (new_ip, old_ip),
    )
    conn.execute("DELETE FROM dns_hourly WHERE ip = ?", (old_ip,))

    conn.execute(
        """
        INSERT INTO dns_daily (day, ip, domain, hits)
        SELECT day, ?, domain, hits FROM dns_daily WHERE ip = ?
        ON CONFLICT (day, ip, domain) DO UPDATE SET hits = dns_daily.hits + excluded.hits
        """,
        (new_ip, old_ip),
    )
    conn.execute("DELETE FROM dns_daily WHERE ip = ?", (old_ip,))

    conn.execute(
        """
        INSERT INTO traffic_hourly (hour, ip, bytes_up, bytes_down)
        SELECT hour, ?, bytes_up, bytes_down FROM traffic_hourly WHERE ip = ?
        ON CONFLICT (hour, ip) DO UPDATE SET
            bytes_up = traffic_hourly.bytes_up + excluded.bytes_up,
            bytes_down = traffic_hourly.bytes_down + excluded.bytes_down
        """,
        (new_ip, old_ip),
    )
    conn.execute("DELETE FROM traffic_hourly WHERE ip = ?", (old_ip,))

    conn.execute(
        """
        INSERT INTO traffic_daily (day, ip, target, bytes_up, bytes_down, conns)
        SELECT day, ?, target, bytes_up, bytes_down, conns FROM traffic_daily WHERE ip = ?
        ON CONFLICT (day, ip, target) DO UPDATE SET
            bytes_up = traffic_daily.bytes_up + excluded.bytes_up,
            bytes_down = traffic_daily.bytes_down + excluded.bytes_down,
            conns = traffic_daily.conns + excluded.conns
        """,
        (new_ip, old_ip),
    )
    conn.execute("DELETE FROM traffic_daily WHERE ip = ?", (old_ip,))

    conn.execute("DELETE FROM devices WHERE ip = ?", (old_ip,))


def _reconcile_duplicate_macs(conn: sqlite3.Connection) -> None:
    """Pro MAC nur eine aktuelle IP behalten (neueste Lease); ältere IPs zusammenführen."""
    dupes = conn.execute(
        """
        SELECT mac FROM devices
        WHERE mac IS NOT NULL AND mac != ''
        GROUP BY mac HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in dupes:
        ips = conn.execute(
            "SELECT ip FROM devices WHERE mac = ? ORDER BY updated DESC, ip ASC",
            (row["mac"],),
        ).fetchall()
        keep = ips[0]["ip"]
        for other in ips[1:]:
            _merge_ip_into(conn, other["ip"], keep)


def upsert_devices(devices: list[tuple[str, str, str | None]]) -> None:
    """devices: (ip, mac, hostname) aus der DHCP-Lease-Datei.

    Identität ist die MAC: Bekommt ein Gerät eine neue IP, werden alte Einträge und Stats
    auf die neue Adresse zusammengeführt – sonst erscheinen Handy/Laptop mehrfach.
    """
    if not devices:
        return
    now = int(time.time())
    with closing(_connect()) as conn:
        for ip, mac, hostname in devices:
            mac = (mac or "").strip().lower()
            if mac:
                old_ips = [
                    r["ip"]
                    for r in conn.execute(
                        "SELECT ip FROM devices WHERE mac = ? AND ip != ?", (mac, ip)
                    ).fetchall()
                ]
                for old_ip in old_ips:
                    _merge_ip_into(conn, old_ip, ip)
            conn.execute(
                """
                INSERT INTO devices (ip, mac, hostname, updated) VALUES (?, ?, ?, ?)
                ON CONFLICT (ip) DO UPDATE SET
                    mac = excluded.mac,
                    hostname = COALESCE(excluded.hostname, devices.hostname),
                    updated = excluded.updated
                """,
                (ip, mac or None, hostname, now),
            )
        _reconcile_duplicate_macs(conn)
        conn.commit()


def ip_for_mac(mac: str) -> str | None:
    """Aktuelle IPv4-Adresse (Lease) eines Geräts, anhand der MAC."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT ip FROM devices WHERE mac = ? ORDER BY updated DESC LIMIT 1", ((mac or "").strip().lower(),)
        ).fetchone()
    return row["ip"] if row else None


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
                   MAX(COALESCE(dns.ls, 0), COALESCE(tr.ls, 0)) AS last_seen,
                   COALESCE(d.updated, 0) AS updated
            FROM ips
            LEFT JOIN dns ON dns.ip = ips.ip
            LEFT JOIN tr ON tr.ip = ips.ip
            LEFT JOIN devices d ON d.ip = ips.ip
            ORDER BY last_seen DESC
            """
        ).fetchall()
    sites = current_sites()
    # Pro MAC nur eine Zeile (aktuelle Lease); Stats ohne MAC bleiben eigene Zeilen.
    by_mac: dict[str, dict] = {}
    plain: list[dict] = []
    for r in rows:
        d = {**dict(r), "current_site": sites.get(r["ip"])}
        mac = (d.get("mac") or "").strip().lower()
        if not mac:
            plain.append(d)
            continue
        prev = by_mac.get(mac)
        if prev is None:
            by_mac[mac] = d
            continue
        # Neuere Lease / frischere Aktivität behalten, Zahlen addieren.
        keep, drop = (d, prev) if (d["updated"], d["last_seen"]) >= (prev["updated"], prev["last_seen"]) else (prev, d)
        site = keep.get("current_site")
        other = drop.get("current_site")
        if other and (not site or other.get("ts", 0) >= site.get("ts", 0)):
            site = other
        by_mac[mac] = {
            **keep,
            "hits": keep["hits"] + drop["hits"],
            "domains": keep["domains"] + drop["domains"],
            "bytes_up": keep["bytes_up"] + drop["bytes_up"],
            "bytes_down": keep["bytes_down"] + drop["bytes_down"],
            "last_seen": max(keep["last_seen"], drop["last_seen"]),
            "current_site": site,
        }
    out = plain + list(by_mac.values())
    out.sort(key=lambda x: x["last_seen"], reverse=True)
    return out


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
    """ValueError, wenn Quelle oder Ziel ungültig sind."""
    from .redirects import normalize_source, normalize_target

    source, target = normalize_source(source), normalize_target(target)
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO redirects (source, target, created_at) VALUES (?, ?, ?)",
            (source, target, int(time.time())),
        )
        conn.commit()


def remove_redirect(source: str) -> None:
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM redirects WHERE source = ?", (source.strip().lower(),))
        conn.commit()


def redirect_map() -> dict[str, str]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT source, target FROM redirects").fetchall()
        return {r["source"].lower(): r["target"] for r in rows}


def isolated_macs() -> set[str]:
    with closing(_connect()) as conn:
        return {r["mac"] for r in conn.execute("SELECT mac FROM device_isolated")}


def set_isolated(mac: str, isolated: bool) -> None:
    with closing(_connect()) as conn:
        if isolated:
            conn.execute(
                "INSERT INTO device_isolated (mac, added) VALUES (?, ?) ON CONFLICT (mac) DO NOTHING",
                (mac, int(time.time())),
            )
        else:
            conn.execute("DELETE FROM device_isolated WHERE mac = ?", (mac,))
        conn.commit()


def connect() -> sqlite3.Connection:
    """Für Lese-Abfragen anderer Module (z. B. Statistik)."""
    return _connect()
