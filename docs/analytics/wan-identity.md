# WAN identity and classification (v1.0.2)

SD-WAN Analyzer has **one** authoritative WAN identity layer:
`apps/sdwan_analyzer/analyzer/wan_identity.py`. It turns each object in
`status/wan/devices` into a normalized identity record. Every other module
(snapshot, topology, WBOND, steering annotation, cellular, history,
Recordings, the Overview) consumes those records. No module and no UI
component classifies, filters or names a WAN on its own.

```
NCOS collectors -> wan_identity.build_inventory() -> normalize.wan_paths
                -> service snapshot / topology / wbond / history -> API -> frontend
```

It is a pure function over data already in the collector cache: no new
polling, no router writes, no logging, no per-request parsing. The only extra
read is `status/product_info/product_name` (a few bytes), taken with the
static config set at startup and on the hourly reconcile.

## Relationship to Speedtest Analyzer (STA)

The naming and hardware logic is ported from STA's validated
`speedtest_web.py` (`_friendly_wan_name`, `_wan_is_cellular_device`,
`_carrier_abbreviation`, `_sim_slot_label`, `_satellite_wan_label`,
`_sdwan_underlay_uid`, `_get_model_family`, `_validation_captive_models`,
`_validation_match_model`). It is re-implemented against a private copy of the
model names (`analyzer/wan_hardware_catalog.json`, derived from STA's
`device_validation_catalog.json`, catalog version `2026-08-20`). There is no
import of, and no runtime dependency on, another SDK application. STA's
speed-test validation statuses and known defects are intentionally not
carried over.

Deliberate differences from STA (each with an engineering reason):

| Topic | STA | SD-WAN Analyzer | Why |
|---|---|---|---|
| Carrier match for `T-Mobile` | substring `tmobile` | word-boundary match | `Mint Mobile` (`mintmobile`) contains `tmobile`; an MVNO was labelled `TMO` |
| Carrier but no SIM slot | `Cellular WAN` | carrier only (`ATT`) | do not drop a verified carrier |
| Carrier source | `CARRID` only | `CARRID`, then `HOMECARRID` | a populated but unregistered SIM has no `CARRID` (`diagnostics.md`) |
| Captive model unknown | `Captive Modem Captive - ...` | `Captive Modem - ...` | duplicated word |
| SD-WAN tunnel name | `SD-WAN Tunnel - <underlay>` | same | (v1.0.1 used `SD-WAN — <underlay>`; now aligned) |
| eSIM | not handled | `<owner> - ATT-eSIM` | E400 AT&T eSIM scenario |

## Identity axes (never conflated)

| Axis | Fields | Rule |
|---|---|---|
| Immutable NCOS identity | `uid`, `iface`, `info_uid` | The only join keys: history, WBOND, steering, routing. Never replaced by a name. |
| Presentation | `display_name` | Display metadata only. May change when a SIM or carrier changes. |
| Topology object | `object_class` | `physical` / `sdwan` / `wbond` / `unknown` |
| Medium | `kind` | `cellular` / `ethernet` / `wifi` / `satellite` / `sdwan` / `wbond` / `unknown` |
| Inventory | `classification`, `inventory_visible` | `classification` is `object_class`, or `empty_sim` for a confirmed empty SIM slot |
| Operation | `connection_state` (raw), `operational_state`, `failure_reason` | independent of SIM presence |
| SIM | `sim_presence`, `sim_type`, `sim_slot`, `carrier`, `carrier_name`, `carrier_source` | see below |
| Modem | `modem_owner`, `modem_model`, `host_model` | see below |
| Overlay | `underlay_uid`, `underlay_state`, `underlay_available` | overlays only |
| Audit | `classification_evidence` | evidence **codes** only |

## Record fields

| Field | Values / notes |
|---|---|
| `operational_state` | `connected` (raw state is `connected`), `inactive` (any other reported state: standby, disconnected, available, connecting...), `unknown` (no state reported) |
| `failure_reason` | sanitized `status.error_text`, only when not connected. IPv4s and 8+ digit runs are masked; max 80 chars |
| `sim_presence` | `present`, `empty`, `unknown`, `not_applicable` (non-cellular objects). A modem object with too little evidence to be called cellular reports `unknown` |
| `sim_type` | `physical`, `esim`, `unknown`; `null` for non-cellular |
| `sim_slot` | `SIM1` / `SIM2` from `info.sim`; `null` when not reported |
| `carrier` | abbreviation `ATT` / `VZW` / `TMO` only; other carriers and MVNOs are `null` |
| `carrier_name` | sanitized device-reported name (e.g. `Mint Mobile`) |
| `carrier_source` | `registered` (`CARRID`) or `home` (`HOMECARRID`); an empty slot has no carrier |
| `modem_owner` | `internal`, `captive`, `unknown`; `null` for non-cellular |
| `modem_model` | catalog match, else sanitized device-reported name, else `null` |
| `host_model` | `status/product_info/product_name`, upper-cased and sanitized |
| `inventory_visible` | `true` only for physical objects that are not confirmed empty SIM slots |

