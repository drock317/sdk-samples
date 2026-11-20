"""SD-WAN historical analytics and evidence-based correlation (v0.8.4).

Bounded and read-only, like cell_analytics: observations are fed one at a
time and reduced to compact samples; nothing is stored or sent to the router.

What the numbers mean (and do not mean)
* Steering flow counts are GAUGES (flows present at a poll), never counters.
  Min/avg/max/last are statistics of those gauges, not flow totals.
* Bond weights (config_weight, ds_weight, us_weight) are control values NCOS
  reports. They are NOT traffic shares and no distribution, efficiency or
  fairness is derived from them. Weights need not sum to 100.
* Throughput is per interface and per LAYER (physical, sdwan overlay, wbond).
  The layers carry the same traffic at different points; they are never
  summed or compared as totals.
* Latency/jitter are the values NCOS reports. Packet-loss PERCENTAGES are not
  computed: the loss counters' semantics are not verified, so only presence
  and the NCOS `has_loss` indicator are reported.
* Correlation lists events that coincide in time. It is evidence of timing
  only and never asserts a cause.
"""
from . import counters
from .cell_analytics import (_Stat, confidence, median, MIN_GAP_S, GAP_FACTOR)

SCHEMA_VERSION = 1
MAX_RULES = 40
MAX_APPS_PER_RULE = 30
MAX_INTERFACES = 40
MAX_MEMBERS = 32
DEFAULT_MAX_EVENTS = 200
WEIGHT_SHIFT_POINTS = 10          # analysis parameter, reported in output
LATENCY_STEP_MS = 50
LATENCY_STEP_RATIO = 2.0
MAX_PAIRS = 100
MAX_CORRELATION_WINDOW_S = 1800

NOTES = (
    'Steering flow values are gauges (flows present at a poll); statistics '
    'are over polls, never flow totals.',
    'Bond weights are NCOS control values, not traffic shares: no traffic '
    'distribution or efficiency is derived and weights are not assumed to '
    'sum to 100.',
    'Throughput is reported per interface and layer. Physical, sdwan '
    'overlay and wbond counters are different views of the same traffic and '
    'are never added.',
    'Packet-loss percentages are not computed (counter semantics are not '
    'verified); only presence and the NCOS has_loss indicator are shown.',
    'Event thresholds (weight_shift_points, latency step) are analysis '
    'parameters listed in the response, not NCOS values.',
)

CORRELATION_NOTES = (
    'Events are listed together only because they were first observed in '
    'the same or adjacent polls. This is timing evidence, not causation: '
    'the order of events inside one poll interval is unknown.',
    'same_path means the SD-WAN event names the same physical modem as the '
    'cellular event; without it the pair is only time-aligned.',
    'Compare with event_rates_per_hour: frequent event types coincide by '
    'chance.',
)


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v:
        return None
    return float(v)


def _pct(vals, p):
    """Nearest-rank percentile of a list of numbers."""
    if not vals:
        return None
    s = sorted(vals)
    k = max(1, int(round(p / 100.0 * len(s))))
    return s[min(k, len(s)) - 1]


def _event(kind, domain, ts, prev_ts, detail=None, physical_uid=None,
           frm=None, to=None, thr=None):
    dt = ts - prev_ts
    across = thr is not None and dt > thr
    return {
        'type': kind, 'domain': domain, 'first_observed_ts': ts,
        'detected_between': [prev_ts, ts], 'uncertainty_s': int(dt),
        'time_resolution': 'between_polls', 'exact_time': False,
        'across_gap': across, 'confidence': 'low' if across else 'normal',
        'physical_uid': physical_uid, 'from': frm, 'to': to,
        'detail': detail or {}}


