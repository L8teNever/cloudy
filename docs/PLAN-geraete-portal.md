# Plan: Geräte-Portal von unterwegs (`main.kulbarts.com`)

Stand: 30.10.2026. Dieser Plan ist für die spätere Umsetzung geschrieben (Mensch oder KI). Er beschreibt Ziel,
Architektur, alle Schritte mit Dateien, die offenen Punkte, die vorher geprüft werden müssen, und die Tests.

## 1. Ziel

Eine **zweite, eigene Webseite** auf dem Raspberry Pi, die:

1. auf einem **eigenen Port** läuft (z. B. `8081`), getrennt vom Admin-Dashboard (`8080`),
2. über den **vorhandenen Cloudflare Tunnel** unter einer eigenen Adresse erreichbar ist (z. B. `https://main.kulbarts.com`),
3. durch **Cloudflare Zero Trust (Access)** geschützt ist, wobei Geräte mit **WARP-Client (Cloudflare One)**
   **automatisch angemeldet** werden (kein Login-Fenster),
4. **erkennt, welches Gerät** gerade aufruft (z. B. „Laptop Max“), auch wenn es nicht zu Hause ist,
5. für **genau dieses Gerät** anzeigt und umstellen lässt, ob es über VPN/WARP ins Internet geht oder normal über die FRITZ!Box.

Das ist im Prinzip die vorhandene Seite `/mein` („Mein Gerät“), nur von überall erreichbar. `/mein` erkennt das Gerät
heute an der **IP im Heimnetz** – das geht über den Tunnel nicht (dort ist der Absender immer der Tunnel-Container
`172.29.88.10`). Der Kern der Arbeit ist deshalb die **Geräteerkennung über Cloudflare** (Abschnitt 4).

### Wichtig für die Erwartung (in der Seite auch so erklären)

Die VPN-Einstellung gilt pro Gerät (MAC-Adresse) und wirkt, **wenn das Gerät im Heimnetz ist** und der Pi das
Gateway ist. Von unterwegs kann man sie umstellen; sie wirkt dann ab dem nächsten Mal zu Hause. Unterwegs läuft der
Verkehr des Geräts nicht über den Pi, sondern direkt bzw. über den WARP-Client.

## 2. Was es schon gibt (wiederverwenden, nicht neu bauen)

| Was | Wo |
| --- | --- |
| Tunnel-Container (`cloudflared`), Token aus dem Dashboard | `tunnel/`, `docker-compose.yml` (Dienst `tunnel`, Netz `cloudy-tunnel`, Pi = `172.29.88.1`, Tunnel = `172.29.88.10`) |
| Access-JWT prüfen (Signatur, AUD, Ablauf) | `app/access.py` → `verify(token, team, aud)` |
| Tunnel-Anfragen erkennen | `app/main.py` → `from_tunnel()` |
| Tunnel-Schutz fürs Dashboard | `app/main.py` → Middleware `tunnel_guard` |
| Zweiten uvicorn-Server im selben Prozess starten | `app/main.py` (Port 80) und `app/certs.py` → `start_https_server()` (Muster: `uvicorn.Config(..., lifespan="off")` + Thread) |
| „Mein Gerät“: Seite + VPN umstellen | `app/main.py` → `mein_geraet`, `mein_set_warp`; Vorlage `app/templates/seiten/mein.html` |
| VPN/WARP pro MAC speichern | `app/db.py` → `set_device_warp(mac, slot)`, `device_warp()`; Profile: `warp_profiles()` in `app/main.py`; Prüfung `_check_slot()` |
| Geräte inkl. FRITZ!Box-Namen | `app/db.py` → `devices()`, Tabelle `fritz_hosts` |
| Einstellungen speichern | `app/db.py` → `get_setting` / `set_setting` |

## 3. Architektur

