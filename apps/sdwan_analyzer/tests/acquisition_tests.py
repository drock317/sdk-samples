"""v0.5.0: acquisition timing (`collected_at`) and failed-read behavior.

Covers, with no router and no network:

* collectors: `acquired_at` is captured right after each successful read
  (not at the end of a pass) and is None for failed or derived results;
* telemetry/service: `collected_at` is propagated to normalized records, the
  observation and the current state; it is never fabricated; it is not the
  completion time of the whole collection pass;
* failed `wan_devices` read: no newly timestamped wan/interfaces/cellular in
  the observation, `_prev_counters` not advanced, last-known-good current
  state and section freshness preserved, then clean recovery - in memory and
  in persisted (durable) observations;
* persistence: `collected_at` survives encode/decode, legacy records without
  it stay readable, and mixed legacy/new files feed counters.py correctly.

Run through run_tests.py (shared `check` and harness helpers).
"""
import copy
import json
import os
import shutil
import tempfile

import cp
from analyzer import collectors, counters, persistence as P, telemetry
from analyzer.service import AnalyzerService
import fixtures

check = None            # bound by run_tests.main()
_Clock = None
_tel_devices = None

T0 = 1790380800


def bind(check_fn, clock_cls, tel_devices_fn):
    global check, _Clock, _tel_devices
    check = check_fn
    _Clock = clock_cls
    _tel_devices = tel_devices_fn


class _Env(object):
    """Service + fake collectors that can supply acquired_at and fail.

    `acq_offset`: acquired_at = clock.t + acq_offset (None = not supplied,
    like a legacy collector). `pass_s`: seconds the clock advances after the
    reads, simulating the remainder of a slow collection pass.
    """

    def __init__(self, base=None, acq_offset=0.4, pass_s=4):
        self.clock = _Clock(T0)
        if base is None:
            self.svc = AnalyzerService(clock=self.clock)
        else:
            self.svc = AnalyzerService(clock=self.clock, app_root=base,
                                       telemetry_root='tmp/telemetry')
        self.overrides = {}
        self.fail = set()
        self.acq_offset = acq_offset
        self.pass_s = pass_s
        self.acquired = []
        self.calls = []          # keys of every collection pass

    def __enter__(self):
        self._saved = collectors.collect_keys
        data = dict(fixtures.base_raw())
        data.update(fixtures.heavy_raw())

        def fake(keys):
            self.calls.append(tuple(keys))
            data.update(self.overrides)
            out = {}
            acq = (None if self.acq_offset is None
                   else self.clock.t + self.acq_offset)
            for k in tuple(keys):
                if k in self.fail:
                    out[k] = collectors.CollectResult(
                        False, error='boom', error_type='api_error')
                else:
                    out[k] = collectors.CollectResult(
                        True, value=data.get(k), acquired_at=acq)
            self.acquired.append(acq)
            self.clock.t += self.pass_s
            return out
        collectors.collect_keys = fake
        return self

    def __exit__(self, *exc):
        collectors.collect_keys = self._saved
        try:
            self.svc.shutdown()
        except Exception:
            pass


class _Tmp(object):
    def __enter__(self):
        self.base = tempfile.mkdtemp(prefix='sdwan_acq_')
        return self.base

    def __exit__(self, *exc):
        shutil.rmtree(self.base, ignore_errors=True)


def _iface(records, uid='ethernet-wan'):
    return [r for r in records if r['uid'] == uid][0]


# --- collectors: acquired_at ---------------------------------------------

class _StubTime(object):
    def __init__(self, t):
        self.t = t
        self.calls = 0

    def time(self):
        self.calls += 1
        return self.t


