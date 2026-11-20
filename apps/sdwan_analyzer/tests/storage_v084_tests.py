"""v0.8.4 Phase B: history policy, periodic retention, low-disk protection,
coverage and verified query seek (including a 256 MiB history).
Uses TelemetryStorage directly with an injected clock and free-space probe.
"""
import json
import os
import shutil
import tempfile
import time

from analyzer import durable, persistence as P, retention as RT, telemetry

check = None
MIB = 1048576
DAY = 86400
T0 = 1790380800            # 2026-09-26 00:00:00 UTC (a day boundary)


def bind(check_fn):
    global check
    check = check_fn


class _Clock(object):
    def __init__(self, t=T0 + 3600):
        self.t = t

    def __call__(self):
        return self.t


class _Free(object):
    """Injectable free-space probe."""

    def __init__(self, free, total=8 * 1024 * MIB):
        self.free, self.total = free, total

    def __call__(self):
        return {'available': True, 'free_bytes': self.free,
                'total_bytes': self.total}


class _Tmp(object):
    def __enter__(self):
        self.base = tempfile.mkdtemp(prefix='sdwan_v084_')
        return self.base

    def __exit__(self, *exc):
        shutil.rmtree(self.base, ignore_errors=True)


def _mk(base, clock, retention=None, free=None):
    tel = durable.TelemetryStorage(
        os.path.join(base, 'tmp', 'telemetry'), clock=clock,
        retention=retention, free_space_fn=free)
    assert tel.prepare()
    tel.recover(telemetry.BoundedSeen(), telemetry.BoundedSeen())
    return tel


def _obs(ts, pad=0):
    o = {'sample_source': 'scheduled', 'job_id': None, 'refresh_group': None,
         'timestamp': ts, 'collected_ts': ts, 'collector_errors': [],
         'duration_ms': 1}
    if pad:
        o['pad'] = 'x' * pad
    return o


def _day_name(ts):
    return durable.utc_day(ts)


def _write_day(tel, day_ts, count, step=60, pad=0, junk=None):
    """Write `count` observations of one UTC day straight to its file."""
    path = tel.history_store.day_path(_day_name(day_ts))
    with open(path, 'ab') as f:
        for i in range(count):
            f.write(P.dumps(P.encode_observation(
                _obs(day_ts + i * step, pad))).encode('utf-8') + b'\n')
        if junk:
            for line in junk:
                f.write(line)
    return path


def _events(tel, kind):
    return [r for r in P.read_tail_records(tel.events_path, 1 << 20)
            if r.get('event') == kind]


def _query_all(store, start=None, end=None, limit=250):
    """Page through a query; -> (timestamps, scanned, seek_skipped)."""
    out, scanned, seeked, cur = [], 0, 0, None
    for _ in range(2000):
        res = store.query(start=start, end=end, limit=limit, cursor=cur)
        out.extend(s['collected_ts'] for s in res['samples'])
        scanned += res['scanned_bytes']
        seeked += res.get('seek_skipped_bytes', 0)
        cur = res['next_cursor']
        if cur is None:
            break
    return out, scanned, seeked


# --- policy ------------------------------------------------------------------

def test_v084_policy_defaults_and_bounds():
    print('test_v084_policy_defaults_and_bounds')
    lim, rep = RT.normalize_history_policy(None, None)
    check(lim == {'history_max_bytes': 256 * MIB,
                  'history_max_age_s': 7 * DAY} and
          rep['history_max_mb']['source'] == 'default' and
          rep['history_max_days']['source'] == 'default',
          'defaults: 256 MiB / 7 days')
    lim, rep = RT.normalize_history_policy('512', '14')
    check(lim['history_max_bytes'] == 512 * MIB and
          lim['history_max_age_s'] == 14 * DAY and
          rep['history_max_mb']['source'] == 'appdata', 'valid App Data')
    lim, rep = RT.normalize_history_policy('4096', '90')
    check(lim['history_max_bytes'] == 1024 * MIB and
          lim['history_max_age_s'] == 30 * DAY and
          rep['history_max_mb']['source'] == 'clamped' and
          rep['history_max_mb']['requested'] == '4096',
          'above the bounds is clamped and reported')
    lim, rep = RT.normalize_history_policy('1', '0')
    check(lim['history_max_bytes'] == 32 * MIB and
          lim['history_max_age_s'] == 1 * DAY, 'below the bounds is clamped')
    for bad in ('abc', '-5', '12x', '1e3', 'NaN', '  '):
        lim, rep = RT.normalize_history_policy(bad, bad)
        check(lim['history_max_bytes'] == 256 * MIB and
              lim['history_max_age_s'] == 7 * DAY,
              'invalid {!r} falls back to defaults'.format(bad))
    check(RT.normalize_history_policy('x', None)[1]['history_max_mb'][
        'source'] == 'invalid_using_default', 'invalid value reported')
    check(durable.DEFAULT_RETENTION['recordings_max_bytes'] == 32 * MIB and
          durable.DEFAULT_RETENTION['recordings_max_jobs'] == 10,
          'Recording retention is independent of the history policy')


