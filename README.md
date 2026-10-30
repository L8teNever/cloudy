<img src="assets/icon.svg" width="64" height="64" alt="">

# cloudy

Der Raspberry Pi wird DNS-Server, DHCP-Server und (wenn du willst) Gateway fürs Heimnetz und zeigt auf einer
Webseite, **welches Gerät was aufgerufen hat und wie viele Daten dabei geflossen sind**.

Drei Stufen, jede einzeln ein- und ausschaltbar:

| Stufe | Was über den Pi läuft | Was du siehst |
|---|---|---|
| 1. DNS | nur die Namensabfragen | welche Domains jedes Gerät aufruft |
| 2. + DHCP (`cloudy dhcp an`) | + IP-Verteilung | + Gerätenamen und MAC-Adressen |
| 3. + Gateway (`cloudy gateway an`) | **der gesamte Datenverkehr** + DNS-Force (auch fremde Resolver → Pi) | + Download/Upload pro Gerät und Domain |

```
Stufe 3:  Gerät ──alles──► Pi (misst) ──► Fritzbox ──► Internet
```

- Dashboard: **`http://cloudy.lan`** (oder `http://<pi-ip>`) – nur im Heimnetz erreichbar (keine Portfreigabe nötig).
  Der Name `cloudy.lan` funktioniert auf allen Geräten, die den Pi als DNS-Server nutzen.
- Self-Service ohne Passwort: **`http://cloudy.lan/mein`** – erkennt das aufrufende Gerät per IP und lässt nur dessen
  Internet-Weg (normal / WARP / VPN) umstellen; andere Geräte sieht man dort nicht.
- Die Geräteliste zeigt, **welches Gerät gerade online ist** (der Pi fragt alle bekannten Geräte alle 8 Sekunden aktiv per ARP ab;
  schaltet man am Handy das WLAN aus, steht innerhalb weniger Sekunden „offline“ da; über den Button „Prüfen“ im Dashboard auch sofort).
- **FRITZ!Box-Integration:** Zeigt Modell, Online-Status, Sync-Geschwindigkeit, externe IPv4/IPv6 und WLAN-Netze an. In der Geräteliste sieht man, mit welchem WLAN (SSID) oder LAN-Port und welcher FRITZ!Box das Gerät verbunden ist (unterstützt 2 Boxen). Gastnetz und Haupt-WLAN lassen sich direkt im Dashboard ein-/ausschalten, dazu WAN-Reconnect (neue IP) und FRITZ!Box-Neustart.
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

#### Optional: IPv6 durch den Pi und WARP/VPN (`cloudy ipv6 an`)

Willst du IPv6 nicht nur abschalten, sondern selbst durch WARP/VPN schicken (dann zeigt z. B. speed.cloudflare.com
„Connected via IPv6“ mit Cloudflare als Netzwerk), übernimmt der Pi das IPv6 im Heimnetz:

1. Fritzbox: **„Router Advertisement im LAN aktiv“ aus** (wie oben). Sonst gibt es zwei IPv6-Router im Heimnetz.
2. Auf dem Pi: `cloudy ipv6 an` (setzt `IPV6_ENABLED=1`, braucht `GATEWAY_ENABLED=1`).
3. Geräte kurz WLAN aus/an.

So funktioniert es:
- Der Pi verteilt per Router Advertisement ein **eigenes privates Netz** (zufälliges `fdXX:XXXX:XXXX:1::/64`, gespeichert in
  `data/ipv6-prefix`, oder fest über `IPV6_PREFIX` in der `.env`). Die Geräte bilden daraus selbst ihre Adresse.
- Geräte mit **WARP/VPN** (einzeln oder als Standard) schicken ihr IPv6 durch den Container; der schreibt die Absender
  auf seine Tunnel-Adresse um. Die Zuordnung läuft wie bei IPv4 nach MAC-Adresse.
- Der Container prüft selbst (alle ~30 Sekunden), ob sein Tunnel IPv6 durchlässt, und meldet das nach
  `data/warp/<slot>/ipv6` (`ok` oder `nein`; `cloudy status` zeigt es). Nur bei `ok` geht IPv6 in den Tunnel, sonst
  bekommen die Geräte sofort „keine Route“ und nehmen IPv4 – nie der Weg an der Tunnel vorbei.
- Geräte **ohne** Tunnel haben hinter der Fritzbox kein IPv6-Internet (die Fritzbox verteilt ja keins mehr). Sie bekommen
  ebenfalls sofort „keine Route“ und nutzen IPv4.
- Statistik und Domain-Liste ordnen IPv6-Anfragen über die MAC dem Gerät zu (kein zweiter Eintrag pro Gerät).
- Ob Cloudflares WARP-Connector IPv6 wirklich durchlässt, hängt von deinem Konto ab. Zeigt `cloudy status`
  „IPv6 im Tunnel warp1: nein“, bleibt alles auf IPv4 – dann ist nichts kaputt, nur kein IPv6 im Tunnel.
- Zurück: `cloudy ipv6 aus`.

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

