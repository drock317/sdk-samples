# SD-WAN Analyzer

Historical visibility and analysis of SD-WAN behavior on Ericsson Cradlepoint
NCOS routers: WAN paths, SD-WAN interfaces, WAN bonding (WBOND), member
interfaces, traffic steering, underlay connectivity, cellular state, and how
these change over time.

> **Status: BACKEND build (v0.5.0).** Targeted collectors, normalization,
> logical Traffic Steering grouping, semantic fingerprints, a cached snapshot,
> section JSON endpoints, and (new in 0.3.0) the **data-acquisition model**
> below: startup + scheduled + manual collection, source-tagged observations,
> timestamp envelopes, interface and cellular/CA telemetry, SWANS dedupe and
> change-versioned config. **Recording jobs (backend)** are new in 0.3.2. v0.3.3 is a
> hardening/API patch (bounded client sockets, current interface + cellular
> telemetry APIs, numeric RF values). **v0.4.0 added durable telemetry
> storage; v0.4.1-0.4.3 harden it** (state ownership, config version reuse, safe shutdown during upgrade, time-based dedupe recovery). **v0.4.4 adds the bounded, paginated read APIs** `/api/history` and `/api/recordings/<id>/samples` (see *Query APIs* below). The polished UI remains
> deferred.
>
> **v0.5.0 analytics foundation.** Adds fractional counter acquisition time
> (`collected_at`), fixes a defect where a failed collector read re-stamped
> cached values as fresh measurements (WAN counters, QoE, WBOND, steering, SWANS),
> and adds the pure counter delta/rate module `analyzer/counters.py` (a library:
> nothing in the service, storage or API calls it yet). See `docs/ARCHITECTURE-AND-ROADMAP.md`
> and `docs/analytics/`.

> **v0.8.3 telemetry foundation.** Versions the effective steering
> definitions (`steering_rule_defs`) and the configured WAN bonding
> (`wan_bonding_config`), detects definition differences between observations,
> records per-rule application matching, keeps component-carrier RF, and flags
> QoE sentinel candidates. New read-only endpoints are listed below; details in
> `docs/analytics/telemetry-foundation.md`. Existing endpoints are unchanged
> (additive fields only).

> **v0.8.4 engineering analytics.** Adds validity-checked cellular RF
> (every reading says whether it is valid, a placeholder, impossible or of
> unknown scale, and the raw value is kept), NCOS cellular health, stable
> serving-cell identity, **three new history analytics endpoints** (cellular,
> SD-WAN, and time-aligned correlation), and a **256 MiB / 7-day history**
> with configurable limits, periodic cleanup, low-disk write protection and
> coverage reporting. See *Historical analytics (v0.8.4)* and
> *Durable telemetry storage* below.
> **v0.8.5 steering reliability fix.** When the router intermittently returns
> no data for its steering status, that poll is now recorded as a failed read
> (`empty_source` in `collector_errors` and `/api/health`) instead of an empty
> steering configuration. Previously this could report false steering
> configuration changes (`steering_definitions` flipping between versions).
> Polls with missing, partial or not-yet-confirmed empty steering rules keep
> the last known definition version. `/api/analytics/validity` adds
> `steering_source`, the classification of the latest steering read.
> **v0.7.0 adds the first web page: Throughput trends.** Open `http://router_ip:<port>`
> (the port is in the app log and in `sdwan_analyzer_web_port`). The earlier endpoint list is now at `/dev`.

## Throughput trends (v0.7.0)

Shows how much traffic an interface carried over time.

1. Open `http://router_ip:<port>` in a browser on the LAN.
2. Pick an **interface**: a physical WAN, an SD-WAN overlay, or a WBOND.
3. Pick a **time range**: 1h, 6h, 24h, or 7d.
4. Read the chart: RX (received) and TX (transmitted). The unit (bps, Kbps, Mbps or Gbps) is chosen automatically from the highest value shown, and is the same for both lines and the whole axis. Tooltip values use the best unit for each value. Hover, or focus the chart and use the arrow keys, to see each measurement and its time interval.

What to expect:

- A measurement is the average rate between two stored samples, so the chart has one point per collection interval (1 to 60 minutes, default 15).
- A **gap** means there is no valid measurement for that period: a missing or failed read, a counter reset, samples too far apart (more than 2 hours), or counters that cannot be trusted. Gaps are never drawn as zero. A real zero (idle link) is drawn as zero.
- The page loads at most 3000 measurements per view. If a range has more, a notice says the newest were not loaded.
- Times are shown in your browser's time zone. The range is measured from the router's clock.
- The page shows only data the analyzer has already stored. It never reads the router on its own.
- LAN access needs the Primary LAN Zone to Router Zone forwarding rule in the router firewall.

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
- **storage.py** — store interfaces plus bounded in-memory defaults (used by
  tests and as the fallback when durable storage cannot initialise).