def test_v084_policy_reported_in_storage():
    print('test_v084_policy_reported_in_storage')
    from analyzer.service import AnalyzerService
    with _Tmp() as base:
        svc = AnalyzerService(clock=_Clock(), app_root=base,
                              telemetry_root='tmp/telemetry',
                              history_policy={'max_mb': '64',
                                              'max_days': '3'})
        pol = svc.storage_report()['persistent']['history_policy']
        check(pol['history_max_bytes'] == 64 * MIB and
              pol['history_max_days'] == 3.0 and
              pol['effective_history_cap_bytes'] == 64 * MIB and
              pol['recordings_independent'] is True and
              pol['config']['history_max_mb']['source'] == 'appdata' and
              pol['bounds']['history_max_mb'] == [32, 1024],
              '/api/storage reports the effective policy and its source')
        rep = svc.storage_report()['persistent']
        for k in ('space_guard', 'history_coverage', 'retention_events'):
            check(k in rep, '/api/storage carries {}'.format(k))


# --- periodic retention ------------------------------------------------------

def test_v084_periodic_sweep_age_and_size():
    print('test_v084_periodic_sweep_age_and_size')
    with _Tmp() as base:
        clk = _Clock(T0 + 10 * DAY + 600)
        tel = _mk(base, clk, retention={'history_max_age_s': 3 * DAY,
                                        'history_max_bytes': 10 ** 9})
        for d in range(0, 11):
            _write_day(tel, T0 + d * DAY, 3)
        names = tel.history_store.day_files()
        check(len(names) == 11, 'eleven day files before the sweep')
        tel.maintenance(clk.t)
        left = [n[:-6] for n in tel.history_store.day_files()]
        # Whole days: a day is deleted only when it ENDS before now - 3 d.
        check(left[0] >= _day_name(T0 + 7 * DAY) and
              left[-1] == _day_name(T0 + 10 * DAY),
              'periodic sweep removed whole days older than the age limit')
        check(tel.last_retention['reason'] == 'periodic', 'reason recorded')
        n_events = len(_events(tel, 'retention_delete'))
        check(n_events >= 7 and all(
            e['reason'] == 'age' for e in _events(tel, 'retention_delete')),
            'one retention_delete event per removed day')
        before = tel.last_retention['ts']
        clk.t += 600
        tel.maintenance(clk.t)
        check(tel.last_retention['ts'] == before,
              'no second sweep within the hourly interval')
        clk.t += 3600
        tel.maintenance(clk.t)
        check(tel.last_retention['ts'] == int(clk.t),
              'next sweep runs after an hour')
        # Size: protected current day survives even when over the cap.
        tel.limits['history_max_bytes'] = 1
        tel.effective_cap['effective'] = 1
        tel.retention_sweep('test')
        left = tel.history_store.day_files()
        check(len(left) == 1 and left[0][:-6] == _day_name(clk.t),
              'size cap deletes oldest whole days, never the current day')


