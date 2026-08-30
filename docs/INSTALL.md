# Installation guide

Step-by-step from "I have a running ONTAP cluster" to "the dashboard is showing data."

If Harvest, Prometheus, and Grafana are already up and collecting metrics from your cluster,
jump straight to [Step 4](#step-4--verify-the-metrics-are-present).

---

## What you need

| Component | Minimum version | Purpose |
| --- | --- | --- |
| NetApp Harvest | **24.05** | Older releases lack the `ha_partner` label |
| Prometheus or VictoriaMetrics | any recent | Time-series database |
| Grafana | 8 or newer | Dashboard rendering |
| ONTAP | 9.8 or newer (REST API available) | Source of the metrics |

---

## Step 1 — Install Harvest

If Harvest is already installed and running, skip to [Step 2](#step-2--create-a-poller-for-your-cluster).

Official guide: [https://netapp.github.io/harvest/latest/install/native/](https://netapp.github.io/harvest/latest/install/native/)

The quickest path on Linux:

```bash
# Download the latest release from GitHub
# https://github.com/NetApp/harvest/releases — pick the .tar.gz for your platform
tar -xzf harvest-*.tar.gz
cd harvest-*/

# Verify version (must be 24.05 or newer)
bin/harvest version
```

Harvest does not require root. A dedicated service account is recommended.

---

## Step 2 — Create a poller for your cluster

Harvest is configured in `harvest.yml`. Open it in a text editor and add your cluster
under `Pollers`. You also need to add `Rest` and `RestPerf` to the collector list, since
the default ships with `Zapi` and `ZapiPerf` only.

A minimal configuration for one cluster:

```yaml
Exporters:
  prometheus:
    exporter: Prometheus
    local_http_addr: 0.0.0.0
    port: 12990

Defaults:
  # Rest and RestPerf are required. Ems is optional but harmless.
  collectors:
    - Rest
    - RestPerf
    - Ems
  exporters:
    - prometheus

Pollers:
  my-cluster:
    datacenter: DC-01        # shown in the Grafana variable
    addr: 192.0.2.100        # cluster management IP or hostname
    auth_style: basic_auth
    username: harvest-user
    password: your-password
```

If you already have a `Zapi` / `ZapiPerf` poller and do not want to change the global
`Defaults`, add the collectors directly to the individual poller instead:

```yaml
Pollers:
  my-cluster:
    datacenter: DC-01
    addr: 192.0.2.100
    auth_style: basic_auth
    username: harvest-user
    password: your-password
    collectors:
      - Rest
      - RestPerf
      - Ems
```

Both `Rest` and `RestPerf` are required:

| Collector | What it provides for this dashboard |
| --- | --- |
| `Rest` | `node_labels` (HA partner, model), `net_port_status` (ipspace, role) |
| `RestPerf` | `headroom_cpu_*`, `node_cpu_busy`, `nic_util_percent`, `nic_ifgrp_*` |

### ONTAP user permissions

The Harvest user needs read-only access to performance and configuration counters.
The minimum role:

```
security login role create -role harvest -cmddirname "DEFAULT" -access readonly
security login create -user-or-group-name harvest-user -authmethod password \
  -role harvest -application ontapi
security login create -user-or-group-name harvest-user -authmethod password \
  -role harvest -application http
```

Full certificate-auth instructions are at
[https://netapp.github.io/harvest/latest/prepare-cdot-clusters/](https://netapp.github.io/harvest/latest/prepare-cdot-clusters/)

---

## Step 3 — Start Harvest and Prometheus

### Start Harvest

```bash
bin/harvest start
bin/harvest status          # confirm all pollers are running
```

Metrics are exposed on the Prometheus port defined in `harvest.yml` (default 12990 for
a single-poller setup, or the `port_range` if using multiple pollers with
`Exporter: Prometheus`).

### Configure Prometheus to scrape Harvest

Add a scrape job to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: harvest
    static_configs:
      - targets:
          - localhost:12990   # adjust if Harvest runs on a different host or port
```

Or, if you are using Harvest's Prometheus HTTP service discovery:

```yaml
scrape_configs:
  - job_name: harvest
    http_sd_configs:
      - url: http://localhost:8887/api/v1/sd
```

Restart Prometheus after changing `prometheus.yml`.

### VictoriaMetrics

VictoriaMetrics accepts the same Prometheus scrape config. The dashboard queries are
PromQL-compatible and work unchanged.

---

## Step 4 — Verify the metrics are present

Wait 3–5 minutes for Harvest to complete its first collection cycle, then run this query
in the Prometheus/VictoriaMetrics UI (or Grafana's Explore view):

```promql
count({__name__=~"headroom_cpu_current_utilization|headroom_cpu_optimal_point_utilization|headroom_cpu_optimal_point_confidence_factor|node_cpu_busy|node_labels|nic_util_percent|nic_ifgrp_rx_perc|nic_ifgrp_tx_perc|net_port_status"}) by (__name__)
```

Expected result — a non-zero count for each name:

| Metric | If missing |
| --- | --- |
| `node_labels` | Rest collector not running — check `bin/harvest status` |
| `node_labels` has no `ha_partner` label | Harvest older than 24.05, or ZAPI-only poller |
| `net_port_status` | Rest collector not running |
| `headroom_cpu_*` | RestPerf not running, or the template was disabled |
| `nic_ifgrp_*` | No ifgrps on the cluster (scoring falls back to physical NICs — acceptable) |

Also confirm the `ha_partner` label exists:

```promql
count(node_labels{ha_partner!=""}) by (cluster, node, ha_partner)
```

This should return one row per node showing its partner. An empty result means the Rest
collector is missing or the Harvest version is too old.

---

## Step 5 — Import the dashboard

1. In Grafana: **Dashboards → Import**
2. Click **Upload JSON file** and select `grafana/dashboards/cmode/ha_headroom.json`
   (from this repository).
3. Select your Prometheus or VictoriaMetrics datasource.
4. Click **Import**.

Alternatively, if you manage dashboards inside the Harvest tree:

```bash
cp grafana/dashboards/cmode/ha_headroom.json \
   /path/to/harvest/grafana/dashboards/cmode/ha_headroom.json
bin/harvest grafana import --addr localhost:3000 --token <grafana-api-token>
```

---

## Step 6 — First-run checks

Open **ONTAP: HA Headroom**, set **Datacenter** and **Cluster**, and check these
three panels before trusting the traffic light:

| Panel | Expected | If not |
| --- | --- | --- |
| **HA partner coverage** | `1.0` — all nodes paired | Rest collector missing or Harvest < 24.05. Pair scores will be incomplete or blank. |
| **Ethernet ports in scope** | Your data ports only — no cluster ports | Adjust `ExcludePorts`. Cluster ports in this table would inflate the network score. |
| **Headroom confidence (worst node)** | Any value | A value of 0 or 1 is normal and simply means the dashboard uses `node_cpu_busy` instead of PCU. It is not a fault. |

If the **HA pairs, models and partner health** table shows an empty `ha_partner` column,
the Rest collector is the first thing to check.

---

## Troubleshooting

**"No data" on all panels**

- Confirm `bin/harvest status` shows the poller as running.
- Confirm Prometheus is scraping and has data: open the Prometheus UI and query
  `{cluster="your-cluster-name"}` — if that returns nothing, the scrape is not working.
- Check the Grafana datasource is pointed at the right Prometheus URL.

**Traffic light is blank or always green despite obvious load**

- Run the metric verification query in Step 4.
- Check the `ExcludePorts` variable — if it matches data ports, the network score goes blank.
- Check the **HA partner coverage** panel. Below 1.0 means pair matching is incomplete and
  the traffic light may underreport risk.

**`HA partner coverage` shows less than 1.0**

The Rest config collector is not running, or Harvest is older than 24.05. The dashboard
falls back to the `node_cpu_busy` fallback model, which works correctly for 2-node clusters
but produces no per-pair breakdown on larger ones. Upgrade Harvest and add `Rest` to the
collectors list to fix it.

**`net_port_status` is missing / Ethernet ports in scope table is empty**

Same cause: Rest collector not running. The dashboard falls back to filtering ports by the
`ExcludePorts` name regex. For A30/A70/A90 that regex must match the actual cluster ports
(`e4a|e4b` for A30, `e1a|e7a` for A70/A90) or data ports will be excluded instead.
Enabling the Rest collector is strongly preferred.
