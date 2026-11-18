"""Run the shipped examples against a stub `cp` and real HTTP requests.

This proves the documented integration patterns work end to end, including
two apps sharing one device (the second must step to the next free port).
"""
import json
import os
import sys
import types
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, os.path.join(ROOT, 'examples'))
sys.path.insert(0, HERE)

from test_web_port import free_port  # noqa: E402


class StubCp(types.ModuleType):
    """Minimal stand-in for the SDK cp module (App Data + log + put)."""

    def __init__(self):
        types.ModuleType.__init__(self, 'cp')
        self.appdata = {}
        self.logs = []
        self.puts = []

    def log(self, msg):
        self.logs.append(str(msg))

    def get_appdata(self, name):
        return self.appdata.get(name)

    def put_appdata(self, name, value):
        self.appdata[name] = value

    def put(self, path, value):
        self.puts.append((path, value))


STUB = StubCp()
sys.modules['cp'] = STUB

import minimal_web_app  # noqa: E402
import retrofit_custom_server_class  # noqa: E402


def fetch(port, path='/'):
    url = 'http://127.0.0.1:{}{}'.format(port, path)
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


class ExampleTests(unittest.TestCase):
    def setUp(self):
        STUB.appdata.clear()
        del STUB.logs[:]
        self._servers = []

    def tearDown(self):
        for s in self._servers:
            try:
                s.shutdown()
                s.server_close()
            except Exception:
                pass

    def start(self, module, **kw):
        server, result = module.start_web_server(**kw)
        if server is not None:
            self._servers.append(server)
        return server, result

    def test_minimal_app_serves_and_reports_port(self):
        start = free_port()
        server, result = self.start(minimal_web_app,
                                    start_port=start, max_ports=8)
        self.assertIsNotNone(server)
        status, body = fetch(result.port, '/api/status')
        self.assertEqual(status, 200)
        info = json.loads(body.decode('utf-8'))
        self.assertEqual(info['web']['port'], result.port)
        self.assertEqual(info['web']['source'], 'discovered')
        self.assertEqual(STUB.appdata['my_web_app_web_port'],
                         str(result.port))
        self.assertTrue(any('started on TCP port' in m for m in STUB.logs))

    def test_restart_reuses_persisted_port(self):
        start = free_port()
        s1, r1 = self.start(minimal_web_app, start_port=start, max_ports=8)
        s1.shutdown()
        s1.server_close()
        self._servers.remove(s1)
        # Same App Data, new "boot": must come back as 'configured'.
        s2, r2 = self.start(minimal_web_app, start_port=start + 5,
                            max_ports=8)
        self.assertEqual(r2.port, r1.port)
        self.assertEqual(r2.source, 'configured')

    def test_admin_override_wins(self):
        want = free_port()
        STUB.appdata['my_web_app_web_port'] = str(want)
        server, result = self.start(minimal_web_app)
        self.assertEqual((result.port, result.source), (want, 'configured'))
        self.assertEqual(fetch(want)[0], 200)

    def test_two_apps_on_one_device_get_different_ports(self):
        start = free_port()
        _, a = self.start(minimal_web_app, app_name='app_one',
                          start_port=start, max_ports=16)
        _, b = self.start(retrofit_custom_server_class,
                          app_name='app_two', start_port=start,
                          max_ports=16)
        self.assertNotEqual(a.port, b.port)
        self.assertEqual(STUB.appdata['app_one_web_port'], str(a.port))
        self.assertEqual(STUB.appdata['app_two_web_port'], str(b.port))
        self.assertEqual(fetch(a.port)[0], 200)
        self.assertEqual(fetch(b.port)[0], 200)

    def test_retrofit_server_sets_server_attributes(self):
        server, result = self.start(retrofit_custom_server_class,
                                    start_port=free_port(), max_ports=8)
        self.assertEqual(server.server_port, result.port)
        self.assertTrue(server.server_name)
        self.assertEqual(json.loads(fetch(result.port)[1].decode())['app'],
                         'my_existing_app')

    def test_malformed_value_returns_no_server_and_one_log(self):
        STUB.appdata['my_web_app_web_port'] = 'abc'
        server, result = self.start(minimal_web_app)
        self.assertIsNone(server)
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)
        self.assertEqual(STUB.appdata['my_web_app_web_port'], 'abc')


if __name__ == '__main__':
    unittest.main()
