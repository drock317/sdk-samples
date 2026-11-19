"""Deterministic tests for analyzer/counters.py (pure math).

Every expected value below was calculated by hand BEFORE the module was
written; the derivation is in docs/analytics/counter-derivation.md. The
numbers are chosen to be exact in binary floating point (times on .25/.5/.75
fractions, power-of-two-friendly intervals) so equality, not tolerance, is
asserted wherever the arithmetic is exact.

Fixture family "eth" (uid ethernet-wan, physical). Times are offsets from
BASE (a fixed epoch second).

  rec   collected_at   collected_ts   rx_bytes   tx_bytes   rx_packets
  A0    BASE+0.25      BASE+0         1 000 000    500 000    1 000
  A1    BASE+10.75     BASE+10        1 131 250    552 500    1 105
        (one more observation A2 etc. are built per test)

Hand calculation A0 -> A1 (fractional collected_at):
  interval   = 10.75 - 0.25                = 10.5 s
  rx delta   = 1 131 250 - 1 000 000       = 131 250 B
  rx B/s     = 131 250 / 10.5              = 12 500
  rx bit/s   = 12 500 x 8                  = 100 000
  rx Mbps    = 100 000 / 1 000 000         = 0.1
  tx delta   = 552 500 - 500 000           = 52 500 B
  tx B/s     = 52 500 / 10.5               = 5 000
  tx bit/s   = 40 000        tx Mbps = 0.04
  rx packets = (1 105 - 1 000) / 10.5      = 10 per s

Same pair using integer collected_ts only (legacy):
  interval   = 10 - 0 = 10 s
  rx B/s     = 131 250 / 10                = 13 125
  rx bit/s   = 105 000     rx Mbps = 0.105
  reduced_precision = True, interval_error_bound_s = 1.0
"""
import math

from analyzer import counters as C

check = None            # bound by run_tests.main()

BASE = 1790380800


def bind(check_fn):
    global check
    check = check_fn


def _rec(at, ts, rx=0, tx=0, rxp=0, txp=0, uid='ethernet-wan',
         cls='physical', **extra):
    r = {'uid': uid, 'object_class': cls, 'rx_bytes': rx, 'tx_bytes': tx,
         'rx_packets': rxp, 'tx_packets': txp, 'rx_errors': 0,
         'tx_errors': 0, 'rx_drops': 0, 'tx_drops': 0}
    if at is not None:
        r['collected_at'] = at
    if ts is not None:
        r['collected_ts'] = ts
        r['metric_ts'] = ts
        r['timestamp_source'] = 'collector'
    r.update(extra)
    return r


def A0():
    return _rec(BASE + 0.25, BASE + 0, 1000000, 500000, 1000, 0)


def A1():
    return _rec(BASE + 10.75, BASE + 10, 1131250, 552500, 1105, 0)


def _legacy(r):
    return {k: v for k, v in r.items() if k != 'collected_at'}


# --- hand-verified math ----------------------------------------------------

def test_counters_hand_calculated_fractional():
    print('test_counters_hand_calculated_fractional')
    r = C.byte_rate(A0(), A1(), 'rx_bytes')
    check(r['status'] == 'ok' and r['delta'] == 131250 and
          r['interval_s'] == 10.5, 'rx delta 131 250 B over 10.5 s')
    check(r['bytes_per_s'] == 12500.0 and r['bits_per_s'] == 100000.0 and
          r['mbps'] == 0.1, 'rx 12 500 B/s = 100 000 bit/s = 0.1 Mbps')
    check(r['per_second'] == r['bytes_per_s'],
          'per_second is the byte rate for byte counters')
    check(r['time_source'] == 'collected_at' and
          r['time_precision'] == 'fractional' and
          r['reduced_precision'] is False and
          r['interval_error_bound_s'] is None,
          'fractional pair: collected_at, not reduced')
    check(r['start_ts'] == BASE + 0.25 and r['end_ts'] == BASE + 10.75,
          'reported time window')
    t = C.byte_rate(A0(), A1(), 'tx_bytes')
    check(t['delta'] == 52500 and t['bytes_per_s'] == 5000.0 and
          t['bits_per_s'] == 40000.0 and t['mbps'] == 0.04,
          'tx 52 500 B -> 5 000 B/s = 40 000 bit/s = 0.04 Mbps')
    p = C.counter_delta(A0(), A1(), 'rx_packets')
    check(p['delta'] == 105 and p['per_second'] == 10.0,
          'rx packets 105 over 10.5 s = 10 /s')
    check(r['uid'] == 'ethernet-wan' and r['object_class'] == 'physical',
          'identity carried on the result')