- **persistence.py / durable.py** — the durable backend (stdlib only):
  segmented JSONL under `tmp/telemetry`. Only these modules touch the
  filesystem for telemetry.
- **service.py** — the application API. Composes the layers. The **only**
  layer the web tier may call.
- **web.py** — minimal `http.server` dev page + JSON API. Calls `service`
  only; never touches NCOS. The browser never reaches NCOS APIs.

## Web server connection handling (0.3.3)
The server is deliberately single threaded (shared service, Recording and
store state has no locking design yet). Every accepted client socket gets a
5 s timeout (`web.CLIENT_SOCKET_TIMEOUT_S`) so a browser that connects but
never sends a request (speculative/idle connection) cannot hold the only
handler: it is dropped and queued requests are served. Responses are
HTTP/1.0 with `Connection: close`. The 15 s NCOS collector timeout is
unrelated and unchanged.

## Backend endpoints

Router collection happens only for startup, the selected schedule, a manual
refresh or a backend-owned Recording job — see "Data acquisition". **Web `GET`
requests only read the latest saved state.** Endpoints return `503 {"error":"snapshot
not ready"}` until the first snapshot has been composed.

- `GET /` — dev/status page
- `GET /api/status`, `GET /api/health` — app + storage + collector health,
  effective web port, latest `system_summary`
- `GET /api/snapshot` — full cached normalized snapshot (all sections)
- `GET /api/wan` — `paths`, `physical`, `overlays`, `underlay_map`, `config`,
  and (0.3.3) `interfaces`: the latest normalized per-interface counters
  (`rx/tx_bytes`, `rx/tx_packets`, `rx/tx_errors`, `rx/tx_drops`,
  `reset_suspected`, `metric_ts`, `collected_ts`, `timestamp_source`). Raw
  counters only: no rates; the meaning of drop counters is not interpreted.
- `GET /api/storage` (0.3.4) — Storage Inspector, generated live on every
  call (nothing stored, no timer, no router I/O, no history entry).
  `memory` (counts only: history observations, config versions, Recording
  jobs/samples/active job, dedupe entries, configured limits), `persistent`
  (0.4.0: `enabled: true`, `root` `tmp/telemetry`, `status` ok/degraded,
  `total_bytes`, `file_count`, `files[]`, per-area subtotals `history`,
  `recordings` (+`jobs`), `config`, `events`, `state`, `quarantine`,
  `write_errors`, `last_write_error`, `last_fsync_ts`, `retention` limits,
  `recovery` summary, `free_space`, `clock_sane`; `enabled: false` only when
  the durable backend could not initialise) and `application_files` (metadata only: `relative_path`,
  `size_bytes`, `modified_ts`, `type`). Strict allowlist rooted at the app's
  own directory; symlinks are never followed; credentials, keys, certs,
  `sdk_settings.ini` and unknown file types are excluded (counted only).
- `GET /api/cellular` (0.3.3) — `modems[]`: latest merged cellular/CA
  telemetry per modem (carrier, service mode, serving band/bandwidth,
  RSRP/RSRQ/SINR LTE + 5G as JSON numbers or null, carrier count, total
  active MHz, zero-MHz count, `carriers[]`, `ca_signature`, `cell_key_hash`,
  timestamp envelope). No IMEI/IMSI/ICCID/MDN/GPS/cell ids.
- `GET /api/swans` — SWANS per-path latency/jitter (Traffic Class measurement)
- `GET /api/qoe` — SD-WAN transport QoE (distinct object from SWANS)
- `GET /api/traffic_classes` — normalized Traffic Class definitions
- `GET /api/traffic_steering` — `logical_rules`, `compiled_rules`, `events`,
  `stats`, `flow_alignment`
