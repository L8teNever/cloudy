from unittest.mock import MagicMock, PropertyMock, patch

from fastapi.testclient import TestClient

from app import db, fritz
from app.main import app
from app.settings import Settings


def test_fritz_routers_config():
    s = Settings.from_env()
    # Standard: 1 Router mit router_ip
    routers = fritz.get_routers(s)
    assert len(routers) == 1
    assert routers[0]["ip"] == s.router_ip

    # Speichern von 2 Routern
    new_routers = [
        {"name": "Box 1", "ip": "192.168.178.1", "user": "admin", "password": "pw1"},
        {"name": "Box 2", "ip": "192.168.178.2", "user": "", "password": "pw2"},
    ]
    fritz.save_routers(new_routers)
    loaded = fritz.get_routers(s)
    assert len(loaded) == 2
    assert loaded[0]["name"] == "Box 1"
    assert loaded[1]["ip"] == "192.168.178.2"


def test_fritz_data_without_password():
    s = Settings.from_env()
    fritz.save_routers([{"name": "Box 1", "ip": "192.168.178.1", "user": "", "password": ""}])
    fritz.invalidate_cache()
    data = fritz.get_all_fritz_data(s, force=True)
    assert len(data["boxes"]) == 1
    box = data["boxes"][0]
    assert box["online"] is False
    assert "Kein Kennwort hinterlegt" in box["error"]


def test_fritz_data_with_mocked_connection():
    s = Settings.from_env()
    fritz.save_routers([{"name": "Hauptbox", "ip": "192.168.178.1", "user": "admin", "password": "secret"}])
    fritz.invalidate_cache()

    mock_fc = MagicMock()
    mock_fc.modelname = "FRITZ!Box 7590 AX"
    mock_fc.system_version = "7.80"

    mock_fs = MagicMock()
    mock_fs.device_uptime = 90000
    mock_fs.is_connected = True
    mock_fs.external_ip = "93.184.216.34"
    mock_fs.external_ipv6 = "2001:db8::1"
    mock_fs.max_bit_rate = (100_000_000, 40_000_000)
    mock_fs.update_available = False

    mock_fw = MagicMock()
    mock_fw.ssid = "WLAN-Zuhause"
    mock_fw.is_enabled = True
    mock_fw.channel = 6
    mock_fw.get_hosts_info.return_value = [
        {"mac": "11:22:33:44:55:66", "signal": 75, "speed": 450}
    ]

    mock_fg = MagicMock()
    mock_fg.ssid = "FRITZ!Box Gastzugang"
    mock_fg.is_enabled = True

    mock_fh = MagicMock()
    mock_fh.get_hosts_info.return_value = [
        {"mac": "aa:bb:cc:dd:ee:ff", "interface_type": "Ethernet"}
    ]

    with patch("fritzconnection.FritzConnection", return_value=mock_fc), \
         patch("fritzconnection.lib.fritzstatus.FritzStatus", return_value=mock_fs), \
         patch("fritzconnection.lib.fritzwlan.FritzWLAN", return_value=mock_fw), \
         patch("fritzconnection.lib.fritzwlan.FritzGuestWLAN", return_value=mock_fg), \
         patch("fritzconnection.lib.fritzhosts.FritzHosts", return_value=mock_fh):
        data = fritz.get_all_fritz_data(s, force=True)

    assert len(data["boxes"]) == 1
    box = data["boxes"][0]
    assert box["online"] is True
    assert box["model"] == "FRITZ!Box 7590 AX"
    assert box["external_ip"] == "93.184.216.34"
    assert "Mbit/s" in box["max_down"]
    assert box["guest_wlan"]["enabled"] is True

    # Geräteverbindungen prüfen
    conns = data["device_connections"]
    assert "11:22:33:44:55:66" in conns
    wlan_dev = conns["11:22:33:44:55:66"]
    assert wlan_dev["type"] == "WLAN"
    assert "WLAN-Zuhause" in wlan_dev["summary"]

    assert "aa:bb:cc:dd:ee:ff" in conns
    lan_dev = conns["aa:bb:cc:dd:ee:ff"]
    assert lan_dev["type"] == "LAN"
    assert "LAN" in lan_dev["summary"]

    # Einzelabfrage
    dev_conn = fritz.get_device_connection(s, "11:22:33:44:55:66")
    assert dev_conn is not None
    assert dev_conn["type"] == "WLAN"


