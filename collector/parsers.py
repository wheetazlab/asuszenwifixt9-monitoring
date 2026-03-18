"""
Parsers for ASUS ZenWiFi XT9 SSH command output.

All parse_* functions accept raw command output as a string and return
plain Python dicts / lists. No side effects.
"""

import re
from typing import Any


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


def parse_loadavg(text: str) -> dict[str, float]:
    """Parse /proc/loadavg → {load1, load5, load15}"""
    parts = text.strip().split()
    return {
        "load1": float(parts[0]),
        "load5": float(parts[1]),
        "load15": float(parts[2]),
    }


def parse_uptime(text: str) -> float:
    """Parse /proc/uptime → uptime in seconds (first field)."""
    return float(text.strip().split()[0])


def parse_meminfo(text: str) -> dict[str, float]:
    """Parse /proc/meminfo → values in bytes (source is kB)."""
    result: dict[str, float] = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)", line)
        if m:
            result[m.group(1)] = float(m.group(2)) * 1024
    return result


def parse_temperature(text: str) -> float | None:
    """
    Parse /sys/class/thermal/thermal_zone0/temp.
    The value is in millidegrees Celsius (e.g. 68493 → 68.493 °C).
    Returns None if the value cannot be parsed.
    """
    try:
        raw = text.strip().splitlines()[0]
        return float(raw) / 1000.0
    except (ValueError, IndexError):
        return None


def parse_cpu_stat(text: str) -> dict[str, float]:
    """
    Parse the aggregate 'cpu' line from /proc/stat.
    Returns raw jiffie counters: user nice system idle iowait irq softirq total.
    """
    for line in text.splitlines():
        if not line.startswith("cpu "):
            continue
        fields = line.split()
        user = float(fields[1])
        nice = float(fields[2])
        system = float(fields[3])
        idle = float(fields[4])
        iowait = float(fields[5]) if len(fields) > 5 else 0.0
        irq = float(fields[6]) if len(fields) > 6 else 0.0
        softirq = float(fields[7]) if len(fields) > 7 else 0.0
        total = user + nice + system + idle + iowait + irq + softirq
        return {
            "user": user,
            "nice": nice,
            "system": system,
            "idle": idle,
            "iowait": iowait,
            "irq": irq,
            "softirq": softirq,
            "total": total,
        }
    return {}


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


def parse_net_dev(text: str) -> dict[str, dict[str, float]]:
    """
    Parse /proc/net/dev.

    Returns:
        {iface: {rx_bytes, rx_packets, rx_errs, rx_drop,
                 tx_bytes, tx_packets, tx_errs, tx_drop}}
    """
    result: dict[str, dict[str, float]] = {}
    for line in text.splitlines()[2:]:  # first two lines are headers
        line = line.strip()
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        iface = iface.strip()
        vals = rest.split()
        if len(vals) < 16:
            continue
        result[iface] = {
            "rx_bytes": float(vals[0]),
            "rx_packets": float(vals[1]),
            "rx_errs": float(vals[2]),
            "rx_drop": float(vals[3]),
            "tx_bytes": float(vals[8]),
            "tx_packets": float(vals[9]),
            "tx_errs": float(vals[10]),
            "tx_drop": float(vals[11]),
        }
    return result


# ---------------------------------------------------------------------------
# WiFi — assoclist
# ---------------------------------------------------------------------------


def parse_assoclist(text: str) -> list[str]:
    """
    Parse `wl -i ethX assoclist` output.
    Returns a list of MAC addresses in upper-case (e.g. "AA:BB:CC:DD:EE:FF").
    """
    macs: list[str] = []
    for line in text.splitlines():
        m = re.match(r"assoclist\s+([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", line.strip())
        if m:
            macs.append(m.group(1).upper())
    return macs


# ---------------------------------------------------------------------------
# WiFi — sta_info
# ---------------------------------------------------------------------------


