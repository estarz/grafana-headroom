# ONTAP HA Headroom dashboard

A Grafana dashboard, built on metrics NetApp Harvest already collects, that answers one
question for a storage admin:

> **If a node in this cluster goes down, will it hurt?**

The answer is green, yellow or red. Everything below the traffic light exists to explain
or verify that answer.

Stock **ONTAP: Headroom** tells you whether a node is *currently* past its latency knee.
It does not model one controller running both nodes' workloads. Active IQ Unified Manager's
**Node Failover Planning** page is the productized version of this idea; this dashboard
approximates its CPU logic in PromQL and adds the network side.

## What it scores

| Colour | Meaning |
| --- | --- |
| 🟢 Green | A takeover should not cause a problem |
| 🟡 Yellow | Higher latency likely — avoid a planned failover at peak |
| 🔴 Red | Expect unacceptable latency or a service interruption |

Two resources feed the score, and the worse one wins:

- **CPU** — combined performance capacity used across the HA pair, or the sum of
  `node_cpu_busy` when ONTAP's optimal point is not trustworthy
- **Ethernet data ports** — the same NIC or ifgrp on both nodes, added together

Scoring is always **per HA pair**, because ONTAP moves a failed node's work to its partner
and nowhere else. A cluster's score is its **worst pair**, so this works on 2-node clusters
and larger ones alike.

See [docs/DESIGN.md](docs/DESIGN.md) for the model, the thresholds and the caveats.

## Requirements

| Requirement | Why |
| --- | --- |
| Harvest **24.05 or newer** | Earlier releases do not export the `ha_partner` label |
| Harvest **25.11 or newer** recommended | Adds `allow_partial_aggregation`, without which `node_cpu_busy` blanks for *every* node while any one node is down — see [docs/METRICS.md](docs/METRICS.md#behaviour-during-a-takeover) |
| A config collector — **`Rest` or `Zapi`** | Supplies `node_labels` (HA partner, model) and `net_port_status` (port role, ipspace) |
| A perf collector — **`RestPerf` or `ZapiPerf`** | Supplies `headroom_cpu_*`, `node_cpu_busy`, `nic_util_percent`, `nic_ifgrp_*` |
| Prometheus or VictoriaMetrics | Both are PromQL-compatible; no recording rules needed |
| Grafana **8.3 or newer** | The JSON targets schemaVersion 30 and migrates cleanly upward. 8.3 is the floor because datasource references use the object form, which older releases do not understand |

Every template involved is enabled by default in Harvest, so **no new collectors or
custom templates are required**. [docs/METRICS.md](docs/METRICS.md) lists each metric with
its scale, labels and source template, plus a query to confirm your TSDB has them.

## Quickstart

If Harvest and Prometheus are not yet set up, start with [docs/INSTALL.md](docs/INSTALL.md).
That guide walks through installing Harvest, enabling the right collectors, pointing
Prometheus at it, and importing the dashboard.

If Harvest is already running, the short version:

1. Confirm your poller has a config collector and a perf collector in `harvest.yml`. The
   shipped default gives you both, and either the REST or the ZAPI family works.
2. Verify the key metrics are present — run the check query in [docs/INSTALL.md §4](docs/INSTALL.md#step-4--verify-the-metrics-are-present).
3. Import `grafana/dashboards/cmode/ha_headroom.json` into Grafana.
4. Open the dashboard and check the three first-run panels described in [grafana/README.md](grafana/README.md#first-run-check-these-three-panels).

```bash
# Regenerate the JSON after changing any query
python3 grafana/generate_ha_headroom.py

# Parse-check every query offline
python3 grafana/validate_promql.py /path/to/promtool

# Prove the HA-pair maths is right against synthetic series
python3 grafana/test_promql.py /path/to/promtool
```

`promtool` ships with Prometheus. The test scripts need no running server and no live cluster.

## Repository layout

| Path | Role |
| --- | --- |
| [grafana/dashboards/cmode/ha_headroom.json](grafana/dashboards/cmode/ha_headroom.json) | The importable dashboard — generated, do not hand-edit |
| [grafana/generate_ha_headroom.py](grafana/generate_ha_headroom.py) | Source of truth for every panel and query |
| [grafana/validate_promql.py](grafana/validate_promql.py) | Offline PromQL parse check of the generated JSON |
| [grafana/test_promql.py](grafana/test_promql.py) | Semantic tests: pair isolation, roll-up, confidence gate, port filtering |
| [grafana/README.md](grafana/README.md) | Import mechanics, variables, threshold overrides |
| [docs/INSTALL.md](docs/INSTALL.md) | Step-by-step: Harvest setup, enabling collectors, metric verification, dashboard import |
| [docs/DESIGN.md](docs/DESIGN.md) | The model, thresholds, and what this deliberately does not do |
| [docs/METRICS.md](docs/METRICS.md) | Verified metric reference and the PromQL patterns used |
| [docs/REVIEW.md](docs/REVIEW.md) | Review of the first version: bugs found, corrections made |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Deferred ideas, and hard limits imposed by available metrics |
| [docs/QA-ERICSSON.md](docs/QA-ERICSSON.md) | Session brief for the DC286 confidence and yellow-light questions |

## Scope

Built for a customer running **2-node AFF clusters on A90, A70 and A30** with NAS plus
iSCSI/NVMe-TCP. Larger clusters are supported through per-HA-pair scoring. FC panels exist
but are switched off — see `INCLUDE_FC` in the generator.

Out of scope: MetroCluster, aggregate and disk headroom (irrelevant on all-flash), and
predicting latency in ms/op after takeover.
