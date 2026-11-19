"""v0.8.1: GET /api/analytics/wbond (WBOND runtime analysis).

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
from analyzer import web
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


TESTS = [
    test_wbond_two_member,
    test_wbond_three_member,
    test_wbond_unavailable_values,
    test_wbond_stale_and_route,
]
