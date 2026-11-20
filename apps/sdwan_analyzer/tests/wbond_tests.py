"""v0.8.1/0.8.2: GET /api/analytics/wbond (WBOND runtime analysis).

Two-member (captured fixture) and three-member bonds; member relationships,
runtime fields, unavailable values, stale data. Run through run_tests.py.
"""
import copy
import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import fixtures
from analyzer import normalize, telemetry, web
from analyzer.service import AnalyzerService

check = None
_free_port = None


def bind(check_fn, free_port_fn):
    global check, _free_port
    check = check_fn
    _free_port = free_port_fn


def _svc(devices=None, bonding=None):
    svc = AnalyzerService()
    raw = fixtures.base_raw()
    raw['wan_devices'] = copy.deepcopy(
        fixtures.WAN_DEVICES if devices is None else devices)
    raw['primary_device'] = 'ethernet-wan'
    svc._runtime_raw.data = raw
    heavy = fixtures.heavy_raw()
    heavy['wan_bonding'] = copy.deepcopy(
        fixtures.WAN_BONDING if bonding is None else bonding)
    svc._config_raw.data = heavy
    svc._runtime_raw._updated['wan_devices'] = 1
    svc._config_raw._updated['wan_bonding'] = 1
    svc._freshness['bonding'] = {'collected_ts': 1, 'sample_source': 't'}
    svc._freshness['wan'] = {'collected_ts': 1, 'sample_source': 't'}
    svc.recompose()
    return svc


def _three():
    """Fixture plus a third overlay + member riding on mdm-f260d."""
    dev = copy.deepcopy(fixtures.WAN_DEVICES)
    ov = copy.deepcopy(dev['sdwan-hub0_1ea0ac'])
    ov['info']['iface'] = 'gres-hub0_f260d'
    ov['info']['uid'] = 'hub0_f260d'
    ov['status']['active_dep_wandev'] = 'mdm-f260d'
    ov['status']['dep_wandevs'] = ['mdm-f260d']
    dev['sdwan-hub0_f260d'] = ov
    bond = copy.deepcopy(fixtures.WAN_BONDING)
    b = bond['interfaces']['gres_wb0-hub0']
    b['Total interfaces'] = 3
    b['members_list']['gres-hub0_f260d'] = {
        'profile name': 'Cellular', 'intf name': 'mdm-f260d',
        'config_weight': 10, 'ds_weight': 12, 'us_weight': 11,
        'DL current flows': 0, 'UL current flows': 0, 'has_loss': 1,
        'link state': 'UP', 'link attach state': 'DETACHED'}
    return dev, bond


def _by(items, key, val):
    return [i for i in items if i.get(key) == val][0]


def test_wbond_two_member():
    print('test_wbond_two_member')
    w = _svc().query_wbond()
    check(w['schema_version'] == 1 and w['counters_additive'] is False and
          w['data_state'] == 'fresh', 'schema, counters_additive false, fresh')
    b = w['bonds'][0]
    o = b['observed']
    check(b['iface'] == 'gres_wb0-hub0' and o['link_state'] == 'UP' and
          o['bonding_mode'] == 'GRE' and
          o['negotiated_capabilities'] == ['FLOW_BALANCE', 'BW_AGGREGATION'],
          'bond state, mode, negotiated capabilities')
    check(o['reported_member_count'] == 2 and o['listed_member_count'] == 2
          and o['resolved_member_count'] == 2 and
          o['member_count_matches'] is True, 'member counts')
    m = _by(b['members'], 'tunnel_iface', 'gres-hub0_164d3')
    check(m['overlay']['uid'] == 'sdwan-hub0_1ea0ac' and
          m['physical']['uid'] == 'mdm-1ea0ac' and
          m['physical_display_name'] and
          m['underlay_consistency'] == 'agree', 'member relationships')
    check(m['config'] == {'configured_weight': 20}, 'configured weight')
    ob = m['observed']
    check(ob['link_state'] == 'UP' and ob['link_attach_state'] == 'ATTACHED'
          and ob['current_download_weight'] == 24 and
          ob['current_upload_weight'] == 20 and
          ob['download_flows'] == 5 and ob['upload_flows'] == 2 and
          ob['loss_indicator'] == 0, 'observed runtime fields')
    blob = json.dumps({k: v for k, v in w.items() if k != 'notes'})
    for banned in ('utilization', 'efficiency', 'percent', 'share',
                   'carrying', 'rx_bytes'):
        check(banned not in blob, 'no {} in response'.format(banned))


