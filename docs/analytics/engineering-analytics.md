# SD-WAN Analyzer v0.8.4: engineering analytics and correlation

Design notes for the v0.8.4 backend. User-facing behavior is in
`apps/sdwan_analyzer/readme.md`. Nothing here is persisted: analytics are
computed on read from stored observations.

## 1. Cellular correctness

### 1.1 Reuse of Speedtest Analyzer

| Capability | Source | Status in SD-WAN Analyzer |
|---|---|---|
| Band, bandwidth, carrier identity, role rank, merge, dedupe, service mode, carrier summary | `speedtest_web.py` | Reused unchanged (nine functions, AST-identical; checked by `test_v084_speedtest_function_sync`) |
| Cell-id canonicalization, PLMN normalization | `cellular_analysis._canonical_cell_id`, `_normalize_plmn` | Adapted into `rf_validity.canonical_cell_id` / `normalize_plmn` |
| Component continuity (`_components_match`) | `cellular_analysis.py` | Adapted into `cell_analytics.components_match` / `match_components` |
| Serving-cell distribution, LTE vs NR usage, per-cell RF statistics, change events | `cellular_analysis.py` | Re-implemented over SD-WAN observations in `cell_analytics.py` |
| NCOS cellular health score, category, signal strength | `status` of the WAN device | Collected in `rf_validity.modem_health` |
| `_scale_sinr`, RF statistics | `speedtest_web.py`, `cellular_analysis._rf_stats` | **Not reused.** Speedtest scales by magnitude only, leaves values at or below 50 as dB, retains numeric zeros and averages placeholders |
| Test phases, geography, CA capability catalog, contribution features | Speedtest | Not carried over |

Speedtest Analyzer was not modified. Its RF statistics have the weaknesses
listed above; fixing them is outside this milestone.

### 1.2 Validity semantics (`analyzer/rf_validity.py`)

Per reading: `valid`, `missing`, `placeholder_zero`, `placeholder_context`,
`implausible_range`, `unknown_scale`. The raw number is kept whenever the
reading is not valid.

* **Unmeasured entry** (`entry_unmeasured`): inactive, 0 MHz, or RSSI 0 while
  RSRP is 0 or absent. Every metric on that entry is a placeholder (a 0 is
  `placeholder_zero`, anything else `placeholder_context`). This is how RSRQ
  -3 on the 0 MHz entry is recognized while RSRQ -3 on a measured carrier
  stays valid.
* **Measured entry**: the value must be inside the 3GPP reporting range for
  its RAT. A raw 0 RSRP is `implausible_range`. In range means plausible, not
  accurate.
* **SINR scale** is decided by the source field family, never by magnitude:
  a decimal token is dB (`db_decimal_token`); an integer token is tenths of a
  dB. When the component is the serving carrier the scaled value is compared
  with the serving SINR: within 5 dB `tenths_corroborated`; up to 10 dB still
  `tenths_family_inferred` with the difference reported; beyond 10 dB the
  scale is `unknown` and the value is withheld. A snapshot with a corroborated
  component upgrades its inferred siblings to `tenths_family_corroborated`.
  `tenths_family_inferred` is an inference. U24 tracks what would verify it.

Bounds (dBm / dB): LTE RSRP -140..-44, RSRQ -34..2.5; NR RSRP -156..-31,
RSRQ -43..20; SINR -23..40; RSSI -120..-20. These come from the 3GPP
reporting ranges and should be re-checked against the specification text
before being cited as verified.

### 1.3 Identity

* `cell_key_hash` (v0.8.3) hashes carrier, radio mode, id, PCI and band, so
  a mode change alone moved it. It is kept (plain decimal ids hash exactly as
  before) and now canonicalizes the id first.
* `serving_cell_hash` (v0.8.4) hashes carrier, id type (LTE ECI, or NR NCI in
  5G SA) and the canonical id only. Analytics use it when present; legacy
  records fall back to `cell_key_hash` and a hash change that coincides with
  a mode change is reported with low confidence.
* `role_aliases` lists every NCOS role that reported the same physical
  carrier, mirroring the dedupe rules, so the NR primary survives being
  merged into an indexed SCell.

## 2. History storage

