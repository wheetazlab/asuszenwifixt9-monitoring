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

The exporter currently pulls data from **two distinct sources**: live SSH commands run against the router/extender, and a SQLite database that the router's firmware maintains locally.

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
| **WiFi radio** | `wl -i <iface> status` | `asus_router_backhaul_{rssi_dbm,snr_db}`, `asus_router_wifi_noise_dbm` |
| | `wl -i <iface> chanim_stats` | `asus_router_wifi_channel_utilization_percent`, `asus_router_wifi_channel_{goodtx,badtx,glitch}_total` |
| | `wl -i <iface> assoclist` | Enumerates connected client MACs (input to the STA info batch) |
| | _(combined above)_ | `asus_router_wifi_clients` |
| **STA info** | `wl -i <iface> sta_info <MAC>` × every client | `asus_router_wifi_client_rssi_dbm`, `asus_router_wifi_client_{tx,rx}_bytes_total`, `asus_router_wifi_client_{tx,rx}_rate_kbps`, `asus_router_wifi_client_tx_failures_total`, `asus_router_wifi_client_idle_seconds` |

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

### Other databases on the router

The `/jffs/.sys/` directory contains three other SQLite databases. None are currently used by this exporter:

| Database | Path | Schema summary | Notes |
|----------|------|----------------|-------|
| `WebHistory.db` | `/jffs/.sys/WebHistory/WebHistory.db` | `history(mac, timestamp, url)` | DNS/web browsing history per client MAC. Populated only when the WRS (Web Reputation Service) daemon is running — not available on all firmware versions. |
| `AiProtectionMonitor.db` | `/jffs/.sys/AiProtectionMonitor/AiProtectionMonitor.db` | `monitor(timestamp, type, mac, src, dst, cat_id, severity)` | AiProtection security threat events. Empty if no threats have been detected. |
| `nt_db.db` | `/jffs/.sys/nc/nt_db.db` | `nt_center(tstamp, event, status, msg)` | Router notification/event log. Not useful for Prometheus metrics. |

> **Note:** `stainfo.db` and `wifi_detect.db` were considered as migration targets for per-client WiFi stats and noise floor data respectively. These databases do **not exist** on this firmware — per-client sta_info and noise floor data are only available via `wl sta_info` and `wl chanim_stats` SSH commands.

---

## How a scrape works

Each Prometheus scrape triggers `RouterCollector.collect()`, which

1. **Iterates over every configured node** (router + extender) and calls `_collect_node(node, metrics)`.
2. Inside `_collect_node`, fires the four SSH command batches (System, Wired, WiFi radio, STA info) in sequence. Router and extender run independently.
3. **Router only** — runs the TrafficAnalyzer SQLite query via `sqlite3` over SSH and merges results into cumulative counters.
4. Appends a `asus_router_scrape_duration_seconds` gauge, then yields all metrics.

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
