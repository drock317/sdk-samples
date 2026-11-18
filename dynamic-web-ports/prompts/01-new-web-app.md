# Prompt 1: build a NEW web app with the dynamic web port

Use when you are starting a new SDK app that has a web UI or JSON API. If the
kit is installed (see the README), the steering file and skill already tell
Kiro to do this automatically. This prompt makes it explicit and gives Kiro the
details it needs.

Replace the text in `[brackets]`, then paste into Kiro chat.

---

```text
Create a new Cradlepoint SDK web app called [app_name] that [one sentence
describing what the app does].

It must use the dynamic web port standard (the `web-port` skill and the
`dynamic-web-port` steering file):

- Copy web_port.py UNMODIFIED into the app folder next to cp.py.
- Pick the listening port with web_port.bind_web_socket_with_retry(), never a
  hardcoded 8000. The App Data field is "[app_name]_web_port".
- Hand the pre-bound socket to HTTPServer(..., bind_and_activate=False). Close
  the unused placeholder socket first, then set httpd.socket = result.sock.
- If the port cannot be bound, log once and stop the app with
  web_port.stop_app(cp.put, cp.log). Never sys.exit(), and never keep running
  without a web port.
- Log the effective port and its source (configured or discovered) at startup,
  and include both in the app's status API.
- Add the "[app_name]_web_port" row to the readme App Data table.
- Do NOT write any default port value to App Data. Only the helper persists a
  port that it actually bound.

Follow the normal SDK workflow: verify the API paths first, then build, then
deploy with make.py deploy.
```

---

## What Kiro should produce

- `web_port.py` byte-identical to the kit copy (check with `shasum -a 256`).
- Startup code that matches `examples/minimal_web_app.py`.
- A readme with the `[app_name]_web_port` App Data row.

## Check it worked

Run [Prompt 3](03-verify-on-device.md).
