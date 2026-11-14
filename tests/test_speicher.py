"""Tests für den Speicher-Container (speicher/): Platten-Erkennung, Puffer, Backup-Planung, Anmeldung."""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "speicher"))

from speicher import auth, backup, config, disks, pool  # noqa: E402


@pytest.fixture(autouse=True)
def speicher_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MOUNT_ROOT", str(tmp_path / "mnt"))
    monkeypatch.setenv("SAMBA_CONF_DIR", str(tmp_path / "samba"))
    monkeypatch.delenv("SPEICHER_PASSWORD", raising=False)
    auth._fails.clear()


LSBLK = {
    "blockdevices": [
        {"name": "sda", "path": "/dev/sda", "size": 256060514304, "type": "disk", "model": "Samsung SSD", "serial": "S1",
         "rota": False, "tran": "sata", "maj:min": "8:0", "children": [
            {"name": "sda1", "path": "/dev/sda1", "size": 1, "type": "part", "fstype": "vfat", "maj:min": "8:1"},
            {"name": "sda2", "path": "/dev/sda2", "size": 2, "type": "part", "fstype": "LVM2_member", "maj:min": "8:2",
             "children": [{"name": "ubuntu--vg-root", "path": "/dev/mapper/ubuntu--vg-root", "type": "lvm", "fstype": "ext4", "maj:min": "252:0"}]},
        ]},
        {"name": "sdb", "path": "/dev/sdb", "size": 1000204886016, "type": "disk", "model": "WDC", "rota": True, "maj:min": "8:16",
         "children": [{"name": "sdb1", "path": "/dev/sdb1", "size": 1000, "type": "part", "fstype": "ext4", "uuid": "u-b", "label": "cloudy-hdd1", "maj:min": "8:17"}]},
        {"name": "sdc", "path": "/dev/sdc", "size": 500107862016, "type": "disk", "model": "Seagate", "rota": "1", "maj:min": "8:32"},
        {"name": "nvme0n1", "path": "/dev/nvme0n1", "size": 256060514304, "type": "disk", "rota": "0", "tran": "nvme", "maj:min": "259:0",
         "children": [{"name": "nvme0n1p1", "path": "/dev/nvme0n1p1", "size": 5, "type": "part", "fstype": "ntfs", "maj:min": "259:1"},
                      {"name": "nvme0n1p2", "path": "/dev/nvme0n1p2", "size": 5, "type": "part", "fstype": "ext4", "uuid": "u-n", "maj:min": "259:2"}]},
        {"name": "zram0", "path": "/dev/zram0", "size": 100, "type": "disk"},
        {"name": "sr0", "path": "/dev/sr0", "size": 100, "type": "rom"},
    ]
}


def test_parse_lsblk_sperrt_systemplatte_und_erkennt_arten():
    result = {d.name: d for d in disks.parse_lsblk(LSBLK, {"252:0"}, set())}
    assert set(result) == {"sda", "sdb", "sdc", "nvme0n1"}
    assert result["sda"].system and result["sda"].art == "SSD"
    assert not result["sdb"].system and result["sdb"].art == "HDD"
    assert result["sdb"].dateisystem.uuid == "u-b"
    assert result["sdc"].art == "HDD" and result["sdc"].dateisystem is None
    assert result["nvme0n1"].art == "NVMe-SSD"
    assert result["nvme0n1"].dateisystem.uuid == "u-n"  # NTFS zählt nicht, ext4 ist eindeutig


def test_parse_lsblk_gemountete_platte_ist_system():
    result = {d.name: d for d in disks.parse_lsblk(LSBLK, set(), {"/dev/sdb1"})}
    assert result["sdb"].system and "sdb1" in result["sdb"].system_grund


