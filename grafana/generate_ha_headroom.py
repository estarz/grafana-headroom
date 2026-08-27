#!/usr/bin/env python3
"""Generate grafana/dashboards/cmode/ha_headroom.json (Harvest-style Grafana 8+ import)."""

from __future__ import annotations

import json
from pathlib import Path

DS = "${DS_PROMETHEUS}"
SEL = 'datacenter=~"$Datacenter",cluster=~"$Cluster"'
NIC_SEL = f'{SEL},nic!~"$ExcludePorts"'
IFGRP_SEL = f'{SEL},ports!=""'

PCU = f"""sum by (cluster) (
  100 *
    headroom_cpu_current_utilization{{{SEL}}}
    /
    clamp_min(headroom_cpu_optimal_point_utilization{{{SEL}}}, 1e-3)
)"""

PCU_P95 = f"quantile_over_time(0.95, ({PCU})[$Window:])"

CPU_BUSY_SUM = f"sum by (cluster) (node_cpu_busy{{{SEL}}})"
CPU_BUSY_P95 = f"quantile_over_time(0.95, ({CPU_BUSY_SUM})[$Window:])"

CONF_MIN = f"min by (cluster) (headroom_cpu_optimal_point_confidence_factor{{{SEL}}})"

IFGRP_MAX_DIR = f"""(
  nic_ifgrp_rx_perc{{{IFGRP_SEL}}}
  + nic_ifgrp_tx_perc{{{IFGRP_SEL}}}
  + abs(nic_ifgrp_rx_perc{{{IFGRP_SEL}}} - nic_ifgrp_tx_perc{{{IFGRP_SEL}}})
) / 2 * 100"""

IFGRP_SURVIVOR = f"sum by (cluster, ifgroup) ({IFGRP_MAX_DIR})"
IFGRP_HOT_P95 = f"quantile_over_time(0.95, (max by (cluster) ({IFGRP_SURVIVOR}))[$Window:])"

NIC_SURVIVOR = f"sum by (cluster, nic) (nic_util_percent{{{NIC_SEL}}}) * 100"
NIC_HOT_P95 = f"quantile_over_time(0.95, (max by (cluster) ({NIC_SURVIVOR}))[$Window:])"

FCP_SURVIVOR = f"sum by (cluster, port) (fcp_util_percent{{{SEL}}}) * 100"
FCP_HOT_P95 = f"quantile_over_time(0.95, (max by (cluster) ({FCP_SURVIVOR}))[$Window:])"

PORT_HOT = f"""max by (cluster) (
  ({NIC_HOT_P95})
  or ({IFGRP_HOT_P95})
  or ({FCP_HOT_P95})
)"""

CPU_RISK = f"""(
  (({CONF_MIN}) >= bool 2)
  *
  clamp_max(
    ((({PCU_P95}) >= bool 160) + (({PCU_P95}) >= bool 200)),
    2
  )
)
+
(
  (({CONF_MIN}) < bool 2)
  *
  clamp_max(
    ((({CPU_BUSY_P95}) >= bool 70) + (({CPU_BUSY_P95}) >= bool 85)),
    2
  )
)"""

PORT_RISK = f"""clamp_max(
  (({PORT_HOT}) >= bool 70) + (({PORT_HOT}) >= bool 85),
  2
)"""

OVERALL = f"max by (cluster) (({CPU_RISK}) or ({PORT_RISK}))"


def target(expr: str, legend: str = "", ref_id: str = "A", instant: bool = False) -> dict:
    t = {
        "datasource": DS,
        "exemplar": False,
        "expr": expr.strip(),
        "interval": "",
        "legendFormat": legend,
        "refId": ref_id,
    }
    if instant:
        t["instant"] = True
    return t


def thresholds(*steps: tuple[str, float | None]) -> dict:
    out = []
    for color, value in steps:
        item = {"color": color, "value": value}
        out.append(item)
    return {"mode": "absolute", "steps": out}


def field_defaults(unit: str = "short", **extra) -> dict:
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
    cfg.update(extra)
    return cfg


