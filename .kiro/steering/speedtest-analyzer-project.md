---
inclusion: fileMatch
fileMatchPattern: "**/speedtest_analyzer/**"
description: "Speedtest Analyzer project knowledge: guardrails, validated behavior, and the locked configuration-management HLD"
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
- Version: `1.1.1` (from `apps/speedtest_analyzer/package.ini`). Release family `1.1.x`.
- Firmware family: NCOS 7.26.x. Architecture: ARM64 (aarch64).
- Product lineage: Speedtest Analyzer 1.0.0 was reset from the unreleased Speed Test 2.7.6 dev baseline. Full engineering history is in `TECHNICAL_GUIDE.md`.
- Do NOT bump version, start the configuration architecture, or add Geo API-key handling until explicitly instructed.

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
(provider-independent GeoView settings, appdata persistence; no external adapters),
`speedtest_web.py` (HTTP transport, GeoView endpoints, explicit GPS request),
`index.html` (vanilla schematic/UI).

## GeoView (local-first; enrichment providers are future)

- Without any external provider/API key, GeoView must remain useful: serving-cell counts, carrier/interface context, site coords if available, serving-cell identities, local observation schematic, distribution, radio analysis. The default view is a NON-geographic local observation schematic (marker positions are not lat/long, direction, distance, azimuth, or topology). No map runtime (Leaflet/Google Maps JS/etc.).
- `geoview_settings` appdata is schema_version 2, with independent Device GPS / Manual Coordinates / Site Address stores and an `active_location_source`. GPS is queried only on explicit Refresh GPS; `0.0,0.0` is the no-fix sentinel (never persisted as a location). Writes are read-back verified; defaults are never auto-written to appdata.
- Google and Unwired are "Research Pending" and disabled in the v1.1.1 UI. Do NOT implement provider API-key encryption/storage as part of the configuration-management work unless explicitly instructed later.

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

The canonical `speedtest_analyzer` consolidated entry described below does NOT exist yet;
current code uses these separate keys:

Persistent configuration:
- `iperf_server_settings` — iPerf3 server source/mode preferences
- `iperf3_servers` — User Server List
- `netperf_servers` — saved Netperf servers
- `speedtest_schedule` — scheduled-test configuration
- `speedtest_outputs` — configured output targets
- `geoview_settings` — GeoView config (schema_version 2)

Runtime / stats / output (NOT normal config):
- `iperf_server_stats` — iPerf3 Reliability stats (30-minute dirty-only checkpoint)
- `speedtest_results` — written output target

---

# LOCKED HLD — Configuration Management (FUTURE WORK — do NOT code yet)

Recorded for understanding only. Implement later on explicit instruction. Do not start
this architecture, and do not merge API-key/secret handling into it.

## Canonical entry

One SDK App Data entry `speedtest_analyzer` holding one COMPLETE normalized JSON document
(not sparse — defaults/blanks get explicit canonical representation).

INCLUDE: scheduled jobs; user-created iPerf3 server config; server-source/mode selections;
non-secret GeoView config; global application settings; future non-secret settings.

DO NOT INCLUDE: API keys/secrets; test history; cellular telemetry/history; analysis
results; logs; caches; last-run/temporary runtime state; the bundled public iPerf3 catalog
(stays packaged/read-only, never copied into appdata).

Metadata:
```json
{ "schema_version": 1, "config_revision": 0,
  "management": { "origin": "device|group", "mode": "device|group|device_override" } }
```
Stable combinations: device/device, group/group, group/device_override. `origin` = where
the lifecycle began; `mode` = current management behavior. No config UUID/hash. Never
store NCM group name or ID.

## Day-1 / first persistent save

App runs normally with NO `speedtest_analyzer` entry, using built-in code defaults. Do NOT
write defaults to appdata on startup (that would override NCM group inheritance). No blank
group placeholder is required. On the first normal Apply/Save with no canonical config:
stage the change in memory, then ask "This Device" vs "NCM Group".
- This Device: build complete JSON, origin=device, mode=device, revision=1, save via appdata.
- NCM Group: build complete JSON, origin=group, mode=group, generate JSON for the user to paste into NCM, no local device write, user Validates until expected config/revision appears, then load into RAM.

## Subsequent saves

- device/device: save directly to device; no repeated prompt.
- group/group: on a persistent change, ask This Device vs NCM Group.
  - This Device → local override, origin stays group, mode→device_override; future saves local without re-prompting.
  - NCM Group → stage complete replacement JSON, increment revision, no local write, expose JSON, Validate until expected revision. Once the group path is chosen and JSON exposed, do NOT offer "Save to This Device Instead" if validation fails (avoids NCM conflict/suspension). On failure: keep running config, allow retryable Validate and Close/Cancel & Troubleshoot, no local write.

## NCM conflict behavior

If device-level SDK Data exists and a conflicting group-level entry of the same name is
pushed, NCM can flag a conflict and suspend device configuration. Group Migration MUST
remove the conflicting device-level config and validate its removal BEFORE creating/applying
the group config. Do NOT use NCM "Clear" (too broad — removes other local config).

## config_revision / RAM sync

First persistent config = 1; increment on every successful persistent change; failed/
cancelled do not increment; Restore/Rollback creates a new revision. It is a lightweight
sync marker, not an audit log. Validation checks: JSON exists/parses, expected
`management.origin`, expected `management.mode`, exact expected `config_revision` (no full
JSON equality). No background watcher/poller. On the next Apply/Save, read persisted config
and compare revision with RAM; if different, persisted is authoritative → load it into RAM
FIRST, discard the staged change, tell the user to review/reapply. Applies whether persisted
revision is higher or lower. Do not determine why it changed; do not auto-merge.

