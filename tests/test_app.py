from app import db
from app.dnsmasq import Collector, build_config, parse_query, read_leases
from app.settings import Settings


def test_parse_query():
    line = "Oct 27 17:00:00 dnsmasq[12]: query[A] WWW.YouTube.com from 192.168.178.25\n"
    assert parse_query(line) == ("A", "www.youtube.com", "192.168.178.25")
    assert parse_query("dnsmasq[1]: query[AAAA] example.org from fd00::12") == ("AAAA", "example.org", "fd00::12")
    assert parse_query("dnsmasq[1]: forwarded www.youtube.com to 172.64.36.1") is None
    assert parse_query("dnsmasq[1]: query[PTR] 25.178.168.192.in-addr.arpa from 192.168.178.25") is None
    assert parse_query("dnsmasq[1]: query[A] laptop.lan from 192.168.178.25") is None
    assert parse_query("dnsmasq[1]: query[A] example.com from 127.0.0.1") is None


def test_build_config_dns_only_and_dhcp(monkeypatch):
    conf = build_config(Settings.from_env())
    assert "server=172.64.36.1" in conf
    assert "server=/fritz.box/192.168.178.1" in conf
    assert "local=/use-application-dns.net/" in conf
    assert "dhcp-range" not in conf

    monkeypatch.setenv("DHCP_ENABLED", "1")
    monkeypatch.setenv("UPSTREAM_DNS", "1.1.1.1, 9.9.9.9")
    conf = build_config(Settings.from_env())
    assert "dhcp-range=192.168.178.20,192.168.178.200,255.255.255.0,1h" in conf
    assert "dhcp-option=option:router,192.168.178.1" in conf
    assert "dhcp-option=option:dns-server,192.168.178.2" in conf
    assert "server=1.1.1.1" in conf and "server=9.9.9.9" in conf


def test_read_leases(tmp_path):
    leases = tmp_path / "dhcp.leases"
    leases.write_text(
        "1793120000 AA:BB:CC:DD:EE:01 192.168.178.25 handy-max 01:aa:bb\n"
        "1793120000 aa:bb:cc:dd:ee:02 192.168.178.26 * *\n"
        "duid 00:01:00:01\n"
    )
    assert read_leases(str(leases)) == [
        ("192.168.178.25", "aa:bb:cc:dd:ee:01", "handy-max"),
        ("192.168.178.26", "aa:bb:cc:dd:ee:02", None),
    ]
    assert read_leases(str(tmp_path / "fehlt")) == []


def test_collector_dedups_and_stores():
    import time

    t = int(time.time())
    c = Collector(dedup_seconds=60)
    q = "dnsmasq[1]: query[{t}] youtube.com from 192.168.178.25"
    assert c.feed(q.format(t="A"), now=t)
    assert c.feed(q.format(t="AAAA"), now=t + 1)  # gleiche Domain kurz danach -> nicht doppelt
    assert c.feed(q.format(t="A"), now=t + 100)  # nach 60 s wieder zählen
    assert not c.feed("dnsmasq[1]: reply youtube.com is 1.2.3.4", now=t + 100)
    c.flush()
    assert len(db.recent()) == 2
    assert db.top_domains("192.168.178.25")[0] == {
        "domain": "youtube.com", "hits": 2, "first_seen": t, "last_seen": t + 100
    }


def test_old_stats_are_pruned():
    db.add_queries([(1000, "192.168.178.25", "alt.com", "A")])
    assert db.top_domains("192.168.178.25") == []


def test_pruning(monkeypatch):
    monkeypatch.setenv("MAX_ROWS", "5")
    import time

    now = int(time.time())
    db.add_queries([(now + i, "192.168.178.25", f"d{i}.com", "A") for i in range(12)])
    recent = db.recent(100)
    assert len(recent) == 5
    assert recent[0]["domain"] == "d11.com"


def test_dashboard_shows_devices_and_domains(client):
    import time

    now = int(time.time())
    db.upsert_devices([("192.168.178.25", "aa:bb:cc:dd:ee:01", "handy-max")])
    db.add_queries([(now, "192.168.178.25", "instagram.com", "A"), (now, "192.168.178.30", "netflix.com", "A")])

    html = client.get("/").text
    assert "handy-max" in html and "instagram.com" in html and "netflix.com" in html

    detail = client.get("/", params={"ip": "192.168.178.25"}).text
    assert "instagram.com" in detail and "netflix.com" not in detail

    devices = client.get("/api/devices").json()
    assert {d["ip"] for d in devices} == {"192.168.178.25", "192.168.178.30"}
    assert client.get("/api/recent", params={"ip": "192.168.178.30"}).json()[0]["domain"] == "netflix.com"


def test_optional_password(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "geheim")
    assert client.get("/").status_code == 401
    assert client.get("/api/devices").status_code == 401
    assert client.get("/", auth=("x", "falsch")).status_code == 401
    assert client.get("/", auth=("x", "geheim")).status_code == 200
    assert client.get("/healthz").status_code == 200
