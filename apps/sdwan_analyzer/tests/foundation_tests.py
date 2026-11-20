"""v0.8.3 telemetry foundation tests (Stages B-H).

Steering definition versioning + change detection, runtime application
matching, configured WAN bonding, cellular component RF, QoE validity,
persistence/restart and the new read-only APIs. Sanitized synthetic fixtures
(foundation_fixtures.py). Run through run_tests.py.
"""
import copy
import json
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import fixtures
import foundation_fixtures as F
from analyzer import (bond_config, cellular, collectors, foundation,
                      normalize, persistence as P, query, steering_changes,
                      steering_defs, steering_state, storage, telemetry, web)
from analyzer.service import AnalyzerService

check = None
_free_port = None


def bind(check_fn, free_port_fn):
    global check, _free_port
    check = check_fn
    _free_port = free_port_fn


# --- helpers -----------------------------------------------------------------

class _Clock(object):
    def __init__(self, t=1790380800):
        self.t = t

    def __call__(self):
        return self.t


class _Tmp(object):
    def __enter__(self):
        self.base = tempfile.mkdtemp(prefix='sdwan_f83_')
        return self.base

    def __exit__(self, *exc):
        shutil.rmtree(self.base, ignore_errors=True)


class _Collect(object):
    """Patch collectors.collect_keys with mutable synthetic data."""

    def __init__(self, data=None, fail=()):
        self.data = data if data is not None else _data()
        self.fail = set(fail)
        self.calls = []

    def __enter__(self):
        self._saved = collectors.collect_keys

        def fake(keys):
            keys = tuple(keys)
            self.calls.append(keys)
            out = {}
            for k in keys:
                if k in self.fail:
                    out[k] = collectors.CollectResult(
                        False, error='boom', error_type='api_error')
                else:
                    out[k] = collectors.CollectResult(
                        True, value=copy.deepcopy(self.data.get(k)))
            return out
        collectors.collect_keys = fake
        return self

    def __exit__(self, *exc):
        collectors.collect_keys = self._saved


def _data():
    d = dict(fixtures.base_raw())
    d.update(fixtures.heavy_raw())
    d.update({
        'steering_rules': F.rules(), 'steering_stats': F.stats(),
        'steering_events': F.events(),
        'identities_ip': F.identities_ip(),
        'identities_intent': F.identities_intent(),
        'traffic_class_config': F.traffic_class_cfg(),
        'wan_bonding_cfg': F.bond_cfg(), 'wan_rules2': F.rules2(),
        'wan_devices': F.wan_devices(), 'wan_bonding': F.bond_runtime(),
        'qoe_cfg': {'pkt_loss_cnt': {'enabled': True}},
    })
    return d


_STEER = ('steering_rules', 'steering_events', 'steering_stats',
          'steering_intents')


def _store_tracker():
    return storage.ConfigVersionStore(), steering_state.SteeringDefsTracker()


def _events_of(tracker):
    out = []
    for t in tracker.live_transitions():
        out.extend(t['events'])
    return out


def _types(evts):
    return sorted(e['change_type'] for e in evts)


def _observe(store, tr, rules, ts, kind_of=None):
    return tr.observe(store, rules, ts, history_recent=lambda: [],
                      kind_of=kind_of)


# --- Stage B: definitions ----------------------------------------------------

def test_defs_arbitrary_identifiers_and_children():
    print('test_defs_arbitrary_identifiers_and_children')
    rules = F.rules() + [F.child('Ω / weird-name #9', 'x-9', dst=ID_X)]
    st, p, inv = steering_defs.extract(rules)
    keys = [r['key'] for r in p['rules']]
    check(st == 'complete' and inv == 0 and
          keys == sorted(keys) and 'ω / weird-name #9' in keys,
          'arbitrary rule names are grouped by normalized logical name')
    alpha = [r for r in p['rules'] if r['key'] == 'rule alpha'][0]
    check(alpha['child_count'] == 2 and len(alpha['children']) == 2,
          'distinct compiled children are preserved')
    check(p['identity_basis'] == 'logical_name_only',
          'name-only identity limitation is explicit')
    d = alpha['children'][0]['def']
    for f in ('matched_devs', 'steering_to', 'primary_wandev'):
        check(f not in d, 'runtime field {} excluded from definition'.format(f))


ID_X = '44444444-aaaa-4bbb-8ccc-00000000000x'


def test_defs_duplicate_children_multiplicity():
    print('test_defs_duplicate_children_multiplicity')
    dup = [F.child('Dup', 'd-1'), F.child('Dup', 'd-2')]
    _, p, _ = steering_defs.extract(dup)
    r = p['rules'][0]
    check(r['child_count'] == 2 and len(r['children']) == 1 and
          r['children'][0]['count'] == 2 and
          r['children'][0]['compiled_ids'] == ['d-1', 'd-2'],
          'children identical except for compiled ids keep multiplicity 2')
    _, p1, _ = steering_defs.extract(dup[:1])
    check(steering_defs.payload_fingerprint(p) !=
          steering_defs.payload_fingerprint(p1),
          'dropping one duplicate child changes the definition fingerprint')
    _, p2, _ = steering_defs.extract([F.child('Dup', 'zz-1'),
                                      F.child('Dup', 'zz-2')])
    check(steering_defs.payload_fingerprint(p) ==
          steering_defs.payload_fingerprint(p2) and
          p['rules'][0]['compiled_id_fp'] != p2['rules'][0]['compiled_id_fp'],
          'compiled id churn alone keeps the definition fp; id fp differs')
    _, p3, _ = steering_defs.extract(list(reversed(F.rules())))
    _, p4, _ = steering_defs.extract(F.rules())
    check(steering_defs.payload_fingerprint(p3) ==
          steering_defs.payload_fingerprint(p4),
          'compiled child order is irrelevant')


def test_defs_duplicate_logical_names():
    print('test_defs_duplicate_logical_names')
    rules = [F.child('Same', 's-1', dst=F.ID_A),
             F.child('Same (2)', 's-2', dst=F.ID_B)]
    _, p, _ = steering_defs.extract(rules)
    check(len(p['rules']) == 1 and p['rules'][0]['child_count'] == 2,
          'compiled names that normalize equally share one logical rule')


