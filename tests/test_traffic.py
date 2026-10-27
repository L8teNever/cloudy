import time

from app import db
from app.dnsmasq import build_config
from app.main import fmt_bytes
from app.settings import Settings
from app.traffic import Conn, DomainMap, TrafficMeter, gateway_rules, parse_conntrack

CT_LINE = (
    "ipv4     2 tcp      6 431999 ESTABLISHED src=192.168.178.25 dst=142.250.1.1 sport=51234 dport=443 "
    "packets=10 bytes=1200 src=142.250.1.1 dst=192.168.178.2 sport=443 dport=51234 packets=20 bytes=30000 "
    "[ASSURED] mark=0 use=1 id=4711"
)


def test_parse_conntrack_list_and_event():
    c = parse_conntrack(CT_LINE)
    assert c == Conn(id="4711", proto="tcp", src="192.168.178.25", dst="142.250.1.1", dport="443",
                     bytes_up=1200, bytes_down=30000)
    ev = parse_conntrack("[DESTROY] " + CT_LINE)
    assert ev is not None and ev.id == "4711" and ev.bytes_down == 30000
    assert parse_conntrack("conntrack v1.4.8 (conntrack-tools): 3 flow entries have been shown.") is None


def test_domain_map_uses_question_not_cname():
    m = DomainMap()
    m.feed("dnsmasq[1]: 46 192.168.178.25/51111 query[A] www.youtube.com from 192.168.178.25")
    m.feed("dnsmasq[1]: 46 192.168.178.25/51111 forwarded www.youtube.com to 172.64.36.1")
    m.feed("dnsmasq[1]: 46 192.168.178.25/51111 reply www.youtube.com is <CNAME>")
    m.feed("dnsmasq[1]: 46 192.168.178.25/51111 reply youtube-ui.l.google.com is 142.250.1.1")
    m.feed("dnsmasq[1]: 47 192.168.178.30/52222 cached netflix.com is 3.3.3.3")
    assert m.lookup("192.168.178.25", "142.250.1.1") == "www.youtube.com"
    assert m.lookup("192.168.178.99", "142.250.1.1") == "www.youtube.com"  # Fallback ohne Gerät
    assert m.lookup("192.168.178.30", "3.3.3.3") == "netflix.com"
    assert m.lookup("192.168.178.25", "9.9.9.9") is None


def test_meter_counts_deltas_and_final():
    m = DomainMap()
    m.feed("dnsmasq[1]: 5 192.168.178.25/5 query[A] youtube.com from 192.168.178.25")
    m.feed("dnsmasq[1]: 5 192.168.178.25/5 reply youtube.com is 142.250.1.1")
    meter = TrafficMeter(Settings.from_env(), m)
    now = time.time()

    meter.observe(parse_conntrack(CT_LINE), now=now)
    meter.observe(parse_conntrack(CT_LINE.replace("bytes=30000", "bytes=50000")), now=now)
    meter.observe(parse_conntrack("[DESTROY] " + CT_LINE.replace("bytes=30000", "bytes=80000")), final=True, now=now)
    # LAN-intern und vom Pi selbst wird nicht gezählt.
    meter.observe(Conn("1", "tcp", "192.168.178.25", "192.168.178.50", "445", 9, 9), now=now)
    meter.observe(Conn("2", "tcp", "192.168.178.2", "1.1.1.1", "443", 9, 9), now=now)
    meter.flush()

    [row] = db.traffic_for("192.168.178.25")
    assert row["target"] == "youtube.com"
    assert row["bytes_down"] == 80000
    assert row["bytes_up"] == 1200
    assert row["conns"] == 1
    assert db.traffic_for("192.168.178.2") == []


def test_gateway_dhcp_config(monkeypatch):
    monkeypatch.setenv("DHCP_ENABLED", "1")
    monkeypatch.setenv("GATEWAY_ENABLED", "1")
    conf = build_config(Settings.from_env())
    assert "filter-AAAA" in conf
    assert "dhcp-option=tag:!direkt,option:router,192.168.178.2" in conf
    assert "dhcp-option=tag:direkt,option:router,192.168.178.1" in conf
    assert "dhcp-option=option:router,192.168.178.1" not in conf


def test_local_name(monkeypatch, tmp_path):
    conf = build_config(Settings.from_env())
    assert "address=/cloudy.lan/192.168.178.2" in conf
    assert f"addn-hosts={tmp_path}" in conf and "lokale-namen.hosts" in conf

    monkeypatch.setenv("LOCAL_NAME", "Dashboard.Home.Arpa.")
    monkeypatch.setenv("PI_IP", "192.168.178.3")
    s = Settings.from_env()
    assert "address=/dashboard.home.arpa/192.168.178.3" in build_config(s)
    assert s.dashboard_url == "http://dashboard.home.arpa:8080"

    monkeypatch.setenv("LOCAL_NAME", "")
    assert "address=/" not in build_config(Settings.from_env())


def test_gateway_rules():
    rules = gateway_rules(Settings.from_env())
    assert ["iptables", "-t", "nat", "-A", "CLOUDY-NAT", "-s", "192.168.178.0/24", "!", "-d", "192.168.178.0/24",
            "-o", "eth0", "-j", "MASQUERADE"] in rules


def test_dashboard_shows_traffic(client):
    now = int(time.time())
    db.add_traffic([("192.168.178.25", "youtube.com", 1000, 5 * 1024 * 1024, 3, now)])
    assert "5,0 MB" in client.get("/").text
    detail = client.get("/", params={"ip": "192.168.178.25"}).text
    assert "Datenverkehr" in detail and "youtube.com" in detail
    assert client.get("/api/devices").json()[0]["bytes_down"] == 5 * 1024 * 1024


def test_fmt_bytes():
    assert fmt_bytes(500) == "500 B"
    assert fmt_bytes(1536) == "1,5 KB"
    assert fmt_bytes(3 * 1024**3) == "3,0 GB"
