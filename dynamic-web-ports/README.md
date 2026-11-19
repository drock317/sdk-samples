# Dynamic Web Ports for Cradlepoint SDK apps
> **Superseded for new apps.** Cradlepoint upstream now ships dynamic web ports in the shared
> `cp.py` (`cp.start_web_server()`, `cp.bind_web_port()`, `cp.stop_app()`). New and synchronized
> apps use those and must not copy `web_port.py`. This kit is kept only as history and for
> SD-WAN Analyzer v0.5.0, which still uses its own `analyzer/web_port.py` until the migration in
> [docs/SDWAN_UPSTREAM_MIGRATION.md](docs/SDWAN_UPSTREAM_MIGRATION.md).


Stop hardcoding port 8000. With this kit an SDK web app **finds a free port on
its first run, saves it in Application Data, and reuses it forever**. An admin
can pin or change the port for one router or a whole fleet from NCM, with no
code change, no recompile, and no new package.

It is a small shared module (`web_port.py`), a Kiro steering file and skill so
every new app is built this way automatically, ready-to-paste prompts to retrofit
existing apps, tests, and the validation record from real hardware.

> Status: validated on E400 (NCOS 7.26.60 and 7.26.101) with Speedtest Analyzer
> 1.1.7, and reported working through an NCM rollout with a second app. A few
> items still need your testing, listed in [docs/VALIDATION.md](docs/VALIDATION.md).

## Why

| Today | With this kit |
|---|---|
| Two web apps on one router both want 8000. The second fails. | The second app takes the next free port (8001) and remembers it. |
| Changing the port means editing code and repackaging. | Set `<app_name>_web_port` in NCM SDK Data and restart the app. |
| A bad value or busy port makes the app restart every few seconds and flood the log. | One log line, then the app stops itself cleanly. |
| A pinned NCM LAN Manager connection breaks if an app quietly moves. | A configured port is never changed. The app binds it or stops. |

## Quick start (5 minutes)

**1. Install into your SDK samples workspace**

```bash
git clone <this repo>
cd <this repo>/dynamic-web-ports
python3 install.py --target /path/to/your/sdk-samples --dry-run   # preview
python3 install.py --target /path/to/your/sdk-samples
```

This adds to your workspace:

```text
.kiro/steering/dynamic-web-port.md        always-on rule for the agent
.kiro/skills/web-port/SKILL.md            the integration procedure
.kiro/skills/web-port/web_port.py         canonical module the skill copies from
apps/templates/web_app_template/web_port.py   so new web apps start with it
```

It never overwrites a file that differs unless you pass `--force`. Reload the
Kiro window afterward so it picks up the new steering and skill.

**2. Use it**

- **New app:** ask Kiro to build a web app. The steering file makes it use the
  standard. Or paste [prompts/01-new-web-app.md](prompts/01-new-web-app.md).
- **Existing app:** paste [prompts/02-retrofit-existing-app.md](prompts/02-retrofit-existing-app.md).
- **Check it on a router:** paste [prompts/03-verify-on-device.md](prompts/03-verify-on-device.md).
- **See what is left in a big workspace:** [prompts/04-audit-and-update.md](prompts/04-audit-and-update.md).

**3. Fleet control from NCM**

Add an SDK Data field named `<app_name>_web_port` (for example
`speedtest_analyzer_web_port`) on a Group or Device. Value is the port number.
The app reads it on its next start.

## How it works in 60 seconds

```text
        <app_name>_web_port set in App Data?
              |                        |
             yes                       no (first run)
              |                        |
   valid number 1-65535?        scan 8000, 8001, ... by real bind
      |            |                    |
     no           yes             first free port wins
      |            |                    |
 log once,    bind EXACTLY that     save it to App Data
 app stops    port (never move)     and use it
              |
        busy? retry the same port about 60 s,
        then log once and the app stops
```

