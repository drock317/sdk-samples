"""Durable telemetry storage tests (persistence.py + durable.py).

Run through run_tests.py (which binds its `check` function and test helpers
into this module), so one runner reports a single PASS count. Every test uses
a private temp directory as the app root; nothing touches the real app dir.
Pure standard library; no router, no network except localhost HTTP.
"""
import copy
import json
import os
import shutil
import tempfile
import threading
from http.server import HTTPServer

from analyzer import collectors, durable, persistence as P, web
from analyzer.service import AnalyzerService
import fixtures

check = None            # bound by run_tests.bind()
_Clock = None
_fake_collectors = None
_free_port = None
_http_json = None


def bind(check_fn, clock_cls, fake_cls, free_port_fn, http_json_fn):
    global check, _Clock, _fake_collectors, _free_port, _http_json
    check = check_fn
    _Clock = clock_cls
    _fake_collectors = fake_cls
    _free_port = free_port_fn
    _http_json = http_json_fn


# --- helpers -----------------------------------------------------------------

def _mk(base, clock, retention=None):
    return AnalyzerService(clock=clock, app_root=base,
                           telemetry_root='tmp/telemetry',
                           telemetry_retention=retention)


def _root(base):
    return os.path.join(base, 'tmp', 'telemetry')


def _read_lines(path):
    return [r for _, _, r in P.iter_jsonl(path)]


def _events(base, name='events.jsonl'):
    return _read_lines(os.path.join(_root(base), name))


def _event_types(base):
    return [e['event'] for e in _events(base)]


def _obs(ts, source='scheduled'):
    return {'sample_source': source, 'job_id': None, 'refresh_group': None,
            'timestamp': ts, 'collected_ts': ts, 'collector_errors': [],
            'duration_ms': 1}


class _Tmp(object):
    def __enter__(self):
        self.base = tempfile.mkdtemp(prefix='sdwan_dur_')
        return self.base

    def __exit__(self, *exc):
        shutil.rmtree(self.base, ignore_errors=True)


class _CountFsync(object):
    """Count os.fsync calls (still performs them)."""

    def __enter__(self):
        self.n = 0
        self._orig = os.fsync

        def counting(fd):
            self.n += 1
            return self._orig(fd)
        os.fsync = counting
        return self

    def __exit__(self, *exc):
        os.fsync = self._orig


def _run(svc, clock, seconds):
    for _ in range(seconds):
        svc.recording_tick()
        clock.t += 1


def _record(svc, clock, label, secs):
    """Run one complete short Recording; returns its job_id."""
    job = svc.start_recording(label, max_duration_sec=secs)['recording']
    _run(svc, clock, secs + 3)
    return job['job_id']


# --- persistence primitives ---------------------------------------------------

def test_dur_atomic_json():
    print('test_dur_atomic_json')
    with _Tmp() as d:
        p = os.path.join(d, 'a', 'state.json')
        P.write_json_atomic(p, {'x': 1})
        check(P.read_json(p) == {'x': 1} and not os.path.exists(p + '.tmp'),
              'atomic write succeeds and leaves no .tmp')
        orig = os.replace

        def boom(*a, **k):
            raise OSError('replace failed')
        os.replace = boom
        try:
            raised = False
            try:
                P.write_json_atomic(p, {'x': 2})
            except OSError:
                raised = True
        finally:
            os.replace = orig
        check(raised and P.read_json(p) == {'x': 1},
              'failed replace keeps the previous primary intact')
        check(os.path.exists(p + '.tmp') and
              P.recover_json(p) == ({'x': 1}, 'ok') and
              not os.path.exists(p + '.tmp'),
              'valid primary wins; leftover .tmp discarded')
        with open(p, 'w') as f:
            f.write('{bad')
        with open(p + '.tmp', 'w') as f:
            f.write('{"x": 3}')
        obj, st = P.recover_json(p)
        check(obj == {'x': 3} and st == 'promoted_tmp' and
              os.path.exists(p + '.corrupt') and P.read_json(p) == {'x': 3},
              'valid .tmp promoted over a corrupt primary (bad one kept)')
        with open(p, 'w') as f:
            f.write('nonsense')
        obj, st = P.recover_json(p)
        check(obj is None and st == 'quarantined' and
              not os.path.exists(p) and os.path.exists(p + '.corrupt'),
              'corrupt primary without tmp is quarantined')
        check(P.recover_json(p) == (None, 'missing'), 'missing reported')
        with open(p, 'w') as f:
            f.write('[1]')
        check(P.recover_json(p)[1] == 'quarantined',
              'wrong root type is treated as invalid')
        oo = os.open

        def nope(*a, **k):
            raise OSError('no dir fsync')
        os.open = nope
        try:
            P.fsync_directory(d)
            P.write_json_atomic(os.path.join(d, 'b.json'), {'ok': True})
            good = True
        except Exception:
            good = False
        finally:
            os.open = oo
        check(good and P.read_json(os.path.join(d, 'b.json')) == {'ok': True},
              'directory fsync is best effort (never raises)')


def test_dur_jsonl_appender_flush_and_fsync():
    print('test_dur_jsonl_appender_flush_and_fsync')
    with _Tmp() as d:
        p = os.path.join(d, 'x', 's.jsonl')
        now = [0.0]
        app = P.JsonlAppender(p, fsync_interval=30, clock=lambda: now[0])
        app.open()
        with _CountFsync() as c:
            app.append({'collected_ts': 1, 'a': 1})
            app.append({'collected_ts': 2, 'a': 2})
            check(len(_read_lines(p)) == 2 and c.n == 0,
                  'each record is flushed (readable) without an fsync')
            now[0] = 31.0
            check(app.maybe_sync() is True and c.n == 1 and
                  app.maybe_sync() is False, 'fsync after the interval only')
            app.append({'collected_ts': 3})
            now[0] = 62.0
            app.append({'collected_ts': 4})
            check(c.n == 2, 'append triggers the periodic fsync')
            app.append({'collected_ts': 5})
            check(app.sync() is True and c.n == 3 and app.sync() is False,
                  'explicit fsync (idempotent when clean)')
            app.close()
            app.close()
            check(not app.is_open(), 'close is safe to repeat')
        app0 = P.JsonlAppender(os.path.join(d, 'y.jsonl'), fsync_interval=0)
        app0.open()
        with _CountFsync() as c:
            app0.append({'collected_ts': 1})
            app0.append({'collected_ts': 2})
            check(c.n == 2, 'interval 0 fsyncs every append')
        app0.close()
        app_n = P.JsonlAppender(os.path.join(d, 'z.jsonl'))
        with _CountFsync() as c:
            app_n.append({'collected_ts': 1})
            app_n.append({'collected_ts': 2})
            base = c.n
            app_n.close()
        check(base <= 1 and c.n == base + 1,
              'no interval: only explicit/close fsync')


def test_dur_jsonl_tail_repair():
    print('test_dur_jsonl_tail_repair')
    with _Tmp() as d:
        p = os.path.join(d, 't.jsonl')
        good = b'{"collected_ts":1,"a":1}\n{"collected_ts":2,"a":2}\n'
        with open(p, 'wb') as f:
            f.write(good + b'{"collected_ts":3,"a":3}')
        rep = P.repair_jsonl_tail(p)
        check(rep['action'] == 'newline_added' and
              len(_read_lines(p)) == 3, 'valid trailing fragment retained')
        with open(p, 'wb') as f:
            f.write(good + b'{"collected_ts":4,"a"')
        rep = P.repair_jsonl_tail(p)
        with open(p, 'rb') as f:
            body = f.read()
        check(rep['action'] == 'truncated' and body == good and
              rep['dropped_bytes'] > 0, 'torn fragment truncated; earlier '
              'records byte-identical')
        with open(p + '.corrupt', 'rb') as f:
            check(b'"collected_ts":4' in f.read(),
                  'torn fragment preserved in .corrupt')
        check(P.repair_jsonl_tail(p)['action'] == 'none',
              'file ending in newline: no action')
        check(P.repair_jsonl_tail(os.path.join(d, 'nope'))['action'] ==
              'none', 'missing file: no action')
        with open(p, 'wb') as f:
            f.write(good + b'{"collected_ts":9,"a"')
        app = P.JsonlAppender(p, fsync_interval=0)
        app.append({'collected_ts': 5, 'a': 5})
        app.close()
        check([r['collected_ts'] for r in _read_lines(p)] == [1, 2, 5],
              'appender repairs a torn tail before its first write')
        big = os.path.join(d, 'big.jsonl')
        with open(big, 'wb') as f:
            f.write(good + b'{"x":"' + b'z' * 200000)
        rep = P.repair_jsonl_tail(big)
        check(rep['action'] == 'truncated' and
              os.path.getsize(big + '.corrupt') <= P.MAX_CORRUPT_FRAGMENT + 1
              and open(big, 'rb').read() == good,
              'huge fragment: corrupt copy capped, file truncated')


def test_dur_jsonl_readers():
    print('test_dur_jsonl_readers')
    with _Tmp() as d:
        p = os.path.join(d, 'r.jsonl')
        with open(p, 'wb') as f:
            for ts in (100, 200, 300):
                f.write(P.dumps({'collected_ts': ts}).encode() + b'\n')
            f.write(b'garbage line\n[1]\n')
            for ts in (400, 500, 600):
                f.write(P.dumps({'collected_ts': ts}).encode() + b'\n')
            f.write(b'{"collected_ts":700')
        stats = {}
        recs = [r for _, _, r in P.iter_jsonl(p, stats=stats)]
        check([r['collected_ts'] for r in recs] ==
              [100, 200, 300, 400, 500, 600] and stats['skipped'] == 2,
              'reader skips invalid lines and the incomplete tail')
        check([r['collected_ts'] for r in P.read_newest(p, 3)] ==
              [400, 500, 600], 'newest-N reader')
        check(len(P.read_newest(p, 100)) == 6, 'newest-N larger than file')
        tail = P.read_tail_records(p, 50)
        check([r['collected_ts'] for r in tail] == [600],
              'tail reader drops the partial first line')
        tail = P.read_tail_records(p, 62)
        check([r['collected_ts'] for r in tail] == [500, 600],
              'tail reader keeps a first line that starts on the boundary')
        recs, nxt, eof = P.read_range(p, 200, 500)
        check([r['collected_ts'] for r in recs] == [200, 300, 400, 500] and
              eof, 'ranged reader (start/end)')
        recs, nxt, eof = P.read_range(p, limit=2)
        check([r['collected_ts'] for r in recs] == [100, 200] and not eof,
              'ranged reader honours limit')
        recs2, nxt2, eof2 = P.read_range(p, offset=nxt, limit=10)
        check([r['collected_ts'] for r in recs2] == [300, 400, 500, 600]
              and eof2, 'byte-offset cursor resumes exactly')
        recs3, _, eof3 = P.read_range(p, max_bytes=20)
        check(len(recs3) == 1 and not eof3, 'byte budget returns >= 1 record')
        check(P.peek_ts(b'{"collected_ts":123,"a":1}') == 123.0 and
              P.peek_ts(b'{"a":1}') is None, 'prefix timestamp peek')
        check(P.read_newest(os.path.join(d, 'none'), 3) == [] and
              list(P.iter_jsonl(os.path.join(d, 'none'))) == [],
              'missing file reads as empty')


