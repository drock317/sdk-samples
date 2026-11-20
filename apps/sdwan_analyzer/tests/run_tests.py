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
          'destination_type application via stats appname (not id prefix)')


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
    # Record every path the runtime/config passes read and assert none is the
    # bare status/sdwan_adv tree.
    saved = collectors._get_result
    seen = []

    def fake(path):
        seen.append(path)
        return collectors.CollectResult(True, value={})

    collectors._get_result = fake
    try:
        collectors.collect_config_snapshot()
        collectors.collect_runtime_snapshot()
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
    svc._runtime_raw.data = fixtures.base_raw()
    svc._config_raw.data = fixtures.heavy_raw()
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
    # LAB01, YouTube, TEST, Management, Teams, Gaming, Multimedia, Netflix.
    check(len(ts['logical_rules']) == 8,
          'eight logical rules composed (got {})'.format(
              len(ts['logical_rules'])))
    check(snap['system_summary']['logical_rule_count'] == 8,
          'logical_rule_count == 8 (Teams branches not double-counted)')
    check(len(wan_overlays(snap)) == 2, 'two SD-WAN overlays')


def wan_overlays(snap):
    return snap.get('wan', {}).get('overlays', [])


def test_compact_history():
    print('test_compact_history')
    import json
    svc = _service_with_fixtures()
    with _fake_collectors():
        svc.collect_startup()
    samples = svc.history(limit=10)
    check(len(samples) == 1, 'one observation stored')
    sample = samples[0]
    check('compiled_rules' not in str(sample.keys()),
          'no compiled_rules key in observation')
    check(all('raw' not in r for r in sample.get('steering', [])),
          'no raw passthrough in steering runtime')
    check(all(k in sample for k in ('wan', 'interfaces', 'swans_samples',
                                    'qoe', 'wbond', 'steering',
                                    'config_versions')),
          'observation has runtime sections + config version refs')
    check('branches' not in json.dumps(sample.get('steering')),
          'static rule definitions not duplicated into the sample')
    full = svc.snapshot()
    check(len(json.dumps(sample, default=str)) <
          len(json.dumps(full, default=str)),
          'observation smaller than full snapshot')


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
    svc._runtime_raw.data = base
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


class _FakeAppData(object):
    """Injected stand-in for cp.get_appdata / cp.put_appdata."""

    def __init__(self, **initial):
        self.store = dict(initial)
        self.puts = []

    def get(self, name):
        return self.store.get(name)

    def put(self, name, value):
        self.puts.append((name, value))
        self.store[name] = value


