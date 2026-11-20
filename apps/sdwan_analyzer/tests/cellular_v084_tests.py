"""v0.8.4 Phase A: cellular correctness (field-aware RF validity, SINR scale
provenance, cell-id canonicalization, role aliases, health, Speedtest sync).
Fixtures are sanitized real-device captures (cellular_v084_fixtures.py).
"""
import ast
import json
import os

import cellular_v084_fixtures as X
from analyzer import cellular
from analyzer import rf_validity as RV

check = None


def bind(check_fn):
    global check
    check = check_fn


def _rec(diag, status=None):
    return cellular.normalize_cellular('mdm-x', diag, 100, 100, 'collector',
                                       info={'sim': 'sim1'}, status=status)


def _by_role(rec):
    return {c['role']: c for c in rec['carriers']}


def test_v084_rsrp_zero_measured_vs_unmeasured():
    print('test_v084_rsrp_zero_measured_vs_unmeasured')
    r = _by_role(_rec(X.ATT_RSRP_ZERO))
    s0 = r['scell0']['rf']
    check(s0['rsrp'] is None and s0['validity']['rsrp'] == 'implausible_range'
          and s0['raw']['rsrp'] == 0.0,
          'RSRP 0 on an ACTIVE measured 5 MHz carrier: implausible, raw kept')
    check(s0['validity']['rsrq'] == 'valid' and s0['rsrq'] == -10.0,
          'the same entry keeps its other valid metrics')
    s2 = r['scell2']['rf']
    check(s2['validity']['rsrp'] == 'placeholder_zero' and
          s2['empty_reason'] == 'zero_bandwidth',
          'RSRP 0 on the 0 MHz entry is a placeholder, not implausible')


def test_v084_rsrq_minus3_context():
    print('test_v084_rsrq_minus3_context')
    r = _by_role(_rec(X.VERIZON))
    check(r['scell0']['rf']['rsrq'] == -3.0 and
          r['scell0']['rf']['validity']['rsrq'] == 'valid',
          'RSRQ -3 on a measured carrier (real RSSI) is valid')
    s2 = r['scell2']['rf']
    check(s2['rsrq'] is None and
          s2['validity']['rsrq'] == 'placeholder_context' and
          s2['raw']['rsrq'] == -3.0,
          'RSRQ -3 on the unmeasured 0 MHz / RSSI 0 entry is a placeholder')
    # RSSI 0 + RSRP 0 on an entry with non-zero bandwidth is also unmeasured.
    d = X.copy_of(X.VERIZON, BANDWIDTH_5G_SCELL2='20 MHz')
    s2b = _by_role(_rec(d))['scell2']['rf']
    check(s2b['empty_reason'] == 'rssi_zero' and s2b['rsrq'] is None,
          'RSSI 0 with RSRP 0 marks the entry unmeasured even with bandwidth')
    # RSRQ -3 with nothing wrong around it is never rejected by value alone.
    check(RV.validate_metric('rsrq', -3.0, 'LTE')[1] == 'valid',
          'RSRQ -3 alone is within the reporting range')


def test_v084_sinr_scale_from_field_family():
    print('test_v084_sinr_scale_from_field_family')
    r = _by_role(_rec(X.ATT_NSA))
    s1 = r['scell1']['rf']
    check(s1['sinr'] == 4.5 and s1['sinr_raw'] == 45.0 and
          s1['sinr_scale'] == 'tenths_family_inferred',
          'integer token 45 is 4.5 dB by FAMILY (not magnitude), labelled '
          'inferred')
    v = _by_role(_rec(X.VERIZON))
    check(v['pcell']['rf']['sinr'] == 18.5 and
          v['pcell']['rf']['sinr_scale'] == 'tenths_corroborated' and
          v['scell1']['rf']['sinr'] == 27.0 and
          v['scell1']['rf']['sinr_scale'] == 'tenths_corroborated',
          'components that ARE the serving carrier corroborate the scale')
    # A small integer is still tenths; a decimal token is dB. Neither is
    # decided by size.
    r1 = RV.component_sinr('7', 'NR')
    r2 = RV.component_sinr('7.5', 'NR')
    r3 = RV.component_sinr('250', 'NR')
    check((r1['value'], r1['scale']) == (0.7, 'tenths_family_inferred'),
          'integer 7 -> 0.7 dB')
    check((r2['value'], r2['scale']) == (7.5, 'db_decimal_token'),
          'decimal token is dB as reported')
    check((r3['value'], r3['scale']) == (25.0, 'tenths_family_inferred'),
          'integer 250 -> 25.0 dB')
    check(RV.component_sinr('900', 'NR')['validity'] == 'implausible_range',
          'scaled value outside the reporting range is withheld')
    st = RV.component_sinr('250', 'NR', unmeasured_reason='zero_bandwidth')
    check(st['value'] is None and st['validity'] == 'placeholder_context' and
          st['raw'] == 250.0, 'SINR on an unmeasured entry is a placeholder')


