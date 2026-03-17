# grafana — Dashboard

`asus-zenwifi-xt9.json` is a Grafana dashboard for the ASUS ZenWiFi XT9 mesh network exporter.

## Import

### Manually

1. Open Grafana → **Dashboards** → **Import**
2. Click **Upload JSON file** and select `asus-zenwifi-xt9.json`
3. Select your Prometheus datasource when prompted

### Via Grafana sidecar (kube-prometheus-stack)

If you're using the Ansible role, the dashboard is deployed automatically as a ConfigMap with the `grafana_dashboard: "1"` label — the Grafana sidecar picks it up within seconds, no restart required.

To update a running cluster after a dashboard change, re-run the playbook:

```bash
aplaybook playbooks/21-deploy_asus_monitoring.yml
```

## Panels

| Panel | Type | Description |
|-------|------|-------------|
| WiFi Clients | Stat | Total associated WiFi clients across all radios |
| DHCP Leases | Stat | Active DHCP leases on the router |
| WAN Download / Upload | Time series | WAN interface throughput (eth0) |
| WiFi Clients per Radio | Time series | Client count per node/band over time |
| Channel Utilization | Time series | TX / OBSS / Idle percentages per radio |
| Noise Floor | Time series | Radio noise floor (dBm) per band |
| Backhaul RSSI | Stat | Extender → Router backhaul signal strength |
| Backhaul SNR | Stat | Extender → Router SNR |
| Memory Usage | Time series | RAM usage per node |
| CPU Load | Time series | 1/5/15-minute load averages |
| Temperature | Time series | Board temperature per node |
| All Clients | Table | Per-client: hostname, IP, MAC, node, band, RSSI, TX/RX rate, download, upload, idle |

## Client Table

The **All Clients** table merges six instant-query metrics by shared labels (`mac`, `node`, `band`, `hostname`, `ip`):

| Column | Metric | Unit |
|--------|--------|------|
| hostname | `asus_router_wifi_client_rssi_dbm` label | — |
| IP Address | DHCP lease label | — |
| mac | label | — |
| node | label (`router` / `extender`) | — |
| band | label (`2.4GHz` / `5GHz` / `5GHz-2`) | — |
| RSSI | `asus_router_wifi_client_rssi_dbm` | dBm (colour-coded) |
| TX Rate | `asus_router_wifi_client_tx_rate_kbps` | kbps |
| RX Rate | `asus_router_wifi_client_rx_rate_kbps` | kbps |
| Download | `rate(asus_router_wifi_client_rx_bytes_total[5m]) * 8` | bps |
| Upload | `rate(asus_router_wifi_client_tx_bytes_total[5m]) * 8` | bps |
| Idle | `asus_router_wifi_client_idle_seconds` | s |

RSSI cells use a gradient colour: red below −70 dBm, yellow −70 to −55 dBm, green above −55 dBm.

## Variables

| Variable | Description |
|----------|-------------|
| `datasource` | Prometheus datasource selector (auto-populated) |

## Requirements

- Grafana 10+ (uses `filterFieldsByName` transformation and `color-background` cell option)
- Dashboard UID: `asus-zenwifi-xt9`
