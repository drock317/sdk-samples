# Validation record

What has been tested, on what, and what has **not** been tested yet. Please add
your own results, especially for the open items at the bottom.

Legend: **Observed** means it was run and checked directly during development.
**Reported** means the developer ran it and reported the outcome; the log
evidence was not captured in this record.

## Test subjects

| Item | Version |
|---|---|
| Module | `src/web_port.py`, sha256 `ebd0293e...3556` (see `web_port.py.sha256`) |
| Reference app | Speedtest Analyzer 1.1.7 (retrofit of 1.1.6) |
| Second app | SD-WAN Analyzer (carries an older copy of the module) |
| Hardware | E400-5GE-AM, NCOS 7.26.60 then 7.26.101, Developer Mode |
| Hardware (reported) | R2400 and other devices, apps delivered through NCM |

## Automated tests (no router)

`python3 -m unittest discover -s tests -v` runs 35 tests in a few seconds.

| Area | What is covered |
|---|---|
| `parse_port` | valid, missing, non-numeric (`abc`, `80.5`, `0x50`), out of range |
| First run | discovers and persists, skips a busy port, exhausted range, persist failure still serves, App Data read error treated as unset |
| Configured value | binds exactly, busy port fails closed without scanning or writing, malformed fails closed and is not retryable |
| Retry | invalid value does not wait, busy port retries the full 60 s then gives up, port freed mid-window binds, value corrected mid-window is picked up, value changed to invalid stops retrying, success returns immediately, log lines collapsed |
| `stop_app` | sends `control/system/sdk/action` = `stop <uuid>`, missing uuid, request error |
| Hygiene | parses as Python 3.8, no `cp` import, standard library only, matches published checksum |
| Examples | two shipped examples served over real HTTP, restart reuses the persisted port, admin override wins, two apps get different ports, custom server class attributes |

The tests were mutation-checked: deliberately marking a busy port as
non-retryable, or overwriting the configured value on failure, each make several
tests fail.

## On-device results

### Observed (E400, Developer Mode)

| Scenario | Result |
|---|---|
| First run, no value | Discovered 8000, persisted it, UI answered HTTP 200 |
| App restart | `bound configured port 8000 (source: configured)` |
| Router reboot | Persisted value survived, app (redeployed after the reboot) came up on 8000 as `configured` |
| Firmware upgrade 7.26.60 to 7.26.101 | Persisted value survived, redeployed app came up on 8000 as `configured` |
| Admin sets `8005` | App bound 8005 on restart, 8000 stopped answering, no rebuild |
| Value `abc` | One "invalid (non-numeric)" line, app stopped itself, stayed stopped for 45 s, value left as `abc` |
| Value `22` (SSH holds it) | App stayed Started while retrying the same port about 60 s, then one "giving up" line and the app stopped itself. Value left as `22`. Never bound another port. Still stopped, with no restart, 40 s later |
| Clear the value, start | Rediscovered 8000, UI answered |

Before self-stop existed, the first design (exit on failure) produced a restart
every 3 to 5 seconds. That behavior is why the standard stops the app instead.

### Reported (NCM rollout)

| Scenario | Result |
|---|---|
| Speedtest Analyzer 1.1.7 delivered to several devices through NCM | Each created `speedtest_analyzer_web_port` App Data and assigned port 8000 |
| SD-WAN Analyzer installed on an R2400 next to Speedtest Analyzer | Created `sdwan_analyzer_web_port` and took port 8001, because 8000 was already held |

### An incident worth knowing about

During testing, the app logged `Web server started on port 8000` while TCP
connections to 8000 from the LAN **timed out**. The app was fine. The router had
no firewall forwarding from the Primary LAN Zone to the Router Zone. See
`TROUBLESHOOTING.md`. A timeout (not a refusal) on a port the app says it bound
points at the firewall.

## Open items: please test and report

| # | Item | Why it matters |
|---|---|---|
| 1 | **Self-stop on a device that is not in Developer Mode** (app delivered by NCM) | The stop uses `control/system/sdk/action`. It was verified only in Developer Mode. If the SDK rejects it, the app idles without the web UI and logs `SDK stop request was not honoured`. Force a failure (value `abc`) on an NCM-delivered app and report the result. |
| 2 | **A port pushed from NCM Group or Device SDK Data** | The module reads App Data the same way however it got there, but the NCM path has not been exercised for an override value. |
| 3 | **Other hardware families** | Only E400 and R2400 so far. |
| 4 | **Apps using the older module copy** | SD-WAN Analyzer still carries a copy without retry or `stop_app`. It works, but on failure it keeps running without a web UI instead of stopping. Update with `prompts/04-audit-and-update.md`. |
| 5 | **Two apps pinned to the same value from NCM** | Expected: the second fails closed and stops. Not yet exercised. |

## How to record a result

Copy a row, add the hardware and NCOS version, the app and its version, and the
log lines that prove the outcome. Open an issue or pull request against this
folder.
