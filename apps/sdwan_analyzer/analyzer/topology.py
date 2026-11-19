"""Read-only WAN / SD-WAN overlay / WBOND relationship view (v0.8.0).

GET /api/analytics/topology: a pure function over data the service already
holds (the cached snapshot plus the cached raw ``status/wan/devices``). It
collects nothing, stores nothing and writes nothing back.

Three object classes stay separate and are never merged or summed:

  physical  the transport (ethernet-wan, mdm-*, ...)
  sdwan     a logical overlay tunnel riding on a physical WAN
  wbond     a logical bonded interface over several overlay tunnels

Every relationship is reported with its state and its basis:

  resolved    supported by NCOS data that is present in the cache; `basis`
              names the field
  unresolved  the data needed to decide is missing or ambiguous; `reason`
              says which. Nothing is guessed to fill the gap

Configuration (priorities) is kept apart from observed runtime state
(connection state, link state, address). Observed membership means "the
router reported it", never "traffic flows through it".

Verified source fields (see docs/analytics/topology.md):
  overlay -> physical : status/wan/devices/<sdwan>/status.active_dep_wandev,
                        .dep_wandevs
  bond member -> overlay : members_list key == overlay info.iface
  bond member -> physical : members_list[].'intf name'
  primary : status/wan/primary_device

Python 3.8 compatible.
"""
from . import normalize

SCHEMA_VERSION = 1

# Fixed limitations, returned with every response so a consumer never has to
# infer what is NOT being claimed.
NOTES = (
    'Physical, sdwan and wbond counters and rates are different layers of '
    'the same traffic and are not additive; no cross-class totals are '
    'computed.',
    'WBOND membership is the runtime members_list reported by '
    'status/sdwan_adv/wan_bonding. A configured bond membership source is '
    'not collected, so configured membership is not_collected.',
    'A member or tunnel reported UP/connected is not evidence that it is '
    'carrying traffic or that the path is healthy.',
    'No bandwidth utilization or bonding efficiency is calculated.',
)

# Collector keys whose cache health is reported under as_of.
_SOURCES = (('wan_devices', 'wan'), ('primary_device', 'primary'),
            ('wan_bonding', 'bonding'))


def _resolved(uid, basis, **extra):
    d = {'state': 'resolved', 'uid': uid, 'basis': basis}
    d.update(extra)
    return d


def _unresolved(reason, **extra):
    d = {'state': 'unresolved', 'uid': None, 'reason': reason}
    d.update(extra)
    return d


def _dep_list(device):
    """Declared dependent WAN uids of an overlay, de-duplicated, in order."""
    status = device.get('status') if isinstance(
        device.get('status'), dict) else {}
    dep = status.get('dep_wandevs')
    items = (list(dep) if isinstance(dep, list)
             else list(dep.keys()) if isinstance(dep, dict) else [])
    out = []
    for d in items:
        d = normalize._s(d)
        if d and d not in out:
            out.append(d)
    return out


def _active_dep(device):
    status = device.get('status') if isinstance(
        device.get('status'), dict) else {}
    return normalize._s(status.get('active_dep_wandev'))


def _check_physical(uid, by_uid):
    """None when uid is a known physical path, else an unresolved reason."""
    rec = by_uid.get(uid)
    if rec is None:
        return 'not_in_wan_devices'
    if rec.get('object_class') != normalize.OBJ_PHYSICAL:
        return 'not_a_physical_wan'
    return None


def _overlay_underlay(uid, device, by_uid):
    """Overlay -> physical underlay, from the overlay's own dependents."""
    deps = _dep_list(device)
    active = _active_dep(device)
    extra = {'declared_dependents': deps, 'active_dependent': active or None}
    if active:
        why = _check_physical(active, by_uid)
        if why:
            return _unresolved(why, candidate=active, **extra)
        return _resolved(active, 'active_dep_wandev', **extra)
    if len(deps) == 1:
        why = _check_physical(deps[0], by_uid)
        if why:
            return _unresolved(why, candidate=deps[0], **extra)
        return _resolved(deps[0], 'dep_wandevs_single', **extra)
    if len(deps) > 1:
        return _unresolved('multiple_dependents_none_active', **extra)
    return _unresolved('no_dependent_reported', **extra)


def _resolve_physical_ref(ref, devices, by_uid):
    """Resolve a bond member's 'intf name' to ONE physical WAN uid.

    Exact device key first; else a unique info.iface match. An ambiguous or
    missing match is unresolved, never the first candidate.
    """
    if not ref:
        return _unresolved('no_intf_name_reported')
    if ref in devices:
        why = _check_physical(ref, by_uid)
        if why:
            return _unresolved(why, candidate=ref)
        return _resolved(ref, 'intf_name_is_device_key')
    hits = [u for u, d in devices.items() if isinstance(d, dict)
            and isinstance(d.get('info'), dict)
            and normalize._s(d['info'].get('iface')) == ref]
    hits = [u for u in hits if not _check_physical(u, by_uid)]
    if len(hits) == 1:
        return _resolved(hits[0], 'intf_name_matches_iface')
    if len(hits) > 1:
        return _unresolved('ambiguous_intf_name', candidates=sorted(hits))
    return _unresolved('intf_name_not_in_wan_devices', candidate=ref)