def test_counters_legacy_integer_second_precision_flagged():
    print('test_counters_legacy_integer_second_precision_flagged')
    r = C.byte_rate(_legacy(A0()), _legacy(A1()), 'rx_bytes')
    check(r['status'] == 'ok' and r['interval_s'] == 10.0 and
          r['bytes_per_s'] == 13125.0 and r['bits_per_s'] == 105000.0 and
          r['mbps'] == 0.105, 'legacy: 131 250 B / 10 s = 13 125 B/s '
          '= 105 000 bit/s = 0.105 Mbps')
    check(r['time_source'] == 'collected_ts' and
          r['time_precision'] == 'integer_second' and
          r['reduced_precision'] is True and
          r['interval_error_bound_s'] == 1.0,
          'legacy result identifies reduced timestamp precision (<1 s)')
    mixed = C.byte_rate(_legacy(A0()), A1(), 'rx_bytes')
    check(mixed['time_source'] == 'collected_ts' and
          mixed['reduced_precision'] is True and
          mixed['interval_s'] == 10.0,
          'one legacy record: both use collected_ts, never mixed bases')
    mixed2 = C.byte_rate(A0(), _legacy(A1()), 'rx_bytes')
    check(mixed2['reduced_precision'] is True and
          mixed2['time_source'] == 'collected_ts',
          'mixed in the other order behaves the same')
    check('collected_at' in A1() and A1()['collected_at'] == BASE + 10.75,
          'inputs are not mutated by analysis')


def test_counters_fractional_precision_is_never_fabricated():
    print('test_counters_fractional_precision_is_never_fabricated')
    r = C.counter_delta(_rec(None, BASE), _rec(None, BASE + 10, 5), 'rx_bytes')
    check(r['time_precision'] == 'integer_second' and
          r['reduced_precision'] is True and r['interval_s'] == 10 and
          isinstance(r['interval_s'], int),
          'integer timestamps stay integer: no fractional part invented')
    # collected_at invalid on one side -> falls back, not silently used
    bad = _rec(float('nan'), BASE + 10, 5)
    r = C.counter_delta(_rec(BASE + 0.5, BASE), bad, 'rx_bytes')
    check(r['time_source'] == 'collected_ts' and r['reduced_precision'],
          'invalid collected_at -> integer fallback, flagged')


def test_counters_zero_baseline_and_zero_delta():
    print('test_counters_zero_baseline_and_zero_delta')
    z0 = _rec(BASE + 1.0, BASE + 1, 0, 0)
    z1 = _rec(BASE + 11.0, BASE + 11, 5000, 0)
    r = C.byte_rate(z0, z1, 'rx_bytes')
    check(r['status'] == 'ok' and r['delta'] == 5000 and
          r['bytes_per_s'] == 500.0 and r['bits_per_s'] == 4000.0 and
          r['mbps'] == 0.004, '0 -> 5 000 B over 10 s = 500 B/s = 4 000 '
          'bit/s = 0.004 Mbps')
    z0_later = dict(z0, collected_at=BASE + 11.0, collected_ts=BASE + 11)
    idle = C.byte_rate(z0, z0_later, 'tx_bytes')
    check(idle['status'] == 'ok' and idle['delta'] == 0 and
          idle['bytes_per_s'] == 0.0 and idle['mbps'] == 0.0,
          'unchanged counter is a real zero rate, not missing')


# --- the live-evidence regression ---------------------------------------------

def test_counters_reproduce_live_ibps_obps():
    print('test_counters_reproduce_live_ibps_obps')
    # status/wan/stats on the R1900 (2026-11-19): `ts` is the device clock
    # (float seconds), in/out the cumulative counters, ibps/obps what NCOS
    # reported at the later read. Records are used purely as arithmetic
    # inputs; the aggregate is not an interface.
    reads = [(7965.133193838, 1058368158, 494503018),
             (7982.134849429, 1058767879, 494822643),
             (7999.080812104, 1059075662, 495038345)]
    reported = [(188086, 150397), (145301, 101831)]
    recs = [_rec(t, None, i, o, uid='aggregate', cls='physical')
            for t, i, o in reads]
    rx = C.series_byte_rates(recs, 'rx_bytes')
    tx = C.series_byte_rates(recs, 'tx_bytes')
    for n in (0, 1):
        check(rx[n]['status'] == 'ok' and
              abs(rx[n]['bits_per_s'] - reported[n][0]) < 1.0,
              'interval {}: 8 x delta(in) / delta(ts) = {:.1f} bit/s vs '
              'reported ibps {}'.format(n + 1, rx[n]['bits_per_s'],
                                        reported[n][0]))
        check(tx[n]['status'] == 'ok' and
              abs(tx[n]['bits_per_s'] - reported[n][1]) < 1.0,
              'interval {}: 8 x delta(out) / delta(ts) = {:.1f} bit/s vs '
              'reported obps {}'.format(n + 1, tx[n]['bits_per_s'],
                                        reported[n][1]))
    check(rx[0]['delta'] == 399721 and tx[0]['delta'] == 319625 and
          rx[1]['delta'] == 307783 and tx[1]['delta'] == 215702,
          'live byte deltas reproduced exactly')
    # If ibps/obps were BYTES per second the byte rate would match them;
    # it is 8x smaller, so they are bits per second.
    check(abs(rx[0]['bytes_per_s'] * 8 - reported[0][0]) < 1.0 and
          abs(rx[0]['bytes_per_s'] - reported[0][0]) > 100000,
          'the reported value matches bits/s, not bytes/s')