- `GET /api/loadbalancers` — normalized rate/spillover/round-robin objects
- `GET /api/bonding` — WBOND bonds with member runtime detail
- `GET /api/identities` — resolved destination identities
- `GET /api/changes` — topology/config change flags vs previous snapshot
- `GET /api/history` (0.4.4) — durable, paginated history; see *Query APIs*.
- `GET /api/analytics/interfaces` (0.6.0) — calculated interface RX/TX rates; see *Query APIs*.
- `GET /api/analytics/topology` (0.8.0) — current physical WAN / SD-WAN overlay / WBOND relationships with known state; unverified links are returned as `unresolved`. See `docs/analytics/topology.md`.
- `GET /api/analytics/wbond` (0.8.1) — read-only WBOND runtime analysis: per-bond state, per-member overlay/physical WAN, configured vs router-reported runtime values, and data freshness. From 0.8.2 it also reports whether each bond flow total was `reported` by NCOS or `summed` from member flows, and stored WBOND history records `tunnel_iface` per member. See `docs/analytics/wbond.md`.
- `GET /api/analytics/steering` (0.8.3) — current effective steering definitions (compiled children, exact-UUID destination and Traffic Class resolution) with per-rule runtime flow gauges and application matching.
- `GET /api/analytics/steering/changes` (0.8.3) — `steering_rule_defs` versions and the observed differences between them; `limit` (1-50), `before_version`.
- `GET /api/analytics/wbond_config` (0.8.3) — configured bonds (static/dynamic mode, selection criteria) resolved against runtime members; configured and runtime weights kept separate.
- `GET /api/analytics/validity` (0.8.3) — collector health, section freshness and data-validity indicators.
- `GET /api/analytics/cellular` (0.8.4) — serving cells, LTE versus NR usage, RF statistics with validity exclusions, carrier-aggregation changes, NCOS cellular health history and time-bounded events per modem.
- `GET /api/analytics/sdwan_history` (0.8.4) — steering rules and observed applications, bond member and weight history, per-layer throughput and QoE/SWANS quality over a time window.
- `GET /api/analytics/correlation` (0.8.4) — cellular and SD-WAN events that were first seen in the same or adjacent polls. Timing evidence only; no cause is asserted.
- `GET /api/recordings/<job_id>/samples` (0.4.4) — paginated Recording
  samples; see *Query APIs*.
- `POST /api/refresh/<group>` — manual refresh of one group
  (`traffic_classes`, `steering`, `bonding`, `wan_paths`, `overview`;
  from 0.8.3 `steering` also reads the identity/intent/Traffic Class config
  and `bonding` also reads `config/sdwan_adv/wan_bonding`);
  ~5 s cooldown per group (`429` while cooling down)

> LAN clients reach this port only if a firewall zone-forward rule exists from
> the Primary LAN Zone to the Router Zone (see web-standards).

## Data acquisition (v0.3.0)

There are three acquisition modes and **no separate cache-refresh poller**.
The old 15 s fast / 120 s slow loops are gone. Between observations the app
only sleeps; the UI always shows the latest saved normalized state.

1. **Startup** — one full current-state collection, normalized and saved as
   `sample_source = startup`, so the UI is never empty.
2. **Scheduled** — one normal observation every **1, 5, 15, 30 or 60 minutes**
   (1 minute is intended for development; it writes 15 times more history than the default), aligned to the local clock
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
runtime state/flows, cellular/CA telemetry. Nothing computed is stored or
served: no rates, averages, percentiles or anomaly scores in any API or stored
observation. The only calculation code is the pure library
`analyzer/counters.py` (v0.5.0), which is not wired to collection, storage or
the web tier.

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
`collected_ts` is integer seconds read after the whole pass finishes.

**`collected_at` (v0.5.0, interface records only).** An additive fractional
Unix timestamp taken from the clock immediately after the successful
`status/wan/devices` read returns (`CollectResult.acquired_at`), before
parsing and before any other read in the pass. It is an upper bound on the
instant NCOS sampled the counters, not the completion time of the pass.
`collected_ts`, `metric_ts` and `timestamp_source` are unchanged. It is
persisted inside each interface record (no storage migration). Records written
before v0.5.0, or acquired without a real read, simply lack the field and are
never given a made-up value; rate calculations then fall back to
`collected_ts` and report reduced precision.

**Interface telemetry.** From `status/wan/devices/*/stats` for every object
class (`physical`, `sdwan`, `wbond`): `rx_bytes`/`tx_bytes`
(`in`/`out`), `rx_packets`/`tx_packets`, `rx_errors`/`tx_errors`,
`rx_drops`/`tx_drops`, plus `connection_state`. Counters stay raw. If a
counter is lower than in the previous pass, `reset_suspected = true`
(no negative deltas); it is a hint only (the baseline is the previous pass of
any source). Byte counters are verified bytes (`in`/`out`); see
`docs/analytics/counter-semantics.md`. Counters of different object classes
overlap (an overlay and WBOND carry traffic already counted on the physical
path), so they must never be summed.

**A failed `wan_devices` read emits nothing new (v0.5.0 fix).** Before this
fix the observation re-stamped last-known-good counters with the new
`collected_ts`. Now the observation omits `wan`, `interfaces` and `cellular`
for that pass (the failure is in `collector_errors`), `_prev_counters` is not
advanced, and the current state and section freshness keep their last values.
The same rule now applies to every runtime section; see *Failed-read
invariant* below.

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
`error_type` (`timeout` | `busy` | `api_error`, and from v0.8.5 for the
steering keys `empty_source` | `incomplete_source`), `last_success_ts`,
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

