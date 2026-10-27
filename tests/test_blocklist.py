import os
from app import blocklist, db
from app.settings import Settings


def test_blocklist_toggle_and_generate(tmp_path):
    s = Settings.from_env()
    # Initial: disabled
    blocklist.set_enabled(False)
    blocklist.resume()
    assert not blocklist.is_enabled()
    assert blocklist.paused_until() is None

    # Enable
    blocklist.set_enabled(True)
    assert blocklist.is_enabled()

    count = blocklist.generate_blocklist(s)
    assert count > 0
    assert os.path.exists(s.blocklist_path)

    with open(s.blocklist_path, encoding="utf-8") as f:
        content = f.read()
    assert "consent.cookiebot.com" in content

    # Whitelist domain
    db.add_whitelist("consent.cookiebot.com")
    assert "consent.cookiebot.com" in db.whitelist()

    blocklist.generate_blocklist(s)
    with open(s.blocklist_path, encoding="utf-8") as f:
        content_after = f.read()
    assert "consent.cookiebot.com" not in content_after

    # Pause
    blocklist.pause_for_seconds(60)
    assert blocklist.paused_until() is not None
    blocklist.generate_blocklist(s)
    with open(s.blocklist_path, encoding="utf-8") as f:
        assert "0.0.0.0" not in f.read()

    # Resume
    blocklist.resume()
    assert blocklist.paused_until() is None

    # Cleanup whitelist
    db.remove_whitelist("consent.cookiebot.com")
    assert "consent.cookiebot.com" not in db.whitelist()


def test_blocklist_dashboard_endpoints(client):
    blocklist.set_enabled(False)
    blocklist.resume()

    # Toggle an
    res = client.post("/blocklist/toggle", follow_redirects=True)
    assert res.status_code == 200
    assert blocklist.is_enabled()
    assert "Cookie-Schutz" in res.text

    # Whitelist add
    res = client.post("/blocklist/whitelist/add", data={"domain": "meineseite.de"}, follow_redirects=True)
    assert res.status_code == 200
    assert "meineseite.de" in db.whitelist()

    # Pause
    res = client.post("/blocklist/pause", data={"minutes": 15}, follow_redirects=True)
    assert res.status_code == 200
    assert blocklist.paused_until() is not None

    # Resume
    res = client.post("/blocklist/resume", follow_redirects=True)
    assert res.status_code == 200
    assert blocklist.paused_until() is None

    # Whitelist remove
    res = client.post("/blocklist/whitelist/remove", data={"domain": "meineseite.de"}, follow_redirects=True)
    assert res.status_code == 200
    assert "meineseite.de" not in db.whitelist()
