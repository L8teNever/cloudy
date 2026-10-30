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

            -- Einzelne Seiten (inkl. Subdomains), die für ALLE Geräte immer durch ein bestimmtes WARP-/VPN-Profil laufen.
            CREATE TABLE IF NOT EXISTS site_routes (
                domain TEXT PRIMARY KEY,
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
            -- Vom Werbeblocker beantwortete Anfragen (pro Tag, Gerät, Domain).
            CREATE TABLE IF NOT EXISTS ad_blocked_daily (
                day TEXT NOT NULL, ip TEXT NOT NULL, domain TEXT NOT NULL, hits INTEGER NOT NULL,
                PRIMARY KEY (day, ip, domain)
            );

            -- Geräte (MAC), die nur ins Internet dürfen und sonst niemanden im Heimnetz sehen.
            CREATE TABLE IF NOT EXISTS device_isolated (
                mac TEXT PRIMARY KEY,
                added INTEGER NOT NULL
            );

            -- Geräte, die die FRITZ!Box(en) kennen: eigener Name aus der FRITZ!Box, letzte IP, ob gerade aktiv.
            -- Bleibt über Neustarts erhalten, damit Namen und Zuordnung sofort wieder da sind.
            CREATE TABLE IF NOT EXISTS fritz_hosts (
                mac TEXT PRIMARY KEY,
                ip TEXT,
                name TEXT,
                active INTEGER NOT NULL DEFAULT 0,
                interface TEXT,
                last_seen INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS redirects (
                source TEXT PRIMARY KEY,
                target TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            -- Gerätegruppen: Ordnung + gemeinsames „Internet über“ (z. B. TVs ohne VPN).
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                color TEXT NOT NULL DEFAULT '',
                rank INTEGER NOT NULL DEFAULT 0,
                slot TEXT NOT NULL DEFAULT '',
                locked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS group_members (
                group_id INTEGER NOT NULL,
                mac TEXT NOT NULL,
                PRIMARY KEY (group_id, mac)
            );

            -- Seiten-Regeln nur für Mitglieder einer Gruppe (wie site_routes, aber MAC-bezogen).
            CREATE TABLE IF NOT EXISTS group_site_routes (
                group_id INTEGER NOT NULL,
                domain TEXT NOT NULL,
                slot TEXT NOT NULL,
                PRIMARY KEY (group_id, domain)
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


def site_routes() -> dict[str, str]:
    """Domain -> Slot (Seiten, die immer durch ein bestimmtes Profil laufen)"""
    with closing(_connect()) as conn:
        return {r["domain"]: r["slot"] for r in conn.execute("SELECT domain, slot FROM site_routes ORDER BY domain")}


def set_site_route(domain: str, slot: str) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO site_routes (domain, slot) VALUES (?, ?) ON CONFLICT (domain) DO UPDATE SET slot = excluded.slot",
            (domain, slot),
        )
        conn.commit()


def remove_site_route(domain: str) -> None:
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM site_routes WHERE domain = ?", (domain.strip().lower(),))
        conn.commit()


def group_site_routes(group_id: int) -> dict[str, str]:
    """Domain → Slot für eine Gruppe."""
    with closing(_connect()) as conn:
        return {
            r["domain"]: r["slot"]
            for r in conn.execute(
                "SELECT domain, slot FROM group_site_routes WHERE group_id = ? ORDER BY domain",
                (group_id,),
            )
        }


def set_group_site_route(group_id: int, domain: str, slot: str) -> None:
    domain = domain.strip().lower()
    slot = slot.strip().lower()
    with closing(_connect()) as conn:
        conn.execute(
            """INSERT INTO group_site_routes (group_id, domain, slot) VALUES (?, ?, ?)
               ON CONFLICT (group_id, domain) DO UPDATE SET slot = excluded.slot""",
            (group_id, domain, slot),
        )
        conn.commit()


def remove_group_site_route(group_id: int, domain: str) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            "DELETE FROM group_site_routes WHERE group_id = ? AND domain = ?",
            (group_id, domain.strip().lower()),
        )
        conn.commit()


def all_group_site_domains() -> dict[str, set[str]]:
    """Domain → alle Slots aus Gruppen-Seitenregeln (für dnsmasq-ipsets)."""
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT domain, slot FROM group_site_routes").fetchall()
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["domain"], set()).add(r["slot"])
    return out


def group_site_mac_slots() -> list[tuple[str, str]]:
    """(MAC, Slot)-Paare für Gruppen-Seitenregeln, höhere Gruppe zuerst (ohne Doppelte)."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            """SELECT m.mac, r.slot
               FROM group_site_routes r
               JOIN group_members m ON m.group_id = r.group_id
               JOIN groups g ON g.id = r.group_id
               ORDER BY g.rank ASC, g.id ASC, m.mac ASC, r.slot ASC"""
        ).fetchall()
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for r in rows:
        pair = (r["mac"], r["slot"])
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


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
            WITH ips AS (SELECT ip FROM domain_stats UNION SELECT ip FROM traffic UNION SELECT ip FROM devices),
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
        hosts = {r["mac"]: dict(r) for r in conn.execute("SELECT * FROM fritz_hosts")}
    sites = current_sites()
    # IP -> MAC laut FRITZ!Box (nur aktive Geräte, deren Adresse also stimmt)
    fritz_ip_mac = {h["ip"]: mac for mac, h in hosts.items() if h["active"] and h["ip"]}
    # Pro MAC nur eine Zeile (aktuelle Lease); Stats ohne MAC bleiben eigene Zeilen.
    by_mac: dict[str, dict] = {}
    plain: list[dict] = []
    for r in rows:
        d = {**dict(r), "current_site": sites.get(r["ip"])}
        mac = (d.get("mac") or "").strip().lower() or fritz_ip_mac.get(d["ip"], "")
        d["mac"] = mac or None
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
    # Geräte, die nur die FRITZ!Box kennt (z. B. gerade aus, fest eingestellte IP, eigenes DNS)
    for mac, h in hosts.items():
        if mac not in by_mac:
            by_mac[mac] = {
                "ip": h["ip"] or "", "mac": mac, "hostname": None, "hits": 0, "domains": 0,
                "bytes_up": 0, "bytes_down": 0, "last_seen": h["last_seen"], "updated": 0,
                "current_site": None, "fritz_only": True,
            }
    for mac, d in by_mac.items():
        h = hosts.get(mac)
        d["fritz_name"] = h["name"] if h else None
        d["fritz_active"] = bool(h and h["active"])
        d["lease_name"] = d.get("hostname")
        d["hostname"] = d["fritz_name"] or d["hostname"]
    out = plain + list(by_mac.values())
    out.sort(key=lambda x: x["last_seen"], reverse=True)
    return out


def device(ip: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT d.ip, d.mac, COALESCE(f.name, d.hostname) AS hostname, d.hostname AS lease_name, f.name AS fritz_name
            FROM devices d LEFT JOIN fritz_hosts f ON f.mac = d.mac WHERE d.ip = ?
            """,
            (ip,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT ip, mac, name AS hostname, NULL AS lease_name, name AS fritz_name FROM fritz_hosts WHERE ip = ?"
                " ORDER BY active DESC, last_seen DESC LIMIT 1",
                (ip,),
            ).fetchone()
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
            SELECT q.ts, q.ip, q.domain, q.qtype, COALESCE(f.name, d.hostname) AS hostname
            FROM queries q LEFT JOIN devices d ON d.ip = q.ip LEFT JOIN fritz_hosts f ON f.mac = d.mac
            {where}
            ORDER BY q.id DESC LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]

