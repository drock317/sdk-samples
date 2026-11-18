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


# --- WAN state + interface counters --------------------------------------

def wan_state(raw_devices, primary_uid, collected_ts):
    """Per-WAN connection state records plus the primary WAN uid."""
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
                'is_primary': uid == primary_uid,
            }
            rec.update(envelope(collected_ts))
            out.append(rec)
    return {'primary_wan': primary_uid, 'paths': out}


def interface_records(raw_devices, prev_counters, collected_ts):
    """Normalize per-device interface counters.

    Returns (records, new_counters). `prev_counters` is {uid: {field: n}}
    from the previous observation; a counter lower than before sets
    reset_suspected = True (no negative deltas are ever produced).
    """
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
            SRC_COLLECTOR)
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
            'members': [{
                'member_uid': m.get('member_uid'),
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
        rec.update(envelope(collected_ts, metric_ts=ev.get('epoch')))
        out.append(rec)
    return out