def test_v084_sinr_reference_conflict_and_weak_tier():
    print('test_v084_sinr_reference_conflict_and_weak_tier')
    # Weak tier: 9.1 vs serving 0.8 (8.3 dB apart): consistent, not
    # corroborating.
    a = _by_role(_rec(X.ATT_NSA))['pcell']['rf']
    check(a['sinr'] == 9.1 and a['sinr_scale'] == 'tenths_family_inferred'
          and a['sinr_ref_diff_db'] == 8.3,
          'AT&T PCell 9.1 vs serving 0.8: reported, not called corroborated')
    # Conflict: tenths reading is 25 dB away from the serving carrier.
    d = X.copy_of(X.VERIZON, SINR_5G_SCELL1='9')
    c = _by_role(_rec(d))['scell1']['rf']
    check(c['sinr'] is None and c['sinr_scale'] == 'unknown' and
          c['validity']['sinr'] == 'unknown_scale' and c['sinr_raw'] == 9.0,
          'contradicted scale: value withheld, raw preserved')


def test_v084_serving_validity_and_raw_preserved():
    print('test_v084_serving_validity_and_raw_preserved')
    d = X.copy_of(X.VERIZON, RSRP='0', RSRQ_5G='-3', SINR='99')
    r = _rec(d)
    check(r['rsrp'] is None and r['rf_validity']['rsrp'] ==
          'implausible_range' and r['rf_raw']['rsrp'] == 0.0,
          'serving RSRP 0: value withheld, raw kept in rf_raw')
    check(r['sinr'] is None and r['rf_raw']['sinr'] == 99.0,
          'serving SINR 99 dB is implausible')
    check(r['rsrq_5g'] == -3.0 and r['rf_validity']['rsrq_5g'] == 'valid',
          'serving RSRQ -3 is not rejected by value')
    ok = _rec(X.VERIZON)
    check(ok['rsrp'] == -79.0 and 'rf_raw' not in ok and
          ok['rf_version'] == 2, 'valid serving values unchanged, versioned')


def test_v084_cell_id_canonicalization_and_hash():
    print('test_v084_cell_id_canonicalization_and_hash')
    c = RV.canonical_cell_id
    check(c('6643981 (0x65610d)') == '6643981', 'decimal form with hex note')
    check(c('0x65610d') == '6643981', 'bare hex form')
    check(c('  6643981 ') == '6643981', 'whitespace')
    check(c('6643981') == '6643981', 'plain decimal')
    check(c('0') == '' and c('None') == '' and c('') == '' and
          c(None) == '' and c('abc') == '' and c('-5') == '',
          'zero/empty/garbage are not identities')
    h1 = cellular.cell_key_hash('Verizon', '5G NSA', '6643981 (0x65610d)',
                                '371', 'Band 66')
    h2 = cellular.cell_key_hash('Verizon', '5G NSA', '0x65610d', '371',
                                'Band 66')
    h3 = cellular.cell_key_hash('Verizon', '5G NSA', '6643981', '371',
                                'Band 66')
    check(h1 == h2 == h3 and len(h1) == 16,
          'formatting differences of one cell hash alike')
    check(cellular.cell_key_hash('V', 'LTE', 'garbage', '1', 'B2') is None,
          'unusable id yields no hash (never invented)')
    rec = _rec(X.VERIZON)
    want = cellular.cell_key_hash(None, '5G NSA', '6643981', '371',
                                  'Band 66')
    check(rec['cell_key_hash'] == want and rec['cell_key_state'] == 'ok',
          'record hash uses the canonical id')
    check(_rec(X.ATT_NSA)['cell_key_state'] == 'absent' and
          _rec(X.copy_of(X.ATT_LTE, CELL_ID='garbage'))[
              'cell_key_state'] == 'unusable', 'id state reported')
    blob = json.dumps(rec)
    check('6643981' not in blob and '0x65610d' not in blob,
          'raw cell id is never persisted')
    check(RV.normalize_plmn('310-260') == '310260' and
          RV.normalize_plmn('99') == '', 'PLMN normalization')


def test_v084_role_aliases_and_dedupe_preserved():
    print('test_v084_role_aliases_and_dedupe_preserved')
    v = _rec(X.VERIZON)
    r = _by_role(v)
    check(v['carrier_count'] == 4 and 'nr_primary' in r['scell1'][
        'role_aliases'] and 'scell1' in r['scell1']['role_aliases'],
          'Verizon: NR primary alias survives dedupe into scell1')
    check(r['pcell']['role_aliases'] == ['pcell'], 'single-role carrier')
    t = _rec(X.TMOBILE)
    nr = [c for c in t['carriers'] if c['rat'] == 'NR'][0]
    check(t['carrier_count'] == 2 and nr['role_aliases'] == [
        'nr_primary', 'scell0', 'scell1'],
        'T-Mobile: identical SCell0/SCell1 + channel-less nr_primary keep '
        'all three aliases')
    check(t['carrier_evidence']['ambiguous_identity_count'] == 1,
          'identical SCell identity still flagged ambiguous')
    check(t['total_active_mhz'] == 110.0,
          'collapsed duplicates are not double counted in bandwidth')
    one = _rec(X.R1900_TMOBILE)
    check([c['role'] for c in one['carriers']] == ['pcell', 'nr_primary'],
          'R1900: LTE pcell + NR nr_primary roles preserved')


