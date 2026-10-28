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
- Die Geräteliste zeigt, **welches Gerät gerade online ist** (der Pi fragt alle bekannten Geräte alle 8 Sekunden aktiv per ARP ab;
  schaltet man am Handy das WLAN aus, steht innerhalb weniger Sekunden „offline“ da; über den Button „Prüfen“ im Dashboard auch sofort).
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

### IPv6 (wichtig für VPN/WARP – sonst läuft IPv6 am VPN vorbei)

Im Heimnetz ist die **Fritzbox der IPv6-Router**. Solange sie IPv6 verteilt, schicken die Geräte IPv6 direkt an
sie – der Pi sieht diesen Verkehr nie und kann ihn auch nicht ins VPN leiten. Deshalb:

1. Fritzbox → Heimnetz → Netzwerk → Netzwerkeinstellungen → Reiter **IPv6** →
   Haken bei **„Router Advertisement im LAN aktiv“ entfernen** → Übernehmen.
2. An den Geräten einmal WLAN aus/an.

Danach läuft alles über IPv4 durch den Pi (und bei zugeordneten Geräten durchs VPN). Im Gateway-Modus liefert
der Pi außerdem nur IPv4-Adressen aus (`filter-AAAA`). Das Dashboard zeigt eine rote Warnung, solange die Fritzbox
noch IPv6 verteilt und ein VPN benutzt wird.

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

## 5. Einzelne Geräte durch Cloudflare WARP schicken

Pro Gerät einstellbar: **normal (Fritzbox)**, **WARP-Account 1** oder **WARP-Account 2**. Der gesamte
Internetverkehr des Geräts läuft dann durch den WARP-Tunnel des gewählten Cloudflare-Zero-Trust-Accounts
(und damit durch dessen Gateway-Regeln). Am Gerät selbst muss nichts installiert werden.

```
Gerät ──► Pi ──► WARP-Container (Account 1) ══Tunnel══► Cloudflare ──► Internet
```

Voraussetzung: Gateway-Modus ist an (Schritt 4).

