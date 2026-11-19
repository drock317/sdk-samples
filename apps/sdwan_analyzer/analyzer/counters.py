"""Interface counter deltas and rates (pure mathematical analysis).

No I/O, no `cp`, no storage access, no clock, no imports from the rest of the
analyzer: plain records in, plain dicts out. Derivation and evidence:
docs/analytics/counter-derivation.md and docs/analytics/counter-semantics.md.

Input records are the normalized interface records produced by
telemetry.interface_records (and served by /api/history, Recording samples and
/api/wan): dicts with `uid`, `object_class`, counter fields such as
`rx_bytes`, and the time fields `collected_at` (fractional, optional) and
`collected_ts` (integer seconds).

What this module deliberately does NOT do:

* No counter-width assumption and no wrap correction. A counter that
  decreases yields status `counter_decrease` and no delta, whatever the cause
  (reboot, reconnect, new interface, wrap).
* No default gap threshold. `max_gap_s` is an explicit caller policy; with
  the default None, gaps are not judged at all.
* No interpolation, no bridging over records with missing values, and no
  summation across interfaces or object classes (physical, sdwan and wbond
  counters overlap; see counter-semantics.md section 4).
* The persisted `reset_suspected` hint is ignored: resets are re-derived from
  the counter values themselves.

Python 3.8 compatible.
"""
import math

# --- status vocabulary ---------------------------------------------------

STATUS_OK = 'ok'
STATUS_MISSING_BASELINE = 'missing_baseline'      # no previous record
STATUS_MISSING_VALUE = 'missing_value'            # field absent/invalid
STATUS_IDENTITY_MISMATCH = 'identity_mismatch'    # uid/object_class differ
STATUS_INVALID_TIME = 'invalid_time'              # no usable timestamps
STATUS_NON_MONOTONIC_TIME = 'non_monotonic_time'  # t1 <= t0
STATUS_COUNTER_DECREASE = 'counter_decrease'      # c1 < c0: no delta
STATUS_GAP_EXCEEDED = 'gap_exceeded'              # interval > caller policy

# --- time vocabulary -----------------------------------------------------

TIME_SOURCE_COLLECTED_AT = 'collected_at'
TIME_SOURCE_COLLECTED_TS = 'collected_ts'
PRECISION_FRACTIONAL = 'fractional'
PRECISION_INTEGER_SECOND = 'integer_second'
# Each integer timestamp is a truncation of a true time in [ts, ts + 1), so
# the difference of two of them can be off by strictly less than 1 s. This is
# the truncation component only; read latency is not bounded by this module.
INTEGER_SECOND_ERROR_BOUND_S = 1.0

# --- fields --------------------------------------------------------------

BYTE_FIELDS = ('rx_bytes', 'tx_bytes')
COUNTER_FIELDS = ('rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets',
                  'rx_errors', 'tx_errors', 'rx_drops', 'tx_drops')

BITS_PER_BYTE = 8
BITS_PER_MEGABIT = 1000000   # decimal SI megabit (not 2**20)


# --- primitive validation ------------------------------------------------

