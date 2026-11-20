"""v0.8.4 Phases C-E: cellular engineering analytics, SD-WAN historical
analytics, correlation, config attribution and compatibility checks.
"""
import ast
import json
import os
import re

import cellular_v084_fixtures as X
from analyzer import (cell_analytics as CA, cellular, persistence as P,
                      query, rf_validity as RV, sdwan_analytics as SA)
from analyzer.service import AnalyzerService

check = None
T = 1790380800
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bind(check_fn):
    global check
    check = check_fn


class _Clock(object):
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _rec(diag, ts, uid='mdm-a', status=None, cell='6643981'):
    d = dict(diag)
    if cell is not None:
        d['CELL_ID'] = cell
    r = cellular.normalize_cellular(uid, d, ts, ts, 'collector',
                                    info={'sim': 'sim1'}, status=status)
    return r


def _obs(ts, **sections):
    o = {'collected_ts': ts, 'sample_source': 'scheduled',
         'timestamp': ts, 'collector_errors': []}
    o.update(sections)
    return o


def _cell_obs(seq, step=60, start=T, uid='mdm-a', status=None, cells=None):
    out = []
    for i, d in enumerate(seq):
        ts = start + i * step
        c = (cells[i] if cells else '6643981')
        out.append(_obs(ts, cellular=[_rec(d, ts, uid, status, c)]))
    return out


def _run(observations, **kw):
    an = CA.CellularAnalyzer(**kw)
    for o in observations:
        an.add(o)
    return an


def _types(modem):
    return [e['type'] for e in modem['events']]


# --- Phase C: cellular ---------------------------------------------------------

def test_v084_cell_distribution_and_time_attribution():
    print('test_v084_cell_distribution_and_time_attribution')
    obs = _cell_obs([X.ATT_NSA] * 4, cells=['111', '111', '222', '222'])
    obs += _cell_obs([X.ATT_NSA], start=T + 4 * 60 + 3600, cells=['222'])
    res = _run(obs).result()
    m = res['modems'][0]
    cells = {c['cell_key_hash']: c for c in m['serving_cells']}
    check(len(cells) == 2, 'two serving cells identified')
    a, b = sorted(cells.values(), key=lambda c: c['first_seen_ts'])
    check(a['samples'] == 2 and b['samples'] == 3 and
          a['share_of_samples'] == 0.4 and b['share_of_samples'] == 0.6,
          'sample counts and shares')
    check(a['observed_seconds'] == 120 and b['observed_seconds'] == 60,
          'seconds attributed to the earlier poll; the 1 h silence is NOT')
    check(m['data_quality']['unattributed_seconds'] >= 3600,
          'unattributed time reported')
    check(len(res['gaps']) == 1 and res['gaps'][0]['seconds'] >= 3600,
          'gap listed')
    check(res['observation_interval_s'] == 60 and a['confidence'] == 'low',
          'interval and confidence reported')
    check(all(c['identity_basis'] == 'serving_cell_hash'
              for c in m['serving_cells']), 'privacy-safe identity basis')
    blob = json.dumps(res)
    check('6643981' not in blob, 'no raw cell id anywhere')


def test_v084_lte_vs_nr_usage_and_modes():
    print('test_v084_lte_vs_nr_usage_and_modes')
    seq = [X.ATT_LTE, X.ATT_LTE, X.ATT_NSA, X.ATT_NSA, X.ATT_NSA, X.ATT_LTE]
    m = _run(_cell_obs(seq)).result()['modems'][0]
    tu = m['technology_usage']
    check(tu['lte_only_samples'] == 3 and
          tu['nr_carrier_present_samples'] == 3, 'LTE-only vs NR-present')
    bm = tu['by_service_mode']
    check(bm['LTE']['samples'] == 3 and bm['5G NSA']['samples'] == 3 and
          bm['5G NSA']['share_of_samples'] == 0.5, 'by service mode')


