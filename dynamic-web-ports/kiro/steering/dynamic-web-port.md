---
inclusion: auto
name: dynamic-web-port
description: "Dynamic, persistent, NCM-configurable web port standard for Cradlepoint SDK apps that listen on a TCP port (web UIs, JSON APIs). Apply when building or modifying any SDK app with a web server."
---
# Dynamic Web Port Standard

Every SDK app that listens on a TCP port for browsers or LAN clients MUST pick its port through the shared `web_port.py` helper. NEVER hardcode `8000` (or any port) in app code. Load the `web-port` skill for the full integration pattern.

## Rules

- **Use `web_port.bind_web_socket_with_retry(app_name, cp.get_appdata, cp.put_appdata, cp.log)`**, not the bare `bind_web_socket`. Hand the returned pre-bound socket to the server: `HTTPServer(..., bind_and_activate=False)` then `httpd.socket = result.sock`. The successful bind is the availability test, so there is no check-then-bind race.
- **App Data field is namespaced per app: `<app_name>_web_port`.** Device-level App Data is shared across apps, so a generic `web_port` key is never used. `<app_name>` matches the app folder and `package.ini` section name.
- **A configured value is authoritative.** Bind exactly that port. Never scan for another port, never relocate, never overwrite the value. A malformed or out-of-range value also fails closed. This protects NCM LAN Manager connections pinned to that port.
- **First run (no value):** the helper discovers a free port by real socket bind starting at 8000 (bounded to 64 ports), then persists it with `cp.put_appdata`. Later starts reuse the persisted value even if 8000 frees up. This records an assignment; it is NOT writing a code default to App Data.
- **NEVER `sys.exit()` on a web-port failure.** Apps run with `restart = true`, so NCOS restarts an exited app every few seconds forever and floods the log. Instead:
  - malformed value: logged once, no retry (`result.retryable` is False)
  - valid but busy port: retried in-process (same port only, about 60 s, App Data re-read each try), then logged once
  - after the final failure call `web_port.stop_app(cp.put, cp.log)` so the app stops itself via the SDK (a stopped app is not restarted). If the SDK ignores the stop, idle without the web server; still never exit
- **A web app must never keep running without a port** (other than the idle fallback above).
- **Keep `web_port.py` byte-identical in every app** (same as `cp.py`). Do not edit per app. Fix bugs in the canonical copy and propagate. Verify with the checksum in `dynamic-web-ports/src/web_port.py.sha256`.
- **Log the effective port and its source** (`configured` or `discovered`) at startup, and surface both in the app's status or health API when it has one.
- **Document the field in the app readme** App Data table: `<app_name>_web_port`, string integer, optional, authoritative when set, otherwise discovered from 8000 and persisted.
- **Do not import `cp` inside `web_port.py`.** It is standard-library-only and dependency-injected so it can be unit tested with stubs.

## Remember

- LAN clients reaching an SDK app port need a firewall forwarding rule from the Primary LAN Zone to the Router Zone. A TCP **timeout** (not a refusal) to the app port while the log says the server started is the signature of a missing forwarding rule, not an app bug.
- NCM can set `<app_name>_web_port` at group or device level to standardize a fleet. The app reads it on its next start. No NCM API credentials are involved.