def test_v084_retention_log_and_coverage():
    print('test_v084_retention_log_and_coverage')
    with _Tmp() as base:
        clk = _Clock(T0 + 5 * DAY + 7200)
        tel = _mk(base, clk, retention={'history_max_age_s': 30 * DAY})
        for d in (0, 1, 3, 5):                    # days 2 and 4 missing
            _write_day(tel, T0 + d * DAY, 5)
        cov = tel.history_store.coverage(clk.t)
        check(cov['days_present'] == 4 and cov['oldest_ts'] == T0 and
              cov['newest_ts'] == T0 + 5 * DAY + 4 * 60,
              'oldest/newest observations reported')
        check(cov['missing_days'] == [_day_name(T0 + 2 * DAY),
                                      _day_name(T0 + 4 * DAY)],
              'missing calendar days between retained files are listed')
        check(cov['limiting_factor'] == 'none' and
              cov['protected_day_overflow'] is False and
              cov['coverage_shortfall'] is False, 'limits not binding')
        tel.limits['history_max_bytes'] = 50
        tel.effective_cap.update(effective=50)
        tel.retention_sweep('test')
        log = list(tel.retention_log)
        check(log and all(r['reason'] == 'size' and r['bytes'] > 0
                          for r in log), 'in-memory retention log kept')
        info = tel.inspector_info()
        kinds = {e['event'] for e in info['retention_events']}
        check('retention_delete' in kinds and
              info['history_policy']['history_max_bytes'] == 50,
              'inspector exposes retention events and the effective policy')


def test_v084_gap_detection():
    print('test_v084_gap_detection')
    with _Tmp() as base:
        clk = _Clock(T0 + 600)
        tel = _mk(base, clk)
        tel.set_expected_interval(300)
        h = tel.history_store
        h.append(_obs(T0 + 600))
        h.append(_obs(T0 + 900))
        check(not tel.gaps, 'regular cadence is not a gap')
        h.append(_obs(T0 + 5000))
        check(len(tel.gaps) == 1 and tel.gaps[0]['seconds'] == 4100 and
              tel.gaps[0]['reason'] == 'missed_collection',
              'a silence of several intervals is reported as a gap')
        h.append(_obs(T0 + 9000))
        check(len(tel.gaps) == 2 and tel.gaps[-1]['seconds'] == 4000,
              'a second silence is a second gap')
        check(len(_events(tel, 'history_gap')) == 2, 'gap events persisted')
        tel2 = _mk(base, _Clock(T0 + 20000))
        tel2.set_expected_interval(300)
        tel2.history_store.append(_obs(T0 + 20000))
        check(tel2.gaps and tel2.gaps[-1]['start_ts'] == T0 + 9000 and
              tel2.gaps[-1]['reason'] == 'process_start',
              'restart gap measured against the last persisted observation')


# --- low disk ----------------------------------------------------------------

def test_v084_low_space_pause_resume_hysteresis():
    print('test_v084_low_space_pause_resume_hysteresis')
    with _Tmp() as base:
        clk = _Clock(T0 + 3 * DAY + 100)
        free = _Free(4096 * MIB)
        tel = _mk(base, clk, retention={'space_reserve_bytes': 1000 * MIB},
                  free=free)
        tel.set_expected_interval(60)
        h = tel.history_store
        for d in range(0, 3):
            _write_day(tel, T0 + d * DAY, 4, pad=2000)
        h.append(_obs(clk.t))
        check(tel.space['state'] == 'ok' and
              tel.health.persisted_observations == 1, 'writes while healthy')
        size_ok = P.file_size(h.day_path(_day_name(clk.t)))
        # Disk gets low: emergency sweep frees nothing (the fake probe does
        # not react to deletes), so history pauses.
        free.free = 900 * MIB
        clk.t += 60
        h.append(_obs(clk.t))
        check(tel.space['state'] == 'paused' and
              tel.space['reason'] == 'low_space', 'paused below the reserve')
        check(tel.space['emergency_deleted_files'] == 3 and
              [n[:-6] for n in h.day_files()] == [_day_name(clk.t)],
              'oldest unprotected days were removed first, current kept')
        check(P.file_size(h.day_path(_day_name(clk.t))) == size_ok,
              'no history bytes written while paused')
        check(len(h._ring) >= 2 and h._ring[-1]['collected_ts'] == clk.t,
              'telemetry continues in memory (RAM ring) while paused')
        events_after_pause = len(_events(tel, 'history_paused'))
        for i in range(5):
            clk.t += 60
            h.append(_obs(clk.t))
        check(tel.space['paused_observations'] >= 5, 'paused obs counted')
        check(P.file_size(h.day_path(_day_name(clk.t))) == size_ok,
              'still nothing written')
        # Hysteresis: above the reserve but below the resume threshold.
        free.free = 1010 * MIB
        clk.t += 60
        h.append(_obs(clk.t))
        check(tel.space['state'] == 'paused',
              'between reserve and resume threshold stays paused')
        free.free = RT.resume_threshold(1000 * MIB) + MIB
        clk.t += 60
        h.append(_obs(clk.t))
        check(tel.space['state'] == 'ok' and
              P.file_size(h.day_path(_day_name(clk.t))) > size_ok,
              'resumes above the resume threshold and writes again')
        check(tel.space['pauses'] == 1 and tel.space['resumes'] == 1,
              'one pause/resume pair')
        check(events_after_pause == 1 and
              len(_events(tel, 'history_paused')) == 1 and
              len(_events(tel, 'history_resumed')) <= 1,
              'transition events, not per-observation writes')
        gap = [g for g in tel.gaps if g['reason'] == 'history_paused']
        check(gap, 'the paused interval is reported as a gap')


