# Roadmap and known limits

## Hard limits — not deferred, not possible with today's Harvest

These need a new Harvest collector, a new ONTAP counter, or a different product. Do not spend
time trying to work around them in PromQL.

| Want | Why it cannot be done |
| --- | --- |
| **NVRAM pressure after takeover** | Harvest collects no NVRAM metric. `node-nvram-id` and `nvram-battery-status` are commented out in the ZAPI node template, and there is nothing in REST. |
| **HA interconnect utilization** | No metric exists. The nearest thing is FCVI (MetroCluster FC-VI), which yields throughput and error counters but no utilization and no link speed to normalize against. On A70 and A90 this is a real blind spot: cluster interconnect and HA traffic share the same physical 100GbE ports. |
| **Per-port iSCSI utilization** | Harvest exposes only per-LIF `iscsi_lif_*` and the aggregate `node_iscsi_ops`. The Ethernet ports carrying that traffic are the proxy. |
| **Per-port NVMe-TCP utilization** | `nvmf_tcp_port_*` exists but is commented out in both `restperf/default.yaml` and `zapiperf/default.yaml`, and even enabled it exposes only latency, ops and data. |
| **Estimated latency in ms/op after takeover** | AIQUM's model is proprietary. This dashboard scores capacity headroom, not predicted response time. |
| **LIF failover groups** | `lif_labels` carries `home_node`, `home_port`, `node`, `port`, `is_home` and `services`, but not `failover_group` or `service_policy`. Which ports a LIF can actually land on is therefore not derivable. |
| **A chassis or HA-pair identifier** | No such label anywhere in Harvest. The pair is derived from `ha_partner` instead, which works but means the pair has no stable name of its own. |

## Deferred — feasible, not yet built

### Pair-scoped model mismatch detection

Today the **Distinct node models** stat is cluster-scoped, because PromQL cannot compare two
label values. On a 2-node cluster that is exactly equivalent. On a larger mixed-model cluster
it tells you *something* is heterogeneous but not *which pair*.

Doing this properly needs a numeric per-model capacity weight — either a recording rule
mapping `model` to a relative capacity figure, or a small static series scraped alongside
Harvest. That would also let the score handle asymmetric pairs correctly:

$$\text{load on survivor} = \text{PCU}_{\text{survivor}} + \text{PCU}_{\text{victim}} \times \frac{\text{capacity}_{\text{victim}}}{\text{capacity}_{\text{survivor}}}$$

Low priority while every HA pair is same-model, which ONTAP requires.

### Fleet overview

A table with one row per cluster, sorted worst-first, as the landing view for someone
watching several 2-node clusters at once. Today the headline stat repeats per cluster when
`$Cluster` is multi-selected, which works but does not sort by risk.

### Alert rules

Grafana or Prometheus alerts on the risk index crossing 1 (yellow) or 2 (red), sustained over
the percentile window. The index is already designed for this: a single threshold pair covers
every resource.

Worth pairing with an inhibition rule so a cluster that is *already* in trouble does not also
alert on takeover headroom.

### Optional recording rules

The pair join makes queries long — around 10 KB for the headline panel. This is fine for
Prometheus and Grafana, and was chosen deliberately so the dashboard needs nothing outside
Grafana. If your TSDB config is yours to change, recording rules that materialize
`pair:cpu_index` and `pair:port_index` would make the dashboard queries trivial and cheaper to
evaluate, at the cost of a deployment dependency.

### Latency gating on the traffic light

Currently latency is context only: the score can be green while the cluster is already
suffering. An optional gate could downgrade green to yellow when current latency exceeds a
threshold. Kept out for now because it conflates two questions — "is the cluster healthy?" and
"can it survive a node loss?" — and the second is what this dashboard is for.

### FC support

Written and switched off behind `INCLUDE_FC` in the generator, since this customer runs NAS
plus iSCSI/NVMe-TCP. Before enabling it, note that `fcp_util_percent` carries a `speed` label,
so summing utilization across ports of differing speed is not meaningful. A correct version
would weight by speed or compare throughput against remaining port capacity rather than
summing percentages.

### Aggregate headroom

`resource_headroom_aggr` is collected by Harvest and deliberately unused here, because disk is
not the binding constraint on all-flash. Would need to be added if this dashboard is ever
pointed at a hybrid or FAS cluster.

### MetroCluster

Out of scope. Switchover is a different failure model from HA takeover: the surviving *site*
absorbs the load, and `metrocluster_check_*` metrics report check verdicts rather than
capacity. This would be a separate dashboard, not a variant of this one.
