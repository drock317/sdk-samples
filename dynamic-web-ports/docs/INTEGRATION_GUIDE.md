# Integration guide

How to add the dynamic web port to an app by hand, without Kiro. If you use
Kiro, the prompts in `../prompts/` do these steps for you.

## 0. What you need

- `src/web_port.py` from this kit. Copy it **unmodified**.
- An app that serves HTTP with `http.server` (the SDK web standard).

## 1. Add the module

Put `web_port.py` in the app folder, next to `cp.py`. Verify it is the
validated copy:

```bash
shasum -a 256 web_port.py        # compare with src/web_port.py.sha256
```

Do not edit it per app. If you find a bug, fix `src/web_port.py`, update the
checksum, and re-propagate.

## 2. Replace the hardcoded bind

### New app, or an app using plain `HTTPServer`

```python
import time
from http.server import HTTPServer
from threading import Thread

import cp
import web_port

APP_NAME = 'your_app_name'      # matches the folder and package.ini section

result = web_port.bind_web_socket_with_retry(
    APP_NAME, cp.get_appdata, cp.put_appdata, cp.log)

if not result.ok:
    cp.log('web: NOT started ({}); stopping the app'.format(result.error))
    if web_port.stop_app(cp.put, cp.log):
        time.sleep(30)                   # the SDK terminates this process
    cp.log('web: SDK stop not honoured; idling without the web server')
    while True:
        time.sleep(3600)

httpd = HTTPServer(('0.0.0.0', result.port), Handler, bind_and_activate=False)
httpd.socket.close()                     # close the unused placeholder socket
httpd.socket = result.sock               # adopt the pre-bound socket
Thread(target=httpd.serve_forever, daemon=True).start()
cp.log('{} web interface started on TCP port {} (source: {})'.format(
    APP_NAME, result.port, result.source))
```

A complete runnable version is `examples/minimal_web_app.py`.

### Before and after

```python
# BEFORE
PORT = 8000
server = HTTPServer(('', PORT), Handler)
Thread(target=server.serve_forever, daemon=True).start()
cp.log('Web server started on port {}'.format(PORT))
```

```python
# AFTER: the constant only matters as the first-run start_port
PORT = 8000
result = web_port.bind_web_socket_with_retry(
    APP_NAME, cp.get_appdata, cp.put_appdata, cp.log, start_port=PORT)
# ... failure handling as above, then the server built from result.sock
```

### App with its own `HTTPServer` subclass

If `server_bind()` was customized, it is skipped by `bind_and_activate=False`.
Add an `adopt_bound_socket()` that sets the same attributes. This is exactly
what was done for Speedtest Analyzer. See
`examples/retrofit_custom_server_class.py`.

### App using `socketserver.TCPServer`

Same idea: `TCPServer(('0.0.0.0', result.port), Handler,
bind_and_activate=False)`, close its `.socket`, assign `result.sock`.

## 3. Surface the port

- Log it at startup: `... on TCP port <n> (source: configured|discovered)`.
- If the app has a status or health API, include `effective_port` and
  `source`, so users and support can see which port an app chose.

## 4. Document it

Add a row to the app readme App Data table, and replace any "listens on port
8000" text with the real behavior:

```markdown
| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `<app_name>_web_port` | string (integer port) | No | Effective web listening port. If set, it is authoritative (bind exactly that port or fail closed). If unset, the app discovers a free port from 8000 and persists it. NCM can set this at Group or Device level to standardize a fleet. |
```

Search the repo for stray `8000` in readmes, technical guides, UI text and
troubleshooting sections.

## 5. Bump the version

Bump the patch version in `package.ini` and add a changelog entry, as you would
for any release.

## 6. Test

1. Unit tests for the module: `python3 -m unittest discover -s tests` from the
   kit folder.
2. On a router: follow `../prompts/03-verify-on-device.md`.

## Things to watch for

- **Work that starts before the web server.** On a busy configured port the app
  waits up to about 60 seconds before stopping. Threads you started earlier keep
  running during that window.
- **Anything that reads the port later** (links in the UI, a status API) must use
  `result.port`, not a constant.
- **`sys.exit()` on web start failure.** Remove it. It causes the restart loop.
- **Do not write a default port into App Data yourself.** The helper persists a
  port only after it has bound it.
- **Do not catch and ignore `result.ok == False`.** An app that runs without a
  port reports "Started" while serving nothing.
- **Pre-existing stale copies.** Apps built from an earlier version of the
  module lack `bind_web_socket_with_retry`, `retryable` and `stop_app`. Use
  `prompts/04-audit-and-update.md` to find and update them.

## Keeping the kit in sync across apps

| Task | Command |
|---|---|
| Check an app's copy | `shasum -a 256 apps/<app>/web_port.py` and compare with `src/web_port.py.sha256` |
| Change the module | Edit `src/web_port.py`, run the tests, then `cd src && shasum -a 256 web_port.py > web_port.py.sha256` |
| Re-install into a workspace | `python3 install.py --target <workspace> --force` |