def test_wbond_three_member():
    print('test_wbond_three_member')
    dev, bond = _three()
    b = _svc(dev, bond).query_wbond()['bonds'][0]
    check(b['observed']['reported_member_count'] == 3 and
          b['observed']['listed_member_count'] == 3 and
          b['observed']['resolved_member_count'] == 3, 'three members')
    m = _by(b['members'], 'tunnel_iface', 'gres-hub0_f260d')
    check(m['physical']['uid'] == 'mdm-f260d' and
          m['overlay']['uid'] == 'sdwan-hub0_f260d' and
          m['observed']['link_attach_state'] == 'DETACHED' and
          m['observed']['loss_indicator'] == 1 and
          m['observed']['download_flows'] == 0,
          'third member resolved; loss indicator passed through, not a %')


def test_wbond_unavailable_values():
    print('test_wbond_unavailable_values')
    bond = copy.deepcopy(fixtures.WAN_BONDING)
    ml = bond['interfaces']['gres_wb0-hub0']['members_list']
    for k in ('ds_weight', 'us_weight', 'DL current flows',
              'UL current flows', 'has_loss', 'config_weight',
              'link attach state'):
        ml['gres-hub0_wan'].pop(k)
    ml['gres-hub0_164d3']['intf name'] = 'nope0'
    b = _svc(bonding=bond).query_wbond()['bonds'][0]
    m = _by(b['members'], 'tunnel_iface', 'gres-hub0_wan')
    check(m['observed']['current_download_weight'] is None and
          m['observed']['current_upload_weight'] is None and
          m['observed']['download_flows'] is None and
          m['observed']['upload_flows'] is None and
          m['observed']['loss_indicator'] is None and
          m['observed']['link_attach_state'] is None and
          m['config']['configured_weight'] is None,
          'missing measurements are null, not zero')
    check(m['observed']['link_state'] == 'UP', 'reported values kept')
    u = _by(b['members'], 'tunnel_iface', 'gres-hub0_164d3')
    check(u['physical']['state'] == 'unresolved' and
          u['physical_display_name'] is None and
          b['observed']['resolved_member_count'] == 1,
          'unresolved physical: no name, not counted as resolved')


def test_wbond_stale_and_route():
    print('test_wbond_stale_and_route')
    svc = _svc()
    svc._config_raw._errors['wan_bonding'] = 'timeout'
    svc._config_raw._error_ts['wan_bonding'] = 5
    w = svc.query_wbond()
    src = w['as_of']['sources']
    check(w['data_state'] == 'stale' and src['bonding']['stale'] is True and
          src['bonding']['read_error'] == 'timeout' and
          src['wan']['stale'] is False and len(w['bonds']) == 1,
          'failed bonding read flagged stale; cached data still shown')
    empty = AnalyzerService()
    check(empty.query_wbond() is None, 'None before the first snapshot')
    port, port2 = _free_port(), _free_port()
    ok = HTTPServer(('127.0.0.1', port), web._make_handler(svc))
    nope = HTTPServer(('127.0.0.1', port2), web._make_handler(empty))
    for s in (ok, nope):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        def get(p):
            try:
                with urllib.request.urlopen(
                        'http://127.0.0.1:%d/api/analytics/wbond' % p,
                        timeout=5) as r:
                    return r.status, json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode())
        code, body = get(port)
        check(code == 200 and body['schema_version'] == 1 and
              len(body['bonds']) == 1, 'GET /api/analytics/wbond')
        code, body = get(port2)
        check(code == 503 and 'error' in body, '503 before first snapshot')
    finally:
        for s in (ok, nope):
            s.shutdown()
            s.server_close()


