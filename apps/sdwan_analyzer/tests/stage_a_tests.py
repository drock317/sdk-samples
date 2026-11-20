"""v0.8.3 Stage A: correctness/compatibility regressions.

Bonding membership fingerprint (mode key mismatch, tunnel identity, safe
sorting, versioned definition), flow-total provenance fallback, and the
unchanged legacy steering semantic_fingerprint. Run through run_tests.py.
"""
import copy

import fixtures
from analyzer import normalize

check = None


def bind(check_fn):
    global check
    check = check_fn


def _bond(mode='GRE', members=None, iface='gres_wb0-hub0', name=None):
    return {'iface': iface, 'name': name, 'bonding_mode': mode,
            'members': members if members is not None else [
                {'member_uid': 'ethernet-wan', 'tunnel_iface': 't0',
                 'configured_weight': 80},
                {'member_uid': 'mdm-1', 'tunnel_iface': 't1',
                 'configured_weight': 20}]}


def test_bond_fp_v2_sees_mode_change():
    a = normalize.bonding_membership_fingerprint_v2([_bond('GRE')])
    b = normalize.bonding_membership_fingerprint_v2([_bond('IPSEC')])
    check(a != b, 'v2 bond fingerprint changes when bonding_mode changes')
    # The legacy function reads a key that never exists ('mode'): kept as-is
    # for historical compatibility, its output must not change.
    la = normalize.bonding_membership_fingerprint([_bond('GRE')])
    lb = normalize.bonding_membership_fingerprint([_bond('IPSEC')])
    check(la == lb, 'legacy fingerprint contract unchanged (ignores mode)')


def test_bond_fp_legacy_value_pinned():
    """The legacy digest for a known input must never change."""
    fp = normalize.bonding_membership_fingerprint([_bond('GRE')])
    check(fp == normalize._stable_hash([{
        'iface': 'gres_wb0-hub0', 'name': '', 'mode': '',
        'members': [('ethernet-wan', 80), ('mdm-1', 20)]}]),
        'legacy fingerprint payload shape/digest is pinned')


def test_bond_fp_v2_tunnel_identity():
    a = normalize.bonding_membership_fingerprint_v2([_bond()])
    moved = _bond(members=[
        {'member_uid': 'ethernet-wan', 'tunnel_iface': 'tX',
         'configured_weight': 80},
        {'member_uid': 'mdm-1', 'tunnel_iface': 't1',
         'configured_weight': 20}])
    check(a != normalize.bonding_membership_fingerprint_v2([moved]),
          'v2 fingerprint changes when a member tunnel identity changes')


def test_bond_fp_v2_safe_sorting():
    members = [
        {'member_uid': 'm', 'tunnel_iface': 't', 'configured_weight': None},
        {'member_uid': 'm', 'tunnel_iface': 't', 'configured_weight': 50},
        {'member_uid': 'm', 'tunnel_iface': 't', 'configured_weight': 0},
        {'member_uid': None, 'tunnel_iface': None,
         'configured_weight': None}]
    try:
        a = normalize.bonding_membership_fingerprint_v2([_bond(members=members)])
        b = normalize.bonding_membership_fingerprint_v2(
            [_bond(members=list(reversed(members)))])
        ok = a == b
    except TypeError:
        ok = False
    check(ok, 'v2 fingerprint is order independent with null/mixed weights')
    try:
        normalize.bonding_membership_fingerprint([_bond(members=members)])
        legacy_ok = True
    except TypeError:
        legacy_ok = False
    check(legacy_ok, 'legacy fingerprint no longer raises on null weights')
    # Weight 0 and missing weight are different observations.
    z = normalize.bonding_membership_fingerprint_v2([_bond(members=[
        {'member_uid': 'm', 'tunnel_iface': 't', 'configured_weight': 0}])])
    n = normalize.bonding_membership_fingerprint_v2([_bond(members=[
        {'member_uid': 'm', 'tunnel_iface': 't',
         'configured_weight': None}])])
    check(z != n, 'zero weight differs from a missing weight')


def test_bond_fp_v2_excludes_runtime():
    a = _bond()
    b = copy.deepcopy(a)
    for m in b['members']:
        m['current_download_weight'] = 99
        m['download_flows'] = 12
    check(normalize.bonding_membership_fingerprint_v2([a])
          == normalize.bonding_membership_fingerprint_v2([b]),
          'v2 fingerprint ignores runtime weights and flow gauges')


