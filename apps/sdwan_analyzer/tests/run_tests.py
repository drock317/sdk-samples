"""Local test harness for SD-WAN Analyzer backend, real-R1900-shape.

Pure standard-library. Run from the repo root with the project venv:

    .venv/bin/python3 apps/sdwan_analyzer/tests/run_tests.py

Exercises the real captured shapes (two-layer compiled rules with nested
xpolicy, dict matched_devs/steering_to, trigger_strings list-of-dicts,
xpolicy.dst_ip_network, stats/events keyed by logical NAME, dict stats.apps)
against the real normalize/service code. The service is driven by injecting
cached raw passes (no router I/O).

No network, no router, no external dependencies.
"""

import os
import sys

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from analyzer import normalize            # noqa: E402
from analyzer import collectors           # noqa: E402
from analyzer.service import AnalyzerService, _RawCache  # noqa: E402
import fixtures                            # noqa: E402


_failures = []


def check(cond, msg):
    if cond:
        print('  PASS: {}'.format(msg))
    else:
        print('  FAIL: {}'.format(msg))
        _failures.append(msg)


def _logical_by_name():
    dest_index = normalize.destination_index(fixtures.IDENTITIES_IP)
    logical = normalize.logical_rules(
        fixtures.STEERING_RULES, fixtures.STEERING_STATS,
        fixtures.STEERING_EVENTS, dest_index)
    return {lr['logical_name']: lr for lr in logical}, logical


# -------------------------------------------------------------------------
# WAN paths / naming
# -------------------------------------------------------------------------

def test_wan_paths():
    print('test_wan_paths')
    devices = fixtures.WAN_DEVICES
    underlay_map = {'sdwan-hub0_wan': 'ethernet-wan',
                    'sdwan-hub0_1ea0ac': 'mdm-1ea0ac'}
    paths = normalize.wan_paths(devices, fixtures.PRIMARY_DEVICE, underlay_map)
    by_uid = {p['uid']: p for p in paths}
    check(by_uid['sdwan-hub0_wan']['object_class'] == normalize.OBJ_SDWAN,
          'sdwan overlay classed sdwan')
    check(by_uid['wbond-wbond0_hub0']['object_class'] == normalize.OBJ_WBOND,
          'wbond classed wbond (not flattened)')
    check(by_uid['sdwan-hub0_wan']['display_name'] ==
          'SD-WAN \u2014 Ethernet WAN', 'sdwan-eth named from underlay')
    check(by_uid['ethernet-wan']['is_primary'] is True,
          'primary from primary_device only')


# -------------------------------------------------------------------------
# xpolicy parsing / selector / dev dicts
# -------------------------------------------------------------------------

def test_selector_from_xpolicy():
    print('test_selector_from_xpolicy')
    by_id = {r['id']: r for r in fixtures.STEERING_RULES}
    check(normalize.classify_wan_selector(by_id['885823d0-1111-2222-3333-444455556666'])
          == normalize.SEL_BONDED, 'xpolicy wbond trigger -> bonded')
    check(normalize.classify_wan_selector(by_id['yt-child-1'])
          == normalize.SEL_ALL_SDWAN, 'xpolicy sdwan + 2 matched -> all_sdwan')
    check(normalize.classify_wan_selector(by_id['mgmt-1'])
          == normalize.SEL_EXPLICIT_PRIORITY,
          'xpolicy.wan_priority_enabled -> explicit_priority')


def test_dev_dicts():
    print('test_dev_dicts')
    ids = normalize._dev_ids(fixtures._dev('sdwan-hub0_1ea0ac',
                                           'sdwan-hub0_wan'))
    check(ids == ['sdwan-hub0_1ea0ac', 'sdwan-hub0_wan'],
          'matched_devs dict keys normalized to sorted WAN IDs')
    members = normalize._dev_members(
        {'wbond-wbond0_hub0': {'priority': 10}})
    check(members['wbond-wbond0_hub0'].get('priority') == 10,
          'member metadata preserved from dict values')


def test_trigger_strings_list_of_dicts():
    print('test_trigger_strings_list_of_dicts')
    mgmt = [r for r in fixtures.STEERING_RULES if r['id'] == 'mgmt-1'][0]
    entries = normalize._trigger_entries(mgmt)
    check(len(entries) == 5, 'all five trigger entries parsed')
    check(entries[0]['trigger_string'] == 'type|is|wbond',
          'trigger_string extracted from dict (not str(dict))')
    check(entries[1]['trigger_priority'] == 20,
          'trigger_priority extracted from trigger dict')


