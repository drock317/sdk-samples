"""WAN bonding CONFIGURATION (v0.8.3): what the bond was configured to do.

Source: config/sdwan_adv/wan_bonding (NCOS 7.26.81, verified in Phase 1A):
a list of bonds {_id_, name, dynamic_weight, members[]} where each member is
a selection CRITERION {_id_, trigger_string, weight}. A criterion is not a
runtime member: one criterion can match several runtime members and a
member can match none.

Confirmed:
  * dynamic_weight is a boolean: false = static, true = dynamic operation;
  * runtime member physical UID -> status/wan/devices/<uid>/info.config_id ->
    config/wan/rules2[]._id_;
  * the profile criterion form `type|is|none%config_name|is|<name>` matches
    rules2[].trigger_name;
  * `type|is|ethernet` is a verified criterion form.
Not verified (kept raw / unresolved, never guessed):
  * any other criterion expression;
  * a universal configured-bond _id_ -> runtime bond interface mapping;
  * how a criterion weight becomes runtime config_weight / ds_weight /
    us_weight (a 100-weight cellular criterion produced three runtime values
    of 33 on one lab router; the general rule is unknown);
  * the units of the runtime bandwidth fields.

Python 3.8 compatible. Pure functions: no router I/O.
"""
import hashlib
import json

from . import normalize

CFG_SCHEMA = 1
CFG_FP_VERSION = 1

MODE_STATIC = 'static'
MODE_DYNAMIC = 'dynamic'
MODE_UNKNOWN = 'unknown'

NOTES = (
    'operating_mode comes only from the boolean dynamic_weight field. It '
    'is never inferred from equal weights or member counts.',
    'A configured criterion is not a runtime member; one criterion can '
    'match several runtime members and a member may match none.',
    'criterion weight, runtime config_weight, ds_weight and us_weight are '
    'four separate quantities. No relationship between them is assumed, '
    'weights are not assumed to sum to 100, and no traffic share or '
    'bonding efficiency is calculated from them.',
    'No universal configured-bond to runtime-bond mapping is verified. '
    'With exactly one configured and one runtime bond, criteria coverage '
    'gives a candidate association; otherwise the association is '
    'unverified.',
    'Runtime bandwidth fields are preserved as reported; their units are '
    'not verified.',
)


def _hash(payload):
    blob = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                      default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:20]


def operating_mode(dynamic_weight):
    if dynamic_weight is True:
        return MODE_DYNAMIC
    if dynamic_weight is False:
        return MODE_STATIC
    return MODE_UNKNOWN


def parse_criterion(trigger_string):
    """Split a criterion expression into its clauses.

    Returns {'form', 'clauses', 'profile_name', 'type_value'}. `form` is
    `type`, `profile`, or `other` (raw and unresolved). Clause splitting
    is on '%' then the first two '|' only, so a profile name that contains
    '|' survives intact.
    """
    ts = normalize._s(trigger_string)
    clauses = []
    for part in ts.split('%') if ts else []:
        bits = part.split('|', 2)
        if len(bits) == 3:
            clauses.append({'field': bits[0], 'op': bits[1],
                            'value': bits[2]})
        else:
            clauses.append({'field': part, 'op': None, 'value': None})
    form, profile, tvalue = 'other', None, None
    if (len(clauses) == 1 and clauses[0]['field'] == 'type'
            and clauses[0]['op'] == 'is' and clauses[0]['value']):
        form, tvalue = 'type', clauses[0]['value']
    elif (len(clauses) == 2 and clauses[0]['field'] == 'type'
          and clauses[0]['op'] == 'is' and clauses[0]['value'] == 'none'
          and clauses[1]['field'] == 'config_name'
          and clauses[1]['op'] == 'is' and clauses[1]['value']):
        form, profile = 'profile', clauses[1]['value']
    return {'form': form, 'clauses': clauses, 'profile_name': profile,
            'type_value': tvalue}


def normalize_config(raw):
    """Normalize config/sdwan_adv/wan_bonding.

    Returns (state, bonds): state is 'ok' for a list/dict (possibly empty),
    'unavailable' for anything else. A missing weight stays None; zero stays
    zero. Entry order from NCOS is preserved in `criteria`.
    """
    if raw is None or not isinstance(raw, (list, dict)):
        return 'unavailable', []
    bonds = []
    for key, obj in normalize._as_entries(raw):
        if not isinstance(obj, dict):
            continue
        dyn = obj.get('dynamic_weight')
        criteria = []
        for ckey, c in normalize._as_entries(obj.get('members')):
            if not isinstance(c, dict):
                continue
            ts = normalize._s(c.get('trigger_string'))
            parsed = parse_criterion(ts)
            criteria.append({
                'criterion_id': normalize._s(c.get('_id_')) or None,
                'trigger_string': ts,
                'weight': normalize._num(c.get('weight')),
                'form': parsed['form'],
                'profile_name': parsed['profile_name'],
                'type_value': parsed['type_value'],
            })
        bonds.append({
            'config_id': normalize._s(obj.get('_id_')) or normalize._s(key)
            or None,
            'name': normalize._s(obj.get('name')) or None,
            'dynamic_weight': dyn if isinstance(dyn, bool) else None,
            'operating_mode': operating_mode(
                dyn if isinstance(dyn, bool) else None),
            'criteria': criteria,
        })
    return 'ok', bonds


