"""Normalization: raw NCOS shapes -> stable internal records.

This layer isolates the app from NCOS field naming and quirks. Downstream
code (storage, service, web) depends on THESE record shapes, not on raw
NCOS JSON. If NCOS field names change, only this module changes.

Friendly-naming and cellular-classification logic here is a distilled reuse
of the VALIDATED patterns in Speedtest Analyzer (speedtest_web.py:
_friendly_wan_name / _wan_is_cellular_device / _satellite_wan_label). We
reuse the *concepts* proven to work on real NCOS devices, not the
speed-test code:

  - An mdm-* UID alone is NOT proof of cellular. Require carrier (CARRID),
    SIM slot, or an LTE/5G/NR/cellular/wwan service token.
  - Starlink/satellite must be detected BEFORE cellular, because Starlink
    can present an mdm-* UID while reporting like a non-cellular WAN.
  - The raw iface / UID is preserved untouched as the routing identity;
    friendly names are display-only.

Internal record shapes (documented contract):

  WanPathRecord = {
    'uid': str,                 # NCOS device UID (routing identity)
    'iface': str,               # raw NCOS iface (routing identity)
    'kind': str,                # 'cellular'|'ethernet'|'wifi'|'satellite'|'unknown'
    'display_name': str,        # friendly, display-only
    'connection_state': str,    # e.g. 'connected'
    'ip_address': str|None,
    'priority': number,         # config priority (lower = higher priority)
    'is_primary': bool,
  }

  SdwanOverlayRecord / WbondRecord / SteeringRecord are declared with the
  fields we can populate from VERIFIED paths; fields sourced from OPAQUE
  sub-trees are intentionally left as passthrough 'raw' until their shapes
  are confirmed on a router (see collectors.py OPAQUE note).
"""

import hashlib
import json
import re

# Overlay/tunnel wan_type values that are NOT physical WAN paths. Matches
# Speedtest Analyzer's exclude set; SD-WAN Analyzer keeps them but tags kind.
_OVERLAY_TYPES = ('sdwan', 'vpn', 'gre', 'ipsec')

# High-level object classes for a WAN dictionary entry. This is a DIFFERENT
# axis from classify_wan_kind() (which describes the physical medium of a
# path). object_class answers: physical WAN vs SD-WAN overlay vs WBOND
# virtual interface. WBOND is a separate forwarding object and is NEVER
# flattened into its physical members.
OBJ_PHYSICAL = 'physical'
OBJ_SDWAN = 'sdwan'
OBJ_WBOND = 'wbond'
OBJ_UNKNOWN = 'unknown'

_CELLULAR_TOKEN_RE = re.compile(
    r'(^|[^a-z0-9])(lte|4g|5g|nr5g|nr|cellular|wwan)([^a-z0-9]|$)')

_BLANK_CARRIER = ('', '--', 'unknown', 'none', 'n/a', 'not available')


def _s(value):
    """Safe lower-cased string of a possibly-None value."""
    return str(value if value is not None else '').strip()


def _identity_blob(uid, info, diagnostics):
    """Join identifying fields into one lower-case string for token tests."""
    parts = [
        _s(uid),
        _s(info.get('iface')),
        _s(info.get('type')),
        _s(info.get('product')),
    ]
    for v in (diagnostics or {}).values():
        if v is not None:
            parts.append(_s(v))
    return ' '.join(parts).lower()


def _is_satellite(identity_blob):
    return 'starlink' in identity_blob or 'satellite' in identity_blob


def _is_cellular(uid, info, diagnostics):
    """Positive-evidence cellular test (ported from Speedtest Analyzer)."""
    info = info or {}
    diagnostics = diagnostics or {}
    identity = _identity_blob(uid, info, diagnostics)

    if _is_satellite(identity):
        return False

    modem_identity = (
        _s(info.get('type')).lower() == 'mdm'
        or _s(uid).lower().startswith('mdm-')
    )
    if not modem_identity:
        return False

    carrier_present = _s(diagnostics.get('CARRID')).lower() not in _BLANK_CARRIER
    sim_present = bool(_s(info.get('sim')))
    service_token = _CELLULAR_TOKEN_RE.search(identity) is not None
    return carrier_present or sim_present or service_token


def classify_wan_kind(uid, device):
    """Return one of cellular|ethernet|wifi|satellite|unknown."""
    device = device if isinstance(device, dict) else {}
    info = device.get('info') if isinstance(device.get('info'), dict) else {}
    diagnostics = (device.get('diagnostics')
                   if isinstance(device.get('diagnostics'), dict) else {})

    identity = _identity_blob(uid, info, diagnostics)
    if _is_satellite(identity):
        return 'satellite'
    if _is_cellular(uid, info, diagnostics):
        return 'cellular'

    wan_type = _s(info.get('type')).lower()
    iface = _s(info.get('iface')).lower()
    if wan_type in ('wifi', 'wi-fi', 'wlan') or 'wifi' in identity:
        return 'wifi'
    if wan_type in ('ethernet', 'eth') or iface in ('wan', 'ethernet-wan'):
        return 'ethernet'
    return 'unknown'


def object_class(uid, device):
    """Classify a WAN dictionary entry as physical / sdwan / wbond / unknown.

    Uses info.type first (authoritative: 'sdwan', 'wbond', 'mdm',
    'ethernet', ...) then falls back to UID prefix conventions. This is the
    object-topology axis; classify_wan_kind() is the physical-medium axis.
    """
    device = device if isinstance(device, dict) else {}
    info = device.get('info') if isinstance(device.get('info'), dict) else {}
    wan_type = _s(info.get('type')).lower()
    u = _s(uid).lower()

    if wan_type == 'wbond' or u.startswith('wbond-') or u.startswith('wbond'):
        return OBJ_WBOND
    if wan_type == 'sdwan' or u.startswith('sdwan-') or u.startswith('sdwan'):
        return OBJ_SDWAN
    if wan_type in ('mdm', 'ethernet', 'eth', 'wwan', 'wifi', 'wi-fi', 'wlan'):
        return OBJ_PHYSICAL
    if wan_type in _OVERLAY_TYPES:
        # vpn/gre/ipsec overlays that are not SD-WAN/WBOND
        return OBJ_UNKNOWN
    # A device with a recognizable physical medium is physical.
    if classify_wan_kind(uid, device) in (
            'cellular', 'ethernet', 'wifi', 'satellite'):
        return OBJ_PHYSICAL
    return OBJ_UNKNOWN


def _cellular_label(uid, device, diagnostics):
    """Best-available cellular display label using carrier/model, no PII."""
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    device = device if isinstance(device, dict) else {}
    info = device.get('info') if isinstance(device.get('info'), dict) else {}
    carrier = _s(diagnostics.get('CARRID'))
    service = _s(diagnostics.get('SERDIS')) or _s(diagnostics.get('RFBAND'))
    if carrier and carrier.lower() not in _BLANK_CARRIER:
        if service:
            return '{} {}'.format(carrier, service)
        return carrier
    prod = _s(info.get('product'))
    if prod:
        return prod
    return 'Cellular WAN'


def friendly_name(uid, device):
    """Display-only WAN name for PHYSICAL paths. Overlay/WBOND naming is
    handled by friendly_name_ex() which can correlate an underlay label.

    NOTE: The full Speedtest implementation also folds in a device
    validation catalog (captive-modem model names). That refinement is
    intentionally DEFERRED; this returns a correct, generic label.
    """
    device = device if isinstance(device, dict) else {}
    info = device.get('info') if isinstance(device.get('info'), dict) else {}
    diagnostics = (device.get('diagnostics')
                   if isinstance(device.get('diagnostics'), dict) else {})
    kind = classify_wan_kind(uid, device)

    if kind == 'satellite':
        ident = _s(uid) or _s(info.get('iface'))
        m = re.search(r'([a-zA-Z0-9]{4})$', ident)
        return 'Satellite WAN-' + m.group(1).upper() if m else 'Satellite WAN'
    if kind == 'cellular':
        return _cellular_label(uid, device, diagnostics)
    if kind == 'wifi':
        return 'Wi-Fi as WAN'
    if kind == 'ethernet':
        return 'Ethernet WAN'
    return _s(info.get('product')) or _s(info.get('iface')) or _s(uid) or 'Unknown WAN'