class _patched_cp_get(object):
    """cp.get stub that advances a stub clock while 'reading'."""

    def __init__(self, stub, step, fail=()):
        self.stub, self.step, self.fail = stub, step, set(fail)

    def __enter__(self):
        self._cp_get = cp.get
        self._time = collectors.time
        collectors.time = self.stub

        def fake_get(path):
            self.stub.t += self.step     # the read itself takes time
            if path in self.fail:
                raise RuntimeError('api down')
            return {'path': path}
        cp.get = fake_get
        return self

    def __exit__(self, *exc):
        cp.get = self._cp_get
        collectors.time = self._time


def test_collector_acquired_at_captured_after_read():
    print('test_collector_acquired_at_captured_after_read')
    stub = _StubTime(1000.0)
    with _patched_cp_get(stub, 0.5):
        res = collectors._get_result('status/wan/devices')
    check(res.ok and res.acquired_at == 1000.5 and
          isinstance(res.acquired_at, float),
          'acquired_at is the clock AFTER the read returned (1000.5, not '
          '1000.0)')
    check(stub.calls == 1, 'clock read exactly once per successful read')
    stub = _StubTime(1000.0)
    with _patched_cp_get(stub, 0.5, fail=('status/wan/devices',)):
        bad = collectors._get_result('status/wan/devices')
    check(not bad.ok and bad.acquired_at is None and stub.calls == 0,
          'failed read has no acquired_at and never samples the clock')
    check(collectors.CollectResult(True, value=1).acquired_at is None,
          'results built without a real read carry no acquired_at')


def test_collect_keys_acquired_at_is_per_read():
    print('test_collect_keys_acquired_at_is_per_read')
    stub = _StubTime(2000.0)
    with _patched_cp_get(stub, 0.25):
        out = collectors.collect_keys(('wan_devices', 'primary_device'))
    a, b = out['wan_devices'].acquired_at, out['primary_device'].acquired_at
    check(a == 2000.25 and b == 2000.5,
          'each read has its own acquired_at ({} then {}), not a shared '
          'end-of-pass time'.format(a, b))


# --- telemetry: collected_at on records ------------------------------------

def test_interface_records_collected_at():
    print('test_interface_records_collected_at')
    devs = _tel_devices(1)
    legacy, _ = telemetry.interface_records(devs, {}, T0)
    check(all('collected_at' not in r for r in legacy),
          'no acquired_at -> no collected_at (precision not invented)')
    recs, _ = telemetry.interface_records(devs, {}, T0, acquired_at=T0 + 0.25)
    check(all(r['collected_at'] == T0 + 0.25 for r in recs) and
          all(isinstance(r['collected_at'], float) for r in recs),
          'every record carries the fractional acquisition time')
    check(all(r['collected_ts'] == T0 and r['metric_ts'] == T0 and
              r['timestamp_source'] == 'collector' for r in recs),
          'collected_ts / metric_ts / timestamp_source unchanged')
    stripped = [{k: v for k, v in r.items() if k != 'collected_at'}
                for r in recs]
    check(stripped == legacy, 'otherwise identical to legacy records')
    for bad in (None, True, False, 0, -5, float('nan'), float('inf'), 'x',
                [1.0]):
        r, _ = telemetry.interface_records(devs, {}, T0, acquired_at=bad)
        check(all('collected_at' not in x for x in r),
              'invalid acquired_at {!r} is ignored'.format(bad))


# --- service: propagation, not pass-end ------------------------------------