def _free_port():
    import socket
    s = socket.socket()
    s.bind(('0.0.0.0', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_json(port, path):
    import json
    import urllib.request
    with urllib.request.urlopen(
            'http://127.0.0.1:{}{}'.format(port, path), timeout=5) as r:
        return r.status, json.loads(r.read().decode('utf-8'))


def test_web_port_module_is_the_current_standard():
    print('test_web_port_module_is_the_current_standard')
    from analyzer import web_port
    check(callable(getattr(web_port, 'bind_web_socket_with_retry', None)),
          'bind_web_socket_with_retry present (retry wrapper)')
    check(callable(getattr(web_port, 'stop_app', None)),
          'stop_app present (final-failure stop)')
    r = web_port.PortResult(ok=False)
    check(hasattr(r, 'retryable'), 'PortResult.retryable present')


def test_app_package_ini_is_found_from_subpackage():
    print('test_app_package_ini_is_found_from_subpackage')
    # web_port.py lives in analyzer/, package.ini is one level up. The
    # module's default lookup (beside itself) would miss it, so the app passes
    # app_package_ini() explicitly. Both halves are asserted on purpose.
    from analyzer import web_port
    from analyzer.web import app_package_ini
    path = app_package_ini()
    check(os.path.isfile(path), 'app_package_ini() points at a real file')
    uuid = web_port.read_app_uuid(path)
    check(bool(uuid), 'uuid read from the app-root package.ini')
    check(web_port.read_app_uuid() is None,
          'default lookup (beside web_port.py) finds nothing in this layout')


def test_stop_app_uses_app_root_package_ini():
    print('test_stop_app_uses_app_root_package_ini')
    from analyzer import web_port
    from analyzer.web import app_package_ini
    calls, logs = [], []
    ok = web_port.stop_app(lambda p, v: calls.append((p, v)), logs.append,
                           package_ini=app_package_ini())
    check(ok, 'stop_app reports the request was sent')
    uuid = web_port.read_app_uuid(app_package_ini())
    check(calls == [('control/system/sdk/action', 'stop ' + uuid)],
          'sends control/system/sdk/action = "stop <app uuid>"')
    # The failure mode the explicit path prevents:
    check(web_port.stop_app(lambda p, v: None, logs.append) is False,
          'without the explicit path stop_app cannot find the uuid')


def test_web_server_first_run_and_override():
    print('test_web_server_first_run_and_override')
    from analyzer.web import DevWebServer
    svc = _service_with_fixtures()
    # First run: no value -> discover, persist, serve.
    ad = _FakeAppData()
    srv = DevWebServer(svc, get_appdata=ad.get, put_appdata=ad.put)
    # Same wiring sdwan_analyzer.main() does, so /api/status reports the port.
    svc.set_web_port_provider(srv.port_status)
    started = srv.start()
    try:
        check(started, 'first run starts')
        st = srv.port_status()
        check(st['source'] == 'discovered', 'source is discovered')
        check(ad.store.get('sdwan_analyzer_web_port') == str(st['effective_port']),
              'discovered port persisted to sdwan_analyzer_web_port')
        code, body = _http_json(st['effective_port'], '/api/status')
        check(code == 200 and 'web' in body, '/api/status served over HTTP')
        check(body['web']['effective_port'] == st['effective_port'],
              '/api/status reports the effective port')
    finally:
        srv.stop()
    # Override: an admin-set port is authoritative and bound exactly.
    want = _free_port()
    ad2 = _FakeAppData(sdwan_analyzer_web_port=str(want))
    srv2 = DevWebServer(svc, get_appdata=ad2.get, put_appdata=ad2.put)
    started2 = srv2.start()
    try:
        check(started2, 'configured port starts')
        st2 = srv2.port_status()
        check((st2['effective_port'], st2['source']) == (want, 'configured'),
              'binds exactly the configured port (source=configured)')
        check(ad2.puts == [], 'a configured value is never rewritten')
    finally:
        srv2.stop()


def test_web_server_malformed_value_fails_fast():
    print('test_web_server_malformed_value_fails_fast')
    import time
    from analyzer.web import DevWebServer
    ad = _FakeAppData(sdwan_analyzer_web_port='abc')
    srv = DevWebServer(_service_with_fixtures(), get_appdata=ad.get,
                       put_appdata=ad.put)
    t0 = time.time()
    started = srv.start()
    elapsed = time.time() - t0
    st = srv.port_status()
    check(started is False, 'start() reports failure')
    check(elapsed < 2, 'fails at once, no retry wait ({:.2f}s)'.format(elapsed))
    check(str(st['error']).startswith('non-numeric'), 'error is non-numeric')
    check(st['running'] is False and st['effective_port'] is None,
          'not running, no port')
    check(ad.puts == [] and ad.store['sdwan_analyzer_web_port'] == 'abc',
          'bad value left untouched, nothing written')


def _wan_paths():
    umap = {'sdwan-hub0_wan': 'ethernet-wan', 'sdwan-hub0_1ea0ac': 'mdm-1ea0ac'}
    return normalize.wan_paths(fixtures.WAN_DEVICES, fixtures.PRIMARY_DEVICE,
                               umap)


def _logical(rules=None, wan_paths=True):
    dest_index = normalize.destination_index(fixtures.IDENTITIES_IP)
    return normalize.logical_rules(
        rules if rules is not None else fixtures.STEERING_RULES,
        fixtures.STEERING_STATS, fixtures.STEERING_EVENTS, dest_index,
        _wan_paths() if wan_paths else None)


def test_logical_policy_branches():
    print('test_logical_policy_branches')
    logical = _logical()
    teams = [lr for lr in logical if lr['logical_name'] == 'Lab01 - Teams']
    check(len(teams) == 1, 'two compiled Teams branches -> ONE logical policy')
    t = teams[0]
    check(len(logical) == 8, 'logical_rule_count == 8 (not 9)')
    check(len(t['branches']) == 2, 'Teams has TWO branch records')
    check([b['traffic_class_name'] for b in t['branches']] ==
          ['Best Effort (Data traffic)', 'Interactive (Real-time traffic)'],
          'branches: Best Effort then Interactive')
    check([b['trigger_priority'] for b in t['branches']] == [5, 6],
          'branch trigger priorities 5 and 6 preserved')
    check(t['branches'][0]['compiled_rule_ids'] == ['teams-be'] and
          t['branches'][1]['compiled_rule_ids'] == ['teams-int'],
          'compiled ids live in their own branch')
    for k in ('traffic_class_id', 'runtime_algorithm', 'configured_algorithm',
              'matched_devs', 'steering_to', 'source_lans',
              'wan_selector_type', 'wan_priority_enabled',
              'wan_priority_order', 'failover', 'primary_wandev'):
        if k not in t['branches'][0]:
            check(False, 'branch carries field {}'.format(k))
    check(t['branches'][1]['traffic_class_id'] == fixtures.TC_INTERACTIVE,
          'branch carries its own Traffic Class id')
    check(t['preferred_flows'] == 12 and t['nonpreferred_flows'] == 3,
          'preferred/nonpreferred attach ONCE (not 24/6)')
    check(t['latest_event_reason'] == 'WANs equal, chose default' and
          t['latest_flows_moved'] == 2, 'event attaches once at logical level')
    check('preferred_flows' not in t['branches'][0],
          'flow counts are not copied into branches')
    check(len(t['compiled_rule_ids']) == 2, 'logical lists both compiled ids')


def test_logical_fingerprint_branches():
    print('test_logical_fingerprint_branches')
    base = {lr['logical_name']: lr['semantic_fingerprint']
            for lr in _logical()}
    churned = []
    for i, r in enumerate(fixtures.STEERING_RULES):
        c = dict(r)
        c['id'] = 'regen-{}'.format(i)
        xp = dict(r['xpolicy'])
        xp['_id_'] = 'regen-xp-{}'.format(i)
        c['xpolicy'] = xp
        churned.append(c)
    again = {lr['logical_name']: lr['semantic_fingerprint']
             for lr in _logical(churned)}
    check(base['Lab01 - Teams'] == again['Lab01 - Teams'],
          'Teams fingerprint survives compiled UUID churn')
    check(base == again, 'all logical fingerprints survive UUID churn')

    # Meaningful change inside ONE branch changes the logical fingerprint.
    changed = []
    for r in fixtures.STEERING_RULES:
        c = dict(r)
        if c['id'] == 'teams-int':
            xp = dict(c['xpolicy'])
            xp['trigger_priority'] = 7
            c['xpolicy'] = xp
        changed.append(c)
    chg = {lr['logical_name']: lr for lr in _logical(changed)}
    check(chg['Lab01 - Teams']['semantic_fingerprint'] !=
          base['Lab01 - Teams'], 'branch config change changes fingerprint')
    check(len(chg['Lab01 - Teams']['branches']) == 2 and
          [b['trigger_priority'] for b in
           chg['Lab01 - Teams']['branches']] == [5, 7],
          'branch differences remain represented after grouping')
    check(chg['Lab01 - Youtube']['semantic_fingerprint'] ==
          base['Lab01 - Youtube'], 'unrelated policy fingerprint unchanged')


def test_preferred_wan_correlation():
    print('test_preferred_wan_correlation')
    by_name = {lr['logical_name']: lr for lr in _logical()}
    t = by_name['Lab01 - Teams']
    check(t['preferred_wan_raw'] == 'sdwan-mdm-Internal 5GB (SIM1)-T-Mobile',
          'preferred_wan_raw preserves exact NCOS value')
    check(t['preferred_wan'] == t['preferred_wan_raw'],
          'legacy preferred_wan is the raw value')
    check(t['preferred_wan_uid'] == 'sdwan-hub0_1ea0ac',
          'cellular SD-WAN resolves to sdwan-hub0_1ea0ac')
    inv = {p['uid']: p for p in _wan_paths()}
    check(t['preferred_wan_name'] ==
          inv['sdwan-hub0_1ea0ac']['display_name'] and t['preferred_wan_name'],
          'canonical name is the normalized WAN display name')
    check(t['preferred_wan_match'] == 'steering_to', 'match method recorded')
    lab = by_name['LAB01-Best Effort']
    check(lab['preferred_wan_uid'] == 'wbond-wbond0_hub0' and
          lab['preferred_wan_match'] == 'uid',
          'WBOND resolves to wbond-wbond0_hub0')
    # Unresolvable: no inventory supplied -> raw kept, nothing guessed.
    nolookup = {lr['logical_name']: lr
                for lr in _logical(wan_paths=False)}['Lab01 - Teams']
    check(nolookup['preferred_wan_raw'] == t['preferred_wan_raw'] and
          nolookup['preferred_wan_uid'] is None and
          nolookup['preferred_wan_name'] is None,
          'no inventory -> raw kept, canonical null')
    # Ambiguous: two distinct steering_to uids.
    amb = []
    for r in fixtures.STEERING_RULES:
        c = dict(r)
        if c['id'] == 'teams-int':
            c['steering_to'] = fixtures._dev('sdwan-hub0_wan')
        amb.append(c)
    a = {lr['logical_name']: lr for lr in _logical(amb)}['Lab01 - Teams']
    check(a['preferred_wan_uid'] is None and a['preferred_wan_name'] is None
          and a['preferred_wan_raw'] == t['preferred_wan_raw'],
          'ambiguous steering_to -> not guessed, raw retained')
    # Class mismatch: raw says sdwan but only candidate is a wbond.
    mism = []
    for r in fixtures.STEERING_RULES:
        c = dict(r)
        if r['xpolicy']['name'] == 'Lab01 - Teams':
            c['steering_to'] = fixtures._dev('wbond-wbond0_hub0')
        mism.append(c)
    m = {lr['logical_name']: lr for lr in _logical(mism)}['Lab01 - Teams']
    check(m['preferred_wan_uid'] is None, 'class mismatch -> not guessed')
    # Raw value unrelated to any uid and no candidate.
    check(all(lr['preferred_wan_uid'] is None
              for lr in _logical() if lr['preferred_wan_raw'] is None),
          'no raw value -> no canonical value')


def test_destination_internal_name():
    print('test_destination_internal_name')
    idx = normalize.destination_index(fixtures.IDENTITIES_IP)
    check(idx[fixtures.DST_NETFLIX]['internal_name'] == 'a000012DE',
          'internal_name == identity name (Netflix a000012DE)')
    check(idx[fixtures.DST_GAMING]['internal_name'] == 'c0116',
          'internal_name == identity name (Gaming c0116)')
    check(idx[fixtures.DST_YT_COM]['friendly_name'] == 'Youtube.com' and
          idx[fixtures.DST_YT_COM]['uuid'] == fixtures.DST_YT_COM and
          'raw' in idx[fixtures.DST_YT_COM] and
          'type' in idx[fixtures.DST_YT_COM],
          'uuid/friendly_name/type/raw preserved')
    check(idx[fixtures.DST_NETFLIX]['type'] is None and
          idx[fixtures.DST_GAMING]['type'] is None,
          'type NOT inferred from a.../c... prefix')
    by_name = {lr['logical_name']: lr for lr in _logical()}
    g = by_name['Lab01 - Gaming']
    check(g['destinations'][0]['internal_name'] == 'c0116' and
          g['destination_type'] is None,
          'logical destination keeps internal_name; type stays unknown')


def test_sdwan_wbond_kind():
    print('test_sdwan_wbond_kind')
    by_uid = {p['uid']: p for p in _wan_paths()}
    check(by_uid['sdwan-hub0_1ea0ac']['kind'] == 'sdwan', 'sdwan kind == sdwan')
    check(by_uid['sdwan-hub0_wan']['kind'] == 'sdwan', 'sdwan eth kind == sdwan')
    check(by_uid['wbond-wbond0_hub0']['kind'] == 'wbond', 'wbond kind == wbond')
    check(by_uid['mdm-1ea0ac']['kind'] == 'cellular' and
          by_uid['ethernet-wan']['kind'] == 'ethernet',
          'physical kinds unchanged')


def test_history_logical_once():
    print('test_history_logical_once')
    svc = _service_with_fixtures()
    with _fake_collectors():
        svc.collect_startup()
    sample = svc.history(limit=1)[0]
    names = [r['logical_name'] for r in sample['steering']]
    check(len(names) == 8 and names.count('Lab01 - Teams') == 1,
          'observation stores each logical policy once')
    t = [r for r in sample['steering'] if r['logical_name'] == 'Lab01 - Teams'][0]
    check(t['preferred_wan_uid'] == 'sdwan-hub0_1ea0ac' and
          t['preferred_flows'] == 12,
          'observation carries canonical WAN + flows once')
    defs = svc._config_store.current('steering_definitions')['payload']
    teams = [d for d in defs if d['logical_name'] == 'Lab01 - Teams'][0]
    check([b['traffic_class'] for b in teams['branches']] ==
          ['Best Effort (Data traffic)', 'Interactive (Real-time traffic)'],
          'branch summary lives in the versioned steering definition')


# -------------------------------------------------------------------------
# v0.3.0: telemetry, cellular, SWANS dedupe, acquisition model
# -------------------------------------------------------------------------

import copy                                # noqa: E402
import hashlib                             # noqa: E402
import json                                # noqa: E402
import time                                # noqa: E402
import threading                           # noqa: E402
from analyzer import cellular, telemetry, service as service_mod  # noqa: E402,E501
from analyzer import storage               # noqa: E402


class _fake_collectors(object):
    """Patch collectors.collect_keys: serve fixtures, record every call."""

    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = overrides if overrides is not None else {}

    def __enter__(self):
        self._saved = collectors.collect_keys
        data = dict(fixtures.base_raw())
        data.update(fixtures.heavy_raw())

        def fake(keys):
            keys = tuple(keys)
            self.calls.append(keys)
            data.update(self.overrides)
            return {k: collectors.CollectResult(True, value=data.get(k))
                    for k in keys}
        collectors.collect_keys = fake
        return self

    def __exit__(self, *exc):
        collectors.collect_keys = self._saved


class _Clock(object):
    def __init__(self, t=1790380800):
        self.t = t

    def __call__(self):
        return self.t


def _stats(i, o, ip, op, ie=0, oe=0, idr=0, odr=0):
    return {'in': i, 'out': o, 'ipackets': ip, 'opackets': op,
            'ierrors': ie, 'oerrors': oe, 'idrops': idr, 'odrops': odr,
            'multicast': 0, 'collisions': 0}


def _devices_with_stats(scale=1):
    devs = copy.deepcopy(fixtures.WAN_DEVICES)
    devs['ethernet-wan']['stats'] = _stats(1000 * scale, 2000 * scale,
                                           10 * scale, 20 * scale, 1, 2, 3, 4)
    devs['sdwan-hub0_wan']['stats'] = _stats(500 * scale, 600 * scale,
                                             5 * scale, 6 * scale)
    devs['wbond-wbond0_hub0']['stats'] = _stats(900 * scale, 800 * scale,
                                                9 * scale, 8 * scale)
    return devs


NSA_DIAG = {
    'CARRID': 'Verizon', 'SRVC_TYPE': '5G', 'SRVC_TYPE_DETAILS': 'NSA',
    'RFBAND': 'Band 66', 'RFCHANNEL': '66486', 'LTEBANDWIDTH': '20 MHz',
    'RSRP': -90, 'RSRQ': -11, 'SINR': 10, 'PHY_CELL_ID': '101',
    'CELL_ID': '123456789',
    'RFBAND_5G': 'n77', 'RFBANDWIDTH_5G': '100 MHz', 'RFCHANNEL_5G': '650000',
    'RSRP_5G': -95, 'RSRQ_5G': -12, 'SINR_5G': 8, 'PHY_CELL_ID_5G': '202',
    'BAND_SCELL0': 'Band 2', 'BANDWIDTH_SCELL0': '10 MHz',
    'CHANNEL_SCELL0': '900', 'ACTIVE_SCELL0': 'active',
    'BAND_5G_SCELL0': 'n41', 'BANDWIDTH_5G_SCELL0': '0 MHz',
    'CHANNEL_5G_SCELL0': '500000', 'ACTIVE_5G_SCELL0': 'active',
    'IMEI': '350000000000001', 'IMSI': '310260000000000',
    'ICCID': '8901000000000', 'MDN': '5551234567', 'GPS': '37.0,-122.0',
}


def test_interface_stats_normalization():
    print('test_interface_stats_normalization')
    recs, counters = telemetry.interface_records(
        _devices_with_stats(), {}, 1790380800)
    by = {r['uid']: r for r in recs}
    check(set(by) == {'ethernet-wan', 'sdwan-hub0_wan', 'wbond-wbond0_hub0'},
          'only devices exposing stats are reported')
    check(by['ethernet-wan']['object_class'] == 'physical' and
          by['sdwan-hub0_wan']['object_class'] == 'sdwan' and
          by['wbond-wbond0_hub0']['object_class'] == 'wbond',
          'physical/sdwan/wbond object classes')
    e = by['ethernet-wan']
    check((e['rx_bytes'], e['tx_bytes'], e['rx_packets'], e['tx_packets'])
          == (1000, 2000, 10, 20), 'in/out/ipackets/opackets mapped')
    check((e['rx_errors'], e['tx_errors'], e['rx_drops'], e['tx_drops'])
          == (1, 2, 3, 4), 'errors/drops mapped by documented name')
    check(e['connection_state'] == 'connected', 'connection_state carried')
    check(by['wbond-wbond0_hub0']['rx_bytes'] == 900 and
          by['sdwan-hub0_wan']['tx_packets'] == 6,
          'sdwan + wbond counters normalized')
    check(all(r['metric_ts'] == 1790380800 == r['collected_ts'] and
              r['timestamp_source'] == 'collector' for r in recs),
          'collector timestamp envelope')
    check(not any('mbps' in k or 'rate' in k for r in recs for k in r),
          'no rates computed')
    check(all(r['reset_suspected'] is False for r in recs),
          'first observation: no reset suspected')
    check(counters['ethernet-wan']['rx_bytes'] == 1000, 'counters returned')


def test_counter_decrease_reset_suspected():
    print('test_counter_decrease_reset_suspected')
    _, prev = telemetry.interface_records(
        _devices_with_stats(2), {}, 100)
    recs, _ = telemetry.interface_records(_devices_with_stats(1), prev, 200)
    check(all(r['reset_suspected'] is True for r in recs),
          'decreased counters -> reset_suspected = true')
    check(all(r['rx_bytes'] >= 0 for r in recs) and
          not any('delta' in k for r in recs for k in r),
          'no negative deltas exposed')
    recs2, _ = telemetry.interface_records(_devices_with_stats(3), prev, 300)
    check(all(r['reset_suspected'] is False for r in recs2),
          'increasing counters -> not suspected')


def test_cellular_ca_parsing():
    print('test_cellular_ca_parsing')
    rec = cellular.normalize_cellular('mdm-1', NSA_DIAG, 50, 60, 'collector')
    check(rec['service_mode'] == '5G NSA', 'service mode 5G NSA')
    check(rec['carrier'] == 'Verizon', 'carrier')
    check(rec['carrier_count'] == 4, 'NSA component carriers: 4 (got {})'.format(
        rec['carrier_count']))
    rats = sorted(c['rat'] for c in rec['carriers'])
    check(rats == ['LTE', 'LTE', 'NR', 'NR'], 'LTE anchor + NR components')
    check(rec['total_active_mhz'] == 130.0,
          'total active MHz = 130 (got {})'.format(rec['total_active_mhz']))
    check(rec['zero_mhz_count'] == 1, 'zero-MHz placeholder counted')
    zero = [c for c in rec['carriers'] if c['bandwidth_mhz'] == 0]
    check(len(zero) == 1 and zero[0]['band'] == 'n41',
          'zero-MHz carrier kept, contributes no MHz')
    check(rec['serving_band'] == 'Band 66' and
          rec['serving_bandwidth_mhz'] == 20.0, 'serving band from pcell')
    check((rec['rsrp'], rec['rsrq'], rec['sinr']) == (-90, -11, 10) and
          (rec['rsrp_5g'], rec['rsrq_5g'], rec['sinr_5g']) == (-95, -12, 8),
          'LTE and 5G signal fields')
    # v0.8.3: the legacy fields are unchanged; `rf` is additive.
    check(set(rec['carriers'][0]) == {'role', 'role_aliases', 'rat', 'band',
                                      'bandwidth_mhz', 'channel', 'pci',
                                      'active', 'rf'},
          'carrier entry fields')
    check(rec['timestamp_source'] == 'collector' and rec['metric_ts'] == 50
          and rec['collected_ts'] == 60, 'timestamp envelope')


def test_ca_signature_ignores_roles():
    print('test_ca_signature_ignores_roles')
    carriers = cellular._parse_active_carriers(NSA_DIAG)
    swapped = [dict(c, role='x{}'.format(i)) for i, c in
               enumerate(reversed(carriers))]
    check(cellular.ca_signature(carriers) == cellular.ca_signature(swapped),
          'signature independent of role names and order')
    fewer = carriers[:-1]
    check(cellular.ca_signature(fewer) != cellular.ca_signature(carriers),
          'signature changes when the carrier set changes')


def test_cell_hash_deterministic():
    print('test_cell_hash_deterministic')
    h1 = cellular.cell_key_hash('Verizon', '5G NSA', '123456789', '101',
                                'Band 66')
    h2 = cellular.cell_key_hash('Verizon', '5G NSA', '123456789', '101',
                                'Band 66')
    expect = hashlib.sha256(
        b'Verizon|5G NSA|123456789|101|Band 66').hexdigest()[:16]
    check(h1 == h2 == expect, 'deterministic SHA-256 prefix')
    check(h1 != cellular.cell_key_hash('Verizon', '5G NSA', '123456780',
                                       '101', 'Band 66'),
          'different cell -> different hash')
    check(cellular.cell_key_hash('Verizon', 'LTE', None, '1', 'B2') is None,
          'no cell id -> no hash')
    rec = cellular.normalize_cellular('mdm-1', NSA_DIAG, 1, 1)
    check(rec['cell_key_hash'] == h1, 'record carries the hash')


def test_cellular_sensitive_fields_excluded():
    print('test_cellular_sensitive_fields_excluded')
    rec = cellular.normalize_cellular('mdm-1', NSA_DIAG, 1, 1)
    blob = json.dumps(rec)
    for secret in ('350000000000001', '310260000000000', '8901000000000',
                   '5551234567', '37.0,-122.0', '123456789'):
        check(secret not in blob, 'value not persisted: {}'.format(secret[:4]))
    for key in ('IMEI', 'IMSI', 'ICCID', 'MDN', 'GPS', 'CELL_ID',
                'NR_CELL_ID', 'cell_id'):
        check(key not in blob, 'field not persisted: {}'.format(key))
    bare = copy.deepcopy(fixtures.WAN_DEVICES)
    for d in bare.values():
        d.pop('diagnostics', None)
    check(telemetry.cellular_records(bare, 5) == [],
          'devices without diagnostics produce no record')
    devs = copy.deepcopy(fixtures.WAN_DEVICES)
    devs['mdm-1ea0ac']['diagnostics'] = dict(NSA_DIAG)
    recs = telemetry.cellular_records(devs, 5)
    check(len(recs) == 2 and all(r['uid'].startswith('mdm') for r in recs),
          'cellular records for mdm devices with diagnostics')


def _swans(entries):
    return {'history': [
        {'epoch': ep, 'upd_status': [
            {'wandev': uid, 'criteria': {'Latency': lat, 'Jitter': 1.0}}
            for uid, lat in ups]} for ep, ups in entries]}


def test_swans_dedupe():
    print('test_swans_dedupe')
    seen = telemetry.BoundedSeen()
    resp = _swans([(30, [('a', 10.0)]), (10, [('a', 9.0), ('b', 5.0)]),
                   (30, [('a', 10.0)]),            # dup within response
                   (20, [('a', 9.5)])])
    out = telemetry.swans_new_samples(resp, seen, 999)
    check([(r['uid'], r['metric_ts']) for r in out] ==
          [('a', 10), ('b', 10), ('a', 20), ('a', 30)],
          'within-response duplicates removed, all history walked, sorted')
    check(all(r['timestamp_source'] == 'ncos' and r['collected_ts'] == 999
              for r in out), 'ncos timestamp envelope')
    check(telemetry.swans_new_samples(resp, seen, 1000) == [],
          'same response again -> nothing new (across polls)')
    resp2 = _swans([(30, [('a', 10.0)]), (40, [('a', 8.0), ('b', 6.0)])])
    out2 = telemetry.swans_new_samples(resp2, seen, 1001)
    check([(r['uid'], r['metric_ts']) for r in out2] ==
          [('a', 40), ('b', 40)], 'only unseen (uid, epoch) persisted')
    check(out2[0]['latency_ms'] == 8.0 and 'jitter_ms' in out2[0],
          'latency/jitter normalized')


def test_qoe_wbond_telemetry():
    print('test_qoe_wbond_telemetry')
    q = telemetry.qoe_records(fixtures.QOE, 77)
    check(len(q) == 2 and all(r['timestamp_source'] == 'collector' and
                              r['metric_ts'] == 77 for r in q),
          'QoE uses collector timestamp')
    check(q[0]['pkt_loss_stats_raw'] is not None, 'packet-loss kept raw')
    bonds = normalize.wan_bonding_detail(fixtures.WAN_BONDING)
    w = telemetry.wbond_runtime(bonds, 77)
    check(len(w) >= 1 and w[0]['members'], 'wbond runtime with members')
    m = w[0]['members'][0]
    check(set(m) == {'member_uid', 'configured_weight',
                     'current_download_weight', 'current_upload_weight',
                     'download_flows', 'upload_flows', 'has_loss',
                     'link_state', 'attach_state', 'tunnel_iface'}, 'member fields')
    blob = json.dumps(w)
    check('ds_curr_bw' not in blob and 'last_bw_sample' not in blob and
          'trend' not in blob, 'raw BW fields not persisted as throughput')


def _fresh(clock=None):
    return AnalyzerService(clock=clock or _Clock())


def test_startup_collection_and_sources():
    print('test_startup_collection_and_sources')
    svc = _fresh()
    check(svc.snapshot() is None, 'no data before startup')
    with _fake_collectors() as fc:
        obs = svc.collect_startup()
        check(obs['sample_source'] == 'startup', 'startup source tag')
        check(svc.snapshot() is not None, 'startup makes snapshot available')
        check(len(fc.calls) == 1 and 'identities_ip' in fc.calls[0] and
              'wan_devices' in fc.calls[0],
              'startup reads runtime + config once')
        check(svc.next_scheduled_ts() is not None, 'schedule armed')
        sched = svc.collect_scheduled()
        check(sched['sample_source'] == 'scheduled', 'scheduled source tag')
        started = svc.start_recording('t', max_duration_sec=60)
        svc.recording_tick()
        rec = svc.recording_samples(started['recording']['job_id'])[-1]
        check(rec['sample_source'] == 'recording' and
              rec['job_id'] == started['recording']['job_id'],
              'recording source tag + job_id')
    srcs = [o['sample_source'] for o in svc.history()]
    check(srcs == ['startup', 'scheduled'] and
          set(srcs) <= set(service_mod.SAMPLE_SOURCES),
          'all persisted observations carry a source tag')


def test_schedule_selection():
    print('test_schedule_selection')
    clock = _Clock(1790380800 + 7 * 60)
    svc = _fresh(clock)
    check(svc.schedule_minutes() == 15, 'default 15 minutes')
    check(service_mod.SCHEDULE_CHOICES == (1, 5, 15, 30, 60),
          'choices are 1/5/15/30/60')
    for m in (1, 5, 15, 30, 60):
        check(svc.set_schedule_minutes(m) is True and
              svc.schedule_minutes() == m, '{} min accepted'.format(m))
    for bad in (2, 10, 120, 0, None, '15'):
        check(svc.set_schedule_minutes(bad) is False,
              'rejected: {!r}'.format(bad))
    check(svc.schedule_minutes() == 60, 'rejected values leave schedule')
    svc.set_schedule_minutes(15)
    nxt = svc.next_scheduled_ts()
    off = time.localtime(clock.t).tm_gmtoff or 0
    check(nxt > clock.t and (nxt + off) % (15 * 60) == 0 and
          nxt - clock.t <= 15 * 60, 'next run aligned to the 15-min boundary')
    check(not svc.scheduled_due(nxt - 1) and svc.scheduled_due(nxt),
          'due exactly at the boundary')
    check(AnalyzerService(schedule_minutes=2).schedule_minutes() == 15,
          'invalid constructor interval falls back to default')


def test_no_legacy_polling():
    print('test_no_legacy_polling')
    svc = _fresh()
    for name in ('fast_interval', 'slow_interval', 'refresh_base',
                 'refresh_heavy', 'build_snapshot', 'record_snapshot'):
        check(not hasattr(svc, name), 'legacy API removed: {}'.format(name))
    check(not hasattr(collectors, 'collect_base_snapshot') and
          not hasattr(collectors, 'collect_heavy_snapshot'),
          'legacy fast/slow collector passes removed')
    clock = _Clock()
    svc = _fresh(clock)
    with _fake_collectors() as fc:
        svc.collect_startup()
        n = len(fc.calls)
        for dt in (15, 30, 300, 840):
            clock.t += dt
            svc.snapshot(); svc.section('wan'); svc.status(); svc.history()
            check(not svc.scheduled_due(clock.t) or dt >= 840,
                  'not due {}s after startup'.format(dt))
        check(len(fc.calls) == n,
              'reads (snapshot/section/status/history) never collect')
    src = open(os.path.join(_APP_DIR, 'sdwan_analyzer.py')).read()
    check('refresh_base' not in src and 'fast_interval' not in src and
          'slow_interval' not in src, 'main loop has no fast/slow cadence')


def test_scheduled_config_reconcile():
    print('test_scheduled_config_reconcile')
    clock = _Clock()
    svc = _fresh(clock)
    with _fake_collectors() as fc:
        svc.collect_startup()
        svc.collect_scheduled()
        check('identities_ip' not in fc.calls[1],
              'scheduled sample skips static config reads')
        clock.t += 3600
        svc.collect_scheduled()
        check('identities_ip' in fc.calls[2],
              'config re-read on ~60 min reconciliation')


def test_manual_refresh_targeted():
    print('test_manual_refresh_targeted')
    clock = _Clock()
    svc = _fresh(clock)
    with _fake_collectors() as fc:
        svc.collect_startup()
        due = svc.next_scheduled_ts()
        r = svc.refresh('traffic_classes')
        check(r['ok'] and r['refresh_group'] == 'traffic_classes' and
              set(('refresh_ts', 'duration_ms', 'collector_errors')) <= set(r),
              'refresh metadata returned')
        check(set(fc.calls[-1]) == {'identities_intent',
                                    'traffic_class_config'},
              'traffic_classes reads only its endpoints')
        obs = svc.history()[-1]
        check(obs['sample_source'] == 'manual' and
              obs['refresh_group'] == 'traffic_classes',
              'manual source tagging')
        check('interfaces' not in obs and 'swans_samples' not in obs,
              'targeted refresh yields a partial observation')
        check(svc.next_scheduled_ts() == due and
              svc.schedule_minutes() == 15,
              'manual refresh does not alter the schedule')
        again = svc.refresh('traffic_classes')
        check(not again['ok'] and again['error'] == 'cooldown' and
              len(fc.calls) == 2, 'cooldown rejects repeat within 5 s')
        check(svc.refresh('steering')['ok'], 'cooldown is per group')
        check(set(fc.calls[-1]) == {'steering_rules', 'steering_events',
                                    'steering_stats', 'steering_intents',
                                    'identities_ip', 'identities_intent',
                                    'traffic_class_config'},
              'steering group keys (v0.8.3: + identity/intent/TC deps)')
        clock.t += 6
        check(svc.refresh('traffic_classes')['ok'], 'allowed after cooldown')
        svc.refresh('bonding')
        check(set(fc.calls[-1]) == {'wan_devices', 'wan_bonding',
                                    'wan_bonding_cfg'},
              'bonding group keys (v0.8.3: + configured bonding)')
        svc.refresh('wan_paths')
        check({'wan_devices', 'primary_device', 'swans', 'qoe'} ==
              set(fc.calls[-1]), 'wan_paths group keys')
        svc.refresh('overview')
        check('wan_devices' in fc.calls[-1] and 'qoe' in fc.calls[-1],
              'overview reads current-state endpoints')
        bad = svc.refresh('nope')
        check(not bad['ok'] and 'error' in bad, 'unknown group rejected')


def test_config_version_change_only():
    print('test_config_version_change_only')
    clock = _Clock()
    svc = _fresh(clock)
    with _fake_collectors() as fc:
        o1 = svc.collect_startup()
        o2 = svc.collect_scheduled()
        v1 = o1['config_versions']['traffic_classes']
        v2 = o2['config_versions']['traffic_classes']
        check(v1['version'] == v2['version'] == 1 and v1['changed'] and
              not v2['changed'], 'identical config -> no new version')
        check(len(svc._config_store.versions('traffic_classes')) == 1 and
              len(svc._config_store.versions('steering_definitions')) == 1,
              'static objects stored once, not per sample')
        check(set(o2['config_versions']) >= {
            'traffic_classes', 'destinations', 'wan_rules',
            'steering_definitions', 'wbond_config', 'overlay_underlay',
            'capabilities'}, 'all static object kinds versioned')
        intents = copy.deepcopy(fixtures.IDENTITIES_INTENT)
        for c in intents[fixtures.INTENT_RT_ID]['criteria']:
            if c['criterion'] == 'Latency':
                c['threshold'] = 500
        fc.overrides['identities_intent'] = intents
        clock.t += 3600
        o3 = svc.collect_scheduled()
        v3 = o3['config_versions']['traffic_classes']
        check(v3['changed'] and v3['version'] == 2,
              'semantic change -> new version')
        check(o3['config_versions']['wbond_config']['version'] == 1,
              'unrelated config versions unchanged')


def test_steering_events_once():
    print('test_steering_events_once')
    svc = _fresh()
    with _fake_collectors():
        o1 = svc.collect_startup()
        o2 = svc.collect_scheduled()
    n1 = len(o1['steering_events'])
    check(n1 >= 1 and all(e['timestamp_source'] == 'ncos' for e in
                          o1['steering_events']),
          'events stored with ncos epoch (got {})'.format(n1))
    check(o2['steering_events'] == [], 'events stored only once')
    check('raw' not in json.dumps(o1['steering_events']),
          'event records are compact')


def test_cache_updated_by_collection_only():
    print('test_cache_updated_by_collection_only')
    clock = _Clock()
    svc = _fresh(clock)
    with _fake_collectors() as fc:
        svc.collect_startup()
        t1 = svc.snapshot()['timestamp']
        clock.t += 1
        before = len(fc.calls)
        check(svc.snapshot()['timestamp'] == t1 and len(fc.calls) == before,
              'UI reads latest saved state without collecting')
        svc.refresh('wan_paths')
        check(svc.snapshot() is not None and len(fc.calls) == before + 1,
              'manual refresh updates the cache')


def test_collector_timeout_bound():
    print('test_collector_timeout_bound')
    saved_get, saved_to = collectors.cp.get, collectors.READ_TIMEOUT_S
    release = []

    def slow_get(path, *a, **k):
        t0 = time.time()
        while not release and time.time() - t0 < 2:
            time.sleep(0.02)
        return {'ok': 1}
    collectors.cp.get = slow_get
    collectors.READ_TIMEOUT_S = 0.2
    try:
        t0 = time.time()
        res = collectors._get_result('status/wan/devices')
        check(not res.ok and 'timeout' in res.error and time.time() - t0 < 1,
              'hung read bounded by wall-clock timeout')
        res2 = collectors._get_result('status/wan/primary_device')
        check(not res2.ok and 'busy' in res2.error,
              'follow-up reads fail fast while one is hung')
        release.append(1)
        time.sleep(0.2)
        collectors.cp.get = lambda path, *a, **k: {'ok': 2}
        res3 = collectors._get_result('status/wan/devices')
        check(res3.ok and res3.value == {'ok': 2},
              'reads recover after the hung call finishes')
    finally:
        release.append(1)
        collectors.cp.get, collectors.READ_TIMEOUT_S = saved_get, saved_to
        time.sleep(0.1)


# -------------------------------------------------------------------------
# v0.3.1: current state vs observation, freshness, health
# -------------------------------------------------------------------------

_SECTIONS = ('wan', 'cellular', 'swans', 'qoe', 'bonding',
             'traffic_steering', 'traffic_classes')


def _intents_changed():
    intents = copy.deepcopy(fixtures.IDENTITIES_INTENT)
    for c in intents[fixtures.INTENT_RT_ID]['criteria']:
        if c['criterion'] == 'Latency':
            c['threshold'] = 500
    return intents


def _scheduled_then_manual(group='traffic_classes', overrides=None):
    clock = _Clock()
    svc = _fresh(clock)
    fc = _fake_collectors()
    fc.__enter__()
    svc.collect_startup()
    clock.t += 1080                      # 18:00 -> 18:18
    svc.collect_scheduled()
    full = svc.snapshot()
    clock.t += 420                       # +7 min
    fc.overrides.update(overrides or {})
    r = svc.refresh(group)
    return svc, fc, clock, full, r


def test_scheduled_establishes_current_state():
    print('test_scheduled_establishes_current_state')
    clock = _Clock()
    svc = _fresh(clock)
    with _fake_collectors():
        svc.collect_startup()
        clock.t += 900
        svc.collect_scheduled()
    snap = svc.snapshot()
    fr = snap['section_freshness']
    check(all(k in fr for k in _SECTIONS), 'freshness for every major section')
    check(all(v['sample_source'] == 'scheduled' and
              v['collected_ts'] == clock.t for k, v in fr.items()
              if k != 'traffic_classes' and k != 'loadbalancers'),
          'runtime sections stamped by the scheduled pass')
    check(fr['traffic_classes']['sample_source'] == 'startup',
          'static section keeps the startup stamp (not re-read)')
    check(snap['wan']['paths'] and snap['bonding']['bonds'] and
          snap['qoe']['paths'], 'current state populated')
    check(svc.status()['section_freshness'] == fr, 'status exposes freshness')


def test_partial_manual_refresh_keeps_current_state():
    print('test_partial_manual_refresh_keeps_current_state')
    svc, fc, clock, full, r = _scheduled_then_manual(
        'traffic_classes', {'identities_intent': _intents_changed()})
    try:
        check(r['ok'], 'refresh accepted')
        obs = svc.history()[-1]
        check(obs['sample_source'] == 'manual' and
              not any(k in obs for k in ('wan', 'interfaces', 'cellular',
                                         'wbond', 'steering', 'qoe',
                                         'swans_samples')),
              'persisted observation stays partial')
        check('config_versions' in obs, 'partial observation has config refs')
        cur = svc.snapshot()
        for sec in ('wan', 'bonding', 'qoe', 'traffic_steering'):
            check(cur[sec] == full[sec],
                  'current {} unchanged by traffic_classes refresh'.format(sec))
        check(cur['traffic_classes'] != full['traffic_classes'],
              'current traffic classes reflect the newer refresh')
        fr = cur['section_freshness']
        check(fr['traffic_classes'] == {'collected_ts': clock.t,
                                        'sample_source': 'manual'},
              'traffic_classes freshness advanced, source=manual')
        for sec in ('wan', 'cellular', 'swans', 'qoe', 'bonding',
                    'traffic_steering'):
            check(fr[sec]['collected_ts'] == clock.t - 420 and
                  fr[sec]['sample_source'] == 'scheduled',
                  '{} freshness not advanced (source scheduled)'.format(sec))
    finally:
        fc.__exit__()


def test_bonding_refresh_leaves_unrelated_intact():
    print('test_bonding_refresh_leaves_unrelated_intact')
    svc, fc, clock, full, r = _scheduled_then_manual('bonding')
    try:
        cur = svc.snapshot()
        check(cur['traffic_classes'] == full['traffic_classes'] and
              cur['identities'] == full['identities'] and
              cur['wan']['config'] == full['wan']['config'],
              'config-derived sections intact after bonding refresh')
        check(cur['traffic_steering'] == full['traffic_steering'] and
              cur['qoe'] == full['qoe'], 'steering/QoE intact')
        fr = cur['section_freshness']
        check(fr['bonding']['sample_source'] == 'manual' and
              fr['bonding']['collected_ts'] == clock.t and
              fr['qoe']['sample_source'] == 'scheduled' and
              fr['traffic_classes']['sample_source'] == 'startup',
              'only bonding (and its devices) freshness advanced')
        obs = svc.history()[-1]
        check('wbond' in obs and 'qoe' not in obs and 'steering' not in obs,
              'bonding observation partial')
    finally:
        fc.__exit__()


def test_failed_section_does_not_advance_freshness():
    print('test_failed_section_does_not_advance_freshness')
    clock = _Clock()
    svc = _fresh(clock)
    with _fake_collectors():
        svc.collect_startup()
    t0 = clock.t
    clock.t += 60
    saved = collectors.collect_keys
    collectors.collect_keys = lambda keys: {
        k: collectors.CollectResult(False, error='x', error_type='timeout')
        for k in keys}
    try:
        svc.collect_scheduled()
    finally:
        collectors.collect_keys = saved
    fr = svc.snapshot()['section_freshness']
    check(all(v['collected_ts'] == t0 for v in fr.values()),
          'failed collection does not manufacture freshness')
    check(svc.snapshot()['wan']['paths'], 'last-known-good state retained')


def test_api_after_partial_refresh():
    print('test_api_after_partial_refresh')
    import threading
    import urllib.request
    from http.server import HTTPServer
    from analyzer import web
    svc, fc, clock, full, _ = _scheduled_then_manual('bonding')
    port = _free_port()
    httpd = HTTPServer(('127.0.0.1', port), web._make_handler(svc))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        clock.t += 6
        fc.overrides['identities_intent'] = _intents_changed()
        req = urllib.request.Request(
            'http://127.0.0.1:{}/api/refresh/traffic_classes'.format(port),
            method='POST')
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        check(body['ok'] and body['refresh_group'] == 'traffic_classes',
              'POST /api/refresh/traffic_classes ok')
        for path, sec in (('/api/wan', 'wan'), ('/api/bonding', 'bonding'),
                          ('/api/qoe', 'qoe'),
                          ('/api/traffic_steering', 'traffic_steering')):
            code, data = _http_json(port, path)
            check(code == 200 and data == full[sec] or
                  (sec == 'bonding' and code == 200 and data['bonds']),
                  'GET {} still returns prior content'.format(path))
        code, tcs = _http_json(port, '/api/traffic_classes')
        check(tcs != full['traffic_classes'], 'traffic classes updated')
        code, snap = _http_json(port, '/api/snapshot')
        check(snap['section_freshness']['traffic_classes']['sample_source']
              == 'manual' and snap['wan']['paths'],
              '/api/snapshot is the merged current state')
        code, hist = _http_json(port, '/api/history')
        check(hist['samples'][-1]['sample_source'] == 'manual' and
              'wan' not in hist['samples'][-1],
              '/api/history serves the partial persisted observation')
    finally:
        httpd.shutdown()
        httpd.server_close()
        fc.__exit__()


def test_loadbalancer_expensive_runtime():
    print('test_loadbalancer_expensive_runtime')
    check(collectors.key_class('loadbalancers') ==
          'expensive_runtime', 'loadbalancers classified expensive_runtime')
    check(collectors.key_class('wan_rules2') == 'static_config' and
          collectors.key_class('wan_devices') == 'runtime',
          'other classes unchanged')
    check('loadbalancers' not in collectors.RUNTIME_KEYS,
          'not in the per-observation runtime set')
    check('loadbalancers' in collectors.CONFIG_KEYS,
          'still read at startup/reconciliation only')
    check('loadbalancers' not in collectors.STATIC_CONFIG_KEYS,
          'not treated as static configuration')
    clock = _Clock()
    svc = _fresh(clock)
    with _fake_collectors() as fc:
        svc.collect_startup()
        svc.collect_scheduled()
        check('loadbalancers' not in fc.calls[1],
              'not read by a normal scheduled sample')
        check(svc.refresh('loadbalancers')['ok'] and
              fc.calls[-1] == ('loadbalancers',),
              'targeted manual refresh available')


def test_health_error_types():
    print('test_health_error_types')
    cache = service_mod._RawCache()
    R = collectors.CollectResult
    cache.update(lambda: {'a': R(True, value=1), 'b': R(True, value=1),
                          'c': R(True, value=1)})
    cache.update(lambda: {
        'a': R(False, error='timeout after 15s', error_type='timeout'),
        'b': R(False, error='collector busy', error_type='busy'),
        'c': R(False, error='boom', error_type='api_error')})
    h = cache.health()
    check((h['a']['error_type'], h['b']['error_type'], h['c']['error_type'])
          == ('timeout', 'busy', 'api_error'),
          'timeout / busy / api_error distinguished')
    check(h['a']['last_success_ts'] is not None and
          h['a']['last_error_ts'] is not None and
          h['a']['last_error'] == 'timeout after 15s',
          'last_success/last_error fields')
    cache.update(lambda: {'a': R(True, value=2)})
    h = cache.health()
    check(h['a']['error'] is None and h['a']['error_type'] is None and
          h['a']['last_error_type'] == 'timeout',
          'current error clears, last_error history retained')


def test_timeout_busy_types_and_single_worker():
    print('test_timeout_busy_types_and_single_worker')
    saved_get, saved_to = collectors.cp.get, collectors.READ_TIMEOUT_S
    release, calls = [], []

    def slow_get(path, *a, **k):
        calls.append(path)
        t0 = time.time()
        while not release and time.time() - t0 < 2:
            time.sleep(0.02)
        return {}
    collectors.cp.get = slow_get
    collectors.READ_TIMEOUT_S = 0.15
    try:
        base = threading.active_count()
        r1 = collectors._get_result('status/wan/devices')
        check(r1.error_type == 'timeout', 'timeout typed as timeout')
        rs = [collectors._get_result('status/wan/swans') for _ in range(5)]
        check(all(r.error_type == 'busy' for r in rs),
              'later reads typed as busy')
        check(len(calls) == 1 and threading.active_count() <= base + 1,
              'no overlapping worker threads while one call is pending')
        k = collectors.collect_keys(('wan_devices', 'qoe'))
        check(all(r.error_type == 'busy' for r in k.values()) and
              len(calls) == 1, 'whole passes fail fast, no new threads')
        collectors.cp.get = lambda p, *a, **kw: (_ for _ in ()).throw(
            RuntimeError('x'))
        release.append(1)
        time.sleep(0.3)
        r2 = collectors._get_result('status/wan/devices')
        check(r2.error_type == 'api_error', 'raised error typed api_error')
    finally:
        release.append(1)
        collectors.cp.get, collectors.READ_TIMEOUT_S = saved_get, saved_to
        time.sleep(0.1)


# -------------------------------------------------------------------------
# v0.3.2: Recording jobs
# -------------------------------------------------------------------------

from analyzer import recording as rec_mod  # noqa: E402

_STATIC = collectors.STATIC_CONFIG_KEYS


class _RecEnv(object):
    """Service + fake collectors that log (clock, keys) and concurrency."""

    def __init__(self, fail=(), t0=1790380800):
        self.clock = _Clock(t0)
        self.svc = _fresh(self.clock)
        self.log = []
        self.overrides = {}
        self.fail = set(fail)
        self.active = 0
        self.max_active = 0
        self._saved = None

    def __enter__(self):
        self._saved = collectors.collect_keys
        data = dict(fixtures.base_raw())
        data.update(fixtures.heavy_raw())

        def fake(keys):
            keys = tuple(keys)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.log.append((self.clock.t, keys))
            data.update(self.overrides)
            out = {}
            for k in keys:
                if k in self.fail:
                    out[k] = collectors.CollectResult(
                        False, error='boom', error_type='api_error')
                else:
                    out[k] = collectors.CollectResult(True, value=data.get(k))
            self.active -= 1
            return out
        collectors.collect_keys = fake
        return self

    def __exit__(self, *exc):
        collectors.collect_keys = self._saved

    def run(self, seconds):
        for _ in range(seconds):
            self.svc.recording_tick()
            self.clock.t += 1

    def calls(self, keys):
        return [t for t, k in self.log if k == tuple(keys)]


def test_recording_start_validation():
    print('test_recording_start_validation')
    with _RecEnv() as env:
        svc = env.svc
        r = svc.start_recording('speed test', 'notes', 600, created_by_ui=True)
        job = r['recording']
        check(r['ok'] and job['state'] == 'recording' and
              job['max_duration_sec'] == 600 and job['label'] == 'speed test'
              and job['created_by_ui'] is True and job['ended_ts'] is None and
              job['sample_profile'] == 'default_v1' and
              job['stop_reason'] is None and job['job_id'].startswith('rec-'),
              'valid start creates a recording job')
        dup = svc.start_recording('x', max_duration_sec=60)
        check(not dup['ok'] and dup['error'] == 'active_exists',
              'second simultaneous Recording rejected')
        svc.stop_recording(job['job_id'])
        for bad in (0, -1, 1801, 99999, 'abc', True, None.__class__):
            res = svc.start_recording('x', max_duration_sec=bad)
            check(not res['ok'] and res['error'] == 'invalid',
                  'rejected duration {!r}'.format(bad))
        check(svc.start_recording('x', max_duration_sec=1800)['ok'],
              'exactly 30 minutes accepted')
        check(rec_mod.MAX_DURATION_SEC == 1800, 'hard cap is 1800 s')
        ids = [j['job_id'] for j in svc.list_recordings()]
        check(len(ids) == len(set(ids)) == 2, 'job ids unique')


def test_recording_backend_owns_clock():
    print('test_recording_backend_owns_clock')
    with _RecEnv() as env:
        svc = env.svc
        job = svc.start_recording('t', max_duration_sec=300)['recording']
        a0 = svc.active_recording()
        env.clock.t += 120              # client gone; no tick needed for time
        a1 = svc.active_recording()
        env.clock.t += 60
        a2 = svc.active_recording()
        check(a0['elapsed_sec'] == 0 and a1['elapsed_sec'] == 120 and
              a2['elapsed_sec'] == 180, 'elapsed advances with backend clock')
        check(a1['remaining_sec'] == 180 and a2['remaining_sec'] == 120,
              'remaining decreases')
        check(a2['job_id'] == job['job_id'] and a2['state'] == 'recording',
              'job survives client disappearance')
        env.run(1)
        check(svc.active_recording() is not None, 'still active after tick')
        st = svc.status()['recording']
        check(st['active'] and st['job_id'] == job['job_id'] and
              set(('state', 'started_ts', 'elapsed_sec', 'remaining_sec',
                   'max_duration_sec', 'sample_count',
                   'collector_errors')) <= set(st),
              '/api/status recording block while active')
        svc.stop_recording(job['job_id'])
        check(svc.status()['recording']['active'] is False,
              'status recording block when idle')


def test_recording_stop_expiry_events():
    print('test_recording_stop_expiry_events')
    with _RecEnv() as env:
        svc = env.svc
        j1 = svc.start_recording('a', max_duration_sec=60)['recording']
        env.run(3)
        check(svc.stop_recording('nope')['error'] == 'not_found',
              'stop unknown job rejected')
        r = svc.stop_recording(j1['job_id'])
        check(r['ok'] and r['recording']['state'] == 'stopped' and
              r['recording']['stop_reason'] == 'user' and
              r['recording']['ended_ts'] is not None, 'manual stop')
        check(svc.stop_recording(j1['job_id'])['error'] == 'not_active',
              'stop only an active job')
        env.run(1)
        j2 = svc.start_recording('b', max_duration_sec=30)['recording']
        env.run(29)
        check(svc.active_recording() is not None, 'active before expiry')
        env.run(2)
        d = svc.get_recording(j2['job_id'])
        check(d['state'] == 'expired' and d['stop_reason'] == 'max_duration'
              and svc.active_recording() is None, 'automatic expiry')
        ev1 = [e['event_type'] for e in svc.get_recording(
            j1['job_id'])['events']]
        ev2 = [e['event_type'] for e in d['events']]
        check(ev1 == ['recording_started', 'recording_stopped'] and
              ev2 == ['recording_started', 'recording_expired'],
              'lifecycle events persisted')
        e = d['events'][-1]
        check(set(('job_id', 'event_ts', 'event_type', 'reason')) <= set(e)
              and e['reason'] == 'max_duration', 'event record fields')
        check(d['remaining_sec'] == 0 and d['elapsed_sec'] <= 31,
              'ended job timing frozen')


def test_recording_cadence_tagging_dedupe():
    print('test_recording_cadence_tagging_dedupe')
    with _RecEnv() as env:
        svc = env.svc
        job = svc.start_recording('c', max_duration_sec=300)['recording']
        t0 = env.clock.t
        env.run(31)

        def gaps(keys):
            ts = env.calls(keys)
            return [b - a for a, b in zip(ts, ts[1:])]
        wan_keys = ('wan_devices', 'primary_device')
        check(set(gaps(wan_keys)) == {5} and
              len(env.calls(wan_keys)) == 7, 'WAN cadence 5 s '
              '(wan_devices + primary_device in one serial group)')
        check(not env.calls(('wan_devices',)) and
              not env.calls(('primary_device',)),
              'primary_device is read with the WAN group, not on its own')
        check(set(gaps(('wan_bonding',))) == {5}, 'WBOND cadence 5 s')
        steer = ('steering_rules', 'steering_events', 'steering_stats',
                 'steering_intents')
        check(set(gaps(steer)) == {10} and len(env.calls(steer)) == 4,
              'steering cadence 10 s (one parent read)')
        check(set(gaps(('qoe',))) == {10}, 'QoE cadence 10 s')
        check(set(gaps(('swans',))) == {10}, 'SWANS cadence 10 s')
        check(env.calls(_STATIC) == [t0], 'static config read once at start')
        check(not any('loadbalancers' in k for _, k in env.log),
              'loadbalancers never polled during Recording')
        check(env.max_active == 1, 'reads strictly serial')
        samples = svc.recording_samples(job['job_id'])
        check(samples and all(s['sample_source'] == 'recording' and
                              s['job_id'] == job['job_id']
                              for s in samples),
              'every sample: sample_source=recording + job_id')
        check(svc.history() == [], 'recording samples not mixed into history')
        sw = [s for s in samples if s.get('recording_group') == 'swans']
        check(len(sw) == 4 and len(sw[0]['swans_samples']) == 4 and
              all(s['swans_samples'] == [] for s in sw[1:]),
              'SWANS unseen (uid, epoch) only')
        d = svc.get_recording(job['job_id'])
        check(d['sample_counts']['wan_samples'] == 7 and
              d['sample_counts']['steering_samples'] == 4 and
              d['sample_count'] == len(samples), 'sample counts exposed')
        check('samples' not in d and 'interfaces' not in json.dumps(d),
              'details do not dump telemetry')
        check(svc.refresh('loadbalancers')['error'].startswith('not allowed'),
              'loadbalancer refresh blocked during Recording')


def test_recording_config_start_end_and_schedule():
    print('test_recording_config_start_end_and_schedule')
    with _RecEnv() as env:
        svc = env.svc
        svc.collect_startup()
        sched, due = svc.schedule_minutes(), svc.next_scheduled_ts()
        hist = len(svc.history())
        n0 = len(env.log)
        job = svc.start_recording('c', max_duration_sec=300)['recording']
        env.run(8)
        n1 = len(env.log)
        env.clock.t = due + 1
        check(svc.scheduled_due() and svc.collect_scheduled() is None and
              len(env.log) == n1 and len(svc.history()) == hist,
              'scheduled collection skipped while Recording (no duplicate '
              'reads)')
        check(svc.schedule_minutes() == sched and
              svc.next_scheduled_ts() > due, 'schedule setting unchanged')
        env.clock.t = env.clock.t - 1 + 0
        env.overrides['identities_intent'] = _intents_changed()
        svc.stop_recording(job['job_id'])
        env.run(1)
        check(env.calls(_STATIC)[-1] >= env.clock.t - 2 and
              len(env.calls(_STATIC)) == 2,
              'static config read at start and at end')
        d = svc.get_recording(job['job_id'])
        check(d['config_versions_start']['traffic_classes'] == 1 and
              d['config_versions_end']['traffic_classes'] == 2,
              'config version refs at start and end')
        check([c['name'] for c in d['config_changes']] == ['traffic_classes']
              and d['events'][-1]['event_type'] ==
              'recording_config_changed', 'config change event emitted')
        check(len(svc._config_store.versions('traffic_classes')) == 2 and
              len(svc._config_store.versions('wan_rules')) == 1,
              'changed config versioned; unchanged not duplicated')
        check('payload' not in json.dumps(
            svc.recording_samples(job['job_id'])),
            'static payloads not copied into samples')
        check(svc.collect_scheduled()['sample_source'] == 'scheduled',
              'scheduled collection resumes after Recording')
    with _RecEnv() as env:
        job = env.svc.start_recording('c', max_duration_sec=60)['recording']
        env.run(2)
        env.svc.stop_recording(job['job_id'])
        env.run(1)
        d = env.svc.get_recording(job['job_id'])
        check(d['config_changes'] == [] and not any(
            e['event_type'] == 'recording_config_changed'
            for e in d['events']), 'unchanged config -> no change event')


def test_recording_partial_failure_and_infra_failure():
    print('test_recording_partial_failure_and_infra_failure')
    with _RecEnv(fail=('qoe',)) as env:
        svc = env.svc
        job = svc.start_recording('f', max_duration_sec=300)['recording']
        env.run(21)
        d = svc.get_recording(job['job_id'])
        check(d['state'] == 'recording' and d['collector_errors'] >= 3 and
              d['collector_health']['qoe']['last_error_type'] ==
              'api_error', 'collector failure does not kill the job')
        check(d['sample_counts']['wan_samples'] >= 4,
              'other collectors keep recording')
        def boom(*a, **k):
            raise RuntimeError('store down')
        svc.collect = boom
        env.run(6)
        d = svc.get_recording(job['job_id'])
        check(d['state'] == 'failed' and
              d['events'][-1]['event_type'] == 'recording_failed',
              'infrastructure failure -> failed + event')
    with _RecEnv() as env:
        svc = env.svc
        svc._rec_store._max_samples = 6
        job = svc.start_recording('cap', max_duration_sec=300)['recording']
        env.run(20)
        d = svc.get_recording(job['job_id'])
        check(d['state'] == 'expired' and d['stop_reason'] == 'sample_cap' and
              d['sample_count'] <= 7, 'sample cap bounds memory')


def test_recording_timeout_busy_no_overlap():
    print('test_recording_timeout_busy_no_overlap')
    saved_get, saved_to = collectors.cp.get, collectors.READ_TIMEOUT_S
    release, calls = [], []

    def slow_get(path, *a, **k):
        calls.append(path)
        t0 = time.time()
        while not release and time.time() - t0 < 2:
            time.sleep(0.02)
        return {}
    collectors.cp.get = slow_get
    collectors.READ_TIMEOUT_S = 0.1
    try:
        svc = _fresh(_Clock())
        job = svc.start_recording('t', max_duration_sec=300)['recording']
        base = threading.active_count()
        svc.recording_tick()
        d = svc.get_recording(job['job_id'])
        types = {h['last_error_type'] for h in d['collector_health'].values()}
        check(d['state'] == 'recording' and 'timeout' in types and
              'busy' in types, 'timeout + busy recorded, job continues')
        check(len(calls) == 1 and threading.active_count() <= base + 1,
              'no overlapping workers during Recording')
        svc._clock.t += 5
        svc.recording_tick()
        check(len(calls) == 1 and
              svc.get_recording(job['job_id'])['state'] == 'recording',
              'still no new worker while the call is pending')
    finally:
        release.append(1)
        collectors.cp.get, collectors.READ_TIMEOUT_S = saved_get, saved_to
        time.sleep(0.2)


def _post(port, path, body=None):
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode('utf-8') if body is not None else b''
    req = urllib.request.Request(
        'http://127.0.0.1:{}{}'.format(port, path), data=data, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))


def _get(port, path):
    import urllib.error
    try:
        return _http_json(port, path)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))