# --- statuses ----------------------------------------------------------------

def test_counters_missing_baseline_and_values():
    print('test_counters_missing_baseline_and_values')
    r = C.byte_rate(None, A1(), 'rx_bytes')
    check(r['status'] == 'missing_baseline' and r['delta'] is None and
          r['bytes_per_s'] is None and r['bits_per_s'] is None and
          r['mbps'] is None, 'no previous record -> missing_baseline')
    check(C.byte_rate(A0(), None, 'rx_bytes')['status'] == 'missing_value',
          'no current record -> missing_value')
    for label, bad in (('None', None), ('absent', 'ABSENT'), ('bool', True),
                       ('str', '12'), ('nan', float('nan')),
                       ('inf', float('inf')), ('negative', -1)):
        cur = A1()
        if bad == 'ABSENT':
            del cur['rx_bytes']
        else:
            cur['rx_bytes'] = bad
        r = C.byte_rate(A0(), cur, 'rx_bytes')
        check(r['status'] == 'missing_value' and r['delta'] is None,
              'current rx_bytes {} -> missing_value'.format(label))
        base = A0()
        base['rx_bytes'] = None
        r = C.byte_rate(base, A1(), 'rx_bytes')
        check(r['status'] == 'missing_value',
              'baseline rx_bytes None -> missing_value')
    cur = A1()
    cur['tx_bytes'] = None
    check(C.byte_rate(A0(), cur, 'rx_bytes')['status'] == 'ok',
          'a missing OTHER counter does not affect this one')
    check(C.counter_value({'rx_bytes': 0}, 'rx_bytes') == 0,
          'zero is a value')


def test_counters_non_monotonic_and_invalid_time():
    print('test_counters_non_monotonic_and_invalid_time')
    same = _rec(BASE + 0.25, BASE, 1131250)
    r = C.byte_rate(A0(), same, 'rx_bytes')
    check(r['status'] == 'non_monotonic_time' and r['interval_s'] == 0.0 and
          r['delta'] is None and r['bytes_per_s'] is None,
          'equal times -> non_monotonic_time, no division by zero')
    early = _rec(BASE - 4.75, BASE - 5, 1131250)
    r = C.byte_rate(A0(), early, 'rx_bytes')
    check(r['status'] == 'non_monotonic_time' and r['interval_s'] == -5.0 and
          r['delta'] is None, 'time going backwards -> non_monotonic_time '
          '(interval -5.0 reported, no delta)')
    notime = _rec(None, None, 1131250)
    check(C.byte_rate(A0(), notime, 'rx_bytes')['status'] == 'invalid_time',
          'no timestamps -> invalid_time')
    zero_t = _rec(0, 0, 1131250)
    check(C.byte_rate(A0(), zero_t, 'rx_bytes')['status'] == 'invalid_time',
          'non-positive timestamps are invalid')
    bool_t = _rec(True, True, 1131250)
    check(C.byte_rate(A0(), bool_t, 'rx_bytes')['status'] == 'invalid_time',
          'boolean timestamps are invalid')


