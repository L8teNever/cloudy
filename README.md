<img src="assets/icon.svg" width="64" height="64" alt="">

# cloudy

Der Raspberry Pi wird DNS-Server, DHCP-Server und (wenn du willst) Gateway fürs Heimnetz und zeigt auf einer
Webseite, **welches Gerät was aufgerufen hat und wie viele Daten dabei geflossen sind**.

Drei Stufen, jede einzeln ein- und ausschaltbar:

| Stufe | Was über den Pi läuft | Was du siehst |
|---|---|---|
| 1. DNS | nur die Namensabfragen | welche Domains jedes Gerät aufruft |
| 2. + DHCP (`cloudy dhcp an`) | + IP-Verteilung | + Gerätenamen und MAC-Adressen |
| 3. + Gateway (`cloudy gateway an`) | **der gesamte Datenverkehr** (Videos, Downloads …) | + Download/Upload pro Gerät und Domain |

```
Stufe 3:  Gerät ──alles──► Pi (misst) ──► Fritzbox ──► Internet
```

- Dashboard: **`http://cloudy.lan`** (oder `http://<pi-ip>`) – nur im Heimnetz erreichbar (keine Portfreigabe nötig).
  Der Name `cloudy.lan` funktioniert auf allen Geräten, die den Pi als DNS-Server nutzen.
- Pro Gerät: Name, IP, MAC, alle aufgerufenen Domains, Datenmenge pro Domain (z. B. `youtube.com 1,2 GB`).
- Inhalte von HTTPS-Seiten (welches Video, was getippt wurde) sieht man **nicht** – nur Domain und Datenmenge.
- Die Daten bleiben auf dem Pi (`~/cloudy/data`), 30 Tage Statistik, max. 50 000 Einzel-Aufrufe.

Gedacht für Raspberry Pi OS Lite (64-bit), Pi 3B+ oder neuer, per LAN-Kabel an der Fritzbox.

---

## 0. Vorher

- **Fritzbox-Backup:** System → Sicherung → Sichern.
- **Alte Portfreigabe** für cloudy (falls angelegt) löschen: Internet → Freigaben → Portfreigaben.
  Das Dashboard soll nur zu Hause erreichbar sein.
- Aktuellen Wert von **„Lokaler DNS-Server“** aufschreiben (bei dir `172.64.36.1`).

## 1. Pi einrichten (DNS an, IP-Verteilung noch aus)

Per SSH auf den Pi und ausführen:

```bash
curl -fsSL https://raw.githubusercontent.com/l8tenever/cloudy/main/scripts/setup-pi.sh | bash
```

Das Script installiert Docker, lädt cloudy, installiert den Befehl `cloudy` und gibt dem Pi die
**feste IP 192.168.178.2**. Dabei bricht die SSH-Verbindung ab → neu verbinden: `ssh <user>@192.168.178.2`.

> Andere IP gewünscht? `curl ... | PI_IP=192.168.178.3 bash` (muss außerhalb von .20–.200 liegen).

**Testen:** Am PC `nslookup youtube.com 192.168.178.2` ausführen → im Dashboard erscheint dein PC mit `youtube.com`.

## 2. Feste IPs übernehmen

Geräte, die in der Fritzbox „immer die gleiche IP“ haben (Drucker, NAS, Portfreigaben …), auf dem Pi in
`~/cloudy/data/dhcp-hosts.conf` eintragen – sonst bekommen sie später evtl. eine andere IP:

```
aa:bb:cc:dd:ee:ff,192.168.178.50,drucker
```

Die MAC-Adressen stehen in der Fritzbox unter Heimnetz → Netzwerk → Gerät bearbeiten.

## 3. Pi übernimmt die IP-Verteilung

1. Fritzbox: Heimnetz → Netzwerk → Netzwerkeinstellungen → IPv4 →
   Haken bei **„DHCP-Server aktivieren“ entfernen** → Übernehmen.
2. Auf dem Pi: `cloudy dhcp an`
3. Geräte kurz WLAN aus/an (oder Kabel ziehen), damit sie sofort vom Pi ihre IP holen.
   Ohne das passiert es von selbst, spätestens wenn die alte Fritzbox-Zuteilung abläuft.

Der Pi gibt allen Geräten weiterhin die **Fritzbox als Gateway** (Internet bleibt gleich schnell)
und **sich selbst als DNS-Server**.

### IPv6 (wichtig, sonst fragen manche Geräte am Pi vorbei)

Fritzbox → Heimnetz → Netzwerk → Netzwerkeinstellungen → **IPv6**:
- „DNSv6-Server auch über Router Advertisement bekanntgeben (RFC 5006)“ **ausschalten**
- DHCPv6-Server im Heimnetz **ausschalten** (bzw. keine DNS-Server per DHCPv6 zuweisen)

Die Menünamen können je nach FRITZ!OS-Version leicht abweichen. Kontrolle: Im Dashboard sollten deine
Geräte auftauchen – nicht die Fritzbox (`192.168.178.1`) als einziges „Gerät“.

