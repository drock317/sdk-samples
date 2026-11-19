# Future change: SD-WAN Analyzer to upstream `cp.py` web ports

Status: deferred. SD-WAN Analyzer v0.5.0 is deployed and validated with its own
`apps/sdwan_analyzer/analyzer/web_port.py`. Do not change it as part of an upstream sync.

## Scope (compatibility change only, no feature work)

1. Refresh `apps/sdwan_analyzer/cp.py` from upstream (it must expose `bind_web_port` and `stop_app`).
2. In `analyzer/web.py`, replace `web_port.bind_web_socket_with_retry(...)` with
   `cp.bind_web_port()`; on `None`, call `cp.stop_app()` and idle (never `sys.exit()`).
   Keep the existing `ThreadingHTTPServer` and assign the returned socket
   (`bind_and_activate=False`, close the placeholder socket, set `server.socket = sock`).
3. Delete `analyzer/web_port.py` and its references in `tests/run_tests.py` and `readme.md`.
4. Keep the `sdwan_analyzer_web_port` appdata field name so pinned NCM config still applies.
   Note upstream retries a busy configured port for about 10 s (ours: about 60 s). Confirm a
   redeploy still rebinds.
5. Bump the patch version, run the app's test suite, deploy to the dev router, and confirm the
   log shows the same port as before the change.

Risk: low, but port behavior on redeploy is the one thing to verify on hardware.
