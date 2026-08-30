#!/usr/bin/env python3
"""Semantic tests for the HA-pair PromQL, using promtool's offline test engine.

Parse-checking (validate_promql.py) cannot tell you whether the label matching
produces the series you meant. This builds synthetic Harvest-shaped series for
three clusters and asserts exact expected values, which is what actually proves:

  * a 4-node cluster is scored per HA pair and pairs never bleed into each other
  * the cluster roll-up is the worst pair
  * a low headroom confidence factor really does force the node_cpu_busy fallback
  * cluster-interconnect ports are excluded by ipspace
  * clusters with no net_port series still get scored via the name-regex fallback

    python3 grafana/test_promql.py [path/to/promtool]

Expressions are imported from generate_ha_headroom.py so the tests cannot drift
away from the dashboard.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

spec = importlib.util.spec_from_file_location("gen", HERE / "generate_ha_headroom.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

# Match all datacenters and clusters so the fixtures below are visible.
SUBSTITUTIONS = {
    "$Datacenter": ".*",
    "$Cluster": ".*",
    "$Window": "30m",
    "$ExcludePorts": "e0M",
    "$PcuYellow": "160",
    "$PcuRed": "200",
    "$BusyYellow": "70",
    "$BusyRed": "85",
    "$PortYellow": "70",
    "$PortRed": "85",
    "$ConfMin": "2",
}

STEPS = 45
EVAL_TIME = "40m"


def resolve(expr: str) -> str:
    for var, value in SUBSTITUTIONS.items():
        expr = expr.replace(var, value)
    return expr.strip()


def flat(value: float) -> str:
    return f"{value}+0x{STEPS}"


def series(name: str, labels: dict[str, str], value: float) -> dict:
    rendered = ",".join(f'{k}="{v}"' for k, v in labels.items())
    return {"series": f"{name}{{{rendered}}}", "values": flat(value)}


def node_label_series(cluster: str, node: str, partner: str, model: str) -> dict:
    return series(
        "node_labels",
        {
            "datacenter": "dc1",
            "cluster": cluster,
            "node": node,
            "ha_partner": partner,
            "model": model,
        },
        1,
    )


def build_input() -> list[dict]:
    inputs: list[dict] = []

    # ---- c1: 2-node A90 pair, high headroom confidence, real net_port topology.
    for node, partner in (("n1", "n2"), ("n2", "n1")):
        inputs.append(node_label_series("c1", node, partner, "AFF-A90"))
    for node, current, busy, e1a in (("n1", 40, 45, 0.30), ("n2", 30, 35, 0.25)):
        base = {"datacenter": "dc1", "cluster": "c1", "node": node}
        inputs.append(series("headroom_cpu_current_utilization", {**base, "headroom": "cpu"}, current))
        inputs.append(series("headroom_cpu_optimal_point_utilization", {**base, "headroom": "cpu"}, 80))
        inputs.append(series("headroom_cpu_optimal_point_confidence_factor", {**base, "headroom": "cpu"}, 3))
        inputs.append(series("node_cpu_busy", base, busy))
        inputs.append(series("nic_util_percent", {**base, "nic": "e1a"}, e1a))
        # e1b is the cluster interconnect and is deliberately saturated: it must not
        # be able to influence the score.
        inputs.append(series("nic_util_percent", {**base, "nic": "e1b"}, 0.90))
        inputs.append(series("net_port_status", {**base, "port": "e1a", "ipspace": "Default"}, 1))
        inputs.append(series("net_port_status", {**base, "port": "e1b", "ipspace": "Cluster"}, 1))

    # ---- c2: 4-node A70 cluster, two pairs with very different load. Headroom
    # confidence is 1 with a deliberately alarming PCU, so the gate must ignore it.
    for node, partner in (("n1", "n2"), ("n2", "n1"), ("n3", "n4"), ("n4", "n3")):
        inputs.append(node_label_series("c2", node, partner, "AFF-A70"))
    for node, busy in (("n1", 10), ("n2", 20), ("n3", 60), ("n4", 70)):
        base = {"datacenter": "dc1", "cluster": "c2", "node": node}
        inputs.append(series("headroom_cpu_current_utilization", {**base, "headroom": "cpu"}, 90))
        inputs.append(series("headroom_cpu_optimal_point_utilization", {**base, "headroom": "cpu"}, 50))
        inputs.append(series("headroom_cpu_optimal_point_confidence_factor", {**base, "headroom": "cpu"}, 1))
        inputs.append(series("node_cpu_busy", base, busy))

    # ---- c3: 2-node cluster with no net_port series at all, exercising the
    # name-regex fallback. e0M is saturated and must stay out of the score.
    for node, partner in (("n1", "n2"), ("n2", "n1")):
        inputs.append(node_label_series("c3", node, partner, "AFF-A30"))
    for node in ("n1", "n2"):
        base = {"datacenter": "dc1", "cluster": "c3", "node": node}
        inputs.append(series("node_cpu_busy", base, 10))
        inputs.append(series("nic_util_percent", {**base, "nic": "e1a"}, 0.40))
        inputs.append(series("nic_util_percent", {**base, "nic": "e0M"}, 0.99))

    return inputs


def sample(labels: dict[str, str], value: float) -> dict:
    rendered = ",".join(f'{k}="{v}"' for k, v in labels.items())
    return {"labels": f"{{{rendered}}}", "value": value}


def pair(cluster: str, survivor: str, victim: str, **extra) -> dict:
    return {"datacenter": "dc1", "cluster": cluster, "survivor": survivor, "victim": victim, **extra}


def build_expectations() -> list[dict]:
    # PCU: c1 n1 = 100*40/80 = 50, n2 = 100*30/80 = 37.5 -> pair 87.5
    pcu_c1 = 87.5
    cpu_index_c1 = min(pcu_c1 / 160, 1) + max((pcu_c1 - 160) / 40, 0)

    # c1 network: e1a 30% + 25% = 55%. e1b is in the Cluster ipspace and excluded.
    nic_c1 = 55.0
    port_index_c1 = min(nic_c1 / 70, 1) + max((nic_c1 - 70) / 15, 0)
    overall_c1 = max(cpu_index_c1, port_index_c1)

    # c2 falls back to node_cpu_busy because confidence is 1.
    busy_quiet, busy_hot = 30.0, 130.0
    index_quiet = min(busy_quiet / 70, 1) + max((busy_quiet - 70) / 15, 0)
    index_hot = min(busy_hot / 70, 1) + max((busy_hot - 70) / 15, 0)

    tests = [
        {
            "name": "pair sums stay inside their own HA pair",
            "expr": gen.BUSY_PAIR,
            "exp_samples": [
                sample(pair("c1", "n1", "n2"), 80),
                sample(pair("c1", "n2", "n1"), 80),
                sample(pair("c2", "n1", "n2"), busy_quiet),
                sample(pair("c2", "n2", "n1"), busy_quiet),
                sample(pair("c2", "n3", "n4"), busy_hot),
                sample(pair("c2", "n4", "n3"), busy_hot),
                sample(pair("c3", "n1", "n2"), 20),
                sample(pair("c3", "n2", "n1"), 20),
            ],
        },
        {
            "name": "combined PCU is summed per pair",
            "expr": gen.PCU_PAIR,
            "exp_samples": [
                sample(pair("c1", "n1", "n2"), pcu_c1),
                sample(pair("c1", "n2", "n1"), pcu_c1),
                sample(pair("c2", "n1", "n2"), 360),
                sample(pair("c2", "n2", "n1"), 360),
                sample(pair("c2", "n3", "n4"), 360),
                sample(pair("c2", "n4", "n3"), 360),
            ],
        },
        {
            "name": "cluster interconnect and e0M are excluded from the network score",
            "expr": gen.NIC_PAIR,
            "exp_samples": [
                sample(pair("c1", "n1", "n2", nic="e1a"), nic_c1),
                sample(pair("c1", "n2", "n1", nic="e1a"), nic_c1),
                sample(pair("c3", "n1", "n2", nic="e1a"), 80),
                sample(pair("c3", "n2", "n1", nic="e1a"), 80),
            ],
        },
        {
            "name": "low headroom confidence forces the CPU busy fallback",
            "expr": gen.CPU_INDEX,
            "exp_samples": [
                sample(pair("c1", "n1", "n2"), cpu_index_c1),
                sample(pair("c1", "n2", "n1"), cpu_index_c1),
                sample(pair("c2", "n1", "n2"), index_quiet),
                sample(pair("c2", "n2", "n1"), index_quiet),
                sample(pair("c2", "n3", "n4"), index_hot),
                sample(pair("c2", "n4", "n3"), index_hot),
                sample(pair("c3", "n1", "n2"), min(20 / 70, 1)),
                sample(pair("c3", "n2", "n1"), min(20 / 70, 1)),
            ],
        },
        {
            "name": "overall pair risk is the worst of CPU and network",
            "expr": gen.OVERALL_PAIR,
            "exp_samples": [
                sample(pair("c1", "n1", "n2"), overall_c1),
                sample(pair("c1", "n2", "n1"), overall_c1),
                sample(pair("c2", "n1", "n2"), index_quiet),
                sample(pair("c2", "n2", "n1"), index_quiet),
                sample(pair("c2", "n3", "n4"), index_hot),
                sample(pair("c2", "n4", "n3"), index_hot),
                sample(pair("c3", "n1", "n2"), min(80 / 70, 1) + max((80 - 70) / 15, 0)),
                sample(pair("c3", "n2", "n1"), min(80 / 70, 1) + max((80 - 70) / 15, 0)),
            ],
        },
        {
            "name": "cluster roll-up is the worst pair, not a cluster-wide sum",
            "expr": gen.OVERALL_CLUSTER,
            "exp_samples": [
                sample({"datacenter": "dc1", "cluster": "c1"}, overall_c1),
                sample({"datacenter": "dc1", "cluster": "c2"}, index_hot),
                sample(
                    {"datacenter": "dc1", "cluster": "c3"},
                    min(80 / 70, 1) + max((80 - 70) / 15, 0),
                ),
            ],
        },
        {
            "name": "HA partner coverage is complete for all fixtures",
            "expr": gen.HA_COVERAGE,
            "exp_samples": [
                sample({"datacenter": "dc1", "cluster": "c1"}, 1),
                sample({"datacenter": "dc1", "cluster": "c2"}, 1),
                sample({"datacenter": "dc1", "cluster": "c3"}, 1),
            ],
        },
    ]

    for t in tests:
        t["expr"] = resolve(t["expr"])
        t["eval_time"] = EVAL_TIME
    return tests


def find_promtool() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    found = shutil.which("promtool")
    if found:
        return found
    sys.exit(
        "promtool not found. Install Prometheus or pass the path:\n"
        "  python3 grafana/test_promql.py /path/to/promtool"
    )


def main() -> None:
    promtool = find_promtool()
    expectations = build_expectations()

    # promtool's promqlTestCase has no `name` field, so it is kept only for reporting.
    cases = [{k: v for k, v in t.items() if k != "name"} for t in expectations]
    suite = {
        "evaluation_interval": "1m",
        "tests": [
            {
                "interval": "1m",
                "input_series": build_input(),
                "promql_expr_test": cases,
            }
        ],
    }

    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
        json.dump(suite, fh)
        suite_file = fh.name

    result = subprocess.run(
        [promtool, "test", "rules", suite_file],
        capture_output=True,
        text=True,
    )
    Path(suite_file).unlink()

    print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode == 0:
        for t in expectations:
            print(f"  ok  {t['name']}")
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
