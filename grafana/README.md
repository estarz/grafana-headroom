# Grafana: ONTAP HA Headroom

Import mechanics for the dashboard. For the model behind it see [../docs/DESIGN.md](../docs/DESIGN.md);
for the queries see [../docs/METRICS.md](../docs/METRICS.md).

## Import

1. Grafana → **Dashboards → Import** → upload
   [`dashboards/cmode/ha_headroom.json`](dashboards/cmode/ha_headroom.json).
2. Select your Prometheus or VictoriaMetrics datasource.
3. Open **ONTAP: HA Headroom** and set **Datacenter** and **Cluster**.

The same generator also writes
[`dashboards/cmode/ha_headroom_debug.json`](dashboards/cmode/ha_headroom_debug.json),
imported the same way. That is **ONTAP: HA Headroom Debug**: every chart behind the
light, including available CPU ops. A link at the top of each dashboard opens the other
and keeps the time range and variables. Both stay in step because one Python file
emits both JSON files.

Alternatively copy the JSON into your Harvest tree as
`grafana/dashboards/cmode/ha_headroom.json` and import with `bin/harvest grafana import`.
Custom files may need to sit beside Harvest's own dashboards for that to pick them up.

The dashboard targets schemaVersion 30 for broad compatibility. Grafana 9 through 13
migrate it on import without intervention.

