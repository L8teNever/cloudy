import ipaddress
import os
from dataclasses import dataclass


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "ja", "an", "on")


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

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=os.environ.get("DATA_DIR", "/data"),
            lan_interface=os.environ.get("LAN_INTERFACE", "eth0"),
            pi_ip=os.environ.get("PI_IP", "192.168.178.2"),
            router_ip=os.environ.get("ROUTER_IP", "192.168.178.1"),
            netmask=os.environ.get("NETMASK", "255.255.255.0"),
            upstream_dns=[
                s.strip() for s in os.environ.get("UPSTREAM_DNS", "172.64.36.1").split(",") if s.strip()
            ],
            dnsmasq_enabled=_flag("DNSMASQ_ENABLED", "1"),
            dhcp_enabled=_flag("DHCP_ENABLED", "0"),
            dhcp_start=os.environ.get("DHCP_START", "192.168.178.20"),
            dhcp_end=os.environ.get("DHCP_END", "192.168.178.200"),
            dhcp_lease=os.environ.get("DHCP_LEASE", "30d"),
            gateway_enabled=_flag("GATEWAY_ENABLED", "0"),
            traffic_poll_seconds=float(os.environ.get("TRAFFIC_POLL_SECONDS", "10")),
            local_name=os.environ.get("LOCAL_NAME", "cloudy.lan").strip().lower().rstrip("."),
            https_port=int(os.environ.get("HTTPS_PORT", "443").strip() or 0),
            # WARP-/VPN-Container aus docker-compose.yml (warpN = Cloudflare WARP, vpnN = WireGuard): Name=IP im Netz "cloudy-warp".
            warp_slots=[
                (name.strip(), ip.strip())
                for name, _, ip in (
                    s.partition("=")
                    for s in os.environ.get("WARP_SLOTS", "warp1=172.29.86.11,warp2=172.29.86.12,vpn1=172.29.86.21,vpn2=172.29.86.22").split(",")
                )
                if name.strip() and ip.strip()
            ],
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
