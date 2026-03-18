# collector/

Python package that implements the Prometheus custom collector.

---

## Module overview

| File | Responsibility |
|------|---------------|
| `config.py` | Environment-variable configuration and static hardware tables (interface lists, MAC addresses) |
| `ssh_client.py` | `RouterSSHClient` — persistent paramiko SSH wrapper with auto-reconnect |
| `parsers.py` | Pure parsing functions: no side effects, no I/O; given raw SSH output returns structured dicts |
| `collector.py` | `RouterCollector` — `prometheus_client` custom Collector; orchestrates SSH batches, calls parsers, yields `Metric` objects |
| `main.py` | Entry point: builds `NodeConfig` objects, registers the collector, starts the HTTP server, handles SIGTERM |
| `__init__.py` | Package marker |

---

## Data sources

The exporter currently pulls data from **three distinct sources**: live SSH commands run against the router/extender, and a SQLite database that the router's firmware maintains locally.

### Source 1 — SSH commands (both nodes)

All system, WiFi, and wired metrics come from commands executed over SSH. Multiple commands are concatenated into a single exec call ("batch") to minimise connection overhead. Each call is delimited with `echo __section__` markers so the output can be split into named sections before parsing.

| Batch | Commands & files read | Metrics produced |
|-------|-----------------------|-----------------|
| **System** | `cat /proc/loadavg` | `asus_router_load_{1,5,15}m` |
| | `cat /proc/uptime` | `asus_router_uptime_seconds` |
| | `cat /proc/meminfo` | `asus_router_memory_*_bytes` |
| | `grep '^cpu ' /proc/stat` | `asus_router_cpu_seconds_total` |
| | `cat /sys/class/thermal/thermal_zone0/temp` | `asus_router_temperature_celsius` |
| | `cat /proc/net/dev` | `asus_router_interface_{rx,tx}_{bytes,packets,errors,drops}_total` |
| | `cat /var/lib/misc/dnsmasq.leases` | `asus_router_dhcp_leases_total`; builds the MAC→hostname map used to label all per-client metrics |
| **Wired** | `brctl showmacs br0` | Bridge MAC-to-port table — identifies which LAN port a wired client is on |
| | `/sys/class/net/br0/brif/*/port_no` | Port number mapping |
| | `cat /proc/net/arp` | Resolves wired client MACs to IPs |
| | `/sys/class/net/eth{1,2,3}/speed` | Link speed (10M / 100M / 1G / 2.5G / 10G) |
| | _(combined above)_ | `asus_router_wired_client_info` |
| **WiFi radio** | `wl -i <iface> status` | `asus_router_backhaul_{rssi_dbm,snr_db}` |
| | `wl -i <iface> chanim_stats` | `asus_router_wifi_channel_utilization_percent`, `asus_router_wifi_channel_{goodtx,badtx,glitch}_total` |
| | ~~`wl -i <iface> assoclist`~~ | ~~Enumerates connected client MACs (input to the STA info batch)~~ **Removed** — client enumeration is now via stainfo.db |
| **STA info** | ~~`wl -i <iface> sta_info <MAC>` × every client~~ | **Replaced by stainfo.db** — see Source 3. `asus_router_wifi_client_rssi_dbm`, `asus_router_wifi_client_{tx,rx}_bytes_total`, `asus_router_wifi_client_{tx,rx}_rate_kbps`, `asus_router_wifi_client_conn_time_seconds`, `asus_router_wifi_{clients,associated}` |
| | ~~`wl -i <iface> chanim_stats`~~ (noise only) | **Replaced by wifi_detect.db** — see Source 4. `asus_router_wifi_noise_dbm` |

`wl` is a Broadcom proprietary WiFi utility (`/usr/sbin/wl`) built into the ASUS firmware. There is no standard Linux equivalent.

---

### Source 2 — TrafficAnalyzer SQLite database (router only)

> **Database location on the router:** `/jffs/.sys/TrafficAnalyzer/TrafficAnalyzer.db`  
> **Written by:** the `TrafficAnalyzer` firmware daemon (`bwdpi_check` / `bwdpi_wred_alive` processes)  
> **Write cadence:** hourly aggregates — one row per MAC per hour  
> **Table:** `traffic (mac TEXT, tx INTEGER, rx INTEGER, timestamp INTEGER)`

The exporter SSHes to the router and runs `sqlite3` directly against the file:

```sql
SELECT mac, SUM(tx), SUM(rx), MAX(timestamp)
FROM traffic
WHERE timestamp > <last_seen_ts>
GROUP BY mac;
```

`sqlite3` is a standard binary present in ASUS firmware. The query runs via SSH as part of the same `ssh.run()` mechanism as the command batches above — it is just a single-line shell command, not a separate connection.

