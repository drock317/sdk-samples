"""v0.8.0: GET /api/analytics/topology (current WAN/overlay/WBOND relations).

Fixture case: the captured R1900-shape devices and bonding runtime resolve
fully. Synthetic cases: each relationship that cannot be verified comes back
unresolved with a reason, never guessed. Run through run_tests.py.
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

check = None            # bound by run_tests.main()
_free_port = None


def bind(check_fn, free_port_fn):
    global check, _free_port
    check = check_fn
    _free_port = free_port_fn


def _svc(devices=None, primary='ethernet-wan', bonding='fixture'):
    svc = AnalyzerService()
    raw = fixtures.base_raw()
    raw['wan_devices'] = copy.deepcopy(
        fixtures.WAN_DEVICES if devices is None else devices)
    raw['primary_device'] = primary
    svc._runtime_raw.data = raw
    heavy = fixtures.heavy_raw()
    heavy['wan_bonding'] = (copy.deepcopy(fixtures.WAN_BONDING)
                            if bonding == 'fixture' else bonding)
    svc._config_raw.data = heavy
    svc._freshness['bonding'] = {'collected_ts': 1, 'sample_source': 't'}
    svc._freshness['wan'] = {'collected_ts': 1, 'sample_source': 't'}
    svc.recompose()
    return svc


def _by(items, key, val):
    return [i for i in items if i.get(key) == val][0]


def _reasons(topo):
    return sorted((u['kind'], u['reason']) for u in topo['unresolved'])


def test_topology_fixture_resolves():
    print('test_topology_fixture_resolves')
    t = _svc().query_topology()
    check(t['schema_version'] == 1 and t['counters_additive'] is False,
          'schema version, counters flagged not additive')
    check(t['primary']['state'] == 'known' and
          t['primary']['uid'] == 'ethernet-wan' and
          t['primary']['object_class'] == 'physical',
          'primary known from primary_device')
    check(sorted(p['uid'] for p in t['physical']) ==
          ['ethernet-wan', 'mdm-1ea0ac', 'mdm-f260d'],
          'physical list excludes overlays and wbond')
    check(sorted(o['uid'] for o in t['overlays']) ==
          ['sdwan-hub0_1ea0ac', 'sdwan-hub0_wan'], 'overlay list')
    wan = _by(t['overlays'], 'uid', 'sdwan-hub0_wan')
    check(wan['underlay']['state'] == 'resolved' and
          wan['underlay']['uid'] == 'ethernet-wan' and
          wan['underlay']['basis'] == 'active_dep_wandev',
          'overlay underlay from active_dep_wandev')
    eth = _by(t['physical'], 'uid', 'ethernet-wan')
    check(eth['overlays'] == ['sdwan-hub0_wan'] and
          eth['bond_memberships'] == ['gres_wb0-hub0'] and
          eth['observed']['is_primary'] is True, 'physical back-references')
    mdm_down = _by(t['physical'], 'uid', 'mdm-f260d')
    check(mdm_down['overlays'] == [] and mdm_down['bond_memberships'] == []
          and mdm_down['observed']['is_primary'] is False,
          'unused physical WAN has no relations')
    bond = t['bonds'][0]
    check(bond['wbond_device']['uid'] == 'wbond-wbond0_hub0' and
          bond['config']['membership'] == {'state': 'not_collected'},
          'bond device resolved; configured membership not collected')
    check(bond['observed']['reported_member_count'] == 2 and
          bond['observed']['listed_member_count'] == 2 and
          bond['observed']['member_count_matches'] is True,
          'reported vs listed member count')
    m = _by(bond['members'], 'tunnel_iface', 'gres-hub0_164d3')
    check(m['overlay']['uid'] == 'sdwan-hub0_1ea0ac' and
          m['physical']['uid'] == 'mdm-1ea0ac' and
          m['underlay_consistency'] == 'agree' and
          m['observed']['link_state'] == 'UP',
          'member -> overlay -> physical, both routes agree')
    check(t['unresolved'] == [], 'nothing unresolved on the fixture')
    check(wan['bond_memberships'] == [{'bond_iface': 'gres_wb0-hub0',
                                       'tunnel_iface': 'gres-hub0_wan'}],
          'overlay lists its bond membership')
    blob = json.dumps({k: v for k, v in t.items() if k != 'notes'})
    for banned in ('rx_bytes', 'tx_bytes', 'bytes_per_s', 'utilization',
                   'efficiency', 'carrying'):
        check(banned not in blob, 'no {} in response'.format(banned))


def test_topology_unresolved_not_guessed():
    print('test_topology_unresolved_not_guessed')
    dev = copy.deepcopy(fixtures.WAN_DEVICES)
    st = dev['sdwan-hub0_wan']['status']
    st['active_dep_wandev'] = ''
    st['dep_wandevs'] = ['ethernet-wan', 'mdm-1ea0ac']     # ambiguous
    st1 = dev['sdwan-hub0_1ea0ac']['status']
    st1['active_dep_wandev'] = 'wbond-wbond0_hub0'          # not physical
    t = _svc(dev).query_topology()
    a = _by(t['overlays'], 'uid', 'sdwan-hub0_wan')['underlay']
    check(a['state'] == 'unresolved' and a['uid'] is None and
          a['reason'] == 'multiple_dependents_none_active' and
          a['declared_dependents'] == ['ethernet-wan', 'mdm-1ea0ac'],
          'multiple dependents, none active: not guessed')
    b = _by(t['overlays'], 'uid', 'sdwan-hub0_1ea0ac')['underlay']
    check(b['state'] == 'unresolved' and b['reason'] == 'not_a_physical_wan',
          'underlay that is not physical is rejected')
    check(_by(t['physical'], 'uid', 'ethernet-wan')['overlays'] == [],
          'no back-reference from an unresolved underlay')

    dev = copy.deepcopy(fixtures.WAN_DEVICES)
    for k in ('active_dep_wandev', 'dep_wandevs'):
        dev['sdwan-hub0_wan']['status'].pop(k)
    dev['sdwan-hub0_1ea0ac']['status']['active_dep_wandev'] = 'mdm-gone'
    t = _svc(dev).query_topology()
    check(_by(t['overlays'], 'uid', 'sdwan-hub0_wan')['underlay']['reason']
          == 'no_dependent_reported', 'no dependent reported')
    check(_by(t['overlays'], 'uid', 'sdwan-hub0_1ea0ac')['underlay']['reason']
          == 'not_in_wan_devices', 'dependent absent from wan devices')
    m = _by(t['bonds'][0]['members'], 'tunnel_iface', 'gres-hub0_wan')
    check(m['physical']['state'] == 'resolved' and
          m['underlay_consistency'] == 'not_comparable',
          'member physical stays resolved; consistency not comparable')


def test_topology_member_conflict_and_missing():
    print('test_topology_member_conflict_and_missing')
    bond = copy.deepcopy(fixtures.WAN_BONDING)
    mem = bond['interfaces']['gres_wb0-hub0']['members_list']
    mem['gres-hub0_wan']['intf name'] = 'mdm-1ea0ac'    # disagrees w/ overlay
    mem['gres-hub0_164d3']['intf name'] = 'nope0'       # unknown
    mem['gres-extra'] = {'intf name': 'ethernet-wan', 'link state': 'UP'}
    t = _svc(bonding=bond).query_topology()
    ms = t['bonds'][0]['members']
    c = _by(ms, 'tunnel_iface', 'gres-hub0_wan')
    check(c['underlay_consistency'] == 'conflict', 'conflict surfaced')
    u = _by(ms, 'tunnel_iface', 'gres-hub0_164d3')
    check(u['physical']['reason'] == 'intf_name_not_in_wan_devices' and
          u['overlay']['state'] == 'resolved', 'unknown intf name unresolved')
    x = _by(ms, 'tunnel_iface', 'gres-extra')
    check(x['overlay']['reason'] == 'tunnel_iface_not_an_overlay',
          'tunnel that is not a known overlay is unresolved')
    check(t['bonds'][0]['observed']['member_count_matches'] is False,
          'reported 2 vs 3 listed flagged')
    kinds = {k for k, _ in _reasons(t)}
    check({'member_underlay_conflict', 'member_physical',
           'member_overlay'} <= kinds, 'all listed in unresolved[]')


def test_topology_primary_and_bond_runtime_unknowns():
    print('test_topology_primary_and_bond_runtime_unknowns')
    t = _svc(primary=None).query_topology()
    check(t['primary']['state'] == 'unknown' and t['primary']['uid'] is None,
          'primary unknown when not reported')
    check(all(p['observed']['is_primary'] is None for p in t['physical']) and
          all(o['observed']['is_primary'] is None for o in t['overlays']),
          'is_primary is None (not False) when primary unknown')
    t = _svc(primary='mdm-ghost').query_topology()
    check(t['primary']['state'] == 'unresolved' and
          t['primary']['reason'] == 'primary_not_in_wan_devices',
          'primary absent from devices is unresolved')
    # WBOND device present, runtime read but empty.
    t = _svc(bonding={'interfaces': {}}).query_topology()
    check(t['bonds'] == [] and t['bonding_runtime']['state'] == 'read' and
          ('wbond_runtime', 'wbond_device_without_runtime') in _reasons(t),
          'wbond device without runtime reported')
    # Bonding never read: unknown, and no false "missing runtime" claim.
    svc = _svc(bonding={'interfaces': {}})
    svc._freshness.pop('bonding')
    svc.recompose()
    t = svc.query_topology()
    check(t['bonding_runtime']['state'] == 'unknown' and
          all(k != 'wbond_runtime' for k, _ in _reasons(t)),
          'bonding never read: unknown, not asserted absent')
    # Bond runtime with no matching WBOND device.
    dev = {k: v for k, v in fixtures.WAN_DEVICES.items()
           if not k.startswith('wbond')}
    t = _svc(dev).query_topology()
    check(t['bonds'][0]['wbond_device']['reason'] == 'no_wbond_device_for_bond'
          and t['bonds'][0]['observed']['connection_state'] is None,
          'bond without a WBOND device stays unresolved')


def test_topology_stale_source_and_route():
    print('test_topology_stale_source_and_route')
    svc = _svc()
    svc._runtime_raw._errors['primary_device'] = 'timeout'
    svc._runtime_raw._error_ts['primary_device'] = 5
    t = svc.query_topology()
    src = t['as_of']['sources']
    check(src['primary']['stale'] is True and src['wan']['stale'] is False
          and t['as_of']['wan_collected_ts'] == 1,
          'failed primary read flagged stale, others not')
    empty = AnalyzerService()
    check(empty.query_topology() is None, 'None before the first snapshot')
    port = _free_port()
    ok = HTTPServer(('127.0.0.1', port), web._make_handler(svc))
    threading.Thread(target=ok.serve_forever, daemon=True).start()
    port2 = _free_port()
    nope = HTTPServer(('127.0.0.1', port2), web._make_handler(empty))
    threading.Thread(target=nope.serve_forever, daemon=True).start()
    try:
        def get(p):
            try:
                with urllib.request.urlopen(
                        'http://127.0.0.1:%d/api/analytics/topology' % p,
                        timeout=5) as r:
                    return r.status, json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode())
        code, body = get(port)
        check(code == 200 and body['schema_version'] == 1 and
              len(body['overlays']) == 2, 'GET /api/analytics/topology')
        code, body = get(port2)
        check(code == 503 and 'error' in body, '503 before first snapshot')
    finally:
        for s in (ok, nope):
            s.shutdown()
            s.server_close()


TESTS = [
    test_topology_fixture_resolves,
    test_topology_unresolved_not_guessed,
    test_topology_member_conflict_and_missing,
    test_topology_primary_and_bond_runtime_unknowns,
    test_topology_stale_source_and_route,
]
