# Counter delta and rate derivation

Implemented by `apps/sdwan_analyzer/analyzer/counters.py`. Evidence for the
inputs (units, identity, timestamps) is in `counter-semantics.md`. Every worked
number below is asserted by `tests/counters_tests.py`, and was calculated by
hand before the module was written.

Scope: cumulative interface counters on one interface between two readings.
Out of scope and not implemented: averages, percentiles, rollups of any
period, utilization percentages, anomaly scoring, and summation across
interfaces.

## 1. Definitions

A **reading** is a normalized interface record (a dict from
`telemetry.interface_records`, as stored in `/api/history`, Recording samples,
and `/api/wan`) with an identity (`uid`, `object_class`), counters, and times.

For one counter on one interface, two readings `(t0, c0)` and `(t1, c1)`:

| Symbol | Meaning |
|---|---|
| `c0`, `c1` | counter values (cumulative, non-negative, finite numbers) |
| `t0`, `t1` | reading times, seconds, from one consistent time base (section 3) |
| `Δt` | `t1 - t0` |
| `Δc` | `c1 - c0` |

Input integrity. Rates are only as good as the observations they read. Since v0.5.0 a
failed collector read never produces a newly timestamped historical measurement from
cached values (`docs/ARCHITECTURE-AND-ROADMAP.md` section 7). So a stored interface
record is always a real reading taken at its `collected_at` / `collected_ts`, and a
failed read shows up as an absent record, not a flat counter. Consequently a series
with a missing record is handled by the pairing rules in section 5 (the pair is not
bridged), and a long interval between two records is judged only by the caller's
`max_gap_s`. Records written by v0.4.4 and earlier may contain re-stamped counters
from failed reads. Those cannot be identified from the record alone; the observation's
`collector_errors` names `wan_devices` when it happened, and a caller that needs strict
data should discard interface records from observations whose `collector_errors`
includes `wan_devices`.

## 2. Delta and rate

When the readings are valid (section 4) and `Δt > 0` and `c1 >= c0`:

```
Δc          = c1 - c0
Δt          = t1 - t0
per_second  = Δc / Δt
```

For the byte counters `rx_bytes` and `tx_bytes` (NCOS `in` and `out`, verified
as bytes in `counter-semantics.md` section 2):

```
bytes_per_s = Δc / Δt
bits_per_s  = 8 × bytes_per_s            (1 byte = 8 bits)
Mbps        = bits_per_s / 1 000 000     (decimal SI megabit, not 2^20)
```

Packet, error and drop counters use `per_second` only. No bit or Mbps value
is produced for them.

`delta` is an exact integer when both counters are integers. Rates are floats.

## 3. Time base and precision

Two time fields exist on an interface record:

| Field | Resolution | Taken |
|---|---|---|
| `collected_at` | fractional seconds (v0.5.0+) | immediately after the `status/wan/devices` read returned |
| `collected_ts` | integer seconds | after the whole collection pass, `int(clock())` |

Rules (`reading_times`):

1. If **both** readings have a valid `collected_at`, use `collected_at` for
   both. Precision is `fractional`.
2. Otherwise use `collected_ts` for **both**. Precision is `integer_second` and
   the result is flagged `reduced_precision = true`.
3. The two bases are never mixed inside one pair: `collected_at` is earlier
   than `collected_ts` by the rest of the pass, so subtracting across bases
   would add an arbitrary error.
4. A time is valid only if it is a finite positive int or float, not a boolean.
5. Fractional precision is never derived from an integer timestamp.

Error bound for the integer base. Each `collected_ts` is `floor(true time)`, so
the true time lies in `[ts, ts + 1)`. The difference of two such values
therefore differs from the true interval by strictly less than 1 s. The result
reports `interval_error_bound_s = 1.0`. This is the truncation component only.
Read latency is not bounded by this module for either base. The relative rate
error is approximately `1 / Δt` for the integer base (20% at 5 s, 6.7% at 15 s,
0.1% at 15 minutes).

