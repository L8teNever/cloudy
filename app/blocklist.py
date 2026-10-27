import os
import time

from . import db
from .settings import Settings

BLOCKLIST_SETTING_KEY = "blocklist_enabled"
PAUSED_UNTIL_KEY = "blocklist_paused_until"

# Gecrawlte und kuratierte Consent Management Plattformen (CMPs) und Tracker.
# Wichtig: Reine Werbe-Server (wie doubleclick, pagead2) sind hier bewusst NICHT enthalten,
# damit Webseiten NICHT mit "Bitte Werbeblocker ausschalten" blockieren.
COOKIE_CONSENT_DOMAINS = [
    # Cookiebot
    "consent.cookiebot.com",
    "consentcdn.cookiebot.com",
    "cookiebot.com",
    # OneTrust / CookieLaw
    "cdn.cookielaw.org",
    "geolocation.onetrust.com",
    "optanon.blob.core.windows.net",
    "onetrust.com",
    # Usercentrics
    "privacy-cs.usercentrics.eu",
    "api.usercentrics.eu",
    "app.usercentrics.eu",
    "usercentrics.eu",
    # Didomi
    "sdk.didomi.io",
    "app.didomi.io",
    "didomi.io",
    # Consentmanager
    "cdn.consentmanager.net",
    "delivery.consentmanager.net",
    "service.consentmanager.net",
    "consentmanager.mgr.consensu.org",
    # Quantcast CMP
    "cmp.quantcast.com",
    "quantcast.mgr.consensu.org",
    # TrustArc
    "consent.trustarc.com",
    "consent-pref.trustarc.com",
    # Sourcepoint
    "cdn.privacy-mgmt.com",
    "sp-prod.sp-api.com",
    "sourcepoint.mgr.consensu.org",
    # Iubenda
    "cdn.iubenda.com",
    "cs.iubenda.com",
    # Civic UK
    "cc.cdn.civicuk.com",
    # Termly
    "app.termly.io",
    # Klaro
    "cdn.kiprotect.com",
    # Cookie Information
    "policy.cookieinformation.com",
    # Osano
    "cmp.osano.com",
    # Commanders Act / TrustCommander
    "cdn.trustcommander.net",
    # Axeptio
    "static.axept.io",
    "client.axept.io",
]


def is_enabled() -> bool:
    val = db.get_setting(BLOCKLIST_SETTING_KEY, "0")
    return val in ("1", "true", "yes", "on")


def set_enabled(enabled: bool) -> None:
    db.set_setting(BLOCKLIST_SETTING_KEY, "1" if enabled else "0")


def paused_until() -> int | None:
    val = db.get_setting(PAUSED_UNTIL_KEY, "0")
    try:
        ts = int(val)
        return ts if ts > time.time() else None
    except ValueError:
        return None


def pause_for_seconds(seconds: int) -> None:
    until = int(time.time()) + seconds
    db.set_setting(PAUSED_UNTIL_KEY, str(until))


def resume() -> None:
    db.set_setting(PAUSED_UNTIL_KEY, "0")


def generate_blocklist(settings: Settings) -> int:
    """Schreibt die blocklist.hosts-Datei fuer dnsmasq."""
    enabled = is_enabled()
    is_paused = paused_until() is not None
    whitelist = set(db.whitelist())

    domains = set()
    if enabled and not is_paused:
        for d in COOKIE_CONSENT_DOMAINS:
            d_clean = d.strip().lower()
            if d_clean and d_clean not in whitelist:
                domains.add(d_clean)

    os.makedirs(os.path.dirname(settings.blocklist_path) or ".", exist_ok=True)
    with open(settings.blocklist_path, "w", encoding="utf-8") as f:
        f.write("# cloudy Cookie- & Tracking-Schutz\n")
        for d in sorted(domains):
            f.write(f"0.0.0.0 {d}\n:: {d}\n")

    return len(domains)