**DNS-Force (automatisch mit Gateway):** Anfragen an fremde DNS-Server (z. B. fest eingestelltes `8.8.8.8`)
werden auf dem Pi abgefangen und von cloudy beantwortet – an den Geräten musst du DNS nicht umstellen.
Ausnahmen: Geräte mit `set:direkt` (gehen an der Fritzbox vorbei) und „Sicheres DNS“/DoH über HTTPS
(die bekannten DoH-Hosts blockt cloudy separat).

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
Gerät ──► Pi ──► WARP-Container (Account 1) ════Tunnel════► Cloudflare ──► Internet
```

- **WARP einrichten:** Im Dashboard auf **„VPN“** → Token des Accounts einfügen.
- **Gerät zuordnen:** Auf der Startseite in der Tabelle unter „Internet über“ **WARP 1** oder **WARP 2** wählen.
  Oder oben eine **Standard-Route** für alle Geräte ohne eigene Zuordnung einstellen.
- **Fällt der Tunnel aus**, gehen die Geräte automatisch wieder normal über die Fritzbox. Soll das Gerät dann
  gar kein Internet mehr haben (Kill-Switch), in `~/cloudy/.env` `WARP_KILLSWITCH=1` setzen.

## 6. Einzelne Geräte durch ein normales WireGuard-VPN schicken (z. B. Proton VPN)

Wie bei WARP, aber mit einer beliebigen WireGuard-Konfigurationsdatei (z. B. von Proton VPN, Mullvad,
NordVPN …). Zwei VPN-Plätze stehen bereit (`vpn1`, `vpn2`).

1. Beim VPN-Anbieter eine WireGuard-Konfiguration herunterladen (`.conf`).
2. Im Dashboard unter **„VPN“** bei **VPN 1** (oder VPN 2) auf **„Konfiguration bearbeiten“** klicken,
   den Inhalt der `.conf`-Datei hineinkopieren und speichern.
3. In der Geräteliste beim Gerät unter „Internet über“ den Slot wählen (z. B. **Proton NL**).

- **Fällt das VPN aus,** gehen die Geräte automatisch wieder normal ins Internet (über die Fritzbox).
  Mit `WARP_KILLSWITCH=1` ist das Internet für sie gesperrt, bis das VPN wieder steht.

## FRITZ!Box-Steuerung & Topologie

Unter dem Menüpunkt **FRITZ!Box** im Dashboard:
- **Auslesen:** Modell, FRITZ!OS-Version, DSL/Kabel-Geschwindigkeiten, externe IPv4/IPv6, Uptime, WLAN-Status (2,4 GHz, 5 GHz, Gastzugang).
- **Steuern:** Gastzugang ein-/ausschalten, Haupt-WLAN schalten, WAN-Neuverbindung (neue IP beziehen) und FRITZ!Box neu starten.
- **Geräte-Erkennung:** In der Geräteliste und auf der Geräte-Detailseite wird für jedes Gerät angezeigt, wie es verbunden ist (z. B. `📶 WLAN (5 GHz · 450 Mbit/s) · SSID` oder `🔌 LAN`).
- **Mehrere Boxen:** Unterstützt die Konfiguration von zwei FRITZ!Boxen (z. B. Hauptrouter + zweite Box / Repeater).
- **Voraussetzung:** In der FRITZ!Box unter *Heimnetz → Netzwerk → Netzwerkeinstellungen* den Haken bei **„Zugriff für Anwendungen zulassen“** setzen.

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

## Fernzugriff über `cloudy.kulbarts.com` (Cloudflare Tunnel + Zero Trust)

Das Dashboard ist von unterwegs unter deiner eigenen Adresse erreichbar – **nur nach Anmeldung** über Cloudflare
Zero Trust (Mail-Code/Google, mit WARP-Client automatisch). Eingerichtet wird alles im Dashboard unter
**Einstellungen → Fernzugriff**; auf dem Pi läuft dafür der Container `cloudy-tunnel` (ohne Token tut er nichts).

1. Zero Trust → **Networks → Tunnels** → Tunnel anlegen (z. B. `cloudy-pi`). Den langen Token (`eyJh…`) im Dashboard
   bei *Tunnel-Token* einfügen → Speichern. Nach ein paar Sekunden steht dort „Tunnel: verbunden“.
2. Beim Tunnel einen **Public Hostname** anlegen: `cloudy.kulbarts.com` → Service `http://172.29.88.1:<DASHBOARD_PORT>`
   (genau diese Adresse zeigt auch das Dashboard an).
3. Zero Trust → **Access → Applications** → *Self-hosted* für `cloudy.kulbarts.com`, Policy z. B. „nur meine E-Mail“,
   „Mit Cloudflare One Client authentifizieren“ an. Den **AUD-Tag** der App (und die Team-Domain, z. B.
   `l8tenever.cloudflareaccess.com`) im Dashboard eintragen → Speichern.

Sicherheit:
- cloudy prüft bei **jeder** Anfrage über den Tunnel selbst das Access-Token (Signatur, Team, AUD, Ablauf). Fehlt es,
  ist es falsch oder sind Team/AUD nicht eingetragen, gibt es nur „Nur mit Cloudflare-Anmeldung“ – auch dann, wenn die
  Access-App bei Cloudflare fehlt oder falsch eingestellt ist.
- Im Heimnetz ändert sich nichts (optional weiter `DASHBOARD_PASSWORD`). „Mein Gerät“ (`/mein`) geht nur zu Hause.
- Status: `cloudy status`, Logs: `cloudy logs tunnel`. Ausschalten: im Dashboard „Tunnel ausschalten“.

## Entwickeln

```bash
pip install -r requirements.txt pytest httpx
python -m pytest
DNSMASQ_ENABLED=0 DB_PATH=./data/cloudy.db DATA_DIR=./data uvicorn app.main:app --port 8080
```
