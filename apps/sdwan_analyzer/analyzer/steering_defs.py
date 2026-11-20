"""Steering DEFINITIONS (v0.8.3): what NCOS was configured to do.

Source: the nested `xpolicy` objects of status/wan/steering/rules (NCOS
7.26.81, verified in Phase 1A). Only the definition fields are read here;
runtime-only values (matched_devs, steering_to, primary_wandev, flow gauges,
runtime-selected WANs, resolved display names, identity-cache completeness)
never enter a definition fingerprint.

Confirmed:
  * the effective definition lives in xpolicy (name, trigger_priority,
    trigger_strings[], intent, dst_ip_network, dst_neg, dst_port_neg,
    src_lans, excluded_lans[].config_id, wan_priority_enabled,
    loadbalance_algo, failover, reset_existing_flow, dia);
  * xpolicy.dst_ip_network == config/identities/ip[]._id_ (full UUID);
  * xpolicy.intent == config/identities/intent[]._id_, whose traffic_class ==
    config/sdwan_adv/traffic_class[]._id_.
Not available: a stable logical rule UUID. Stats and events identify a rule
by NAME, so a logical rule is identified by its normalized name only
(identity_basis 'logical_name_only'). A rename therefore looks like a
removal plus an addition.

The `steering_rule_defs` version fingerprint (SRD_FP_VERSION) is DISTINCT
from the legacy `semantic_fingerprint` on logical rules, which stays as it
was for historical compatibility.

Python 3.8 compatible. Pure functions: no router I/O.
"""
import hashlib
import json

from . import normalize

SRD_SCHEMA = 1
SRD_FP_VERSION = 1
IDENTITY_BASIS = 'logical_name_only'
MAX_IDS_PER_GROUP = 8

# Source states of one read of status/wan/steering/rules.
ST_COMPLETE = 'complete'
ST_EMPTY = 'empty'              # read ok, zero compiled rules
ST_PARTIAL = 'partial'          # some children unusable
ST_UNAVAILABLE = 'unavailable'  # read ok but no rules object

# xpolicy fields that make up a child definition. `name` is the identity,
# not a definition field.
_SCALAR_FIELDS = ('trigger_priority', 'intent', 'dst_neg', 'dst_port_neg',
                  'wan_priority_enabled', 'loadbalance_algo', 'failover',
                  'reset_existing_flow', 'dia')

# dst_ip_network value classes.
DST_IDENTITY = 'identity'
DST_LITERAL_NONE = 'literal_none'   # the literal string "None"
DST_EMPTY = 'empty'                 # absent / null / ''
DST_OTHER = 'other'


def _hash(payload):
    blob = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                      default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:20]


