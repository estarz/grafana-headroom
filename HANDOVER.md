# Handover notes

Repository: `github.com/estarz/grafana-headroom`, branch `main`.
Last updated at commit `0cb000a`.

## What this repo is

A Grafana dashboard that scores ONTAP HA takeover headroom — green/yellow/red — using
metrics from NetApp Harvest (no custom collectors needed). Built for a customer running AFF
A90, A70 and A30 nodes in 2-node clusters, with NAS + iSCSI/NVMe-TCP. Supports larger
clusters via per-HA-pair scoring.

## Current state

The dashboard is complete and tested offline. **It has not yet been run against real
hardware.** The next milestone is lab validation on a 2-node AFF A220 — see
[LAB-PLAN.md](LAB-PLAN.md) and the open questions below.

The generator is [grafana/generate_ha_headroom.py](grafana/generate_ha_headroom.py); the
committed JSON is its output — **never hand-edit the JSON**. Regeneration is byte-stable, so
`python3 grafana/generate_ha_headroom.py && git diff --exit-code` is a useful invariant check.

## Repository map

| Path | What it is |
| --- | --- |
| [README.md](README.md) | Entry point: what it scores, requirements, quickstart |
| [LAB-PLAN.md](LAB-PLAN.md) | 14 test cases for validating against a real A220 lab |
| [docs/INSTALL.md](docs/INSTALL.md) | Harvest / Prometheus / Grafana setup, metric verification |
| [docs/DESIGN.md](docs/DESIGN.md) | The model, thresholds, caveats |
| [docs/METRICS.md](docs/METRICS.md) | Verified metric reference, PromQL patterns, takeover behaviour |
| [docs/REVIEW.md](docs/REVIEW.md) | **Read first.** What was wrong in v1 and why the code looks like it does |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Deferred work and hard limits |
| [grafana/README.md](grafana/README.md) | Import mechanics, variables, threshold overrides |
| `grafana/generate_ha_headroom.py` | Source of truth for every panel and query |
| `grafana/validate_promql.py` | Parse-checks every query via `promtool check rules` |
| `grafana/test_promql.py` | 7 semantic tests via `promtool test rules` against synthetic series |

## Key technical constraints — read before changing anything

**Harvest collectors.** Harvest's default `harvest.yml` uses `Zapi`/`ZapiPerf` only. Both
`Rest` and `RestPerf` collectors must be enabled. `Rest` provides `node_labels{ha_partner=...}`
(Harvest ≥ 24.05) and `net_port_status{ipspace=...}`. Without them the pair scores go blank.

**Datasource references must stay in object form.** Every `datasource` in the JSON is
`{"type": "prometheus", "uid": "${DS_PROMETHEUS}"}`, never the bare `"${DS_PROMETHEUS}"`
string that stock Harvest dashboards use. Released Grafana does an unguarded `"uid" in ds`
while applying `__inputs`, which throws on a string and makes the dashboard unimportable
through the UI on Grafana Cloud and 12.4+. This was hit for real. Upstream has since taught
the importer to accept both forms, but that is not in a release yet, and the object form
works on everything from Grafana 8.3 up — so do not "simplify" it back to a string to match
Harvest's style. Details in [grafana/README.md](grafana/README.md#import-fails-with-a-uid-error).

**Metric scales.** Easy to get wrong:

| Metric | Scale |
| --- | --- |
| `nic_util_percent`, `nic_ifgrp_rx_perc`, `nic_ifgrp_tx_perc`, `fcp_util_percent` | **0–1** — queries multiply by 100 |
| `headroom_cpu_*_utilization`, `node_cpu_busy`, `node_avg_processor_busy` | **0–100** — do not scale |

**Label names.** The ifgrp label is `ifgroup` (not `ifgrp`). `ports` on an ifgrp is a
comma-joined string of member ports, not one series per member.

**Port filtering.** Uses `ipspace != "Cluster"` via `net_port_status`, not a name regex.
Cluster ports are `e4a`/`e4b` on A30, `e1a`/`e7a` on A70/A90, and `e0a`/`e0b` on older
platforms including the A220 — a name regex cannot cover all platforms cleanly.

**`or` is a union, not a max in PromQL.** When two sides share a label set, only the left
survives. The generator stamps a `resource` label before unions to make them real. This is
the most important correctness property in the codebase — see `docs/REVIEW.md` §1–2 for
what went wrong before.

**The risk index.** Maps `1.0 = yellow line, 2.0 = red line` so resources with different
native scales (PCU uses 160/200, ports use 70/85) share one threshold pair. Thresholds are
Grafana variables, hidden by default, overridable by URL parameter.