def test_v084_low_space_guard_bounded_writes():
    print('test_v084_low_space_guard_bounded_writes')
    with _Tmp() as base:
        clk = _Clock(T0 + 100)
        free = _Free(100 * MIB)          # permanently below the reserve
        tel = _mk(base, clk, retention={'space_reserve_bytes': 1000 * MIB},
                  free=free)
        h = tel.history_store
        ev0 = P.file_size(tel.events_path)
        for i in range(500):
            clk.t += 30
            h.append(_obs(clk.t))
            if i % 50 == 0:
                tel.maintenance(clk.t)
        ev1 = P.file_size(tel.events_path)
        check(h.day_files() == [] or P.file_size(
            h.day_path(_day_name(T0 + 100))) == 0,
            'no history written for 500 observations')
        check(len(_events(tel, 'history_paused')) == 1 and
              ev1 - ev0 < 4096,
              'the guard wrote one event, not one per observation '
              '({} bytes)'.format(ev1 - ev0))
        check(len(h._ring) == 20, 'RAM ring stays bounded')


def test_v084_effective_cap_limited_by_disk():
    print('test_v084_effective_cap_limited_by_disk')
    cap, lim = RT.effective_cap(256 * MIB, 10 * MIB, 700 * MIB, 600 * MIB)
    check(lim and cap == 10 * MIB + 700 * MIB - 600 * MIB - RT.CAP_SAFETY_MARGIN
          or cap == RT.MIN_EFFECTIVE_CAP, 'cap lowered to what the disk holds')
    cap, lim = RT.effective_cap(256 * MIB, 0, 5000 * MIB, 600 * MIB)
    check(cap == 256 * MIB and not lim, 'ample disk leaves the cap alone')
    cap, lim = RT.effective_cap(256 * MIB, 0, 100 * MIB, 600 * MIB)
    check(cap == RT.MIN_EFFECTIVE_CAP and lim, 'floor applies')
    check(RT.space_reserve(8000 * MIB) == 800 * MIB and
          RT.space_reserve(1000 * MIB) == 512 * MIB,
          'reserve = max(512 MiB, 10% of the filesystem)')