def test_service_collected_at_is_acquisition_not_pass_end():
    print('test_service_collected_at_is_acquisition_not_pass_end')
    with _Env(acq_offset=0.3, pass_s=4) as env:
        env.overrides['wan_devices'] = _tel_devices(1)
        obs = env.svc.collect_startup()
        e = _iface(obs['interfaces'])
        check(e['collected_at'] == T0 + 0.3,
              'collected_at is the acquisition time (T0+0.3)')
        check(e['collected_ts'] == T0 + 4 == obs['collected_ts'],
              'collected_ts remains the integer end-of-pass time (T0+4)')
        check(e['collected_at'] < e['collected_ts'] and
              e['collected_at'] != env.clock.t,
              'collected_at is not the completion time of the pass')
        snap = env.svc.snapshot()
        cur = _iface(snap['wan']['interfaces'])
        check(cur['collected_at'] == T0 + 0.3 and
              cur['collected_ts'] == T0 + 4,
              'current-state API carries both times (additive field)')
        for k in ('uid', 'kind', 'object_class', 'connection_state',
                  'rx_bytes', 'tx_bytes', 'reset_suspected', 'metric_ts',
                  'collected_ts', 'timestamp_source'):
            check(k in cur, 'existing field still present: {}'.format(k))
        hist = env.svc.history(5)
        check(_iface(hist[-1]['interfaces'])['collected_at'] == T0 + 0.3,
              'stored observation carries collected_at')
        check(all('collected_at' not in m for m in obs['cellular']),
              'cellular records unchanged (no collected_at)')


def test_service_no_fabricated_precision():
    print('test_service_no_fabricated_precision')
    with _Env(acq_offset=None) as env:
        env.overrides['wan_devices'] = _tel_devices(1)
        obs = env.svc.collect_startup()
        check(obs['interfaces'] and all(
            'collected_at' not in r for r in obs['interfaces']),
            'legacy-style collector result -> no collected_at in observation')
        check(all('collected_at' not in r for r in
                  env.svc.snapshot()['wan']['interfaces']),
              'and none in the current state')
        check(all(isinstance(r['collected_ts'], int)
                  for r in obs['interfaces']),
              'integer-second collected_ts still present')


# --- failed read: success -> failure -> recovery ---------------------------

def _failure_sequence(env):
    """startup ok -> manual fail -> manual recovery. Returns the 3 obs."""
    env.overrides['wan_devices'] = _tel_devices(5)
    ok1 = env.svc.collect_startup()
    snap1 = copy.deepcopy(env.svc.snapshot())
    # Make the baseline distinguishable so "not advanced" is observable:
    # a failed pass that re-parsed last-known-good data would overwrite it.
    env.svc._prev_counters['ethernet-wan'] = {
        f: 0 for f in env.svc._prev_counters['ethernet-wan']}
    prev_before = copy.deepcopy(env.svc._prev_counters)
    env.fail.add('wan_devices')
    env.clock.t += 30
    bad = env.svc.refresh('wan_paths')
    bad_obs = env.svc.history(1)[-1]
    snap_bad = copy.deepcopy(env.svc.snapshot())
    prev_after = copy.deepcopy(env.svc._prev_counters)
    env.fail.discard('wan_devices')
    env.overrides['wan_devices'] = _tel_devices(1)       # counters decreased
    env.clock.t += 30
    good = env.svc.refresh('wan_paths')
    rec_obs = env.svc.history(1)[-1]
    snap_rec = copy.deepcopy(env.svc.snapshot())
    return {'ok1': ok1, 'snap1': snap1, 'bad': bad, 'bad_obs': bad_obs,
            'snap_bad': snap_bad, 'prev_before': prev_before,
            'prev_after': prev_after, 'good': good, 'rec_obs': rec_obs,
            'snap_rec': snap_rec}


def test_failed_read_emits_no_new_measurements():
    print('test_failed_read_emits_no_new_measurements')
    with _Env() as env:
        s = _failure_sequence(env)
        bad = s['bad_obs']
        check(any(e['collector'] == 'wan_devices'
                  for e in bad['collector_errors']),
              'the failure is recorded in collector_errors')
        check('interfaces' not in bad and 'cellular' not in bad and
              'wan' not in bad,
              'failed read: no wan / interfaces / cellular in the '
              'observation (no re-stamped last-known-good data)')
        check(bad['collected_ts'] > s['ok1']['collected_ts'],
              'the failed pass itself is still timestamped')
        check(s['prev_after'] == s['prev_before'] and
              s['prev_after']['ethernet-wan']['rx_bytes'] == 0,
              '_prev_counters not advanced by the failed read')


