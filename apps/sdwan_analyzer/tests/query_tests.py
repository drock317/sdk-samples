"""Durable history / Recording query API tests (v0.4.4).

Run through run_tests.py, which binds its `check` function and helpers into
this module (see durable_tests.py for the same arrangement). Every test uses
a private temp directory as the app root. Pure standard library.
"""
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from urllib.parse import urlencode

import cp
from analyzer import durable, persistence as P, query as Q, web
from analyzer.service import AnalyzerService
import durable_tests as D

check = None            # bound by run_tests.bind()
_Clock = None
_fake_collectors = None
_free_port = None

MIB = 1024 * 1024


def bind(check_fn, clock_cls, fake_cls, free_port_fn, http_json_fn):
    global check, _Clock, _fake_collectors, _free_port
    check = check_fn
    _Clock = clock_cls
    _fake_collectors = fake_cls
    _free_port = free_port_fn


# --- helpers ---------------------------------------------------------------------

D0 = 1790380800                        # a UTC midnight (2026-09-26)


def _svc(base, clock, retention=None):
    return D._mk(base, clock, retention)


def _obs(ts, source='scheduled', pad=0):
    o = D._obs(ts, source)
    if pad:
        o['pad'] = 'x' * pad
    return o


def _hist_path(base, ts):
    return os.path.join(D._root(base), 'history',
                        durable.utc_day(ts) + '.jsonl')


def _qs(**kw):
    return urlencode({k: v for k, v in kw.items() if v is not None})


def _hist(svc, **kw):
    return svc.query_history(_qs(**kw))


def _status(fn):
    """HTTP status of the QueryError raised by fn(), else None."""
    try:
        fn()
    except Q.QueryError as e:
        return e.status
    return None


def _drain_history(svc, **kw):
    """Follow next_cursor to the end; return (timestamps, pages, skipped)."""
    out, pages, skipped, cur = [], 0, 0, None
    while True:
        r = _hist(svc, cursor=cur, **kw)
        out.extend(s['collected_ts'] for s in r['samples'])
        skipped += r['skipped_lines']
        pages += 1
        cur = r['next_cursor']
        if cur is None or pages > 400:
            return out, pages, skipped


def _drain_rec(svc, jid, **kw):
    out, pages, skipped, cur = [], 0, 0, None
    while True:
        r = svc.query_recording_samples(jid, _qs(cursor=cur, **kw))
        out.extend(r['samples'])
        skipped += r['skipped_lines']
        pages += 1
        cur = r['next_cursor']
        if cur is None or pages > 400:
            return out, pages, skipped


def _get(port, path):
    try:
        with urllib.request.urlopen(
                'http://127.0.0.1:{}{}'.format(port, path), timeout=10) as r:
            return r.status, json.loads(r.read().decode('utf-8')), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8')), e.headers


def _tree(base):
    """(relative path, size, mtime_ns) of every file under the app root."""
    out = []
    for dp, _, names in os.walk(base):
        for n in names:
            p = os.path.join(dp, n)
            st = os.stat(p)
            out.append((os.path.relpath(p, base), st.st_size, st.st_mtime_ns))
    return sorted(out)


def _record_job(svc, clock, label, secs):
    return D._record(svc, clock, label, secs)


# --- scanner hardening (persistence.scan_range / read_range) ---------------------------

def test_q_scan_range_counts_every_byte_examined():
    print('test_q_scan_range_counts_every_byte_examined')
    with D._Tmp() as d:
        p = os.path.join(d, 'x.jsonl')
        with open(p, 'wb') as f:
            for i in range(2000):                       # filtered out
                f.write(P.dumps({'collected_ts': 100 + i, 'p': 'a' * 90})
                        .encode() + b'\n')
            for i in range(500):                        # malformed
                f.write(b'not json ' + b'z' * 90 + b'\n')
            f.write(P.dumps({'collected_ts': 99999}).encode() + b'\n')
        size = os.path.getsize(p)
        res = P.scan_range(p, 0, start_ts=99999, max_bytes=100000)
        check(res['scanned_bytes'] <= 100000 and res['records'] == [] and
              not res['eof'], 'filtered-out lines count against the budget')
        check(0 < res['next_offset'] <= 100000, 'scan stops on a boundary')
        check(P.offset_state(p, res['next_offset']) == 'ok',
              'resume offset is on a record boundary')
        # walk the whole file in 100 KB pages: every byte examined once
        total, off, pages, got, skipped = 0, 0, 0, [], 0
        while True:
            res = P.scan_range(p, off, start_ts=99999, max_bytes=100000)
            total += res['scanned_bytes']
            skipped += res['skipped']
            got.extend(res['records'])
            off = res['next_offset']
            pages += 1
            if res['eof'] or pages > 100:
                break
        # Bytes read for a line the budget cut short are counted (they were
        # read) and re-read next page, so total >= size, never less.
        check(total >= size and total <= size + pages * 3000 and
              len(got) == 1 and skipped == 500,
              'pages cover every byte ({} of {}), malformed counted'
              .format(total, size))
        # legacy wrapper keeps its contract and now reports scanned bytes
        st = {}
        recs, nxt, eof = P.read_range(p, 100, 120, max_bytes=3000, stats=st)
        check(len(recs) == 21 and st['scanned_bytes'] <= 3000 + 200,
              'read_range reports scanned bytes ({})'.format(
                  st.get('scanned_bytes')))
        # incomplete tail is not consumed and ends the scan
        with open(p, 'ab') as f:
            f.write(b'{"collected_ts":123456,"x"')
        res = P.scan_range(p, size, max_bytes=MIB)
        check(res['records'] == [] and res['eof'] and
              res['next_offset'] == size, 'incomplete trailing line ignored')