Datasource references use the object form, `{"type": "prometheus", "uid": "${DS_PROMETHEUS}"}`,
rather than the bare `"${DS_PROMETHEUS}"` string that older Harvest dashboards use. That sets
the floor at Grafana 8.3, and it is deliberate — see [Import fails with a `uid` error](#import-fails-with-a-uid-error).

## Import fails with a `uid` error

If Grafana refuses the file with **Dashboard import failed** and one of:

- `cannot use 'in' operator to search for "uid" in "${DS_PROMETHEUS}"` (Chrome, Edge, Firefox)
- `e is not an Object. (evaluating '"uid" in e')` (Safari)

then Grafana is applying its `__inputs` mapping to a *string* datasource reference and
crashing on it. The check is unguarded in released Grafana, so a bare-string dashboard
cannot be imported through the UI on affected versions. It bites Grafana Cloud and
Grafana 12.4+ in particular; the same file often imports fine on older or self-hosted builds,
which makes it look intermittent.

This is not specific to this dashboard — every stock Harvest dashboard uses the bare-string
form and hits it too, which is why Harvest ships its own `bin/harvest grafana import` that
substitutes the datasource server-side and bypasses the browser entirely. Upstream has
[since taught the importer to accept both forms](https://github.com/grafana/grafana/blob/main/public/app/features/manage-dashboards/import/utils/inputs.ts),
but that fix is not in released versions yet.

Using the object form avoids the crash on every Grafana from 8.3 up, so this repo emits it.
If you hit the error, you are on an old copy of the JSON — pull the current
[`dashboards/cmode/ha_headroom.json`](dashboards/cmode/ha_headroom.json) and re-import. To
confirm which form you have, the file should contain no bare-string datasource:

```bash
grep -c '"datasource": "\${DS_PROMETHEUS}"' dashboards/cmode/ha_headroom.json   # must be 0
```

## First run: check the light

Open the dashboard. The only decision on screen is **Can this cluster survive a node loss?** Green is go, yellow is wait, red is no, blue is Unknown. The band under it is the last 7 days. Everything else is folded into collapsed rows for someone explaining a colour. **HA Headroom Debug**, linked from the top of the dashboard, is the same score with every chart open.

| Panel | Expected | If not |
| --- | --- | --- |
| **Can this cluster survive a node loss?** | OK, Latency risk, or Service impact risk | Unknown means HA pairing or CPU or Ethernet data is missing. Expand **Why this colour** only after the light itself is unexplained. |
| **HA partner coverage** | `1.0` — all nodes paired, inside the detail rows | Enable a Harvest config collector (`Rest` **or** `Zapi`), and confirm Harvest is 24.05 or newer. Below 1.0 the light shows Unknown. |
| **Ethernet ports in scope** | Your data ports, no cluster ports | Adjust `ExcludePorts`. Cluster ports appearing here means the `ipspace` filter did not work. |

## Regenerating

The JSON is generated. Edit the Python, never the JSON.

```bash
python3 generate_ha_headroom.py                    # rebuild the JSON
python3 validate_promql.py /path/to/promtool       # every query parses
python3 test_promql.py     /path/to/promtool       # the HA-pair maths is right
```

`promtool` ships with Prometheus. Neither test needs a running server or a live cluster.

## Variables

Visible in the dashboard header:

| Variable | Default | Purpose |
| --- | --- | --- |
| `Datacenter` / `Cluster` | all | Same as other Harvest cDOT dashboards |
| `Window` | `30m` | Percentile window for the traffic-light inputs |
| `ExcludePorts` | `e0M` | Extra port-name exclusions on top of the `ipspace != "Cluster"` filter. See below. |

Thresholds are hidden to keep the header uncluttered. Override per session with a URL
parameter, or change the defaults in `generate_ha_headroom.py` to make it permanent:

```
/d/cdot-ha-headroom/?var-PcuYellow=175&var-PortYellow=75
```

| Variable | Default | Controls |
| --- | --- | --- |
| `PcuYellow` / `PcuRed` | 160 / 200 | Combined performance capacity used |
| `BusyYellow` / `BusyRed` | 70 / 85 | Combined `node_cpu_busy` |
| `PortYellow` / `PortRed` | 70 / 85 | Estimated survivor port utilization |
| `ConfMin` | 2 | Minimum headroom confidence before PCU is trusted over CPU busy |

## Customizing port exclusions

`ExcludePorts` is a name regex applied on top of the primary `ipspace != "Cluster"` filter.
Its main job is to exclude management ports (`e0M`) and to act as a fallback if
`net_port_status` is absent (no config collector), in which case the regex is the only
thing keeping cluster ports out of the network score.

The dropdown ships with presets for common platforms:

| Preset | Value sent to queries | Cluster ports covered |
| --- | --- | --- |
| `e0M` (default) | `e0M` | — (relies on ipspace filter) |
| `A70 / A90 (also e1a + e7a)` | `e0M\|e1a\|e7a` | A70, A90 |
| `A30 (also e4a + e4b)` | `e0M\|e4a\|e4b` | A30 |
| `A220 / older FAS (also e0a + e0b)` | `e0M\|e0a\|e0b` | A220, older FAS/AFF |
| `None` | `^$` | nothing extra excluded |

**Three ways to change it:**

1. **Type a regex directly into the dropdown** (per-session, nothing to edit). Grafana's
   "Allow custom values" option is enabled, so you can type e.g. `e0M|e2a|e2b` and press
   Enter to make it the selected value for that session.
2. **URL parameter** (shareable link): `?var-ExcludePorts=e0M%7Ce2a%7Ce2b`. Useful for
   linking colleagues to the right view for their platform.
3. **Edit the generator and regenerate** (permanent): add a new tuple to the
   `ExcludePorts` list in `generate_ha_headroom.py` and run `python3
   generate_ha_headroom.py`. Display labels must not contain commas.

**Two caveats:**

- Picking the wrong preset excludes real data ports and silently *under*-reports network
  risk. Selecting "A30" on an A70 would drop `e4a`/`e4b`, which are data ports there.
- A mixed-model cluster (e.g. one A70 node and one A90 node with different cluster ports)
  has no single correct preset. In that case the `ipspace` filter handles the exclusion
  correctly on its own — leave `ExcludePorts` at the default `e0M`.

## Generator options

`INCLUDE_FC` in `generate_ha_headroom.py` adds FC target-port panels and folds
`fcp_util_percent` into the network score. Off by default — read the caveat in
[../docs/ROADMAP.md](../docs/ROADMAP.md#fc-support) before enabling it.
