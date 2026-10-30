"""Gerät behält Identität über die MAC – neue IP darf keinen zweiten Listeneintrag erzeugen."""

import time

from app import db


def test_ip_change_merges_stats_by_mac():
    now = int(time.time())
    db.upsert_devices([("192.168.178.25", "aa:bb:cc:dd:ee:01", "Nothing-Phone-2a")])
    db.add_queries([(now - 100, "192.168.178.25", "old.example", "A")])
    db.add_traffic([("192.168.178.25", "old.example", 10, 1000, 1, now - 100)])

    db.upsert_devices([("192.168.178.80", "aa:bb:cc:dd:ee:01", "Nothing-Phone-2a")])
    db.add_queries([(now, "192.168.178.80", "new.example", "A")])

    listed = db.devices()
    phones = [d for d in listed if (d.get("hostname") or "") == "Nothing-Phone-2a"]
    assert len(phones) == 1
    assert phones[0]["ip"] == "192.168.178.80"
    assert not any(d["ip"] == "192.168.178.25" for d in listed)

    domains = {r["domain"] for r in db.top_domains("192.168.178.80")}
    assert domains == {"old.example", "new.example"}
    traffic = db.traffic_for("192.168.178.80")
    assert sum(r["bytes_down"] for r in traffic) == 1000


def test_same_hostname_different_mac_stays_separate():
    db.upsert_devices(
        [
            ("192.168.178.10", "aa:bb:cc:dd:ee:01", "Nothing-Phone-2a"),
            ("192.168.178.11", "aa:bb:cc:dd:ee:02", "Nothing-Phone-2a"),
        ]
    )
    now = int(time.time())
    db.add_queries(
        [
            (now, "192.168.178.10", "a.com", "A"),
            (now, "192.168.178.11", "b.com", "A"),
        ]
    )
    phones = [d for d in db.devices() if d.get("hostname") == "Nothing-Phone-2a"]
    assert len(phones) == 2
    assert {d["mac"] for d in phones} == {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"}


def test_init_reconciles_existing_duplicate_macs(tmp_path, monkeypatch):
    now = int(time.time())
    # Zwei Geräte-Zeilen mit gleicher MAC (wie vor dem Fix nach IP-Wechsel).
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO devices (ip, mac, hostname, updated) VALUES (?, ?, ?, ?)",
            ("192.168.178.25", "aa:bb:cc:dd:ee:99", "handy", now - 3600),
        )
        conn.execute(
            "INSERT INTO devices (ip, mac, hostname, updated) VALUES (?, ?, ?, ?)",
            ("192.168.178.90", "aa:bb:cc:dd:ee:99", "handy", now),
        )
        conn.execute(
            "INSERT INTO domain_stats (ip, domain, hits, first_seen, last_seen) VALUES (?, ?, 1, ?, ?)",
            ("192.168.178.25", "old.com", now - 3600, now - 3600),
        )
        conn.execute(
            "INSERT INTO domain_stats (ip, domain, hits, first_seen, last_seen) VALUES (?, ?, 1, ?, ?)",
            ("192.168.178.90", "new.com", now, now),
        )
        conn.commit()

    db.init()  # reconcile
    rows = db.devices()
    assert len([d for d in rows if d.get("mac") == "aa:bb:cc:dd:ee:99"]) == 1
    assert db.device("192.168.178.90")["hostname"] == "handy"
    assert {r["domain"] for r in db.top_domains("192.168.178.90")} == {"old.com", "new.com"}