def test_failed_read_preserves_current_state_and_freshness():
    print('test_failed_read_preserves_current_state_and_freshness')
    with _Env() as env:
        s = _failure_sequence(env)
        before, after = s['snap1'], s['snap_bad']
        check(after['wan']['interfaces'] == before['wan']['interfaces'],
              'current interface telemetry = last-known-good (incl. '
              'collected_at and collected_ts)')
        check(after['cellular'] == before['cellular'],
              'current cellular telemetry = last-known-good')
        check(after['section_freshness']['wan'] ==
              before['section_freshness']['wan'] and
              after['section_freshness']['cellular'] ==
              before['section_freshness']['cellular'],
              'section freshness not advanced by the failure')
        check(_iface(after['wan']['interfaces'])['rx_bytes'] == 5000,
              'last-known-good values still served')


def test_recovery_after_failed_read():
    print('test_recovery_after_failed_read')
    with _Env() as env:
        s = _failure_sequence(env)
        rec = s['rec_obs']
        e = _iface(rec['interfaces'])
        check(e['rx_bytes'] == 1000 and e['collected_ts'] ==
              s['bad_obs']['collected_ts'] + 34,
              'recovery observation has fresh counters and timestamp')
        # The test planted an all-zero baseline before the failure. Recovery
        # counters (scale 1) are lower than the stale scale-5 values a
        # failed pass would have written as the new baseline, so no reset is
        # suspected only if the baseline survived the failure untouched.
        check(e['reset_suspected'] is False,
              'baseline used at recovery is the pre-failure one (zeros)')
        check('wan' in rec and 'cellular' in rec,
              'recovery observation has wan/interfaces/cellular again')
        fr = s['snap_rec']['section_freshness']['wan']
        check(fr['collected_ts'] == e['collected_ts'],
              'section freshness advances again after recovery')
        check(_iface(s['snap_rec']['wan']['interfaces'])['rx_bytes'] == 1000,
              'current state updated after recovery')
        check(env.svc._prev_counters['ethernet-wan']['rx_bytes'] == 1000,
              'baseline advances normally after the successful read')


def test_failed_read_persisted_observations():
    print('test_failed_read_persisted_observations')
    with _Tmp() as base:
        with _Env(base=base) as env:
            _failure_sequence(env)
            page = env.svc.query_history('limit=10')
            samples = page['samples']
            check(len(samples) == 3, 'three persisted observations')
            ok1, bad, rec = samples
            check('interfaces' in ok1 and 'interfaces' in rec,
                  'successful reads persisted interface records')
            check('interfaces' not in bad and 'cellular' not in bad and
                  'wan' not in bad and bad['collector_errors'],
                  'persisted failed observation has no re-stamped '
                  'measurements and records the collector error')
            check(_iface(ok1['interfaces'])['collected_at'] == T0 + 0.4 and
                  _iface(rec['interfaces'])['collected_at'] ==
                  bad['collected_ts'] + 34 - 4 + 0.4,
                  'collected_at persisted and read back for both successes')
            ts_vals = [r['collected_ts'] for r in samples]
            check(ts_vals == sorted(ts_vals), 'observations in time order')
            hist_dir = os.path.join(base, 'tmp', 'telemetry', 'history')
            raw = ''
            for name in os.listdir(hist_dir):
                with open(os.path.join(hist_dir, name)) as f:
                    raw += f.read()
            n = len(ok1['interfaces'])
            check(n > 0 and raw.count('"collected_at"') == 2 * n,
                  'collected_at appears once per interface record on disk, '
                  'only for the two successful reads ({} each)'.format(n))


# --- persistence compatibility ---------------------------------------------

def _obs_with(interfaces, ts):
    return {'sample_source': 'scheduled', 'job_id': None,
            'refresh_group': None, 'timestamp': ts, 'collected_ts': ts,
            'collector_errors': [], 'duration_ms': 1,
            'interfaces': interfaces}


