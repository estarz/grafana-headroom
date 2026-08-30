# Grafana: ONTAP HA Headroom

Import mechanics for the dashboard. For the model behind it see [../docs/DESIGN.md](../docs/DESIGN.md);
for the queries see [../docs/METRICS.md](../docs/METRICS.md).

## Import

1. Grafana → **Dashboards → Import** → upload
   [`dashboards/cmode/ha_headroom.json`](dashboards/cmode/ha_headroom.json).
2. Select your Prometheus or VictoriaMetrics datasource.
3. Open **ONTAP: HA Headroom** and set **Datacenter** and **Cluster**.

Alternatively copy the JSON into your Harvest tree as
`grafana/dashboards/cmode/ha_headroom.json` and import with `bin/harvest grafana import`.
Custom files may need to sit beside Harvest's own dashboards for that to pick them up.

The dashboard targets schemaVersion 30 (Grafana 8) for maximum compatibility. Grafana 9
through 12 migrate it on import without intervention.

## First run: check these three panels

Before trusting the traffic light, confirm the model has what it needs.

| Panel | Expected | If not |
| --- | --- | --- |
| **HA partner coverage** | `1.0` — all nodes paired | Enable the Harvest **Rest** collector, and confirm Harvest is 24.05 or newer. Below 1.0, the pair scores are incomplete. |
| **Ethernet ports in scope** | Your data ports, no cluster ports | Adjust `ExcludePorts`. Cluster ports appearing here means the `ipspace` filter did not work. |
| **Headroom confidence (worst node)** | Any value | 0 or 1 is normal and simply means the score uses `node_cpu_busy` instead of PCU. Not a fault. |

## Regenerating

The JSON is generated. Edit the Python, never the JSON.

```bash
python3 generate_ha_headroom.py                    # rebuild the JSON
python3 validate_promql.py /path/to/promtool       # every query parses
python3 test_promql.py     /path/to/promtool       # the HA-pair maths is right
```

`promtool` ships with Prometheus. Neither test needs a running server or a live cluster.

## Variables

Visible in the dashboard header:

| Variable | Default | Purpose |
| --- | --- | --- |
| `Datacenter` / `Cluster` | all | Same as other Harvest cDOT dashboards |
| `Window` | `30m` | Percentile window for the traffic-light inputs |
| `ExcludePorts` | `e0M` | Extra port-name exclusions on top of the `ipspace != "Cluster"` filter |

Thresholds are hidden to keep the header uncluttered. Override per session with a URL
parameter, or change the defaults in `generate_ha_headroom.py` to make it permanent:

```
/d/cdot-ha-headroom/?var-PcuYellow=175&var-PortYellow=75
```

| Variable | Default | Controls |
| --- | --- | --- |
| `PcuYellow` / `PcuRed` | 160 / 200 | Combined performance capacity used |
| `BusyYellow` / `BusyRed` | 70 / 85 | Combined `node_cpu_busy` |
| `PortYellow` / `PortRed` | 70 / 85 | Estimated survivor port utilization |
| `ConfMin` | 2 | Minimum headroom confidence before PCU is trusted over CPU busy |

## Generator options

`INCLUDE_FC` in `generate_ha_headroom.py` adds FC target-port panels and folds
`fcp_util_percent` into the network score. Off by default — read the caveat in
[../docs/ROADMAP.md](../docs/ROADMAP.md#fc-support) before enabling it.
