"""Read-only interface throughput analytics (v0.6.0).

GET /api/analytics/interfaces: calculated RX/TX rates for ONE interface,
computed on demand from the stored historical observations with the existing
counters.py math. Nothing is persisted, cached or written back, and no raw
record is modified.

Rules (docs/analytics/counter-derivation.md):

* One interface identity per request (`uid` is required). Physical, sdwan and
  wbond interfaces are separate identities and are never summed.
* Rates are computed only between CONSECUTIVE observations. An observation
  with no record for the interface (failed read, interface absent) is not
  skipped: the pair that touches it gets status missing_value (current) or
  missing_baseline (previous). There is no bridging and no zero fill.
* Observations written by v0.4.4 and earlier whose `collector_errors` names
  `wan_devices` may carry re-stamped counters from a failed read. A pair that
  touches one is reported as `suspect_input` with no rate.
* `max_gap_s` is an optional caller policy; there is no default threshold.
* Pagination reuses the history store cursor. The page's last observation is
  carried inside the cursor as the baseline for the next page, so the pair
  across a page boundary is computed instead of lost.

Python 3.8 compatible.
"""
import re

from . import counters
from . import query as Q

STATUS_SUSPECT_INPUT = 'suspect_input'
WAN_READ_COLLECTOR = 'wan_devices'
MAX_FETCH_CALLS = 4               # bounded store reads per HTTP request
MAX_CURSOR_CHARS = 1024           # cursor carries a small baseline record

_UID_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,96}\Z')
_RATE_FIELDS = ('rx_bytes', 'tx_bytes')


def parse_params(raw_query):
    """-> dict(uid, limit, start, end, max_gap_s, cursor), all validated."""
    q = Q.split_query(raw_query, ('uid', 'limit', 'start', 'end',
                                  'max_gap_s', 'cursor'))
    uid = q.get('uid')
    if uid is None or not _UID_RE.match(uid):
        raise Q.bad('uid is required (an interface uid such as '
                    'ethernet-wan)')
    gap = Q.parse_ts('max_gap_s', q.get('max_gap_s'))
    if gap is not None and gap <= 0:
        raise Q.bad('max_gap_s must be greater than 0')
    cursor = q.get('cursor')
    if cursor is not None and (not cursor or len(cursor) > MAX_CURSOR_CHARS
                               or not Q._TOKEN_RE.match(cursor)):
        raise Q.bad('invalid cursor')
    out = {'uid': uid, 'limit': Q.parse_limit(q.get('limit')),
           'start': Q.parse_ts('start', q.get('start')),
           'end': Q.parse_ts('end', q.get('end')),
           'max_gap_s': gap, 'cursor': cursor}
    if (out['start'] is not None and out['end'] is not None
            and out['start'] > out['end']):
        raise Q.bad('start must not be after end')
    return out


# --- observation -> comparable point -------------------------------------------

def _suspect(obs):
    errs = obs.get('collector_errors')
    if not isinstance(errs, list):
        return False
    return any(isinstance(e, dict) and e.get('collector') == WAN_READ_COLLECTOR
               for e in errs)


def _point(obs, uid):
    """{'rec': interface record or None, 'ts': int|None, 'suspect': bool}."""
    rec = None
    for r in (obs.get('interfaces') or []):
        if isinstance(r, dict) and r.get('uid') == uid:
            rec = r
            break
    return {'rec': rec, 'ts': obs.get('collected_ts'),
            'suspect': _suspect(obs)}


def _pack(pt):
    """Baseline for the cursor (only what counters.py reads)."""
    r = pt['rec']
    rec = None if r is None else [r.get('object_class'),
                                  r.get('collected_ts'),
                                  r.get('collected_at'),
                                  r.get('rx_bytes'), r.get('tx_bytes')]
    return {'r': rec, 'ts': pt['ts'], 'x': bool(pt['suspect'])}


