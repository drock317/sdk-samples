"""Cellular engineering analytics over stored observations (v0.8.4).

Pure and bounded: the caller feeds already-decoded observations one at a time
(`CellularAnalyzer.add`) and asks for a result. Nothing is persisted or
cached, nothing touches the router, and no raw identifier is read: serving
cells are the privacy-preserving `cell_key_hash`.

Adapted from Speedtest Analyzer's cellular analysis (serving-cell
distribution, LTE-vs-NR usage, per-cell RF statistics, component continuity
`_components_match`, configuration-change detection). Not carried over:
test phases, geography, map/contribution features. Unlike Speedtest, every
RF statistic here counts only readings whose validity is `valid` and reports
what was excluded and why.

Time semantics (important)
* Observations are polls. A change seen between two polls is reported as
  `detected_between [t_prev, t_cur]` with `uncertainty_s` = the poll interval
  and `exact_time: false`. No handover time is ever claimed.
* `first_observed_ts` is the poll at which the new state was first seen.
* Time between two polls is attributed to the EARLIER poll's state
  (`observed_seconds`) only when the polls are not further apart than the gap
  threshold; longer silences are listed as gaps and attribute nothing.
"""
from . import cellular
from . import rf_validity as RV

SCHEMA_VERSION = 1
MIN_GAP_S = 150
GAP_FACTOR = 2.5
MAX_CELLS = 50
MAX_COMPONENTS_PER_CELL = 12
MAX_GAPS = 50
DEFAULT_MAX_EVENTS = 200

SERVING_METRICS = ('rsrp', 'rsrq', 'sinr')
_NR_NAMES = ('rsrp_5g', 'rsrq_5g', 'sinr_5g')

NOTES = (
    'Observations are polls. A change is reported as detected_between two '
    'polls with exact_time false; no handover time is claimed and cells or '
    'carriers that came and went between two polls are not seen.',
    'observed_seconds attributes the interval after a poll to that poll\'s '
    'state only when the next poll is within the gap threshold.',
    'RF statistics use only readings whose validity is valid; placeholder, '
    'implausible, missing and unknown-scale readings are counted in '
    'excluded, never averaged. Records written before v0.8.4 are validated '
    'against plausibility ranges when read.',
    'A cell is identified by the privacy-preserving cell_key_hash; samples '
    'without a usable cell id are grouped as unknown.',
    'Health score, category and signal strength are NCOS-reported values.',
)


# --- small helpers -------------------------------------------------------------

class _Stat(object):
    __slots__ = ('n', 'total', 'lo', 'hi', 'last')

    def __init__(self):
        self.n, self.total, self.lo, self.hi, self.last = 0, 0.0, None, None, None

    def add(self, v):
        self.n += 1
        self.total += v
        self.lo = v if self.lo is None or v < self.lo else self.lo
        self.hi = v if self.hi is None or v > self.hi else self.hi
        self.last = v

    def out(self):
        if not self.n:
            return {'count': 0, 'avg': None, 'min': None, 'max': None}
        return {'count': self.n, 'avg': round(self.total / self.n, 1),
                'min': round(self.lo, 1), 'max': round(self.hi, 1)}


def confidence(samples, attributed_fraction=1.0):
    """Sample-count confidence label (documented thresholds)."""
    if samples < 5:
        level = 'low'
    elif samples < 30:
        level = 'medium'
    else:
        level = 'high'
    if attributed_fraction < 0.5 and level == 'high':
        level = 'medium'
    return level


def median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def components_match(prev, cur):
    """Do two observations describe the same radio component?

    Adapted from Speedtest `_components_match`: RAT and band must agree and
    at least one explicit shared discriminator (channel or PCI) must match;
    a conflicting channel or PCI rejects the match. Role and bandwidth are
    excluded: roles move and bandwidth is component state.
    """
    if (prev['rat'], prev['band']) != (cur['rat'], cur['band']):
        return False
    pc, cc = prev.get('channel'), cur.get('channel')
    pp, cp_ = prev.get('pci'), cur.get('pci')
    if pc and cc and pc != cc:
        return False
    if pp and cp_ and pp != cp_:
        return False
    return bool((pc and cc and pc == cc) or (pp and cp_ and pp == cp_))


def match_components(prev, cur):
    """Greedy one-to-one matching -> (pairs, removed, added)."""
    unmatched_cur = list(range(len(cur)))
    pairs, removed = [], []
    for p in prev:
        hit = None
        for i in unmatched_cur:
            if components_match(p, cur[i]):
                hit = i
                break
        if hit is None:
            removed.append(p)
        else:
            unmatched_cur.remove(hit)
            pairs.append((p, cur[hit]))
    return pairs, removed, [cur[i] for i in unmatched_cur]