- A configured value is **authoritative**. The app never scans for another port,
  never relocates, and never overwrites the value.
- On a final failure the app **stops itself** through the SDK instead of exiting.
  Exiting makes NCOS restart it every few seconds (`restart = true`), which
  floods the log. A stopped app is not restarted.
- The module returns an **already-bound socket**, so there is no
  check-then-bind race.

Full detail, a flow diagram, and the reasoning behind each decision:
[docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

## What is in the kit

```text
dynamic-web-ports/
├── README.md
├── install.py                       install into a workspace (dry-run, no overwrite)
├── src/
│   ├── web_port.py                  THE module. Copy unmodified into every app.
│   └── web_port.py.sha256           checksum to detect drift
├── kiro/
│   ├── steering/dynamic-web-port.md          always-on guardrail
│   ├── steering-snippets/web-standards-addition.md   alternative: merge into web-standards.md
│   └── skills/web-port/SKILL.md              integration procedure
├── prompts/                         paste-ready Kiro chat prompts
│   ├── 01-new-web-app.md
│   ├── 02-retrofit-existing-app.md
│   ├── 03-verify-on-device.md
│   └── 04-audit-and-update.md
├── examples/
│   ├── minimal_web_app.py           smallest complete integration
│   └── retrofit_custom_server_class.py   for apps with their own HTTPServer subclass
├── tests/                           35 tests, standard library only, no router needed
└── docs/
    ├── HOW_IT_WORKS.md
    ├── INTEGRATION_GUIDE.md         manual steps, before/after
    ├── VALIDATION.md                what was tested, and what still needs testing
    └── TROUBLESHOOTING.md           log lines explained, the firewall gotcha
```

## Where the pieces go (so every future app gets it)

| Piece | Location in your workspace | Role |
|---|---|---|
| Steering file | `.kiro/steering/dynamic-web-port.md` | Loaded automatically when the agent builds or edits a web app. States the rules. |
| Skill | `.kiro/skills/web-port/SKILL.md` | The step-by-step procedure the agent follows. |
| Module (skill copy) | `.kiro/skills/web-port/web_port.py` | The file the agent copies from. |
| Module (template copy) | `apps/templates/web_app_template/web_port.py` | Delivered with the web app template alongside `static/` and `index.html`. |

Use **one** of the two steering options, not both: the standalone
`dynamic-web-port.md`, or paste
[kiro/steering-snippets/web-standards-addition.md](kiro/steering-snippets/web-standards-addition.md)
into an existing `web-standards.md`.

## Run the tests

```bash
cd dynamic-web-ports
python3 -m unittest discover -s tests -v
```

No router and no third-party packages. They run in a few seconds. The suite
includes a check that `src/web_port.py` still matches `web_port.py.sha256`.

## Changing the module

The module must stay **byte-identical in every app**, like `cp.py`. To change it:

1. Edit `src/web_port.py` and add or adjust a test.
2. Run the tests.
3. Update the checksum: `cd src && shasum -a 256 web_port.py > web_port.py.sha256`
4. Re-install with `python3 install.py --target <workspace> --force`, then
   propagate to each app (prompt 4b) and bump their versions.

## Requirements

- **On the router:** Python 3.8 (cppython). The module is standard library only
  and is checked against Python 3.8 syntax by the tests.
- **On your machine:** Python 3.8 or newer to run `install.py` and the tests.
- **For LAN access to an app port:** a firewall forwarding from the Primary LAN
  Zone to the Router Zone. See the troubleshooting guide. A TCP timeout on a port
  that the log says is bound is the signature of this, not an app bug.

## Known limits

IPv4 only. App Data changes take effect on the next app start. A discovered port
sticks until the value is deleted. `cp.start_file_server()` is not covered. Details
in [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

## Reference implementation

Speedtest Analyzer 1.1.7 (`apps/speedtest_analyzer/`) is the first app migrated
with this kit, including a custom `HTTPServer` subclass.