## 4. Gesamter Datenverkehr über den Pi (Gateway)

1. Geräte mit **Portfreigabe** in der Fritzbox (NAS, Kamera, Server …) in `~/cloudy/data/dhcp-hosts.conf`
   mit `set:direkt` eintragen – sie gehen weiter direkt zur Fritzbox, sonst funktioniert die Freigabe nicht:
   ```
   aa:bb:cc:dd:ee:01,set:direkt,192.168.178.60,nas
   ```
2. Auf dem Pi: `cloudy gateway an`
3. Geräte kurz WLAN aus/an.

Der Pi leitet dann alles an die Fritzbox weiter (NAT) und misst pro Gerät und Domain, wie viel geladen wurde.
Damit nichts über IPv6 an ihm vorbeiläuft, liefert er im Gateway-Modus nur IPv4-Adressen aus (`filter-AAAA`).

**Gut zu wissen:**
- **Tempo:** Ein Pi 3B+ schafft ca. **150 Mbit/s für alle Geräte zusammen**. Ist dein Anschluss schneller,
  wird das Internet für alle langsamer. Ein Pi 4/5 schafft deutlich mehr.
- **Fritzbox sieht nur noch den Pi:** Kindersicherung, Online-Zähler und Priorisierung pro Gerät in der
  Fritzbox wirken dann nur noch auf den Pi als Ganzes.
- **Ausschalten:** `cloudy gateway aus` – Geräte gehen nach spätestens 1 Stunde wieder direkt zur Fritzbox,
  bis dahin leitet der Pi weiter (kein Ausfall).

## Zurück zur Fritzbox (alles wie vorher)

1. Auf dem Pi: `cloudy dhcp aus` (schaltet auch das Gateway aus)
2. Fritzbox: Haken bei **„DHCP-Server aktivieren“** wieder setzen, „Lokaler DNS-Server“ auf `172.64.36.1`.
3. Geräte kurz WLAN aus/an.

Ist der Pi kaputt/aus: Schritt 2 und 3 reichen. War das Gateway an, haben die Geräte bis zum WLAN aus/an
kein Internet – deshalb Schritt 3 nicht vergessen.

## Befehle auf dem Pi

| Befehl | Was passiert |
|---|---|
| `cloudy update` | Neueste Version von GitHub holen und neu starten |
| `cloudy dhcp an` / `cloudy dhcp aus` | IP-Verteilung durch den Pi ein-/ausschalten |
| `cloudy gateway an` / `cloudy gateway aus` | Gesamten Datenverkehr über den Pi leiten und messen |
| `cloudy status` | Läuft alles? |
| `cloudy logs` | Live-Logs |

Updates passieren **nur**, wenn du `cloudy update` ausführst. Neue Versionen baut GitHub Actions bei
jedem Push automatisch (`ghcr.io/l8tenever/cloudy`, für Pi und PC).

Einstellungen (Upstream-DNS, DHCP-Bereich, Dashboard-Passwort …) stehen in `~/cloudy/.env`,
danach `docker compose up -d` in `~/cloudy`.

## Eigene Namen im Heimnetz

- `http://cloudy.lan` zeigt auf den Pi. Anderen Namen: in `~/cloudy/.env` `LOCAL_NAME=...` ändern
  (nicht `.local`, keine echte Domain wie `cloudy.com` – die würdest du sonst im Internet nicht mehr erreichen).
- Weitere Namen für andere Geräte: `~/cloudy/data/lokale-namen.hosts` bearbeiten, z. B. `192.168.178.60  nas.lan`,
  danach `cloudy restart`.
- Geräte, die ihren Namen per DHCP melden, sind automatisch als `<name>.lan` erreichbar.
- Im Browser `http://` davor tippen, sonst sucht Chrome bei einem einzelnen Wort evtl. im Internet.
- Geht nicht bei Cloudflare WARP oder „Sicherem DNS“ im Browser (die gehen am Pi vorbei).

## Hinweise

- **Sicheres DNS (DoH):** Firefox und iCloud Private Relay werden automatisch auf normales DNS
  zurückgeschaltet. Chrome/Edge mit fest eingestelltem „Sicheres DNS“ oder VPN-Apps gehen trotzdem am Pi vorbei.
- **Mitbewohner/Familie:** Protokollieren, was andere im Netz aufrufen, ist ein Eingriff in deren
  Privatsphäre – sag ihnen Bescheid.
- **Später VPN für einzelne Geräte:** baut auf dem Gateway-Modus auf – der Pi schickt dann den Verkehr
  markierter Geräte durch einen VPN-Tunnel statt direkt zur Fritzbox. (Noch nicht eingebaut.)

## Entwickeln

```bash
pip install -r requirements.txt pytest httpx
python -m pytest
DNSMASQ_ENABLED=0 DB_PATH=./data/cloudy.db DATA_DIR=./data uvicorn app.main:app --port 8080
```