def test_dur_job_id_validation():
    print('test_dur_job_id_validation')
    check(P.valid_job_id('rec-1790380800-001') and
          P.valid_job_id('rec-1-1000'), 'generated IDs accepted')
    for bad in ('../x', 'rec-1-1/..', '/etc/passwd', 'rec-abc-001', '',
                'rec-1790380800-01', 'rec-1790380800-001/', None, 5,
                'rec-1790380800-001\n', 'REC-1-001'):
        check(not P.valid_job_id(bad), 'rejected: {!r}'.format(bad))
    raised = False
    try:
        P.safe_job_dir('/r', '../../etc')
    except ValueError:
        raised = True
    check(raised, 'safe_job_dir refuses path fragments')
    check(P.safe_job_dir('/r', 'rec-1-001') == os.path.join('/r',
                                                            'rec-1-001'),
          'safe_job_dir joins a valid ID')


def test_dur_observation_codec():
    print('test_dur_observation_codec')
    with _Tmp() as base:
        clock = _Clock()
        svc = _mk(base, clock)
        with _fake_collectors():
            obs = svc.collect_startup()
            job = svc.start_recording('c', max_duration_sec=30)['recording']
            _run(svc, clock, 12)
        full = json.dumps(obs, default=str)
        rec = P.encode_observation(obs)
        line = P.dumps(rec)
        check(line.startswith('{"collected_ts":'), 'collected_ts is first')
        check(len(line) < len(P.dumps(obs)) * 0.85,
              'persist form is materially smaller ({} vs {})'.format(
                  len(line), len(P.dumps(obs))))
        check('duration_ms' not in rec and 'config_versions' not in rec and
              'job_id' not in rec and 'refresh_group' not in rec and
              'timestamp' not in rec and 'collector_errors' not in rec,
              'null/empty/duplicate envelope fields dropped')
        check(isinstance(rec['cv'], dict) and
              rec['cv']['traffic_classes'] == 1 and
              set(rec['cvc']) == set(rec['cv']),
              'config refs compacted to {name: version} (+changed names)')
        check(all('metric_ts' not in r for r in rec['interfaces']) and
              all('metric_ts' in r for r in rec['swans_samples']) and
              all('metric_ts' in r for r in rec['steering_events']) and
              all('metric_ts' not in r for r in rec['wan']['paths']),
              'collector envelopes hoisted; NCOS-native timestamps kept')
        dec = P.decode_observation(
            json.loads(line),
            fingerprint_lookup=svc._config_store.fingerprint_for)
        check(dec == dict(obs, duration_ms=None),
              'decode(encode(obs)) rebuilds the API representation')
        check(json.dumps(obs, default=str) == full, 'encode is non-mutating')
        samples = svc.recording_samples(job['job_id'])
        raw = None
        for s in samples:
            if s['recording_group'] == 'wan':
                raw = s
        check(raw is not None and raw['sample_source'] == 'recording' and
              raw['job_id'] == job['job_id'] and 'interfaces' in raw and
              all('metric_ts' in r for r in raw['interfaces']),
              'Recording samples decode with source/job and full envelopes')
        with open(os.path.join(_root(base), 'recordings', job['job_id'],
                               'samples.jsonl')) as f:
            first = json.loads(f.readline())
        check('sample_source' not in first and 'job_id' not in first and
              'cv' not in first and 'recording_group' in first,
              'Recording lines keep recording_group, drop implied fields')


# --- config versions ------------------------------------------------------------

def test_dur_config_versions_restart():
    print('test_dur_config_versions_restart')
    with _Tmp() as base:
        clock = _Clock()
        with _fake_collectors() as fc:
            s1 = _mk(base, clock)
            s1.collect_startup()
            lines = _read_lines(os.path.join(_root(base),
                                             'config_versions.jsonl'))
            names = sorted(r['name'] for r in lines)
            # v0.8.3 adds steering_rule_defs (fixtures carry no configured
            # bonding, so wan_bonding_config is absent here): 8 objects.
            check(len(lines) == 8 and 'steering_rule_defs' in names and all(
                set(r) == {'name', 'version', 'fingerprint', 'ts', 'payload'}
                for r in lines) and all(r['version'] == 1 for r in lines),
                'one record per object on first sight: {}'.format(names))
            before = copy.deepcopy(s1._config_store.summary())
            s2 = _mk(base, clock)
            check(s2._config_store.summary() == before,
                  'restart restores versions + fingerprints')
            s2.collect_startup()
            check(len(_read_lines(os.path.join(
                _root(base), 'config_versions.jsonl'))) == 8 and
                s2._config_store.summary() == before,
                'matching post-restart collection reuses the version')
            fc.overrides['identities_intent'] = copy.deepcopy(
                fixtures.IDENTITIES_INTENT)
            tc = list(fc.overrides['identities_intent'].values())[0]
            tc['name'] = 'Renamed class'
            for _ in range(2):
                fc.overrides['traffic_class_config'] = copy.deepcopy(
                    fixtures.TRAFFIC_CLASS_CFG)
                list(fc.overrides['traffic_class_config'].values())[0][
                    'dscp'] = 10 + _
                s2.collect('manual', collectors.CONFIG_KEYS)
            v = s2._config_store.current('traffic_classes')['version']
            check(v >= 2, 'a real change makes a new version ({})'.format(v))
            s3 = _mk(base, clock)
            check(s3._config_store.current('traffic_classes')['version'] ==
                  v and s3._durable.recovery[
                      'config_versions_restored'] >= 8,
                  'restart: counter continues from the maximum version')
            fc.overrides['traffic_class_config'] = copy.deepcopy(
                fixtures.TRAFFIC_CLASS_CFG)
            list(fc.overrides['traffic_class_config'].values())[0][
                'dscp'] = 99
            s3.collect('manual', collectors.CONFIG_KEYS)
            check(s3._config_store.current('traffic_classes')['version'] ==
                  v + 1, 'next change continues the numbering')


def test_dur_config_compaction_and_bak():
    print('test_dur_config_compaction_and_bak')
    with _Tmp() as base:
        clock = _Clock()
        tel = durable.TelemetryStorage(_root(base), clock=clock)
        check(tel.prepare(), 'tree created')
        saved = durable.CONFIG_COMPACT_BYTES
        durable.CONFIG_COMPACT_BYTES = 4000
        try:
            cs = tel.config_store
            for i in range(1, 41):
                cs.update('big', 'fp{}'.format(i), {'i': i, 'pad': 'x' * 200},
                          1790380800 + i)
            cs.update('small', 'fps1', {'a': 1}, 1790380800)
            path = tel.config_path
            check(os.path.exists(path + '.bak'), 'compaction kept a .bak')
            check(os.path.getsize(path) < 40 * 260,
                  'compaction bounded the file ({} B)'.format(
                      os.path.getsize(path)))
            t2 = durable.TelemetryStorage(_root(base), clock=clock)
            t2.prepare()
            t2.config_store.load()
            vs = t2.config_store.versions('big')
            check(len(vs) <= 20 and vs[-1]['version'] == 40 and
                  t2.config_store.versions('small')[-1]['version'] == 1,
                  'newest <=20 versions per object kept, latest preserved')
            res = t2.config_store.update('big', 'fp40', {'i': 40}, 1)
            check(res['changed'] is False and res['version'] == 40,
                  'fingerprint reuse after compaction')
            res = t2.config_store.update('big', 'fp41', {'i': 41}, 2)
            check(res['version'] == 41, 'counter continuity after compaction')
            # .bak recovery: primary lost.
            shutil.copyfile(path, path + '.bak')
            os.remove(path)
            t3 = durable.TelemetryStorage(_root(base), clock=clock)
            t3.prepare()
            t3.config_store.load()
            check(t3.config_store.current('big')['version'] >= 40 and
                  os.path.exists(path) and
                  'config_versions_recovered' in [
                      e['event'] for e in _read_lines(t3.events_path)],
                  'primary lost: restored from .bak')
            # Crash between rotate and promote: complete .tmp wins.
            shutil.copyfile(path, path + '.tmp')
            os.remove(path)
            t4 = durable.TelemetryStorage(_root(base), clock=clock)
            t4.prepare()
            t4.config_store.load()
            check(t4.config_store.current('big')['version'] >= 40 and
                  not os.path.exists(path + '.tmp'),
                  'primary missing + valid .tmp: promoted')
            # Corrupt primary falls back to .bak and is quarantined.
            with open(path, 'w') as f:
                f.write('not json at all\n')
            t5 = durable.TelemetryStorage(_root(base), clock=clock)
            t5.prepare()
            t5.config_store.load()
            check(os.path.exists(path + '.corrupt') and
                  t5.config_store.current('big') is not None,
                  'corrupt primary quarantined; backup used')
        finally:
            durable.CONFIG_COMPACT_BYTES = saved


# --- history --------------------------------------------------------------------