def _unpack(uid, b):
    if b is None:
        return None
    if not isinstance(b, dict) or not isinstance(b.get('x'), bool):
        raise Q.bad('invalid cursor')
    r = b.get('r')
    rec = None
    if r is not None:
        if not isinstance(r, list) or len(r) != 5:
            raise Q.bad('invalid cursor')
        rec = {'uid': uid, 'object_class': r[0], 'collected_ts': r[1],
               'collected_at': r[2], 'rx_bytes': r[3], 'tx_bytes': r[4]}
    return {'rec': rec, 'ts': b.get('ts'), 'suspect': b['x']}


# --- pair -> result -------------------------------------------------------------

def _direction(res):
    return {'status': res['status'], 'delta_bytes': res['delta'],
            'bytes_per_s': res['bytes_per_s'],
            'bits_per_s': res['bits_per_s'], 'mbps': res['mbps']}


def _pair(uid, prev, cur, max_gap_s):
    p, c = prev['rec'], cur['rec']
    rx = counters.byte_rate(p, c, 'rx_bytes', max_gap_s)
    tx = counters.byte_rate(p, c, 'tx_bytes', max_gap_s)
    out = {
        'uid': uid,
        'object_class': (c or p or {}).get('object_class'),
        'start_ts': rx['start_ts'] if rx['start_ts'] is not None
        else prev['ts'],
        'end_ts': rx['end_ts'] if rx['end_ts'] is not None else cur['ts'],
        'interval_s': rx['interval_s'],
        'time_source': rx['time_source'],
        'time_precision': rx['time_precision'],
        'reduced_precision': rx['reduced_precision'],
        'interval_error_bound_s': rx['interval_error_bound_s'],
        'rx': _direction(rx), 'tx': _direction(tx),
    }
    if prev['suspect'] or cur['suspect']:
        # v0.4.4-era failed-collector observation: counters are not trusted.
        for d in (out['rx'], out['tx']):
            d.update(status=STATUS_SUSPECT_INPUT, delta_bytes=None,
                     bytes_per_s=None, bits_per_s=None, mbps=None)
        out['status'] = STATUS_SUSPECT_INPUT
    elif rx['status'] != counters.STATUS_OK:
        out['status'] = rx['status']
    else:
        out['status'] = tx['status']
    return out


# --- request --------------------------------------------------------------------

def interface_rates(store, p, window_start=None):
    """Compute one page of rates. `store` is the history store (read-only)."""
    uid = p['uid']
    ctx = Q.context_hash('a', uid, p['start'], p['end'], p['max_gap_s'])
    store_cursor, baseline = None, None
    if p['cursor'] is not None:
        doc = Q.decode_cursor(p['cursor'], 'a', ctx)
        store_cursor = doc.get('h')
        if not isinstance(store_cursor, str):
            raise Q.bad('invalid cursor')
        baseline = _unpack(uid, doc.get('b'))
    # N rates need N+1 observations, or N when a baseline is carried.
    want = p['limit'] + (0 if baseline is not None else 1)
    pts = [baseline] if baseline is not None else []
    got, skipped, nxt = 0, 0, store_cursor
    for _ in range(MAX_FETCH_CALLS):
        if got >= want:
            break
        res = store.query(start=p['start'], end=p['end'], limit=want - got,
                          cursor=nxt, window_start=window_start)
        skipped += res['skipped_lines']
        for obs in res['samples']:
            pts.append(_point(obs, uid))
        got += len(res['samples'])
        nxt = res['next_cursor']
        if nxt is None:
            break
    rates = [_pair(uid, pts[i - 1], pts[i], p['max_gap_s'])
             for i in range(1, len(pts))]
    next_cursor = None
    if nxt is not None:
        next_cursor = Q.encode_cursor({
            't': 'a', 'q': ctx, 'h': nxt,
            'b': _pack(pts[-1]) if pts else None})
    classes = [r['object_class'] for r in rates if r['object_class']]
    return {'uid': uid, 'object_class': classes[0] if classes else None,
            'max_gap_s': p['max_gap_s'], 'rates': rates, 'count': len(rates),
            'next_cursor': next_cursor, 'has_more': next_cursor is not None,
            'skipped_lines': skipped}