# -------------------------------------------------------------------------
# LAB01-Best Effort acceptance
# -------------------------------------------------------------------------

def test_lab01_best_effort():
    print('test_lab01_best_effort')
    by_name, _ = _logical_by_name()
    lab = by_name.get('LAB01-Best Effort')
    check(lab is not None, 'LAB01-Best Effort logical rule present')
    check(lab['wan_selector_type'] == normalize.SEL_BONDED, 'selector bonded')
    check(lab['trigger_priority'] == 2, 'trigger_priority == 2 (from xpolicy)')
    check(lab['source_lans'] == ['Primary LAN'],
          'source_lans == ["Primary LAN"] (not dicts)')
    check(lab['traffic_class_name'] == 'Best Effort (Data traffic)',
          'traffic_class_name from top-level intent')
    check(lab['traffic_class_id'] == fixtures.TC_BEST_EFFORT,
          'traffic_class_id from xpolicy.intent UUID')
    check(lab['matched_devs'] == ['wbond-wbond0_hub0'],
          'matched_devs == [wbond-wbond0_hub0]')
    check(lab['steering_to'] == ['wbond-wbond0_hub0'],
          'steering_to == [wbond-wbond0_hub0]')
    check(lab['preferred_wan'] == 'wbond-wbond0_hub0',
          'preferred_wan from stats (by logical name)')
    check(lab['preferred_flows'] == 54 and lab['nonpreferred_flows'] == 26,
          'preferred/nonpreferred populated exactly ONCE (not multiplied)')
    check(lab['latest_event_reason'] == 'Single selected WAN is connected',
          'event reason verbatim')
    check(lab['latest_flows_moved'] == 60, 'flows_moved == 60')


# -------------------------------------------------------------------------
# YouTube (four children -> one logical rule) acceptance
# -------------------------------------------------------------------------

def test_youtube_all():
    print('test_youtube_all')
    by_name, logical = _logical_by_name()
    yt = by_name.get('Lab01 - Youtube')
    check(yt is not None, 'Lab01 - Youtube logical rule present')
    # Four compiled children collapse to one logical rule.
    yt_count = sum(1 for lr in logical if lr['logical_name'] == 'Lab01 - Youtube')
    check(yt_count == 1, 'four YouTube children group to ONE logical rule')
    check(len(yt['compiled_rule_ids']) == 4, 'four compiled ids retained')
    check(yt['wan_selector_type'] == normalize.SEL_ALL_SDWAN,
          'selector all_sdwan')
    check(yt['source_lans'] == ['Primary LAN'], 'source_lans normalized')
    check(set(yt['matched_devs']) == {'sdwan-hub0_1ea0ac', 'sdwan-hub0_wan'},
          'both SD-WAN matched devices represented')
    check(yt['steering_to'] == ['sdwan-hub0_1ea0ac'],
          'selected cellular steering_to represented')
    names = {d['friendly_name'] for d in yt['destinations']}
    check(names == {'Youtube.com', 'Youtube HD', 'Youtube Music',
                    'YouTube Kids'},
          'all four destination UUIDs resolve and merge')
    check(yt['preferred_flows'] == 18 and yt['nonpreferred_flows'] == 4,
          'stats attach once by logical name (not x4)')


# -------------------------------------------------------------------------
# TEST - Applications destination merge + type
# -------------------------------------------------------------------------

def test_test_applications():
    print('test_test_applications')
    by_name, _ = _logical_by_name()
    t = by_name.get('TEST - Applications')
    check(t is not None, 'TEST - Applications present')
    names = {d['friendly_name'] for d in t['destinations']}
    check(names == {'Netflix.com', 'Hulu', 'OnlyFans'},
          'Netflix + Hulu + OnlyFans merged from dst_ip_network')
    check(t['destination_type'] == 'application',
          'destination_type application via identity (no prefix inference)')


# -------------------------------------------------------------------------
# Management explicit priority acceptance
# -------------------------------------------------------------------------

