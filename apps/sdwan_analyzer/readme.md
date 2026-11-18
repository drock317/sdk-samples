# SD-WAN Analyzer

Historical visibility and analysis of SD-WAN behavior on Ericsson Cradlepoint
NCOS routers: WAN paths, SD-WAN interfaces, WAN bonding (WBOND), member
interfaces, traffic steering, underlay connectivity, cellular state, and how
these change over time.

> **Status: BACKEND build (v0.3.2).** Targeted collectors, normalization,
> logical Traffic Steering grouping, semantic fingerprints, a cached snapshot,
> section JSON endpoints, and (new in 0.3.0) the **data-acquisition model**
> below: startup + scheduled + manual collection, source-tagged observations,
> timestamp envelopes, interface and cellular/CA telemetry, SWANS dedupe and
> change-versioned config. **Recording jobs (backend)** are new in 0.3.2. Durable
> storage and the polished UI remain deferred; the in-memory store is
> temporary.

## Architecture

One-directional data flow; each layer depends only on the one to its left:

```
NCOS collectors -> normalization -> historical storage -> application API -> presentation
   collectors.py     normalize.py       storage.py           service.py        web.py
```

- **collectors.py** — read-only NCOS `status/` snapshots via `cp.get()`. No
  business logic. Every path is annotated VERIFIED or OPAQUE.
- **normalize.py** — converts raw NCOS shapes into stable internal records.
  Reuses the validated WAN friendly-naming / cellular-classification concepts
  from Speedtest Analyzer (not its speed-test code).
- **storage.py** — history **interface only**. No persistence format chosen
  yet. Ships a bounded in-memory (non-durable) default so the skeleton runs.
- **service.py** — the application API. Composes the layers. The **only**
  layer the web tier may call.
- **web.py** — minimal `http.server` dev page + JSON API. Calls `service`
  only; never touches NCOS. The browser never reaches NCOS APIs.

## Backend endpoints

Router collection happens only for startup, the selected schedule, a manual
refresh or a backend-owned Recording job — see "Data acquisition". **Web `GET`
requests only read the latest saved state.** Endpoints return `503 {"error":"snapshot
not ready"}` until the first snapshot has been composed.

- `GET /` — dev/status page
- `GET /api/status`, `GET /api/health` — app + storage + collector health,
  effective web port, latest `system_summary`
- `GET /api/snapshot` — full cached normalized snapshot (all sections)
- `GET /api/wan` — `paths`, `physical`, `overlays`, `underlay_map`, `config`
- `GET /api/swans` — SWANS per-path latency/jitter (Traffic Class measurement)
- `GET /api/qoe` — SD-WAN transport QoE (distinct object from SWANS)
- `GET /api/traffic_classes` — normalized Traffic Class definitions
- `GET /api/traffic_steering` — `logical_rules`, `compiled_rules`, `events`,
  `stats`, `flow_alignment`
- `GET /api/loadbalancers` — normalized rate/spillover/round-robin objects
- `GET /api/bonding` — WBOND bonds with member runtime detail
- `GET /api/identities` — resolved destination identities
- `GET /api/changes` — topology/config change flags vs previous snapshot
- `GET /api/history` — recent source-tagged observations (in-memory, non-durable)
- `POST /api/refresh/<group>` — manual refresh of one group
  (`traffic_classes`, `steering`, `bonding`, `wan_paths`, `overview`);
  ~5 s cooldown per group (`429` while cooling down)

> LAN clients reach this port only if a firewall zone-forward rule exists from
> the Primary LAN Zone to the Router Zone (see web-standards).

## Data acquisition (v0.3.0)

There are three acquisition modes and **no separate cache-refresh poller**.
The old 15 s fast / 120 s slow loops are gone. Between observations the app
only sleeps; the UI always shows the latest saved normalized state.

1. **Startup** — one full current-state collection, normalized and saved as
   `sample_source = startup`, so the UI is never empty.
2. **Scheduled** — one normal observation every **5, 15, 30 or 60 minutes**
   (no continuous 1-minute mode), aligned to the local clock
   (e.g. 15 min: 18:00, 18:15, ...). Default 15. Optional App Data
   `sdwan_analyzer_interval_minutes` selects it (read-only; the default is
   applied in code and never written). `sample_source = scheduled`.