## Raw versus displayable inventory

| View | Contents | Where |
|---|---|---|
| **Raw NCOS inventory** | every object NCOS reported, incl. empty SIM slots, overlays, WBOND, unresolved objects | `wan.paths[]`, `/api/wan`, `/api/analytics/wan_identity` `wans[]` |
| **Displayable physical inventory** | physical objects except confirmed `empty_sim` | `wan.physical[]`, `topology.physical[]`, `displayable_physical_uids`, Overview KPIs and Path Status |
| **Hidden physical (diagnostics)** | confirmed empty SIM slots | `topology.hidden_physical[]`, `wan.empty_sim_uids` |

Included in the displayable view: Ethernet, Wi-Fi, satellite, cellular with a
populated SIM, configured eSIM, legitimate disconnected WANs, and cellular
WANs whose SIM presence is `unknown`. Excluded: only confirmed empty SIM slots.

`system_summary`: `physical_count` (displayable), `physical_raw_count`,
`empty_sim_count`, `physical_connected_count`, `physical_inactive_count`,
`wan_path_count` (excludes empty SIM slots), `wan_object_count` (raw).
`wan.inventory` carries the same counts plus `unknown_state`, `overlays`,
`wbond`, `unclassified`.

Validated R2400 expectation (synthetic fixture
`tests/wan_identity_fixtures.py::r2400_devices`): 7 raw physical, 3 empty
SIM, 4 displayable, 3 connected, 1 inactive. The counts are computed, never
hardcoded.

## SIM presence rules

`sim_presence = empty` requires an **explicit NCOS indicator**:

| Indicator | Code |
|---|---|
| `diagnostics.NOSIM == "TRUE"` | `diag_nosim_true` |
| `diagnostics.PIN_STATUS == "NOSIM"` | `diag_pin_status_nosim` |
| `status.error_text` names `NOSIM` (e.g. `SIM error: NOSIM`) | `status_error_text_nosim` |

Any one is sufficient when nothing contradicts it. These are **never**
evidence of an empty SIM: disconnected, not registered, standby, no IP,
failing configuration, zero throughput, unavailable carrier name, a disabled
interface, or `SERDIS: NOSIM` (a display string, not an indicator).

`present` needs positive evidence and no empty indicator: ICCID present
(presence only; the value is never read into a record), a registered carrier
(`CARRID`), `PIN_STATUS == READY`, or an eSIM active profile. Otherwise
`unknown`.

**Contradiction** (an empty indicator alongside any of: ICCID, registered
carrier, `PIN_STATUS READY`, `NOSIM == FALSE`, eSIM evidence) gives
`sim_presence = unknown` with evidence `contradictory_sim_indicators`. The
object is retained. When in doubt the object is shown, never hidden.

An empty SIM object keeps `object_class = physical`, `kind = cellular`, its
raw uid/iface, its topology relationships and its counters. Only
`classification = empty_sim` and `inventory_visible = false` change.

## Active / inactive handling

`operational_state` is derived from the raw `connection_state` alone and is
independent of SIM presence. A populated SIM or configured eSIM that cannot
connect (standby, failed registration, carrier rejection, APN failure) is a
legitimate physical WAN: `classification = physical`, `inventory_visible =
true`, `operational_state = inactive`, and `failure_reason` preserved.
Disconnected Ethernet is likewise `inactive` and stays listed.

Documented E400 scenario (AT&T eSIM, `Internal 5GE-AM (eSIM)`, state
`available (operation failed)`, `CPPM failed: no carrier`, not registered):
`E400 Internal - ATT-eSIM`, `sim_type = esim`, `carrier = ATT`
(`carrier_source = home`), `inactive`, reason kept, **not** `empty_sim`.

eSIM evidence: the module label contains `eSIM` (`info.product`), or an
eUICC-capable module (`EUICC_SGP22 == TRUE`) reports an **active** profile
(`EUICC_PROF<n>` active flag `1`). eUICC capability alone is not enough.

## Internal and captive modems

Ownership never relies on the UID prefix.