def test_v084_rf_stats_only_valid_readings():
    print('test_v084_rf_stats_only_valid_readings')
    seq = [X.ATT_NSA, X.ATT_RSRP_ZERO, X.ATT_NSA]
    m = _run(_cell_obs(seq)).result()['modems'][0]
    cell = m['serving_cells'][0]
    comp = [c for c in cell['components']
            if c['identity'] == 'LTE:Band 66:66661'][0]
    check(comp['samples'] == 3 and comp['rf']['rsrp']['count'] == 2 and
          comp['rf']['rsrp']['min'] == -98.0 and
          comp['rf']['rsrp']['max'] == -98.0,
          'RSRP 0 on the measured SCell is not averaged in')
    check(comp['excluded']['rsrp'] == {'implausible_range': 1},
          'the exclusion and its reason are reported')
    check(m['excluded']['component_rsrp']['placeholder_zero'] == 3 and
          m['excluded']['component_rsrq']['placeholder_context'] == 3,
          'placeholders on the 0 MHz entry counted per metric')
    check(cell['serving_rf']['rsrp'] == {'count': 3, 'avg': -108.0,
                                         'min': -108.0, 'max': -108.0},
          'serving RF statistics')
    # Never average placeholders: a 0 MHz SCell contributes nothing.
    z = [c for c in cell['components'] if c['channel'] == '658080'][0]
    check(z['rf']['rsrp']['count'] == 0 and z['rf']['rsrq']['count'] == 0,
          '0 MHz placeholder contributes no RF statistics')


def test_v084_events_between_polls_not_exact_handover():
    print('test_v084_events_between_polls_not_exact_handover')
    seq = [X.ATT_LTE, X.ATT_NSA, X.ATT_NSA]
    cells = ['111', '111', '222']
    m = _run(_cell_obs(seq, cells=cells)).result()['modems'][0]
    ev = {e['type']: e for e in m['events']}
    check('radio_mode_change' in ev and 'ca_composition_change' in ev,
          'mode and carrier-aggregation changes detected')
    check(ev['radio_mode_change']['from'] == 'LTE' and
          ev['radio_mode_change']['to'] == '5G NSA', 'mode from/to')
    sc = [e for e in m['events'] if e['type'] == 'serving_cell_change']
    check(len(sc) == 1 and sc[0]['detected_between'] == [T + 60, T + 120],
          'serving cell change only when the CELL changed (not on the '
          'LTE->NSA mode change of the same cell)')
    for e in m['events']:
        check(e['exact_time'] is False and
              e['time_resolution'] == 'between_polls' and
              e['uncertainty_s'] == 60 and 'ts' not in e,
              'no exact time claimed: {}'.format(e['type']))
    ca = ev['ca_composition_change']['detail']
    check(ca['added'] == ['LTE:Band 66:66661', 'NR:Band n77:630720',
                          'NR:Band n77:658080'] and ca['removed'] == [],
          'carrier additions listed')
    check(m['event_counts']['radio_mode_change'] == 1, 'event counts')


def test_v084_component_continuity_and_bandwidth_roles():
    print('test_v084_component_continuity_and_bandwidth_roles')
    a = {'rat': 'NR', 'band': 'N77', 'channel': '630720', 'pci': None}
    check(CA.components_match(a, dict(a, channel='630720')),
          'same channel matches')
    check(not CA.components_match(a, dict(a, channel='630721')),
          'conflicting channel rejects')
    check(CA.components_match(dict(a, channel=None, pci='5'),
                              dict(a, channel='630720', pci='5')),
          'shared PCI matches when only one side has a channel')
    check(not CA.components_match(a, dict(a, band='N41')) and
          not CA.components_match(dict(a, channel=None),
                                  dict(a, channel=None)),
          'different band / no discriminator do not match')
    # Bandwidth change on a continuing component.
    wider = X.copy_of(X.ATT_NSA, BANDWIDTH_5G_SCELL1='80 MHz')
    m = _run(_cell_obs([X.ATT_NSA, wider])).result()['modems'][0]
    bw = [e for e in m['events'] if e['type'] == 'ca_bandwidth_change']
    check(len(bw) == 1 and bw[0]['detail']['components'] == [{
        'identity': 'NR:Band n77:630720', 'from_mhz': 100.0,
        'to_mhz': 80.0}] and 'ca_composition_change' not in _types(m),
        'bandwidth change reported without a composition change')
    # SCell index shift on the same channel is a minor role change only.
    shifted = X.copy_of(X.ATT_NSA)
    for k in list(shifted):
        if k.endswith('_5G_SCELL0'):
            shifted[k.replace('SCELL0', 'SCELL3')] = shifted.pop(k)
    m = _run(_cell_obs([X.ATT_NSA, shifted])).result()['modems'][0]
    rc = [e for e in m['events'] if e['type'] == 'component_role_change']
    check(rc and rc[0]['detail']['significance'] == 'minor' and
          'ca_composition_change' not in _types(m),
          'role-index shift is a minor role change, not a CA change')


