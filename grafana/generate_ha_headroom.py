#!/usr/bin/env python3
"""Generate grafana/dashboards/cmode/ha_headroom.json.

Produces a Harvest-style Grafana 8.3+ import file (schemaVersion 30) that scores
ONTAP takeover risk per HA pair and rolls it up to the cluster as the worst pair.

See ../docs/METRICS.md for the PromQL shapes this emits and why.
"""

from __future__ import annotations

import json
from pathlib import Path

# Must be the object form, not the legacy bare "${DS_PROMETHEUS}" string. Grafana's
# UI import path does `"uid" in datasource` and throws a TypeError on a string, which
# breaks import on Grafana Cloud and 12.4+. Object refs need Grafana >= 8.3.
DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
GRAFANA_DS = {"type": "datasource", "uid": "grafana"}

# Harvest label set shared by every query.
SEL = 'datacenter=~"$Datacenter",cluster=~"$Cluster"'

# The customer running this dashboard is NAS + iSCSI/NVMe-TCP only. Harvest has no
# per-port iSCSI or NVMe-TCP utilization counter, so Ethernet ports carry that
# traffic in the model. Flip to True to add FC target-port panels and fold
# fcp_util_percent into the network score.
INCLUDE_FC = False

# Aggregation granularities. A takeover is always within an HA pair, so the pair is
# the unit of truth and the cluster is the worst of its pairs.
PAIR_KEYS = ("datacenter", "cluster", "survivor", "victim")
CLUSTER_KEYS = ("datacenter", "cluster")


def keys(k) -> str:
    return ", ".join(k)


# --------------------------------------------------------------------------------
# HA pair machinery
#
# Harvest exports the partner name as a label on the config-collector node object
# (`node_labels{ha_partner="..."}`, Harvest >= 24.05). Perf metrics come from a
# different template and lack it, so each one is joined first. There is no chassis
# or pair-id label, so the pair is formed by matching each node against its partner
# with node/ha_partner swapped on one side.
# --------------------------------------------------------------------------------

NODE_LABELS = f'node_labels{{{SEL},ha_partner!=""}}'


def with_partner(expr: str) -> str:
    """Attach the ha_partner label to a per-node expression."""
    return (
        f"(\n{expr}\n)\n"
        "* on (datacenter, cluster, node) group_left (ha_partner)\n"
        f"({NODE_LABELS} == 1)"
    )


def _orient(expr: str, survivor_from: str, victim_from: str) -> str:
    return (
        f'label_replace(\nlabel_replace(\n{expr}\n'
        f', "survivor", "$1", "{survivor_from}", "(.*)")\n'
        f', "victim", "$1", "{victim_from}", "(.*)")'
    )


def pair_sum(expr: str, extra: tuple[str, ...] = ()) -> str:
    """Load the survivor would carry after absorbing the victim.

    Input must carry datacenter, cluster and node (plus any `extra` labels that
    identify a resource, e.g. nic). Output is keyed by survivor/victim, so a
    2-node cluster yields two symmetric series and an N-node cluster yields one
    series per HA pair per direction.

    Both orientations are always numerically identical: each is val(A) + val(B),
    and every downstream stage (p95, risk_index, worst_of) is a function of that
    one number. Emitting a single series per unordered pair would mean choosing a
    canonical direction, which needs a comparison between `node` and `ha_partner`
    -- PromQL has no label-comparison operator. The labels are kept because the
    capacity-weighted model in docs/ROADMAP.md would make the direction real; the
    panels meanwhile read as one tile per surviving node.
    """
    joined = with_partner(expr)
    k = keys(list(PAIR_KEYS) + list(extra))
    survivor_side = _orient(joined, "node", "ha_partner")
    victim_side = _orient(joined, "ha_partner", "node")
    return f"(\n{survivor_side}\n)\n+ on ({k})\n(\n{victim_side}\n)"


def p95(expr: str) -> str:
    """95th percentile over $Window so a momentary spike cannot flip the light."""
    return f"quantile_over_time(0.95, (\n{expr}\n)[$Window:])"


def risk_index(expr: str, yellow: str, red: str) -> str:
    """Map a utilization value onto a continuous 0-2+ index.

    1.0 is exactly the yellow line and 2.0 exactly the red line, so a single
    Grafana threshold pair colours resources that have different native scales.
    Above red the index keeps climbing, which shows how far past the line it is.
    """
    return (
        f"clamp_max(\n(\n{expr}\n)\n/ {yellow}, 1)\n"
        f"+ clamp_min(\n(\n(\n{expr}\n)\n- {yellow}\n)\n/ ({red} - {yellow}), 0)"
    )


def worst_of(parts: dict[str, str], k=PAIR_KEYS) -> str:
    """Max across expressions that would otherwise share an identical label set.

    `or` is a union, not a max: for series with equal label sets it silently
    returns only the left side. Stamping a distinguishing label first makes the
    union real, and `max by` then picks the worst. Absent series contribute
    nothing instead of blanking the result.
    """
    tagged = [
        f'label_replace(\n{expr}\n, "resource", "{name}", "", "")'
        for name, expr in parts.items()
    ]
    body = "\nor\n".join(tagged)
    return f"max by ({keys(k)}) (\n{body}\n)"


# --------------------------------------------------------------------------------
# Per-node inputs
# --------------------------------------------------------------------------------

# headroom_cpu_*_utilization are 0-100. Dividing by a `> 0` filter drops nodes whose
# optimal point is unpopulated; clamp_min() would instead turn 0 into a fake red.
PCU_NODE = f"""100
* headroom_cpu_current_utilization{{{SEL}}}
/ (headroom_cpu_optimal_point_utilization{{{SEL}}} > 0)"""

CONF_OK_NODE = (
    f"headroom_cpu_optimal_point_confidence_factor{{{SEL}}} >= bool $ConfMin"
)

BUSY_NODE = f"node_cpu_busy{{{SEL}}}"
AVG_BUSY_NODE = f"node_avg_processor_busy{{{SEL}}}"

# Cluster-interconnect ports are identified by ipspace, not by name. Name regexes
# cannot work across platforms: cluster ports are e4a/e4b on A30, e1a/e7a on
# A70/A90 and e0a/e0b only on older FAS/AFF.
DATA_PORTS = f'(net_port_status{{{SEL},ipspace!="Cluster",port!~"$ExcludePorts"}} == 1)'