def test_dur_history_utc_day_files_and_ring():
    print('test_dur_history_utc_day_files_and_ring')
    with _Tmp() as base:
        clock = _Clock()
        svc = _mk(base, clock)
        d0 = durable.day_start_ts(durable.utc_day(1790380800))
        day_a = durable.utc_day(d0)
        day_b = durable.utc_day(d0 + 86400)
        with _CountFsync() as c:
            svc._store.append(_obs(d0 + 86400 - 5))
            n1 = c.n
            svc._store.append(_obs(d0 + 86400 + 5))
        check(n1 >= 1 and c.n > n1, 'each observation is fsynced')
        hdir = os.path.join(_root(base), 'history')
        check(sorted(os.listdir(hdir)) == [day_a + '.jsonl',
                                           day_b + '.jsonl'],
              'UTC day boundary selects the day file')
        for i in range(30):
            svc._store.append(_obs(d0 + 86400 + 10 + i))
        check(len(svc.history(limit=None)) == 20 and
              svc._store.max_observations() == 20, 'RAM ring bounded to 20')
        check(svc._store.stats() == {'backend': 'durable-jsonl',
                                     'count': 20, 'durable': True},
              'stats report the durable backend')
        total = sum(len(_read_lines(os.path.join(hdir, n)))
                    for n in os.listdir(hdir))
        check(total == 32, 'all observations are on disk ({})'.format(total))
        s2 = _mk(base, clock)
        ring = s2.history(limit=None)
        check(len(ring) == 20 and ring[-1]['collected_ts'] == d0 + 86400 + 39
              and ring[0]['collected_ts'] == d0 + 86400 + 20,
              'restart restores only the newest ~20 observations')
        check(s2._durable.recovery['history_restored'] == 20,
              'recovery reports ring size, not the full history')
        check(ring[-1]['sample_source'] == 'scheduled' and
              ring[-1]['job_id'] is None, 'ring entries use the API form')


def test_dur_history_insane_clock():
    print('test_dur_history_insane_clock')
    with _Tmp() as base:
        clock = _Clock(100)
        svc = _mk(base, clock)
        svc._store.append(_obs(100))
        svc._store.append(_obs(200))
        hdir = os.path.join(_root(base), 'history')
        rep = svc.storage_report()['persistent']
        check(os.listdir(hdir) == [] and len(svc.history()) == 2,
              'insane timestamps stay in the RAM ring, no 1970 day file')
        check(rep['clock_sane'] is False and rep['held_observations'] == 2,
              'condition visible in storage health')
        svc._store.append(_obs(1790380800))
        rep = svc.storage_report()['persistent']
        check(len(os.listdir(hdir)) == 1 and rep['clock_sane'] is True and
              rep['persisted_observations'] == 1,
              'normal writes resume once time is sane')


def test_dur_history_query_paging():
    print('test_dur_history_query_paging')
    with _Tmp() as base:
        clock = _Clock()
        svc = _mk(base, clock)
        d0 = durable.day_start_ts(durable.utc_day(1790380800))
        stamps = [d0 + 86400 - 50 + i * 10 for i in range(25)]
        for ts in stamps:
            svc._store.append(_obs(ts))
        st = svc._store
        got, cur, pages = [], None, 0
        while True:
            r = st.query(limit=10, cursor=cur)
            got.extend(s['collected_ts'] for s in r['samples'])
            pages += 1
            cur = r['next_cursor']
            if cur is None or pages > 10:
                break
        check(got == stamps and pages == 3,
              'cursor paging returns every record once, in order')
        r = st.query(start=stamps[5], end=stamps[9], limit=100)
        check([s['collected_ts'] for s in r['samples']] == stamps[5:10] and
              r['next_cursor'] is None, 'range query')
        r = st.query(start=stamps[0], limit=100, max_bytes=300)
        check(0 < r['count'] < 25 and r['next_cursor'] is not None,
              'byte budget bounds a page')
        raised = False
        try:
            st.query(cursor='../../etc')
        except ValueError:
            raised = True
        check(raised, 'invalid cursor rejected')
        check(all(s['sample_source'] == 'scheduled' for s in
                  st.query(limit=3)['samples']), 'queries decode to API form')


def _write_day(base, day_ts, size, mark='x'):
    hdir = os.path.join(_root(base), 'history')
    os.makedirs(hdir, exist_ok=True)
    name = durable.utc_day(day_ts) + '.jsonl'
    with open(os.path.join(hdir, name), 'w') as f:
        f.write(P.dumps({'collected_ts': day_ts + 5, 'pad': mark * size})
                + '\n')
    return name


def test_dur_history_retention_age_and_size():
    print('test_dur_history_retention_age_and_size')
    with _Tmp() as base:
        now = durable.day_start_ts('2026-10-10') + 12 * 3600
        clock = _Clock(now)
        svc = _mk(base, clock, retention={'history_max_age_s': 2 * 86400})
        n = {i: _write_day(base, now - i * 86400 - 12 * 3600, 100)
             for i in range(0, 6)}
        svc._durable.retention_sweep('test')
        left = sorted(os.listdir(os.path.join(_root(base), 'history')))
        check(left == sorted([n[0], n[1], n[2]]),
              'files wholly older than 7d(2d here) deleted: {}'.format(left))
        check('retention_delete' in _event_types(base),
              'retention deletions are logged')
    with _Tmp() as base:
        now = durable.day_start_ts('2026-10-10') + 12 * 3600
        clock = _Clock(now)
        svc = _mk(base, clock, retention={'history_max_bytes': 3000})
        o = [_write_day(base, now - i * 86400 - 12 * 3600, 1400)
             for i in (3, 2, 1)]
        today = _write_day(base, now - 12 * 3600, 5000)
        svc._durable.retention_sweep('test')
        left = sorted(os.listdir(os.path.join(_root(base), 'history')))
        check(left == [today], 'oldest removed first; today\'s file '
              'protected even when over the size limit: {}'.format(left))
    with _Tmp() as base:
        now = durable.day_start_ts('2026-10-10') + 12 * 3600
        clock = _Clock(now)
        svc = _mk(base, clock, retention={'history_max_age_s': 86400})
        old = _write_day(base, now - 4 * 86400, 50)
        svc._store.append(_obs(now))                    # opens today's file
        clock.t = now + 12 * 3600 + 60                  # next UTC day
        svc._store.append(_obs(int(clock.t)))           # day rollover
        left = os.listdir(os.path.join(_root(base), 'history'))
        check(old not in left and len(left) == 2,
              'sweep runs at UTC day rollover')


def test_dur_history_retention_default_profile():
    print('test_dur_history_retention_default_profile')
    check(durable.DEFAULT_RETENTION == {
        'history_max_age_s': 7 * 86400,
        'history_max_bytes': 256 * 1024 * 1024,
        'recordings_max_jobs': 10,
        'recordings_max_bytes': 32 * 1024 * 1024},
        'locked default retention: 7 d / 256 MiB history (v0.8.4), 10 jobs / '
        '32 MB '
        'Recordings')
    with _Tmp() as base:
        svc = _mk(base, _Clock())
        lim = svc.storage_report()['persistent']['retention']
        check(lim['history_max_age_s'] == 604800 and
              lim['recordings_max_jobs'] == 10, 'limits shown in /api/storage')


# --- recordings -----------------------------------------------------------------

def test_dur_recording_samples_on_disk():
    print('test_dur_recording_samples_on_disk')
    with _Tmp() as base:
        clock = _Clock()
        svc = _mk(base, clock)
        with _fake_collectors():
            svc.collect_startup()
            job = svc.start_recording('disk', max_duration_sec=90)[
                'recording']
            jid = job['job_id']
            jdir = os.path.join(_root(base), 'recordings', jid)
            meta = P.read_json(os.path.join(jdir, 'metadata.json'))
            check(meta['state'] == 'recording' and
                  [e['event_type'] for e in meta['events']] ==
                  ['recording_started'], 'metadata written at start')
            t_start = clock.t
            with _CountFsync() as c:
                _run(svc, clock, 40)
                n40 = c.n
            meta = P.read_json(os.path.join(jdir, 'metadata.json'))
            check(meta['checkpoint_ts'] == t_start + 60 or
                  meta['checkpoint_ts'] < t_start + 60, 'no early checkpoint')
            _run(svc, clock, 25)
            meta = P.read_json(os.path.join(jdir, 'metadata.json'))
            check(meta['checkpoint_ts'] >= t_start + 60 and
                  meta['state'] == 'recording',
                  'metadata checkpointed ~every 60 s while active')
            check(n40 >= 1 and n40 <= 4,
                  'samples fsynced about every 30 s ({} in 40 s)'.format(n40))
            lines = _read_lines(os.path.join(jdir, 'samples.jsonl'))
            n = svc._rec_store.sample_count(jid)
            check(len(lines) == n and n > 20,
                  'samples are on disk ({} lines)'.format(len(lines)))
            check(svc._rec_store._samples == {} and
                  svc._rec_store.info()['samples_in_ram'] == 0,
                  'samples are NOT held in RAM')
            got = svc.recording_samples(jid)
            check(len(got) == n and got[0]['job_id'] == jid and
                  len(svc.recording_samples(jid, limit=5)) == 5,
                  'full samples are read back from disk on request')
            r = svc.stop_recording(jid)
            check(r['ok'], 'stop accepted')
            _run(svc, clock, 2)
            meta = P.read_json(os.path.join(jdir, 'metadata.json'))
            check(meta['state'] == 'stopped' and
                  meta['stop_reason'] == 'user' and
                  meta['ended_ts'] is not None and
                  meta['config_versions_end'] is not None and
                  meta['sample_count'] == svc._rec_store.sample_count(jid) and
                  meta['sample_counts']['config_samples'] == 2 and
                  [e['event_type'] for e in meta['events']] == [
                      'recording_started', 'recording_stopped'],
                  'final metadata: state, counts, config refs, events')
            check(jid not in svc._rec_store._app,
                  'sample file closed after completion')
            d = svc.get_recording(jid)
            check(d['state'] == 'stopped' and d['events'] == meta['events'],
                  'API view unchanged')


def test_dur_recording_expiry_and_failure_metadata():
    print('test_dur_recording_expiry_and_failure_metadata')
    with _Tmp() as base:
        clock = _Clock()
        svc = _mk(base, clock)
        with _fake_collectors():
            jid = _record(svc, clock, 'exp', 20)
            meta = P.read_json(os.path.join(
                _root(base), 'recordings', jid, 'metadata.json'))
            check(meta['state'] == 'expired' and
                  meta['stop_reason'] == 'max_duration',
                  'expiry persisted')
            job = svc.start_recording('fail', max_duration_sec=60)[
                'recording']
            orig = svc._recording._collect

            def bad(*a, **k):
                raise RuntimeError('boom')
            svc._recording._collect = bad
            _run(svc, clock, 2)
            svc._recording._collect = orig
            meta = P.read_json(os.path.join(
                _root(base), 'recordings', job['job_id'], 'metadata.json'))
            check(meta['state'] == 'failed' and
                  'infrastructure error' in meta['stop_reason'],
                  'failure persisted')


