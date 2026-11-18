# Prompt 4: audit apps and update stale copies

Two read-first prompts for a workspace that already has many apps.

## 4a. Audit: which apps still hardcode a port?

Read-only. It changes nothing.

```text
Audit every app under apps/ for web server port handling. Do not edit
anything. For each app that listens on a TCP port, report in a table:

- app folder
- the file and line where the port is set or bound
- whether it uses web_port.py (yes or no)
- if yes: does its web_port.py match dynamic-web-ports/src/web_port.py.sha256
  (use shasum -a 256), or is it an older copy
- whether the readme documents "<app>_web_port"
- whether a failed web start calls sys.exit() or stop_app()

Skip apps with no listening socket. Also list any helper in cp.py that binds a
fixed port (start_file_server defaults to 8000) as a separate note. Finish with
the apps ordered by how much work migration needs.
```

## 4b. Update an app that carries an older web_port.py

Some apps were built from an earlier version of the module (no
`bind_web_socket_with_retry`, `retryable` or `stop_app`). They still work, but a
failed bind leaves the app running without a web UI instead of stopping it, and
a busy port is not retried. Bring them up to date:

```text
The app [app_name] has an older web_port.py. Update it to the current standard
without changing anything else:

1. Show me a diff between its web_port.py and dynamic-web-ports/src/web_port.py.
2. Replace the file with the kit copy, unmodified, and verify the sha256.
3. Change its startup call from web_port.bind_web_socket() to
   web_port.bind_web_socket_with_retry() (same arguments).
4. On a failed bind, replace any "continue without web UI" or sys.exit() with a
   log line followed by web_port.stop_app(cp.put, cp.log) and an idle fallback
   (see examples/minimal_web_app.py: stop_or_idle).
5. Close the unused placeholder socket before assigning httpd.socket.
6. Bump the patch version and add a changelog line.
7. Compile, deploy to my dev router, and run prompts/03-verify-on-device.md.
```

Apps with a web_port.py that differs only because it was edited per app are a
smell. The module is meant to be byte-identical everywhere.