| Evidence | Owner | Model |
|---|---|---|
| `info.remote.internal_captive == true` | `internal` | `remote.product_name` (e.g. `R2400SDX`) |
| `info.remote` present, flag not true | `captive` | catalog match in `remote.product_name` (longest token), else the device-reported name, else a catalog match in any remote text, else `null` |
| no `info.remote` | `internal` | `MFG_MDL` / `MDL` if reported |
| `info`/`remote` malformed | `unknown` | `null` |

`internal_captive = true` is authoritative: NCOS represents some internal
modems (R2400) through the remote-modem model, so they are never labelled
captive. If such a record also names a catalog captive model, ownership stays
`internal` and the evidence `remote_product_matches_captive_catalog` records
the conflict.

Host router recognition uses the catalog: `controllers` (E400, E3000, R920,
R980, R1900, R2400) get the ` Internal` suffix; other known standalone models
(W1850, ...) are named without it (`W1850 - TMO-SIM1`); an unknown host model
is used verbatim (upper-cased); with no host model the owner prefix is omitted.

## Friendly-name precedence

`display_name` is built in this order; an unsupported part is omitted, never
guessed:

1. Satellite evidence (`starlink`/`satellite` anywhere in uid, iface, type,
   product or diagnostics) -> `Satellite WAN-XXXX` (last 4 of the uid). This
   is checked before cellular because satellite WANs can use `mdm-*` uids.
2. Cellular (needs carrier, SIM slot, an `lte/4g/5g/nr/cellular/wwan` token,
   or an explicit empty-SIM indicator; an `mdm-*` uid alone is not proof):
   `<owner> - <carrier>-<slot>`
   - owner: `<MODEL> Captive`, `Captive Modem`, `<HOST> Internal`, `<HOST>` or none
   - connection: `ATT-SIM1`, `ATT-eSIM`, `SIM2`, `ATT`, else `Cellular WAN`
   - e.g. `R2400 Internal - ATT-SIM1`, `RC1250 Captive - VZW-SIM1`,
     `W1855 Captive - TMO-SIM2`, `E400 Internal - ATT-eSIM`
3. `Wi-Fi as WAN`, `Ethernet WAN`.
4. WBOND: `Bonded WAN` (`Bonded WAN (<config name>)` when known).
5. SD-WAN overlay: `SD-WAN Tunnel - <underlay name>`, else `SD-WAN Tunnel`.
6. Otherwise `info.product`, `info.iface`, the uid, or `Unknown WAN`.

No carrier logos. Carrier firmware packaging is never read as evidence of a
SIM.

## Consumers and API contracts

All additions are additive; no existing field changed meaning except the
documented `physical` / `physical_count` / `wan_path_count` semantics.

| Consumer | Contract |
|---|---|
| `GET /api/wan`, `/api/snapshot` | `wan.paths[]` = raw inventory with every identity field; `wan.physical[]` = displayable; `wan.empty_sim_uids`; `wan.inventory`; `wan.identity` (host model/family, catalog info) |
| `GET /api/analytics/wan_identity` | raw `wans[]`, `displayable_physical_uids`, `empty_sim_uids`, `inventory`, `identity_versions`, `notes`; `?version=N` returns that retained version's payload (404 if aged out) |
| `GET /api/analytics/topology` | `physical[]` displayable; `hidden_physical[]`; `inventory`; per-object `identity` block; relationship refs gain `display_name`, `classification`, `sim_presence`, `available`, `unavailable_reason`; `unavailable_references[]` |
| `GET /api/analytics/wbond` | members are never filtered; `physical_display_name` from the shared identity; `unavailable_references[]` |
| `snapshot.bonding.bonds[].members[]` | `display_name` and an `identity` block (`available`, `operational_state`, `failure_reason`, ...) |
| `snapshot.cellular.modems[]` | `identity` block (observations persisted to history stay identity-free) |
| `snapshot.traffic_steering` | `logical_rules[].steering_to_identity[]`, `primary_wandev_identity`; `events[].wan_from_identity` / `wan_to_identity`. Raw selector values are untouched; names are additive; unresolved references stay unresolved |
| `GET /api/analytics/cellular`, `/api/analytics/sdwan_history` | each `modems[]` / `interfaces[]` row gains `identity`: `current`, `basis`, `changed_in_window`, `versions_in_window[]` |
| Overview | KPIs and Path Status use `topology.physical`; inactive WANs show their failure reason; WBOND member details show SIM placement and flag an empty-SIM member; the interface selector skips empty SIM slots |

Not part of v1.0.2: new pages. The same records are what future WAN, bonding,
steering, cellular, history and recording pages should read.

