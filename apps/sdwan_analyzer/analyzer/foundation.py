"""Read-only v0.8.3 telemetry-foundation views.

    GET /api/analytics/steering          current definitions + runtime
    GET /api/analytics/steering/changes  definition versions + differences
    GET /api/analytics/wbond_config      configured bonds vs runtime members
    GET /api/analytics/validity          source validity / freshness

Pure functions over data the service already holds (cached raw reads, the
config version store, the steering-definitions tracker). They collect
nothing and write nothing. Configured values, observed runtime values and
derived resolutions are kept in separate, labelled blocks.

Python 3.8 compatible.
"""
from . import bond_config
from . import collectors
from . import cellular
from . import normalize
from . import query
from . import steering_changes
from . import steering_defs
from . import steering_state
from . import telemetry

SCHEMA_VERSION = 1
CHANGES_DEFAULT_LIMIT = 20
CHANGES_MAX_LIMIT = 50

STEERING_NOTES = (
    'definitions are the effective compiled xpolicy objects NCOS reported '
    'in status/wan/steering/rules, not a separate configuration object.',
    'Rules are identified by logical name only; NCOS exposes no stable '
    'logical rule id.',
    'runtime flow values are gauges (current flow populations), never '
    'cumulative counters. null means not reported; 0 is a reported zero.',
    'An application missing from runtime only means it matched no current '
    'flows; it is not a definition change.',
    'criterion_kind is application/category only when NCOS runtime labels '
    'establish it; otherwise unknown. It is never inferred from the '
    'identifier text.',
)


def _health(health, key):
    h = (health or {}).get(key) or {}
    return {'last_success_ts': h.get('last_success_ts'),
            'read_error': h.get('error'),
            'error_type': h.get('error_type'),
            'stale': bool(h.get('error'))}


def _name_index(identities_ip):
    out = {}
    for key, obj in normalize._as_entries(identities_ip):
        if isinstance(obj, dict):
            n = normalize._s(obj.get('name'))
            u = normalize._s(obj.get('_id_')) or normalize._s(key)
            if n and u:
                out.setdefault(n, []).append(u)
    return {n: sorted(set(v)) for n, v in out.items()}


def steering_view(store, tracker, raw, health, snapshot_ts=None):
    """Current steering definitions with resolution and runtime matching."""
    raw = raw or {}
    res = steering_defs.resolver(
        raw.get('identities_ip'), raw.get('identities_intent'),
        raw.get('traffic_class_config'), raw.get('steering_stats'))
    names = _name_index(raw.get('identities_ip'))
    cur = store.current(steering_state.NAME)
    payload = cur['payload'] if cur else None
    runtime = telemetry.steering_apps_record(
        raw.get('steering_stats'), cur['version'] if cur else None)
    rt_by_key = {}
    for r in runtime.get('rules', []):
        if r.get('rule_key'):
            rt_by_key.setdefault(r['rule_key'], r)

    rules = []
    for r in (payload or {}).get('rules', []):
        children = []
        dst_values = set()
        for g in r.get('children', []):
            d = g['def']
            if d.get('dst_kind') == steering_defs.DST_IDENTITY:
                dst_values.add(d.get('dst_ip_network'))
            children.append({
                'count': g['count'],
                'definition_fp': g['fp'],
                'definition': d,
                'destination': steering_defs.resolve_destination(
                    res, d.get('dst_ip_network'), d.get('dst_kind')),
                'traffic_class': steering_defs.resolve_intent(
                    res, d.get('intent')),
                'compiled_ids': g.get('compiled_ids', []),
            })
        rt = rt_by_key.get(r['key'])
        runtime_block = None
        if rt is not None:
            apps = []
            for a in rt['apps']:
                uu = names.get(a['id'], [])
                apps.append(dict(
                    a, identity_uuids=uu,
                    in_rule_definition=(any(u in dst_values for u in uu)
                                        if uu else None)))
            runtime_block = {k: rt[k] for k in (
                'preferred_flows', 'nonpreferred_flows',
                'preferred_flows_state', 'nonpreferred_flows_state',
                'was_flow_matched', 'total_perf_events', 'apps_total',
                'apps_omitted')}
            runtime_block['apps'] = apps
        rules.append({
            'key': r['key'], 'name': r['name'],
            'child_count': r['child_count'],
            'definition_fp': r['def_fp'],
            'compiled_id_fp': r['compiled_id_fp'],
            'compiled_id_count': r['compiled_id_count'],
            'children': children,
            'runtime': runtime_block,
            'runtime_state': ('reported' if rt is not None
                              else 'not_in_runtime_stats'),
        })
    def_keys = {r['key'] for r in rules}
    runtime_only = [{'rule': r['rule'], 'rule_key': r['rule_key']}
                    for r in runtime.get('rules', [])
                    if r.get('rule_key') not in def_keys]
    return {
        'schema_version': SCHEMA_VERSION,
        'as_of': {
            'snapshot_ts': snapshot_ts,
            'sources': {
                'steering': _health(health, 'steering_rules'),
                'identities_ip': _health(health, 'identities_ip'),
                'identities_intent': _health(health, 'identities_intent'),
                'traffic_class_config': _health(
                    health, 'traffic_class_config'),
            },
        },
        'definitions': tracker.status(store),
        'rules': rules,
        'runtime_only_rules': runtime_only,
        'capture': {k: runtime.get(k) for k in (
            'source_state', 'began_capture', 'flows_truncated',
            'rules_truncated', 'flow_values')},
        'notes': list(STEERING_NOTES),
    }