For the fractional base the true sampling instant lies between the start of the
read and `collected_at`, so `collected_at` is an upper bound on the sampling
time. The module reports no numeric bound for it (`interval_error_bound_s` is
`None`) because the read start is not recorded. The effect on a rate is the
difference in read latency between the two reads, which is small compared with
an integer-second truncation, but it is not measured here.

## 4. Status decision order

The first condition that applies decides `status`. Later checks do not run.

| # | Status | Condition | `delta` | `interval_s` | rates |
|---|---|---|---|---|---|
| 1 | `identity_mismatch` | both records exist and `uid` or `object_class` differ | None | None | None |
| 2 | `missing_baseline` | previous record is absent | None | None | None |
| 3 | `missing_value` | current record absent, or either counter missing/invalid | None | None | None |
| 4 | `invalid_time` | no usable time base (section 3) | None | None | None |
| 5 | `non_monotonic_time` | `Δt <= 0` | None | `Δt` | None |
| 6 | `counter_decrease` | `c1 < c0` | None | `Δt` | None |
| 7 | `gap_exceeded` | caller gave `max_gap_s` and `Δt > max_gap_s` | `Δc` | `Δt` | None |
| 8 | `ok` | otherwise | `Δc` | `Δt` | computed |

Notes:

- **Missing versus zero.** A counter is a value if it is an int or float,
  finite, not a boolean, and `>= 0`. `0` is a value. `None`, a missing key, a
  string, a boolean, NaN, infinity and a negative number are all "missing".
- **Decrease.** No delta, no wrap correction, no guess. Counter width is
  unknown (`counter-semantics.md` section 6), and a decrease can be a reboot,
  reconnect, new interface, or wrap. Equal values are not a decrease (delta 0).
- **Gap.** The module defines no threshold. `max_gap_s` is `None` by default,
  meaning gaps are not judged. If given, it must be a positive finite number
  (otherwise `ValueError`). `Δt == max_gap_s` is accepted (strict `>`). When
  exceeded, the byte total over the gap is still true, so `delta` and
  `interval_s` are reported, but the average rate is withheld, since that is the
  value the policy refuses.
- **Persisted hint ignored.** `reset_suspected` is not consulted. Decreases are
  re-derived from the counter values.

## 5. Series

`series_deltas` and `series_byte_rates` take records in the order given and
evaluate strictly consecutive pairs `(r[i-1], r[i])`.

- `n` records give `n - 1` results; fewer than two give an empty list.
- A record with a missing value spoils both pairs it belongs to. It is not
  bridged over, because bridging would silently stretch the interval.
- After a decrease the lower reading is simply the next baseline.
- Out-of-order input is reported as `non_monotonic_time`, not re-sorted.
- A mixed-interface series yields `identity_mismatch` at each boundary.

## 6. No cross-class summation

`physical`, `sdwan` and `wbond` counters overlap (`counter-semantics.md`
section 4: the NCOS aggregate equals the sum of all of them, and the
physical-only total is about 2.8 times smaller on the R1900). The module has no
function that sums, totals or combines results across interfaces, and
`interface_rates` works on one interface only. A test asserts that no public
name suggests aggregation. Any future total must be defined in its own
derivation, per object class.

## 7. Worked examples (hand-calculated, asserted in tests)

Interface `ethernet-wan`, `physical`.

| Reading | `collected_at` | `collected_ts` | `rx_bytes` | `tx_bytes` | `rx_packets` |
|---|---|---|---|---|---|
| A0 | BASE + 0.25 | BASE + 0 | 1 000 000 | 500 000 | 1 000 |
| A1 | BASE + 10.75 | BASE + 10 | 1 131 250 | 552 500 | 1 105 |

Fractional pair A0 to A1 (`Δt = 10.75 - 0.25 = 10.5 s`):