def parse_sta_info(text: str) -> dict[str, Any]:
    """
    Parse `wl -i ethX sta_info <MAC>` output.

    Extracted fields (all optional — only present if found in the output):
        idle_seconds      int
        tx_bytes          float  (tx total bytes)
        rx_bytes          float  (rx data bytes)
        tx_rate_kbps      float  (rate of last tx pkt, PHY rate)
        rx_rate_kbps      float  (rate of last rx pkt)
        tx_failures       float
        tx_retries        float
        rssi_dbm          float  (avg across active antennas, <0)
    """
    info: dict[str, Any] = {}

    for line in text.splitlines():
        line = line.strip()

        m = re.search(r"idle\s+(\d+)\s+second", line)
        if m:
            info["idle_seconds"] = float(m.group(1))

        m = re.match(r"tx total bytes:\s+(\d+)", line)
        if m:
            info["tx_bytes"] = float(m.group(1))

        m = re.match(r"rx data bytes:\s+(\d+)", line)
        if m:
            info["rx_bytes"] = float(m.group(1))

        m = re.match(r"tx failures:\s+(\d+)", line)
        if m:
            info["tx_failures"] = float(m.group(1))

        m = re.match(r"tx pkts retries:\s+(\d+)", line)
        if m:
            info["tx_retries"] = float(m.group(1))

        # "rate of last tx pkt: 65000 kbps - 19500 kbps"
        # First number is PHY rate; second is data rate after encoding overhead.
        m = re.match(r"rate of last tx pkt:\s+(\d+)\s+kbps", line)
        if m:
            info["tx_rate_kbps"] = float(m.group(1))

        m = re.match(r"rate of last rx pkt:\s+(\d+)\s+kbps", line)
        if m:
            info["rx_rate_kbps"] = float(m.group(1))

        # "per antenna average rssi of rx data frames: -34 -34 0 0"
        # Zero values mean that antenna is unused / not present — skip them.
        if "per antenna average rssi" in line and ":" in line:
            raw_values = line.split(":", 1)[1].split()
            active = [int(v) for v in raw_values if int(v) < 0]
            if active:
                info["rssi_dbm"] = float(sum(active) / len(active))

    return info


# ---------------------------------------------------------------------------
# WiFi — status
# ---------------------------------------------------------------------------


def parse_wifi_status(text: str) -> dict[str, Any]:
    """
    Parse `wl -i ethX status` output.

    Extracted fields:
        ssid              str
        noise_dbm         float
        rssi_dbm          float   (link RSSI; non-zero only on connected clients /
                                   backhaul links — the AP's own radio shows 0)
        snr_db            float
        channel_util_pct  float   (QBSS, 0-100)
        chanspec          str     (e.g. "5GHz channel 50 160MHz")
        primary_channel   int
    """
    info: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()

        m = re.search(r'SSID:\s+"([^"]*)"', line)
        if m:
            info["ssid"] = m.group(1)

        m = re.search(r"noise:\s+([-\d]+)\s+dBm", line)
        if m:
            info["noise_dbm"] = float(m.group(1))

        m = re.search(r"RSSI:\s+([-\d]+)\s+dBm", line)
        if m:
            info["rssi_dbm"] = float(m.group(1))

        m = re.search(r"SNR:\s+([\d]+)\s+dB", line)
        if m:
            info["snr_db"] = float(m.group(1))

        # "QBSS Channel Utilization: 0x38 (21 %)"
        m = re.search(r"QBSS Channel Utilization:\s+\S+\s+\((\d+)\s+%\)", line)
        if m:
            info["channel_util_pct"] = float(m.group(1))

        # "Chanspec: 5GHz channel 50 160MHz (0xee32)"
        m = re.search(r"Chanspec:\s+([\w.]+\s+channel\s+[\w/]+\s+[\w]+)", line)
        if m:
            info["chanspec"] = m.group(1).strip()

        m = re.search(r"Primary channel:\s+(\d+)", line)
        if m:
            info["primary_channel"] = int(m.group(1))

    return info


# ---------------------------------------------------------------------------
# WiFi — chanim_stats
# ---------------------------------------------------------------------------