def test_recording_http_api():
    print('test_recording_http_api')
    from http.server import HTTPServer
    from analyzer import web
    with _RecEnv() as env:
        svc = env.svc
        port = _free_port()
        httpd = HTTPServer(('127.0.0.1', port), web._make_handler(svc))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            check(_get(port, '/api/recordings') == (200, {'recordings': []}),
                  'GET /api/recordings (empty)')
            check(_get(port, '/api/recordings/active') ==
                  (200, {'recording': None}), 'GET active -> null')
            code, body = _post(port, '/api/recordings/start',
                               {'label': 'L', 'notes': 'N',
                                'max_duration_sec': 1801})
            check(code == 400 and body['error'] == 'invalid',
                  'POST start >30 min -> 400')
            code, body = _post(port, '/api/recordings/start',
                               {'max_duration_sec': 0})
            check(code == 400, 'POST start duration 0 -> 400')
            code, body = _post(port, '/api/recordings/start',
                               {'label': 'L', 'notes': 'N',
                                'max_duration_sec': 120})
            job = body['recording']
            check(code == 200 and job['state'] == 'recording' and
                  job['created_by_ui'] is True, 'POST start')
            check(_post(port, '/api/recordings/start',
                        {'max_duration_sec': 60})[0] == 409,
                  'second start -> 409')
            env.run(6)
            env.clock.t += 40             # "browser closed" for a while
            code, act = _get(port, '/api/recordings/active')
            check(code == 200 and act['recording']['job_id'] ==
                  job['job_id'] and act['recording']['elapsed_sec'] >= 46,
                  'active job still there; elapsed advanced')
            code, st = _get(port, '/api/status')
            check(st['recording']['active'] and st['recording'][
                'sample_count'] > 0, '/api/status recording block')
            code, det = _get(port, '/api/recordings/' + job['job_id'])
            check(code == 200 and 'sample_counts' in det and
                  'collector_health' in det and det['events'], 'details')
            code, lst = _get(port, '/api/recordings')
            check(len(lst['recordings']) == 1 and
                  'events' not in lst['recordings'][0],
                  'list returns metadata only')
            check(_get(port, '/api/recordings/rec-nope')[0] == 404,
                  'unknown job -> 404')
            check(_post(port, '/api/recordings/rec-nope/stop')[0] == 404,
                  'stop unknown -> 404')
            code, body = _post(port, '/api/recordings/{}/stop'.format(
                job['job_id']))
            check(code == 200 and body['recording']['state'] == 'stopped',
                  'POST stop')
            check(_post(port, '/api/recordings/{}/stop'.format(
                job['job_id']))[0] == 409, 'stop again -> 409')
            check(_get(port, '/api/recordings/active')[1] ==
                  {'recording': None}, 'no active after stop')
        finally:
            httpd.shutdown()
            httpd.server_close()


