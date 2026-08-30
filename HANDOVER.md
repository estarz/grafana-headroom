# Handover notes

Repository: `github.com/estarz/grafana-headroom`, branch `main`.

## What this repo is

A Grafana dashboard that scores ONTAP HA takeover headroom — green/yellow/red — using
metrics from NetApp Harvest (no custom collectors needed). Built for a customer running AFF
A90, A70 and A30 nodes in 2-node clusters, with NAS + iSCSI/NVMe-TCP. Supports larger
clusters via per-HA-pair scoring.

## Current state

The dashboard is working and tested. The generator is `grafana/generate_ha_headroom.py`;
the committed JSON `grafana/dashboards/cmode/ha_headroom.json` is its output — never
hand-edit the JSON. Two offline test scripts exist: `grafana/validate_promql.py` (parse
check via promtool) and `grafana/test_promql.py` (7 semantic tests with synthetic Harvest
series, all passing). Docs are in `docs/` — read `docs/REVIEW.md` first for context on
what was wrong in v1 and why things are the way they are.

## Key technical constraints — read before changing anything

**Harvest collectors.** Harvest's default `harvest.yml` uses `Zapi`/`ZapiPerf` only. Both
`Rest` and `RestPerf` collectors must be enabled. `Rest` provides `node_labels{ha_partner=...}`
(Harvest ≥ 24.05) and `net_port_status{ipspace=...}`. Without them the pair scores go blank.

**Metric scales.** Easy to get wrong:

| Metric | Scale |
| --- | --- |
| `nic_util_percent`, `nic_ifgrp_rx_perc`, `nic_ifgrp_tx_perc`, `fcp_util_percent` | **0–1** — queries multiply by 100 |
| `headroom_cpu_*_utilization`, `node_cpu_busy`, `node_avg_processor_busy` | **0–100** — do not scale |

**Label names.** The ifgrp label is `ifgroup` (not `ifgrp`). `ports` on an ifgrp is a
comma-joined string of member ports, not one series per member.

**Port filtering.** Uses `ipspace != "Cluster"` via `net_port_status`, not a name regex.
Cluster ports are `e4a`/`e4b` on A30 and `e1a`/`e7a` on A70/A90 — a name regex cannot
cover all platforms cleanly.

**`or` is a union, not a max in PromQL.** When two sides share a label set, only the left
survives. The generator stamps a `resource` label before unions to make them real. This is
the most important correctness property in the codebase — see `docs/REVIEW.md §1–2` for
what went wrong before.

**The risk index.** Maps `1.0 = yellow line, 2.0 = red line` so resources with different
native scales (PCU uses 160/200, ports use 70/85) share one threshold pair. Thresholds are
Grafana variables, hidden by default, overridable by URL parameter.

**Headroom confidence 1 is normal.** ONTAP reports confidence 1 when using a seed value —
expected on lightly loaded or recently rebooted nodes. The dashboard falls back to
`node_cpu_busy` automatically via `(PCU_index and confidence_ok) or busy_index`. Missing
`headroom_cpu_*` also triggers the fallback — do not break this `or`.

## Running the tests

```bash
python3 grafana/generate_ha_headroom.py
python3 grafana/validate_promql.py /path/to/promtool
python3 grafana/test_promql.py /path/to/promtool
```

No server or cluster needed. `promtool` ships with Prometheus.

## Deferred work worth picking up next

From `docs/ROADMAP.md`:

1. **Fleet overview table** — one row per cluster sorted worst-first, for watching multiple
   clusters at once.
2. **Grafana/Prometheus alert rules** — alert on risk index crossing 1 or 2, sustained over
   `$Window`.
3. **FC support** — code exists behind `INCLUDE_FC = True` in the generator, but read the
   speed-weighting caveat in `docs/ROADMAP.md` before enabling.
4. **Pair-scoped model mismatch detection** — currently only cluster-scoped; a per-pair check
   needs a static capacity-weight series mapping model names to relative capacity.
