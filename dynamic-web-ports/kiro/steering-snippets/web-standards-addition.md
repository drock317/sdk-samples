<!--
OPTION B: merge into an existing web-standards.md instead of installing the
standalone steering file (kiro/steering/dynamic-web-port.md).

Use ONE of the two, not both, so the agent does not see the rule twice.
Paste the block below at the end of your existing web-standards.md list.
-->
- **Persistent dynamic web port (standard)** - Every web app selects its port through the shared `web_port.py` helper (copied in with the web app template), never a hardcoded 8000. The app namespaces its port in Application Data as `<app_name>_web_port`.
  - A configured `<app_name>_web_port` is authoritative: bind exactly that port or FAIL closed (do not relocate, do not overwrite, do not start the web server on another port). This protects pinned NCM LAN Manager connections. A malformed/out-of-range value also fails closed
  - If unset (first run), discover a free port by real socket bind from 8000 (bounded range), then persist it with `cp.put_appdata`. The persisted value is reused on every later start - this records an assignment, it is NOT writing a code default to appdata (which coding-standards forbids)
  - Call `web_port.bind_web_socket_with_retry(app_name, cp.get_appdata, cp.put_appdata, cp.log)` (not the bare `bind_web_socket`). It returns an already-bound socket; hand it to `HTTPServer(..., bind_and_activate=False)` and set `httpd.socket = result.sock` to avoid a check-then-bind race
  - **NEVER `sys.exit()` on a web-port failure** - apps run with `restart = true`, so NCOS restarts an exited app every few seconds forever and spams the log. A malformed value is logged once; a valid-but-busy port is retried in-process (same port only, ~60s) then logged once. After the final failure call `web_port.stop_app(cp.put, cp.log)` so the app STOPS ITSELF via the SDK (a stopped app is not restarted). A web app must never keep running without a port; only if the stop is not honoured, idle without the web server (still no exit)
  - Admins set `<app_name>_web_port` via NCM group/device config to standardize a fleet. Log the effective port and its source, and document the field in the app readme's appdata table
  - See the `web-port` skill for the full integration pattern
