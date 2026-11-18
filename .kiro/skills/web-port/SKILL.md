---
name: web-port
description: >-
  Persistent, NCM-configurable dynamic web-port selection for Cradlepoint SDK
  web apps. Use whenever you build or modify an SDK web app that listens on a
  TCP port, so the port is discovered-and-persisted on first run and can be
  overridden from NCM group/device config without recompiling the app.
---

# Web Port Standard

Every SDK web app selects its listening port through the shared, app-agnostic
`web_port.py` helper instead of a hardcoded `8000`. This gives three things
with no code changes per deployment:

1. **No collisions** — first run discovers a free port by real socket bind.
2. **Persistence** — the chosen port is stored in Application Data and reused.
3. **Fleet control** — an admin can pin the port from NCM group/device config.

## When to apply

Apply this skill whenever an app:

- runs an `http.server` web UI or JSON API, or
- otherwise binds a TCP listening port for browser/LAN access.

New web apps created from `apps/templates/web_app_template/` already ship
`web_port.py` and the wired-in startup — keep it, don't replace it with a
hardcoded port. For an existing app, copy `web_port.py` in and follow the
integration below.

## The convention

Each app namespaces its port in Application Data as:

```
<app_name>_web_port            e.g.  speedtest_analyzer_web_port
```

Device-level App Data is shared, so a generic `web_port` key is never used.

## Resolution contract

- **Configured value present** (`cp.get_appdata('<app>_web_port')`): it is
  **authoritative**. Bind exactly that port, or **fail closed** — do not
  scan, do not relocate, do not overwrite the value, do not start the web
  server on another port. A malformed/out-of-range value also fails closed.
  This protects an NCM LAN Manager connection pinned to that port.
- **Failures never exit the app, and the app never runs without a port.**
  Apps run with `restart = true`, so `sys.exit()` makes NCOS restart the app
  every few seconds, forever. Instead:
  - *Malformed value* (letters, out of range): logged **once**, no waiting
    (`result.retryable` is False), then the app **stops itself**.
  - *Valid but busy port*: retried **in-process** every 5 s for ~60 s (same
    port only, App Data re-read each try so an NCM fix is picked up), repeated
    log lines collapsed, **one** final "giving up" line, then the app
    **stops itself**.
  - Self-stop uses `web_port.stop_app(cp.put, cp.log)`, which sends
    `control/system/sdk/action` = `stop <uuid>` (uuid from `package.ini`). A
    stopped app is not restarted by the restart policy. The admin fixes the
    value and starts the app again; `auto_start` also retries on reboot.
  - If the SDK ignores the stop, idle without the web server (never exit).
- **No configured value** (first run): discover a free port by actual socket
  bind from `8000` within a bounded range, then persist it with
  `cp.put_appdata`. Reused on every later start. This records an assignment —
  it is **not** writing a code default (which coding-standards forbids).

## Integration (copy this pattern)

1. Put `web_port.py` beside the app's main module (same place as `cp.py`).
   Copy it unmodified from `apps/templates/web_app_template/web_port.py`, or
   from this skill's own folder (`.kiro/skills/web-port/web_port.py`) when the
   template is not present.
2. Resolve + bind through the helper, then hand the **pre-bound** socket to
   `HTTPServer(..., bind_and_activate=False)`:

```python
import time
from http.server import HTTPServer
from threading import Thread

import cp
import web_port

APP_NAME = 'your_app_name'   # builds "your_app_name_web_port"

result = web_port.bind_web_socket_with_retry(
    APP_NAME, cp.get_appdata, cp.put_appdata, cp.log)  # start_port=8000 default

if not result.ok:
    # Do NOT sys.exit() (NCOS would restart the app in a loop) and do NOT keep
    # running without a port. The reason is already logged: stop the app.
    cp.log('web: NOT started ({}); stopping the app'.format(result.error))
    if web_port.stop_app(cp.put, cp.log):
        time.sleep(30)          # SDK terminates this process
    # Only reached if the SDK did not honour the stop: idle, never exit.
    cp.log('web: SDK stop not honoured; idling without the web server')
    while True:
        time.sleep(3600)
else:
    httpd = HTTPServer(('0.0.0.0', result.port), Handler,
                       bind_and_activate=False)
    httpd.socket.close()                # close the unused placeholder socket
    httpd.socket = result.sock          # hand over the pre-bound socket
    Thread(target=httpd.serve_forever, daemon=True).start()
    cp.log('{} web interface started on TCP port {} (source: {})'.format(
        APP_NAME, result.port, result.source))
```

`PortResult` fields: `ok`, `port`, `sock`, `source` (`configured` |
`discovered`), `persisted`, `error`, `retryable`.

3. **Surface** the effective port and source in the app's status/health API
   and **log** them at startup.
4. **Document** the appdata field in the app readme (see table below).

## Readme appdata table (add to each web app)

```markdown
| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `<app_name>_web_port` | string (integer port) | No | Effective web listening port. Namespaced per app (device-level App Data is shared). If set, it is authoritative (bind-exact or fail closed). If unset, the app discovers a free port from 8000 and persists it. NCM can set this at group/device level to standardize a fleet. |
```

## Why fail-closed (not relocate)

Relocating a configured port is exactly what breaks a pinned NCM LAN Manager
connection. The helper therefore never moves off a configured port; if it
can't bind, it logs a clear error once and the app stops itself (a web app
with no port is not useful, and a stopped app is not restarted in a loop).

## Race avoidance

`bind_web_socket()` returns an **already-bound, listening** socket. Handing
it to `HTTPServer(bind_and_activate=False)` makes the successful bind itself
the availability test — there is no check-then-bind window.

## Do / Don't

- **Do** keep `web_port.py` byte-for-byte identical across apps (shared file,
  like `cp.py`). Fix bugs in the template copy and propagate.
- **Do** pass the real `cp.get_appdata` / `cp.put_appdata` / `cp.log`.
- **Don't** hardcode a port or scan independently of the helper.
- **Don't** `sys.exit()` on a web-port failure (restart-loop log spam); call
  `web_port.stop_app()` instead.
- **Don't** leave the app running without a web port.
- **Don't** write a default port value to appdata — only persist a port that
  was actually bound.
- **Don't** import `cp` inside `web_port.py`; it stays dependency-injected and
  standard-library-only so it's trivially unit-testable with stubs.