def friendly_name_ex(uid, device, devices=None, underlay_uid='',
                     bond_config_name=''):
    """Object-aware friendly name.

    - physical: delegates to friendly_name().
    - sdwan overlay: 'SD-WAN — <underlay label>' when the underlay device is
      resolvable, else a generic 'SD-WAN Overlay'.
    - wbond: configured bond name when supplied, else 'Bonded WAN'.

    devices is the full status/wan/devices map so an overlay can borrow its
    physical underlay's label. Never discards the raw UID/iface upstream.
    """
    device = device if isinstance(device, dict) else {}
    devices = devices if isinstance(devices, dict) else {}
    oclass = object_class(uid, device)

    if oclass == OBJ_WBOND:
        name = _s(bond_config_name)
        if name:
            return 'Bonded WAN ({})'.format(name)
        return 'Bonded WAN'

    if oclass == OBJ_SDWAN:
        underlay_uid = _s(underlay_uid)
        underlay_dev = devices.get(underlay_uid) if underlay_uid else None
        if isinstance(underlay_dev, dict):
            return 'SD-WAN — {}'.format(friendly_name(underlay_uid, underlay_dev))
        return 'SD-WAN Overlay'

    if oclass == OBJ_PHYSICAL:
        return friendly_name(uid, device)

    # Unknown object class: fall back to physical-style naming, never blank.
    return friendly_name(uid, device)


def wan_paths(raw_devices, primary_uid, underlay_map=None):
    """Normalize status/wan/devices into a list of WanPathRecord.

    Includes overlay + WBOND objects too but tags each with object_class so
    the analyzer never flattens WBOND into its members and never confuses an
    overlay with a physical path. underlay_map (sdwan_uid -> underlay_uid),
    when supplied, lets overlay display names borrow the underlay label.

    Priority domains are kept SEPARATE and named clearly (they are not the
    same scale):
      cm_priority       config.priority  (physical Connection Manager order)
      original_priority status/config original_priority when present
      parent_priority   status/config parent_priority when present
    """
    records = []
    if not isinstance(raw_devices, dict):
        return records
    underlay_map = underlay_map if isinstance(underlay_map, dict) else {}
    for uid, device in raw_devices.items():
        if not isinstance(device, dict):
            continue
        info = device.get('info') if isinstance(device.get('info'), dict) else {}
        status = device.get('status') if isinstance(device.get('status'), dict) else {}
        config = device.get('config') if isinstance(device.get('config'), dict) else {}
        ipinfo = status.get('ipinfo') if isinstance(status.get('ipinfo'), dict) else {}

        oclass = object_class(uid, device)
        underlay_uid = underlay_map.get(uid, '')
        display = friendly_name_ex(uid, device, raw_devices, underlay_uid)

        # Priority fields can live in config or status depending on tree.
        cm_priority = config.get('priority')
        if cm_priority is None:
            cm_priority = status.get('priority')
        records.append({
            'uid': uid,
            'iface': _s(info.get('iface')),
            'info_uid': _s(info.get('uid')),
            'object_class': oclass,
            'kind': classify_wan_kind(uid, device),
            'is_overlay': oclass in (OBJ_SDWAN,) or _s(info.get('type')).lower() in _OVERLAY_TYPES,
            'display_name': display,
            'connection_state': _s(status.get('connection_state')) or 'unknown',
            'ip_address': ipinfo.get('ip_address') or None,
            'underlay_uid': underlay_uid or None,
            'cm_priority': cm_priority if cm_priority is not None else 999,
            'original_priority': config.get('original_priority',
                                            status.get('original_priority')),
            'parent_priority': config.get('parent_priority',
                                          status.get('parent_priority')),
            'is_primary': (uid == primary_uid),
        })
    records.sort(key=lambda r: (r.get('cm_priority') if isinstance(
        r.get('cm_priority'), (int, float)) else 999))
    return records


def sdwan_overlay(raw_sdwan):
    """Normalize status/wan/sdwan (hub/spoke).

    STA v1.1.4 does NOT use this for membership; links[] inner shape is
    still device-dependent, so it is carried as raw.
    """
    raw = raw_sdwan if isinstance(raw_sdwan, dict) else {}
    return {
        'connected_hub': raw.get('connected_hub', 'None'),
        'link_count': len(raw.get('links', {}) or {}),
        'links_raw': raw.get('links', {}),   # device-dependent, passthrough
    }


def sdwan_underlay_uid(device):
    """Return the dependent (underlay) WAN device key for an SD-WAN device.

    STA-validated (_sdwan_underlay_uid): prefer status.active_dep_wandev,
    then the first entry of status.dep_wandevs (list or dict). '' if none.
    """
    device = device if isinstance(device, dict) else {}
    status = device.get('status') if isinstance(device.get('status'), dict) else {}

    active_dep = _s(status.get('active_dep_wandev'))
    if active_dep:
        return active_dep

    dep = status.get('dep_wandevs')
    if isinstance(dep, list):
        for d in dep:
            d = _s(d)
            if d:
                return d
    elif isinstance(dep, dict):
        for d in dep:
            d = _s(d)
            if d:
                return d
    return ''


def wbond_member_uids(raw_wbond_interface):
    """Extract WBOND member WAN device keys from a wan_bonding interface obj.

    Authoritative source (STA-validated, _wbond_member_uids):
    status/sdwan_adv/wan_bonding/interfaces/{iface} -> members_list, where
    each entry's 'intf name' is the underlying WAN device key. members_list
    may be a list of entries OR a dict keyed by member id. De-duplicated in
    exposed order. This is NOT dep_wandevs/active_dep_wandev and NOT
    'profile name'.
    """
    obj = raw_wbond_interface if isinstance(raw_wbond_interface, dict) else {}
    members = obj.get('members_list')
    if isinstance(members, list):
        entries = members
    elif isinstance(members, dict):
        entries = list(members.values())
    else:
        return []

    uids = []
    for entry in entries:
        if isinstance(entry, dict):
            val = _s(entry.get('intf name'))
            if val and val not in uids:
                uids.append(val)
    return uids


def wbond(raw_wbond_interface):
    """Normalize one WBOND interface object into a minimal WbondRecord.

    Kept for back-compat (service layer still calls it for member UID
    enumeration). Richer bond-level/member-level runtime normalization is in
    wbond_detail().
    """
    obj = raw_wbond_interface if isinstance(raw_wbond_interface, dict) else {}
    member_uids = wbond_member_uids(obj)
    return {
        'present': bool(member_uids),
        'member_uids': member_uids,       # underlying WAN device keys
        'member_count': len(member_uids),
    }


# --- Numeric coercion helper ---------------------------------------------

def _num(value):
    """Coerce to int/float when possible, else None. Never raises."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).strip()
        if text == '':
            return None
        if '.' in text or 'e' in text.lower():
            return float(text)
        return int(text)
    except (TypeError, ValueError):
        return None


def _first_present(d, keys, default=None):
    """Return the first key present (non-None) in dict d, else default."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d.get(k) is not None:
            return d.get(k)
    return default


def _as_entries(container):
    """Normalize a list-or-dict container into a list of (key, value)."""
    if isinstance(container, list):
        return [(str(i), v) for i, v in enumerate(container)]
    if isinstance(container, dict):
        return list(container.items())
    return []


def steering(raw_steering):
    """Normalize status/wan/steering summary.

    IMPORTANT: intent_count/empty intents_raw do NOT mean Traffic Classes
    don't exist -- authoritative class definitions live in the config tree
    (see traffic_classes()). 'active' therefore reflects only that runtime
    steering rules are present.
    """
    raw = raw_steering if isinstance(raw_steering, dict) else {}
    rules = raw.get('rules', []) or []
    intents = raw.get('intents', {}) or {}
    return {
        'active': bool(rules),
        'rule_count': len(rules) if isinstance(rules, (list, dict)) else 0,
        'intent_count': len(intents) if isinstance(intents, (list, dict)) else 0,
        'rules_raw': rules,       # compiled runtime rules, passthrough
        'intents_raw': intents,   # runtime intents (may be sparse)
    }


# =========================================================================
# SWANS telemetry (Traffic Class path measurement)
# =========================================================================