def test_management_explicit_priority():
    print('test_management_explicit_priority')
    by_name, _ = _logical_by_name()
    m = by_name.get('Management Steering')
    check(m is not None, 'Management Steering present')
    check(m['wan_selector_type'] == normalize.SEL_EXPLICIT_PRIORITY,
          'selector explicit_priority')
    check(m['wan_priority_enabled'] is True, 'wan_priority_enabled true')
    check(m['source_lans'] == ['NCX DNS LAN'], 'source_lans == [NCX DNS LAN]')
    order = m['wan_priority_order']
    prios = [e['trigger_priority'] for e in order]
    check(prios == [10, 20, 30, 40, 50],
          'priority order 10/20/30/40/50 from xpolicy.trigger_strings')
    check(order[0]['trigger_string'] == 'type|is|wbond',
          'WBOND at priority 10')


# -------------------------------------------------------------------------
# Runtime vs configured algorithm not flattened
# -------------------------------------------------------------------------

def test_runtime_vs_configured_algo():
    print('test_runtime_vs_configured_algo')
    by_name, _ = _logical_by_name()
    lab = by_name['LAB01-Best Effort']
    check(lab['runtime_algorithm'] == 'none', 'runtime_algorithm from top level')
    check(lab['configured_algorithm'] == 'none',
          'configured_algorithm from xpolicy (kept as separate field)')


# -------------------------------------------------------------------------
# stats.apps dict handling
# -------------------------------------------------------------------------

def test_stats_apps_dict():
    print('test_stats_apps_dict')
    stats = normalize.steering_stats(fixtures.STEERING_STATS)
    by_rule = {r['steering_rule']: r for r in stats['rules']}
    test_rule = by_rule['TEST - Applications']
    ids = {a['internal_id'] for a in test_rule['apps']}
    check(ids == {'a000012DE', 'a00001496'},
          'stats.apps dict keys preserved as internal_id')
    names = {a['appname'] for a in test_rule['apps']}
    check(names == {'Netflix.com', 'Hulu'}, 'app names normalized from dict')


# -------------------------------------------------------------------------
# Fingerprint stability under compiled UUID churn
# -------------------------------------------------------------------------

def test_fingerprint_stability():
    print('test_fingerprint_stability')
    dest_index = normalize.destination_index(fixtures.IDENTITIES_IP)
    logical1 = normalize.logical_rules(
        fixtures.STEERING_RULES, fixtures.STEERING_STATS,
        fixtures.STEERING_EVENTS, dest_index)

    churned = []
    for i, r in enumerate(fixtures.STEERING_RULES):
        c = dict(r)
        c['id'] = 'regen-top-{}'.format(i)
        xp = dict(r.get('xpolicy', {}))
        xp['_id_'] = 'regen-xp-{}'.format(i)
        c['xpolicy'] = xp
        churned.append(c)
    logical2 = normalize.logical_rules(churned, fixtures.STEERING_STATS,
                                       fixtures.STEERING_EVENTS, dest_index)

    fp1 = {lr['logical_name']: lr['semantic_fingerprint'] for lr in logical1}
    fp2 = {lr['logical_name']: lr['semantic_fingerprint'] for lr in logical2}
    check(fp1 == fp2,
          'semantic fingerprints stable across compiled UUID churn')


# -------------------------------------------------------------------------
# Bonding / loadbalancers / traffic classes / swans+qoe
# -------------------------------------------------------------------------

def test_bonding_detail():
    print('test_bonding_detail')
    detail = normalize.wan_bonding_detail(fixtures.WAN_BONDING)
    bond = detail['bonds'][0]
    check(bond['bonding_mode'] == 'GRE', 'bonding_mode GRE from Bonding Mode')
    check(bond['link_state'] == 'UP', 'link_state from link state')
    check(bond['total_download_flows'] == 29,
          'bond DL total = 29 (reported field)')
    check(bond['total_upload_flows'] == 13,
          'bond UL total = 13 (reported field)')
    check(bond['gre_key'] == 16448, 'bond gre key captured')
    members = {m['member_uid']: m for m in bond['members']}
    eth = members['ethernet-wan']
    check(eth['configured_weight'] == 80, 'Ethernet config weight 80')
    check(eth['current_download_weight'] == 75,
          'Ethernet DL runtime weight 75 (ds_weight)')
    check(eth['current_upload_weight'] == 80,
          'Ethernet UL runtime weight 80 (us_weight)')
    check(eth['download_flows'] == 24 and eth['upload_flows'] == 11,
          'Ethernet DL/UL flows 24/11')
    check(eth['tunnel_iface'] == 'gres-hub0_wan',
          'member tunnel iface from members_list dict key')
    check(eth['profile_name'] == 'Ethernet', 'member profile_name captured')
    cell = members['mdm-1ea0ac']
    check(cell['configured_weight'] == 20 and
          cell['current_download_weight'] == 24 and
          cell['current_upload_weight'] == 20,
          'Cellular weights 20/24/20 (config/ds/us)')
    check(cell['download_flows'] == 5 and cell['upload_flows'] == 2,
          'Cellular DL/UL flows 5/2')
    check('FLOW_DUPLICATE' in detail['supported_capabilities'],
          'supported_capabilities captured')


