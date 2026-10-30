from app import adblock, db
from app.dnsmasq import build_config, shift_serial
from app.settings import Settings

MAC = "aa:bb:cc:dd:ee:01"


def test_defaults_and_device_toggle():
    assert adblock.is_enabled()
    assert adblock.device_on(MAC)
    adblock.set_device(MAC, False)
    assert not adblock.device_on(MAC) and adblock.off_macs() == {MAC}
    adblock.set_device(MAC, True)
    assert adblock.device_on(MAC)


def test_parse_hosts_and_whitelist():
    text = "# kommentar\n127.0.0.1 localhost\n0.0.0.0 0.0.0.0\n0.0.0.0 ads.example.com # x\n0.0.0.0 track.example.org\n::1 ip6\n"
    assert adblock.parse_hosts(text, {"track.example.org"}) == ["ads.example.com"]


def test_rules_skip_disabled_devices_then_redirect():
    s = Settings.from_env()
    rules = adblock.rules(s, {MAC})
    assert rules[0] == ["-m", "mac", "--mac-source", MAC, "-j", "RETURN"]
    assert ["-p", "udp", "--dport", "53", "-j", "REDIRECT", "--to-ports", "5354"] in rules


def test_second_dnsmasq_config_has_no_dhcp():
    s = Settings.from_env()
    conf = adblock.build_config(build_config(s) + "dhcp-range=1,2\n", s)
    assert "port=5354" in conf and "addn-hosts=" in conf and "dhcp-" not in conf
    assert f"addn-hosts={s.blocklist_path}" in conf  # Cookie-Schutz, Weiterleitungen usw. gelten weiter


def test_serial_shift():
    line = "dnsmasq[9]: 12 192.168.178.60/5353 query[A] x.com from 192.168.178.60"
    assert " 1000000012 192.168.178.60/5353 " in shift_serial(line)


def test_hub_toggle_and_admin(client):
    db.upsert_devices([("testclient", MAC, "handy")])
    assert "Werbeblocker" not in client.get("/hub").text  # gehört zu „Mein Gerät“
    page = client.get("/mein").text
    assert "Werbeblocker" in page and 'aria-checked="true"' in page
    client.post("/mein/werbeblocker", data={"on": "0"})
    assert not adblock.device_on(MAC)
    assert 'aria-checked="false"' in client.get("/mein").text
    assert "handy" in client.get("/schutz").text  # Liste „Ausgeschaltet bei“
    client.post("/werbeblocker", data={"mac": MAC, "on": "1"})
    assert adblock.device_on(MAC)
    client.post("/werbeblocker", data={"on": "0"})
    assert not adblock.is_enabled()
    assert "vom Admin ausgeschaltet" in client.get("/mein").text


def test_whitelist_removes_from_ad_list(client, tmp_path):
    s = Settings.from_env()
    adblock.write_hosts(s, ["ads.example.com", "keep.example.com"])
    client.post("/blocklist/whitelist/add", data={"domain": "ads.example.com"})
    assert open(adblock.hosts_path(s), encoding="utf-8").read().count("ads.example.com") == 0
    assert adblock.count(s) == 1


def test_blocked_lines_are_counted_once_per_page():
    import time as t

    from app.dnsmasq import Collector

    c = Collector()
    now = int(t.time())
    c.feed("dnsmasq[9]: 1000000005 192.168.178.60/5353 /data/adblock.hosts ads.example.com is 0.0.0.0", now)
    c.feed("dnsmasq[9]: 1000000006 192.168.178.60/5353 /data/adblock.hosts ads.example.com is NODATA-IPv6", now + 1)
    c.feed("dnsmasq[9]: 1000000007 192.168.178.61/5353 /data/adblock.hosts track.example.org is 0.0.0.0", now)
    c.flush()
    st = db.blocked_stats("2000-01-01")
    assert st["hits"] == 2 and st["domains_count"] == 2
    assert db.blocked_stats("2000-01-01", ip="192.168.178.60")["hits"] == 1


def test_blocked_stats_on_pages(client):
    import time as t

    db.upsert_devices([("testclient", MAC, "handy")])
    now = int(t.time())
    db.add_blocked([(now, "testclient", "ads.example.com"), (now, "testclient", "x.example.com")])
    assert "in 7 Tagen blockiert" in client.get("/mein").text
    page = client.get("/schutz").text
    assert "Heute blockiert" in page and "ads.example.com" in page
    hub = client.get("/hub/statistik").text
    assert "Werbung blockiert" in hub and "ads.example.com" in hub