def _band_key(band):
    rat, ident = cellular._parse_band_name(band)
    return ident if ident else str(band or '').strip().upper()


def _component(c):
    rat = str(c.get('rat') or '').upper()
    return {'rat': rat, 'band': _band_key(c.get('band')),
            'channel': (str(c['channel']).strip()
                        if c.get('channel') not in (None, '') else None),
            'pci': (str(c['pci']).strip()
                    if c.get('pci') not in (None, '') else None),
            'bandwidth_mhz': c.get('bandwidth_mhz'),
            'role': c.get('role'),
            'aliases': tuple(c.get('role_aliases') or
                             ([c['role']] if c.get('role') else [])),
            'rf': c.get('rf')}


def _ident(comp):
    return '{}:{}:{}'.format(comp['rat'], comp['band'],
                             comp['channel'] or '?')


def _rf_value(rec, name, rat):
    """(value, validity) of one serving metric; legacy records are
    validated against the plausibility range here."""
    val = rec.get(name)
    if rec.get('rf_version') == cellular.RF_VERSION:
        vd = (rec.get('rf_validity') or {}).get(name)
        if vd is None:
            vd = RV.VALID if val is not None else RV.MISSING
        return (val if vd == RV.VALID else None), vd
    if val is None:
        return None, RV.MISSING
    metric = name.replace('_5g', '')
    if not RV.in_range(metric, val, rat):
        return None, RV.IMPLAUSIBLE
    return val, RV.VALID


def _component_rf(comp, metric):
    rf = comp.get('rf')
    if not isinstance(rf, dict):
        return None, None
    val = rf.get(metric)
    vd = (rf.get('validity') or {}).get(metric)
    if vd is None:
        vd = RV.VALID if val is not None else RV.MISSING
    if vd == RV.VALID and val is not None and not RV.in_range(
            metric, val, comp['rat']):
        return None, RV.IMPLAUSIBLE       # legacy value that is impossible
    if vd == 'ambiguous_scale':
        vd = RV.UNKNOWN_SCALE
    return (val if vd == RV.VALID else None), vd


class _Excl(object):
    """Counts of non-valid readings per metric."""

    def __init__(self):
        self.d = {}

    def add(self, metric, validity):
        if validity in (RV.VALID, None):
            return
        m = self.d.setdefault(metric, {})
        m[validity] = m.get(validity, 0) + 1

    def out(self):
        return {k: dict(sorted(v.items())) for k, v in sorted(self.d.items())}


# --- sample extraction -----------------------------------------------------------

def _sample(ts, rec):
    ctx = rec.get('context') or {}
    comps = [_component(c) for c in rec.get('carriers') or []
             if isinstance(c, dict)]
    health = rec.get('health') if isinstance(rec.get('health'), dict) else {}
    mode = rec.get('service_mode') or None
    sa = mode == '5G SA'
    serving = {}
    for name in SERVING_METRICS + _NR_NAMES:
        rat = 'NR' if (name in _NR_NAMES or sa) else 'LTE'
        serving[name] = _rf_value(rec, name, rat)
    return {
        'ts': ts, 'state': 'present',
        'conn': ctx.get('connection_state'),
        'mode': mode,
        'cell': rec.get('serving_cell_hash') or rec.get('cell_key_hash'),
        # v0.8.3 hashes include the radio mode: a mode change alone moves it.
        'cell_basis': ('serving_cell_hash' if rec.get('serving_cell_hash')
                       else 'cell_key_hash_legacy'),
        'comps': comps,
        'nr': any(c['rat'] == 'NR' for c in comps),
        'lte': any(c['rat'] == 'LTE' for c in comps),
        'mhz': rec.get('total_active_mhz'),
        'serving': serving,
        'health': {'category': health.get('category'),
                   'score': health.get('score'),
                   'signal': health.get('signal_strength')},
        'v2': rec.get('rf_version') == cellular.RF_VERSION,
    }


# --- analyzer -------------------------------------------------------------------