def test_v084_missing_observations_and_gaps():
    print('test_v084_missing_observations_and_gaps')
    obs = _cell_obs([X.ATT_NSA, X.ATT_NSA])
    obs.append(_obs(T + 120))                       # cellular not collected
    obs.append(_obs(T + 180, cellular=[]))          # modem absent
    obs.append(_obs(T + 240, cellular=[]))
    obs += _cell_obs([X.ATT_NSA], start=T + 300)
    obs += _cell_obs([X.ATT_NSA], start=T + 300 + 3600)   # long silence
    res = _run(obs).result()
    m = res['modems'][0]
    dq = m['data_quality']
    check(dq['section_not_collected'] == 1 and dq['modem_absent'] == 2 and
          dq['present'] == 4 and
          res['observations_without_cellular_section'] == 1,
          'not-collected section is distinct from an absent modem')
    miss = [e for e in m['events'] if e['type'] == 'modem_missing']
    check(len(miss) == 1 and miss[0]['detail']['absent_since_poll'] ==
          T + 180 and miss[0]['to'] == 'present_again',
          'modem disappearance and return reported')
    gap = [e for e in m['events'] if e['type'] == 'observation_gap']
    check(len(gap) == 1 and gap[0]['confidence'] == 'low' and
          gap[0]['across_gap'] is True,
          'a long silence is a gap event with low confidence')
    tail = _run(_cell_obs([X.ATT_NSA, X.ATT_NSA]) +
                [_obs(T + 120, cellular=[])]).result()['modems'][0]
    check(tail['events'][-1]['to'] == 'absent' and
          tail['events'][-1]['detail']['still_absent_at_window_end'],
          'a modem still absent at the end of the window is reported')


def test_v084_health_history():
    print('test_v084_health_history')
    good = {'connection_state': 'connected', 'cellular_health_score': 82.0,
            'cellular_health_category': 'excellent', 'signal_strength': '100'}
    poor = {'connection_state': 'connected', 'cellular_health_score': 30.0,
            'cellular_health_category': 'poor', 'signal_strength': '40'}
    obs = []
    for i, st in enumerate((good, good, poor, poor, good)):
        ts = T + i * 60
        obs.append(_obs(ts, cellular=[_rec(X.ATT_NSA, ts, status=st)]))
    m = _run(obs).result()['modems'][0]
    h = m['health']
    check(h['by_category']['excellent']['samples'] == 3 and
          h['by_category']['poor']['samples'] == 2 and
          h['score'] == {'count': 5, 'avg': 61.2, 'min': 30.0, 'max': 82.0},
          'health categories and score statistics')
    ch = [e for e in m['events'] if e['type'] == 'health_category_change']
    check(len(ch) == 2 and ch[0]['from'] == 'excellent' and
          ch[0]['to'] == 'poor', 'health transitions')


def test_v084_connection_state_and_legacy_records():
    print('test_v084_connection_state_and_legacy_records')
    up = {'connection_state': 'connected'}
    down = {'connection_state': 'disconnected'}
    obs = [_obs(T + i * 60, cellular=[_rec(X.ATT_NSA, T + i * 60, status=s)])
           for i, s in enumerate((up, down, up))]
    m = _run(obs).result()['modems'][0]
    check(_types(m).count('connection_state_change') == 2,
          'modem connection changes')
    legacy = {
        'uid': 'mdm-a', 'service_mode': '5G NSA', 'rsrp': 0.0, 'rsrq': -9.0,
        'sinr': 60.0, 'cell_key_hash': 'aaaa', 'carrier_count': 1,
        'total_active_mhz': 10.0, 'context': {'connection_state': 'connected'},
        'carriers': [{'role': 'pcell', 'rat': 'LTE', 'band': 'Band 66',
                      'bandwidth_mhz': 10.0, 'channel': '1', 'pci': '1',
                      'active': True, 'rf': {
                          'rsrp': 0.0, 'rsrq': -3.0, 'sinr': None,
                          'sinr_raw': 25.0, 'sinr_scale': 'ambiguous',
                          'validity': {'rsrp': 'valid', 'rsrq': 'valid',
                                       'sinr': 'ambiguous_scale'}}}]}
    lg2 = dict(legacy, cell_key_hash='bbbb', service_mode='LTE')
    res = _run([_obs(T, cellular=[legacy]),
                _obs(T + 60, cellular=[lg2])]).result()
    m = res['modems'][0]
    check(m['data_quality']['legacy_records'] == 2 and
          m['data_quality']['records_with_v084_validity'] == 0,
          'legacy (v0.8.3) records recognized')
    check(m['excluded']['rsrp'] == {'implausible_range': 2} and
          m['excluded']['sinr'] == {'implausible_range': 2} and
          m['excluded']['component_rsrp'] == {'implausible_range': 2} and
          m['excluded']['component_sinr'] == {'unknown_scale': 2},
          'legacy RSRP 0 / SINR 60 / ambiguous scale are excluded when read')
    sc = [e for e in m['events'] if e['type'] == 'serving_cell_change']
    check(len(sc) == 1 and sc[0]['confidence'] == 'low' and
          sc[0]['detail']['ambiguous_with_mode_change'] is True,
          'legacy hash change coinciding with a mode change is not trusted')
    check(m['serving_cells'][0]['identity_basis'] == 'cell_key_hash_legacy',
          'identity basis states the legacy hash')


