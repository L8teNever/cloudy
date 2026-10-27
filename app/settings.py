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
            dhcp_lease=os.environ.get("DHCP_LEASE", "1h"),
            gateway_enabled=_flag("GATEWAY_ENABLED", "0"),
            traffic_poll_seconds=float(os.environ.get("TRAFFIC_POLL_SECONDS", "10")),
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
    def conf_path(self) -> str:
        return os.path.join(self.data_dir, "dnsmasq.conf")