# nic_util_percent is 0-1 in Harvest. The `unless` clause falls back to the plain
# name regex only for clusters where net_port is entirely absent (Rest config
# collector disabled); a bare `or` would instead add the cluster ports back in.
NIC_NODE = f"""(
  nic_util_percent{{{SEL}}} * 100
  * on (datacenter, cluster, node, nic) group_left ()
    label_replace({DATA_PORTS}, "nic", "$1", "port", "(.*)")
)
or (
  (nic_util_percent{{{SEL},nic!~"$ExcludePorts"}} * 100)
  unless on (datacenter, cluster) {DATA_PORTS}
)"""

_IFGRP_SEL = f'{SEL},ports!=""'
_RX = f"nic_ifgrp_rx_perc{{{_IFGRP_SEL}}}"
_TX = f"nic_ifgrp_tx_perc{{{_IFGRP_SEL}}}"

# max(rx, tx) without max(): a full-duplex LAG is not the sum of both directions,
# and whichever direction binds first is the one that matters after takeover.
IFGRP_NODE = f"""(
  {_RX}
  + {_TX}
  + abs({_RX} - {_TX})
) / 2 * 100"""

FCP_NODE = f"fcp_util_percent{{{SEL}}} * 100"


# --------------------------------------------------------------------------------
# Pair-level scores
# --------------------------------------------------------------------------------

PCU_PAIR = pair_sum(PCU_NODE)
BUSY_PAIR = pair_sum(BUSY_NODE)
CONF_PAIR_OK = f"(\n{pair_sum(CONF_OK_NODE)}\n) == 2"

NIC_PAIR = pair_sum(NIC_NODE, extra=("nic",))
IFGRP_PAIR = pair_sum(IFGRP_NODE, extra=("ifgroup",))

_PORT_PARTS = {"nic": NIC_PAIR, "ifgrp": IFGRP_PAIR}
if INCLUDE_FC:
    _PORT_PARTS["fc"] = pair_sum(FCP_NODE, extra=("port",))

PORT_HOT_PAIR = worst_of(_PORT_PARTS)

PCU_PAIR_P95 = p95(PCU_PAIR)
BUSY_PAIR_P95 = p95(BUSY_PAIR)
PORT_HOT_P95 = p95(PORT_HOT_PAIR)

# PCU is only trustworthy when ONTAP's optimal point is measured rather than seeded.
# `or` is the correct operator here because both branches share a label set, so it
# is a true fallback -- including when headroom_cpu_* is missing altogether.
CPU_INDEX = f"""(
(
{risk_index(PCU_PAIR_P95, "$PcuYellow", "$PcuRed")}
)
and on ({keys(PAIR_KEYS)}) (
{CONF_PAIR_OK}
)
)
or (
{risk_index(BUSY_PAIR_P95, "$BusyYellow", "$BusyRed")}
)"""

PORT_INDEX = risk_index(PORT_HOT_P95, "$PortYellow", "$PortRed")

INDEX_BY_RESOURCE = {"cpu": CPU_INDEX, "network": PORT_INDEX}
# Raw worst-of still drops an absent resource. The gated expressions below are
# what the traffic light shows: a missing required input must be Unknown, not a
# green score from whatever series happened to be present. Low headroom
# confidence is not a missing input — CPU_INDEX has already fallen back to
# node_cpu_busy, and that fallback stays a valid score.
OVERALL_PAIR_RAW = worst_of(INDEX_BY_RESOURCE)
OVERALL_CLUSTER_RAW = f"max by ({keys(CLUSTER_KEYS)}) (\n{OVERALL_PAIR_RAW}\n)"

# Roll-ups for the headline numbers, kept in native units because those are the
# values an admin can check against ONTAP.
PCU_CLUSTER_P95 = f"max by ({keys(CLUSTER_KEYS)}) (\n{PCU_PAIR_P95}\n)"
BUSY_CLUSTER_P95 = f"max by ({keys(CLUSTER_KEYS)}) (\n{BUSY_PAIR_P95}\n)"
PORT_CLUSTER_P95 = f"max by ({keys(CLUSTER_KEYS)}) (\n{PORT_HOT_P95}\n)"

# Health of the model itself.
HA_COVERAGE = f"""count by ({keys(CLUSTER_KEYS)}) ({NODE_LABELS})
/
count by ({keys(CLUSTER_KEYS)}) (node_labels{{{SEL}}})"""

# A real risk index is >= 0, so -1 is free as the Unknown sentinel.
_PAIR_ON = keys(PAIR_KEYS)
_CLUSTER_ON = keys(CLUSTER_KEYS)
PAIR_READY = f"""(
({BUSY_PAIR_P95})
and on ({_PAIR_ON}) ({PORT_HOT_P95})
and on ({_CLUSTER_ON}) ({HA_COVERAGE} == 1)
)"""
CLUSTER_READY = f"""(
({HA_COVERAGE} == 1)
and on ({_CLUSTER_ON}) ({BUSY_CLUSTER_P95})
and on ({_CLUSTER_ON}) ({PORT_CLUSTER_P95})
)"""


def unknown_unless(score: str, ready: str, anchor: str, on: str) -> str:
    """Keep `score` only where `ready` has a series; otherwise emit -1.

    `or` keeps the left-hand series when both sides share a label set, so the
    score is filtered with `and` before the sentinel is attached. The anchor
    is any series that proves the cluster is being scraped at all: with no
    anchor there is nothing to label, and the panel correctly reads No data.
    """
    return f"""(
({score})
and on ({on}) ({ready})
)
or
(
0 * (
({anchor})
unless on ({on}) ({ready})
) - 1
)"""


OVERALL_PAIR = unknown_unless(
    OVERALL_PAIR_RAW,
    PAIR_READY,
    f"({BUSY_PAIR_P95}) or ({PORT_HOT_P95})",
    _PAIR_ON,
)
OVERALL_CLUSTER = unknown_unless(
    OVERALL_CLUSTER_RAW,
    CLUSTER_READY,
    f"({HA_COVERAGE}) or ({BUSY_CLUSTER_P95}) or ({PORT_CLUSTER_P95})",
    _CLUSTER_ON,
)

