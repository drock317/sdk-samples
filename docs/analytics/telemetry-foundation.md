# Telemetry foundation (v0.8.3)

v0.8.3 records the difference between what NCOS was configured to do, what it
reported at runtime, and how both changed over time. It adds two versioned
configuration objects (`steering_rule_defs`, `wan_bonding_config`), compact
runtime application-matching records, component-carrier RF, QoE validity
flags and four read-only endpoints. Everything is additive: v0.8.2 response
keys, observations, fingerprints and configuration versions are unchanged.

Evidence tags used below: **Confirmed** (verified on R1900 and R2400, NCOS
7.26.81, Phase 1A), **Inference** (consistent with evidence, not proven),
**Unknown** (not verified; handled without interpretation).

## 1. Verified NCOS 7.26.81 relationships

| Relationship | Status |
|---|---|
| Effective steering definitions live in `status/wan/steering/rules[].xpolicy` | Confirmed. No separate logical steering configuration object was found |
| `xpolicy.dst_ip_network` == `config/identities/ip[]._id_` (full UUID) | Confirmed |
| `status/wan/steering/stats.rules[].apps` key == `config/identities/ip[].name` | Confirmed on both routers |
| `xpolicy.intent` == `config/identities/intent[]._id_` | Confirmed |
| `config/identities/intent[].traffic_class` == `config/sdwan_adv/traffic_class[]._id_` | Confirmed |
| `began_capture` marks the steering stats/events capture epoch | Confirmed (useful marker) |
| `config/sdwan_adv/wan_bonding[].dynamic_weight`: false static, true dynamic | Confirmed boolean |
| Bond `members[]` are selection criteria, distinct from runtime members | Confirmed |
| Runtime member physical UID -> `status/wan/devices/<uid>/info.config_id` -> `config/wan/rules2[]._id_` | Confirmed |
| Criterion `type\|is\|none%config_name\|is\|<name>` matches `rules2[].trigger_name` | Confirmed |
| Criterion `type\|is\|ethernet` | Confirmed form |
| Runtime `config_weight`, `ds_weight`, `us_weight` are separate reported quantities | Confirmed |
| `config/sdwan_adv/qoe.pkt_loss_cnt.enabled` | Confirmed |
| A stable logical steering rule UUID | Not exposed. Stats and events use the rule name |
| Configured bond `_id_` -> runtime bond interface | Unknown (no universal mapping) |
| Criterion weight -> runtime weights (100 -> 33/33/33 on R2400) | Inference only (consistent with division across members); not encoded |
| Units of runtime bond bandwidth fields | Unknown |
| Source and meaning of the previously seen QoE value `4294967295` | Unknown (not reproduced) |

## 2. Steering definitions: `steering_rule_defs`

Built by `analyzer/steering_defs.py` from successful reads of
`status/wan/steering/rules` (the steering parent read already performed each
pass; no extra router I/O).

Definition fields (per compiled child, from `xpolicy` only): `trigger_priority`,
`intent` (Traffic Class UUID), `dst_ip_network` (identity UUID) with
`dst_kind`, `dst_neg`, `dst_port_neg`, `src_lans` (names), `excluded_lans`
(`config_id`s), `trigger_strings` (`[trigger_string, trigger_priority]`),
`wan_priority_enabled`, `loadbalance_algo`, `failover`,
`reset_existing_flow`, `dia`.

Excluded (runtime, never fingerprinted): `matched_devs`, `steering_to`,
`primary_wandev`, the top-level runtime `loadbalance_algo` and friendly
`intent` label, flow gauges, selected WANs, resolved names and identity-cache
completeness. Same-named fields at the top level of a compiled rule are
runtime values; only the nested `xpolicy` path is read.

`dst_kind` keeps destination values explicit: `identity` (a UUID),
`literal_none` (the string `"None"` as NCOS reported it), `empty` (absent,
null or empty: no destination criterion), `other`.

Identity: rules are grouped by the existing normalized logical name
(`identity_basis: logical_name_only`). Within a rule, compiled children are
grouped by a hash of their canonical definition with a multiplicity `count`,
so children identical except for compiled IDs are kept (count 2), never
dropped. Compiled IDs are stored as supporting evidence (`compiled_ids`, at
most 8 per group, and `compiled_id_fp`), never as identity and never in the
version fingerprint.