# -------------------------------------------------------------------------
# v0.3.3: web connection handling, current telemetry APIs, numeric RF
# -------------------------------------------------------------------------
import socket                              # noqa: E402

_PRIVATE_VALUES = ('350000000000001', '310260000000000', '8901000000000',
                   '5551234567', '37.0,-122.0', '123456789')
_PRIVATE_KEYS = ('IMEI', 'IMSI', 'ICCID', 'MDN', 'GPS', 'CELL_ID',
                 'NR_CELL_ID', 'cell_id', 'imei', 'imsi', 'iccid', 'mdn',
                 'gps')


def _tel_devices(scale=1, rsrp='-89', sinr='16.2'):
    """WAN devices with stats on eth/wbond/mdm and an NSA modem."""
    devs = _devices_with_stats(scale)
    diag = dict(NSA_DIAG)
    diag.update({'RSRP': rsrp, 'SINR': sinr, 'RSRQ': '-14',
                 'RSRP_5G': '-72', 'RSRQ_5G': '-11', 'SINR_5G': '32.0'})
    devs['mdm-1ea0ac']['diagnostics'] = diag
    devs['mdm-1ea0ac']['stats'] = _stats(7000 * scale, 8000 * scale,
                                         70 * scale, 80 * scale, 0, 0,
                                         8031, 0)
    return devs


