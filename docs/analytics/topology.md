# Topology and relationships (v0.8.0)

`GET /api/analytics/topology` reports how the physical WAN interfaces, SD-WAN
overlays and WBOND interfaces relate, with the known state of each. It is
read-only and current-state only. It is computed on demand from the cached
snapshot and the cached `status/wan/devices`: no new collector, no storage, no
router I/O.

## Object classes (never merged, never summed)

| Class | Meaning | Source |
|---|---|---|
| `physical` | actual transport (`ethernet-wan`, `mdm-*`) | `status/wan/devices` |
| `sdwan` | logical overlay tunnel | `status/wan/devices` (`info.type = sdwan`) |
| `wbond` | logical bonded interface | `status/wan/devices` + `status/sdwan_adv/wan_bonding` |

Their byte counters and rates describe the same traffic at different layers and
are **not additive**. The response carries `counters_additive: false` and no
counters, rates, bandwidth utilization or efficiency figures.

## Response

| Key | Content |
|---|---|
| `schema_version` | `1` |
| `as_of` | snapshot time, WAN and bonding collection times, per-source `last_success_ts`, `read_error`, `stale` |
| `primary` | `state` `known` / `unknown` / `unresolved`; when known: uid, class, kind, name, connection state, IP |
| `physical[]` | `config` (priorities), `observed` (connection state, IP, `is_primary`), `overlays[]` riding on it, `bond_memberships[]` |
| `overlays[]` | `config`, `observed`, `underlay` relationship, `bond_memberships[]` |
| `bonds[]` | `wbond_device` relationship, `config` (`membership: not_collected`), `observed` (link state, mode, reported vs listed member count), `members[]` |
| `bonds[].members[]` | `tunnel_iface`, `reported_intf_name`, `overlay`, `physical`, `underlay_consistency`, `observed`, `config.configured_weight` |
| `bonding_runtime` | `read` or `unknown` (never read), and bond count |
| `unresolved[]` | flat list of `{kind, uid, reason}` for every relationship not established |
| `notes[]` | fixed statements of what is not claimed |

Every relationship object has `state` `resolved` or `unresolved`. Resolved ones
carry `basis` (the NCOS field used); unresolved ones carry `reason` and `uid:
null`. `is_primary` is `null`, not `false`, when the primary is unknown.

Configuration (`config`) and observed runtime (`observed`) are separate blocks.

## What is verified

| Relationship | Rule | Basis |
|---|---|---|
| overlay to physical | `status.active_dep_wandev` when set; else a single `dep_wandevs` entry; the target must be a known physical WAN | `active_dep_wandev`, `dep_wandevs_single` |
| bond to WBOND device | exactly one `wbond` device whose `info.iface` equals the bond interface key | `wbond_iface_matches_bond` |
| bond member to overlay | members_list key equals exactly one overlay `info.iface` | `tunnel_iface_matches_overlay` |
| bond member to physical | the member's `intf name`: exact device key, else a unique `info.iface` match | `intf_name_is_device_key`, `intf_name_matches_iface` |
| primary | `status/wan/primary_device`, present in `status/wan/devices` | |

A bond member has two independent routes to a physical WAN (overlay underlay and
`intf name`). `underlay_consistency` is `agree`, `conflict` or `not_comparable`.

## Unresolved reasons

`multiple_dependents_none_active`, `no_dependent_reported`,
`not_in_wan_devices`, `not_a_physical_wan`, `no_wbond_device_for_bond`,
`ambiguous_wbond_device`, `tunnel_iface_not_an_overlay`,
`ambiguous_tunnel_iface`, `no_intf_name_reported`,
`intf_name_not_in_wan_devices`, `ambiguous_intf_name`,
`wbond_device_without_runtime`, `overlay_dependent_differs_from_intf_name`,
`primary_device_not_reported`, `primary_not_in_wan_devices`.

The overlay rule is stricter than `wan.underlay_map` in `/api/snapshot`, which
takes the first `dep_wandevs` entry when none is active. Topology reports that
case as unresolved instead.

## Known limitations

- **Configured WBOND membership is not collected.** Membership is the runtime
  `members_list`. `config.membership.state` is `not_collected`. No configuration
  path for it has been verified.
- Member and overlay state (`UP`, `connected`, `ATTACHED`) is observed state. It
  does not show that traffic is carried, or that the path is healthy.
- A WBOND member's physical WAN comes from NCOS-reported `intf name`. If the
  field is absent the physical relationship is unresolved, not inferred.
- Relationships reflect the last successful reads. `as_of.sources.*.stale` is
  true when the latest read of that source failed and cached data is shown.
- Tested against fixtures in the R1900 captured shape. The underlying fields
  were validated on R1900 and R2400 in earlier phases; this endpoint has not
  itself been run against a live router.
