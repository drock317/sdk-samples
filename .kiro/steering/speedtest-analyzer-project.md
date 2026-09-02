---
inclusion: fileMatch
fileMatchPattern: "**/speedtest_analyzer/**"
description: "Speedtest Analyzer project knowledge: guardrails, validated behavior, and the shipped two-layer configuration-management architecture"
---
# Speedtest Analyzer — Project Knowledge

Project-specific knowledge for the `apps/speedtest_analyzer` SDK application. Generic
speedtest-engine patterns live in `speedtest-standards.md`; this file records the
guardrails, validated behavior, and architecture decisions specific to this app so
future development sessions preserve them.

The current repository is the source of truth for existing implementation. `readme.md`
(user guide) and `TECHNICAL_GUIDE.md` (engineering reference + full changelog) are the
authoritative existing documentation. This file supplements them — it does not replace
them. If code and this file ever disagree, the code and app docs win; fix this file.

## Current Baseline

- Branch: `speedtest-analyzer-development` (established dev branch/workflow — verify `git status`/branch before development).
- Version: `1.1.2` (from `apps/speedtest_analyzer/package.ini`). Release family `1.1.x`.
- Firmware family: NCOS 7.26.x. Architecture: ARM64 (aarch64).
- Product lineage: Speedtest Analyzer 1.0.0 was reset from the unreleased Speed Test 2.7.6 dev baseline. Full engineering history is in `TECHNICAL_GUIDE.md`.
- The two-layer configuration architecture is SHIPPED and E400-validated (see "Two-Layer Configuration Management" below). Do NOT add Geo API-key/provider-secret handling until explicitly instructed.

## Development Guardrails (permanent)

- Preserve previously validated behavior outside the feature being changed. Do not opportunistically refactor, harden, or redesign unrelated subsystems.
- If a change could affect another validated subsystem, identify the risk and add regression validation.
- Once an LLD/direction is approved, proceed through tightly related coding and validation without asking approval for each small step. Stop only for a real design decision, missing dependency, safety issue, or a validation result needing user input.
- Do not create patch files for the user to run. Use directly executable terminal commands. For small files/configs, full-file replacement is fine. Avoid huge one-shot edits; use reviewable chunks.
- Do not commit/checkpoint every tiny edit — use meaningful milestones. Do not commit unless asked.
- Stack stays appropriate for NCOS SDK: Python backend + vanilla HTML/CSS/JS. Node.js is NOT installed on the Mac dev environment and must not become a dependency. No Flask/Node/CDNs/large frameworks/map runtimes unless explicitly approved.
- Test/validation must reflect real NCOS/Cradlepoint behavior, not assumptions.

## Core Engineering Principles (already validated — do not regress)

- A user-selected WAN must never silently fall back to another WAN. `Active Primary WAN` is a selector alias resolved to one concrete NCOS interface at execution; it fails closed if primary cannot be determined.
- Known test-engine/platform defects are tracked independently from general device validation (`device_validation_catalog.json` → `known_defects`).
- Never accept a failed/stale native (Netperf) result as a fresh success. Validate WAN, device UID, direction, result timestamp, start time; use monotonic clocks for timeouts.
- App-created source-routing state (`STWEB-*` tables/policies) must be cleaned up safely; delete policy before table; clean up stale objects from interrupted tests.
- Cellular telemetry collection failure must not invalidate an otherwise successful throughput test.
- iPerf3 listener retries are bounded: up to 3 unique ports per attempt as designed/validated on the selected server (note: `TECHNICAL_GUIDE.md` §10.2.1 documents the historical five-port budget — reconcile the exact bound against current code during any related LLD before changing behavior). One same-Region Public backup server only. Never convert WAN/DNS/routing/timeout/system failures into listener retries.
- Server identity, scheduled-test dependencies, and Reliability statistics stay internally consistent when server config changes.
- Carrier Activity distinguishes observed serving-carrier state from published modem capability (`modem_ca_capabilities.json` is reference-only). Never infer uplink CA when NCOS does not expose TX-channel/uplink component-carrier telemetry.