Fingerprint (`fp_version` 1): hash of `[(rule key, definition-content hash)]`.
Array order is ignored (children, LAN lists, trigger lists are sorted);
duplicate multiplicity is preserved. The legacy per-rule
`semantic_fingerprint` and the `steering_definitions` version object are
unchanged. They remain sensitive to runtime WAN membership and identity-cache
contents; use `steering_rule_defs` for definition analysis.

### Read quality

| Read | Treatment |
|---|---|
| Failed read | Nothing versioned (the steering keys are not in the pass's successful set) |
| `unavailable` (no rules object) | Withheld |
| `partial` (a child without `xpolicy` or name) | Withheld; `invalid_children` counted |
| `empty` after a non-empty version | Withheld once (`empty_unconfirmed`); a second consecutive empty read is versioned |
| `complete` | Versioned; a new version only when the fingerprint changes |

### Resolution (exact UUIDs only)

`resolve_destination` joins `dst_ip_network` to `config/identities/ip` by full
`_id_`; prefixes and case variants never match. `resolve_intent` joins
`intent` -> intent `_id_` -> `traffic_class` -> Traffic Class `_id_`.
`criterion_kind` is `application` or `category` only when a runtime stats
entry for that identity name carries a real `appname` or `catname`; otherwise
`unknown`. It is never inferred from identifier text.

The identity, intent and Traffic Class configuration is read in the **same
pass** as a changed definition set (`Service._with_steering_deps`), so
resolution is consistent. It is not re-read while definitions are unchanged,
so high-rate Recording does not re-read identity configuration.

## 3. Steering change detection

`analyzer/steering_changes.py` compares two `steering_rule_defs` payloads.
`analyzer/steering_state.py` runs it when the current version moves.

Change types: `rule_added`, `rule_removed`, `destination_criterion_added`,
`destination_criterion_removed`, `traffic_class_changed`,
`rule_priority_changed`, `wan_selector_changed`,
`wan_selector_priority_changed`, `algorithm_changed`, `failover_changed`,
`reset_existing_flow_changed`, `source_lans_changed`, `excluded_lans_changed`,
`definition_field_changed` (`wan_priority_enabled`, `dst_neg`,
`dst_port_neg`, `dia`), `compiled_child_count_changed`, and
`definition_changed` (only a combination of fields moved between children).

Each event carries `rule_key`, `rule_name`, `change_type`, `field`,
`previous`, `current`, `from_version`, `to_version`, `previous_observed_ts`,
`detected_ts`, `interval_complete`, `identity_basis`, `identity_confidence`
(`name_only`), `source.completeness`, `source.valid`, `evidence` (compiled
IDs before/after and whether they changed), `derived_from`
(`live_observation` or `adjacent_versions`) and
`detection_semantics: observed_definition_difference`. Destination events also
carry `value_kind`, `criterion_kind` and `kind_basis`; rule add/remove events
carry `possible_rename`.

Example: Traffic Class and priority changed on rule `R`:

```json
{"change_type": "traffic_class_changed", "rule_key": "r", "field": "intent",
 "previous": ["<intent uuid 1>"], "current": ["<intent uuid 2>"],
 "from_version": 1, "to_version": 2, "previous_observed_ts": 100,
 "detected_ts": 200, "interval_complete": true,
 "identity_basis": "logical_name_only",
 "source": {"completeness": "complete", "valid": true},
 "evidence": {"compiled_ids_changed": false, "...": "..."}}
{"change_type": "rule_priority_changed", "previous": [10], "current": [5]}
```

Limits and semantics:

- An event means two successful observations differ. It does not prove a
  user edit. The change happened at an unknown time inside
  (`previous_observed_ts`, `detected_ts`].
- Startup: the first-ever baseline announces nothing. After a restart the
  baseline is the version referenced by the newest retained observation (its
  time becomes `previous_observed_ts`). If none is retained, the store's
  newest version is used with `previous_observed_ts: null` and
  `interval_complete: false`.
- Rename: a removed and an added rule with identical definition content, in an
  unambiguous 1:1 pair, get `possible_rename` with `confirmed: false`. A rename
  is never confirmed; compiled-ID persistence under rename is unverified.
- Runtime flow gauges, selected WANs, capture restarts and applications that
  stop matching flows never create events (only definitions are compared).
- Persistence: change detail is derived from adjacent version payloads; there
  is no second change database. Transitions seen live are also kept in a
  50-entry in-memory ring (this also covers a return to an earlier retained
  version, A -> B -> A). At most 200 events per transition (`truncated`).

## 4. Runtime application matching (observation `steering_apps`)

Added to every observation whose `steering_stats` read succeeded
(`telemetry.steering_apps_record`):

`source_state` (`ok` | `empty` | `unavailable`), `defs_version` (the
`steering_rule_defs` version in force), `began_capture`, `capture_restarted`
(true/false against the previous pass, null when unknown), `flows_truncated`,
`rules_truncated`, `flow_values: "gauge"`, and `rules[]` with `rule`,
`rule_key`, `preferred_flows`, `nonpreferred_flows`, their `*_state`
(`value` or `missing`), `was_flow_matched`, `total_perf_events`,
`apps_total`, `apps_omitted`, `apps[]` (`id`, `appname`, `catname`,
`preferred_flows`, `nonpreferred_flows`, `matched`).

Applications with no match and no flows are counted in `apps_omitted`, not
listed. Bounds: 50 rules, 16 apps per rule, 200 apps per record (worst case
about 40 KB; typical under 2 KB). A failed read omits the section (the error
is in `collector_errors`); a successful empty read gives `empty`.

`steering_events_meta` (per successful events read): `began_capture`,
`total_perf_events`, `event_overflow`, `capture_restarted`. Stored steering
events additionally carry a bounded scalar-only copy of `destinations[]`
(10 entries, 8 keys, 64-character strings, `destinations_truncated`).

A capture restart is a new measurement epoch for gauges. It is never a rule
deletion.

## 5. Configured WAN bonding: `wan_bonding_config`

`analyzer/bond_config.py` normalizes `config/sdwan_adv/wan_bonding` (now a
static config key, read at startup, on reconciliation and by the `bonding`
manual refresh). The legacy `wbond_config` object is unchanged.

Per bond: `config_id`, `name`, `dynamic_weight` (bool or null),
`operating_mode` (`static` | `dynamic` | `unknown`, from `dynamic_weight`
only, never from equal weights or member counts) and `criteria[]`
(`criterion_id`, `trigger_string`, `weight`, `form`, `profile_name`,
`type_value`). A missing weight is null; zero stays zero.

Fingerprint (`fp_version` 1) excludes criterion `_id_` and criterion order,
keeps duplicates, and distinguishes a null weight from 0. Runtime member
changes do not create a configuration version.

### Resolution against runtime (`GET /api/analytics/wbond_config`)

- `form: profile` resolves through `rules2.trigger_name`; `form: type`
  through an exact `rules2.trigger_string` match. Any other expression is
  `unresolved_expression` and kept raw.
- A criterion matches every runtime member whose physical UID's
  `info.config_id` is one of its `rules2_ids`. One criterion can match many
  members. A WAN that matches but is not a runtime member (for example a
  disconnected modem) is not counted.
- Association with a runtime bond: `candidate` only when there is exactly one
  configured and one runtime bond and at least one criterion matched
  (`coverage` full/partial). Otherwise `unverified` with a `basis`
  (`multiple_bonds_no_verified_mapping`, `no_runtime_bond`, ...).
- Runtime profile display names (`profile_name_reported`) are shown but never
  used to join.

### Weights

Four separate quantities, never related by a formula: criterion `weight`
(configuration), runtime `config_weight`, `ds_weight`, `us_weight` (reported
per member under `weights_reported`). They are not assumed to sum to 100, and
no traffic share or bonding efficiency is calculated. Static mode does not
imply constant runtime upload weights.

Legacy naming: `config.configured_weight` in `/api/analytics/wbond` and
`members[].configured_weight` in stored `wbond` history come from the runtime
`members_list[].config_weight`, not from configuration. They are kept; the
same value is also exposed as `observed.runtime_config_weight`.

Bandwidth fields (`ds_curr_bw`, `us_curr_bw`, `*_bw_trend`,
`*_last_bw_sample`) are reported raw under
`bandwidth_raw_units_unverified` and are not converted.

## 6. Bonding fingerprint and flow-total corrections

- `bonding_membership_fingerprint` (legacy) is pinned: it reads `mode`, a key
  normalized bonds never carry, so it ignores the bonding mode. It now falls
  back to a deterministic sort instead of raising on mixed null/number
  weights; digests for previously valid input are unchanged.
- `bonding_membership_fingerprint_v2` reads `bonding_mode`, includes
  `tunnel_iface`, sorts safely and tags its payload (`fp_version: 2`). It is
  exposed as `fingerprints.bonding_membership_v2` and
  `changes.bonding_structure_changed`.
- Flow totals: `summed` now requires at least one numeric member flow in that
  direction. Members that exist but reported no flow value give `unknown`
  with a null total (previously `summed` with 0). `reported` still includes a
  reported 0. Directions stay independent. History written by v0.8.2 with
  `summed`/0 for this case is not rewritten.

## 7. Cellular telemetry

Additive per modem record (`/api/cellular`, observation `cellular`):

- `carriers[].rf`: component RF from the indexed component families
  (`<METRIC>_5G_PCELL`, `<METRIC>_5G_SCELL<n>`) only, matched to the
  deduplicated carrier by role and identity. `rsrp`, `rsrq` (as reported,
  scaling unverified), `sinr` (dB or null), `sinr_raw`, `sinr_scale`,
  `validity` per metric. `rf: null` means no component RF; serving-level
  RSRP/RSRQ/SINR stay on the modem record and are never copied in.
- RAT comes from the band text, not from `_5G_` in the field name (an
  `_5G_PCELL` with `Band 66` is LTE).
- SINR scaling: |raw| > 50 is treated as tenths of a dB
  (`tenths_db_plausibility`; consistent with Speedtest Analyzer and the
  Phase 1A values 181, 290, 250). Smaller values are `ambiguous`: `sinr` null,
  `sinr_raw` kept, validity `ambiguous_scale`.
- Placeholders: a 0 on an inactive or 0 MHz component entry is
  `placeholder_zero` (null). A 0 elsewhere stays a valid 0.
- `carrier_evidence`: `source_entry_count`, `active_source_entry_count`,
  `deduplicated_count`, `collapsed_duplicate_count`, `channelless_count`,
  `ambiguous_identity_count`, `collapsed_groups[]` (identity, roles,
  `ambiguous`: two different indexed component entries with one identity,
  such as SCELL0 and SCELL1). Deduplication behavior is unchanged.
- `context`: `connection_state`, `sim_slot` (`sim1`/`sim2` from `info.sim`),
  `sim_present` (false only for `NOSIM: TRUE`, else null), `module_port`
  (`info.port`), `wan_profile_id` (`info.config_id`), `carrier_label`
  (`CARRID`, falling back to `HOMECARRID`), `roaming_code` and
  `roaming_state` (`home` only for the documented `ROAM` value `1`, else
  `unverified`).

Never persisted: IMEI, IMEI2, IMEISV, IMSI, ICCID, MDN, CGSN, GSN, raw cell
IDs, serials, MAC addresses, GPS, credentials. The existing hashed
`cell_key_hash` and `ca_signature` are unchanged. A typical NSA modem record
is about 2 KB.

## 8. QoE validity

`normalize.qoe_paths` withholds latency values that are sentinel
candidates (4294967295, 2^64-1) or implausible (< 0 or > 600000 ms):
`latency_*_ms` becomes null, `latency_*_raw` keeps the value and
`validity.latency_*` is `valid` | `missing` | `sentinel_candidate` |
`implausible`. `pkt_loss_sentinel_candidates` counts candidates inside the
raw loss object, which is kept unchanged. No packet-loss percentage is
calculated. Observation `qoe` records carry these fields only when something
is not plainly valid. `qoe.config.pkt_loss_cnt_enabled` (snapshot) and
`qoe_meta` (observation, with `config_read_ts`) report the QoE config
context. SWANS and QoE remain separate sources.

## 9. Endpoints (read-only, no router I/O)

| Endpoint | Content |
|---|---|
| `GET /api/analytics/steering` | `definitions` (tracker status), `rules[]` (children with resolved destination and Traffic Class, compiled IDs, `runtime` block with apps joined to identity UUIDs and `in_rule_definition`), `runtime_only_rules`, `capture`, `as_of.sources`, `notes`. 503 before the first snapshot |
| `GET /api/analytics/steering/changes` | `status`, `versions[]` (newest first), `transitions[]` (newest first), `next_before_version`, `limits`. Params `limit` (1-50, default 20), `before_version` (exclusive). 400 on invalid params |
| `GET /api/analytics/wbond_config` | `config_state`, `config_version`, `configured[]` (mode, criteria with resolution and matched members, association), `runtime[]` (members, reported weights, flows, raw bandwidth), `as_of.sources`, `notes`. 503 before the first snapshot |
| `GET /api/analytics/validity` | per-collector health, section freshness, steering definition status, capture metadata, bonding config state, QoE validity counts and config, cellular carrier evidence |

Existing endpoints keep their keys and meaning. Additive keys: `qoe.config`,
`cellular.rf_notes`, `fingerprints.bonding_membership_v2`,
`changes.bonding_structure_changed`, `/api/analytics/wbond`
`observed.runtime_config_weight`.

## 10. Historical schema

New observation keys: `steering_apps`, `steering_events_meta`, `qoe_meta`;
`steering_events[].destinations` (+ `destinations_truncated`);
`cellular[].carriers[].rf`, `cellular[].carrier_evidence`,
`cellular[].context`; QoE validity fields (only when not plainly valid).
New config-version objects: `steering_rule_defs`, `wan_bonding_config`.
Older observations lack these keys; readers treat an absent key as "not
recorded", never as zero or as a current-state lookup. Nothing is back-filled.
There is no application catalog.

## 11. Resource and storage considerations

- Collection: one new small config read per reconciliation
  (`config/sdwan_adv/wan_bonding`, `config/sdwan_adv/qoe`). Identity, intent
  and Traffic Class configuration are additionally read only in a pass whose
  steering definitions changed.
- CPU per steering pass: one definition extract and hash; diffing only when
  the version moves.
- History growth, measured locally on the R1900-shaped test fixture (5
  logical rules, 3 matched apps, one NSA modem with 4 carriers): a scheduled
  observation grows from 5.8 KB to 8.5 KB (+2.7 KB: `steering_apps` 1.6 KB,
  meta blocks under 200 bytes, the rest component RF/evidence/context). That
  is +0.06 MB/day at 60 min, +0.26 MB/day at 15 min and +0.78 MB/day at
  5 min, inside the 32 MB / 7-day history budget. A Recording steering sample
  is about 3.7 KB (180 per 30-minute job, about 0.7 MB). Real routers with
  more rules, apps or modems scale roughly linearly up to the caps above.
- Config versions: one `steering_rule_defs` version measured 8 KB for that
  fixture (about 1.6 KB per rule with two compiled children each); 20
  versions per object are retained and a new version is written only when
  definitions change. The config file compacts above 512 KB.

## 12. Live validation record (v0.8.3, lab routers)

Measured on two NCOS 7.26.81 lab routers (R1900: static bond ETH+CELL, one
connected modem; R2400: dynamic bond CELL ONLY, three connected modems) about
15 minutes after a fresh deploy, schedule 1 minute. This supersedes the
fixture estimate in section 11 for real routers.

- Observation size (compact JSON, scheduled): R1900 about 17 KB, R2400 about
  25 KB. v0.8.3 added about 5.3 KB (R1900: `steering_apps` 4.1 KB, cellular
  1.1 KB) and about 7.8 KB (R2400: `steering_apps` 3.4 KB, cellular 4.3 KB,
  three modems with component carriers) per observation, about 31% of the
  record. That is 2-3 times the fixture estimate, because real routers have
  more rules and more modems.
- Growth: R1900 about 4.9 MB/day at 5 min, 1.6 MB/day at 15 min, 0.4 MB/day at
  60 min. R2400 about 7.2, 2.4 and 0.6 MB/day. At 1 minute the 32 MiB history
  cap, not the 7-day age limit, bounds retention (about 1.4 days on R1900,
  0.9 days on R2400). At 5 minutes the cap binds on both (about 6.8 and
  4.7 days). At 15 minutes or slower the 7-day limit binds first.
- Retention sweep under load was not observed live (history was far below the
  cap); it is covered by automated tests only.
- Configuration groups, including `wan_bonding_config`, are re-read at startup
  and about hourly. A scheduled observation therefore references
  `wan_bonding_config` only when that version was read in the same pass.
- Component RF: a raw `0` on an active carrier with non-zero bandwidth is
  kept and labelled `valid` by design (`test_cell_valid_zero_not_placeholder`).
  One AT&T SCell reported RSRP 0 this way (2 of 79 stored component RF
  records). RSRP 0 dBm is not physically plausible, so treat it as unverified
  until a plausibility range is added.
- Not observed live: a real steering definition change, a QoE sentinel value
  (`4294967295`), the hourly config reconciliation, and reading legacy (pre
  v0.8.3) records from disk (the redeploy started from an empty store).