## Failed-read invariant (v0.5.0)

A failed NCOS collector read never creates a newly timestamped historical
measurement from last-known-good cached values. Two views are kept apart:

- **Current state** (`/api/snapshot`, section endpoints, `section_freshness`)
  keeps serving the last-known-good value and its original freshness. A failure
  does not advance freshness and does not blank the data.
- **Historical observations** (`/api/history`, Recording samples) omit the
  section fed by the failed collector. Nothing is zero filled and no timestamp
  is invented; the failure is listed in the observation's `collector_errors`.

| Observation section | Needs (all must succeed in that pass) |
|---|---|
| `wan`, `interfaces`, `cellular` | `wan_devices` |
| `wan.primary_wan`, `paths[].is_primary` | `primary_device` read successfully in the same pass; otherwise (failed, or not requested by a targeted refresh) both are omitted, never guessed as false |
| `swans_samples` | `swans` |
| `qoe` | `qoe` |
| `wbond` | `wan_bonding` |
| `steering` | `steering_rules`, `steering_stats` and `steering_events` |
| `steering_events` | `steering_events` |

Dedupe state (SWANS and steering events) and the counter baseline are not
touched by a failed pass. Not covered by the invariant: config version refs
(`config_versions`, derived from cached config, a version changes only when a
fingerprint changes). Config reconciliation (about hourly) is recorded only when every
config read succeeded, so a failed one is retried at the next scheduled pass and the on-demand `/api/cellular_detail` reads.
The Recording `wan` group reads `primary_device` together with `wan_devices` every 5 s, so
Recording samples carry the primary identity. A primary change is seen at the first poll after it;
the exact failover instant is not known.

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
| `wan` | `status/wan/devices` (state, counters, cellular/CA) + `status/wan/primary_device` | 5 s |
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

**Persistence (0.4.0).** Jobs, events and samples are durable
(`tmp/telemetry/recordings/<job_id>/`). A Recording that was active when the
app stopped uncleanly comes back as `state=failed`, `stop_reason=app_restart`
(never resumed); a graceful stop ends it as `stopped`/`app_shutdown`.
Bounded to 3000 samples per job; samples are read from disk, not held in RAM.

## Durable telemetry storage (0.4.0)
Lifecycle: survives an app restart and a router reboot; an application
update/reinstall **may wipe it (accepted)** — there is no cross-version
migration. Everything lives under the app-local `tmp/` (never packaged).
```
tmp/telemetry/
  state.json                  schema + clean/unclean exit markers (atomic)
  config_versions.jsonl       one line per NEW semantic config version
  events.jsonl (+ .1)         app/storage/recovery/retention events (256 KB)
  history/YYYY-MM-DD.jsonl    startup/scheduled/manual observations (UTC day)
  recordings/<job_id>/metadata.json, samples.jsonl
```
- Compact **persist form**: one JSON object per line, `collected_ts` first,
  config refs as `{name: version}`, redundant envelope/null fields dropped,
  collector timestamps hoisted. The API representation is unchanged.
- Small files are written atomically (`.tmp`, fsync, validate, replace);
  JSONL files are append-only. A torn last line is repaired on open and kept
  in `<file>.corrupt`; earlier records are never rewritten.
- fsync: history/config/events on every write; Recording samples flushed per
  sample and fsynced about every 30 s (and on stop/expiry/failure/shutdown).
- Retention (whole files only; the active Recording and today's history file
  are never deleted): **history 7 days or 256 MiB** (v0.8.4; was 32 MiB), set
  with `sdwan_analyzer_history_max_mb` (32-1024) and
  `sdwan_analyzer_history_max_days` (1-30); Recordings 10 completed jobs or
  32 MB (independent of the history limits). Swept at startup, UTC day
  rollover, Recording completion and **every hour**. The effective policy,
  where each value came from, retained coverage and recent deletions are in
  `GET /api/storage` (`persistent.history_policy`, `history_coverage`,
  `retention_events`).
- **Low-disk protection (v0.8.4).** History writes pause when free space falls
  below the larger of 512 MiB and 10% of the filesystem, and resume only after
  a higher threshold. Before pausing, the oldest whole days are removed.
  While paused the app keeps collecting in memory (the latest state and the
  20 newest observations stay available) and writes only one pause event, not
  one per observation. The size limit is also lowered when the disk could not
  hold it above that reserve. A single day that alone reaches the limit pauses
  writes until the next UTC day. `persistent.space_guard` shows the state.
