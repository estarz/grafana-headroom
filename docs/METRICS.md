# Metrics and PromQL reference

Every metric below was verified against Harvest's actual templates and plugin source rather
than assumed. Scales in particular are easy to get wrong: some Harvest utilization metrics
are fractions and some are percentages, and mixing them up silently produces a dashboard
that is wrong by 100x.

Harvest labels used throughout: `datacenter`, `cluster`, `node`, plus `nic`, `ifgroup`,
`ports`, `port`.

## Verified metric reference

### Config collector — `Rest`, poll interval 3m

| Metric | Scale | Labels | Template |
| --- | --- | --- | --- |
| `node_labels` | 1.0 gauge | `node`, `ha_partner`, `serial` as instance keys; `model`, `vendor`, `version`, `healthy`, `partner_healthy`, `state`, `location` as instance labels | `conf/rest/9.12.0/node.yaml` |
| `node_new_status` | 0/1 | `node`, `ha_partner`, `serial` | same |
| `net_port_status` | 1 = up | `node`, `port`, `role`, `port_type`, `ipspace`, `broadcast`, `op_speed`, `status`, `lag_*`, `vlan_*` | `conf/rest/9.12.0/netport.yaml` |
| `cluster_new_status` | 0/1 | `cluster`, `datacenter`, `system_type` | `conf/rest/9.12.0/status.yaml` |

Both `node.yaml` and `netport.yaml` are enabled by default in `conf/rest/default.yaml`.

### Performance collector — `RestPerf` / `ZapiPerf`, poll interval 1m

| Metric | Scale | Labels | Template |
| --- | --- | --- | --- |
| `headroom_cpu_current_utilization` | **0–100** | `node`, `headroom` | `conf/restperf/9.12.0/resource_headroom_cpu.yaml` |
| `headroom_cpu_optimal_point_utilization` | **0–100** | same | same |
| `headroom_cpu_optimal_point_confidence_factor` | 0/1/2/3 | same | same |
| `headroom_cpu_current_ops`, `headroom_cpu_optimal_point_ops` | ops/sec | same | same |
| `node_cpu_busy` | **0–100** | `node` only | `conf/restperf/9.12.0/system_node.yaml` |
| `node_avg_processor_busy` | **0–100** | `node` only | same |
| `node_total_latency`, `node_read_latency`, `node_write_latency` | microseconds | `node` | same |
| `node_nfs_ops`, `node_cifs_ops`, `node_iscsi_ops`, `node_nvmf_ops` | ops/sec | `node` | same |
| `nic_util_percent` | **0–1** | `nic`, `node` | `conf/restperf/9.12.0/nic_common.yaml` |
| `nic_rx_percent`, `nic_tx_percent` | **0–1** | same | same |
| `nic_ifgrp_rx_perc`, `nic_ifgrp_tx_perc` | **0–1** | `node`, **`ifgroup`**, `ports` | Nic plugin on `nic_common` |
| `fcp_util_percent` | **0–1** | `node`, `port`, `speed` | `conf/restperf/9.12.0/fcp.yaml` |
| `iscsi_lif_avg_latency` | microseconds | `lif`, `node`, `svm` | `conf/restperf/9.12.0/iscsi_lif.yaml` |

### Traps worth knowing

- **The ifgrp label is `ifgroup`, not `ifgrp`.** `ifgrp` is only the ONTAP CLI field name in
  the query; the plugin calls `SetLabel("ifgroup", ...)`.
- **`ports` on an ifgrp is a comma-joined sorted list** of member ports, e.g. `"e1a,e1b"` —
  one composite string, not one series per member. Filtering `ports!=""` is how stock Harvest
  distinguishes LAGs.
- **`ha_partner` is an instance *key*, so it lands on every `node_*` metric from the config
  collector.** `model` is an instance *label*, so it appears only on `node_labels`. Perf
  metrics carry neither and must be joined.
- **`node_cpu_busy` comes from `system_node.yaml`, whose `instance_keys` is `[node]` alone.**
  No Harvest template, in any collector or ONTAP version, puts a partner label on a perf
  metric.
- **`speed` on `nic_util_percent` does not exist** — it is an instance label on `nic_labels`,
  and the plugin rewrites it to bits per second (`"10000000000"`, not `"10000M"`).
- **`node_nfs_latency` is ambiguous.** Four templates (`nfsv3_node`, `nfsv4_node`,
  `nfsv4_1_node`, `nfsv4_2_node`) all use `object: node_nfs` with `instance_keys: [node]`,
  and all four are enabled by default, so the label set does not distinguish them. This
  dashboard uses `node_total_latency` from `system:node` instead: one unambiguous series per
  node, covering every protocol.
- **`health_ha_alerts` looks like an HA topology source but is not.** Harvest's Health plugin
  filters on `takeover_possible != true`, so the series exists *only when HA is already
  broken*. On a healthy cluster it is empty.

### What Harvest does not provide

`nic_util_percent`, `nic_ifgrp_{rx,tx}_perc` and `fcp_util_percent` are the **complete** set
of port-utilization metrics Harvest produces — there are exactly four utilization
computations in its entire plugin source.