| Item | v0.8.3 | v0.8.4 |
|---|---|---|
| History size / age | 32 MiB / 7 days | 256 MiB / 7 days default; App Data `sdwan_analyzer_history_max_mb` (32-1024), `sdwan_analyzer_history_max_days` (1-30), validated and reported |
| Sweep | startup, UTC rollover, Recording completion | plus hourly |
| Low disk | none | pause below max(512 MiB, 10% of the filesystem); resume above reserve + max(64 MiB, 25% of reserve); oldest whole days removed first; size cap lowered to what the disk can hold |
| Protected day over the cap | grows unbounded | writes pause until the next UTC day (cap floor 16 MiB) |
| Events | per deleted file | plus pause/resume (rate limited to one per 5 minutes) and `history_gap` |
| Reporting | `retention` limits, `free_space` | plus `history_policy`, `space_guard`, `history_coverage`, `retention_events` in `/api/storage` |
| Query seek | linear scan from byte 0 of each day file | verified prefix-maximum index (below) |

Write protection never needs unbounded writes: while paused nothing is
appended to history, and the transition events are rate limited and the
events file is size-rotated.

### 2.1 Verified seek index

Timestamps in a day file are normally increasing but nothing guarantees it, so
a timestamp binary search is not used. `persistence.DayIndex` reads a file
once, incrementally and never during a query, and records every 256 KiB the
maximum timestamp of all valid records before that offset. Those maxima never
decrease, so a lookup is a bisect over verified data: it picks the last offset
whose prefix is entirely older than the requested start. Out-of-order records
simply stop the skip earlier. The index is in memory and rebuildable, only
complete lines are consumed, a shrunk file resets it, and a boundary that is
not a line start is rejected at use. Existing scan limits are unchanged.

## 3. Analytics

All three endpoints stream history through the existing store query (50
observations per page, memory holds one page), scan at most 8 MiB and 1,500
observations, and report `truncated` / `next_start`.

* `/api/analytics/cellular`: see the readme. Time is attributed to the earlier
  poll only when polls are within `max(150 s, 2.5 x interval)`.
* `/api/analytics/sdwan_history`: steering gauges and applications, bond
  members and weights (arbitrary member counts), per-layer throughput through
  `counters.byte_rate`, QoE/SWANS. Stored bond members now carry
  `overlay_uid` (the device whose `info.iface` equals the tunnel interface,
  omitted when ambiguous) so member throughput can name its overlay layer.
* `/api/analytics/correlation`: pairs events within `window_s` (default
  max(2 x interval, 120 s)). Evidence classes: same poll and same path, same
  poll, adjacent polls and same path, adjacent polls. It lists base rates so
  coincidence by chance can be judged. It never states a cause.

Event thresholds that are analysis parameters, not NCOS values: runtime weight
shift of 10 points, QoE latency step of 50 ms and 2x.

## 4. Configuration attribution

`config_versions[name].attribution`: `read` (a source collector succeeded in
that pass), `latest_known` (the latest known version is referenced; never
`changed`), `unspecified` (pre-0.8.4 record). Persisted as `cvl` (names that
are latest-known); its presence marks an attribution-aware record.

## 5. Compatibility

* All v0.8.3 modem and carrier keys remain; new keys are additive
  (`rf_version`, `rf_validity`, `rf_raw`, `serving_cell_hash`,
  `cell_key_state`, `health`, `role_aliases`, component `rssi`, `raw`,
  `empty_reason`, `source`, `sinr_ref_diff_db`).
* Changed values: component `sinr_scale` vocabulary, `validity` values
  (`ambiguous_scale` is now `unknown_scale`/inferred scale), serving RF values
  that are implausible are `null` with the number in `rf_raw`, and the default
  history cap.
* v0.8.3 history stays readable; analytics validate legacy values on read.
* The development machine has Python 3.14 only. Python 3.8 compatibility is
  checked by grammar (`ast.parse(feature_version=(3, 8))`) and a scan for 3.9+
  library calls, not by running 3.8. Run the suite on CPython 3.8 before
  release if a 3.8 interpreter is available.

## 6. Live-validation checklist

See the v0.8.4 completion report; items cover R1900 (static ETH+CELL) and
R2400 (dynamic CELL ONLY, three modems).