```
Laptop (WARP-Client an, irgendwo)
   │ https://main.kulbarts.com
   ▼
Cloudflare Access (App „cloudy Geräte-Portal“, eigener AUD, WARP-Anmeldung an)
   │ Header Cf-Access-Jwt-Assertion (JWT mit E-Mail und – bei WARP – Geräte-ID)
   ▼
Cloudflare Tunnel (derselbe Tunnel, zweiter Public Hostname)
   │ http://172.29.88.1:8081
   ▼
Pi: cloudy-Prozess, zweiter uvicorn-Server „Portal“ (Port 8081, eigene kleine FastAPI-App)
   ├─ prüft JWT (access.verify mit Portal-AUD)
   ├─ ermittelt Cloudflare-Geräte-ID → ordnet sie einem Heimnetz-Gerät (MAC) zu
   └─ zeigt Gerät + VPN-Auswahl, speichert per db.set_device_warp(mac, slot)
```

Entscheidungen:

- **Eigene FastAPI-App** (`app/portal.py`) statt Routen im Dashboard. So kann das Portal technisch nichts vom
  Admin-Dashboard erreichen, auch bei einem Fehler im Schutz. Gleicher Prozess, gleiche DB.
- **Derselbe Tunnel**, nur ein zweiter Public Hostname. Kein zweiter Container nötig.
- **Eigene Access-App mit eigenem AUD.** Ein Token für `cloudy.kulbarts.com` darf das Portal nicht öffnen und umgekehrt.

## 4. Geräteerkennung (Kernstück)

### 4.1 Cloudflare-Geräte-ID ermitteln

Meldet sich jemand über den WARP-Client an, enthält die Identität bei Cloudflare eine Geräte-ID. Weg:

1. JWT aus `Cf-Access-Jwt-Assertion` (oder Cookie `CF_Authorization`) mit `access.verify` prüfen (Portal-AUD).
2. Die vollständige Identität abrufen:
   `GET https://<team>.cloudflareaccess.com/cdn-cgi/access/get-identity` mit Cookie `CF_Authorization=<jwt>`.
   Die Antwort enthält bei WARP-Anmeldung u. a. `device_id` (und ggf. `device_sessions`, `devicePosture`).
3. Ergebnis pro JWT kurz zwischenspeichern (z. B. 5 Minuten, Schlüssel = Hash des JWT), damit nicht jeder Seitenaufruf
   Cloudflare fragt (Pi 3!).

**Vorher prüfen (offener Punkt A):** Mit einem echten Login einmal `get-identity` aufrufen und die Antwort ansehen.
Genau festhalten, in welchem Feld die Geräte-ID steht. Falls auch das JWT selbst eine Geräte-ID enthält, diese bevorzugen
(spart den Abruf). Ohne WARP-Client (Login im Browser per E-Mail) gibt es **keine** Geräte-ID → dann Abschnitt 4.3.

### 4.2 Cloudflare-Gerät → Heimnetz-Gerät (MAC)

Reihenfolge, die erste Regel mit Treffer gewinnt:

1. **Feste Zuordnung** aus neuer Tabelle `portal_devices` (siehe 5.1): `cf_device_id → mac`.
2. **Automatisch über die MAC:** Gerätedaten über die Cloudflare-API holen
   (`GET /accounts/{account_id}/devices/{device_id}` bzw. die neuere Physical-Devices-API). Enthält die Antwort eine
   MAC-Adresse, die cloudy kennt (`devices` oder `fritz_hosts`), Zuordnung speichern und benutzen.
   **Offener Punkt B:** Prüfen, ob/welches Feld die MAC enthält und ob es die WLAN/LAN-MAC ist, die im Heimnetz
   auftaucht. Achtung: Handys mit „privater WLAN-Adresse“ haben im Heimnetz eine andere MAC.
3. **Automatisch über den Namen:** Gerätename aus Cloudflare (z. B. Rechnername „LAPTOP-MAX“) gleich (ohne
   Groß/Klein) einem `hostname`/FRITZ!Box-Namen in `db.devices()` und **eindeutig** → als Vorschlag anzeigen, erst nach
   Bestätigung speichern (Namen sind nicht sicher genug für eine stille Zuordnung).
4. **Sonst: Kopplung** (4.3).