def _modem(snap, uid='mdm-1ea0ac'):
    return [m for m in snap['cellular']['modems'] if m['uid'] == uid][0]


def _iface(snap, uid):
    return [i for i in snap['wan']['interfaces'] if i['uid'] == uid][0]


# --- numeric RF ----------------------------------------------------------

def test_rf_numeric_normalization():
    print('test_rf_numeric_normalization')
    rf = cellular.rf_number
    check(rf('-89') == -89.0 and isinstance(rf('-89'), float),
          'negative integer string -> float -89.0')
    check(rf('16.2') == 16.2, 'decimal string -> float')
    check(rf(' -11 ') == -11.0, 'whitespace tolerated')
    check(rf(-90) == -90.0 and isinstance(rf(-90), float),
          'int -> float')
    check(rf(32.0) == 32.0, 'float stays float')
    for bad in (None, '', '  ', 'abc', '--', 'n/a', 'NaN', 'inf', '-inf',
                '12dBm', True, [], {}):
        check(rf(bad) is None, 'unusable {!r} -> None'.format(bad))
    check(rf('0') == 0.0, 'a real zero stays zero')
    rec = cellular.normalize_cellular(
        'mdm-1', dict(NSA_DIAG, RSRP='-89', RSRQ='-14', SINR='16.2',
                      RSRP_5G='-72', RSRQ_5G='-11', SINR_5G='32.0'),
        100, 100)
    check((rec['rsrp'], rec['rsrq'], rec['sinr']) == (-89.0, -14.0, 16.2),
          'LTE RF fields are numbers')
    check((rec['rsrp_5g'], rec['rsrq_5g'], rec['sinr_5g']) ==
          (-72.0, -11.0, 32.0), '5G RF fields are numbers')
    check(all(isinstance(rec[k], float) for k in (
        'rsrp', 'rsrq', 'sinr', 'rsrp_5g', 'rsrq_5g', 'sinr_5g')),
        'RF values are JSON numbers (float), not strings')
    bad = cellular.normalize_cellular(
        'mdm-1', dict(NSA_DIAG, RSRP='garbage', RSRQ='', SINR=None,
                      RSRP_5G='n/a', RSRQ_5G='x', SINR_5G='--'), 100, 100)
    check(all(bad[k] is None for k in (
        'rsrp', 'rsrq', 'sinr', 'rsrp_5g', 'rsrq_5g', 'sinr_5g')),
        'malformed/empty/missing RF -> null, never zero')
    check(bad['carrier_count'] == rec['carrier_count'] and
          bad['ca_signature'] == rec['ca_signature'],
          'malformed RF does not disturb CA parsing')
    lte = cellular.normalize_cellular(
        'mdm-2', {'CARRID': 'AT&T', 'SRVC_TYPE': 'LTE', 'RFBAND': 'Band 14',
                  'RFCHANNEL': '5330', 'LTEBANDWIDTH': '10 MHz',
                  'RSRP': '-92', 'RSRQ': '-18', 'SINR': '9.0'}, 1, 1)
    check(lte['rsrp'] == -92.0 and lte['sinr'] == 9.0 and
          lte['rsrp_5g'] is None and lte['sinr_5g'] is None,
          'LTE-only modem: 5G RF stays null (not zero)')
    check(json.loads(json.dumps(rec))['rsrp'] == -89.0,
          'serializes as a JSON number')
    check(rec['serving_bandwidth_mhz'] is not None and
          isinstance(rec['carrier_count'], int),
          'other numeric fields keep their existing types')