def test_counters_decrease_has_no_delta_and_no_wrap_correction():
    print('test_counters_decrease_has_no_delta_and_no_wrap_correction')
    # 4 294 967 290 -> 100 looks like a 32-bit wrap (+106 B). The module
    # must NOT assume a width: it reports a decrease and no delta.
    hi = _rec(BASE + 0.25, BASE, 4294967290, 0)
    lo = _rec(BASE + 10.75, BASE + 10, 100, 0)
    r = C.byte_rate(hi, lo, 'rx_bytes')
    check(r['status'] == 'counter_decrease' and r['delta'] is None and
          r['bytes_per_s'] is None and r['bits_per_s'] is None and
          r['mbps'] is None, 'decrease -> counter_decrease, no delta, no '
          'wrap guess')
    check(r['interval_s'] == 10.5, 'interval still reported on a decrease')
    r64 = C.byte_rate(_rec(BASE + 0.25, BASE, 2 ** 64 - 1), lo, 'rx_bytes')
    check(r64['status'] == 'counter_decrease', 'same for a 64-bit-sized value')
    exact = _rec(BASE + 10.75, BASE + 10, 1000000)
    r = C.counter_delta(A0(), exact, 'rx_bytes')
    check(r['status'] == 'ok' and r['delta'] == 0,
          'equal counter value is NOT a decrease')


def test_counters_identity_mismatch():
    print('test_counters_identity_mismatch')
    other = _rec(BASE + 10.75, BASE + 10, 1131250, uid='sdwan-hub0_wan',
                 cls='sdwan')
    r = C.byte_rate(A0(), other, 'rx_bytes')
    check(r['status'] == 'identity_mismatch' and r['delta'] is None,
          'different uid -> identity_mismatch')
    same_uid_other_class = _rec(BASE + 10.75, BASE + 10, 1131250,
                                cls='overlay')
    check(C.byte_rate(A0(), same_uid_other_class,
                      'rx_bytes')['status'] == 'identity_mismatch',
          'same uid, different object_class -> identity_mismatch')
    check(C.byte_rate(A0(), A1(), 'rx_bytes')['status'] == 'ok',
          'same identity is fine')


def test_counters_gap_policy_is_explicit_caller_policy():
    print('test_counters_gap_policy_is_explicit_caller_policy')
    far = _rec(BASE + 900.25, BASE + 900, 1900000, 800000)
    # 900 s apart, 900 000 B -> 1 000 B/s = 8 000 bit/s = 0.008 Mbps
    r = C.byte_rate(A0(), far, 'rx_bytes')
    check(r['status'] == 'ok' and r['delta'] == 900000 and
          r['interval_s'] == 900.0 and r['bytes_per_s'] == 1000.0 and
          r['bits_per_s'] == 8000.0 and r['mbps'] == 0.008,
          'no policy given -> gaps are not judged (900 s accepted)')
    r = C.byte_rate(A0(), far, 'rx_bytes', max_gap_s=60)
    check(r['status'] == 'gap_exceeded' and r['delta'] == 900000 and
          r['interval_s'] == 900.0 and r['bytes_per_s'] is None and
          r['bits_per_s'] is None and r['mbps'] is None,
          'interval > policy -> gap_exceeded; delta kept, no rate')
    r = C.byte_rate(A0(), far, 'rx_bytes', max_gap_s=900)
    check(r['status'] == 'ok', 'interval == policy is accepted (strict >)')
    r = C.byte_rate(A0(), far, 'rx_bytes', max_gap_s=900.5)
    check(r['status'] == 'ok' and r['bits_per_s'] == 8000.0,
          'interval < policy accepted')
    for bad in (0, -1, True, 'x', float('nan'), float('inf')):
        try:
            C.byte_rate(A0(), A1(), 'rx_bytes', max_gap_s=bad)
            ok = False
        except ValueError:
            ok = True
        check(ok, 'invalid max_gap_s {!r} raises ValueError'.format(bad))
    check(not hasattr(C, 'DEFAULT_MAX_GAP_S') and
          not hasattr(C, 'MAX_GAP_S'),
          'the module defines no default gap threshold')
    import inspect
    sig = inspect.signature(C.byte_rate)
    check(sig.parameters['max_gap_s'].default is None,
          'max_gap_s defaults to None (no judgement)')


def test_counters_field_validation():
    print('test_counters_field_validation')
    for fn in (C.counter_delta, C.byte_rate):
        try:
            fn(A0(), A1(), 'bogus')
            ok = False
        except ValueError:
            ok = True
        check(ok, '{} rejects an unknown field'.format(fn.__name__))
    try:
        C.byte_rate(A0(), A1(), 'rx_packets')
        ok = False
    except ValueError:
        ok = True
    check(ok, 'byte_rate refuses non-byte counters (no bit conversion of '
          'packets)')
    p = C.counter_delta(A0(), A1(), 'rx_packets')
    check('bits_per_s' not in p and 'mbps' not in p,
          'packet counters get no bit/Mbps keys')


# --- series / pairing ---------------------------------------------------------