def test_defs_runtime_churn_ignored():
    print('test_defs_runtime_churn_ignored')
    store, tr = _store_tracker()
    r1 = _observe(store, tr, F.rules(), 100)
    churn = F.rules()
    for c in churn:
        c['matched_devs'] = {'sdwan-other': {}, 'wbond-z': {}}
        c['steering_to'] = {'wbond-z': {}}
        c['primary_wandev'] = 'wbond-z'
        c['loadbalance_algo'] = 'round-robin'   # RUNTIME layer
        c['intent'] = 'Different friendly label'
    r2 = _observe(store, tr, churn, 200)
    check(r1['version'] == r2['version'] == 1 and not r2['changed'] and
          not tr.live_transitions(),
          'runtime WAN membership/selection churn creates no version/event')


def test_defs_identity_cache_delay():
    print('test_defs_identity_cache_delay')
    # Legacy semantic_fingerprint depends on the identity cache.
    empty = normalize.logical_rules(F.rules(), None, None, {}, None)
    full = normalize.logical_rules(
        F.rules(), None, None,
        normalize.destination_index(F.identities_ip()), None)
    check(empty[0]['semantic_fingerprint'] != full[0]['semantic_fingerprint'],
          'legacy fingerprint (kept for history) changes with identity cache')
    _, p, _ = steering_defs.extract(F.rules())
    fp = steering_defs.payload_fingerprint(p)
    check(fp == steering_defs.payload_fingerprint(
        steering_defs.extract(F.rules())[1]),
        'definition fp never consults the identity cache')
    res0 = steering_defs.resolver([], None, None)
    res1 = steering_defs.resolver(F.identities_ip(), None, None)
    a = steering_defs.resolve_destination(res0, F.ID_A, 'identity')
    b = steering_defs.resolve_destination(res1, F.ID_A, 'identity')
    check(not a['resolved'] and b['resolved'] and
          b['identity_name'] == 'zq-17',
          'resolution reports unresolved until the identity is known')


def test_defs_exact_uuid_resolution():
    print('test_defs_exact_uuid_resolution')
    res = steering_defs.resolver(F.identities_ip(), F.identities_intent(),
                                 F.traffic_class_cfg(), F.stats())
    prefix = F.ID_A[:13]
    check(not steering_defs.resolve_destination(
        res, prefix, 'identity')['resolved'],
        'a UUID prefix never resolves')
    check(not steering_defs.resolve_destination(
        res, F.ID_A.upper(), 'identity')['resolved'],
        'resolution is exact (no case folding)')
    a = steering_defs.resolve_destination(res, F.ID_A, 'identity')
    b = steering_defs.resolve_destination(res, F.ID_B, 'identity')
    c = steering_defs.resolve_destination(res, F.ID_C, 'identity')
    check(a['criterion_kind'] == 'application' and
          a['kind_basis'] == 'runtime_stats_labels',
          'kind=application from runtime appname label')
    check(b['criterion_kind'] == 'category',
          'kind=category from runtime catname with appname "None"')
    check(c['resolved'] and c['criterion_kind'] == 'unknown',
          'no runtime evidence -> kind unknown (not inferred from id text)')
    i = steering_defs.resolve_intent(res, F.INTENT_2)
    check(i['resolved'] and i['traffic_class_id'] == F.TC_2 and
          i['traffic_class_name'] == 'TC Two',
          'intent uuid -> intent -> traffic_class uuid -> TC name')
    check(not steering_defs.resolve_intent(res, F.INTENT_2[:20])['resolved'],
          'intent prefix never resolves')


def test_defs_match_all_and_literal_none():
    print('test_defs_match_all_and_literal_none')
    _, p_abs, _ = steering_defs.extract([F.child('M', 'm1', dst=None)])
    _, p_none, _ = steering_defs.extract([F.child('M', 'm1', dst='None')])
    _, p_emp, _ = steering_defs.extract([F.child('M', 'm1', dst='')])
    d_abs = p_abs['rules'][0]['children'][0]['def']
    d_none = p_none['rules'][0]['children'][0]['def']
    check(d_abs['dst_kind'] == 'empty' and d_abs['dst_ip_network'] is None,
          'absent destination -> empty criterion')
    check(d_none['dst_kind'] == 'literal_none' and
          d_none['dst_ip_network'] == 'None',
          'literal "None" preserved with its own kind')
    check(steering_defs.payload_fingerprint(p_abs) ==
          steering_defs.payload_fingerprint(p_emp) !=
          steering_defs.payload_fingerprint(p_none),
          'literal "None" is distinct from an absent/empty destination')
    r = steering_defs.resolve_destination(None, 'None', 'literal_none')
    check(not r['resolved'] and 'literal' in r['match_semantics'],
          'literal "None" is never resolved as an identity')


# --- Stage C: change detection -------------------------------------------------

def _transition(prev_rules, cur_rules, kind_of=None):
    store, tr = _store_tracker()
    _observe(store, tr, prev_rules, 100)
    _observe(store, tr, cur_rules, 200, kind_of=kind_of)
    return tr, _events_of(tr)


def test_changes_initial_baseline_quiet():
    print('test_changes_initial_baseline_quiet')
    store, tr = _store_tracker()
    _observe(store, tr, F.rules(), 100)
    check(tr.live_transitions() == [] and
          tr.status(store)['baseline'] == 'first_baseline',
          'first baseline announces nothing')


def test_changes_rule_added_removed():
    print('test_changes_rule_added_removed')
    cur = F.rules() + [F.child('Rule Gamma', 'g-1', dst=F.ID_C, prio=30)]
    _, ev = _transition(F.rules(), cur)
    check(_types(ev) == ['rule_added'] and ev[0]['rule_key'] == 'rule gamma',
          'rule addition detected')
    _, ev = _transition(cur, F.rules())
    check(_types(ev) == ['rule_removed'], 'rule removal detected')
    e = ev[0]
    for k in ('rule_key', 'rule_name', 'change_type', 'from_version',
              'to_version', 'previous', 'current', 'previous_observed_ts',
              'detected_ts', 'identity_basis', 'identity_confidence',
              'source', 'evidence', 'detection_semantics'):
        check(k in e, 'event carries {}'.format(k))
    check(e['previous_observed_ts'] == 100 and e['detected_ts'] == 200 and
          e['interval_complete'] is True,
          'interval bounded by the previous observation and detection')
    check(e['detection_semantics'] == 'observed_definition_difference',
          'semantics: observed difference, not a proven user edit')