# --- current-state WAN telemetry -----------------------------------------

def test_current_wan_interface_telemetry():
    print('test_current_wan_interface_telemetry')
    clock = _Clock()
    svc = _fresh(clock)
    with _fake_collectors({'wan_devices': _tel_devices(1)}) as fc:
        svc.collect_startup()
        snap = svc.snapshot()
        ifs = snap['wan']['interfaces']
        check({i['uid'] for i in ifs} == {
            'ethernet-wan', 'mdm-1ea0ac', 'sdwan-hub0_wan',
            'wbond-wbond0_hub0'}, 'current WAN interfaces exposed')
        e = _iface(snap, 'ethernet-wan')
        for k in ('uid', 'kind', 'object_class', 'connection_state',
                  'rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets',
                  'rx_errors', 'tx_errors', 'rx_drops', 'tx_drops',
                  'reset_suspected', 'metric_ts', 'collected_ts',
                  'timestamp_source'):
            check(k in e, 'interface field present: {}'.format(k))
        check((e['rx_bytes'], e['tx_bytes'], e['rx_drops'],
               e['tx_drops']) == (1000, 2000, 3, 4) and
              e['kind'] == 'ethernet' and
              e['timestamp_source'] == 'collector', 'values + kind')
        check(_iface(snap, 'mdm-1ea0ac')['rx_drops'] == 8031,
              'drop counters exposed as raw counters, untouched')
        check(not any('mbps' in k or 'rate' in k for i in ifs for k in i),
              'no rates computed')
        check(not any(k in i for i in ifs for k in ('stats', 'diagnostics')),
              'normalized records, not raw NCOS')
        # /api/wan section serves the same data.
        check(svc.section('wan')['interfaces'] == ifs,
              'wan section == snapshot wan interfaces')
        # Scheduled pass advances current counters.
        clock.t += 900
        fc.overrides['wan_devices'] = _tel_devices(3)
        svc.collect_scheduled()
        e2 = _iface(svc.snapshot(), 'ethernet-wan')
        check(e2['rx_bytes'] == 3000 and e2['collected_ts'] == clock.t and
              e2['reset_suspected'] is False,
              'scheduled update visible in current state')
        # Unrelated partial refresh keeps WAN telemetry.
        before = copy.deepcopy(svc.snapshot()['wan']['interfaces'])
        clock.t += 60
        svc.refresh('traffic_classes')
        check(svc.snapshot()['wan']['interfaces'] == before,
              'traffic_classes refresh does not erase WAN telemetry')
        clock.t += 60
        svc.refresh('steering')
        check(svc.snapshot()['wan']['interfaces'] == before,
              'steering refresh does not erase WAN telemetry')
        # Bonding refresh re-reads wan_devices -> advances.
        clock.t += 60
        fc.overrides['wan_devices'] = _tel_devices(5)
        svc.refresh('bonding')
        e3 = _iface(svc.snapshot(), 'ethernet-wan')
        check(e3['rx_bytes'] == 5000 and e3['collected_ts'] == clock.t,
              'manual bonding refresh updates WAN telemetry')
        # Counter decrease still flagged in current state.
        clock.t += 60
        fc.overrides['wan_devices'] = _tel_devices(1)
        svc.refresh('wan_paths')
        check(_iface(svc.snapshot(), 'ethernet-wan')['reset_suspected'],
              'reset_suspected visible in current state')


def test_current_telemetry_failed_read_retained():
    print('test_current_telemetry_failed_read_retained')
    with _RecEnv() as env:
        env.overrides['wan_devices'] = _tel_devices(2)
        env.svc.collect_startup()
        good = copy.deepcopy(env.svc.snapshot())
        env.fail.add('wan_devices')
        env.clock.t += 30
        env.svc.refresh('wan_paths')
        cur = env.svc.snapshot()
        check(cur['wan']['interfaces'] == good['wan']['interfaces'] and
              cur['cellular'] == good['cellular'],
              'failed wan_devices read keeps last-known-good telemetry')


def test_recording_updates_current_telemetry():
    print('test_recording_updates_current_telemetry')
    with _RecEnv() as env:
        env.overrides['wan_devices'] = _tel_devices(1)
        env.svc.collect_startup()
        check(_iface(env.svc.snapshot(), 'ethernet-wan')['rx_bytes'] == 1000,
              'startup counters visible')
        env.overrides['wan_devices'] = _tel_devices(9, rsrp='-80')
        env.svc.start_recording('t', max_duration_sec=60)
        env.run(7)
        snap = env.svc.snapshot()
        check(_iface(snap, 'ethernet-wan')['rx_bytes'] == 9000,
              'Recording update visible in current WAN telemetry')
        check(_modem(snap)['rsrp'] == -80.0,
              'Recording update visible in current cellular telemetry')
        check(snap['section_freshness']['wan']['sample_source'] ==
              'recording', 'freshness tagged recording')


# --- /api/cellular -------------------------------------------------------

def test_cellular_current_state_api():
    print('test_cellular_current_state_api')
    from http.server import HTTPServer
    from analyzer import web
    clock = _Clock()
    svc = _fresh(clock)
    with _fake_collectors({'wan_devices': _tel_devices(1)}) as fc:
        svc.collect_startup()
        m = _modem(svc.snapshot())
        check(m['carrier'] == 'Verizon' and m['service_mode'] == '5G NSA',
              'startup cellular state visible')
        for k in ('uid', 'carrier', 'service_mode', 'serving_band',
                  'serving_bandwidth_mhz', 'rsrp', 'rsrq', 'sinr',
                  'rsrp_5g', 'rsrq_5g', 'sinr_5g', 'carrier_count',
                  'total_active_mhz', 'zero_mhz_count', 'carriers',
                  'ca_signature', 'cell_key_hash', 'metric_ts',
                  'collected_ts', 'timestamp_source'):
            check(k in m, 'cellular field present: {}'.format(k))
        check(m['rsrp'] == -89.0 and m['sinr'] == 16.2 and
              m['sinr_5g'] == 32.0, 'RF values numeric in current state')
        check(m['carriers'] and all(
            set(c) == {'role', 'role_aliases', 'rat', 'band',
                       'bandwidth_mhz', 'channel', 'pci', 'active', 'rf'}
            for c in m['carriers']),
            'carrier entries carry the expected fields only')
        check(m['cell_key_hash'] and len(m['cell_key_hash']) == 16,
              'hashed cell key present')
        # Scheduled update visible.
        clock.t += 900
        fc.overrides['wan_devices'] = _tel_devices(1, rsrp='-95')
        svc.collect_scheduled()
        check(_modem(svc.snapshot())['rsrp'] == -95.0 and
              _modem(svc.snapshot())['collected_ts'] == clock.t,
              'scheduled cellular update visible')
        # Unrelated partial refresh keeps it.
        keep = copy.deepcopy(svc.snapshot()['cellular'])
        clock.t += 30
        svc.refresh('traffic_classes')
        check(svc.snapshot()['cellular'] == keep,
              'traffic_classes refresh does not erase cellular state')
        clock.t += 30
        svc.refresh('steering')
        check(svc.snapshot()['cellular'] == keep,
              'steering refresh does not erase cellular state')
        # Privacy: nothing sensitive anywhere in the cellular section.
        text = json.dumps(svc.snapshot()['cellular'])
        check(not any(v in text for v in _PRIVATE_VALUES),
              'no IMEI/IMSI/ICCID/MDN/GPS/cell-id values exposed')
        check(not any('"{}"'.format(k) in text for k in _PRIVATE_KEYS),
              'no sensitive keys exposed')
        # HTTP route + root page.
        port = _free_port()
        httpd = HTTPServer(('127.0.0.1', port), web._make_handler(svc))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            code, body = _http_json(port, '/api/cellular')
            check(code == 200 and body == svc.snapshot()['cellular'] and
                  body['modems'], 'GET /api/cellular serves current state')
            import urllib.request
            with urllib.request.urlopen(
                    'http://127.0.0.1:{}/dev'.format(port), timeout=5) as r:
                page = r.read().decode('utf-8')
            check('/api/cellular' in page, 'dev page lists /api/cellular')
            code, wan = _http_json(port, '/api/wan')
            check(code == 200 and wan['interfaces'],
                  'GET /api/wan includes interfaces')
            code, snap = _http_json(port, '/api/snapshot')
            check(snap['cellular'] == body and
                  snap['wan']['interfaces'] == wan['interfaces'],
                  'snapshot carries the same current WAN + cellular')
        finally:
            httpd.shutdown()
            httpd.server_close()
    # Before any data: route is 503 like the other sections.
    fresh = _fresh()
    port = _free_port()
    httpd = HTTPServer(('127.0.0.1', port), web._make_handler(fresh))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        check(_get(port, '/api/cellular')[0] == 503,
              '/api/cellular before first collection -> 503')
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cellular_recording_update_and_startup_only_modems():
    print('test_cellular_recording_update_and_startup_only_modems')
    with _RecEnv() as env:
        env.overrides['wan_devices'] = _tel_devices(1)
        env.svc.collect_startup()
        check(_modem(env.svc.snapshot())['rsrp'] == -89.0, 'startup rsrp')
        env.overrides['wan_devices'] = _tel_devices(1, rsrp='-70')
        env.svc.start_recording('c', max_duration_sec=60)
        env.run(6)
        check(_modem(env.svc.snapshot())['rsrp'] == -70.0,
              'Recording update visible in /api/cellular state')