def test_v084_event_bounds_and_confidence_levels():
    print('test_v084_event_bounds_and_confidence_levels')
    seq = [X.ATT_LTE, X.ATT_NSA] * 30
    res = _run(_cell_obs(seq), max_events=10).result()
    m = res['modems'][0]
    check(len(m['events']) == 10 and m['events_truncated'] is True and
          m['event_counts']['radio_mode_change'] == 59,
          'events capped; counts remain complete')
    check(CA.confidence(2) == 'low' and CA.confidence(10) == 'medium' and
          CA.confidence(50) == 'high' and CA.confidence(50, 0.2) == 'medium',
          'confidence thresholds')


# --- Phase D: SD-WAN ------------------------------------------------------------

def _steer(rules, defs_v=1, attribution='read', changed=False,
           restarted=False):
    sa = {'source_state': 'ok', 'defs_version': defs_v,
          'capture_restarted': restarted, 'rules': rules}
    return sa, {'steering_rule_defs': {'version': defs_v,
                                       'attribution': attribution,
                                       'changed': changed}}


def _rule(name, pf, nf, apps=(), matched=True):
    return {'rule': name, 'rule_key': name.lower(), 'preferred_flows': pf,
            'nonpreferred_flows': nf, 'preferred_flows_state': 'value',
            'nonpreferred_flows_state': 'value', 'was_flow_matched': matched,
            'apps_total': len(apps), 'apps_omitted': 0,
            'apps': [dict(a) for a in apps]}


def test_v084_steering_application_history():
    print('test_v084_steering_application_history')
    app = {'id': 'a1', 'appname': 'Teams', 'catname': 'None',
           'matched': True, 'identity_uuids': ['u'], 'in_rule_definition': True}
    an = SA.SdwanAnalyzer()
    for i, (pf, nf, ver) in enumerate(((5, 0, 1), (7, 1, 1), (0, 0, 2))):
        sa, cv = _steer([_rule('Teams', pf, nf,
                               [dict(app, preferred_flows=pf,
                                     nonpreferred_flows=nf)],
                               matched=pf > 0)], defs_v=ver,
                        attribution='read' if i < 2 else 'latest_known',
                        changed=(i == 2))
        steer = [{'logical_name': 'Teams',
                  'preferred_wan_uid': 'wbond-1' if i < 2 else 'ethernet-wan'}]
        an.add(_obs(T + i * 60, steering_apps=sa, steering=steer,
                    config_versions=cv))
    res = an.result()
    r = res['steering']['rules'][0]
    check(r['samples'] == 3 and r['matched_samples'] == 2 and
          r['preferred_flows'] == {'count': 3, 'avg': 4.0, 'min': 0.0,
                                   'max': 7.0} and r['flow_values'] == 'gauge',
          'rule flow gauges: statistics over polls (no totals)')
    check(r['applications'][0]['appname'] == 'Teams' and
          r['applications'][0]['samples'] == 3 and
          r['applications'][0]['nonpreferred_flows']['max'] == 1.0,
          'observed application history')
    check([v['version'] for v in r['definition_versions']] == [1, 2] and
          r['definition_versions'][0]['samples'] == 2,
          'rule observed under two definition revisions')
    revs = res['steering']['definition_revisions']
    check([x['version'] for x in revs] == [1, 2] and
          revs[1]['attribution'] == 'latest_known',
          'definition revisions carry their attribution')
    kinds = [e['type'] for e in res['events']]
    check('steering_definition_revision' in kinds and
          'steering_preferred_wan_change' in kinds and r[
              'preferred_wan_changes'] == 1,
          'revision and preferred-WAN change events')
    check('applications_truncated' in r and
          r['zero_flow_unmatched_apps_listed'] is False,
          'omitted zero-flow apps are declared, not silently dropped')