def test_dur_recording_restart_interrupted():
    print('test_dur_recording_restart_interrupted')
    with _Tmp() as base:
        clock = _Clock()
        s1 = _mk(base, clock)
        with _fake_collectors():
            job = s1.start_recording('crash', max_duration_sec=600)[
                'recording']
            jid = job['job_id']
            _run(s1, clock, 25)
            last = clock.t
        spath = os.path.join(_root(base), 'recordings', jid, 'samples.jsonl')
        lines_before = len(_read_lines(spath))
        with open(spath, 'ab') as f:
            f.write(b'{"collected_ts":17903808')      # torn final write
        # --- process "dies" here (no shutdown) ---
        s2 = _mk(base, clock)
        d = s2.get_recording(jid)
        check(d['state'] == 'failed' and d['stop_reason'] == 'app_restart',
              'interrupted Recording becomes failed/app_restart')
        check(d['ended_ts'] is not None and
              d['started_ts'] < d['ended_ts'] <= last,
              'ended_ts from best known durable timestamp')
        check(s2.active_recording() is None and
              s2.status()['recording']['active'] is False,
              'never comes back as actively recording')
        check(d['sample_count'] == lines_before and
              os.path.exists(spath + '.corrupt'),
              'torn tail repaired; all earlier samples kept')
        check(d['events'][-1]['event_type'] == 'recording_failed' and
              d['events'][-1]['reason'] == 'app_restart', 'failure event')
        check(s2._rec_store._samples == {} and
              s2._rec_store.info()['samples_in_ram'] == 0,
              'recovery loads metadata only, not samples')
        types = _event_types(base)
        check('recording_interrupted' in types and
              'unclean_prior_exit' in types and 'tail_repaired' in types,
              'recovery events persisted: {}'.format(types))
        rec = s2._durable.recovery
        check(rec['recordings_interrupted'] == 1 and
              rec['previous_exit'] == 'unclean', 'recovery summary')
        st = P.read_json(os.path.join(_root(base), 'state.json'))
        check(st['running'] is True and st['schema'] == 1,
              'state.json marks the new run')
        with _fake_collectors():
            j2 = s2.start_recording('again', max_duration_sec=30)[
                'recording']
        check(j2['job_id'] != jid, 'new Recording gets a distinct ID')
        s3 = _mk(base, clock)
        check(s3.get_recording(jid)['state'] == 'failed' and
              s3.get_recording(j2['job_id'])['stop_reason'] == 'app_restart',
              'second restart recovers the next interrupted job too')


def test_dur_recording_graceful_shutdown():
    print('test_dur_recording_graceful_shutdown')
    with _Tmp() as base:
        clock = _Clock()
        s1 = _mk(base, clock)
        with _fake_collectors():
            job = s1.start_recording('g', max_duration_sec=600)['recording']
            _run(s1, clock, 15)
        with _CountFsync() as c:
            s1.shutdown()
        check(c.n >= 1, 'shutdown fsyncs')
        st = P.read_json(os.path.join(_root(base), 'state.json'))
        check(st['clean_stop'] is True and st['running'] is False and
              st['last_clean_stop_ts'] == int(clock.t), 'clean-stop marker')
        s2 = _mk(base, clock)
        d = s2.get_recording(job['job_id'])
        check(d['state'] == 'stopped' and d['stop_reason'] == 'app_shutdown'
              and s2._durable.recovery['previous_exit'] == 'clean' and
              s2._durable.recovery['recordings_interrupted'] == 0,
              'graceful stop: stopped/app_shutdown, clean prior exit')
        check('unclean_prior_exit' not in _event_types(base),
              'no unclean-exit event after a clean stop')


def test_dur_recording_isolation_and_corruption():
    print('test_dur_recording_isolation_and_corruption')
    with _Tmp() as base:
        clock = _Clock()
        s1 = _mk(base, clock)
        with _fake_collectors():
            a = _record(s1, clock, 'A', 20)
            clock.t += 1000
            b = _record(s1, clock, 'B', 20)
        na = s1._rec_store.sample_count(a)
        nb = s1._rec_store.sample_count(b)
        meta_b = copy.deepcopy(s1.get_recording(b))
        ra = os.path.join(_root(base), 'recordings', a)
        rb = os.path.join(_root(base), 'recordings', b)
        with open(os.path.join(ra, 'metadata.json'), 'w') as f:
            f.write('{"job_id": "corrupt')
        with open(os.path.join(rb, 'samples.jsonl'), 'ab') as f:
            f.write(b'{"torn')
        s2 = _mk(base, clock)
        da, db = s2.get_recording(a), s2.get_recording(b)
        check(da['state'] == 'failed' and
              da['stop_reason'] == 'metadata_recovered' and
              da['sample_count'] == na and
              os.path.exists(os.path.join(ra, 'metadata.json.corrupt')),
              'corrupt metadata: quarantined, rebuilt from samples')
        check(db == meta_b and db['sample_count'] == nb,
              'the other Recording is unaffected')
        check(s2._durable.recovery['metadata_recovered'] == 1 and
              'metadata_recovered' in _event_types(base), 'event logged')
        # Mid-file damage to one job's samples never leaks into another.
        sp = os.path.join(ra, 'samples.jsonl')
        with open(sp, 'rb') as f:
            lines = f.readlines()
        lines[3] = b'garbage\n'
        with open(sp, 'wb') as f:
            f.writelines(lines)
        got_a = s2.recording_samples(a)
        check(len(got_a) == na - 1 and len(s2.recording_samples(b)) == nb,
              'mid-file invalid line skipped for that job only')
        shutil.rmtree(ra)
        s3 = _mk(base, clock)
        check(s3.get_recording(a) is None and
              s3.get_recording(b)['sample_count'] == nb and
              len(s3.recording_samples(b)) == nb,
              'deleting one Recording is just removing its directory')
        # Empty orphan directories are dropped; bad names ignored.
        os.makedirs(os.path.join(_root(base), 'recordings', 'rec-5-001'))
        os.makedirs(os.path.join(_root(base), 'recordings', '..evil'))
        s4 = _mk(base, clock)
        check(not os.path.exists(os.path.join(_root(base), 'recordings',
                                              'rec-5-001')) and
              s4.get_recording('..evil') is None and
              s4.recording_samples('../../etc') == [],
              'orphans removed; unsafe IDs never reach the filesystem')


def test_dur_recording_retention():
    print('test_dur_recording_retention')
    with _Tmp() as base:
        clock = _Clock()
        svc = _mk(base, clock, retention={'recordings_max_jobs': 2})
        ids = []
        with _fake_collectors():
            for i in range(4):
                ids.append(_record(svc, clock, 'j{}'.format(i), 10))
                clock.t += 5
        left = sorted(os.listdir(os.path.join(_root(base), 'recordings')))
        check(left == sorted(ids[2:]) and
              [r['job_id'] for r in svc.list_recordings()] == ids[2:],
              'only the newest 2 completed jobs kept: {}'.format(left))
        check(svc._rec_store.sample_count(ids[0]) == 0 and
              svc.get_recording(ids[0]) is None, 'evicted from the store')
        ev = [e for e in _events(base) if e['event'] == 'retention_delete'
              and e.get('kind') == 'recording']
        check(len(ev) == 2, 'recording retention deletions logged')
    with _Tmp() as base:
        clock = _Clock()
        svc = _mk(base, clock)
        with _fake_collectors():
            j1 = _record(svc, clock, 'one', 10)
            size1 = svc._rec_store._dir_bytes(j1)
            svc._durable.limits['recordings_max_bytes'] = int(size1 * 1.5)
            j2 = _record(svc, clock, 'two', 10)
        check(svc.get_recording(j1) is None and
              svc.get_recording(j2) is not None,
              'size limit removes the oldest completed job')
    with _Tmp() as base:
        clock = _Clock()
        svc = _mk(base, clock)
        with _fake_collectors():
            done = _record(svc, clock, 'done', 10)
            act = svc.start_recording('active', max_duration_sec=600)[
                'recording']['job_id']
            _run(svc, clock, 6)
            svc._durable.limits.update(recordings_max_jobs=0,
                                       recordings_max_bytes=1)
            svc._durable.retention_sweep('test')
        check(svc.get_recording(done) is None and
              svc.get_recording(act)['state'] == 'recording' and
              os.path.isdir(os.path.join(_root(base), 'recordings', act)),
              'the active Recording is never purged')


# --- dedupe ---------------------------------------------------------------------

def test_dur_dedupe_rebuild_from_tails():
    print('test_dur_dedupe_rebuild_from_tails')
    with _Tmp() as base:
        clock = _Clock()
        with _fake_collectors():
            s1 = _mk(base, clock)
            o1 = s1.collect_startup()
            check(len(o1['swans_samples']) == 4 and
                  len(o1['steering_events']) == 4, 'first import is 4 + 4')
            s2 = _mk(base, clock)
            check(set(s2._swans_seen._keys) == set(s1._swans_seen._keys) and
                  set(s2._event_seen._keys) == set(s1._event_seen._keys),
                  'dedupe keys rebuilt exactly from the durable tail')
            o2 = s2.collect('scheduled', collectors.RUNTIME_KEYS)
            check(o2['swans_samples'] == [] and
                  o2['steering_events'] == [],
                  'no duplicate SWANS/steering imports after restart')
            check(s2._durable.recovery['dedupe_swans'] == 4 and
                  s2._durable.recovery['dedupe_steering'] == 4,
                  'recovery counts')
    with _Tmp() as base:
        with _fake_collectors():
            fresh = _mk(base, _Clock())
            o = fresh.collect_startup()
        check(len(o['swans_samples']) == 4,
              'control: without durable tails the same data re-imports')
        names = []
        for dp, _, fn in os.walk(_root(base)):
            names.extend(fn)
        check(not any('seen' in n or 'dedupe' in n for n in names),
              'the full dedupe sets are not persisted')
    with _Tmp() as base:
        clock = _Clock()
        with _fake_collectors():
            s1 = _mk(base, clock)
            s1.start_recording('d', max_duration_sec=600)
            _run(s1, clock, 12)
            seen = set(s1._swans_seen._keys)
            s2 = _mk(base, clock)
            check(seen and seen <= set(s2._swans_seen._keys),
                  'dedupe also rebuilt from the recent Recording tail')
            o = s2.collect('scheduled', collectors.RUNTIME_KEYS)
            check(o['swans_samples'] == [], 'no duplicates vs the Recording')