## Historical identity stability

- Observations and Recordings persist stable uids only; no display name is
  written to a sample, so a rename never rewrites or orphans history.
- Identity is versioned as the `wan_identity` config object (compact payload
  keyed by uid, fields: name, class, classification, kind, SIM presence/type/
  slot, carrier, modem owner/model, underlay uid). Volatile fields
  (connection state, failure reason, IP) are excluded, so a link flap never
  creates a version. It is versioned only from a fresh `wan_devices` /
  `product_name` read (failed-read invariant), and the version number rides in
  every observation's `config_versions` and in a Recording's
  `config_versions_start/end` (so `config_changes` reports an identity change
  during a Recording).
- History APIs label the current identity as current and list the identity
  versions seen in the scanned window. A consumer must not present the current
  carrier or SIM placement as historical fact when `changed_in_window` is true
  or `basis` is not `versioned`.
- Limits: only the newest versions are retained (`max_versions_per_object`);
  data written before v1.0.2 carries no identity version (`basis:
  current_only`). A version that aged out is listed with `retained: false`.

## Unknown and ambiguous cases

| Situation | Result |
|---|---|
| contradictory SIM indicators | `sim_presence unknown`, retained |
| cellular object, no SIM evidence | `unknown`, retained |
| `mdm-*` uid, no cellular evidence | `kind unknown`, `sim_presence unknown`, named from product/iface/uid (v1.0.1 behavior) |
| unlisted carrier / MVNO | `carrier null`, `carrier_name` kept, SIM slot in the name |
| unlisted captive model | the device-reported model, else `Captive Modem` |
| malformed `info` / `remote` / `diagnostics` | `unknown` ownership or presence; never an exception |
| overlay with no / unknown / non-physical dependent | `underlay_state unresolved`, name `SD-WAN Tunnel` |
| steering reference that is not a uid or a unique iface | `resolved false` |
| model catalog missing or invalid | models are not recognised (no `Internal` suffix; the device-reported captive model is used); `catalog.load_error` is reported |

## Security and privacy

- ICCID, IMSI, IMEI, serial numbers, MDN/phone number, PIN, EID and eUICC
  profile strings are never copied into an identity record, a snapshot, an
  API response or a version payload. ICCID and eSIM profiles are used only to
  establish that something is present.
- Device-reported text (carrier, model, error text) is sanitized: restricted
  character set, length-limited, and rejected or masked when it contains
  an 8+ digit run (identifier-like) or an IPv4 address.
- `classification_evidence` lists evidence codes, not raw values.
- Read-only: nothing here writes router configuration.
- Tests plant synthetic IMEI/IMSI/ICCID/MDN/EID/serial values in the fixtures
  and assert none appear in any response.

## Model-specific validation status

"Validated" below means the identity logic is exercised by a synthetic,
sanitized fixture built from the documented/supplied field shapes. It does
**not** mean the logic was run against a live router: the dev router was
unreachable during the v1.0.2 work.

| Model / case | Status |
|---|---|
| R2400, internal modem (`internal_captive = true`) | fixture; matches the supplied R2400 API evidence (NOSIM, PIN_STATUS, error_text, remote model) |
| R2400 + RC1250 captive | fixture (STA validated the pairing for speed tests, 2026-08-19) |
| R2400 + W1855 captive | fixture |
| R1900 internal / captive naming | STA-validated naming; fixture only here |
| E400 AT&T eSIM, failed registration | fixture from the documented scenario. Unverified on hardware: the exact field carrying `CPPM failed: no carrier` (assumed `status.error_text`) and the `info.product` eSIM label |
| `MFG_MDL` / `MDL` as internal modem model | field names from existing app code and `control/wan.md`; unverified on the R2400 |
| Satellite (Starlink) with `mdm-*` uid | STA-validated rule; fixture only here |

## Tests

`tests/wan_identity_tests.py` with `tests/wan_identity_fixtures.py`, run by
`tests/run_tests.py`. Covers: R2400 multi-module inventory, each explicit
NOSIM indicator, standby and failed populated SIMs, "inactive is not empty",
E400 eSIM, `internal_captive`, RC1250, W1855, unknown captive model, unknown
carrier, Ethernet up/down, satellite with an `mdm-*` uid, empty-SIM and
inactive-WAN relationships (topology, WBOND), resolved and unresolved
overlays, steering references, historical continuity after a name change,
contradictory indicators, malformed input, catalog handling, snapshot and API
contracts, privacy, and version sync (`package.ini` = `analyzer.VERSION`).
