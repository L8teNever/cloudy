"""Auswertungen für die Statistik-Seite: Zeiträume Heute / 7 Tage / 30 Tage, gesamt oder pro Gerät."""

import time
from contextlib import closing
from dataclasses import dataclass

from . import db

RANGES = {"heute": "Heute", "7t": "7 Tage", "30t": "30 Tage"}


@dataclass(frozen=True)
class Range:
    key: str
    label: str
    since_ts: int  # Beginn als Zeitstempel
    since_day: str  # Beginn als Kalendertag (YYYY-MM-DD)
    hourly: bool  # Verlauf stündlich (Heute) oder täglich


def get_range(key: str | None, now: float | None = None) -> Range:
    key = key if key in RANGES else "heute"
    now = time.time() if now is None else now
    lt = time.localtime(now)
    midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    days = {"heute": 0, "7t": 6, "30t": 29}[key]
    since = midnight - days * 86400
    # Sommer-/Winterzeit: auf Mitternacht des Zieltags runden
    st = time.localtime(since + 3 * 3600)
    since = int(time.mktime((st.tm_year, st.tm_mon, st.tm_mday, 0, 0, 0, 0, 0, -1)))
    return Range(key, RANGES[key], since, db.day_of(since), key == "heute")


def _where(ip: str | None, column: str, value) -> tuple[str, list]:
    sql, params = f"{column} >= ?", [value]
    if ip:
        sql += " AND ip = ?"
        params.append(ip)
    return sql, params


def totals(r: Range, ip: str | None = None) -> dict:
    tw, tp = _where(ip, "day", r.since_day)
    dw, dp = _where(ip, "day", r.since_day)
    with closing(db.connect()) as conn:
        t = conn.execute(
            f"SELECT COALESCE(SUM(bytes_down),0) d, COALESCE(SUM(bytes_up),0) u, COUNT(DISTINCT ip) n "
            f"FROM traffic_daily WHERE {tw}", tp
        ).fetchone()
        q = conn.execute(
            f"SELECT COALESCE(SUM(hits),0) h, COUNT(DISTINCT domain) dom, COUNT(DISTINCT ip) n "
            f"FROM dns_daily WHERE {dw}", dp
        ).fetchone()
    return {
        "bytes_down": t["d"], "bytes_up": t["u"], "hits": q["h"], "domains": q["dom"],
        "devices": max(t["n"], q["n"]),
    }


def device_usage(r: Range, limit: int = 20) -> list[dict]:
    """Datenmenge pro Gerät (absteigend), mit Name."""
    with closing(db.connect()) as conn:
        rows = conn.execute(
            """
            SELECT t.ip, d.hostname, SUM(t.bytes_down) AS bytes_down, SUM(t.bytes_up) AS bytes_up
            FROM traffic_daily t LEFT JOIN devices d ON d.ip = t.ip
            WHERE t.day >= ? GROUP BY t.ip ORDER BY SUM(t.bytes_down + t.bytes_up) DESC LIMIT ?
            """,
            (r.since_day, limit),
        ).fetchall()
    return [dict(x) for x in rows]


def device_hits(r: Range, limit: int = 20) -> list[dict]:
    """Aufrufe pro Gerät (absteigend)."""
    with closing(db.connect()) as conn:
        rows = conn.execute(
            """
            SELECT q.ip, d.hostname, SUM(q.hits) AS hits
            FROM dns_daily q LEFT JOIN devices d ON d.ip = q.ip
            WHERE q.day >= ? GROUP BY q.ip ORDER BY SUM(q.hits) DESC LIMIT ?
            """,
            (r.since_day, limit),
        ).fetchall()
    return [dict(x) for x in rows]


def top_targets(r: Range, ip: str | None = None, limit: int = 15) -> list[dict]:
    """Ziele mit der größten Datenmenge."""
    w, p = _where(ip, "day", r.since_day)
    with closing(db.connect()) as conn:
        rows = conn.execute(
            f"""
            SELECT target, SUM(bytes_down) AS bytes_down, SUM(bytes_up) AS bytes_up, SUM(conns) AS conns
            FROM traffic_daily WHERE {w} GROUP BY target ORDER BY SUM(bytes_down + bytes_up) DESC LIMIT ?
            """,
            (*p, limit),
        ).fetchall()
    return [dict(x) for x in rows]


def top_domains(r: Range, ip: str | None = None, limit: int = 15) -> list[dict]:
    """Meistaufgerufene Domains."""
    w, p = _where(ip, "day", r.since_day)
    with closing(db.connect()) as conn:
        rows = conn.execute(
            f"""
            SELECT domain, SUM(hits) AS hits, COUNT(DISTINCT ip) AS devices
            FROM dns_daily WHERE {w} GROUP BY domain ORDER BY SUM(hits) DESC LIMIT ?
            """,
            (*p, limit),
        ).fetchall()
    return [dict(x) for x in rows]


def timeseries(r: Range, ip: str | None = None, now: float | None = None) -> list[dict]:
    """Verlauf: pro Stunde (Heute) oder pro Tag. Lücken werden mit 0 aufgefüllt.
    [{label, start, bytes_down, bytes_up, hits}]"""
    now = time.time() if now is None else now
    buckets: list[dict] = []
    if r.hourly:
        hour = db.hour_of(r.since_ts)
        end = db.hour_of(int(now))
        while hour <= end:
            buckets.append({"key": hour, "label": time.strftime("%H:00", time.localtime(hour)), "start": hour})
            hour += 3600
        tw, tp = _where(ip, "hour", r.since_ts)
        with closing(db.connect()) as conn:
            traffic = {x["hour"]: x for x in conn.execute(
                f"SELECT hour, SUM(bytes_down) d, SUM(bytes_up) u FROM traffic_hourly WHERE {tw} GROUP BY hour", tp)}
            hits = {x["hour"]: x["h"] for x in conn.execute(
                f"SELECT hour, SUM(hits) h FROM dns_hourly WHERE {tw} GROUP BY hour", tp)}
    else:
        day_ts = r.since_ts
        while day_ts <= now:
            day = db.day_of(day_ts)
            if not buckets or buckets[-1]["key"] != day:
                buckets.append({"key": day, "label": time.strftime("%d.%m.", time.localtime(day_ts)), "start": day_ts})
            day_ts += 86400
        tw, tp = _where(ip, "day", r.since_day)
        with closing(db.connect()) as conn:
            traffic = {x["day"]: x for x in conn.execute(
                f"SELECT day, SUM(bytes_down) d, SUM(bytes_up) u FROM traffic_daily WHERE {tw} GROUP BY day", tp)}
            hits = {x["day"]: x["h"] for x in conn.execute(
                f"SELECT day, SUM(hits) h FROM dns_daily WHERE {tw} GROUP BY day", tp)}
    for b in buckets:
        t = traffic.get(b["key"])
        b["bytes_down"] = t["d"] if t else 0
        b["bytes_up"] = t["u"] if t else 0
        b["hits"] = hits.get(b["key"], 0)
    return buckets