# --- failure / state / events -----------------------------------------------------

def test_dur_write_failure_degrades_not_stops():
    print('test_dur_write_failure_degrades_not_stops')
    with _Tmp() as base:
        clock = _Clock()
        svc = _mk(base, clock)
        with _fake_collectors():
            svc.collect_startup()
            orig = os.fsync

            def full(fd):
                raise OSError(28, 'No space left on device')
            os.fsync = full
            try:
                obs = svc.collect('manual', collectors.RUNTIME_KEYS)
            finally:
                os.fsync = orig
            check(obs['sample_source'] == 'manual' and
                  svc.history()[-1] is obs and svc.snapshot() is not None,
                  'collection continues; in-memory state keeps working')
            p = svc.storage_report()['persistent']
            check(p['write_errors'] >= 1 and p['status'] == 'degraded' and
                  'No space' in (p['last_write_error'] or '') and
                  p['last_write_error_ts'] is not None,
                  'degraded storage health is visible')
            svc.collect('manual', collectors.RUNTIME_KEYS)
            p = svc.storage_report()['persistent']
            check(p['status'] == 'ok' and p['write_errors'] >= 1,
                  'recovers; the error count is retained')
            # Recording samples degrade into a bounded RAM fallback.
            job = svc.start_recording('w', max_duration_sec=60)['recording']
            real = P.JsonlAppender.append

            def bad(self, rec):
                raise OSError('disk gone')
            P.JsonlAppender.append = bad
            try:
                _run(svc, clock, 6)
            finally:
                P.JsonlAppender.append = real
            n = svc._rec_store.sample_count(job['job_id'])
            check(n > 0 and svc._rec_store.info()['samples_in_ram'] > 0 and
                  len(svc.recording_samples(job['job_id'])) == n and
                  svc.get_recording(job['job_id'])['state'] == 'recording',
                  'Recording continues; failed-write samples kept in a '
                  'bounded fallback')
            _run(svc, clock, 3)
            check(svc.storage_report()['persistent']['write_errors'] > 2,
                  'write errors counted')


def test_dur_state_json_exit_tracking():
    print('test_dur_state_json_exit_tracking')
    with _Tmp() as base:
        clock = _Clock()
        s1 = _mk(base, clock)
        check(s1._durable.recovery['previous_exit'] == 'first_run',
              'first run')
        st = P.read_json(os.path.join(_root(base), 'state.json'))
        check(st['schema'] == 1 and st['running'] is True and
              st['created_ts'] == int(clock.t) and 'app_version' in st,
              'state.json content')
        s1.shutdown()
        s2 = _mk(base, clock)
        check(s2._durable.recovery['previous_exit'] == 'clean', 'clean exit')
        s3 = _mk(base, clock)             # s2 never shut down
        check(s3._durable.recovery['previous_exit'] == 'unclean' and
              'unclean_prior_exit' in _event_types(base),
              'unclean exit detected and logged')
        sp = os.path.join(_root(base), 'state.json')
        with open(sp, 'w') as f:
            f.write('{broken')
        s4 = _mk(base, clock)
        check(s4._durable.recovery['previous_exit'] == 'unknown' and
              os.path.exists(sp + '.corrupt') and
              P.read_json(sp)['running'] is True,
              'corrupt state.json quarantined and regenerated')


def test_dur_events_rotation():
    print('test_dur_events_rotation')
    with _Tmp() as base:
        svc = _mk(base, _Clock())
        saved = durable.EVENTS_ROTATE_BYTES
        durable.EVENTS_ROTATE_BYTES = 2000
        try:
            for i in range(60):
                svc._durable.log_event('test', n=i, pad='y' * 80)
        finally:
            durable.EVENTS_ROTATE_BYTES = saved
        root = _root(base)
        check(os.path.exists(os.path.join(root, 'events.1.jsonl')) and
              os.path.getsize(os.path.join(root, 'events.jsonl')) < 4000,
              'events rotate to events.1.jsonl')
        check(all(isinstance(e, dict) for e in _events(base)),
              'events remain decodable')


def test_dur_unwritable_root_falls_back_to_memory():
    print('test_dur_unwritable_root_falls_back_to_memory')
    with _Tmp() as base:
        with open(os.path.join(base, 'tmp'), 'w') as f:
            f.write('a file where the runtime dir should be')
        clock = _Clock()
        svc = _mk(base, clock)
        with _fake_collectors():
            svc.collect_startup()
            job = svc.start_recording('m', max_duration_sec=30)['recording']
            _run(svc, clock, 8)
        rep = svc.storage_report()
        check(svc._durable is None and
              rep['persistent']['enabled'] is False and
              svc._store.stats()['durable'] is False and
              len(svc.recording_samples(job['job_id'])) > 0,
              'init failure: app keeps running on in-memory stores')


# --- Storage Inspector --------------------------------------------------------------

def test_dur_storage_inspector_persistent():
    print('test_dur_storage_inspector_persistent')
    with _Tmp() as base:
        clock = _Clock()
        svc = _mk(base, clock)
        with _fake_collectors():
            svc.collect_startup()
            r0 = svc.storage_report()
            p0 = r0['persistent']
            check(p0['enabled'] is True and p0['root'] == 'tmp/telemetry' and
                  p0['status'] == 'ok' and p0['root_exists'] is True,
                  'persistent enabled at tmp/telemetry')
            _record(svc, clock, 'growth', 10)
            svc.collect('manual', collectors.RUNTIME_KEYS)
            svc._durable.log_event('test_event')
            rep = svc.storage_report()
        p = rep['persistent']
        check(p['total_bytes'] > p0['total_bytes'] and
              p['file_count'] > p0['file_count'],
              'growth is visible ({} -> {} bytes)'.format(
                  p0['total_bytes'], p['total_bytes']))
        rels = {f['relative_path']: f for f in p['files']}
        check({'state.json', 'config_versions.jsonl', 'events.jsonl'} <=
              set(rels) and any(r.startswith('history/') for r in rels) and
              any(r.endswith('/samples.jsonl') for r in rels) and
              any(r.endswith('/metadata.json') for r in rels),
              'actual persistent files listed with safe relative names')
        check(all(not r.startswith('/') and '..' not in r for r in rels) and
              base not in json.dumps(rep), 'no absolute paths')
        check(rels['config_versions.jsonl']['type'] == 'data' and
              rels['state.json']['type'] == 'config',
              'file types')
        check(p['history']['files'] >= 1 and p['history']['bytes'] > 0 and
              p['recordings']['jobs'] == 1 and
              p['recordings']['files'] == 2 and
              p['config']['bytes'] > 0 and p['events']['files'] >= 1 and
              p['state']['files'] == 1,
              'per-area subtotals')
        areas = ('history', 'recordings', 'config', 'events', 'state')
        check(p['total_bytes'] == sum(f['size_bytes'] for f in p['files']) ==
              sum(p[x]['bytes'] for x in areas), 'bytes add up')
        check(not any(n.startswith('tmp/') for n in
                      _rel_names(rep['application_files']['files'])),
              'tmp/ is not double counted in application_files')
        check(p['retention']['history_max_bytes'] == 256 * 1024 * 1024 and
              p['retention']['last']['reason'] == 'recording_complete',
              'retention limits (and the last sweep) shown')
        check(p['last_fsync_ts'] is not None and p['write_errors'] == 0 and
              p['last_write_error'] is None and
              p['recovery']['previous_exit'] == 'first_run', 'health fields')
        fs = p['free_space']
        check(isinstance(fs, dict) and 'available' in fs and
              (not fs['available'] or fs['free_bytes'] >= 0),
              'free-space information reported (or unavailable)')
        # working/backup/quarantine types and quarantine bytes
        hdir = os.path.join(_root(base), 'history')
        for name, data in (('x.jsonl.corrupt', 'abc'),
                           ('config_versions.jsonl.bak', '{}'),
                           ('state.json.tmp', '{}')):
            with open(os.path.join(_root(base), name), 'w') as f:
                f.write(data)
        p = svc.storage_report()['persistent']
        types = {f['relative_path']: f['type'] for f in p['files']}
        check(types.get('x.jsonl.corrupt') == 'quarantine' and
              types.get('config_versions.jsonl.bak') == 'backup' and
              types.get('state.json.tmp') == 'working' and
              p['quarantine'] == {'files': 1, 'bytes': 3},
              'quarantine/backup/working types + quarantine bytes')
        check(os.path.isdir(hdir), 'history dir exists')


def _rel_names(files):
    return {f['relative_path'] for f in files}


# --- compatibility ----------------------------------------------------------------------

def test_dur_public_api_compat():
    print('test_dur_public_api_compat')
    with _Tmp() as base:
        clock = _Clock()
        svc = _mk(base, clock)
        fc = _fake_collectors()
        fc.__enter__()
        httpd = None
        try:
            svc.collect_startup()
            job = svc.start_recording('api', max_duration_sec=30)[
                'recording']
            _run(svc, clock, 8)
            port = _free_port()
            httpd = HTTPServer(('127.0.0.1', port), web._make_handler(svc))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            code, hist = _http_json(port, '/api/history')
            check(code == 200 and len(hist['samples']) == 1 and
                  hist['samples'][0]['sample_source'] == 'startup',
                  '/api/history')
            code, lst = _http_json(port, '/api/recordings')
            check(code == 200 and lst['recordings'][0]['job_id'] ==
                  job['job_id'] and lst['recordings'][0]['sample_count'] > 0,
                  '/api/recordings')
            code, act = _http_json(port, '/api/recordings/active')
            check(act['recording']['state'] == 'recording',
                  '/api/recordings/active')
            code, one = _http_json(port, '/api/recordings/' + job['job_id'])
            check(code == 200 and one['events'][0]['event_type'] ==
                  'recording_started' and 'profile' in one,
                  '/api/recordings/<id>')
            code, st = _http_json(port, '/api/status')
            check(code == 200 and st['storage']['durable'] is True and
                  st['recording']['active'] is True, '/api/status')
            code, snap = _http_json(port, '/api/snapshot')
            check(code == 200 and 'wan' in snap, '/api/snapshot')
            code, rep = _http_json(port, '/api/storage')
            check(code == 200 and rep['persistent']['enabled'] is True,
                  '/api/storage')
        finally:
            if httpd is not None:
                httpd.shutdown()
            fc.__exit__()


