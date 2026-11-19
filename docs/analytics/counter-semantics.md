# Interface counter semantics

Research for the first analytics slice (interface counter deltas and rates).
Evidence comes from the repository code and fixtures, the NCOS docs in
`docs/ncos-api/`, and read-only GET requests to two lab routers on
2026-11-19. No configuration was written, nothing was deployed, and no
speed test or traffic generator was run.

Confidence labels: **Verified** (observed live and cross-checked), **Documented**
(repo docs only), **Inferred** (reasoning, not proven), **Unknown**.

Status of this document (updated with the v0.5.0 implementation): sections 1 to 6
are research findings and are unchanged. Sections 5, 7 and 8 now also record what
was implemented. `counter-derivation.md` holds the formulas.

| Topic | State |
|---|---|
| Byte units, `ibps`/`obps` in bits | **Verified**, short sample (derivation doc section 9) |
| Cross-class double counting | **Verified** on the R1900 |
| Stale counters after a failed read | **Fixed** in v0.5.0, regression-tested |
| Fractional acquisition time `collected_at` | **Implemented** in v0.5.0 (was option B) |
| Counter width, wrap, reconnect behaviour | **Unresolved**, not observed |
| Collection-gap threshold | **Unresolved**, caller policy only |

## 1. Sources and routers

| Router | Model | Notes |
|---|---|---|
| R1900 `10.10.101.1` | R1900-5GB | 1 Ethernet WAN, 2 modems, 2 SD-WAN overlays, 1 WBOND |
| R2400 `10.10.105.1` | R2400-5GF-NA | Ethernet WAN down, 6 modem devices, 3 SD-WAN overlays, 1 WBOND |

Paths read, all GET: `status/product_info`, `status/wan/devices` (once per router),
`status/wan/devices/<id>/stats` (R1900: four devices over three rounds, then six devices over two rounds), `status/wan/stats`
(R1900, seven reads), and a DTD probe (`dtd/status/...`, which returned an error: status paths have
no DTD). The router password was not printed or stored.

## 2. Counter names and units

Path: `status/wan/devices/<device_id>/stats`. Observed keys on every device, both routers:

`collisions, idrops, ierrors, in, ipackets, multicast, odrops, oerrors, opackets, out`

The analyzer maps eight of them (`telemetry._COUNTER_MAP`):

| NCOS key | Analyzer field | Meaning | Unit | Confidence |
|---|---|---|---|---|
| `in` | `rx_bytes` | Received | **bytes** | Verified |
| `out` | `tx_bytes` | Transmitted | **bytes** | Verified |
| `ipackets` | `rx_packets` | Received | packets | Documented |
| `opackets` | `tx_packets` | Transmitted | packets | Documented |
| `ierrors` / `oerrors` | `rx_errors` / `tx_errors` | Errors | count | Documented |
| `idrops` / `odrops` | `rx_drops` / `tx_drops` | Drops | count | Documented |

Not mapped: `multicast`, `collisions`.

### Bytes versus bits (verified)

`status/wan/stats` returns cumulative `in`/`out` plus instantaneous `ibps`/`obps` and `ts`. The
documentation calls `bps`, `ibps`, `obps` "bytes per second". The data shows they are **bits per
second**. Three reads 17 s apart on the R1900, using `ts` as the clock:

| Interval | Δ`ts` (s) | Δ`in` (B) | 8 x Δ`in` / Δ`ts` | Reported `ibps` | 8 x Δ`out` / Δ`ts` | Reported `obps` |
|---|---|---|---|---|---|---|
| 1 | 17.001656 | 399 721 | 188 085.7 | 188 086 | 150 397.1 | 150 397 |
| 2 | 16.945963 | 307 783 | 145 300.9 | 145 301 | 101 830.5 | 101 831 |

