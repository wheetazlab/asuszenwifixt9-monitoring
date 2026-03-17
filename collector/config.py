import os

ROUTER_SSH_HOST = os.environ.get("ROUTER_SSH_HOST", "")
ROUTER_SSH_PORT = int(os.environ.get("ROUTER_SSH_PORT", "2222"))

EXTENDER_SSH_HOST = os.environ.get("EXTENDER_SSH_HOST", "")
EXTENDER_SSH_PORT = int(os.environ.get("EXTENDER_SSH_PORT", "2222"))

SSH_USERNAME = os.environ.get("SSH_USERNAME", "router")
SSH_PASSWORD = os.environ.get("SSH_PASSWORD", "")

METRICS_PORT = int(os.environ.get("METRICS_PORT", "9100"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# WiFi radio interface → human-readable band label.
# Router: eth4=2.4GHz, eth5=5GHz-160MHz, eth6=5GHz-2-160MHz(backhaul-DWB)
# Extender: same physical layout.
ROUTER_WIFI_IFACES: list[tuple[str, str]] = [
    ("eth4", "2.4GHz"),
    ("eth5", "5GHz"),
    ("eth6", "5GHz-2"),
]

EXTENDER_WIFI_IFACES: list[tuple[str, str]] = [
    ("eth4", "2.4GHz"),
    ("eth5", "5GHz"),
    ("eth6", "5GHz-2"),
]

# Network interfaces to export traffic counters for (per node type)
ROUTER_TRACKED_INTERFACES: set[str] = {
    "eth0",        # WAN
    "br0",         # LAN bridge
    "eth4",        # 2.4GHz radio
    "eth5",        # 5GHz radio
    "eth6",        # 5GHz-2 / dedicated backhaul radio
    "wds0.0.1",   # WDS backhaul link (2.4GHz side)
    "wds2.0.1",   # WDS backhaul link (5GHz-2 side)
}

EXTENDER_TRACKED_INTERFACES: set[str] = {
    "br0",         # LAN bridge
    "eth1",        # Wired LAN port
    "eth2",        # Wired LAN port
    "eth4",        # 2.4GHz backhaul + clients
    "eth6",        # 5GHz-2 dedicated backhaul
    "wl0.1",       # 2.4GHz client-facing virtual BSS
    "wl1.1",       # 5GHz client-facing virtual BSS
    "wl2.1",       # 5GHz-2 client-facing virtual BSS
}

# Router backhaul MAC addresses — these appear in the extender's assoclist on
# the backhaul radios. We still collect metrics for them (backhaul link quality)
# but label them as the router node rather than a wifi client.
ROUTER_BACKHAUL_MACS: set[str] = {
    "E8:9C:25:AB:FC:50",  # router eth4 (2.4GHz)
    "E8:9C:25:AB:FC:54",  # router eth5 (5GHz)
    "E8:9C:25:AB:FC:58",  # router eth6 (5GHz-2 backhaul)
}