def test_v084_protected_day_overflow_pauses_until_next_day():
    print('test_v084_protected_day_overflow_pauses_until_next_day')
    with _Tmp() as base:
        clk = _Clock(T0 + 3600)
        cap = 32 * MIB
        tel = _mk(base, clk, retention={'history_max_bytes': cap},
                  free=_Free(4096 * MIB))
        h = tel.history_store
        path = h.day_path(_day_name(clk.t))
        with open(path, 'wb') as f:                 # today's file at the cap
            line = P.dumps(P.encode_observation(_obs(T0 + 10))).encode(
                'utf-8') + b'\n'
            f.write(line * (cap // len(line) + 2))
        h.append(_obs(clk.t))
        check(tel.space['state'] == 'paused' and
              tel.space['reason'] == 'day_file_exceeds_cap',
              'protected current day at the cap pauses history writes')
        check(h.coverage(clk.t)['protected_day_overflow'] is True,
              'coverage reports the overflow')
        size = P.file_size(path)
        h.append(_obs(clk.t + 60))
        check(P.file_size(path) == size, 'no further growth')
        clk.t = T0 + DAY + 60
        h.append(_obs(clk.t))
        check(tel.space['state'] == 'ok' and P.file_size(
            h.day_path(_day_name(clk.t))) > 0,
            'next UTC day resumes writing into a new file')


# --- verified seek -----------------------------------------------------------

def test_v084_seek_is_verified_not_assumed():
    print('test_v084_seek_is_verified_not_assumed')
    idx = P.DayIndex(stride=4096)
    with _Tmp() as base:
        tel = _mk(base, _Clock())
        h = tel.history_store
        day_ts = T0
        path = h.day_path(_day_name(day_ts))
        lines = []
        for i in range(400):
            ts = day_ts + i * 60
            lines.append(P.dumps(P.encode_observation(
                _obs(ts, 200))).encode('utf-8') + b'\n')
        # Out-of-order (late old record), a future spike, malformed lines, an
        # oversized line and an unterminated tail.
        lines.insert(300, P.dumps(P.encode_observation(
            _obs(day_ts + 5, 200))).encode('utf-8') + b'\n')
        lines.insert(100, b'{"collected_ts": not json}\n')
        lines.insert(200, b'garbage line\n')
        lines.insert(350, b'{"collected_ts":%d,"pad":"%s"}\n' % (
            day_ts + 50, b'y' * (P.QUERY_MAX_LINE + 10)))
        with open(path, 'wb') as f:
            f.write(b''.join(lines))
            f.write(b'{"collected_ts":99')           # torn tail
        n = idx.extend(path, 1 << 30)
        check(n > 0 and idx.size < P.file_size(path) and
              idx.out_of_order >= 1 and idx.malformed >= 3 and idx.points,
              'index built; out-of-order, malformed and oversized lines '
              'noted; unterminated tail not consumed')
        # Brute force: for many starts, seeking must never skip a record
        # that the plain scan would return.
        bad = 0
        for start in (None, day_ts, day_ts + 4, day_ts + 5, day_ts + 600,
                      day_ts + 6000, day_ts + 12000, day_ts + 23940,
                      day_ts + 24000, day_ts + 90000):
            off = idx.seek_offset(start)
            full = P.scan_range(path, 0, start_ts=start)
            seek = P.scan_range(path, off, start_ts=start)
            a = [r['collected_ts'] for r in full['records']]
            b = [r['collected_ts'] for r in seek['records']]
            if a != b:
                bad += 1
        check(bad == 0, 'seeking never changes the result (disordered file)')
        late = [r['collected_ts'] for r in P.scan_range(
            path, idx.seek_offset(day_ts + 5), start_ts=day_ts + 5)[
            'records']]
        check(late.count(day_ts + 5) == 1,
              'a late out-of-order record is still found after a seek')
        check(idx.seek_offset(day_ts + 5) == 0,
              'prefix maximum prevents skipping past a disordered region')
        # Safe skip in an ordered file.
        ordered = os.path.join(base, 'ordered.jsonl')
        with open(ordered, 'wb') as f:
            for i in range(2000):
                f.write(P.dumps(P.encode_observation(
                    _obs(day_ts + i * 60, 300))).encode('utf-8') + b'\n')
        oi = P.DayIndex(stride=8192)
        oi.extend(ordered, 1 << 30)
        off = oi.seek_offset(day_ts + 1500 * 60)
        check(off > 0, 'ordered file: a verified skip exists')
        full = P.scan_range(ordered, 0, start_ts=day_ts + 1500 * 60)
        seek = P.scan_range(ordered, off, start_ts=day_ts + 1500 * 60)
        check([r['collected_ts'] for r in full['records']] ==
              [r['collected_ts'] for r in seek['records']] and
              seek['scanned_bytes'] < full['scanned_bytes'] // 2,
              'same records, far fewer bytes scanned')
        # Truncation / replacement invalidates the index.
        with open(ordered, 'r+b') as f:
            f.truncate(oi.size // 2)
        oi.extend(ordered, 1 << 30)
        check(oi.size <= P.file_size(ordered), 'shrunk file resets the index')
        check(P.DayIndex().seek_offset(5) == 0 and
              P.DayIndex().extend(os.path.join(base, 'missing'), 10) == 0,
              'empty/missing file is safe')


def test_v084_query_uses_seek_and_matches_plain_scan():
    print('test_v084_query_uses_seek_and_matches_plain_scan')
    with _Tmp() as base:
        clk = _Clock(T0 + 2 * DAY)
        tel = _mk(base, clk)
        h = tel.history_store
        for d in (0, 1):
            _write_day(tel, T0 + d * DAY, 1440, step=60, pad=4000)
        start, end = T0 + DAY // 2 + 100, T0 + DAY + 3000
        plain, sc_plain, sk0 = _query_all(h, start, end)
        check(sk0 == 0, 'without an index nothing is skipped')
        h.warm_index(1 << 30)
        fast, sc_fast, sk1 = _query_all(h, start, end)
        check(plain == fast and len(plain) > 0,
              'identical results with and without the index')
        check(sk1 > 0 and sc_fast < sc_plain,
              'index skipped {} bytes; scanned {} < {}'.format(
                  sk1, sc_fast, sc_plain))
        res = h.query(start=start, end=end, limit=250)
        check(res['scanned_bytes'] <= 1048576 + 262144,
              'per-request scan limit still enforced')


def test_v084_index_follows_appends_and_retention():
    print('test_v084_index_follows_appends_and_retention')
    with _Tmp() as base:
        clk = _Clock(T0 + 600)
        tel = _mk(base, clk, retention={'history_max_age_s': 2 * DAY})
        h = tel.history_store
        for i in range(30):
            clk.t = T0 + 600 + i * 60
            h.append(_obs(clk.t, 2000))
        idx = h._index(_day_name(T0))
        check(idx.size == P.file_size(h.day_path(_day_name(T0))),
              'index extended incrementally on append')
        clk.t = T0 + 5 * DAY
        h.append(_obs(clk.t))                 # rolls the appender to a new day
        tel.retention_sweep('test')
        h.warm_index(1 << 20)
        check(_day_name(T0) not in h._idx, 'deleted days leave the index')


# --- 256 MiB performance -----------------------------------------------------

def test_v084_history_256mib_query_performance():
    print('test_v084_history_256mib_query_performance')
    with _Tmp() as base:
        clk = _Clock(T0 + 8 * DAY + 3600)
        tel = _mk(base, clk, retention={'history_max_age_s': 30 * DAY})
        h = tel.history_store
        line_tpl = P.dumps(P.encode_observation(_obs(0, 20000))).encode(
            'utf-8')
        per_day = (32 * MIB) // (len(line_tpl) + 1)
        total = 0
        t_build = time.time()
        for d in range(8):
            path = h.day_path(_day_name(T0 + d * DAY))
            step = DAY // per_day
            with open(path, 'wb') as f:
                buf = []
                for i in range(per_day):
                    ts = T0 + d * DAY + i * step
                    buf.append(line_tpl.replace(
                        b'"collected_ts":0', b'"collected_ts":%d' % ts, 1)
                        + b'\n')
                    if len(buf) >= 200:
                        f.write(b''.join(buf))
                        buf = []
                f.write(b''.join(buf))
            total += P.file_size(path)
        build_s = time.time() - t_build
        check(total >= 250 * MIB, 'synthetic history is {:.0f} MiB '
              '(built in {:.1f}s)'.format(total / MIB, build_s))
        t = time.time()
        cov = h.coverage(clk.t)
        cov_s = time.time() - t
        check(cov['days_present'] == 8 and cov['total_bytes'] == total,
              'coverage over 8 day files in {:.3f}s'.format(cov_s))
        check(cov_s < 2.0, 'coverage is cheap (first/last line only)')
        # Query a late-in-day window BEFORE indexing (worst case, no seek).
        start = T0 + 7 * DAY + DAY * 3 // 4
        end = start + 3600
        t = time.time()
        cold = h.query(start=start, end=end, limit=250)
        cold_s = time.time() - t
        pages = 1
        cur = cold['next_cursor']
        cold_scanned = cold['scanned_bytes']
        while cur is not None and not cold['samples'] and pages < 200:
            cold = h.query(start=start, end=end, limit=250, cursor=cur)
            cold_scanned += cold['scanned_bytes']
            cur = cold['next_cursor']
            pages += 1
        t = time.time()
        warmed = 0
        while warmed < total:
            before = sum(i.size for i in h._idx.values())
            h.warm_index(INDEX_STEP)
            warmed = sum(i.size for i in h._idx.values())
            if warmed == before:
                break
        warm_s = time.time() - t
        t = time.time()
        fast = h.query(start=start, end=end, limit=250)
        fast_s = time.time() - t
        print('  INFO: 256 MiB history: cold first-hit needs {} request(s), '
              '{:.1f} MiB scanned; indexing {:.2f}s; seek query {:.3f}s '
              'scanned {:.2f} MiB skipped {:.1f} MiB; coverage {:.3f}s'.format(
                  pages, cold_scanned / MIB, warm_s, fast_s,
                  fast['scanned_bytes'] / MIB,
                  fast['seek_skipped_bytes'] / MIB, cov_s))
        check(fast['count'] > 0 and fast['scanned_bytes'] <= 1.3 * MIB,
              'seek query answers in ONE bounded request')
        check(fast['seek_skipped_bytes'] > 20 * MIB,
              'verified seek skipped most of the day file')
        check(pages > 1 or cold['samples'] == [] or
              cold_scanned > fast['scanned_bytes'],
              'without the index the same window needs many more bytes')
        check(fast_s < 1.0, 'indexed query latency under 1 s')
        t = time.time()
        full, sc, sk = _query_all(h, T0 + 7 * DAY, T0 + 7 * DAY + 7200)
        check(len(full) > 0 and sorted(full) == full,
              'multi-page query over the indexed 256 MiB history works')


INDEX_STEP = 64 * MIB


def test_v084_legacy_history_files_remain_readable():
    print('test_v084_legacy_history_files_remain_readable')
    with _Tmp() as base:
        clk = _Clock(T0 + 7200)
        tel = _mk(base, clk)
        h = tel.history_store
        legacy = {'collected_ts': T0 + 60, 'sample_source': 'scheduled',
                  'cv': {'steering_definitions': 1, 'wbond_config': 1},
                  'cellular': [{
                      'uid': 'mdm-1', 'service_mode': '5G NSA',
                      'rsrp': -90.0, 'carriers': [{
                          'role': 'pcell', 'rat': 'LTE', 'band': 'Band 66',
                          'bandwidth_mhz': 10.0, 'channel': '1', 'pci': '1',
                          'active': True, 'rf': {
                              'rsrp': None, 'rsrq': None, 'sinr': 2.5,
                              'sinr_raw': 25.0, 'sinr_scale': 'ambiguous',
                              'validity': {'sinr': 'ambiguous_scale'}}}],
                      'metric_ts': T0 + 60, 'collected_ts': T0 + 60,
                      'timestamp_source': 'collector'}]}
        with open(h.day_path(_day_name(T0)), 'ab') as f:
            f.write(P.dumps(legacy).encode('utf-8') + b'\n')
        h.append(_obs(T0 + 120))
        out = h.query(start=T0, end=T0 + 600)
        first = out['samples'][0]
        check(out['count'] == 2 and first['cellular'][0]['carriers'][0][
            'rf']['validity']['sinr'] == 'ambiguous_scale' and
            first['config_versions']['wbond_config']['version'] == 1,
            'a v0.8.3-shaped record is returned unchanged')
        check(h.coverage(clk.t)['days_present'] == 1,
              'coverage handles legacy files')


TESTS = [
    test_v084_policy_defaults_and_bounds,
    test_v084_policy_reported_in_storage,
    test_v084_periodic_sweep_age_and_size,
    test_v084_retention_log_and_coverage,
    test_v084_gap_detection,
    test_v084_low_space_pause_resume_hysteresis,
    test_v084_low_space_guard_bounded_writes,
    test_v084_effective_cap_limited_by_disk,
    test_v084_protected_day_overflow_pauses_until_next_day,
    test_v084_seek_is_verified_not_assumed,
    test_v084_query_uses_seek_and_matches_plain_scan,
    test_v084_index_follows_appends_and_retention,
    test_v084_history_256mib_query_performance,
    test_v084_legacy_history_files_remain_readable,
]
