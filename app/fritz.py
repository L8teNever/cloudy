"""FRITZ!Box-Integration über TR-064 (mittels fritzconnection).

Liest System-, Verbindungs-, WLAN- und Mesh-/Gerätedaten aus einer oder mehreren
FRITZ!Boxen aus und erlaubt das Schalten von Gast-WLAN, Haupt-WLAN, WAN-Neuverbindung
und Neustart über das cloudy-Dashboard.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from . import db
from .settings import Settings

logger = logging.getLogger("cloudy.fritz")

FRITZ_ROUTERS_KEY = "fritz_routers"
CACHE_TTL = 15.0  # Sekunden Cache, damit nicht bei jedem Seitenaufruf SOAP-Abfragen laufen

_cache_lock = threading.Lock()
_cached_data: dict[str, Any] | None = None
_cached_at: float = 0.0


def default_routers(settings: Settings) -> list[dict[str, str]]:
    return [
        {
            "name": "FRITZ!Box 1",
            "ip": settings.router_ip or "192.168.178.1",
            "user": "",
            "password": "",
        }
    ]


def get_routers(settings: Settings) -> list[dict[str, str]]:
    raw = db.get_setting(FRITZ_ROUTERS_KEY, "")
    if not raw:
        return default_routers(settings)
    try:
        data = json.loads(raw)
        if isinstance(data, list) and len(data) > 0:
            return data
    except Exception:
        pass
    return default_routers(settings)


def save_routers(routers: list[dict[str, str]]) -> None:
    db.set_setting(FRITZ_ROUTERS_KEY, json.dumps(routers))
    invalidate_cache()


def invalidate_cache() -> None:
    global _cached_at, _cached_data
    with _cache_lock:
        _cached_at = 0.0
        _cached_data = None


def _format_uptime(seconds: int | float | None) -> str:
    if not seconds:
        return ""
    s = int(seconds)
    days = s // 86400
    hours = (s % 86400) // 3600
    minutes = (s % 3600) // 60
    parts = []
    if days > 0:
        parts.append(f"{days} T{'ag' if days == 1 else 'age'}")
    if hours > 0 or days > 0:
        parts.append(f"{hours} Std.")
    parts.append(f"{minutes} Min.")
    return ", ".join(parts)


def _format_rate(bits_per_sec: int | float | None) -> str:
    if not bits_per_sec:
        return ""
    n = float(bits_per_sec)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f} Gbit/s".replace(".", ",")
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} Mbit/s".replace(".", ",")
    if n >= 1_000:
        return f"{n / 1_000:.0f} kbit/s"
    return f"{n:.0f} bit/s"


def test_router_connection(ip: str, user: str = "", password: str = "") -> tuple[bool, str]:
    """Testet die Verbindung zu einer FRITZ!Box und gibt (Erfolg, Modell/Fehlertext) zurück."""
    try:
        from fritzconnection import FritzConnection
        from fritzconnection.core.exceptions import FritzConnectionException, FritzSecurityError

        fc = FritzConnection(address=ip, user=user or None, password=password or None, timeout=4.0)
        model = getattr(fc, "modelname", None) or "FRITZ!Box"
        return True, f"Erfolgreich verbunden ({model})"
    except FritzSecurityError:
        return False, "Anmeldung fehlgeschlagen: Benutzername oder Kennwort ungültig."
    except FritzConnectionException as e:
        return False, f"FRITZ!Box nicht erreichbar ({e})"
    except Exception as e:
        return False, f"Verbindungsfehler: {e}"


def _fetch_single_router_data(router_cfg: dict[str, str]) -> dict[str, Any]:
    ip = router_cfg.get("ip", "192.168.178.1")
    user = router_cfg.get("user", "")
    password = router_cfg.get("password", "")
    name = router_cfg.get("name") or f"FRITZ!Box ({ip})"

    result: dict[str, Any] = {
        "name": name,
        "ip": ip,
        "user": user,
        "has_password": bool(password),
        "online": False,
        "error": None,
        "model": "",
        "software_version": "",
        "uptime": "",
        "connection_status": "Getrennt",
        "is_connected": False,
        "external_ip": "",
        "external_ipv6": "",
        "max_down": "",
        "max_up": "",
        "update_available": False,
        "wlan_24": None,
        "wlan_50": None,
        "guest_wlan": None,
        "device_connections": {},  # mac -> info dict
    }

    if not password:
        result["error"] = "Kein Kennwort hinterlegt. Bitte in den FRITZ!Box-Einstellungen eintragen."
        return result

    try:
        from fritzconnection import FritzConnection
        from fritzconnection.core.exceptions import FritzConnectionException, FritzSecurityError, FritzServiceError
        from fritzconnection.lib.fritzhosts import FritzHosts
        from fritzconnection.lib.fritzstatus import FritzStatus
        from fritzconnection.lib.fritzwlan import FritzGuestWLAN, FritzWLAN

        fc = FritzConnection(address=ip, user=user or None, password=password, timeout=4.0)
        fs = FritzStatus(fc)

        result["online"] = True
        result["model"] = getattr(fc, "modelname", "FRITZ!Box")
        result["software_version"] = getattr(fc, "system_version", "")
        result["uptime"] = _format_uptime(getattr(fs, "device_uptime", None))
        result["is_connected"] = bool(getattr(fs, "is_connected", False))
        result["connection_status"] = "Verbunden" if result["is_connected"] else "Nicht verbunden"
        result["external_ip"] = getattr(fs, "external_ip", "") or ""
        result["external_ipv6"] = getattr(fs, "external_ipv6", "") or ""

        bit_rate = getattr(fs, "max_bit_rate", (0, 0)) or (0, 0)
        if isinstance(bit_rate, (tuple, list)) and len(bit_rate) >= 2:
            result["max_down"] = _format_rate(bit_rate[0])
            result["max_up"] = _format_rate(bit_rate[1])

        result["update_available"] = bool(getattr(fs, "update_available", False))

        # WLAN Services (1 = 2.4 GHz, 2 = 5 GHz)
        fw = FritzWLAN(fc)
        for s_id in (1, 2):
            try:
                fw.service = s_id
                info = {
                    "service": s_id,
                    "band": "2,4 GHz" if s_id == 1 else "5 GHz",
                    "ssid": getattr(fw, "ssid", ""),
                    "enabled": bool(getattr(fw, "is_enabled", False)),
                    "channel": getattr(fw, "channel", None),
                }
                if s_id == 1:
                    result["wlan_24"] = info
                else:
                    result["wlan_50"] = info

                # Verbundene WLAN-Geräte
                for dev in fw.get_hosts_info():
                    mac = (dev.get("mac") or "").strip().lower()
                    if not mac:
                        continue
                    signal = dev.get("signal")
                    speed = dev.get("speed")
                    signal_str = f"{signal} %" if signal is not None else ""
                    speed_str = f"{speed} Mbit/s" if speed else ""
                    band_str = "2,4 GHz" if s_id == 1 else "5 GHz"
                    ssid_str = info["ssid"] or "WLAN"

                    details_parts = [f"WLAN ({band_str})"]
                    if ssid_str:
                        details_parts.append(f"SSID: {ssid_str}")
                    if speed_str:
                        details_parts.append(speed_str)
                    if signal_str:
                        details_parts.append(f"Signal: {signal_str}")

                    result["device_connections"][mac] = {
                        "type": "WLAN",
                        "router_name": name,
                        "router_ip": ip,
                        "ssid": ssid_str,
                        "band": band_str,
                        "speed": speed_str,
                        "signal": signal_str,
                        "port": "",
                        "summary": f"{ssid_str} ({band_str}) · {name}" if name else ssid_str,
                        "full_details": " · ".join(details_parts),
                    }
            except FritzServiceError:
                pass
            except Exception as e:
                logger.debug("Fehler beim Lesen von WLAN-Service %s: %s", s_id, e)

        # Gast-WLAN
        try:
            fg = FritzGuestWLAN(fc)
            result["guest_wlan"] = {
                "ssid": getattr(fg, "ssid", "FRITZ!Box Gastzugang"),
                "enabled": bool(getattr(fg, "is_enabled", False)),
            }
        except Exception:
            pass

        # LAN / Ethernet Hosts
        try:
            fh = FritzHosts(fc)
            for h in fh.get_hosts_info():
                mac = (h.get("mac") or "").strip().lower()
                if not mac or mac in result["device_connections"]:
                    continue  # Schon als WLAN erkannt
                itype = (h.get("interface_type") or "").strip()
                if itype.lower() in ("ethernet", "lan"):
                    result["device_connections"][mac] = {
                        "type": "LAN",
                        "router_name": name,
                        "router_ip": ip,
                        "ssid": "",
                        "band": "",
                        "speed": "",
                        "signal": "",
                        "port": "LAN",
                        "summary": f"LAN · {name}",
                        "full_details": f"LAN-Kabel an {name}",
                    }
        except Exception as e:
            logger.debug("Fehler beim Lesen von Hosts: %s", e)

    except FritzSecurityError:
        result["error"] = "Anmeldung fehlgeschlagen: Benutzername oder Kennwort ungültig."
    except FritzConnectionException as e:
        result["error"] = f"FRITZ!Box nicht erreichbar ({e})"
    except Exception as e:
        result["error"] = f"Fehler bei Abfrage: {e}"

    return result


def get_all_fritz_data(settings: Settings, force: bool = False) -> dict[str, Any]:
    """Liefert aggregierte Daten aller konfigurierten FRITZ!Boxen (mit Caching)."""
    global _cached_at, _cached_data

    now = time.time()
    with _cache_lock:
        if not force and _cached_data is not None and (now - _cached_at) < CACHE_TTL:
            return _cached_data

    routers = get_routers(settings)
    boxes = []
    all_device_connections: dict[str, dict[str, Any]] = {}

    for r_cfg in routers:
        data = _fetch_single_router_data(r_cfg)
        boxes.append(data)
        # Verbindungsdaten zusammenführen (aktive/aktuellere behalten)
        for mac, info in data.get("device_connections", {}).items():
            if mac not in all_device_connections or info.get("type") == "WLAN":
                all_device_connections[mac] = info

    res = {
        "boxes": boxes,
        "device_connections": all_device_connections,
        "updated_at": int(now),
    }

    with _cache_lock:
        _cached_data = res
        _cached_at = now

    return res


def get_device_connection(settings: Settings, mac: str) -> dict[str, Any] | None:
    """Liefert Verbindungsdetails für eine bestimmte MAC-Adresse."""
    if not mac:
        return None
    mac = mac.strip().lower()
    data = get_all_fritz_data(settings)
    return data.get("device_connections", {}).get(mac)


# ---------------------------------------------------------------------------
# Steuer-Aktionen (Änderungen über die Weboberfläche)
# ---------------------------------------------------------------------------

def _get_connection_for_router(settings: Settings, router_index: int):
    routers = get_routers(settings)
    if router_index < 0 or router_index >= len(routers):
        raise ValueError("Ungültiger FRITZ!Box-Index")
    cfg = routers[router_index]
    from fritzconnection import FritzConnection
    return FritzConnection(
        address=cfg.get("ip", "192.168.178.1"),
        user=cfg.get("user") or None,
        password=cfg.get("password") or None,
        timeout=5.0,
    )


def toggle_guest_wlan(settings: Settings, router_index: int, enable: bool) -> tuple[bool, str]:
    """Schaltet das Gast-WLAN an der angegebenen FRITZ!Box ein oder aus."""
    try:
        from fritzconnection.lib.fritzwlan import FritzGuestWLAN
        fc = _get_connection_for_router(settings, router_index)
        fg = FritzGuestWLAN(fc)
        if enable:
            fg.enable()
            msg = "Gast-WLAN wurde eingeschaltet."
        else:
            fg.disable()
            msg = "Gast-WLAN wurde ausgeschaltet."
        invalidate_cache()
        return True, msg
    except Exception as e:
        return False, f"Konnte Gast-WLAN nicht schalten: {e}"


def toggle_wlan(settings: Settings, router_index: int, service_id: int, enable: bool) -> tuple[bool, str]:
    """Schaltet ein WLAN-Band (1 = 2.4 GHz, 2 = 5 GHz) ein oder aus."""
    try:
        from fritzconnection.lib.fritzwlan import FritzWLAN
        fc = _get_connection_for_router(settings, router_index)
        fw = FritzWLAN(fc)
        fw.service = service_id
        band_name = "2,4 GHz" if service_id == 1 else "5 GHz"
        if enable:
            fw.enable()
            msg = f"{band_name}-WLAN wurde eingeschaltet."
        else:
            fw.disable()
            msg = f"{band_name}-WLAN wurde ausgeschaltet."
        invalidate_cache()
        return True, msg
    except Exception as e:
        return False, f"Konnte WLAN nicht schalten: {e}"


def reconnect_internet(settings: Settings, router_index: int) -> tuple[bool, str]:
    """Fordert eine neue Internet-IP an (WAN Reconnect)."""
    try:
        from fritzconnection.lib.fritzstatus import FritzStatus
        fc = _get_connection_for_router(settings, router_index)
        fs = FritzStatus(fc)
        fs.reconnect()
        invalidate_cache()
        return True, "Internetverbindung wird neu aufgebaut (neue IP angefordert)."
    except Exception as e:
        return False, f"Fehler beim Neuverbinden: {e}"


def reboot_fritzbox(settings: Settings, router_index: int) -> tuple[bool, str]:
    """Startet die FRITZ!Box neu."""
    try:
        fc = _get_connection_for_router(settings, router_index)
        fc.reboot()
        invalidate_cache()
        return True, "FRITZ!Box wird neu gestartet. Dies kann 2-3 Minuten dauern."
    except Exception as e:
        return False, f"Fehler beim Neustart: {e}"
