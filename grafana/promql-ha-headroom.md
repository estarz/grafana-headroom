# PromQL for ONTAP HA Headroom

Queries assume Harvest labels `datacenter`, `cluster`, `node`, plus `nic` / `ifgroup` / `port`.
Grafana variables: `$Datacenter`, `$Cluster`, `$Window` (`15m` or `30m`), `$ExcludePorts` (regex, default `e0a|e0b|e0M`).

`nic_util_percent` and `fcp_util_percent` are **0–1**; multiply by 100 for display.

---

## Verify Harvest is scraping (todo: verify-metrics)

Run against Prometheus or VictoriaMetrics. Default RestPerf/ZapiPerf templates already collect these objects:

| Metric | Harvest object / template |
| --- | --- |
| `headroom_cpu_*` | `resource_headroom_cpu` — `conf/restperf/9.12.0/resource_headroom_cpu.yaml` |
| `node_cpu_busy`, `node_avg_processor_busy`, `node_cpu_domain_busy` | `system:node` — `conf/restperf/9.12.0/system_node.yaml` |
| `nic_util_percent`, `nic_rx_percent`, `nic_tx_percent` | Nic plugin on `nic_common` |
| `nic_ifgrp_rx_perc`, `nic_ifgrp_tx_perc` | Nic plugin (`ifgroup`, `node`, `ports` labels) |
| `fcp_util_percent` | `conf/restperf/9.12.0/fcp.yaml` |
| `node_nfs_latency` | `system:node` NFS counters |
| `fcp_avg_read_latency`, `fcp_avg_write_latency` | FCP |

```promql
count({__name__=~"headroom_cpu_current_utilization|headroom_cpu_optimal_point_utilization|headroom_cpu_optimal_point_confidence_factor|node_cpu_busy|nic_util_percent|nic_ifgrp_rx_perc|nic_ifgrp_tx_perc|fcp_util_percent"}) by (__name__)
```

Expect a non-zero count per name for the AFF cluster. If `headroom_cpu_*` is missing, confirm RestPerf/ZapiPerf includes `resource_headroom_cpu`. If `nic_ifgrp_*` is missing, ifgrps may not be configured (Ethernet panels still work via `nic_util_percent`). If `fcp_util_percent` is missing, the cluster has no FC (hide SAN or treat as no FC risk).

Stock dashboards for a live data check: **ONTAP: Headroom**, **ONTAP: Node**, **ONTAP: Network**.

---

## CPU: performance capacity used (todo: promql-pcu)

Per-node PCU (AIUM-style):

```promql
100 *
  headroom_cpu_current_utilization{datacenter=~"$Datacenter",cluster=~"$Cluster"}
  /
  clamp_min(headroom_cpu_optimal_point_utilization{datacenter=~"$Datacenter",cluster=~"$Cluster"}, 1e-3)
```

Pair takeover estimate (`PCU_takeover`):

```promql
sum by (cluster) (
  100 *
    headroom_cpu_current_utilization{datacenter=~"$Datacenter",cluster=~"$Cluster"}
    /
    clamp_min(headroom_cpu_optimal_point_utilization{datacenter=~"$Datacenter",cluster=~"$Cluster"}, 1e-3)
)
```

95th percentile over `$Window` (traffic-light input; ignores short spikes):

```promql
quantile_over_time(
  0.95,
  (
    sum by (cluster) (
      100 *
        headroom_cpu_current_utilization{datacenter=~"$Datacenter",cluster=~"$Cluster"}
        /
        clamp_min(headroom_cpu_optimal_point_utilization{datacenter=~"$Datacenter",cluster=~"$Cluster"}, 1e-3)
    )
  )[$Window:]
)
```

Confidence (use PCU only when this is ≥ 2):

```promql
min by (cluster) (
  headroom_cpu_optimal_point_confidence_factor{datacenter=~"$Datacenter",cluster=~"$Cluster"}
)
```

Available CPU ops (optional; confidence > 1):

```promql
sum by (node) (
  headroom_cpu_optimal_point_ops{datacenter=~"$Datacenter",cluster=~"$Cluster"}
  - headroom_cpu_current_ops{datacenter=~"$Datacenter",cluster=~"$Cluster"}
)
```

Raw CPU busy (fallback when confidence &lt; 2):

```promql
node_cpu_busy{datacenter=~"$Datacenter",cluster=~"$Cluster"}

sum by (cluster) (node_cpu_busy{datacenter=~"$Datacenter",cluster=~"$Cluster"})

quantile_over_time(
  0.95,
  (sum by (cluster) (node_cpu_busy{datacenter=~"$Datacenter",cluster=~"$Cluster"}))[$Window:]
)
```

CPU risk **0 / 1 / 2** (green / yellow / red), PCU if confidence ≥ 2 else `node_cpu_busy` sum:

```promql
(
  (min by (cluster) (headroom_cpu_optimal_point_confidence_factor{datacenter=~"$Datacenter",cluster=~"$Cluster"}) >= bool 2)
  *
  clamp_max(
    (
      (quantile_over_time(0.95, (sum by (cluster) (100 * headroom_cpu_current_utilization{datacenter=~"$Datacenter",cluster=~"$Cluster"} / clamp_min(headroom_cpu_optimal_point_utilization{datacenter=~"$Datacenter",cluster=~"$Cluster"}, 1e-3)))[$Window:]) >= bool 160)
      +
      (quantile_over_time(0.95, (sum by (cluster) (100 * headroom_cpu_current_utilization{datacenter=~"$Datacenter",cluster=~"$Cluster"} / clamp_min(headroom_cpu_optimal_point_utilization{datacenter=~"$Datacenter",cluster=~"$Cluster"}, 1e-3)))[$Window:]) >= bool 200)
    ),
    2
  )
)
+
(
  (min by (cluster) (headroom_cpu_optimal_point_confidence_factor{datacenter=~"$Datacenter",cluster=~"$Cluster"}) < bool 2)
  *
  clamp_max(
    (
      (quantile_over_time(0.95, (sum by (cluster) (node_cpu_busy{datacenter=~"$Datacenter",cluster=~"$Cluster"}))[$Window:]) >= bool 70)
      +
      (quantile_over_time(0.95, (sum by (cluster) (node_cpu_busy{datacenter=~"$Datacenter",cluster=~"$Cluster"}))[$Window:]) >= bool 85)
    ),
    2
  )
)
```

| Risk | Combined PCU | Sum of `node_cpu_busy` |
| --- | --- | --- |
| 0 green | &lt; 160% | &lt; 70% |
| 1 yellow | 160–200% | 70–85% |
| 2 red | &gt; 200% | &gt; 85% |

---

## Ports: survivor estimates (todo: promql-ports)

`max(rx, tx)` without `max()`:

```
(rx + tx + abs(rx - tx)) / 2
```

### Ethernet physical NICs (exclude cluster / mgmt)

Per-port util %:

```promql
nic_util_percent{datacenter=~"$Datacenter",cluster=~"$Cluster",nic!~"$ExcludePorts"} * 100
```

Estimated survivor util by port **name** (node1 + node2 of `e0e`, etc.):

```promql
sum by (cluster, nic) (
  nic_util_percent{datacenter=~"$Datacenter",cluster=~"$Cluster",nic!~"$ExcludePorts"}
) * 100
```

Hottest estimated survivor NIC (95th):

```promql
quantile_over_time(
  0.95,
  (
    max by (cluster) (
      sum by (cluster, nic) (
        nic_util_percent{datacenter=~"$Datacenter",cluster=~"$Cluster",nic!~"$ExcludePorts"}
      ) * 100
    )
  )[$Window:]
)
```

### ifgrp / LAG (preferred for NAS LIF failover)

Per-ifgrp `max(rx,tx)` %:

```promql
(
  nic_ifgrp_rx_perc{datacenter=~"$Datacenter",cluster=~"$Cluster",ports!=""}
  + nic_ifgrp_tx_perc{datacenter=~"$Datacenter",cluster=~"$Cluster",ports!=""}
  + abs(
      nic_ifgrp_rx_perc{datacenter=~"$Datacenter",cluster=~"$Cluster",ports!=""}
      - nic_ifgrp_tx_perc{datacenter=~"$Datacenter",cluster=~"$Cluster",ports!=""}
    )
) / 2 * 100
```

Estimated survivor by ifgroup name:

```promql
sum by (cluster, ifgroup) (
  (
    nic_ifgrp_rx_perc{datacenter=~"$Datacenter",cluster=~"$Cluster",ports!=""}
    + nic_ifgrp_tx_perc{datacenter=~"$Datacenter",cluster=~"$Cluster",ports!=""}
    + abs(
        nic_ifgrp_rx_perc{datacenter=~"$Datacenter",cluster=~"$Cluster",ports!=""}
        - nic_ifgrp_tx_perc{datacenter=~"$Datacenter",cluster=~"$Cluster",ports!=""}
      )
  ) / 2 * 100
)
```

Hottest estimated survivor ifgrp (95th):

```promql
quantile_over_time(
  0.95,
  (
    max by (cluster) (
      sum by (cluster, ifgroup) (
        (
          nic_ifgrp_rx_perc{datacenter=~"$Datacenter",cluster=~"$Cluster",ports!=""}
          + nic_ifgrp_tx_perc{datacenter=~"$Datacenter",cluster=~"$Cluster",ports!=""}
          + abs(
              nic_ifgrp_rx_perc{datacenter=~"$Datacenter",cluster=~"$Cluster",ports!=""}
              - nic_ifgrp_tx_perc{datacenter=~"$Datacenter",cluster=~"$Cluster",ports!=""}
            )
        ) / 2 * 100
      )
    )
  )[$Window:]
)
```

### FC target ports

```promql
fcp_util_percent{datacenter=~"$Datacenter",cluster=~"$Cluster"} * 100

sum by (cluster, port) (
  fcp_util_percent{datacenter=~"$Datacenter",cluster=~"$Cluster"}
) * 100

quantile_over_time(
  0.95,
  (
    max by (cluster) (
      sum by (cluster, port) (
        fcp_util_percent{datacenter=~"$Datacenter",cluster=~"$Cluster"}
      ) * 100
    )
  )[$Window:]
)
```

Port risk **0 / 1 / 2** from hottest of NIC / ifgrp / FCP estimates:

| Risk | Estimated survivor port/ifgrp util |
| --- | --- |
| 0 green | &lt; 70% |
| 1 yellow | 70–85% |
| 2 red | &gt; 85% |

iSCSI / NVMe-TCP use the Ethernet/ifgrp numbers (same physical ports). NVMe/FC uses `fcp_util_percent`.

---

## Overall traffic light

`max(cpu_risk, port_risk)` where `port_risk` uses the max of the three 95th-percentile survivor utilities with thresholds 70 / 85.

See panel **Overall HA headroom** in [`dashboards/cmode/ha_headroom.json`](dashboards/cmode/ha_headroom.json).
