"""v0.6.0: GET /api/analytics/interfaces (calculated interface rates).

Focused on the integration of stored history with counters.py: correct rates
through the real durable store and service, per-identity separation, no
bridging or zero fill, v0.4.4 suspect input, pagination across a page
boundary, and the HTTP route. Counter math itself is covered by
counters_tests.py. Run through run_tests.py (shared `check`).
"""
import json
import urllib.error
import urllib.request
from http.server import HTTPServer
from urllib.parse import urlencode
import threading

from analyzer import query as Q
from analyzer import web
import durable_tests as D

check = None            # bound by run_tests.main()
_Clock = None
T0 = 1790380800


def bind(check_fn, clock_cls):
    global check, _Clock
    check = check_fn
    _Clock = clock_cls


def _rec(uid, cls, ts, at, rx, tx):
    r = {'uid': uid, 'object_class': cls, 'collected_ts': ts,
         'rx_bytes': rx, 'tx_bytes': tx}
    if at is not None:
        r['collected_at'] = at
    return r


def _obs(ts, ifaces, errors=None):
    o = D._obs(ts)
    o['interfaces'] = ifaces
    if errors:
        o['collector_errors'] = errors
    return o


def _svc(base):
    return D._mk(base, _Clock(T0 + 3600))


def _rates(svc, **kw):
    kw.setdefault('start', T0)
    kw.setdefault('end', T0 + 3000)
    return svc.query_interface_rates(urlencode(
        {k: v for k, v in kw.items() if v is not None}))


def _status(fn):
    try:
        fn()
    except Q.QueryError as e:
        return e.status
    return None


def test_an_hand_computed_rates_and_identity():
    print('test_an_hand_computed_rates_and_identity')
    with D._Tmp() as base:
        svc = _svc(base)
        # physical eth: 131 250 B rx / 52 500 B tx over 10.5 s (fractional)
        svc._store.append(_obs(T0 + 0, [
            _rec('ethernet-wan', 'physical', T0, T0 + 0.25, 1000000, 500000),
            _rec('sdwan-hub0_wan', 'sdwan', T0, T0 + 0.25, 40, 80),
            _rec('wbond-wbond0_hub0', 'wbond', T0, T0 + 0.25, 7, 9)]))
        svc._store.append(_obs(T0 + 10, [
            _rec('ethernet-wan', 'physical', T0 + 10, T0 + 10.75, 1131250,
                 552500),
            _rec('sdwan-hub0_wan', 'sdwan', T0 + 10, T0 + 10.75, 145, 185),
            _rec('wbond-wbond0_hub0', 'wbond', T0 + 10, T0 + 10.75, 7, 9)]))
        r = _rates(svc, uid='ethernet-wan')
        p = r['rates'][0]
        check(r['uid'] == 'ethernet-wan' and r['object_class'] == 'physical'
              and r['count'] == 1 and not r['has_more'] and
              p['object_class'] == 'physical', 'uid and class preserved')
        check(p['status'] == 'ok' and p['interval_s'] == 10.5 and
              p['time_source'] == 'collected_at' and
              p['time_precision'] == 'fractional' and
              p['reduced_precision'] is False and
              p['start_ts'] == T0 + 0.25 and p['end_ts'] == T0 + 10.75,
              'fractional collected_at used')
        check(p['rx'] == {'status': 'ok', 'delta_bytes': 131250,
                          'bytes_per_s': 12500.0, 'bits_per_s': 100000.0,
                          'mbps': 0.1}, 'rx bytes/s, bits/s, Mbps')
        check(p['tx']['bytes_per_s'] == 5000.0 and
              p['tx']['bits_per_s'] == 40000.0 and p['tx']['mbps'] == 0.04,
              'tx rates')
        # independent identities: nothing summed across classes
        o = _rates(svc, uid='sdwan-hub0_wan')
        w = _rates(svc, uid='wbond-wbond0_hub0')
        check(o['object_class'] == 'sdwan' and
              o['rates'][0]['rx']['bytes_per_s'] == 10.0 and
              o['rates'][0]['tx']['bytes_per_s'] == 10.0,
              'sdwan overlay computed on its own counters')
        check(w['object_class'] == 'wbond' and
              w['rates'][0]['rx']['bytes_per_s'] == 0.0 and
              w['rates'][0]['rx']['status'] == 'ok',
              'wbond computed alone; a real zero rate stays a zero rate')
        n = _rates(svc, uid='nope')['rates'][0]
        check(n['status'] == 'missing_baseline' and
              n['rx']['bytes_per_s'] is None, 'unknown interface: no rate')


def test_an_integrity_no_bridge_no_invention():
    print('test_an_integrity_no_bridge_no_invention')
    with D._Tmp() as base:
        svc = _svc(base)
        u = 'ethernet-wan'
        # t0 ok, t10 ok, t20 interface missing, t30 ok, t40 counter decrease,
        # t50 one record lacks collected_at (integer fallback)
        seq = [
            (0, _rec(u, 'physical', T0, T0 + 0.0 + 1, 100, 100)),
            (10, _rec(u, 'physical', T0 + 10, T0 + 11, 1100, 600)),
            (20, None),
            (30, _rec(u, 'physical', T0 + 30, T0 + 31, 5000, 900)),
            (40, _rec(u, 'physical', T0 + 40, T0 + 41, 10, 950)),
            (50, _rec(u, 'physical', T0 + 50, None, 1010, 1050)),
        ]
        for off, rec in seq:
            svc._store.append(_obs(T0 + off, [rec] if rec else []))
        rs = _rates(svc, uid=u)['rates']
        st = [r['status'] for r in rs]
        check(st == ['ok', 'missing_value', 'missing_baseline',
                     'counter_decrease', 'ok'], 'statuses {}'.format(st))
        check(rs[1]['rx']['bytes_per_s'] is None and
              rs[2]['rx']['bytes_per_s'] is None,
              'no rate across a missing observation (no bridge, no zero)')
        check(rs[3]['rx']['bytes_per_s'] is None and
              rs[3]['rx']['delta_bytes'] is None and
              rs[3]['tx']['status'] == 'ok',
              'decrease gives no rx rate; tx judged on its own counter')
        check(rs[4]['time_source'] == 'collected_ts' and
              rs[4]['reduced_precision'] is True and
              rs[4]['interval_s'] == 10 and
              rs[4]['rx']['bytes_per_s'] == 100.0,
              'integer-second fallback is flagged, never mixed')
        # explicit caller gap policy; none by default
        g = _rates(svc, uid=u, max_gap_s=5)['rates'][0]
        check(g['status'] == 'gap_exceeded' and g['rx']['bytes_per_s'] is None
              and _rates(svc, uid=u)['rates'][0]['status'] == 'ok',
              'max_gap_s is opt-in only')