def _raw_bond(dl=None, ul=None, member_flows=None):
    """Minimal status/sdwan_adv/wan_bonding with chosen flow values."""
    mem = {}
    for i, mf in enumerate(member_flows or []):
        m = {'intf name': 'w{}'.format(i), 'link state': 'UP'}
        if mf is not None:
            dlf, ulf = mf
            if dlf is not None:
                m['DL current flows'] = dlf
            if ulf is not None:
                m['UL current flows'] = ulf
        mem[('t{}'.format(i))] = m
    obj = {'Bonding Mode': 'GRE', 'members_list': mem}
    if dl is not None:
        obj['DL total flows'] = dl
    if ul is not None:
        obj['UL total flows'] = ul
    return {'interfaces': {'wb0': obj}}


def _b(raw):
    return normalize.wan_bonding_detail(raw)['bonds'][0]


def test_flow_totals_members_without_flow_values():
    b = _b(_raw_bond(member_flows=[None, None]))
    check(b['total_download_flows_source'] == 'unknown'
          and b['total_download_flows'] is None,
          'members without numeric flows and no reported total -> unknown/null (DL)')
    check(b['total_upload_flows_source'] == 'unknown'
          and b['total_upload_flows'] is None,
          'members without numeric flows and no reported total -> unknown/null (UL)')
    check(b['computed_download_flows'] is None
          and b['computed_upload_flows'] is None,
          'computed flow sums are null when nothing numeric was reported')


def test_flow_totals_reported_zero_and_summed():
    b = _b(_raw_bond(dl=0, ul=0, member_flows=[(3, 4)]))
    check(b['total_download_flows'] == 0
          and b['total_download_flows_source'] == 'reported',
          'a reported zero total is reported, not summed or unknown')
    b = _b(_raw_bond(member_flows=[(3, 4), (0, 1)]))
    check(b['total_download_flows'] == 3
          and b['total_download_flows_source'] == 'summed',
          'numeric member flows are summed when no total is reported')


def test_flow_totals_per_direction_independent():
    # DL reported; UL has one numeric member -> summed; DL members empty.
    b = _b(_raw_bond(dl=7, member_flows=[(None, 5), (None, None)]))
    check(b['total_download_flows_source'] == 'reported'
          and b['total_download_flows'] == 7, 'DL reported')
    check(b['total_upload_flows_source'] == 'summed'
          and b['total_upload_flows'] == 5, 'UL summed independently')
    # UL has no numeric at all while DL does.
    b = _b(_raw_bond(member_flows=[(2, None), (1, None)]))
    check(b['total_download_flows_source'] == 'summed'
          and b['total_upload_flows_source'] == 'unknown'
          and b['total_upload_flows'] is None,
          'a direction with no numeric flows stays unknown while the other sums')
    # A single numeric zero is a valid sum of zero.
    b = _b(_raw_bond(member_flows=[(0, None)]))
    check(b['total_download_flows'] == 0
          and b['total_download_flows_source'] == 'summed',
          'one numeric zero member flow sums to a valid zero')


def test_legacy_steering_fingerprint_unchanged():
    lrs = normalize.logical_rules(
        fixtures.STEERING_RULES, fixtures.STEERING_STATS,
        fixtures.STEERING_EVENTS,
        normalize.destination_index(fixtures.IDENTITIES_IP), None)
    lr = [r for r in lrs if r['logical_name'] == 'Lab01 - Netflix'][0]
    expect = normalize._stable_hash({
        'name': 'lab01 - netflix',
        'branches': [{'trigger_priority': 11,
                      'traffic_class': 'best effort (data traffic)',
                      'source_lans': ['Primary LAN'],
                      'selector': lr['wan_selector_type'],
                      'wan_priority_enabled': False}],
        'destinations': [fixtures.IDENTITIES_IP[fixtures.DST_NETFLIX]['name']]})
    check(lr['semantic_fingerprint'] == expect,
          'legacy steering semantic_fingerprint payload is unchanged')


TESTS = [
    test_bond_fp_v2_sees_mode_change,
    test_bond_fp_legacy_value_pinned,
    test_bond_fp_v2_tunnel_identity,
    test_bond_fp_v2_safe_sorting,
    test_bond_fp_v2_excludes_runtime,
    test_flow_totals_members_without_flow_values,
    test_flow_totals_reported_zero_and_summed,
    test_flow_totals_per_direction_independent,
    test_legacy_steering_fingerprint_unchanged,
]
