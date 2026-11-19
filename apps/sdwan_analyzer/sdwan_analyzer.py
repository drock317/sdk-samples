# Ericsson Cradlepoint NCOS SDK Application
#
# SD-WAN Analyzer -- BACKEND build (v0.3.x): startup + scheduled + manual collection.
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

from analyzer import VERSION, web_port
from analyzer.service import AnalyzerService
from analyzer.web import DevWebServer, app_package_ini

_running = True
_service = None

# Durable telemetry root, relative to the app directory (never shipped:
# listed in buildignore).
TELEMETRY_ROOT = 'tmp/telemetry'


def _handle_signal(signum, frame):
    global _running
    cp.log('sdwan_analyzer: signal {} received, shutting down'.format(signum))
    _running = False
    # From here on storage must not (re)create directories: an upgrade may
    # remove the telemetry tree while this old process is still stopping.
    svc = _service
    if svc is not None:
        try:
            svc.begin_shutdown()
        except Exception:
            pass


def _configured_schedule_minutes():
    """Optional App Data sdwan_analyzer_interval_minutes (5/15/30/60).

    Read-only: defaults are applied in code and never written to appdata.
    Anything missing/invalid falls back to the service default.
    """
    try:
        raw = cp.get_appdata('sdwan_analyzer_interval_minutes')
        return int(str(raw).strip()) if raw not in (None, '') else None
    except Exception:
        return None


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

    # Durable telemetry lives under the app-local runtime dir (tmp/telemetry):
    # it survives an app restart and a router reboot, and may be wiped by an
    # application update/reinstall (accepted). If it cannot be initialised the
    # service falls back to in-memory stores and keeps running.
    service = AnalyzerService(schedule_minutes=_configured_schedule_minutes(),
                              telemetry_root=TELEMETRY_ROOT)
    global _service
    _service = service
    server = DevWebServer(service)
    # Let /api/status report the effective listening port.
    service.set_web_port_provider(server.port_status)

    # Web startup: a configured port that is malformed fails at once; one
    # that is valid but busy is retried (same port only) for ~60 s inside
    # start(). If there is still no port, this app has nothing to offer (its
    # snapshot and history are only reachable through the web API), so it
    # STOPS ITSELF via the SDK. It must not sys.exit(): restart = true would
    # make NCOS relaunch it every few seconds and flood the log. A stopped app
    # is not restarted. The reason has already been logged by web_port.
    if not server.start():
        cp.log('sdwan_analyzer: web interface NOT started ({}); stopping the '
               'app. Fix sdwan_analyzer_web_port and start it again.'.format(
                   server.port_status().get('error')))
        # package.ini is one level above the analyzer/ package web_port lives in.
        if web_port.stop_app(cp.put, cp.log, package_ini=app_package_ini()):
            # The SDK sends SIGTERM, which clears _running. Wait in 1s slices
            # so a successful stop ends the app promptly and cleanly.
            for _ in range(30):
                if not _running:
                    break
                time.sleep(1)
        if not _running:
            # Stop honoured: leave now, before any collection work starts.
            service.shutdown()
            cp.log('sdwan_analyzer: stopped (no web interface)')
            return
        # The SDK did not honour the stop. Keep collecting without the web
        # interface (never exit: that would start a restart loop).
        cp.log('sdwan_analyzer: SDK stop request was not honoured; '
               'continuing without the web interface.')

    # Data acquisition (no separate cache poller, no 15 s/120 s loops):
    #   1. one STARTUP observation so the UI is never empty,
    #   2. one SCHEDULED observation every N minutes (5/15/30/60),
    #   3. manual refresh (web-triggered, handled inside the service).
    # Between observations the process only sleeps; it does not touch NCOS.
    try:
        first = service.collect_startup()
        cp.log('sdwan_analyzer: startup observation saved '
               '(wan={}, interfaces={}, cellular={}, errors={})'.format(
                   len((first.get('wan') or {}).get('paths', [])),
                   len(first.get('interfaces', [])),
                   len(first.get('cellular', [])),
                   len(first.get('collector_errors', []))))
    except Exception as e:
        cp.log('sdwan_analyzer: startup collection error: {}'.format(e))

    cp.log('sdwan_analyzer: scheduled collection every {} min'.format(
        service.schedule_minutes()))

    # time.sleep avoids a spin-wait (coding-standards memory rules); sleep in
    # 1s slices so SIGTERM shutdown stays responsive.
    while _running:
        time.sleep(1)
        # Backend-owned Recording job (no-op when idle).
        service.recording_tick()
        if not _running or not service.scheduled_due():
            continue
        try:
            obs = service.collect_scheduled()
            if obs is None:
                cp.log('sdwan_analyzer: scheduled sample skipped '
                       '(Recording active)')
                continue
            cp.log('sdwan_analyzer: scheduled observation saved '
                   '(interfaces={}, swans_new={}, errors={})'.format(
                       len(obs.get('interfaces', [])),
                       len(obs.get('swans_samples', [])),
                       len(obs.get('collector_errors', []))))
        except Exception as e:
            cp.log('sdwan_analyzer: scheduled collection error: {}'.format(e))

    server.stop()
    # Graceful stop: end any active Recording, fsync and close durable files,
    # mark a clean exit.
    try:
        service.shutdown()
    except Exception as e:
        cp.log('sdwan_analyzer: shutdown flush error: {}'.format(e))
    cp.log('sdwan_analyzer: stopped')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        cp.log('sdwan_analyzer: fatal error: {}'.format(e))
        sys.exit(1)
