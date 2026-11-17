# Prompt 3: verify an integration on a real router

Run this after deploying an app that uses the dynamic web port. It exercises
every behavior of the standard and restores the device to a clean state. It
needs a router in Developer Mode and `sdk_settings.ini` configured.

Replace the text in `[brackets]`, then paste into Kiro chat.

---

```text
Verify that [app_name] correctly uses the dynamic web port standard on my dev
router. Use the router in sdk_settings.ini. Report each result as pass or fail
with the evidence (log lines, App Data value, app state, HTTP result).

Do these in order. Check the app log for every step, and trust only log lines
newer than the step you just ran.

1. First run. Delete "[app_name]_web_port" from App Data if it exists, restart
   the app. Expect: log "discovered and persisted [app_name]_web_port=<n>",
   "Web server started on port <n> (source: discovered)", the App Data value
   equals <n>, and the UI answers on <n>.
2. Restart. Restart the app again. Expect "(source: configured)" and the same
   port.
3. Override. Set "[app_name]_web_port" to a free port such as 8005, restart.
   Expect it binds 8005 (source: configured) and the old port stops answering.
   No rebuild or redeploy may be needed.
4. Typo. Set the value to "abc", restart. Expect ONE "invalid (non-numeric)"
   log line, no retries, the app stops itself (state Stopped), the value is
   still "abc", and the app does NOT restart on its own. Wait 45 seconds to be
   sure.
5. Busy port. Set the value to 22 (SSH always holds it), start the app. Expect
   retries on the SAME port for about 60 seconds (the app shows Started), then
   a single "giving up" line and the app stops itself. The value must still be
   22. The app must never bind a different port.
6. Recovery. Delete the App Data value, start the app. Expect rediscovery and a
   working UI.
7. Two apps. If another web app is installed, confirm it uses a different port
   with its own "<other_app>_web_port" value.

Then restore the device: remove any test values I did not have before, leave the
app installed and started, and tell me what you left behind.

Reachability note: if the log says the server started but TCP to the port times
out (not "refused") from a LAN client, that is the router firewall. A Primary
LAN Zone to Router forwarding rule is required. Tell me instead of changing
the firewall.
```

---

## Expected results at a glance

| Step | Value | Result |
|---|---|---|
| 1 | not set | discovers a free port from 8000, persists it |
| 2 | persisted | binds the same port, `source: configured` |
| 3 | `8005` | binds 8005, no redeploy |
| 4 | `abc` | one log line, app stops, no restart loop |
| 5 | `22` | retries about 60 s on port 22, then stops, value untouched |
| 6 | cleared | rediscovers, UI works |
| 7 | two apps | second app gets the next free port |

## Not covered here (test on your own hardware)

- **Self-stop on a device that is not in Developer Mode** (for example an app
  delivered by NCM). It is verified only in Developer Mode. If the SDK
  rejects the stop, the app idles without the web UI and logs
  `SDK stop request was not honoured`. See `docs/VALIDATION.md`.
- A port value pushed from NCM Group or Device SDK Data.
