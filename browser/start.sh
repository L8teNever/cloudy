#!/bin/sh
# Chromium headless mit festem Profil (Cookies von Schutzseiten bleiben erhalten).
# Als root gestartet (Compose): Profilordner dem Benutzer "browser" geben (er gehört sonst root → "Permission denied"
# bei SingletonLock) und dann ohne Rechte weiterlaufen.
if [ "$(id -u)" = 0 ]; then
  chown -R 10001:10001 /profil 2>/dev/null
  exec setpriv --reuid=10001 --regid=10001 --init-groups env HOME=/home/browser "$0" "$@"
fi
rm -f /profil/Singleton*
# ANIME_PROXY gesetzt: der Browser geht NUR über den VPN-Proxy (kein Fallback auf direkt).
PROXY=""
[ -n "${ANIME_PROXY:-}" ] && PROXY="--proxy-server=${ANIME_PROXY}"
# Erweiterungen (z. B. ein VPN-Add-on): entpackte Ordner in data/browser/extensions/<name>/ (manifest.json) werden geladen.
EXT=""; for d in /profil/extensions/*/; do [ -f "${d}manifest.json" ] && EXT="${EXT:+$EXT,}${d%/}"; done
if [ -n "$EXT" ]; then EXT="--load-extension=$EXT"; DISABLE=""; else DISABLE="--disable-extensions"; fi
exec chromium $PROXY $EXT $DISABLE \
  --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --no-first-run --no-default-browser-check \
  --mute-audio --disable-background-networking --blink-settings=imagesEnabled=false \
  --remote-debugging-address=127.0.0.1 --remote-debugging-port="${CDP_PORT:-9222}" --remote-allow-origins=* \
  --user-data-dir=/profil --lang=de-DE \
  --user-agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36" \
  about:blank
