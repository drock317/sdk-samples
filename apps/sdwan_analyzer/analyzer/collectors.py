"""NCOS collectors: read-only snapshots of WAN / SD-WAN status.

This layer does ONE thing: fetch raw NCOS status trees via cp.get() and
return them (or None on failure). No normalization, no business logic, no
storage. Keeping it thin makes the verified API surface auditable in one
place.

API VERIFICATION STATUS
-----------------------
Every path below was checked against docs/ncos-api/ (steering docs:
api-reference.md "API Verification Workflow"). Paths are grouped by
confidence:

VERIFIED (documented structure, safe to consume specific fields):
    status/wan/devices                 docs/ncos-api/status/wan/devices/README.md
    status/wan/devices/{id}/diagnostics  (mdm only) docs .../diagnostics.md
    status/wan/devices/{id}/stats        docs .../stats.md
    status/wan/primary_device          docs/ncos-api/status/wan/primary_device.md
    status/wan/policies                docs/ncos-api/status/wan/policies.md
    status/wan/swans                   docs/ncos-api/status/wan/swans.md
    status/wan/sdwan                   docs/ncos-api/status/wan/sdwan.md
    status/sdwan_adv                   docs/ncos-api/status/sdwan_adv.md
    status/wan/steering                docs/ncos-api/status/wan/steering.md

STA-VALIDATED (shape confirmed by Speedtest Analyzer v1.1.4 validated code,
which resolved these on real hardware incl. R1900; see normalize.py for the
field semantics):
    status/sdwan_adv/wan_bonding/interfaces/{wbond_iface}
        -> members_list[] -> entry['intf name'] = underlying WAN device key
           (WBOND member enumeration; NOT dep_wandevs/active_dep_wandev)
    status/wan/devices/{sdwan}/status.active_dep_wandev, .dep_wandevs
        -> SD-WAN single dependent (underlay) WAN
    status/wan/steering -> rules[] -> rule['intent'] (string)
        -> steering "active" when any intent != 'Management (Management traffic)'
    control/sdwan_adv/user_mode_driver/interface/{info.uid} -> local_ip/remote_ip
        -> SD-WAN overlay source/gateway (control plane, not status.ipinfo)
    control/sdwan_adv/wan_bonding/interface/{info.uid} -> local_ip/remote_ip
        -> WBOND overlay source/gateway (fallback: status/routing analysis)

PHASE-1A VERIFIED on R1900 + R2400, NCOS 7.26.81 (v0.8.3 consumers):
    status/wan/steering/rules[].xpolicy -> effective compiled definitions
        (dst_ip_network == config/identities/ip[]._id_, intent ==
        config/identities/intent[]._id_; see steering_defs.py)
    status/wan/steering/stats.rules[].apps -> key == identities/ip[].name
    config/identities/intent[].traffic_class == config/sdwan_adv/traffic_class._id_
    config/sdwan_adv/wan_bonding -> {_id_, name, dynamic_weight (bool),
        members[] = criteria {_id_, trigger_string, weight}} (bond_config.py)
    config/sdwan_adv/qoe.pkt_loss_cnt.enabled

These paths are validated in STA code but are NOT yet in this repo's
docs/ncos-api and have not been re-verified on a router by THIS app -- they
require device validation before we depend on them in production (see
readme "Requires router validation").

STILL DEVICE-DEPENDENT (empty until the feature is configured; the read is
safe and returns None/{} otherwise):
    status/wan/sdwan -> links          (SD-WAN hub/spoke; STA does not use
                                        this for membership)

MEMORY NOTE (coding-standards "Memory Management"): prefer the most specific
sub-path. Per-device diagnostics/stats are fetched per UID, never by pulling
the whole status/wan tree in a loop.
"""

import threading
import time

import cp


ERR_TIMEOUT = 'timeout'   # read exceeded READ_TIMEOUT_S
ERR_BUSY = 'busy'         # skipped: an earlier timed-out read is still pending
ERR_API = 'api_error'     # cp.get raised