class CellularAnalyzer(object):
    """Accumulates compact per-modem samples, then analyzes them."""

    def __init__(self, uid=None, interval_hint_s=None,
                 max_events=DEFAULT_MAX_EVENTS):
        self._uid = uid
        self._hint = interval_hint_s
        self._max_events = max(1, int(max_events))
        self._times = []                  # every observation ts
        self._by_uid = {}                 # uid -> [samples]
        self._no_cell_section = 0
        self._out_of_order = 0

    def add(self, obs):
        ts = obs.get('collected_ts')
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            return
        if self._times and ts < self._times[-1]:
            self._out_of_order += 1
        self._times.append(ts)
        cell = obs.get('cellular')
        if not isinstance(cell, list):
            self._no_cell_section += 1       # section not collected
            for lst in self._by_uid.values():
                lst.append({'ts': ts, 'state': 'not_collected'})
            return
        seen = set()
        for rec in cell:
            if not isinstance(rec, dict) or not rec.get('uid'):
                continue
            uid = rec['uid']
            if self._uid and uid != self._uid:
                continue
            seen.add(uid)
            lst = self._by_uid.setdefault(uid, [])
            lst.append(_sample(ts, rec))
        for uid, lst in self._by_uid.items():
            if uid not in seen:
                lst.append({'ts': ts, 'state': 'absent'})

    # -- interval ------------------------------------------------------------
    def interval_s(self):
        d = [b - a for a, b in zip(self._times, self._times[1:]) if b > a]
        est = median(d)
        if est is None:
            return self._hint
        return max(est, self._hint) if self._hint else est

    def gap_threshold_s(self):
        iv = self.interval_s()
        return max(MIN_GAP_S, GAP_FACTOR * iv) if iv else MIN_GAP_S

    # -- analysis ---------------------------------------------------------------
    def result(self, window=None):
        thr = self.gap_threshold_s()
        gaps = []
        for a, b in zip(self._times, self._times[1:]):
            if b - a > thr:
                gaps.append({'start_ts': a, 'end_ts': b,
                             'seconds': int(b - a)})
        modems = [self._modem(uid, lst, thr)
                  for uid, lst in sorted(self._by_uid.items())]
        out = {
            'schema_version': SCHEMA_VERSION,
            'window': dict(window or {}),
            'observations': len(self._times),
            'observations_without_cellular_section': self._no_cell_section,
            'out_of_order_observations': self._out_of_order,
            'observation_interval_s': (None if self.interval_s() is None
                                       else round(self.interval_s(), 1)),
            'gap_threshold_s': round(thr, 1),
            'gaps': gaps[:MAX_GAPS],
            'gaps_truncated': len(gaps) > MAX_GAPS,
            'modems': modems,
            'notes': list(NOTES),
        }
        out['window'].update(
            first_observation_ts=self._times[0] if self._times else None,
            last_observation_ts=self._times[-1] if self._times else None)
        return out

    def events(self):
        """All detected events (for correlation), oldest first."""
        thr = self.gap_threshold_s()
        evs = []
        for uid, lst in self._by_uid.items():
            evs.extend(self._events(uid, lst, thr))
        evs.sort(key=lambda e: (e['first_observed_ts'], e['uid'], e['type']))
        return evs

    # -- per modem ------------------------------------------------------------------
    def _modem(self, uid, samples, thr):
        present = [s for s in samples if s['state'] == 'present']
        absent = sum(1 for s in samples if s['state'] == 'absent')
        notcol = sum(1 for s in samples if s['state'] == 'not_collected')
        cells = {}
        modes = {}
        excl = _Excl()
        attributed = 0.0
        total_span = 0.0
        health = {}
        health_scores, health_sig = _Stat(), _Stat()
        lte_only = nr_present = 0
        for i, s in enumerate(samples):
            if i + 1 < len(samples):
                dt = samples[i + 1]['ts'] - s['ts']
            else:
                dt = 0.0
            ok_dt = dt if 0 < dt <= thr else 0.0
            total_span += max(dt, 0.0)
            if s['state'] != 'present':
                continue
            attributed += ok_dt
            key = s['cell'] or 'unknown'
            c = cells.setdefault(key, {
                'samples': 0, 'seconds': 0.0, 'first': s['ts'],
                'last': s['ts'], 'modes': {}, 'bands': {},
                'rf': {n: _Stat() for n in SERVING_METRICS + _NR_NAMES},
                'excl': _Excl(), 'comps': {}, 'basis': s['cell_basis']})
            c['samples'] += 1
            c['seconds'] += ok_dt
            c['last'] = s['ts']
            m = s['mode'] or 'unknown'
            c['modes'][m] = c['modes'].get(m, 0) + 1
            mm = modes.setdefault(m, {'samples': 0, 'seconds': 0.0})
            mm['samples'] += 1
            mm['seconds'] += ok_dt
            primary = [x for x in s['comps'] if 'pcell' in x['aliases']]
            if primary:
                b = '{}:{}'.format(primary[0]['rat'], primary[0]['band'])
                c['bands'][b] = c['bands'].get(b, 0) + 1
            for name, (val, vd) in s['serving'].items():
                if val is not None:
                    c['rf'][name].add(val)
                else:
                    c['excl'].add(name, vd)
                    excl.add(name, vd)
            for comp in s['comps']:
                ci = c['comps'].setdefault(_ident(comp), {
                    'rat': comp['rat'], 'band': comp['band'],
                    'channel': comp['channel'], 'samples': 0,
                    'roles': set(), 'bandwidths': set(),
                    'rf': {n: _Stat() for n in SERVING_METRICS},
                    'excl': _Excl()})
                ci['samples'] += 1
                ci['roles'].update(comp['aliases'])
                if comp['bandwidth_mhz'] is not None:
                    ci['bandwidths'].add(comp['bandwidth_mhz'])
                for metric in SERVING_METRICS:
                    val, vd = _component_rf(comp, metric)
                    if val is not None:
                        ci['rf'][metric].add(val)
                    elif vd is not None:
                        ci['excl'].add(metric, vd)
                        excl.add('component_' + metric, vd)
            if s['nr']:
                nr_present += 1
            else:
                lte_only += 1
            hc = s['health']['category']
            if hc:
                h = health.setdefault(hc, {'samples': 0, 'seconds': 0.0})
                h['samples'] += 1
                h['seconds'] += ok_dt
            if s['health']['score'] is not None:
                health_scores.add(s['health']['score'])
            if s['health']['signal'] is not None:
                health_sig.add(s['health']['signal'])
        n_present = len(present)
        cell_rows = []
        for key, c in sorted(cells.items(), key=lambda kv: -kv[1]['samples']):
            comps = []
            for ident, ci in sorted(c['comps'].items(),
                                    key=lambda kv: -kv[1]['samples']):
                comps.append({
                    'identity': ident, 'rat': ci['rat'], 'band': ci['band'],
                    'channel': ci['channel'], 'samples': ci['samples'],
                    'role_aliases_seen': sorted(ci['roles']),
                    'bandwidths_mhz': sorted(ci['bandwidths']),
                    'rf': {k: v.out() for k, v in ci['rf'].items()},
                    'excluded': ci['excl'].out()})
            frac = (c['seconds'] / (c['samples'] * (self.interval_s() or 1))
                    if c['samples'] and self.interval_s() else 1.0)
            cell_rows.append({
                'cell_key_hash': None if key == 'unknown' else key,
                'identity_basis': c['basis'],
                'samples': c['samples'],
                'share_of_samples': round(c['samples'] / float(n_present), 3)
                if n_present else None,
                'observed_seconds': int(c['seconds']),
                'first_seen_ts': c['first'], 'last_seen_ts': c['last'],
                'service_modes': dict(sorted(c['modes'].items())),
                'primary_bands': dict(sorted(c['bands'].items())),
                'serving_rf': {k: v.out() for k, v in c['rf'].items()
                               if v.n or k in SERVING_METRICS},
                'excluded': c['excl'].out(),
                'components': comps[:MAX_COMPONENTS_PER_CELL],
                'components_truncated': len(comps) > MAX_COMPONENTS_PER_CELL,
                'confidence': confidence(c['samples'], min(1.0, frac)),
            })
        tech_rows = {k: {'samples': v['samples'],
                         'observed_seconds': int(v['seconds']),
                         'share_of_samples': round(
                             v['samples'] / float(n_present), 3)
                         if n_present else None}
                     for k, v in sorted(modes.items())}
        events = self._events(uid, samples, thr)
        counts = {}
        for e in events:
            counts[e['type']] = counts.get(e['type'], 0) + 1
        return {
            'uid': uid,
            'data_quality': {
                'samples': len(samples), 'present': n_present,
                'modem_absent': absent, 'section_not_collected': notcol,
                'records_with_v084_validity': sum(1 for s in present
                                                  if s['v2']),
                'legacy_records': sum(1 for s in present if not s['v2']),
                'observed_seconds': int(attributed),
                'unattributed_seconds': int(max(0.0, total_span - attributed)),
            },
            'serving_cells': cell_rows[:MAX_CELLS],
            'serving_cells_truncated': len(cell_rows) > MAX_CELLS,
            'technology_usage': {
                'by_service_mode': tech_rows,
                'nr_carrier_present_samples': nr_present,
                'lte_only_samples': lte_only,
            },
            'health': {
                'by_category': {k: {'samples': v['samples'],
                                    'observed_seconds': int(v['seconds'])}
                                for k, v in sorted(health.items())},
                'score': health_scores.out(),
                'signal_strength': health_sig.out(),
                'samples_with_health': sum(
                    v['samples'] for v in health.values()),
            },
            'excluded': excl.out(),
            'event_counts': dict(sorted(counts.items())),
            'events': events[:self._max_events],
            'events_truncated': len(events) > self._max_events,
            'confidence': confidence(n_present),
        }

    # -- transitions -----------------------------------------------------------------
    def _events(self, uid, samples, thr):
        evs = []

        def emit(kind, prev, cur, frm=None, to=None, detail=None):
            dt = cur['ts'] - prev['ts']
            across = dt > thr
            evs.append({
                'type': kind, 'uid': uid,
                'first_observed_ts': cur['ts'],
                'detected_between': [prev['ts'], cur['ts']],
                'uncertainty_s': int(dt),
                'time_resolution': 'between_polls', 'exact_time': False,
                'across_gap': across,
                'confidence': 'low' if across else 'normal',
                'from': frm, 'to': to, 'detail': detail or {}})

        last_present = None
        pending_absent = None
        for s in samples:
            if s['state'] == 'not_collected':
                continue
            if s['state'] == 'absent':
                if pending_absent is None and last_present is not None:
                    pending_absent = s
                continue
            if last_present is None:
                last_present = s
                pending_absent = None
                continue
            prev = last_present
            if pending_absent is not None:
                emit('modem_missing', prev, s, 'present', 'present_again',
                     {'absent_since_poll': pending_absent['ts']})
                pending_absent = None
            elif s['ts'] - prev['ts'] > thr:
                emit('observation_gap', prev, s, None, None,
                     {'seconds': int(s['ts'] - prev['ts'])})
            if prev['conn'] != s['conn'] and (prev['conn'] or s['conn']):
                emit('connection_state_change', prev, s, prev['conn'],
                     s['conn'])
            if prev['mode'] != s['mode'] and (prev['mode'] or s['mode']):
                emit('radio_mode_change', prev, s, prev['mode'], s['mode'])
            if prev['cell'] and s['cell'] and prev['cell'] != s['cell']:
                legacy = 'cell_key_hash_legacy' in (prev['cell_basis'],
                                                   s['cell_basis'])
                if legacy and prev['mode'] != s['mode']:
                    # The legacy hash includes the radio mode, so this may be
                    # the same cell. Reported, but not as a normal change.
                    emit('serving_cell_change', prev, s, prev['cell'],
                         s['cell'], {'ambiguous_with_mode_change': True,
                                     'note': 'pre-v0.8.4 hash includes the '
                                     'radio mode'})
                    evs[-1]['confidence'] = 'low'
                else:
                    emit('serving_cell_change', prev, s, prev['cell'],
                         s['cell'], {'note': 'cells that came and went '
                                     'between polls are not seen'})
            pairs, removed, added = match_components(prev['comps'],
                                                     s['comps'])
            if removed or added:
                emit('ca_composition_change', prev, s,
                     sorted(_ident(c) for c in prev['comps']),
                     sorted(_ident(c) for c in s['comps']),
                     {'removed': sorted(_ident(c) for c in removed),
                      'added': sorted(_ident(c) for c in added)})
            bw = [{'identity': _ident(b),
                   'from_mhz': a['bandwidth_mhz'], 'to_mhz': b['bandwidth_mhz']}
                  for a, b in pairs
                  if a['bandwidth_mhz'] is not None
                  and b['bandwidth_mhz'] is not None
                  and a['bandwidth_mhz'] != b['bandwidth_mhz']]
            if bw:
                emit('ca_bandwidth_change', prev, s, prev['mhz'], s['mhz'],
                     {'components': bw})
            roles = [{'identity': _ident(b), 'from': list(a['aliases']),
                      'to': list(b['aliases'])}
                     for a, b in pairs if set(a['aliases']) != set(b['aliases'])]
            if roles:
                emit('component_role_change', prev, s, None, None,
                     {'components': roles, 'significance': 'minor'})
            pc, sc = prev['health']['category'], s['health']['category']
            if pc and sc and pc != sc:
                emit('health_category_change', prev, s, pc, sc,
                     {'score_from': prev['health']['score'],
                      'score_to': s['health']['score']})
            last_present = s
        if pending_absent is not None and last_present is not None:
            emit('modem_missing', last_present, pending_absent, 'present',
                 'absent', {'absent_since_poll': pending_absent['ts'],
                            'still_absent_at_window_end': True})
        return evs