def test_changes_criteria_and_fields():
    print('test_changes_criteria_and_fields')
    base = [F.child('R', 'r1', dst=F.ID_A)]
    cases = (
        (dict(dst=F.ID_C), ['destination_criterion_added',
                            'destination_criterion_removed']),
        (dict(intent=F.INTENT_2), ['traffic_class_changed']),
        (dict(prio=99), ['rule_priority_changed']),
        (dict(triggers=(('type|is|wbond', 10),)), ['wan_selector_changed']),
        (dict(triggers=(('type|is|sdwan', 5),)),
         ['wan_selector_priority_changed']),
        (dict(algo='round-robin'), ['algorithm_changed']),
        (dict(failover=True), ['failover_changed']),
        (dict(reset=True), ['reset_existing_flow_changed']),
        (dict(src=('Guest LAN',)), ['source_lans_changed']),
        (dict(excluded=('lan-cfg-7',)), ['excluded_lans_changed']),
        (dict(dia=True), ['definition_field_changed']),
    )
    for kw, expect in cases:
        args = dict(dst=F.ID_A)
        args.update(kw)
        _, ev = _transition(base, [F.child('R', 'r1', **args)])
        check(_types(ev) == sorted(expect),
              '{} -> {} (got {})'.format(sorted(kw), expect, _types(ev)))
    _, ev = _transition(base, base + [F.child('R', 'r2', dst=F.ID_A)])
    check(_types(ev) == ['compiled_child_count_changed'],
          'compiled child count change detected')


def test_changes_criterion_kind_labels():
    print('test_changes_criterion_kind_labels')
    base = [F.child('R', 'r1', dst=F.ID_C)]
    cur = [F.child('R', 'r1', dst=F.ID_C), F.child('R', 'r2', dst=F.ID_A)]
    _, ev = _transition(base, cur)
    add = [e for e in ev if e['change_type'] == 'destination_criterion_added']
    check(add and add[0]['criterion_kind'] == 'unknown',
          'no runtime labels -> criterion_kind unknown')
    res = steering_defs.resolver(F.identities_ip(), None, None, F.stats())
    _, ev = _transition(base, cur, kind_of=lambda: (
        lambda v, k: steering_defs.resolve_destination(res, v, k)))
    add = [e for e in ev if e['change_type'] == 'destination_criterion_added']
    check(add and add[0]['criterion_kind'] == 'application',
          'runtime appname label -> application criterion')


def test_changes_priority_ties():
    print('test_changes_priority_ties')
    a = [F.child('A', 'a1', prio=10), F.child('B', 'b1', prio=10)]
    st, p, _ = steering_defs.extract(a)
    check(st == 'complete' and p['rule_count'] == 2,
          'equal trigger priorities across rules are both kept')
    _, ev = _transition(a, [F.child('A', 'a1', prio=10),
                            F.child('B', 'b1', prio=11)])
    check(_types(ev) == ['rule_priority_changed'] and
          ev[0]['rule_key'] == 'b', 'tie broken by one rule is detected')


def test_changes_withheld_reads():
    print('test_changes_withheld_reads')
    store, tr = _store_tracker()
    _observe(store, tr, F.rules(), 100)
    bad = F.rules() + [{'id': 'no-xpolicy', 'name': 'Broken'}]
    check(_observe(store, tr, bad, 110) is None and
          tr.status(store)['withheld_reason'] == 'partial' and
          tr.invalid_children == 1, 'partial read withheld')
    check(_observe(store, tr, None, 120) is None and
          tr.state == 'unavailable', 'read without a rules object withheld')
    check(_observe(store, tr, [], 130) is None and
          tr.state == 'empty_unconfirmed',
          'first unexpected empty read withheld')
    check(tr.live_transitions() == [] and
          store.current(steering_state.NAME)['version'] == 1,
          'no removal inferred from partial/unavailable/empty reads')
    check(_observe(store, tr, F.rules(), 140)['version'] == 1 and
          tr.empty_streak == 0, 'a good read resets the empty streak')
    _observe(store, tr, [], 150)
    r = _observe(store, tr, [], 160)
    ev = _events_of(tr)
    check(r is not None and r['version'] == 2 and
          _types(ev) == ['rule_removed', 'rule_removed'] and
          ev[0]['source']['completeness'] == 'empty_confirmed',
          'two consecutive empty reads confirm the removal')


def test_changes_failed_read_service():
    print('test_changes_failed_read_service')
    with _Collect() as c:
        svc = AnalyzerService(clock=_Clock())
        svc.collect_startup()
        v1 = svc._config_store.current(steering_state.NAME)['version']
        c.data['steering_rules'] = []
        c.fail = {'steering_rules', 'steering_events', 'steering_stats',
                  'steering_intents'}
        obs = svc.collect('manual', _STEER, group='steering')
        check(steering_state.NAME not in obs['config_versions'] and
              'steering_apps' not in obs and
              svc._config_store.current(steering_state.NAME)['version']
              == v1, 'failed steering read: no version, no runtime record')


def test_changes_capture_reset_not_removal():
    print('test_changes_capture_reset_not_removal')
    with _Collect() as c:
        clock = _Clock()
        svc = AnalyzerService(clock=clock)
        svc.collect_startup()
        clock.t += 60
        c.data['steering_stats'] = F.stats(began=1790009999.0,
                                           alpha_apps={})
        c.data['steering_events'] = F.events(began=1790009999.0)
        obs = svc.collect('manual', _STEER, group='steering')
        check(obs['steering_apps']['capture_restarted'] is True and
              obs['steering_events_meta']['capture_restarted'] is True,
              'capture restart flagged on stats and events')
        check(not obs['config_versions'][steering_state.NAME]['changed'] and
              svc._defs_tracker.live_transitions() == [],
              'capture restart / app stopped matching is not a def change')


def test_changes_possible_rename():
    print('test_changes_possible_rename')
    _, ev = _transition([F.child('Old Name', 'o1')],
                        [F.child('New Name', 'n1')])
    check(_types(ev) == ['rule_added', 'rule_removed'],
          'rename appears as removal + addition')
    check(all(e['possible_rename'] and
              e['possible_rename']['confirmed'] is False for e in ev),
          'identical definition -> possible_rename, never confirmed')
    _, ev = _transition([F.child('A', 'a'), F.child('B', 'b')],
                        [F.child('C', 'c'), F.child('D', 'd')])
    check(all(e['possible_rename'] is None for e in ev),
          'ambiguous pairs are not annotated as renames')