def test_persistence_roundtrip_with_and_without_collected_at():
    print('test_persistence_roundtrip_with_and_without_collected_at')
    new, _ = telemetry.interface_records(_tel_devices(1), {}, T0,
                                         acquired_at=T0 - 0.375)
    old, _ = telemetry.interface_records(_tel_devices(1), {}, T0)
    enc_new = P.encode_observation(_obs_with(new, T0))
    enc_old = P.encode_observation(_obs_with(old, T0))
    check(all(r['collected_at'] == T0 - 0.375
              for r in enc_new['interfaces']),
          'collected_at kept in the compact persisted form')
    check(all('collected_at' not in r for r in enc_old['interfaces']),
          'legacy persisted form unchanged (no new key)')
    check(all('collected_ts' not in r and 'metric_ts' not in r
              for r in enc_new['interfaces']),
          'envelope hoisting still applies')
    dec_new = P.decode_observation(json.loads(json.dumps(enc_new)))
    dec_old = P.decode_observation(json.loads(json.dumps(enc_old)))
    check(dec_new['interfaces'] == new, 'new record round-trips exactly')
    check(dec_old['interfaces'] == old, 'legacy record round-trips exactly')
    check(all('collected_at' not in r for r in dec_old['interfaces']),
          'decoding a legacy record does not add collected_at')


def test_mixed_legacy_and_new_history_feeds_counters():
    print('test_mixed_legacy_and_new_history_feeds_counters')
    with _Tmp() as base:
        with _Env(base=base, acq_offset=None, pass_s=10) as env:
            env.overrides['wan_devices'] = _tel_devices(1)
            env.svc.collect_startup()                 # legacy-shaped record
            env.acq_offset = 0.5                      # after an "upgrade"
            env.overrides['wan_devices'] = _tel_devices(2)
            env.svc.refresh('wan_paths')
            env.overrides['wan_devices'] = _tel_devices(4)
            env.svc.refresh('wan_paths')
            page = env.svc.query_history('limit=10')
            recs = [_iface(s['interfaces']) for s in page['samples']
                    if 'interfaces' in s]
            check(len(recs) == 3 and 'collected_at' not in recs[0] and
                  'collected_at' in recs[1] and 'collected_at' in recs[2],
                  'one history stream holds legacy and new records, all '
                  'readable')
            res = counters.series_byte_rates(recs, 'rx_bytes')
            # pass_s = 10 and the reads sit 0.5 s into each pass, so the
            # acquisition-time interval of the two new records is exactly 10
            check(res[0]['status'] == 'ok' and
                  res[0]['reduced_precision'] is True and
                  res[0]['time_source'] == 'collected_ts',
                  'legacy->new pair uses integer timestamps and says so')
            check(res[1]['status'] == 'ok' and
                  res[1]['reduced_precision'] is False and
                  res[1]['time_source'] == 'collected_at',
                  'new->new pair uses fractional collected_at')
            check(res[1]['delta'] == 2000 and res[1]['interval_s'] == 10.0 and
                  res[1]['bytes_per_s'] == 200.0 and
                  res[1]['bits_per_s'] == 1600.0,
                  'rate from persisted records matches hand calculation '
                  '(2000 B / 10 s = 200 B/s = 1600 bit/s)')


TESTS = [
    test_collector_acquired_at_captured_after_read,
    test_collect_keys_acquired_at_is_per_read,
    test_interface_records_collected_at,
    test_service_collected_at_is_acquisition_not_pass_end,
    test_service_no_fabricated_precision,
    test_failed_read_emits_no_new_measurements,
    test_failed_read_preserves_current_state_and_freshness,
    test_recovery_after_failed_read,
    test_failed_read_persisted_observations,
    test_persistence_roundtrip_with_and_without_collected_at,
    test_mixed_legacy_and_new_history_feeds_counters,
]