def test_no_bare_sdwan_adv_collected():
    print('test_no_bare_sdwan_adv_collected')
    # Record every path collect_heavy_snapshot reads and assert none is the
    # bare status/sdwan_adv tree.
    saved = collectors._get_result
    seen = []

    def fake(path):
        seen.append(path)
        return collectors.CollectResult(True, value={})

    collectors._get_result = fake
    try:
        collectors.collect_heavy_snapshot()
        collectors.collect_base_snapshot()
    finally:
        collectors._get_result = saved
    check('status/sdwan_adv' not in seen,
          'no periodic collector reads bare status/sdwan_adv')
    check('status/sdwan_adv/wan_bonding' in seen and
          'status/sdwan_adv/qoe' in seen,
          'targeted sdwan_adv subtrees still collected')


def test_loadbalancers():
    print('test_loadbalancers')
    lbs = normalize.loadbalancers(fixtures.LOADBALANCERS)
    lb = lbs[0]
    check(lb['algorithm'] == 'spillover' and
          lb['active_device'] == 'sdwan-hub0_1ea0ac',
          'spillover + active_device')


def test_traffic_classes():
    print('test_traffic_classes')
    tcs = normalize.traffic_classes(fixtures.IDENTITIES_INTENT,
                                    fixtures.TRAFFIC_CLASS_CFG)
    by_name = {tc['name']: tc for tc in tcs}
    check('Best Effort (Data traffic)' in by_name,
          'Best Effort NCX class produced')
    check('Voice' not in by_name,
          'legacy intent without traffic_class excluded from NCX view')
    be = by_name['Best Effort (Data traffic)']
    check(be['intent_id'] == fixtures.TC_BEST_EFFORT,
          'intent_id = 00000004-...')
    check(be['traffic_class_id'] == fixtures.TC_BE_ID,
          'traffic_class_id = 00000003-...694a... (correlated)')
    check(be['latency']['threshold'] == 1000,
          'latency threshold 1000 from criteria[]')
    check(be['jitter']['threshold'] == 600, 'jitter threshold 600')
    check(be['signal_strength']['enabled'] is False,
          'SS enabled = false from criterion "SS"')
    check(be['signal_strength']['threshold'] == 70, 'SS threshold 70')
    check(be['loadbalance_algo'] == 'none', 'loadbalance_algo = none')
    check(be['dscp'] == 8, 'dscp = 8 from traffic_class config')
    check(be['forward_error_correction'] is False, 'FEC = false')
    check(be['wan_bonding_algorithm'] == 'flow_balance',
          'wan_bonding algo = flow_balance')


def test_traffic_class_fingerprint_change():
    print('test_traffic_class_fingerprint_change')
    import copy
    tcs = normalize.traffic_classes(fixtures.IDENTITIES_INTENT,
                                    fixtures.TRAFFIC_CLASS_CFG)
    fp_before = normalize._stable_hash(sorted(
        (normalize.traffic_class_fingerprint_payload(tc) for tc in tcs),
        key=lambda p: (p.get('intent_id') or '',
                       p.get('traffic_class_id') or '')))

    # Change Real Time thresholds 1000/600 -> 500/200.
    intents = copy.deepcopy(fixtures.IDENTITIES_INTENT)
    for c in intents[fixtures.INTENT_RT_ID]['criteria']:
        if c['criterion'] == 'Latency':
            c['threshold'] = 500
        elif c['criterion'] == 'Jitter':
            c['threshold'] = 200
    tcs2 = normalize.traffic_classes(intents, fixtures.TRAFFIC_CLASS_CFG)
    fp_after = normalize._stable_hash(sorted(
        (normalize.traffic_class_fingerprint_payload(tc) for tc in tcs2),
        key=lambda p: (p.get('intent_id') or '',
                       p.get('traffic_class_id') or '')))
    check(fp_before != fp_after,
          'threshold-only change changes traffic_class fingerprint')


