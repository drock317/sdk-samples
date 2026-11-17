# Prompt 2: retrofit an EXISTING app

Use for an app that already has a web server on a hardcoded port. This is the
prompt used to migrate Speedtest Analyzer (1.1.6 to 1.1.7). It tells Kiro to
change only the port handling and leave everything else alone.

Replace the text in `[brackets]`, then paste into Kiro chat.

---

```text
Migrate the existing app [app_name] to the dynamic web port standard.
Use the `web-port` skill and the `dynamic-web-port` steering file.

Step 1, read before changing anything:
- Find where the web server is created and started (HTTPServer, TCPServer,
  or a custom subclass), where the hardcoded port is defined, and every place
  the port number is referenced (code, readme, docs, UI text).
- Tell me what you found before you edit. If the app has a custom server
  class, note what its server_bind() does, because that is skipped when the
  server is built with bind_and_activate=False.
- Check whether App Data keys already exist that could collide with
  "[app_name]_web_port" (for example "[app_name]" or "[app_name]_group").
  Confirm cp.get_appdata matches names EXACTLY.

Step 2, make the change, and ONLY this change:
- Copy web_port.py unmodified into the app folder next to cp.py.
- Replace the hardcoded bind with web_port.bind_web_socket_with_retry(
  "[app_name]", cp.get_appdata, cp.put_appdata, cp.log). Keep the old port
  constant only as the first-run start_port.
- Build the server with bind_and_activate=False, close its placeholder socket,
  then adopt result.sock. If the app has a custom server class, add an
  adopt_bound_socket() that sets server_address, server_name and server_port
  exactly as server_bind() did (see examples/retrofit_custom_server_class.py).
- On a final failure: log once, then web_port.stop_app(cp.put, cp.log). Never
  sys.exit(). Replace any existing sys.exit() on web start failure.
- Log the effective port and its source at startup. Add them to the status API
  if the app has one.
- Update the readme: the access section (no longer "always 8000"), a
  "[app_name]_web_port" row in the App Data table, troubleshooting text that
  mentions port 8000, and a changelog entry.
- Bump the patch version in package.ini and any version text in the docs.
- Do NOT write a default port to App Data, do NOT change unrelated behavior,
  and do NOT edit web_port.py.

Step 3, verify:
- Compile the app, then run `shasum -a 256` on web_port.py and compare it with
  dynamic-web-ports/src/web_port.py.sha256.
- Deploy to my dev router with make.py deploy, then run the checks in
  dynamic-web-ports/prompts/03-verify-on-device.md.
- Report what you verified and what you could not verify.
```

---

## Notes for the reviewer

- The diff for the app should be small: one import, a few lines at startup, the
  readme, and the version bump. A large diff means Kiro changed more than asked.
- A hardcoded `8000` in documentation or UI text is easy to miss. Search for it.
- A router that already served on 8000 keeps 8000. On first start the helper
  scans from 8000, finds it free, and persists it. Nothing changes for users.