def _primary_block(primary_uid, by_uid, health):
    h = health.get('primary_device') or {}
    base = {'last_success_ts': h.get('last_success_ts'),
            'read_error': h.get('error')}
    uid = normalize._s(primary_uid)
    if not uid:
        return dict(base, state='unknown', uid=None,
                    reason='primary_device_not_reported')
    rec = by_uid.get(uid)
    if rec is None:
        return dict(base, state='unresolved', uid=uid,
                    reason='primary_not_in_wan_devices')
    return dict(base, state='known', uid=uid,
                object_class=rec.get('object_class'),
                kind=rec.get('kind'),
                display_name=rec.get('display_name'),
                connection_state=rec.get('connection_state'),
                ip_address=rec.get('ip_address'))


def _health(runtime_health, config_health):
    merged = dict(config_health or {})
    merged.update(runtime_health or {})
    return merged


def build(snapshot, raw_devices, health):
    """Compose the topology view.

    snapshot    the cached composed snapshot (dict)
    raw_devices cached raw status/wan/devices (for dependent fields only)
    health      {collector key: {last_success_ts, error, ...}}
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    devices = raw_devices if isinstance(raw_devices, dict) else {}
    health = health if isinstance(health, dict) else {}
    wan = snapshot.get('wan') if isinstance(snapshot.get('wan'), dict) else {}
    bonding = (snapshot.get('bonding')
               if isinstance(snapshot.get('bonding'), dict) else {})
    fresh = (snapshot.get('section_freshness')
             if isinstance(snapshot.get('section_freshness'), dict) else {})
    paths = [p for p in (wan.get('paths') or []) if isinstance(p, dict)]
    by_uid = {p.get('uid'): p for p in paths}

    primary = _primary_block(wan.get('primary_device'), by_uid, health)
    primary_known = primary['state'] == 'known'
    unresolved = []

    # --- overlays -------------------------------------------------------
    overlays, overlay_by_iface = [], {}
    for p in paths:
        if p.get('object_class') != normalize.OBJ_SDWAN:
            continue
        uid = p.get('uid')
        dev = devices.get(uid) if isinstance(devices.get(uid), dict) else {}
        under = _overlay_underlay(uid, dev, by_uid)
        if under['state'] != 'resolved':
            unresolved.append({'kind': 'overlay_underlay', 'uid': uid,
                               'reason': under['reason']})
        rec = {
            'uid': uid, 'iface': p.get('iface') or None,
            'display_name': p.get('display_name'),
            'config': {'cm_priority': p.get('cm_priority'),
                       'parent_priority': p.get('parent_priority')},
            'observed': {'connection_state': p.get('connection_state'),
                         'ip_address': p.get('ip_address'),
                         'is_primary': (p.get('is_primary')
                                        if primary_known else None)},
            'underlay': under,
            'bond_memberships': [],
        }
        overlays.append(rec)
        if rec['iface']:
            overlay_by_iface.setdefault(rec['iface'], []).append(rec)

    # --- bonds ----------------------------------------------------------
    wbond_paths = [p for p in paths
                   if p.get('object_class') == normalize.OBJ_WBOND]
    bonds, bond_ifaces = [], set()
    bonding_read = 'bonding' in fresh
    for b in (bonding.get('bonds') or []):
        if not isinstance(b, dict):
            continue
        iface = b.get('iface')
        bond_ifaces.add(iface)
        match = [p for p in wbond_paths if p.get('iface') == iface]
        if len(match) == 1:
            dev_ref = _resolved(match[0]['uid'], 'wbond_iface_matches_bond')
            wp = match[0]
        else:
            dev_ref = _unresolved('no_wbond_device_for_bond' if not match
                                  else 'ambiguous_wbond_device')
            wp = {}
            unresolved.append({'kind': 'bond_device', 'uid': iface,
                               'reason': dev_ref['reason']})
        members = []
        for m in (b.get('members') or []):
            if not isinstance(m, dict):
                continue
            tun = m.get('tunnel_iface')
            raw = m.get('raw') if isinstance(m.get('raw'), dict) else {}
            ref = normalize._s(raw.get('intf name'))
            hits = overlay_by_iface.get(tun) or []
            if len(hits) == 1:
                ov = _resolved(hits[0]['uid'], 'tunnel_iface_matches_overlay')
                hits[0]['bond_memberships'].append(
                    {'bond_iface': iface, 'tunnel_iface': tun})
            elif len(hits) > 1:
                ov = _unresolved('ambiguous_tunnel_iface')
            else:
                ov = _unresolved('tunnel_iface_not_an_overlay')
            phys = _resolve_physical_ref(ref, devices, by_uid)
            # Two independent routes to a physical WAN: the overlay's own
            # dependent, and the member's reported 'intf name'.
            ov_under = None
            if ov['state'] == 'resolved':
                ov_under = [o for o in overlays
                            if o['uid'] == ov['uid']][0]['underlay']
            if (ov_under is None or ov_under['state'] != 'resolved'
                    or phys['state'] != 'resolved'):
                consistency = 'not_comparable'
            elif ov_under['uid'] == phys['uid']:
                consistency = 'agree'
            else:
                consistency = 'conflict'
            for kind, r in (('member_overlay', ov), ('member_physical', phys)):
                if r['state'] != 'resolved':
                    unresolved.append({'kind': kind, 'uid': tun or iface,
                                       'reason': r['reason']})
            if consistency == 'conflict':
                unresolved.append({'kind': 'member_underlay_conflict',
                                   'uid': tun or iface,
                                   'reason': 'overlay_dependent_differs_'
                                             'from_intf_name'})
            members.append({
                'tunnel_iface': tun,
                'reported_intf_name': ref or None,
                'overlay': ov,
                'physical': phys,
                'underlay_consistency': consistency,
                'observed': {'link_state': m.get('link_state'),
                             'link_attach_state': m.get('link_attach_state'),
                             'has_loss': m.get('has_loss')},
                'config': {'configured_weight': m.get('configured_weight')},
            })
        # The normalized member_count falls back to len(members); use the
        # router's own 'Total interfaces' so the comparison is meaningful.
        braw = b.get('raw') if isinstance(b.get('raw'), dict) else {}
        reported = normalize._num(braw.get('Total interfaces'))
        bonds.append({
            'iface': iface,
            'wbond_device': dev_ref,
            'display_name': b.get('display_name'),
            'config': {'cm_priority': wp.get('cm_priority'),
                       'membership': {'state': 'not_collected'}},
            'observed': {
                'connection_state': wp.get('connection_state'),
                'link_state': b.get('link_state'),
                'bonding_mode': b.get('bonding_mode'),
                'negotiated_capabilities': b.get('negotiated_capabilities'),
                'is_primary': ((wp.get('is_primary') if wp else None)
                               if primary_known else None),
                'reported_member_count': reported,
                'listed_member_count': len(members),
                'member_count_matches': (None if reported is None
                                         else reported == len(members)),
            },
            'members': members,
        })
    for p in wbond_paths:
        if bonding_read and p.get('iface') not in bond_ifaces:
            unresolved.append({'kind': 'wbond_runtime', 'uid': p.get('uid'),
                               'reason': 'wbond_device_without_runtime'})

    # --- physical -------------------------------------------------------
    member_of = {}
    for b in bonds:
        for m in b['members']:
            if m['physical']['state'] == 'resolved':
                member_of.setdefault(m['physical']['uid'], []).append(
                    b['iface'])
    carried = {}
    for o in overlays:
        if o['underlay']['state'] == 'resolved':
            carried.setdefault(o['underlay']['uid'], []).append(o['uid'])
    physical = []
    for p in paths:
        if p.get('object_class') != normalize.OBJ_PHYSICAL:
            continue
        uid = p.get('uid')
        physical.append({
            'uid': uid, 'iface': p.get('iface') or None,
            'kind': p.get('kind'), 'display_name': p.get('display_name'),
            'config': {'cm_priority': p.get('cm_priority'),
                       'original_priority': p.get('original_priority'),
                       'parent_priority': p.get('parent_priority')},
            'observed': {'connection_state': p.get('connection_state'),
                         'ip_address': p.get('ip_address'),
                         'is_primary': (p.get('is_primary')
                                        if primary_known else None)},
            'overlays': carried.get(uid, []),
            'bond_memberships': member_of.get(uid, []),
        })

    # --- freshness ------------------------------------------------------
    as_of = {'snapshot_ts': snapshot.get('timestamp'),
             'wan_collected_ts': (fresh.get('wan') or {}).get('collected_ts'),
             'bonding_collected_ts': (fresh.get('bonding') or {}).get(
                 'collected_ts'),
             'sources': {}}
    for key, label in _SOURCES:
        h = health.get(key) or {}
        as_of['sources'][label] = {
            'last_success_ts': h.get('last_success_ts'),
            'read_error': h.get('error'),
            'stale': bool(h.get('error'))}

    return {
        'schema_version': SCHEMA_VERSION,
        'as_of': as_of,
        'primary': primary,
        'physical': physical,
        'overlays': overlays,
        'bonds': bonds,
        'bonding_runtime': {'state': 'read' if bonding_read else 'unknown',
                            'bond_count': len(bonds)},
        'unresolved': unresolved,
        'counters_additive': False,
        'notes': list(NOTES),
    }