The match is exact to rounding in both directions and both intervals. Therefore `in`/`out` are
bytes, `ibps`/`obps` are bits per second, and `bps = ibps + obps` (338 483 = 188 086 + 150 397,
also confirmed). The repository doc `status/wan/stats.md` mislabels these as bytes per second and
has been corrected.

Packet-size plausibility agrees: R1900 `ethernet-wan` shows 628 bytes per received packet
(163 498 738 / 260 320), which is realistic for bytes and implausible for bits.

### Conversion basis (for the derivation document)

For two readings of the same counter at times `t0 < t1` with no reset:

- `delta_bytes = c1 - c0`
- `bytes_per_second = delta_bytes / (t1 - t0)`
- `bits_per_second = 8 * delta_bytes / (t1 - t0)`
- `Mbps = bits_per_second / 1 000 000` (decimal SI megabit, matching ISP and link-rate usage and the
  Ericsson style guide's SI units; not `1 048 576`)

Packet, error, and drop counters use the same delta and per-second form without the factor 8.

## 3. Interface identity and object classes

Observed `info.type` to analyzer `object_class`, and the identity axes (WAN key, `info.uid`,
`info.iface`) agree with the readme:

| WAN key (example) | `info.type` | `info.iface` | `object_class` |
|---|---|---|---|
| `ethernet-wan` | ethernet | `wan` | physical |
| `mdm-1ea0ac` | mdm | `rmnet501` (R2400: `rm_3fc411dd.3`) | physical (cellular) |
| `sdwan-hub0_wan` | sdwan | `gres-hub0_wan` | sdwan (overlay) |
| `wbond-wbond0_hub0` | wbond | `gres_wb0-hub0` | wbond |

Analysis must key counters by the **WAN dictionary key** (`uid` in the records), never by `info.uid`
or `info.iface`, and must carry `object_class` through. A counter series belongs to exactly one key.
Key stability across a reboot is Documented in the readme and not re-tested here. If a key
disappears and reappears, that is a series break, not a continuation.

Disconnected devices still report a full stats object. R2400 disconnected modems show `in: 0` with
small non-zero `opackets` and `out` (for example 16 packets, 1 752 bytes), and the disconnected
R2400 `ethernet-wan` retains 51 644 658 bytes in. So a counter does not necessarily read zero when
a link is down, and a series can sit flat for a long time without being "missing".

## 4. Double counting across object classes (verified)

`status/wan/stats` is **the sum of every device in `status/wan/devices`, including overlays and
WBOND**. Two rounds on the R1900, each reading the aggregate before and after the six per-device
reads:

| Counter | agg before | sum of all six devices | agg after | Sum of physical only |
|---|---|---|---|---|
| `in` | 1 068 138 226 | 1 068 141 422 | 1 068 164 792 | 386 083 174 |
| `out` | 514 388 001 | 514 395 775 | 514 424 655 | 189 969 731 |

The all-device sum lies between the bracketing aggregate reads in every case (both rounds, all four
counters). Summing all classes therefore overstates traffic by about 2.8 times on this router.

Observed relationships (not identities):

- Overlay traffic is carried inside a physical path. R1900 `sdwan-hub0_wan` `in` is 138 359 938 B and
  the underlying `ethernet-wan` is 163 498 738 B. The difference includes encapsulation, other
  traffic on that interface, and counting differences.
- WBOND `in` (340 249 193) is close to, but not equal to, the sum of its two member overlays
  (196 779 744 + 138 359 938 = 335 139 682). The R2400 WBOND (207 984) is far below the sum of its
  three overlays (1 001 540). **No additive identity exists between classes.**

Rules that follow:

1. Never sum counters or rates across `object_class` values.
2. Report each class separately. A total WAN throughput, if ever needed, uses `physical` only.
3. Do not subtract classes to derive overhead. The relationship is not verified.

## 5. Timestamp semantics

| Item | Finding | Confidence |
|---|---|---|
| Per-device `stats` | Contains **no timestamp** | Verified |
| `status/wan/stats` | Has `ts`, a float with sub-second precision that advances in real time (about 17 s over 17 s of wall clock). It resembles uptime. It is not an epoch | Verified for behaviour, Inferred for "uptime" |
| Analyzer `metric_ts` for counters | Equal to `collected_ts`, `timestamp_source = collector` | Verified in code |
| `collected_ts` | `int(self._clock())` evaluated **after** collection finishes (`service.collect`, line 410), so the value is integer seconds, truncated, at the end of the read | Verified in code |
| One pass, many devices | All devices in a pass share one `collected_ts` | Verified in code |
| Read duration | Five REST reads took about 2 s on the R1900 from the workstation. On-router `cp.get` latency was not measured | Verified (REST), Unknown (on-router) |

Implemented in v0.5.0: interface records now also carry `collected_at`, a fractional
Unix time read from the clock immediately after the successful `status/wan/devices`
read returns (not at the end of the pass, not derived from `collected_ts`). It is an
upper bound on the sampling instant, because NCOS sampled the counters somewhere
inside the read. Records from before v0.5.0 lack it and `counters.py` treats them as
reduced precision (integer seconds). The rows above describe the legacy behaviour,
which still applies to those records and to `collected_ts` in general.

Consequences for rates:

- The time base for a counter rate is the collector clock, not a hardware sampling time. The error
  is the read latency (variable) plus truncation (up to 1 s, uniform).
- For two timestamps `Δt` seconds apart the stated interval can be off by up to about 1 s. The
  relative error bound is roughly `1 / Δt`: 20% at 5 s, 6.7% at 15 s, about 0.1% at 15 minutes.
  This bound is arithmetic, not a measurement.
- The state of `cp`'s clock source (router wall clock, which may be unsynchronized) is also
  relevant: the durable layer already tracks `clock_sane`. A clock step makes `Δt` negative or huge.

## 6. Reset, wrap, and missing data

### Observed

- No counter decrease, wrap, or reset was observed in any read. The window was short (minutes).
- Counter magnitude: the largest value was about 1.07e9, below 2^32 (4.29e9). The width is not
  discoverable from the REST API.

### Analyzer behaviour today (`telemetry.interface_records`)

- `reset_suspected` is true if **any** of the eight counters on a device is lower than the previous
  pass. It does not say which counter, and a single decreasing counter flags the whole record.
- The baseline is the previous **pass** held in memory (`_prev_counters`), regardless of source. A
  Recording sample, a manual refresh, or a scheduled observation can each be the baseline for the
  next. The flag in a stored scheduled record may therefore refer to a baseline that was never
  stored in the same series.
- After an app restart there is no baseline, so the first record is `False` even if the counters
  reset because the router rebooted.
- `reset_suspected` is persisted with the observation, so it is usable as a hint but should not be
  trusted as the only reset signal. Analysis will re-derive resets from the stored counter values.

### Candidate causes of a decrease (Inferred, none observed)

Router reboot, modem reconnect creating a new `rmnet` interface (note the R2400 interface names
embed a changing index, for example `rm_3fc411dd.3`), SD-WAN or WBOND tunnel re-creation, and a
counter wrap if the width is 32 bit. A decrease cannot distinguish these from each other.

### Missing and partial data

- A device may lack `stats`. `interface_records` then omits it entirely, so absence means "not
  reported", not zero.
- A counter can be `None` (non-numeric). `reset_suspected` ignores it for that field.
- Partial observations: `wan_devices` is only read by some groups. A `traffic_classes` refresh has no
  `interfaces` list at all, which is legitimate absence.
- Zero must be kept distinct from missing in analysis output.

## 7. Verified defect: stale counters stamped as fresh (FIXED in v0.5.0)

Fixed: a failed `wan_devices` read now produces an observation with no `wan`,
`interfaces`, or `cellular` section (the failure stays in `collector_errors`).
`_prev_counters` is not advanced, and the current state and section freshness keep
their last-known-good values. Regression tests: `acquisition_tests.py`
(`test_failed_read_emits_no_new_measurements`,
`test_failed_read_preserves_current_state_and_freshness`,
`test_recovery_after_failed_read`, `test_failed_read_persisted_observations`).
Reverting the fix makes four of those checks fail. The same rule was then applied to
every runtime observation section (`ARCHITECTURE-AND-ROADMAP.md` section 7). The
description below is the original finding.

Reproduced offline with the project's test harness (no router involved). Sequence: startup
collection succeeds, then `wan_devices` is made to fail, then a manual `wan_paths` refresh runs.

Result: `collector_errors` reports `wan_devices`, yet the persisted observation still contains
`interfaces` with the **old** `rx_bytes` (2000) and the **new** `collected_ts` (+30 s).

Cause: `Service._wan_telemetry` runs `telemetry.interface_records` over the last-known-good raw data
whenever `'wan_devices'` is in the requested keys, and the result is returned for the observation
even when the read failed. Only the current-state cache update is guarded by `res.ok`.

Impact: any rate over a failed read sees zero change over the interval and then a double-length
interval of traffic on the next success. The error is invisible in the data except through the
observation's `collector_errors`. An interface that is genuinely idle looks identical, so analysis
cannot detect it from counters alone.

This is an independently verified defect in the normalization and service layers. It is **not
fixed** here. It needs an explicit decision (U1 in the roadmap). The intended fix is small: skip
interface and cellular records, and do not advance `_prev_counters`, when the `wan_devices` read
did not succeed. The existing test `test_current_telemetry_failed_read_retained` covers only the
current-state snapshot, not the observation.

## 8. Timestamp precision (U2): option B implemented

Decision: option B, a fractional `collected_at` captured right after the successful
counter acquisition. Not mixed with `collected_ts`, not defaulted for old records,
no storage migration (it is one more key inside each interface record).

The options considered, kept for the record:

| Option | Change | Effect |
|---|---|---|
| A | Document the error bound and gate rates by a minimum interval | No code change. Rates at 5 s Recording cadence carry up to about 20% uncertainty |
| B | Add a float `collected_at` (start and end of the read) next to the integer `collected_ts` | Collector and normalization change, storage schema gains a field. Reduces the bound to the read latency |
| C | Use a counter-native clock where one exists | Only `status/wan/stats.ts` has one, and it is an aggregate. Not usable per device |

## 9. Unresolved questions

1. Counter width and wrap behaviour (U3). Needs a router-side source or a vendor statement.
2. Whether modem reconnects reset counters or create new interfaces. Needs a controlled reconnect,
   which is outside the approved read-only research.
3. Whether `idrops`/`odrops` and `ierrors`/`oerrors` count the same events on overlay and WBOND
   interfaces as on physical ones. Not interpreted.
4. Whether `multicast` should be included in `rx_packets`. Not analysed.
5. On-router read latency for `status/wan/devices` under load (R1900 is documented as slow for
   loadbalancers, not for devices).
6. (Done) `ibps`/`obps` units corrected in `docs/ncos-api/status/wan/stats.md`.

## 10. Implications for the first slice (implemented in `counters.py`)

- Operate on stored or in-memory interface records as plain dicts with `uid`, `object_class`,
  counters, and `collected_at` or `collected_ts`. No storage access.
- A delta is defined only between two readings of the same `uid` and counter, both numeric (not
  boolean), non-negative, with strictly increasing timestamps.
- A decrease yields no delta and a reset status. No negative values, no interpolation.
- A missing value or a missing baseline yields an explicit status, not zero.
- Unit conversion uses `8 / Δt` for bits and `1e6` for Mbps, as derived in section 2.
- Rates are never summed across `object_class`.
- Any gap threshold is left as a caller-supplied parameter until it is derived from the schedule and
  validated. No default value is invented here.
