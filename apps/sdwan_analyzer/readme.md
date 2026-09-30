# SD-WAN Analyzer

Historical visibility and analysis of SD-WAN behavior on Ericsson Cradlepoint
NCOS routers: WAN paths, SD-WAN interfaces, WAN bonding (WBOND), member
interfaces, traffic steering, underlay connectivity, cellular state, and how
these change over time.

> **Status: BACKEND build (v0.2.2).** This build implements the full backend:
> targeted collectors, normalization, cross-object correlation, logical
> Traffic Steering grouping, change-ready semantic fingerprints, a structured
> cached snapshot, and section JSON endpoints. **Durable** historical storage
> (format/retention) and the **polished UI** remain intentionally deferred.
> The in-memory `HistoryStore` stays until storage constraints are separately
> validated.

## Architecture

One-directional data flow; each layer depends only on the one to its left:

```
NCOS collectors -> normalization -> historical storage -> application API -> presentation
   collectors.py     normalize.py       storage.py           service.py        web.py
```

- **collectors.py** — read-only NCOS `status/` snapshots via `cp.get()`. No
  business logic. Every path is annotated VERIFIED or OPAQUE.
- **normalize.py** — converts raw NCOS shapes into stable internal records.
  Reuses the validated WAN friendly-naming / cellular-classification concepts
  from Speedtest Analyzer (not its speed-test code).
- **storage.py** — history **interface only**. No persistence format chosen
  yet. Ships a bounded in-memory (non-durable) default so the skeleton runs.
- **service.py** — the application API. Composes the layers. The **only**
  layer the web tier may call.
- **web.py** — minimal `http.server` dev page + JSON API. Calls `service`
  only; never touches NCOS. The browser never reaches NCOS APIs.

## Backend endpoints

A background collector owns all router polling on two cadences (see below)
and caches one normalized snapshot. **Web requests only read the cache — they
never trigger router collection.** Endpoints return `503 {"error":"snapshot
not ready"}` until the first snapshot has been composed.

- `GET /` — dev/status page
- `GET /api/status`, `GET /api/health` — app + storage + collector health,
  effective web port, latest `system_summary`
- `GET /api/snapshot` — full cached normalized snapshot (all sections)
- `GET /api/wan` — `paths`, `physical`, `overlays`, `underlay_map`, `config`
- `GET /api/swans` — SWANS per-path latency/jitter (Traffic Class measurement)
- `GET /api/qoe` — SD-WAN transport QoE (distinct object from SWANS)
- `GET /api/traffic_classes` — normalized Traffic Class definitions
- `GET /api/traffic_steering` — `logical_rules`, `compiled_rules`, `events`,
  `stats`, `flow_alignment`
- `GET /api/loadbalancers` — normalized rate/spillover/round-robin objects
- `GET /api/bonding` — WBOND bonds with member runtime detail
- `GET /api/identities` — resolved destination identities
- `GET /api/changes` — topology/config change flags vs previous snapshot
- `GET /api/history` — recent recorded snapshots (in-memory, non-durable)

> LAN clients reach this port only if a firewall zone-forward rule exists from
> the Primary LAN Zone to the Router Zone (see web-standards).

## Collection cadence & safety

The router management plane can degrade under aggressive heavy-tree polling,
so collection is split into two cadences (centralized in `service.py`):

| Cadence | Default | Trees |
|---------|---------|-------|
| Fast (topology/state) | 15 s | `status/wan/devices`, `primary_device`, `swans`, and a single `status/wan/steering` read split locally into rules/events/stats/intents |
| Slow (heavy + static config) | 120 s | `status/wan/loadbalancers` (slow on R1900), `status/sdwan_adv/wan_bonding`, `status/sdwan_adv/qoe`, `config/wan/rules2`, `config/identities/ip`, `config/identities/intent`, `config/sdwan_adv/traffic_class` |

The broad `status/sdwan_adv` tree is **not** polled — only the targeted
`sdwan_adv/wan_bonding` and `sdwan_adv/qoe` subtrees are read, to minimize
management-plane load.

**Traffic Classes** come from `config/identities/intent`, whose `criteria[]`
array holds each Latency/Jitter/SS criterion (`enabled`, `threshold`,
`hysteresis`, `priority`, targets). Each intent's `traffic_class` field
correlates to a `config/sdwan_adv/traffic_class` entry by `_id_` for `dscp`,
`forward_error_correction`, `reset_existing_flow`, and the WAN bonding
algorithm. `intent_id` and `traffic_class_id` are distinct and both exposed.
Legacy identity intents without a `traffic_class` field are excluded from the
NCX Traffic Class view. The Traffic Class fingerprint covers criteria
thresholds and the semantic config, so a threshold-only change is detected as
`traffic_class_changed`.