- **Queries on large history.** `/api/history` keeps its 1 MiB scan limit. A
  verified in-memory index lets a query that starts late in a large day file
  skip bytes that are provably older than the start; it never assumes the
  records are time-ordered, and a file it cannot prove is read from the
  beginning as before.
- `state.json` carries a per-process `run_id` (+ `pid`). A process claims it
  at startup and, on shutdown, writes its clean-stop marker ONLY if the file
  still carries its own `run_id`; if a newer process already replaced it (NCOS
  can start the new instance before the old one finishes stopping) the state
  is left alone and a `state_ownership_mismatch` event is logged. A new
  process also waits (<= 5 s) for a still-live predecessor to finish before
  judging `previous_exit`.
- Shutdown never creates directories. Once SIGTERM arrives, no write may
  recreate `tmp/telemetry`: during an upgrade the installer can remove the
  tree while the old process is still stopping, and a recreated directory
  (owned by the old process's user) would make the new version's
  `tmp/telemetry` unwritable. If the root is gone at shutdown the clean-state
  write is skipped.
- If durable storage cannot initialise, `/api/storage` reports
  `persistent.enabled: false`, `status: disabled`, `init_error_type` and a
  path-sanitised `init_error`; the app keeps running on in-memory stores.
- Dedupe recovery after a restart rebuilds SWANS / steering-event keys from
  disk (the dedupe sets themselves are never persisted): the last ~64 KB of
  the newest history file, and, for Recordings, a TIME window - newest
  Recording samples back to (newest sample - 10 min), scanned backward and
  capped at 4 MiB for the whole startup. Several back-to-back Recordings
  inside that window all contribute; one that ended > 30 min ago is skipped.
  Malformed lines are skipped and counted (`dedupe_skipped_lines`).
- Config version numbers identify DISTINCT semantic configurations: a
  fingerprint equal to the current version is unchanged; equal to any
  retained earlier version it reuses that number (no new record, the current
  pointer just moves); only a never-retained fingerprint gets a new version.
  A -> B -> A is v1, v2, v1. (Versions aged out of the 20/object window may
  legitimately be numbered anew.) Startup collection is NOT delayed: partial
  boot-time state is simply another distinct version.
- Write failures never stop collection; they are counted and shown in
  `/api/storage` (`status: degraded`). Observations stamped with an obviously
  invalid clock stay in RAM only.

## Query APIs (0.4.4)

Two read-only, paginated endpoints serve the durable data. They never trigger
collection, never write to disk, and never use the ~20-observation RAM ring
while durable storage is healthy. Both return the normalized observation
objects (the same shape as in memory), not raw JSONL records or NCOS payloads.

Response envelope (both endpoints):

```json
{"samples": [], "count": 0, "next_cursor": null, "has_more": false,
 "skipped_lines": 0}
```

`samples` are oldest first (persisted collection order). `next_cursor` is an
opaque token: pass it back unchanged as `cursor=` with the same filters to get
the next page; `has_more` is `true` exactly when `next_cursor` is set.
`skipped_lines` counts malformed or oversized lines passed over while reading
this page.

### `GET /api/history`

| Parameter | Behaviour |
|---|---|
| `limit` | 1-250, default 100 |
| `start` | inclusive UTC Unix epoch seconds (integer or decimal) |
| `end` | inclusive UTC Unix epoch seconds |
| `sample_source` | `scheduled`, `manual` or `startup` |
| `cursor` | token from the previous page |

With neither `start` nor `end` (and no `cursor`) the window is the most recent
24 hours of the router clock (no default window if the clock is not sane).
Explicit bounds may span the whole retained 7-day history. The default window
is pinned inside the cursor, so it does not slide between pages.

```
GET /api/history?limit=100
GET /api/history?start=1790380800&end=1790467199&sample_source=manual
GET /api/history?limit=100&cursor=<next_cursor from the previous response>
```

### `GET /api/recordings/<job_id>/samples`

| Parameter | Behaviour |
|---|---|
| `limit` | 1-250, default 100 |
| `group` | one of `wan`, `wbond`, `steering`, `qoe`, `swans`, `config` |
| `after` | exclusive UTC epoch seconds on `collected_ts` |
| `cursor` | token from the previous page |

Samples keep their `collected_ts` / `timestamp` and `recording_group`. Active,
completed, stopped, expired and failed Recordings are all readable while their
sample file exists. To follow an active Recording, page with `cursor` until
`has_more` is `false`, then poll with `after=<last collected_ts>`.

```
GET /api/recordings/rec-1790380800-001/samples?group=wan&limit=50
GET /api/recordings/rec-1790380800-001/samples?after=1790380830.5
```

### `GET /api/analytics/interfaces` (0.6.0)

Calculated RX/TX throughput of **one** interface, computed on demand from the
stored history. Read-only: nothing is stored, cached or changed.

| Parameter | Behaviour |
|---|---|
| `uid` | **required**, the interface uid (e.g. `ethernet-wan`, `sdwan-hub0_wan`, `wbond-wbond0_hub0`) |
| `limit` | 1-250 rates per page, default 100 |
| `start`, `end` | as `/api/history` (default: last 24 h) |
| `max_gap_s` | optional: a pair further apart than this reports `gap_exceeded` and no rate. No default |
| `cursor` | token from the previous page (same `uid`, `start`, `end`, `max_gap_s`) |

One entry in `rates` per pair of consecutive observations, oldest first:

```
{"uid": "ethernet-wan", "object_class": "physical", "max_gap_s": null,
 "rates": [{
   "uid": "ethernet-wan", "object_class": "physical",
   "start_ts": 1790380800.25, "end_ts": 1790380810.75, "interval_s": 10.5,
   "time_source": "collected_at", "time_precision": "fractional",
   "reduced_precision": false, "interval_error_bound_s": null,
   "status": "ok",
   "rx": {"status": "ok", "delta_bytes": 131250, "bytes_per_s": 12500.0,
          "bits_per_s": 100000.0, "mbps": 0.1},
   "tx": {"status": "ok", "delta_bytes": 52500, "bytes_per_s": 5000.0,
          "bits_per_s": 40000.0, "mbps": 0.04}}],
 "count": 1, "next_cursor": null, "has_more": false, "skipped_lines": 0}
```

- `status` is `ok` only when RX and TX are both `ok`; otherwise it is the
  first failing status: `missing_baseline`, `missing_value`,
  `identity_mismatch`, `invalid_time`, `non_monotonic_time`,
  `counter_decrease`, `gap_exceeded` (see `counters.py`) or `suspect_input`.
  Rates and `delta_bytes` are `null` unless the direction is `ok`.
- Time uses fractional `collected_at` when both observations have it,
  otherwise integer `collected_ts` with `reduced_precision: true`.
- An observation with no record for the interface (for example a failed
  read) is never skipped or zero-filled: the pairs on either side of it are
  reported with `missing_value` / `missing_baseline`.
- `suspect_input`: an observation written by v0.4.4 or earlier whose
  `collector_errors` names `wan_devices` may hold re-stamped counters, so no
  rate is calculated from it.
- Physical, `sdwan` and `wbond` interfaces are separate identities; their
  counters overlap and are never added together.
- Errors and limits follow `/api/history` (400 for a missing or invalid
  `uid`, parameter or cursor; 410 for an expired cursor). The cursor also
  carries the last observation as the baseline for the next page, so the pair
  across a page boundary is calculated.

### Cursors

Cursors are base64url JSON and are opaque to clients. A history cursor holds
the UTC day segment and a byte offset; a Recording cursor holds a byte offset
into `samples.jsonl`. Both carry a digest of the query parameters they were
issued for (`start`, `end`, `sample_source` / `group`, `after`, the job), so
reusing a cursor with different filters is rejected. `limit` may change
between pages. Cursors are only ever resumed on a record boundary and are
re-validated against the real files on every use, so a forged cursor can be
rejected but cannot steer a read outside the telemetry tree. Nothing is
re-read: each page resumes at the byte after the last line it consumed.

### Resource limits

- at most 250 observations per page;
- at most 1 MiB of file data examined per request, counting every byte read:
  accepted, filtered-out, malformed and skipped lines alike;
- at most 512 KiB of accepted (persisted) records per page, which keeps a
  response body to roughly 1 MiB (decoded JSON is about 1.6x the persisted
  size);
- a line longer than 256 KiB is never a record: it is skipped, counted in
  `skipped_lines`, and not parsed. The skip can span pages (the cursor then
  points inside that line and the next request finishes skipping it);
- only complete (newline-terminated) lines are served, so a Recording that is
  being appended to is safe to page;
- no total-count scans, no reverse order, no offset/row pagination.

A page may therefore hold fewer than `limit` samples, and a filtered page can
be **empty while `has_more` is `true`**: keep following `next_cursor`.

Because history files are in collection order, a query with `end` stops at the
first sample after `end` and does not look further in that query.

### Errors

All errors are JSON `{"error": "<message>"}` with no filesystem paths.

| Status | When |
|---|---|
| 200 | valid query |
| 400 | bad `limit`/`start`/`end`/`after`/`sample_source`/`group`, unknown or repeated parameter, `start` after `end`, malformed / mismatched / out-of-range cursor |
| 404 | unknown (or malformed) Recording id, and `/api/recordings/<id>/samples` for a job that is gone |
| 410 | the cursor's backing data expired: its history day file or its Recording was removed by retention |
| 500 | unexpected read failure (logged via `cp.log`, body is `{"error": "internal error"}`) |

When durable storage is unavailable, both endpoints fall back to the bounded
in-memory stores with the same contract (cursors there are sequence positions;
an evicted position is `410`).

## Historical analytics (v0.8.4)

Read-only and bounded. Each request streams stored observations through the
analysis (memory holds one page), scans at most **8 MiB** of history and
**1,500** observations, and reports `window.truncated` with `next_start` when
a limit stopped it; ask again from `next_start` to continue. The default
window is the last 6 hours. Parameters: `start`, `end` (UTC epoch seconds),
`uid` (cellular and correlation), `max_events` (1-500), `window_s`
(correlation, 60-1800).

What the results do **not** claim: events are detected **between two polls**
(`detected_between`, `exact_time: false`; no handover time is claimed);
bond weights are NCOS control values, not traffic shares; flow counts are
gauges; physical, overlay and bond throughput are separate layers and are
never added; packet-loss percentages are not computed; correlation is timing
evidence, never a cause.

### `GET /api/analytics/cellular`
Per modem: `serving_cells[]` (privacy-safe `cell_key_hash`, samples, share,
`observed_seconds`, RF statistics, per-component statistics, `confidence`),
`technology_usage` (LTE versus NR carrier presence, by service mode),
`health` (NCOS score, category and signal strength history), `excluded`
(readings left out of the statistics and why), `events[]`
(`serving_cell_change`, `radio_mode_change`, `ca_composition_change`,
`ca_bandwidth_change`, `component_role_change`, `connection_state_change`,
`health_category_change`, `modem_missing`, `observation_gap`) and
`data_quality`. Example event:

```json
{"type": "ca_composition_change", "uid": "mdm-1c00798a",
 "first_observed_ts": 1795196762, "detected_between": [1795196702, 1795196762],
 "uncertainty_s": 60, "time_resolution": "between_polls", "exact_time": false,
 "confidence": "normal",
 "detail": {"added": ["NR:Band n77:630720"], "removed": []}}
```

### `GET /api/analytics/sdwan_history`
`steering.rules[]` (flow gauge statistics, observed applications, preferred
WAN history, definition revisions with their attribution),
`bonds[]` (member count, per-member runtime `config_weight`,
`download_weight` and `upload_weight` statistics, link and attach states,
member `throughput` with the physical and overlay layers kept separate),
`interfaces.by_interface[]` (per-interface rates with excluded intervals
listed), `quality` (QoE latency and SWANS latency/jitter; invalid readings
excluded and counted) and `events[]`.

### `GET /api/analytics/correlation`
`correlation.pairs[]` pairs a cellular event with an SD-WAN event first
observed within `window_s`. `evidence` is `A_same_poll_same_path`,
`B_same_poll`, `C_adjacent_polls_same_path` or `D_adjacent_polls`
(`same_path` means the SD-WAN event names the same modem). Compare with
`event_rates_per_hour`, which shows how often each event type occurs on its
own. `cause_asserted` is always `false`.

### Cellular RF validity (v0.8.4)
Every RF reading carries a validity: `valid`, `missing`, `placeholder_zero`,
`placeholder_context`, `implausible_range` or `unknown_scale`. The raw value
stays in `raw` / `sinr_raw` / `rf_raw` when it is not valid. Component SINR
carries `sinr_scale`: `tenths_corroborated`, `tenths_family_corroborated`,
`tenths_family_inferred` (**inferred**, not verified), `db_decimal_token` or
`unknown`. A raw 0 on a measured carrier is `implausible_range`; a value on an
unmeasured entry (inactive, 0 MHz, or RSSI 0 with RSRP 0) is a placeholder;
RSRQ -3 is never rejected by value alone. Each carrier lists every NCOS
`role_aliases` under which it was reported. Records written before v0.8.4 are
checked against the same plausibility ranges when analyzed.

### Configuration version attribution (v0.8.4)
Each observation's `config_versions` entry now has `attribution`: `read` (its
source was read in that pass), `latest_known` (the latest known version is
referenced; no fresh read and `changed: false`) or `unspecified` (record from
before v0.8.4).