Results are merged into in-memory cumulative counters (`_traffic_cumulative`) keyed by MAC address, so the Prometheus counters never reset even across exporter restarts. On first start the lookback window is set to **25 hours** in the past to ensure all recent hourly rows are picked up immediately.

| Metric | Source column |
|--------|--------------|
| `asus_router_traffic_analyzer_tx_bytes_total` | `SUM(tx)` — bytes transmitted by that MAC in the query window |
| `asus_router_traffic_analyzer_rx_bytes_total` | `SUM(rx)` — bytes received by that MAC in the query window |

Labels: `mac`, `hostname` (from DHCP leases), `ip` (from DHCP leases).

> **Why a database instead of live counters?** The router's per-client live byte counters reset on disconnect and on radio restart. TrafficAnalyzer persists cumulative totals across disconnections, making it the only reliable source for long-term per-device bandwidth accounting.

---

### Source 2 — stainfo.db (router query, covers both nodes)

> **Database location on the router:** `/tmp/.diag/stainfo.db`  
> **Written by:** `conn_diag` firmware process  
> **Write cadence:** every ~60 seconds  
> **Table:** `DATA_INFO` — one row per currently-associated client per snapshot

Replaces the entire `wl assoclist` + `wl sta_info <MAC>` loop.  One SQL query returns all clients from **both the router and extender** in a single SSH exec:

```sql
SELECT sta_mac, node_type, node_ip, sta_band, sta_rssi, sta_active,
       sta_tx, sta_rx, sta_tbyte, sta_rbyte, conn_time, txpr, conn_if, data_time
FROM DATA_INFO
WHERE data_time >= (SELECT MAX(data_time) - 2 FROM DATA_INFO)
```

The `- 2` window is needed because the router (`node_type=C`) and extender (`node_type=R`) write to the same DB ~1 second apart.

| DB column | Unit | Metric produced |
|-----------|------|-----------------|
| `sta_rssi` | dBm | `asus_router_wifi_client_rssi_dbm` |
| `sta_tx` / `sta_rx` | Mbps PHY rate | `asus_router_wifi_client_{tx,rx}_rate_kbps` (×1000) |
| `sta_tbyte` / `sta_rbyte` | bytes since assoc | `asus_router_wifi_client_{tx,rx}_bytes_total` |
| `conn_time` | seconds | `asus_router_wifi_client_conn_time_seconds` |
| `txpr` | retry count | `asus_router_wifi_client_tx_retries_total` |
| `conn_if` | interface name | `radio` label (e.g. `eth5`, `wl1.1`) |
| `node_type` | C / R | `node` label (`router` / `extender`) |
| `sta_band` | 2G / 5G / 5G1 | `band` label (`2.4GHz` / `5GHz` / `5GHz-2`) |

Wifi client counts (`wifi_clients`, `wifi_associated`) are also derived here by grouping rows on `(node, conn_if, band)`. Backhaul MACs (`ROUTER_BACKHAUL_MACS`) are excluded from `wifi_clients` but counted in `wifi_associated`.

If the latest `data_time` is more than 180 seconds old, all client metrics are skipped and a warning is logged.

---

### Source 3 — wifi_detect.db (router query, covers both nodes)

> **Database location on the router:** `/tmp/.diag/wifi_detect.db`  
> **Written by:** `conn_diag` firmware process  
> **Write cadence:** every ~60 seconds  
> **Table:** `DATA_INFO` — one row per radio interface per snapshot (6 rows total: 3 radios × 2 nodes)

Replaces per-node `wl status` and `wl chanim_stats` noise floor parsing. One SQL query returns noise floor for all 6 radios on both nodes:

```sql
SELECT node_type, node_ip, band, ifname, noise, txop, tx_byte, rx_byte, glitch, txfail, data_time
FROM DATA_INFO
WHERE data_time >= (SELECT MAX(data_time) - 2 FROM DATA_INFO)
```

| DB column | Unit | Metric produced |
|-----------|------|-----------------|
| `noise` | dBm | `asus_router_wifi_noise_dbm` |

Channel utilisation percentages (`inbss`, `obss`, `tx`, etc.) are NOT available in this DB (`chanim` column is always `-1`) — they still come from `wl chanim_stats` in Batch 2.

---

### Other databases on the router

The `/jffs/.sys/` directory contains three other SQLite databases. None are currently used by this exporter:

| Database | Path | Schema summary | Notes |
|----------|------|----------------|-------|
| `WebHistory.db` | `/jffs/.sys/WebHistory/WebHistory.db` | `history(mac, timestamp, url)` | DNS/web browsing history per client MAC. Populated only when the WRS (Web Reputation Service) daemon is running — not available on all firmware versions. |
| `AiProtectionMonitor.db` | `/jffs/.sys/AiProtectionMonitor/AiProtectionMonitor.db` | `monitor(timestamp, type, mac, src, dst, cat_id, severity)` | AiProtection security threat events. Empty if no threats have been detected. |
| `nt_db.db` | `/jffs/.sys/nc/nt_db.db` | `nt_center(tstamp, event, status, msg)` | Router notification/event log. Not useful for Prometheus metrics. |

