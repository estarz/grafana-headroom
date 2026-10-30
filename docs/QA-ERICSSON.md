# Q&A: DC286 takeover light after the 15 October upgrade

Brief for the Ericsson session. No change to the scoring model. The dashboard
Ericsson imported on 15 September is `main` at `03dcc9f`, plus the wording and
Unknown gate described at the end.

## What the light is

A historical estimate of CPU and Ethernet headroom for an HA takeover, using the
95th percentile over the selected window (default 30 minutes). It is not proof
that the surviving controller can carry every workload. Unmodelled: HA overhead,
NVRAM, the HA interconnect, and the way the latency knee itself moves after
takeover. iSCSI and NVMe-TCP are scored only as Ethernet utilization on the
ports those LIFs fail over onto.

Green means a takeover should not cause a problem. Yellow means avoid a planned
failover at peak. Red means expect unacceptable latency or loss. Unknown means
the score was not evaluated.

## PCU

PCU is performance capacity used:

100 × current utilization / optimal-point utilization.

It is how full the node is relative to ONTAP's latency knee, not raw CPU percent.
The pair score adds the two nodes. 200% combined means the survivor would sit
exactly on the knee. That is the line Active IQ Unified Manager calls "HA pair
over-utilized". It is not an ONTAP hard limit. The green line at 160% is this
dashboard's conservative choice. Both are hidden variables, `PcuYellow` and
`PcuRed`.

## Which CPU number drives the light

PCU is used only when headroom confidence is at least 2 on **both** nodes
(`ConfMin`, default 2). One node below 2 switches the whole pair to the sum of
`node_cpu_busy`. The other CPU tile stays on screen as reference and can
disagree with the light. A low confidence factor is not a fault, and it is not
Unknown. The score remains a real headroom estimate, on the CPU-busy ruler.

The two rulers are far apart. Combined CPU busy turns yellow at 70% and red at
85%. Combined PCU turns yellow at 160%. About 40% busy on each node is
comfortable on the PCU ruler and already yellow on the busy ruler.

## What DC286 showed

Cluster `dc286-netapp01`, both nodes paired, one node model, hottest survivor
port 0%. The light is yellow ("Latency risk") on both surviving-node tiles.

| Tile | Reading | Meaning |
| --- | --- | --- |
| Combined PCU | 68% green | Far under the 160% yellow line. Reference only while confidence is low. |
| Combined CPU busy | 84% yellow | Between 70% and 85%. Risk index about 1.93. Red starts at 2.0, which is 85% combined busy. |
| Headroom confidence | 1 seed | The minimum of the two nodes. This is why CPU busy is the live model. |
| 7-day timeline | Yellow since the upgrade | One red half-hour, 15 October 21:30–22:00 UTC, during the upgrade window. |

The confidence chart does not match the sentence "node -02 dropped to 1 and
stuck there":

- `dc286-netapp01-01` reached 3 before the upgrade, fell to 1 on 16 October, and was
  still at 1 more than a week later (mean 1, last 1, max 3). There was a brief touch of
  2 on 21 October. That blip is not a recovery. Confidence has recovered when it stays
  at 2 or 3.
- `dc286-netapp01-02` never went above 2. It moves between 1 and 2 all week
  (mean 2, last 2, max 2). At 16 October 15:45 it was at 2 while `-01` was at 1.

One node at 1 is enough. `-02` returning to 2 does not put PCU back in charge.

## Why the confidence changed, and why the nodes differ

Confidence is ONTAP's trust in that controller's own latency knee. The dashboard
displays the counter. An upgrade reboots each controller, and the rebooted node
goes back to confidence 1, the seed. It climbs again only after **that** node
has seen enough load, since its boot, to remeasure the knee. The partner's
value does not move it. A quiet node can stay at 1 indefinitely.

A two-node upgrade reboots the controllers one at a time. The survivor during
each step keeps running and, on the second step, carries both nodes' work. The
node that comes back last missed that double-load window and then sees only its
own share. With each node near 40% busy, utilization stays under the seed knee,
so confidence does not have to recover. The chart is the evidence that the two
nodes did not follow the same path: `-01` had a measured knee (3) and then
stayed at the seed, while `-02` stayed in the 1–2 band the whole week.

The iSCSI path ticket does not write this counter. It would matter only if it
is keeping one node quieter than the other. Port headroom on this cluster was
0%, so the yellow light is the CPU-busy model alone.

Leave the 70% busy line where it is. Raising it would hide the reading the
fallback model is meant to give. Forcing PCU with `ConfMin=1` would trust a
seed knee. Leave the default at 2.

## What was not a NetApp certification

The counters and the 200% line follow Active IQ Unified Manager and the metrics
Harvest already collects. The author lab on an AFF A220 running ONTAP 9.19.1
covers the queries, the confidence fallback, the port filter, and the colour
range. A takeover on that lab has not been run. CPU and Ethernet scoring works
on FAS as well as AFF. Disk headroom is omitted on purpose, and FC is off, so
it is not a full picture where disk or FC binds first.

## Unknown, after this change

The traffic light shows Unknown, in blue, when HA partner coverage is below 1
or the CPU-busy series or the Ethernet series is missing. A low confidence
factor does not do that. It keeps the CPU-busy score. The DC286 light would
still be yellow after this change: coverage was complete, both CPU busy and
Ethernet were present, and confidence on `-01` was 1.
