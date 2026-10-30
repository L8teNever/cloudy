"""Zugehörige Domains: Streaming-Dienste laden Video, Bilder und Anmeldung über eigene Domains.

Trägt man z. B. netflix.com als Seiten-Regel ein, laufen nflxvideo.net, nflximg.net usw. automatisch mit.
Dazu kommen Vorschläge aus dem echten DNS-Verkehr (welche Domains Geräte direkt nach der Seite abfragen).
"""

from __future__ import annotations

import time
from collections import Counter

from . import db

# Jede Gruppe: alle Domains gehören zusammen – eine davon als Regel reicht für alle.
BUNDLES: dict[str, tuple[str, ...]] = {
    "Netflix": ("netflix.com", "netflix.net", "nflxvideo.net", "nflximg.net", "nflximg.com", "nflxext.com",
                "nflxso.net", "nflxsearch.net"),
    "Disney+": ("disneyplus.com", "disney-plus.net", "dssott.com", "bamgrid.com", "disneystreaming.com",
                "dssedge.com"),
    "Prime Video": ("primevideo.com", "amazonvideo.com", "aiv-cdn.net", "aiv-delivery.net", "pv-cdn.net"),
    "Max": ("max.com", "hbomax.com", "hbo.com", "hbomaxcdn.com", "discomax.com"),
    "YouTube": ("youtube.com", "youtu.be", "googlevideo.com", "ytimg.com", "youtube-nocookie.com", "ggpht.com",
                "youtubei.googleapis.com"),
    "Spotify": ("spotify.com", "scdn.co", "spotifycdn.com", "spoti.fi"),
    "Twitch": ("twitch.tv", "ttvnw.net", "jtvnw.net", "twitchcdn.net", "twitchsvc.net", "ext-twitch.tv"),
    "DAZN": ("dazn.com", "dazn-api.com", "daznedge.net", "indazn.com", "dazndn.com"),
    "Paramount+": ("paramountplus.com", "cbsivideo.com", "pplusstatic.com"),
    "Crunchyroll": ("crunchyroll.com", "vrv.co"),
    "Hulu": ("hulu.com", "hulustream.com", "huluim.com"),
    "ChatGPT": ("chatgpt.com", "openai.com", "oaistatic.com", "oaiusercontent.com"),
    "TikTok": ("tiktok.com", "tiktokv.com", "tiktokcdn.com", "tiktokcdn-us.com", "byteoversea.com",
               "ibytedtos.com", "muscdn.com"),
}

# Fragen fast alle Geräte ständig nebenbei ab – nie als „zugehörig“ vorschlagen.
NOISE = {
    "google.com", "googleapis.com", "gstatic.com", "apple.com", "icloud.com", "microsoft.com", "windows.com",
    "msftconnecttest.com", "cloudflare.com", "cloudflare-dns.com", "akamaized.net", "akamaihd.net",
    "doubleclick.net", "google-analytics.com", "googletagmanager.com", "facebook.com", "fbcdn.net",
    "whatsapp.net", "whatsapp.com", "instagram.com", "amazonaws.com", "cloudfront.net", "fastly.net",
    "mzstatic.com", "aaplimg.com", "push.apple.com", "ntp.org", "local", "lan",
}


def _covers(rule: str, domain: str) -> bool:
    return domain == rule or domain.endswith("." + rule)


def bundle_of(domain: str) -> tuple[str, tuple[str, ...]] | None:
    """(Name, Domains) des Dienstes, zu dem diese Domain gehört."""
    domain = domain.lower().strip(".")
    for name, members in BUNDLES.items():
        if any(_covers(m, domain) or _covers(domain, m) for m in members):
            return name, members
    return None


def companions(domain: str) -> list[str]:
    """Weitere Domains, die mit dieser Regel automatisch mitlaufen."""
    found = bundle_of(domain)
    if not found:
        return []
    return [m for m in found[1] if not _covers(domain, m) and not _covers(m, domain)]


def expand(routes: dict[str, set[str]]) -> dict[str, set[str]]:
    """Regeln um die zugehörigen Domains erweitern (gleiche Profile)."""
    out = {d: set(sl) for d, sl in routes.items()}
    for domain, slots in routes.items():
        for extra in companions(domain):
            out.setdefault(extra, set()).update(slots)
    return out


def base_domain(name: str) -> str:
    """Grobe „Haupt-Domain“: letzte zwei Teile (bei co.uk, com.au usw. drei)."""
    parts = name.lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org", "ac", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def suggestions(rules: list[str], days: int = 7, window: int = 90, limit: int = 6) -> dict[str, list[tuple[str, int]]]:
    """Pro Regel: Domains, die Geräte oft innerhalb von `window` Sekunden nach der Seite abfragen.

    Zählt „Sitzungen“ (ein Gerät ruft die Seite auf), nicht einzelne Anfragen. Vorschlag, wenn eine Domain in
    mindestens 3 Sitzungen und in mindestens 40 % davon vorkommt – und noch nicht abgedeckt ist.
    """
    if not rules:
        return {}
    since = int(time.time()) - days * 86400
    covered = set(rules)
    for r in rules:
        covered.update(companions(r))
    out: dict[str, list[tuple[str, int]]] = {}
    for rule in rules:
        sessions = db.query_sessions(rule, since, window)
        if len(sessions) < 3:
            continue
        counts: Counter[str] = Counter()
        for names in sessions:
            counts.update({base_domain(n) for n in names})
        need = max(3, int(len(sessions) * 0.4 + 0.999))
        picked = [
            (d, n) for d, n in counts.most_common()
            if n >= need and d not in NOISE and not any(_covers(c, d) or _covers(d, c) for c in covered)
        ]
        if picked:
            out[rule] = picked[:limit]
    return out
