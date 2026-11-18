"""Minimal SDK web app using the dynamic web port standard.

Copy this pattern into a NEW app. It is the smallest complete integration:

  1. resolve + bind the port with web_port.bind_web_socket_with_retry()
  2. hand the PRE-BOUND socket to HTTPServer (no check-then-bind race)
  3. on a final failure, stop the app via the SDK (never sys.exit())

Put web_port.py next to this file (same folder as cp.py). Set APP_NAME to
your app's folder / package.ini section name; the App Data field becomes
"<APP_NAME>_web_port".

Python 3.8 compatible (the router runs 3.8).
"""
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import cp
import web_port

APP_NAME = 'my_web_app'


class Handler(BaseHTTPRequestHandler):
    # Filled in by start_web_server() so /api/status can report the port.
    port_info = {}

    def do_GET(self):
        if self.path.split('?', 1)[0] == '/api/status':
            body = json.dumps({
                'app': APP_NAME,
                # Surface the effective port and where it came from.
                'web': dict(self.port_info),
            }).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
        else:
            body = ('<h1>%s</h1>' % APP_NAME).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # No stdout on the router: route access logs through cp.log.
        cp.log('web: ' + (fmt % args))


def start_web_server(app_name=APP_NAME, **bind_kwargs):
    """Bind the port and start serving in a daemon thread.

    Returns (httpd, result). httpd is None when the port could not be
    bound; result.error says why (it has already been logged).
    `bind_kwargs` (start_port, max_ports, host) pass through to the helper.
    """
    result = web_port.bind_web_socket_with_retry(
        app_name, cp.get_appdata, cp.put_appdata, cp.log, **bind_kwargs)
    if not result.ok:
        return None, result

    # Subclass per server so each instance reports its own port.
    handler = type('BoundHandler', (Handler,), {
        'port_info': {'port': result.port, 'source': result.source},
    })
    httpd = HTTPServer(('0.0.0.0', result.port), handler,
                       bind_and_activate=False)
    httpd.socket.close()                # close the unused placeholder socket
    httpd.socket = result.sock          # hand over the pre-bound socket
    Thread(target=httpd.serve_forever, daemon=True).start()
    cp.log('{} web interface started on TCP port {} (source: {})'.format(
        app_name, result.port, result.source))
    return httpd, result


def stop_or_idle(result, app_name=APP_NAME):
    """Final-failure policy. Never exits; never keeps serving without a port.

    A web app with no port is useless, and sys.exit() would make NCOS
    restart it every few seconds (restart = true). So stop the app through
    the SDK; a stopped app is not restarted.
    """
    cp.log('web: NOT started ({}); stopping the app. Fix {} and start it '
           'again.'.format(result.error, web_port.appdata_field(app_name)))
    if web_port.stop_app(cp.put, cp.log):
        time.sleep(30)                  # the SDK terminates this process
    # Only reached if the SDK did not honour the stop: idle, never exit.
    cp.log('web: SDK stop not honoured; idling without the web server.')
    while True:
        time.sleep(3600)


def main():
    cp.log('Starting {}'.format(APP_NAME))
    httpd, result = start_web_server()
    if httpd is None:
        stop_or_idle(result)            # does not return
    # ... your app's real work goes here ...
    while True:
        time.sleep(1)


if __name__ == '__main__':
    main()