CONF_MIN_CLUSTER = (
    f"min by ({keys(CLUSTER_KEYS)}) "
    f"(headroom_cpu_optimal_point_confidence_factor{{{SEL}}})"
)

# Summing normalized percentages assumes both nodes have the same capacity. That
# holds inside a real HA pair; >1 model in a cluster is worth surfacing anyway.
MODEL_COUNT = (
    f"count by ({keys(CLUSTER_KEYS)}) "
    f"(count by (datacenter, cluster, model) (node_labels{{{SEL}}}))"
)


# --------------------------------------------------------------------------------
# Panel builders
# --------------------------------------------------------------------------------


def target(
    expr: str,
    legend: str = "",
    ref_id: str = "A",
    instant: bool = False,
    fmt: str | None = None,
    interval: str = "",
) -> dict:
    t = {
        "datasource": DS,
        "exemplar": False,
        "expr": expr.strip(),
        "interval": interval,
        "legendFormat": legend,
        "refId": ref_id,
    }
    if instant:
        t["instant"] = True
    if fmt:
        t["format"] = fmt
    return t


def thresholds(*steps: tuple[str, float | None]) -> dict:
    return {
        "mode": "absolute",
        "steps": [{"color": color, "value": value} for color, value in steps],
    }


def field_defaults(unit: str = "short", decimals=None, **extra) -> dict:
    cfg = {
        "color": {"mode": "palette-classic"},
        "custom": {
            "axisLabel": "",
            "axisPlacement": "auto",
            "barAlignment": 0,
            "drawStyle": "line",
            "fillOpacity": 10,
            "gradientMode": "none",
            "hideFrom": {"legend": False, "tooltip": False, "viz": False},
            "lineInterpolation": "linear",
            "lineWidth": 1,
            "pointSize": 5,
            "scaleDistribution": {"type": "linear"},
            "showPoints": "never",
            "spanNulls": True,
            "stacking": {"group": "A", "mode": "none"},
            "thresholdsStyle": {"mode": "off"},
        },
        "mappings": [],
        "thresholds": thresholds(("green", None)),
        "unit": unit,
    }
    if decimals is not None:
        cfg["decimals"] = decimals
    cfg.update(extra)
    return cfg