# --- state.json ownership (overlapping old/new processes) -------------------------

def _state(base):
    return P.read_json(os.path.join(_root(base), 'state.json'))


def test_dur_state_ownership_normal_lifecycle():
    print('test_dur_state_ownership_normal_lifecycle')
    with _Tmp() as base:
        clock = _Clock()
        a = _mk(base, clock)
        st = _state(base)
        check(st['run_id'] == a._durable.run_id and
              st['pid'] == os.getpid() and st['running'] is True and
              st['clean_stop'] is False, 'start: state carries this run_id')
        check(len(a._durable.run_id) >= 16, 'run_id is a unique token')
        a.shutdown()
        st = _state(base)
        check(st['run_id'] == a._durable.run_id and st['running'] is False
              and st['clean_stop'] is True and a._durable.state_owned is True,
              'shutdown by the owner marks a clean stop')
        b = _mk(base, clock)
        check(b._durable.run_id != a._durable.run_id and
              b._durable.recovery['previous_exit'] == 'clean',
              'next run: new run_id, previous exit clean')
        check(b.storage_report()['persistent']['run_id'] ==
              b._durable.run_id, 'inspector exposes the run_id')


def test_dur_state_ownership_overlapping_restart():
    print('test_dur_state_ownership_overlapping_restart')
    with _Tmp() as base:
        clock = _Clock()
        a = _mk(base, clock)                       # process A starts
        b = _mk(base, clock)                       # B starts before A stops
        check(_state(base)['run_id'] == b._durable.run_id,
              'B claimed state.json at startup')
        a.shutdown()                               # A finishes shutdown late
        st = _state(base)
        check(st['run_id'] == b._durable.run_id and st['running'] is True
              and st['clean_stop'] is False,
              "A did NOT overwrite B's state (no false clean marker)")
        check(a._durable.state_owned is False, 'A knows it lost ownership')
        mism = [e for e in _events(base)
                if e['event'] == 'state_ownership_mismatch']
        check(len(mism) == 1 and mism[0]['own_run_id'] ==
              a._durable.run_id and mism[0]['current_run_id'] ==
              b._durable.run_id, 'benign mismatch event logged')
        b.shutdown()                               # B later shuts down
        st = _state(base)
        check(st['run_id'] == b._durable.run_id and st['running'] is False
              and st['clean_stop'] is True,
              'B can mark its own clean stop')
        c = _mk(base, clock)
        check(c._durable.recovery['previous_exit'] == 'clean',
              'following start sees a truthful clean exit')
    with _Tmp() as base:
        a = _mk(base, _Clock())
        os.remove(os.path.join(_root(base), 'state.json'))
        a.shutdown()
        check(_state(base)['clean_stop'] is True,
              'missing state file has no other owner: owner may write')


def test_dur_state_previous_instance_wait():
    print('test_dur_state_previous_instance_wait')
    saved = (durable._sleep, durable.OVERLAP_WAIT_S, durable.OVERLAP_POLL_S)
    try:
        with _Tmp() as base:
            root = _root(base)
            os.makedirs(root)
            sp = os.path.join(root, 'state.json')
            old = {'schema': 1, 'run_id': 'old', 'pid': os.getppid(),
                   'running': True, 'clean_stop': False,
                   'last_start_ts': 5}
            P.write_json_atomic(sp, old)
            calls = []

            def old_finishes(sec):
                calls.append(sec)
                P.write_json_atomic(sp, dict(old, running=False,
                                             clean_stop=True))
            durable._sleep = old_finishes
            svc = _mk(base, _Clock())
            check(svc._durable.recovery['previous_exit'] == 'clean' and
                  len(calls) == 1 and
                  svc._durable.recovery['waited_for_previous_instance'],
                  'live predecessor finishing its shutdown is waited for '
                  '(no false unclean)')
        with _Tmp() as base:
            root = _root(base)
            os.makedirs(root)
            P.write_json_atomic(os.path.join(root, 'state.json'), old)
            durable._sleep = saved[0]
            durable.OVERLAP_WAIT_S = 0.3
            durable.OVERLAP_POLL_S = 0.05
            svc = _mk(base, _Clock())
            check(svc._durable.recovery['previous_exit'] == 'unclean',
                  'predecessor that never finishes: bounded wait, then '
                  'unclean')
        with _Tmp() as base:
            root = _root(base)
            os.makedirs(root)
            P.write_json_atomic(os.path.join(root, 'state.json'),
                                dict(old, pid=2 ** 22 + 12345))
            durable.OVERLAP_WAIT_S = 30
            svc = _mk(base, _Clock())
            check(svc._durable.recovery['previous_exit'] == 'unclean' and
                  'waited_for_previous_instance' not in
                  svc._durable.recovery,
                  'dead predecessor pid: no wait, judged unclean at once')
    finally:
        durable._sleep, durable.OVERLAP_WAIT_S, durable.OVERLAP_POLL_S = saved


# --- config version identity reuse ---------------------------------------------------

def test_dur_config_reuse_in_memory_a_b_a():
    print('test_dur_config_reuse_in_memory_a_b_a')
    from analyzer import storage
    cs = storage.ConfigVersionStore()
    r1 = cs.update('x', 'fpA', {'v': 'A'}, 1)
    r2 = cs.update('x', 'fpB', {'v': 'B'}, 2)
    r3 = cs.update('x', 'fpA', {'v': 'A2'}, 3)
    check((r1['version'], r2['version'], r3['version']) == (1, 2, 1),
          'A -> v1, B -> v2, A again -> v1 (no v3)')
    check(r3['changed'] is True and r3['fingerprint'] == 'fpA',
          'a return to an earlier version still reports the change')
    check(cs.current('x')['version'] == 1 and
          cs.summary()['x']['version'] == 1 and
          cs.summary()['x']['fingerprint'] == 'fpA',
          'current pointer / summary reflect the reused version')
    check(len(cs.versions('x')) == 2 and
          cs.versions('x')[0]['payload'] == {'v': 'A'},
          'no duplicate version or payload stored')
    r4 = cs.update('x', 'fpA', {'v': 'A'}, 4)
    check(r4['changed'] is False and r4['version'] == 1,
          'same as current: unchanged')
    check(cs.update('x', 'fpB', {}, 5)['version'] == 2 and
          cs.current('x')['version'] == 2, 'B reused as v2')
    check(cs.update('x', 'fpC', {'v': 'C'}, 6)['version'] == 3,
          'a never-seen fingerprint gets the next number')
    check(cs.info()['by_name'] == {'x': 3}, 'info counts distinct versions')
    small = storage.ConfigVersionStore(max_versions=3)
    for i, fp in enumerate(('f1', 'f2', 'f3', 'f4')):
        small.update('y', fp, {}, i)
    check([v['version'] for v in small.versions('y')] == [2, 3, 4],
          'only the newest 3 retained')
    check(small.update('y', 'f1', {}, 9)['version'] == 5,
          'aged-out fingerprint legitimately gets a new version')
    check(small.update('y', 'f3', {}, 10)['version'] == 3 and
          small.current('y')['version'] == 3,
          'a still-retained fingerprint is reused')


def test_dur_config_reuse_persistence_and_restart():
    print('test_dur_config_reuse_persistence_and_restart')
    with _Tmp() as base:
        clock = _Clock()
        t1 = durable.TelemetryStorage(_root(base), clock=clock)
        t1.prepare()
        cs = t1.config_store
        cs.update('x', 'fpA', {'v': 'A'}, 1790380800)
        cs.update('x', 'fpB', {'v': 'B'}, 1790380801)
        r = cs.update('x', 'fpA', {'v': 'A'}, 1790380802)
        cs.update('x', 'fpB', {'v': 'B'}, 1790380803)
        r = cs.update('x', 'fpA', {'v': 'A'}, 1790380804)
        lines = _read_lines(t1.config_path)
        check(r['version'] == 1 and len(lines) == 2 and
              [(l['version'], l['fingerprint']) for l in lines] ==
              [(1, 'fpA'), (2, 'fpB')],
              'file holds one record per distinct version (2), not per '
              'change')
        size = os.path.getsize(t1.config_path)
        t2 = durable.TelemetryStorage(_root(base), clock=clock)
        t2.prepare()
        t2.config_store.load()
        r = t2.config_store.update('x', 'fpA', {'v': 'A'}, 1790380900)
        check(r['version'] == 1 and os.path.getsize(t2.config_path) == size
              and len(_read_lines(t2.config_path)) == 2,
              'restart: A reuses v1, no new line appended')
        check(t2.config_store.current('x')['version'] == 1,
              'current pointer follows the reused version after restart')
        r = t2.config_store.update('x', 'fpB', {'v': 'B'}, 1790380901)
        check(r['version'] == 2 and len(_read_lines(t2.config_path)) == 2,
              'restart: B reuses v2')
        r = t2.config_store.update('x', 'fpC', {'v': 'C'}, 1790380902)
        check(r['version'] == 3 and len(_read_lines(t2.config_path)) == 3,
              'restart: a new fingerprint continues the numbering (v3)')
        # Compaction keeps version numbers + fingerprints; reuse still works.
        saved = durable.CONFIG_COMPACT_BYTES
        durable.CONFIG_COMPACT_BYTES = 3000
        try:
            for i in range(40):
                t2.config_store.update('big', 'g%d' % i,
                                       {'pad': 'z' * 150}, 1790381000 + i)
        finally:
            durable.CONFIG_COMPACT_BYTES = saved
        t3 = durable.TelemetryStorage(_root(base), clock=clock)
        t3.prepare()
        t3.config_store.load()
        vs = t3.config_store.versions('big')
        check(len(vs) <= 20 and vs[-1]['version'] == 40 and
              vs[-1]['fingerprint'] == 'g39', 'compaction preserved numbers '
              'and fingerprints')
        n = len(_read_lines(t3.config_path))
        r = t3.config_store.update('big', vs[3]['fingerprint'], {}, 1)
        check(r['version'] == vs[3]['version'] and
              len(_read_lines(t3.config_path)) == n,
              'a retained version is still reusable after compaction')
        r = t3.config_store.update('big', 'g0', {}, 2)
        check(r['version'] == 41, 'compacted-away fingerprint is not '
              'searched: gets a new version')