def swans_paths(raw_swans):
    """Normalize status/wan/swans into per-path latency/jitter records.

    SWANS is the Traffic Class path measurement. Keep separate from SD-WAN
    QoE even when values look similar. Structure varies across firmware, so
    we probe common shapes and keep the raw sub-object per path. Fields whose
    semantics are not proven are preserved raw, not relabeled.

    Returns {'paths': [ ... ], 'history_summary': {...}, 'raw': <raw>}.
    """
    raw = raw_swans if isinstance(raw_swans, dict) else {}

    # Real R1900 shape:
    #   status/wan/swans = {
    #     'cm': {...}, 'dataused': {...},
    #     'history': [ {'epoch': N,
    #                   'upd_status': [ {'wandev': 'sdwan-hub0_wan',
    #                                    'criteria': {'Latency': 9.48,
    #                                                 'Jitter': 1.79}}, ... ]},
    #                  ... ]
    #   }
    # 'cm' and 'dataused' are NOT WAN path records and are ignored here.
    history = raw.get('history')
    history = history if isinstance(history, list) else []

    # Latest measurement per wandev = the entry with the highest epoch that
    # contains that wandev. Also build a bounded normalized history for
    # future graphing.
    latest = {}          # wandev -> {latency_ms, jitter_ms, epoch}
    norm_history = []     # bounded, oldest-first
    for entry in history:
        if not isinstance(entry, dict):
            continue
        epoch = _num(entry.get('epoch'))
        samples = []
        for upd in (entry.get('upd_status') or []):
            if not isinstance(upd, dict):
                continue
            wandev = _s(upd.get('wandev'))
            if not wandev:
                continue
            crit = upd.get('criteria') if isinstance(
                upd.get('criteria'), dict) else {}
            lat = _num(crit.get('Latency'))
            jit = _num(crit.get('Jitter'))
            samples.append({'uid': wandev, 'latency_ms': lat,
                            'jitter_ms': jit})
            prev = latest.get(wandev)
            if (prev is None
                    or not isinstance(prev.get('epoch'), (int, float))
                    or (isinstance(epoch, (int, float))
                        and epoch >= prev['epoch'])):
                latest[wandev] = {'latency_ms': lat, 'jitter_ms': jit,
                                  'epoch': epoch}
        if samples:
            norm_history.append({'epoch': epoch, 'samples': samples})

    # Bound the normalized history so it cannot grow unbounded in memory.
    _HISTORY_CAP = 50
    if len(norm_history) > _HISTORY_CAP:
        norm_history = norm_history[-_HISTORY_CAP:]

    paths = []
    for wandev in sorted(latest.keys()):
        m = latest[wandev]
        paths.append({
            'uid': wandev,
            'latency_ms': m.get('latency_ms'),
            'jitter_ms': m.get('jitter_ms'),
            'epoch': m.get('epoch'),
        })

    return {
        'paths': paths,
        'history': norm_history,
        'history_summary': {'path_count': len(paths),
                            'history_len': len(norm_history)},
    }


# =========================================================================
# SD-WAN QoE (transport quality measurement) -- distinct from SWANS
# =========================================================================

def qoe_paths(raw_qoe):
    """Normalize status/sdwan_adv/qoe into per-path transport-quality records.

    QoE is the SD-WAN transport measurement (NOT SWANS). Many field
    units/semantics are unproven, so obvious metrics (latency samples) are
    normalized only where clear; everything else is preserved raw with its
    original name/value and units only when actually supplied.
    """
    raw = raw_qoe if isinstance(raw_qoe, dict) else {}
    paths = []

    # Real R1900 shape: status/sdwan_adv/qoe = {'interface': {<uid>: {...,
    #   'qoe-latency-stats': {'pkts-intf-qoe-latency-last-sample': 8,
    #                         'pkts-intf-qoe-latency-average': 7},
    #   'pkt-loss-stats': {...}}}}. Note SINGULAR 'interface'. The uid here is
    # the overlay info.uid (e.g. 'hub0_wan'), not the WAN dictionary key.
    container = raw.get('interface')
    if not isinstance(container, (dict, list)):
        # Tolerant fallbacks for other shapes.
        for key in ('interfaces', 'paths', 'devices', 'links'):
            if isinstance(raw.get(key), (dict, list)):
                container = raw.get(key)
                break
    if not isinstance(container, (dict, list)):
        container = {k: v for k, v in raw.items() if isinstance(v, dict)}

    for key, obj in _as_entries(container):
        if not isinstance(obj, dict):
            continue
        lat_stats = obj.get('qoe-latency-stats')
        lat_stats = lat_stats if isinstance(lat_stats, dict) else {}
        lat_avg = _num(_first_present(
            lat_stats, ('pkts-intf-qoe-latency-average',)))
        lat_last = _num(_first_present(
            lat_stats, ('pkts-intf-qoe-latency-last-sample',)))
        # Legacy fallbacks if the stats sub-object is absent.
        if lat_avg is None:
            lat_avg = _num(_first_present(
                obj, ('latency_avg', 'avg_latency')))
        if lat_last is None:
            lat_last = _num(_first_present(
                obj, ('latency_last', 'last_latency')))
        paths.append({
            'uid': str(key),
            'latency_avg_ms': lat_avg,
            'latency_last_ms': lat_last,
            # pkt-loss-stats semantics/units unproven -> keep raw only.
            'pkt_loss_stats_raw': obj.get('pkt-loss-stats'),
        })

    return {'paths': paths}


# =========================================================================
# WAN Connection Manager config correlation (config/wan/rules2, read-only)
# =========================================================================

def wan_rules2_index(raw_rules2):
    """Index config/wan/rules2 by WAN profile identity for correlation.

    Returns a dict keyed by a best-effort profile key (trigger_string or
    _id_ or name) -> normalized config-priority record. Read-only. All four
    priority domains are kept separate and named.
    """
    out = {}
    for key, rule in _as_entries(raw_rules2):
        if not isinstance(rule, dict):
            continue
        profile_key = (_s(rule.get('trigger_string'))
                       or _s(rule.get('_id_'))
                       or _s(rule.get('name'))
                       or str(key))
        out[profile_key] = {
            'id': _s(rule.get('_id_')) or None,
            'name': _s(rule.get('name')) or None,
            'trigger_name': _s(rule.get('trigger_name')) or None,
            'trigger_string': _s(rule.get('trigger_string')) or None,
            'rule_indexes': rule.get('rule_indexes'),
            'cm_priority': _num(rule.get('priority')),
            'original_priority': _num(rule.get('original_priority')),
            'parent_priority': _num(rule.get('parent_priority')),
            'bandwidth_ingress': _num(_first_present(
                rule, ('bandwidth_ingress', 'ingress_bandwidth'))),
            'bandwidth_egress': _num(_first_present(
                rule, ('bandwidth_egress', 'egress_bandwidth'))),
            'raw': rule,
        }
    return out


# =========================================================================
# Destination identity resolution (config/identities/ip)
# =========================================================================

def destination_index(raw_identities_ip):
    """Build a destination-UUID -> identity map from config/identities/ip.

    Maps each destination UUID to friendly_name + internal application/
    category name/id. Does NOT hardcode a*/c* prefix meaning -- the internal
    name and any type field are preserved raw so the caller can classify
    using actual data (or steering event appname/catname).

    Returns {uuid: {'uuid','friendly_name','internal_name','type','raw'}}.
    """
    out = {}
    for key, obj in _as_entries(raw_identities_ip):
        if not isinstance(obj, dict):
            # Some trees key by UUID with the UUID also inside; handle scalars
            continue
        uuid = (_s(obj.get('_id_')) or _s(obj.get('uuid')) or _s(obj.get('id'))
                or str(key))
        friendly = (_s(obj.get('friendly_name')) or _s(obj.get('name'))
                    or _s(obj.get('display_name')))
        internal = (_s(obj.get('internal_name')) or _s(obj.get('app'))
                    or _s(obj.get('appid')) or _s(obj.get('id')))
        dtype = _s(obj.get('type')) or None
        out[uuid] = {
            'uuid': uuid,
            'friendly_name': friendly or None,
            'internal_name': internal or None,
            'type': dtype,
            'raw': obj,
        }
    return out


def resolve_destinations(dest_uuids, dest_index):
    """Resolve a list of destination UUIDs against destination_index output.

    Preserves raw UUID + internal name; classifies type only when the
    identity supplies it (never inferred from prefix). Unknown UUIDs are
    kept with friendly_name None so nothing is silently dropped.
    """
    dest_index = dest_index if isinstance(dest_index, dict) else {}
    resolved = []
    for uuid in (dest_uuids or []):
        u = _s(uuid)
        if not u:
            continue
        ident = dest_index.get(u)
        if isinstance(ident, dict):
            resolved.append({
                'uuid': u,
                'friendly_name': ident.get('friendly_name'),
                'internal_name': ident.get('internal_name'),
                'type': ident.get('type'),
            })
        else:
            resolved.append({
                'uuid': u,
                'friendly_name': None,
                'internal_name': None,
                'type': None,
            })
    return resolved