def timeseries(
    pid: int,
    title: str,
    x: int,
    y: int,
    w: int,
    h: int,
    targets: list,
    unit: str = "percent",
    description: str = "",
    thresh=None,
    decimals=None,
    max_data_points=None,
) -> dict:
    defaults = field_defaults(unit, decimals=decimals)
    if thresh:
        defaults["thresholds"] = thresh
        defaults["custom"]["thresholdsStyle"] = {"mode": "line+area"}
        defaults["color"] = {"mode": "thresholds"}
    panel = {
        "datasource": DS,
        "description": description,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": pid,
        "options": {
            "legend": {
                "calcs": ["mean", "lastNotNull", "max"],
                "displayMode": "table",
                "placement": "bottom",
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": targets,
        "title": title,
        "type": "timeseries",
    }
    if max_data_points:
        panel["maxDataPoints"] = max_data_points
    return panel


def sorted_tiles(name_field: str) -> list[dict]:
    """Transformations that order a multi-series stat panel by a label value.

    Prometheus returns instant vectors in map order, and a stat panel renders one
    tile per frame in the order it receives them, so tiles come out unsorted.
    `sortBy` sorts rows *within* a frame rather than frames within a response, so
    the target asks for a `table` frame -- every series as one row -- keeps only
    the name and value columns, and sorts on the name. `values: True` on the panel
    then renders one tile per row, titled from the one remaining string column.

    The server-side fix would be `sort_by_label()`, but that is gated behind
    Prometheus's `--enable-feature=promql-experimental-functions`, so a query using
    it would simply fail on a server without the flag.

    Targets must therefore pass `fmt="table"` and omit `legendFormat`, which a
    table frame has no use for, and the panel must pass `values=True`.
    """
    return [
        {
            "id": "filterFieldsByName",
            "options": {"include": {"pattern": f"^({name_field}|Value.*)$"}},
        },
        {
            "id": "sortBy",
            "options": {"fields": {}, "sort": [{"field": name_field, "desc": False}]},
        },
    ]


def stat(
    pid: int,
    title: str,
    x: int,
    y: int,
    w: int,
    h: int,
    targets: list,
    unit: str,
    thresh,
    description: str = "",
    mappings=None,
    decimals=None,
    text_mode: str = "auto",
    transformations=None,
    values: bool = False,
) -> dict:
    defaults = {
        "color": {"mode": "thresholds"},
        "mappings": mappings or [],
        "thresholds": thresh,
        "unit": unit,
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    panel = {
        "datasource": DS,
        "description": description,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": pid,
        "options": {
            "colorMode": "background",
            "graphMode": "none",
            "justifyMode": "auto",
            "orientation": "auto",
            "reduceOptions": {
                "calcs": ["lastNotNull"],
                "fields": "",
                "values": values,
            },
            "textMode": text_mode,
        },
        "pluginVersion": "8.1.8",
        "targets": targets,
        "title": title,
        "type": "stat",
    }
    if transformations:
        panel["transformations"] = transformations
    return panel


def state_timeline(
    pid: int,
    title: str,
    x: int,
    y: int,
    w: int,
    h: int,
    targets: list,
    thresh,
    mappings,
    description: str = "",
    time_from: str = "7d",
) -> dict:
    return {
        "datasource": DS,
        "description": description,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "custom": {
                    "fillOpacity": 85,
                    "lineWidth": 0,
                    "spanNulls": False,
                },
                "mappings": mappings,
                "thresholds": thresh,
                "unit": "short",
            },
            "overrides": [],
        },
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": pid,
        "maxDataPoints": 400,
        "options": {
            "alignValue": "center",
            "legend": {"displayMode": "list", "placement": "bottom"},
            "mergeValues": True,
            "rowHeight": 0.9,
            "showValue": "never",
            "tooltip": {"mode": "single"},
        },
        "targets": targets,
        "timeFrom": time_from,
        "title": title,
        "type": "state-timeline",
    }


def table(
    pid: int,
    title: str,
    x: int,
    y: int,
    w: int,
    h: int,
    targets: list,
    include_pattern: str,
    description: str = "",
) -> dict:
    return {
        "datasource": DS,
        "description": description,
        "fieldConfig": {
            "defaults": {
                "custom": {"align": "auto", "displayMode": "auto"},
                "mappings": [],
                "thresholds": thresholds(("green", None)),
            },
            "overrides": [],
        },
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": pid,
        "options": {"showHeader": True},
        "pluginVersion": "8.1.8",
        "targets": targets,
        "title": title,
        "transformations": [
            {"id": "filterFieldsByName", "options": {"include": {"pattern": include_pattern}}}
        ],
        "type": "table",
    }


def row(pid: int, title: str, y: int, collapsed: bool = False, panels=None) -> dict:
    return {
        "collapsed": collapsed,
        "datasource": DS,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "id": pid,
        "panels": panels or [],
        "title": title,
        "type": "row",
    }


NOTES = """**What this scores**

Whether the **surviving node** could absorb its partner's load after a takeover — not
whether a node is healthy today. Every score is per **HA pair**; the cluster value is
the **worst pair**, because ONTAP moves a failed node's work to its partner only, never
across the cluster.

- 🟢 **OK** — takeover should not cause a problem
- 🟡 **Latency risk** — avoid a planned failover at peak
- 🔴 **Service impact risk** — expect unacceptable latency or loss

Inputs are the **95th percentile over $Window**, so a brief spike will not flip the light.
CPU uses combined **performance capacity used** (current ÷ optimal point) when ONTAP's
optimal-point confidence is ≥ $ConfMin on both nodes, otherwise the sum of `node_cpu_busy`.
Network sums the same NIC or ifgrp on both nodes. Cluster-interconnect ports are excluded
by **ipspace**, not by port name.

**PCU** is performance capacity used: 100 × current utilization / optimal-point
utilization. It is how full the node is relative to ONTAP's latency knee, not raw CPU percent.

**Only one CPU tile drives the score at a time.** PCU is live only when headroom confidence
is at least $ConfMin on **both** nodes. One node below that switches the whole pair to
combined CPU busy, so the light can change color while the load does not. An upgrade or
reboot sets that node back to the seed, and it stays there until that node's own load
remeasures the knee. The other CPU tile is reference only, so its colour can disagree
with the light. Confidence is informational — a low value is not a fault, and it is not
Unknown.

**Unknown** means the score was not evaluated. It appears when a node has no HA partner,
or when the CPU-busy series or the Ethernet series is missing. A low confidence factor
still produces a real score, on CPU busy.

**Limits.** Summing CPU is a proxy — takeover adds HA overhead on top. Harvest has no NVRAM
or HA-interconnect counter, so those are unmodelled. Harvest has no per-port iSCSI or
NVMe-TCP utilization either, so that traffic is scored through its Ethernet ports.
Aggregate and disk headroom are omitted deliberately (all-flash). This is not AIQUM's
estimated ms/op after takeover."""


# Below 0 is the Unknown sentinel (-1). A real index starts at 0.
INDEX_THRESH = thresholds(("blue", None), ("green", 0), ("yellow", 1), ("red", 2))
PCU_THRESH = thresholds(("green", None), ("yellow", 160), ("red", 200))
BUSY_THRESH = thresholds(("green", None), ("yellow", 70), ("red", 85))
PORT_THRESH = thresholds(("green", None), ("yellow", 70), ("red", 85))
# Confidence is not a health signal: at 0 or 1 the score silently switches to the
# CPU busy model and stays valid. Colouring those red reads as a fault, so the
# tile is neutral and the value mappings carry the meaning instead.
CONF_THRESH = thresholds(("blue", None))
COVERAGE_THRESH = thresholds(("red", None), ("green", 1))
MODEL_THRESH = thresholds(("green", None), ("yellow", 2))

INDEX_MAP = [
    {
        "type": "range",
        "options": {
            "from": -2,
            "to": -0.5,
            "result": {"text": "Unknown", "color": "blue", "index": 0},
        },
    },
    {
        "type": "range",
        "options": {
            "from": 0,
            "to": 1,
            "result": {"text": "OK", "color": "green", "index": 1},
        },
    },
    {
        "type": "range",
        "options": {
            "from": 1,
            "to": 2,
            "result": {"text": "Latency risk", "color": "yellow", "index": 2},
        },
    },
    {
        "type": "range",
        "options": {
            "from": 2,
            "to": 1000,
            "result": {"text": "Service impact risk", "color": "red", "index": 3},
        },
    },
]

# Mapping text is deliberately phrased in terms of what ONTAP measured rather than
# which model wins, because the gate is $ConfMin: at ConfMin=1 a confidence of 1 does
# trust PCU, so "CPU busy" baked into the text here would be wrong.
CONF_MAP = [
    {
        "type": "value",
        "options": {
            "0": {"text": "0 unknown", "color": "blue", "index": 0},
            "1": {"text": "1 seed", "color": "blue", "index": 1},
            "2": {"text": "2 medium", "color": "green", "index": 2},
            "3": {"text": "3 high", "color": "green", "index": 3},
        },
    }
]

COVERAGE_MAP = [
    {
        "type": "range",
        "options": {
            "from": -1,
            "to": 0.999,
            "result": {"text": "Incomplete — see docs", "color": "red", "index": 0},
        },
    },
    {
        "type": "range",
        "options": {
            "from": 0.999,
            "to": 1000,
            "result": {"text": "All nodes paired", "color": "green", "index": 1},
        },
    },
]


def query_var(name: str, query: str, label=None) -> dict:
    return {
        "allValue": ".*",
        "current": {},
        "datasource": DS,
        "definition": query,
        "description": None,
        "error": None,
        "hide": 0,
        "includeAll": True,
        "label": label,
        "multi": True,
        "name": name,
        "options": [],
        "query": {"query": query, "refId": "StandardVariableQuery"},
        "refresh": 2,
        "regex": "",
        "skipUrlSync": False,
        "sort": 7,
        "type": "query",
    }


def custom_var(
    name: str,
    values: list,  # str | (display: str, value: str)
    default: str,
    label: str,
    hide: int = 0,
) -> dict:
    """Build a Grafana custom (CSV) variable.

    Each entry in *values* may be either a plain string (display = value) or a
    ``(display, value)`` tuple, which Grafana renders as ``display : value`` in
    the CSV query string.  Display labels must not contain commas — Grafana
    parses the CSV before splitting on `` : ``, so a comma in the label would
    silently break the option list.

    ``allowCustomValue`` is emitted as True (the schema default) to explicitly
    document that users may type an arbitrary value into the dropdown — e.g.
    ``e0M|e2a|e2b`` for a platform not covered by the presets.
    """
    items: list[tuple[str, str]] = []
    for v in values:
        if isinstance(v, tuple):
            disp, val = v
        else:
            disp = val = v
        assert "," not in disp, (
            f"custom_var '{name}': display label {disp!r} contains a comma, "
            "which would corrupt Grafana's CSV parser"
        )
        items.append((disp, val))

    # Build the current "selected" entry using the display text of the default value.
    default_disp = next((d for d, v in items if v == default), default)

    def _query_entry(disp: str, val: str) -> str:
        return val if disp == val else f"{disp} : {val}"

    return {
        "allowCustomValue": True,
        "current": {"selected": True, "text": default_disp, "value": default},
        "hide": hide,
        "includeAll": False,
        "label": label,
        "multi": False,
        "name": name,
        "options": [
            {"selected": v == default, "text": d, "value": v} for d, v in items
        ],
        "query": ",".join(_query_entry(d, v) for d, v in items),
        "skipUrlSync": False,
        "type": "custom",
    }


def build() -> dict:
    panels: list[dict] = []
    pid = 1

    def next_id() -> int:
        nonlocal pid
        current = pid
        pid += 1
        return current

    # ---------------------------------------------------------------- readiness
    y = 0
    panels.append(row(next_id(), "Takeover readiness", y))
    y += 1

    panels.append(
        stat(
            next_id(),
            "Can this cluster survive a node loss?",
            0,
            y,
            8,
            8,
            [target(OVERALL_CLUSTER, instant=True, fmt="table")],
            "short",
            INDEX_THRESH,
            "Worst HA pair in the cluster. Risk index: 1.0 is the yellow line, 2.0 the red "
            "line. Worst of CPU and network, 95th percentile over $Window. The CPU input is "
            "PCU when headroom confidence is at least $ConfMin on both nodes, and combined "
            "node_cpu_busy otherwise. One node below $ConfMin switches the pair to CPU busy, "
            "so the light can change color while the load does not. The other CPU tile is "
            "reference only and its colour can disagree. Unknown means HA pairing is "
            "incomplete, or the CPU-busy series or the network series is missing. A low "
            "confidence factor is not Unknown: the score stays valid on CPU busy.",
            INDEX_MAP,
            text_mode="value_and_name",
            transformations=sorted_tiles("cluster"),
            values=True,
        )
    )
    panels.append(
        stat(
            next_id(),
            "Combined PCU after takeover (p95)",
            8,
            y,
            4,
            4,
            [target(PCU_CLUSTER_P95, instant=True, fmt="table")],
            "percent",
            PCU_THRESH,
            "PCU is performance capacity used: 100 × current utilization / optimal-point "
            "utilization, summed across the HA pair. It is fullness against ONTAP's latency "
            "knee, not raw CPU percent. 200% means the survivor would sit exactly at that "
            "knee — the same line AIQUM calls 'HA pair over-utilized'. This drives the score "
            "only when confidence is >= $ConfMin on both nodes; below that the knee is a seed "
            "value, so this tile is reference only and CPU busy drives the score instead.",
            decimals=0,
            transformations=sorted_tiles("cluster"),
            values=True,
        )
    )
    panels.append(
        stat(
            next_id(),
            "Hottest survivor port (p95)",
            12,
            y,
            4,
            4,
            [target(PORT_CLUSTER_P95, instant=True, fmt="table")],
            "percent",
            PORT_THRESH,
            "Worst NIC or ifgrp once both nodes' traffic lands on one controller. Covers NAS, "
            "iSCSI and NVMe-TCP, which all share these Ethernet ports.",
            decimals=0,
            transformations=sorted_tiles("cluster"),
            values=True,
        )
    )
    panels.append(
        {
            "datasource": DS,
            "gridPos": {"h": 8, "w": 8, "x": 16, "y": y},
            "id": next_id(),
            "options": {"content": NOTES, "mode": "markdown"},
            "title": "How to read this",
            "type": "text",
        }
    )

    panels.append(
        stat(
            next_id(),
            "Combined CPU busy after takeover (p95)",
            8,
            y + 4,
            4,
            4,
            [target(BUSY_CLUSTER_P95, instant=True, fmt="table")],
            "percent",
            BUSY_THRESH,
            "Sum of node_cpu_busy (hottest CPU domain, or remaining idle, whichever binds "
            "first). This drives the score only while headroom confidence is below $ConfMin "
            "— common, since ONTAP reports confidence 1 until it has measured enough load. "
            "At or above $ConfMin the score uses PCU instead and this tile is reference "
            "only, so its colour can be red while the traffic light is green. Check the "
            "confidence tile to see which of the two is in play.",
            decimals=0,
            transformations=sorted_tiles("cluster"),
            values=True,
        )
    )
    panels.append(
        stat(
            next_id(),
            "Headroom confidence (worst node)",
            12,
            y + 4,
            2,
            4,
            [target(CONF_MIN_CLUSTER, instant=True, fmt="table")],
            "short",
            CONF_THRESH,
            "How well ONTAP knows the latency knee on the worst node — a property of the "
            "measurement, not of health, so this tile is informational (blue) and never an "
            "alarm. 0 unknown and 1 seed mean ONTAP has not measured the knee yet. An upgrade "
            "or reboot sets that node back to the seed, and it stays there until that node's "
            "own load reaches the knee; the partner recovering does not bring it back. One "
            "node still on the seed switches the pair to combined node_cpu_busy, so the light "
            "can change color while the load does not. The score remains valid. 2 medium and "
            "3 high mean the knee is measured, so PCU drives the score once both nodes reach "
            "$ConfMin.",
            CONF_MAP,
            decimals=0,
            transformations=sorted_tiles("cluster"),
            values=True,
        )
    )
    panels.append(
        stat(
            next_id(),
            "Distinct node models",
            14,
            y + 4,
            2,
            4,
            [target(MODEL_COUNT, instant=True, fmt="table")],
            "short",
            MODEL_THRESH,
            "Summing normalized CPU percentages assumes both nodes have equal capacity. "
            "1 means the cluster is uniform. More than 1 means check that each HA pair is "
            "still same-model before trusting the score.",
            decimals=0,
            transformations=sorted_tiles("cluster"),
            values=True,
        )
    )
    y += 8

    # ------------------------------------------------------------------ per pair
    panels.append(row(next_id(), "Per HA pair", y))
    y += 1

    panels.append(
        stat(
            next_id(),
            "Takeover risk per surviving node",
            0,
            y,
            8,
            7,
            [target(OVERALL_PAIR, instant=True, fmt="table")],
            "short",
            INDEX_THRESH,
            "One tile per node, sorted by node name so the two nodes of an HA pair sit next "
            "to each other under ONTAP's default naming. What that node would carry if its "
            "partner failed. Look up the "
            "node you intend to keep running. Both nodes of a pair always read the same "
            "value — summing normalized percentages is direction-independent, so the tiles "
            "are two views of one pair, not two different outcomes. On a 2-node cluster "
            "there is only one pair, so this is the same number as 'Can this cluster survive "
            "a node loss?' above, which is the worst pair in the cluster. This panel only "
            "adds information on clusters of four nodes or more. Unknown means this pair "
            "was not scored: HA coverage is below 1, or CPU busy or Ethernet data is missing.",
            INDEX_MAP,
            text_mode="value_and_name",
            transformations=sorted_tiles("survivor"),
            values=True,
        )
    )
    panels.append(
        timeseries(
            next_id(),
            "Risk index by resource",
            8,
            y,
            8,
            7,
            [
                target(
                    worst_of(INDEX_BY_RESOURCE, k=PAIR_KEYS + ("resource",)),
                    "{{resource}}: {{survivor}} survives",
                )
            ],
            "short",
            "Which resource is driving the score. 1.0 = yellow line, 2.0 = red line.",
            INDEX_THRESH,
            decimals=2,
        )
    )
    panels.append(
        table(
            next_id(),
            "HA pairs, models and partner health",
            16,
            y,
            8,
            7,
            [target(f"node_labels{{{SEL}}}", "", instant=True, fmt="table")],
            "^(cluster|node|ha_partner|model|version|healthy|partner_healthy)$",
            "From the Harvest config collector (Rest or Zapi). An empty ha_partner column means the "
            "pair-aware queries cannot resolve that node.",
        )
    )
    y += 7

    panels.append(
        state_timeline(
            next_id(),
            "Takeover risk over the last 7 days — pick a maintenance window",
            0,
            y,
            20,
            8,
            [
                target(
                    OVERALL_PAIR,
                    "{{survivor}} survives",
                    interval="10m",
                )
            ],
            INDEX_THRESH,
            INDEX_MAP,
            "Green bands are safe windows for a planned takeover or an ONTAP upgrade. "
            "Blue is Unknown: a required input was missing, so that interval was not scored. "
            "Ignores this dashboard's time picker and always shows 7 days. One row per node; "
            "the two nodes of a pair track identically, so read a pair from either row.",
        )
    )
    panels.append(
        stat(
            next_id(),
            "HA partner coverage",
            20,
            y,
            4,
            8,
            [target(HA_COVERAGE, instant=True, fmt="table")],
            "percentunit",
            COVERAGE_THRESH,
            "Fraction of nodes with an ha_partner label. Anything below 1 means the takeover "
            "light shows Unknown: check that a Harvest config collector (Rest or Zapi) is "
            "enabled and Harvest is 24.05 or newer.",
            COVERAGE_MAP,
            text_mode="value_and_name",
            transformations=sorted_tiles("cluster"),
            values=True,
        )
    )
    y += 8

    # ----------------------------------------------------------- CPU capacity
    panels.append(row(next_id(), "CPU performance capacity", y))
    y += 1

    panels.append(
        timeseries(
            next_id(),
            "PCU per node",
            0,
            y,
            12,
            8,
            [target(PCU_NODE, "{{node}}")],
            "percent",
            "Performance capacity used = 100 x current_utilization / optimal_point_utilization. "
            "A node missing here has no populated optimal point.",
        )
    )
    panels.append(
        timeseries(
            next_id(),
            "Combined PCU per HA pair",
            12,
            y,
            12,
            8,
            [target(PCU_PAIR, "{{survivor}} survives")],
            "percent",
            "PCU is performance capacity used: 100 × current utilization / optimal-point "
            "utilization. Green below $PcuYellow, yellow to $PcuRed, red beyond. 200% is the "
            "survivor sitting exactly at its latency knee.",
            PCU_THRESH,
        )
    )
    y += 8

    panels.append(
        timeseries(
            next_id(),
            "CPU utilization vs ONTAP optimal point",
            0,
            y,
            12,
            8,
            [
                target(
                    f"headroom_cpu_current_utilization{{{SEL}}}", "current {{node}}", "A"
                ),
                target(
                    f"headroom_cpu_optimal_point_utilization{{{SEL}}}",
                    "optimal {{node}}",
                    "B",
                ),
            ],
            "percent",
            "The optimal point is the latency knee: past it, latency rises "
            "disproportionately to added load.",
        )
    )
    panels.append(
        timeseries(
            next_id(),
            "Headroom confidence factor",
            12,
            y,
            12,
            8,
            [
                target(
                    f"headroom_cpu_optimal_point_confidence_factor{{{SEL}}}", "{{node}}"
                )
            ],
            "short",
            "1 means ONTAP is using a seed value, not a measured curve — expected on lightly "
            "loaded or recently rebooted nodes, and a reason the CPU busy fallback matters.",
            decimals=0,
        )
    )
    y += 8

    panels.append(
        timeseries(
            next_id(),
            "Available CPU ops (optimal - current, confidence > 1 only)",
            0,
            y,
            24,
            6,
            [
                target(
                    f"(\n  headroom_cpu_optimal_point_ops{{{SEL}}}\n"
                    f"  - headroom_cpu_current_ops{{{SEL}}}\n)\n"
                    f"and (headroom_cpu_optimal_point_confidence_factor{{{SEL}}} > 1)",
                    "{{node}}",
                )
            ],
            "iops",
            "Gated on confidence > 1, matching stock Harvest behaviour. Negative means the "
            "node is already running past its optimal-point ops.",
        )
    )
    y += 6

    # --------------------------------------------------------------- CPU busy
    panels.append(row(next_id(), "CPU busy (fallback model, and sanity check)", y))
    y += 1

    panels.append(
        timeseries(
            next_id(),
            "node_cpu_busy per node and per HA pair",
            0,
            y,
            12,
            8,
            [
                target(BUSY_NODE, "{{node}}", "A"),
                target(BUSY_PAIR, "{{survivor}} survives", "B"),
            ],
            "percent",
            "node_cpu_busy tracks the hottest CPU domain or remaining idle, whichever binds "
            "first, so it is the better 'how close to the wall' number. Keep each node under "
            "roughly 40-50% if you want the pair sum to stay green.",
            BUSY_THRESH,
        )
    )
    panels.append(
        timeseries(
            next_id(),
            "Average processor busy per node",
            12,
            y,
            12,
            8,
            [target(AVG_BUSY_NODE, "{{node}}")],
            "percent",
            "Mean across cores. Can look comfortable while a single domain is saturated, "
            "which is why it is not used for the traffic light.",
        )
    )
    y += 8

    # ---------------------------------------------------------------- Ethernet
    panels.append(
        row(next_id(), "Ethernet data ports — NAS, iSCSI and NVMe-TCP", y)
    )
    y += 1

    panels.append(
        timeseries(
            next_id(),
            "NIC utilization per node (cluster ipspace excluded)",
            0,
            y,
            12,
            8,
            [target(NIC_NODE, "{{node}} {{nic}}")],
            "percent",
            "Harvest nic_util_percent is max(rx, tx) on a 0-1 scale, shown here x100. Ports "
            "are filtered by ipspace via net_port_status rather than by name, because cluster "
            "port names differ per platform (e4a/e4b on A30, e1a/e7a on A70 and A90).",
        )
    )
    panels.append(
        timeseries(
            next_id(),
            "Estimated survivor NIC utilization",
            12,
            y,
            12,
            8,
            [target(NIC_PAIR, "{{nic}} on {{survivor}}")],
            "percent",
            "Same port name on both nodes of the pair, added together.",
            PORT_THRESH,
        )
    )
    y += 8

    panels.append(
        timeseries(
            next_id(),
            "ifgrp max(rx, tx) per node",
            0,
            y,
            12,
            8,
            [target(IFGRP_NODE, "{{node}} {{ifgroup}}")],
            "percent",
            "Preferred model for NAS: data LIFs fail over onto the partner's LAG. Harvest "
            "labels these ifgroup and ports, where ports is a comma-joined member list.",
        )
    )
    panels.append(
        timeseries(
            next_id(),
            "Estimated survivor ifgrp utilization",
            12,
            y,
            12,
            8,
            [target(IFGRP_PAIR, "{{ifgroup}} on {{survivor}}")],
            "percent",
            "For equal-speed LAGs with the same name on both nodes, this answers: would the "
            "remaining LAG exceed its capacity?",
            PORT_THRESH,
        )
    )
    y += 8

    panels.append(
        table(
            next_id(),
            "Ethernet ports in scope",
            0,
            y,
            24,
            8,
            [target(f"({DATA_PORTS})", "", instant=True, fmt="table")],
            "^(cluster|node|port|role|port_type|ipspace|broadcast|op_speed|status)$",
            "Exactly the ports the network score considers. If a data port is missing here it "
            "is not being scored; if a cluster port appears, adjust ExcludePorts.",
        )
    )
    y += 8

    # ------------------------------------------------------------------ SAN/FC
    if INCLUDE_FC:
        panels.append(row(next_id(), "SAN (FC) — upper bound", y))
        y += 1
        panels.append(
            timeseries(
                next_id(),
                "FC target port utilization per node",
                0,
                y,
                12,
                8,
                [target(FCP_NODE, "{{node}} {{port}}")],
                "percent",
                "Harvest fcp_util_percent is 0-1, shown x100.",
            )
        )
        panels.append(
            timeseries(
                next_id(),
                "Estimated survivor FC utilization",
                12,
                y,
                12,
                8,
                [target(pair_sum(FCP_NODE, extra=("port",)), "{{port}} on {{survivor}}")],
                "percent",
                "Upper bound only: multipath hosts already send some I/O to the survivor, and "
                "fcp_util_percent carries a speed label, so summing across ports of differing "
                "speed is not meaningful.",
                PORT_THRESH,
            )
        )
        y += 8

    # ----------------------------------------------------------------- context
    panels.append(
        row(next_id(), "Context — is the cluster already hurting? (not scored)", y)
    )
    y += 1

    panels.append(
        timeseries(
            next_id(),
            "Node latency, all protocols",
            0,
            y,
            12,
            8,
            [
                target(f"node_total_latency{{{SEL}}}", "total {{node}}", "A"),
                target(f"node_read_latency{{{SEL}}}", "read {{node}}", "B"),
                target(f"node_write_latency{{{SEL}}}", "write {{node}}", "C"),
            ],
            "µs",
            "From system:node, so a single unambiguous series per node. If latency is already "
            "high, do not plan a takeover even when the score is green.",
        )
    )
    panels.append(
        timeseries(
            next_id(),
            "iSCSI LIF latency",
            12,
            y,
            12,
            8,
            [target(f"iscsi_lif_avg_latency{{{SEL}}}", "{{node}} {{lif}}")],
            "µs",
            "Per-LIF, since Harvest has no per-port iSCSI counter.",
        )
    )
    y += 8

    panels.append(
        timeseries(
            next_id(),
            "Node operations by protocol",
            0,
            y,
            24,
            7,
            [
                target(f"node_nfs_ops{{{SEL}}}", "NFS {{node}}", "A"),
                target(f"node_cifs_ops{{{SEL}}}", "SMB {{node}}", "B"),
                target(f"node_iscsi_ops{{{SEL}}}", "iSCSI {{node}}", "C"),
                target(f"node_nvmf_ops{{{SEL}}}", "NVMe-oF {{node}}", "D"),
            ],
            "iops",
            "Where the work actually is. Useful for judging whether the CPU sum is dominated "
            "by one node's workload.",
        )
    )

    templating = [
        {
            "current": {"selected": False, "text": "Prometheus", "value": "Prometheus"},
            "hide": 2,
            "includeAll": False,
            "label": "Data Source",
            "multi": False,
            "name": "DS_PROMETHEUS",
            "options": [],
            "query": "prometheus",
            "refresh": 1,
            "regex": "",
            "skipUrlSync": False,
            "type": "datasource",
        },
        query_var(
            "Datacenter",
            'label_values(cluster_new_status{system_type!="7mode"}, datacenter)',
        ),
        query_var(
            "Cluster",
            'label_values(cluster_new_status{system_type!="7mode",datacenter=~"$Datacenter"}, cluster)',
        ),
        custom_var("Window", ["15m", "30m", "1h"], "30m", "Percentile window"),
        # Safety net on top of the ipspace filter. Useful when net_port_status is
        # absent (no config collector), in which case this name regex is the only
        # thing keeping cluster ports out of the network score.
        #
        # Preset values match the known cluster-port names per platform:
        #   A70 / A90 : e1a, e7a   A30 : e4a, e4b   A220 / older FAS : e0a, e0b
        # All presets also exclude e0M (management port, 1GbE, never a data port).
        #
        # If your platform is not listed, type a regex directly into the dropdown
        # (allowCustomValue is True) or append to the list below and regenerate.
        #
        # WARNING: display labels must not contain commas — Grafana splits the CSV
        # on commas before splitting on " : ", so "A70, A90" would corrupt the list.
        custom_var(
            "ExcludePorts",
            [
                "e0M",
                ("A70 / A90 (also e1a + e7a)", "e0M|e1a|e7a"),
                ("A30 (also e4a + e4b)", "e0M|e4a|e4b"),
                ("A220 / older FAS (also e0a + e0b)", "e0M|e0a|e0b"),
                ("None", "^$"),
            ],
            "e0M",
            "Also exclude ports",
        ),
        # Thresholds are hidden to keep the header uncluttered. Override per-session with
        # a URL parameter, e.g. ?var-PcuYellow=175 — see docs/DESIGN.md.
        custom_var("PcuYellow", ["150", "160", "175", "180"], "160", "PCU yellow", hide=2),
        custom_var("PcuRed", ["190", "200", "210"], "200", "PCU red", hide=2),
        custom_var("BusyYellow", ["60", "70", "75"], "70", "CPU busy yellow", hide=2),
        custom_var("BusyRed", ["80", "85", "90"], "85", "CPU busy red", hide=2),
        custom_var("PortYellow", ["60", "70", "75"], "70", "Port yellow", hide=2),
        custom_var("PortRed", ["80", "85", "90"], "85", "Port red", hide=2),
        custom_var("ConfMin", ["1", "2", "3"], "2", "Min headroom confidence", hide=2),
    ]

    return {
        "__inputs": [
            {
                "name": "DS_PROMETHEUS",
                "label": "Prometheus",
                "description": "",
                "type": "datasource",
                "pluginId": "prometheus",
                "pluginName": "Prometheus",
            }
        ],
        "__requires": [
            {"type": "grafana", "id": "grafana", "name": "Grafana", "version": "8.3.0"},
            {
                "type": "datasource",
                "id": "prometheus",
                "name": "Prometheus",
                "version": "1.0.0",
            },
            {"type": "panel", "id": "timeseries", "name": "Time series", "version": ""},
            {"type": "panel", "id": "stat", "name": "Stat", "version": ""},
            {
                "type": "panel",
                "id": "state-timeline",
                "name": "State timeline",
                "version": "",
            },
            {"type": "panel", "id": "table", "name": "Table", "version": ""},
            {"type": "panel", "id": "text", "name": "Text", "version": ""},
        ],
        "annotations": {
            "list": [
                {
                    "builtIn": 1,
                    "datasource": GRAFANA_DS,
                    "enable": True,
                    "hide": True,
                    "iconColor": "rgba(0, 211, 255, 1)",
                    "name": "Annotations & Alerts",
                    "type": "dashboard",
                }
            ]
        },
        "description": (
            "ONTAP takeover headroom per HA pair: can the survivor absorb its partner's "
            "CPU and Ethernet load? Cluster score is the worst pair. All-flash scope, "
            "so no aggregate or disk headroom."
        ),
        "editable": True,
        "gnetId": None,
        "graphTooltip": 1,
        "id": None,
        "iteration": 1,
        "links": [
            {
                "asDropdown": True,
                "icon": "external link",
                "includeVars": True,
                "keepTime": True,
                "tags": ["cdot"],
                "targetBlank": False,
                "title": "Related Dashboards",
                "type": "dashboards",
                "url": "",
            }
        ],
        "panels": panels,
        "refresh": "5m",
        "schemaVersion": 30,
        "style": "dark",
        "tags": ["harvest", "ontap", "cdot", "ha", "headroom"],
        "templating": {"list": templating},
        "time": {"from": "now-6h", "to": "now"},
        "timepicker": {},
        "timezone": "",
        "title": "ONTAP: HA Headroom",
        "uid": "cdot-ha-headroom",
        "version": 1,
    }


def main() -> None:
    out = Path(__file__).resolve().parent / "dashboards" / "cmode" / "ha_headroom.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = build()
    out.write_text(json.dumps(data, indent=1) + "\n")
    longest = max(
        (len(t["expr"]) for p in data["panels"] for t in p.get("targets", [])),
        default=0,
    )
    print(
        f"wrote {out} ({len(data['panels'])} panels, "
        f"longest query {longest} chars, FC {'on' if INCLUDE_FC else 'off'})"
    )


if __name__ == "__main__":
    main()