def test_dur_config_reuse_through_service():
    print('test_dur_config_reuse_through_service')
    with _Tmp() as base:
        clock = _Clock()
        with _fake_collectors() as fc:
            svc = _mk(base, clock)
            svc.collect_startup()
            a_intent = copy.deepcopy(fixtures.IDENTITIES_INTENT)
            a_cfg = copy.deepcopy(fixtures.TRAFFIC_CLASS_CFG)
            b_intent = copy.deepcopy(a_intent)
            list(b_intent.values())[0]['name'] = 'Partial boot class'
            b_cfg = copy.deepcopy(a_cfg)
            list(b_cfg.values())[0]['dscp'] = 12
            fc.overrides.update(identities_intent=b_intent,
                                traffic_class_config=b_cfg)
            ob = svc.collect('manual', collectors.CONFIG_KEYS)
            fc.overrides.update(identities_intent=a_intent,
                                traffic_class_config=a_cfg)
            oa = svc.collect('manual', collectors.CONFIG_KEYS)
        tc = svc._config_store
        cvb = ob['config_versions']['traffic_classes']
        cva = oa['config_versions']['traffic_classes']
        check((cvb['version'], cvb['changed']) == (2, True) and
              (cva['version'], cva['changed']) == (1, True),
              'A -> v1, partial B -> v2, A again -> v1 (observations)')
        check(svc.status()['config_versions']['traffic_classes'][
            'version'] == 1 and tc.current('traffic_classes')['version'] ==
            1, 'status reflects the reused version')
        n = sum(1 for r in _read_lines(os.path.join(
            _root(base), 'config_versions.jsonl'))
            if r['name'] == 'traffic_classes')
        check(n == 2, 'durable file: 2 traffic_classes records, no dup')
        s2 = _mk(base, clock)
        with _fake_collectors():
            o = s2.collect('manual', collectors.CONFIG_KEYS)
        check(o['config_versions']['traffic_classes']['version'] == 1 and
              sum(1 for r in _read_lines(os.path.join(
                  _root(base), 'config_versions.jsonl'))
                  if r['name'] == 'traffic_classes') == 2,
              'after a restart A still maps to v1 with no new record')


# --- shutdown never recreates storage; init failure visibility ----------------------

def test_dur_shutdown_never_recreates_removed_root():
    print('test_dur_shutdown_never_recreates_removed_root')
    with _Tmp() as base:
        clock = _Clock()
        svc = _mk(base, clock)
        with _fake_collectors():
            svc.collect_startup()
            job = svc.start_recording('up', max_duration_sec=600)[
                'recording']
            _run(svc, clock, 6)
        root = _root(base)
        check(os.path.isdir(root), 'telemetry tree exists before the upgrade')
        shutil.rmtree(root)                      # installer removes it
        svc.shutdown()                           # old process still stopping
        check(not os.path.exists(root),
              'shutdown did NOT recreate tmp/telemetry (or any child)')
        check(svc._durable.state_owned is False and
              svc._durable.closing is True,
              'clean-state write skipped; shutdown completed safely')
        check(svc.get_recording(job['job_id'])['state'] == 'stopped',
              'in-memory Recording still ended as stopped/app_shutdown')
    with _Tmp() as base:
        clock = _Clock()
        svc = _mk(base, clock)
        svc._store.append(_obs(1790380800))
        root = _root(base)
        shutil.rmtree(root)
        svc.begin_shutdown()                     # SIGTERM arrived
        svc._store.append(_obs(1790380900))      # in-flight collection
        svc._config_store.update('x', 'fp', {'a': 1}, 1790380900)
        svc._durable.log_event('late_event')
        svc._durable.retention_sweep('late')
        check(not os.path.exists(root),
              'after SIGTERM no history/config/event write recreates the '
              'root')
        check(len(svc.history(limit=None)) == 2 and
              svc._config_store.current('x')['version'] == 1,
              'in-memory state keeps working')
    with _Tmp() as base:
        clock = _Clock()
        svc = _mk(base, clock)
        import sdwan_analyzer
        saved = (sdwan_analyzer._service, sdwan_analyzer._running)
        try:
            sdwan_analyzer._service = svc
            sdwan_analyzer._handle_signal(15, None)
            check(svc._durable.closing is True and
                  sdwan_analyzer._running is False,
                  'SIGTERM handler marks storage as closing immediately')
        finally:
            sdwan_analyzer._service, sdwan_analyzer._running = saved
    with _Tmp() as base:
        svc = _mk(base, _Clock())
        svc.shutdown()
        st = _state(base)
        check(st['clean_stop'] is True and st['run_id'] ==
              svc._durable.run_id, 'normal shutdown (root exists, '
              'run_id matches) still writes the clean marker')
    with _Tmp() as base:
        a = _mk(base, _Clock())
        b = _mk(base, _Clock())
        a.shutdown()
        st = _state(base)
        check(st['run_id'] == b._durable.run_id and st['running'] is True,
              'root exists but a newer run_id owns state: not overwritten')


def test_dur_init_failure_visibility():
    print('test_dur_init_failure_visibility')
    with _Tmp() as base:
        svc = _mk(base, _Clock())
        p = svc.storage_report()['persistent']
        check(p['enabled'] is True and p['status'] == 'ok' and
              p['init_error'] is None and p['init_error_type'] is None and
              p['write_errors'] == 0, 'healthy durable: no init_error')
    with _Tmp() as base:
        with open(os.path.join(base, 'tmp'), 'w') as f:
            f.write('blocker')
        clock = _Clock()
        svc = _mk(base, clock)
        rep = svc.storage_report()
        p = rep['persistent']
        check(p['enabled'] is False and p['status'] == 'disabled' and
              p['init_error'] and p['init_error_type'],
              'init failure: disabled + reason exposed ({}: {})'.format(
                  p['init_error_type'], p['init_error']))
        check(base not in json.dumps(rep),
              'no absolute filesystem paths in the report')
        with _fake_collectors():
            obs = svc.collect_startup()
            job = svc.start_recording('f', max_duration_sec=30)['recording']
            _run(svc, clock, 8)
        check(obs['sample_source'] == 'startup' and svc.snapshot() and
              len(svc.recording_samples(job['job_id'])) > 0 and
              svc._store.stats()['durable'] is False,
              'fallback: collection, snapshot and Recording keep working')
        port = _free_port()
        httpd = HTTPServer(('127.0.0.1', port), web._make_handler(svc))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            code, rep2 = _http_json(port, '/api/storage')
        finally:
            httpd.shutdown()
        check(code == 200 and rep2['persistent']['enabled'] is False and
              rep2['persistent']['init_error_type'] ==
              p['init_error_type'], '/api/storage exposes the init error')
    if hasattr(os, 'geteuid') and os.geteuid() != 0:
        with _Tmp() as base:
            root = _root(base)
            os.makedirs(root)
            os.chmod(root, 0o555)             # stale dir we cannot write to
            try:
                svc = _mk(base, _Clock())
                p = svc.storage_report()['persistent']
                check(svc._durable is None and p['enabled'] is False and
                      p['init_error_type'] == 'PermissionError' and
                      'tmp/telemetry' in p['init_error'] and
                      base not in p['init_error'],
                      'EACCES on a stale root: sanitized PermissionError '
                      'reported: {}'.format(p['init_error']))
            finally:
                os.chmod(root, 0o755)


# --- time-based Recording dedupe rebuild ---------------------------------------------

_T0 = 1790380800


def _srec(ts, swans=(), events=(), pad=0):
    """A persisted Recording sample carrying SWANS / steering-event keys."""
    r = {'collected_ts': ts, 'recording_group': 'swans'}
    if swans:
        r['swans_samples'] = [
            {'uid': u, 'latency_ms': 1.0, 'jitter_ms': 1.0, 'metric_ts': m,
             'collected_ts': ts, 'timestamp_source': 'ncos'}
            for u, m in swans]
    if events:
        r['steering_events'] = [
            {'steering_rule': rule, 'reason': 'r', 'wan_from_uid': None,
             'wan_to_uid': 'w', 'metric_ts': m, 'collected_ts': ts,
             'timestamp_source': 'ncos'} for rule, m in events]
    if pad:
        r['pad'] = 'x' * pad
    return r


def _synth_job(base, start, end, lines, seq=1):
    """Write a completed Recording (metadata + samples) straight to disk."""
    jid = 'rec-{}-{:03d}'.format(start, seq)
    d = os.path.join(_root(base), 'recordings', jid)
    os.makedirs(d, exist_ok=True)
    job = {'job_id': jid, 'label': '', 'notes': '', 'state': 'expired',
           'started_ts': start, 'ended_ts': end, 'max_duration_sec': 1800,
           'sample_profile': 'default_v1', 'created_by_ui': False,
           'stop_reason': 'max_duration', 'collector_error_count': 0,
           'collector_health': {}, 'sample_counts': {}, 'events': [],
           'config_versions_start': None, 'config_versions_end': None,
           'config_changes': [], 'sample_count': len(lines)}
    P.write_json_atomic(os.path.join(d, 'metadata.json'), job)
    path = os.path.join(d, 'samples.jsonl')
    with open(path, 'wb') as f:
        for ln in lines:
            f.write(ln if isinstance(ln, bytes) else
                    P.dumps(ln).encode() + b'\n')
    return jid, path


def _swk(uid, m):
    return (uid, m)


def _evk(rule, m):
    return (rule, m, 'r', None, 'w')