> **Note:** `stainfo.db` and `wifi_detect.db` live at `/tmp/.diag/` (not `/jffs/.sys/`) and are written by the `conn_diag` process. They are now **fully implemented** — see Sources 2 and 3 above.

---

## How a scrape works

Each Prometheus scrape triggers `RouterCollector.collect()`, which

1. **Iterates over every configured node** (router + extender) and calls `_collect_node(node, metrics)`.
2. Inside `_collect_node`, fires three SSH exec calls per node:
   - **System batch** — `/proc` + `/sys` + DHCP leases (identical for both nodes)
   - **WiFi batch** — `wl status` + `wl chanim_stats` per radio (backhaul RSSI, channel utilisation; assoclist removed)
   - **Wired batch** — `brctl showmacs`, ARP table, link speeds
3. **Router only** — fires one additional SSH exec containing three SQLite queries bundled together:
   - `TrafficAnalyzer.db` — hourly per-device bandwidth totals
   - `stainfo.db` — current per-client RSSI, rates, bytes, conn_time for **all clients on both nodes**
   - `wifi_detect.db` — current per-radio noise floor for **all radios on both nodes**
4. Appends `asus_router_scrape_duration_seconds`, then yields all metrics.

Total SSH exec calls per scrape: **7** (3 per node × 2 nodes + 1 DB exec on router). Previously this was up to **60+** when a full house of WiFi clients triggered individual `wl sta_info` calls.

---

## SSH connection model

- `RouterSSHClient` keeps **one persistent paramiko transport** per node, opened on first use.
- On `SSHException` or `OSError`, `run()` calls `close()` and retries **once** with a new connection.
- ASUS router dropbear closes idle channels; the retry logic handles this transparently.
- Connections are created at `RouterCollector.__init__` time (not lazily) so the startup SSH test is visible in logs.

---

## TrafficAnalyzer DB maintenance

The ASUS firmware has a known bug: the `TrafficAnalyzer` daemon is launched with a `-d <size_kb>` flag (14 MB on XT9 firmware). When the SQLite DB reaches that cap, the daemon **silently stops writing** instead of rotating old rows.

### Auto-prune on startup (`_ensure_prune_cron`)

At startup `RouterCollector.__init__` calls `_ensure_prune_cron(router_ssh)`, which:

1. **Checks `cru l`** — if a job named `prune_trafficanalyzer` is already registered, returns immediately (idempotent).
2. **Writes `/jffs/scripts/prune_trafficanalyzer.sh`** (only if the file is absent):

   ```sh
   #!/bin/sh
   cutoff=$(( $(date +%s) - 604800 ))
   sqlite3 /jffs/.sys/TrafficAnalyzer/TrafficAnalyzer.db \
     "DELETE FROM traffic WHERE timestamp < $cutoff; VACUUM;"
   ```

   This keeps 7 days of rows and shrinks the DB file with `VACUUM`.

3. **Registers the cron job** immediately in the live daemon:

   ```
   cru a prune_trafficanalyzer '0 0 * * * /jffs/scripts/prune_trafficanalyzer.sh'
   ```

4. **Persists across reboots** by appending to `/jffs/scripts/services-start` — a script the ASUS firmware runs after every boot from the persistent `/jffs` flash partition.

`/jffs` survives firmware updates and factory-reset-less reboots, so this configuration is durable.

---

## Configuration reference

All settings come from environment variables (see `config.py`):

| Variable | Default | Notes |
|----------|---------|-------|
| `ROUTER_SSH_HOST` | _(required)_ | Router LAN IP |
| `ROUTER_SSH_PORT` | `2222` | |
| `EXTENDER_SSH_HOST` | _(required)_ | Extender LAN IP |
| `EXTENDER_SSH_PORT` | `2222` | |
| `SSH_USERNAME` | `router` | Same credential used for both nodes |
| `SSH_PASSWORD` | _(required)_ | SSH password for both nodes |
| `METRICS_PORT` | `9100` | Port that `/metrics` listens on |
| `LOG_LEVEL` | `INFO` | Python log level (`DEBUG`, `INFO`, `WARNING`, …) |

Static hardware tables (interface names, MAC addresses) live in `config.py` and only need to change if the hardware changes.

---

## Running locally

```bash
pip install -r ../requirements.txt
export ROUTER_SSH_HOST=192.168.86.1
export EXTENDER_SSH_HOST=192.168.86.179
export SSH_PASSWORD=yourpassword
python -m collector.main
curl http://localhost:9100/metrics | grep asus_router
```