def _bond(members, mode='GRE', dl=None, ul=None):
    return {'iface': 'gres_wb0-hub0', 'link_state': 'UP',
            'bonding_mode': mode, 'member_count': len(members),
            'total_download_flows': dl, 'total_upload_flows': ul,
            'total_download_flows_source': 'reported',
            'total_upload_flows_source': 'reported', 'members': members}


def _member(uid, cw=50, dw=50, uw=50, link='UP', attach='ATTACHED',
            overlay=None, dfl=3, ufl=1):
    m = {'member_uid': uid, 'tunnel_iface': 'gres-' + uid,
         'configured_weight': cw, 'current_download_weight': dw,
         'current_upload_weight': uw, 'download_flows': dfl,
         'upload_flows': ufl, 'has_loss': 0, 'link_state': link,
         'attach_state': attach}
    if overlay:
        m['overlay_uid'] = overlay
    return m


def test_v084_bond_members_weights_static_dynamic_arbitrary():
    print('test_v084_bond_members_weights_static_dynamic_arbitrary')
    an = SA.SdwanAnalyzer()
    seq = [
        [_member('eth', 50, 50, 50), _member('mdm-1', 50, 54, 50)],
        [_member('eth', 50, 45, 50), _member('mdm-1', 50, 54, 50)],
        [_member('eth', 50, 20, 50), _member('mdm-1', 50, 80, 50, link='DOWN')],
        [_member('eth', 50, 20, 50)],                       # member removed
        [_member('eth', 50, 20, 50), _member('mdm-2', 33, 33, 33),
         _member('mdm-3', 33, 33, 33), _member('mdm-4', 33, 33, 33),
         _member('mdm-5', 33, 33, 33)],                     # 5 members
    ]
    for i, mem in enumerate(seq):
        an.add(_obs(T + i * 60, wbond=[_bond(mem, dl=62, ul=16)]))
    res = an.result()
    b = res['bonds'][0]
    check(b['samples'] == 5 and b['member_count']['max'] == 5.0 and
          b['member_count']['min'] == 1.0 and len(b['members']) == 6,
          'arbitrary member counts (1..5) and all members tracked')
    eth = [m for m in b['members'] if m['member_uid'] == 'eth'][0]
    check(eth['download_weight'] == {'count': 5, 'avg': 31.0, 'min': 20.0,
                                     'max': 50.0} and
          eth['weight_values'] == 'ncos_control_values_not_traffic_share'
          and eth['runtime_weight_changes'] >= 1,
          'runtime weights reported as control values')
    kinds = [e['type'] for e in res['events']]
    check(kinds.count('bond_member_removed') == 1 and
          kinds.count('bond_member_added') == 4 and
          'bond_member_state_change' in kinds and
          'bond_runtime_weight_shift' in kinds,
          'member add/remove, link-state and weight-shift events')
    shift = [e for e in res['events']
             if e['type'] == 'bond_runtime_weight_shift'][0]
    check(shift['detail']['threshold_points'] == 10 and
          shift['detail']['note'] == 'control value, not traffic share',
          'weight shifts carry their threshold and meaning')
    check(b['flow_total_sources'] and b['flow_values'] == 'gauge',
          'flow-total provenance kept')
    blob = json.dumps(res).lower()
    check('share_pct' not in blob and 'efficiency' not in blob.replace(
        'no traffic distribution or efficiency', '') and
        'traffic_share' in blob, 'no traffic distribution is derived')


def _iface(uid, cls, rx, tx, ts, state='connected'):
    return {'uid': uid, 'object_class': cls, 'connection_state': state,
            'rx_bytes': rx, 'tx_bytes': tx, 'collected_ts': ts,
            'metric_ts': ts, 'timestamp_source': 'collector'}