**Headroom confidence 1 is normal.** ONTAP reports confidence 1 when using a seed value —
expected on lightly loaded or recently rebooted nodes. The dashboard falls back to
`node_cpu_busy` automatically via `(PCU_index and confidence_ok) or busy_index`. Missing
`headroom_cpu_*` also triggers the fallback — do not break this `or`.

**`system:node` blanks cluster-wide when a node is down.** ONTAP treats it as an aggregated
counter table and marks all rows incomplete — including healthy nodes' — if any node is
unreachable (Harvest [#3731](https://github.com/NetApp/harvest/issues/3731),
[#2769](https://github.com/NetApp/harvest/issues/2769)). Harvest 25.11 added
`allow_partial_aggregation: true` as a workaround. `headroom_cpu` is not aggregated and is
unaffected. So `node_cpu_busy` — the everyday fallback model — is also the one that degrades
worst during a real outage. Recommend Harvest 25.11+.

## Useful dashboard facts

- UID is `cdot-ha-headroom`, so threshold overrides look like
  `/d/cdot-ha-headroom/?var-BusyYellow=5&var-BusyRed=10`. This is the practical way to drive
  the traffic light through yellow and red on an idle system without generating load.
- Visible variables: `Datacenter`, `Cluster`, `Window` (30m), `ExcludePorts` (`e0M`).
- Hidden variables: `PcuYellow` 160, `PcuRed` 200, `BusyYellow` 70, `BusyRed` 85,
  `PortYellow` 70, `PortRed` 85, `ConfMin` 2.
- `?var-ConfMin=1` forces the PCU path when confidence is stuck at 1 — the only practical way
  to exercise that branch without sustained real load.

## Running the tests

```bash
python3 grafana/generate_ha_headroom.py
python3 grafana/validate_promql.py /path/to/promtool
python3 grafana/test_promql.py /path/to/promtool
```

No server or cluster needed. `promtool` ships with Prometheus — grab a release tarball from
[prometheus/prometheus](https://github.com/prometheus/prometheus/releases) if it is not
already on the box.

## Open questions awaiting lab validation

These are genuinely unknown, not merely unverified. Do not guess at them in documentation —
they are cheap to answer empirically and [LAB-PLAN.md](LAB-PLAN.md) walks through capturing
each one.

1. **Does the taken-over node's `headroom_cpu` row disappear, or persist stale?**
   `headroom_cpu` is not an aggregated table so the survivor is fine, but nothing documents
   what happens to the down node's row. LAB-PLAN TC-13.
2. **What does the dashboard actually render mid-takeover?** Expectation is that the pair
   series vanishes (the pair sum needs both nodes) and panels read "No data". If confirmed,
   consider whether an explicit "takeover in progress" state would be better than a blank
   panel. LAB-PLAN TC-13.
3. **Does `node_cpu_busy` blank for the surviving node too?** Depends on Harvest version and
   whether `allow_partial_aggregation` is in play. LAB-PLAN TC-12.
4. **A220 maximum ONTAP release.** Evidence is indirect — the A220 is absent from NetApp's
   end-of-support list through 9.19.1, but Hardware Universe was not directly checked.
5. **Does the ipspace port filter behave on a platform where the old name regex would also
   have worked?** A220 cluster ports are `e0a`/`e0b`, exactly what the old buggy regex
   assumed, so the lab is a good test that the new mechanism is doing the work rather than
   coincidentally agreeing. LAB-PLAN TC-04.

## Deferred work worth picking up next

From [docs/ROADMAP.md](docs/ROADMAP.md):

1. **Act on the lab results** — fold answers to the open questions above into
   `docs/METRICS.md` and `docs/DESIGN.md`, and fill in the LAB-PLAN results table.
2. **Fleet overview table** — one row per cluster sorted worst-first, for watching multiple
   clusters at once.
3. **Grafana/Prometheus alert rules** — alert on risk index crossing 1 or 2, sustained over
   `$Window`.
4. **FC support** — code exists behind `INCLUDE_FC = True` in the generator, but read the
   speed-weighting caveat in `docs/ROADMAP.md` before enabling. Relevant if a lab or customer
   A220 has its UTA2 ports in FC rather than 10GbE mode.
5. **Pair-scoped model mismatch detection** — currently only cluster-scoped; a per-pair check
   needs a static capacity-weight series mapping model names to relative capacity.

## Commit history

| Commit | What |
| --- | --- |
| `498778d` | First version (had the bugs documented in `docs/REVIEW.md`) |
| `5f03c4b` | HA-pair scoring, PromQL bug fixes, test suites, docs restructure |
| `0b344ca` | `docs/INSTALL.md` and README quickstart |
| `05ba109` | This file |
| `88089a7` | `LAB-PLAN.md` for the A220 lab |
| `0cb000a` | `system:node` partial-aggregation caveat documented |