def _is_real(value):
    """True for a finite int/float that is not a bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return not (isinstance(value, float)
                and (math.isnan(value) or math.isinf(value)))


def counter_value(record, field):
    """The counter as a non-negative finite number, else None.

    Missing records or keys, None, booleans, strings, NaN, infinities and
    negative numbers are all "no value". Zero is a real value.
    """
    if not isinstance(record, dict):
        return None
    value = record.get(field)
    if not _is_real(value) or value < 0:
        return None
    return value


def _time_candidate(record, key, positive_only=True):
    if not isinstance(record, dict):
        return None
    value = record.get(key)
    if not _is_real(value):
        return None
    if positive_only and value <= 0:
        return None
    return value


def reading_times(prev_record, cur_record):
    """Choose ONE consistent time base for a pair of records.

    Returns (t0, t1, source, precision) or None when no base is usable.

    Fractional `collected_at` is used only when BOTH records have a valid
    one. Otherwise both fall back to integer-second `collected_ts` (reduced
    precision). The two bases are never mixed within a pair: collected_at is
    taken right after the read, collected_ts at the end of the whole pass.
    Fractional precision is never derived from an integer timestamp.
    """
    a0 = _time_candidate(prev_record, 'collected_at')
    a1 = _time_candidate(cur_record, 'collected_at')
    if a0 is not None and a1 is not None:
        return (float(a0), float(a1), TIME_SOURCE_COLLECTED_AT,
                PRECISION_FRACTIONAL)
    s0 = _time_candidate(prev_record, 'collected_ts')
    s1 = _time_candidate(cur_record, 'collected_ts')
    if s0 is not None and s1 is not None:
        return (s0, s1, TIME_SOURCE_COLLECTED_TS, PRECISION_INTEGER_SECOND)
    return None


# --- result construction -------------------------------------------------

def _result(field, status, prev_record=None, cur_record=None, **kw):
    """Every result has the same keys, whatever the status."""
    cur = cur_record if isinstance(cur_record, dict) else {}
    prev = prev_record if isinstance(prev_record, dict) else {}
    res = {
        'field': field,
        'status': status,
        'uid': cur.get('uid', prev.get('uid')),
        'object_class': cur.get('object_class', prev.get('object_class')),
        'delta': None,
        'interval_s': None,
        'per_second': None,
        'time_source': None,
        'time_precision': None,
        'reduced_precision': None,
        'interval_error_bound_s': None,
        'start_ts': None,
        'end_ts': None,
    }
    res.update(kw)
    return res


def _validate_policy(max_gap_s):
    if max_gap_s is None:
        return
    if not _is_real(max_gap_s) or max_gap_s <= 0:
        raise ValueError('max_gap_s must be None or a positive finite number')


# --- delta and per-second rate -------------------------------------------

def counter_delta(prev_record, cur_record, field, max_gap_s=None):
    """Delta and per-second rate of one counter between two records.

    Math, for readings c0 at t0 and c1 at t1 of the SAME interface with
    t1 > t0 and c1 >= c0:

        delta      = c1 - c0
        interval_s = t1 - t0
        per_second = delta / interval_s

    Checks run in this order and the first failure decides `status`:
    identity_mismatch, missing_baseline, missing_value, invalid_time,
    non_monotonic_time, counter_decrease, gap_exceeded, else ok.

    `max_gap_s` (seconds, default None = no gap judgement) is the caller's
    policy. When the interval exceeds it, status is gap_exceeded and the
    delta and interval are still reported (the byte total over the gap is
    true) but `per_second` is None, because an average over a long gap is
    exactly what the policy refuses.

    Raises ValueError only for an invalid `max_gap_s` or `field`.
    """
    if field not in COUNTER_FIELDS:
        raise ValueError('unknown counter field: {!r}'.format(field))
    _validate_policy(max_gap_s)

    # identity: only compare when both records exist.
    if isinstance(prev_record, dict) and isinstance(cur_record, dict):
        if (prev_record.get('uid') != cur_record.get('uid')
                or prev_record.get('object_class')
                != cur_record.get('object_class')):
            return _result(field, STATUS_IDENTITY_MISMATCH,
                           prev_record, cur_record)
    if not isinstance(prev_record, dict):
        return _result(field, STATUS_MISSING_BASELINE, prev_record,
                       cur_record)
    if not isinstance(cur_record, dict):
        return _result(field, STATUS_MISSING_VALUE, prev_record, cur_record)

    c0 = counter_value(prev_record, field)
    c1 = counter_value(cur_record, field)
    if c0 is None or c1 is None:
        return _result(field, STATUS_MISSING_VALUE, prev_record, cur_record)

    times = reading_times(prev_record, cur_record)
    if times is None:
        return _result(field, STATUS_INVALID_TIME, prev_record, cur_record)
    t0, t1, source, precision = times
    reduced = precision == PRECISION_INTEGER_SECOND
    timing = {
        'time_source': source,
        'time_precision': precision,
        'reduced_precision': reduced,
        'interval_error_bound_s':
            INTEGER_SECOND_ERROR_BOUND_S if reduced else None,
        'start_ts': t0,
        'end_ts': t1,
    }
    interval = t1 - t0
    if interval <= 0:
        return _result(field, STATUS_NON_MONOTONIC_TIME, prev_record,
                       cur_record, interval_s=interval, **timing)
    if c1 < c0:
        # No delta, no wrap correction: counter width is not known.
        return _result(field, STATUS_COUNTER_DECREASE, prev_record,
                       cur_record, interval_s=interval, **timing)
    delta = c1 - c0
    if max_gap_s is not None and interval > max_gap_s:
        return _result(field, STATUS_GAP_EXCEEDED, prev_record, cur_record,
                       delta=delta, interval_s=interval, **timing)
    return _result(field, STATUS_OK, prev_record, cur_record, delta=delta,
                   interval_s=interval, per_second=delta / interval, **timing)


# --- byte counters: bytes/s, bits/s, Mbps --------------------------------

def bits_per_second(bytes_per_s):
    """bits/s = 8 x bytes/s (byte counters verified; see semantics doc)."""
    return None if bytes_per_s is None else bytes_per_s * BITS_PER_BYTE


def megabits_per_second(bits_per_s):
    """Mbps = bits/s / 1 000 000 (decimal SI)."""
    return None if bits_per_s is None else bits_per_s / BITS_PER_MEGABIT


def byte_rate(prev_record, cur_record, field, max_gap_s=None):
    """counter_delta for a byte counter plus bytes/s, bits/s and Mbps.

    `field` must be `rx_bytes` or `tx_bytes` (NCOS `in`/`out`, verified as
    bytes against `ibps`/`obps` of status/wan/stats). The extra keys are None
    unless status is ok:

        bytes_per_s = delta / interval_s
        bits_per_s  = 8 x bytes_per_s
        mbps        = bits_per_s / 1 000 000
    """
    if field not in BYTE_FIELDS:
        raise ValueError('byte_rate needs a byte counter: {!r}'.format(field))
    res = counter_delta(prev_record, cur_record, field, max_gap_s)
    bytes_per_s = res['per_second']
    bits = bits_per_second(bytes_per_s)
    res['bytes_per_s'] = bytes_per_s
    res['bits_per_s'] = bits
    res['mbps'] = megabits_per_second(bits)
    return res


def interface_rates(prev_record, cur_record, max_gap_s=None):
    """All counters of ONE interface between two of its records.

    Returns {'uid', 'object_class', 'counters': {field: result}} where byte
    fields use byte_rate() and the rest counter_delta(). Per-interface only:
    nothing is ever summed across interfaces or object classes.
    """
    counters = {}
    for field in COUNTER_FIELDS:
        if field in BYTE_FIELDS:
            counters[field] = byte_rate(prev_record, cur_record, field,
                                        max_gap_s)
        else:
            counters[field] = counter_delta(prev_record, cur_record, field,
                                            max_gap_s)
    cur = cur_record if isinstance(cur_record, dict) else {}
    prev = prev_record if isinstance(prev_record, dict) else {}
    return {'uid': cur.get('uid', prev.get('uid')),
            'object_class': cur.get('object_class',
                                    prev.get('object_class')),
            'counters': counters}


def series_deltas(records, field, max_gap_s=None):
    """Deltas for consecutive pairs of `records`, in the order given.

    Returns len(records) - 1 results (empty for fewer than two records).
    Pairs are strictly consecutive: a record with a missing value spoils the
    two pairs it belongs to rather than being bridged over, and after a
    counter decrease the lower reading simply becomes the next baseline. The
    caller supplies collection order; out-of-order times are reported as
    non_monotonic_time, not re-sorted. A series that mixes interfaces yields
    identity_mismatch at each boundary.
    """
    seq = list(records) if records is not None else []
    return [counter_delta(seq[i - 1], seq[i], field, max_gap_s)
            for i in range(1, len(seq))]


def series_byte_rates(records, field, max_gap_s=None):
    """byte_rate() for consecutive pairs; same pairing rules as above."""
    seq = list(records) if records is not None else []
    return [byte_rate(seq[i - 1], seq[i], field, max_gap_s)
            for i in range(1, len(seq))]