class CollectResult(object):
    """Explicit outcome of one collector read.

    Distinguishes an ENDPOINT FAILURE (exception) from a SUCCESSFUL read that
    legitimately returned None/empty. The per-collector last-known-good cache
    (service._RawCache) relies on this: a failure must NOT overwrite a prior
    good value, while a legitimately empty result MUST replace old data.

    Attributes:
        ok (bool): True when the read completed without an exception.
        value: the raw value returned by cp.get() (may be None/{}/[] and
            still be a valid success).
        error (str|None): failure reason when not ok.
        acquired_at (float|None): fractional Unix time read from the
            router clock immediately after THIS read returned successfully
            (not the end of the collection pass). None for failed reads and
            for results built without a real read (derived/split results,
            test doubles): precision is never fabricated.
    """

    __slots__ = ('ok', 'value', 'error', 'error_type', 'acquired_at')

    def __init__(self, ok, value=None, error=None, error_type=None,
                 acquired_at=None):
        self.ok = ok
        self.value = value
        self.error = error
        # One of ERR_TIMEOUT / ERR_BUSY / ERR_API when not ok.
        self.error_type = error_type
        self.acquired_at = acquired_at


# Wall-clock bound for one collector read (seconds). Shared cp.py is NOT
# modified: cp.get() has only a per-recv socket timeout on the router and no
# bound at all over REST, so each read runs in a short-lived daemon thread
# and the caller stops waiting after READ_TIMEOUT_S. A timed-out read cannot
# be killed; its thread is abandoned and `_hung` makes later reads fail fast
# (instead of piling up concurrent cp.get calls) until it finishes. LIMITATION:
# the abandoned call may still complete in the background.
READ_TIMEOUT_S = 15
_hung = []


