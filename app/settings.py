import ipaddress
import os
import socket
from dataclasses import dataclass

PI_IP_FILE = "pi_ip"  # im Dashboard gesetzte Pi-Adresse (hat Vorrang vor PI_IP aus .env)


ENV_FILE = os.environ.get("CLOUDY_ENV_FILE", "/cloudy.env")  # ~/cloudy/.env, schreibgeschützt eingebunden


def apply_env_file(path: str = ENV_FILE) -> list[str]:
    """Deine Werte aus .env gewinnen: bei doppelten Einträgen gilt die ERSTE Zeile.

    Ältere „cloudy update“-Versionen haben .env.example bei jedem Update unten an .env angehängt; Docker nimmt
    bei env_file die letzte Zeile -> Standardwerte (GATEWAY_ENABLED=0, PI_IP=…2) hätten deine Einstellungen
    überschrieben. Rückgabe: Namen, die korrigiert wurden.
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    first: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if not key or key in first:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        first[key] = value
    fixed = []
    for key, value in first.items():
        if os.environ.get(key) != value:
            os.environ[key] = value
            fixed.append(key)
    return fixed


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "ja", "an", "on")


def _warp_slots_from_data(data_dir: str) -> list[tuple[str, str]]:
    from . import slots

    return slots.as_pairs(slots.ensure(data_dir))


def _valid_ipv4(ip: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(ip), ipaddress.IPv4Address)
    except ValueError:
        return False


def read_pi_ip_override(data_dir: str) -> str:
    try:
        with open(os.path.join(data_dir, PI_IP_FILE), encoding="utf-8") as f:
            ip = f.read().strip()
    except OSError:
        return ""
    return ip if _valid_ipv4(ip) else ""


def write_pi_ip_override(data_dir: str, ip: str) -> None:
    """Leer = Override löschen (dann wieder .env bzw. automatische Erkennung)."""
    path = os.path.join(data_dir, PI_IP_FILE)
    ip = (ip or "").strip()
    if not ip:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    if not _valid_ipv4(ip):
        raise ValueError("Keine gültige IPv4-Adresse")
    os.makedirs(data_dir, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(ip + "\n")
    os.replace(path + ".tmp", path)


def is_local_ip(ip: str) -> bool:
    """Gehört die Adresse wirklich zu diesem Rechner? (cloudy läuft im Host-Netz)"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((ip, 0))
        return True
    except OSError:
        return False