def parse_changes_params(raw_query):
    q = query.split_query(raw_query, ('limit', 'before_version'))
    limit = CHANGES_DEFAULT_LIMIT
    if 'limit' in q:
        t = q['limit']
        if not t.isdigit() or not 1 <= int(t) <= CHANGES_MAX_LIMIT:
            raise query.bad('limit must be an integer between 1 and {}'
                            .format(CHANGES_MAX_LIMIT))
        limit = int(t)
    before = None
    if 'before_version' in q:
        t = q['before_version']
        if not t.isdigit() or len(t) > 9 or int(t) < 1:
            raise query.bad('before_version must be a positive integer')
        before = int(t)
    return {'limit': limit, 'before_version': before}


def steering_changes_view(store, tracker, raw, raw_query=''):
    """Definition versions and their differences, newest first, paginated
    by to_version (`before_version` = exclusive upper bound)."""
    p = parse_changes_params(raw_query)
    res = steering_defs.resolver(
        (raw or {}).get('identities_ip'), None, None,
        (raw or {}).get('steering_stats'))

    def kind_of(v, k):
        return steering_defs.resolve_destination(res, v, k)

    live = {(t['from_version'], t['to_version'], t['detected_ts']): t
            for t in tracker.live_transitions()}
    live_pairs = {(t['from_version'], t['to_version'])
                  for t in live.values()}
    trans = list(live.values())
    for t in tracker.derived_transitions(store, kind_of):
        if (t['from_version'], t['to_version']) not in live_pairs:
            trans.append(t)
    trans.sort(key=lambda t: (t.get('detected_ts') or 0,
                              t.get('to_version') or 0), reverse=True)
    if p['before_version'] is not None:
        trans = [t for t in trans
                 if (t.get('to_version') or 0) < p['before_version']]
    page = trans[:p['limit']]
    nxt = None
    if len(trans) > p['limit'] and page:
        nxt = page[-1].get('to_version')
    versions = [{'version': v['version'], 'fingerprint': v['fingerprint'],
                 'first_seen_ts': v.get('ts'),
                 'rule_count': (v.get('payload') or {}).get('rule_count'),
                 'child_count': (v.get('payload') or {}).get('child_count')}
                for v in sorted(store.versions(steering_state.NAME),
                                key=lambda v: v['version'], reverse=True)]
    return {
        'schema_version': SCHEMA_VERSION,
        'object': steering_state.NAME,
        'status': tracker.status(store),
        'versions': versions,
        'transitions': page,
        'next_before_version': nxt,
        'limits': {'limit': p['limit'],
                   'max_events_per_transition':
                       steering_changes.MAX_EVENTS_PER_TRANSITION,
                   'retained_versions_per_object': store.info().get(
                       'max_versions_per_object')},
        'notes': list(steering_changes.NOTES),
    }


