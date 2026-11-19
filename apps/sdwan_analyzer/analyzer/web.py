"""Presentation layer: minimal http.server dev/status page + JSON API.

Deliberately minimal — this is a development/status page, NOT the eventual
UI. Per web-standards: Python's built-in http.server only (no Flask/Node),
port chosen by the web_port helper (App Data sdwan_analyzer_web_port, else
discovered from 8000), SO_REUSEADDR, runs in a daemon thread.

The handler calls ONLY AnalyzerService. It never imports cp or touches NCOS
directly, so the browser never talks to NCOS. All data crosses the
service boundary as plain JSON.
"""

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import cp

from . import web_port
from .query import QueryError

# Exact route for GET /api/recordings/<job_id>/samples. The id is matched
# loosely here and validated against the generated format in the store, so a
# traversal attempt is a plain 404 and never reaches a filesystem join.
_SAMPLES_ROUTE = re.compile(r'^/api/recordings/([^/]+)/samples\Z')

# App name used for the namespaced appdata field (<app_name>_web_port).
APP_NAME = 'sdwan_analyzer'

# Per-connection socket timeout (seconds). The server is deliberately
# SINGLE-THREADED (shared service/Recording/store state has no locking
# design yet), so one client that connects but never sends a request would
# otherwise hold the only handler indefinitely (seen with Chrome speculative
# connections on a live R2400). Every accepted socket therefore gets this
# short timeout: an idle client is dropped after it, and queued requests
# are served. 5 s is long enough for a normal LAN browser/curl request line
# and body, short enough that a stall is barely noticeable. Applies to this
# web server only; the 15 s NCOS collector timeout is unrelated and unchanged.
CLIENT_SOCKET_TIMEOUT_S = 5


class _BoundedHTTPServer(HTTPServer):
    """HTTPServer that bounds every accepted client socket with a timeout.

    Still single-threaded (no ThreadingMixIn): requests run one at a time.
    """

    def get_request(self):
        conn, addr = self.socket.accept()
        try:
            conn.settimeout(CLIENT_SOCKET_TIMEOUT_S)
        except Exception:
            pass
        return conn, addr

    def handle_error(self, request, client_address):
        # Default prints a traceback to stderr; the router has no console.
        cp.log('web: connection error from {}'.format(client_address[0]))


def app_package_ini():
    """Path to this app's package.ini.

    web_port.py lives in the analyzer/ subpackage, but package.ini (which
    holds the app uuid web_port.stop_app() needs) is one level up in the app
    folder. web_port.py must stay byte-identical across apps, so its default
    lookup (beside the module) is not changed; callers pass this path instead.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), 'package.ini')

# Minimal dev page. Intentionally plain — the polished UI (and the template
# static/ design system) is DEFERRED to the presentation phase.
_DEV_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SD-WAN Analyzer (backend)</title>
</head>
<body>
<h1>SD-WAN Analyzer</h1>
<p>Backend build. Structured cached snapshot + section JSON APIs; UI deferred.</p>
<ul>
<li><a href="/api/status">/api/status</a> (includes effective web port)</li>
<li><a href="/api/health">/api/health</a></li>
<li><a href="/api/snapshot">/api/snapshot</a> (latest MERGED current state, not the last observation)</li>
<li><a href="/api/wan">/api/wan</a> (paths, physical, overlays, underlay map, current interface counters)</li>
<li><a href="/api/cellular">/api/cellular</a> (current cellular/CA telemetry)</li>
<li><a href="/api/swans">/api/swans</a> (SWANS latency/jitter)</li>
<li><a href="/api/qoe">/api/qoe</a> (SD-WAN transport QoE)</li>
<li><a href="/api/traffic_classes">/api/traffic_classes</a></li>
<li><a href="/api/traffic_steering">/api/traffic_steering</a> (logical rules, events, stats, flow alignment)</li>
<li><a href="/api/loadbalancers">/api/loadbalancers</a></li>
<li><a href="/api/bonding">/api/bonding</a> (WBOND detail)</li>
<li><a href="/api/identities">/api/identities</a> (destinations)</li>
<li><a href="/api/changes">/api/changes</a> (topology/config change flags)</li>
<li><a href="/api/storage">/api/storage</a> (Storage Inspector: memory counts, app file metadata; generated on demand)</li>
<li><a href="/api/history">/api/history</a> (durable, paginated: limit, start, end, sample_source, cursor)</li>
<li><a href="/api/recordings">/api/recordings</a>, <a href="/api/recordings/active">/active</a>, /api/recordings/&lt;id&gt;, /api/recordings/&lt;id&gt;/samples (paginated); POST /api/recordings/start, /api/recordings/&lt;id&gt;/stop (backend-owned, max 30 min)</li>
<li>POST /api/refresh/&lt;group&gt; (manual: traffic_classes, steering, bonding, wan_paths, overview)</li>
</ul>
</body>
</html>
"""


