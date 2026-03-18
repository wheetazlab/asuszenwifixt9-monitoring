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

## How a scrape works

Each Prometheus scrape triggers `RouterCollector.collect()`, which

1. **Iterates over every configured node** (router + extender) and calls `_collect_node(node, metrics)`.
2. Inside `_collect_node`, runs **4 batched SSH exec calls** per node to minimise connection overhead:

   | Batch | SSH command group | What it collects |
   |-------|------------------|-----------------|
   | **System** | `/proc` + `/sys` + DHCP leases | CPU, memory, uptime, load, temperature, interface counters, DHCP lease count |
   | **Wired** | `brctl showmacs`, ARP table, link speed sysfs | Wired client MAC→port mapping, link speeds |
   | **WiFi radio** | `wl status` + `wl chanim_stats` + `wl assoclist` | Channel utilisation, noise floor, backhaul RSSI, per-radio client list |
   | **STA info** | `wl sta_info <MAC>` × all clients | Per-client RSSI, TX/RX bytes, PHY rate, idle time, TX failures |

3. On the router only, a fifth SQLite query batch is run against the TrafficAnalyzer DB:

   ```sql
   SELECT mac, SUM(tx), SUM(rx), MAX(timestamp)
   FROM traffic
   WHERE timestamp > <last_seen_ts>
   GROUP BY mac;
   ```

   Results are merged into cumulative counters (`_traffic_cumulative`) so the Prometheus counters never reset even across exporter restarts. The lookback window starts **25 hours** in the past on first start to pick up any existing hourly DB rows.

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
| `SSH_PASSWORD` | _(required)_ | Set from a Kubernetes Secret via `envFrom` |
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
