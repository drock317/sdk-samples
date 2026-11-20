"""Detect differences between two observed `steering_rule_defs` versions.

This reports that the EFFECTIVE definitions NCOS exposed differ between two
successful observations. It does not prove a user edited anything (a policy
recompile, a firmware change or an NCM push all look the same), and it says
nothing about when inside the observation interval the difference arose.

Limitations carried into every event:
  * rules are identified by normalized logical NAME only (identity_basis);
    a rename appears as removal + addition. When an unambiguous removed/
    added pair has an identical definition content hash it is annotated
    `possible_rename` with confirmed=false. Whether compiled ids survive a
    rename is unverified, so that is never claimed.
  * a criterion whose application/category kind is not established from
    NCOS runtime labels is reported with criterion_kind 'unknown'.
  * application "removal" because an application stopped matching flows is
    impossible here: only definitions are compared.

Pure functions; Python 3.8 compatible.
"""
import json

MAX_EVENTS_PER_TRANSITION = 200
MAX_VALUES = 20          # per previous/current value list
MAX_EVIDENCE_IDS = 16

NOTES = (
    'A change event means the effective definitions observed from NCOS '
    'differ between two successful observations. It does not prove a user '
    'edited the configuration.',
    'Rules are identified by logical name only; a rename looks like a '
    'removal plus an addition.',
    'previous_observed_ts is the last successful observation of the '
    'previous definition set; the change happened at an unknown time '
    'between it and detected_ts.',
    'Runtime flow gauges, selected WANs and application match activity '
    'never create change events.',
)


def _k(v):
    return json.dumps(v, sort_keys=True, default=str)


def _uniq(values):
    seen = {}
    for v in values:
        seen[_k(v)] = v
    return [seen[k] for k in sorted(seen)]


def _agg(rule):
    """Aggregate a rule's children into per-field value sets."""
    ch = rule.get('children') or []
    a = {'child_count': rule.get('child_count', 0)}

    def vals(f):
        return _uniq(g['def'].get(f) for g in ch)

    for f in ('trigger_priority', 'intent', 'dst_neg', 'dst_port_neg',
              'wan_priority_enabled', 'loadbalance_algo', 'failover',
              'reset_existing_flow', 'dia'):
        a[f] = vals(f)
    a['dst'] = _uniq([g['def'].get('dst_ip_network'),
                      g['def'].get('dst_kind')] for g in ch)
    a['src_lans'] = sorted({n for g in ch
                            for n in g['def'].get('src_lans') or []})
    a['excluded_lans'] = sorted({n for g in ch
                                 for n in g['def'].get('excluded_lans') or []})
    sel = {}
    for g in ch:
        for s, p in g['def'].get('trigger_strings') or []:
            sel.setdefault(s, [])
            if p not in sel[s]:
                sel[s].append(p)
    a['selectors'] = {s: sorted(ps, key=repr) for s, ps in sel.items()}
    return a


def _cap(values):
    if isinstance(values, list) and len(values) > MAX_VALUES:
        return values[:MAX_VALUES]
    return values


def _evidence(prev, cur):
    def ids(rule):
        out = []
        for g in (rule or {}).get('children') or []:
            for i in g.get('compiled_ids') or []:
                if i not in out:
                    out.append(i)
        return sorted(out)[:MAX_EVIDENCE_IDS]
    return {
        'compiled_ids_previous': ids(prev),
        'compiled_ids_current': ids(cur),
        'compiled_id_fp_previous': (prev or {}).get('compiled_id_fp'),
        'compiled_id_fp_current': (cur or {}).get('compiled_id_fp'),
        'compiled_ids_changed': ((prev or {}).get('compiled_id_fp')
                                 != (cur or {}).get('compiled_id_fp')),
        'basis': 'supporting evidence only; compiled ids are not a '
                 'permanent logical rule identifier',
    }


def _chg(rule, ctype, prev, cur, field=None, extra=None):
    d = {'rule_key': rule['key'], 'rule_name': rule['name'],
         'change_type': ctype, 'field': field,
         'previous': _cap(prev), 'current': _cap(cur)}
    if extra:
        d.update(extra)
    return d


