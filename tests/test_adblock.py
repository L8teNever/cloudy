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
    page = client.get("/hub").text
    assert "Werbeblocker" in page and 'aria-checked="true"' in page
    client.post("/hub/werbeblocker", data={"on": "0"})
    assert not adblock.device_on(MAC)
    assert 'aria-checked="false"' in client.get("/hub").text
    assert "handy" in client.get("/schutz").text  # Liste „Ausgeschaltet bei“
    client.post("/werbeblocker", data={"mac": MAC, "on": "1"})
    assert adblock.device_on(MAC)
    client.post("/werbeblocker", data={"on": "0"})
    assert not adblock.is_enabled()
    assert "vom Admin ausgeschaltet" in client.get("/hub").text


def test_whitelist_removes_from_ad_list(client, tmp_path):
    s = Settings.from_env()
    adblock.write_hosts(s, ["ads.example.com", "keep.example.com"])
    client.post("/blocklist/whitelist/add", data={"domain": "ads.example.com"})
    assert open(adblock.hosts_path(s), encoding="utf-8").read().count("ads.example.com") == 0
    assert adblock.count(s) == 1
