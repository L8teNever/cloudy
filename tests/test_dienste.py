import time

from app import db, dienste
from app.dnsmasq import sync_redirects_hosts
from app.settings import Settings


def test_companions_and_expand():
    assert "nflxvideo.net" in dienste.companions("netflix.com")
    assert "netflix.com" in dienste.companions("www.netflix.com") or "nflxvideo.net" in dienste.companions("www.netflix.com")
    assert dienste.companions("bank.de") == []
    out = dienste.expand({"netflix.com": {"vpn1"}})
    assert out["nflxvideo.net"] == {"vpn1"} and out["netflix.com"] == {"vpn1"}


def test_ipset_lines_include_companions(monkeypatch):
    monkeypatch.setenv("GATEWAY_ENABLED", "1")
    monkeypatch.setattr("app.dnsmasq.ipset_supported", lambda: True)
    s = Settings.from_env()
    db.set_site_route("netflix.com", "vpn1")
    sync_redirects_hosts(s)
    conf = open(s.redirects_path, encoding="utf-8").read()
    assert "ipset=/netflix.com/" in conf and "ipset=/nflxvideo.net/" in conf


def test_suggestions_from_dns_traffic():
    now = int(time.time())
    rows = []
    for i, ip in enumerate(["192.168.178.10", "192.168.178.11", "192.168.178.12"]):
        t = now - 3600 * (i + 1)
        rows += [(t, ip, "example-stream.com", "A"), (t + 3, ip, "cdn.examplecdn.net", "A"),
                 (t + 4, ip, "www.google.com", "A")]
    rows.append((now - 100, "192.168.178.10", "only-once.org", "A"))
    db.add_queries(rows)
    sug = dienste.suggestions(["example-stream.com"])
    names = [d for d, _ in sug.get("example-stream.com", [])]
    assert "examplecdn.net" in names and "google.com" not in names


def test_page_shows_bundle_badge(client):
    client.post("/seiten-routen/add", data={"domain": "netflix.com", "slot": "direct"})
    page = client.get("/vpn").text
    assert "Netflix +" in page and "nflxvideo.net" in page
