# Lab validation plan — AFF A220

A test plan for standing up the HA Headroom dashboard against a 2-node AFF A220 lab and
proving it behaves correctly, including during a real takeover.

For generic install steps see [docs/INSTALL.md](docs/INSTALL.md). This document covers
what is specific to an A220 lab and the test cases worth running.

---

## What you are validating

| Goal | How |
| --- | --- |
| The stack collects the right metrics | Phase 3 verification queries |
| HA pair detection works | TC-01 to TC-03 |
| Port filtering excludes the cluster interconnect | TC-04, TC-05 |
| The traffic light transitions green to yellow to red | TC-06 to TC-08 |
| The CPU model switches correctly on confidence | TC-09, TC-10 |
| Behaviour during a real takeover is understood | TC-11 to TC-13 |
| Numbers match ONTAP | TC-14 |

---

## Phase 0 — Lab environment

### A220 platform facts that shape this plan

| Ports | Role | Notes |
| --- | --- | --- |
| `e0a`, `e0b` | Cluster interconnect, 10GbE | Excluded by the `ipspace != "Cluster"` filter |
| `e0c`, `e0d`, `e0e`, `e0f` | UTA2/CNA data ports | **Either** 10GbE **or** 16Gb FC — see the warning below |
| `e0M` | Node management, 1GbE | Excluded by the `ExcludePorts` default |
| `0a`, `0b` | Onboard SAS | Not relevant |

The A220 has **no expansion slots**, so the port set is fixed. You will not see `e1a`-style
names on this platform.

> **Check your UTA2 personality before you start.** The four UTA2 ports are paired per ASIC
> (`e0c`/`e0d` on one, `e0e`/`e0f` on the other) and both ports on an ASIC must share a
> personality. If they are in FC mode they appear as `0c`/`0d` and there will be **no Ethernet
> data ports at all** — the network half of the score will be permanently blank. In that case
> either flip a pair to CNA/10GbE mode, or set `INCLUDE_FC = True` in
> [grafana/generate_ha_headroom.py](grafana/generate_ha_headroom.py) and regenerate.
>
> ```
> ucadmin show
> ```

Also note: your lab probably has **no ifgrps**. That is fine — the ifgrp panels will be empty
and scoring falls back to physical NICs via `nic_util_percent`. Record it as expected rather
than as a failure.

### ONTAP version

