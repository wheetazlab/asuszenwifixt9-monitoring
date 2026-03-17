"""
Prometheus custom collector for ASUS ZenWiFi XT9 mesh network.

Connects to each mesh node via SSH, runs batched commands to minimise
round-trips, parses the output with the functions in parsers.py, and
yields prometheus_client Metric objects.
"""

import logging
import time
from collections.abc import Iterator

from prometheus_client.metrics_core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    Metric,
)

from . import parsers
from .config import (
    EXTENDER_TRACKED_INTERFACES,
    EXTENDER_WIFI_IFACES,
    ROUTER_BACKHAUL_MACS,
    ROUTER_TRACKED_INTERFACES,
    ROUTER_WIFI_IFACES,
    ROUTER_WIRED_PORTS,
)
from .ssh_client import RouterSSHClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSH batch commands
# ---------------------------------------------------------------------------

_SYSTEM_BATCH = """\
echo __loadavg__
cat /proc/loadavg
echo __uptime__
cat /proc/uptime
echo __meminfo__
cat /proc/meminfo
echo __cpustat__
grep '^cpu ' /proc/stat
echo __temp__
cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo 0
echo __net_dev__
cat /proc/net/dev
echo __dhcp_leases__
cat /var/lib/misc/dnsmasq.leases 2>/dev/null
"""

_WIRED_BATCH = """\
echo __brctl_showmacs__
brctl showmacs br0 2>/dev/null
echo __brif_ports__
for d in /sys/class/net/br0/brif/*; do echo "$(basename $d) $(cat $d/port_no 2>/dev/null)"; done
echo __arp__
cat /proc/net/arp
"""


def _build_wifi_batch(ifaces: list[tuple[str, str]]) -> str:
    """Build a single SSH command that dumps assoclist + status + chanim for every radio."""
    parts: list[str] = []
    for iface, _ in ifaces:
        parts += [
            f"echo __assoclist_{iface}__",
            f"wl -i {iface} assoclist 2>/dev/null",
            f"echo __status_{iface}__",
            f"wl -i {iface} status 2>/dev/null",
            f"echo __chanim_{iface}__",
            f"wl -i {iface} chanim_stats 2>/dev/null",
        ]
    return "\n".join(parts)