3. **Manual refresh** — targeted groups, `sample_source = manual`. It never
   changes the schedule. Each call returns `refresh_ts`, `refresh_group`,
   `duration_ms`, `collector_errors`.

   | Group | Reads |
   |-------|-------|
   | `traffic_classes` | `config/identities/intent`, `config/sdwan_adv/traffic_class` |
   | `steering` | `status/wan/steering` (one read, split locally), `config/identities/ip` |
   | `bonding` | `status/wan/devices`, `status/sdwan_adv/wan_bonding` |
   | `wan_paths` | `status/wan/devices`, `primary_device`, `swans`, `qoe` |
   | `overview` | all runtime endpoints |
   | `loadbalancers` | `status/wan/loadbalancers` (expensive runtime) |

Recording is the fourth mode; see "Recording jobs" below.

Every pass updates the in-memory cache the UI reads and stores one compact,
source-tagged **observation** (never a raw API snapshot).

**Runtime telemetry** (every observation): WAN state + primary WAN, interface
counters, SWANS path samples, QoE, WBOND runtime weights/flows, steering
runtime state/flows, cellular/CA telemetry. No rates, averages, percentiles or
anomaly scores are computed yet.

**Static/config data** (Traffic Classes, destination identities, WAN rules,
steering definitions, WBOND membership/configured weights, overlay→underlay
map, capabilities) is **change-versioned**: re-read at startup, about every
60 minutes (reconciliation) and on targeted manual refresh, and a new version
is stored only when its semantic fingerprint changes. Observations carry just
the version number (`config_versions`).

**Observation vs current state.** These are different things:

- An **observation** is the persisted record of what one acquisition actually
  collected. It may be **partial** (a `traffic_classes` refresh stores only
  traffic-class/config data) and is tagged `startup | scheduled | manual |
  recording`. Served by `/api/history`.
- The **current state** is the merged last-known-good view used by
  `/api/snapshot`, every section endpoint and `/api/status`. It is built from
  per-endpoint caches, so a partial refresh replaces only what it collected;
  WAN, cellular, bonding, steering and QoE from 18:00 survive an 18:07
  Traffic Class refresh. `/api/snapshot` is the **latest merged current
  state**, not the last observation.
- `section_freshness` (in the snapshot and `/api/status`) gives, per section
  (`wan`, `cellular`, `swans`, `qoe`, `bonding`, `traffic_steering`,
  `traffic_classes`, `loadbalancers`), the `collected_ts` and
  `sample_source` of the pass that last refreshed it. It advances only when
  that section's collectors succeeded; a failed read never manufactures
  freshness.

**Loadbalancers are `expensive_runtime`**, not static configuration:
runtime data that is slow on the R1900. They are read only at startup,
~hourly reconciliation and targeted manual refresh (never per observation,
never by default during high-rate Recording). Cadence is unchanged.

**Timestamp model.** Every telemetry record has `metric_ts`, `collected_ts`
and `timestamp_source`. With a native NCOS measurement time (SWANS `epoch`,
steering event `epoch`) `metric_ts` is that value and `timestamp_source =
ncos`; otherwise `metric_ts = collected_ts` and `timestamp_source =
collector` (QoE, interface counters, cellular, WBOND, steering runtime).

**Interface telemetry.** From `status/wan/devices/*/stats` for every object
class (`physical`, `sdwan`, `wbond`): `rx_bytes`/`tx_bytes`
(`in`/`out`), `rx_packets`/`tx_packets`, `rx_errors`/`tx_errors`,
`rx_drops`/`tx_drops`, plus `connection_state`. Counters stay raw. If a
counter is lower than in the previous observation, `reset_suspected = true`
(no negative deltas). No Mbps yet.