**SWANS** is parsed from `status/wan/swans` `history[].upd_status[]` (per
`wandev` `criteria.Latency`/`.Jitter`), taking the latest sample per path;
`cm` and `dataused` are not path records. **QoE** is parsed from
`status/sdwan_adv/qoe` (singular `interface` map) using `qoe-latency-stats`
(`pkts-intf-qoe-latency-average`/`-last-sample`); `pkt-loss-stats` is kept
raw. SWANS and QoE remain separate concepts.

**WBOND** is parsed from the real `status/sdwan_adv/wan_bonding` shape:
top-level `supported_capabilities`; per bond `Bonding Mode`, `link state`,
`Total interfaces`, reported `DL/UL total flows`, `negotiated_capabilities`,
`gre key`, `mtu`, `Priority`; per member (`members_list` dict keyed by tunnel
iface) `intf name` → member WAN id, `profile name`, `config_weight`,
`ds_weight` → download runtime weight, `us_weight` → upload runtime weight,
`DL/UL current flows`, and raw bandwidth/trend/sample values, `has_loss`,
link/attach state, `gre_key`, `mtu`. Configured weight, runtime directional
weight, and observed flow share stay separate; bandwidth units are not
assumed.

The four steering sub-trees are children of one `status/wan/steering` object,
so the fast pass reads that parent **once** and splits it locally rather than
issuing four sub-path reads.

Caching is **per collector**. Each read returns an explicit success/error
result, so a failed endpoint retains its previous last-known-good value while
a legitimately empty result (`{}`/`[]`/`None`) still replaces stale data. One
failing optional collector never blanks unrelated data or stops the app.
Per-collector health (last-known-good timestamp, error, error timestamp) is
surfaced under `collectors.{fast,slow}.collectors` in `/api/status`. No full
`/api/` tree is ever fetched. Intervals are constructor-configurable
(`AnalyzerService(fast_interval=..., slow_interval=...)`).

`/api/history` serves a **compact** per-sample projection (primary WAN, WAN
connection states, underlay map, SWANS/QoE metrics, logical-rule fingerprints
+ preferred WAN/flow alignment, loadbalancer active_device/scores, WBOND
runtime weights/flow counts, change flags) — never the full snapshot with raw
passthrough trees — so the bounded in-memory store stays memory-safe on the
R1900. The live `/api/snapshot` still carries the richer current data.

## Normalized snapshot schema

```
{ timestamp,
  system_summary: { primary_device, wan_path_count, physical_count,
                    overlay_count, bond_count, logical_rule_count },
  wan:   { primary_device, paths[], physical[], overlays[],
           underlay_map[], config{} },
  swans: { paths[], history_summary, raw },
  traffic_classes: [],
  traffic_steering: { logical_rules[], compiled_rules[], events[],
                      events_meta, stats[], flow_alignment[] },
  loadbalancers: [],
  bonding: { bonds[] },
  qoe: { paths[], raw },
  identities: { destinations[] },
  fingerprints: { topology, bonding_membership, logical_rules,
                  logical_rule_map, traffic_classes },
  collectors: { fast{...}, slow{...} },
  changes: { topology[], config[], first_snapshot } }
```

Each normalized object keeps a `raw` passthrough where field semantics are
not yet proven, so nothing is fabricated or discarded.

### Identity axes (kept separate, never conflated)

- **WAN dictionary key** (e.g. `sdwan-hub0_wan`) — device id in WAN/steering
  APIs.
- **`info.uid`** (e.g. `hub0_wan`) — overlay/control-routing identity.
- **`info.iface`** (e.g. `gres-hub0_wan`) — Linux interface.
- **`object_class`** — `physical` | `sdwan` | `wbond` | `unknown`. WBOND is a
  separate forwarding object and is **never** flattened into its members.

### Priority domains (kept separate)

`cm_priority` (physical Connection Manager), `original_priority`,
`parent_priority` (overlay generated priority), plus steering
`wan_priority_order` (explicit selector) and logical `trigger_priority`.
Overlay generated fractional priorities are exposed for analysis but **no**
tie-breaker theory is encoded as fact.

### Logical Traffic Steering rules

