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
under `Pollers`. The shipped default collector list already covers everything this
dashboard needs, so in most cases you only add the poller.

A minimal configuration for one cluster:

```yaml
Exporters:
  prometheus:
    exporter: Prometheus
    local_http_addr: 0.0.0.0
    port: 12990

Defaults:
  # A config collector and a perf collector. Ems is optional but harmless.
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

### Which collectors this dashboard needs

Two, one of each kind. **Either the REST or the ZAPI family works** — Harvest has shipped
matching templates for both since 22.11, exporting identical metric names:

| Kind | Use either | What it provides for this dashboard |
| --- | --- | --- |
| Config | `Rest` **or** `Zapi` | `node_labels` (HA partner, model), `net_port_status` (ipspace, role) |
| Perf | `RestPerf` **or** `ZapiPerf` | `headroom_cpu_*`, `node_cpu_busy`, `nic_util_percent`, `nic_ifgrp_*` |

If you list both families, Harvest does not collect anything twice. It resolves the overlap
by **the order the collectors appear in your poller**, then falls through to the next
collector for objects the first one does not support. So a list beginning `Zapi, ZapiPerf,
..., Rest, RestPerf` means ZAPI serves every object this dashboard uses and the REST
collectors only pick up REST-only objects. That is a perfectly good configuration, and it is
what the A220 validation lab runs — see [LAB-PLAN.md](../LAB-PLAN.md#what-the-first-lab-run-found).

Put `Rest` before `Zapi` if you want REST to win. That is the better long-term choice:
from ONTAP 9.14.1, ZAPIs are automatically disabled 30 days after an upgrade if nothing
uses them. Harvest handles the switch on its own if the cluster stops speaking ZAPI, so
this is a preference rather than an urgent migration.

To set the collectors per poller instead of globally:

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
| `node_labels` | No config collector running — check `bin/harvest status` |
| `node_labels` has no `ha_partner` label | Harvest older than 24.05. Both `Rest` and `Zapi` supply this label on 24.05+ |
| `net_port_status` | No config collector running |
| `headroom_cpu_*` | No perf collector running, or the template was disabled |
| `nic_ifgrp_*` | No ifgrps on the cluster (scoring falls back to physical NICs — acceptable) |

Also confirm the `ha_partner` label exists:

```promql
count(node_labels{ha_partner!=""}) by (cluster, node, ha_partner)
```

This should return one row per node showing its partner. An empty result means no config
collector is running, or the Harvest version is too old.

---

## Step 5 — Import the dashboard

1. In Grafana: **Dashboards → Import**
2. Click **Upload JSON file** and select `grafana/dashboards/cmode/ha_headroom.json`
   (from this repository).
3. Select your Prometheus or VictoriaMetrics datasource.
4. Click **Import**.

The same steps import `grafana/dashboards/cmode/ha_headroom_debug.json` as
**ONTAP: HA Headroom Debug**. That dashboard is the charts behind the light. A link
at the top of each one opens the other.

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
| **HA partner coverage** | `1.0` — all nodes paired | No config collector running, or Harvest < 24.05. Pair scores will be incomplete or blank. |
| **Ethernet ports in scope** | Your data ports only — no cluster ports | Adjust `ExcludePorts`. Cluster ports in this table would inflate the network score. |
| **Headroom confidence (worst node)** | Any value — the tile is informational (blue), never an alarm | `0 unknown` or `1 seed` is normal and simply means the dashboard uses `node_cpu_busy` instead of PCU. It is not a fault. It also tells you which of the two CPU tiles drives the score; the other one is reference only and its colour can disagree with the traffic light. |

If the **HA pairs, models and partner health** table shows an empty `ha_partner` column,
the config collector is the first thing to check.

---

## Troubleshooting

**Import fails with `"uid" in e` / `cannot use 'in' operator to search for "uid"`**

Your copy of the JSON uses the legacy bare-string datasource reference, which crashes
Grafana's UI importer on Grafana Cloud and 12.4+. Pull the current `ha_headroom.json` from
this repository and re-import — it uses the object form and imports everywhere from
Grafana 8.3 up. Full explanation in
[grafana/README.md](../grafana/README.md#import-fails-with-a-uid-error).

**"No data" on all panels**

- Confirm `bin/harvest status` shows the poller as running.
- Confirm Prometheus is scraping and has data: open the Prometheus UI and query
  `{cluster="your-cluster-name"}` — if that returns nothing, the scrape is not working.
- Check the Grafana datasource is pointed at the right Prometheus URL.

**Traffic light is blank or always green despite obvious load**

- Run the metric verification query in Step 4.
- Check the `ExcludePorts` variable — if it matches data ports, the network score goes blank.
- Check the **HA partner coverage** panel. Below 1.0 the takeover light shows Unknown
  rather than a score from the nodes that did pair.

**`HA partner coverage` shows less than 1.0**

No config collector is running, or Harvest is older than 24.05. The dashboard falls back to
the `node_cpu_busy` fallback model, which works correctly for 2-node clusters but produces
no per-pair breakdown on larger ones. Upgrade Harvest and make sure `Rest` or `Zapi` is in
the collectors list to fix it.

**`net_port_status` is missing / Ethernet ports in scope table is empty**

Same cause: no config collector running. The dashboard falls back to filtering ports by the
`ExcludePorts` name regex. Use the dropdown preset that matches your platform (`A30`,
`A70 / A90`, `A220 / older FAS`) so the right cluster ports are excluded. Enabling a
config collector is strongly preferred — the `ipspace` filter then does the job
automatically for any platform.