| Wanted | Status |
| --- | --- |
| NVRAM utilization | Not collected. `node-nvram-id` and `nvram-battery-status` are commented out in the ZAPI node template. |
| HA interconnect utilization | No metric. The closest is FCVI (MetroCluster only), which yields throughput and error counters but no utilization or link speed. |
| iSCSI per-port utilization | None. Only per-LIF `iscsi_lif_*` and the aggregate `node_iscsi_ops`. |
| NVMe-TCP per-port utilization | `nvmf_tcp_port_*` exists but is commented out in both default configs, and exposes only latency, ops and data — no utilization. |
| Chassis / HA-pair identifier | No label anywhere. The pair must be derived from `ha_partner`. |
| LIF failover group | Not in Harvest. `lif_labels` has `home_node`, `home_port`, `node`, `port`, `is_home`, `services` — but no `failover_group` or `service_policy`. |

## Confirming your TSDB has what is needed

```promql
count({__name__=~"headroom_cpu_current_utilization|headroom_cpu_optimal_point_utilization|headroom_cpu_optimal_point_confidence_factor|node_cpu_busy|nic_util_percent|nic_ifgrp_rx_perc|nic_ifgrp_tx_perc|node_labels|net_port_status"}) by (__name__)
```

Expect a non-zero count for each name. If any is missing:

| Missing | Cause and consequence |
| --- | --- |
| `node_labels` or no `ha_partner` label on it | Rest config collector disabled, or Harvest older than 24.05. All pair-aware panels go blank. Check the **HA partner coverage** panel. |
| `net_port_status` | Rest collector disabled. Port filtering falls back to the `ExcludePorts` name regex — verify that regex is right for your platform. |
| `headroom_cpu_*` | `resource_headroom_cpu` not collected. The dashboard falls back to `node_cpu_busy` automatically. |
| `nic_ifgrp_*` | No ifgrps configured. Ethernet scoring still works through `nic_util_percent`. |

Stock **ONTAP: Headroom**, **ONTAP: Node** and **ONTAP: Network** are useful as a live data
check before blaming this dashboard.

## HA pair scoping

Harvest has no chassis or pair-id label, so the pair is derived from `ha_partner`.

### Step 1 — attach the partner to a perf metric

```promql
(
  node_cpu_busy{datacenter=~"$Datacenter",cluster=~"$Cluster"}
)
* on (datacenter, cluster, node) group_left (ha_partner)
(node_labels{datacenter=~"$Datacenter",cluster=~"$Cluster",ha_partner!=""} == 1)
```

`node_labels` is a 1.0 gauge, so multiplying preserves the value. `ha_partner!=""` drops
unpaired nodes, and `== 1` guards against the join changing the value.

### Step 2 — match each node against its partner

There is no way to compare two label values in PromQL, so one side is re-oriented with
`label_replace` and the two are matched on synthetic `survivor` / `victim` labels:

```promql
(
  label_replace(label_replace(<joined>, "survivor", "$1", "node",       "(.*)"),
                                        "victim",   "$1", "ha_partner", "(.*)")
)
+ on (datacenter, cluster, survivor, victim)
(
  label_replace(label_replace(<joined>, "survivor", "$1", "ha_partner", "(.*)"),
                                        "victim",   "$1", "node",       "(.*)")
)
```

For each pair this yields "the load `survivor` would carry after absorbing `victim`". One-to-one
vector matching drops every label not listed in `on()`, so the result is clean:
`{datacenter, cluster, survivor, victim}`.

On a 2-node cluster this produces exactly the same number as the old `sum by (cluster)`, so
it is a strict generalization rather than a behaviour change. Resource-scoped metrics pass
their identifying label through `on()` as well — `nic` for NICs, `ifgroup` for LAGs.

### Step 3 — roll up to the cluster

```promql
max by (datacenter, cluster) ( <pair index> )
```

