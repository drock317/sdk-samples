"""Reusable persistent dynamic web-port selection for SDK web apps.

This module is intentionally app-agnostic so the same convention can be
adopted by any SDK web application WITHOUT modifying it. It depends only on
the standard library plus injected `cp`-style callables (get_appdata /
put_appdata / log), so it is trivial to unit-test with stubs and carries no
app-specific logic.

CONVENTION
----------
Each app namespaces its web port in Application Data as:

    <app_name>_web_port          e.g. web_app_template_web_port

Application Data is shared at the device-configuration level, so a generic
`web_port` is deliberately NOT used.

RESOLUTION CONTRACT
-------------------
Case A -- configured value exists (`cp.get_appdata('<app>_web_port')`):
    The configured value is AUTHORITATIVE.
      * Bind exactly that port. Never scan from 8000. Never silently move.
      * If it cannot be bound, FAIL (return an error result). Do NOT
        overwrite the configured value. The caller must not start the web
        server on another port. This determinism matters because an admin
        may have an NCM LAN Manager connection pinned to that exact port.
      * A malformed / non-numeric / out-of-range configured value is treated
        as a configuration error and FAILS closed (does not fall back to
        discovery), so a typo can't silently relocate a pinned port.

Case B -- no configured value (first run):
    Discover a usable port by ACTUAL socket bind, starting at `start_port`
    (8000) and incrementing within a bounded range. The first port that
    binds wins. Once bound successfully, persist it via
    `cp.put_appdata('<app>_web_port', '<port>')`. This write is intentional:
    it records the discovered listening-port assignment, not a code default.
    On later restarts/reboots the persisted value is read back (Case A) and
    reused, even if 8000 later frees up.

NCM
---
Application Data remains the single source of truth. An admin can set
`<app>_web_port` via NCM group/device config to standardize the port across
a fleet; the app consumes the effective value on next startup. No NCM API
credentials are involved -- this only reads/writes NCOS Application Data.

FAILURE HANDLING (no restart loops)
-----------------------------------
SDK apps normally run with `restart = true`, so an app that EXITS on a web
port failure is restarted by NCOS every few seconds, forever. To avoid
spamming the log, failures are classified:

  * NOT retryable (`PortResult.retryable` False): the configured value is
    malformed / non-numeric / out of range. This is a configuration error
    that no amount of waiting will fix. Log it ONCE and do not start the web
    server.
  * Retryable (`retryable` True): the configured port is valid but could not
    be bound (e.g. briefly held by a previous instance during a redeploy), or
    first-run discovery found no free port. `bind_web_socket_with_retry()`
    retries IN-PROCESS on a short interval for a bounded time, re-reading
    App Data each attempt (so a fix pushed from NCM is picked up), and
    collapses repeated identical log lines. After the window it gives up
    once, with a single clear log line.

A web app with no listening port is useless, so on a final failure the app
should STOP ITSELF cleanly rather than linger half-alive. Do NOT sys.exit():
NCOS restarts an exited app (restart = true), which is the log-spam loop.
Instead call `stop_app()`, which asks the SDK to stop this app
(`control/system/sdk/action` = "stop <uuid>"). A stopped app is not
restarted by the restart policy, so the failure is logged once and the app
stays stopped until the admin fixes the value and starts it again (it will
also try again on the next router reboot, since auto_start applies).

If the stop request is rejected (stop_app() returns False, or the process is
still running afterwards), fall back to idling without the web server so
NCOS still has nothing to restart.

A configured port is never relocated: retrying only ever re-tries the SAME
configured port, it never scans for another one.

RACE AVOIDANCE
--------------
`bind_web_socket()` returns an already-bound, listening socket. The caller
hands that exact socket to HTTPServer (bind_and_activate=False), so the
successful bind itself is the authoritative availability test -- there is no
separate check-then-bind window.
"""

import configparser
import os
import socket
import time

# Bounded first-run discovery range. Small and deterministic.
DEFAULT_START_PORT = 8000
DEFAULT_MAX_PORTS = 64          # 8000..8063
MIN_VALID_PORT = 1
MAX_VALID_PORT = 65535

# Bounded in-process retry window for a valid-but-busy port.
DEFAULT_RETRY_SECONDS = 60
DEFAULT_RETRY_INTERVAL = 5


def appdata_field(app_name):
    """Return the namespaced appdata field name for an app."""
    return '{}_web_port'.format(app_name)