## Group Migration Wizard

One unified workflow for device/device → group/group and group/device_override →
group/group. Before any destructive step: capture current effective config, normalize the
future group target, increment revision for the target, store known-good original + prepared
target in `tmp/`, verify recovery files before allowing removal. Sequence: prepare target +
recovery → instruct removal of Device SDK Data in NCM → Validate Removal (do not unlock next
step until it passes; preferred failure text: "Device-level configuration is still visible.
The NCM change may still be synchronizing.") → expose the ALREADY PREPARED group JSON →
user creates/updates group entry → Validate Group Configuration (origin=group, mode=group,
expected revision) → on success hot-reload config + affected subsystems → restart app only
if technically required. Do NOT reconstruct the target after local removal — it was captured
before deletion.

## Migration backup / rollback

`tmp/` is only for migration/schema-conversion recovery, not normal edits. Do not create
migration backups for ordinary Device/Group saves or for group/group → group/device_override.
Keep the last successful migration/schema recovery snapshot in `tmp/` until cleared by app-
version lifecycle. Valid rollback: restore captured USER CONFIGURATION, write in CURRENT
schema, normalize management to origin=device/mode=device, increment revision, do not recreate
fragmented legacy appdata, must not contain decrypted secrets, state-aware (do not blindly
create a device entry while a conflicting group entry would cause suspension). No resumable-
migration subsystem. Before Validate Removal passes: Cancel aborts, discard temp state,
existing config untouched. After Validate Removal passes: in-app cancel/exit behaves as
"Restore Previous Configuration & Exit". No browser-tab interception/resume logic.

## Legacy / future schema migration

Reusable schema migration framework (legacy fragmented appdata → schema 1 → 2 → 3 …), not a
one-time hack. Legacy names historically observed: `iperf_server_settings`,
`speedtest_schedule`, `iperf_server_stats`, `iperf3_servers`, `speedtest_outputs`,
`geoview_settings`. Do NOT infer classification from names alone. Working expectation
(VERIFY against code during LLD):
- Likely configuration: `iperf_server_settings`, `speedtest_schedule`, `iperf3_servers`,
  `netperf_servers`, `geoview_settings`, `speedtest_outputs`.
- Likely runtime/output/history: `iperf_server_stats`, `speedtest_results`.
(Code check for this pass confirmed `iperf_server_settings` and `netperf_servers` are active
persistent config, `speedtest_outputs` is config, and `speedtest_results` is the runtime
write target. The HLD shortlist should be reconciled at LLD.) Do not auto-delete legacy
appdata after adoption; import what is appropriate, leave legacy cleanup manual/separate.
An older schema may be converted in memory on startup, but the app must not silently persist
ownership/config changes just because a new version started; adopting a converted schema
follows the same Device vs NCM Group framework.

## Config repair / reset

Parses but has invalid sections: salvage valid sections, reset/discard only invalid ones,
normalize to current schema, tell the user exactly what was rejected/repaired, successful
repair = new revision, respect management scope. Syntactically unusable JSON: no speculative
fragment reconstruction, no rollback-as-corruption-recovery — Reset and rebuild. Reset
Configuration affects ONLY `speedtest_analyzer` (not history/cellular/results/logs).
- device/device: delete local entry → true Day-1 built-in defaults.
- group/group: no local override; generate smallest valid clean GROUP config; user replaces
  corrupt group JSON in NCM; Validate; device stays group-managed.
- group/device_override: delete local override; underlying group config becomes visible;
  validate/load; return to group/group.

## Settings page HLD (last left-nav item; global admin only)

- A. Configuration Management: management state, schema version, config revision, Migrate to
  NCM Group (visible ONLY for device/device and group/device_override — not for clean
  group/group), Reset Configuration, Rollback Configuration (when a valid migration/schema
  backup exists).
- B. Application Information: app version, schema version, build/support info.
- C. Data Management: test-history info, Clear Test History, future data controls.
- D. Factory Reset: its OWN separate destructive section (NOT inside Configuration
  Management). Red button, explains what is deleted, explicit "Are you sure?". Broader than
  Reset Configuration: removes Speedtest Analyzer-owned persistent local data/history/settings,
  leaves the app installed. device/device → built-in defaults; group/group → local data/history
  reset then inherited group config used again; group/device_override → override removed,
  underlying group config authoritative. Exact removed-data list derived from current app
  storage at LLD so unrelated NCOS/device data is never touched.

## Secrets are out of scope for this framework pass

Do NOT merge API-key/provider-secret work into the initial canonical `speedtest_analyzer`
implementation. Secrets must never be stored in plaintext there. Non-secret config framework
is validated against real E400/NCM first; encryption/secret migration/provider credential UX
get a separate design later.

## Normal reboot lifecycle (agreed)

Load persisted `speedtest_analyzer`, parse/validate, load supported schema into RAM, rebuild
runtime objects (e.g., scheduled jobs) from persisted config; do NOT rewrite config just
because the app started; do NOT increment revision; preserve origin/mode; reload normal
persistent app data/history. `tmp/` migration recovery data is expected to survive an
ordinary router reboot per current observed NCOS behavior.