### Known issue (v0.8.5): legacy `steering_definitions` startup baseline
Found in live R2400 verification. If the first steering read after app start
fails (`empty_source`), the legacy `steering_definitions` object records a
baseline (v1) from the missing data. The first successful read then bumps it
to v2 (`changed: true`). No event reaches `/api/changes` and the new
`steering_rule_defs` object is not affected: it stayed at v1 and referenced
`latest_known` during the failure. Treat a legacy `steering_definitions`
v1 to v2 step at startup as an artifact, and use `steering_rule_defs` for
configuration versioning. Planned fix for a future release: do not baseline
the legacy object until a read completes successfully.

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

`run_tests.py` also runs `durable_tests.py`, `query_tests.py`, and (v0.5.0)
`acquisition_tests.py` (acquisition timing, WAN failed-read/recovery, persisted
records, legacy compatibility), `failed_read_tests.py` (failed-read invariant
for every runtime section, including persisted history and Recording) and
`counters_tests.py` (hand-calculated counter math), and (v0.8.3)
`stage_a_tests.py` (bonding fingerprint and flow-total regressions) and
`foundation_tests.py` (steering definitions/changes, application matching,
configured bonding, component RF, QoE validity, persistence, new APIs; fixtures
in `foundation_fixtures.py`, synthetic and sanitized). The suite has been run on CPython 3.8.20 (the cppython
version) as well as the workstation interpreter.

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
versioning, collector timeout). `tests/durable_tests.py` covers durable
storage; `tests/query_tests.py` (0.4.4) covers the query APIs: multi-day
paging, inclusive bounds, source/group/`after` filters, cursor rejection and
expiry, malformed and oversized lines, byte ceilings, active-Recording
appends, fallback mode and read-side-effect freedom.

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
| `status/wan/steering/rules[].xpolicy` | Effective steering definitions (`steering_rule_defs`) | Verified on R1900 + R2400, NCOS 7.26.81 |
| `status/wan/steering/stats.rules[].apps` | Runtime application matching; key == `config/identities/ip[].name` | Verified, NCOS 7.26.81 |
| `config/identities/ip`, `config/identities/intent`, `config/sdwan_adv/traffic_class` | Exact-UUID destination and Traffic Class resolution | Verified, NCOS 7.26.81 |
| `config/sdwan_adv/wan_bonding` | Configured bonds: `dynamic_weight`, selection criteria (`wan_bonding_config`) | Verified, NCOS 7.26.81 |
| `config/sdwan_adv/qoe` → `pkt_loss_cnt.enabled` | QoE config context | Verified, NCOS 7.26.81 |