## History Persistence / Data Integrity (v1.1.0 hardening — preserve)

Rolling retained history of 100 tests (oldest rolls off); atomic temp-file writes with
flush/fsync + validation + atomic promotion; last-known-good backup and recovery;
corrupt-primary quarantine (never overwrite a good backup with a corrupt primary);
history transaction lock around add/delete/clear/recovery; explicit Clear History that
removes primary/backup/temp/quarantined files then creates an empty history.

- Router reboot must NOT clear normal retained test history.
- Development caveat: loading a new SDK build / version replacement can clear the app's retained local history. After a dev reload, missing history-dependent data is expected — run fresh tests before concluding a Cellular Analysis regression. Reboot ≠ SDK reload.

## Cellular Service Type: LTE / 5G NSA / 5G SA (preserve distinctions)

- UI uses "Service Type" rather than a simplistic RAT label where applicable.
- NSA: LTE is the anchor; primary can display "PCell (LTE Anchor)"; NR PCell/SCells shown separately where NCOS exposes them.
- SA: NR is the actual primary serving connection ("PCell (Primary)"), no LTE anchor required; render the NR connection correctly.
- Determine RAT from reported band value, not the diagnostic key family (NCOS may report an LTE band under an indexed `_5G_` PCell key in NSA).
- Preserve native zero-based SCell numbering (SCell0/1/2…), same-band carriers on different channels, and active carriers reporting `0 MHz`.
- Validated examples: W1855 SA / T-Mobile (ACTIVE_5G_PCELL + SCELL, ACTIVE_PCELL may be Not Registered, SRVC_TYPE 5G, SA / Sub-6). W2255 NSA / Verizon (LTE B66 anchor + NR n77, multiple 5G SCells, NSA / Sub-6). Note: W2255 Netperf is a confirmed disabled defect.

## Cellular Analysis Architecture (v1.1.x — local-first)

- Reuse telemetry collected during actual speed tests; do NOT add a second continuous cellular collector.
- ~2-second in-test samples preserve serving-cell transitions a final snapshot would lose. Persist enough state to reconstruct history later.
- Analysis scoped to selected cellular interface + selected history range. Tests without usable serving-cell identity are handled explicitly, not misclassified. One single Unknown bucket; never invent a handoff through an unidentified observation.
- Backend foundation: serving-cell identity normalization; LTE/NSA vs SA primary-cell handling; PLMN/TAC/PCI/band/channel/device UID; transitions; contiguous timeline segments; interface/history filtering; distribution; change metrics; selected-cell RF stats; observed radio configs. Use the existing normalization layer — do not duplicate modem-specific logic in new UI features.
- API source of truth: `GET /api/cellular_analysis` (attaches a site-wide `geo` object at the HTTP layer). GeoView endpoints: `GET/POST /api/geo_settings`, `GET /api/geo_gps`. Do not rename/restructure current APIs without need.
- Two scopes coexist and must both keep working: (1) live/current analysis; (2) historical analysis loaded from retained test results (validated end-to-end on E400). Do not fix one by breaking the other.

Modules: `cellular_analysis.py` (normalization + interface/history-scoped analysis +
`build_site_cell_inventory(history)` for site-wide GeoView), `cellular_geo.py`
(provider-independent GeoView settings schema + validation; no external adapters),
`configuration_manager.py` (two-layer config: merge, migration, reset, Update NCM Group,
scheduler running derivation), `speedtest_web.py` (HTTP transport, config endpoints,
runtime apply, GeoView endpoints, explicit GPS request), `index.html` (vanilla schematic/UI).

## GeoView (local-first; enrichment providers are future)

