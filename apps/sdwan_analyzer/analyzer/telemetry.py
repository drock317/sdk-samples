"""Runtime telemetry records: normalized, timestamp-enveloped, compact.

Pure functions (no router I/O). Every record carries the timestamp envelope:

    metric_ts, collected_ts, timestamp_source

If NCOS supplies a native measurement timestamp: metric_ts = that value and
timestamp_source = 'ncos'. Otherwise metric_ts = collected_ts and
timestamp_source = 'collector'.

No rates, averages or interpretation are computed here. Raw counters and raw
vendor fields are carried as received.
"""

from collections import OrderedDict

from . import cellular
from . import normalize

SRC_NCOS = 'ncos'
SRC_COLLECTOR = 'collector'

# status/wan/devices/{uid}/stats key -> normalized counter name. Validated on
# R1900 + R2400 for physical, sdwan and wbond objects.
_COUNTER_MAP = (
    ('in', 'rx_bytes'),
    ('out', 'tx_bytes'),
    ('ipackets', 'rx_packets'),
    ('opackets', 'tx_packets'),
    ('ierrors', 'rx_errors'),
    ('oerrors', 'tx_errors'),
    ('idrops', 'rx_drops'),
    ('odrops', 'tx_drops'),
)
COUNTER_FIELDS = tuple(n for _, n in _COUNTER_MAP)


def envelope(collected_ts, metric_ts=None):
    """Timestamp envelope. metric_ts given => NCOS-native, else collector."""
    if metric_ts is None:
        return {'metric_ts': collected_ts, 'collected_ts': collected_ts,
                'timestamp_source': SRC_COLLECTOR}
    return {'metric_ts': metric_ts, 'collected_ts': collected_ts,
            'timestamp_source': SRC_NCOS}


class BoundedSeen(object):
    """Bounded insertion-ordered set used for cross-poll dedupe.

    add() returns True when the key is new. The oldest keys are evicted past
    `cap` so memory stays bounded; the cap is far above the rolling
    30-entry SWANS window so live duplicates are still caught.
    """

    def __init__(self, cap=4000):
        self._cap = cap
        self._keys = OrderedDict()

    def add(self, key):
        if key in self._keys:
            return False
        self._keys[key] = True
        while len(self._keys) > self._cap:
            self._keys.popitem(last=False)
        return True

    def __len__(self):
        return len(self._keys)

    def cap(self):
        return self._cap


# --- WAN state + interface counters --------------------------------------

def wan_state(raw_devices, primary_uid, collected_ts, primary_known=True):
    """Per-WAN connection state records plus the primary WAN uid.

    `primary_known=False` means primary_device was not read successfully in
    this pass (not requested, or failed): the primary identity is then
    omitted (no `primary_wan`, no `is_primary`) rather than re-stamped from a
    cached value or guessed as False.
    """
    out = []
    if isinstance(raw_devices, dict):
        for uid, dev in raw_devices.items():
            if not isinstance(dev, dict):
                continue
            status = dev.get('status') if isinstance(
                dev.get('status'), dict) else {}
            rec = {
                'uid': uid,
                'object_class': normalize.object_class(uid, dev),
                'connection_state': normalize._s(
                    status.get('connection_state')) or 'unknown',
            }
            if primary_known:
                rec['is_primary'] = uid == primary_uid
            rec.update(envelope(collected_ts))
            out.append(rec)
    if not primary_known:
        return {'paths': out}
    return {'primary_wan': primary_uid, 'paths': out}