**Token holen (pro Account einmal):**
1. [one.dash.cloudflare.com](https://one.dash.cloudflare.com) mit dem Account öffnen.
2. Unter **Networks → Mesh** (früher „WARP Connector“, je nach Dashboard auch unter Networks → Connectors)
   einen neuen Knoten anlegen, z. B. `cloudy-pi`. Cloudflare zeigt dann einen Befehl wie
   `warp-cli connector new eyJh...` – **nur den langen Token** (`eyJh...`) kopieren.
3. Im Geräteprofil des Knotens den **Split Tunnel** so lassen, dass `192.168.0.0/16` und `172.16.0.0/12`
   ausgeschlossen sind (Standard). Sonst findet der Verkehr nicht zurück ins Heimnetz.

**Im Dashboard:**
1. Abschnitt **„Cloudflare WARP“** → Name eintragen (z. B. „Account Max“), Token einfügen → Speichern.
   Nach ca. 10–30 Sekunden steht dort **„verbunden“**.
2. Gerät in der Liste anklicken → **„Internet über“** → Account wählen → Übernehmen.
3. Zweiten Account genauso im zweiten Profil anmelden.

**Gut zu wissen:**
- **Fällt der Tunnel aus**, gehen die Geräte automatisch wieder normal über die Fritzbox. Soll das Gerät dann
  lieber gar kein Internet haben: in `~/cloudy/.env` `WARP_KILLSWITCH=1`, dann `cloudy restart`.
- **Tempo:** Ein Pi 3B+ schafft durch die Tunnel zusammen etwa 50–80 Mbit/s.
- **DNS** fragt weiterhin der Pi (bei dir an `172.64.36.1`), das Dashboard zeigt also weiter alle Domains.
- **Abmelden** im Dashboard trägt den Pi auch bei Cloudflare aus.
- Die Anmeldung bleibt bei `cloudy update` und Neustarts erhalten (`~/cloudy/data/warp/`). Der Token ist ein
  Zugangsschlüssel zu deinem Zero-Trust-Account – die Datei nicht weitergeben.
- Status auf dem Pi: `cloudy status`, Logs: `cloudy logs warp1`.

## 6. Einzelne Geräte durch Proton VPN (oder anderes WireGuard-VPN) schicken

Gleiches Prinzip wie bei WARP: Im Dashboard stehen **VPN 1** und **VPN 2**. Pro Gerät wählst du unter
„Internet über“, ob es normal, über WARP oder über ein VPN ins Internet geht. Am Gerät muss nichts installiert werden.

```
Gerät ──► Pi ──► VPN-Container (WireGuard) ══Tunnel══► Proton ──► Internet
```

**WireGuard-Datei bei Proton holen:**
1. [account.protonvpn.com/downloads](https://account.protonvpn.com/downloads) → Abschnitt **WireGuard-Konfiguration**.
2. Plattform **Router** (alternativ GNU/Linux) wählen, einen Namen geben (z. B. `cloudy`), einen **Server** wählen
   (mit dem kostenlosen Tarif nur die Free-Server) und **Erstellen** → Datei herunterladen.
3. Die Datei mit einem Texteditor öffnen und den **ganzen Inhalt** kopieren.

**Im Dashboard:** Abschnitt „Cloudflare WARP & VPN“ → bei **VPN 1** den Inhalt einfügen → Speichern.
Nach wenigen Sekunden steht dort **„verbunden“**. Danach beim Gerät „Internet über“ → VPN 1.

**Gut zu wissen:**
- Proton erlaubt pro Konfiguration nur **ein** Gerät gleichzeitig. Der Pi ist dabei ein Gerät, alle zugeordneten
  Geräte teilen sich also die Verbindung. Eine zweite Datei (anderer Server) kommt in **VPN 2**.
- Der kostenlose Tarif ist langsamer als Bezahltarife und hat wenige Länder. Auf dem Pi 3B+ sind etwa
  **100 Mbit/s** durch WireGuard möglich.
- **Fällt das VPN aus,** gehen die Geräte automatisch wieder normal ins Internet (über die Fritzbox).
  Willst du das nicht, damit nichts am VPN vorbei geht: `WARP_KILLSWITCH=1` in `~/cloudy/.env`, dann `cloudy restart`.
- Befehle in der Datei (`PostUp` …) und `DNS` werden ignoriert. DNS fragt weiterhin der Pi.
- Die Datei enthält deinen privaten Schlüssel (liegt in `~/cloudy/data/warp/vpn1/`) – nicht weitergeben.
- Status: `cloudy status`, Logs: `cloudy logs vpn1`.

## Alles auf einmal umschalten (ohne am PC etwas zu ändern)

Alles läuft auf dem Pi, an den Geräten wird nichts eingestellt. In der Geräteliste steht oben
**„Alle Geräte ohne eigene Einstellung gehen über: …“**. Wähl dort z. B. **VPN 1** – dann läuft der gesamte
Internetverkehr aller Geräte durch Proton, auch von neuen Geräten. Bei einzelnen Geräten kannst du in der Spalte
„Internet über“ etwas anderes wählen, z. B. **normal (Fritzbox)** für den Fernseher oder das NAS.
Zurück auf normal: oben **„normal (Fritzbox)“** wählen. Es wirkt innerhalb weniger Sekunden, die Geräte brauchen
kein WLAN aus/an. Voraussetzung ist der Gateway-Modus (Schritt 4).

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

## Domain-Weiterleitungen

Im Dashboard unter **„Domain-Weiterleitungen“**: z. B. `tiktok.com` → `wikipedia.org`. Gilt für die Domain und alle
Subdomains (`www.`, `m.` …) auf allen Geräten, die den Pi als DNS benutzen. Wirkt sofort.

- **https://-Seiten** (fast alle): Der Pi erzeugt automatisch ein passendes Zertifikat. Ohne Fehlermeldung klappt das
  nur auf Geräten, auf denen das **cloudy-Zertifikat** installiert ist (siehe „HTTPS mit eigenem Zertifikat“).
  Ohne Zertifikat zeigt der Browser eine Warnung statt weiterzuleiten.
- Geht nicht bei „Sicherem DNS“ im Browser und nicht bei Apps, die eigene DNS-Server benutzen.
- Bereits geöffnete Seiten merkt sich der Browser kurz – ggf. Browser neu starten.

## Eigene Namen im Heimnetz

- `http://cloudy.lan` zeigt auf den Pi. Anderen Namen: in `~/cloudy/.env` `LOCAL_NAME=...` ändern
  (nicht `.local`, keine echte Domain wie `cloudy.com` – die würdest du sonst im Internet nicht mehr erreichen).
- Weitere Namen für andere Geräte: `~/cloudy/data/lokale-namen.hosts` bearbeiten, z. B. `192.168.178.60  nas.lan`,
  danach `cloudy restart`.
- Geräte, die ihren Namen per DHCP melden, sind automatisch als `<name>.lan` erreichbar.
- Im Browser `http://` davor tippen, sonst sucht Chrome bei einem einzelnen Wort evtl. im Internet.
- Geht nicht bei „Sicherem DNS“ im Browser (geht am Pi vorbei). Auch mit Cloudflare WARP kann es Probleme geben.

## HTTPS mit eigenem Zertifikat (`https://cloudy.lan`)

Der Pi erzeugt beim Start selbst eine kleine **lokale Zertifizierungsstelle (CA)** und ein Zertifikat für
`cloudy.lan` und die Pi-IP (gültig ca. 2 Jahre, wird automatisch erneuert). `https://cloudy.lan` läuft auf Port 443,
`http://cloudy.lan` bleibt zusätzlich bestehen.

Ohne Installation zeigt der Browser „Nicht sicher“. Damit die Warnung verschwindet, die CA **einmal pro Gerät**
installieren: `http://cloudy.lan/ca.crt` (bzw. `http://192.168.178.3/ca.crt`) herunterladen und dann:

- **Windows:** Doppelklick auf `cloudy-ca.crt` → Zertifikat installieren → Lokaler Computer →
  „Alle Zertifikate in folgendem Speicher speichern“ → **Vertrauenswürdige Stammzertifizierungsstellen**.
  Browser neu starten.
- **iPhone/iPad:** Datei in Safari laden → Einstellungen → „Profil geladen“ → Installieren. Danach
  Einstellungen → Allgemein → Info → **Zertifikatsvertrauenseinstellungen** → „cloudy lokale CA“ einschalten.
- **Android:** Einstellungen → Sicherheit → Weitere Einstellungen → Verschlüsselung & Anmeldedaten →
  Zertifikat installieren → **CA-Zertifikat**.
- **Mac:** Doppelklick → Schlüsselbund „System“ → Zertifikat öffnen → Vertrauen → „Immer vertrauen“.
- **Firefox** nutzt einen eigenen Speicher: Einstellungen → Datenschutz & Sicherheit → Zertifikate anzeigen →
  Zertifizierungsstellen → Importieren.

Der **private Schlüssel** der CA bleibt auf dem Pi (`~/cloudy/data/certs/`, nur für root lesbar). Über `/ca.crt`
wird nur das öffentliche Zertifikat ausgeliefert. Die CA gilt nur für deine eigenen Geräte, in die du sie
installierst. Sichere die Dateien nicht öffentlich.

Ausschalten: in `~/cloudy/.env` `HTTPS_PORT=0`, dann `cloudy restart`.

## Hinweise

- **Sicheres DNS (DoH):** Firefox und iCloud Private Relay werden automatisch auf normales DNS
  zurückgeschaltet. Chrome/Edge mit fest eingestelltem „Sicheres DNS“ oder VPN-Apps gehen trotzdem am Pi vorbei.
- **Mitbewohner/Familie:** Protokollieren, was andere im Netz aufrufen, ist ein Eingriff in deren
  Privatsphäre – sag ihnen Bescheid.

## Entwickeln

```bash
pip install -r requirements.txt pytest httpx
python -m pytest
DNSMASQ_ENABLED=0 DB_PATH=./data/cloudy.db DATA_DIR=./data uvicorn app.main:app --port 8080
```