def test_q_scan_range_oversized_records():
    print('test_q_scan_range_oversized_records')
    with D._Tmp() as d:
        p = os.path.join(d, 'x.jsonl')
        good = lambda ts: P.dumps({'collected_ts': ts}).encode() + b'\n'
        with open(p, 'wb') as f:
            f.write(good(1))
            f.write(b'{"collected_ts":2,"pad":"' + b'x' * 600000 + b'"}\n')
            f.write(good(3))
            f.write(b'{"collected_ts":4,"pad":"' + b'y' * (MIB + MIB // 2))
            f.write(b'"}\n')
            f.write(good(5))
        off, resync, got, pages, skipped, scanned = 0, False, [], 0, 0, []
        while True:
            res = P.scan_range(p, off, max_bytes=300000, resync=resync)
            scanned.append(res['scanned_bytes'])
            got.extend(r['collected_ts'] for r in res['records'])
            skipped += res['skipped']
            off, resync = res['next_offset'], res['resync']
            pages += 1
            if res['eof'] or pages > 40:
                break
        check(got == [1, 3, 5], 'oversized records never served ({})'.format(
            got))
        check(skipped == 2, 'each oversized record counted once ({})'.format(
            skipped))
        check(max(scanned) <= 300000, 'budget respected through oversized '
              'lines (max {})'.format(max(scanned)))
        check(pages < 40, 'terminates ({} pages)'.format(pages))
        # an unterminated oversized tail at EOF: no endless resync
        with open(p, 'ab') as f:
            f.write(b'{"collected_ts":6,"pad":"' + b'z' * 400000)
        res = P.scan_range(p, os.path.getsize(p) - 400000 - 26, max_bytes=MIB)
        check(res['eof'] and res['records'] == [],
              'unterminated oversized tail ends cleanly')


# --- parameter validation and envelope ----------------------------------------------------

def test_q_parameter_validation():
    print('test_q_parameter_validation')
    with D._Tmp() as base:
        clock = _Clock()
        svc = _svc(base, clock)
        svc._store.append(_obs(clock.t - 10))
        bad = ['limit=0', 'limit=251', 'limit=-1', 'limit=abc', 'limit=1.5',
               'limit=', 'start=abc', 'start=-5', 'end=1e9', 'start=99999999999',
               'start=200&end=100', 'sample_source=recording',
               'sample_source=SCHEDULED', 'sample_source=', 'cursor=!!',
               'cursor=' + 'a' * 600, 'bogus=1', 'limit=1&limit=2',
               'start=1&start=2', 'cursor=',
               'sample_source=manual&sample_source=startup']
        for q in bad:
            check(_status(lambda q=q: svc.query_history(q)) == 400,
                  'history rejects ?' + q[:40])
        r = svc.query_history('limit=250&sample_source=scheduled')
        check(r['count'] == 1 and set(r) == {
            'samples', 'count', 'next_cursor', 'has_more', 'skipped_lines'},
            'valid query returns exactly the documented envelope')
        check(r['has_more'] is False and r['next_cursor'] is None,
              'no more data -> has_more false, cursor null')
        r = svc.query_history('start=1.5&end=%d.25' % (clock.t + 5))
        check(r['count'] == 1, 'decimal timestamps accepted')
        with _fake_collectors():
            jid = _record_job(svc, clock, 'v', 20)
        for q in ['limit=0', 'limit=251', 'group=nope', 'group=', 'after=x',
                  'after=-1', 'start=1', 'bogus=1', 'cursor=@@', 'group=wan&group=qoe']:
            check(_status(lambda q=q: svc.query_recording_samples(jid, q))
                  == 400, 'samples reject ?' + q[:40])
        check(svc.query_recording_samples(jid, 'group=wan&limit=2')['count']
              == 2, 'valid group filter accepted')


# --- history ------------------------------------------------------------------------

def test_q_history_multi_day_pagination_and_default_window():
    print('test_q_history_multi_day_pagination_and_default_window')
    with D._Tmp() as base:
        clock = _Clock(D0 + 6 * 3600)
        svc = _svc(base, clock)
        # 4 UTC days (12 observations per day, 2 h apart) + today
        stamps = [D0 - 4 * 86400 + i * 7200 for i in range(48)]
        stamps += [D0 + i * 3600 for i in range(7)]            # today
        for ts in stamps:
            svc._store.append(_obs(ts))
        files = svc._store.day_files()
        check(len(files) == 5, 'history spans 5 UTC day files ({})'.format(
            len(files)))
        # no explicit bounds: the last 24 h only
        lo = clock.t - 86400
        want = [t for t in stamps if t >= lo]
        got, pages, _ = _drain_history(svc, limit=5)
        check(got == want, 'default window = last 24 h ({} samples)'.format(
            len(want)))
        # explicit range covers the whole retained window, across day files
        got, pages, skipped = _drain_history(svc, start=stamps[0],
                                             end=stamps[-1], limit=7)
        check(got == stamps and skipped == 0,
              'explicit range pages across UTC day files')
        check(len(got) == len(set(got)), 'no duplicates')
        for lim in (1, 2, 11, 12, 13, 250):
            g, _, _ = _drain_history(svc, start=stamps[0], limit=lim)
            check(g == stamps, 'limit={} pages with no omission or '
                  'duplicate'.format(lim))
        # a cursor taken exactly at a day boundary
        r = _hist(svc, start=stamps[0], limit=12)
        check(r['has_more'] and r['count'] == 12, 'first day page full')
        r2 = _hist(svc, start=stamps[0], limit=12, cursor=r['next_cursor'])
        check([s['collected_ts'] for s in r2['samples']] == stamps[12:24],
              'cursor crosses the UTC day transition')
        # start/end only, one bound
        g, _, _ = _drain_history(svc, end=stamps[3])
        check(g == stamps[:4], 'end alone: no default window, inclusive end')
        g, _, _ = _drain_history(svc, start=stamps[-3])
        check(g == stamps[-3:], 'start alone: open end')
        # decoded observation objects, not raw JSONL
        s0 = _hist(svc, limit=1)['samples'][0]
        check(s0['sample_source'] == 'scheduled' and 'duration_ms' in s0 and
              s0['collected_ts'] == want[0] and 'cv' not in s0,
              'returns decoded normalized observations')


def test_q_history_inclusive_bounds_and_sources():
    print('test_q_history_inclusive_bounds_and_sources')
    with D._Tmp() as base:
        clock = _Clock(D0 + 3600)
        svc = _svc(base, clock)
        kinds = ['startup', 'scheduled', 'manual', 'scheduled', 'manual',
                 'scheduled', 'startup']
        stamps = [D0 + 100 + i * 60 for i in range(7)]
        for ts, k in zip(stamps, kinds):
            svc._store.append(_obs(ts, k))
        r = _hist(svc, start=stamps[2], end=stamps[4])
        check([s['collected_ts'] for s in r['samples']] == stamps[2:5],
              'start and end are both inclusive')
        r = _hist(svc, start=stamps[2] + 0.5, end=stamps[4] - 0.5)
        check([s['collected_ts'] for s in r['samples']] == [stamps[3]],
              'bounds just inside exclude the edge samples')
        r = _hist(svc, start=stamps[2], end=stamps[2])
        check(r['count'] == 1, 'start == end returns that instant')
        for k in ('scheduled', 'manual', 'startup'):
            g, _, _ = _drain_history(svc, sample_source=k, limit=1)
            want = [t for t, kk in zip(stamps, kinds) if kk == k]
            check(g == want, 'sample_source={} filter ({} samples)'.format(
                k, len(want)))
        g, _, _ = _drain_history(svc, sample_source='manual',
                                 start=stamps[3], limit=1)
        check(g == [stamps[4]], 'source + time filters combine')


def test_q_history_filter_pages_with_empty_intermediate_pages():
    print('test_q_history_filter_pages_with_empty_intermediate_pages')
    with D._Tmp() as base:
        clock = _Clock(D0 + 3600)
        svc = _svc(base, clock)
        stamps = [D0 + 10 + i * 5 for i in range(120)]
        for i, ts in enumerate(stamps):
            svc._store.append(_obs(ts, 'manual' if i in (100, 101, 118)
                                   else 'scheduled', pad=40000))
        pages, cur, got, empties = 0, None, [], 0
        while True:
            r = _hist(svc, sample_source='manual', cursor=cur)
            pages += 1
            if r['count'] == 0 and r['has_more']:
                empties += 1
            got.extend(s['collected_ts'] for s in r['samples'])
            check(r['count'] <= 250, 'page within the record ceiling')
            cur = r['next_cursor']
            if cur is None or pages > 50:
                break
        check(got == [stamps[100], stamps[101], stamps[118]],
              'every filtered match found across pages')
        check(empties >= 2, 'empty intermediate pages with has_more true '
              '({} empty of {})'.format(empties, pages))
        # time-filter variant: window is late in a big day file
        g, p2, _ = _drain_history(svc, start=stamps[110])
        check(g == stamps[110:], 'late window found despite scanning past '
              'earlier data')


def test_q_history_cursor_rejection():
    print('test_q_history_cursor_rejection')
    with D._Tmp() as base:
        clock = _Clock(D0 + 5 * 3600)
        svc = _svc(base, clock)
        stamps = [D0 - 86400 + i * 3600 for i in range(30)]
        for ts in stamps:
            svc._store.append(_obs(ts))
        first = _hist(svc, start=stamps[0], limit=4)
        cur = first['next_cursor']
        check(cur and Q._TOKEN_RE.match(cur), 'cursor is an opaque URL-safe '
              'token')
        check(_status(lambda: _hist(svc, start=stamps[0], limit=4,
                                    cursor=cur)) is None,
              'valid cursor accepted')
        garbage = ['abcd', 'e30', '%%%', 'A' * 100, cur[:-4], cur + 'AA',
                   cur[::-1]]
        for g in garbage:
            tok = g if Q._TOKEN_RE.match(g) else 'x'
            check(_status(lambda t=tok: _hist(svc, start=stamps[0],
                                              cursor=t)) == 400,
                  'malformed cursor rejected: {}'.format(tok[:16]))
        # mismatched query parameters
        check(_status(lambda: _hist(svc, start=stamps[1], cursor=cur)) == 400,
              'different start rejected')
        check(_status(lambda: _hist(svc, start=stamps[0], end=stamps[-1],
                                    cursor=cur)) == 400,
              'added end rejected')
        check(_status(lambda: _hist(svc, start=stamps[0],
                                    sample_source='manual', cursor=cur))
              == 400, 'different sample_source rejected')
        check(_status(lambda: _hist(svc, cursor=cur)) == 400,
              'dropping start rejected')
        check(_status(lambda: _hist(svc, start=stamps[0], limit=9,
                                    cursor=cur)) is None,
              'limit may change between pages')
        # a recording cursor is not a history cursor
        with _fake_collectors():
            jid = _record_job(svc, clock, 'x', 20)
        rcur = svc.query_recording_samples(jid, 'limit=2')['next_cursor']
        check(_status(lambda: _hist(svc, start=stamps[0], cursor=rcur))
              == 400, 'recording cursor rejected by /api/history')
        check(_status(lambda: svc.query_recording_samples(
            jid, _qs(cursor=cur))) == 400,
              'history cursor rejected by recording samples')
        # forged but well-formed tokens (valid integrity field)
        ctx = Q.context_hash('h', float(stamps[0]), None, None)
        day = durable.utc_day(stamps[0])
        path = os.path.join(D._root(base), 'history', day + '.jsonl')
        size = os.path.getsize(path)

        def forge(**over):
            doc = {'t': 'h', 'q': ctx, 'd': day, 'o': 0, 's': float(stamps[0]),
                   'r': False}
            doc.update(over)
            return Q.encode_cursor(doc)
        check(_status(lambda: _hist(svc, start=stamps[0],
                                    cursor=forge())) is None,
              'forged cursor at offset 0 is a legitimate position')
        for label, tok in (
                ('mid-line offset', forge(o=5)),
                ('offset beyond the file', forge(o=size + 10)),
                ('negative offset', forge(o=-1)),
                ('boolean offset', forge(o=True)),
                ('string offset', forge(o='7')),
                ('traversal day', forge(d='../../etc/passwd')),
                ('slash day', forge(d='2026-09-25/../x')),
                ('impossible date', forge(d='2026-13-45')),
                ('day before the range', forge(d='2020-01-01')),
                ('day after the range', forge(d='2099-01-01')),
                ('wrong kind', Q.encode_cursor({'t': 'r', 'q': ctx, 'o': 0})),
        ):
            check(_status(lambda t=tok: _hist(svc, start=stamps[0],
                                              cursor=t)) == 400,
                  'forged cursor rejected: ' + label)
        # retention removed the segment between pages -> 410, never a restart
        r = _hist(svc, start=stamps[0], limit=4)
        os.remove(path)
        check(_status(lambda: _hist(svc, start=stamps[0], limit=4,
                                    cursor=r['next_cursor'])) == 410,
              'cursor into a removed day file is 410 (not a silent restart)')
        check(_status(lambda: _hist(svc, start=stamps[0])) is None,
              'the same query without a cursor still works')


def test_q_history_malformed_and_incomplete_lines():
    print('test_q_history_malformed_and_incomplete_lines')
    with D._Tmp() as base:
        clock = _Clock(D0 + 3600)
        svc = _svc(base, clock)
        day_ts = D0 - 3 * 86400
        path = os.path.join(D._root(base), 'history',
                            durable.utc_day(day_ts) + '.jsonl')
        enc = lambda ts: P.dumps(P.encode_observation(_obs(ts))).encode()
        with open(path, 'wb') as f:
            f.write(enc(day_ts + 1) + b'\n')
            f.write(b'this is not json\n')
            f.write(b'{"collected_ts":' + str(day_ts + 2).encode() + b',bad\n')
            f.write(b'{"no_ts":1}\n')
            f.write(b'[1,2,3]\n')
            f.write(b'\n')
            f.write(enc(day_ts + 3) + b'\n')
            f.write(enc(day_ts + 4)[:30])                    # incomplete tail
        r = _hist(svc, start=day_ts, end=day_ts + 100)
        check([s['collected_ts'] for s in r['samples']] ==
              [day_ts + 1, day_ts + 3], 'valid records returned around '
              'malformed lines')
        check(r['skipped_lines'] == 4, 'malformed lines counted in '
              'skipped_lines ({})'.format(r['skipped_lines']))
        check(r['has_more'] is False and r['next_cursor'] is None,
              'incomplete trailing line is not a record and not "more"')
        # a complete line appended later is picked up by a fresh query
        with open(path, 'ab') as f:
            f.write(b'\n')              # terminates the fragment: now bad
            f.write(enc(day_ts + 5) + b'\n')
        r = _hist(svc, start=day_ts, end=day_ts + 100)
        check([s['collected_ts'] for s in r['samples']][-1] == day_ts + 5 and
              r['skipped_lines'] == 5, 'completed lines are read; the '
              'torn fragment is skipped')
        # limit pages: skipped lines are reported across pages, no loss
        g, pages, skipped = _drain_history(svc, start=day_ts,
                                           end=day_ts + 100, limit=1)
        check(g == [day_ts + 1, day_ts + 3, day_ts + 5] and skipped == 5,
              'skips and records are consistent when paging')


def test_q_history_resource_ceilings():
    print('test_q_history_resource_ceilings')
    with D._Tmp() as base:
        clock = _Clock(D0 + 3600)
        svc = _svc(base, clock)
        st = svc._store
        stamps = [D0 + 10 + i for i in range(300)]
        for ts in stamps:
            st.append(_obs(ts, pad=20000))                  # ~20 KB each
        size = os.path.getsize(_hist_path(base, D0))
        check(size > 5 * MIB, 'fixture file is {} MiB'.format(size // MIB))
        r = st.query(start=stamps[0], limit=250)
        check(r['scanned_bytes'] <= MIB, 'scanned {} <= 1 MiB'.format(
            r['scanned_bytes']))
        check(0 < r['count'] < 250 and r['next_cursor'] is not None,
              'page shorter than limit when the ceiling is reached '
              '({} records)'.format(r['count']))
        body = json.dumps(Q.envelope(r['samples'], r['next_cursor'], 0))
        check(len(body) < 2 * MIB, 'response body bounded ({} bytes)'.format(
            len(body)))
        got, cur, pages, worst = [], None, 0, 0
        while True:
            r = st.query(start=stamps[0], limit=250, cursor=cur)
            worst = max(worst, r['scanned_bytes'])
            got.extend(s['collected_ts'] for s in r['samples'])
            cur = r['next_cursor']
            pages += 1
            if cur is None or pages > 100:
                break
        check(got == stamps, 'ceiling-limited pages still deliver every '
              'record exactly once ({} pages)'.format(pages))
        check(worst <= MIB, 'every page within the 1 MiB scan ceiling')
        # a page that only filters out data is also bounded
        r = st.query(start=stamps[0], limit=5, sample_source='manual')
        check(r['count'] == 0 and r['scanned_bytes'] <= MIB and
              r['next_cursor'] is not None, 'filtering scan is bounded and '
              'resumable')
        # many tiny filtered-out lines: bytes, not records, are the bound
        with D._Tmp() as b2:
            s2 = _svc(b2, _Clock(D0 + 3600))
            for i in range(6000):
                s2._store.append(_obs(D0 + 10 + i * 0.01))
            r = s2._store.query(start=D0 + 10, sample_source='manual')
            check(r['scanned_bytes'] <= MIB and r['count'] == 0,
                  'tiny-record scan bounded ({} bytes)'.format(
                      r['scanned_bytes']))


def test_q_history_oversized_and_malformed_runs():
    print('test_q_history_oversized_and_malformed_runs')
    with D._Tmp() as base:
        clock = _Clock(D0 + 3600)
        svc = _svc(base, clock)
        day_ts = D0 - 2 * 86400
        path = os.path.join(D._root(base), 'history',
                            durable.utc_day(day_ts) + '.jsonl')
        enc = lambda ts: P.dumps(P.encode_observation(_obs(ts))).encode()
        with open(path, 'wb') as f:
            f.write(enc(day_ts + 1) + b'\n')
            for _ in range(3000):                          # ~3 MiB of junk
                f.write(b'garbage ' * 128 + b'\n')
            f.write(b'{"collected_ts":%d,"pad":"' % (day_ts + 2) +
                    b'q' * (2 * MIB) + b'"}\n')            # oversized record
            f.write(enc(day_ts + 3) + b'\n')
        got, cur, pages, worst, skipped = [], None, 0, 0, 0
        while True:
            r = svc._store.query(start=day_ts, end=day_ts + 50, cursor=cur)
            worst = max(worst, r['scanned_bytes'])
            skipped += r['skipped_lines']
            got.extend(s['collected_ts'] for s in r['samples'])
            cur = r['next_cursor']
            pages += 1
            if cur is None or pages > 30:
                break
        check(got == [day_ts + 1, day_ts + 3], 'valid records survive junk '
              'and an oversized record: {}'.format(got))
        check(skipped == 3001, 'junk lines + oversized record counted once '
              '({})'.format(skipped))
        check(worst <= MIB, 'no page exceeded the scan ceiling')
        check(pages <= 12, 'terminates promptly ({} pages)'.format(pages))


# --- recordings ---------------------------------------------------------------------------

def test_q_recording_group_after_and_states():
    print('test_q_recording_group_after_and_states')
    with D._Tmp() as base:
        clock = _Clock()
        svc = _svc(base, clock)
        with _fake_collectors():
            svc.collect_startup()
            done = _record_job(svc, clock, 'done', 30)
            job = svc.start_recording('stop-me', max_duration_sec=100)[
                'recording']
            D._run(svc, clock, 15)
            svc.stop_recording(job['job_id'])
            D._run(svc, clock, 3)
            stopped = job['job_id']
            job = svc.start_recording('fail-me', max_duration_sec=100)[
                'recording']
            D._run(svc, clock, 12)
            failed = job['job_id']
            svc._recording._fail(svc._rec_store.get_recording(failed),
                                 'test failure')
        states = {j: svc.get_recording(j)['state']
                  for j in (done, stopped, failed)}
        check(states[done] in ('completed', 'expired') and
              states[stopped] == 'stopped' and states[failed] == 'failed',
              'jobs ended in different states: {}'.format(states))
        for jid in (done, stopped, failed):
            truth = svc.recording_samples(jid)
            got, pages, skipped = _drain_rec(svc, jid, limit=7)
            check(len(got) == len(truth) and skipped == 0 and
                  [(o['collected_ts'], o['recording_group']) for o in got] ==
                  [(o['collected_ts'], o['recording_group']) for o in truth],
                  '{} ({}) reads back exactly, {} samples / {} pages'.format(
                      jid, states[jid], len(truth), pages))
            check(all(o['sample_source'] == 'recording' and
                      o['job_id'] == jid and 'recording_group' in o
                      for o in got), 'normalized Recording observations')
        # group filter
        truth = svc.recording_samples(done)
        for g in ('wan', 'wbond', 'steering', 'qoe', 'swans', 'config'):
            want = [o for o in truth if o['recording_group'] == g]
            got, _, _ = _drain_rec(svc, done, group=g, limit=3)
            check(len(got) == len(want) and
                  all(o['recording_group'] == g for o in got) and
                  [o['collected_ts'] for o in got] ==
                  [o['collected_ts'] for o in want],
                  'group={} filter ({} samples)'.format(g, len(want)))
        # exclusive 'after'
        ts_all = sorted({o['collected_ts'] for o in truth})
        pivot = ts_all[len(ts_all) // 2]
        want = [o for o in truth if o['collected_ts'] > pivot]
        got, _, _ = _drain_rec(svc, done, after=pivot, limit=4)
        check(len(got) == len(want) and
              all(o['collected_ts'] > pivot for o in got),
              'after is exclusive ({} samples)'.format(len(want)))
        check(_drain_rec(svc, done, after=ts_all[-1])[0] == [],
              'after the last sample -> empty')
        got, _, _ = _drain_rec(svc, done, after=pivot, group='wan')
        check(all(o['collected_ts'] > pivot and o['recording_group'] == 'wan'
                  for o in got) and got, 'after + group combine')
        # metadata is unchanged by the new routes
        meta = svc.get_recording(done)
        check(meta['sample_count'] == len(truth) and 'events' in meta,
              'recording metadata and counts unchanged')


def test_q_recording_active_append_between_pages():
    print('test_q_recording_active_append_between_pages')
    with D._Tmp() as base:
        clock = _Clock()
        svc = _svc(base, clock)
        with _fake_collectors():
            svc.collect_startup()
            job = svc.start_recording('live', max_duration_sec=600)[
                'recording']
            jid = job['job_id']
            D._run(svc, clock, 12)
            r1 = svc.query_recording_samples(jid, 'limit=4')
            check(r1['count'] == 4 and r1['has_more'], 'first page of an '
                  'active Recording')
            D._run(svc, clock, 20)                       # more samples land
            seen = list(r1['samples'])
            cur = r1['next_cursor']
            pages = 0
            while cur and pages < 50:
                r = svc.query_recording_samples(
                    jid, _qs(limit=4, cursor=cur))
                seen.extend(r['samples'])
                cur = r['next_cursor']
                pages += 1
            truth = svc.recording_samples(jid)
            key = lambda o: (o['collected_ts'], o['recording_group'])
            check([key(o) for o in seen] == [key(o) for o in truth],
                  'samples appended between pages are included once, in '
                  'order ({} samples)'.format(len(truth)))
            # complete lines only: a torn tail is invisible until finished
            spath = svc._rec_store.samples_path(jid)
            n_before = len(_drain_rec(svc, jid)[0])
            with open(spath, 'ab') as f:
                f.write(b'{"collected_ts":99999999999,"recording_group":"w')
            got, _, sk = _drain_rec(svc, jid)
            check(len(got) == n_before and sk == 0,
                  'a half-written trailing line is not served or counted')
            # tailing: has_more false at the end, `after` continues
            last = got[-1]['collected_ts']
            check(svc.query_recording_samples(
                jid, _qs(after=last))['count'] == 0, 'tail poll via after')
            os.truncate(spath, os.path.getsize(spath) - 52)   # undo torn tail
            D._run(svc, clock, 12)
            more = svc.query_recording_samples(jid, _qs(after=last))
            check(more['count'] > 0 and all(
                o['collected_ts'] > last for o in more['samples']),
                'new samples are visible via after')


def test_q_recording_cursor_and_retention():
    print('test_q_recording_cursor_and_retention')
    with D._Tmp() as base:
        clock = _Clock()
        svc = _svc(base, clock, retention={'recordings_max_jobs': 1})
        with _fake_collectors():
            svc.collect_startup()
            a = _record_job(svc, clock, 'a', 20)
            cur = svc.query_recording_samples(a, 'limit=3')['next_cursor']
            check(cur is not None, 'cursor issued')
            check(_status(lambda: svc.query_recording_samples(
                a, _qs(limit=3, group='wan', cursor=cur))) == 400,
                'cursor bound to the group filter')
            check(_status(lambda: svc.query_recording_samples(
                a, _qs(limit=3, after=5, cursor=cur))) == 400,
                'cursor bound to the after filter')
            check(_status(lambda: svc.query_recording_samples(
                a, _qs(limit=3, cursor=cur))) is None, 'cursor valid')
            spath = svc._rec_store.samples_path(a)
            size = os.path.getsize(spath)
            ctx = Q.context_hash('r', a, None, None)
            for label, off in (('mid-line', 7), ('beyond end', size + 1)):
                tok = Q.encode_cursor({'t': 'r', 'q': ctx, 'o': off,
                                       'r': False})
                check(_status(lambda t=tok: svc.query_recording_samples(
                    a, _qs(cursor=t))) == 400, 'forged cursor: ' + label)
            b = _record_job(svc, clock, 'b', 20)         # sweep removes `a`
            check(svc.get_recording(a) is None and not os.path.exists(
                os.path.dirname(spath)), 'retention removed the old job')
            check(_status(lambda: svc.query_recording_samples(
                a, _qs(limit=3, cursor=cur))) == 410,
                'cursor for a retention-deleted Recording is 410')
            check(_status(lambda: svc.query_recording_samples(a, '')) == 404,
                  'same job without a cursor is 404')
            # sample file removed under a known job
            os.remove(svc._rec_store.samples_path(b))
            check(svc.query_recording_samples(b, '')['count'] == 0,
                  'known job with no sample file reads as empty')
            check(_status(lambda: svc.query_recording_samples(
                b, _qs(cursor=Q.encode_cursor({
                    't': 'r', 'q': Q.context_hash('r', b, None, None),
                    'o': 5, 'r': False})))) == 410,
                'cursor into a vanished sample file is 410')


def test_q_recording_oversized_and_junk_samples():
    print('test_q_recording_oversized_and_junk_samples')
    with D._Tmp() as base:
        clock = _Clock()
        svc = _svc(base, clock)
        with _fake_collectors():
            svc.collect_startup()
            jid = _record_job(svc, clock, 'x', 12)
        spath = svc._rec_store.samples_path(jid)
        n = len(svc.recording_samples(jid))
        with open(spath, 'ab') as f:
            f.write(b'junk\n' * 10)
            f.write(b'{"collected_ts":4000000000,"recording_group":"wan",'
                    b'"pad":"' + b'p' * (MIB + 4096) + b'"}\n')
            f.write(b'{"collected_ts":4000000001,"recording_group":"wan"}\n')
        got, pages, skipped = _drain_rec(svc, jid, limit=250)
        check(len(got) == n + 1 and skipped == 11 and
              got[-1]['collected_ts'] == 4000000001,
              'junk and an oversized sample are skipped, later samples '
              'still served ({} + 1, skipped {})'.format(n, skipped))
        check(all(len(P.dumps(o)) < 262144 for o in got),
              'nothing oversized is returned')


# --- routing, errors, no side effects ------------------------------------------------------

def _serve(svc):
    port = _free_port()
    httpd = HTTPServer(('127.0.0.1', port), web._make_handler(svc))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def test_q_http_routes_and_errors():
    print('test_q_http_routes_and_errors')
    with D._Tmp() as base:
        clock = _Clock(D0 + 3600)
        svc = _svc(base, clock)
        httpd = None
        logged = []
        orig_log = cp.log
        cp.log = lambda m, *a, **k: logged.append(str(m))
        try:
            with _fake_collectors():
                svc.collect_startup()
                for i in range(5):
                    svc._store.append(_obs(D0 + 100 + i))
                jid = _record_job(svc, clock, 'h', 15)
            httpd, port = _serve(svc)
            code, h, hdr = _get(port, '/api/history?limit=2&start=%d' % D0)
            check(code == 200 and h['count'] == 2 and h['has_more'] and
                  hdr['Content-Type'] == 'application/json' and
                  set(h) == {'samples', 'count', 'next_cursor', 'has_more',
                             'skipped_lines'}, 'GET /api/history envelope')
            code, h2, _ = _get(port, '/api/history?limit=2&start=%d&cursor=%s'
                               % (D0, h['next_cursor']))
            check(code == 200 and h2['samples'][0]['collected_ts'] !=
                  h['samples'][0]['collected_ts'], 'HTTP cursor continues')
            code, s, _ = _get(port, '/api/recordings/%s/samples?limit=3' % jid)
            check(code == 200 and s['count'] == 3 and s['has_more'] and
                  s['samples'][0]['job_id'] == jid, 'GET .../samples')
            code, m, _ = _get(port, '/api/recordings/' + jid)
            check(code == 200 and m['job_id'] == jid and 'samples' not in m,
                  'metadata route is not shadowed by .../samples')
            code, lst, _ = _get(port, '/api/recordings')
            check(code == 200 and lst['recordings'][0]['job_id'] == jid,
                  '/api/recordings unchanged')
            for path, want in (
                    ('/api/history?limit=999', 400),
                    ('/api/history?cursor=zzzz', 400),
                    ('/api/history?bogus=1', 400),
                    ('/api/recordings/%s/samples?group=zzz' % jid, 400),
                    ('/api/recordings/rec-1-001/samples', 404),
                    ('/api/recordings/nope/samples', 404),
                    ('/api/recordings/../samples', 404),
                    ('/api/recordings/..%2f..%2fetc%2fpasswd/samples', 404),
                    ('/api/recordings/%2e%2e/samples', 404),
                    ('/api/recordings//samples', 404),
                    ('/api/recordings/%s/samples/' % jid, 404),
                    ('/api/recordings/%s/samplesx' % jid, 404),
                    ('/api/history/', 404)):
                code, body, _ = _get(port, path)
                check(code == want and 'error' in body and
                      base not in json.dumps(body),
                      '{} -> {} JSON error, no path leak'.format(path[:60],
                                                                  want))
            # 410 end to end
            os.remove(_hist_path(base, D0 + 100))
            code, body, _ = _get(port, '/api/history?limit=2&start=%d&cursor=%s'
                                 % (D0, h['next_cursor']))
            check(code == 410 and 'error' in body, 'expired cursor -> 410')
            # unexpected failure -> 500, generic body, logged
            def boom(**kw):
                raise OSError('/secret/path/telemetry exploded')
            svc._store.query = boom
            code, body, _ = _get(port, '/api/history')
            check(code == 500 and body == {'error': 'internal error'},
                  '500 with a generic body')
            check(any('exploded' in m for m in logged),
                  'unexpected failure logged through cp.log')
            check('secret' not in json.dumps(body), 'no internals leaked')
            # existing endpoints still fine
            for path in ('/api/status', '/api/storage', '/api/snapshot',
                         '/api/recordings/active', '/api/wan'):
                code, _, _ = _get(port, path)
                check(code == 200, 'existing {} still 200'.format(path))
        finally:
            cp.log = orig_log
            if httpd is not None:
                httpd.shutdown()
                httpd.server_close()


def test_q_traversal_and_ids_direct():
    print('test_q_traversal_and_ids_direct')
    with D._Tmp() as base:
        clock = _Clock()
        svc = _svc(base, clock)
        secret = os.path.join(base, 'tmp', 'telemetry', 'secret.txt')
        with open(secret, 'w') as f:
            f.write('{"collected_ts":1}\n')
        for jid in ('../', '..', '../history', 'rec-1-001/../..',
                    'rec-1-001\n', '', 'rec-99999999999999-001',
                    'REC-1-001', '/etc/passwd', 'rec-1-001/samples'):
            check(_status(lambda j=jid: svc.query_recording_samples(j, ''))
                  == 404, 'job id {!r} -> 404'.format(jid))
        check(_status(lambda: svc._rec_store.query_samples('../x')) == 404,
              'store rejects traversal ids too')


def test_q_fallback_without_durable_storage():
    print('test_q_fallback_without_durable_storage')
    with D._Tmp() as base:
        clock = _Clock(D0 + 3600)
        svc = AnalyzerService(clock=clock, app_root=base)     # in-memory
        check(svc._durable is None, 'no durable storage')
        stamps = [D0 + 100 + i * 10 for i in range(25)]
        for i, ts in enumerate(stamps):
            svc._store.append(_obs(ts, 'manual' if i % 5 == 0 else
                                   'scheduled'))
        got, pages, _ = _drain_history(svc, limit=10)
        check(got == stamps and pages == 3, 'in-memory history pages with '
              'no duplicates or omissions')
        g, _, _ = _drain_history(svc, sample_source='manual', limit=2)
        check(g == stamps[::5], 'in-memory source filter')
        g, _, _ = _drain_history(svc, start=stamps[3], end=stamps[8])
        check(g == stamps[3:9], 'in-memory inclusive range')
        r = _hist(svc, limit=10)
        check(_status(lambda: _hist(svc, sample_source='manual',
                                    cursor=r['next_cursor'])) == 400,
              'in-memory cursor bound to its query')
        check(_status(lambda: _hist(svc, cursor='AAAA')) == 400,
              'in-memory malformed cursor')
        # eviction -> 410
        small = AnalyzerService(clock=clock, app_root=base,
                                store=__import__('analyzer.storage',
                                                 fromlist=['x'])
                                .InMemoryHistoryStore(maxlen=5))
        for ts in stamps[:8]:
            small._store.append(_obs(ts))
        r = small.query_history('limit=2')
        for ts in stamps[8:14]:
            small._store.append(_obs(ts))
        check(_status(lambda: small.query_history(
            _qs(limit=2, cursor=r['next_cursor']))) == 410,
            'in-memory cursor to evicted samples is 410')
        # recordings in memory
        with _fake_collectors():
            svc.collect_startup()
            jid = _record_job(svc, clock, 'mem', 20)
        truth = svc.recording_samples(jid)
        got, pages, _ = _drain_rec(svc, jid, limit=6)
        check(len(got) == len(truth) and len(got) > 6 and pages > 1,
              'in-memory Recording samples paged ({} samples)'.format(
                  len(got)))
        g, _, _ = _drain_rec(svc, jid, group='wan')
        check(g and all(o['recording_group'] == 'wan' for o in g),
              'in-memory group filter')
        check(_status(lambda: svc.query_recording_samples('rec-1-001', ''))
              == 404, 'in-memory unknown job 404')
        # durable root that cannot be created -> in-memory fallback + API
        blocker = os.path.join(base, 'blocker')
        with open(blocker, 'w') as f:
            f.write('file')
        bad = AnalyzerService(clock=clock, app_root=base,
                              telemetry_root='blocker/telemetry')
        check(bad._durable is None, 'unwritable telemetry root -> fallback')
        bad._store.append(_obs(D0 + 5))
        check(_hist(bad)['count'] == 1, 'history API works on the fallback')


def test_q_default_window_needs_sane_clock():
    print('test_q_default_window_needs_sane_clock')
    with D._Tmp() as base:
        clock = _Clock(100)                          # pre-NTP clock
        svc = AnalyzerService(clock=clock, app_root=base)
        for ts in (50, 90, D0):
            svc._store.append(_obs(ts))
        check(_hist(svc)['count'] == 3,
              'insane router clock: no default window is applied')
        clock.t = D0 + 10
        check(_hist(svc)['count'] == 1,
              'sane clock: default window is the last 24 h')


def test_q_reads_are_side_effect_free():
    print('test_q_reads_are_side_effect_free')
    with D._Tmp() as base:
        clock = _Clock(D0 + 7200)
        svc = _svc(base, clock)
        with _fake_collectors() as fc:
            svc.collect_startup()
            for i in range(20):
                svc._store.append(_obs(D0 + 100 + i, pad=2000))
            jid = _record_job(svc, clock, 'ro', 15)
            svc.start_recording('active', max_duration_sec=100)
            D._run(svc, clock, 5)
            # make sure appenders are flushed so the tree is stable
            before_tree = _tree(base)
            before_calls = len(fc.calls)
            h = svc._durable.health
            counters = (h.persisted_observations, h.write_errors,
                        h.held_observations)
            sched = svc.next_scheduled_ts()
            rec_counts = {j['job_id']: j['sample_count']
                          for j in svc.list_recordings()}
            with D._CountFsync() as fs:
                for _ in range(3):
                    cur = None
                    for _ in range(10):
                        r = _hist(svc, start=D0, limit=3, cursor=cur)
                        cur = r['next_cursor']
                        if cur is None:
                            break
                    svc.query_recording_samples(jid, 'limit=5')
                    svc.query_recording_samples(jid, 'group=wan&after=1')
                    try:
                        svc.query_history('cursor=bad')
                    except Q.QueryError:
                        pass
            check(_tree(base) == before_tree,
                  'no file created, changed, removed or touched by reads')
            check(fs.n == 0, 'no fsync caused by reads')
            check(len(fc.calls) == before_calls,
                  'reads trigger no NCOS collection')
            check(counters == (h.persisted_observations, h.write_errors,
                               h.held_observations),
                  'no storage write counters moved')
            check(svc.next_scheduled_ts() == sched and rec_counts == {
                j['job_id']: j['sample_count'] for j in svc.list_recordings()
            }, 'schedule and Recording counts untouched')


def test_q_storage_report_and_lifecycle_unchanged():
    print('test_q_storage_report_and_lifecycle_unchanged')
    with D._Tmp() as base:
        clock = _Clock(D0 + 3600)
        svc = _svc(base, clock)
        with _fake_collectors():
            svc.collect_startup()
            jid = _record_job(svc, clock, 'life', 15)
            _hist(svc)
            svc.query_recording_samples(jid, '')
        rep = svc.storage_report()
        p = rep.get('persistent')
        check(p and p['enabled'] and p['status'] == 'ok' and
              'recovery' in p and 'retention' in p and 'free_space' in p,
              '/api/storage health/recovery reporting preserved')
        d = svc.get_recording(jid)
        check(d['state'] in ('completed', 'expired', 'stopped') and
              d['sample_count'] == len(svc.recording_samples(jid)),
              'Recording lifecycle and counts unchanged')
        check(svc.status()['version'] == '0.8.1', 'version 0.8.1')


TESTS = [
    test_q_scan_range_counts_every_byte_examined,
    test_q_scan_range_oversized_records,
    test_q_parameter_validation,
    test_q_history_multi_day_pagination_and_default_window,
    test_q_history_inclusive_bounds_and_sources,
    test_q_history_filter_pages_with_empty_intermediate_pages,
    test_q_history_cursor_rejection,
    test_q_history_malformed_and_incomplete_lines,
    test_q_history_resource_ceilings,
    test_q_history_oversized_and_malformed_runs,
    test_q_recording_group_after_and_states,
    test_q_recording_active_append_between_pages,
    test_q_recording_cursor_and_retention,
    test_q_recording_oversized_and_junk_samples,
    test_q_http_routes_and_errors,
    test_q_traversal_and_ids_direct,
    test_q_fallback_without_durable_storage,
    test_q_default_window_needs_sane_clock,
    test_q_reads_are_side_effect_free,
    test_q_storage_report_and_lifecycle_unchanged,
]