- Without any external provider/API key, GeoView must remain useful: serving-cell counts, carrier/interface context, site coords if available, serving-cell identities, local observation schematic, distribution, radio analysis. The default view is a NON-geographic local observation schematic (marker positions are not lat/long, direction, distance, azimuth, or topology). No map runtime (Leaflet/Google Maps JS/etc.).
- GeoView config is schema_version 2 with independent Device GPS / Manual Coordinates / Site Address stores and an `active_location_source`. As of 1.1.2 it is persisted as the `geoview` SECTION inside the canonical two-key documents (not the standalone `geoview_settings` key); `cellular_geo.py` still owns validation. Runtime device-GPS coordinates are stripped before persistence AND before Group promotion. GPS is queried only on explicit Refresh GPS; `0.0,0.0` is the no-fix sentinel (never persisted). Writes are read-back verified; defaults are never auto-written to appdata.
- Google and Unwired are "Research Pending" and disabled in the UI. Do NOT implement provider API-key encryption/storage unless explicitly instructed later.

## v1.1.1 Timeline / Reporting (validated on E400 — do not remove/restyle in unrelated work)

Cellular Analysis HTML report export (browser-side; self-contained; US Letter landscape
print/Save-as-PDF); serving-cell transition visualization; handoff/transition markers;
timeline zoom; horizontally expandable/scrollable timeline so long histories stay
readable and transitions stay distinguishable as history grows.

## Device / Testing Observations

Validation set includes E400, E3000, R1900, R980, R2400, W1850, W1855, W2255 and captive
combinations (E3000+W1850, R2400+RC1250). Not every modem exposes identical cellular
paths. Code must gracefully handle LTE-only, NSA, SA, missing secondary-cell identity,
incomplete telemetry, tests with no identifiable serving cell, and dynamic carrier
configs during a test.

## Current SDK Appdata Keys (verified in code)

Canonical configuration (two-key model, shipped in 1.1.2):
- `speedtest_analyzer_group` — NCM Group standard. READ/validated only; the app NEVER writes/deletes it locally.
- `speedtest_analyzer_device` — locally managed Device config + overrides. The ONLY canonical key the app writes/deletes.

Both are schema-versioned, section-sparse, with independent `group_revision`/`device_revision`.
Sections: `schedule`, `outputs`, `iperf3_server_settings`, `iperf3_user_servers`,
`netperf_servers`, `geoview`. GeoView is embedded as the `geoview` section (schema_version 2),
not a standalone key; `cellular_geo.py` still owns its validation.

Runtime / stats / output (NOT normal config):
- `iperf_server_stats` — iPerf3 Reliability stats (dirty-only checkpoint)
- `speedtest_results` — written output target

Migration inputs ONLY (never written by 1.1.2; read once to convert an older install):
- `speedtest_analyzer` — abandoned experimental single-key document
- `iperf_server_settings`, `iperf3_servers`, `netperf_servers`, `speedtest_schedule`,
  `speedtest_outputs`, `geoview_settings` — fragmented legacy keys

---

# Two-Layer Configuration Management (SHIPPED in 1.1.2 — do NOT regress)

The shipped architecture is the two-key model below (implemented in
`configuration_manager.py`; full engineering detail in `TECHNICAL_GUIDE.md` §18). An earlier
single-key design (`speedtest_analyzer` with `management.origin`/`management.mode` +
`config_revision`) was explored but NEVER built — if those notes resurface anywhere, they are
obsolete. The code and `TECHNICAL_GUIDE.md` §18 are authoritative.

## Canonical model

- `speedtest_analyzer_group` — NCM Group standard. READ/validated ONLY; ZERO local writes/deletes.
- `speedtest_analyzer_device` — the ONLY canonical key the app writes/deletes.
- Both are schema-versioned, `document_type`-tagged, section-SPARSE, with INDEPENDENT
  `group_revision`/`device_revision`. No `config_revision`, no `management.origin`/`mode`.
  Management state is DERIVED from key presence.

## Invariants (must not regress)

- App never writes/deletes `speedtest_analyzer_group`.
- Effective config = whole-section `DEVICE > GROUP > DEFAULT` by section-key PRESENCE (falsey
  values `[]`/`{}`/`false`/`""` are authoritative). Section-atomic; no field-level deep merge.