def test_swans_real_shape():
    print('test_swans_real_shape')
    swans = {p['uid']: p for p in
             normalize.swans_paths(fixtures.SWANS)['paths']}
    eth = swans['sdwan-hub0_wan']
    cell = swans['sdwan-hub0_1ea0ac']
    check(eth['latency_ms'] == 9.48 and eth['jitter_ms'] == 1.79,
          'latest Ethernet SWANS 9.48 / 1.79 (from history upd_status)')
    check(cell['latency_ms'] == 111.93 and cell['jitter_ms'] == 9.59,
          'latest Cellular SWANS 111.93 / 9.59')
    # cm/dataused must NOT appear as path records.
    check('cm' not in swans and 'dataused' not in swans,
          'cm/dataused not treated as WAN paths')


def test_qoe_real_shape():
    print('test_qoe_real_shape')
    qoe = {p['uid']: p for p in normalize.qoe_paths(fixtures.QOE)['paths']}
    check(qoe['hub0_wan']['latency_avg_ms'] == 7 and
          qoe['hub0_wan']['latency_last_ms'] == 8,
          'QoE hub0_wan avg=7 last=8 (singular interface)')
    check(qoe['hub0_1ea0ac']['latency_avg_ms'] == 113 and
          qoe['hub0_1ea0ac']['latency_last_ms'] == 112,
          'QoE hub0_1ea0ac avg=113 last=112')
    check('pkt_loss_stats_raw' in qoe['hub0_wan'],
          'pkt-loss-stats preserved raw')


# -------------------------------------------------------------------------
# Per-collector last-known-good cache
# -------------------------------------------------------------------------

def test_per_collector_lkg_cache():
    print('test_per_collector_lkg_cache')
    cache = _RawCache()

    # Pass 1: two keys succeed (one with legit empty value).
    def pass1():
        return {
            'wan_devices': collectors.CollectResult(True, value={'a': 1}),
            'swans': collectors.CollectResult(True, value={}),
        }
    cache.update(pass1)
    check(cache.data['wan_devices'] == {'a': 1}, 'success stores value')
    check(cache.data['swans'] == {}, 'legit empty {} stored as success')

    # Pass 2: wan_devices FAILS, swans succeeds with new empty list.
    def pass2():
        return {
            'wan_devices': collectors.CollectResult(False, error='boom'),
            'swans': collectors.CollectResult(True, value=[]),
        }
    cache.update(pass2)
    check(cache.data['wan_devices'] == {'a': 1},
          'failed read retains previous last-known-good')
    check(cache.data['swans'] == [],
          'successful empty [] replaces old value')
    health = cache.health()
    check(health['wan_devices']['error'] == 'boom',
          'per-collector error recorded')
    check(health['wan_devices']['error_ts'] is not None,
          'per-collector error timestamp recorded')
    check(health['swans']['error'] is None,
          'recovered collector clears error')


def test_steering_single_read_split():
    print('test_steering_single_read_split')
    # Simulate collectors._get_result over the single parent object.
    saved = collectors._get_result
    calls = {'n': 0, 'paths': []}

    def fake(path):
        calls['n'] += 1
        calls['paths'].append(path)
        if path == 'status/wan/steering':
            return collectors.CollectResult(True, value=fixtures.STEERING_PARENT)
        return collectors.CollectResult(True, value=None)

    collectors._get_result = fake
    try:
        split = collectors._steering_split_results()
    finally:
        collectors._get_result = saved

    check(calls['paths'].count('status/wan/steering') == 1,
          'steering parent read exactly once (not four sub-reads)')
    check(split['steering_rules'].value is fixtures.STEERING_RULES,
          'rules split from parent')
    check(split['steering_stats'].value is fixtures.STEERING_STATS,
          'stats split from parent')
    check(split['steering_events'].value is fixtures.STEERING_EVENTS,
          'events split from parent')
    check(split['steering_intents'].ok is True, 'intents split (ok)')


# -------------------------------------------------------------------------
# Service end-to-end + compact history + change detection + privacy
# -------------------------------------------------------------------------

def _service_with_fixtures():
    svc = AnalyzerService()
    svc._base_raw.data = fixtures.base_raw()
    svc._heavy_raw.data = fixtures.heavy_raw()
    return svc


