# SD-WAN Analyzer: architecture and roadmap

Status of this document: draft for review, on branch
`feature/sdwan-v0.5.0-analytics-foundation` (baseline commit `56d5b21`, v0.4.4,
plus the uncommitted v0.5.0 work described below). Source of truth for what
exists is `apps/sdwan_analyzer/readme.md` and the code. This document adds
structure and states what is **not** built.

Each item is tagged with one of four states:

| Tag | Meaning |
|---|---|
| **Implemented** | In the code at the baseline commit and covered by the local test harness |
| **Approved direction** | Agreed in project instructions, not yet built |
| **Deferred** | Acknowledged and intentionally postponed |
| **Unresolved** | A decision or fact that is still open |

The only calculation code is the pure library `analyzer/counters.py` (counter
deltas, per-second rates, bits per second, Mbps). Nothing calls it: no service,
storage, or API path computes, stores, or serves a rate. There are no averages,
percentiles, rollups, utilization figures, or anomaly scores anywhere. Counters
are stored raw.

## 1. Layered architecture

Data flows in one direction. Each layer depends only on the layer before it.

```
acquisition -> normalization -> persistence -> mathematical analysis -> API -> presentation
collectors.py   normalize.py     storage.py      (new) counters.py      service.py   (future)
                telemetry.py     persistence.py                         web.py
                cellular.py     durable.py
```

| Layer | Modules | Responsibility | State |
|---|---|---|---|
| Acquisition | `collectors.py` | Read-only NCOS `status/` and `config/` reads with a 15 s wall-clock guard. No business logic | Implemented |
| Normalization | `normalize.py`, `telemetry.py`, `cellular.py` | Convert raw NCOS shapes to stable records; timestamp envelope; counter and SWANS/QoE/WBOND records | Implemented |
| Persistence | `storage.py`, `persistence.py`, `durable.py`, `recording.py` | Bounded in-memory stores, durable segmented JSONL under `tmp/telemetry`, Recording jobs | Implemented |
| Mathematical analysis | `counters.py` | Pure functions over normalized records. Imports only `math`. No I/O, no `cp`, no storage access, no UI imports. Used by the interface rate API (`/api/analytics/interfaces`, v0.6.0) | Implemented (v0.5.0, wired in v0.6.0) |
| API | `service.py`, `query.py`, `web.py` | `service` is the only layer the web tier may call. Read-only paginated `/api/history` and Recording samples | Implemented |
| Presentation | `static/`, `index.html` | Throughput trends UI exists (v0.7.x); further screens follow approved artboards | Partly implemented |

Rules that keep the layers separate:

- Analysis modules take plain Python values (records, numbers, timestamps) and return plain values.
  They never read files, call `cp`, or import `service`, `web`, or the durable store.
- Persistence stores normalized observations only, never derived values, until a derived value is
  explicitly approved for storage. Analysis is recomputable from stored observations.
- The web tier calls `service` only and never reaches NCOS.
- Presentation consumes API output only.

## 2. Implemented capabilities (baseline v0.4.4, plus v0.5.0 items marked)

These are described in the app readme. Summarised here for orientation.

- **Acquisition modes**: startup, scheduled (5, 15, 30, or 60 minutes, default 15, clock aligned),
  manual refresh by group, and backend-owned Recording jobs (5 s WAN/WBOND, 10 s steering/QoE/SWANS,
  30 minute cap, one job at a time).
- **Normalization**: WAN object classes (`physical`, `sdwan`, `wbond`), friendly naming, logical
  Traffic Steering grouping and fingerprints, Traffic Classes, SWANS, QoE, WBOND runtime, cellular and
  carrier aggregation telemetry with privacy filtering.
- **Timestamp envelope**: `metric_ts`, `collected_ts`, `timestamp_source` (`ncos` or `collector`).
- **Interface counters**: raw `rx/tx_bytes`, `packets`, `errors`, `drops` and a `reset_suspected`
  flag. No deltas or rates are produced by the service.
- **Fractional acquisition time `collected_at`** (v0.5.0): an additive field on interface records,
  captured right after the successful `status/wan/devices` read, persisted inside each record,
  absent (never invented) on older records.
- **Failed-read invariant** (v0.5.0): a failed collector read never creates a newly timestamped
  historical measurement from last-known-good values. Applied to every runtime observation section
  (section 7). Current state and section freshness keep serving last-known-good. This was a verified
  defect, now fixed and regression-tested for each section.
- **Counter math library** (v0.5.0): `analyzer/counters.py`. Deltas, per-second rates, bits per
  second, decimal Mbps; explicit statuses for missing baseline or value, identity mismatch, invalid
  or non-monotonic time, decreasing counters, and caller-defined gaps. See
  `docs/analytics/counter-derivation.md`.