def test_snapshot_normalized_telemetry():
    print('test_snapshot_normalized_telemetry')
    svc = _fresh()
    with _fake_collectors({'wan_devices': _tel_devices(1)}):
        svc.collect_startup()
    snap = svc.snapshot()
    check('interfaces' in snap['wan'] and 'cellular' in snap,
          'snapshot has current interface + cellular telemetry')
    blob = json.dumps(snap['cellular']) + json.dumps(snap['wan'][
        'interfaces'])
    check('"diagnostics"' not in blob and '"stats"' not in blob and
          '"IMEI"' not in blob and 'RSRP"' not in blob,
          'snapshot telemetry is normalized, not a raw NCOS dump')
    check(snap['wan']['paths'] and snap['bonding']['bonds'],
          'existing sections unchanged')
    # Observation (history) still carries the same normalized records.
    obs = svc.history(limit=1)[0]
    check(obs['interfaces'] == snap['wan']['interfaces'] or all(
        {k: v for k, v in i.items() if k != 'kind'} == o
        for i, o in zip(snap['wan']['interfaces'], obs['interfaces'])),
        'observation and current state share the same records')
    check(obs['cellular'] == snap['cellular']['modems'],
          'observation cellular == current cellular')


# --- web connection handling --------------------------------------------

class _FakeConn(object):
    def __init__(self):
        self.timeouts = []

    def settimeout(self, t):
        self.timeouts.append(t)


class _FakeListener(object):
    def __init__(self):
        self.conn = _FakeConn()

    def accept(self):
        return self.conn, ('127.0.0.1', 5555)


def _raw_request(port, data, timeout=5):
    """Send raw bytes, read until close. Returns (response_bytes, closed)."""
    s = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    try:
        s.sendall(data)
        buf = b''
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return buf, True
            buf += chunk
    except socket.timeout:
        return buf, False
    finally:
        s.close()


class _ShortTimeoutServer(object):
    """Real single-threaded server with a tiny client timeout (no sleeps
    in assertions: the timeout itself is what is being proven)."""

    def __init__(self, svc, timeout=0.3):
        from analyzer import web
        self.web = web
        self.saved = web.CLIENT_SOCKET_TIMEOUT_S
        web.CLIENT_SOCKET_TIMEOUT_S = timeout
        self.httpd = web._BoundedHTTPServer(
            ('127.0.0.1', 0), web._make_handler(svc))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.web.CLIENT_SOCKET_TIMEOUT_S = self.saved


def test_web_accepted_socket_timeout():
    print('test_web_accepted_socket_timeout')
    from analyzer import web
    check(web.CLIENT_SOCKET_TIMEOUT_S == 5, 'client timeout constant is 5 s')
    check(web.CLIENT_SOCKET_TIMEOUT_S < 15,
          'distinct from (shorter than) the 15 s collector timeout')
    srv = web._BoundedHTTPServer(('127.0.0.1', 0),
                                 web._make_handler(_fresh()),
                                 bind_and_activate=False)
    real = srv.socket
    fake = _FakeListener()
    srv.socket = fake
    try:
        conn, addr = srv.get_request()
        check(conn is fake.conn and fake.conn.timeouts ==
              [web.CLIENT_SOCKET_TIMEOUT_S],
              'accepted client socket receives the configured timeout')
    finally:
        srv.socket = real
        srv.server_close()
    h = web._make_handler(_fresh())
    check(h.timeout == web.CLIENT_SOCKET_TIMEOUT_S,
          'handler class also bounds its connection')
    check(not any(c.__name__ == 'ThreadingMixIn' for c in
                  web._BoundedHTTPServer.__mro__),
          'server stays single-threaded (no ThreadingMixIn)')
    import inspect
    check('ThreadingHTTPServer' not in inspect.getsource(web),
          'web module does not use a threading server')
    # DevWebServer uses the bounded server.
    ad = _FakeAppData()
    dws = web.DevWebServer(_fresh(), get_appdata=ad.get, put_appdata=ad.put)
    try:
        check(dws.start() and isinstance(dws._httpd,
                                         web._BoundedHTTPServer),
              'DevWebServer serves via the bounded server')
    finally:
        dws.stop()


def test_web_get_post_and_connection_close():
    print('test_web_get_post_and_connection_close')
    with _RecEnv() as env:
        srv = _ShortTimeoutServer(env.svc, timeout=2)
        try:
            resp, closed = _raw_request(
                srv.port, b'GET /api/recordings HTTP/1.1\r\n'
                          b'Host: x\r\nConnection: keep-alive\r\n\r\n')
            head, _, body = resp.partition(b'\r\n\r\n')
            check(head.startswith(b'HTTP/1.0 200') or
                  head.startswith(b'HTTP/1.1 200'), 'GET works')
            check(b'Connection: close' in head, 'GET sends Connection: close')
            check(closed, 'server closes the connection after GET')
            check(json.loads(body.decode()) == {'recordings': []},
                  'GET body intact')
            payload = json.dumps({'label': 'p', 'max_duration_sec': 60})
            req = ('POST /api/recordings/start HTTP/1.1\r\nHost: x\r\n'
                   'Content-Type: application/json\r\n'
                   'Content-Length: {}\r\n\r\n{}'.format(
                       len(payload), payload)).encode()
            resp, closed = _raw_request(srv.port, req)
            head, _, body = resp.partition(b'\r\n\r\n')
            check(b' 200 ' in head.split(b'\r\n')[0] and
                  json.loads(body.decode())['recording']['state'] ==
                  'recording', 'POST works')
            check(b'Connection: close' in head and closed,
                  'POST sends Connection: close and closes')
            resp, closed = _raw_request(
                srv.port, b'GET /dev HTTP/1.1\r\nHost: x\r\n\r\n')
            check(b'Connection: close' in resp.partition(b'\r\n\r\n')[0] and
                  closed and b'/api/cellular' in resp,
                  'HTML page also closes the connection')
        finally:
            srv.close()


def test_web_idle_client_cannot_block():
    print('test_web_idle_client_cannot_block')
    with _RecEnv() as env:
        srv = _ShortTimeoutServer(env.svc, timeout=0.3)
        idle = []
        try:
            # A speculative browser connection: connects, sends nothing.
            s1 = socket.create_connection(('127.0.0.1', srv.port))
            idle.append(s1)
            # Half-written request line, never finished.
            s2 = socket.create_connection(('127.0.0.1', srv.port))
            s2.sendall(b'GET /api/sta')
            idle.append(s2)
            t0 = time.time()
            code, body = _http_json(srv.port, '/api/status')
            waited = time.time() - t0
            check(code == 200 and body['app'] == 'sdwan_analyzer',
                  'later request is served despite idle clients')
            check(waited < 4.0,
                  'later request not blocked indefinitely')
            # Connection cleanup: server closed the idle sockets.
            for s in idle:
                s.settimeout(3)
            check(s1.recv(10) == b'', 'idle connection closed by server')
            try:
                tail = s2.recv(100)
            except socket.timeout:
                tail = None
            check(tail is not None and b'200' not in tail,
                  'half-written request dropped, not served')
        finally:
            for s in idle:
                s.close()
            srv.close()


def test_recording_independent_of_stuck_http_client():
    print('test_recording_independent_of_stuck_http_client')
    with _RecEnv() as env:
        srv = _ShortTimeoutServer(env.svc, timeout=0.3)
        stuck = socket.create_connection(('127.0.0.1', srv.port))
        try:
            job = env.svc.start_recording('r', max_duration_sec=60)[
                'recording']
            # The main-loop tick drives the job; a hung HTTP client cannot
            # affect it (web only reads cached state).
            env.run(15)
            rec = env.svc.active_recording()
            check(rec is not None and rec['job_id'] == job['job_id'] and
                  rec['sample_count'] > 0,
                  'Recording keeps sampling while a client is stuck')
            code, act = _get(srv.port, '/api/recordings/active')
            check(code == 200 and act['recording']['job_id'] ==
                  job['job_id'], 'HTTP serves the live job after the stall')
        finally:
            stuck.close()
            srv.close()


# -------------------------------------------------------------------------
# Storage Inspector (GET /api/storage)
# -------------------------------------------------------------------------
import shutil                              # noqa: E402
import tempfile                            # noqa: E402
from analyzer import inspector             # noqa: E402

_MARK = 'TOPSECRETMARKER-DO-NOT-LEAK'


def _make_app_tree():
    """Temp app root with allowed, sensitive, symlinked and data files."""
    base = tempfile.mkdtemp(prefix='sdwan_insp_')
    app = os.path.join(base, 'app')
    outside = os.path.join(base, 'outside')
    for d in (app, os.path.join(app, 'analyzer'),
              os.path.join(app, '__pycache__'),
              os.path.join(app, 'tmp', 'telemetry'),
              os.path.join(app, 'certs'), outside):
        os.makedirs(d)

    def w(path, text=_MARK):
        with open(path, 'w') as f:
            f.write(text)
    w(os.path.join(app, 'main.py'), _MARK * 3)
    w(os.path.join(app, 'readme.md'))
    w(os.path.join(app, 'package.ini'))
    w(os.path.join(app, 'analyzer', 'web.py'))
    w(os.path.join(app, 'analyzer', 'events.jsonl'))
    # Sensitive / unlisted:
    for name in ('sdk_settings.ini', 'server.pem', 'id_rsa', 'api_token.json',
                 'credentials.json', 'private.key', 'blob.bin', '.env'):
        w(os.path.join(app, name))
    w(os.path.join(app, 'certs', 'ca.crt'))
    w(os.path.join(app, '__pycache__', 'x.cpython-38.pyc'))
    # Durable telemetry runtime dir (reported under `persistent`):
    w(os.path.join(app, 'tmp', 'telemetry', 'obs-0001.jsonl'), 'x' * 10)
    # Outside the root, reachable only via symlinks:
    w(os.path.join(outside, 'outside.py'))
    w(os.path.join(outside, 'sdk_settings.ini'))
    os.symlink(outside, os.path.join(app, 'escape_dir'))
    os.symlink(os.path.join(outside, 'outside.py'),
               os.path.join(app, 'escape_file.py'))
    return base, app, outside


def _rel(files):
    return {f['relative_path'] for f in files}


