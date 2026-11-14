#!/bin/sh
# Chromium headless mit festem Profil (Cookies von Schutzseiten bleiben erhalten).
rm -f /profil/Singleton*
exec chromium \
  --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --no-first-run --no-default-browser-check \
  --mute-audio --disable-background-networking --disable-extensions --blink-settings=imagesEnabled=false \
  --remote-debugging-address=127.0.0.1 --remote-debugging-port="${CDP_PORT:-9222}" --remote-allow-origins=* \
  --user-data-dir=/profil --lang=de-DE \
  --user-agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36" \
  about:blank