def test_v084_throughput_per_layer_never_summed():
    print('test_v084_throughput_per_layer_never_summed')
    an = SA.SdwanAnalyzer()
    for i in range(4):
        ts = T + i * 60
        ifs = [_iface('mdm-1', 'physical', 1000000 * i * 10,
                      500000 * i * 10, ts),
               _iface('sdwan-hub0_1', 'sdwan', 900000 * i * 10,
                      450000 * i * 10, ts),
               _iface('wbond0', 'wbond', 1800000 * i * 10,
                      900000 * i * 10, ts)]
        if i == 3:                                 # counter reset on rx
            ifs[0]['rx_bytes'] = 10
        an.add(_obs(ts, interfaces=ifs, wbond=[_bond(
            [_member('mdm-1', overlay='sdwan-hub0_1')])]))
    res = an.result()
    rows = {r['uid']: r for r in res['interfaces']['by_interface']}
    phys = rows['mdm-1']
    check(phys['rx']['intervals_used'] == 2 and
          phys['rx']['intervals_excluded'] == {'counter_decrease': 1} and
          phys['tx']['intervals_used'] == 3, 'counter reset excluded')
    check(abs(phys['tx']['avg_mbps'] - 0.667) < 0.01 and
          rows['sdwan-hub0_1']['layer'] == 'sdwan' and
          all(r['additive'] is False for r in rows.values()),
          'rates per interface with layer labels, flagged non-additive')
    check('total' not in json.dumps(res['interfaces']).lower().replace(
        'not additive', ''), 'no cross-layer total anywhere')
    mem = res['bonds'][0]['members'][0]['throughput']
    check(mem['physical_layer']['uid'] == 'mdm-1' and
          mem['overlay_layer']['uid'] == 'sdwan-hub0_1' and
          mem['additive'] is False, 'member throughput lists both layers '
          'separately')
    nomap = SA.SdwanAnalyzer()
    nomap.add(_obs(T, wbond=[_bond([_member('mdm-1')])]))
    check(nomap.result()['bonds'][0]['members'][0]['throughput'][
        'overlay_layer'] is None, 'unmappable overlay stays null')


def test_v084_qoe_and_swans_quality_no_loss_percentage():
    print('test_v084_qoe_and_swans_quality_no_loss_percentage')
    an = SA.SdwanAnalyzer()
    lat = (20, 22, 180, 21)
    for i, v in enumerate(lat):
        q = {'uid': 'hub0_1', 'latency_avg_ms': v, 'latency_last_ms': v,
             'pkt_loss_stats_raw': {'pkts-intf-qoe-dn-tot-pkts-lost': 3}}
        if i == 3:
            q['validity'] = {'latency_avg': 'sentinel',
                             'latency_last': 'valid'}
            q['pkt_loss_sentinel_candidates'] = 1
        an.add(_obs(T + i * 60, qoe=[q], swans_samples=[
            {'uid': 'mdm-1', 'latency_ms': 30 + i, 'jitter_ms': 4}]))
    res = an.result()
    q = res['quality']['qoe_paths'][0]
    check(q['latency_avg_ms']['count'] == 3 and
          q['latency_avg_ms']['max'] == 180.0 and
          q['excluded'] == {'latency_avg:sentinel': 1} and
          q['loss_sentinel_candidate_samples'] == 1,
          'invalid latency excluded with its reason')
    check(q['packet_loss_percentage'] is None and
          'not computed' in q['packet_loss_note'] and
          q['raw_loss_counters_present_samples'] == 4,
          'no packet-loss percentage; counters only noted as present')
    st = [e for e in res['events'] if e['type'] == 'qoe_latency_step']
    check(len(st) == 1 and st[0]['to'] == 180 and
          st[0]['detail']['heuristic']['min_step_ms'] == 50,
          'latency step flagged as a documented heuristic')
    sw = res['quality']['swans_paths'][0]
    check(sw['samples'] == 4 and sw['jitter_ms']['avg'] == 4.0,
          'SWANS latency/jitter statistics')


# --- config attribution -------------------------------------------------------------

def test_v084_config_version_attribution_roundtrip():
    print('test_v084_config_version_attribution_roundtrip')
    cv = {'steering_rule_defs': {'version': 3, 'fingerprint': 'f',
                                 'changed': False,
                                 'attribution': 'latest_known'},
          'wbond_config': {'version': 1, 'fingerprint': 'g', 'changed': True,
                           'attribution': 'read'}}
    rec = P.encode_observation({
        'collected_ts': T, 'sample_source': 'scheduled', 'timestamp': T,
        'config_versions': cv})
    check(rec['cv'] == {'steering_rule_defs': 3, 'wbond_config': 1} and
          rec['cvl'] == ['steering_rule_defs'] and rec['cvc'] ==
          ['wbond_config'], 'persisted form marks latest-known references')
    out = P.decode_observation(rec, fingerprint_lookup=lambda n, v: 'fp')
    check(out['config_versions']['steering_rule_defs']['attribution'] ==
          'latest_known' and out['config_versions']['wbond_config'][
              'attribution'] == 'read' and
          out['config_versions']['steering_rule_defs']['changed'] is False,
          'attribution survives the round trip')
    legacy = {'collected_ts': T, 'sample_source': 'scheduled',
              'cv': {'wbond_config': 1}}
    check(P.decode_observation(legacy)['config_versions']['wbond_config'][
        'attribution'] == 'unspecified',
        'v0.8.3 record without attribution reads as unspecified')


