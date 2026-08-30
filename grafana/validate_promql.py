#!/usr/bin/env python3
"""Parse-check every PromQL query in the generated dashboard.

Grafana variables are substituted with their defaults, then each query is wrapped in
a Prometheus recording rule and handed to `promtool check rules`, which is the only
offline PromQL parser Prometheus ships. This catches syntax errors and malformed
label_replace / bool / subquery constructs without needing a live TSDB.

    python3 grafana/validate_promql.py [path/to/promtool]

Semantics -- whether the label matching produces the series you expect -- still has
to be checked against real data. See docs/METRICS.md.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Defaults matching the dashboard's template variables.
SUBSTITUTIONS = {
    "$Datacenter": "dc1",
    "$Cluster": "cluster1",
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

DASHBOARD = Path(__file__).resolve().parent / "dashboards" / "cmode" / "ha_headroom.json"


def resolve(expr: str) -> str:
    for var, value in SUBSTITUTIONS.items():
        expr = expr.replace(var, value)
    return expr


def collect() -> list[tuple[str, str, str]]:
    """Return (panel title, refId, resolved expr) for every target."""
    dashboard = json.loads(DASHBOARD.read_text())
    found = []
    for panel in dashboard["panels"]:
        for tgt in panel.get("targets", []):
            expr = tgt.get("expr")
            if expr:
                found.append((panel.get("title", "?"), tgt.get("refId", "?"), resolve(expr)))
    return found


def find_promtool() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    found = shutil.which("promtool")
    if found:
        return found
    sys.exit(
        "promtool not found. Install Prometheus or pass the path:\n"
        "  python3 grafana/validate_promql.py /path/to/promtool"
    )


def main() -> None:
    promtool = find_promtool()
    queries = collect()
    if not queries:
        sys.exit(f"no queries found in {DASHBOARD}")

    rules = {
        "groups": [
            {
                "name": "ha_headroom_syntax_check",
                "rules": [
                    {"record": f"check:q{i}", "expr": expr}
                    for i, (_, _, expr) in enumerate(queries)
                ],
            }
        ]
    }

    # JSON is valid YAML, which avoids any block-scalar quoting surprises.
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
        json.dump(rules, fh)
        rule_file = fh.name

    result = subprocess.run(
        [promtool, "check", "rules", rule_file],
        capture_output=True,
        text=True,
    )
    Path(rule_file).unlink()

    print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)

    if result.returncode != 0:
        print("\nQueries submitted, in order:", file=sys.stderr)
        for i, (title, ref, _) in enumerate(queries):
            print(f"  check:q{i}  {title} [{ref}]", file=sys.stderr)
        sys.exit(result.returncode)

    longest = max(len(expr) for _, _, expr in queries)
    print(f"{len(queries)} queries parsed, longest {longest} chars")


if __name__ == "__main__":
    main()