def test_dur_dedupe_recording_large_file_time_window():
    print('test_dur_dedupe_recording_large_file_time_window')
    with _Tmp() as base:
        end = _T0 + 600
        lines = [_srec(_T0 + i * 5, swans=[('pathA', _T0 + i * 5 - 1)],
                       events=[('ruleA', _T0 + i * 5 - 2)], pad=5000)
                 for i in range(121)]
        jid, path = _synth_job(base, _T0, end, lines)
        size = os.path.getsize(path)
        check(size > 500000 and len(P.read_tail_records(path, 65536)) < 20,
              'synthetic Recording is {} B; a 64 KB tail would cover only '
              '{} of 121 samples'.format(
                  size, len(P.read_tail_records(path, 65536))))
        svc = _mk(base, _Clock(end + 60))
        sw = set(svc._swans_seen._keys)
        ev = set(svc._event_seen._keys)
        want_sw = {_swk('pathA', _T0 + i * 5 - 1) for i in range(121)}
        want_ev = {_evk('ruleA', _T0 + i * 5 - 2) for i in range(121)}
        check(sw == want_sw,
              'ALL 121 SWANS keys rebuilt, including those far outside the '
              'final 64 KB ({} of {})'.format(len(sw & want_sw), 121))
        check(ev == want_ev, 'ALL 121 steering-event keys rebuilt')
        check(svc._swans_seen.add(('pathA', _T0 - 1)) is False and
              svc._event_seen.add(('ruleA', _T0 - 2, 'r', None, 'w'))
              is False, 'the very first keys of the window dedupe correctly')
        rec = svc._durable.recovery
        check(rec['dedupe_swans'] == 121 and rec['dedupe_steering'] == 121
              and rec['dedupe_recording_capped'] is False and
              rec['dedupe_skipped_lines'] == 0,
              'recovery summary: counts, not capped, nothing skipped')


def test_dur_dedupe_recording_time_cutoff_and_bounded_scan():
    print('test_dur_dedupe_recording_time_cutoff_and_bounded_scan')
    with _Tmp() as base:
        n = 300                       # 1500 s of samples, 5 s apart
        newest = _T0 + (n - 1) * 5
        lines = [_srec(_T0 + i * 5, swans=[('p', _T0 + i * 5)], pad=2000)
                 for i in range(n)]
        jid, path = _synth_job(base, _T0, newest, lines)
        size = os.path.getsize(path)
        svc = _mk(base, _Clock(newest + 30))
        keys = svc._swans_seen._keys
        cutoff = newest - 600
        check(('p', cutoff) in keys and ('p', newest) in keys and
              ('p', cutoff - 5) not in keys and ('p', _T0) not in keys,
              'samples at/after newest-600 s are rebuilt; older are ignored')
        check(len(keys) == 121, 'exactly the 10-minute window ({} keys)'
              .format(len(keys)))
        scanned = svc._durable.recovery['dedupe_recording_scanned_bytes']
        check(0 < scanned < size * 0.6,
              'startup scan stayed bounded: {} of {} bytes'.format(
                  scanned, size))
        # direct function checks
        recs, info = P.read_recent_window(path, 600, 4 << 20)
        check(len(recs) == 121 and recs[0]['collected_ts'] == cutoff and
              recs[-1]['collected_ts'] == newest and not info['capped'],
              'read_recent_window: oldest-first, inclusive cutoff')
        recs, info = P.read_recent_window(path, 600, 4 << 20,
                                          cutoff_ts=newest - 100)
        check(len(recs) == 21, 'an explicit shared cutoff overrides')
        check(P.read_recent_window(os.path.join(base, 'none'), 600, 1 << 20)
              == ([], {'scanned_bytes': 0, 'skipped': 0, 'capped': False,
                       'newest_ts': None}), 'missing file is empty')


def test_dur_dedupe_recording_4mib_cap():
    print('test_dur_dedupe_recording_4mib_cap')
    with _Tmp() as base:
        n = 1500                      # ~6 MB, all within 300 s: horizon never
        newest = _T0 + 300            # reached, so only the byte cap stops it
        lines = [_srec(_T0 + (i * 300) // n,
                       swans=[('p%d' % (i % 3), 1790000000 + i)], pad=4000)
                 for i in range(n)]
        jid, path = _synth_job(base, _T0, newest, lines)
        size = os.path.getsize(path)
        check(size > 5.5 * 1024 * 1024, 'file is {} B (> cap)'.format(size))
        svc = _mk(base, _Clock(newest + 30))
        rec = svc._durable.recovery
        check(rec['dedupe_recording_scanned_bytes'] <= 4 * 1024 * 1024 and
              rec['dedupe_recording_scanned_bytes'] > 3 * 1024 * 1024 and
              rec['dedupe_recording_capped'] is True,
              'scan stopped at the 4 MiB cap ({} B read of {})'.format(
                  rec['dedupe_recording_scanned_bytes'], size))
        keys = svc._swans_seen._keys
        check(('p%d' % ((n - 1) % 3), 1790000000 + n - 1) in keys and
              ('p0', 1790000000) not in keys and 0 < len(keys) < n,
              'newest keys rebuilt, the oldest (beyond the cap) are not; '
              'no crash')
        check(svc._durable.recovery['dedupe_skipped_lines'] == 0 and
              svc.get_recording(jid)['state'] == 'expired',
              'recovery report sane; Recording metadata intact')


def test_dur_dedupe_recording_malformed_lines():
    print('test_dur_dedupe_recording_malformed_lines')
    with _Tmp() as base:
        end = _T0 + 100
        lines = [_srec(_T0 + 10, swans=[('p', 1)]),
                 b'this is not json\n',
                 _srec(_T0 + 20, swans=[('p', 2)]),
                 b'{"collected_ts":%d,"swans_samples":[{"uid":\n' % (_T0 + 30),
                 b'[1, 2, 3]\n',
                 b'{"no_timestamp":true}\n',
                 _srec(_T0 + 40, swans=[('p', 3)], events=[('r1', 3)]),
                 _srec(_T0 + 50, swans=[('p', 4)]),
                 b'{"collected_ts":%d,"swans_samples":[{"uid":"p","metr' %
                 (_T0 + 60)]                       # incomplete final line
        jid, path = _synth_job(base, _T0, end, lines)
        svc = _mk(base, _Clock(end + 30))
        keys = svc._swans_seen._keys
        check({('p', 1), ('p', 2), ('p', 3), ('p', 4)} == set(keys) and
              ('r1', 3, 'r', None, 'w') in svc._event_seen._keys,
              'valid keys before/between/after bad lines are rebuilt')
        rec = svc._durable.recovery
        check(rec['dedupe_skipped_lines'] >= 3 and
              rec['tail_repairs'] >= 1 and os.path.exists(path + '.corrupt'),
              'bad lines counted ({}); incomplete tail repaired as before'
              .format(rec['dedupe_skipped_lines']))
        check('dedupe_skipped_lines' in _event_types(base),
              'skipped lines logged as an event')
        check(svc.get_recording(jid)['sample_count'] == len(lines),
              'startup completed normally')


def test_dur_dedupe_recording_multiple_jobs_in_window():
    print('test_dur_dedupe_recording_multiple_jobs_in_window')
    with _Tmp() as base:
        newest = _T0 + 5000
        old_a = _synth_job(base, _T0, _T0 + 60,
                           [_srec(_T0 + 30, swans=[('p', 111)])], seq=1)
        # B ends 450 s before C's newest sample: inside the 600 s window.
        b_start = newest - 600
        job_b = _synth_job(base, b_start, b_start + 60,
                           [_srec(b_start + 5, swans=[('p', 222)]),
                            _srec(b_start + 55, swans=[('p', 223)])], seq=1)
        job_c = _synth_job(base, newest - 300, newest,
                           [_srec(newest - 300, swans=[('p', 333)]),
                            _srec(newest, swans=[('p', 334)])], seq=1)
        svc = _mk(base, _Clock(newest + 60))
        keys = set(svc._swans_seen._keys)
        check({('p', 222), ('p', 223), ('p', 333), ('p', 334)} <= keys and
              ('p', 111) not in keys,
              'back-to-back Recordings inside the window all contribute; a '
              'long-finished one does not')
    with _Tmp() as base:
        _synth_job(base, _T0, _T0 + 60,
                   [_srec(_T0 + 30, swans=[('p', 1)])])
        svc = _mk(base, _Clock(_T0 + 60 + 1801 + 5))
        check(len(svc._swans_seen) == 0,
              'a Recording that ended > 30 min ago is not scanned at all')


def test_dur_dedupe_history_tail_unchanged():
    print('test_dur_dedupe_history_tail_unchanged')
    with _Tmp() as base:
        clock = _Clock()
        with _fake_collectors():
            s1 = _mk(base, clock)
            s1.collect_startup()
            s2 = _mk(base, clock)
        check(set(s2._swans_seen._keys) == set(s1._swans_seen._keys) and
              len(s2._swans_seen) == 4 and
              s2._durable.recovery['dedupe_recording_scanned_bytes'] == 0,
              'history-only rebuild still exact; no Recording scanned')


TESTS = [
    test_dur_atomic_json,
    test_dur_jsonl_appender_flush_and_fsync,
    test_dur_jsonl_tail_repair,
    test_dur_jsonl_readers,
    test_dur_job_id_validation,
    test_dur_observation_codec,
    test_dur_config_versions_restart,
    test_dur_config_compaction_and_bak,
    test_dur_history_utc_day_files_and_ring,
    test_dur_history_insane_clock,
    test_dur_history_query_paging,
    test_dur_history_retention_age_and_size,
    test_dur_history_retention_default_profile,
    test_dur_recording_samples_on_disk,
    test_dur_recording_expiry_and_failure_metadata,
    test_dur_recording_restart_interrupted,
    test_dur_recording_graceful_shutdown,
    test_dur_recording_isolation_and_corruption,
    test_dur_recording_retention,
    test_dur_dedupe_rebuild_from_tails,
    test_dur_write_failure_degrades_not_stops,
    test_dur_state_json_exit_tracking,
    test_dur_events_rotation,
    test_dur_unwritable_root_falls_back_to_memory,
    test_dur_storage_inspector_persistent,
    test_dur_public_api_compat,
    test_dur_state_ownership_normal_lifecycle,
    test_dur_state_ownership_overlapping_restart,
    test_dur_state_previous_instance_wait,
    test_dur_config_reuse_in_memory_a_b_a,
    test_dur_config_reuse_persistence_and_restart,
    test_dur_config_reuse_through_service,
    test_dur_shutdown_never_recreates_removed_root,
    test_dur_init_failure_visibility,
    test_dur_dedupe_recording_large_file_time_window,
    test_dur_dedupe_recording_time_cutoff_and_bounded_scan,
    test_dur_dedupe_recording_4mib_cap,
    test_dur_dedupe_recording_malformed_lines,
    test_dur_dedupe_recording_multiple_jobs_in_window,
    test_dur_dedupe_history_tail_unchanged,
]