def parse_port(raw):
    """Parse a configured port value.

    Returns (port:int, None) when valid, or (None, reason:str) when the
    value is missing, non-numeric, or out of range. Accepts int or str.
    """
    if raw is None:
        return None, 'missing'
    text = str(raw).strip()
    if text == '':
        return None, 'missing'
    try:
        # int(text) rejects floats/hex/whitespace-embedded junk cleanly.
        port = int(text)
    except (TypeError, ValueError):
        return None, 'non-numeric: {!r}'.format(raw)
    if port < MIN_VALID_PORT or port > MAX_VALID_PORT:
        return None, 'out-of-range: {}'.format(port)
    return port, None


def _try_bind(port, reuse_addr=True, host='0.0.0.0'):
    """Attempt to bind+listen a TCP socket on `port`.

    Returns the listening socket on success, or None on failure. Sets
    SO_REUSEADDR before bind per SDK web standards.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if reuse_addr:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(5)
        return sock
    except OSError:
        try:
            sock.close()
        except Exception:
            pass
        return None


class PortResult(object):
    """Outcome of a port-resolution attempt.

    Attributes:
        ok (bool): True when `sock` is a bound, listening socket.
        port (int|None): The effective bound port when ok.
        sock (socket|None): The bound socket to hand to HTTPServer.
        source (str): 'configured' or 'discovered'.
        persisted (bool): True when a discovered port was written to appdata.
        error (str|None): Human-readable reason when not ok.
        retryable (bool): True when a failure may clear on its own (valid
            port briefly busy). False for a configuration error (malformed
            value) that waiting cannot fix.
    """

    def __init__(self, ok, port=None, sock=None, source='',
                 persisted=False, error=None, retryable=False):
        self.ok = ok
        self.port = port
        self.sock = sock
        self.source = source
        self.persisted = persisted
        self.error = error
        self.retryable = retryable


def bind_web_socket(app_name, get_appdata, put_appdata, log,
                    start_port=DEFAULT_START_PORT,
                    max_ports=DEFAULT_MAX_PORTS,
                    host='0.0.0.0'):
    """Resolve and bind the app's web port. Returns a PortResult.

    Args:
        app_name: App name used to build the appdata field.
        get_appdata: callable(name)->str|None (e.g. cp.get_appdata).
        put_appdata: callable(name, value)->None (e.g. cp.put_appdata).
        log: callable(msg) for operator-visible logging (e.g. cp.log).
        start_port: first port for first-run discovery.
        max_ports: bounded number of ports to probe during discovery.
        host: bind address.

    On success the caller owns result.sock and must pass it to HTTPServer
    with bind_and_activate=False (or close it). On failure result.sock is
    None and the caller must NOT start the web server.
    """
    field = appdata_field(app_name)

    # --- Case A: configured value is authoritative -----------------------
    raw = None
    try:
        raw = get_appdata(field)
    except Exception as e:
        # Reading appdata failed; treat as no configuration but log it.
        log('web_port: error reading appdata {}: {}'.format(field, e))
        raw = None

    if raw is not None and str(raw).strip() != '':
        port, reason = parse_port(raw)
        if port is None:
            msg = ('web_port: configured {}={!r} is invalid ({}). '
                   'Refusing to guess another port; web server not started.'
                   ).format(field, raw, reason)
            log(msg)
            # Configuration error: waiting will not fix it -> not retryable.
            return PortResult(ok=False, source='configured', error=reason,
                              retryable=False)

        sock = _try_bind(port, host=host)
        if sock is not None:
            log('web_port: bound configured port {} (from {})'.format(
                port, field))
            return PortResult(ok=True, port=port, sock=sock,
                              source='configured')

        # Configured port occupied -> fail safe, do NOT overwrite/relocate.
        msg = ('web_port: configured port {} (from {}) is not available. '
               'Not selecting another port and not overwriting {}. '
               'Web server not started.').format(port, field, field)
        log(msg)
        return PortResult(ok=False, port=port, source='configured',
                          error='configured-port-unavailable',
                          retryable=True)

    # --- Case B: first-run discovery -------------------------------------
    end_port = min(start_port + max_ports - 1, MAX_VALID_PORT)
    for port in range(start_port, end_port + 1):
        sock = _try_bind(port, host=host)
        if sock is None:
            continue
        # Bound successfully -> persist the discovered assignment.
        persisted = False
        try:
            put_appdata(field, str(port))
            persisted = True
            log('web_port: discovered and persisted {}={}'.format(field, port))
        except Exception as e:
            # Binding succeeded but persistence failed: still serve on the
            # bound port this run; log so the operator can set it in NCM.
            log('web_port: bound {} but failed to persist {}: {}'.format(
                port, field, e))
        return PortResult(ok=True, port=port, sock=sock,
                          source='discovered', persisted=persisted)

    msg = ('web_port: no available port in range {}-{}; '
           'web server not started.').format(start_port, end_port)
    log(msg)
    return PortResult(ok=False, source='discovered',
                      error='discovery-exhausted', retryable=True)


def read_app_uuid(package_ini=None):
    """Return this app's uuid from package.ini, or None if unavailable.

    Defaults to the package.ini beside this module (the app directory).
    """
    path = package_ini or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'package.ini')
    try:
        cfg = configparser.ConfigParser()
        cfg.read(path)
        for section in cfg.sections():
            value = cfg.get(section, 'uuid', fallback='').strip()
            if value:
                return value
    except Exception:
        pass
    return None


def stop_app(put, log, package_ini=None):
    """Ask the SDK to stop THIS app. Returns True if the request was sent.

    Use after a final web-port failure instead of sys.exit() (an exited app
    is restarted by NCOS every few seconds; a stopped one is not).

    Args:
        put: callable(path, value) -- e.g. cp.put.
        log: callable(msg).
        package_ini: optional path override (tests).

    A True return only means the request was sent without error. If the
    process is still running afterwards the stop was not honoured, and the
    caller should fall back to idling without the web server.
    """
    uuid = read_app_uuid(package_ini)
    if not uuid:
        log('web_port: cannot stop app: uuid not found in package.ini.')
        return False
    try:
        put('control/system/sdk/action', 'stop {}'.format(uuid))
    except Exception as e:
        log('web_port: stop request failed: {}'.format(e))
        return False
    log('web_port: requested SDK stop of this app (uuid {}).'.format(uuid))
    return True


def bind_web_socket_with_retry(app_name, get_appdata, put_appdata, log,
                               retry_seconds=DEFAULT_RETRY_SECONDS,
                               retry_interval=DEFAULT_RETRY_INTERVAL,
                               sleep=time.sleep, now=time.time,
                               **bind_kwargs):
    """bind_web_socket() plus a bounded in-process retry for busy ports.

    Use this instead of bind_web_socket() at app startup. It avoids an
    exit-and-restart loop (NCOS restarts apps with restart=true every few
    seconds) while still covering a port that is only briefly held, such as
    during a redeploy.

      * Success: returns the PortResult immediately.
      * Non-retryable failure (malformed configured value): returns at once;
        the reason is logged exactly once.
      * Retryable failure: retries every `retry_interval` seconds for up to
        `retry_seconds`. App Data is re-read on every attempt, and the SAME
        configured port is retried (never a different one). Identical log
        lines are collapsed, and one final line is logged if it gives up.

    Extra keyword arguments (start_port, max_ports, host) are passed through
    to bind_web_socket(). `sleep` / `now` are injectable for testing.

    On a final failure the caller should stop the app with stop_app(), not
    sys.exit() (see FAILURE HANDLING above).
    """
    last = {'msg': None}

    def dedup_log(msg):
        # Collapse back-to-back identical lines across retry attempts.
        if msg != last['msg']:
            log(msg)
            last['msg'] = msg

    result = bind_web_socket(app_name, get_appdata, put_appdata, dedup_log,
                             **bind_kwargs)
    if result.ok or not result.retryable:
        return result

    deadline = now() + retry_seconds
    log('web_port: will retry for up to {}s (every {}s) in case the port is '
        'only briefly held.'.format(retry_seconds, retry_interval))
    attempts = 1
    while now() < deadline:
        sleep(retry_interval)
        attempts += 1
        result = bind_web_socket(app_name, get_appdata, put_appdata,
                                 dedup_log, **bind_kwargs)
        if result.ok or not result.retryable:
            if result.ok:
                log('web_port: bound after {} attempts.'.format(attempts))
            return result

    log('web_port: giving up after {} attempts over {}s ({}). Web server '
        'NOT started. Fix {}.'.format(attempts, retry_seconds,
                                      result.error,
                                      appdata_field(app_name)))
    return result