class SdwanAnalyzer(object):
    def __init__(self, interval_hint_s=None, max_events=DEFAULT_MAX_EVENTS):
        self._hint = interval_hint_s
        self._max_events = max(1, int(max_events))
        self._obs = []               # compact per-observation samples

    # -- ingestion -------------------------------------------------------------
    def add(self, obs):
        ts = obs.get('collected_ts')
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            return
        sa = obs.get('steering_apps')
        steer = obs.get('steering')
        wbond = obs.get('wbond')
        ifs = obs.get('interfaces')
        qoe = obs.get('qoe')
        sw = obs.get('swans_samples')
        cv = obs.get('config_versions') or {}
        d = cv.get('steering_rule_defs')
        self._obs.append({
            'ts': ts,
            'sa': sa if isinstance(sa, dict) else None,
            'steer': steer if isinstance(steer, list) else None,
            'wbond': wbond if isinstance(wbond, list) else None,
            'ifs': [{k: r.get(k) for k in (
                'uid', 'object_class', 'connection_state', 'rx_bytes',
                'tx_bytes', 'collected_ts', 'metric_ts', 'collected_at',
                'timestamp_source')} for r in ifs if isinstance(r, dict)]
            if isinstance(ifs, list) else None,
            'qoe': [{k: r.get(k) for k in (
                'uid', 'latency_avg_ms', 'latency_last_ms', 'validity',
                'pkt_loss_stats_raw', 'pkt_loss_sentinel_candidates')}
                for r in qoe if isinstance(r, dict)]
            if isinstance(qoe, list) else None,
            'swans': [{'uid': r.get('uid'), 'lat': r.get('latency_ms'),
                       'jit': r.get('jitter_ms')} for r in sw
                      if isinstance(r, dict)]
            if isinstance(sw, list) else None,
            'defs': ({'version': d.get('version'),
                      'attribution': d.get('attribution'),
                      'changed': d.get('changed')}
                     if isinstance(d, dict) else None),
        })

    # -- timing --------------------------------------------------------------------
    def interval_s(self):
        ts = [o['ts'] for o in self._obs]
        est = median([b - a for a, b in zip(ts, ts[1:]) if b > a])
        if est is None:
            return self._hint
        return max(est, self._hint) if self._hint else est

    def gap_threshold_s(self):
        iv = self.interval_s()
        return max(MIN_GAP_S, GAP_FACTOR * iv) if iv else MIN_GAP_S

    # -- result ------------------------------------------------------------------------
    def result(self, window=None):
        thr = self.gap_threshold_s()
        events = []
        steering = self._steering(thr, events)
        bonds = self._bonds(thr, events)
        ifaces = self._interfaces(thr, events)
        quality = self._quality(thr, events)
        self._attach_member_throughput(bonds, ifaces)
        events.sort(key=lambda e: (e['first_observed_ts'], e['type']))
        counts = {}
        for e in events:
            counts[e['type']] = counts.get(e['type'], 0) + 1
        ts = [o['ts'] for o in self._obs]
        win = dict(window or {})
        win.update(first_observation_ts=ts[0] if ts else None,
                   last_observation_ts=ts[-1] if ts else None)
        return {
            'schema_version': SCHEMA_VERSION,
            'window': win,
            'observations': len(self._obs),
            'observation_interval_s': (None if self.interval_s() is None
                                       else round(self.interval_s(), 1)),
            'gap_threshold_s': round(thr, 1),
            'parameters': {'weight_shift_points': WEIGHT_SHIFT_POINTS,
                           'latency_step_ms': LATENCY_STEP_MS,
                           'latency_step_ratio': LATENCY_STEP_RATIO},
            'steering': steering,
            'bonds': bonds,
            'interfaces': ifaces,
            'quality': quality,
            'event_counts': dict(sorted(counts.items())),
            'events': events[:self._max_events],
            'events_truncated': len(events) > self._max_events,
            'notes': list(NOTES),
            '_events_all': events,
        }

    # -- steering ---------------------------------------------------------------------------
    def _steering(self, thr, events):
        rules = {}
        no_data = 0
        revisions = []
        last_defs = None
        prev_obs = None
        wan_prev = {}
        for o in self._obs:
            if o['defs'] is not None:
                v = o['defs']['version']
                if last_defs is None or v != last_defs:
                    revisions.append({
                        'version': v, 'first_observed_ts': o['ts'],
                        'attribution': o['defs']['attribution'],
                        'observed_as_changed': o['defs']['changed']})
                    if last_defs is not None and prev_obs is not None:
                        events.append(_event(
                            'steering_definition_revision', 'sdwan', o['ts'],
                            prev_obs['ts'], {'version_from': last_defs,
                                             'version_to': v},
                            frm=last_defs, to=v, thr=thr))
                last_defs = v
            sa = o['sa']
            if sa is None or sa.get('source_state') != 'ok':
                no_data += 1
            else:
                for r in sa.get('rules') or []:
                    if not isinstance(r, dict):
                        continue
                    key = r.get('rule_key') or str(r.get('rule') or '').lower()
                    if not key:
                        continue
                    a = rules.setdefault(key, self._new_rule(r))
                    a['samples'] += 1
                    pf = _num(r.get('preferred_flows'))
                    nf = _num(r.get('nonpreferred_flows'))
                    if pf is not None and r.get('preferred_flows_state',
                                                'value') == 'value':
                        a['pf'].add(pf)
                    if nf is not None and r.get('nonpreferred_flows_state',
                                                'value') == 'value':
                        a['nf'].add(nf)
                    if r.get('was_flow_matched') is True:
                        a['matched'] += 1
                    if sa.get('capture_restarted') is True:
                        a['capture_restarts'] += 1
                    if o['defs'] is not None:
                        vv = a['versions'].setdefault(
                            o['defs']['version'], [o['ts'], o['ts'], 0])
                        vv[1] = o['ts']
                        vv[2] += 1
                    a['apps_omitted_last'] = r.get('apps_omitted')
                    a['apps_total_last'] = r.get('apps_total')
                    for app in r.get('apps') or []:
                        if not isinstance(app, dict):
                            continue
                        ak = str(app.get('id') if app.get('id') is not None
                                 else '')
                        ap = a['apps'].setdefault(ak, {
                            'id': ak, 'appname': app.get('appname'),
                            'catname': app.get('catname'), 'samples': 0,
                            'matched': 0, 'pf': _Stat(), 'nf': _Stat(),
                            'in_def': app.get('in_rule_definition')})
                        ap['samples'] += 1
                        if app.get('matched') is True:
                            ap['matched'] += 1
                        p = _num(app.get('preferred_flows'))
                        n = _num(app.get('nonpreferred_flows'))
                        if p is not None:
                            ap['pf'].add(p)
                        if n is not None:
                            ap['nf'].add(n)
            if o['steer'] is not None:
                for r in o['steer']:
                    if not isinstance(r, dict):
                        continue
                    key = str(r.get('logical_name') or '').lower()
                    if not key:
                        continue
                    a = rules.setdefault(key, self._new_rule(
                        {'rule': r.get('logical_name')}))
                    w = r.get('preferred_wan_uid')
                    if w is not None:
                        a['wan'][w] = a['wan'].get(w, 0) + 1
                    pw = wan_prev.get(key)
                    if pw is not None and w is not None and pw != w \
                            and prev_obs is not None:
                        a['wan_changes'] += 1
                        events.append(_event(
                            'steering_preferred_wan_change', 'sdwan',
                            o['ts'], prev_obs['ts'],
                            {'rule': r.get('logical_name')}, frm=pw, to=w,
                            thr=thr))
                    if w is not None:
                        wan_prev[key] = w
            prev_obs = o
        rows = []
        for key, a in sorted(rules.items(), key=lambda kv: -kv[1]['samples'])[
                :MAX_RULES]:
            apps = sorted(a['apps'].values(), key=lambda x: -x['samples'])
            rows.append({
                'rule': a['name'], 'rule_key': key,
                'samples': a['samples'],
                'matched_samples': a['matched'],
                'preferred_flows': a['pf'].out(),
                'nonpreferred_flows': a['nf'].out(),
                'flow_values': 'gauge',
                'preferred_wan_samples': dict(sorted(a['wan'].items())),
                'preferred_wan_changes': a['wan_changes'],
                'definition_versions': [
                    {'version': v, 'first_observed_ts': x[0],
                     'last_observed_ts': x[1], 'samples': x[2]}
                    for v, x in sorted(a['versions'].items())],
                'capture_restarts_observed': a['capture_restarts'],
                'applications': [{
                    'id': p['id'], 'appname': p['appname'],
                    'catname': p['catname'], 'samples': p['samples'],
                    'matched_samples': p['matched'],
                    'preferred_flows': p['pf'].out(),
                    'nonpreferred_flows': p['nf'].out(),
                    'in_rule_definition': p['in_def']}
                    for p in apps[:MAX_APPS_PER_RULE]],
                'applications_truncated': len(apps) > MAX_APPS_PER_RULE,
                'zero_flow_unmatched_apps_listed': False,
                'apps_omitted_last_sample': a['apps_omitted_last'],
                'confidence': confidence(a['samples']),
            })
        return {'rules': rows, 'rules_truncated': len(rules) > MAX_RULES,
                'observations_without_steering_apps': no_data,
                'definition_revisions': revisions[:50]}

    @staticmethod
    def _new_rule(r):
        return {'name': r.get('rule'), 'samples': 0, 'matched': 0,
                'pf': _Stat(), 'nf': _Stat(), 'apps': {}, 'wan': {},
                'wan_changes': 0, 'versions': {}, 'capture_restarts': 0,
                'apps_omitted_last': None, 'apps_total_last': None}

    # -- bonds ------------------------------------------------------------------------------------
    def _bonds(self, thr, events):
        bonds = {}
        prev_members = {}
        prev_obs = None
        for o in self._obs:
            if o['wbond'] is None:
                continue
            for b in o['wbond']:
                if not isinstance(b, dict) or not b.get('iface'):
                    continue
                iface = b['iface']
                acc = bonds.setdefault(iface, {
                    'samples': 0, 'count': _Stat(),
                    'dl_flows': _Stat(), 'ul_flows': _Stat(),
                    'sources': {}, 'members': {}, 'modes': {}})
                acc['samples'] += 1
                mc = _num(b.get('member_count'))
                if mc is not None:
                    acc['count'].add(mc)
                for fld, st in (('total_download_flows', 'dl_flows'),
                                ('total_upload_flows', 'ul_flows')):
                    v = _num(b.get(fld))
                    if v is not None:
                        acc[st].add(v)
                for fld in ('total_download_flows_source',
                            'total_upload_flows_source'):
                    src = b.get(fld) or 'unknown'
                    k = '{}:{}'.format(fld.split('_')[1], src)
                    acc['sources'][k] = acc['sources'].get(k, 0) + 1
                mode = b.get('bonding_mode')
                if mode:
                    acc['modes'][mode] = acc['modes'].get(mode, 0) + 1
                cur = {}
                for m in b.get('members') or []:
                    if not isinstance(m, dict):
                        continue
                    mk = m.get('member_uid') or m.get('tunnel_iface')
                    if not mk:
                        continue
                    cur[mk] = m
                    ma = acc['members'].setdefault(mk, {
                        'member_uid': m.get('member_uid'),
                        'tunnel_iface': m.get('tunnel_iface'),
                        'overlay_uid': None, 'samples': 0,
                        'cw': _Stat(), 'dw': _Stat(), 'uw': _Stat(),
                        'dfl': _Stat(), 'ufl': _Stat(), 'loss': 0,
                        'links': {}, 'attach': {}, 'w_changes': 0,
                        'first': o['ts'], 'last': o['ts']})
                    ma['samples'] += 1
                    ma['last'] = o['ts']
                    if m.get('overlay_uid'):
                        ma['overlay_uid'] = m['overlay_uid']
                    for fld, st in (('configured_weight', 'cw'),
                                    ('current_download_weight', 'dw'),
                                    ('current_upload_weight', 'uw'),
                                    ('download_flows', 'dfl'),
                                    ('upload_flows', 'ufl')):
                        v = _num(m.get(fld))
                        if v is not None:
                            ma[st].add(v)
                    if m.get('has_loss'):
                        ma['loss'] += 1
                    for fld, st in (('link_state', 'links'),
                                    ('attach_state', 'attach')):
                        val = m.get(fld)
                        if val:
                            ma[st][val] = ma[st].get(val, 0) + 1
                before = prev_members.get(iface)
                if before is not None and prev_obs is not None:
                    self._bond_events(iface, before, cur, prev_obs['ts'],
                                      o['ts'], thr, events, acc)
                prev_members[iface] = cur
            prev_obs = o
        rows = []
        for iface, a in sorted(bonds.items()):
            members = sorted(a['members'].values(),
                             key=lambda x: str(x['member_uid']))
            rows.append({
                'iface': iface, 'samples': a['samples'],
                'member_count': a['count'].out(),
                'bonding_modes_seen': dict(sorted(a['modes'].items())),
                'total_download_flows': a['dl_flows'].out(),
                'total_upload_flows': a['ul_flows'].out(),
                'flow_total_sources': dict(sorted(a['sources'].items())),
                'flow_values': 'gauge',
                'members': [{
                    'member_uid': m['member_uid'],
                    'tunnel_iface': m['tunnel_iface'],
                    'overlay_uid': m['overlay_uid'],
                    'samples': m['samples'],
                    'first_seen_ts': m['first'], 'last_seen_ts': m['last'],
                    'runtime_config_weight': m['cw'].out(),
                    'download_weight': m['dw'].out(),
                    'upload_weight': m['uw'].out(),
                    'weight_values': 'ncos_control_values_not_traffic_share',
                    'runtime_weight_changes': m['w_changes'],
                    'download_flows': m['dfl'].out(),
                    'upload_flows': m['ufl'].out(),
                    'has_loss_samples': m['loss'],
                    'link_states': dict(sorted(m['links'].items())),
                    'attach_states': dict(sorted(m['attach'].items())),
                    'throughput': None,
                } for m in members[:MAX_MEMBERS]],
                'members_truncated': len(members) > MAX_MEMBERS,
                'membership_changes': a.get('changes', 0),
                'confidence': confidence(a['samples']),
            })
        return rows

    def _bond_events(self, iface, before, cur, t0, t1, thr, events, acc):
        for mk in sorted(set(cur) - set(before)):
            acc['changes'] = acc.get('changes', 0) + 1
            events.append(_event('bond_member_added', 'sdwan', t1, t0,
                                 {'bond': iface, 'member': mk},
                                 physical_uid=cur[mk].get('member_uid'),
                                 thr=thr))
        for mk in sorted(set(before) - set(cur)):
            acc['changes'] = acc.get('changes', 0) + 1
            events.append(_event('bond_member_removed', 'sdwan', t1, t0,
                                 {'bond': iface, 'member': mk},
                                 physical_uid=before[mk].get('member_uid'),
                                 thr=thr))
        for mk in sorted(set(cur) & set(before)):
            a, b = before[mk], cur[mk]
            uid = b.get('member_uid')
            for fld, label in (('link_state', 'link_state'),
                               ('attach_state', 'attach_state')):
                if a.get(fld) != b.get(fld) and (a.get(fld) or b.get(fld)):
                    events.append(_event(
                        'bond_member_state_change', 'sdwan', t1, t0,
                        {'bond': iface, 'member': mk, 'field': label},
                        physical_uid=uid, frm=a.get(fld), to=b.get(fld),
                        thr=thr))
            shifted = False
            changed = False
            for fld in ('configured_weight', 'current_download_weight',
                        'current_upload_weight'):
                x, y = _num(a.get(fld)), _num(b.get(fld))
                if x is None or y is None or x == y:
                    continue
                changed = True
                if abs(y - x) >= WEIGHT_SHIFT_POINTS:
                    shifted = True
                    events.append(_event(
                        'bond_runtime_weight_shift', 'sdwan', t1, t0,
                        {'bond': iface, 'member': mk,
                         'field': {'configured_weight':
                                   'runtime_config_weight',
                                   'current_download_weight':
                                   'download_weight',
                                   'current_upload_weight':
                                   'upload_weight'}[fld],
                         'threshold_points': WEIGHT_SHIFT_POINTS,
                         'note': 'control value, not traffic share'},
                        physical_uid=uid, frm=x, to=y, thr=thr))
            if changed or shifted:
                acc['members'][mk]['w_changes'] += 1

    # -- interfaces --------------------------------------------------------------------------------
    def _interfaces(self, thr, events):
        series = {}
        states = {}
        prev_obs = None
        for o in self._obs:
            if o['ifs'] is None:
                continue
            for r in o['ifs']:
                uid = r.get('uid')
                if not uid:
                    continue
                series.setdefault(uid, []).append(r)
                cs = r.get('connection_state')
                if cs is None:
                    continue
                last = states.get(uid)
                if last is not None and last != cs and prev_obs is not None:
                    events.append(_event(
                        'interface_state_change', 'sdwan', o['ts'],
                        prev_obs['ts'],
                        {'interface': uid,
                         'layer': r.get('object_class')},
                        physical_uid=(uid if r.get('object_class') ==
                                      'physical' else None),
                        frm=last, to=cs, thr=thr))
                states[uid] = cs
            prev_obs = o
        rows, by_uid = [], {}
        for uid, recs in sorted(series.items()):
            cls = next((r.get('object_class') for r in recs
                        if r.get('object_class')), None)
            row = {'uid': uid, 'layer': cls, 'additive': False,
                   'observations': len(recs)}
            for fld, name in (('rx_bytes', 'rx'), ('tx_bytes', 'tx')):
                rates, ok, excl = [], 0, {}
                tot_b = tot_t = 0.0
                for a, b in zip(recs, recs[1:]):
                    res = counters.byte_rate(a, b, fld, thr)
                    if res['status'] == counters.STATUS_OK:
                        ok += 1
                        rates.append(res['mbps'])
                        tot_b += res['delta']
                        tot_t += res['interval_s']
                    else:
                        excl[res['status']] = excl.get(res['status'], 0) + 1
                row[name] = {
                    'intervals_used': ok,
                    'intervals_excluded': dict(sorted(excl.items())),
                    'avg_mbps': (round(tot_b * 8 / tot_t / 1e6, 3)
                                 if tot_t > 0 else None),
                    'peak_mbps': round(max(rates), 3) if rates else None,
                    'p95_mbps': (round(_pct(rates, 95), 3)
                                 if rates else None),
                    'last_mbps': round(rates[-1], 3) if rates else None,
                    'rate_basis': 'delta bytes / interval between polls '
                                  '(average over the interval, not '
                                  'instantaneous)'}
            by_uid[uid] = row
            rows.append(row)
        rows.sort(key=lambda r: (str(r['layer']), r['uid']))
        self._if_by_uid = by_uid
        return {'by_interface': rows[:MAX_INTERFACES],
                'truncated': len(rows) > MAX_INTERFACES,
                'layers_note': 'physical, sdwan and wbond rows are '
                               'different layers of the same traffic and '
                               'are not additive'}

    def _attach_member_throughput(self, bonds, ifaces):
        by_uid = getattr(self, '_if_by_uid', {})
        for b in bonds:
            for m in b['members']:
                phys = by_uid.get(m['member_uid'])
                over = by_uid.get(m['overlay_uid']) if m['overlay_uid'] \
                    else None
                m['throughput'] = {
                    'physical_layer': phys and {
                        'uid': phys['uid'], 'rx': phys['rx'],
                        'tx': phys['tx']},
                    'overlay_layer': over and {
                        'uid': over['uid'], 'rx': over['rx'],
                        'tx': over['tx']},
                    'additive': False,
                    'note': 'layers are separate views of the same traffic; '
                            'overlay_layer is null when the tunnel '
                            'interface does not name exactly one device'}

    # -- quality ----------------------------------------------------------------------------------------
    def _quality(self, thr, events):
        qoe, prev = {}, {}
        sw = {}
        prev_obs = None
        for o in self._obs:
            if o['qoe'] is not None:
                for r in o['qoe']:
                    uid = r.get('uid')
                    if not uid:
                        continue
                    a = qoe.setdefault(uid, {
                        'samples': 0, 'avg': _Stat(), 'last': _Stat(),
                        'excl': {}, 'sentinels': 0, 'loss_raw': 0})
                    a['samples'] += 1
                    val = r.get('validity') or {}
                    for fld, st, vk in (('latency_avg_ms', 'avg',
                                         'latency_avg'),
                                        ('latency_last_ms', 'last',
                                         'latency_last')):
                        v = _num(r.get(fld))
                        vd = val.get(vk, 'valid')
                        if v is not None and vd == 'valid':
                            a[st].add(v)
                        elif vd != 'valid':
                            k = '{}:{}'.format(vk, vd)
                            a['excl'][k] = a['excl'].get(k, 0) + 1
                    if r.get('pkt_loss_sentinel_candidates'):
                        a['sentinels'] += 1
                    if r.get('pkt_loss_stats_raw'):
                        a['loss_raw'] += 1
                    cur = _num(r.get('latency_avg_ms'))
                    p = prev.get(uid)
                    if (cur is not None and p is not None
                            and val.get('latency_avg', 'valid') == 'valid'
                            and prev_obs is not None):
                        big = abs(cur - p) >= LATENCY_STEP_MS and (
                            (p > 0 and (cur / p >= LATENCY_STEP_RATIO
                                        or cur / p <= 1.0 / LATENCY_STEP_RATIO))
                            or p == 0)
                        if big:
                            events.append(_event(
                                'qoe_latency_step', 'sdwan', o['ts'],
                                prev_obs['ts'],
                                {'path': uid, 'heuristic': {
                                    'min_step_ms': LATENCY_STEP_MS,
                                    'ratio': LATENCY_STEP_RATIO}},
                                frm=p, to=cur, thr=thr))
                    if cur is not None and val.get(
                            'latency_avg', 'valid') == 'valid':
                        prev[uid] = cur
            if o['swans'] is not None:
                for r in o['swans']:
                    uid = r.get('uid')
                    if not uid:
                        continue
                    a = sw.setdefault(uid, {'samples': 0, 'lat': _Stat(),
                                            'jit': _Stat()})
                    a['samples'] += 1
                    for fld, st in (('lat', 'lat'), ('jit', 'jit')):
                        v = _num(r.get(fld))
                        if v is not None:
                            a[st].add(v)
            prev_obs = o
        return {
            'qoe_paths': [{
                'uid': uid, 'samples': a['samples'],
                'latency_avg_ms': a['avg'].out(),
                'latency_last_ms': a['last'].out(),
                'excluded': dict(sorted(a['excl'].items())),
                'loss_sentinel_candidate_samples': a['sentinels'],
                'raw_loss_counters_present_samples': a['loss_raw'],
                'packet_loss_percentage': None,
                'packet_loss_note': 'not computed: counter semantics are '
                                    'not verified',
                'confidence': confidence(a['samples'])}
                for uid, a in sorted(qoe.items())],
            'swans_paths': [{
                'uid': uid, 'samples': a['samples'],
                'latency_ms': a['lat'].out(), 'jitter_ms': a['jit'].out(),
                'confidence': confidence(a['samples'])}
                for uid, a in sorted(sw.items())],
        }


