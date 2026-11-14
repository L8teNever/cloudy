"""Startet die Webseiten im selben Prozess: Verwaltung (SPEICHER_PORT), Dateien (DATEIEN_PORT) und Anime (ANIME_PORT; 0 = aus)."""
import asyncio
import os

import uvicorn


async def main() -> None:
    common = {"host": "0.0.0.0", "server_header": False, "proxy_headers": True}
    servers = [uvicorn.Server(uvicorn.Config("speicher.main:app", port=int(os.environ.get("SPEICHER_PORT", "8091")), **common))]
    dateien_port = int(os.environ.get("DATEIEN_PORT", "8092") or 0)
    if dateien_port:
        # Die Regeln pro Gerät hängen an der echten IP. X-Forwarded-For zählt nur von 127.0.0.1 (cloudy leitet
        # eigene Domains so weiter, Standard von uvicorn: forwarded_allow_ips=127.0.0.1) – Geräte können nichts vortäuschen.
        servers.append(uvicorn.Server(uvicorn.Config("speicher.dateien:app", port=dateien_port, host="0.0.0.0",
                                                     server_header=False, proxy_headers=True, forwarded_allow_ips="127.0.0.1",
                                                     lifespan="off")))
    anime_port = int(os.environ.get("ANIME_PORT", "8093") or 0)
    if anime_port:
        # Anime-Seite als eigene Webseite; X-Forwarded-For wie bei den Dateien nur von cloudy (127.0.0.1).
        servers.append(uvicorn.Server(uvicorn.Config("speicher.anime_web:app", port=anime_port, host="0.0.0.0",
                                                     server_header=False, proxy_headers=True, forwarded_allow_ips="127.0.0.1",
                                                     lifespan="off")))
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for s in servers:  # endet einer (Fehler oder Stopp), beide sauber beenden
        s.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)
    for t in done:
        t.result()


if __name__ == "__main__":
    asyncio.run(main())
