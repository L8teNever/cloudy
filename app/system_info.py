"""System- und Leistungsmetriken (CPU, RAM, Festplatte, Temperatur, Uptime, Cloudy-Verbrauch).

Liest Ressourcenverbrauch des Raspberry Pi / Hostsystems und des Cloudy-Prozesses aus.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from typing import Any

logger = logging.getLogger("cloudy.system")

try:
    import psutil
except ImportError:
    psutil = None

_cloudy_proc = None
if psutil:
    try:
        _cloudy_proc = psutil.Process()
    except Exception:
        pass


def _fmt_bytes(n: int | float) -> str:
    n = float(n)
    if n >= 1_000_000_000_000:
        return f"{n / 1_000_000_000_000:.1f} TB".replace(".", ",")
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f} GB".replace(".", ",")
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f} MB"
    if n >= 1_000:
        return f"{n / 1_000:.0f} KB"
    return f"{n:.0f} B"


def _read_cpu_temp() -> float | None:
    """Liest die CPU-Temperatur (Raspberry Pi thermal_zone0 oder psutil)."""
    # 1. Raspberry Pi Standard-Pfad
    thermal_path = "/sys/class/thermal/thermal_zone0/temp"
    if os.path.exists(thermal_path):
        try:
            with open(thermal_path, "r", encoding="utf-8") as f:
                val = f.read().strip()
                if val.isdigit():
                    return round(float(val) / 1000.0, 1)
        except Exception:
            pass

    # 2. psutil sensors_temperatures
    if psutil and hasattr(psutil, "sensors_temperatures"):
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for name in ("cpu_thermal", "coretemp", "k10temp", "cpu-thermal"):
                    if name in temps and temps[name]:
                        return round(temps[name][0].current, 1)
                # Beliebige erste Temperatur
                for entries in temps.values():
                    if entries:
                        return round(entries[0].current, 1)
        except Exception:
            pass

    return None


def get_system_metrics() -> dict[str, Any]:
    """Liefert detaillierte Systemmetriken und freien/verbrauchten Speicher."""
    # 1. CPU
    cpu_percent = 0.0
    cpu_cores = 1
    if psutil:
        try:
            cpu_percent = round(psutil.cpu_percent(interval=None), 1)
            cpu_cores = psutil.cpu_count(logical=True) or 1
        except Exception:
            pass

    cpu_free = round(max(0.0, 100.0 - cpu_percent), 1)

    load_avg = None
    if hasattr(os, "getloadavg"):
        try:
            lavg = os.getloadavg()
            load_avg = f"{lavg[0]:.2f}, {lavg[1]:.2f}, {lavg[2]:.2f}"
        except Exception:
            pass

    # 2. RAM
    mem_total = 0
    mem_used = 0
    mem_available = 0
    mem_percent = 0.0
    if psutil:
        try:
            vm = psutil.virtual_memory()
            mem_total = vm.total
            mem_used = vm.used
            mem_available = vm.available
            mem_percent = round(vm.percent, 1)
        except Exception:
            pass

    mem_free_percent = round(max(0.0, 100.0 - mem_percent), 1)

    # 3. Festplatte / SD-Karte (Wurzelverzeichnis oder Datenpfad)
    disk_path = "/data" if os.path.exists("/data") else "."
    disk_total = 0
    disk_used = 0
    disk_free = 0
    disk_percent = 0.0
    try:
        du = shutil.disk_usage(disk_path)
        disk_total = du.total
        disk_used = du.used
        disk_free = du.free
        if disk_total > 0:
            disk_percent = round((disk_used / disk_total) * 100.0, 1)
    except Exception:
        pass

    disk_free_percent = round(max(0.0, 100.0 - disk_percent), 1)

    # 4. CPU-Temperatur
    cpu_temp = _read_cpu_temp()
    temp_status = "good"
    if cpu_temp:
        if cpu_temp >= 80.0:
            temp_status = "bad"
        elif cpu_temp >= 68.0:
            temp_status = "warn"

    # 5. Uptime
    uptime_str = ""
    if psutil:
        try:
            boot = psutil.boot_time()
            uptime_sec = int(time.time() - boot)
            days = uptime_sec // 86400
            hours = (uptime_sec % 86400) // 3600
            mins = (uptime_sec % 3600) // 60
            parts = []
            if days > 0:
                parts.append(f"{days} T{'ag' if days == 1 else 'age'}")
            if hours > 0 or days > 0:
                parts.append(f"{hours} Std.")
            parts.append(f"{mins} Min.")
            uptime_str = ", ".join(parts)
        except Exception:
            pass

    # 6. Cloudy-Eigenverbrauch (dieser Prozess)
    proc_ram_bytes = 0
    proc_cpu_percent = 0.0
    if _cloudy_proc:
        try:
            proc_ram_bytes = _cloudy_proc.memory_info().rss
            proc_cpu_percent = round(_cloudy_proc.cpu_percent(interval=None), 1)
        except Exception:
            pass

    return {
        "cpu": {
            "percent": cpu_percent,
            "free_percent": cpu_free,
            "cores": cpu_cores,
            "load_avg": load_avg,
        },
        "memory": {
            "total_bytes": mem_total,
            "used_bytes": mem_used,
            "free_bytes": mem_available,
            "total_fmt": _fmt_bytes(mem_total),
            "used_fmt": _fmt_bytes(mem_used),
            "free_fmt": _fmt_bytes(mem_available),
            "percent": mem_percent,
            "free_percent": mem_free_percent,
        },
        "disk": {
            "total_bytes": disk_total,
            "used_bytes": disk_used,
            "free_bytes": disk_free,
            "total_fmt": _fmt_bytes(disk_total),
            "used_fmt": _fmt_bytes(disk_used),
            "free_fmt": _fmt_bytes(disk_free),
            "percent": disk_percent,
            "free_percent": disk_free_percent,
        },
        "temperature": {
            "celsius": cpu_temp,
            "status": temp_status,
        },
        "uptime": uptime_str,
        "process": {
            "ram_bytes": proc_ram_bytes,
            "ram_fmt": _fmt_bytes(proc_ram_bytes),
            "cpu_percent": proc_cpu_percent,
        },
    }