def save_fritz_hosts(hosts: list[dict]) -> None:
    """Geräteliste der FRITZ!Box(en) merken: [{mac, ip, name, active, interface}].

    Aktive Geräte mit IPv4 kommen zusätzlich in die Geräteliste (Identität = MAC), so werden sie auch ohne
    cloudy-DHCP-Lease erkannt und nach einem Neustart nicht doppelt geführt.
    """
    if not hosts:
        return
    now = int(time.time())
    with closing(_connect()) as conn:
        conn.executemany(
            """
            INSERT INTO fritz_hosts (mac, ip, name, active, interface, last_seen) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (mac) DO UPDATE SET
                ip = COALESCE(excluded.ip, fritz_hosts.ip),
                name = COALESCE(excluded.name, fritz_hosts.name),
                active = excluded.active,
                interface = COALESCE(excluded.interface, fritz_hosts.interface),
                last_seen = MAX(fritz_hosts.last_seen, excluded.last_seen)
            """,
            [
                (h["mac"], h.get("ip") or None, h.get("name") or None, int(bool(h.get("active"))),
                 h.get("interface") or None, now if h.get("active") else 0)
                for h in hosts if h.get("mac")
            ],
        )
        conn.commit()
    active = [(h["ip"], h["mac"], None) for h in hosts if h.get("active") and h.get("mac") and _is_ipv4(h.get("ip"))]
    known = {(r["ip"], r["mac"]) for r in _rows("SELECT ip, mac FROM devices")}
    # Nur schreiben, was neu ist: sonst würde jede Abfrage die Lease-Zeitstempel verschieben.
    upsert_devices([a for a in active if (a[0], a[1]) not in known])