- **Durable storage**: survives app restart and reboot; may be wiped by an app update (accepted).
  Retention is 7 days or 32 MB of history, and 10 completed Recordings or 32 MB.
- **Query APIs**: `/api/history` and `/api/recordings/<id>/samples`, cursor paginated, bounded reads.
- **Local test harness**: `tests/run_tests.py` plus `durable_tests.py`, `query_tests.py`,
  `acquisition_tests.py`, `counters_tests.py`; standard library only. Run on CPython 3.8.20.

## 3. Approved engineering direction

From project instructions for the v0.5.0 phase:

1. Existing collectors, normalization, durable storage, and query APIs are the validated foundation
   and are not redesigned.
2. Mathematical analysis lives in testable backend modules independent of the UI.
3. Field semantics, units, timestamp alignment, missing data, counter reset or rollover, and partial
   observations are validated **before** a calculation is written.
4. No production analytical formula without a documented derivation and an expected-result test.
5. First slice: interface counter deltas and rates, implemented in `analyzer/counters.py`, with
   derivations under `docs/analytics/`.
6. Bits per second and Mbps conversion is included once byte units are verified (see
   `docs/analytics/counter-semantics.md`). Verified, with limits stated in the derivation doc.
7. No collection-gap threshold is fixed without a documented and validated rationale. Gap handling
   is an explicit caller policy (`max_gap_s`, default none) until a threshold is agreed.
8. Branded application shell and base components are built by Kiro using the Ericsson branding skill
   and existing web standards. Detailed layouts, hierarchy, and interactions follow approved artboards
   led by ChatGPT. Kiro does not independently design product screens.
9. No router deployment, no change to shared `cp.py`, no change to Speedtest Analyzer during this phase.
10. Nothing is pushed or merged until the implementation and these documents are reviewed.

## 4. Deferred capabilities

Not built, and not to be implied by any current document or UI:

- Storing counter rates. (Interface rates are exposed read-only by `/api/analytics/interfaces` since v0.6.0.)
- Counter wrap correction. Counter width is unknown, so a decrease is never corrected.
- A default collection-gap threshold.
- Averages, percentiles, rolling statistics, and bandwidth utilization percentages.
- **Daily rollups.** No rollup formula, bucket boundary, time zone rule, or storage format has been
  defined. Any rollup requires its own derivation document first. Nothing in this repository
  specifies one.
- Anomaly scoring and alerting.
- Drop and error interpretation (counter meaning is documented but not interpreted as loss).
- QoE packet-loss and WBOND bandwidth-field interpretation (units unproven).
- Production UI, dashboards, Recording UI, and Recording analysis.
- SD-WAN overlay source-IP resolution wiring.
- Device validation catalog for friendly model names.
- Cross-version migration of durable data.

## 5. Decisions and unresolved items

Resolved in this phase:

| # | Item | Resolution |
|---|---|---|
| U1 | Stale counters recorded as fresh after a failed `wan_devices` read | Fixed in `Service._wan_telemetry` and `_build_observation`. Regression tests in `acquisition_tests.py` |
| U2 | Integer-second `collected_ts` limits rate precision | Fractional `collected_at` added (option B). Legacy records stay readable and are flagged reduced precision |
| U13 | Primary identity re-stamped when `primary_device` not read in the pass | Recording `wan` group now reads `primary_device`; identity recorded only from a same-pass successful read, otherwise omitted |
| U14 | Failed config read postponed the hourly reconcile | `_last_config_ts` advances only when every config read succeeds |
| U7 | Readme "Deferred" list stale | Corrected in `apps/sdwan_analyzer/readme.md` |
| U9 | Same re-stamping for QoE, WBOND, steering runtime (found by audit, also SWANS samples, steering events, and the primary identity) | Fixed with one rule in `Service._build_observation`. See section 7 |

Still open:

| # | Question | Why it matters | Current position |
|---|---|---|---|
| U3 | Counter width (32 or 64 bit) and whether a wrap is possible | Decides if a decrease is a reset or a wrap | Not provable from REST. A decrease is always `counter_decrease` with no delta |
| U4 | Collection-gap threshold | A rate over a long gap hides bursts and may span a reset | No default. Caller policy `max_gap_s` |
| U5 | Where derived values are exposed (new API fields or endpoint) | API contract and storage growth | Not decided. `counters.py` is a library only |
| U6 | Whether analytics results are ever persisted | Storage budget on R1900 and recomputability | Default: compute on read, store nothing |
| U8 | `normalize._num` passes booleans through unchanged | A boolean counter would be treated as a number | `counters.py` rejects booleans itself. `_num` not changed |
| U10 | Counter behaviour on modem reconnect, reboot, or tunnel re-creation | Defines how often `counter_decrease` occurs and what it means | Not observed. Needs a controlled event, outside read-only research |
| U11 | On-router read latency of `status/wan/devices` | Bounds how far `collected_at` is from the true sampling instant | Not measured. No deployment authorised |
| U15 | `config_versions` on observations is derived from cached config | A startup with failed config reads versions the partial state (documented v0.4.x behaviour) | Not changed |

