"""Unit tests for src/web_port.py (standard library only, no router needed).

Run from the dynamic-web-ports folder:

    python3 -m unittest discover -s tests -v

The retry tests use a fake clock, so the whole suite runs in well under a
second even though the real module waits up to 60 s on a busy port.
"""
import ast
import hashlib
import os
import socket
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(os.path.dirname(HERE), 'src')
sys.path.insert(0, SRC_DIR)

import web_port as w  # noqa: E402


def free_port():
    """Ask the OS for a currently free TCP port."""
    s = socket.socket()
    s.bind(('0.0.0.0', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def hold(port):
    """Occupy `port` with a listening socket (simulates a busy port)."""
    s = socket.socket()
    s.bind(('0.0.0.0', port))
    s.listen(1)
    return s


class FakeAppData(object):
    """Stand-in for cp.get_appdata / cp.put_appdata."""

    def __init__(self, **initial):
        self.store = dict(initial)
        self.puts = []

    def get(self, name):
        return self.store.get(name)

    def put(self, name, value):
        self.puts.append((name, value))
        self.store[name] = value


class FakeClock(object):
    """Deterministic time source so retry tests do not really wait."""

    def __init__(self):
        self.t = 0
        self.on_sleep = lambda: None

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        self.on_sleep()


class SocketCleanupMixin(object):
    def setUp(self):
        self._socks = []

    def track(self, sock):
        if sock is not None:
            self._socks.append(sock)
        return sock

    def tearDown(self):
        for s in self._socks:
            try:
                s.close()
            except Exception:
                pass


class ParsePortTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(w.parse_port('8000'), (8000, None))
        self.assertEqual(w.parse_port(8001), (8001, None))
        self.assertEqual(w.parse_port('  8002  '), (8002, None))
        self.assertEqual(w.parse_port('1'), (1, None))
        self.assertEqual(w.parse_port('65535'), (65535, None))

    def test_missing(self):
        self.assertEqual(w.parse_port(None), (None, 'missing'))
        self.assertEqual(w.parse_port(''), (None, 'missing'))
        self.assertEqual(w.parse_port('   '), (None, 'missing'))

    def test_non_numeric(self):
        for bad in ('abc', '80.5', '0x50', '80a', 'eight thousand', '8 000'):
            port, reason = w.parse_port(bad)
            self.assertIsNone(port, bad)
            self.assertTrue(reason.startswith('non-numeric'), bad)

    def test_out_of_range(self):
        for bad in ('0', '65536', '-1', '99999'):
            port, reason = w.parse_port(bad)
            self.assertIsNone(port, bad)
            self.assertTrue(reason.startswith('out-of-range'), bad)

    def test_field_name_is_namespaced(self):
        self.assertEqual(w.appdata_field('speedtest_analyzer'),
                         'speedtest_analyzer_web_port')


class FirstRunDiscoveryTests(SocketCleanupMixin, unittest.TestCase):
    def test_discovers_and_persists(self):
        ad, logs, start = FakeAppData(), [], free_port()
        r = w.bind_web_socket('app', ad.get, ad.put, logs.append,
                              start_port=start, max_ports=8)
        self.track(r.sock)
        self.assertTrue(r.ok)
        self.assertEqual(r.source, 'discovered')
        self.assertTrue(r.persisted)
        self.assertEqual(ad.store['app_web_port'], str(r.port))
        self.assertGreaterEqual(r.port, start)

    def test_skips_busy_port_and_takes_next(self):
        start = free_port()
        self.track(hold(start))
        ad = FakeAppData()
        r = w.bind_web_socket('app', ad.get, ad.put, lambda m: None,
                              start_port=start, max_ports=16)
        self.track(r.sock)
        self.assertTrue(r.ok)
        self.assertGreater(r.port, start)
        self.assertEqual(ad.store['app_web_port'], str(r.port))

    def test_exhausted_range_is_retryable_failure(self):
        start = free_port()
        self.track(hold(start))
        ad = FakeAppData()
        r = w.bind_web_socket('app', ad.get, ad.put, lambda m: None,
                              start_port=start, max_ports=1)
        self.assertFalse(r.ok)
        self.assertEqual(r.error, 'discovery-exhausted')
        self.assertTrue(r.retryable)
        self.assertEqual(ad.puts, [])

    def test_persist_failure_still_serves(self):
        start, logs = free_port(), []

        def boom(name, value):
            raise RuntimeError('appdata write denied')

        r = w.bind_web_socket('app', lambda n: None, boom, logs.append,
                              start_port=start, max_ports=8)
        self.track(r.sock)
        self.assertTrue(r.ok)
        self.assertFalse(r.persisted)
        self.assertTrue(any('failed to persist' in m for m in logs))

    def test_appdata_read_error_is_treated_as_unset(self):
        start = free_port()

        def boom(name):
            raise RuntimeError('appdata read failed')

        ad = FakeAppData()
        r = w.bind_web_socket('app', boom, ad.put, lambda m: None,
                              start_port=start, max_ports=8)
        self.track(r.sock)
        self.assertTrue(r.ok)
        self.assertEqual(r.source, 'discovered')


class ConfiguredValueTests(SocketCleanupMixin, unittest.TestCase):
    def test_configured_port_is_bound_exactly(self):
        port = free_port()
        ad = FakeAppData(app_web_port=str(port))
        r = w.bind_web_socket('app', ad.get, ad.put, lambda m: None)
        self.track(r.sock)
        self.assertTrue(r.ok)
        self.assertEqual((r.port, r.source), (port, 'configured'))
        self.assertEqual(ad.puts, [], 'must not write when configured')

    def test_busy_configured_port_fails_closed_without_scanning(self):
        port = free_port()
        self.track(hold(port))
        ad = FakeAppData(app_web_port=str(port))
        r = w.bind_web_socket('app', ad.get, ad.put, lambda m: None,
                              start_port=port + 1)
        self.assertFalse(r.ok)
        self.assertEqual(r.error, 'configured-port-unavailable')
        self.assertTrue(r.retryable)
        self.assertIsNone(r.sock)
        self.assertEqual(ad.puts, [], 'must never overwrite the value')
        self.assertEqual(ad.store['app_web_port'], str(port))

    def test_malformed_value_fails_closed_and_is_not_retryable(self):
        ad = FakeAppData(app_web_port='abc')
        logs = []
        r = w.bind_web_socket('app', ad.get, ad.put, logs.append)
        self.assertFalse(r.ok)
        self.assertFalse(r.retryable)
        self.assertTrue(r.error.startswith('non-numeric'))
        self.assertEqual(ad.puts, [])
        self.assertEqual(ad.store['app_web_port'], 'abc')
        self.assertEqual(len(logs), 1)

    def test_out_of_range_value_fails_closed(self):
        ad = FakeAppData(app_web_port='70000')
        r = w.bind_web_socket('app', ad.get, ad.put, lambda m: None)
        self.assertFalse(r.ok)
        self.assertFalse(r.retryable)
        self.assertTrue(r.error.startswith('out-of-range'))


class RetryTests(SocketCleanupMixin, unittest.TestCase):
    def run_retry(self, ad, clock, **kw):
        logs = []
        r = w.bind_web_socket_with_retry(
            'app', ad.get, ad.put, logs.append,
            sleep=clock.sleep, now=clock.now, **kw)
        self.track(r.sock)
        return r, logs

    def test_invalid_value_does_not_wait_and_logs_once(self):
        clock = FakeClock()
        r, logs = self.run_retry(FakeAppData(app_web_port='abc'), clock)
        self.assertFalse(r.ok)
        self.assertEqual(clock.t, 0)
        self.assertEqual(len(logs), 1)

    def test_busy_port_retries_whole_window_then_gives_up(self):
        port = free_port()
        self.track(hold(port))
        clock = FakeClock()
        r, logs = self.run_retry(FakeAppData(app_web_port=str(port)), clock)
        self.assertFalse(r.ok)
        self.assertEqual(r.port, port, 'must keep retrying the SAME port')
        self.assertEqual(r.source, 'configured')
        self.assertEqual(clock.t, w.DEFAULT_RETRY_SECONDS)
        # first failure + "will retry" + "giving up" - identical retry
        # lines are collapsed, so this is NOT one line per attempt.
        self.assertLessEqual(len(logs), 3)
        self.assertTrue(any('giving up' in m for m in logs))

    def test_port_freed_mid_window_binds_same_port(self):
        port = free_port()
        holder = hold(port)
        clock, n = FakeClock(), {'k': 0}

        def free_on_third_sleep():
            n['k'] += 1
            if n['k'] == 3:
                holder.close()

        clock.on_sleep = free_on_third_sleep
        r, _ = self.run_retry(FakeAppData(app_web_port=str(port)), clock)
        self.assertTrue(r.ok)
        self.assertEqual(r.port, port)
        self.assertLess(clock.t, w.DEFAULT_RETRY_SECONDS)

    def test_value_corrected_mid_window_is_picked_up(self):
        busy, good = free_port(), free_port()
        self.track(hold(busy))
        ad = FakeAppData(app_web_port=str(busy))
        clock, n = FakeClock(), {'k': 0}

        def fix_on_second_sleep():
            n['k'] += 1
            if n['k'] == 2:
                ad.store['app_web_port'] = str(good)

        clock.on_sleep = fix_on_second_sleep
        r, _ = self.run_retry(ad, clock)
        self.assertTrue(r.ok)
        self.assertEqual(r.port, good)

    def test_value_changed_to_invalid_mid_window_stops_retrying(self):
        busy = free_port()
        self.track(hold(busy))
        ad = FakeAppData(app_web_port=str(busy))
        clock = FakeClock()
        clock.on_sleep = lambda: ad.store.__setitem__('app_web_port', 'oops')
        r, _ = self.run_retry(ad, clock)
        self.assertFalse(r.ok)
        self.assertFalse(r.retryable)
        self.assertEqual(clock.t, w.DEFAULT_RETRY_INTERVAL)

    def test_success_returns_immediately(self):
        clock = FakeClock()
        r, _ = self.run_retry(FakeAppData(), clock,
                              start_port=free_port(), max_ports=8)
        self.assertTrue(r.ok)
        self.assertEqual(clock.t, 0)


class StopAppTests(unittest.TestCase):
    UUID = '11111111-2222-3333-4444-555555555555'

    def ini(self, text):
        fd, path = tempfile.mkstemp(suffix='.ini')
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_reads_uuid_from_package_ini(self):
        path = self.ini('[my_app]\nuuid = {}\nversion_major = 1\n'.format(
            self.UUID))
        self.assertEqual(w.read_app_uuid(path), self.UUID)

    def test_missing_ini_or_uuid_returns_none(self):
        self.assertIsNone(w.read_app_uuid('/nonexistent/package.ini'))
        self.assertIsNone(w.read_app_uuid(self.ini('[my_app]\nvendor = x\n')))

    def test_stop_sends_sdk_stop_action(self):
        path = self.ini('[my_app]\nuuid = {}\n'.format(self.UUID))
        calls, logs = [], []
        ok = w.stop_app(lambda p, v: calls.append((p, v)), logs.append,
                        package_ini=path)
        self.assertTrue(ok)
        self.assertEqual(calls, [('control/system/sdk/action',
                                  'stop ' + self.UUID)])

    def test_stop_without_uuid_returns_false(self):
        logs = []
        ok = w.stop_app(lambda p, v: None, logs.append,
                        package_ini='/nonexistent/package.ini')
        self.assertFalse(ok)
        self.assertTrue(any('uuid not found' in m for m in logs))

    def test_stop_request_error_returns_false(self):
        path = self.ini('[my_app]\nuuid = {}\n'.format(self.UUID))

        def denied(p, v):
            raise RuntimeError('denied')

        self.assertFalse(w.stop_app(denied, lambda m: None,
                                    package_ini=path))


class ModuleHygieneTests(unittest.TestCase):
    """Guards the properties the standard promises."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(SRC_DIR, 'web_port.py')) as f:
            cls.source = f.read()

    def test_parses_as_python_3_8(self):
        # The router runs Python 3.8.
        ast.parse(self.source, feature_version=(3, 8))

    def test_does_not_import_cp(self):
        tree = ast.parse(self.source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split('.')[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or '').split('.')[0])
        self.assertNotIn('cp', imported)

    def test_standard_library_only(self):
        allowed = {'configparser', 'os', 'socket', 'time'}
        tree = ast.parse(self.source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertIn(a.name.split('.')[0], allowed)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn((node.module or '').split('.')[0], allowed)

    def test_matches_published_checksum(self):
        """Fails if src/web_port.py was edited without updating the hash."""
        with open(os.path.join(SRC_DIR, 'web_port.py'), 'rb') as f:
            actual = hashlib.sha256(f.read()).hexdigest()
        with open(os.path.join(SRC_DIR, 'web_port.py.sha256')) as f:
            published = f.read().split()[0]
        self.assertEqual(actual, published)


if __name__ == '__main__':
    unittest.main()
