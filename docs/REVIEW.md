# Review of the first version

Findings from reviewing the initial dashboard against Harvest's actual templates and plugin
source. The concept and the metric selection held up well; the defects were in the PromQL
and in a few assumptions that were never verified.

Every claim below was checked against the Harvest repository or NetApp documentation, not
inferred. Everything listed as fixed is covered by a test in `grafana/test_promql.py`.

---

## Defects

### 1. The overall traffic light silently ignored port risk — fixed

```python
OVERALL = f"max by (cluster) (({CPU_RISK}) or ({PORT_RISK}))"
```

`or` is a union, not a max. For two vectors it returns all of the left side plus only those
right-side series whose label set does not already appear on the left. `CPU_RISK` and
`PORT_RISK` both reduced to the identical label set `{cluster}`, so `PORT_RISK` was discarded
on every evaluation. **The headline panel was CPU risk alone**, and no amount of network
saturation could change its colour.

Fixed by stamping a distinguishing `resource` label before the union so `max by` has
something to choose between. Regression-guarded by the "overall pair risk is the worst of CPU
and network" test, whose third fixture is network-driven and would return the CPU value under
the old form.

### 2. The same bug hid ifgrp and FC from the port score — fixed

```python
PORT_HOT = f"""max by (cluster) (
  ({NIC_HOT_P95})
  or ({IFGRP_HOT_P95})
  or ({FCP_HOT_P95})
)"""
```

All three reduced to `{cluster}`, so only the NIC term survived. A saturated LAG or FC target
port could not turn the light yellow, let alone red. Fixed the same way.

### 3. `clamp_min` on the optimal point manufactured fake reds — fixed

```python
/ clamp_min(headroom_cpu_optimal_point_utilization{...}, 1e-3)
```

When ONTAP has not populated the optimal point — confidence 0, or
`optimal_point.samples == 0` — the metric is 0, and this turned the division into
`current × 100 / 0.001`, producing a PCU in the millions. The confidence gate was supposed to
catch that, but it was a separate multiplied term, so the garbage value still flowed into the
`PCU_P95` traffic-light input and into both PCU panels.

Replaced with `/ (headroom_cpu_optimal_point_utilization > 0)`, which drops the series so the
fallback handles it.

### 4. Missing headroom metrics produced "No data" instead of falling back — fixed

`CPU_RISK` gated the two models with multiplied booleans:

```python
(({CONF_MIN}) >= bool 2) * clamp_max(...) + (({CONF_MIN}) < bool 2) * clamp_max(...)
```

If `headroom_cpu_*` was absent entirely, `CONF_MIN` was empty, so *both* terms were empty and
the whole expression returned nothing — the panel showed "No data" rather than the
`node_cpu_busy` fallback it was designed to have.

Restructured as `(<PCU index> and <confidence high enough>) or <busy index>`, which is the one
place `or` is the correct operator: both branches share a label set, so it behaves as a true
fallback and covers low confidence and absent metrics in a single expression.

### 5. `datacenter` was dropped from every aggregation — fixed

Everything aggregated `by (cluster)`. Two clusters with the same name in different
datacenters would have merged silently. All aggregations now carry `datacenter`.

### 6. Cluster-wide sums broke on anything larger than 2 nodes — fixed

`sum by (cluster) (node_cpu_busy)` is correct only when the cluster is a single HA pair. On a
4-node cluster it adds four unrelated nodes together. See item 9.

---

## Incorrect assumptions

### 7. The default port-exclude regex was wrong for this customer's hardware — fixed

`ExcludePorts` defaulted to `e0a|e0b|e0M`. Per NetApp's platform cluster-port reference, the
default cluster ports are `e4a`/`e4b` on A30 and `e1a`/`e7a` on A70 and A90. `e0a`/`e0b` are
only cluster ports on older FAS/AFF platforms; on an A70 or A90 they are typically NS224
shelf-attach ports.