def test_an_v044_failed_collector_observation_is_suspect():
    print('test_an_v044_failed_collector_observation_is_suspect')
    with D._Tmp() as base:
        svc = _svc(base)
        u = 'ethernet-wan'
        err = [{'collector': 'wan_devices', 'error': 'timeout'}]
        svc._store.append(_obs(T0, [_rec(u, 'physical', T0, None, 100, 100)]))
        svc._store.append(_obs(T0 + 10, [_rec(
            u, 'physical', T0 + 10, None, 100, 100)], err))
        svc._store.append(_obs(T0 + 20, [_rec(
            u, 'physical', T0 + 20, None, 9999, 9999)]))
        svc._store.append(_obs(T0 + 30, [_rec(
            u, 'physical', T0 + 30, None, 10999, 10999)]))
        rs = _rates(svc, uid=u)['rates']
        st = [r['status'] for r in rs]
        check(st == ['suspect_input', 'suspect_input', 'ok'] and
              all(r['rx']['mbps'] is None for r in rs[:2]) and
              rs[2]['rx']['bytes_per_s'] == 100.0,
              'pairs touching the failed observation carry no rate ' +
              str(st))


def test_an_pagination_matches_unpaged_and_spans_boundary():
    print('test_an_pagination_matches_unpaged_and_spans_boundary')
    with D._Tmp() as base:
        svc = _svc(base)
        u = 'ethernet-wan'
        for i in range(12):
            svc._store.append(_obs(T0 + i * 10, [_rec(
                u, 'physical', T0 + i * 10, T0 + i * 10 + 0.5,
                1000 * i * i, 10 * i)]))
        full = _rates(svc, uid=u)['rates']
        check(len(full) == 11, '11 pairs from 12 observations')
        got, cur, pages = [], None, 0
        while True:
            pg = _rates(svc, uid=u, limit=4, cursor=cur)
            got += pg['rates']
            pages += 1
            cur = pg['next_cursor']
            if not pg['has_more']:
                break
            check(pages < 10, 'terminates')
            if pages >= 10:
                break
        check(got == full and pages == 3,
              'paged rates identical to unpaged, boundary pairs included '
              '(pages={})'.format(pages))
        check(_status(lambda: _rates(svc, uid=u, limit=4,
                                     cursor='x')) == 400,
              'bad cursor rejected')
        first = _rates(svc, uid=u, limit=4)
        check(_status(lambda: _rates(
            svc, uid='other', limit=4, cursor=first['next_cursor'])) == 400,
              'cursor bound to its uid')


def test_an_http_route_validation_and_read_only():
    print('test_an_http_route_validation_and_read_only')
    with D._Tmp() as base:
        svc = _svc(base)
        u = 'ethernet-wan'
        for i in range(3):
            svc._store.append(_obs(T0 + i * 10, [_rec(
                u, 'physical', T0 + i * 10, None, 100 * i, 50 * i)]))
        before = svc._store.query(start=T0, end=T0 + 100, limit=50)['samples']
        port = _free_port()
        httpd = HTTPServer(('127.0.0.1', port), web._make_handler(svc))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def get(qs):
                try:
                    with urllib.request.urlopen(
                            'http://127.0.0.1:%d/api/analytics/interfaces%s'
                            % (port, qs), timeout=5) as r:
                        return r.status, json.loads(r.read().decode())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read().decode())
            code, body = get('?uid=%s&start=%d&end=%d' % (u, T0, T0 + 100))
            check(code == 200 and body['count'] == 2 and
                  body['rates'][0]['rx']['bytes_per_s'] == 10.0,
                  'GET /api/analytics/interfaces')
            for qs in ('', '?uid=', '?uid=a/b', '?uid=x&bogus=1',
                       '?uid=x&limit=0', '?uid=x&max_gap_s=0',
                       '?uid=x&start=5&end=1'):
                code, body = get(qs)
                check(code == 400 and 'error' in body,
                      '400 for {!r}'.format(qs))
        finally:
            httpd.shutdown()
            httpd.server_close()
        after = svc._store.query(start=T0, end=T0 + 100, limit=50)['samples']
        check(before == after and all('rates' not in o for o in after),
              'history untouched, no derived data persisted')


_free_port = None


def bind_port(fn):
    global _free_port
    _free_port = fn


TESTS = [
    test_an_hand_computed_rates_and_identity,
    test_an_integrity_no_bridge_no_invention,
    test_an_v044_failed_collector_observation_is_suspect,
    test_an_pagination_matches_unpaged_and_spans_boundary,
    test_an_http_route_validation_and_read_only,
]
