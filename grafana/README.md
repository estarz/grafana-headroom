# Grafana: ONTAP HA Headroom

Custom Harvest dashboard for a **2-node AFF HA pair**. It estimates whether the surviving node can absorb both nodes’ CPU and data-path load after takeover.

Stock **ONTAP: Headroom** is per-node (and includes aggregates). This dashboard does **not** use aggregate/disk metrics.

## Import

1. Grafana → **Dashboards → Import** → upload [`dashboards/cmode/ha_headroom.json`](dashboards/cmode/ha_headroom.json).
2. Select your Prometheus (or VictoriaMetrics Prometheus-compatible) datasource.
3. Open **ONTAP: HA Headroom**, set **Datacenter** and **Cluster**.

Alternatively, copy the JSON into your Harvest tree as `grafana/dashboards/cmode/ha_headroom.json` and import with `bin/harvest grafana import` (custom files may need to sit beside Harvest’s dashboards).

Regenerate the JSON after editing queries:

```bash
python3 grafana/generate_ha_headroom.py
```

## Variables

| Variable | Purpose |
| --- | --- |
| Datacenter / Cluster | Same as other Harvest cDOT dashboards |
| Window | `15m` or `30m` — 95th percentile window for traffic lights |
| ExcludePorts | Regex for NICs to ignore (cluster interconnect / e0M). Default `e0a\|e0b\|e0M` |

## Queries and metric check

See [promql-ha-headroom.md](promql-ha-headroom.md).

## Validate against ONTAP

Pick a busy and a quiet interval and compare:

- Combined PCU vs `statistics show -object resource_headroom_cpu` (current vs optimal utilization per node, then sum PCU).
- Port sums vs System Manager / `statistics show` for the same ifgrp or FCP port names.

No live Prometheus is in this repo, so scrape counts must be confirmed in your environment.