# =========================================================================
# Traffic Classes / intents (config-driven, authoritative)
# =========================================================================

# A criterion sub-record: enabled/threshold/hysteresis/priority + targets.
def _empty_criterion():
    return {'enabled': None, 'threshold': None, 'hysteresis': None,
            'priority': None, 'target4': None, 'target6': None}


# Map the real criterion "criterion" values to normalized slots.
_CRITERION_SLOTS = {
    'latency': 'latency',
    'jitter': 'jitter',
    'ss': 'signal_strength',
    'signal': 'signal_strength',
    'signal strength': 'signal_strength',
}


def _criterion_record(c):
    """Normalize one entry of identities/intent criteria[]."""
    return {
        'enabled': c.get('enabled'),
        'threshold': _num(c.get('threshold')),
        'hysteresis': _num(c.get('hysteresis')),
        'priority': _num(c.get('priority')),
        'target4': _s(c.get('target4')) or None,
        'target6': _s(c.get('target6')) or None,
    }


def traffic_classes(raw_intent_cfg, raw_traffic_class_cfg):
    """Normalize NCX Traffic Classes from the real config trees.

    Real shapes (R1900 NCOS 7.26.41):
      config/identities/intent entry:
        {_id_, name, accept_age, loadbalance_algo,
         traffic_class: <tc uuid>,
         criteria: [{criterion:'Latency'|'Jitter'|'SS'|..., enabled,
                     threshold, hysteresis, priority, target4/target6}]}
      config/sdwan_adv/traffic_class entry:
        {_id_, name, dscp, forward_error_correction, reset_existing_flow,
         wan_bonding: {algo: 'flow_balance'}}

    Correlation: intent._id_ (intent_id) -> intent.traffic_class ->
    traffic_class config._id_. Only intents that carry a `traffic_class`
    field are part of the NCX Traffic Class view; older identity intents
    without it (e.g. legacy Real-Time/Voice, Video, File Transfer) are NOT
    merged in -- they are skipped from this normalized view.

    Values are collected dynamically; nothing is hardcoded. Returns a list of
    class records, each keeping its raw source objects.
    """
    # Index traffic_class config by _id_ for correlation.
    tc_by_id = {}
    for _k, obj in _as_entries(raw_traffic_class_cfg):
        if isinstance(obj, dict):
            tcid = _s(obj.get('_id_')) or _s(obj.get('id'))
            if tcid:
                tc_by_id[tcid] = obj

    out = []
    for _k, intent in _as_entries(raw_intent_cfg):
        if not isinstance(intent, dict):
            continue
        tc_id = _s(intent.get('traffic_class'))
        if not tc_id:
            # Not an NCX Traffic Class (legacy identity intent) -> skip.
            continue

        intent_id = _s(intent.get('_id_')) or _s(intent.get('id')) or None
        tc_cfg = tc_by_id.get(tc_id, {})

        # Parse criteria[] into named slots + collect any others generically.
        latency = _empty_criterion()
        jitter = _empty_criterion()
        signal = _empty_criterion()
        other = []
        for c in (intent.get('criteria') or []):
            if not isinstance(c, dict):
                continue
            slot = _CRITERION_SLOTS.get(_s(c.get('criterion')).lower())
            rec = _criterion_record(c)
            if slot == 'latency':
                latency = rec
            elif slot == 'jitter':
                jitter = rec
            elif slot == 'signal_strength':
                signal = rec
            else:
                entry = dict(rec)
                entry['criterion'] = _s(c.get('criterion')) or None
                other.append(entry)

        wb = tc_cfg.get('wan_bonding')
        wb_algo = _s(wb.get('algo')) if isinstance(wb, dict) else None

        out.append({
            'intent_id': intent_id,
            'traffic_class_id': tc_id,
            'name': _s(intent.get('name')) or _s(tc_cfg.get('name')) or None,
            'accept_age': _num(intent.get('accept_age')),
            'latency': latency,
            'jitter': jitter,
            'signal_strength': signal,
            'other_criteria': other,
            'loadbalance_algo': _s(intent.get('loadbalance_algo')) or None,
            'dscp': _num(tc_cfg.get('dscp')),
            'forward_error_correction':
                tc_cfg.get('forward_error_correction'),
            'reset_existing_flow': tc_cfg.get('reset_existing_flow'),
            'wan_bonding_algorithm': wb_algo or None,
            'raw': {'intent': intent, 'traffic_class': tc_cfg or None},
        })
    return out


def traffic_class_fingerprint_payload(tc):
    """Semantic payload used to fingerprint one Traffic Class.

    Includes the values whose change should count as a config change --
    criteria thresholds/enabled, loadbalance_algo, FEC, WAN bonding algo,
    DSCP -- so a threshold-only edit (e.g. 1000/600 -> 500/200) changes it.
    """
    tc = tc if isinstance(tc, dict) else {}

    def crit(c):
        c = c if isinstance(c, dict) else {}
        return [c.get('enabled'), c.get('threshold'), c.get('hysteresis'),
                c.get('priority')]

    return {
        'intent_id': _s(tc.get('intent_id')),
        'traffic_class_id': _s(tc.get('traffic_class_id')),
        'name': _s(tc.get('name')),
        'latency': crit(tc.get('latency')),
        'jitter': crit(tc.get('jitter')),
        'signal_strength': crit(tc.get('signal_strength')),
        'other': sorted(
            '{}:{}:{}'.format(_s(o.get('criterion')), o.get('enabled'),
                              o.get('threshold'))
            for o in (tc.get('other_criteria') or [])),
        'loadbalance_algo': _s(tc.get('loadbalance_algo')),
        'fec': tc.get('forward_error_correction'),
        'wan_bonding_algorithm': _s(tc.get('wan_bonding_algorithm')),
        'dscp': tc.get('dscp'),
    }


# =========================================================================
# Loadbalancers (status/wan/loadbalancers) -- HEAVY, slow cadence
# =========================================================================

def loadbalancers(raw_loadbalancers):
    """Normalize status/wan/loadbalancers.

    Captures all object types generically (rate-*, spillover-*, and any
    others). Normalizes the known fields; preserves the raw object for
    unknown/algorithm-specific fields. Not every algorithm uses every field.
    """
    out = []
    for key, obj in _as_entries(raw_loadbalancers):
        if not isinstance(obj, dict):
            continue
        algo = _s(_first_present(obj, ('type', 'algo', 'algorithm')))
        if not algo:
            # Infer from the object key prefix (rate-*, spillover-*).
            algo = str(key).split('-', 1)[0]
        members = []
        for mkey, m in _as_entries(_first_present(
                obj, ('devices', 'members', 'wans'), {})):
            if isinstance(m, dict):
                # Per-device runtime fields (algorithm-specific; may or may
                # not all be present). These often live at the DEVICE level,
                # not the top level, so read them here too.
                members.append({
                    'uid': _s(m.get('uid')) or _s(m.get('device')) or str(mkey),
                    'score': _num(m.get('score')),
                    'ingress_max': _num(m.get('ingress_max')),
                    'egress_max': _num(m.get('egress_max')),
                    'ingress_current': _num(m.get('ingress_current')),
                    'egress_current': _num(m.get('egress_current')),
                    'ingress_adj': _num(m.get('ingress_adj')),
                    'ingress_run': _num(m.get('ingress_run')),
                    'active': m.get('active'),
                    'raw': m,   # preserve unknown/algorithm-specific fields
                })
            else:
                members.append({'uid': str(m), 'score': None,
                                'active': None, 'raw': m})
        out.append({
            'name': str(key),
            'algorithm': algo or None,
            'active_device': _s(_first_present(
                obj, ('active_device', 'active'))) or None,
            'aggregate_max': _num(_first_present(
                obj, ('aggregate_max', 'aggregate', 'max'))),
            # Top-level runtime fields when present (do not assume location).
            'ingress_max': _num(obj.get('ingress_max')),
            'egress_max': _num(obj.get('egress_max')),
            'ingress_current': _num(obj.get('ingress_current')),
            'egress_current': _num(obj.get('egress_current')),
            'ingress_adj': _num(obj.get('ingress_adj')),
            'ingress_run': _num(obj.get('ingress_run')),
            'score': _num(obj.get('score')),
            'members': members,
            'raw': obj,   # preserve unknown fields without inventing units
        })
    return out