def test_counters_series_pairing_rules():
    print('test_counters_series_pairing_rules')
    # eth series: 5 records, expected statuses by hand:
    #  S0->S1 ok (delta 131 250, 10.5 s)
    #  S1->S2 counter_decrease (1 131 250 -> 1 000)
    #  S2->S3 ok  (1 000 -> 3 000 = 2 000 B over 10 s = 200 B/s = 1 600 bit/s)
    #  S3->S4 missing_value (S4 rx_bytes None), S4->S5 missing_value
    #  (baseline missing: NOT bridged over to S3)
    s = [A0(), A1(),
         _rec(BASE + 20.75, BASE + 20, 1000),
         _rec(BASE + 30.75, BASE + 30, 3000),
         _rec(BASE + 40.75, BASE + 40, None),
         _rec(BASE + 50.75, BASE + 50, 9000)]
    res = C.series_byte_rates(s, 'rx_bytes')
    check([r['status'] for r in res] == [
        'ok', 'counter_decrease', 'ok', 'missing_value', 'missing_value'],
        'consecutive-pair statuses as hand-derived')
    check(res[2]['delta'] == 2000 and res[2]['interval_s'] == 10.0 and
          res[2]['bytes_per_s'] == 200.0 and res[2]['bits_per_s'] == 1600.0
          and res[2]['mbps'] == 0.0016,
          'after a decrease the lower value is the new baseline: 200 B/s')
    check(len(C.series_byte_rates([], 'rx_bytes')) == 0 and
          len(C.series_byte_rates([A0()], 'rx_bytes')) == 0 and
          C.series_deltas(None, 'rx_bytes') == [],
          'fewer than two records -> no results')
    out_of_order = C.series_deltas([A1(), A0()], 'rx_bytes')
    check(out_of_order[0]['status'] == 'non_monotonic_time',
          'order is the caller\'s; no silent re-sorting')
    mixed = C.series_deltas([A0(), _rec(BASE + 10.75, BASE + 10, 1,
                                        uid='mdm-1ea0ac'), A1()], 'rx_bytes')
    check([m['status'] for m in mixed] == ['identity_mismatch'] * 2,
          'a series mixing interfaces is flagged at every boundary')


def test_counters_interface_rates_all_fields_no_cross_class_sum():
    print('test_counters_interface_rates_all_fields_no_cross_class_sum')
    out = C.interface_rates(A0(), A1())
    check(set(out['counters']) == set(C.COUNTER_FIELDS) and
          out['uid'] == 'ethernet-wan' and out['object_class'] == 'physical',
          'one result per counter, for one interface')
    check(out['counters']['rx_bytes']['mbps'] == 0.1 and
          'mbps' not in out['counters']['rx_packets'],
          'byte fields have Mbps; others do not')
    public = [n for n in dir(C) if not n.startswith('_')]
    check(not any(w in n.lower() for n in public
                  for w in ('sum', 'total', 'aggregate', 'combine')),
          'module offers no summation/aggregation across interfaces')


def test_counters_module_is_pure():
    print('test_counters_module_is_pure')
    import analyzer.counters as mod
    src = open(mod.__file__).read()
    imports = sorted({ln.split()[1].split('.')[0] for ln in src.splitlines()
                      if ln.startswith(('import ', 'from '))})
    check(imports == ['math'],
          'only imports math (no cp, io, storage, service, web): '
          '{}'.format(imports))
    big = C.byte_rate(_rec(BASE + 0.25, BASE, 2 ** 40),
                      _rec(BASE + 10.25, BASE + 10, 2 ** 40 + 10 ** 9),
                      'rx_bytes')
    check(big['status'] == 'ok' and big['delta'] == 10 ** 9 and
          big['bytes_per_s'] == 10 ** 8 and big['mbps'] == 800.0,
          'large counters: 10^9 B / 10 s = 10^8 B/s = 800 Mbps')
    check(math.isfinite(big['mbps']), 'finite result')


TESTS = [
    test_counters_hand_calculated_fractional,
    test_counters_legacy_integer_second_precision_flagged,
    test_counters_fractional_precision_is_never_fabricated,
    test_counters_zero_baseline_and_zero_delta,
    test_counters_reproduce_live_ibps_obps,
    test_counters_missing_baseline_and_values,
    test_counters_non_monotonic_and_invalid_time,
    test_counters_decrease_has_no_delta_and_no_wrap_correction,
    test_counters_identity_mismatch,
    test_counters_gap_policy_is_explicit_caller_policy,
    test_counters_field_validation,
    test_counters_series_pairing_rules,
    test_counters_interface_rates_all_fields_no_cross_class_sum,
    test_counters_module_is_pure,
]