def test_v084_lte_state_and_unregistered_sentinels():
    print('test_v084_lte_state_and_unregistered_sentinels')
    r = _rec(X.ATT_LTE)
    check(r['service_mode'] == 'LTE' and r['carrier_count'] == 1 and
          r['carriers'][0]['rf'] is None and
          r['rf_validity']['rsrp_5g'] == 'missing',
          'AT&T LTE-only: one carrier, no component RF, 5G fields missing')
    check(RV.parse_token('Not Registered')[0] is None and
          RV.parse_token('not available')[0] is None,
          'registration sentinels are absent values')


def test_v084_modem_health():
    print('test_v084_modem_health')
    h = _rec(X.VERIZON, X.VERIZON_STATUS)['health']
    check(h == {'score': 82.0, 'category': 'excellent',
                'signal_strength': 100, 'source': 'ncos_status'},
          'NCOS health score/category/signal strength collected')
    g = _rec(X.ATT_NSA, X.ATT_STATUS)['health']
    check(g['category'] == 'good' and g['score'] == 64.05 and
          g['signal_strength'] == 84, 'second modem health')
    check('health' not in _rec(X.VERIZON, {'connection_state': 'connected'}),
          'absent health fields are omitted, never zero')
    bad = RV.modem_health({'cellular_health_score': 250,
                           'signal_strength': '-4'})
    check(bad['score'] is None and bad['score_raw'] == 250.0 and
          bad['signal_strength'] is None, 'out-of-range health is withheld')
    check(RV.modem_health(None) is None and RV.modem_health({}) is None,
          'no status -> no health')


def test_v084_legacy_record_shape_compatible():
    print('test_v084_legacy_record_shape_compatible')
    rec = _rec(X.R1900_TMOBILE)
    legacy_keys = {'uid', 'carrier', 'service_mode', 'rsrp', 'rsrq', 'sinr',
                   'rsrp_5g', 'rsrq_5g', 'sinr_5g', 'serving_band',
                   'serving_bandwidth_mhz', 'carrier_count',
                   'total_active_mhz', 'zero_mhz_count', 'carriers',
                   'carrier_evidence', 'context', 'ca_signature',
                   'cell_key_hash', 'metric_ts', 'collected_ts',
                   'timestamp_source'}
    check(legacy_keys <= set(rec), 'every v0.8.3 modem key is still present')
    check(rec['rsrp'] == -82.0 and rec['sinr_5g'] == 33.0,
          'serving values unchanged for valid readings')
    check(rec['ca_signature'] == 'LTE:66:66736:10.0|NR:N41:528144:80.0',
          'ca_signature composition unchanged')


def test_v084_speedtest_function_sync():
    print('test_v084_speedtest_function_sync')
    here = os.path.dirname(os.path.abspath(__file__))
    app = os.path.dirname(here)
    other = os.path.join(os.path.dirname(app), 'speedtest_analyzer',
                         'speedtest_web.py')
    if not os.path.isfile(other):
        check(True, 'speedtest_analyzer not present: sync check skipped')
        return

    def funcs(path):
        src = open(path).read()
        return {n.name: ast.dump(n) for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef)}
    a = funcs(other)
    b = funcs(os.path.join(app, 'analyzer', 'cellular.py'))
    names = ('_parse_band_name', '_parse_bandwidth_value',
             '_normalize_carrier', '_carrier_identity',
             '_carrier_role_rank', '_merge_carrier_records',
             '_dedupe_active_carriers', '_determine_service_mode',
             '_build_carrier_summary')
    for n in names:
        check(n in a and n in b and a[n] == b[n],
              'copied algorithm identical to Speedtest: {}'.format(n))


TESTS = [
    test_v084_rsrp_zero_measured_vs_unmeasured,
    test_v084_rsrq_minus3_context,
    test_v084_sinr_scale_from_field_family,
    test_v084_sinr_reference_conflict_and_weak_tier,
    test_v084_serving_validity_and_raw_preserved,
    test_v084_cell_id_canonicalization_and_hash,
    test_v084_role_aliases_and_dedupe_preserved,
    test_v084_lte_state_and_unregistered_sentinels,
    test_v084_modem_health,
    test_v084_legacy_record_shape_compatible,
    test_v084_speedtest_function_sync,
]
