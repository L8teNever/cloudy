"""Domain-Weiterleitungen: Ruft ein Gerät im Heimnetz z. B. tiktok.com auf, landet es auf einer anderen Seite.

So funktioniert es:
1. DNS: dnsmasq beantwortet die Quell-Domain (und alle Subdomains, z. B. www./m.) mit der IP des Pi.
2. HTTP/HTTPS: Der Webserver des Pi erkennt am Host-Namen die Regel und antwortet mit einer Weiterleitung.
   Für HTTPS erzeugt der Pi passend ein Zertifikat (signiert von der lokalen cloudy-CA). Ohne Warnung klappt das
   nur auf Geräten, auf denen die cloudy-CA installiert ist.
"""

import re
from urllib.parse import urlsplit

HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


def normalize_source(text: str) -> str:
    """'https://www.TikTok.com/foo' -> 'tiktok.com'. ValueError, wenn das keine Domain ist."""
    text = text.strip().lower()
    if "://" not in text:
        text = "http://" + text
    host = (urlsplit(text).hostname or "").rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not HOST_RE.match(host):
        raise ValueError(f"„{text}“ ist keine gültige Domain")
    return host


def normalize_target(text: str) -> str:
    """'wikipedia.org' -> 'https://wikipedia.org'. ValueError bei leerer/ungültiger Adresse."""
    text = text.strip()
    if not text:
        raise ValueError("Ziel fehlt")
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"„{text}“ ist keine gültige Ziel-Adresse")
    return text


def find_rule(host: str, rules: dict[str, str]) -> tuple[str, str] | None:
    """Passende Regel für einen Host-Namen inkl. Subdomains: (Quelle, Ziel) oder None."""
    host = host.strip().lower().rstrip(".")
    while host:
        if host in rules:
            return host, rules[host]
        if "." not in host:
            return None
        host = host.split(".", 1)[1]
    return None


def dnsmasq_lines(sources: list[str], pi_ip: str) -> list[str]:
    # address=/domain/IP gilt auch für alle Subdomains. Andere Typen (z. B. AAAA) beantwortet dnsmasq dann
    # lokal ohne Adresse – der Browser bekommt also keine echte IPv6-Adresse der Seite (CI prüft das).
    return [f"address=/{src}/{pi_ip}" for src in sorted(set(sources))]