def _build_sta_batch(ifaces_and_macs: list[tuple[str, str, list[str]]]) -> str:
    """Build a single SSH command that dumps sta_info for all clients on all radios."""
    parts: list[str] = []
    for iface, _band, macs in ifaces_and_macs:
        for mac in macs:
            safe_key = mac.replace(":", "_")
            parts += [
                f"echo __sta_{iface}_{safe_key}__",
                f"wl -i {iface} sta_info {mac} 2>/dev/null",
            ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Node configuration type
# ---------------------------------------------------------------------------


class NodeConfig:
    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        username: str,
        password: str,
        is_router: bool = False,
        wifi_ifaces: list[tuple[str, str]] | None = None,
        tracked_interfaces: set[str] | None = None,
        backhaul_macs: set[str] | None = None,
        wired_ports: set[str] | None = None,
    ) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.is_router = is_router
        self.wifi_ifaces = wifi_ifaces or ROUTER_WIFI_IFACES
        self.tracked_interfaces = tracked_interfaces or ROUTER_TRACKED_INTERFACES
        self.backhaul_macs = backhaul_macs or set()
        self.wired_ports = wired_ports or ROUTER_WIRED_PORTS


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class RouterCollector:
    """
    prometheus_client custom collector.  Register once:

        REGISTRY.register(RouterCollector([...]))
    """

    def __init__(self, nodes: list[NodeConfig]) -> None:
        self._nodes = nodes
        self._ssh: dict[str, RouterSSHClient] = {
            n.name: RouterSSHClient(n.host, n.port, n.username, n.password)
            for n in nodes
        }
        # MAC → {hostname, ip} map, populated from the router's DHCP leases each scrape
        self._dhcp_map: dict[str, dict[str, str]] = {}

    def close(self) -> None:
        for client in self._ssh.values():
            client.close()

    # ------------------------------------------------------------------
    # prometheus_client interface
    # ------------------------------------------------------------------

    def collect(self) -> Iterator[Metric]:  # type: ignore[override]
        t0 = time.monotonic()
        metrics = _MetricBag()

        for node in self._nodes:
            try:
                self._collect_node(node, metrics)
            except Exception:
                logger.exception("Failed to collect metrics from node %s", node.name)

        elapsed = time.monotonic() - t0
        duration_g = GaugeMetricFamily(
            "asus_router_scrape_duration_seconds",
            "Total time taken to collect all router metrics (seconds)",
        )
        duration_g.add_metric([], elapsed)

        yield from metrics.iter_all()
        yield duration_g

    # ------------------------------------------------------------------
    # Per-node collection
    # ------------------------------------------------------------------

    def _collect_node(self, node: NodeConfig, m: "_MetricBag") -> None:
        ssh = self._ssh[node.name]

        # ── Batch 1: system + network + DHCP ────────────────────────────
        sys_raw = ssh.run(_SYSTEM_BATCH)
        sys_sec = parsers.split_sections(sys_raw)

        la = parsers.parse_loadavg(sys_sec.get("loadavg", "0 0 0"))
        m.load1.add_metric([node.name], la["load1"])
        m.load5.add_metric([node.name], la["load5"])
        m.load15.add_metric([node.name], la["load15"])

        uptime = parsers.parse_uptime(sys_sec.get("uptime", "0 0"))
        m.uptime.add_metric([node.name], uptime)

        mem = parsers.parse_meminfo(sys_sec.get("meminfo", ""))
        m.mem_total.add_metric([node.name], mem.get("MemTotal", 0))
        m.mem_free.add_metric([node.name], mem.get("MemFree", 0))
        m.mem_avail.add_metric([node.name], mem.get("MemAvailable", 0))
        m.mem_cached.add_metric([node.name], mem.get("Cached", 0))
        m.mem_buffers.add_metric([node.name], mem.get("Buffers", 0))

        temp = parsers.parse_temperature(sys_sec.get("temp", ""))
        if temp is not None:
            m.temperature.add_metric([node.name], temp)

        cpu = parsers.parse_cpu_stat(sys_sec.get("cpustat", ""))
        if cpu:
            for mode in ("user", "nice", "system", "idle", "iowait", "irq", "softirq"):
                m.cpu_seconds.add_metric([node.name, mode], cpu.get(mode, 0))

        if node.is_router:
            leases_text = sys_sec.get("dhcp_leases", "")
            leases = parsers.parse_dhcp_leases(leases_text)
            self._dhcp_map = {
                l["mac"]: {"hostname": l["hostname"], "ip": l["ip"]}
                for l in leases
            }
            m.dhcp_leases.add_metric([], float(len(leases)))

        net = parsers.parse_net_dev(sys_sec.get("net_dev", ""))
        for iface, stats in net.items():
            if iface not in node.tracked_interfaces:
                continue
            lbl = [node.name, iface]
            m.rx_bytes.add_metric(lbl, stats["rx_bytes"])
            m.tx_bytes.add_metric(lbl, stats["tx_bytes"])
            m.rx_packets.add_metric(lbl, stats["rx_packets"])
            m.tx_packets.add_metric(lbl, stats["tx_packets"])
            m.rx_errors.add_metric(lbl, stats["rx_errs"])
            m.tx_errors.add_metric(lbl, stats["tx_errs"])
            m.rx_drops.add_metric(lbl, stats["rx_drop"])
            m.tx_drops.add_metric(lbl, stats["tx_drop"])

        # ── Batch 2: WiFi radio summaries (assoclist + status + chanim) ──
        wifi_raw = ssh.run(_build_wifi_batch(node.wifi_ifaces))
        wifi_sec = parsers.split_sections(wifi_raw)

        # Build a list of (iface, band, client_macs) for the sta_info batch
        iface_clients: list[tuple[str, str, list[str]]] = []

        for iface, band in node.wifi_ifaces:
            radio_lbl = [node.name, iface, band]

            assoc_text = wifi_sec.get(f"assoclist_{iface}", "")
            all_macs = parsers.parse_assoclist(assoc_text)

            # Separate backhaul MACs from regular client MACs
            client_macs = [mac for mac in all_macs if mac not in node.backhaul_macs]
            backhaul_macs = [mac for mac in all_macs if mac in node.backhaul_macs]

            m.wifi_clients.add_metric(radio_lbl, float(len(client_macs)))
            m.wifi_associated.add_metric(radio_lbl, float(len(all_macs)))

            status = parsers.parse_wifi_status(wifi_sec.get(f"status_{iface}", ""))
            if "noise_dbm" in status:
                m.wifi_noise.add_metric(radio_lbl, status["noise_dbm"])
            if "channel_util_pct" in status:
                m.wifi_chan_util.add_metric(
                    [node.name, iface, band, "qbss"], status["channel_util_pct"]
                )

            # Backhaul link quality (extender nodes: these are the "assoclist" entries
            # for the router's backhaul MACs seen on the extender's radio)
            if not node.is_router and "rssi_dbm" in status and status["rssi_dbm"] < 0:
                m.backhaul_rssi.add_metric(radio_lbl, status["rssi_dbm"])
            if not node.is_router and "snr_db" in status:
                m.backhaul_snr.add_metric(radio_lbl, status["snr_db"])

            chanim = parsers.parse_chanim_stats(wifi_sec.get(f"chanim_{iface}", ""))
            if chanim:
                for util_type in ("tx", "inbss", "obss", "nocat", "nopkt", "idle", "busy"):
                    key = f"{util_type}_pct"
                    if key in chanim:
                        m.wifi_chan_util.add_metric(
                            [node.name, iface, band, util_type], chanim[key]
                        )
                if "knoise_dbm" in chanim:
                    m.wifi_noise.add_metric(radio_lbl, chanim["knoise_dbm"])
                if "goodtx" in chanim:
                    m.wifi_goodtx.add_metric(radio_lbl, chanim["goodtx"])
                if "badtx" in chanim:
                    m.wifi_badtx.add_metric(radio_lbl, chanim["badtx"])
                if "glitch" in chanim:
                    m.wifi_glitch.add_metric(radio_lbl, chanim["glitch"])

            iface_clients.append((iface, band, client_macs))

        # ── Batch 3: per-client sta_info (all radios, all clients in one exec) ──
        all_client_macs_exist = any(macs for _, _, macs in iface_clients)
        if all_client_macs_exist:
            sta_raw = ssh.run(_build_sta_batch(iface_clients))
            sta_sec = parsers.split_sections(sta_raw)

            for iface, band, client_macs in iface_clients:
                for mac in client_macs:
                    safe_key = mac.replace(":", "_")
                    sta = parsers.parse_sta_info(sta_sec.get(f"sta_{iface}_{safe_key}", ""))
                    if not sta:
                        continue
                    dhcp = self._dhcp_map.get(mac, {})
                    hostname = dhcp.get("hostname", "")
                    ip = dhcp.get("ip", "")
                    cl = [node.name, iface, band, mac, hostname, ip]

                    if "rssi_dbm" in sta:
                        m.client_rssi.add_metric(cl, sta["rssi_dbm"])
                    if "tx_bytes" in sta:
                        m.client_tx_bytes.add_metric(cl, sta["tx_bytes"])
                    if "rx_bytes" in sta:
                        m.client_rx_bytes.add_metric(cl, sta["rx_bytes"])
                    if "tx_rate_kbps" in sta:
                        m.client_tx_rate.add_metric(cl, sta["tx_rate_kbps"])
                    if "rx_rate_kbps" in sta:
                        m.client_rx_rate.add_metric(cl, sta["rx_rate_kbps"])
                    if "tx_failures" in sta:
                        m.client_tx_failures.add_metric(cl, sta["tx_failures"])
                    if "tx_retries" in sta:
                        m.client_tx_retries.add_metric(cl, sta["tx_retries"])
                    if "idle_seconds" in sta:
                        m.client_idle.add_metric(cl, sta["idle_seconds"])

        # ── Batch 4: wired clients (bridge FDB + ARP) ────────────────────
        wired_raw = ssh.run(_WIRED_BATCH)
        wired_sec = parsers.split_sections(wired_raw)

        port_to_iface: dict[int, str] = {
            v: k for k, v in parsers.parse_brif_ports(wired_sec.get("brif_ports", "")).items()
        }
        arp_map = parsers.parse_arp(wired_sec.get("arp", ""))

        seen_wired: set[str] = set()
        for entry in parsers.parse_brctl_showmacs(wired_sec.get("brctl_showmacs", "")):
            if entry["is_local"]:
                continue
            mac = entry["mac"]
            if mac in seen_wired:
                continue
            iface = port_to_iface.get(entry["port_no"])
            if iface not in node.wired_ports:
                continue
            seen_wired.add(mac)
            dhcp = self._dhcp_map.get(mac, {})
            hostname = dhcp.get("hostname", "")
            ip = dhcp.get("ip", "") or arp_map.get(mac, "")
            m.wired_client_info.add_metric([node.name, iface, mac, hostname, ip], 1.0)