# =========================================================================
# WAN bonding detailed runtime (status/sdwan_adv/wan_bonding)
# =========================================================================

def wan_bonding_detail(raw_wan_bonding):
    """Normalize status/sdwan_adv/wan_bonding into bond + member records.

    Real R1900 shape:
      {
        'supported_capabilities': [...],
        'interfaces': {
          'gres_wb0-hub0': {
            'negotiated_capabilities': [...], 'gre key': 16448,
            'UL total flows': 13, 'DL total flows': 29, 'link state': 'UP',
            'mtu': '1248', 'Total interfaces': 2, 'Bonding Mode': 'GRE',
            'Priority': 0,
            'members_list': {
              'gres-hub0_wan': {   # tunnel iface key
                'profile name': 'Ethernet', 'intf name': 'ethernet-wan',
                'config_weight': 80, 'DL current flows': 24,
                'UL current flows': 11, 'us_weight': 80, 'ds_weight': 75,
                'us_curr_bw': ..., 'ds_curr_bw': ..., 'ds_bw_trend': ...,
                'us_bw_trend': ..., 'ds_last_bw_sample': ...,
                'us_last_bw_sample': ..., 'has_loss': 0, 'link state': 'UP',
                'link attach state': 'ATTACHED', 'gre_key': 16384,
                'mtu': 1312 }, ... } } } }

    Directional mapping (validated): ds_weight -> current_download_weight,
    us_weight -> current_upload_weight; DL/UL current flows are the member
    flow counts. Configured weight (config_weight) vs runtime directional
    weight vs observed flow share are kept SEPARATE. Bandwidth values are
    preserved RAW (units not proven). Bond DL/UL totals use the reported
    fields; the summed member flows are kept as a validation field.

    Returns {'bonds': [...], 'supported_capabilities': [...]}. Member
    friendly names are resolved by the service layer (needs the devices map).
    """
    raw = raw_wan_bonding if isinstance(raw_wan_bonding, dict) else {}
    supported = _cap_list(raw.get('supported_capabilities'))
    bonds = []

    container = raw.get('interfaces')
    if not isinstance(container, (dict, list)):
        container = {k: v for k, v in raw.items()
                     if isinstance(v, dict) and k not in (
                         'supported_capabilities',)}

    for key, obj in _as_entries(container):
        if not isinstance(obj, dict):
            continue
        members = _normalize_bond_members(obj)
        summed_dl = sum(m['download_flows'] for m in members
                        if isinstance(m.get('download_flows'), (int, float)))
        summed_ul = sum(m['upload_flows'] for m in members
                        if isinstance(m.get('upload_flows'), (int, float)))
        # Prefer the reported bond-level totals; fall back to the sum.
        reported_dl = _num(_first_present(
            obj, ('DL total flows', 'total_download_flows')))
        reported_ul = _num(_first_present(
            obj, ('UL total flows', 'total_upload_flows')))
        bonds.append({
            'present': True,
            'iface': str(key),
            'name': _s(_first_present(obj, ('name', 'identifier'))) or None,
            'bonding_mode': _s(_first_present(
                obj, ('Bonding Mode', 'mode', 'bond_mode'))) or None,
            'link_state': _s(_first_present(
                obj, ('link state', 'state', 'status'))) or None,
            'member_count': _num(_first_present(
                obj, ('Total interfaces',))) or len(members),
            'total_download_flows': (reported_dl if reported_dl is not None
                                     else (summed_dl if members else None)),
            'total_upload_flows': (reported_ul if reported_ul is not None
                                   else (summed_ul if members else None)),
            'computed_download_flows': summed_dl if members else None,
            'computed_upload_flows': summed_ul if members else None,
            'negotiated_capabilities': _cap_list(
                obj.get('negotiated_capabilities')),
            'supported_capabilities': supported,
            'gre_key': _num(_first_present(obj, ('gre key', 'gre_key'))),
            'mtu': _num(obj.get('mtu')),
            'priority': _num(_first_present(obj, ('Priority', 'priority'))),
            'members': members,
            'raw': obj,
        })
    return {'bonds': bonds, 'supported_capabilities': supported}


def _cap_list(caps):
    """Normalize a capabilities value (list or dict of flags) to [str]."""
    if isinstance(caps, list):
        return [str(c) for c in caps]
    if isinstance(caps, dict):
        return [str(k) for k, v in caps.items() if v]
    if caps is not None and str(caps).strip():
        return [str(caps)]
    return []


def _normalize_bond_members(bond_obj):
    """Normalize a bond's members_list into member records (real shape).

    members_list is a DICT keyed by the member's TUNNEL iface (e.g.
    'gres-hub0_wan'); the underlying WAN device key is the member's
    'intf name' (e.g. 'ethernet-wan'). Tolerant of a list shape too.
    """
    members_container = _first_present(
        bond_obj, ('members_list', 'members', 'member'))
    members = []
    for key, m in _as_entries(members_container):
        if not isinstance(m, dict):
            continue
        member_uid = _s(_first_present(m, ('intf name', 'intf_name'))) \
            or _s(_first_present(m, ('uid', 'device'))) or str(key)
        members.append({
            'member_uid': member_uid,
            'tunnel_iface': str(key),   # members_list dict key
            'profile_name': _s(_first_present(
                m, ('profile name', 'profile_name'))) or None,
            'configured_weight': _num(_first_present(
                m, ('config_weight', 'configured_weight', 'weight'))),
            # ds_weight = downstream/download; us_weight = upstream/upload.
            'current_download_weight': _num(_first_present(
                m, ('ds_weight', 'current_download_weight'))),
            'current_upload_weight': _num(_first_present(
                m, ('us_weight', 'current_upload_weight'))),
            'download_flows': _num(_first_present(
                m, ('DL current flows', 'download_flows'))),
            'upload_flows': _num(_first_present(
                m, ('UL current flows', 'upload_flows'))),
            # Bandwidth values RAW (units not proven).
            'ds_curr_bw': _num(m.get('ds_curr_bw')),
            'us_curr_bw': _num(m.get('us_curr_bw')),
            'ds_bw_trend': _num(m.get('ds_bw_trend')),
            'us_bw_trend': _num(m.get('us_bw_trend')),
            'ds_last_bw_sample': _num(m.get('ds_last_bw_sample')),
            'us_last_bw_sample': _num(m.get('us_last_bw_sample')),
            'has_loss': _num(_first_present(m, ('has_loss', 'loss',
                                                'packet_loss'))),
            'link_state': _s(_first_present(
                m, ('link state', 'link_state'))) or None,
            'link_attach_state': _s(_first_present(
                m, ('link attach state', 'link_attach_state'))) or None,
            'gre_key': _num(_first_present(m, ('gre_key', 'gre key'))),
            'mtu': _num(m.get('mtu')),
            'raw': m,
        })
    return members


# =========================================================================
# Steering events / stats (status/wan/steering/{events,stats})
# =========================================================================

def steering_events(raw_events):
    """Normalize status/wan/steering/events.

    Event reason is stored EXACTLY as received (no interpretation). Keeps
    began_capture/total_perf_events/event_overflow plus each event's fields.
    """
    raw = raw_events if isinstance(raw_events, dict) else {}
    events = []
    for ev in (raw.get('steering_events') or []):
        if not isinstance(ev, dict):
            continue
        events.append({
            'epoch': _num(ev.get('epoch')),
            'steering_rule': _s(ev.get('steering_rule')) or None,
            'reason': ev.get('reason'),   # stored verbatim
            'measure_units': _s(ev.get('measure_units')) or None,
            'measure_from': ev.get('measure_from'),
            'measure_to': ev.get('measure_to'),
            'wan_from': _s(ev.get('wan_from')) or None,
            'wan_to': _s(ev.get('wan_to')) or None,
            'wan_from_uid': _s(ev.get('wan_from_uid')) or None,
            'wan_to_uid': _s(ev.get('wan_to_uid')) or None,
            'total_flows_moved': _num(ev.get('total_flows_moved')),
            'destinations': ev.get('destinations') or [],
            'src_lans': ev.get('src_lans') or [],
            'raw': ev,
        })
    return {
        'began_capture': _num(raw.get('began_capture')),
        'total_perf_events': _num(raw.get('total_perf_events')),
        'event_overflow': raw.get('event_overflow'),
        'events': events,
    }