def _make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        # StreamRequestHandler.setup() applies this to the connection, so
        # the bound holds even if the handler is used with another server.
        timeout = CLIENT_SOCKET_TIMEOUT_S
        # One request per connection: finite, never kept alive.
        protocol_version = 'HTTP/1.0'

        def _send_json(self, obj, code=200):
            body = json.dumps(obj).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html, code=200):
            body = html.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        # Map of /api/<name> section endpoints -> snapshot section key.
        # Every handler reads ONLY the cached snapshot via service.section();
        # a web request NEVER triggers router collection.
        _SECTION_ROUTES = {
            '/api/wan': 'wan',
            '/api/cellular': 'cellular',
            '/api/swans': 'swans',
            '/api/qoe': 'qoe',
            '/api/traffic_classes': 'traffic_classes',
            '/api/traffic_steering': 'traffic_steering',
            '/api/loadbalancers': 'loadbalancers',
            '/api/bonding': 'bonding',
            '/api/identities': 'identities',
            '/api/changes': 'changes',
        }

        def do_GET(self):
            try:
                path, _, raw_query = self.path.partition('?')
                m = _SAMPLES_ROUTE.match(path)
                if m is not None:
                    self._send_json(service.query_recording_samples(
                        m.group(1), raw_query))
                elif path == '/api/history':
                    self._send_json(service.query_history(raw_query))
                elif path in ('/', '/index.html'):
                    self._send_html(_DEV_PAGE)
                elif path in ('/api/status', '/api/health'):
                    self._send_json(service.status())
                elif path == '/api/snapshot':
                    snap = service.snapshot()
                    if snap is None:
                        self._send_json(
                            {'error': 'snapshot not ready'}, code=503)
                    else:
                        self._send_json(snap)
                elif path in self._SECTION_ROUTES:
                    if service.snapshot() is None:
                        self._send_json(
                            {'error': 'snapshot not ready'}, code=503)
                    else:
                        self._send_json(
                            service.section(self._SECTION_ROUTES[path]))
                elif path == '/api/storage':
                    # Generated live on every call; nothing stored/cached.
                    self._send_json(service.storage_report())
                elif path == '/api/recordings':
                    self._send_json({'recordings': service.list_recordings()})
                elif path == '/api/recordings/active':
                    self._send_json({'recording': service.active_recording()})
                elif path.startswith('/api/recordings/'):
                    rec = service.get_recording(
                        path[len('/api/recordings/'):])
                    if rec is None:
                        self._send_json({'error': 'not found'}, code=404)
                    else:
                        self._send_json(rec)
                else:
                    self._send_json({'error': 'not found'}, code=404)
            except QueryError as e:
                # Client error (400 / 404 / 410): a fixed message, never a
                # filesystem path or traceback.
                try:
                    self._send_json({'error': e.message}, code=e.status)
                except Exception:
                    pass
            except Exception as e:
                cp.log('web: request error on {}: {}'.format(self.path, e))
                try:
                    self._send_json({'error': 'internal error'}, code=500)
                except Exception:
                    pass

        def _read_json(self):
            """Parse a JSON-object request body ({} if empty), else None."""
            try:
                n = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(min(n, 4096)) if n > 0 else b'{}'
                body = json.loads(raw.decode('utf-8'))
                return body if isinstance(body, dict) else None
            except Exception:
                return None

        @staticmethod
        def _rec_code(result):
            if result.get('ok'):
                return 200
            return {'invalid': 400, 'not_found': 404,
                    'active_exists': 409, 'not_active': 409}.get(
                        result.get('error'), 400)

        def do_POST(self):
            # The only web-triggered collection: an explicit manual refresh
            # of one group, POST /api/refresh/<group> (5 s cooldown/group).
            try:
                path = self.path.split('?', 1)[0]
                prefix = '/api/refresh/'
                if path == '/api/recordings/start':
                    body = self._read_json()
                    if body is None:
                        self._send_json({'ok': False, 'error': 'invalid',
                                         'message': 'JSON object required'},
                                        code=400)
                        return
                    result = service.start_recording(
                        label=body.get('label', ''),
                        notes=body.get('notes', ''),
                        max_duration_sec=body.get('max_duration_sec'),
                        created_by_ui=True)
                    self._send_json(result, code=self._rec_code(result))
                elif (path.startswith('/api/recordings/')
                      and path.endswith('/stop')):
                    job_id = path[len('/api/recordings/'):-len('/stop')]
                    result = service.stop_recording(job_id)
                    self._send_json(result, code=self._rec_code(result))
                elif path.startswith(prefix):
                    result = service.refresh(path[len(prefix):])
                    code = 200 if result.get('ok') else (
                        429 if result.get('error') == 'cooldown' else 400)
                    self._send_json(result, code=code)
                else:
                    self._send_json({'error': 'not found'}, code=404)
            except Exception as e:
                cp.log('web: request error on {}: {}'.format(self.path, e))
                try:
                    self._send_json({'error': 'internal error'}, code=500)
                except Exception:
                    pass

        def log_message(self, fmt, *args):
            # Route access logs through cp.log (no stdout on router).
            cp.log('web: ' + (fmt % args))

    return Handler