# ---------------------------------------------------------------------------
# Metric bag — declare all metric families in one place
# ---------------------------------------------------------------------------

_NODE = ["node"]
_NODE_IFACE = ["node", "interface"]
_NODE_RADIO_BAND = ["node", "radio", "band"]
_NODE_RADIO_BAND_TYPE = ["node", "radio", "band", "type"]
_CLIENT = ["node", "radio", "band", "mac", "hostname", "ip"]
_WIRED_CLIENT = ["node", "interface", "mac", "hostname", "ip"]


class _MetricBag:
    """Container for all GaugeMetricFamily / CounterMetricFamily instances."""

    def __init__(self) -> None:
        # -- system --
        self.uptime = GaugeMetricFamily(
            "asus_router_uptime_seconds", "Router uptime in seconds", labels=_NODE
        )
        self.load1 = GaugeMetricFamily(
            "asus_router_load_1m", "1-minute load average", labels=_NODE
        )
        self.load5 = GaugeMetricFamily(
            "asus_router_load_5m", "5-minute load average", labels=_NODE
        )
        self.load15 = GaugeMetricFamily(
            "asus_router_load_15m", "15-minute load average", labels=_NODE
        )
        self.mem_total = GaugeMetricFamily(
            "asus_router_memory_total_bytes", "Total RAM (bytes)", labels=_NODE
        )
        self.mem_free = GaugeMetricFamily(
            "asus_router_memory_free_bytes", "Free RAM (bytes)", labels=_NODE
        )
        self.mem_avail = GaugeMetricFamily(
            "asus_router_memory_available_bytes", "Available RAM (bytes)", labels=_NODE
        )
        self.mem_cached = GaugeMetricFamily(
            "asus_router_memory_cached_bytes", "Page cache (bytes)", labels=_NODE
        )
        self.mem_buffers = GaugeMetricFamily(
            "asus_router_memory_buffers_bytes", "Buffer cache (bytes)", labels=_NODE
        )
        self.temperature = GaugeMetricFamily(
            "asus_router_temperature_celsius", "Board temperature (°C)", labels=_NODE
        )
        self.cpu_seconds = CounterMetricFamily(
            "asus_router_cpu_seconds",
            "CPU time in jiffies by mode",
            labels=["node", "mode"],
        )

        # -- DHCP --
        self.dhcp_leases = GaugeMetricFamily(
            "asus_router_dhcp_leases_total", "Number of active DHCP leases"
        )

        # -- network interfaces --
        self.rx_bytes = CounterMetricFamily(
            "asus_router_interface_rx_bytes",
            "Bytes received since boot",
            labels=_NODE_IFACE,
        )
        self.tx_bytes = CounterMetricFamily(
            "asus_router_interface_tx_bytes",
            "Bytes transmitted since boot",
            labels=_NODE_IFACE,
        )
        self.rx_packets = CounterMetricFamily(
            "asus_router_interface_rx_packets",
            "Packets received since boot",
            labels=_NODE_IFACE,
        )
        self.tx_packets = CounterMetricFamily(
            "asus_router_interface_tx_packets",
            "Packets transmitted since boot",
            labels=_NODE_IFACE,
        )
        self.rx_errors = CounterMetricFamily(
            "asus_router_interface_rx_errors",
            "Receive errors since boot",
            labels=_NODE_IFACE,
        )
        self.tx_errors = CounterMetricFamily(
            "asus_router_interface_tx_errors",
            "Transmit errors since boot",
            labels=_NODE_IFACE,
        )
        self.rx_drops = CounterMetricFamily(
            "asus_router_interface_rx_drops",
            "Received packets dropped since boot",
            labels=_NODE_IFACE,
        )
        self.tx_drops = CounterMetricFamily(
            "asus_router_interface_tx_drops",
            "Transmitted packets dropped since boot",
            labels=_NODE_IFACE,
        )

        # -- WiFi radio --
        self.wifi_clients = GaugeMetricFamily(
            "asus_router_wifi_clients",
            "Number of associated WiFi clients (excluding backhaul)",
            labels=_NODE_RADIO_BAND,
        )
        self.wifi_associated = GaugeMetricFamily(
            "asus_router_wifi_associated_total",
            "Total associated stations including backhaul",
            labels=_NODE_RADIO_BAND,
        )
        self.wifi_chan_util = GaugeMetricFamily(
            "asus_router_wifi_channel_utilization_percent",
            "Channel utilization percentage by type (tx/inbss/obss/idle/busy/qbss/…)",
            labels=_NODE_RADIO_BAND_TYPE,
        )
        self.wifi_noise = GaugeMetricFamily(
            "asus_router_wifi_noise_dbm",
            "Radio noise floor (dBm)",
            labels=_NODE_RADIO_BAND,
        )
        self.wifi_goodtx = CounterMetricFamily(
            "asus_router_wifi_channel_goodtx",
            "Good TX frame counter from chanim_stats",
            labels=_NODE_RADIO_BAND,
        )
        self.wifi_badtx = CounterMetricFamily(
            "asus_router_wifi_channel_badtx",
            "Bad TX frame counter from chanim_stats",
            labels=_NODE_RADIO_BAND,
        )
        self.wifi_glitch = CounterMetricFamily(
            "asus_router_wifi_channel_glitch",
            "PHY glitch counter from chanim_stats",
            labels=_NODE_RADIO_BAND,
        )

        # -- backhaul --
        self.backhaul_rssi = GaugeMetricFamily(
            "asus_router_backhaul_rssi_dbm",
            "Backhaul link RSSI to parent router (dBm)",
            labels=_NODE_RADIO_BAND,
        )
        self.backhaul_snr = GaugeMetricFamily(
            "asus_router_backhaul_snr_db",
            "Backhaul link SNR to parent router (dB)",
            labels=_NODE_RADIO_BAND,
        )

        # -- per-client WiFi --
        self.client_rssi = GaugeMetricFamily(
            "asus_router_wifi_client_rssi_dbm",
            "Client RSSI averaged across active antennas (dBm)",
            labels=_CLIENT,
        )
        self.client_tx_bytes = CounterMetricFamily(
            "asus_router_wifi_client_tx_bytes",
            "Total bytes sent to this client since radio init",
            labels=_CLIENT,
        )
        self.client_rx_bytes = CounterMetricFamily(
            "asus_router_wifi_client_rx_bytes",
            "Total bytes received from this client since radio init",
            labels=_CLIENT,
        )
        self.client_tx_rate = GaugeMetricFamily(
            "asus_router_wifi_client_tx_rate_kbps",
            "PHY rate of last TX packet to client (kbps)",
            labels=_CLIENT,
        )
        self.client_rx_rate = GaugeMetricFamily(
            "asus_router_wifi_client_rx_rate_kbps",
            "PHY rate of last RX packet from client (kbps)",
            labels=_CLIENT,
        )
        self.client_tx_failures = CounterMetricFamily(
            "asus_router_wifi_client_tx_failures",
            "TX failure counter for this client",
            labels=_CLIENT,
        )
        self.client_tx_retries = CounterMetricFamily(
            "asus_router_wifi_client_tx_retries",
            "TX retry counter for this client",
            labels=_CLIENT,
        )
        self.client_idle = GaugeMetricFamily(
            "asus_router_wifi_client_idle_seconds",
            "Seconds since the last packet from this client",
            labels=_CLIENT,
        )

        # -- wired clients --
        self.wired_client_info = GaugeMetricFamily(
            "asus_router_wired_client_info",
            "Wired client connected to the router/extender (always 1)",
            labels=_WIRED_CLIENT,
        )

    def iter_all(self) -> Iterator[Metric]:
        yield self.uptime
        yield self.load1
        yield self.load5
        yield self.load15
        yield self.mem_total
        yield self.mem_free
        yield self.mem_avail
        yield self.mem_cached
        yield self.mem_buffers
        yield self.temperature
        yield self.cpu_seconds
        yield self.dhcp_leases
        yield self.rx_bytes
        yield self.tx_bytes
        yield self.rx_packets
        yield self.tx_packets
        yield self.rx_errors
        yield self.tx_errors
        yield self.rx_drops
        yield self.tx_drops
        yield self.wifi_clients
        yield self.wifi_associated
        yield self.wifi_chan_util
        yield self.wifi_noise
        yield self.wifi_goodtx
        yield self.wifi_badtx
        yield self.wifi_glitch
        yield self.backhaul_rssi
        yield self.backhaul_snr
        yield self.client_rssi
        yield self.client_tx_bytes
        yield self.client_rx_bytes
        yield self.client_tx_rate
        yield self.client_rx_rate
        yield self.client_tx_failures
        yield self.client_tx_retries
        yield self.client_idle
        yield self.wired_client_info