# --- correlation ----------------------------------------------------------------------

def test_v084_correlation_is_evidence_not_cause():
    print('test_v084_correlation_is_evidence_not_cause')
    seq = [X.ATT_LTE, X.ATT_LTE, X.ATT_NSA, X.ATT_NSA, X.ATT_NSA,
           X.ATT_NSA]
    cell = _run(_cell_obs(seq, uid='mdm-a'))
    sd = SA.SdwanAnalyzer()
    for i in range(6):
        mem = [_member('mdm-a', 50, 50, 50), _member('mdm-b', 50, 50, 50)]
        if i >= 2:
            mem[0] = _member('mdm-a', 50, 20, 50)       # shift, same poll
        if i == 4:
            mem[1] = _member('mdm-b', 50, 50, 50, link='DOWN')   # later
        sd.add(_obs(T + i * 60, wbond=[_bond(mem)]))
    sres = sd.result()
    corr = SA.correlate(cell.events(), sres['_events_all'], 60, None, 300)
    pairs = corr['pairs']
    top = pairs[0]
    check(top['evidence'] == 'A_same_poll_same_path' and top['same_path'] and
          top['cellular_event']['uid'] == 'mdm-a' and
          top['sdwan_event']['type'] == 'bond_runtime_weight_shift' and
          top['relation'] == 'temporal_coincidence' and
          top['cause_asserted'] is False, 'same poll + same modem ranks first')
    levels = {p['evidence'] for p in pairs}
    check('B_same_poll' in levels or 'C_adjacent_polls_same_path' in levels or
          'D_adjacent_polls' in levels, 'weaker levels exist')
    other = [p for p in pairs if p['sdwan_event'].get('physical_uid') ==
             'mdm-b']
    check(other and all(not p['same_path'] for p in other),
          'an event on another modem is not same_path')
    check(corr['window_s'] == 120 and corr['event_rates_per_hour'][
        'cellular']['radio_mode_change']['per_hour'] == 12.0,
        'window and per-hour base rates for judging chance coincidence')
    blob = json.dumps(corr).lower()
    check('root_cause' not in blob and 'caused' not in blob.replace(
        'not causation', '').replace('never asserts a cause', ''),
        'no causal wording in the payload')
    check(SA.correlate([], [], None)['pairs'] == [], 'empty input is safe')
    far = SA.correlate(cell.events(), sres['_events_all'], 60, 60, 300)
    check(far['window_s'] == 60, 'caller window honoured within bounds')
    check(SA.correlate([], [], 60, 99999)['window_s'] == 1800,
          'window clamped')


# --- service/API ------------------------------------------------------------------------

def _svc_with(observations, now):
    svc = AnalyzerService(clock=_Clock(now))
    for o in observations:
        svc._store.append(o)
    return svc


def test_v084_api_contracts_and_bounds():
    print('test_v084_api_contracts_and_bounds')
    obs = []
    for i in range(8):
        ts = T + i * 60
        o = _obs(ts, cellular=[_rec(X.ATT_NSA, ts, status=X.ATT_STATUS)],
                 wbond=[_bond([_member('mdm-a')])],
                 interfaces=[_iface('mdm-a', 'physical', i * 1000, i * 500,
                                    ts)])
        obs.append(o)
    svc = _svc_with(obs, T + 600)
    r = svc.query_cellular_analysis('')
    check(r['schema_version'] == 1 and r['observations'] == 8 and
          r['modems'][0]['uid'] == 'mdm-a' and
          r['window']['truncated'] is False and
          r['window']['limits']['max_scan_bytes'] == 8 * 1048576 and
          r['notes'], 'cellular analysis contract')
    r1 = svc.query_cellular_analysis('uid=mdm-a&max_events=5')
    check(len(r1['modems'][0]['events']) <= 5, 'max_events honoured')
    r2 = svc.query_cellular_analysis('uid=mdm-zzz')
    check(r2['modems'] == [], 'unknown uid -> no modems, not an error')
    s = svc.query_sdwan_history('')
    check('_events_all' not in s and s['bonds'][0]['members'][0][
        'member_uid'] == 'mdm-a' and s['interfaces']['by_interface'],
        'sdwan history contract (internal event list not exposed)')
    c = svc.query_correlation('')
    check(c['cause_asserted'] is False and 'correlation' in c and
          c['correlation']['notes'], 'correlation contract')
    for bad in ('start=x', 'uid=%20', 'max_events=0', 'max_events=9999',
                'bogus=1', 'start=10&end=5'):
        try:
            svc.query_cellular_analysis(bad)
            ok = False
        except query.QueryError:
            ok = True
        check(ok, 'rejects {!r}'.format(bad))
    try:
        svc.query_correlation('window_s=5')
        ok = False
    except query.QueryError:
        ok = True
    check(ok, 'rejects window_s below the bound')
    import analyzer.service as S
    saved = S.ANALYSIS_MAX_OBS
    try:
        S.ANALYSIS_MAX_OBS = 3
        t = svc.query_cellular_analysis('')
        check(t['window']['truncated'] is True and
              t['window']['next_start'] is not None and
              t['observations'] <= 50, 'truncation reported with next_start')
    finally:
        S.ANALYSIS_MAX_OBS = saved