Für Schritt 2 braucht cloudy einen **Cloudflare-API-Token** mit Leserecht „Zero Trust: Read“ (Geräte lesen) und die
Account-ID. Beides in den Einstellungen speichern (wie den Tunnel-Token, Datei mit Rechten 600 oder `settings`).
Ohne Token: Schritt 2 überspringen.

### 4.3 Kopplung (Fallback, immer verfügbar)

Wenn das Gerät nicht erkannt wird, zeigt das Portal: „Dieses Gerät ist noch nicht zugeordnet“ und einen
**6-stelligen Kopplungscode** (gültig 10 Minuten, an die Cloudflare-Geräte-ID bzw. ohne ID an die E-Mail + ein
Browser-Cookie gebunden). Zuordnen geht dann auf zwei Arten:

- **Im Heimnetz am selben Gerät:** `http://cloudy.lan/mein` öffnen (erkennt die MAC über die IP, wie heute) →
  Feld „Kopplungscode“ → eingeben → Zuordnung `cf_device_id → mac` gespeichert.
- **Im Admin-Dashboard:** Geräte-Detailseite → „Mit Cloudflare-Gerät verknüpfen“ → Code eingeben oder aus einer Liste
  der offenen Kopplungen wählen.

Im Heimnetz mit WARP-Client an läuft der Aufruf von `cloudy.lan` evtl. auch über Cloudflare. Dann den Hinweis zeigen:
„Für die Kopplung WARP kurz ausschalten oder im Dashboard koppeln.“

### 4.4 Sicherheit der Zuordnung

- Eine Zuordnung erlaubt nur, **die VPN-Einstellung dieses einen Geräts** zu ändern. Keine anderen Daten, keine Liste
  anderer Geräte, keine Statistik fremder Geräte.
- Zuordnungen im Admin-Dashboard anzeigen und löschbar machen (Einstellungen → Geräte-Portal).
- Optional (Einstellung): Liste erlaubter E-Mails. Grundsätzlich regelt das aber die Access-Policy bei Cloudflare.

## 5. Umsetzung Schritt für Schritt

### 5.1 Datenbank (`app/db.py`)

Neue Tabellen in `init()`:

```sql
CREATE TABLE IF NOT EXISTS portal_devices (
    cf_device_id TEXT PRIMARY KEY,
    mac TEXT NOT NULL,
    email TEXT,
    cf_name TEXT,          -- Name aus Cloudflare, nur zur Anzeige
    created INTEGER NOT NULL,
    last_used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS portal_pairing (
    code TEXT PRIMARY KEY,
    cf_device_id TEXT,
    email TEXT,
    browser_id TEXT,       -- zufälliges Cookie, wenn keine Geräte-ID vorhanden ist
    expires INTEGER NOT NULL
);
```

Funktionen: `portal_mac_for(cf_device_id)`, `portal_link(cf_device_id, mac, email, cf_name)`, `portal_unlink(cf_device_id)`,
`portal_links()`, `pairing_create(...) -> code`, `pairing_redeem(code, mac) -> bool` (abgelaufene löschen),
`touch_portal(cf_device_id)`.

Für Browser-Logins ohne Geräte-ID: Schlüssel `browser:<zufällige id>` als `cf_device_id` benutzen
(Cookie `cloudy_portal`, HttpOnly, Secure, SameSite=Lax, 1 Jahr).

### 5.2 Einstellungen (`app/access.py` + Einstellungsseite)

- Neue Setting-Keys: `portal_aud` (AUD der Portal-Access-App), `cf_api_token` (optional), `cf_account_id` (optional).
  Team-Domain wird vom Dashboard übernommen (`tunnel_team`).
- `access.portal_config() -> (team, aud)`.
- Neue Funktion `access.identity(token, team) -> dict | None` (get-identity, mit Cache, Timeout 5 s, Fehler → None).
- `app/templates/seiten/einstellungen.html`: neuer Block „Geräte-Portal“: AUD, optional API-Token + Account-ID,
  Ziel-URL für Cloudflare anzeigen (`http://172.29.88.1:<PORTAL_PORT>`), Liste der Zuordnungen mit „Entfernen“.