def parse_chanim_stats(text: str) -> dict[str, Any] | None:
    """
    Parse `wl -i ethX chanim_stats` output.

    Data line columns (version 4):
        chanspec tx inbss obss nocat nopkt doze txop goodtx badtx
        glitch badplcp knoise idle busy timestamp

    All percentage fields are 0-100 (already scaled by the driver).

    Returns None if no data line is found.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("version") or line.startswith("chanspec"):
            continue
        parts = line.split()
        if len(parts) < 15:
            continue
        try:
            return {
                "chanspec": parts[0],
                "tx_pct": float(parts[1]),
                "inbss_pct": float(parts[2]),
                "obss_pct": float(parts[3]),
                "nocat_pct": float(parts[4]),
                "nopkt_pct": float(parts[5]),
                "txop_pct": float(parts[7]),
                "goodtx": float(parts[8]),
                "badtx": float(parts[9]),
                "glitch": float(parts[10]),
                "knoise_dbm": float(parts[12]),
                "idle_pct": float(parts[13]),
                "busy_pct": float(parts[14]),
            }
        except (ValueError, IndexError):
            continue
    return None


# ---------------------------------------------------------------------------
# DHCP leases
# ---------------------------------------------------------------------------


def parse_dhcp_leases(text: str) -> list[dict[str, str]]:
    """
    Parse /tmp/dnsmasq.leases.

    Format per line:
        <expire_epoch> <mac> <ip> <hostname> <client_id>

    hostname "*" means no name was provided — stored as empty string.
    """
    leases: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        leases.append(
            {
                "expires": parts[0],
                "mac": parts[1].upper(),
                "ip": parts[2],
                "hostname": "" if parts[3] == "*" else parts[3],
            }
        )
    return leases


# ---------------------------------------------------------------------------
# Section splitter (used by collector to parse batched command output)
# ---------------------------------------------------------------------------


def split_sections(output: str) -> dict[str, str]:
    """
    Split SSH batch output into named sections.

    The batch command emits markers in the form::

        echo __section_name__
        <command output>

    This function returns a dict mapping section_name → output text.
    """
    sections: dict[str, str] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("__") and stripped.endswith("__") and len(stripped) > 4:
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines)
            current_key = stripped[2:-2]  # strip leading/trailing __
            current_lines = []
        elif current_key is not None:
            current_lines.append(line)

    if current_key is not None:
        sections[current_key] = "\n".join(current_lines)

    return sections


# ---------------------------------------------------------------------------
# Wired client helpers
# ---------------------------------------------------------------------------


def parse_brctl_showmacs(text: str) -> list[dict]:
    """
    Parse `brctl showmacs br0` output.

    Format:
        port no   mac addr          is local?   ageing timer
          1       aa:bb:cc:dd:ee:ff   yes          0.00

    Returns list of {port_no: int, mac: str (upper), is_local: bool}.
    Header and malformed lines are skipped.
    """
    entries: list[dict] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            port_no = int(parts[0])
            mac = parts[1].upper()
            is_local = parts[2].lower() == "yes"
            entries.append({"port_no": port_no, "mac": mac, "is_local": is_local})
        except (ValueError, IndexError):
            continue
    return entries


def parse_brif_ports(text: str) -> dict[str, int]:
    """
    Parse sysfs bridge port map output.

    Lines are: "<ifname> <hex_port_no>"  e.g. "eth1 0x1"
    Returns {ifname: port_no_int}.
    """
    port_map: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        try:
            port_map[parts[0]] = int(parts[1], 16)
        except (ValueError, IndexError):
            continue
    return port_map


def parse_arp(text: str) -> dict[str, str]:
    """
    Parse /proc/net/arp.

    Format:
        IP address       HW type     Flags       HW address            Mask     Device
        192.168.86.117   0x1         0x2         48:68:4a:9d:48:4c     *        br0

    Returns {mac_upper: ip} for complete entries (flags 0x2).
    """
    result: dict[str, str] = {}
    for line in text.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 4:
            continue
        ip = parts[0]
        flags = parts[2]
        mac = parts[3].upper()
        if flags == "0x2" and mac != "00:00:00:00:00:00":
            result[mac] = ip
    return result


def parse_link_speeds(text: str) -> dict[str, int]:
    """
    Parse link speed sysfs output.

    Lines are: "<ifname> <speed_mbps>"  e.g. "eth1 1000"
    Returns {ifname: speed_mbps_int}.
    """
    result: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        try:
            result[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return result


def parse_traffic_analyzer(text: str) -> dict[str, dict[str, int]]:
    """
    Parse TrafficAnalyzer.db query output.

    Each line: "mac|sum_tx|sum_rx|max_timestamp"
    Returns {mac_upper: {"tx": int, "rx": int, "max_ts": int}}.
    """
    result: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4:
            continue
        try:
            mac = parts[0].upper()
            result[mac] = {
                "tx": int(parts[1]),
                "rx": int(parts[2]),
                "max_ts": int(parts[3]),
            }
        except (ValueError, IndexError):
            continue
    return result


def parse_web_history(text: str) -> list[tuple[str, int, str]]:
    """
    Parse WebHistory.db query output.

    Each line: "mac|timestamp|url"
    Returns list of (mac_upper, timestamp_int, url) tuples.
    """
    entries: list[tuple[str, int, str]] = []
    for line in text.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) != 3:
            continue
        try:
            entries.append((parts[0].upper(), int(parts[1]), parts[2]))
        except (ValueError, IndexError):
            continue
    return entries