def test_fritz_mesh_repeater_without_wan():
    """Prüft, dass eine FRITZ!Box im IP-Client / Repeater-Modus (UPnP 401 bei WAN) sauber als Mesh-Repeater erkannt wird."""
    s = Settings.from_env()
    fritz.save_routers([{"name": "Repeater 7490", "ip": "192.168.178.2", "user": "admin", "password": "secret"}])
    fritz.invalidate_cache()

    mock_fc = MagicMock()
    mock_fc.modelname = "FRITZ!Box 7490 (UI)"
    mock_fc.system_version = "7.62"

    mock_fs = MagicMock()
    mock_fs.device_uptime = 54000
    # WAN schlägt fehl wie im Screenshot (UPnPError 401: Invalid Action)
    type(mock_fs).is_connected = PropertyMock(side_effect=Exception("UPnPError: errorCode: 401 errorDescription: Invalid Action"))

    mock_fw = MagicMock()
    mock_fw.ssid = "WLAN-Mesh"
    mock_fw.is_enabled = True
    mock_fw.channel = 1
    mock_fw.get_hosts_info.return_value = [
        {"mac": "33:44:55:66:77:88", "signal": 88, "speed": 866}
    ]

    mock_fg = MagicMock()
    mock_fg.ssid = "Gastnetz"
    mock_fg.is_enabled = False

    mock_fh = MagicMock()
    mock_fh.get_hosts_info.return_value = []

    with patch("fritzconnection.FritzConnection", return_value=mock_fc), \
         patch("fritzconnection.lib.fritzstatus.FritzStatus", return_value=mock_fs), \
         patch("fritzconnection.lib.fritzwlan.FritzWLAN", return_value=mock_fw), \
         patch("fritzconnection.lib.fritzwlan.FritzGuestWLAN", return_value=mock_fg), \
         patch("fritzconnection.lib.fritzhosts.FritzHosts", return_value=mock_fh):
        data = fritz.get_all_fritz_data(s, force=True)

    assert len(data["boxes"]) == 1
    box = data["boxes"][0]
    assert box["online"] is True
    assert box["is_repeater"] is True
    assert box["error"] is None  # Kein störender Fehler!
    assert box["connection_status"] == "Mesh-Repeater / IP-Client"
    assert box["model"] == "FRITZ!Box 7490 (UI)"
    assert box["software_version"] == "7.62"
    assert "33:44:55:66:77:88" in data["device_connections"]


def test_fritz_actions_mocked():
    s = Settings.from_env()
    fritz.save_routers([{"name": "Box 1", "ip": "192.168.178.1", "user": "", "password": "pw"}])

    mock_fc = MagicMock()
    mock_fg = MagicMock()
    mock_fw = MagicMock()
    mock_fs = MagicMock()

    with patch("fritzconnection.FritzConnection", return_value=mock_fc), \
         patch("fritzconnection.lib.fritzwlan.FritzGuestWLAN", return_value=mock_fg), \
         patch("fritzconnection.lib.fritzwlan.FritzWLAN", return_value=mock_fw), \
         patch("fritzconnection.lib.fritzstatus.FritzStatus", return_value=mock_fs):

        ok, msg = fritz.toggle_guest_wlan(s, 0, True)
        assert ok is True
        mock_fg.enable.assert_called_once()

        ok, msg = fritz.toggle_guest_wlan(s, 0, False)
        assert ok is True
        mock_fg.disable.assert_called_once()

        ok, msg = fritz.toggle_wlan(s, 0, 1, False)
        assert ok is True
        mock_fw.disable.assert_called_once()

        ok, msg = fritz.reconnect_internet(s, 0)
        assert ok is True
        mock_fs.reconnect.assert_called_once()

        ok, msg = fritz.reboot_fritzbox(s, 0)
        assert ok is True
        mock_fc.reboot.assert_called_once()


def test_fritz_web_routes(client):
    # GET /fritzbox Seite
    r = client.get("/fritzbox")
    assert r.status_code == 200
    assert "FRITZ!Box" in r.text
    assert "Zugangsdaten konfigurieren" in r.text

    # GET /teil/fritzbox Live-Stück
    r_teil = client.get("/teil/fritzbox")
    assert r_teil.status_code == 200

    # POST /fritzbox/config
    r_cfg = client.post(
        "/fritzbox/config",
        data={
            "r1_name": "Haupt-Fritzbox",
            "r1_ip": "192.168.178.1",
            "r1_user": "admin",
            "r1_password": "geheim123",
            "r2_name": "Repeater-Box",
            "r2_ip": "192.168.178.2",
            "r2_user": "",
            "r2_password": "geheim456",
        },
        follow_redirects=False,
    )
    assert r_cfg.status_code == 303
    assert "/fritzbox" in r_cfg.headers["location"]

    s = Settings.from_env()
    routers = fritz.get_routers(s)
    assert len(routers) == 2
    assert routers[0]["name"] == "Haupt-Fritzbox"
    assert routers[1]["name"] == "Repeater-Box"
