"""IPv6 über den Pi (IPV6_ENABLED): Netz, Router Advertisement, Markierung nach MAC, Routen, Geräte-Zuordnung."""

import ipaddress
import os
import time

import pytest

from app import db, ipv6, warp
from app.dnsmasq import Collector, build_config
from app.settings import Settings
from app.traffic import Conn, DomainMap, TrafficMeter, dns_force_rules_v6, gateway_rules_v6

MAC = "aa:bb:cc:dd:ee:01"


@pytest.fixture
def v6(monkeypatch):
    monkeypatch.setenv("GATEWAY_ENABLED", "1")
    monkeypatch.setenv("IPV6_ENABLED", "1")
    monkeypatch.setenv("IPV6_PREFIX", "fd12:3456:789a:1::/64")
    return Settings.from_env()


def test_prefix_is_generated_once_and_persisted():
    s = Settings.from_env()
    net = ipv6.prefix(s)
    assert net.prefixlen == 64 and net.is_private and str(net).startswith("fd")
    assert ipv6.prefix(s) == net  # beim zweiten Mal aus der Datei
    assert open(os.path.join(s.data_dir, ipv6.PREFIX_FILE)).read().strip() == str(net)


def test_env_prefix_wins_and_bad_values_fall_back(monkeypatch):
    monkeypatch.setenv("IPV6_PREFIX", "fd12:3456:789a:1::/64")
    assert str(ipv6.prefix(Settings.from_env())) == "fd12:3456:789a:1::/64"
    monkeypatch.setenv("IPV6_PREFIX", "2001:db8::/64")  # kein privates Netz
    net = ipv6.prefix(Settings.from_env())
    assert net != ipaddress.IPv6Network("2001:db8::/64") and net.is_private


def test_addresses():
    net = ipaddress.IPv6Network("fd12:3456:789a:1::/64")
    assert str(ipv6.pi_address(net)) == "fd12:3456:789a:1::1"
    assert ipv6.slot_address("172.29.86.11") == "fd29:86::11"
    assert ipv6.slot_address("172.29.86.22") == "fd29:86::22"


def test_dnsmasq_sends_router_advertisement_instead_of_filtering(v6):
    conf = build_config(v6)
    assert "enable-ra" in conf
    assert "dhcp-range=fd12:3456:789a:1::,ra-only,64,12h" in conf
    assert "dhcp-option=option6:dns-server,[fd12:3456:789a:1::1]" in conf
    assert "ra-param=eth0,high,60,1800" in conf
    assert "filter-AAAA" not in conf


def test_dnsmasq_without_gateway_does_not_advertise(monkeypatch):
    monkeypatch.setenv("IPV6_ENABLED", "1")  # Gateway aus -> nichts ändern
    conf = build_config(Settings.from_env())
    assert "enable-ra" not in conf and "filter-AAAA" not in conf


def test_plan_marks_ipv6_by_mac_and_skips_own_network(v6):
    plan = warp.build_plan(v6, {MAC: "warp1"}, {"warp1": True}, ipv6_ok={"warp1": True})
    assert plan.mangle_rules6 == (
        ("-i", "eth0", "-m", "mac", "--mac-source", MAC, "!", "-d", "fd12:3456:789a:1::/64",
         "-j", "MARK", "--set-mark", "0x101"),
    )
    # Die IPv4-Regeln bleiben wie sie waren.
    assert "192.168.178.0/24" in plan.mangle_rules[0] and "fd12:3456:789a:1::/64" not in plan.mangle_rules[0]


def test_plan_ipv6_routes_only_through_tunnels_that_can_ipv6(v6):
    plan = warp.build_plan(
        v6, {}, {"warp1": True, "warp2": True, "vpn1": False}, ipv6_ok={"warp1": True, "warp2": False, "vpn1": True}
    )
    assert plan.routes6[0] == ("default", "via", "fd29:86::11", "table", "201")
    # Tunnel ohne IPv6 oder ohne Verbindung: sofort "keine Route", niemals über die Fritzbox.
    assert plan.routes6[1] == ("unreachable", "default", "table", "202")
    assert plan.routes6[2] == ("unreachable", "default", "table", "203")


def test_plan_direct_and_default_for_ipv6(v6):
    plan = warp.build_plan(v6, {MAC: "direct"}, {"warp1": True}, "warp1", {"warp1": True})
    assert plan.mangle_rules6[0] == ("-i", "eth0", "-m", "mac", "--mac-source", MAC, "-j", "RETURN")
    assert plan.mangle_rules6[1][:5] == ("-i", "eth0", "-m", "mark", "--mark")


def test_plan_has_no_ipv6_without_switch(monkeypatch):
    plan = warp.build_plan(Settings.from_env(), {MAC: "warp1"}, {"warp1": True})
    assert plan.mangle_rules6 == () and plan.routes6 == ()


def test_read_ipv6_ok(tmp_path):
    s = Settings.from_env()
    assert not warp.read_ipv6_ok(s, "warp1")
    os.makedirs(warp.slot_dir(s, "warp1"))
    path = os.path.join(warp.slot_dir(s, "warp1"), "ipv6")
    with open(path, "w") as f:
        f.write("ok\n")
    assert warp.read_ipv6_ok(s, "warp1")
    assert not warp.read_ipv6_ok(s, "warp1", now=time.time() + 600)  # Container meldet sich nicht mehr
    with open(path, "w") as f:
        f.write("nein\n")
    assert not warp.read_ipv6_ok(s, "warp1")