def timeseries(pid: int, title: str, x: int, y: int, w: int, h: int, targets: list, unit: str = "percent", description: str = "", thresh=None) -> dict:
    defaults = field_defaults(unit)
    if thresh:
        defaults["thresholds"] = thresh
        defaults["custom"]["thresholdsStyle"] = {"mode": "line+area"}
        defaults["color"] = {"mode": "thresholds"}
    return {
        "datasource": DS,
        "description": description,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": pid,
        "options": {
            "legend": {"calcs": ["mean", "lastNotNull", "max"], "displayMode": "table", "placement": "bottom"},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": targets,
        "title": title,
        "type": "timeseries",
    }


def stat(pid: int, title: str, x: int, y: int, w: int, h: int, targets: list, unit: str, thresh, description: str = "", mappings=None) -> dict:
    defaults = {
        "color": {"mode": "thresholds"},
        "mappings": mappings or [],
        "thresholds": thresh,
        "unit": unit,
    }
    return {
        "datasource": DS,
        "description": description,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": pid,
        "options": {
            "colorMode": "background",
            "graphMode": "area",
            "justifyMode": "auto",
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "auto",
        },
        "pluginVersion": "8.1.8",
        "targets": targets,
        "title": title,
        "type": "stat",
    }


def row(pid: int, title: str, y: int) -> dict:
    return {
        "collapsed": False,
        "datasource": DS,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "id": pid,
        "panels": [],
        "title": title,
        "type": "row",
    }


def text_panel(pid: int, y: int) -> dict:
    md = """**How to read this dashboard**

Score is **takeover on the surviving node**, not current per-node health. Overall = worst of CPU and ports (95th percentile over **$Window**).

- **Green** — takeover should not cause a problem
- **Yellow** — risk of higher latency; avoid planned failover at peak
- **Red** — risk of service interruption / unacceptable latency

CPU uses combined **performance capacity used** (PCU = current / optimal × 100, summed across the HA pair) when headroom confidence ≥ 2; otherwise the sum of `node_cpu_busy`. Ports sum util of the same ifgrp/NIC/FC port **name** on both nodes. Cluster/mgmt NICs matching **ExcludePorts** are ignored.

**Caveats:** summing CPU is a proxy (HA overhead still exists). SAN multipath can make FC sums an **upper bound**. This is not AIUM estimated ms/op after takeover. Aggregate/disk headroom is omitted (AFF)."""
    return {
        "datasource": DS,
        "gridPos": {"h": 8, "w": 8, "x": 16, "y": y},
        "id": pid,
        "options": {"content": md, "mode": "markdown"},
        "title": "Notes",
        "type": "text",
    }


PCU_THRESH = thresholds(("green", None), ("yellow", 160), ("red", 200))
BUSY_THRESH = thresholds(("green", None), ("yellow", 70), ("red", 85))
PORT_THRESH = thresholds(("green", None), ("yellow", 70), ("red", 85))
RISK_THRESH = thresholds(("green", None), ("yellow", 1), ("red", 2))
CONF_THRESH = thresholds(("red", None), ("yellow", 2), ("green", 3))

RISK_MAP = [
    {"type": "value", "options": {"0": {"text": "OK — takeover headroom", "color": "green"}}},
    {"type": "range", "options": {"from": 0.5, "to": 1.5, "result": {"text": "Latency risk", "color": "yellow"}}},
    {"type": "range", "options": {"from": 1.5, "to": 9, "result": {"text": "Service interruption risk", "color": "red"}}},
]


def query_var(name: str, query: str, label=None, include_all=True, hide=0, regex="") -> dict:
    return {
        "allValue": ".*" if include_all else None,
        "current": {},
        "datasource": DS,
        "definition": query,
        "description": None,
        "error": None,
        "hide": hide,
        "includeAll": include_all,
        "label": label,
        "multi": True if include_all else False,
        "name": name,
        "options": [],
        "query": {"query": query, "refId": "StandardVariableQuery"},
        "refresh": 2,
        "regex": regex,
        "skipUrlSync": False,
        "sort": 7,
        "type": "query",
    }


def build() -> dict:
    panels = []
    y = 0
    pid = 1

    panels.append(row(pid, "Overall HA headroom", y))
    pid += 1
    y += 1

    panels.append(
        stat(
            pid,
            "Overall HA headroom",
            0,
            y,
            8,
            8,
            [target(OVERALL, "{{cluster}}", instant=True)],
            "short",
            RISK_THRESH,
            "Worst of CPU risk and port risk. 0 green / 1 yellow / 2 red. Uses 95th percentile over $Window. CPU uses PCU when min confidence ≥ 2, else sum of node_cpu_busy.",
            RISK_MAP,
        )
    )
    pid += 1
    panels.append(
        stat(
            pid,
            "Combined PCU (95th)",
            8,
            y,
            4,
            4,
            [target(PCU_P95, "{{cluster}}", instant=True)],
            "percent",
            PCU_THRESH,
            "Sum of per-node performance capacity used. 200% ≈ survivor at the latency knee (AIUM HA pair over-utilized).",
        )
    )
    pid += 1
    panels.append(
        stat(
            pid,
            "Sum CPU busy (95th)",
            12,
            y,
            4,
            4,
            [target(CPU_BUSY_P95, "{{cluster}}", instant=True)],
            "percent",
            BUSY_THRESH,
            "Sum of node_cpu_busy (hottest CPU domain). Fallback when headroom confidence is low.",
        )
    )
    pid += 1
    panels.append(text_panel(pid, y))
    pid += 1

    y2 = y + 4
    panels.append(
        stat(
            pid,
            "Headroom confidence (min)",
            8,
            y2,
            4,
            4,
            [target(CONF_MIN, "{{cluster}}", instant=True)],
            "short",
            CONF_THRESH,
            "0 unknown, 1 low (do not trust PCU), 2 medium, 3 high.",
        )
    )
    pid += 1
    panels.append(
        stat(
            pid,
            "Hottest survivor port (95th)",
            12,
            y2,
            4,
            4,
            [
                target(NIC_HOT_P95, "NIC {{cluster}}", "A", True),
                target(IFGRP_HOT_P95, "ifgrp {{cluster}}", "B", True),
                target(FCP_HOT_P95, "FC {{cluster}}", "C", True),
            ],
            "percent",
            PORT_THRESH,
            "Max estimated survivor util: sum of same-named NIC / ifgrp / FC port across both nodes. nic_util and fcp_util are 0–1 in Harvest.",
        )
    )
    pid += 1
    y += 8

    panels.append(row(pid, "CPU performance capacity", y))
    pid += 1
    y += 1

    pcu_node = f"""100 *
  headroom_cpu_current_utilization{{{SEL}}}
  /
  clamp_min(headroom_cpu_optimal_point_utilization{{{SEL}}}, 1e-3)"""
    panels.append(
        timeseries(
            pid,
            "PCU per node",
            0,
            y,
            12,
            8,
            [target(pcu_node, "{{node}}")],
            "percent",
            "Per-node performance capacity used = 100 × current_utilization / optimal_point_utilization.",
        )
    )
    pid += 1
    panels.append(
        timeseries(
            pid,
            "Combined PCU (takeover estimate)",
            12,
            y,
            12,
            8,
            [target(PCU, "{{cluster}}")],
            "percent",
            "Green < 160, yellow 160–200, red > 200 (AIUM pair over-utilized at 200%).",
            PCU_THRESH,
        )
    )
    pid += 1
    y += 8

    panels.append(
        timeseries(
            pid,
            "CPU utilization vs optimal point",
            0,
            y,
            12,
            8,
            [
                target(f"headroom_cpu_current_utilization{{{SEL}}}", "current {{node}}", "A"),
                target(f"headroom_cpu_optimal_point_utilization{{{SEL}}}", "optimal {{node}}", "B"),
            ],
            "percent",
            "Current average CPU vs ONTAP optimal-point utilization (latency knee).",
        )
    )
    pid += 1
    panels.append(
        timeseries(
            pid,
            "Available CPU ops (optimal − current)",
            12,
            y,
            12,
            8,
            [
                target(
                    f"headroom_cpu_optimal_point_ops{{{SEL}}} - headroom_cpu_current_ops{{{SEL}}}",
                    "{{node}}",
                )
            ],
            "iops",
            "Negative means current ops already exceed the optimal-point ops.",
        )
    )
    pid += 1
    y += 8

    panels.append(
        timeseries(
            pid,
            "Headroom confidence factor",
            0,
            y,
            24,
            6,
            [target(f"headroom_cpu_optimal_point_confidence_factor{{{SEL}}}", "{{node}}")],
            "short",
            "1 = seed/low — ignore PCU and use CPU busy. 2–3 = usable optimal point.",
        )
    )
    pid += 1
    y += 6

    panels.append(row(pid, "CPU busy (hottest domain)", y))
    pid += 1
    y += 1
    panels.append(
        timeseries(
            pid,
            "node_cpu_busy per node and HA sum",
            0,
            y,
            12,
            8,
            [
                target(f"node_cpu_busy{{{SEL}}}", "{{node}}", "A"),
                target(CPU_BUSY_SUM, "sum (takeover est.)", "B"),
            ],
            "percent",
            "node_cpu_busy is limited by the hottest CPU domain or remaining idle, whichever binds first.",
            BUSY_THRESH,
        )
    )
    pid += 1
    panels.append(
        timeseries(
            pid,
            "Average processor busy (per node and sum)",
            12,
            y,
            12,
            8,
            [
                target(f"node_avg_processor_busy{{{SEL}}}", "{{node}}", "A"),
                target(f"sum by (cluster) (node_avg_processor_busy{{{SEL}}})", "sum", "B"),
            ],
            "percent",
            "Average across cores — can look fine while one domain is saturated. Prefer node_cpu_busy for the traffic light.",
        )
    )
    pid += 1
    y += 8

    panels.append(row(pid, "Ethernet / ifgrp (exclude cluster and mgmt)", y))
    pid += 1
    y += 1
    panels.append(
        timeseries(
            pid,
            "NIC util % (cluster/mgmt excluded)",
            0,
            y,
            12,
            8,
            [target(f"nic_util_percent{{{NIC_SEL}}} * 100", "{{node}} {{nic}}")],
            "percent",
            "Harvest nic_util_percent is 0–1. ExcludePorts default e0a|e0b|e0M.",
        )
    )
    pid += 1
    panels.append(
        timeseries(
            pid,
            "Estimated survivor NIC util (sum by port name)",
            12,
            y,
            12,
            8,
            [target(NIC_SURVIVOR, "{{nic}}")],
            "percent",
            "Same port name on both HA nodes added together. Green < 70, yellow 70–85, red > 85.",
            PORT_THRESH,
        )
    )
    pid += 1
    y += 8
    panels.append(
        timeseries(
            pid,
            "ifgrp max(rx,tx) % per node",
            0,
            y,
            12,
            8,
            [target(IFGRP_MAX_DIR, "{{node}} {{ifgroup}}")],
            "percent",
            "Preferred NAS model: LIFs fail over onto the partner ifgrp.",
        )
    )
    pid += 1
    panels.append(
        timeseries(
            pid,
            "Estimated survivor ifgrp util (sum by ifgroup)",
            12,
            y,
            12,
            8,
            [target(IFGRP_SURVIVOR, "{{ifgroup}}")],
            "percent",
            "Equal-speed ifgrps named the same on both nodes: will the remaining LAG exceed 100%?",
            PORT_THRESH,
        )
    )
    pid += 1
    y += 8

    panels.append(row(pid, "SAN (FC) — upper bound", y))
    pid += 1
    y += 1
    panels.append(
        timeseries(
            pid,
            "FC port util %",
            0,
            y,
            12,
            8,
            [target(f"fcp_util_percent{{{SEL}}} * 100", "{{node}} {{port}}")],
            "percent",
            "Harvest fcp_util_percent is 0–1. Empty if the cluster has no FC.",
        )
    )
    pid += 1
    panels.append(
        timeseries(
            pid,
            "Estimated survivor FC util (sum by port name)",
            12,
            y,
            12,
            8,
            [target(FCP_SURVIVOR, "{{port}}")],
            "percent",
            "Upper bound: hosts with multipath already send I/O to both nodes.",
            PORT_THRESH,
        )
    )
    pid += 1
    y += 8

    panels.append(row(pid, "Context — current latency (not in traffic light)", y))
    pid += 1
    y += 1
    panels.append(
        timeseries(
            pid,
            "NFS latency",
            0,
            y,
            12,
            8,
            [target(f"node_nfs_latency{{{SEL}}}", "{{node}}")],
            "µs",
            "If latency is already high, do not plan a takeover even if headroom is green.",
        )
    )
    pid += 1
    panels.append(
        timeseries(
            pid,
            "FCP read/write latency",
            12,
            y,
            12,
            8,
            [
                target(f"fcp_avg_read_latency{{{SEL}}}", "read {{node}} {{port}}", "A"),
                target(f"fcp_avg_write_latency{{{SEL}}}", "write {{node}} {{port}}", "B"),
            ],
            "µs",
        )
    )
    pid += 1

    dashboard = {
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
            {"type": "grafana", "id": "grafana", "name": "Grafana", "version": "8.1.8"},
            {"type": "datasource", "id": "prometheus", "name": "Prometheus", "version": "1.0.0"},
            {"type": "panel", "id": "timeseries", "name": "Time series", "version": ""},
            {"type": "panel", "id": "stat", "name": "Stat", "version": ""},
            {"type": "panel", "id": "text", "name": "Text", "version": ""},
        ],
        "annotations": {
            "list": [
                {
                    "builtIn": 1,
                    "datasource": "-- Grafana --",
                    "enable": True,
                    "hide": True,
                    "iconColor": "rgba(0, 211, 255, 1)",
                    "name": "Annotations & Alerts",
                    "type": "dashboard",
                }
            ]
        },
        "description": "2-node AFF HA takeover headroom: CPU PCU + Ethernet/ifgrp/FC survivor util. Not stock ONTAP: Headroom (no aggregate).",
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
        "refresh": "1m",
        "schemaVersion": 30,
        "style": "dark",
        "tags": ["harvest", "ontap", "cdot", "ha", "headroom"],
        "templating": {
            "list": [
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
                {
                    "current": {"selected": True, "text": "30m", "value": "30m"},
                    "hide": 0,
                    "includeAll": False,
                    "label": "Percentile window",
                    "multi": False,
                    "name": "Window",
                    "options": [
                        {"selected": False, "text": "15m", "value": "15m"},
                        {"selected": True, "text": "30m", "value": "30m"},
                    ],
                    "query": "15m,30m",
                    "skipUrlSync": False,
                    "type": "custom",
                },
                {
                    "current": {"selected": True, "text": "e0a|e0b|e0M", "value": "e0a|e0b|e0M"},
                    "hide": 0,
                    "includeAll": False,
                    "label": "Exclude NIC regex",
                    "multi": False,
                    "name": "ExcludePorts",
                    "options": [
                        {"selected": True, "text": "e0a|e0b|e0M", "value": "e0a|e0b|e0M"},
                        {"selected": False, "text": "e0a|e0b|e0c|e0d|e0M", "value": "e0a|e0b|e0c|e0d|e0M"},
                    ],
                    "query": "e0a|e0b|e0M,e0a|e0b|e0c|e0d|e0M",
                    "skipUrlSync": False,
                    "type": "custom",
                },
            ]
        },
        "time": {"from": "now-3h", "to": "now"},
        "timepicker": {},
        "timezone": "",
        "title": "ONTAP: HA Headroom",
        "uid": "cdot-ha-headroom",
        "version": 1,
    }
    return dashboard


def main() -> None:
    out = Path(__file__).resolve().parent / "dashboards" / "cmode" / "ha_headroom.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = build()
    out.write_text(json.dumps(data, indent=1) + "\n")
    print(f"wrote {out} ({len(data['panels'])} panels)")


if __name__ == "__main__":
    main()