def test_changes_bounded():
    print('test_changes_bounded')
    many = [F.child('R{}'.format(i), 'c{}'.format(i)) for i in range(250)]
    st, p, _ = steering_defs.extract(many)
    ch = steering_changes.diff({'rules': []}, p)
    tr = steering_changes.build_events(ch, {'from_version': 1,
                                            'to_version': 2})
    check(tr['event_count'] == 250 and
          len(tr['events']) == steering_changes.MAX_EVENTS_PER_TRANSITION
          and tr['truncated'] is True, 'events per transition are capped')


def test_changes_restart_and_history():
    print('test_changes_restart_and_history')
    with _Tmp() as base:
        clock = _Clock()
        with _Collect() as c:
            s1 = AnalyzerService(clock=clock, app_root=base,
                                 telemetry_root='tmp/telemetry')
            s1.collect_startup()
            first_ts = clock.t
            legacy_v = s1._config_store.current(
                'steering_definitions')['version']
            s1.shutdown()
            clock.t += 600
            c.data['steering_rules'] = F.rules()[:2]   # Beta removed
            s2 = AnalyzerService(clock=clock, app_root=base,
                                 telemetry_root='tmp/telemetry')
            s2.collect_startup()
            ev = _events_of(s2._defs_tracker)
            check(_types(ev) == ['rule_removed'] and
                  ev[0]['previous_observed_ts'] == first_ts and
                  ev[0]['interval_complete'] is True,
                  'restart compares with the persisted previous definition')
            check(s2._config_store.current('steering_definitions')
                  is not None and legacy_v == 1,
                  'legacy steering_definitions versions are still written')
            ch = s2.query_steering_changes('')
            check(ch['transitions'] and ch['versions'][0]['version'] == 2,
                  'changes API lists versions newest first')
            s2.shutdown()
        # Restart with no history: interval incomplete.
        hist = os.path.join(base, 'tmp', 'telemetry', 'history')
        shutil.rmtree(hist, ignore_errors=True)
        with _Collect() as c:
            c.data['steering_rules'] = F.rules()[:1]
            clock.t += 600
            s3 = AnalyzerService(clock=clock, app_root=base,
                                 telemetry_root='tmp/telemetry')
            s3.collect_startup()
            ev = _events_of(s3._defs_tracker)
            check(ev and ev[0]['previous_observed_ts'] is None and
                  ev[0]['interval_complete'] is False and
                  ev[0]['interval_note'],
                  'no prior observation time -> interval marked incomplete')
            s3.shutdown()


def test_changes_derived_from_versions():
    print('test_changes_derived_from_versions')
    store, tr = _store_tracker()
    _observe(store, tr, F.rules(), 100)
    _observe(store, tr, F.rules()[:2], 200)
    fresh = steering_state.SteeringDefsTracker()
    d = fresh.derived_transitions(store)
    check(len(d) == 1 and d[0]['events'][0]['derived_from'] ==
          'adjacent_versions' and
          d[0]['events'][0]['interval_complete'] is False,
          'change detail is derivable from adjacent version payloads')


def test_changes_api_params():
    print('test_changes_api_params')
    store, tr = _store_tracker()
    rules = F.rules()
    _observe(store, tr, rules, 100)
    for i in range(5):
        rules = rules + [F.child('X{}'.format(i), 'x{}'.format(i))]
        _observe(store, tr, rules, 200 + i)
    v = foundation.steering_changes_view(store, tr, {}, 'limit=2')
    check(len(v['transitions']) == 2 and v['next_before_version'] == 5,
          'changes page bounded by limit with a next cursor')
    v2 = foundation.steering_changes_view(
        store, tr, {}, 'limit=2&before_version=5')
    check([t['to_version'] for t in v2['transitions']] == [4, 3],
          'before_version pages backwards')
    for bad_q in ('limit=0', 'limit=51', 'limit=x', 'before_version=-1',
                  'nope=1', 'limit=1&limit=2'):
        try:
            foundation.steering_changes_view(store, tr, {}, bad_q)
            ok = False
        except query.QueryError as e:
            ok = e.status == 400
        check(ok, 'rejected query: {}'.format(bad_q))


# --- Stage D: runtime application matching -----------------------------------

def test_runtime_app_matching():
    print('test_runtime_app_matching')
    rec = telemetry.steering_apps_record(F.stats(), defs_version=3)
    alpha = [r for r in rec['rules'] if r['rule'] == 'Rule Alpha'][0]
    beta = [r for r in rec['rules'] if r['rule'] == 'Rule Beta'][0]
    check(rec['defs_version'] == 3 and rec['flow_values'] == 'gauge' and
          rec['source_state'] == 'ok', 'record metadata')
    check({a['id'] for a in alpha['apps']} == {'zq-17', 'k_0042'},
          'matched apps listed (incl. matched with zero flows)')
    check(beta['nonpreferred_flows'] is None and
          beta['nonpreferred_flows_state'] == 'missing' and
          beta['preferred_flows'] == 0 and
          beta['preferred_flows_state'] == 'value',
          'missing flow value vs valid zero distinguished')
    empty = telemetry.steering_apps_record({'rules': []})
    check(empty['source_state'] == 'empty', 'successful empty source')
    un = telemetry.steering_apps_record(None)
    check(un['source_state'] == 'unavailable', 'no stats object: unavailable')
    apps = {'id{}'.format(i): {'appname': 'n{}'.format(i),
                               'preferred_flows': 1, 'nonpreferred_flows': 0,
                               'was_flow_matched': True} for i in range(60)}
    apps['idle'] = {'appname': 'idle', 'preferred_flows': 0,
                    'nonpreferred_flows': 0, 'was_flow_matched': False}
    big = {'rules': [{'steering_rule': 'R{}'.format(i), 'apps': apps}
                     for i in range(130)], 'flows_truncated': True}
    rec = telemetry.steering_apps_record(big)
    r0 = rec['rules'][0]
    check(len(rec['rules']) == telemetry.MAX_STATS_RULES and
          rec['rules_truncated'] is True and rec['flows_truncated'] is True,
          'rule list bounded; truncation explicit')
    check(len(r0['apps']) == telemetry.MAX_APPS_PER_RULE and
          r0['apps_total'] == 61 and
          r0['apps_omitted'] == 61 - telemetry.MAX_APPS_PER_RULE,
          'apps bounded; idle and overflow apps counted, not listed')
    listed = sum(len(r['apps']) for r in rec['rules'])
    check(listed == telemetry.MAX_APPS_TOTAL and all(
        len(r['apps']) + r['apps_omitted'] == r['apps_total']
        for r in rec['rules']), 'global app cap; every app accounted for')
    size = len(P.dumps(rec))
    check(size < 65536, 'worst-case record stays bounded ({} bytes)'.format(
        size))