**Cellular / CA telemetry** (`analyzer/cellular.py`, carrier parsing copied
from Speedtest Analyzer's validated logic, not imported): per cellular WAN
`carrier`, `service_mode`, LTE and 5G `rsrp/rsrq/sinr`, `serving_band`,
`serving_bandwidth_mhz`, `carrier_count`, `total_active_mhz`,
`zero_mhz_count`, `carriers[]` (`role, rat, band, bandwidth_mhz, channel, pci,
active`), `ca_signature` (RAT/band/channel/bandwidth only; role names are
ignored) and `cell_key_hash`. Raw `CELL_ID`/`NR_CELL_ID` are never stored: the
hash is the first 16 hex chars of SHA-256 over `carrier|service_mode|cell_id|
pci|band` (composition isolated in `cellular.cell_key_hash`).

**SWANS dedupe.** `status/wan/swans` returns a rolling, non-time-ordered
30-entry history that can repeat `(uid, epoch)` pairs (across polls and
inside one response). Every poll walks the whole history and persists each
**unseen** unique `(uid, epoch)` once; nothing is synthesized. Path cadence is
~30–60 s, so a 15-minute observation records all samples since the previous
poll that are still inside NCOS's rolling window.

**WBOND runtime** keeps configured and runtime concepts apart. The raw
`ds/us_curr_bw`, `*_last_bw_sample` and trend fields are neither persisted
nor used as throughput; future bandwidth comes from interface counters.

**Steering runtime** reuses the single `status/wan/steering` read. Observations
store per-rule runtime only (preferred WAN, `steering_to`, flows, latest event
reason); static definitions live in the versioned `steering_definitions`.
Events are stored once, keyed with their exact epoch.

**Collector timeout.** Each read is bounded by a 15 s wall-clock timeout
without touching shared `cp.py`: the read runs in a daemon thread and the
caller stops waiting. A hung read cannot be killed, so while one is pending
later reads fail fast (`collector busy`) instead of piling up; no second
worker thread is ever started while one is pending. Limitation: the abandoned
call may still complete in the background. Collector health in `/api/status`
(`collectors.{runtime,config}.collectors`) reports per endpoint
`error_type` (`timeout` | `busy` | `api_error`), `last_success_ts`,
`last_error_ts`, `last_error_type` and `last_error`; the current `error`
clears on the next success while the `last_error_*` history is kept.

The broad `status/sdwan_adv` tree is **not** polled — only the targeted
`sdwan_adv/wan_bonding` and `sdwan_adv/qoe` subtrees are read, to minimize
management-plane load.

**Traffic Classes** come from `config/identities/intent`, whose `criteria[]`
array holds each Latency/Jitter/SS criterion (`enabled`, `threshold`,
`hysteresis`, `priority`, targets). Each intent's `traffic_class` field
correlates to a `config/sdwan_adv/traffic_class` entry by `_id_` for `dscp`,
`forward_error_correction`, `reset_existing_flow`, and the WAN bonding
algorithm. `intent_id` and `traffic_class_id` are distinct and both exposed.
Legacy identity intents without a `traffic_class` field are excluded from the
NCX Traffic Class view. The Traffic Class fingerprint covers criteria
thresholds and the semantic config, so a threshold-only change is detected as
`traffic_class_changed`.

**SWANS** is parsed from `status/wan/swans` `history[].upd_status[]` (per
`wandev` `criteria.Latency`/`.Jitter`), taking the latest sample per path;
`cm` and `dataused` are not path records. **QoE** is parsed from
`status/sdwan_adv/qoe` (singular `interface` map) using `qoe-latency-stats`
(`pkts-intf-qoe-latency-average`/`-last-sample`); `pkt-loss-stats` is kept
raw. SWANS and QoE remain separate concepts.

**WBOND** is parsed from the real `status/sdwan_adv/wan_bonding` shape:
top-level `supported_capabilities`; per bond `Bonding Mode`, `link state`,
`Total interfaces`, reported `DL/UL total flows`, `negotiated_capabilities`,
`gre key`, `mtu`, `Priority`; per member (`members_list` dict keyed by tunnel
iface) `intf name` → member WAN id, `profile name`, `config_weight`,
`ds_weight` → download runtime weight, `us_weight` → upload runtime weight,
`DL/UL current flows`, and raw bandwidth/trend/sample values, `has_loss`,
link/attach state, `gre_key`, `mtu`. Configured weight, runtime directional
weight, and observed flow share stay separate; bandwidth units are not
assumed.

The four steering sub-trees are children of one `status/wan/steering` object,
so a pass reads that parent **once** and splits it locally rather than
issuing four sub-path reads.

Caching is **per collector**. Each read returns an explicit success/error
result, so a failed endpoint retains its previous last-known-good value while
a legitimately empty result (`{}`/`[]`/`None`) still replaces stale data. One
failing optional collector never blanks unrelated data or stops the app.
Per-collector health (last-known-good timestamp, error, error timestamp) is
surfaced under `collectors.{runtime,config}.collectors` in `/api/status`. No
full `/api/` tree is ever fetched.

`/api/history` serves the compact source-tagged observations described above
(bounded in memory, R1900-safe). The live `/api/snapshot` still carries the
richer current data.

## Recording jobs (v0.3.2, backend only)

A Recording is a temporary high-resolution capture for speed tests, transfers,
steering/bonding/cellular/failover tests. The **backend owns the job and its
clock**; the browser only starts, stops and reads it, so closing or
refreshing the browser never stops a Recording while the app is alive. One
job at a time; hard maximum **30 minutes** (`max_duration_sec` 1-1800,
default 600). Job fields: `job_id`, `label`, `notes`, `state`
(`recording|completed|stopped|expired|failed`), `started_ts`, `ended_ts`,
`max_duration_sec`, `sample_profile`, `created_by_ui`, `stop_reason`. A manual
stop gives `stopped`/`user`; the backend expires a job at its cap
(`expired`/`max_duration`) or at the in-memory sample cap (`sample_cap`);
an internal error gives `failed`.

| Endpoint | |
|----------|-|
| `GET /api/recordings` | job metadata list |
| `GET /api/recordings/active` | `{"recording": job or null}` |
| `GET /api/recordings/<job_id>` | metadata, `elapsed_sec`, `remaining_sec`, `sample_count`, per-source `sample_counts`, `collector_health`, config version refs/changes, lifecycle `events` (no telemetry dump) |
| `POST /api/recordings/start` | JSON `{label, notes, max_duration_sec}`; `400` invalid, `409` already active |
| `POST /api/recordings/<job_id>/stop` | `404` unknown, `409` not active |

`/api/status` has a `recording` block (`active, job_id, state, started_ts,
elapsed_sec, remaining_sec, max_duration_sec, sample_count,
collector_errors`).

**Profile `default_v1`** (all reads strictly serial, through the same
timeout/busy guard; a failing or busy collector is counted in
`collector_health` and the job continues):

| Group | Source | Cadence |
|-------|--------|---------|
| `wan` | `status/wan/devices` (state, counters, cellular/CA) | 5 s |
| `wbond` | `status/sdwan_adv/wan_bonding` | 5 s |
| `steering` | `status/wan/steering` (one read, split locally) | 10 s |
| `qoe` | `status/sdwan_adv/qoe` | 10 s |
| `swans` | `status/wan/swans` (unseen `(uid, epoch)` only) | 10 s |

Static config is read once at start and once at end; version references are
compared and a change emits `recording_config_changed` (only changed config
is versioned; payloads are never copied into samples). Loadbalancers are
never read during a Recording (the manual `loadbalancers` refresh is refused).
Lifecycle events: `recording_started`, `recording_stopped`,
`recording_expired`, `recording_failed` (`job_id, event_ts, event_type,
reason`). Samples carry `sample_source = recording` and `job_id`, and are kept
in a separate store from job metadata and from `/api/history`. No averages,
rates or analytics are computed.

**Scheduler interaction.** The 5/15/30/60 schedule is untouched. While a job
is active the Recording worker is the source of fresh runtime observations,
so a scheduled boundary is **skipped** (no duplicate NCOS reads, no scheduled
observation) and the schedule simply advances; scheduled collection resumes
after the job ends. The current merged state and `section_freshness` are
updated by Recording passes like any other collection.

**Limitations.** Jobs, events and samples are in memory only: lost on app
restart or router reboot (an in-flight Recording does not resume). Bounded
to 20 jobs and 3000 samples per job.

## Normalized snapshot schema

```
{ timestamp,
  system_summary: { primary_device, wan_path_count, physical_count,
                    overlay_count, bond_count, logical_rule_count },
  wan:   { primary_device, paths[], physical[], overlays[],
           underlay_map[], config{} },
  swans: { paths[], history_summary, raw },
  traffic_classes: [],
  traffic_steering: { logical_rules[], compiled_rules[], events[],
                      events_meta, stats[], flow_alignment[] },
  loadbalancers: [],
  bonding: { bonds[] },
  qoe: { paths[], raw },
  identities: { destinations[] },
  fingerprints: { topology, bonding_membership, logical_rules,
                  logical_rule_map, traffic_classes },
  collectors: { runtime{...}, config{...} },
  changes: { topology[], config[], first_snapshot } }
```

Each normalized object keeps a `raw` passthrough where field semantics are
not yet proven, so nothing is fabricated or discarded.

### Identity axes (kept separate, never conflated)

- **WAN dictionary key** (e.g. `sdwan-hub0_wan`) — device id in WAN/steering
  APIs.
- **`info.uid`** (e.g. `hub0_wan`) — overlay/control-routing identity.
- **`info.iface`** (e.g. `gres-hub0_wan`) — Linux interface.
- **`object_class`** — `physical` | `sdwan` | `wbond` | `unknown`. WBOND is a
  separate forwarding object and is **never** flattened into its members.

### Priority domains (kept separate)

`cm_priority` (physical Connection Manager), `original_priority`,
`parent_priority` (overlay generated priority), plus steering
`wan_priority_order` (explicit selector) and logical `trigger_priority`.
Overlay generated fractional priorities are exposed for analysis but **no**
tie-breaker theory is encoded as fact.

### Logical Traffic Steering rules

Real compiled steering rules have **two layers**: a top-level runtime object
and a nested `xpolicy` compiled-policy object. Each field is read from its
authoritative layer and they are never flattened together:

- **Runtime (top level):** `loadbalance_algo` → `runtime_algorithm`,
  `matched_devs` / `steering_to` (both **dicts keyed by WAN ID**),
  `primary_wandev`, `failover`, `intent` → friendly `traffic_class_name`.
- **Compiled (`xpolicy`):** `trigger_priority`, `trigger_strings` (a **list of
  dicts** `{_id_, trigger_priority, trigger_string}`), `src_lans` (a list of
  `{lan_name}` → normalized to `["Primary LAN"]`), `wan_priority_enabled`,
  `intent` → `traffic_class_id` (UUID), `dst_ip_network` → destination UUID,
  and `loadbalance_algo` → `configured_algorithm` (kept distinct from
  runtime — they can differ).

One logical policy is one NCM rule **name**; stats and events identify
policies only by that name. A policy may compile into several runtime
**branches** (e.g. `Lab01 - Teams` -> Best Effort @5 and Interactive @6).
Children are grouped into a branch by `trigger_priority` + selector + source
LANs + Traffic Class name — **never** by compiled UUID — and branches are
grouped under the logical name. Each entry in `branches[]` carries the
branch-specific data (`compiled_rule_ids`, `trigger_priority`,
`traffic_class_id/name`, `runtime_algorithm`, `configured_algorithm`,
`matched_devs`, `steering_to`, `source_lans`, `wan_selector_type`,
`wan_priority_*`, `failover`, `primary_wandev`). Top-level logical fields
(`trigger_priority`, `traffic_class_name`, ...) are compatibility aggregates:
the first branch's value, or the union for lists; `wan_selector_type` is
`mixed` when branches differ. Destinations (`xpolicy.dst_ip_network`) from all
children merge and resolve via `config/identities/ip`; `internal_name` is the
identity's `name` (type is never inferred from the `a…`/`c…` prefix). WAN
selector is classified `all_sdwan` | `bonded` | `explicit_priority` |
`specific` | `unknown` from the `xpolicy` trigger structure. Events and stats
correlate by **logical rule name**; exactly one stats record attaches per
logical policy (counts are never duplicated across or summed over branches).
`preferred_wan_raw` is the verbatim NCOS stats value (`preferred_wan` is a
legacy alias of it). `preferred_wan_uid`/`preferred_wan_name` resolve it to the
WAN inventory only when deterministic: an exact uid match, or — when the raw
value is not a uid — exactly one distinct `steering_to` uid of the same
`sdwan-`/`wbond-` class (`preferred_wan_match` records which). Otherwise both
stay null; nothing is fuzzy-matched. WAN inventory `kind` is `sdwan` / `wbond`
for those objects (physical kinds unchanged).
`preferred_flows` /
`nonpreferred_flows` are a current/aging population, not lifetime totals. Event
`reason` is stored verbatim.

Each logical policy carries a stable `semantic_fingerprint` (covers all branches) so history can track
it across policy recompilation/UUID churn.

## Privacy

Cellular detail is privacy-filtered (`normalize_cellular_safe`): only
SD-WAN-useful metrics (carrier, service type, RSRP/RSRQ/SINR, bands, SIM slot)
are surfaced. IMEI/IMSI/ICCID/MDN/GPS/secrets are never included, even when
present in the raw modem object.

## Tests

Local, standard-library-only harness (not shipped — excluded via
`buildignore`):

```
.venv/bin/python3 apps/sdwan_analyzer/tests/run_tests.py
```

`tests/fixtures.py` models the validated R1900 scenario (physical Ethernet +
cellular, two SD-WAN overlays, an 80/20 WBOND, and the LAB01/YouTube/TEST
Applications/Management steering rules). `tests/run_tests.py` drives the real
`normalize`/`service` code by injecting cached raw passes (no router I/O) and
asserts object classification, friendly naming, WAN-selector classification,
logical-rule grouping + destination merge, fingerprint stability across UUID
churn, bonding/loadbalancer/traffic-class/SWANS/QoE normalization, end-to-end
schema composition, change detection, the cellular privacy filter, and the
v0.3.0 acquisition model (startup/scheduled/manual sources, schedule choices,
no legacy polling, manual-refresh cooldown, interface counters/reset
detection, cellular CA parsing + cell hash, SWANS dedupe, config
versioning, collector timeout).

## NCOS API paths

Sources: `docs/ncos-api/` (repo docs) and the validated Speedtest Analyzer
v1.1.4 implementation (STA), which resolved SD-WAN/WBOND/steering behavior on
real hardware (incl. R1900).

| Path | Purpose | Confidence |
|------|---------|-----------|
| `status/wan/devices` | Per-WAN inventory (info/status/stats/config/diagnostics) | Docs + STA |
| `status/wan/devices/{id}/diagnostics` | Cellular (mdm-*) diagnostics | Docs + STA |
| `status/wan/devices/{id}/stats` | Cellular (mdm-*) stats | Docs + STA |
| `status/wan/primary_device` | Primary WAN device UID (sole authority) | Docs + STA |
| `status/wan/policies` | WAN policy engine (FailoverFailback, DualSIM, SWANS, Affinity, OnDemand) | Docs |
| `status/wan/swans` | Smart WAN Selection priority/history/data usage | Docs |
| `status/wan/steering` → `rules[].intent` | Steering active when any `intent` != `"Management (Management traffic)"` | STA-validated; needs device re-validation |
| `status/sdwan_adv/wan_bonding/interfaces/{iface}` → `members_list[]["intf name"]` | **WBOND member enumeration** (underlying WAN device keys) | STA-validated; needs device re-validation |
| `status/wan/devices/{sdwan}` `status.active_dep_wandev` / `status.dep_wandevs` | SD-WAN single dependent (underlay) WAN | STA-validated; needs device re-validation |
| `status/wan/sdwan` | SD-WAN hub/spoke: `connected_hub`, `links` | Docs; not used for membership; `links` device-dependent |
| `control/sdwan_adv/user_mode_driver/interface/{info.uid}` → `local_ip`/`remote_ip` | SD-WAN overlay source/gateway (read-only GET) | STA-validated; not in repo docs; needs device validation |
| `control/sdwan_adv/wan_bonding/interface/{info.uid}` → `local_ip`/`remote_ip` | WBOND overlay source/gateway (read-only GET) | STA-validated; not in repo docs; needs device validation |

Identity note (from STA): `info.uid` is the control-plane/routing identity
for overlays; the WAN object key (e.g. `wbond-wbond0_hub0`) is the
`status/wan/devices` key; `info.iface` is the Linux interface. WBOND
`members_list` entries expose the underlying WAN as `"intf name"` (NOT
`"profile name"`, NOT `dep_wandevs`).

## Appdata

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `sdwan_analyzer_web_port` | string (integer port) | No | Effective web listening port. Namespaced by app per convention `<app_name>_web_port` (device-level appdata is shared). |
| `sdwan_analyzer_interval_minutes` | string (5, 15, 30 or 60) | No | Scheduled collection interval. Default 15 (applied in code, never written). Invalid values fall back to 15. |

Web-port behavior (see `analyzer/web_port.py`, the shared dynamic web port
module, byte-identical to the copy in Speedtest Analyzer 1.1.7 and the kit's
`src/web_port.py`):

- **If `sdwan_analyzer_web_port` is set** it is authoritative: the app binds
  exactly that port. It never scans for another port, never silently
  relocates, and never overwrites the value. This determinism protects an NCM
  LAN Manager connection pinned to that port.
  - A **malformed or out-of-range** value (for example `abc`) is logged **once**
    and the app **stops itself**. There is no retry and no discovery fallback.
  - A **valid but busy** port is retried (the same port only) every 5 seconds
    for about 60 seconds, re-reading the value each time so a correction pushed
    from NCM is picked up. If it is still busy the app logs once and **stops
    itself**.
  - A stopped app is **not** restarted by NCOS, so there is no restart loop or
    log spam (exiting would be relaunched every few seconds because
    `restart = true`). Correct or clear the value and **start the app again**;
    it also tries again after a router reboot.
  - If the SDK ignores the stop request, the app keeps collecting without the
    web interface and logs that. It never exits.
- **If it is not set** (first run) the app discovers a usable port by actual
  socket bind starting at 8000, bounded, then persists the result with
  `cp.put_appdata('sdwan_analyzer_web_port', '<port>')`. This is an
  intentional write of the discovered assignment, not a code default. On
  later restarts/reboots the persisted value is reused even if 8000 frees up.
- **NCM** can set `sdwan_analyzer_web_port` at group/device level to
  standardize the port across a fleet; the app consumes the effective value
  on next startup. No NCM API credentials are used. The port is not written
  to Device Description, Asset ID, or Custom fields.

The effective port is logged (`SD-WAN Analyzer web interface started on TCP
port <n>`) and exposed via `/api/status` and `/api/health` under `web`.

No other appdata is written. Per coding-standards the app never writes
default values to appdata.

## Resolved from STA v1.1.4 (implemented in the skeleton)

- SD-WAN underlay resolution (`active_dep_wandev` / `dep_wandevs`).
- WBOND member enumeration (`wan_bonding/interfaces/{iface}/members_list` →
  `intf name`) with friendly-name reuse.
- Traffic-steering active detection (`rules[].intent` semantics).
- WAN friendly naming + cellular classification (Starlink-before-cellular).

These are wired into the service compose step and validated locally with
mocks. They still require on-device confirmation (see below).

## Fields intentionally left raw (semantics not yet proven)

- **SD-WAN QoE** loss/units and any non-latency fields — carried under
  `qoe.paths[].raw`; only clear latency samples are normalized.
- **SWANS** extra fields beyond latency/jitter — carried under
  `swans.paths[].raw`.
- **Loadbalancer** algorithm-specific fields — normalized where known
  (score, active_device, ingress/egress max/current/adj/run); everything else
  under `raw`. Configured capacity vs runtime utilization vs score vs
  active_device are kept as separate concepts; `39.0625` is **not** labeled
  measured bandwidth.
- **WBOND** capabilities, GRE/bond metadata, member loss/FEC — captured
  generically (`capabilities` list + member `raw`); configured weight vs
  runtime directional weight vs observed flow share are separate fields, with
  no deviation alerts/thresholds invented.
- **Overlay generated fractional priorities** — exposed, but the tie-breaker
  theory is **not** encoded as fact.

## Deferred to later phases

- Durable historical storage (format, retention, sampling interval — see the
  options block in `storage.py`). `HistoryStore` stays in-memory for now.
- SD-WAN/WBOND overlay source-IP resolution wiring (control-plane
  `local_ip`/`remote_ip`; documented in collectors, only needed for active
  testing, not visibility).
- Recording UI, durable Recording storage, Recording analysis.
- Averages, percentiles, bandwidth rates and anomaly scoring.
- Device validation catalog (captive-modem model naming) for friendly names.
- The production UI and the template design system (`static/`).