def test_storage_inspector_files_allowlist():
    print('test_storage_inspector_files_allowlist')
    base, app, outside = _make_app_tree()
    try:
        svc = AnalyzerService(app_root=app)
        rep = svc.storage_report()
        af = rep['application_files']
        names = _rel(af['files'])
        check(names == {'main.py', 'readme.md', 'package.ini',
                        'analyzer/web.py', 'analyzer/events.jsonl'},
              'only allowed app files listed: {}'.format(sorted(names)))
        f = [x for x in af['files'] if x['relative_path'] == 'main.py'][0]
        check(set(f) == {'relative_path', 'size_bytes', 'modified_ts',
                         'type'} and f['type'] == 'code' and
              f['size_bytes'] == len(_MARK) * 3 and
              isinstance(f['modified_ts'], int), 'file metadata fields')
        check({x['relative_path']: x['type'] for x in af['files']}[
            'analyzer/events.jsonl'] == 'data', 'type classification')
        check(af['file_count'] == len(af['files']) and
              af['total_bytes'] == sum(x['size_bytes'] for x in af['files']),
              'file_count and total_bytes match the listed files')
        text = json.dumps(rep)
        check(_MARK not in text, 'file contents are never returned')
        for bad in ('sdk_settings.ini', 'server.pem', 'id_rsa',
                    'api_token', 'credentials', 'private.key', 'blob.bin',
                    '.env', 'ca.crt', '.pyc', 'outside.py', 'escape_',
                    'certs'):
            check(bad not in text,
                  'sensitive/unrelated path excluded: {}'.format(bad))
        check(af['skipped_symlinks'] == 2,
              'symlinks (dir + file) ignored, not followed')
        check(af['excluded_count'] >= 7, 'excluded files only counted')
        check(not any(p.startswith('tmp/') for p in names),
              'tmp/ runtime dir not double-counted under application_files')
        check(os.path.abspath(app) not in text and base not in text,
              'absolute paths are not exposed')
        # A symlinked ROOT is refused outright.
        link_root = os.path.join(base, 'rootlink')
        os.symlink(outside, link_root)
        res = inspector.scan_files(link_root)
        check(not res['root_exists'] and res['files'] == [],
              'symlinked root rejected')
        check(inspector.scan_files(os.path.join(base, 'nope'))[
            'root_exists'] is False, 'missing root reported cleanly')
        # Only the app root is ever scanned: no path parameter on the API.
        import inspect as _inspect
        check(_inspect.signature(AnalyzerService.storage_report)
              .parameters.keys() == {'self'},
              'storage_report accepts no caller-supplied path')
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_storage_inspector_persistent_section():
    print('test_storage_inspector_persistent_section')
    base, app, outside = _make_app_tree()
    try:
        # Memory-only, no telemetry dir yet.
        shutil.rmtree(os.path.join(app, 'tmp'))
        p = AnalyzerService(app_root=app).storage_report()['persistent']
        check(p['enabled'] is False and p['root_exists'] is False and
              p['total_bytes'] == 0 and p['file_count'] == 0 and
              p['files'] == [], 'no telemetry dir: explicit, empty, disabled')
        check(p['root'] == 'tmp/telemetry', 'telemetry root named')
        # Files in the runtime dir show up here without changing the shape.
        os.makedirs(os.path.join(app, 'tmp', 'telemetry'))
        with open(os.path.join(app, 'tmp', 'telemetry', 'obs-1.jsonl'),
                  'w') as f:
            f.write('x' * 25)
        p2 = AnalyzerService(app_root=app).storage_report()['persistent']
        check(p2['root_exists'] is True and p2['file_count'] == 1 and
              p2['total_bytes'] == 25 and
              p2['files'][0]['relative_path'] == 'obs-1.jsonl' and
              set(p2) == set(p), 'telemetry dir files reported; same shape')
        check(p2['enabled'] is False,
              'still reports enabled=false (memory-only telemetry)')
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_storage_inspector_real_app_dir():
    print('test_storage_inspector_real_app_dir')
    rep = AnalyzerService().storage_report()
    names = _rel(rep['application_files']['files'])
    check('analyzer/web.py' in names and 'package.ini' in names,
          'default root is the app directory')
    check(not any(n.endswith('.pyc') or 'sdk_settings' in n or
                  n.startswith('tests/__pycache__') for n in names),
          'no pyc / settings in the real listing')
    check(rep['persistent']['enabled'] is False, 'persistent disabled')


def test_storage_inspector_memory_counts():
    print('test_storage_inspector_memory_counts')
    with _RecEnv() as env:
        svc = env.svc
        m0 = svc.storage_report()['memory']
        check(m0['history_observations'] == 0 and m0['recording_jobs'] == 0
              and m0['recording_samples'] == 0 and
              m0['active_recording'] is None and
              m0['swans_dedupe_entries'] == 0, 'empty stores report zeros')
        svc.collect_startup()
        job = svc.start_recording('s', max_duration_sec=60)['recording']
        env.run(12)
        rep = svc.storage_report()
        m = rep['memory']
        rec = m['details']['recording']
        n = svc._rec_store.sample_count(job['job_id'])
        check(n > 0 and m['recording_samples'] == n and
              rec['samples_by_job'] == {job['job_id']: n} and
              rec['total_sample_count'] == n, 'Recording sample counts')
        check(m['recording_jobs'] == 1 and rec['job_count'] == 1 and
              m['active_recording'] == job['job_id'] and
              rec['active_job_id'] == job['job_id'],
              'job count + active job id')
        check(rec['configured_max_jobs'] == 20 and
              rec['configured_max_samples_per_job'] == 3000,
              'configured Recording limits reported')
        check(m['history_observations'] == len(svc.history(limit=None)) and
              m['history_observations'] == 1, 'history observation count')
        check(m['config_versions'] == sum(
            len(svc._config_store.versions(k)) for k in
            m['details']['config_versions']['by_name']) and
            m['config_versions'] > 0, 'config version count')
        check(m['swans_dedupe_entries'] == len(svc._swans_seen),
              'SWANS dedupe entries')
        check(m['details']['history']['durable'] is False and
              m['details']['history']['max_observations'] == 720,
              'history backend/limits')
        text = json.dumps(rep)
        check('rx_bytes' not in text and 'latency_ms' not in text,
              'counts/metadata only, no telemetry payloads')
        env.svc.stop_recording(job['job_id'])
        env.run(2)
        env.clock.t += 1
        m2 = svc.storage_report()['memory']
        check(m2['active_recording'] is None and m2['recording_jobs'] == 1
              and m2['recording_samples'] >= n,
              'finished job retained, no longer active')


def test_storage_inspector_api_on_demand_no_side_effects():
    print('test_storage_inspector_api_on_demand_no_side_effects')
    from http.server import HTTPServer
    from analyzer import web
    base, app, outside = _make_app_tree()
    clock = _Clock()
    svc = AnalyzerService(clock=clock, app_root=app)
    fc = _fake_collectors()
    fc.__enter__()
    port = None
    httpd = None
    try:
        svc.collect_startup()
        job = svc.start_recording('x', max_duration_sec=60)['recording']
        for _ in range(8):
            svc.recording_tick()
            clock.t += 1
        port = _free_port()
        httpd = HTTPServer(('127.0.0.1', port), web._make_handler(svc))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        before = {
            'history': len(svc.history(limit=None)),
            'next_due': svc.next_scheduled_ts(),
            'sched': svc.schedule_minutes(),
            'rec': copy.deepcopy(svc.active_recording()),
            'rec_samples': svc._rec_store.sample_count(job['job_id']),
            'calls': len(fc.calls),
            'fresh': copy.deepcopy(svc.snapshot()['section_freshness']),
            'cfg': svc._config_store.info(),
            'jobs': len(svc.list_recordings()),
        }
        code, rep = _http_json(port, '/api/storage')
        check(code == 200 and rep['generated_ts'] == int(clock.t) and
              {'memory', 'persistent', 'application_files'} <= set(rep),
              '/api/storage exists and returns the report')
        clock.t += 5
        _, rep2 = _http_json(port, '/api/storage')
        check(rep2['generated_ts'] == int(clock.t) and
              rep2['generated_ts'] != rep['generated_ts'],
              'response is generated on demand (fresh timestamp)')
        with open(os.path.join(app, 'new_file.py'), 'w') as f:
            f.write('abc')
        _, rep3 = _http_json(port, '/api/storage')
        check('new_file.py' in _rel(rep3['application_files']['files']) and
              'new_file.py' not in _rel(rep['application_files']['files']),
              'file changes are visible on the next call (no caching)')
        for _ in range(3):
            _http_json(port, '/api/storage')
        after = {
            'history': len(svc.history(limit=None)),
            'next_due': svc.next_scheduled_ts(),
            'sched': svc.schedule_minutes(),
            'rec': copy.deepcopy(svc.active_recording()),
            'rec_samples': svc._rec_store.sample_count(job['job_id']),
            'calls': len(fc.calls),
            'fresh': copy.deepcopy(svc.snapshot()['section_freshness']),
            'cfg': svc._config_store.info(),
            'jobs': len(svc.list_recordings()),
        }
        # The clock advanced 5 s, which only changes elapsed/remaining.
        for k in ('elapsed_sec', 'remaining_sec'):
            before['rec'].pop(k, None)
            after['rec'].pop(k, None)
        check(before == after,
              'inspector creates no history/observation, schedule, '
              'collection, freshness or Recording change')
        import urllib.request
        with urllib.request.urlopen(
                'http://127.0.0.1:{}/dev'.format(port), timeout=5) as r:
            check('/api/storage' in r.read().decode('utf-8'),
                  'dev page lists /api/storage')
        import inspect as _inspect
        src = _inspect.getsource(inspector)
        check(not any(('import ' + m) in src for m in (
            'threading', 'sched', 'signal', 'asyncio')) and
            'Timer(' not in src and 'Thread(' not in src,
            'inspector has no timer/scheduler')
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        fc.__exit__()
        shutil.rmtree(base, ignore_errors=True)


def main():
    tests = [
        test_logical_policy_branches,
        test_logical_fingerprint_branches,
        test_preferred_wan_correlation,
        test_destination_internal_name,
        test_sdwan_wbond_kind,
        test_history_logical_once,
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
        test_web_port_module_is_the_current_standard,
        test_app_package_ini_is_found_from_subpackage,
        test_stop_app_uses_app_root_package_ini,
        test_web_server_first_run_and_override,
        test_web_server_malformed_value_fails_fast,
        test_interface_stats_normalization,
        test_counter_decrease_reset_suspected,
        test_cellular_ca_parsing,
        test_ca_signature_ignores_roles,
        test_cell_hash_deterministic,
        test_cellular_sensitive_fields_excluded,
        test_swans_dedupe,
        test_qoe_wbond_telemetry,
        test_startup_collection_and_sources,
        test_schedule_selection,
        test_no_legacy_polling,
        test_scheduled_config_reconcile,
        test_manual_refresh_targeted,
        test_config_version_change_only,
        test_steering_events_once,
        test_cache_updated_by_collection_only,
        test_collector_timeout_bound,
        test_scheduled_establishes_current_state,
        test_partial_manual_refresh_keeps_current_state,
        test_bonding_refresh_leaves_unrelated_intact,
        test_failed_section_does_not_advance_freshness,
        test_api_after_partial_refresh,
        test_loadbalancer_expensive_runtime,
        test_health_error_types,
        test_timeout_busy_types_and_single_worker,
        test_recording_start_validation,
        test_recording_backend_owns_clock,
        test_recording_stop_expiry_events,
        test_recording_cadence_tagging_dedupe,
        test_recording_config_start_end_and_schedule,
        test_recording_partial_failure_and_infra_failure,
        test_recording_timeout_busy_no_overlap,
        test_recording_http_api,
        test_rf_numeric_normalization,
        test_current_wan_interface_telemetry,
        test_current_telemetry_failed_read_retained,
        test_recording_updates_current_telemetry,
        test_cellular_current_state_api,
        test_cellular_recording_update_and_startup_only_modems,
        test_snapshot_normalized_telemetry,
        test_web_accepted_socket_timeout,
        test_web_get_post_and_connection_close,
        test_web_idle_client_cannot_block,
        test_recording_independent_of_stuck_http_client,
        test_storage_inspector_files_allowlist,
        test_storage_inspector_persistent_section,
        test_storage_inspector_real_app_dir,
        test_storage_inspector_memory_counts,
        test_storage_inspector_api_on_demand_no_side_effects,
    ]
    # Durable telemetry storage tests (separate module, shared runner/count).
    import durable_tests
    durable_tests.bind(check, _Clock, _fake_collectors, _free_port,
                       _http_json)
    tests = tests + durable_tests.TESTS
    # Durable query API tests (v0.4.4).
    import query_tests
    query_tests.bind(check, _Clock, _fake_collectors, _free_port,
                     _http_json)
    tests = tests + query_tests.TESTS
    # v0.5.0: acquisition timing, failed-read behavior, counter math.
    import acquisition_tests
    acquisition_tests.bind(check, _Clock, _tel_devices)
    tests = tests + acquisition_tests.TESTS
    import counters_tests
    counters_tests.bind(check)
    tests = tests + counters_tests.TESTS
    import analytics_tests
    analytics_tests.bind(check, _Clock)
    analytics_tests.bind_port(_free_port)
    tests = tests + analytics_tests.TESTS
    import topology_tests
    topology_tests.bind(check, _free_port)
    tests = tests + topology_tests.TESTS
    import wbond_tests
    wbond_tests.bind(check, _free_port)
    tests = tests + wbond_tests.TESTS
    import ui_tests
    ui_tests.bind(check, _Clock, _free_port)
    tests = tests + ui_tests.TESTS
    import failed_read_tests
    failed_read_tests.bind(check)
    tests = tests + failed_read_tests.TESTS
    import primary_config_tests
    primary_config_tests.bind(check)
    tests = tests + primary_config_tests.TESTS
    import stage_a_tests
    stage_a_tests.bind(check)
    tests = tests + stage_a_tests.TESTS
    import foundation_tests
    foundation_tests.bind(check, _free_port)
    tests = tests + foundation_tests.TESTS
    import cellular_v084_tests
    cellular_v084_tests.bind(check)
    tests = tests + cellular_v084_tests.TESTS
    import storage_v084_tests
    storage_v084_tests.bind(check)
    tests = tests + storage_v084_tests.TESTS
    import analytics_v084_tests
    analytics_v084_tests.bind(check)
    tests = tests + analytics_v084_tests.TESTS
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
