# Troubleshooting

Start with the app log. Every decision the module makes is logged with a
`web_port:` prefix.

## Log messages

| Log line | Meaning | What to do |
|---|---|---|
| `web_port: discovered and persisted <app>_web_port=<n>` | First run. A free port was found and saved. | Nothing. Normal. |
| `web_port: bound configured port <n> (from <app>_web_port)` | An App Data value exists and was bound. | Nothing. Normal. |
| `Web server started on port <n> (source: discovered\|configured)` | The server is up. | Nothing. Browse to that port. |
| `web_port: configured <field>='<v>' is invalid (non-numeric: '<v>')` | The value contains something that is not a whole number. | Fix or delete the value, start the app. The app stopped itself. |
| `web_port: configured <field>='<v>' is invalid (out-of-range: <n>)` | Not within 1 to 65535. | Same. |
| `web_port: configured port <n> (from <field>) is not available. Not selecting another port...` | The configured port is held by something else. | See "Port busy" below. The app retries about 60 s. |
| `web_port: will retry for up to 60s (every 5s)...` | Waiting in case the port is held only briefly. | Wait. |
| `web_port: giving up after 13 attempts over 60s` | Still busy after the retry window. The app stops itself. | Free the port or pick another, then start the app. |
| `web_port: no available port in range 8000-8063` | First run found no free port. | Free a port, or set the value to a port outside that range. |
| `web_port: bound <n> but failed to persist <field>` | Port bound, but App Data could not be written. The app still serves this run. | Set the value yourself in NCM so the port is stable. |
| `web_port: requested SDK stop of this app (uuid ...)` | The app asked the SDK to stop it after a failure. This line may be missing from the log, because the SDK can terminate the process before it is flushed. The app state (Stopped) and the preceding failure line are the reliable evidence. | Fix the cause, start the app. |
| `web_port: cannot stop app: uuid not found in package.ini` | `package.ini` has no `uuid`. | Add one. Every SDK app should have it. |
| `SDK stop request was not honoured; idling without the web server` | The SDK ignored the stop. The app idles without a UI and does not exit. | Stop the app manually, fix the value, start it. Please report the device mode and NCOS version (see `VALIDATION.md` open item 1). |

## The app says it started, but I cannot reach it

Check which kind of failure you see from the LAN client:

```bash
nc -zv <router_ip> <port>
```

| Result | Meaning |
|---|---|
| `Connection ... succeeded` but the page is blank or errors | The app is up. Look at the app. |
| `Connection refused` | Nothing is listening. The app did not start the web server. Read the log. |
| **`Operation timed out`** | **The packets are being dropped. The app is probably fine.** |

A timeout while the log says `Web server started` is almost always the router
firewall. A client on the Primary LAN needs a forwarding rule from the
**Primary LAN Zone** to the **Router Zone** to reach any SDK app port. SSH (22)
and HTTPS (443) work without it because they have their own admin-access rules,
which makes this easy to misdiagnose.

Check the router (Security > Zone Firewall), or via the API:

```text
GET /api/config/security/zfw/forwardings
GET /api/config/security/zfw/zones
```

Note: the path is `config/security/zfw/...`. `config/firewall/zone_fwd` is not the
zone firewall and being empty there means nothing.

Access through **NCM LAN Manager** does not need the local forwarding rule.

## Port busy

Find what holds the port. The most common causes:

1. **Another SDK app configured to the same port.** Give each app its own value.
2. **The previous instance of this app is still shutting down.** The retry window
   usually covers this. If it does not, reboot the router.
3. **A router service.** Ports like 22, 80, 443 belong to the router. Pick a
   port in the 8000 range.

You can read which App Data values are set:

```text
GET /api/config/system/sdk/appdata
```

## Reset an app to automatic port selection

Delete the `<app_name>_web_port` entry (NCM SDK Data, or App Data on the router)
and start the app. It rediscovers a port from 8000 and saves it.

## The app stopped itself and will not come back

That is the design for a bad port value. The log says why. Correct or delete the
value, then start the app (NCM, router UI, or `make.py start <app>`). It also
retries on its own after a router reboot.

## Two apps ended up on unexpected ports

The first app to run takes 8000. The next takes the next free port, 8001, and
saves it. After that each app keeps its own port. To choose them yourself, set
each app's `<app_name>_web_port` explicitly.

## After a reboot or firmware upgrade my Developer Mode app is gone

That is the platform, not this module. Re-add the device to Developer Mode in NCM
and redeploy. The App Data value survives, so the app comes back on the same port.