## 6. Phase plan

| Step | Output | State |
|---|---|---|
| 1 | Architecture and roadmap (this file) | Done, in review |
| 2 | Counter semantics research and evidence | Done, `docs/analytics/counter-semantics.md` |
| 3 | Fix stale-counter defect (U1) and add `collected_at` (U2) | Done, uncommitted |
| 4 | Derivation document for delta and rate | Done, `docs/analytics/counter-derivation.md` |
| 5 | Deterministic fixtures with hand-computed results | Done, `tests/counters_tests.py` |
| 6 | `analyzer/counters.py` and tests | Done |
| 7 | Generalized failed-read invariant (U9), version 0.5.0, full suite on both interpreters | Done, see the final report for counts |
| 8 | Package prepared for manual validation (not deployed) | Done |
| 9 | Review, then push or merge on approval | Waiting for review. Nothing committed or pushed |
| v0.8.0 | Current-state topology and relationships, `GET /api/analytics/topology` | Done, see `docs/analytics/topology.md` |
| v0.8.1 | Read-only WBOND runtime analysis, `GET /api/analytics/wbond` | Done, see `docs/analytics/wbond.md` |
| v0.8.2 | Stored WBOND observations keep `tunnel_iface` and flow-total provenance (`reported`/`summed`/`unknown`) | Done, see `docs/analytics/wbond.md`. Pre-0.8.2 history is not back-filled. Normal collection intervals are 5, 15, 30 and 60 min; sub-5-min is for short recordings; the 1-minute schedule is temporary testing |

## 7. Failed-read invariant and runtime audit

Invariant: **a failed NCOS collector read never creates a newly timestamped historical measurement
from last-known-good cached values.** Only historical observations change. The current state
(`/api/snapshot`, section endpoints, `section_freshness`) keeps last-known-good values and original
freshness. Missing telemetry is omitted, never zero filled, and no collection timestamp is invented.
The failure stays visible in the observation's `collector_errors`.

Implementation: `Service.collect` computes the set of collector keys that succeeded in the pass
and `_build_observation` builds each section only from collectors in that set.

| Observation section | Fed by | Audit result | Regression test (`failed_read_tests.py` unless noted) |
|---|---|---|---|
| `wan` connection state | `wan_devices` | Defect (fixed earlier in v0.5.0) | `acquisition_tests.py` failed-read tests, `test_all_runtime_collectors_fail_together` |
| `interfaces` | `wan_devices` | Defect, fixed | same |
| `cellular` | `wan_devices` | Defect, fixed | same |
| `wan.primary_wan`, `is_primary` | `primary_device` | Defect when requested and failed, fixed: omitted, not guessed false. Carry-over when not requested: U13 | `test_failed_primary_device_read` |
| `swans_samples` | `swans` | Re-stamped only if the bounded dedupe set had evicted keys. Fixed | `test_failed_swans_read`, `test_failed_read_does_not_reemit_deduped_samples` |
| `qoe` | `qoe` | Defect, fixed | `test_failed_qoe_read` |
| `wbond` | `wan_bonding` | Defect, fixed | `test_failed_wbond_read` |
| `steering` | rules, stats, events | Defect, fixed. Requires all three | `test_failed_steering_read_whole_parent`, `test_failed_steering_stats_only` |
| `steering_events` | `steering_events` | Re-stamped only on dedupe eviction. Fixed | same, plus the dedupe test |
| `config_versions` | cached config | Not a measurement. Unchanged (U15) | none |
| Recording samples | the same builders | Fixed through the same code | `test_failed_recording_samples` (qoe and wbond groups) |

Audited and not part of historical observations: `wan_policies`, `sdwan_links`, and `loadbalancers`
(snapshot only, never stored), and `/api/cellular_detail` (on-demand read, no history). Collectors
return one `status/wan/steering` read split locally, so the four steering keys fail together in
production; the partial case is tested because the rule is per key.

Verified by mutation: disabling the gating makes 21 distinct checks fail. v0.4.4 behaviour check:
on a success path with a fake collector, the persisted history files, `/api/history` and Recording
sample output are byte-identical (after dropping the additive `collected_at`) to the v0.4.4
baseline commit.