- Neue Routen in `app/main.py` (mit `require_password`): `POST /portal/config`, `POST /portal/unlink`,
  `POST /portal/pair` (Code im Admin einlösen).

### 5.3 Portal-App (`app/portal.py`, neu)

- `portal = FastAPI()`; eigene Jinja-Vorlagen unter `app/templates/portal/` (Basis-Layout mit gleichem CSS wie
  `base.html`, aber ohne Menü zum Admin-Dashboard).
- **Middleware (Pflicht, vor allem anderen):**
  - Anfrage muss vom Tunnel kommen (`request.client.host == settings.tunnel_ip`). Ausnahme nur, wenn
    `PORTAL_ALLOW_LAN=1` gesetzt ist (zum Testen); sonst 403.
  - JWT prüfen mit `access.verify(token, team, portal_aud)`; ohne gültiges Token 403 mit Erklärung (wie
    `_tunnel_denied`). Ohne konfigurierten `portal_aud` ist das Portal komplett zu.
  - `request.state.email`, `request.state.cf_device_id` setzen.
- Routen:
  - `GET /` → Gerät ermitteln (Abschnitt 4.2). Treffer: Seite wie `mein.html` (Name, IP, online/offline,
    aktuelle Einstellung, Auswahl „normal (FRITZ!Box)“ / Standard / WARP-/VPN-Profile). Kein Treffer:
    Kopplungsseite mit Code (4.3) und ggf. Namensvorschlag (4.2 Punkt 3) mit Button „Ja, das bin ich“.
  - `POST /vpn` (Form `slot`) → MAC aus Zuordnung, Slot prüfen (gleiche Logik wie `_check_slot`), `db.set_device_warp`.
    Danach Redirect `/?ok=1`. Gegen CSRF: Formular-Token (zufällig, im Cookie + verstecktem Feld) oder
    Prüfung `Origin`/`Referer` == Portal-Host.
  - `POST /bestaetigen` → Namensvorschlag bestätigen → `portal_link`.
  - `GET /healthz`.
- Gemeinsame Logik mit `/mein` (Profile, Slot-Prüfung, Seitenkontext) in eine Hilfsfunktion auslagern, z. B.
  `views.device_vpn_ctx(settings, mac)`, und in beiden benutzen. `_check_slot` und `warp_profiles` dafür aus
  `app/main.py` nach `app/views.py` (oder `app/warp.py`) verschieben.

### 5.4 Server starten (`app/main.py`, lifespan)

- `PORTAL_PORT` aus der Umgebung (Standard `8081`, `0` = aus) → `Settings.portal_port` in `app/settings.py`.
- Im lifespan wie beim Port-80-Server einen zweiten `uvicorn.Server` mit `portal` starten
  (`host="0.0.0.0"`, `lifespan="off"`, `log_level="warning"`, eigener Thread) und beim Beenden `should_exit = True`.
  Nicht in Tests starten (`PYTEST_CURRENT_TEST`). Fehler beim Start nur loggen, nie das Dashboard verhindern.
- `app.state.settings` und Presence (`online_ips`) müssen im Portal verfügbar sein: `portal.state.settings =
  settings`, `portal.state.presence = presence` im lifespan setzen.

### 5.5 Docker / Netz

- `docker-compose.yml`: nichts zu ändern (cloudy läuft im Host-Netz; der Tunnel erreicht `172.29.88.1:8081`).
  Kommentar beim Dienst `tunnel` um den zweiten Hostnamen ergänzen.
- `.env.example`: `PORTAL_PORT=8081` mit Kommentar.
- Firewall: Port 8081 ist im Heimnetz offen, das Portal lehnt aber alles ab, was nicht vom Tunnel kommt (5.3).

### 5.6 Cloudflare einrichten (Anleitung für README und Einstellungsseite)

1. Zero Trust → Networks → Tunnels → vorhandener Tunnel → **Public Hostname hinzufügen**:
   `main.kulbarts.com` → Service `HTTP` → `172.29.88.1:8081`.