def wbond_config_view(store, raw, wbond_view, bonding_norm, health):
    """Configured bonding vs runtime membership (all relationships labelled)."""
    raw = raw or {}
    st, bonds = bond_config.normalize_config(raw.get('wan_bonding_cfg'))
    cur = store.current('wan_bonding_config')
    resolved = bond_config.resolve(bonds, raw.get('wan_rules2'), wbond_view,
                                   raw.get('wan_devices'))
    runtime = []
    for b in (wbond_view or {}).get('bonds') or []:
        rb = {}
        for x in ((bonding_norm or {}).get('bonds') or []):
            if x.get('iface') == b.get('iface'):
                rb = x
        rm = {m.get('tunnel_iface'): m for m in rb.get('members') or []}
        members = []
        for m in b.get('members') or []:
            n = rm.get(m.get('tunnel_iface'), {})
            members.append({
                'tunnel_iface': m.get('tunnel_iface'),
                'overlay_uid': (m.get('overlay') or {}).get('uid'),
                'physical_uid': (m.get('physical') or {}).get('uid'),
                'physical_display_name': m.get('physical_display_name'),
                'profile_name_reported': n.get('profile_name'),
                'link_state': n.get('link_state'),
                'link_attach_state': n.get('link_attach_state'),
                'weights_reported': {
                    'config_weight': n.get('configured_weight'),
                    'ds_weight': n.get('current_download_weight'),
                    'us_weight': n.get('current_upload_weight')},
                'flows': {'download': n.get('download_flows'),
                          'upload': n.get('upload_flows')},
                'bandwidth_raw_units_unverified': {
                    k: n.get(k) for k in (
                        'ds_curr_bw', 'us_curr_bw', 'ds_bw_trend',
                        'us_bw_trend', 'ds_last_bw_sample',
                        'us_last_bw_sample')},
            })
        obs = b.get('observed') or {}
        runtime.append({
            'iface': b.get('iface'),
            'connection_state': obs.get('connection_state'),
            'bonding_mode_reported': obs.get('bonding_mode'),
            'member_count_reported': obs.get('reported_member_count'),
            'member_count_listed': obs.get('listed_member_count'),
            'total_download_flows': obs.get('total_download_flows'),
            'total_download_flows_source': obs.get(
                'total_download_flows_source'),
            'total_upload_flows': obs.get('total_upload_flows'),
            'total_upload_flows_source': obs.get(
                'total_upload_flows_source'),
            'members': members,
        })
    return {
        'schema_version': SCHEMA_VERSION,
        'as_of': {'sources': {
            'wan_bonding_cfg': _health(health, 'wan_bonding_cfg'),
            'wan_bonding': _health(health, 'wan_bonding'),
            'wan_rules2': _health(health, 'wan_rules2'),
            'wan_devices': _health(health, 'wan_devices')}},
        'config_state': st,
        'config_version': ({'version': cur['version'],
                            'fingerprint': cur['fingerprint'],
                            'first_seen_ts': cur['ts']} if cur else None),
        'configured': [{
            'config_id': v['bond']['config_id'],
            'name': v['bond']['name'],
            'dynamic_weight': v['bond']['dynamic_weight'],
            'operating_mode': v['operating_mode'],
            'criteria': v['criteria'],
            'association': v['association'],
        } for v in resolved],
        'runtime': runtime,
        'notes': list(bond_config.NOTES),
    }


def validity_view(health, freshness, tracker, store, snapshot, bond_state,
                  qoe_cfg):
    """One place for data validity / freshness indicators."""
    snap = snapshot or {}
    qoe_paths = (snap.get('qoe') or {}).get('paths') or []
    qoe_flags = {}
    for p in qoe_paths:
        for f, v in (p.get('validity') or {}).items():
            qoe_flags.setdefault(f, {})
            qoe_flags[f][v] = qoe_flags[f].get(v, 0) + 1
    ts = snap.get('traffic_steering') or {}
    modems = (snap.get('cellular') or {}).get('modems') or []
    return {
        'schema_version': SCHEMA_VERSION,
        'snapshot_ts': snap.get('timestamp'),
        'collectors': {k: _health(health, k) for k in sorted(health or {})},
        'section_freshness': dict(freshness or {}),
        'steering_definitions': tracker.status(store),
        'steering_capture': ts.get('events_meta'),
        # v0.8.5: classification of the latest status/wan/steering read
        # (ok | ok_empty_rules | empty_source | incomplete_source |
        # transport_failure) and how many consecutive reads it has held.
        'steering_source': collectors.steering_source_state(),
        'wan_bonding_config': dict(bond_state or {}),
        'qoe': {'validity_counts': qoe_flags,
                'sentinel_candidates': sum(
                    p.get('pkt_loss_sentinel_candidates') or 0
                    for p in qoe_paths),
                'config': qoe_cfg,
                'sentinel_values_checked': list(
                    normalize.QOE_SENTINEL_CANDIDATES)},
        'cellular': [{
            'uid': m.get('uid'),
            'carrier_evidence': m.get('carrier_evidence'),
            'component_rf_present': sum(
                1 for c in m.get('carriers') or [] if c.get('rf')),
        } for m in modems],
        'cellular_rf_notes': list(cellular.RF_NOTES),
    }