def steering_stats(raw_stats):
    """Normalize status/wan/steering/stats.

    preferred/nonpreferred flows are a CURRENT/AGING population, not lifetime
    totals -- named plainly, never 'total since boot'. Per-app/category
    breakdowns preserved.
    """
    raw = raw_stats if isinstance(raw_stats, dict) else {}
    rules = []
    for r in (raw.get('rules') or []):
        if not isinstance(r, dict):
            continue
        # stats.apps is a DICT keyed by internal app/category id on the real
        # router (may also be a list on other shapes). Preserve the internal
        # key as supplemental metadata. Never infer destination type from it.
        apps = []
        for app_key, a in _as_entries(r.get('apps')):
            if not isinstance(a, dict):
                continue
            apps.append({
                'internal_id': app_key,   # e.g. 'a000015A8' (supplemental)
                'appname': _s(a.get('appname')) or None,
                'catname': _s(a.get('catname')) or None,
                'preferred_flows': _num(a.get('preferred_flows')),
                'nonpreferred_flows': _num(a.get('nonpreferred_flows')),
                'was_flow_matched': a.get('was_flow_matched'),
                'raw': a,
            })
        rules.append({
            'steering_rule': _s(r.get('steering_rule')) or None,
            'total_perf_events': _num(r.get('total_perf_events')),
            'preferred_wan': _s(r.get('preferred_wan')) or None,
            'preferred_flows': _num(r.get('preferred_flows')),
            'nonpreferred_flows': _num(r.get('nonpreferred_flows')),
            'was_flow_matched': r.get('was_flow_matched'),
            'apps': apps,
            'raw': r,
        })
    return {
        'began_capture': _num(raw.get('began_capture')),
        'flows_truncated': raw.get('flows_truncated'),
        'rules': rules,
    }


# =========================================================================
# WAN selector classification (from compiled trigger structure)
# =========================================================================

# WAN selector semantic types. Never inferred beyond what the raw trigger
# structure supports.
SEL_ALL_SDWAN = 'all_sdwan'
SEL_BONDED = 'bonded'
SEL_EXPLICIT_PRIORITY = 'explicit_priority'
SEL_SPECIFIC = 'specific'
SEL_UNKNOWN = 'unknown'


def _trigger_strings(rule):
    """Collect trigger strings from a compiled rule in a shape-tolerant way.

    Compiled rules expose triggers as 'trigger_strings' (list) and/or the
    scalar fields trigger_field/trigger_predicate/trigger_value. Return a
    lower-cased list of 'field|predicate|value' style tokens.
    """
    # Deprecated shim retained for callers; real parsing uses
    # _trigger_entries() over the nested xpolicy (see below).
    return _trigger_string_tokens(rule)


def _xpolicy(rule):
    """Return a rule's nested compiled-policy object ('xpolicy'), or {}."""
    rule = rule if isinstance(rule, dict) else {}
    xp = rule.get('xpolicy')
    return xp if isinstance(xp, dict) else {}


def _trigger_entries(rule):
    """Extract compiled trigger entries from xpolicy.trigger_strings.

    Real shape: a LIST of DICTS, each like
        {'_id_': ..., 'trigger_priority': 20,
         'trigger_string': 'type|is|sdwan%subtype|is|mdm%...'}
    Returns a list of {'trigger_string': str(lower), 'trigger_priority': num,
    'id': str}. Tolerant of a bare-string list or scalar fallbacks so older
    shapes still parse. NEVER str(dict).
    """
    xp = _xpolicy(rule)
    entries = []
    ts = xp.get('trigger_strings')
    if isinstance(ts, list):
        for t in ts:
            if isinstance(t, dict):
                s = _s(t.get('trigger_string')).lower()
                if s:
                    entries.append({
                        'trigger_string': s,
                        'trigger_priority': _num(t.get('trigger_priority')),
                        'id': _s(t.get('_id_')) or None,
                    })
            else:
                s = _s(t).lower()
                if s:
                    entries.append({'trigger_string': s,
                                    'trigger_priority': None, 'id': None})
    if not entries:
        field = _s(xp.get('trigger_field')).lower()
        pred = _s(xp.get('trigger_predicate')).lower()
        val = _s(xp.get('trigger_value')).lower()
        if field or pred or val:
            entries.append({
                'trigger_string': '{}|{}|{}'.format(field, pred, val),
                'trigger_priority': _num(xp.get('trigger_priority')),
                'id': None,
            })
    return entries


def _trigger_string_tokens(rule):
    """Lower-cased trigger_string values from the compiled xpolicy."""
    return [e['trigger_string'] for e in _trigger_entries(rule)
            if e.get('trigger_string')]


def _dev_ids(devs):
    """Normalize matched_devs / steering_to into a list of WAN device IDs.

    Real shape is a DICT keyed by WAN device ID (values are member metadata).
    Tolerant of a list of ids or list of {uid/device/...} dicts.
    """
    if isinstance(devs, dict):
        return sorted(_s(k) for k in devs.keys() if _s(k))
    if isinstance(devs, list):
        out = []
        for d in devs:
            if isinstance(d, dict):
                did = _s(_first_present(d, ('uid', 'device', 'name', '_id_')))
            else:
                did = _s(d)
            if did and did not in out:
                out.append(did)
        return out
    return []


def _dev_members(devs):
    """Return {wan_id: member_metadata_dict} for matched_devs / steering_to.

    Preserves member metadata (e.g. explicit priority) when present.
    """
    out = {}
    if isinstance(devs, dict):
        for k, v in devs.items():
            kid = _s(k)
            if kid:
                out[kid] = v if isinstance(v, dict) else {}
    elif isinstance(devs, list):
        for d in devs:
            if isinstance(d, dict):
                did = _s(_first_present(d, ('uid', 'device', 'name', '_id_')))
                if did:
                    out[did] = d
            else:
                did = _s(d)
                if did:
                    out[did] = {}
    return out


def classify_wan_selector(rule):
    """Classify a compiled steering rule's WAN selector semantics.

    Reads triggers from the nested xpolicy (validated). Returns one of SEL_*:
      - xpolicy wbond trigger (type|is|wbond)      -> bonded
      - xpolicy.wan_priority_enabled true          -> explicit_priority
      - xpolicy sdwan trigger with >=2 matched
        overlays and no explicit priority          -> all_sdwan
      - a single specific matched dev              -> specific
      - otherwise                                  -> unknown
    """
    rule = rule if isinstance(rule, dict) else {}
    xp = _xpolicy(rule)
    tokens = _trigger_string_tokens(rule)

    # Explicit WAN priority takes precedence: such a rule can list several
    # trigger_strings (including a wbond one) each with its own priority, so
    # it must be classified BEFORE the single-wbond bonded check.
    if xp.get('wan_priority_enabled') is True:
        return SEL_EXPLICIT_PRIORITY

    # Bonded WAN: an xpolicy trigger references wbond (single-selector rule).
    for t in tokens:
        if '|wbond' in t or 'is|wbond' in t or t.endswith('wbond'):
            return SEL_BONDED
    if _s(xp.get('trigger_value')).lower() == 'wbond':
        return SEL_BONDED

    matched_ids = _dev_ids(rule.get('matched_devs'))
    sdwan_matched = [d for d in matched_ids
                     if _s(d).lower().startswith('sdwan')]
    is_sdwan_trigger = any('|sdwan' in t or t.startswith('type|is|sdwan')
                           for t in tokens)
    if is_sdwan_trigger or len(sdwan_matched) >= 2:
        if len(matched_ids) >= 2:
            return SEL_ALL_SDWAN
        if len(matched_ids) == 1:
            return SEL_SPECIFIC
        return SEL_ALL_SDWAN

    if len(matched_ids) == 1:
        return SEL_SPECIFIC
    return SEL_UNKNOWN


# =========================================================================
# Logical rule name derivation (strip compiled UUID suffix)
# =========================================================================