```
rx: Δc = 131 250   bytes/s = 131 250 / 10.5 = 12 500   bits/s = 100 000   Mbps = 0.1
tx: Δc =  52 500   bytes/s =  52 500 / 10.5 =  5 000   bits/s =  40 000   Mbps = 0.04
rx packets: Δc = 105   per_second = 105 / 10.5 = 10
```

The same pair as legacy records (no `collected_at`, so `Δt = 10 - 0 = 10 s`):

```
rx: bytes/s = 131 250 / 10 = 13 125   bits/s = 105 000   Mbps = 0.105
reduced_precision = true, interval_error_bound_s = 1.0
```

The 5% difference between 12 500 and 13 125 B/s is the cost of integer
timestamps over a 10 s interval, which is why the precision is reported.

Zero baseline: `0 -> 5 000` bytes over 10 s is 500 B/s, 4 000 bit/s, 0.004 Mbps.

Decrease: `4 294 967 290 -> 100` is `counter_decrease` with no delta, even though it
looks like a 32-bit wrap (+106). Large values: `10^9` bytes over 10 s is
`10^8` B/s, 800 Mbps.

Gap, 900 s apart with 900 000 bytes: no policy gives 1 000 B/s, 8 000 bit/s,
0.008 Mbps. `max_gap_s = 60` gives `gap_exceeded` with the delta kept and no
rate. `max_gap_s = 900` gives `ok`.

Series `[A0, A1, 1 000 @ +20, 3 000 @ +30, None @ +40, 9 000 @ +50]` gives
`[ok, counter_decrease, ok, missing_value, missing_value]`. The third result is
`2 000 / 10 = 200 B/s = 1 600 bit/s = 0.0016 Mbps`.

## 8. Live cross-check

The module reproduces the router's own `ibps` and `obps`. Using the aggregate
`status/wan/stats` on the R1900 (2026-11-19) with `ts` as the clock:

| Interval | `Δts` (s) | `Δin` / `Δout` (B) | 8·Δin/Δts | reported `ibps` | 8·Δout/Δts | reported `obps` |
|---|---|---|---|---|---|---|
| 1 | 17.001656 | 399 721 / 319 625 | 188 085.7 | 188 086 | 150 397.1 | 150 397 |
| 2 | 16.945963 | 307 783 / 215 702 | 145 300.9 | 145 301 | 101 830.5 | 101 831 |

`test_counters_reproduce_live_ibps_obps` asserts agreement within 1 bit/s.

## 9. Verification limits

- **Two intervals, one router, one device class.** The unit match is exact in
  both intervals and both directions, but it is a short sample on an almost idle
  link (about 150 to 190 kbit/s). It establishes the unit (bytes, with `ibps`
  and `obps` in bits). It says nothing about behaviour at high rate.
- **The aggregate is not an interface.** The cross-check used `status/wan/stats`
  because only it carries a hardware-side clock (`ts`) and a reported rate.
  Per-device counters were verified as bytes by inference: the aggregate equals
  the sum of the per-device `in` and `out` counters (all-device sum lay between
  the bracketing aggregate reads in four of four checks).
- **`ts` is not proven to be uptime.** It advances in real time and has
  sub-second precision, which is all the check needs.
- **`ibps`/`obps` rounding.** The reported values are integers, so agreement is
  asserted within 1 bit/s, not exactly. One value (101 830.5 computed against
  101 831 reported) sits at the rounding boundary.
- **The R2400 was not used for the unit check.** Its stats keys and shape match
  the R1900's. No rate cross-check was made there.
- **Wrap and reset behaviour was not observed.** No decrease occurred during
  the research window. The status rules are defined for the case, not
  validated against a real event.
- **On-router timing.** `collected_at` is captured after the read returns. The
  latency of `status/wan/devices` on-router was not measured. The
  difference between `collected_at` values and true sampling instants is
  therefore bounded in principle (read duration) but not measured.
- **Unit tests use synthetic records.** The service tests verify propagation and
  failure handling with a faked collector. No on-router run of the new code was
  performed (no deployment was authorised).