def diff_rule(prev, cur, kind_of=None):
    """Changes between two versions of one rule present in both."""
    out = []
    if prev['def_fp'] == cur['def_fp']:
        return out
    pa, ca = _agg(prev), _agg(cur)
    if pa['child_count'] != ca['child_count']:
        out.append(_chg(cur, 'compiled_child_count_changed',
                        pa['child_count'], ca['child_count'],
                        field='child_count'))
    # destination / application criteria (generic add/remove)
    pd = {_k(x): x for x in pa['dst']}
    cd = {_k(x): x for x in ca['dst']}
    for k in sorted(set(cd) - set(pd)):
        v, kind = cd[k]
        info = (kind_of(v, kind) if kind_of else {}) or {}
        out.append(_chg(cur, 'destination_criterion_added', None, v,
                        field='dst_ip_network', extra={
                            'value_kind': kind,
                            'criterion_kind': info.get('criterion_kind',
                                                       'unknown'),
                            'kind_basis': info.get('kind_basis')}))
    for k in sorted(set(pd) - set(cd)):
        v, kind = pd[k]
        info = (kind_of(v, kind) if kind_of else {}) or {}
        out.append(_chg(cur, 'destination_criterion_removed', v, None,
                        field='dst_ip_network', extra={
                            'value_kind': kind,
                            'criterion_kind': info.get('criterion_kind',
                                                       'unknown'),
                            'kind_basis': info.get('kind_basis')}))
    if pa['intent'] != ca['intent']:
        out.append(_chg(cur, 'traffic_class_changed', pa['intent'],
                        ca['intent'], field='intent'))
    if pa['trigger_priority'] != ca['trigger_priority']:
        out.append(_chg(cur, 'rule_priority_changed',
                        pa['trigger_priority'], ca['trigger_priority'],
                        field='trigger_priority'))
    ps, cs = set(pa['selectors']), set(ca['selectors'])
    if ps != cs:
        out.append(_chg(cur, 'wan_selector_changed', sorted(ps), sorted(cs),
                        field='trigger_strings', extra={
                            'added': sorted(cs - ps),
                            'removed': sorted(ps - cs)}))
    prio = {s: (pa['selectors'][s], ca['selectors'][s])
            for s in sorted(ps & cs)
            if pa['selectors'][s] != ca['selectors'][s]}
    if prio:
        out.append(_chg(cur, 'wan_selector_priority_changed',
                        {s: v[0] for s, v in prio.items()},
                        {s: v[1] for s, v in prio.items()},
                        field='trigger_strings.trigger_priority'))
    for f, ctype in (('loadbalance_algo', 'algorithm_changed'),
                     ('failover', 'failover_changed'),
                     ('reset_existing_flow', 'reset_existing_flow_changed')):
        if pa[f] != ca[f]:
            out.append(_chg(cur, ctype, pa[f], ca[f], field=f))
    if pa['src_lans'] != ca['src_lans']:
        out.append(_chg(cur, 'source_lans_changed', pa['src_lans'],
                        ca['src_lans'], field='src_lans'))
    if pa['excluded_lans'] != ca['excluded_lans']:
        out.append(_chg(cur, 'excluded_lans_changed', pa['excluded_lans'],
                        ca['excluded_lans'], field='excluded_lans'))
    for f in ('wan_priority_enabled', 'dst_neg', 'dst_port_neg', 'dia'):
        if pa[f] != ca[f]:
            out.append(_chg(cur, 'definition_field_changed', pa[f], ca[f],
                            field=f))
    if not out:
        out.append(_chg(cur, 'definition_changed',
                        [g['fp'] for g in prev.get('children') or []],
                        [g['fp'] for g in cur.get('children') or []],
                        field='children',
                        extra={'reason': 'combination of fields within '
                                         'compiled children changed'}))
    for c in out:
        c['evidence'] = _evidence(prev, cur)
    return out


def diff(prev_payload, cur_payload, kind_of=None):
    """All changes between two steering_rule_defs payloads (unbounded list)."""
    pr = {r['key']: r for r in (prev_payload or {}).get('rules', [])}
    cr = {r['key']: r for r in (cur_payload or {}).get('rules', [])}
    out = []
    removed = [pr[k] for k in sorted(set(pr) - set(cr))]
    added = [cr[k] for k in sorted(set(cr) - set(pr))]
    rename = {}
    for r in removed:
        peers = [a for a in added if a['def_fp'] == r['def_fp']]
        same_removed = [x for x in removed if x['def_fp'] == r['def_fp']]
        if len(peers) == 1 and len(same_removed) == 1:
            a = peers[0]
            if len([x for x in added if x['def_fp'] == a['def_fp']]) == 1:
                rename[('r', r['key'])] = a['key']
                rename[('a', a['key'])] = r['key']

    def rn(side, rule):
        other = rename.get((side, rule['key']))
        if other is None:
            return None
        return {'basis': 'identical_definition_fingerprint',
                'counterpart_rule_key': other, 'confirmed': False,
                'note': 'compiled-id persistence under rename is unverified'}

    for r in removed:
        c = _chg(r, 'rule_removed', {'child_count': r['child_count']}, None)
        c['possible_rename'] = rn('r', r)
        c['evidence'] = _evidence(r, None)
        out.append(c)
    for a in added:
        c = _chg(a, 'rule_added', None, {'child_count': a['child_count']})
        c['possible_rename'] = rn('a', a)
        c['evidence'] = _evidence(None, a)
        out.append(c)
    for k in sorted(set(pr) & set(cr)):
        out.extend(diff_rule(pr[k], cr[k], kind_of))
    return out


def build_events(changes, ctx):
    """Wrap raw changes into bounded API events.

    ctx: from_version, to_version, previous_observed_ts, detected_ts,
    source_completeness, source_valid, derived_from.
    """
    total = len(changes)
    events = []
    prev_ts = ctx.get('previous_observed_ts')
    for i, c in enumerate(changes[:MAX_EVENTS_PER_TRANSITION]):
        e = dict(c)
        e.update({
            'event_id': 'srd:{}->{}:{}'.format(
                ctx.get('from_version'), ctx.get('to_version'), i),
            'from_version': ctx.get('from_version'),
            'to_version': ctx.get('to_version'),
            'previous_observed_ts': prev_ts,
            'detected_ts': ctx.get('detected_ts'),
            'interval_complete': prev_ts is not None,
            'interval_note': (None if prev_ts is not None else
                              'previous observation time unavailable '
                              '(restart or history not retained); the '
                              'change interval is open-ended'),
            'identity_basis': 'logical_name_only',
            'identity_confidence': 'name_only',
            'source': {'completeness': ctx.get('source_completeness'),
                       'valid': bool(ctx.get('source_valid', True))},
            'derived_from': ctx.get('derived_from', 'live_observation'),
            'detection_semantics': 'observed_definition_difference',
        })
        events.append(e)
    return {'from_version': ctx.get('from_version'),
            'to_version': ctx.get('to_version'),
            'detected_ts': ctx.get('detected_ts'),
            'event_count': total,
            'events': events,
            'truncated': total > MAX_EVENTS_PER_TRANSITION}