# Compiled children often suffix the logical name with a UUID or index, e.g.
# 'YouTube-3f2a9c...' or 'Office 365 (2)'. Strip trailing UUID/hex/index so
# the logical name is stable across recompilation.
_UUID_SUFFIX_RE = re.compile(
    r'[-_\s]*[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE)
_HEX_SUFFIX_RE = re.compile(r'[-_\s]+[0-9a-f]{6,}$', re.IGNORECASE)
_PAREN_INDEX_RE = re.compile(r'\s*\(\d+\)\s*$')


def logical_rule_name(compiled_name):
    """Derive a stable logical rule name by stripping compiled suffixes.

    Removes a trailing UUID, long hex token, or (n) index. Leaves the core
    NCM rule name (e.g. 'LAB01-Best Effort', 'YouTube', 'Office 365').
    """
    name = _s(compiled_name)
    if not name:
        return ''
    name = _UUID_SUFFIX_RE.sub('', name)
    name = _PAREN_INDEX_RE.sub('', name)
    name = _HEX_SUFFIX_RE.sub('', name)
    return name.strip()


def _rule_field(rule, keys, default=None):
    return _first_present(rule, keys, default)


def logical_rules(raw_steering_rules, raw_steering_stats=None,
                  raw_steering_events=None, dest_index=None):
    """Group compiled runtime steering rules into normalized logical rules.

    Real compiled rules have TWO layers -- a top-level runtime object and a
    nested 'xpolicy' compiled-policy object (validated on the R1900). Each
    field is read from its authoritative layer:

      RUNTIME (top level): loadbalance_algo, matched_devs (DICT),
        steering_to (DICT), primary_wandev, failover, intent (friendly
        Traffic Class NAME).
      COMPILED (xpolicy): trigger_priority, trigger_strings, src_lans,
        wan_priority_enabled, intent (Traffic Class UUID), dst_ip_network
        (destination identity UUID), configured loadbalance_algo.

    One logical NCM rule may compile into several children; children are
    grouped by a semantic key (logical name + trigger_priority + selector +
    source LANs + Traffic Class name), NOT by compiled UUID. Destinations
    (xpolicy.dst_ip_network) from all children merge into one list resolved
    via dest_index.

    stats/events correlate by LOGICAL RULE NAME (their steering_rule field
    holds the logical name on real hardware, not the compiled UUID). Stats
    are already logical-rule-level, so exactly ONE stats record attaches per
    logical rule -- counts are never multiplied by the number of children.
    """
    rules = raw_steering_rules
    if isinstance(rules, dict):
        rules = list(rules.values())
    if not isinstance(rules, list):
        rules = []

    stats_by_name = _index_stats_by_logical_name(raw_steering_stats)
    event_by_name = _index_latest_event_by_logical_name(raw_steering_events)

    groups = {}
    order = []
    for child in rules:
        if not isinstance(child, dict):
            continue
        xp = _xpolicy(child)

        # Logical name: prefer the CLEAN xpolicy.name (the compiled-policy
        # name, e.g. 'Lab01 - Youtube'); the top-level runtime name carries a
        # compiled-UUID suffix. Fall back to stripping the runtime name.
        xp_name = _s(xp.get('name'))
        if xp_name:
            lname = logical_rule_name(xp_name)
        else:
            lname = logical_rule_name(_s(_first_present(
                child, ('name', 'steering_rule', 'rule_name'))))

        trigger_priority = _num(xp.get('trigger_priority'))
        selector = classify_wan_selector(child)
        src_lans = _normalize_src_lans(xp.get('src_lans'))
        # Friendly Traffic Class name = top-level intent; UUID = xpolicy.intent
        tc_name = _s(child.get('intent')) or None
        tc_id = _s(xp.get('intent')) or None

        gkey = _semantic_group_key(lname, trigger_priority, selector,
                                   src_lans, tc_name)
        if gkey not in groups:
            groups[gkey] = _new_logical_rule(
                lname, trigger_priority, selector, src_lans, tc_name, tc_id)
            order.append(gkey)
        _merge_child_into_logical(groups[gkey], child)

    out = []
    for gkey in order:
        lr = groups[gkey]
        lr['destinations'] = resolve_destinations(
            lr.pop('_dest_uuids'), dest_index)
        lr['destination_type'] = _infer_destination_type(
            lr['destinations'], lr.get('logical_name'), stats_by_name)
        _correlate_stats_events(lr, stats_by_name, event_by_name)
        lr['semantic_fingerprint'] = logical_rule_fingerprint(lr)
        lr.pop('_compiled_ids', None)
        out.append(lr)
    return out


def _normalize_src_lans(src_lans):
    """Normalize xpolicy.src_lans (list of {'lan_name': ...}) to [str].

    Real shape: [{"lan_name": "Primary LAN"}]. Tolerant of plain strings.
    Returns a list of LAN name strings; never returns dicts.
    """
    out = []
    if isinstance(src_lans, list):
        for entry in src_lans:
            if isinstance(entry, dict):
                name = _s(_first_present(entry, ('lan_name', 'name')))
            else:
                name = _s(entry)
            if name and name not in out:
                out.append(name)
    elif isinstance(src_lans, dict):
        for _k, entry in src_lans.items():
            if isinstance(entry, dict):
                name = _s(_first_present(entry, ('lan_name', 'name')))
            else:
                name = _s(entry)
            if name and name not in out:
                out.append(name)
    return out


def _new_logical_rule(lname, trigger_priority, selector, src_lans,
                      tc_name, tc_id):
    return {
        'logical_name': lname or None,
        'trigger_priority': trigger_priority,
        'traffic_class_id': tc_id,          # xpolicy.intent UUID
        'traffic_class_name': tc_name,      # top-level intent (friendly)
        'source_lans': list(src_lans),
        'wan_selector_type': selector,
        'wan_priority_enabled': False,
        'wan_priority_order': [],           # explicit-priority detail
        'matched_devs': [],                 # runtime WAN IDs (from dict keys)
        'steering_to': [],                  # runtime WAN IDs (from dict keys)
        'runtime_algorithm': None,          # top-level loadbalance_algo
        'configured_algorithm': None,       # xpolicy loadbalance_algo
        'primary_wandev': None,
        'failover': None,
        'preferred_wan': None,
        'preferred_flows': None,
        'nonpreferred_flows': None,
        'was_flow_matched': None,
        'latest_event_reason': None,
        'latest_flows_moved': None,
        'destination_type': None,
        'destinations': [],
        'compiled_rule_ids': [],
        '_compiled_ids': [],
        '_dest_uuids': [],
    }


def _merge_child_into_logical(lr, child):
    """Fold one compiled child (top-level + xpolicy) into its logical rule."""
    xp = _xpolicy(child)

    # Compiled id (UUID) -- supplemental metadata only, never the identity.
    cid = _s(_first_present(child, ('id', '_id_'))) or _s(xp.get('_id_'))
    if cid and cid not in lr['compiled_rule_ids']:
        lr['compiled_rule_ids'].append(cid)
        lr['_compiled_ids'].append(cid)

    # Explicit WAN priority order comes from xpolicy triggers.
    if xp.get('wan_priority_enabled') is True:
        lr['wan_priority_enabled'] = True
        for entry in _extract_priority_order(child):
            if entry not in lr['wan_priority_order']:
                lr['wan_priority_order'].append(entry)

    # RUNTIME matched_devs / steering_to are DICTS keyed by WAN ID.
    for did in _dev_ids(child.get('matched_devs')):
        if did and did not in lr['matched_devs']:
            lr['matched_devs'].append(did)
    for did in _dev_ids(child.get('steering_to')):
        if did and did not in lr['steering_to']:
            lr['steering_to'].append(did)

    # RUNTIME loadbalance_algo (top level) kept distinct from the configured
    # xpolicy loadbalance_algo. Never flatten one into the other.
    runtime_algo = _s(child.get('loadbalance_algo'))
    if runtime_algo and lr['runtime_algorithm'] in (None, 'none'):
        lr['runtime_algorithm'] = runtime_algo
    elif runtime_algo and lr['runtime_algorithm'] is None:
        lr['runtime_algorithm'] = runtime_algo
    cfg_algo = _s(xp.get('loadbalance_algo'))
    if cfg_algo and lr['configured_algorithm'] is None:
        lr['configured_algorithm'] = cfg_algo

    if lr['primary_wandev'] is None:
        pw = _s(child.get('primary_wandev'))
        if pw:
            lr['primary_wandev'] = pw
    if lr['failover'] is None:
        lr['failover'] = child.get('failover', xp.get('failover'))

    # Destination identity UUID from xpolicy.dst_ip_network (merged).
    for uuid in _extract_dest_uuids(child):
        if uuid not in lr['_dest_uuids']:
            lr['_dest_uuids'].append(uuid)


def _extract_priority_order(child):
    """Extract explicit WAN priority order from xpolicy triggers.

    Each xpolicy.trigger_strings entry may carry a trigger_priority and a
    trigger_string encoding the WAN selector (e.g. type|is|wbond or
    type|is|sdwan%subtype|is|mdm%service_type|is|5G). Returns a list of
    {'trigger_string': str, 'trigger_priority': num} preserving order.
    """
    out = []
    for e in _trigger_entries(child):
        prio = e.get('trigger_priority')
        if prio is None:
            continue
        out.append({'trigger_string': e.get('trigger_string'),
                    'trigger_priority': prio})
    out.sort(key=lambda x: (x['trigger_priority']
                            if isinstance(x['trigger_priority'], (int, float))
                            else 1e9))
    return out


def _extract_dest_uuids(child):
    """Collect destination identity UUIDs from xpolicy.dst_ip_network.

    Real compiled children identify their destination via a single
    xpolicy.dst_ip_network UUID. Tolerant of list/dict shapes and legacy
    'destinations' fallback, but the primary source is dst_ip_network.
    """
    out = []
    xp = _xpolicy(child)

    dst = xp.get('dst_ip_network')
    if isinstance(dst, str):
        u = _s(dst)
        if u:
            out.append(u)
    elif isinstance(dst, (list, dict)):
        for _k, d in _as_entries(dst):
            if isinstance(d, dict):
                u = _s(_first_present(d, ('uuid', '_id_', 'id')))
            else:
                u = _s(d)
            if u and u not in out:
                out.append(u)

    # Legacy fallback (older/other shapes) -- never overrides dst_ip_network.
    if not out:
        legacy = _first_present(xp, ('destinations', 'dest')) or \
            _first_present(child, ('destinations', 'dest'))
        for _k, d in _as_entries(legacy):
            if isinstance(d, dict):
                u = _s(_first_present(d, ('uuid', '_id_', 'id', 'dst_ip_network')))
            else:
                u = _s(d)
            if u and u not in out:
                out.append(u)
    return out


def _semantic_group_key(lname, trigger_priority, selector, src_lans, tc_name):
    """Build the grouping key for compiled children of one logical rule.

    Excludes compiled UUIDs. Uses logical name, trigger priority, selector,
    source LANs, and Traffic Class name.
    """
    lans = ','.join(sorted(_s(x) for x in src_lans)) if isinstance(
        src_lans, list) else _s(src_lans)
    return '||'.join([
        _s(lname).lower(),
        '' if trigger_priority is None else str(trigger_priority),
        _s(selector),
        lans,
        _s(tc_name).lower(),
    ])


def _infer_destination_type(destinations, logical_name, stats_by_name):
    """Infer destination type from identity data / stats signals only.

    Prefers the identity 'type' field; otherwise falls back to the logical
    rule's stats apps appname/catname. Never infers from any id prefix.
    Returns 'application' | 'category' | 'mixed' | None.
    """
    types = set()
    for d in destinations:
        t = _s(d.get('type')).lower()
        if t in ('application', 'app'):
            types.add('application')
        elif t in ('category', 'cat'):
            types.add('category')

    if not types:
        srec = stats_by_name.get(_s(logical_name).lower())
        if srec:
            for a in srec.get('apps', []):
                if a.get('appname') and _s(a.get('appname')).lower() != 'none':
                    types.add('application')
                if a.get('catname') and _s(a.get('catname')).lower() != 'none':
                    types.add('category')

    if len(types) == 1:
        return types.pop()
    if len(types) > 1:
        return 'mixed'
    return None


def _index_stats_by_logical_name(raw_steering_stats):
    """Map LOGICAL RULE NAME (lower) -> normalized per-rule stats record.

    On the real router stats.rules[].steering_rule holds the logical name
    (e.g. 'LAB01-Best Effort'), not the compiled UUID. Stats are already
    logical-rule-level, so this is a 1:1 map.
    """
    norm = steering_stats(raw_steering_stats)
    out = {}
    for r in norm.get('rules', []):
        name = logical_rule_name(_s(r.get('steering_rule')))
        if name:
            out[name.lower()] = r
    return out


def _index_latest_event_by_logical_name(raw_steering_events):
    """Map LOGICAL RULE NAME (lower) -> latest event (by epoch) record.

    events[].steering_rule holds the logical name on real hardware.
    """
    norm = steering_events(raw_steering_events)
    out = {}
    for ev in norm.get('events', []):
        name = logical_rule_name(_s(ev.get('steering_rule')))
        if not name:
            continue
        key = name.lower()
        prev = out.get(key)
        ep = ev.get('epoch')
        if prev is None:
            out[key] = ev
        elif (isinstance(ep, (int, float)) and
              isinstance(prev.get('epoch'), (int, float)) and
              ep >= prev.get('epoch')):
            out[key] = ev
    return out


def _correlate_stats_events(lr, stats_by_name, event_by_name):
    """Attach the single logical-rule stats record + latest event.

    Correlation is by logical rule NAME. Because stats are already
    logical-rule-level, exactly one record is attached -- counts are NOT
    summed across compiled children (that would double-count).
    """
    key = _s(lr.get('logical_name')).lower()

    srec = stats_by_name.get(key)
    if srec:
        lr['preferred_flows'] = srec.get('preferred_flows')
        lr['nonpreferred_flows'] = srec.get('nonpreferred_flows')
        lr['was_flow_matched'] = srec.get('was_flow_matched')
        lr['preferred_wan'] = srec.get('preferred_wan')

    ev = event_by_name.get(key)
    if ev is not None:
        lr['latest_event_reason'] = ev.get('reason')  # verbatim
        lr['latest_flows_moved'] = ev.get('total_flows_moved')


# =========================================================================
# Semantic fingerprints (change-ready, durable across compiled UUID churn)
# =========================================================================

def _stable_hash(payload):
    """Deterministic short hash of a JSON-serializable payload."""
    try:
        blob = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                          default=str)
    except (TypeError, ValueError):
        blob = str(payload)
    return hashlib.sha1(blob.encode('utf-8')).hexdigest()[:16]


