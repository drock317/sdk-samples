# WBOND runtime analysis (v0.8.1, history integrity v0.8.2)

`GET /api/analytics/wbond` reports each WAN bond and its members from data the
analyzer already holds. It makes no router calls and writes nothing. It reuses
the relationships from `/api/analytics/topology` (see `topology.md`) and adds
the normalized runtime fields from `/api/bonding`. Returns 503 until the first
snapshot exists.

## Response

Top level: `schema_version` (1), `as_of`, `data_state`, `bonding_runtime`,
`bonds[]`, `unresolved[]`, `counters_additive` (always `false`), `notes[]`.

Each bond: `iface`, `wbond_device`, `display_name`, and `observed`:
`connection_state`, `link_state`, `bonding_mode`, `negotiated_capabilities`,
`reported_member_count` (router's `Total interfaces`), `listed_member_count`,
`resolved_member_count` (members whose overlay and physical WAN both resolved),
`member_count_matches`, `total_download_flows`, `total_upload_flows`,
`total_download_flows_source`, `total_upload_flows_source` (v0.8.2, see below).

Each member: `tunnel_iface`, `overlay`, `physical` (each `resolved` with a
`basis`, or `unresolved` with a `reason`), `physical_display_name`,
`underlay_consistency`, and two separate blocks:

- `config.configured_weight` (legacy name: the value is the runtime
  `members_list[].config_weight`, not configuration; from v0.8.3 the same
  value is also `observed.runtime_config_weight`)
- `observed`: `link_state`, `link_attach_state`, `current_download_weight`,
  `current_upload_weight`, `download_flows`, `upload_flows`, `loss_indicator`

## Freshness

`as_of` is the topology block: `snapshot_ts`, collection timestamps and, per
source (`wan`, `bonding`, `primary`), `last_success_ts`, `read_error` and
`stale`. `data_state` is `stale` if the `wan` or `bonding` read last failed,
`unknown` if either was never read, else `fresh`. Stale data is still shown,
as last successfully read.

## What it does not claim

- Traffic distribution is not inferred from weights. Weights are router-reported.
- No bandwidth utilization or bonding efficiency.
- Attached or UP members are not shown to carry user traffic.
- `loss_indicator` is the raw `has_loss` value, not a packet-loss percentage.
- A measurement the router did not report is `null`, never `0`.
- Counters and flow counts of different layers are not additive.
- Membership here is the runtime list. Configured bonding selection criteria
  (v0.8.3) are criteria, not members; they are reported by
  `/api/analytics/wbond_config` (see `telemetry-foundation.md`).

## History integrity fields (v0.8.2)
Two fields are added to each newly stored WBOND observation (`telemetry.wbond_runtime`).
Nothing else about collection, scheduling, retention or storage changes.

### `members[].tunnel_iface`
The SD-WAN tunnel interface (the `members_list` key, e.g. `gres-hub0_164d3`) the
member was reported under, taken from the same normalization pass that produced
`member_uid`. It records the association as it was at collection time, so later
analysis does not need to guess it from current topology.
- `null` when the normalized member carries no tunnel interface. It is never
  filled in from current topology or from another observation.
- If NCOS ever returns `members_list` as a list rather than a keyed object, the
  existing normalization uses the list index as the key; that is stored as is
  and is not a real tunnel interface. NCOS returns a keyed object today.

### `total_download_flows_source`, `total_upload_flows_source`
Provenance of the bond-level `total_download_flows` / `total_upload_flows`,
one value per direction, decided by the existing normalization fallback
(`normalize.wan_bonding_detail`), not recalculated:
| Value | Meaning |
|---|---|
| `reported` | NCOS supplied the total (`DL total flows` / `UL total flows`). A reported `0` is still `reported`. |
| `summed` | NCOS did not supply it, so it is the sum of member flow counts. From v0.8.3 this requires at least one numeric member flow in that direction. |
| `unknown` | Provenance cannot be established: no reported total and no numeric member flow to sum (the total is `null`), or the input had no provenance. Before v0.8.3, members that existed but reported no flow values were labelled `summed` with a total of 0; that history is not rewritten. |
The two directions are independent; one can be `reported` and the other `summed`.
The same two fields appear in `observed` of `/api/analytics/wbond`, for the live
snapshot.

### Historical limitations
- Observations stored before v0.8.2 have no `tunnel_iface` and no `*_source`
  fields. They are not back-filled: readers must treat the missing keys as
  unknown, not as `reported` and not as a current-topology lookup.
- `unknown` written by v0.8.2 means the source could not be established at
  collection; an absent key means the observation predates the field.
- `/api/analytics/wbond` reads the live snapshot and is not affected by older
  history.

## Collection interval
Normal scheduled collection intervals are 5, 15, 30 and 60 minutes. Sub-5-minute
collection is for short recording sessions. The current one-minute scheduled
interval is temporary development testing. Flow counts are point-in-time
populations, so how often they are sampled determines what history can show.

## Live validation (v0.8.1)

Deployed through NCM and checked on two routers.

| Router | Bond | Result |
|---|---|---|
| R1900 | `gres_wb0-hub0`, 2 members (T-Mobile 5G, Ethernet WAN) | UP, GRE, 2/2 resolved and ATTACHED. Configured weights 50/50. Runtime download weights 59/40, upload 50/50. Download flows 28/29 (57), upload flows 8/7 (15). No unresolved relationships or underlay conflicts. |
| R2400 | `gres_wb0-hub0`, 3 members (AT&T LTE, T-Mobile 5G, Verizon Wireless) | UP, GRE, 3/3 resolved and ATTACHED. Configured weights 33/33/33; runtime download/upload weights 33/33/33. Zero flows while idle; 47 download flows (16/16/15) during traffic testing; a later snapshot showed 79 download and 7 upload flows. No unresolved relationships or underlay conflicts. |

The existing interface rate API (`/api/analytics/interfaces`) also captured
web browsing, speed test and YouTube traffic on the R2400. Examples: WBOND
10.61 Mbps RX in one interval, 7.04 Mbps RX / 16.19 Mbps TX in another; the AT&T
overlay held about 1.7 to 3.1 Mbps RX during streaming and fell to about
8.53 Kbps RX in the next full interval after streaming stopped. UI values
matched the API and the reviewed charts showed no invalid measurement gaps.

### Interpretation rules these results support

- Configured weights are not necessarily the current runtime weights (R1900
  download: configured 50/50, runtime 59/40).
- Runtime weights are not measured traffic percentages.
- Flow counts are point-in-time populations, not cumulative byte counters.
- Equal flow counts (R2400 16/16/15) do not imply equal bandwidth distribution.
- Physical WAN, SD-WAN overlay and WBOND counters measure overlapping layers
  of the same traffic and must not be added together.
- The interface rate API reports average throughput over each collection
  interval, not instantaneous peak throughput.
- Overlay traffic cannot automatically be attributed to a WBOND: traffic
  steering may select an overlay directly.
- The YouTube stop test supports the observed association between that traffic
  and the AT&T overlay. It does not independently prove which steering rule
  selected the overlay.
