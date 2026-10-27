<img src="assets/icon.svg" width="64" height="64" alt="">

# cloudy

Der Raspberry Pi wird DNS-Server und (wenn du willst) DHCP-Server fürs Heimnetz und zeigt auf einer Webseite,
**welches Gerät welche Webseiten/Dienste aufgerufen hat**.

```
Gerät ──"wo ist youtube.com?"──► Pi (protokolliert) ──► Cloudflare-DNS 172.64.36.1
Gerät ──eigentliche Daten──────► Fritzbox ──────────────► Internet   (unverändert schnell)
```

- Dashboard: `http://192.168.178.2:8080` – nur im Heimnetz erreichbar (keine Portfreigabe nötig).
- Pro Gerät: Name, IP, MAC, alle aufgerufenen Domains mit Anzahl und Zeit.
- Inhalte von HTTPS-Seiten (welches Video, was getippt wurde) sieht man **nicht** – nur die Domain.
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

## Zurück zur Fritzbox (alles wie vorher)

1. Auf dem Pi: `cloudy dhcp aus`
2. Fritzbox: Haken bei **„DHCP-Server aktivieren“** wieder setzen, „Lokaler DNS-Server“ auf `172.64.36.1`.
3. Geräte kurz WLAN aus/an.

Ist der Pi kaputt/aus: Schritt 2 und 3 reichen.

## Befehle auf dem Pi

| Befehl | Was passiert |
|---|---|
| `cloudy update` | Neueste Version von GitHub holen und neu starten |
| `cloudy dhcp an` / `cloudy dhcp aus` | IP-Verteilung durch den Pi ein-/ausschalten |
| `cloudy status` | Läuft alles? |
| `cloudy logs` | Live-Logs |

Updates passieren **nur**, wenn du `cloudy update` ausführst. Neue Versionen baut GitHub Actions bei
jedem Push automatisch (`ghcr.io/l8tenever/cloudy`, für Pi und PC).

Einstellungen (Upstream-DNS, DHCP-Bereich, Dashboard-Passwort …) stehen in `~/cloudy/.env`,
danach `docker compose up -d` in `~/cloudy`.

## Hinweise

- **Sicheres DNS (DoH):** Firefox und iCloud Private Relay werden automatisch auf normales DNS
  zurückgeschaltet. Chrome/Edge mit fest eingestelltem „Sicheres DNS“ oder VPN-Apps gehen trotzdem am Pi vorbei.
- **Mitbewohner/Familie:** Protokollieren, was andere im Netz aufrufen, ist ein Eingriff in deren
  Privatsphäre – sag ihnen Bescheid.
- **Später VPN für einzelne Geräte:** möglich, da der Pi die IPs verteilt – dann bekommt nur das markierte
  Gerät den Pi als Gateway. (Noch nicht eingebaut.)

## Entwickeln

```bash
pip install -r requirements.txt pytest httpx
python -m pytest
DNSMASQ_ENABLED=0 DB_PATH=./data/cloudy.db DATA_DIR=./data uvicorn app.main:app --port 8080
```