def logical_rule_fingerprint(lr):
    """Stable semantic fingerprint for a logical steering rule.

    Inputs are the durable semantic properties -- NOT compiled UUIDs. Two
    snapshots of the same logical NCM policy produce the same fingerprint
    even after policy recompilation/UUID churn, so history can track a
    logical rule across compiles.
    """
    lr = lr if isinstance(lr, dict) else {}
    dest_ids = sorted(
        _s(d.get('internal_name') or d.get('uuid'))
        for d in lr.get('destinations', []))
    payload = {
        'name': _s(lr.get('logical_name')).lower(),
        'trigger_priority': lr.get('trigger_priority'),
        'traffic_class': _s(lr.get('traffic_class_name')).lower(),
        'source_lans': sorted(_s(x) for x in lr.get('source_lans', [])),
        'selector': _s(lr.get('wan_selector_type')),
        'wan_priority_enabled': bool(lr.get('wan_priority_enabled')),
        'destinations': dest_ids,
    }
    return _stable_hash(payload)


def topology_fingerprint(wan_path_records):
    """Stable fingerprint of WAN topology (presence + object class + state).

    Used to detect TOPOLOGY changes (WAN appeared/disappeared, connection
    state, overlay/underlay mapping). Excludes fast-moving runtime metrics.
    """
    items = []
    for r in (wan_path_records or []):
        if not isinstance(r, dict):
            continue
        items.append({
            'uid': _s(r.get('uid')),
            'object_class': _s(r.get('object_class')),
            'kind': _s(r.get('kind')),
            'connection_state': _s(r.get('connection_state')),
            'underlay_uid': _s(r.get('underlay_uid')),
        })
    items.sort(key=lambda x: x['uid'])
    return _stable_hash(items)


def bonding_membership_fingerprint(bonds):
    """Stable fingerprint of bond membership + configured weights.

    Configured weight is part of the CONFIG fingerprint; runtime directional
    weights and flow counts are intentionally EXCLUDED (those are runtime
    changes, tracked separately).
    """
    items = []
    for b in (bonds or []):
        if not isinstance(b, dict):
            continue
        members = sorted(
            (_s(m.get('member_uid')), m.get('configured_weight'))
            for m in b.get('members', []) if isinstance(m, dict))
        items.append({
            'iface': _s(b.get('iface')),
            'name': _s(b.get('name')),
            'mode': _s(b.get('mode')),
            'members': members,
        })
    items.sort(key=lambda x: x['iface'])
    return _stable_hash(items)
