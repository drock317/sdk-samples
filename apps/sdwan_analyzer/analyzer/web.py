"""Presentation layer: minimal http.server dev/status page + JSON API.

Deliberately minimal — this is a development/status page, NOT the eventual
UI. Per web-standards: Python's built-in http.server only (no Flask/Node),
port 8000, SO_REUSEADDR, runs in a daemon thread.

The handler calls ONLY AnalyzerService. It never imports cp or touches NCOS
directly, so the browser never talks to NCOS. All data crosses the
service boundary as plain JSON.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import cp

from . import web_port

# App name used for the namespaced appdata field (<app_name>_web_port).
APP_NAME = 'sdwan_analyzer'

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
<li><a href="/api/snapshot">/api/snapshot</a> (latest cached normalized snapshot)</li>
<li><a href="/api/wan">/api/wan</a> (paths, physical, overlays, underlay map)</li>
<li><a href="/api/swans">/api/swans</a> (SWANS latency/jitter)</li>
<li><a href="/api/qoe">/api/qoe</a> (SD-WAN transport QoE)</li>
<li><a href="/api/traffic_classes">/api/traffic_classes</a></li>
<li><a href="/api/traffic_steering">/api/traffic_steering</a> (logical rules, events, stats, flow alignment)</li>
<li><a href="/api/loadbalancers">/api/loadbalancers</a></li>
<li><a href="/api/bonding">/api/bonding</a> (WBOND detail)</li>
<li><a href="/api/identities">/api/identities</a> (destinations)</li>
<li><a href="/api/changes">/api/changes</a> (topology/config change flags)</li>
<li><a href="/api/history">/api/history</a></li>
</ul>
</body>
</html>
"""


def _make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, obj, code=200):
            body = json.dumps(obj).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html, code=200):
            body = html.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # Map of /api/<name> section endpoints -> snapshot section key.
        # Every handler reads ONLY the cached snapshot via service.section();
        # a web request NEVER triggers router collection.
        _SECTION_ROUTES = {
            '/api/wan': 'wan',
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
                path = self.path.split('?', 1)[0]
                if path in ('/', '/index.html'):
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
                elif path.startswith('/api/history'):
                    self._send_json({'samples': service.history(limit=100)})
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
        result = web_port.bind_web_socket(
            self._app_name, self._get_appdata, self._put_appdata, cp.log)

        self._port_source = result.source
        if not result.ok:
            # Fail web startup safely. The rest of the app keeps running.
            self._last_error = result.error
            cp.log('web: web server NOT started ({}).'.format(result.error))
            return False

        self._port = result.port
        try:
            # Hand the pre-bound socket to HTTPServer without re-binding.
            self._httpd = HTTPServer(('0.0.0.0', result.port),
                                     _make_handler(self._service),
                                     bind_and_activate=False)
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