- Sparse documents; absent sections inherit downward.
- Exact-name appdata matching (never rely on loose/substring `get_appdata(name)` for canonical keys).
- Read-back verified Device writes (verify document_type/schema/expected revision; fail closed).
- Never write defaults to appdata on startup (would override Group inheritance).
- Normalized no-op guard: identical proposed device body → zero writes, no revision bump, no
  key create/delete, no hot reload. Runtime-only values (e.g. live GPS) are stripped before
  persistence and never count as a change.
- Secrets/provider API keys stay OUT of scope; never store in either canonical document.

## Reset dependency

Section reset builds the PROPOSED device doc, recomputes PROPOSED effective, and validates
BEFORE persisting. Enforced dependency: an iPerf3 schedule's `params.server_source`
(`public`/`user`) must match effective `iperf3_server_settings.server_mode`; Netperf/non-iperf3
are never coupled. On conflict, return `dependency_reset_required` with `required_reset_sections`,
reason, and `reset_target` (`group`/`default`) and write NOTHING. Confirmed coupled reset removes
all coupled overrides in ONE transaction (one revision or one Device-key delete), re-validates
before write, Group untouched. Reset All validates final GROUP+DEFAULT before deleting the Device
key. Reset wording uses the backend `reset_target`. Shared helpers `check_effective_dependencies`
and `compute_schedule_running` live in `configuration_manager.py`.

## Update NCM Group

Offered ONLY in `group_with_device_overrides` (state flag `can_update_group`); never re-shows
"Migrate to NCM Group" (Device→Group first migration, `can_migrate_to_group`, Device state only).
Candidate = DEEP COPY of current group + only selected Device sections; `group_revision = current
+ 1`; unrelated Group sections and unselected overrides preserved; no defaults copied in; nothing
written locally. Dependency validation runs against the PROPOSED revised group. GeoView:
device_gps policy promotable with runtime coords stripped; manual/site sources non-promotable.
Reconciliation token `(group_revision, device_revision)` aborts validate/cleanup
(`reconcile_aborted`) if either layer changes mid-workflow → restart, never auto-merge.
Validate-present THEN trim promoted Device sections; emptied Device doc is deleted; trim failure
leaves Group intact, Device still wins, returns `cleanup_incomplete` (Retry). Validation proves
payload present on device, NOT NCM provenance.

## Scheduler enabled/autostart/running

Persisted `enabled` and `autostart` are INDEPENDENT and never coerced. Runtime `running` is
derived and non-persistent; the scheduler thread checks `running`, not `enabled`.
`compute_schedule_running(enabled, autostart, is_startup)`: startup → `enabled AND autostart`;
interactive save → `enabled`; `enabled=false` never runs. An explicit unchanged Save after a
restart starts runtime even when persistence is `no_change` (no write, no revision bump);
schedule-specific, does not change global no-op semantics. `GET /api/schedule` exposes
`enabled`/`autostart`/`running`; UI states: Active / Enabled — Not Running / Disabled.

## Migration / Factory Reset

Legacy fragmented keys and the experimental `speedtest_analyzer` key are migration INPUTS ONLY
(never written). No canonical key + valid migration source → state `upgrade_required`, mutation
blocked until Convert (builds schema-1 Device doc, rev 1; sources never modified/deleted).
Factory Reset removes Device/experimental/legacy/runtime keys + local history; NEVER touches
`speedtest_analyzer_group`.

## Validation posture

- Permanent off-router regression suite `test_configuration_manager.py` (299 tests as of 1.1.2):
  merge, sparse/presence, revisions, no-op, migration, reset dependency + wording, Update NCM
  Group (candidate/preserve/promote/trim/token/GeoView/dependency/button-visibility), scheduler
  running semantics, and an appdata write/delete audit (zero lifetime Group/experimental/legacy
  writes). Local gate also runs `py_compile` + `pyjsparser` over `index.html`.
- E400-validated end-to-end: reset dependency modal + coupled reset; full Update NCM Group wizard
  (Device key removed after Group validation); enabled/autostart/running across fresh app startups.
- Dev caveat: in Developer Mode an SCP-installed build can be dropped by a device reboot (SDK
  service shows `apps: []`); appdata survives. Reboot ≠ SDK reload. Redeploy to reinstall.