def test_runtime_steering_view():
    print('test_runtime_steering_view')
    with _Collect():
        svc = AnalyzerService(clock=_Clock())
        check(svc.query_steering() is None, 'None before first snapshot')
        svc.collect_startup()
        v = svc.query_steering()
        alpha = [r for r in v['rules'] if r['key'] == 'rule alpha'][0]
        app = [a for a in alpha['runtime']['apps'] if a['id'] == 'zq-17'][0]
        check(app['identity_uuids'] == [F.ID_A] and
              app['in_rule_definition'] is True,
              'runtime app id -> identity name -> uuid in rule definition')
        dests = sorted(c['destination']['identity_name']
                       for c in alpha['children'])
        check(dests == ['k_0042', 'zq-17'],
              'definition destinations resolved by exact uuid')
        tcs = {c['traffic_class']['traffic_class_name']
               for c in alpha['children']}
        check(tcs == {'TC One'}, 'traffic class resolved via intent')
        check(v['definitions']['current_version'] == 1 and
              v['capture']['source_state'] == 'ok',
              'definition status + capture metadata exposed')


def test_runtime_event_destinations():
    print('test_runtime_event_destinations')
    ev = normalize.steering_events(F.events())['events']
    out = telemetry.steering_new_events(ev, telemetry.BoundedSeen(), 5)
    check(out[0]['destinations'] == [{'appname': 'Synthetic App A',
                                      'catname': 'None', 'flows_moved': 3}],
          'event destinations carried compactly')
    many = dict(ev[0], epoch=2.0, destinations=[
        {'appname': 'x' * 500}] * 30)
    out = telemetry.steering_new_events([many], telemetry.BoundedSeen(), 5)
    check(len(out[0]['destinations']) == telemetry.MAX_DEST_PER_EVENT and
          out[0]['destinations_truncated'] and
          len(out[0]['destinations'][0]['appname']) == 64,
          'event destinations bounded and truncation explicit')


# --- Stage E: WAN bonding configuration ---------------------------------------

def _bond_view(n_cell=2, cfg=None, weights=None, extra_disc=False,
               runtime=None):
    d = _data()
    d['wan_devices'] = F.wan_devices(n_cell, extra_disc)
    d['wan_bonding'] = (runtime if runtime is not None
                        else F.bond_runtime(n_cell, weights))
    if cfg is not None:
        d['wan_bonding_cfg'] = cfg
    with _Collect(d):
        svc = AnalyzerService(clock=_Clock())
        svc.collect_startup()
        return svc, svc.query_wbond_config()


def test_bond_mode():
    print('test_bond_mode')
    for dyn, mode in ((False, 'static'), (True, 'dynamic'),
                      (None, 'unknown'), ('yes', 'unknown')):
        cfg = F.bond_cfg()
        cfg[0]['dynamic_weight'] = dyn
        if dyn is None:
            cfg[0].pop('dynamic_weight')
        st, bonds = bond_config.normalize_config(cfg)
        check(bonds[0]['operating_mode'] == mode,
              'dynamic_weight={!r} -> {}'.format(dyn, mode))
    eq = F.bond_cfg(criteria=[
        {'_id_': 'a', 'trigger_string': 'type|is|ethernet', 'weight': 50},
        {'_id_': 'b', 'trigger_string': 'type|is|ethernet', 'weight': 50}])
    eq[0].pop('dynamic_weight')
    check(bond_config.normalize_config(eq)[1][0]['operating_mode']
          == 'unknown', 'equal weights never imply a mode')


def test_bond_member_counts_and_criteria():
    print('test_bond_member_counts_and_criteria')
    for n in (0, 1, 2, 3, 5):
        _, v = _bond_view(n)
        crit = {c['criterion_id']: c for c in v['configured'][0]['criteria']}
        rt = v['runtime'][0]
        check(crit['crit-2']['matched_member_count'] == n and
              crit['crit-1']['matched_member_count'] == 1 and
              len(rt['members']) == n + 1,
              '{} cellular runtime members matched by one criterion'.format(
                  n))
    _, v = _bond_view(3)
    c2 = [c for c in v['configured'][0]['criteria']
          if c['criterion_id'] == 'crit-2'][0]
    check(c2['form'] == 'profile' and c2['profile_name'] == 'Cell Profile'
          and c2['resolution'] == 'rules2_trigger_name' and
          c2['rules2_ids'] == ['r2-cell'],
          'profile criterion resolved via rules2.trigger_name')
    check(c2['weight'] == 100 and all(
        m['weights_reported']['config_weight'] == 33
        for m in v['runtime'][0]['members']
        if m['physical_uid'].startswith('mdm')),
        'criterion weight and runtime config_weight kept separate as reported')


def test_bond_weights_missing_zero():
    print('test_bond_weights_missing_zero')
    _, v = _bond_view(2, weights={0: {'config_weight': 0, 'ds_weight': 0},
                                  1: {}})
    ms = {m['physical_uid']: m['weights_reported']
          for m in v['runtime'][0]['members']}
    check(ms['mdm-c0'] == {'config_weight': 0, 'ds_weight': 0,
                           'us_weight': None},
          'zero weights kept as 0, missing as null')
    check(ms['mdm-c1'] == {'config_weight': None, 'ds_weight': None,
                           'us_weight': None}, 'all-missing weights are null')
    blob = json.dumps(v)
    for bad in ('share', 'efficiency', 'utilization'):
        check(bad not in blob.replace('no traffic share', '').replace(
            'bonding efficiency', ''), 'no derived {} field'.format(bad))