def _wkey(w):
    if w is None:
        return (1, 0)
    return (0, w)


def config_payload(bonds):
    """Version payload: the normalized bonds, ordered deterministically."""
    ordered = sorted(bonds, key=lambda b: (b.get('config_id') or '',
                                           b.get('name') or ''))
    return {'schema': CFG_SCHEMA, 'fp_version': CFG_FP_VERSION,
            'bonds': [dict(b) for b in ordered]}


def config_fingerprint(payload):
    """Versioned fingerprint of the configured content.

    Criterion `_id_` values are excluded (re-creating an identical
    criterion is not a meaningful change); criteria are ordered so array
    order is irrelevant while duplicates are preserved; a missing weight
    differs from a zero weight.
    """
    items = []
    for b in (payload or {}).get('bonds', []):
        crit = sorted(([c.get('trigger_string'), list(_wkey(c.get('weight')))]
                       for c in b.get('criteria', [])),
                      key=lambda x: (x[0] or '', x[1]))
        items.append({'config_id': b.get('config_id'),
                      'name': b.get('name'),
                      'dynamic_weight': b.get('dynamic_weight'),
                      'criteria': crit})
    return _hash({'v': 'wbc{}'.format(CFG_FP_VERSION), 'bonds': items})


# --- resolution against runtime ---------------------------------------------

def _rules2_candidates(criterion, rules2):
    """rules2 entries a criterion refers to (exact comparisons only)."""
    out = []
    for key, r in normalize._as_entries(rules2):
        if not isinstance(r, dict):
            continue
        rid = normalize._s(r.get('_id_')) or normalize._s(key)
        if criterion['form'] == 'profile':
            if normalize._s(r.get('trigger_name')) == criterion['profile_name']:
                out.append(rid)
        elif criterion['form'] == 'type':
            if (normalize._s(r.get('trigger_string')).lower()
                    == criterion['trigger_string'].lower()):
                out.append(rid)
    return sorted(set(out))


def resolve(bonds, rules2, wbond_view, raw_devices):
    """Join configured criteria to runtime members.

    `wbond_view` is wbond.build() output (its members already carry the
    topology-resolved physical uid). Join: member physical uid ->
    wan_devices[uid].info.config_id -> rules2._id_. Returns a list of
    configured-bond views plus the runtime-bond association evidence.
    """
    devices = raw_devices if isinstance(raw_devices, dict) else {}
    runtime = (wbond_view or {}).get('bonds') or []

    def member_cfg_id(m):
        puid = (m.get('physical') or {}).get('uid')
        if not puid:
            return None, None
        dev = devices.get(puid)
        info = dev.get('info') if isinstance(dev, dict) else None
        cid = normalize._s((info or {}).get('config_id')) or None
        return puid, cid

    out = []
    for b in bonds:
        crit_views = []
        matched_runtime = {}
        for c in b['criteria']:
            cands = (_rules2_candidates(c, rules2)
                     if c['form'] in ('profile', 'type') else [])
            cv = dict(c)
            if c['form'] == 'other':
                cv['resolution'] = 'unresolved_expression'
            elif not cands:
                cv['resolution'] = 'no_matching_rules2_entry'
            elif c['form'] == 'profile':
                cv['resolution'] = 'rules2_trigger_name'
            else:
                cv['resolution'] = 'rules2_trigger_string'
            cv['rules2_ids'] = cands
            members = []
            for rb in runtime:
                for m in rb.get('members') or []:
                    puid, cid = member_cfg_id(m)
                    if cid and cid in cands:
                        members.append({
                            'runtime_bond_iface': rb.get('iface'),
                            'tunnel_iface': m.get('tunnel_iface'),
                            'physical_uid': puid})
                        matched_runtime.setdefault(rb.get('iface'), set()).add(
                            m.get('tunnel_iface'))
            cv['matched_member_count'] = len(members)
            cv['matched_members'] = members
            crit_views.append(cv)
        out.append({'bond': b, 'criteria': crit_views,
                    'matched_runtime': matched_runtime})

    cfg_n, rt_n = len(out), len(runtime)
    for v in out:
        b = v['bond']
        mr = v.pop('matched_runtime')
        if cfg_n == 1 and rt_n == 1 and mr:
            rb = runtime[0]
            total = len(rb.get('members') or [])
            covered = len(mr.get(rb.get('iface'), ()))
            assoc = {'state': 'candidate',
                     'runtime_bond_iface': rb.get('iface'),
                     'basis': 'single_configured_bond_single_runtime_bond'
                              '_with_criteria_match',
                     'runtime_members': total,
                     'runtime_members_matched': covered,
                     'coverage': ('full' if total and covered == total
                                  else 'partial')}
        elif cfg_n == 1 and rt_n == 1:
            assoc = {'state': 'unverified', 'runtime_bond_iface': None,
                     'basis': 'no_criterion_matched_a_runtime_member'}
        else:
            assoc = {'state': 'unverified', 'runtime_bond_iface': None,
                     'basis': ('no_runtime_bond' if rt_n == 0 else
                               'multiple_bonds_no_verified_mapping'),
                     'candidate_runtime_bonds': sorted(
                         k for k in mr if k)}
        v['association'] = assoc
        v['operating_mode'] = b['operating_mode']
    return out