def detect_lan_ip(router_ip: str) -> str:
    """Eigene Adresse, mit der der Pi die Fritzbox erreicht (es wird nichts gesendet)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((router_ip, 9))
            ip = sock.getsockname()[0]
    except OSError:
        return ""
    return ip if _valid_ipv4(ip) and not ip.startswith("127.") else ""


def _same_net(a: str, b: str, netmask: str) -> bool:
    try:
        return ipaddress.ip_address(a) in ipaddress.ip_network(f"{b}/{netmask}", strict=False)
    except ValueError:
        return False


def resolve_pi_ip(
    data_dir: str, env_ip: str, router_ip: str, check: bool, netmask: str = "255.255.255.0"
) -> tuple[str, str, str]:
    """(Adresse, Quelle, erkannte Adresse). Quelle: dashboard | env | erkannt.

    Eine falsche Pi-Adresse ist fatal: DHCP verteilt sie als Gateway und DNS, dann geht der Verkehr am Pi
    (und an WARP/VPN) vorbei. Darum: Passt die eingestellte Adresse nicht zum Pi, nehmen wir die erkannte.
    """
    override = read_pi_ip_override(data_dir)
    detected = detect_lan_ip(router_ip) if check else ""
    if detected and not _same_net(detected, router_ip, netmask):
        detected = ""
    if override:
        return override, "dashboard", detected
    if not check or is_local_ip(env_ip) or not detected:
        return env_ip, "env", detected
    return detected, "erkannt", detected


@dataclass(frozen=True)
class Settings:
    data_dir: str
    lan_interface: str
    pi_ip: str
    router_ip: str
    netmask: str
    upstream_dns: list[str]
    dnsmasq_enabled: bool
    dhcp_enabled: bool
    dhcp_start: str
    dhcp_end: str
    dhcp_lease: str
    gateway_enabled: bool
    traffic_poll_seconds: float
    local_name: str
    https_port: int
    warp_slots: list[tuple[str, str]]
    warp_killswitch: bool
    ipv6_enabled: bool = False
    ipv6_prefix: str = ""
    pi_ip_source: str = "env"  # dashboard | env | erkannt
    pi_ip_env: str = ""
    pi_ip_detected: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        fixed = apply_env_file()
        if fixed:
            print(f"cloudy: .env enthielt doppelte Einträge – deine (erste) Zeile gilt: {', '.join(sorted(fixed))}", flush=True)
        data_dir = os.environ.get("DATA_DIR", "/data")
        env_ip = os.environ.get("PI_IP", "192.168.178.2").strip()
        router_ip = os.environ.get("ROUTER_IP", "192.168.178.1").strip()
        dnsmasq_enabled = _flag("DNSMASQ_ENABLED", "1")
        # Prüfen nur im echten Betrieb (Host-Netz); Tests und Vorschau laufen mit DNSMASQ_ENABLED=0.
        netmask = os.environ.get("NETMASK", "255.255.255.0").strip()
        pi_ip, source, detected = resolve_pi_ip(data_dir, env_ip, router_ip, dnsmasq_enabled, netmask)
        return cls(
            data_dir=data_dir,
            lan_interface=os.environ.get("LAN_INTERFACE", "eth0"),
            pi_ip=pi_ip,
            pi_ip_source=source,
            pi_ip_env=env_ip,
            pi_ip_detected=detected,
            router_ip=router_ip,
            netmask=netmask,
            upstream_dns=[
                s.strip() for s in os.environ.get("UPSTREAM_DNS", "172.64.36.1").split(",") if s.strip()
            ],
            dnsmasq_enabled=dnsmasq_enabled,
            dhcp_enabled=_flag("DHCP_ENABLED", "0"),
            dhcp_start=os.environ.get("DHCP_START", "192.168.178.20"),
            dhcp_end=os.environ.get("DHCP_END", "192.168.178.200"),
            dhcp_lease=os.environ.get("DHCP_LEASE", "30d"),
            gateway_enabled=_flag("GATEWAY_ENABLED", "0"),
            traffic_poll_seconds=float(os.environ.get("TRAFFIC_POLL_SECONDS", "10")),
            local_name=os.environ.get("LOCAL_NAME", "cloudy.lan").strip().lower().rstrip("."),
            https_port=int(os.environ.get("HTTPS_PORT", "443").strip() or 0),
            # WARP-/VPN-Profile: data/warp_slots.json (Dashboard kann anlegen/löschen), sonst WARP_SLOTS-Default.
            warp_slots=_warp_slots_from_data(os.environ.get("DATA_DIR", "/data")),
            warp_killswitch=_flag("WARP_KILLSWITCH", "0"),
            ipv6_enabled=_flag("IPV6_ENABLED", "0"),
            ipv6_prefix=os.environ.get("IPV6_PREFIX", "").strip(),
        )

    @property
    def lan_network(self) -> ipaddress.IPv4Network:
        return ipaddress.ip_network(f"{self.pi_ip}/{self.netmask}", strict=False)

    @property
    def leases_path(self) -> str:
        return os.path.join(self.data_dir, "dhcp.leases")

    @property
    def hosts_path(self) -> str:
        return os.path.join(self.data_dir, "dhcp-hosts.conf")

    @property
    def local_names_path(self) -> str:
        return os.path.join(self.data_dir, "lokale-namen.hosts")

    @property
    def blocklist_path(self) -> str:
        return os.path.join(self.data_dir, "blocklist.hosts")

    @property
    def redirects_path(self) -> str:
        return os.path.join(self.data_dir, "redirects.conf")

    @property
    def dashboard_url(self) -> str:
        host = self.local_name or self.pi_ip
        port = os.environ.get("DASHBOARD_PORT", "8080")
        return f"http://{host}" + ("" if port == "80" else f":{port}")

    @property
    def warp_dir(self) -> str:
        return os.path.join(self.data_dir, "warp")

    @property
    def conf_path(self) -> str:
        return os.path.join(self.data_dir, "dnsmasq.conf")