def test_v084_http_routes_and_errors():
    print('test_v084_http_routes_and_errors')
    import threading
    import urllib.error
    import urllib.request
    from http.server import HTTPServer
    from analyzer import web
    obs = [_obs(T + i * 60, cellular=[_rec(X.ATT_NSA, T + i * 60)])
           for i in range(4)]
    svc = _svc_with(obs, T + 400)
    import socket
    sk = socket.socket()
    sk.bind(('127.0.0.1', 0))
    port = sk.getsockname()[1]
    sk.close()
    srv = HTTPServer(('127.0.0.1', port), web._make_handler(svc))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(path):
        try:
            with urllib.request.urlopen(
                    'http://127.0.0.1:%d%s' % (port, path), timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())
    try:
        for path in ('/api/analytics/cellular', '/api/analytics/sdwan_history',
                     '/api/analytics/correlation'):
            code, body = get(path)
            check(code == 200 and body.get('schema_version') == 1,
                  'GET {}'.format(path))
        code, body = get('/api/analytics/cellular?start=zz')
        check(code == 400 and 'error' in body, 'bad start -> 400')
        code, body = get('/api/analytics/correlation?window_s=1')
        check(code == 400, 'bad window_s -> 400')
        code, body = get('/api/analytics/cellular?uid=mdm-a&max_events=3')
        check(code == 200 and body['modems'][0]['uid'] == 'mdm-a',
              'uid filter over HTTP')
    finally:
        srv.shutdown()
        srv.server_close()


def test_v084_python38_syntax_and_library_compat():
    print('test_v084_python38_syntax_and_library_compat')
    files = ['rf_validity', 'cell_analytics', 'sdwan_analytics', 'retention',
             'cellular', 'durable', 'persistence', 'service', 'telemetry',
             'web', 'inspector']
    bad_calls = re.compile(
        r'\.removeprefix\(|\.removesuffix\(|functools\.cache\b|'
        r'math\.lcm|\.bit_count\(|zip\([^)]*strict=|\bmatch\s+\w+:\s*$|'
        r'->\s*(list|dict|tuple|set)\[|:\s*(list|dict|tuple|set)\[\w|'
        r'\w\s\|\s(None|str|int|dict|list)\b.*:\s*$')
    problems = []
    for name in files:
        path = os.path.join(APP, 'analyzer', name + '.py')
        src = open(path).read()
        try:
            ast.parse(src, filename=path, feature_version=(3, 8))
        except SyntaxError as e:
            problems.append('{}: {}'.format(name, e))
        for n, line in enumerate(src.splitlines(), 1):
            code = line.split('#')[0]
            if bad_calls.search(code):
                problems.append('{}:{} {}'.format(name, n, line.strip()))
    check(not problems, 'Python 3.8 grammar and 3.9+ API scan clean '
          '{}'.format(problems[:3]))


TESTS = [
    test_v084_cell_distribution_and_time_attribution,
    test_v084_lte_vs_nr_usage_and_modes,
    test_v084_rf_stats_only_valid_readings,
    test_v084_events_between_polls_not_exact_handover,
    test_v084_component_continuity_and_bandwidth_roles,
    test_v084_missing_observations_and_gaps,
    test_v084_health_history,
    test_v084_connection_state_and_legacy_records,
    test_v084_event_bounds_and_confidence_levels,
    test_v084_steering_application_history,
    test_v084_bond_members_weights_static_dynamic_arbitrary,
    test_v084_throughput_per_layer_never_summed,
    test_v084_qoe_and_swans_quality_no_loss_percentage,
    test_v084_config_version_attribution_roundtrip,
    test_v084_correlation_is_evidence_not_cause,
    test_v084_api_contracts_and_bounds,
    test_v084_http_routes_and_errors,
    test_v084_python38_syntax_and_library_compat,
]