def _prim(v):
    """JSON-primitive form of a field; containers become canonical text."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    try:
        return json.dumps(v, sort_keys=True, separators=(',', ':'),
                          default=str)
    except (TypeError, ValueError):
        return str(v)


def classify_dst(value):
    """(raw string or None, kind) for xpolicy.dst_ip_network."""
    if value is None:
        return None, DST_EMPTY
    if not isinstance(value, str):
        return _prim(value), DST_OTHER
    s = value.strip()
    if not s:
        return None, DST_EMPTY
    if s == 'None':
        return s, DST_LITERAL_NONE
    return s, DST_IDENTITY


def _excluded_lans(v):
    out = []
    if isinstance(v, dict):
        v = list(v.values())
    if isinstance(v, list):
        for e in v:
            if isinstance(e, dict):
                c = e.get('config_id')
            else:
                c = e
            c = normalize._s(c)
            if c and c not in out:
                out.append(c)
    return sorted(out)


def _trigger_pairs(xp):
    out = []
    ts = xp.get('trigger_strings')
    if isinstance(ts, dict):
        ts = list(ts.values())
    if isinstance(ts, list):
        for t in ts:
            if isinstance(t, dict):
                s = t.get('trigger_string')
                p = normalize._num(t.get('trigger_priority'))
            else:
                s, p = t, None
            s = normalize._s(s)
            if s:
                out.append([s, p])
    out.sort(key=lambda x: (x[0], repr(x[1])))
    return out


def child_definition(xp):
    """Canonical definition dict of one compiled child (xpolicy only)."""
    d = {}
    for f in _SCALAR_FIELDS:
        v = xp.get(f)
        d[f] = normalize._num(v) if f == 'trigger_priority' else _prim(v)
        if f == 'intent' and isinstance(v, str):
            d[f] = v.strip() or None
    dst, kind = classify_dst(xp.get('dst_ip_network'))
    d['dst_ip_network'] = dst
    d['dst_kind'] = kind
    d['src_lans'] = sorted(normalize._normalize_src_lans(xp.get('src_lans')))
    d['excluded_lans'] = _excluded_lans(xp.get('excluded_lans'))
    d['trigger_strings'] = _trigger_pairs(xp)
    return d


def _compiled_ids(child, xp):
    ids = []
    for v in (xp.get('_id_'), child.get('id'), child.get('_id_')):
        s = normalize._s(v)
        if s and s not in ids:
            ids.append(s)
    return ids


def extract(raw_rules):
    """Parse the raw rules list into (state, payload|None, invalid_count).

    A payload is returned for every state except `unavailable`; the caller
    decides whether the state is trustworthy enough to version (only
    `complete` always is; `empty` needs confirmation; `partial` never).
    """
    rules = raw_rules
    if isinstance(rules, dict):
        rules = list(rules.values())
    if not isinstance(rules, list):
        return ST_UNAVAILABLE, None, 0
    by_key = {}
    order = []
    invalid = 0
    for child in rules:
        if not isinstance(child, dict):
            invalid += 1
            continue
        xp = normalize._xpolicy(child)
        raw_name = normalize._s(xp.get('name')) or normalize._s(
            normalize._first_present(child, ('name', 'steering_rule',
                                             'rule_name')))
        lname = normalize.logical_rule_name(raw_name)
        if not xp or not lname:
            invalid += 1
            continue
        key = lname.lower()
        rec = by_key.get(key)
        if rec is None:
            rec = {'key': key, 'name': lname, 'raw_names': [],
                   'groups': {}, 'ids': [], 'child_count': 0}
            by_key[key] = rec
            order.append(key)
        if lname not in rec['raw_names']:
            rec['raw_names'].append(lname)
        d = child_definition(xp)
        fp = _hash(d)
        g = rec['groups'].get(fp)
        if g is None:
            g = {'fp': fp, 'def': d, 'count': 0, 'compiled_ids': []}
            rec['groups'][fp] = g
        g['count'] += 1
        rec['child_count'] += 1
        for cid in _compiled_ids(child, xp):
            if cid not in rec['ids']:
                rec['ids'].append(cid)
            if (cid not in g['compiled_ids']
                    and len(g['compiled_ids']) < MAX_IDS_PER_GROUP):
                g['compiled_ids'].append(cid)
    out = []
    child_total = 0
    for key in sorted(by_key):
        rec = by_key[key]
        groups = sorted(rec['groups'].values(), key=lambda g: g['fp'])
        for g in groups:
            g['compiled_ids'].sort()
        child_total += rec['child_count']
        out.append({
            'key': key,
            'name': rec['name'],
            'child_count': rec['child_count'],
            # Content hash of the definition multiset. Excludes the name
            # and compiled ids.
            'def_fp': _hash([[g['fp'], g['count']] for g in groups]),
            # Supporting evidence only: never part of the version fp.
            'compiled_id_fp': _hash(sorted(rec['ids'])),
            'compiled_id_count': len(rec['ids']),
            'children': groups,
        })
    payload = {
        'schema': SRD_SCHEMA,
        'fp_version': SRD_FP_VERSION,
        'identity_basis': IDENTITY_BASIS,
        'rule_count': len(out),
        'child_count': child_total,
        'rules': out,
    }
    if not out and not invalid:
        return ST_EMPTY, payload, 0
    if invalid:
        return ST_PARTIAL, payload, invalid
    return ST_COMPLETE, payload, 0


def payload_fingerprint(payload):
    """Version fingerprint: logical key + content hash per rule only."""
    return _hash({'v': 'srd{}'.format(SRD_FP_VERSION),
                  'rules': [[r['key'], r['def_fp']]
                            for r in (payload or {}).get('rules', [])]})


# --- identity resolution (exact UUIDs only) ----------------------------------

def _by_id(raw):
    out = {}
    for key, obj in normalize._as_entries(raw):
        if isinstance(obj, dict):
            i = normalize._s(obj.get('_id_')) or normalize._s(key)
            if i:
                out[i] = obj
    return out


def resolver(identities_ip, identities_intent, traffic_class_cfg,
             runtime_stats=None):
    """Build exact-UUID lookups from the cached config trees.

    Returns a dict of lookups used by resolve_*(). Never prefix matching.
    `runtime_stats` (raw status/wan/steering/stats) only annotates the KIND
    of an identity (application/category) from NCOS-reported labels.
    """
    ips = _by_id(identities_ip)
    intents = _by_id(identities_intent)
    tcs = _by_id(traffic_class_cfg)
    kinds = runtime_kinds(runtime_stats)
    return {'ips': ips, 'intents': intents, 'tcs': tcs, 'kinds': kinds}


def _real(v):
    s = normalize._s(v)
    return s if s and s.lower() not in ('none', 'n/a', '--') else ''


def runtime_kinds(raw_stats):
    """{internal id (== identities/ip name): {'kind', 'appname', 'catname'}}.

    kind is `application` when NCOS reports a real appname for the entry,
    `category` when only a real catname, else `unknown`. This is evidence
    from runtime labels, not from the identifier text.
    """
    out = {}
    stats = raw_stats if isinstance(raw_stats, dict) else {}
    for r in (stats.get('rules') or []):
        if not isinstance(r, dict):
            continue
        for k, a in normalize._as_entries(r.get('apps')):
            if not isinstance(a, dict):
                continue
            app, cat = _real(a.get('appname')), _real(a.get('catname'))
            kind = 'application' if app else ('category' if cat else 'unknown')
            prev = out.get(str(k))
            if prev is None or prev['kind'] == 'unknown':
                out[str(k)] = {'kind': kind, 'appname': app or None,
                               'catname': cat or None}
    return out


def resolve_destination(res, dst_value, dst_kind):
    """Resolve one dst_ip_network value. `res` is resolver() output."""
    rec = {'value': dst_value, 'value_kind': dst_kind,
           'resolved': False, 'identity_name': None, 'friendly_name': None,
           'criterion_kind': 'unknown', 'kind_basis': None}
    if dst_kind != DST_IDENTITY:
        rec['match_semantics'] = {
            DST_EMPTY: 'no destination criterion present',
            DST_LITERAL_NONE: 'literal "None" reported by NCOS',
            DST_OTHER: 'unrecognized destination value'}.get(dst_kind)
        return rec
    obj = (res or {}).get('ips', {}).get(dst_value)   # exact UUID only
    if isinstance(obj, dict):
        name = normalize._s(obj.get('name')) or None
        rec.update({'resolved': True, 'identity_name': name,
                    'friendly_name': normalize._s(
                        obj.get('friendly_name')) or None})
        k = (res.get('kinds') or {}).get(name or '')
        if k and k['kind'] != 'unknown':
            rec['criterion_kind'] = k['kind']
            rec['kind_basis'] = 'runtime_stats_labels'
    return rec


def resolve_intent(res, intent_uuid):
    rec = {'intent_id': intent_uuid, 'resolved': False, 'intent_name': None,
           'traffic_class_id': None, 'traffic_class_name': None}
    if not intent_uuid:
        return rec
    it = (res or {}).get('intents', {}).get(intent_uuid)
    if isinstance(it, dict):
        rec['resolved'] = True
        rec['intent_name'] = normalize._s(it.get('name')) or None
        tcid = normalize._s(it.get('traffic_class')) or None
        rec['traffic_class_id'] = tcid
        tc = (res or {}).get('tcs', {}).get(tcid) if tcid else None
        if isinstance(tc, dict):
            rec['traffic_class_name'] = normalize._s(tc.get('name')) or None
    return rec