def _prov(bond):
    b = normalize.wan_bonding_detail(bond)['bonds'][0]
    return b['total_download_flows_source'], b['total_upload_flows_source']


def test_flow_total_provenance():
    print('test_flow_total_provenance')
    # reported: fixture carries DL/UL total flows
    check(_prov(fixtures.WAN_BONDING) == ('reported', 'reported'),
          'reported totals -> reported')
    b = _svc().query_wbond()['bonds'][0]['observed']
    check(b['total_download_flows_source'] == 'reported' and
          b['total_upload_flows_source'] == 'reported' and
          b['total_download_flows'] == 29, 'API exposes reported provenance')
    # summed: download total absent only; upload stays reported
    bond = copy.deepcopy(fixtures.WAN_BONDING)
    bond['interfaces']['gres_wb0-hub0'].pop('DL total flows')
    check(_prov(bond) == ('summed', 'reported'),
          'provenance is independent per direction')
    o = _svc(bonding=bond).query_wbond()['bonds'][0]['observed']
    check(o['total_download_flows_source'] == 'summed' and
          o['total_download_flows'] == 29,
          'API exposes summed provenance')
    # unknown: no reported total and no members to sum
    bond = copy.deepcopy(fixtures.WAN_BONDING)
    bi = bond['interfaces']['gres_wb0-hub0']
    for k in ('DL total flows', 'UL total flows', 'members_list'):
        bi.pop(k, None)
    check(_prov(bond) == ('unknown', 'unknown'),
          'nothing reported or summable -> unknown')
    # a reported 0 is still reported
    bond = copy.deepcopy(fixtures.WAN_BONDING)
    bond['interfaces']['gres_wb0-hub0']['DL total flows'] = 0
    check(_prov(bond)[0] == 'reported', 'reported zero stays reported')


def test_history_record_integrity():
    print('test_history_record_integrity')
    bonding = normalize.wan_bonding_detail(fixtures.WAN_BONDING)
    rec = telemetry.wbond_runtime(bonding, 100)[0]
    check(rec['total_download_flows_source'] == 'reported' and
          rec['total_upload_flows_source'] == 'reported',
          'record carries provenance for both directions')
    ifaces = sorted(m['tunnel_iface'] for m in rec['members'])
    check(ifaces == ['gres-hub0_164d3', 'gres-hub0_wan'],
          'member records carry the collected tunnel_iface')
    check(all(m['member_uid'] for m in rec['members']),
          'member_uid preserved')
    # tunnel identity is never inferred: a member without it stays null
    bonding['bonds'][0]['members'][0].pop('tunnel_iface')
    rec = telemetry.wbond_runtime(bonding, 100)[0]
    check(rec['members'][0]['tunnel_iface'] is None and
          rec['members'][1]['tunnel_iface'] is not None,
          'missing tunnel identity -> null, no inference')
    # legacy (pre-0.8.2) normalized input: no source keys -> unknown
    legacy = {'bonds': [{'iface': 'b', 'total_download_flows': 3,
                         'members': [{'member_uid': 'x'}]}]}
    rec = telemetry.wbond_runtime(legacy, 100)[0]
    check(rec['total_download_flows_source'] == 'unknown' and
          rec['total_upload_flows_source'] == 'unknown' and
          rec['members'][0]['tunnel_iface'] is None,
          'input lacking provenance/tunnel -> unknown/null')
    # old stored observations lack the keys and are never rewritten here;
    # the API reads the live snapshot, not history.
    old = {'iface': 'b', 'members': [{'member_uid': 'x'}]}
    check('total_download_flows_source' not in old and
          old['members'][0].get('tunnel_iface') is None,
          'legacy stored record reads as absent/null')


TESTS = [
    test_wbond_two_member,
    test_wbond_three_member,
    test_wbond_unavailable_values,
    test_wbond_stale_and_route,
    test_flow_total_provenance,
    test_history_record_integrity,
]