def valid_acquired_at(value):
    """Return `value` as a float if it is a usable fractional Unix time."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float('inf'), float('-inf')) or value <= 0:
        return None
    return float(value)


def interface_records(raw_devices, prev_counters, collected_ts,
                      acquired_at=None):
    """Normalize per-device interface counters.

    Returns (records, new_counters). `prev_counters` is {uid: {field: n}}
    from the previous observation; a counter lower than before sets
    reset_suspected = True (no negative deltas are ever produced).

    `acquired_at` is the fractional Unix time captured immediately after the
    successful status/wan/devices read. When valid, each record gets a
    `collected_at` field (additive; collected_ts/metric_ts are unchanged).
    When absent or invalid the field is omitted: precision is never invented.
    """
    at = valid_acquired_at(acquired_at)
    records = []
    new_counters = {}
    prev_counters = prev_counters if isinstance(prev_counters, dict) else {}
    if not isinstance(raw_devices, dict):
        return records, new_counters
    for uid, dev in raw_devices.items():
        if not isinstance(dev, dict) or not isinstance(dev.get('stats'), dict):
            continue
        stats = dev['stats']
        status = dev.get('status') if isinstance(
            dev.get('status'), dict) else {}
        rec = {
            'uid': uid,
            'object_class': normalize.object_class(uid, dev),
            'connection_state': normalize._s(
                status.get('connection_state')) or 'unknown',
        }
        cur = {}
        for raw_key, name in _COUNTER_MAP:
            val = normalize._num(stats.get(raw_key))
            rec[name] = val
            cur[name] = val
        prev = prev_counters.get(uid) or {}
        rec['reset_suspected'] = any(
            cur[f] is not None and prev.get(f) is not None
            and cur[f] < prev[f] for f in COUNTER_FIELDS)
        rec.update(envelope(collected_ts))
        if at is not None:
            rec['collected_at'] = at
        records.append(rec)
        new_counters[uid] = cur
    return records, new_counters


# --- Cellular -------------------------------------------------------------

def cellular_records(raw_devices, collected_ts):
    """Cellular/CA telemetry for every device exposing diagnostics."""
    out = []
    if not isinstance(raw_devices, dict):
        return out
    for uid, dev in raw_devices.items():
        if not isinstance(dev, dict) or not str(uid).startswith('mdm'):
            continue
        rec = cellular.normalize_cellular(
            uid, dev.get('diagnostics'), collected_ts, collected_ts,
            SRC_COLLECTOR, info=dev.get('info'), status=dev.get('status'))
        if rec is not None:
            out.append(rec)
    return out


# --- SWANS (rolling history, dedupe by (uid, epoch)) ----------------------

def swans_new_samples(raw_swans, seen, collected_ts):
    """Return every UNSEEN unique (uid, epoch) path sample.

    Walks the whole rolling history on every poll. Responses are not time
    ordered and can repeat a (uid, epoch) pair inside one response, so each
    pair is registered in `seen` (a BoundedSeen) as it is accepted. Sorted
    by (epoch, uid) for stable output. Nothing is synthesized.
    """
    raw = raw_swans if isinstance(raw_swans, dict) else {}
    history = raw.get('history')
    out = []
    for entry in (history if isinstance(history, list) else []):
        if not isinstance(entry, dict):
            continue
        epoch = normalize._num(entry.get('epoch'))
        if epoch is None:
            continue
        for upd in (entry.get('upd_status') or []):
            if not isinstance(upd, dict):
                continue
            uid = normalize._s(upd.get('wandev'))
            if not uid or not seen.add((uid, epoch)):
                continue
            crit = upd.get('criteria') if isinstance(
                upd.get('criteria'), dict) else {}
            rec = {'uid': uid,
                   'latency_ms': normalize._num(crit.get('Latency')),
                   'jitter_ms': normalize._num(crit.get('Jitter'))}
            rec.update(envelope(collected_ts, metric_ts=epoch))
            out.append(rec)
    out.sort(key=lambda r: (r['metric_ts'], r['uid']))
    return out


# --- QoE ------------------------------------------------------------------

def qoe_records(raw_qoe, collected_ts):
    """QoE has no validated native timestamp -> collector envelope."""
    out = []
    for p in normalize.qoe_paths(raw_qoe).get('paths', []):
        rec = {'uid': p.get('uid'),
               'latency_avg_ms': p.get('latency_avg_ms'),
               'latency_last_ms': p.get('latency_last_ms'),
               'pkt_loss_stats_raw': p.get('pkt_loss_stats_raw')}
        # v0.8.3 additive validity: only present when something is not a
        # plain valid value, so ordinary records do not grow.
        val = p.get('validity') or {}
        if any(v != 'valid' for v in val.values()) or p.get(
                'pkt_loss_sentinel_candidates'):
            rec['validity'] = val
            rec['latency_avg_raw'] = p.get('latency_avg_raw')
            rec['latency_last_raw'] = p.get('latency_last_raw')
            rec['pkt_loss_sentinel_candidates'] = p.get(
                'pkt_loss_sentinel_candidates')
        rec.update(envelope(collected_ts))
        out.append(rec)
    return out


# --- WBOND runtime (raw BW fields intentionally NOT carried) --------------

def wbond_runtime(bonding, collected_ts):
    """Compact WBOND runtime from normalize.wan_bonding_detail output."""
    out = []
    for b in (bonding or {}).get('bonds', []):
        if not isinstance(b, dict):
            continue
        rec = {
            'iface': b.get('iface'),
            'link_state': b.get('link_state'),
            'bonding_mode': b.get('bonding_mode'),
            'member_count': b.get('member_count'),
            'total_download_flows': b.get('total_download_flows'),
            'total_upload_flows': b.get('total_upload_flows'),
            # reported | summed | unknown; absent on pre-0.8.2 observations.
            'total_download_flows_source': b.get(
                'total_download_flows_source') or 'unknown',
            'total_upload_flows_source': b.get(
                'total_upload_flows_source') or 'unknown',
            'members': [{
                'member_uid': m.get('member_uid'),
                # Tunnel iface as collected; null when not resolvable.
                'tunnel_iface': m.get('tunnel_iface'),
                'configured_weight': m.get('configured_weight'),
                'current_download_weight': m.get('current_download_weight'),
                'current_upload_weight': m.get('current_upload_weight'),
                'download_flows': m.get('download_flows'),
                'upload_flows': m.get('upload_flows'),
                'has_loss': m.get('has_loss'),
                'link_state': m.get('link_state'),
                'attach_state': m.get('link_attach_state'),
            } for m in b.get('members', []) if isinstance(m, dict)],
        }
        rec.update(envelope(collected_ts))
        out.append(rec)
    return out


# --- Traffic steering runtime ---------------------------------------------

def steering_runtime(logical_rules, collected_ts):
    """Per-logical-rule runtime state. Static definitions are NOT repeated."""
    out = []
    for lr in logical_rules or []:
        if not isinstance(lr, dict):
            continue
        rec = {
            'logical_name': lr.get('logical_name'),
            'semantic_fingerprint': lr.get('semantic_fingerprint'),
            'preferred_wan_uid': lr.get('preferred_wan_uid'),
            'steering_to': lr.get('steering_to'),
            'preferred_flows': lr.get('preferred_flows'),
            'nonpreferred_flows': lr.get('nonpreferred_flows'),
            'latest_event_reason': lr.get('latest_event_reason'),
        }
        rec.update(envelope(collected_ts))
        out.append(rec)
    return out


MAX_DEST_PER_EVENT = 10
MAX_DEST_KEYS = 8
_MAX_STR = 64


def _compact_destinations(dests):
    """Bounded scalar-only copy of an event's destinations[].

    The inner key names of destinations[] (application/category labels and
    moved-flow counts) are carried as NCOS reports them: only scalar values
    are kept, strings are truncated, and the list and each entry are capped.
    No key names are assumed.
    """
    out = []
    if not isinstance(dests, list):
        return out
    for d in dests[:MAX_DEST_PER_EVENT]:
        if isinstance(d, dict):
            c = {}
            for k in list(d)[:MAX_DEST_KEYS]:
                v = d[k]
                if isinstance(v, str):
                    c[str(k)] = v[:_MAX_STR]
                elif v is None or isinstance(v, (bool, int, float)):
                    c[str(k)] = v
            out.append(c)
        elif isinstance(d, str):
            out.append(d[:_MAX_STR])
    return out


def steering_new_events(events, seen, collected_ts):
    """Unseen steering events; dedupe key includes the exact event epoch."""
    out = []
    for ev in events or []:
        if not isinstance(ev, dict) or ev.get('epoch') is None:
            continue
        key = (ev.get('steering_rule'), ev.get('epoch'), str(ev.get('reason')),
               ev.get('wan_from_uid'), ev.get('wan_to_uid'))
        if not seen.add(key):
            continue
        rec = {k: ev.get(k) for k in (
            'steering_rule', 'reason', 'wan_from_uid', 'wan_to_uid',
            'wan_from', 'wan_to', 'total_flows_moved')}
        # v0.8.3 (additive): which destinations the moved flows belonged to.
        dests = _compact_destinations(ev.get('destinations'))
        if dests:
            rec['destinations'] = dests
        if isinstance(ev.get('destinations'), list) and len(
                ev['destinations']) > MAX_DEST_PER_EVENT:
            rec['destinations_truncated'] = True
        rec.update(envelope(collected_ts, metric_ts=ev.get('epoch')))
        out.append(rec)
    return out


def steering_events_meta(raw_events, prev_began=None):
    """Capture metadata of status/wan/steering/events (additive v0.8.3).

    `capture_restarted` is True/False only when a previous began_capture is
    known to compare against, else None. A restart is a new measurement
    epoch, never a rule deletion.
    """
    raw = raw_events if isinstance(raw_events, dict) else None
    if raw is None:
        return {'source_state': 'unavailable'}
    began = normalize._num(raw.get('began_capture'))
    return {
        'source_state': 'ok',
        'began_capture': began,
        'total_perf_events': normalize._num(raw.get('total_perf_events')),
        'event_overflow': raw.get('event_overflow'),
        'capture_restarted': (None if prev_began is None or began is None
                              else began != prev_began),
    }


# --- Steering runtime: per-rule application matching (v0.8.3) --------------

# Bounds keep one record far below the 256 KiB history line limit even at
# the Recording steering cadence (10 s). Worst case ~40 KB; typical < 2 KB.
MAX_STATS_RULES = 50
MAX_APPS_PER_RULE = 16
MAX_APPS_TOTAL = 200


def _flow_state(v):
    return 'missing' if v is None else 'value'


def steering_apps_record(raw_stats, defs_version=None, prev_began=None):
    """Compact application-level matching from status/wan/steering/stats.

    preferred/nonpreferred flows are GAUGES (current flow populations), never
    cumulative counters. A null flow value means NCOS did not report it; a
    zero is a valid reported zero. Applications with no match and no flows
    are counted in `apps_omitted` rather than listed, so records stay
    bounded; the full per-rule app total is `apps_total`.

    source_state: ok | empty (read succeeded, zero rules) | unavailable
    (read succeeded but no stats object). A failed read never reaches here
    (the section is omitted and the failure is in collector_errors).
    `defs_version` is the steering_rule_defs revision in force.
    """
    raw = raw_stats if isinstance(raw_stats, dict) else None
    if raw is None or not isinstance(raw.get('rules'), list):
        return {'source_state': 'unavailable', 'defs_version': defs_version,
                'rules': []}
    began = normalize._num(raw.get('began_capture'))
    rules = []
    listed_total = 0
    for r in raw['rules'][:MAX_STATS_RULES]:
        if not isinstance(r, dict):
            continue
        name = normalize._s(r.get('steering_rule')) or None
        apps, total, omitted = [], 0, 0
        for key, a in normalize._as_entries(r.get('apps')):
            if not isinstance(a, dict):
                continue
            total += 1
            pf = normalize._num(a.get('preferred_flows'))
            nf = normalize._num(a.get('nonpreferred_flows'))
            matched = a.get('was_flow_matched')
            active = (matched is True or (pf or 0) > 0 or (nf or 0) > 0
                      or pf is None or nf is None)
            if not active:
                omitted += 1
                continue
            if (len(apps) >= MAX_APPS_PER_RULE
                    or listed_total >= MAX_APPS_TOTAL):
                omitted += 1
                continue
            listed_total += 1
            apps.append({
                'id': str(key),
                'appname': normalize._s(a.get('appname')) or None,
                'catname': normalize._s(a.get('catname')) or None,
                'preferred_flows': pf,
                'nonpreferred_flows': nf,
                'matched': matched if isinstance(matched, bool) else None,
            })
        pf = normalize._num(r.get('preferred_flows'))
        nf = normalize._num(r.get('nonpreferred_flows'))
        rules.append({
            'rule': name,
            'rule_key': (normalize.logical_rule_name(name).lower()
                         if name else None),
            'preferred_flows': pf,
            'nonpreferred_flows': nf,
            'preferred_flows_state': _flow_state(pf),
            'nonpreferred_flows_state': _flow_state(nf),
            'was_flow_matched': (r.get('was_flow_matched') if isinstance(
                r.get('was_flow_matched'), bool) else None),
            'total_perf_events': normalize._num(r.get('total_perf_events')),
            'apps_total': total,
            'apps_omitted': omitted,
            'apps': apps,
        })
    return {
        'source_state': 'ok' if rules else 'empty',
        'defs_version': defs_version,
        'began_capture': began,
        'capture_restarted': (None if prev_began is None or began is None
                              else began != prev_began),
        'flows_truncated': (raw.get('flows_truncated') if isinstance(
            raw.get('flows_truncated'), bool) else None),
        'rules_truncated': len(raw['rules']) > MAX_STATS_RULES,
        'flow_values': 'gauge',
        'rules': rules,
    }