# --- correlation -------------------------------------------------------------------------------------

_CELL_TYPES = ('serving_cell_change', 'radio_mode_change',
               'ca_composition_change', 'ca_bandwidth_change',
               'connection_state_change', 'modem_missing',
               'observation_gap', 'health_category_change')


def correlate(cell_events, sdwan_events, interval_s, window_s=None,
              span_s=None, max_pairs=MAX_PAIRS):
    """Time-aligned cellular and SD-WAN events (evidence, not cause)."""
    iv = interval_s or 60
    win = window_s if window_s is not None else max(2 * iv, 120)
    win = min(max(win, iv), MAX_CORRELATION_WINDOW_S)
    cells = [e for e in cell_events if e['type'] in _CELL_TYPES]
    pairs = []
    for c in cells:
        for s in sdwan_events:
            delta = s['first_observed_ts'] - c['first_observed_ts']
            if abs(delta) > win:
                continue
            same_poll = (c['detected_between'] == s['detected_between'])
            same_path = bool(s.get('physical_uid')
                             and s['physical_uid'] == c['uid'])
            if same_poll and same_path:
                level = 'A_same_poll_same_path'
            elif same_poll:
                level = 'B_same_poll'
            elif same_path:
                level = 'C_adjacent_polls_same_path'
            else:
                level = 'D_adjacent_polls'
            pairs.append({
                'evidence': level,
                'relation': 'temporal_coincidence',
                'delta_s': int(delta),
                'same_path': same_path,
                'cellular_event': {k: c[k] for k in (
                    'type', 'uid', 'first_observed_ts', 'detected_between',
                    'from', 'to', 'confidence')},
                'sdwan_event': {k: s[k] for k in (
                    'type', 'first_observed_ts', 'detected_between',
                    'physical_uid', 'from', 'to', 'confidence', 'detail')},
                'cause_asserted': False})
    pairs.sort(key=lambda p: (p['evidence'], p['cellular_event'][
        'first_observed_ts'], p['sdwan_event']['type']))
    summary = {}
    for p in pairs:
        k = '{}+{}'.format(p['cellular_event']['type'],
                           p['sdwan_event']['type'])
        e = summary.setdefault(k, {})
        e[p['evidence']] = e.get(p['evidence'], 0) + 1
    hours = (span_s / 3600.0) if span_s and span_s > 0 else None

    def rates(evs):
        out = {}
        for e in evs:
            out[e['type']] = out.get(e['type'], 0) + 1
        return {k: {'count': v, 'per_hour': (round(v / hours, 2)
                                             if hours else None)}
                for k, v in sorted(out.items())}
    return {
        'window_s': int(win),
        'pairs': pairs[:max_pairs],
        'pairs_total': len(pairs),
        'pairs_truncated': len(pairs) > max_pairs,
        'summary': dict(sorted(summary.items())),
        'event_rates_per_hour': {'cellular': rates(cells),
                                 'sdwan': rates(sdwan_events)},
        'notes': list(CORRELATION_NOTES),
    }
