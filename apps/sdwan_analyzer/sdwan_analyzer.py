# Ericsson Cradlepoint NCOS SDK Application
#
# SD-WAN Analyzer -- BACKEND build (v0.2.x).
#
# Scope of this build: full backend. Targeted collectors, normalization with
# real compiled-rule (xpolicy) parsing, cross-object correlation, logical
# Traffic Steering grouping, change-ready semantic fingerprints, a structured
# cached snapshot, and section JSON endpoints. Durable historical storage
# (format/retention) and the polished UI remain deferred; the HistoryStore
# stays in-memory (compact samples) for now.
#
# Architecture (one-directional data flow):
#   collectors -> normalize -> storage -> service (app API) -> web (UI)
# The browser talks only to `web`; `web` talks only to `service`. The browser
# never reaches NCOS APIs.

import signal
import sys
import time

import cp

from analyzer import VERSION
from analyzer.service import AnalyzerService
from analyzer.web import DevWebServer

# Background collector cadence. Two intervals so heavy/slow trees
# (loadbalancers, bonding, QoE, config) poll far less often than lightweight
# topology/state. The service owns polling; the web layer only serves the
# cached snapshot (COLLECTION PERFORMANCE / SAFETY). Values are the service
# defaults; kept here as the single place the loop reads them.
_running = True


def _handle_signal(signum, frame):
    global _running
    cp.log('sdwan_analyzer: signal {} received, shutting down'.format(signum))
    _running = False


def main():
    cp.log('sdwan_analyzer: starting backend build v{}'.format(VERSION))

    # SIGTERM is how the router asks the app to stop. No KeyboardInterrupt on
    # the router (no keyboard), so handle SIGTERM/SIGINT explicitly.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except Exception as e:
            cp.log('sdwan_analyzer: could not set handler for {}: {}'.format(
                sig, e))

    service = AnalyzerService()
    server = DevWebServer(service)
    # Let /api/status report the effective listening port.
    service.set_web_port_provider(server.port_status)

    # Web startup may fail safely (e.g. a pinned configured port is
    # occupied). The rest of the app keeps collecting either way.
    if not server.start():
        cp.log('sdwan_analyzer: continuing without web interface '
               '(see web_port log above)')

    # Background collector: the service owns all router polling. It runs the
    # lightweight (fast) topology pass every fast_interval and the heavy
    # (slow) runtime+config pass every slow_interval, then recomposes and
    # caches one normalized snapshot. Web requests read only the cache.
    fast_interval = service.fast_interval()
    slow_interval = service.slow_interval()

    # Prime both caches once at startup so the first snapshot is complete.
    try:
        service.refresh_heavy()
        service.refresh_base()
        first = service.recompose()
        cp.log('sdwan_analyzer: initial snapshot composed '
               '(paths={}, logical_rules={})'.format(
                   first.get('system_summary', {}).get('wan_path_count'),
                   first.get('system_summary', {}).get('logical_rule_count')))
    except Exception as e:
        cp.log('sdwan_analyzer: initial compose error: {}'.format(e))

    last_slow = time.time()
    # time.sleep avoids a spin-wait (coding-standards memory rules); sleep in
    # 1s slices so SIGTERM shutdown stays responsive.
    while _running:
        try:
            service.refresh_base()
            now = time.time()
            if now - last_slow >= slow_interval:
                service.refresh_heavy()
                last_slow = now
            snap = service.recompose()
            summ = snap.get('system_summary', {}) if snap else {}
            cp.log('sdwan_analyzer: snapshot recomposed '
                   '(paths={}, bonds={}, logical_rules={})'.format(
                       summ.get('wan_path_count'), summ.get('bond_count'),
                       summ.get('logical_rule_count')))
        except Exception as e:
            cp.log('sdwan_analyzer: collector loop error: {}'.format(e))

        slept = 0
        while _running and slept < fast_interval:
            time.sleep(1)
            slept += 1

    server.stop()
    cp.log('sdwan_analyzer: stopped')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        cp.log('sdwan_analyzer: fatal error: {}'.format(e))
        sys.exit(1)