def _rows(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with closing(_connect()) as conn:
        return conn.execute(sql, params).fetchall()


def _is_ipv4(val: str | None) -> bool:
    try:
        return ipaddress.ip_address(val or "").version == 4
    except ValueError:
        return False


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


# --------------------------------------------------------------------------- Gerätegruppen

GROUP_COLORS = ("#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#a855f7", "#64748b")


def _group_row(r) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "color": r["color"] or GROUP_COLORS[0],
        "rank": r["rank"],
        "slot": r["slot"] or "",
        "locked": bool(r["locked"]),
    }


def groups() -> list[dict]:
    """Alle Gruppen, oben = kleineres rank."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT id, name, color, rank, slot, locked FROM groups ORDER BY rank ASC, id ASC"
        ).fetchall()
        return [_group_row(r) for r in rows]


def group(group_id: int) -> dict | None:
    with closing(_connect()) as conn:
        r = conn.execute(
            "SELECT id, name, color, rank, slot, locked FROM groups WHERE id = ?", (group_id,)
        ).fetchone()
        return _group_row(r) if r else None


def group_members(group_id: int) -> set[str]:
    with closing(_connect()) as conn:
        return {
            r["mac"]
            for r in conn.execute("SELECT mac FROM group_members WHERE group_id = ?", (group_id,))
        }


def groups_by_mac() -> dict[str, list[dict]]:
    """MAC -> Gruppen dieses Geräts, sortiert nach Rang (oben zuerst)."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            """SELECT g.id, g.name, g.color, g.rank, g.slot, g.locked, m.mac
               FROM group_members m JOIN groups g ON g.id = m.group_id
               ORDER BY g.rank ASC, g.id ASC"""
        ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["mac"], []).append(_group_row(r))
    return out


def create_group(name: str, color: str = "", slot: str = "", locked: bool = False) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Name fehlt")
    color = color if color in GROUP_COLORS else GROUP_COLORS[0]
    slot = (slot or "").strip().lower()
    locked = bool(locked and slot)
    with closing(_connect()) as conn:
        rank = conn.execute("SELECT COALESCE(MAX(rank), -1) + 1 FROM groups").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO groups (name, color, rank, slot, locked) VALUES (?, ?, ?, ?, ?)",
            (name, color, rank, slot, int(locked)),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_group(group_id: int, name: str, color: str = "", slot: str = "", locked: bool = False) -> None:
    name = (name or "").strip()
    if not name:
        raise ValueError("Name fehlt")
    color = color if color in GROUP_COLORS else GROUP_COLORS[0]
    slot = (slot or "").strip().lower()
    locked = bool(locked and slot)
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE groups SET name = ?, color = ?, slot = ?, locked = ? WHERE id = ?",
            (name, color, slot, int(locked), group_id),
        )
        conn.commit()


def delete_group(group_id: int) -> None:
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM group_site_routes WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        conn.commit()


def set_group_members(group_id: int, macs: list[str]) -> None:
    clean = sorted({m.strip().lower() for m in macs if m and m.strip()})
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
        conn.executemany(
            "INSERT INTO group_members (group_id, mac) VALUES (?, ?)",
            [(group_id, m) for m in clean],
        )
        conn.commit()


def move_group(group_id: int, direction: int) -> None:
    """direction -1 = höher (kleineres rank), +1 = tiefer."""
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT id, rank FROM groups ORDER BY rank ASC, id ASC").fetchall()
        ids = [r["id"] for r in rows]
        if group_id not in ids:
            return
        i = ids.index(group_id)
        j = i + (1 if direction > 0 else -1)
        if j < 0 or j >= len(ids):
            return
        ids[i], ids[j] = ids[j], ids[i]
        for rank, gid in enumerate(ids):
            conn.execute("UPDATE groups SET rank = ? WHERE id = ?", (rank, gid))
        conn.commit()


def connect() -> sqlite3.Connection:
    """Für Lese-Abfragen anderer Module (z. B. Statistik)."""
    return _connect()


def query_sessions(rule: str, since: int, window: int = 90, max_sessions: int = 60) -> list[set[str]]:
    """Für Vorschläge zugehöriger Domains: Pro Aufruf der Seite (Gerät + Zeitpunkt) die Domains, die dasselbe
    Gerät in den `window` Sekunden danach abgefragt hat (ohne die Seite selbst)."""
    rule = rule.lower().strip(".")
    with closing(_connect()) as conn:
        hits = conn.execute(
            """SELECT ip, ts FROM queries
               WHERE ts >= ? AND (domain = ? OR domain LIKE ?)
               ORDER BY ip, ts""",
            (since, rule, "%." + rule),
        ).fetchall()
        starts: list[tuple[str, int]] = []
        last: dict[str, int] = {}
        for row in hits:
            ip, ts = row["ip"], row["ts"]
            if ip in last and ts - last[ip] < window:
                continue  # gehört noch zur selben Sitzung
            last[ip] = ts
            starts.append((ip, ts))
        starts = sorted(starts, key=lambda x: -x[1])[:max_sessions]
        out = []
        for ip, ts in starts:
            names = {
                r["domain"].lower()
                for r in conn.execute(
                    "SELECT DISTINCT domain FROM queries WHERE ip = ? AND ts BETWEEN ? AND ?",
                    (ip, ts - 5, ts + window),
                )
            }
            out.append({n for n in names if n != rule and not n.endswith("." + rule)})
        return out


# --------------------------------------------------------------------------- Werbeblocker-Statistik


def add_blocked(rows: list[tuple[int, str, str]]) -> None:
    """rows: (ts, ip, domain) – vom Werbeblocker blockierte Anfragen."""
    if not rows:
        return
    with closing(_connect()) as conn:
        conn.executemany(
            """INSERT INTO ad_blocked_daily (day, ip, domain, hits) VALUES (?, ?, ?, 1)
               ON CONFLICT (day, ip, domain) DO UPDATE SET hits = hits + 1""",
            [(day_of(ts), ip, domain) for ts, ip, domain in rows],
        )
        conn.execute(
            "DELETE FROM ad_blocked_daily WHERE day < ?", (day_of(int(time.time()) - retention_days() * 86400),)
        )
        conn.commit()


def blocked_stats(since_day: str, ip: str | None = None, limit: int = 10) -> dict:
    """Summe, Verlauf pro Tag, Top-Domains und Top-Geräte ab since_day (optional nur ein Gerät)."""
    where, params = "day >= ?", [since_day]
    if ip:
        where += " AND ip = ?"
        params.append(ip)
    with closing(_connect()) as conn:
        total = conn.execute(
            f"SELECT COALESCE(SUM(hits),0) h, COUNT(DISTINCT domain) d FROM ad_blocked_daily WHERE {where}", params
        ).fetchone()
        all_q = conn.execute(
            f"SELECT COALESCE(SUM(hits),0) h FROM dns_daily WHERE {where}", params
        ).fetchone()
        days = [dict(r) for r in conn.execute(
            f"SELECT day, SUM(hits) hits FROM ad_blocked_daily WHERE {where} GROUP BY day ORDER BY day", params
        )]
        domains = [dict(r) for r in conn.execute(
            f"SELECT domain, SUM(hits) hits FROM ad_blocked_daily WHERE {where} GROUP BY domain "
            "ORDER BY hits DESC LIMIT ?", [*params, limit]
        )]
        devices = [dict(r) for r in conn.execute(
            f"""SELECT b.ip, SUM(b.hits) hits, MAX(d.hostname) hostname FROM ad_blocked_daily b
                LEFT JOIN devices d ON d.ip = b.ip WHERE {where.replace('day', 'b.day').replace('ip =', 'b.ip =')}
                GROUP BY b.ip ORDER BY hits DESC LIMIT ?""", [*params, limit]
        )]
    hits = int(total["h"])
    return {
        "hits": hits,
        "domains_count": int(total["d"]),
        "share": round(100 * hits / int(all_q["h"]), 1) if int(all_q["h"]) else 0.0,
        "days": days,
        "top_domains": domains,
        "top_devices": devices,
    }