The worst pair, for the reasons in [DESIGN.md](DESIGN.md#the-ha-pair-is-the-unit-of-truth).

### If you are on Harvest older than 24.05

`ha_partner` will not exist and the pair panels will be empty. Cluster-wide aggregation is
still valid for a genuine 2-node cluster:

```promql
sum by (datacenter, cluster) (node_cpu_busy{datacenter=~"$Datacenter",cluster=~"$Cluster"})
```

Do **not** use that form on a cluster with more than two nodes — it sums unrelated HA pairs
into one meaningless figure. Upgrade Harvest instead.

## PromQL patterns used, and why

### `or` is a union, not a max

This is the single most important thing to understand about the queries here, and the cause
of the worst bug in the first version.

For two vectors, `a or b` returns all of `a`, plus only those series of `b` whose label set
does not already appear in `a`. When `a` and `b` have been aggregated to the **same** label
set, `b` is discarded entirely.

```promql
# WRONG: silently returns only the CPU risk. Both sides reduce to {cluster}.
max by (cluster) ((cpu_risk) or (port_risk))
```

There are two correct uses, and the dashboard relies on both.

**As a genuine fallback**, when both sides share a label set and you want the left one to
win whenever it exists:

```promql
(
  (<PCU index>) and on (datacenter, cluster, survivor, victim) (<confidence is high enough>)
)
or
(<CPU busy index>)
```

If confidence is too low, or `headroom_cpu_*` is absent altogether, the left side is empty
and the right side takes over. This also fixes a second bug in the first version, where
missing headroom metrics produced "No data" instead of falling back.

**As a real max**, by first making the label sets distinct so the union is meaningful:

```promql
max by (datacenter, cluster, survivor, victim) (
  label_replace(<cpu index>,     "resource", "cpu",     "", "")
  or
  label_replace(<network index>, "resource", "network", "", "")
)
```

Each input appears once, so the query stays linear in size, absent inputs contribute nothing
instead of blanking the result, and the `resource` label is useful in its own right — it tells
you which resource is driving the score.

### `max(rx, tx)` without `max()`

PromQL has no elementwise two-vector max, so:

```promql
(rx + tx + abs(rx - tx)) / 2
```

### Divide by a filter, never by `clamp_min`

```promql
100
* headroom_cpu_current_utilization{...}
/ (headroom_cpu_optimal_point_utilization{...} > 0)
```

`(x > 0)` keeps only series whose value is above zero, so nodes with an unpopulated optimal
point disappear and the confidence fallback handles them. The first version used
`clamp_min(optimal, 1e-3)`, which turned a zero into a division by 0.001 — a PCU in the
millions, flowing straight into the traffic light as a fake red.

### `unless` for "fall back only if the metric is entirely absent"

Port filtering prefers `net_port_status` and falls back to a name regex only where
`net_port_status` does not exist at all:

```promql
(
  nic_util_percent{...} * 100
  * on (datacenter, cluster, node, nic) group_left ()
    label_replace(<data ports>, "nic", "$1", "port", "(.*)")
)
or (
  (nic_util_percent{...,nic!~"$ExcludePorts"} * 100)
  unless on (datacenter, cluster) <data ports>
)
```

A plain `or` would be wrong here: because the fallback's label set differs per port, `or`
would add the cluster-interconnect ports *back into* the result. `unless on (datacenter,
cluster)` drops the fallback for any cluster where `net_port_status` exists, so the two
branches are genuinely exclusive.

The known gap: if `net_port_status` exists for a cluster but a particular NIC has no matching
entry, that NIC is dropped from the score rather than included. The **Ethernet ports in
scope** panel exists so you can confirm nothing you care about is missing.

### Excluding cluster ports

```promql
(net_port_status{datacenter=~"$Datacenter",cluster=~"$Cluster",ipspace!="Cluster",port!~"$ExcludePorts"} == 1)
```

`ipspace` is present in both the REST and ZAPI templates. `role` is REST-only and, while
present, reads an ONTAP CLI field that is no longer documented in 9.14+, so ipspace is the
safer discriminator.

### The risk index

```promql
clamp_max((v) / $PcuYellow, 1)
+ clamp_min(((v) - $PcuYellow) / ($PcuRed - $PcuYellow), 0)
```

1.0 at the yellow line, 2.0 at the red line, unbounded above. Grafana substitutes the
variables as text, and PromQL folds `(200 - 160)` into a scalar.

### 95th percentile over the window

```promql
quantile_over_time(0.95, (<pair expression>)[$Window:])
```

Subquery resolution defaults to the global evaluation interval. With Harvest polling every
60s, a 30m window gives roughly 30 samples. Prefer 30m over 15m if your poll interval is
longer than 1m.

## Testing

```bash
python3 grafana/validate_promql.py /path/to/promtool   # every query parses
python3 grafana/test_promql.py     /path/to/promtool   # the maths is right
```

`validate_promql.py` wraps each query in a recording rule and runs `promtool check rules`,
which is the only PromQL parser Prometheus ships offline.

`test_promql.py` uses `promtool test rules` with synthetic Harvest-shaped series for three
fixture clusters, and asserts exact values for:

| Test | What it proves |
| --- | --- |
| Pair sums stay inside their own HA pair | A 4-node cluster with a quiet pair (30%) and a hot pair (130%) scores them separately instead of summing to 160% |
| Combined PCU is summed per pair | The PCU join and orientation swap are correct |
| Cluster interconnect and `e0M` are excluded | A saturated port in the `Cluster` ipspace cannot influence the score, and the no-`net_port` fallback still excludes `e0M` by name |
| Low headroom confidence forces the CPU busy fallback | A fixture with confidence 1 and a deliberately catastrophic PCU of 360% is correctly ignored |
| Overall pair risk is the worst of CPU and network | Regression guard for the `or`-as-max bug: one fixture is network-driven, so the buggy form would return the CPU value |
| Cluster roll-up is the worst pair | Not an average and not a cluster-wide sum |
| HA partner coverage | The coverage indicator reports 1.0 when every node is paired |

These run offline against no cluster and no server. They cannot tell you whether *your*
cluster's labels match — see [DESIGN.md](DESIGN.md#validating-against-ontap) for that.