So the old default did two harmful things at once: excluded data ports that should have been
scored, and left the real 100GbE cluster interconnect *in* the score, where its traffic could
turn the light red for no reason.

Replaced with filtering on `ipspace != "Cluster"` via `net_port_status`, which is stable
across platforms and present in both the REST and ZAPI Harvest templates. `ExcludePorts`
remains as a name-based safety net, now defaulting to `e0M`.

### 8. "No new Harvest collectors" was true but incomplete — documented

Correct that no custom templates are needed, but the original docs implied a perf collector
alone was sufficient. A **config collector** is also required, for `node_labels` (HA partner
and model) and `net_port_status` (ipspace and role). Both `node.yaml` and `netport.yaml` are
enabled by default, so this is a matter of confirming the collector runs, not of writing
anything.

Requirements are now stated explicitly in the README, including the Harvest 24.05 floor for
`ha_partner`.

> **Later correction.** The fix for this initially over-specified the requirement as `Rest`
> and `RestPerf`. Either the REST or the ZAPI collector family works, and the A220 lab
> proved it by running entirely on ZAPI. See
> [LAB-PLAN.md](../LAB-PLAN.md#what-the-first-lab-run-found).

### 9. HA-pair support was parked as possibly infeasible — implemented

The original plan said pair-level detail might have to be deferred if Harvest exposed no HA
topology. It does expose it: `conf/rest/9.12.0/node.yaml` maps
`^ha.partners.0.name => ha_partner`, added in **Harvest 24.05.0** (PR #2723). It is an
instance *key*, so it appears on every `node_*` metric from the config collector. The ZAPI
collector gets the same label from a plugin calling `system-get-node-info-iter`.

Perf metrics come from a different template with `instance_keys: [node]` and carry no partner
label, so each is joined to `node_labels` first. There is no chassis or pair-id label, so the
pair itself is formed by re-orienting one side with `label_replace` and matching on synthetic
`survivor`/`victim` labels. Mechanics in [METRICS.md](METRICS.md#ha-pair-scoping).

Scoring is now per pair everywhere, with the cluster as the worst pair. On a 2-node cluster
this returns exactly the number the old cluster-wide sum did, so it is a generalization rather
than a behaviour change.

One dead end worth recording: `health_ha_alerts` looks like an HA topology source and does
carry `partner` and `takeover_possible` labels, but Harvest's Health plugin filters on
`takeover_possible != true`. The series therefore exists *only when HA is already broken*, and
is empty on a healthy cluster.

### 10. `node_nfs_latency` is ambiguous — replaced

Four templates (`nfsv3_node`, `nfsv4_node`, `nfsv4_1_node`, `nfsv4_2_node`) all declare
`object: node_nfs` with `instance_keys: [node]`, and all four are enabled by default. Nothing
in the label set distinguishes them.

The context row now uses `node_total_latency` from `system:node`: one unambiguous series per
node, covering all protocols, which is also more useful for a mixed NAS and iSCSI environment.

### 11. Metric scales — verified, no change needed

The original doc's scale handling was right, and worth recording so nobody "fixes" it later:

| Metric | Scale | Original handling |
| --- | --- | --- |
| `nic_util_percent`, `nic_ifgrp_{rx,tx}_perc`, `fcp_util_percent` | 0–1 | `* 100` — correct |
| `headroom_cpu_*_utilization` | 0–100 | no scaling — correct |
| `node_cpu_busy`, `node_avg_processor_busy` | 0–100 | no scaling — correct |

The ifgrp label name (`ifgroup`) and the `ports!=""` LAG filter were also correct.

### 12. FC target-port sums have a flaw worth stating — documented, FC now off

`fcp_util_percent` carries a `speed` label, so summing utilization percentages across ports of
different speeds is not meaningful. Combined with the existing multipath caveat — hosts
already send I/O to both nodes, so the sum overstates post-failover load — FC deserves to be
labelled an upper bound rather than an estimate.

FC is switched off via `INCLUDE_FC` in the generator, since this customer runs NAS plus
iSCSI/NVMe-TCP. The panels remain in the code for when FC is added.

---

## Gaps between the documentation and the dashboard

### 13. The promised maintenance-window view did not exist — added

The design doc specified a "sparkline of last 24h 95th percentile (answers 'is tonight a good
maintenance window?')". No such panel was in the JSON.

Added as a 7-day **state timeline** of the risk index, which answers the question better than
a sparkline: green bands are directly readable as safe windows. It pins its own time range and
ignores the dashboard time picker.

### 14. The confidence-1 case was underplayed — reframed

The docs treated `node_cpu_busy` as a footnote fallback. NetApp documents confidence 1 as
ONTAP using a **seed value** because it has not gathered enough data to predict the optimal
point — expected behaviour on lightly loaded or recently rebooted nodes, not a fault. On a
healthy, comfortably sized 2-node cluster this is likely to be the normal state.

So `node_cpu_busy` may be the model that actually drives the traffic light day to day. It now
has its own headline stat panel and equal billing in the documentation, and the dashboard
shows which model is in play.

### 15. Mixed node models were unaddressed — flagged

The customer runs A90, A70 and A30. Summing normalized percentages assumes both nodes have
equal capacity — true within a real HA pair, which must be same-model, but worth verifying
rather than assuming. Node model now appears in the HA pair table, and a **Distinct node
models** stat flags any cluster that is not uniform.

PromQL cannot compare two label values, so this check is cluster-scoped rather than
pair-scoped. For 2-node clusters those are the same thing.

### 16. Unmodelled resources were not stated as limits — documented

Harvest produces exactly four port-utilization computations in its entire plugin source, and
nothing at all for NVRAM or the HA interconnect. Notably, on A70 and A90 the cluster
interconnect and HA traffic share the same physical 100GbE ports, so HA interconnect load is
genuinely invisible.

There is also no per-port utilization for iSCSI or NVMe-TCP. The original doc's line that
iSCSI "uses the Ethernet/ifgrp numbers" implied a modelling choice; it is the only option
available, and is now stated as a limitation. Full inventory in
[METRICS.md](METRICS.md#what-harvest-does-not-provide).

---

## What was already good

- The metric selection was right, including the preference for `node_cpu_busy` over
  `node_avg_processor_busy` and for ifgrps over summing physical NICs.
- Anchoring the CPU thresholds on AIQUM's 200% "HA pair over-utilized" line, with a
  conservative 160% green ceiling for an operations audience.
- Using a 95th percentile over 15–30 minutes so spikes do not flip the light, matching
  AIQUM's own ~30-minute sample requirement.
- The `(rx + tx + abs(rx - tx)) / 2` trick for `max(rx, tx)`, and the reasoning that a
  full-duplex link should not be double-counted.
- Excluding aggregate and disk headroom deliberately for all-flash.
- Being explicit that this is not a substitute for AIQUM's estimated ms/op after takeover.
- Generating the JSON from a Python script rather than hand-editing it. The committed JSON was
  byte-identical to the generator's output, which made the review straightforward.

## Additions

- **Offline test suites.** `validate_promql.py` parse-checks every query via
  `promtool check rules`. `test_promql.py` builds synthetic Harvest-shaped series for three
  fixture clusters and asserts exact values through `promtool test rules`, covering pair
  isolation, the cluster roll-up, the confidence gate and port filtering. Both run with no
  cluster and no server.
- **Thresholds as variables** (`PcuYellow`, `PcuRed`, `BusyYellow`, `BusyRed`, `PortYellow`,
  `PortRed`, `ConfMin`), hidden by default and overridable by URL parameter.
- **A continuous risk index** where 1.0 is the yellow line and 2.0 the red line, so resources
  with different native scales share one threshold pair and you can see how far past a line
  something is.
- **Model self-diagnostics**: HA partner coverage, headroom confidence, distinct node models,
  and a table of exactly which Ethernet ports are in scope. These turn a misconfiguration into
  something visible rather than a silently wrong colour.