def test_bond_unequal_and_unresolved_criteria():
    print('test_bond_unequal_and_unresolved_criteria')
    cfg = F.bond_cfg(criteria=[
        {'_id_': 'a', 'trigger_string': 'type|is|ethernet', 'weight': 90},
        {'_id_': 'b', 'trigger_string':
         'type|is|none%config_name|is|Cell Profile', 'weight': 10},
        {'_id_': 'c', 'trigger_string': 'type|is|mdm%sim|is|sim1',
         'weight': 5},
        {'_id_': 'd', 'trigger_string': 'type|is|none%config_name|is|Nope',
         'weight': None}])
    _, v = _bond_view(2, cfg=cfg)
    cr = {c['criterion_id']: c for c in v['configured'][0]['criteria']}
    check(cr['a']['weight'] == 90 and cr['b']['weight'] == 10,
          'unequal criterion weights preserved')
    check(cr['c']['form'] == 'other' and
          cr['c']['resolution'] == 'unresolved_expression' and
          cr['c']['matched_member_count'] == 0,
          'unverified criterion expression stays raw/unresolved')
    check(cr['d']['resolution'] == 'no_matching_rules2_entry' and
          cr['d']['weight'] is None, 'unknown profile: no match, null weight')


def test_bond_disconnected_candidate():
    print('test_bond_disconnected_candidate')
    _, v = _bond_view(2, extra_disc=True)
    c2 = [c for c in v['configured'][0]['criteria']
          if c['criterion_id'] == 'crit-2'][0]
    check(c2['matched_member_count'] == 2 and 'mdm-idle' not in json.dumps(
        c2['matched_members']),
        'a matching but non-member (disconnected) WAN is not a member')


def test_bond_association():
    print('test_bond_association')
    _, v = _bond_view(2)
    a = v['configured'][0]['association']
    check(a['state'] == 'candidate' and a['runtime_bond_iface'] ==
          'gres_wb0' and a['coverage'] == 'full',
          'single bond: criteria coverage gives a candidate association')
    two = F.bond_cfg() + [dict(F.bond_cfg()[0], _id_='bond-cfg-2',
                               name='Bond Two')]
    _, v = _bond_view(2, cfg=two)
    check(all(b['association']['state'] == 'unverified' and
              b['association']['basis'] ==
              'multiple_bonds_no_verified_mapping'
              for b in v['configured']),
          'multiple configured bonds: no mapping asserted')
    _, v = _bond_view(2, runtime={'interfaces': {}})
    check(v['configured'][0]['association']['state'] == 'unverified',
          'no runtime bond: association unverified')


def test_bond_versions_and_legacy():
    print('test_bond_versions_and_legacy')
    d = _data()
    with _Collect(d) as c:
        clock = _Clock()
        svc = AnalyzerService(clock=clock)
        o1 = svc.collect_startup()
        v1 = o1['config_versions']['wan_bonding_config']['version']
        check('wbond_config' in o1['config_versions'],
              'legacy wbond_config still versioned')
        clock.t += 60
        c.data['wan_bonding'] = F.bond_runtime(3)
        c.data['wan_devices'] = F.wan_devices(3)
        o2 = svc.collect('manual', ('wan_devices', 'wan_bonding',
                                    'wan_bonding_cfg'), group='bonding')
        check(o2['config_versions']['wan_bonding_config']['version'] == v1
              and not o2['config_versions']['wan_bonding_config']['changed'],
              'runtime member addition does not version the configuration')
        clock.t += 60
        c.data['wan_bonding_cfg'] = F.bond_cfg(dynamic=True)
        o3 = svc.collect('manual', ('wan_devices', 'wan_bonding',
                                    'wan_bonding_cfg'), group='bonding')
        check(o3['config_versions']['wan_bonding_config']['version'] == v1 + 1,
              'static -> dynamic is a new configuration version')
        c.fail = {'wan_bonding_cfg'}
        clock.t += 60
        o4 = svc.collect('manual', ('wan_bonding_cfg',), group='bonding')
        check('wan_bonding_config' not in o4['config_versions'],
              'failed config read does not re-version from cache')
    shuffled = F.bond_cfg()
    shuffled[0]['members'] = list(reversed(shuffled[0]['members']))
    for m in shuffled[0]['members']:
        m['_id_'] = m['_id_'] + '-recreated'
    p1 = bond_config.config_payload(bond_config.normalize_config(
        F.bond_cfg())[1])
    p2 = bond_config.config_payload(bond_config.normalize_config(
        shuffled)[1])
    check(bond_config.config_fingerprint(p1) ==
          bond_config.config_fingerprint(p2),
          'criterion order and criterion _id_ do not change the fingerprint')


# --- Stage F: cellular -----------------------------------------------------------

def _cell(diag, info=None, status=None):
    return cellular.normalize_cellular('mdm-x', diag, 1, 1, 'collector',
                                       info=info, status=status)


def test_cell_lte_only():
    print('test_cell_lte_only')
    r = _cell(F.LTE_ONLY, info={'sim': 'sim1', 'port': 'int1',
                                'config_id': 'r2-cell'},
              status={'connection_state': 'connected'})
    check(r['carrier_count'] == 1 and r['carriers'][0]['rat'] == 'LTE' and
          r['carriers'][0]['rf'] is None,
          'LTE-only: serving RF not copied into the component carrier')
    check(r['rsrp'] == -95.0 and r['sinr'] == 12.0,
          'serving-level RF stays on the modem record')
    ctx = r['context']
    check(ctx['sim_slot'] == 'sim1' and ctx['module_port'] == 'int1' and
          ctx['connection_state'] == 'connected' and
          ctx['roaming_state'] == 'home' and ctx['carrier_label'] ==
          'CarrierX', 'safe modem/SIM context')


