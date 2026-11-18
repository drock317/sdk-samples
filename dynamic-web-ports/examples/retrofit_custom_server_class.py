"""Retrofit pattern: an EXISTING app that already subclasses HTTPServer.

Many apps (Speedtest Analyzer is one) define their own server class, for
example to work around NCOS hostnames that cannot be encoded as a DNS label.
HTTPServer.server_bind() normally sets `server_name` / `server_port`, but it
is SKIPPED when the server is built with bind_and_activate=False. So the
subclass needs a small `adopt_bound_socket()` that sets the same attributes
for the pre-bound socket the web_port helper returns.

What changes in an existing app (and nothing else):

    BEFORE                                   AFTER
    PORT = 8000                              (PORT stays only as first-run start)
    server = MyServer(('', PORT), Handler)   result = web_port.bind_web_socket_with_retry(...)
                                             server = MyServer(('', result.port), Handler,
                                                               bind_and_activate=False)
                                             server.adopt_bound_socket(result.sock)

Python 3.8 compatible.
"""
import json
import socket
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import cp
import web_port

APP_NAME = 'my_existing_app'


class ResilientHTTPServer(HTTPServer):
    """HTTPServer with a numeric fallback for unencodable router hostnames."""

    def server_bind(self):
        # Original behavior, kept for the (now unused) self-binding path.
        HTTPServer.server_bind(self)

    def adopt_bound_socket(self, sock):
        """Adopt an already-bound, listening socket (web_port standard).

        Sets what server_bind() would have set. Call right after building
        the server with bind_and_activate=False.
        """
        # HTTPServer.__init__ created an unbound placeholder socket when we
        # passed bind_and_activate=False. Close it so it does not leak.
        try:
            self.socket.close()
        except Exception:
            pass
        self.socket = sock
        self.server_address = sock.getsockname()
        host, port = self.server_address[:2]
        try:
            self.server_name = socket.getfqdn(host)
        except UnicodeError as e:
            # NCOS can expose a hostname that is not a valid DNS label; the
            # numeric listener address is enough for LAN access.
            self.server_name = host or '0.0.0.0'
            cp.log('Web server hostname lookup unavailable; using {}: {}'
                   .format(self.server_name, e))
        self.server_port = port


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'app': APP_NAME}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        cp.log('web: ' + (fmt % args))


def start_web_server(app_name=APP_NAME, **bind_kwargs):
    """Returns (server, result); server is None if the port was unavailable."""
    result = web_port.bind_web_socket_with_retry(
        app_name, cp.get_appdata, cp.put_appdata, cp.log, **bind_kwargs)
    if not result.ok:
        return None, result

    server = ResilientHTTPServer(('', result.port), Handler,
                                 bind_and_activate=False)
    server.adopt_bound_socket(result.sock)
    Thread(target=server.serve_forever, daemon=True).start()
    cp.log('Web server started on port {} (source: {})'.format(
        result.port, result.source))
    return server, result


def main():
    server, result = start_web_server()
    if server is None:
        # Same final-failure policy as examples/minimal_web_app.py:
        # stop the app through the SDK, never sys.exit().
        cp.log('Web server NOT started ({}). Stopping the app.'.format(
            result.error))
        if web_port.stop_app(cp.put, cp.log):
            time.sleep(30)
        cp.log('SDK stop not honoured; idling without the web server.')
        while True:
            time.sleep(3600)
    while True:
        time.sleep(1)


if __name__ == '__main__':
    main()