2. Zero Trust → Access → Applications → **Self-hosted** „cloudy Geräte-Portal“ für `main.kulbarts.com`:
   - Policy: Allow, z. B. eigene E-Mail(s).
   - Unter Authentication **„Cloudflare One Client authentication“ / WARP-Anmeldung** einschalten, damit Geräte mit
     WARP-Client ohne Login-Fenster rein kommen.
   - Optional zusätzlich Policy-Regel „Require Gateway“ (nur Geräte mit WARP-Client).
   - **AUD-Tag** kopieren → im cloudy-Dashboard unter Einstellungen → Geräte-Portal eintragen.
3. Zero Trust → Settings → WARP Client: **„Allow WARP authentication identity“** einschalten (nötig für Schritt 2).
4. Optional für die automatische Zuordnung: API-Token mit „Zero Trust: Read“ erstellen, Token + Account-ID in cloudy eintragen.

Hinweis: Wegen der Gateway-Regel „Nicht entschluesseln (alles)“ (Do Not Inspect, angelegt am 30.10.2026) gibt es keine
Zertifikatsprobleme; Cloudflare liefert für `main.kulbarts.com` ein gültiges Zertifikat (`*.kulbarts.com`).

## 6. Tests (`tests/test_portal.py`, neu)

Mit `TestClient(portal)` und gefälschter Client-IP (Tunnel-IP) und gemocktem `access.verify` / `access.identity`:

- Ohne Tunnel-IP → 403. Ohne Token → 403. Ohne `portal_aud` → 403. Token mit Dashboard-AUD → 403.
- Gültiges Token + bekannte Zuordnung → Seite zeigt Gerätenamen und aktuelle VPN-Einstellung.
- `POST /vpn` ändert nur die MAC aus der Zuordnung (`db.device_warp()` prüfen); ungültiger Slot → 400.
- `POST /vpn` ohne CSRF-Token / mit fremdem Origin → abgelehnt.
- Unbekanntes Gerät → Kopplungscode wird angezeigt; Code über `/mein` (Heimnetz, MAC bekannt) einlösen → danach erkannt.
- Abgelaufener Code → wird nicht angenommen.
- Automatische Zuordnung über MAC aus (gemockter) Cloudflare-API.
- Namensvorschlag nur bei eindeutigem Treffer; Zuordnung erst nach Bestätigung.
- Admin: `/portal/unlink` entfernt Zuordnung; ohne Dashboard-Passwort nicht erreichbar.
- Bestehende Tests müssen weiter laufen (`python -m pytest -q`).

## 7. Abnahme (fertig, wenn …)

- [ ] `https://main.kulbarts.com` auf einem Laptop mit WARP-Client öffnet ohne Login-Fenster und ohne Zertifikatswarnung.
- [ ] Die Seite zeigt den richtigen Gerätenamen (z. B. FRITZ!Box-Name) dieses Laptops.
- [ ] VPN-Auswahl umstellen → im Admin-Dashboard unter Geräte steht die neue Einstellung bei genau diesem Gerät.
- [ ] Ein zweites Gerät sieht nur sich selbst.
- [ ] Aus dem Heimnetz direkt `http://<pi>:8081` → 403.
- [ ] Ohne WARP-Client (Browser-Login per E-Mail) → Kopplung funktioniert, danach erkannt (Cookie).
- [ ] Neustart des Pi → Zuordnungen bleiben erhalten.
- [ ] Pi 3: Seitenaufruf < 1 s nach dem ersten Aufruf (Identität zwischengespeichert).

## 8. Offene Punkte, vor dem Bauen klären

- **A:** Wo genau steht die Geräte-ID (JWT-Claim oder nur `get-identity`)? Mit echtem Login prüfen.
- **B:** Liefert die Cloudflare-Geräte-API eine brauchbare MAC? Wenn nein: nur Kopplung + Namensvorschlag.
- **C:** Name der Adresse (`main.kulbarts.com` war ein Beispiel) mit dem Nutzer festlegen.
- **D:** Soll das Portal mehr können als VPN an/aus (z. B. Isolierung, eigene Statistik)? Erst mal **nur VPN**.
