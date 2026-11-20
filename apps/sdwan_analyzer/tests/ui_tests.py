"""v0.7.0: Throughput trends UI foundation (focused checks).

* Static serving: the shell and assets are served from static/ only.
* UI/API integration: the exact query the page sends to
  /api/analytics/interfaces, followed across pages, returns valid zero
  throughput as 0 and every missing / reset / suspect / too-far-apart interval
  as a non-ok rate with no number (the chart draws those as gaps).
* Chart logic (series.js / format.js) and a rendered page, run in headless
  Chrome when it is installed (skipped with a note otherwise).

Test fixtures here are synthetic history written into a temporary store; the
production UI never generates data. Run through run_tests.py (shared `check`).
Manual look at the page:  python tests/ui_tests.py --serve
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.dirname(_HERE)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from analyzer import web          # noqa: E402
import durable_tests as D         # noqa: E402

check = None            # bound by run_tests.main()
_Clock = None
_free_port = None
T0 = 1790380800
STEP = 300              # seconds between synthetic observations


def bind(check_fn, clock_cls, port_fn):
    global check, _Clock, _free_port
    check = check_fn
    _Clock = clock_cls
    _free_port = port_fn


# --- helpers ------------------------------------------------------------------

def _rec(uid, cls, ts, rx, tx):
    return {'uid': uid, 'object_class': cls, 'collected_ts': ts,
            'collected_at': ts + 0.5, 'rx_bytes': rx, 'tx_bytes': tx}


def _obs(ts, ifaces):
    o = D._obs(ts)
    o['interfaces'] = ifaces
    return o


class _Server(object):
    """The real request handler on a real bounded HTTP server."""

    def __init__(self, service):
        self.port = _free_port()
        self.httpd = web._BoundedHTTPServer(
            ('127.0.0.1', self.port), web._make_handler(service))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def get(self, path, headers=None):
        req = urllib.request.Request(
            'http://127.0.0.1:%d%s' % (self.port, path), headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def json(self, path):
        code, _, body = self.get(path)
        return code, json.loads(body.decode('utf-8'))

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _ui_query(uid, start, cursor=None):
    """Exactly the query string static/js/analyzer/api.js builds."""
    q = ('uid=%s&start=%.3f&limit=250&max_gap_s=7200' % (uid, start))
    return q + ('&cursor=' + cursor if cursor else '')


def _fetch_all(srv, uid, start):
    rates, cursor, pages = [], None, 0
    while True:
        code, body = srv.json('/api/analytics/interfaces?' +
                              _ui_query(uid, start, cursor))
        if code != 200:
            return code, rates, pages
        rates.extend(body['rates'])
        pages += 1
        cursor = body['next_cursor']
        if not body['has_more'] or pages > 20:
            return 200, rates, pages


# --- static serving -----------------------------------------------------------

def test_ui_static_serving_is_confined():
    print('test_ui_static_serving_is_confined')
    with D._Tmp() as base:
        srv = _Server(D._mk(base, _Clock(T0)))
        try:
            code, hdr, body = srv.get('/')
            check(code == 200 and b'throughput-trends-root' in body and
                  hdr['Content-Type'].startswith('text/html'),
                  'GET / serves the application shell')
            check('Date' in hdr, 'responses carry the router Date header (used for the time range)')
            code, hdr, body = srv.get('/static/js/analyzer/series.js')
            check(code == 200 and hdr['Content-Type'].startswith(
                'application/javascript') and b'SdwanSeries' in body,
                  'static JS served with its MIME type')
            code, hdr, body = srv.get('/static/css/analyzer.css')
            check(code == 200 and hdr['Content-Type'].startswith('text/css'),
                  'static CSS served with its MIME type')
            etag = hdr.get('ETag')
            code, _, body = srv.get('/static/css/analyzer.css',
                                    {'If-None-Match': etag})
            check(code == 304 and body == b'', 'unchanged asset answered 304')
            for bad in ('/static/../package.ini', '/static/%2e%2e/package.ini',
                        '/static/..%2fpackage.ini', '/static/js/..%5c..%5cpackage.ini',
                        '/static//etc/passwd', '/static/', '/static/js',
                        '/static/js/', '/static/nope.js', '/static/css/analyzer.css%00.js',
                        '/static/../analyzer/web.py', '/static/js/analyzer/series.py'):
                code, _, body = srv.get(bad)
                check(code == 404 and b'not found' in body,
                      '404 for {!r}'.format(bad))
            code, _, body = srv.get('/dev')
            check(code == 200 and b'/api/analytics/interfaces' in body,
                  'previous index page still available at /dev')
            code, body = srv.json('/api/status')
            check(code == 200 and body['version'] == '0.8.3',
                  '/api/status reports version 0.8.3')
        finally:
            srv.close()


# --- UI/API integration + data integrity --------------------------------------

def _seed_integrity(svc):
    """300 observations: steady traffic, a zero period, a reset, a missing
    record, a long outage. Returns the expected-per-interval spec."""
    u, other = 'ethernet-wan', 'sdwan-hub0_wan'
    rx = tx = 0
    plan = []
    shift = 0
    for i in range(300):
        if i == 150:
            shift = 7200 * 2                   # long outage before this sample
        ts = T0 + i * STEP + shift
        ifaces = [_rec(other, 'sdwan', ts, i * 10, i * 10)]
        if i == 100:
            rx, tx = 1000, 1000                # counter reset (values go down)
        elif 60 <= i < 70:
            pass                               # idle: counters unchanged -> 0 Mbps
        else:
            rx += 375000000                    # 10 Mbps over 300 s
            tx += 75000000                     # 2 Mbps over 300 s
        if i != 200:                           # observation 200 has no record
            ifaces.append(_rec(u, 'physical', ts, rx, tx))
        svc._store.append(_obs(ts, ifaces))
        plan.append(ts)
    return u


def test_ui_api_integration_and_gaps():
    print('test_ui_api_integration_and_gaps')
    with D._Tmp() as base:
        svc = D._mk(base, _Clock(T0 + 400000))
        u = _seed_integrity(svc)
        srv = _Server(svc)
        try:
            code, rates, pages = _fetch_all(srv, u, T0 - 1)
            check(code == 200 and pages >= 2 and len(rates) == 299,
                  'UI query pages through all history: %d rates in %d pages' %
                  (len(rates), pages))
            by_end = {int(r['end_ts']): r for r in rates}
            # contiguity: consecutive valid pairs share a boundary, including
            # across the page boundary (the cursor carries the baseline)
            gaps = [i for i in range(1, len(rates))
                    if rates[i]['status'] == 'ok' and rates[i - 1]['status'] == 'ok'
                    and abs(rates[i]['start_ts'] - rates[i - 1]['end_ts']) > 0.001]
            check(not gaps, 'no pair lost or duplicated across page boundaries')
            zero = [r for r in rates if r['status'] == 'ok' and r['rx']['mbps'] == 0]
            check(len(zero) == 10 and all(r['tx']['mbps'] == 0 for r in zero),
                  'idle period is 10 valid zero measurements (status ok, not null)')
            normal = by_end[T0 + 10 * STEP]
            check(abs(normal['rx']['mbps'] - 10.0) < 0.01 and
                  abs(normal['tx']['mbps'] - 2.0) < 0.01,
                  'steady traffic gives 10 / 2 Mbps')
            reset = by_end[T0 + 100 * STEP]
            check(reset['status'] == 'counter_decrease' and
                  reset['rx']['mbps'] is None and reset['tx']['mbps'] is None,
                  'counter reset: no number, status counter_decrease')
            miss = [by_end[T0 + 200 * STEP + 14400], by_end[T0 + 201 * STEP + 14400]]
            check(miss[0]['status'] == 'missing_value' and miss[0]['rx']['mbps'] is None and
                  miss[1]['status'] == 'missing_baseline' and miss[1]['rx']['mbps'] is None,
                  'missing record: both neighbouring pairs have no number')
            outage = [r for r in rates if r['status'] == 'gap_exceeded']
            check(len(outage) == 1 and outage[0]['rx']['mbps'] is None,
                  'max_gap_s=7200 turns the long outage into one gap_exceeded interval')
            not_ok = [r for r in rates if r['status'] != 'ok']
            check(len(not_ok) == 4 and all(
                r['rx']['mbps'] is None and r['tx']['mbps'] is None for r in not_ok),
                  'every non-ok interval carries no number (never zero)')
            # other identities are untouched by this uid's gaps
            code, body = srv.json('/api/analytics/interfaces?' +
                                  _ui_query('sdwan-hub0_wan', T0 - 1))
            check(code == 200 and body['rates'][0]['status'] == 'ok',
                  'a different interface is a separate series')
            # empty range -> 200 with no rates (empty state), not an error
            code, body = srv.json('/api/analytics/interfaces?' +
                                  _ui_query(u, T0 + 900000))
            check(code == 200 and body['count'] == 0 and not body['has_more'],
                  'range with no history returns an empty list')
            # an interface with no records at all: every interval is a gap
            code, body = srv.json('/api/analytics/interfaces?' +
                                  _ui_query('nope', T0 - 1))
            check(code == 200 and body['count'] > 0 and all(
                r['status'] != 'ok' and r['rx']['mbps'] is None
                for r in body['rates']),
                  'interface with no records yields only number-less gaps')
            code, body = srv.json('/api/analytics/interfaces?' +
                                  _ui_query('a/b', T0))
            check(code == 400 and 'error' in body, 'invalid uid is a JSON 400 (UI error state)')
        finally:
            srv.close()


# --- headless Chrome ---------------------------------------------------------

def _chrome():
    for p in ('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
              '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
              shutil.which('google-chrome') or '', shutil.which('chromium') or ''):
        if p and os.path.exists(p):
            return p
    return None


def _run_chrome(args, timeout=60):
    prof = tempfile.mkdtemp(prefix='sdwan_chrome_')
    try:
        cmd = [_chrome(), '--headless=new', '--disable-gpu', '--no-sandbox',
               '--no-first-run', '--user-data-dir=' + prof,
               '--enable-logging=stderr', '--v=0'] + args
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
        return (p.stdout.decode('utf-8', 'replace'),
                p.stderr.decode('utf-8', 'replace'))
    finally:
        shutil.rmtree(prof, ignore_errors=True)


def test_ui_chart_logic_in_browser():
    print('test_ui_chart_logic_in_browser')
    if _chrome() is None:
        print('  SKIP: no Chrome/Edge installed for the browser checks')
        return
    out, _ = _run_chrome(['--virtual-time-budget=5000', '--dump-dom',
                          'file://' + os.path.join(_HERE, 'ui', 'js_checks.html')])
    lines = [l for l in out.split('\n') if l.startswith(('PASS: ', 'FAIL: '))]
    fails = [l for l in lines if l.startswith('FAIL')]
    for l in lines:
        check(l.startswith('PASS'), l[6:])
    check(len(lines) >= 25 and 'DONE' in out and not fails,
          'browser checks ran ({} checks)'.format(len(lines)))


class _UiService(object):
    """Real analytics over a seeded store; /api/wan from a fixed interface list."""

    def __init__(self, svc, paths, primary):
        self._svc = svc
        self._snap = {'wan': {'primary_device': primary, 'paths': paths}}

    def snapshot(self):
        return self._snap

    def section(self, name):
        return self._snap.get(name)

    def __getattr__(self, name):
        return getattr(self._svc, name)


def _wan_paths():
    def p(uid, cls, name):
        return {'uid': uid, 'object_class': cls, 'display_name': name,
                'connection_state': 'connected'}
    return [p('ethernet-wan', 'physical', 'Ethernet WAN'),
            p('mdm-1', 'physical', 'Cellular WAN'),
            p('sdwan-hub0_wan', 'sdwan', 'SD-WAN overlay hub0'),
            p('wbond-wbond0_hub0', 'wbond', 'WBOND wbond0')]


def _seed_recent(svc, now, hours=30):
    """STEP-spaced history ending near `now` for every WAN path, with a counter
    reset and a missing record on ethernet-wan."""
    n = int(hours * 3600 / STEP)
    rx = tx = 0
    cell = 0
    for i in range(n):
        ts = now - (n - i) * STEP
        step_rx = 100000 + (i * 7919) % 900000
        rx = rx + step_rx if i != n // 2 else 5000
        tx += step_rx // 5
        cell += 50000 + (i % 40) * 20000
        ifaces = [_rec('mdm-1', 'physical', ts, cell, cell // 4),
                  _rec('sdwan-hub0_wan', 'sdwan', ts, rx // 2, tx // 2),
                  _rec('wbond-wbond0_hub0', 'wbond', ts, rx, tx)]
        if i != n // 3:
            ifaces.append(_rec('ethernet-wan', 'physical', ts, rx, tx))
        svc._store.append(_obs(ts, ifaces))


def test_ui_page_renders_in_browser():
    print('test_ui_page_renders_in_browser')
    if _chrome() is None:
        print('  SKIP: no Chrome/Edge installed for the browser checks')
        return
    now = int(time.time())
    with D._Tmp() as base:
        svc = D._mk(base, _Clock(now))
        _seed_recent(svc, now)
        srv = _Server(_UiService(svc, _wan_paths(), 'ethernet-wan'))
        try:
            out, err = _run_chrome(['--virtual-time-budget=20000', '--dump-dom',
                                    'http://127.0.0.1:%d/' % srv.port], timeout=90)
        finally:
            srv.close()
        check('<option value="ethernet-wan"' in out and
              'Physical WAN' in out and 'SD-WAN overlay' in out and 'WBOND' in out,
              'interface selector lists physical, overlay and WBOND groups')
        check((out.count('class="sa-segment"') + out.count('class="sa-segment active"')) == 4 and
              all('>%s</button>' % r in out for r in ('1h', '6h', '24h', '7d')),
              'time range buttons 1h, 6h, 24h, 7d')
        check('class="sa-line sa-rx"' in out and 'class="sa-line sa-tx"' in out,
              'RX and TX lines drawn from API data')
        check('class="sa-gap-band"' in out, 'invalid intervals are shaded gaps')
        import re
        check(re.search(r'Throughput \((bps|Kbps|Mbps|Gbps)\)', out) is not None and 'RX (received)' in out and
              'TX (transmitted)' in out and 'Gap: no valid measurement' in out,
              'axis unit and legend present')
        check('sa-state-error' not in out and 'sa-state-loading' not in out,
              'no error or loading state left on screen')
        check('Uncaught' not in err and 'Error' not in ''.join(
            l for l in err.split('\n') if 'CONSOLE' in l),
              'no JavaScript errors in the console')


def serve_for_manual_look():
    """python tests/ui_tests.py --serve : browse the UI on seeded local data."""
    now = int(time.time())
    base = tempfile.mkdtemp(prefix='sdwan_ui_')
    try:
        svc = D._mk(base, _Clock(now))
        _seed_recent(svc, now, hours=24 * 7)
        port = int(os.environ.get('PORT', '8765'))
        httpd = web._BoundedHTTPServer(
            ('127.0.0.1', port),
            web._make_handler(_UiService(svc, _wan_paths(), 'ethernet-wan')))
        print('serving on http://127.0.0.1:%d/ (Ctrl-C to stop)' % port)
        httpd.serve_forever()
    finally:
        shutil.rmtree(base, ignore_errors=True)


TESTS = [
    test_ui_static_serving_is_confined,
    test_ui_api_integration_and_gaps,
    test_ui_chart_logic_in_browser,
    test_ui_page_renders_in_browser,
]

if __name__ == '__main__':
    if '--serve' in sys.argv:
        class _FixedClock(object):
            def __init__(self, t):
                self.t = t

            def __call__(self):
                return self.t
        _Clock = _FixedClock
        serve_for_manual_look()