def test_cell_nsa_components():
    print('test_cell_nsa_components')
    r = _cell(F.NSA)
    by = {c['role']: c for c in r['carriers']}
    check(by['pcell']['rat'] == 'LTE',
          '_5G_PCELL key with an LTE band value is LTE (RAT from band)')
    check(by['pcell']['rf']['sinr'] == 18.1 and
          by['pcell']['rf']['sinr_raw'] == 181 and
          by['pcell']['rf']['sinr_scale'] == 'tenths_db_plausibility',
          'raw SINR 181 -> 18.1 dB with scaling provenance')
    check(by['pcell']['rf']['rsrp'] is None and
          by['pcell']['rf']['validity']['rsrp'] == 'missing',
          'serving RSRP -90 is NOT copied into the PCELL component')
    s0 = by['scell0']
    check(s0['rat'] == 'NR' and s0['rf']['sinr'] == 29.0 and
          s0['rf']['rsrp'] == -88.0 and s0['rf']['rsrq'] == -12.0,
          'NR component RF persisted')
    s1 = by['scell1']
    check(s1['bandwidth_mhz'] == 0 and
          s1['rf']['validity'] == {'rsrp': 'placeholder_zero',
                                   'rsrq': 'placeholder_zero',
                                   'sinr': 'placeholder_zero'} and
          s1['rf']['rsrp'] is None, '0 MHz component zeros are placeholders')
    s2 = by['scell2']
    check(s2['rf']['sinr'] is None and s2['rf']['sinr_raw'] == 25 and
          s2['rf']['sinr_scale'] == 'ambiguous' and
          s2['rf']['validity']['sinr'] == 'ambiguous_scale',
          'ambiguous SINR keeps raw, no unit claimed')
    check(r['context']['roaming_state'] == 'unverified' and
          r['context']['roaming_code'] == '3',
          'undocumented ROAM code is not interpreted')


def test_cell_valid_zero_not_placeholder():
    print('test_cell_valid_zero_not_placeholder')
    d = dict(F.NSA, RSRQ_5G_SCELL0='0')
    s0 = [c for c in _cell(d)['carriers'] if c['role'] == 'scell0'][0]
    check(s0['rf']['rsrq'] == 0.0 and s0['rf']['validity']['rsrq'] ==
          'valid', 'a zero on an active, non-zero-MHz carrier stays valid')


def test_cell_dedupe_evidence():
    print('test_cell_dedupe_evidence')
    r = _cell(F.nsa_dup_scell())
    ev = r['carrier_evidence']
    check(ev['deduplicated_count'] == r['carrier_count'] == 4,
          'deduplication behavior unchanged (4 carriers)')
    check(ev['collapsed_duplicate_count'] == ev['active_source_entry_count']
          - 4 and ev['collapsed_duplicate_count'] >= 2,
          'collapsed duplicate count exposed')
    amb = [g for g in ev['collapsed_groups'] if g['ambiguous']]
    check(len(amb) == 1 and amb[0]['roles'] == ['scell0', 'scell3'] and
          ev['ambiguous_identity_count'] == 1,
          'identical SCELL identities flagged ambiguous')
    pc = [g for g in ev['collapsed_groups'] if 'pcell' in g['roles']]
    check(pc and not pc[0]['ambiguous'],
          'serving + PCELL schema duplicate is not ambiguous')


def test_cell_no_sim_and_sensitive():
    print('test_cell_no_sim_and_sensitive')
    r = _cell(F.NO_SIM, info={'sim': 'sim2', 'serial': 'SER-000123',
                              'mac': '00:00:5e:00:53:01'})
    check(r['carrier_count'] == 0 and r['context']['sim_present'] is False
          and r['context']['sim_slot'] == 'sim2' and
          r['context']['carrier_label'] == 'CarrierZ',
          'no-SIM slot: no carriers, sim_present false, HOMECARRID label')
    blob = json.dumps([r, _cell(F.NSA, info={'serial': 'SER-000123'})])
    for k in ('IMEI', 'IMEI2', 'IMEISV', 'IMSI', 'ICCID', 'MDN', 'CGSN',
              'GSN', 'DISP_IMEI', 'NR_CELL_ID', 'CELL_ID', 'GPS'):
        v = F.NSA.get(k)
        check(v is None or v not in blob, 'value of {} not persisted'.format(
            k))
    for v in ('SER-000123', '00:00:5e:00:53:01', '9999999'):
        check(v not in blob, 'sensitive value {}.. not persisted'.format(
            v[:4]))


def test_cell_observation_size():
    print('test_cell_observation_size')
    devs = F.wan_devices(1)
    devs['mdm-c0']['diagnostics'] = F.nsa_dup_scell()
    recs = telemetry.cellular_records(devs, 1)
    size = len(P.dumps(recs))
    check(len(recs) == 1 and size < 4096,
          'one NSA modem record stays compact ({} bytes)'.format(size))


# --- Stage G: QoE -------------------------------------------------------------

def test_qoe_sentinel():
    print('test_qoe_sentinel')
    raw = {'interface': {'o1': {
        'qoe-latency-stats': {'pkts-intf-qoe-latency-average': 4294967295,
                              'pkts-intf-qoe-latency-last-sample': 0},
        'pkt-loss-stats': {'pkts-lost': 4294967295, 'pkts-total': 10}}}}
    p = normalize.qoe_paths(raw)['paths'][0]
    check(p['latency_avg_ms'] is None and p['latency_avg_raw'] ==
          4294967295 and p['validity']['latency_avg'] ==
          'sentinel_candidate', 'latency sentinel candidate withheld, raw kept')
    check(p['latency_last_ms'] == 0 and p['validity']['latency_last'] ==
          'valid', 'a zero latency is not globally nulled')
    check(p['pkt_loss_sentinel_candidates'] == 1 and
          p['pkt_loss_stats_raw']['pkts-lost'] == 4294967295 and
          'pkt_loss_pct' not in p, 'loss raw kept, counted, no percentage')
    rec = telemetry.qoe_records(raw, 1)[0]
    check(rec['validity']['latency_avg'] == 'sentinel_candidate',
          'observation carries the validity flags')
    ok = telemetry.qoe_records(fixtures.QOE, 1)[0]
    check('validity' not in ok, 'ordinary QoE records do not grow')
    check(normalize.qoe_config({'pkt_loss_cnt': {'enabled': False}})
          == {'state': 'ok', 'pkt_loss_cnt_enabled': False} and
          normalize.qoe_config(None)['state'] == 'unavailable',
          'qoe config pkt_loss_cnt.enabled')


# --- Stage H: storage + APIs ---------------------------------------------------