Identity note (from STA): `info.uid` is the control-plane/routing identity
for overlays; the WAN object key (e.g. `wbond-wbond0_hub0`) is the
`status/wan/devices` key; `info.iface` is the Linux interface. WBOND
`members_list` entries expose the underlying WAN as `"intf name"` (NOT
`"profile name"`, NOT `dep_wandevs`).

## Appdata

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `sdwan_analyzer_web_port` | string (integer port) | No | Effective web listening port. Namespaced by app per convention `<app_name>_web_port` (device-level appdata is shared). |
| `sdwan_analyzer_interval_minutes` | string (1, 5, 15, 30 or 60) | No | Scheduled collection interval. Default 15 (applied in code, never written). Invalid values fall back to 15. |
| `sdwan_analyzer_history_max_mb` | string (integer, 32-1024) | No | Maximum disk used by normal history. Default 256. Values outside the range are clamped; invalid values fall back to 256. Applied in code, never written. |
| `sdwan_analyzer_history_max_days` | string (integer, 1-30) | No | Maximum age of normal history. Default 7. Values outside the range are clamped; invalid values fall back to 7. Applied in code, never written. |

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
  `qoe.paths[].pkt_loss_stats_raw`; only clear latency samples are normalized.
  From 0.8.3 latency sentinel candidates (4294967295) are withheld and flagged,
  never interpreted.
- **WBOND bandwidth fields** and the criterion-weight to runtime-weight
  relationship — reported raw; units and formula unverified.
- **Component SINR between -50 and 50** — kept as `sinr_raw` with
  `sinr_scale: ambiguous`.
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

Already delivered (removed from this list): durable historical storage and
durable Recording storage (v0.4.0-0.4.3), the query APIs (v0.4.4), and the pure
counter delta/rate library (v0.5.0, library only).

- SD-WAN/WBOND overlay source-IP resolution wiring (control-plane
  `local_ip`/`remote_ip`; documented in collectors, only needed for active
  testing, not visibility).
- Recording UI and Recording analysis.
- Exposing computed counter rates through the service/API (undecided), and
  all other calculations: averages, percentiles, rollups of any period,
  utilization percentages and anomaly scoring. No rollup formula is defined.
- A default collection-gap threshold (gap handling is a caller policy).
- Counter wrap handling (counter width is unknown, so a decrease is never
  corrected).
- Device validation catalog (captive-modem model naming) for friendly names.
- The production UI and the template design system (`static/`), pending the
  approved design artboards.