def test_service_compose_schema():
    print('test_service_compose_schema')
    svc = _service_with_fixtures()
    snap = svc.recompose()
    for key in ('timestamp', 'system_summary', 'wan', 'swans',
                'traffic_classes', 'traffic_steering', 'loadbalancers',
                'bonding', 'qoe', 'identities', 'fingerprints', 'changes'):
        check(key in snap, 'snapshot has section: {}'.format(key))
    ts = snap['traffic_steering']
    # LAB01, YouTube, TEST, Management = 4 logical rules.
    check(len(ts['logical_rules']) == 4,
          'four logical rules composed (got {})'.format(
              len(ts['logical_rules'])))
    check(len(wan_overlays(snap)) == 2, 'two SD-WAN overlays')


def wan_overlays(snap):
    return snap.get('wan', {}).get('overlays', [])


def test_compact_history():
    print('test_compact_history')
    svc = _service_with_fixtures()
    svc.recompose()
    samples = svc.history(limit=10)
    check(len(samples) == 1, 'one compact history sample stored')
    sample = samples[0]
    # Compact sample must NOT carry raw passthrough or compiled_rules.
    check('compiled_rules' not in str(sample.keys()),
          'no compiled_rules key in compact sample')
    rules = sample.get('logical_rules', [])
    check(rules and all('raw' not in r for r in rules),
          'no raw passthrough in compact logical rules')
    check(all('fingerprint' in r for r in rules),
          'compact rule carries fingerprint')
    check('wan_states' in sample and 'swans' in sample and 'qoe' in sample,
          'compact sample has analysis-useful sections')
    # Ensure the compact sample is materially smaller than the full snapshot.
    import json
    full = svc.snapshot()
    check(len(json.dumps(sample, default=str)) <
          len(json.dumps(full, default=str)),
          'compact history sample smaller than full snapshot')


def test_service_change_detection():
    print('test_service_change_detection')
    svc = _service_with_fixtures()
    svc.recompose()
    snap2 = svc.recompose()
    check(snap2['changes']['topology'] == [] and snap2['changes']['config'] == [],
          'no changes on identical recompose')

    # Change LAB01 selector Bonded -> All (edit xpolicy trigger to sdwan).
    churned = []
    for r in fixtures.STEERING_RULES:
        c = dict(r)
        if c['id'] == '885823d0-1111-2222-3333-444455556666':
            xp = dict(c['xpolicy'])
            xp['trigger_value'] = 'sdwan'
            xp['trigger_strings'] = [
                {'_id_': 't1', 'trigger_priority': 2,
                 'trigger_string': 'type|is|sdwan'}]
            c['xpolicy'] = xp
            c['matched_devs'] = fixtures._dev('sdwan-hub0_wan',
                                              'sdwan-hub0_1ea0ac')
        churned.append(c)
    base = fixtures.base_raw()
    base['steering_rules'] = churned
    svc._base_raw.data = base
    snap3 = svc.recompose()
    check(any('logical_rule' in c for c in snap3['changes']['config']),
          'selector change detected as config change')


def test_cellular_privacy_filter():
    print('test_cellular_privacy_filter')
    from analyzer.service import normalize_cellular_safe
    safe = normalize_cellular_safe(
        fixtures.WAN_DEVICES['mdm-1ea0ac']['diagnostics'])
    check(safe.get('CARRID') == 'T-Mobile', 'safe carrier surfaced')
    for forbidden in ('IMEI', 'IMSI', 'ICCID', 'MDN', 'GPS'):
        check(forbidden not in safe,
              'sensitive field filtered out: {}'.format(forbidden))


def main():
    tests = [
        test_wan_paths,
        test_selector_from_xpolicy,
        test_dev_dicts,
        test_trigger_strings_list_of_dicts,
        test_lab01_best_effort,
        test_youtube_all,
        test_test_applications,
        test_management_explicit_priority,
        test_runtime_vs_configured_algo,
        test_stats_apps_dict,
        test_fingerprint_stability,
        test_bonding_detail,
        test_loadbalancers,
        test_traffic_classes,
        test_traffic_class_fingerprint_change,
        test_swans_real_shape,
        test_qoe_real_shape,
        test_no_bare_sdwan_adv_collected,
        test_per_collector_lkg_cache,
        test_steering_single_read_split,
        test_service_compose_schema,
        test_compact_history,
        test_service_change_detection,
        test_cellular_privacy_filter,
    ]
    for t in tests:
        t()
    print('')
    if _failures:
        print('RESULT: {} FAILURE(S)'.format(len(_failures)))
        for f in _failures:
            print('  - {}'.format(f))
        return 1
    print('RESULT: ALL PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