def _bounded_call(path, timeout):
    """Run cp.get(path) with a wall-clock bound.

    Returns (done, value, err, err_type). At most ONE worker thread exists:
    while a timed-out call is still pending, no new thread is started.
    """
    _hung[:] = [t for t in _hung if t.is_alive()]
    if _hung:
        return (False, None, 'collector busy (previous read still pending)',
                ERR_BUSY)
    box = {}

    def _run():
        try:
            box['value'] = cp.get(path)
        except Exception as e:
            box['error'] = str(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        _hung.append(t)
        return False, None, 'timeout after {}s'.format(timeout), ERR_TIMEOUT
    if 'error' in box:
        return False, None, box['error'], ERR_API
    return True, box.get('value'), None, None


def _get_result(path):
    """Fetch a status/config path as an explicit CollectResult.

    A successful cp.get() (even returning None/empty) yields ok=True; a
    raised exception or a wall-clock timeout yields ok=False. This is the
    caching-authoritative read.
    """
    done, value, err, etype = _bounded_call(path, READ_TIMEOUT_S)
    if done:
        # Captured immediately after the read returns, before any parsing
        # or other reads. The counters were sampled by NCOS at some instant
        # inside this call, so this is an upper bound on the sampling time.
        return CollectResult(True, value=value, error=None,
                             acquired_at=time.time())
    cp.log('collectors: error reading {}: {}'.format(path, err))
    return CollectResult(False, value=None, error=err, error_type=etype)


def _get(path):
    """Fetch a status path, returning the raw value or None on error.

    Thin convenience over _get_result for the per-device/per-interface reads
    the service layer does directly (WBOND interface, modem diag/stats). The
    per-collector LKG cache uses _get_result; this helper is only for reads
    whose result is consumed immediately, not cached across passes.
    """
    return _get_result(path).value


# --- WAN device inventory & path state -----------------------------------

def wan_devices():
    """status/wan/devices -> {device_id: {info,status,stats,config,...}}.

    Device IDs look like 'mdm-41949674', 'ethernet-wan', 'ethernet-sfp0'.
    """
    return _get('status/wan/devices')


def primary_device():
    """status/wan/primary_device -> primary WAN device UID string."""
    return _get('status/wan/primary_device')


def wan_policies():
    """status/wan/policies -> {policies:{...}, primary}.

    Policy names include FailoverFailback, DualSIM, SWANS, Affinity, OnDemand.
    """
    return _get('status/wan/policies')


def swans():
    """status/wan/swans -> Smart WAN Selection priority/history/data usage."""
    return _get('status/wan/swans')


# --- SD-WAN / WBOND / steering (may be empty until configured) -----------

def sdwan_links():
    """status/wan/sdwan -> {connected_hub, links}. Empty until SD-WAN set up.

    links inner shape is OPAQUE (see module docstring).
    """
    return _get('status/wan/sdwan')


def steering():
    """status/wan/steering -> {events,stats,perf,rules,intents}.

    rules/intents entry shapes are OPAQUE until steering is configured.
    """
    return _get('status/wan/steering')


# --- Per-device cellular / modem diagnostics -----------------------------

# --- SD-WAN / WBOND detail (STA-validated paths) -------------------------

def wbond_interface(wbond_iface):
    """status/sdwan_adv/wan_bonding/interfaces/{iface} for one WBOND device.

    STA v1.1.4 reads members_list here; each entry's 'intf name' is the
    underlying WAN device key. wbond_iface is the WBOND device's info.iface.
    """
    if not wbond_iface:
        return None
    return _get('status/sdwan_adv/wan_bonding/interfaces/{}'.format(
        wbond_iface))


def sdwan_umd_interface(info_uid):
    """control/sdwan_adv/user_mode_driver/interface/{info.uid}.

    STA-validated SD-WAN overlay addressing (local_ip/remote_ip). Read-only
    GET of a control object; we never write it.
    """
    if not info_uid:
        return None
    return _get('control/sdwan_adv/user_mode_driver/interface/{}'.format(
        info_uid))


def wbond_control_interface(info_uid):
    """control/sdwan_adv/wan_bonding/interface/{info.uid}.

    STA-validated WBOND overlay addressing (local_ip/remote_ip). Read-only.
    """
    if not info_uid:
        return None
    return _get('control/sdwan_adv/wan_bonding/interface/{}'.format(info_uid))


def routing():
    """status/routing -> routing tables. STA WBOND source-IP fallback source.

    Larger tree; fetch only when the control-plane WBOND address is
    unavailable (per STA's fallback order).
    """
    return _get('status/routing')


def modem_diagnostics(device_id):
    """status/wan/devices/{id}/diagnostics for a cellular (mdm-*) device.

    Returns None for non-modem device IDs (matches Speedtest Analyzer guard).
    """
    if not device_id or not str(device_id).startswith('mdm'):
        return None
    return _get('status/wan/devices/{}/diagnostics'.format(device_id))


def modem_stats(device_id):
    """status/wan/devices/{id}/stats for a cellular (mdm-*) device."""
    if not device_id or not str(device_id).startswith('mdm'):
        return None
    return _get('status/wan/devices/{}/stats'.format(device_id))


# --- Traffic steering detail (validated on R1900 NCOS 7.26.41) -----------
#
# The top-level status/wan/steering shape (events/stats/perf/rules/intents)
# is documented in docs/ncos-api/status/wan/steering.md. The INNER shapes
# (compiled rule fields, event fields, per-rule stats fields) were validated
# on the lab R1900 per the implementation brief and are consumed in
# normalize.py. We fetch the specific sub-trees so each is small and one
# failing sub-tree does not blank the others.

def steering_events():
    """status/wan/steering/events -> {began_capture, total_perf_events,
    event_overflow, steering_events[]}.

    Each steering_events[] entry may carry epoch, steering_rule, reason,
    measure_units/from/to, wan_from/to, wan_from_uid/wan_to_uid,
    total_flows_moved, destinations[], src_lans[].
    """
    return _get('status/wan/steering/events')


def steering_stats():
    """status/wan/steering/stats -> {began_capture, flows_truncated, rules[]}.

    Per-rule: steering_rule, total_perf_events, preferred_wan, apps,
    preferred_flows, nonpreferred_flows, was_flow_matched. NOTE: flow
    counts are a current/aging population, NOT lifetime totals.
    """
    return _get('status/wan/steering/stats')


def steering_rules():
    """status/wan/steering/rules -> compiled runtime steering rules[].

    One logical NCM rule may compile into several runtime children. UUIDs
    are NOT durable logical IDs (see normalize.logical_rules).
    """
    return _get('status/wan/steering/rules')


def steering_intents():
    """status/wan/steering/intents -> runtime intent map (may be sparse).

    Do NOT treat an empty map as proof Traffic Classes do not exist; the
    authoritative class definitions live in the config tree (see
    traffic_class_config / intent_config below).
    """
    return _get('status/wan/steering/intents')


# --- Loadbalancers (HEAVY / slow on R1900 -- collect conservatively) ------

def loadbalancers():
    """status/wan/loadbalancers -> {name: {type/algo, devices, score, ...}}.

    WARNING: this endpoint can be slow on the R1900. It is a HEAVY collector
    and is read only on startup/reconciliation/manual refresh. Object
    types include rate-* and spillover-*; fields vary by algorithm.
    """
    return _get('status/wan/loadbalancers')


# --- SD-WAN QoE (transport quality -- distinct from SWANS) ----------------

def qoe():
    """status/sdwan_adv/qoe -> SD-WAN transport QoE per path.

    Distinct object from status/wan/swans (Traffic Class path measurement).
    Field units/semantics are only partially proven -> carried mostly raw.
    """
    return _get('status/sdwan_adv/qoe')


# --- WAN bonding detailed runtime -----------------------------------------

def wan_bonding():
    """status/sdwan_adv/wan_bonding -> full bonding runtime tree.

    Contains per-bond runtime (mode, state, members, directional weights,
    flow counts, capabilities). Per-interface member detail is fetched via
    wbond_interface(iface). Read on every observation.
    """
    return _get('status/sdwan_adv/wan_bonding')


# --- Config trees (static-ish -- cache long, read on startup, reconciliation, manual refresh) -----

def wan_rules2():
    """config/wan/rules2 -> WAN profile config (priorities, bandwidth, etc.).

    Read-only. Fields include _id_, trigger_name, trigger_string,
    rule_indexes, priority, original_priority, parent_priority, configured
    ingress/egress bandwidth, failover config. Analyzer NEVER writes config.
    """
    return _get('config/wan/rules2')


def identities_ip():
    """config/identities/ip -> destination identity map (UUID -> identity).

    Authoritative translation of Traffic Steering destination UUIDs to
    friendly_name + internal application/category name/id. Do NOT hardcode
    a*/c* prefix meaning; resolve from actual fields.
    """
    return _get('config/identities/ip')


def identities_intent():
    """config/identities/intent -> Traffic Class / intent definitions.

    One of two likely sources for Traffic Class criteria (latency/jitter/
    signal thresholds, priority, FEC, Multi-WAN, WBOND behavior). Both this
    and config/sdwan_adv/traffic_class are collected; whichever exists is
    used. Read-only.
    """
    return _get('config/identities/intent')


def traffic_class_config():
    """config/sdwan_adv/traffic_class -> Traffic Class definitions.

    Second likely source for Traffic Class criteria. Read-only. Preserve
    raw; normalize known criteria fields where present.
    """
    return _get('config/sdwan_adv/traffic_class')


def _steering_split_results():
    """Read status/wan/steering ONCE and split into per-key CollectResults.

    COLLECTION EFFICIENCY: the four steering sub-trees (rules/events/stats/
    intents) are all children of the single status/wan/steering object (see
    docs/ncos-api/status/wan/steering.md, which documents the parent as one
    object returning {events, stats, perf, rules, intents}). One cp.get() of
    the parent is strictly fewer API dispatches than four separate sub-path
    reads and returns identical data, so we fetch once and split locally.

    If the single parent read FAILS, all four keys report that same failure
    (so the LKG cache keeps each key's previous value). We do NOT fall back
    to four separate reads -- that would defeat the load reduction.
    """
    res = _get_result('status/wan/steering')
    if not res.ok:
        return {k: CollectResult(False, error=res.error,
                                 error_type=res.error_type)
                for k in _STEERING_KEYS}
    parent = res.value if isinstance(res.value, dict) else {}
    # A successful parent read that lacks a child key is a legitimate empty
    # for that child (ok=True, value None) -- it must replace stale data.
    return {
        'steering_rules': CollectResult(True, value=parent.get('rules')),
        'steering_events': CollectResult(True, value=parent.get('events')),
        'steering_stats': CollectResult(True, value=parent.get('stats')),
        'steering_intents': CollectResult(True, value=parent.get('intents')),
    }


# --- Collection groups ----------------------------------------------------
#
# There are NO periodic fast/slow passes. A pass is run only for startup, the
# user-selected schedule, a manual refresh or (later) a Recording job.
#
# RUNTIME keys are read for every scheduled observation. CONFIG keys are
# static/change-versioned: read at startup, on ~hourly reconciliation and on
# targeted manual refresh. NOTE: the broad status/sdwan_adv read is never
# collected; only the targeted wan_bonding and qoe subtrees are.

_PATHS = {
    'wan_devices': 'status/wan/devices',
    'primary_device': 'status/wan/primary_device',
    'wan_policies': 'status/wan/policies',
    'swans': 'status/wan/swans',
    'sdwan_links': 'status/wan/sdwan',
    'wan_bonding': 'status/sdwan_adv/wan_bonding',
    'qoe': 'status/sdwan_adv/qoe',
    'loadbalancers': 'status/wan/loadbalancers',
    'wan_rules2': 'config/wan/rules2',
    'identities_ip': 'config/identities/ip',
    'identities_intent': 'config/identities/intent',
    'traffic_class_config': 'config/sdwan_adv/traffic_class',
    # v0.8.3: configured WAN bonding (criteria + static/dynamic mode).
    'wan_bonding_cfg': 'config/sdwan_adv/wan_bonding',
    # v0.8.3: QoE config (pkt_loss_cnt.enabled) -- interpretation context.
    'qoe_cfg': 'config/sdwan_adv/qoe',
}
_STEERING_KEYS = ('steering_rules', 'steering_events', 'steering_stats',
                  'steering_intents')

# Key classes. loadbalancers is RUNTIME data (not static config) but is
# expensive on the R1900, so it is read only at startup, ~hourly reconciliation
# and targeted manual refresh -- never on every observation and never during
# high-rate Recording by default.
STATIC_CONFIG_KEYS = ('wan_rules2', 'identities_ip', 'identities_intent',
                      'traffic_class_config', 'wan_bonding_cfg', 'qoe_cfg')
# Small config trees the steering definitions resolve against (identity
# uuid, intent uuid, traffic class uuid). Read in the SAME pass as a changed
# steering definition set so resolution is consistent; never re-read on every
# observation.
STEERING_DEP_KEYS = ('identities_ip', 'identities_intent',
                     'traffic_class_config')
EXPENSIVE_RUNTIME_KEYS = ('loadbalancers',)
CLASS_RUNTIME = 'runtime'
CLASS_EXPENSIVE_RUNTIME = 'expensive_runtime'
CLASS_STATIC_CONFIG = 'static_config'

RUNTIME_KEYS = ('wan_devices', 'primary_device', 'wan_policies', 'swans',
                'sdwan_links', 'wan_bonding', 'qoe') + _STEERING_KEYS
# Low-frequency set (startup + reconciliation): static config + expensive
# runtime. Name kept for the existing reconcile logic; see classes above.
CONFIG_KEYS = EXPENSIVE_RUNTIME_KEYS + STATIC_CONFIG_KEYS


def key_class(key):
    if key in EXPENSIVE_RUNTIME_KEYS:
        return CLASS_EXPENSIVE_RUNTIME
    if key in STATIC_CONFIG_KEYS:
        return CLASS_STATIC_CONFIG
    return CLASS_RUNTIME

# Targeted manual-refresh groups -> keys read.
REFRESH_GROUPS = {
    'traffic_classes': ('identities_intent', 'traffic_class_config'),
    'steering': _STEERING_KEYS + STEERING_DEP_KEYS,
    'bonding': ('wan_devices', 'wan_bonding', 'wan_bonding_cfg'),
    'wan_paths': ('wan_devices', 'primary_device', 'swans', 'qoe'),
    'overview': RUNTIME_KEYS,
    'loadbalancers': EXPENSIVE_RUNTIME_KEYS,
}


def collect_keys(keys):
    """Read the named collector keys. Returns {key: CollectResult}.

    Steering keys share ONE status/wan/steering read (split locally).
    """
    out = {}
    keys = tuple(keys)
    for key in keys:
        if key in _STEERING_KEYS or key not in _PATHS:
            continue
        out[key] = _get_result(_PATHS[key])
    if any(k in _STEERING_KEYS for k in keys):
        split = _steering_split_results()
        for key in keys:
            if key in _STEERING_KEYS:
                out[key] = split[key]
    return out


def collect_runtime_snapshot():
    """Runtime telemetry reads for one observation."""
    return collect_keys(RUNTIME_KEYS)


def collect_config_snapshot():
    """Static/config reads (change-versioned; startup + reconciliation)."""
    return collect_keys(CONFIG_KEYS)