Real compiled steering rules have **two layers**: a top-level runtime object
and a nested `xpolicy` compiled-policy object. Each field is read from its
authoritative layer and they are never flattened together:

- **Runtime (top level):** `loadbalance_algo` → `runtime_algorithm`,
  `matched_devs` / `steering_to` (both **dicts keyed by WAN ID**),
  `primary_wandev`, `failover`, `intent` → friendly `traffic_class_name`.
- **Compiled (`xpolicy`):** `trigger_priority`, `trigger_strings` (a **list of
  dicts** `{_id_, trigger_priority, trigger_string}`), `src_lans` (a list of
  `{lan_name}` → normalized to `["Primary LAN"]`), `wan_priority_enabled`,
  `intent` → `traffic_class_id` (UUID), `dst_ip_network` → destination UUID,
  and `loadbalance_algo` → `configured_algorithm` (kept distinct from
  runtime — they can differ).

Compiled children are grouped into **logical** rules by a semantic key
(clean `xpolicy.name` + `trigger_priority` + selector + source LANs + Traffic
Class name) — **never** by compiled UUID. Destinations
(`xpolicy.dst_ip_network`) from all children merge and resolve via
`config/identities/ip`. WAN selector is classified `all_sdwan` | `bonded` |
`explicit_priority` | `specific` | `unknown` from the `xpolicy` trigger
structure (explicit priority is checked before bonded). Events and stats
correlate by **logical rule name** — on the real router
`steering/events[].steering_rule` and `steering/stats.rules[].steering_rule`
hold the logical name, not a UUID — and because stats are already
logical-rule-level, exactly one stats record attaches per logical rule (counts
are never multiplied across children). `preferred_flows` /
`nonpreferred_flows` are a current/aging population, not lifetime totals. Event
`reason` is stored verbatim.

Each logical rule carries a stable `semantic_fingerprint` so history can track
it across policy recompilation/UUID churn.

## Privacy

Cellular detail is privacy-filtered (`normalize_cellular_safe`): only
SD-WAN-useful metrics (carrier, service type, RSRP/RSRQ/SINR, bands, SIM slot)
are surfaced. IMEI/IMSI/ICCID/MDN/GPS/secrets are never included, even when
present in the raw modem object.

## Tests

Local, standard-library-only harness (not shipped — excluded via
`buildignore`):

```
.venv/bin/python3 apps/sdwan_analyzer/tests/run_tests.py
```

`tests/fixtures.py` models the validated R1900 scenario (physical Ethernet +
cellular, two SD-WAN overlays, an 80/20 WBOND, and the LAB01/YouTube/TEST
Applications/Management steering rules). `tests/run_tests.py` drives the real
`normalize`/`service` code by injecting cached raw passes (no router I/O) and
asserts object classification, friendly naming, WAN-selector classification,
logical-rule grouping + destination merge, fingerprint stability across UUID
churn, bonding/loadbalancer/traffic-class/SWANS/QoE normalization, end-to-end
schema composition, change detection, and the cellular privacy filter.

## NCOS API paths

Sources: `docs/ncos-api/` (repo docs) and the validated Speedtest Analyzer
v1.1.4 implementation (STA), which resolved SD-WAN/WBOND/steering behavior on
real hardware (incl. R1900).

| Path | Purpose | Confidence |
|------|---------|-----------|
| `status/wan/devices` | Per-WAN inventory (info/status/stats/config/diagnostics) | Docs + STA |
| `status/wan/devices/{id}/diagnostics` | Cellular (mdm-*) diagnostics | Docs + STA |
| `status/wan/devices/{id}/stats` | Cellular (mdm-*) stats | Docs + STA |
| `status/wan/primary_device` | Primary WAN device UID (sole authority) | Docs + STA |
| `status/wan/policies` | WAN policy engine (FailoverFailback, DualSIM, SWANS, Affinity, OnDemand) | Docs |
| `status/wan/swans` | Smart WAN Selection priority/history/data usage | Docs |
| `status/wan/steering` → `rules[].intent` | Steering active when any `intent` != `"Management (Management traffic)"` | STA-validated; needs device re-validation |
| `status/sdwan_adv/wan_bonding/interfaces/{iface}` → `members_list[]["intf name"]` | **WBOND member enumeration** (underlying WAN device keys) | STA-validated; needs device re-validation |
| `status/wan/devices/{sdwan}` `status.active_dep_wandev` / `status.dep_wandevs` | SD-WAN single dependent (underlay) WAN | STA-validated; needs device re-validation |
| `status/wan/sdwan` | SD-WAN hub/spoke: `connected_hub`, `links` | Docs; not used for membership; `links` device-dependent |
| `control/sdwan_adv/user_mode_driver/interface/{info.uid}` → `local_ip`/`remote_ip` | SD-WAN overlay source/gateway (read-only GET) | STA-validated; not in repo docs; needs device validation |
| `control/sdwan_adv/wan_bonding/interface/{info.uid}` → `local_ip`/`remote_ip` | WBOND overlay source/gateway (read-only GET) | STA-validated; not in repo docs; needs device validation |

