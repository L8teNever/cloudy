<img src="assets/icon.svg" width="64" height="64" alt="">

# cloudy

Ein winziger Webserver für den Raspberry Pi. Er kann nur eins: auf der Webseite anzeigen,
**welche Geräte (IP/Hostname) was aufgerufen haben**.

- `/` zeigt das Dashboard (Geräte + letzte Aufrufe, aktualisiert sich alle 10 Sekunden).
- Jeder andere Pfad (`/foo`, `/test`, …) wird protokolliert und zeigt „OK“.
- Gespeichert werden die letzten 10 000 Aufrufe in `./data/access.db`.

## 1. Auf dem Raspberry Pi installieren

Per SSH auf den Pi verbinden und ausführen:

```bash
curl -fsSL https://raw.githubusercontent.com/l8tenever/cloudy/main/scripts/setup-pi.sh | bash
```

Das Script installiert Docker (falls nötig), legt `~/cloudy` an, lädt die `docker-compose.yml`
und startet alles. Danach läuft cloudy auf `http://<pi-ip>:8080`.

**Automatische Updates:** Bei jedem Push auf GitHub baut GitHub Actions ein neues Image
(`ghcr.io/l8tenever/cloudy`). Watchtower läuft im selben Compose-Stack, prüft alle 5 Minuten
auf ein neues Image und startet cloudy automatisch neu. Du musst nichts tun.

> Das Paket muss in GitHub auf **Public** stehen (Profil → Packages → cloudy → Package settings →
> Change visibility). Sonst auf dem Pi vorher `docker login ghcr.io` mit einem Token (`read:packages`) machen.

## 2. Fritzbox einrichten

1. **Feste IP für den Pi:** Heimnetz → Netzwerk → Netzwerkverbindungen → Pi → Bearbeiten →
   „Diesem Netzwerkgerät immer die gleiche IPv4-Adresse zuweisen“.
2. **Portfreigabe:** Internet → Freigaben → Portfreigaben → „Gerät für Freigaben hinzufügen“ → Pi wählen →
   „Neue Freigabe“ → Portfreigabe → Protokoll TCP, Port extern 80 (oder 8080) → Port an Gerät 8080.
3. **Adresse von außen:** Über die MyFRITZ-Adresse (Internet → MyFRITZ-Konto) oder einen DynDNS-Namen
   (Internet → Freigaben → DynDNS).

Dann ist das Dashboard unter `http://<deine-adresse>[:port]/` erreichbar.

## Hinweise

- **Kein Passwort:** Standardmäßig sieht jeder, der die Adresse kennt, die Zugriffsliste.
  Optional in `~/cloudy/.env` `DASHBOARD_PASSWORD=...` setzen (HTTP-Basic-Auth, Benutzername egal) und
  mit `docker compose up -d` neu starten. Alle anderen Pfade bleiben öffentlich.
- **Nur HTTP, kein TLS.** Das Passwort wird also unverschlüsselt übertragen. Für HTTPS später z. B.
  Cloudflare Tunnel oder Caddy davorsetzen (dann `TRUST_PROXY=1` setzen, damit die echte Client-IP erscheint).
- **Hostnamen** werden nur für Geräte im Heimnetz angezeigt (Reverse-DNS über die Fritzbox).
  Aus dem Internet siehst du nur IP-Adressen.

## Entwickeln

```bash
pip install -r requirements.txt pytest httpx
pytest
DB_PATH=./data/access.db uvicorn app.main:app --port 8080
```
