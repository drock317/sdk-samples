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

import cp


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
    """

    __slots__ = ('ok', 'value', 'error')

    def __init__(self, ok, value=None, error=None):
        self.ok = ok
        self.value = value
        self.error = error


def _get_result(path):
    """Fetch a status/config path as an explicit CollectResult.

    A successful cp.get() (even returning None/empty) yields ok=True; only an
    exception yields ok=False. This is the caching-authoritative read.
    """
    try:
        value = cp.get(path)
        return CollectResult(True, value=value, error=None)
    except Exception as e:
        cp.log('collectors: error reading {}: {}'.format(path, e))
        return CollectResult(False, value=None, error=str(e))


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
    and must run on the slow cadence, never the fast topology cadence. Object
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
    wbond_interface(iface). HEAVY-ish -> slow cadence.
    """
    return _get('status/sdwan_adv/wan_bonding')


# --- Config trees (static-ish -- cache long, refresh on slow cadence) -----

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
        err = res.error
        return {
            'steering_rules': CollectResult(False, error=err),
            'steering_events': CollectResult(False, error=err),
            'steering_stats': CollectResult(False, error=err),
            'steering_intents': CollectResult(False, error=err),
        }
    parent = res.value if isinstance(res.value, dict) else {}
    # A successful parent read that lacks a child key is a legitimate empty
    # for that child (ok=True, value None) -- it must replace stale data.
    return {
        'steering_rules': CollectResult(True, value=parent.get('rules')),
        'steering_events': CollectResult(True, value=parent.get('events')),
        'steering_stats': CollectResult(True, value=parent.get('stats')),
        'steering_intents': CollectResult(True, value=parent.get('intents')),
    }


def collect_base_snapshot():
    """LIGHTWEIGHT topology/state pass -- safe to run on the fast cadence.

    Returns {key: CollectResult}. Small, frequently-changing trees only.
    Excludes heavy/slow trees (loadbalancers), detailed bonding runtime,
    QoE, and static config trees (collected on the slow cadence). The four
    steering sub-trees come from ONE status/wan/steering read (see
    _steering_split_results).
    """
    out = {
        'wan_devices': _get_result('status/wan/devices'),
        'primary_device': _get_result('status/wan/primary_device'),
        'wan_policies': _get_result('status/wan/policies'),
        'swans': _get_result('status/wan/swans'),
        'sdwan_links': _get_result('status/wan/sdwan'),
    }
    out.update(_steering_split_results())
    return out


def collect_heavy_snapshot():
    """HEAVIER runtime + static config pass -- run on the SLOW cadence.

    Returns {key: CollectResult}. Groups the expensive/slow trees
    (loadbalancers), detailed bonding runtime, QoE, and the static-ish config
    identity/class trees. Kept separate so heavy cadence differs from
    lightweight topology cadence (COLLECTION PERFORMANCE / SAFETY).
    """
    # NOTE: the broad status/sdwan_adv read is intentionally NOT collected --
    # it is not consumed anywhere and pulling the whole subtree adds needless
    # management-plane load. Only the targeted sub-trees below are read.
    return {
        'wan_bonding': _get_result('status/sdwan_adv/wan_bonding'),
        'qoe': _get_result('status/sdwan_adv/qoe'),
        'loadbalancers': _get_result('status/wan/loadbalancers'),
        'wan_rules2': _get_result('config/wan/rules2'),
        'identities_ip': _get_result('config/identities/ip'),
        'identities_intent': _get_result('config/identities/intent'),
        'traffic_class_config': _get_result('config/sdwan_adv/traffic_class'),
    }