Identity note (from STA): `info.uid` is the control-plane/routing identity
for overlays; the WAN object key (e.g. `wbond-wbond0_hub0`) is the
`status/wan/devices` key; `info.iface` is the Linux interface. WBOND
`members_list` entries expose the underlying WAN as `"intf name"` (NOT
`"profile name"`, NOT `dep_wandevs`).

## Appdata

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `sdwan_analyzer_web_port` | string (integer port) | No | Effective web listening port. Namespaced by app per convention `<app_name>_web_port` (device-level appdata is shared). |

Web-port behavior (see `analyzer/web_port.py`):

- **If `sdwan_analyzer_web_port` is set** it is authoritative: the app binds
  exactly that port. It does not scan from 8000, does not silently relocate,
  and does not overwrite the value if the port is occupied — it logs a clear
  error and fails web startup safely (the rest of the app keeps running).
  This determinism protects an NCM LAN Manager connection pinned to that port.
  A malformed/out-of-range value fails closed (no discovery fallback).
- **If it is not set** (first run) the app discovers a usable port by actual
  socket bind starting at 8000, bounded, then persists the result with
  `cp.put_appdata('sdwan_analyzer_web_port', '<port>')`. This is an
  intentional write of the discovered assignment, not a code default. On
  later restarts/reboots the persisted value is reused even if 8000 frees up.
- **NCM** can set `sdwan_analyzer_web_port` at group/device level to
  standardize the port across a fleet; the app consumes the effective value
  on next startup. No NCM API credentials are used. The port is not written
  to Device Description, Asset ID, or Custom fields.

The effective port is logged (`SD-WAN Analyzer web interface started on TCP
port <n>`) and exposed via `/api/status` and `/api/health` under `web`.

No other appdata is written. Per coding-standards the app never writes
default values to appdata.

## Resolved from STA v1.1.4 (implemented in the skeleton)

- SD-WAN underlay resolution (`active_dep_wandev` / `dep_wandevs`).
- WBOND member enumeration (`wan_bonding/interfaces/{iface}/members_list` →
  `intf name`) with friendly-name reuse.
- Traffic-steering active detection (`rules[].intent` semantics).
- WAN friendly naming + cellular classification (Starlink-before-cellular).

These are wired into `service.build_snapshot()` and validated locally with
mocks. They still require on-device confirmation (see below).

## Fields intentionally left raw (semantics not yet proven)

- **SD-WAN QoE** loss/units and any non-latency fields — carried under
  `qoe.paths[].raw`; only clear latency samples are normalized.
- **SWANS** extra fields beyond latency/jitter — carried under
  `swans.paths[].raw`.
- **Loadbalancer** algorithm-specific fields — normalized where known
  (score, active_device, ingress/egress max/current/adj/run); everything else
  under `raw`. Configured capacity vs runtime utilization vs score vs
  active_device are kept as separate concepts; `39.0625` is **not** labeled
  measured bandwidth.
- **WBOND** capabilities, GRE/bond metadata, member loss/FEC — captured
  generically (`capabilities` list + member `raw`); configured weight vs
  runtime directional weight vs observed flow share are separate fields, with
  no deviation alerts/thresholds invented.
- **Overlay generated fractional priorities** — exposed, but the tie-breaker
  theory is **not** encoded as fact.

## Deferred to later phases

- Durable historical storage (format, retention, sampling interval — see the
  options block in `storage.py`). `HistoryStore` stays in-memory for now.
- SD-WAN/WBOND overlay source-IP resolution wiring (control-plane
  `local_ip`/`remote_ip`; documented in collectors, only needed for active
  testing, not visibility).
- Carrier-aggregation parsing for cellular (base safe metrics are surfaced).
- Device validation catalog (captive-modem model naming) for friendly names.
- The production UI and the template design system (`static/`).