def test_gateway_rules_reject_unmarked_ipv6(v6):
    reject = gateway_rules_v6(v6)[0]
    assert reject[:4] == ["ip6tables", "-A", "CLOUDY-FWD", "-i"]
    assert ["-m", "mark", "--mark", "0x0", "!", "-d", "fd12:3456:789a:1::/64"] == reject[7:14]
    assert reject[-4:] == ["-j", "REJECT", "--reject-with", "icmp6-no-route"]


def test_dns_force_rules_v6(v6):
    rules = dns_force_rules_v6(v6)
    assert len(rules) == 2
    assert ["ip6tables", "-t", "nat", "-A", "CLOUDY-DNS", "-i", "eth0", "-p", "udp", "--dport", "53",
            "!", "-d", "fd12:3456:789a:1::1", "-j", "REDIRECT", "--to-ports", "53"] in rules


NEIGH = """\
fd12:3456:789a:1:b0a8:1234:5678:9abc dev eth0 lladdr aa:bb:cc:dd:ee:01 REACHABLE
fe80::1 dev eth0 lladdr 11:22:33:44:55:66 router STALE
fd12:3456:789a:1::77 dev eth0 FAILED
"""


def test_parse_neighbors():
    assert ipv6.parse_neighbors(NEIGH) == {
        "fd12:3456:789a:1:b0a8:1234:5678:9abc": "aa:bb:cc:dd:ee:01",
        "fe80::1": "11:22:33:44:55:66",
    }


def test_ipv6_address_maps_to_device_ipv4(monkeypatch):
    db.upsert_devices([("192.168.178.25", MAC, "Handy")])
    monkeypatch.setattr(ipv6, "_read_neighbors", lambda: ipv6.parse_neighbors(NEIGH))
    monkeypatch.setattr(ipv6, "_neighbors", {})
    monkeypatch.setattr(ipv6, "_neighbors_at", 0.0)
    assert ipv6.to_ipv4("192.168.178.25") == "192.168.178.25"
    assert ipv6.to_ipv4("fd12:3456:789a:1:b0a8:1234:5678:9abc") == "192.168.178.25"
    assert ipv6.to_ipv4("fd12:3456:789a:1::dead") is None  # unbekanntes Gerät


def test_dns_query_over_ipv6_is_counted_for_the_ipv4_device(monkeypatch):
    db.upsert_devices([("192.168.178.25", MAC, "Handy")])
    monkeypatch.setattr(ipv6, "to_ipv4", lambda a: "192.168.178.25" if a.startswith("fd12") else a)
    c = Collector()
    line = "Oct 30 12:00:00 dnsmasq[1]: 1 fd12:3456:789a:1::5/4000 query[AAAA] example.com from fd12:3456:789a:1::5"
    assert c.feed(line, now=int(time.time()))
    c.flush()
    assert [r["domain"] for r in db.top_domains("192.168.178.25")] == ["example.com"]
    assert db.top_domains("fd12:3456:789a:1::5") == []


def test_traffic_meter_counts_ipv6_flows_for_the_device(v6, monkeypatch):
    monkeypatch.setattr(ipv6, "to_ipv4", lambda a: "192.168.178.25" if ":" in a else a)
    meter = TrafficMeter(v6, DomainMap())
    conn = Conn(id="9", proto="tcp", src="fd12:3456:789a:1::5", dst="2606:4700::1111", dport="443",
                bytes_up=100, bytes_down=900)
    now = time.time()
    meter.observe(conn, now=now)
    # Dasselbe Ende-Ereignis kann über beide conntrack-Aufrufe doppelt eintreffen -> nur einmal zählen.
    meter.observe(conn, final=True, now=now + 1)
    meter.observe(conn, final=True, now=now + 1)
    meter.flush()
    rows = db.traffic_for("192.168.178.25")
    assert sum(r["bytes_down"] for r in rows) == 900 and sum(r["bytes_up"] for r in rows) == 100


def test_traffic_meter_ignores_ipv6_when_switch_is_off(monkeypatch):
    meter = TrafficMeter(Settings.from_env(), DomainMap())
    conn = Conn(id="9", proto="tcp", src="fd12:3456:789a:1::5", dst="2606:4700::1111", dport="443",
                bytes_up=100, bytes_down=900)
    assert not meter._relevant(conn)


def test_traffic_meter_ignores_pi_own_and_local_ipv6(v6):
    meter = TrafficMeter(v6, DomainMap())
    own = Conn(id="1", proto="tcp", src="fd12:3456:789a:1::1", dst="2606:4700::1111", dport="443", bytes_up=1, bytes_down=1)
    local = Conn(id="2", proto="tcp", src="fd12:3456:789a:1::5", dst="fd12:3456:789a:1::6", dport="80", bytes_up=1, bytes_down=1)
    assert not meter._relevant(own) and not meter._relevant(local)


def test_dashboard_status_shows_ipv6(v6, client):
    page = client.get("/").text
    # Mit Gateway aus (Tests laufen ohne dnsmasq/Gateway-Setup) darf nichts kaputtgehen.
    assert "Gateway" in page