def test_storage_legacy_config_file():
    print('test_storage_legacy_config_file')
    with _Tmp() as base:
        root = os.path.join(base, 'tmp', 'telemetry')
        os.makedirs(root)
        legacy = {'name': 'wbond_config', 'version': 3, 'fingerprint': 'f',
                  'ts': 5, 'payload': []}
        with open(os.path.join(root, 'config_versions.jsonl'), 'w') as f:
            f.write(P.dumps(legacy) + '\n')
        with _Collect():
            svc = AnalyzerService(clock=_Clock(), app_root=base,
                                  telemetry_root='tmp/telemetry')
            check(svc._config_store.current('wbond_config')['version'] == 3,
                  'v0.8.2 config versions load unchanged')
            obs = svc.collect_startup()
            cv = obs['config_versions']
            check(cv['steering_rule_defs']['version'] == 1 and
                  cv['wan_bonding_config']['version'] == 1 and
                  svc._defs_tracker.live_transitions() == [],
                  'new objects start at v1 with no announced changes')
            svc.shutdown()
            svc2 = AnalyzerService(clock=_Clock(), app_root=base,
                                   telemetry_root='tmp/telemetry')
            check(svc2._config_store.current('steering_rule_defs') and
                  svc2._config_store.current('wan_bonding_config'),
                  'new version objects survive restart')
            h = svc2.query_history('')
            last = h['samples'][-1]
            check('steering_apps' in last and 'qoe_meta' in last and
                  'steering_events_meta' in last,
                  'new observation sections persist and decode')
            svc2.shutdown()


def test_storage_steering_deps_same_pass():
    print('test_storage_steering_deps_same_pass')
    with _Collect() as c:
        clock = _Clock()
        svc = AnalyzerService(clock=clock)
        svc.collect_startup()
        n = len(c.calls)
        svc.collect('recording', _STEER, group='steering', job_id=None)
        check(len(c.calls) == n + 1,
              'unchanged definitions: no identity/intent re-read')
        c.data['steering_rules'] = F.rules()[:1]
        svc.collect('scheduled', _STEER)
        check(len(c.calls) == n + 3 and
              set(c.calls[-1]) == set(collectors.STEERING_DEP_KEYS),
              'changed definitions: dependencies read in the same pass')


def test_api_routes():
    print('test_api_routes')
    with _Collect():
        svc = AnalyzerService(clock=_Clock())
        svc.collect_startup()
    empty = AnalyzerService()
    port, port2 = _free_port(), _free_port()
    ok = HTTPServer(('127.0.0.1', port), web._make_handler(svc))
    nope = HTTPServer(('127.0.0.1', port2), web._make_handler(empty))
    for s in (ok, nope):
        threading.Thread(target=s.serve_forever, daemon=True).start()

    def get(p, path):
        try:
            with urllib.request.urlopen(
                    'http://127.0.0.1:%d%s' % (p, path), timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())
    try:
        for path in ('/api/analytics/steering', '/api/analytics/wbond_config',
                     '/api/analytics/steering/changes',
                     '/api/analytics/validity'):
            code, body = get(port, path)
            check(code == 200 and body.get('schema_version') == 1,
                  'GET {}'.format(path))
        for path in ('/api/analytics/steering', '/api/analytics/wbond_config'):
            code, _ = get(port2, path)
            check(code == 503, '{} 503 before first snapshot'.format(path))
        code, body = get(port, '/api/analytics/steering/changes?limit=999')
        check(code == 400 and 'error' in body, 'bad changes query -> 400')
        for path in ('/api/analytics/wbond', '/api/analytics/topology',
                     '/api/traffic_steering', '/api/bonding', '/api/qoe',
                     '/api/cellular', '/api/changes'):
            code, _ = get(port, path)
            check(code == 200, 'existing route still 200: {}'.format(path))
        code, body = get(port, '/api/analytics/validity')
        check('collectors' in body and 'steering_definitions' in body and
              body['qoe']['config']['pkt_loss_cnt_enabled'] is True,
              'validity view exposes collector, definition and QoE context')
    finally:
        for s in (ok, nope):
            s.shutdown()
            s.server_close()


def test_api_legacy_keys_preserved():
    print('test_api_legacy_keys_preserved')
    with _Collect(dict(_data(), **{
            k: v for k, v in fixtures.base_raw().items()})):
        svc = AnalyzerService(clock=_Clock())
        svc.collect_startup()
        snap = svc.snapshot()
        lr = snap['traffic_steering']['logical_rules'][0]
        for k in ('logical_name', 'semantic_fingerprint', 'branches',
                  'preferred_wan', 'preferred_wan_raw', 'destinations'):
            check(k in lr, 'logical rule key kept: {}'.format(k))
        wb = svc.query_wbond()
        check(wb['schema_version'] == 1 and 'bonds' in wb,
              '/api/analytics/wbond contract unchanged')
        check('paths' in snap['qoe'] and 'config' in snap['qoe'],
              'qoe section additive config block')


TESTS = [
    test_defs_arbitrary_identifiers_and_children,
    test_defs_duplicate_children_multiplicity,
    test_defs_duplicate_logical_names,
    test_defs_runtime_churn_ignored,
    test_defs_identity_cache_delay,
    test_defs_exact_uuid_resolution,
    test_defs_match_all_and_literal_none,
    test_changes_initial_baseline_quiet,
    test_changes_rule_added_removed,
    test_changes_criteria_and_fields,
    test_changes_criterion_kind_labels,
    test_changes_priority_ties,
    test_changes_withheld_reads,
    test_changes_failed_read_service,
    test_changes_capture_reset_not_removal,
    test_changes_possible_rename,
    test_changes_bounded,
    test_changes_restart_and_history,
    test_changes_derived_from_versions,
    test_changes_api_params,
    test_runtime_app_matching,
    test_runtime_steering_view,
    test_runtime_event_destinations,
    test_bond_mode,
    test_bond_member_counts_and_criteria,
    test_bond_weights_missing_zero,
    test_bond_unequal_and_unresolved_criteria,
    test_bond_disconnected_candidate,
    test_bond_association,
    test_bond_versions_and_legacy,
    test_cell_lte_only,
    test_cell_nsa_components,
    test_cell_valid_zero_not_placeholder,
    test_cell_dedupe_evidence,
    test_cell_no_sim_and_sensitive,
    test_cell_observation_size,
    test_qoe_sentinel,
    test_storage_legacy_config_file,
    test_storage_steering_deps_same_pass,
    test_api_routes,
    test_api_legacy_keys_preserved,
]