def test_host_used_ignoriert_eigene_mounts(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    (proc / "1").mkdir(parents=True)
    root = config.mount_root()
    (proc / "1" / "mountinfo").write_text(
        "29 1 252:0 / / rw shared:1 - ext4 /dev/mapper/ubuntu--vg-root rw\n"
        f"90 29 8:17 / {root}/platten/hdd1 rw shared:5 - ext4 /dev/sdb1 rw\n"
    )
    (proc / "swaps").write_text("Filename Type Size Used Priority\n/swap.img file 1 0 -2\n")
    monkeypatch.setattr(disks, "HOST_PROC", str(proc))
    majmin, paths = disks.host_used()
    assert majmin == {"252:0"}
    assert "/dev/sdb1" not in paths and "/swap.img" in paths


def test_partition_path():
    assert disks._partition_path("/dev/sdb") == "/dev/sdb1"
    assert disks._partition_path("/dev/nvme0n1") == "/dev/nvme0n1p1"


def test_mergerfs_options():
    opts = pool.mergerfs_options(config.Config(create_policy="kaputt", minfree_gb=0))
    assert "category.create=epmfs" in opts and "minfreespace=1G" in opts and "allow_other" in opts


def test_changed_files_nutzt_auch_ctime(tmp_path):
    root = tmp_path / "pool"
    (root / "Fotos" / "2019").mkdir(parents=True)
    alt = root / "Fotos" / "2019" / "urlaub.jpg"
    alt.write_text("x")
    os.utime(alt, (1_500_000_000, 1_500_000_000))  # altes Aufnahmedatum, aber gerade erst kopiert
    (root / ".mergerfs").write_text("")
    since = time.time() - 60
    assert backup.changed_files(root, since) == ["Fotos/2019/urlaub.jpg"]
    assert backup.changed_files(root, time.time() + 60) == []


def test_puffer_aufraeumen_loescht_nur_gesichertes(tmp_path):
    sicherung = backup.sicherung_dir()
    (sicherung / "a").mkdir(parents=True)
    (sicherung / "a" / "alt.txt").write_text("1")
    cut = time.time() + 0.05
    time.sleep(0.1)
    (sicherung / "neu.txt").write_text("2")
    assert backup.puffer_aufraeumen(None, cut) == 1
    assert not (sicherung / "a").exists() and (sicherung / "neu.txt").exists()


def test_kopie_marker_vor_erstem_backup():
    cfg = config.Config(puffer_seit=100.0, letztes_backup_start=0.0)
    assert backup.kopie_marker(cfg) == 100.0
    cfg.letztes_backup_start = 200.0
    assert backup.kopie_marker(cfg) == 200.0


def ts(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


def test_backup_due():
    now = ts("2026-11-14 00:05")
    assert backup.backup_due(now, "00:00", 0, 0)
    assert not backup.backup_due(now, "00:00", ts("2026-11-14 00:01"), ts("2026-11-14 00:01"))
    assert not backup.backup_due(now, "00:00", ts("2026-11-13 00:00"), ts("2026-11-14 00:01"))  # Fehler: 1 h warten
    assert backup.backup_due(ts("2026-11-14 01:10"), "00:00", ts("2026-11-13 00:00"), ts("2026-11-14 00:01"))
    assert not backup.backup_due(ts("2026-11-13 23:00"), "00:00", ts("2026-11-13 00:00"), 0)


def test_write_rclone_nimmt_nur_json():
    text = 'Paste the following:\n--->\n{"access_token":"a","token_type":"Bearer","refresh_token":"r","expiry":"x"}\n<---End paste'
    backup.write_rclone(text, client_id="id.apps")
    conf = backup.rclone_conf().read_text()
    assert "[cloudy-drive]" in conf and "client_id = id.apps" in conf and '"refresh_token":"r"' in conf
    assert oct(backup.rclone_conf().stat().st_mode & 0o777) == "0o600"
    assert not (config.data_dir() / "restic-passwort").exists()
    with pytest.raises(backup.BackupError):
        backup.write_rclone('{"access_token":"a"}')


def test_alte_ordner_zum_loeschen():
    now = ts("2026-11-14 00:05")
    namen = ["2026-11-13_00-00/", "2026-10-01_00-00/", "2026-10-15_23-00/", "meine-sachen/"]
    assert backup.alte_ordner_zum_loeschen(namen, now, 30) == ["2026-10-01_00-00"]


def test_restore_lehnt_fremde_staende_ab():
    for stand in ("../etc", "alt/../../x", "alt/kaputt", "aktuell2"):
        with pytest.raises(backup.BackupError):
            backup.restore(None, stand)


def test_config_update_roundtrip():
    config.update(pool=["a", "b"], backup_zeit="03:30")
    cfg = config.Config.load()
    assert cfg.pool == ["a", "b"] and cfg.backup_zeit == "03:30"
    with pytest.raises(KeyError):
        config.update(gibtsnicht=1)


@pytest.fixture
def web(monkeypatch):
    from fastapi.testclient import TestClient
    from speicher import main

    status = {
        "cfg": config.Config(), "drive": False, "mount_root": "/mnt/cloudy", "naechstes": 0, "warnungen": [],
        "st": {"hdds": [], "puffer": None, "pool": pool.Branch("pool", "", "/mnt/cloudy/pool")},
    }
    monkeypatch.setattr(main, "_status", lambda: status)
    monkeypatch.setattr(main, "_disks", lambda: {"disks": [], "rollen": {}, "health": {}, "disk_fehler": ""})
    with TestClient(main.app, base_url="http://speicher.lan") as c:
        yield c


def test_web_erst_einrichten_dann_anmelden(web):
    r = web.get("/", follow_redirects=False)
    assert r.headers["location"] == "/einrichten"
    r = web.post("/einrichten", data={"passwort": "geheim123", "passwort2": "geheim123"}, follow_redirects=False)
    assert r.status_code == 303 and auth.COOKIE in r.cookies
    for seite in ("/", "/platten", "/backup", "/einstellungen"):
        r = web.get(seite)
        assert r.status_code == 200 and "Platten" in r.text, seite
    assert r.headers["x-frame-options"] == "DENY"
    assert "script-src 'self';" in r.headers["content-security-policy"]
    assert web.get("/static/m3.css").status_code == 200
    # Zweites Einrichten geht nicht mehr
    r = web.post("/einrichten", data={"passwort": "anders1234", "passwort2": "anders1234"}, follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert auth.check_password("geheim123") and not auth.check_password("anders1234")


def test_web_ohne_csrf_und_fremde_herkunft_abgelehnt(web):
    web.post("/einrichten", data={"passwort": "geheim123", "passwort2": "geheim123"})
    r = web.post("/aktion/backup", data={}, follow_redirects=False)
    assert "fehler=" in r.headers["location"]
    r = web.post("/aktion/backup", data={}, headers={"origin": "http://boese.example"}, follow_redirects=False)
    assert r.status_code == 403


def test_web_login_sperre(web):
    auth.set_password("geheim123")
    for _ in range(5):
        assert "Falsches Passwort" in web.post("/login", data={"passwort": "nein"}).text
    assert "Zu viele Versuche" in web.post("/login", data={"passwort": "geheim123"}).text


def test_einstellungen_aendern_nur_gesendete_felder(web):
    web.post("/einrichten", data={"passwort": "geheim123", "passwort2": "geheim123"})
    config.update(backup_an=True, puffer_minuten=7)
    csrf = auth.csrf_token(web.cookies.get(auth.COOKIE))
    r = web.post("/einstellungen", data={"csrf": csrf, "zurueck": "/einstellungen", "create_policy": "mfs", "minfree_gb": "50"},
                 follow_redirects=False)
    assert r.headers["location"].startswith("/einstellungen?ok=")
    cfg = config.Config.load()
    assert cfg.create_policy == "mfs" and cfg.minfree_gb == 50 and cfg.backup_an and cfg.puffer_minuten == 7
    r = web.post("/einstellungen", data={"csrf": csrf, "zurueck": "https://boese.example", "behalten_tage": "9"}, follow_redirects=False)
    assert r.headers["location"].startswith("/?ok=") and config.Config.load().behalten_tage == 9


def test_web_api_ohne_login(web):
    auth.set_password("geheim123")
    assert web.get("/api/job").status_code == 401


# ---------- Freigaben (Samba/DLNA) ----------

from speicher import freigaben as fg  # noqa: E402


@pytest.fixture
def samba_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMBA_CONF_DIR", str(tmp_path / "samba"))
    fg._cache.update(t=0.0, geraete=[], fehler="")
    return tmp_path / "samba"


def test_parse_cloudy_nur_ipv4_und_eindeutig():
    data = [
        {"ip": "192.168.178.20", "mac": "AA:BB:CC:00:00:01", "hostname": "Simon-PC", "online": True},
        {"ip": "192.168.178.21", "mac": "aa:bb:cc:00:00:01", "hostname": "doppelt"},
        {"ip": "fd00::5", "mac": "aa:bb:cc:00:00:02", "hostname": "nur v6"},
        {"ip": "../../etc/x", "hostname": "boese"},
        {"ip": "192.168.178.30", "mac": None, "hostname": None, "lease_name": "tv"},
    ]
    result = fg.parse_cloudy(data)
    assert [(g.key, g.name, g.ip) for g in result] == [
        ("aa:bb:cc:00:00:01", "Simon-PC", "192.168.178.20"),
        ("ip:192.168.178.30", "tv", "192.168.178.30"),
    ]


def test_render_pro_geraet():
    e = fg.Einstellungen(benutzer=["simon", "mama"], freigaben=[
        fg.Freigabe("Familie", "Familie", standard="passwort", geraete={"pc": "offen", "tv": "versteckt"}),
        fg.Freigabe("Filme", "Filme", standard="offen", nur_lesen=True),
        fg.Freigabe("Privat", "", standard="versteckt", geraete={"pc": "passwort"}, benutzer=["simon"]),
    ])
    main = fg.render(e, None)
    assert "config file = " in main and "[Familie]" in main and "[Privat]" not in main
    assert "valid users = simon mama" in main
    pc = fg.render(e, "pc")
    assert "config file" not in pc  # keine Schleife
    familie = pc[pc.index("[Familie]"):pc.index("[Filme]")]
    assert "guest ok = yes" in familie and "guest only" not in familie and "valid users" not in familie
    assert "[Privat]" in pc and "valid users = simon\n" in pc
    filme = pc[pc.index("[Filme]"):pc.index("[Privat]")]
    assert "read only = yes" in filme and "guest ok = yes" in filme
    tv = fg.render(e, "tv")
    assert "[Familie]" not in tv and "[Filme]" in tv


def test_render_passwort_ohne_benutzer_sperrt():
    e = fg.Einstellungen(freigaben=[fg.Freigabe("X", standard="passwort")])
    assert "valid users = nobody-darf-rein" in fg.render(e, None)


def test_write_configs_nur_fuer_geraete_mit_regel_und_raeumt_auf(samba_dir, monkeypatch):
    devices = [fg.Geraet("pc", "PC", "192.168.178.20"), fg.Geraet("tv", "TV", "192.168.178.30")]
    monkeypatch.setattr(fg, "geraete", lambda max_age=30: (devices, ""))
    e = fg.Einstellungen(freigaben=[fg.Freigabe("A", geraete={"pc": "offen"})])
    assert fg.write_configs(e)
    assert sorted(p.name for p in (samba_dir / "hosts").iterdir()) == ["192.168.178.20.conf"]
    assert not fg.write_configs(e)  # nichts geändert
    devices[0] = fg.Geraet("pc", "PC", "192.168.178.25")  # neue IP aus cloudy
    assert fg.write_configs(e)
    assert sorted(p.name for p in (samba_dir / "hosts").iterdir()) == ["192.168.178.25.conf"]


def test_pruefungen():
    assert fg.check_name("  Meine  Filme ") == "Meine Filme"
    for bad in ("", "global", "a/b", "x" * 41, "[x]"):
        with pytest.raises(fg.FreigabeError):
            fg.check_name(bad)
    assert fg.check_ordner("/Fotos//2026/") == "Fotos/2026"
    for bad in ("../etc", "a/../../b", "a%Ib"):
        with pytest.raises(fg.FreigabeError):
            fg.check_ordner(bad)
    assert fg.check_user("Simon") == "simon"
    for bad in ("root", "speicher", "1abc", "a b"):
        with pytest.raises(fg.FreigabeError):
            fg.check_user(bad)
    with pytest.raises(fg.FreigabeError):
        fg.check_password("123")


def test_minidlna_nur_dlna_freigaben():
    e = fg.Einstellungen(dlna_name="Wohnzimmer", freigaben=[fg.Freigabe("Filme", "Filme", dlna=True), fg.Freigabe("Privat", "P")])
    conf = fg.minidlna_conf(e)
    assert "friendly_name=Wohnzimmer" in conf and conf.count("media_dir=") == 1 and "/pool/Filme" in conf


def _login(web):
    web.post("/einrichten", data={"passwort": "geheim123", "passwort2": "geheim123"})
    return auth.csrf_token(web.cookies.get(auth.COOKIE))


def test_web_freigabe_anlegen_und_geraete_regeln(web, samba_dir, monkeypatch):
    from speicher import main

    monkeypatch.setattr(fg.dienste, "abgleichen", lambda: None)
    monkeypatch.setattr(main, "_freigaben_data", lambda: {
        "fe": fg.Einstellungen.load(), "geraete": [fg.Geraet("aa:bb", "Simon-PC", "192.168.178.20", "aa:bb", True)],
        "cloudy_fehler": "", "server_ip": "192.168.178.2", "smb_laeuft": True, "dlna_laeuft": False, "dienst_fehler": {},
        "regel_text": fg.REGEL_TEXT, "pool_da": True,
    })
    csrf = _login(web)
    r = web.post("/freigaben/neu", data={"csrf": csrf, "name": "Familie", "ordner": "Familie", "standard": "passwort"}, follow_redirects=False)
    assert r.headers["location"].startswith("/freigaben/Familie?ok=")
    assert web.get("/freigaben").status_code == 200
    page = web.get("/freigaben/Familie")
    assert page.status_code == 200 and "Simon-PC" in page.text
    r = web.post("/freigaben/Familie/geraete", data={"csrf": csrf, "regel:aa:bb": "offen", "regel:cc:dd": "standard"}, follow_redirects=False)
    assert "ok=" in r.headers["location"]
    share = fg.Einstellungen.load().get("Familie")
    assert share.geraete == {"aa:bb": "offen"}
    web.post("/freigaben/Familie/geraete", data={"csrf": csrf, "regel:aa:bb": "standard"})
    assert fg.Einstellungen.load().get("Familie").geraete == {}
    r = web.post("/freigaben/neu", data={"csrf": csrf, "name": "../x", "standard": "offen"}, follow_redirects=False)
    assert "fehler=" in r.headers["location"]
    r = web.post("/freigaben/Familie/loeschen", data={"csrf": csrf}, follow_redirects=False)
    assert fg.Einstellungen.load().freigaben == []


def test_web_eigenes_geraet_nur_ipv4(web, samba_dir, monkeypatch):
    monkeypatch.setattr(fg.dienste, "abgleichen", lambda: None)
    csrf = _login(web)
    r = web.post("/geraete/neu", data={"csrf": csrf, "ip": "1.2.3", "zurueck": "/freigaben"}, follow_redirects=False)
    assert "fehler=" in r.headers["location"]
    web.post("/geraete/neu", data={"csrf": csrf, "ip": "192.168.178.50", "name": "Drucker"})
    assert fg.Einstellungen.load().eigene_geraete == {"ip:192.168.178.50": "Drucker"}


def test_windows_namen_pruefen_und_vorschlag():
    assert fg.check_windows_name("DESKTOP-ABC\\Simon") == "simon"
    assert fg.check_windows_name("MicrosoftAccount\\L8teNever@Gmail.com") == "l8tenever@gmail.com"
    assert fg.check_windows_name("  Simon   K ") == "simon k"
    for bad in ('a"b', "a=b", "", "x" * 105, "a;b"):
        with pytest.raises(fg.FreigabeError):
            fg.check_windows_name(bad)
    assert fg.parse_windows_names("a@b.de, Simon\nsimon") == ["a@b.de", "simon"]
    assert fg.name_aus_windows("l8tenever@gmail.com") == "l8tenever"
    assert fg.name_aus_windows("1.Simon.K@x.de") == "simon-k"


def test_benutzer_mit_windows_anmeldung(samba_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(fg, "_run", lambda cmd, stdin="": calls.append(cmd) or fg.subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(fg, "geraete", lambda max_age=30: ([], ""))
    name = fg.add_user("", "geheim123", "MicrosoftAccount\\L8teNever@gmail.com, Simon K")
    assert name == "l8tenever"
    e = fg.Einstellungen.load()
    assert e.windows == {"l8tenever": ["l8tenever@gmail.com", "simon k"]}
    assert (samba_dir / "benutzer.map").read_text() == (
        'l8tenever = "l8tenever@gmail.com" "MicrosoftAccount\\l8tenever@gmail.com" "simon k"\n')
    assert f"username map = {samba_dir}/benutzer.map" in (samba_dir / "smb.conf").read_text()
    # Browser-Anmeldung auch mit Windows-Namen
    assert fg.benutzer_fuer("DESKTOP-X\\Simon K") == "l8tenever"
    assert fg.check_login("l8tenever@gmail.com", "geheim123")
    # Name darf nicht doppelt vergeben werden
    with pytest.raises(fg.FreigabeError):
        fg.add_user("mama", "mama1234", "simon k")
    fg.set_windows_names("l8tenever", "")
    assert (samba_dir / "benutzer.map").read_text() == ""
    fg.set_windows_names("l8tenever", "neu@outlook.de")
    fg.delete_user("l8tenever")
    assert fg.Einstellungen.load().windows == {} and (samba_dir / "benutzer.map").read_text() == ""


def test_hintergrund_regel_und_messen(tmp_path, monkeypatch):
    from datetime import datetime

    from speicher import last

    assert last.im_fenster("01:00", "07:00", datetime(2026, 1, 1, 3, 0))
    assert not last.im_fenster("01:00", "07:00", datetime(2026, 1, 1, 12, 0))
    assert last.im_fenster("22:00", "06:00", datetime(2026, 1, 1, 23, 30))  # über Mitternacht
    e = fg.Einstellungen(hintergrund="zeitfenster", fenster_von="01:00", fenster_bis="07:00")
    ok, grund = last.erlaubt(e, datetime(2026, 1, 1, 12, 0))
    assert not ok and "Zeitfenster" in grund
    e.hintergrund = "leerlauf"
    last._uebertrag.clear()
    last._uebertrag.append((0, 50.0))  # gerade wird kopiert
    assert not last.erlaubt(e)[0]
    last._uebertrag.clear()
    assert last.erlaubt(e)[0]
    assert last.niedrig(["rsync"])[-1] == "rsync"
    last.messen()
    w = last.messen()
    assert 0 <= w["cpu"] <= 100 and w["ram_total"] > 0


def test_jellyfin_einrichtung_ablauf(tmp_path, monkeypatch):
    from speicher import jellyfin, pool as pl

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MOUNT_ROOT", str(tmp_path / "mnt"))
    (tmp_path / "data").mkdir()
    root = pl.paths()["pool"]
    root.mkdir(parents=True)
    monkeypatch.setattr(pl, "mounts", lambda: {str(root): "fuse.mergerfs"})
    fg.Einstellungen(benutzer=["simon"], freigaben=[fg.Freigabe("Familie", "Familie", dlna=True)]).save()
    aufrufe = []
    libs = []
    fertig = []

    def fake(method, path, body=None, token="", timeout=15):
        aufrufe.append((method, path.split("?")[0]))
        if path == "/Startup/Complete":
            fertig.append(True)
        if path == "/System/Info/Public":
            return {"Version": "10.10", "StartupWizardCompleted": bool(fertig)}
        if path == "/Users/AuthenticateByName":
            return {"AccessToken": "tok"}
        if path == "/Library/VirtualFolders" and method == "GET":
            return [{"Name": n} for n in libs]
        if path.startswith("/Library/VirtualFolders?"):
            libs.append(path.split("name=")[1].split("&")[0])
        return None

    monkeypatch.setattr(jellyfin, "_req", fake)
    monkeypatch.setattr(jellyfin, "gpu", lambda: "intel")
    jellyfin.abgleichen()
    assert ("POST", "/System/Configuration/encoding") in aufrufe and jellyfin.status()["hardware"] == "intel (VA-API)"
    s = jellyfin.status()
    assert s["eingerichtet"] and s["passwort"] and s["admin"] == "admin"
    assert ("POST", "/Startup/Complete") in aufrufe
    assert libs == ["Filme", "Serien", "Familie"]
    assert (root / "Streaming" / "Filme").is_dir()
    assert not fg.Einstellungen.load().videos_optimieren  # einmalig aus, Jellyfin wandelt beim Abspielen um
    aufrufe.clear()
    jellyfin.abgleichen()  # zweites Mal: nichts neu
    assert ("POST", "/Startup/Complete") not in aufrufe and libs == ["Filme", "Serien", "Familie"]


def test_jellyfin_streaming_freigabe_nicht_doppelt(tmp_path, monkeypatch):
    """Freigabe „Streaming“ mit „Auf Fernsehern zeigen“ wird keine Heimvideo-Mediathek (nur Ordner, kein Cover);
    eine schon angelegte wird entfernt, andere bleiben."""
    from speicher import jellyfin, pool as pl

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MOUNT_ROOT", str(tmp_path / "mnt"))
    (tmp_path / "data").mkdir()
    root = pl.paths()["pool"]
    root.mkdir(parents=True)
    fg.Einstellungen(benutzer=["simon"], freigaben=[
        fg.Freigabe("Familie", "Familie", dlna=True), fg.Freigabe("Streaming", "Streaming", dlna=True)]).save()
    assert [m["name"] for m in jellyfin.gewuenschte_mediatheken()] == ["Filme", "Serien", "Familie"]
    geloescht = []

    def fake(method, path, body=None, token="", timeout=15):
        if method == "DELETE":
            geloescht.append(path.split("name=")[1].split("&")[0])
        return None

    monkeypatch.setattr(jellyfin, "_req", fake)
    weg = jellyfin._doppelte_streaming_mediatheken("tok", [
        {"Name": "Streaming", "CollectionType": "homevideos", "Locations": [f"{root}/Streaming"]},
        {"Name": "Serien", "CollectionType": "tvshows", "Locations": [f"{root}/Streaming/Serien"]},
        {"Name": "Familie", "CollectionType": "homevideos", "Locations": [f"{root}/Familie"]},
    ])
    assert weg == ["streaming"] and geloescht == ["Streaming"]