class DevWebServer(object):
    """Runs the dev/status server in a daemon thread.

    Port selection is delegated to the reusable web_port helper:
      * a configured <app>_web_port in appdata is authoritative (bind-exact
        or fail), and
      * first-run discovery scans from 8000 by real bind and persists the
        result.
    The helper returns an already-bound socket, which we hand to HTTPServer
    (bind_and_activate=False) so the bind itself is the availability test --
    no check-then-bind race.

    start() returns False when no port could be bound. It does NOT exit and
    does NOT decide what the app does next: the caller (sdwan_analyzer.main)
    applies the final-failure policy (stop the app via web_port.stop_app).
    A malformed configured value fails at once; a valid but busy port is
    retried (same port only) for about 60 s first, so start() can block for
    that long on a held port.
    """

    def __init__(self, service, app_name=APP_NAME,
                 get_appdata=None, put_appdata=None):
        self._service = service
        self._app_name = app_name
        # Injectable for tests; default to the real cp appdata functions.
        self._get_appdata = get_appdata if get_appdata is not None else cp.get_appdata
        self._put_appdata = put_appdata if put_appdata is not None else cp.put_appdata
        self._httpd = None
        self._thread = None
        self._port = None            # effective bound port, or None
        self._port_source = None     # 'configured' | 'discovered' | None
        self._last_error = None

    def effective_port(self):
        """Return the effective listening port, or None if not started."""
        return self._port

    def port_status(self):
        """Introspection payload for the runtime/health API."""
        return {
            'effective_port': self._port,
            'source': self._port_source,
            'running': self._httpd is not None,
            'error': self._last_error,
        }

    def start(self):
        result = web_port.bind_web_socket_with_retry(
            self._app_name, self._get_appdata, self._put_appdata, cp.log)

        self._port_source = result.source
        if not result.ok:
            # No port. Report it; the caller applies the stop-the-app policy.
            self._last_error = result.error
            cp.log('web: web server NOT started ({}).'.format(result.error))
            return False

        self._port = result.port
        try:
            # Hand the pre-bound socket to HTTPServer without re-binding.
            self._httpd = _BoundedHTTPServer(('0.0.0.0', result.port),
                                     _make_handler(self._service),
                                     bind_and_activate=False)
            # Close the unused placeholder socket HTTPServer created, then
            # adopt the pre-bound one (otherwise one fd leaks per server).
            self._httpd.socket.close()
            self._httpd.socket = result.sock
            self._thread = threading.Thread(
                target=self._httpd.serve_forever, daemon=True)
            self._thread.start()
            cp.log('SD-WAN Analyzer web interface started on TCP port {}'
                   .format(result.port))
            return True
        except Exception as e:
            self._last_error = str(e)
            cp.log('web: failed to activate server on port {}: {}'.format(
                result.port, e))
            try:
                result.sock.close()
            except Exception:
                pass
            self._httpd = None
            self._port = None
            return False

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
                cp.log('web: dev server stopped (port {})'.format(self._port))
            except Exception as e:
                cp.log('web: error stopping server: {}'.format(e))
            finally:
                self._httpd = None