The A220 was introduced at ONTAP 9.4, and does not appear on NetApp's
[hardware no longer supported](https://docs.netapp.com/us-en/ontap-systems/eoa-hardware.html)
list through 9.19.1. Anything 9.8 or newer gives you the REST API this dashboard needs.

```
version
cluster image show
```

### Linux VM sizing

One VM runs Harvest, Prometheus and Grafana comfortably for a single 2-node cluster.

| Resource | Minimum | Comfortable |
| --- | --- | --- |
| vCPU | 2 | 4 |
| RAM | 4 GB | 8 GB |
| Disk | 20 GB | 50 GB (Prometheus retention) |
| OS | Any modern Linux (RHEL/Rocky 8+, Ubuntu 22.04+) | |

### Network access required

| From | To | Port | Purpose |
| --- | --- | --- | --- |
| VM | Cluster management LIF | 443 | Harvest REST polling |
| Your workstation | VM | 3000 | Grafana UI |
| Your workstation | VM | 9090 | Prometheus UI (verification queries) |
| Prometheus (on VM) | Harvest (on VM) | 12990 | Scrape |

---

## Phase 1 — Prepare ONTAP

### 1.1 Confirm cluster health and HA configuration

```
cluster show
storage failover show
cluster ha show
network options switchless-cluster show     # advanced privilege
```

Expected: both nodes healthy, `Takeover Possible: true`, and for a 2-node cluster
`cluster ha` configured `true`.

> In a 2-node cluster with cluster HA enabled there is no epsilon. Halting a node with
> `-inhibit-takeover true` will stop **both** nodes serving data. Do not use that flag during
> these tests.

### 1.2 Record the port layout

```
network port show -role cluster
network port show -role data
network port show -fields node,port,ipspace,broadcast-domain,speed,health-status
```

Write down which ports are cluster and which are data. You will compare this against the
**Ethernet ports in scope** panel in TC-04.

### 1.3 Create the Harvest user

```
security login role create -role harvest -cmddirname "DEFAULT" -access readonly
security login create -user-or-group-name harvest -authmethod password -role harvest -application ontapi
security login create -user-or-group-name harvest -authmethod password -role harvest -application http
```

---

## Phase 2 — Install the stack

Full detail is in [docs/INSTALL.md](docs/INSTALL.md). The lab-specific essentials:

### 2.1 Harvest

Install Harvest **24.05 or newer** (for the `ha_partner` label). If you have a choice, take
**25.11 or newer** — it adds `allow_partial_aggregation` to `system_node.yaml`, which
materially changes the outcome of TC-12.

```bash
bin/harvest version     # must be >= 24.05
```

`harvest.yml` for this lab:

```yaml
Exporters:
  prometheus:
    exporter: Prometheus
    local_http_addr: 0.0.0.0
    port: 12990

Defaults:
  collectors:
    - Rest
    - RestPerf
  use_insecure_tls: true      # lab only, if the cluster uses a self-signed cert

Pollers:
  a220-lab:
    datacenter: LAB
    addr: <cluster-mgmt-ip>
    auth_style: basic_auth
    username: harvest
    password: <password>
    exporters:
      - prometheus
```

A config collector and a perf collector are required, but **either the REST or the ZAPI
family works** — Harvest ships matching templates that export identical metric names. The
list above picks REST because ONTAP 9.14.1+ disables unused ZAPIs 30 days after an upgrade,
so REST is the better long-term default. Harvest's shipped `Defaults` enables all of
`Zapi, ZapiPerf, Ems, Rest, RestPerf`, which also works fine and is what the validated lab
actually ran — see [What the first lab run found](#what-the-first-lab-run-found).

```bash
bin/harvest start
bin/harvest status
curl -s localhost:12990/metrics | head
```

### 2.2 Prometheus

```yaml
scrape_configs:
  - job_name: harvest
    static_configs:
      - targets: ['localhost:12990']
```

### 2.3 Grafana

Install, add the Prometheus datasource pointing at `http://localhost:9090`, then import
[grafana/dashboards/cmode/ha_headroom.json](grafana/dashboards/cmode/ha_headroom.json).

---

## Phase 3 — Verify collection

Wait 5 minutes after starting Harvest, then run these in the Prometheus UI.

```promql
count({__name__=~"headroom_cpu_current_utilization|headroom_cpu_optimal_point_utilization|headroom_cpu_optimal_point_confidence_factor|node_cpu_busy|node_labels|nic_util_percent|net_port_status"}) by (__name__)
```

Every name must return a non-zero count. Then confirm HA topology specifically:

```promql
count(node_labels{ha_partner!=""}) by (cluster, node, ha_partner)
```

Expected: exactly two rows, each node naming the other as partner.

> **Timing.** `Window` defaults to `30m`, so the p95 stat panels need 30 minutes of history
> before they read correctly. The 7-day state timeline will stay mostly empty until the lab
> has been running a week. Neither is a fault.

---

## Phase 4 — Functional test cases

Record results in the table at the end of this document.

### TC-01 — HA partner coverage

| | |
| --- | --- |
| **Steps** | Open the dashboard, set Datacenter and Cluster. Read the **HA partner coverage** panel. |
| **Expected** | `1.0`, displayed as "All nodes paired", green. |
| **Fails if** | Below 1.0 — the Rest collector is not running, or Harvest is older than 24.05. |

### TC-02 — Pair table is populated

| | |
| --- | --- |
| **Steps** | Read the **HA pairs, models and partner health** table. |
| **Expected** | Two rows. `ha_partner` populated on both. `model` reads `AFF-A220`. `healthy` and `partner_healthy` both true. |

### TC-03 — Pair scoring produces both directions

| | |
| --- | --- |
| **Steps** | Read the **Takeover risk per surviving node** panel. |
| **Expected** | Two tiles, "node1 survives" and "node2 survives", with **identical values** — each direction evaluates to the same pair sum. |
| **Note** | Identical values are correct, not a bug. The tiles are two views of one pair, which is why the panel is labelled per surviving node rather than per direction. |

### TC-04 — Cluster ports are excluded

| | |
| --- | --- |
| **Steps** | Read the **Ethernet ports in scope** table. Compare against the `network port show` output from step 1.2. |
| **Expected** | Data ports (`e0c`–`e0f` in CNA mode) present. `e0a` and `e0b` **absent**. `e0M` absent. |
| **Why this matters here** | On A220 the cluster ports happen to be `e0a`/`e0b`, which the *old* buggy name regex would also have caught. This test confirms the new `ipspace != "Cluster"` mechanism is doing the work, not coincidence. |

### TC-05 — Cluster interconnect cannot influence the score

| | |
| --- | --- |
| **Steps** | Note the value in **Hottest survivor port (p95)**. Then in Prometheus compare: `nic_util_percent{nic=~"e0a\|e0b"} * 100` against `nic_util_percent{nic=~"e0c\|e0d"} * 100`. |
| **Expected** | The dashboard number tracks the data ports only. Cluster-port utilization is not reflected. |

### TC-06 — Baseline is green

| | |
| --- | --- |
| **Steps** | With the lab idle, read **Can this cluster survive a node loss?** |
| **Expected** | "OK", green. Risk index below 1.0. |

### TC-07 — Force yellow

| | |
| --- | --- |
| **Steps** | Open the dashboard with lowered CPU thresholds. Read the current combined CPU busy value first, then set yellow just below it. |
| **URL** | `/d/cdot-ha-headroom/?var-BusyYellow=5&var-BusyRed=50` |
| **Expected** | Headline panel turns **yellow**, reading "Latency risk". **Risk index by resource** shows `cpu` between 1.0 and 2.0. |

### TC-08 — Force red

| | |
| --- | --- |
| **Steps** | Lower the thresholds further. |
| **URL** | `/d/cdot-ha-headroom/?var-BusyYellow=1&var-BusyRed=2` |
| **Expected** | Headline panel turns **red**, reading "Service impact risk". Index above 2.0. |
| **Purpose** | Proves the full colour range and the risk-index maths without needing real load. |

### TC-09 — Confidence factor reads 1 on an idle lab

| | |
| --- | --- |
| **Steps** | Read **Headroom confidence (worst node)** and the **Headroom confidence factor** timeseries. |
| **Expected** | `1`, coloured red. This is **correct and expected**, not a fault. ONTAP reports 1 when it is using a seed value because it has not observed enough load to model the latency curve. |
| **Corroborate** | `statistics show -object resource_headroom_cpu -instance CPU_*` — an idle node typically shows `current_utilization 0` with a non-zero `optimal_point_utilization` (the seed) and `confidence_factor 1`. |

### TC-10 — The CPU model switches on confidence

| | |
| --- | --- |
| **Steps** | With confidence at 1, confirm the score is driven by CPU busy. Then reload with `?var-ConfMin=1` to force the PCU path. |
| **Expected** | With `ConfMin=2` (default): **Combined CPU busy after takeover** drives the score. With `ConfMin=1`: **Combined PCU after takeover** drives it instead, and the value changes. |
| **Purpose** | Exercises the `(PCU and confidence_ok) or busy` fallback, which is the single most important piece of conditional logic in the dashboard. |

---

## Phase 5 — Load generation (optional)

There is **no built-in load generator in ONTAP 9.x**. `sysstat`, `statit` and `statistics`
measure; they do not generate. You need a client.

Simplest approach — `fio` from a Linux client against an NFS export:

```bash
mount -t nfs <data-lif>:/vol_test /mnt/test
fio --name=load --directory=/mnt/test --rw=randread --bs=4k \
    --iodepth=64 --numjobs=8 --size=2G --runtime=1800 --time_based --direct=1
```

Watch controller-side CPU per domain:

```
node run -node <node> sysstat -M 1
```

**Set expectations.** The A220 is a 12-core Broadwell-DE with 64 GB. A single client will
usually bottleneck on the network before the controller saturates. Small-block random reads
at high concurrency from multiple clients across several volumes is what actually moves the
needle.

**Do not expect the confidence factor to climb during a short run.** ONTAP builds the optimal
point from one-minute samples since boot plus hourly/daily/weekly/monthly EWMAs, and
confidence 3 requires observations that actually reach or exceed the optimal point. A 30-minute
fio burst will move `current_utilization` but will very likely leave confidence at 1. Treat
reaching confidence 2 as a stretch goal, not a pass criterion.

---

## Phase 6 — Takeover test

This is the most interesting phase, and partly exploratory. Some expected results below are
marked **measure and record** because they are not documented anywhere I could find.

### Before you start

Capture a baseline so you can diff against it:

```bash
# From the VM, snapshot the raw metrics
curl -s localhost:12990/metrics | grep -E '^(node_cpu_busy|headroom_cpu_current_utilization|node_labels)' > /tmp/before.txt
```

```
storage failover show
```

### TC-11 — Node inventory survives takeover

| | |
| --- | --- |
| **Steps** | `storage failover takeover -ofnode <node2>` then watch `storage failover show-takeover`. Once takeover completes, check the dashboard. |
| **Expected** | `api/cluster/nodes` continues to list node2 with state `taken_over` or `waiting_for_giveback`. The **HA pairs, models and partner health** table still shows both nodes. **HA partner coverage** stays at `1.0`. |
| **Why** | The taken-over node's state is reported by its HA partner, so the config collector keeps seeing it. |

### TC-12 — `node_cpu_busy` behaviour during takeover

| | |
| --- | --- |
| **Background** | `system:node` is an *aggregated* counter table. NetApp/Harvest issues [#3731](https://github.com/NetApp/harvest/issues/3731) and [#2769](https://github.com/NetApp/harvest/issues/2769) document that when one node is down, ONTAP sets `aggregation.complete = false` for **all** rows including the healthy node's. Harvest 25.11 added `allow_partial_aggregation: true` to work around it. |
| **Steps** | During takeover, query `node_cpu_busy` in Prometheus. |
| **Expected, Harvest < 25.11** | `node_cpu_busy` likely goes blank for **both** nodes, not just the down one. |
| **Expected, Harvest >= 25.11** | The surviving node continues to report. |
| **Record** | Which behaviour you see, and your exact Harvest version. |

### TC-13 — Dashboard behaviour during takeover

| | |
| --- | --- |
| **Steps** | With node2 taken over, observe the headline panel and the pair panels. |
| **Expected** | The **pair series disappears**, because the pair sum requires a matching series from both nodes. Panels go to "No data". |
| **Is this correct?** | Arguably yes — "can the survivor absorb its partner" is moot once the takeover has already happened. But it should be a deliberate, documented behaviour rather than a surprise. Record what you see so we can decide whether to add an explicit "takeover in progress" indicator. |
| **Also check** | Does `headroom_cpu` for the down node vanish, or persist stale? `headroom_cpu` is *not* an aggregated table, so it should be unaffected by the partial-aggregation issue — but whether the down node's row disappears is undocumented. Query `/api/cluster/counter/tables/headroom_cpu/rows` directly before, during and after. |

### Giveback

```
storage failover giveback -ofnode <node2>
storage failover show-giveback
```

Confirm the dashboard returns to its pre-test state within one or two poll cycles
(Rest 3m, RestPerf 1m).

---

## Phase 7 — Validate the numbers against ONTAP

### TC-14 — Cross-check against `statistics show`

| | |
| --- | --- |
| **Steps** | Pick a moment with some load. In ONTAP: `statistics show -object resource_headroom_cpu`. Note `current_utilization` and `optimal_point_utilization` per node. Compute PCU for each as `100 * current / optimal`, then add them. |
| **Expected** | Your hand-computed sum matches **Combined PCU per HA pair** on the dashboard, allowing for the p95 window smoothing. Compare against the raw (non-p95) panel for a closer match. |
| **Also** | `statistics show -object system:node -counter cpu_busy` against the **node_cpu_busy per node and per HA pair** panel. |

---

## Known lab deviations — expect these, they are not bugs

| Observation | Why |
| --- | --- |
| Headroom confidence stuck at 1 | Idle system; ONTAP is using a seed optimal point |
| ifgrp panels empty | No ifgrps configured in the lab |
| 7-day timeline mostly blank | Needs a week of history |
| p95 panels odd for the first 30 minutes | `Window` is 30m |
| **Distinct node models** reads 1 | Correct — a uniform A220 pair |
| iSCSI LIF latency panel empty | No iSCSI configured |
| Traffic light green no matter what you do | An idle A220 genuinely has enormous headroom. Use the threshold overrides in TC-07/TC-08. |

---

## What the first lab run found

First run against real hardware, after a week of collection on the `orion` A220 pair.
All 33 panel queries were executed against Prometheus with default variables: **31 returned
data, 0 errored.** Phases 0–4 pass. The takeover phase (TC-11 to TC-14) is still to do.

Three findings worth carrying forward:

**Either collector family works.** This lab runs Harvest's shipped default, which lists
`Zapi, ZapiPerf, Ems, Rest, RestPerf`. Because Harvest resolves overlap by the order
collectors appear, ZAPI serves every object this dashboard uses and `RestPerf` produces
nothing at all. Everything still works, including `ha_partner` and `net_port_status`.
Earlier drafts of these docs claimed `Rest`/`RestPerf` were mandatory. That was wrong, and
the requirement has been corrected everywhere.

**The confidence fallback is provably live.** With confidence pinned at 1, the cluster
score of 0.2053 equals combined CPU busy 14.37 ÷ `BusyYellow` 70 — the busy path. Forcing
`?var-ConfMin=1` moves it to 0.1458, which is combined PCU 23.34 ÷ `PcuYellow` 160 — the PCU
path. The two paths produce visibly different numbers, so the `(PCU and confidence_ok) or
busy` branch is doing real work rather than silently collapsing to one side.

**The empty panels are both correct.** "Available CPU ops" is gated on confidence > 1, and
`max_over_time(...[7d])` confirms confidence never left 1 all week. "iSCSI LIF latency" is
empty because the lab has no iSCSI at all — zero `iscsi_lif_*` series exist.

---

## Results

Phase 0–4 results below were captured by querying Prometheus directly through the Grafana
datasource proxy, running each panel's own query with the dashboard's default variables.

| Test | Result | Notes |
| --- | --- | --- |
| TC-01 HA partner coverage | **Pass** | `1.0`. `ha_partner` present on both nodes, supplied by the **Zapi** collector |
| TC-02 Pair table populated | **Pass** | 2 rows, model `AFF-A220`, `healthy` and `partner_healthy` both true |
| TC-03 Both pair directions | **Pass** | `orion-01 absorbs orion-02` and the reverse, both 0.2053 |
| TC-04 Cluster ports excluded | **Pass** | 24 ports total → 4 dropped as `ipspace="Cluster"`, 2 as `e0M`, 18 in scope, 14 of them up |
| TC-05 Interconnect cannot score | **Pass** | NIC panels return 4 series (`e0c`/`e0d` × 2 nodes); `e0a`/`e0b` absent despite running *hotter* (0.021–0.023) than `e0c` (0.003) |
| TC-06 Baseline green | **Pass** | Risk index 0.2053, well below 1.0 |
| TC-07 Force yellow | **Pass** | `?var-BusyYellow=5&var-BusyRed=50` → 1.2082, inside the yellow band |
| TC-08 Force red | **Pass** | `?var-BusyYellow=1&var-BusyRed=2` → 14.3686, above the red line |
| TC-09 Confidence reads 1 | **Pass** | 1 on both nodes; `max_over_time([7d])` also 1, so never higher all week |
| TC-10 Model switches on confidence | **Pass** | Default 0.2053 (busy path) vs `?var-ConfMin=1` 0.1458 (PCU path) |
| TC-11 Inventory survives takeover | Not yet run | |
| TC-12 `node_cpu_busy` during takeover | Not yet run | Harvest 26.08.0 ships `allow_partial_aggregation: true` in `conf/zapiperf/cdot/9.15.1/system_node.yaml`, so the survivor is **expected to keep reporting** |
| TC-13 Dashboard during takeover | Not yet run | |
| TC-14 Numbers match ONTAP | Not yet run | |

### Environment recorded

| Item | Value |
| --- | --- |
| Cluster / datacenter | `orion` / `SWELAB`, nodes `orion-01` and `orion-02` |
| Platform | AFF-A220 pair |
| ONTAP version | 9.19.1 |
| Harvest version | 26.08.0 |
| Collectors actually serving data | `Zapi` and `ZapiPerf` (`RestPerf` idle — ordering, see above) |
| Prometheus version | 3.14.0 |
| Grafana version | 12.4.9 |
| UTA2 personality (`ucadmin show`) | |
| Cluster ports | `e0a`, `e0b` — confirmed via `ipspace="Cluster"`, 4 series across the pair |
| Data ports | 18 in scope, 14 up; `e0M` excluded by the default `ExcludePorts` |
| Switchless? | |

Two rows are left blank because they need an ONTAP CLI session rather than metrics.
