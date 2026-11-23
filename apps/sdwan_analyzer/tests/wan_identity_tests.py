"""v1.0.2 shared WAN identity tests.

One identity layer (analyzer/wan_identity.py) feeds the snapshot, topology,
WBOND, steering annotation, history and the wan_identity API. Fixtures are
sanitized and synthetic (wan_identity_fixtures.py). Run through
run_tests.py.
"""
import copy
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer

import analytics_v084_tests as AV
import cellular_v084_fixtures as X
import fixtures
import wan_identity_fixtures as F
from analyzer import (VERSION, cellular, normalize, telemetry,
                      wan_identity as WI, web)
from analyzer.service import AnalyzerService

check = None            # bound by run_tests.main()
_free_port = None


def bind(check_fn, free_port_fn=None):
    global check, _free_port
    check = check_fn
    _free_port = free_port_fn


# --- helpers ---------------------------------------------------------------

def _inv(devices, host='R2400'):
    return WI.build_inventory(copy.deepcopy(devices), host)


def _one(device, uid='mdm-t0001', host='R2400'):
    inv = _inv({uid: device}, host)
    return inv['by_uid'][uid]


def _svc(devices=None, bonding='r2400', host='R2400', primary='ethernet-wan'):
    """AnalyzerService with cached raw, composed (no router I/O)."""
    svc = AnalyzerService()
    raw = fixtures.base_raw()
    raw['wan_devices'] = copy.deepcopy(
        F.r2400_devices() if devices is None else devices)
    raw['primary_device'] = primary
    svc._runtime_raw.data = raw
    heavy = fixtures.heavy_raw()
    if bonding == 'r2400':
        heavy['wan_bonding'] = F.r2400_bonding()
    elif bonding == 'fixture':
        heavy['wan_bonding'] = copy.deepcopy(fixtures.WAN_BONDING)
    if host:
        heavy['product_name'] = host
    svc._config_raw.data = heavy
    # Telemetry caches normally filled by a collection pass.
    svc._cur_interfaces, _ = telemetry.interface_records(
        raw['wan_devices'], {}, 1)
    svc._cur_cellular = telemetry.cellular_records(raw['wan_devices'], 1)
    svc._freshness['bonding'] = {'collected_ts': 1, 'sample_source': 't'}
    svc._freshness['wan'] = {'collected_ts': 1, 'sample_source': 't'}
    svc.recompose()
    return svc


def _by(items, key, val):
    return [i for i in items if i.get(key) == val][0]


# --- 1. R2400 with several modem modules and SIM slots ---------------------

def test_r2400_multi_module_inventory():
    print('test_r2400_multi_module_inventory')
    devices = F.r2400_devices()
    before = copy.deepcopy(devices)
    inv = WI.build_inventory(devices, 'R2400')
    check(devices == before, 'input devices are never mutated')
    s = inv['summary']
    check((s['raw_physical'], s['empty_sim'], s['displayable_physical'],
           s['connected'], s['inactive']) == (7, 3, 4, 3, 1),
          'raw 7 / empty 3 / displayable 4 / connected 3 / inactive 1: '
          '{}'.format(s))
    check(s['overlays'] == 4 and s['wbond'] == 1,
          'overlay and WBOND objects counted apart from physical')
    check(inv['order'] == list(devices) and set(inv['by_uid']) == set(devices),
          'raw inventory keeps every NCOS object, in order')
    names = {u: r['display_name'] for u, r in inv['by_uid'].items()}
    expect = {
        'ethernet-wan': 'Ethernet WAN',
        'mdm-r24a1': 'R2400 Internal - ATT-SIM1',
        'mdm-rc01': 'RC1250 Captive - VZW-SIM1',
        'mdm-w185b': 'W1855 Captive - TMO-SIM2',
        'mdm-r24a2': 'R2400 Internal - SIM2',
        'mdm-rc02': 'RC1250 Captive - SIM2',
        'mdm-w185a': 'W1855 Captive - SIM1',
        'sdwan-hub0_rc01': 'SD-WAN Tunnel - RC1250 Captive - VZW-SIM1',
    }
    for uid, name in expect.items():
        check(names[uid] == name, '{} -> {!r} (got {!r})'.format(
            uid, name, names[uid]))
    for uid, r in inv['by_uid'].items():
        info = devices[uid]['info']
        check(r['uid'] == uid and r['iface'] == info['iface'],
              'raw uid/iface preserved for {}'.format(uid))
    vis = sorted(u for u, r in inv['by_uid'].items()
                 if r['inventory_visible'])
    check(vis == ['ethernet-wan', 'mdm-r24a1', 'mdm-rc01', 'mdm-w185b'],
          'displayable physical inventory: {}'.format(vis))
    check(inv['host_model'] == 'R2400' and inv['host_family'] == 'R2400',
          'host model and family')


# --- 2. explicit NOSIM -------------------------------------------------------

def test_explicit_empty_sim_indicators():
    print('test_explicit_empty_sim_indicators')
    variants = {
        'diagnostics.NOSIM': dict(nosim=True),
        'diagnostics.PIN_STATUS': dict(pin='NOSIM'),
        'status.error_text': dict(error_text=F.NOSIM_ERROR),
        'all three': dict(nosim=True, pin='NOSIM', error_text=F.NOSIM_ERROR),
    }
    for label, kw in variants.items():
        r = _one(F.mdm('mdm-t0001', 'sim2', state='disconnected',
                       iccid=False, **kw))
        check(r['sim_presence'] == 'empty' and
              r['classification'] == 'empty_sim' and
              r['inventory_visible'] is False and r['kind'] == 'cellular',
              '{} alone is authoritative'.format(label))
        check(r['object_class'] == 'physical' and r['carrier'] is None,
              '{}: still a physical object, no carrier on an empty '
              'slot'.format(label))
    r = _one(F.mdm('mdm-t0001', 'sim2', nosim=True, pin='NOSIM',
                   error_text=F.NOSIM_ERROR, state='disconnected'))
    check(set(r['classification_evidence']) >= {
        'diag_nosim_true', 'diag_pin_status_nosim',
        'status_error_text_nosim'}, 'evidence codes list every indicator')
    check(r['display_name'] == 'R2400 Internal - SIM2',
          'empty slot name keeps the SIM slot, no carrier')


# --- 3/4. populated SIMs that are not connected ---------------------------

def test_populated_sim_standby_and_failed():
    print('test_populated_sim_standby_and_failed')
    r = _one(F.mdm('mdm-t0001', 'sim1', state='standby', home='Verizon'))
    check(r['sim_presence'] == 'present' and
          r['operational_state'] == 'inactive' and
          r['inventory_visible'] and r['classification'] == 'physical',
          'standby populated SIM: present, inactive, displayable')
    check(r['carrier'] == 'VZW' and r['carrier_source'] == 'home' and
          r['display_name'] == 'R2400 Internal - VZW-SIM1',
          'home carrier used when no registered carrier')
    r = _one(F.mdm('mdm-t0001', 'sim2', state='disconnected',
                   carrid='T-Mobile', error_text='APN authentication failed'))
    check(r['sim_presence'] == 'present' and r['inventory_visible'] and
          r['operational_state'] == 'inactive' and
          r['failure_reason'] == 'APN authentication failed' and
          r['carrier_source'] == 'registered',
          'failed connection keeps SIM presence and the failure reason')
    # Operational state is independent of SIM presence.
    up = _one(F.mdm('mdm-t0001', 'sim2', carrid='T-Mobile'))
    check(up['operational_state'] == 'connected' and
          up['failure_reason'] is None, 'connected has no failure reason')


def test_inactive_is_not_empty():
    print('test_inactive_is_not_empty')
    cases = {
        'disconnected, no IP': F.mdm('mdm-t0001', 'sim1',
                                     state='disconnected', iccid=False),
        'standby, no carrier': F.mdm('mdm-t0001', 'sim1', state='standby',
                                     iccid=False),
        'not registered': F.mdm('mdm-t0001', 'sim1', state='connecting',
                                iccid=False, diag={'EMMSTATE': 'Searching'}),
        'config failing': F.mdm('mdm-t0001', 'sim1', state='disconnected',
                                iccid=False,
                                error_text='Configuration invalid'),
        'zero throughput': F.mdm('mdm-t0001', 'sim1', iccid=False,
                                 diag={'SERDIS': 'NOSIM'}),
    }
    cases['zero throughput']['stats'] = {'rx_bytes': 0, 'tx_bytes': 0}
    disabled = F.mdm('mdm-t0001', 'sim1', state='disconnected', iccid=False)
    disabled['config']['enabled'] = False
    cases['interface disabled'] = disabled
    for label, dev in cases.items():
        r = _one(dev)
        check(r['sim_presence'] == 'unknown' and
              r['classification'] == 'physical' and r['inventory_visible'],
              '{}: not evidence of an empty SIM (unknown, retained)'.format(
                  label))


# --- 5. E400 eSIM ------------------------------------------------------------

def test_e400_att_esim_failed_registration():
    print('test_e400_att_esim_failed_registration')
    r = _one(F.e400_esim_device(), 'mdm-e400e', 'E400')
    check(r['sim_type'] == 'esim' and r['sim_presence'] == 'present' and
          r['carrier'] == 'ATT' and r['carrier_name'] == 'AT&T',
          'AT&T eSIM identified: {}'.format(
              (r['sim_type'], r['sim_presence'], r['carrier'])))
    check(r['display_name'] == 'E400 Internal - ATT-eSIM',
          'name: {}'.format(r['display_name']))
    check(r['operational_state'] == 'inactive' and
          r['failure_reason'] == 'CPPM failed: no carrier',
          'inactive with the failure reason preserved')
    check(r['classification'] == 'physical' and r['inventory_visible'] and
          r['sim_presence'] != 'empty',
          'retained in the physical inventory, not an empty SIM slot')
    check(r['modem_owner'] == 'internal' and r['host_model'] == 'E400',
          'internal modem on an E400 host')
    # A failed registration alone never implies a missing SIM.
    dev = F.e400_esim_device()
    dev['diagnostics'].pop('HOMECARRID')
    r2 = _one(dev, 'mdm-e400e', 'E400')
    check(r2['sim_presence'] != 'empty' and r2['inventory_visible'],
          'no carrier information still does not make it empty')


# --- 6/7/8/9. modem ownership ---------------------------------------------

def test_internal_captive_flag_means_internal():
    print('test_internal_captive_flag_means_internal')
    inv = _inv(F.r2400_devices())
    r = inv['by_uid']['mdm-r24a1']
    check(r['modem_owner'] == 'internal' and r['modem_model'] == 'R2400SDX',
          'internal_captive=true: internal modem, reported model kept')
    check('internal_captive_true' in r['classification_evidence'],
          'evidence cites internal_captive')
    # Same uid shape, different evidence: the flag, not the prefix, decides.
    dev = F.mdm('mdm-r24a1', 'sim1', carrid='ATT',
                remote=F.captive_remote('RC1250'))
    check(_one(dev, 'mdm-r24a1')['modem_owner'] == 'captive',
          'internal_captive=false: captive, same uid prefix')
    # No host model: ownership still resolved, the unsupported owner prefix
    # is omitted instead of guessed.
    r = _one(copy.deepcopy(F.r2400_devices()['mdm-r24a1']), 'mdm-r24a1',
             host=None)
    check(r['modem_owner'] == 'internal' and
          r['display_name'] == 'ATT-SIM1' and r['host_model'] is None,
          'unknown host: no invented owner label')
    # Contradiction: flag wins (explicit), but the conflict is recorded.
    dev = F.mdm('mdm-r24a1', 'sim1', carrid='ATT', remote={
        'internal_captive': True, 'product_name': 'RC1250'})
    r = _one(dev, 'mdm-r24a1')
    check(r['modem_owner'] == 'internal' and
          'remote_product_matches_captive_catalog' in
          r['classification_evidence'],
          'internal flag wins; catalog match noted as evidence')
    # No remote block at all: the host router's own modem.
    r = _one(F.mdm('mdm-t0001', 'sim1', carrid='Verizon'))
    check(r['modem_owner'] == 'internal' and
          r['display_name'] == 'R2400 Internal - VZW-SIM1' and
          'no_remote_block' in r['classification_evidence'],
          'no remote block: internal')
    # Standalone router (no controller): no ' Internal' suffix (STA).
    r = _one(F.mdm('mdm-t0001', 'sim1', carrid='T-Mobile'), host='W1850')
    check(r['display_name'] == 'W1850 - TMO-SIM1',
          'standalone host: {}'.format(r['display_name']))


def test_captive_modem_models():
    print('test_captive_modem_models')
    r = _inv(F.r2400_devices())['by_uid']
    check(r['mdm-rc01']['modem_owner'] == 'captive' and
          r['mdm-rc01']['modem_model'] == 'RC1250' and
          r['mdm-rc01']['display_name'] == 'RC1250 Captive - VZW-SIM1',
          'RC1250 captive modem')
    check(r['mdm-w185b']['modem_owner'] == 'captive' and
          r['mdm-w185b']['modem_model'] == 'W1855' and
          r['mdm-w185b']['display_name'] == 'W1855 Captive - TMO-SIM2',
          'W1855 captive modem')
    # Variant model strings resolve to the catalog family (longest token).
    dev = F.mdm('mdm-t0001', 'sim1', carrid='ATT',
                remote=F.captive_remote('W1855-5GB'))
    check(_one(dev)['modem_model'] == 'W1855', 'W1855-5GB -> W1855')


def test_unknown_captive_modem():
    print('test_unknown_captive_modem')
    r = _one(F.mdm('mdm-t0001', 'sim1', carrid='ATT',
                   remote=F.captive_remote('ZX9000')))
    check(r['modem_owner'] == 'captive' and r['modem_model'] == 'ZX9000' and
          r['display_name'] == 'ZX9000 Captive - ATT-SIM1' and
          'remote_product_unlisted' in r['classification_evidence'],
          'unlisted model taken verbatim from the device')
    r = _one(F.mdm('mdm-t0001', 'sim1', carrid='ATT',
                   remote={'internal_captive': False, 'mac0': 'x'}))
    check(r['modem_owner'] == 'captive' and r['modem_model'] is None and
          r['display_name'] == 'Captive Modem - ATT-SIM1' and
          'remote_model_unknown' in r['classification_evidence'],
          'no model reported: nothing invented')
    r = _one(F.mdm('mdm-t0001', 'sim1', carrid='ATT',
                   remote={'internal_captive': False, 'product_name': '',
                           'description': 'attached CBA850 router'}))
    check(r['modem_model'] == 'CBA850', 'catalog match elsewhere in remote')
    r = _one(F.mdm('mdm-t0001', 'sim1', carrid='ATT', remote='malformed'))
    check(r['modem_owner'] == 'unknown' and
          'remote_block_malformed' in r['classification_evidence'],
          'malformed remote block: unknown ownership')


def test_unknown_carrier():
    print('test_unknown_carrier')
    r = _one(F.mdm('mdm-t0001', 'sim1', carrid='Mint Mobile'))
    check(r['carrier'] is None and r['carrier_name'] == 'Mint Mobile' and
          r['display_name'] == 'R2400 Internal - SIM1',
          'MVNO: no abbreviation, SIM slot remains: {}'.format(
              r['display_name']))
    r = _one(F.mdm('mdm-t0001', None, carrid='FirstNet'))
    check(r['carrier'] == 'ATT' and r['sim_slot'] is None and
          r['display_name'] == 'R2400 Internal - ATT',
          'carrier without a verified slot: slot omitted, carrier kept')
    r = _one(F.mdm('mdm-t0001', None, iccid=True, diag={'SERDIS': '5G'}))
    check(r['carrier'] is None and r['sim_slot'] is None and
          r['display_name'] == 'R2400 Internal - Cellular WAN',
          'nothing verified: STA generic label')
    for text, abbr in (('T-Mobile USA', 'TMO'), ('Verizon', 'VZW'),
                       ('ATT', 'ATT'), ('AT&T', 'ATT'), ('TMobile', 'TMO'),
                       ('Mint Mobile', ''), ('Verizon Wireless', 'VZW'),
                       ('', ''), ('Unknown', '')):
        check(WI.carrier_abbreviation(text) == abbr,
              'carrier_abbreviation({!r})'.format(text))
    check([WI.sim_slot_label(v) for v in ('sim1', 'SIM 2', '2', '', 'x3')] ==
          ['SIM1', 'SIM2', 'SIM2', '', ''], 'sim_slot_label')


# --- 11/12. Ethernet and satellite ---------------------------------------

def test_ethernet_and_satellite():
    print('test_ethernet_and_satellite')
    up = _one(F.ethernet(), 'ethernet-wan')
    down = _one(F.ethernet('disconnected'), 'ethernet-wan')
    check(up['display_name'] == down['display_name'] == 'Ethernet WAN' and
          up['kind'] == 'ethernet' and up['sim_presence'] ==
          'not_applicable' and up['modem_owner'] is None,
          'Ethernet WAN naming; no SIM/modem fields')
    check(up['operational_state'] == 'connected' and
          down['operational_state'] == 'inactive' and
          down['inventory_visible'] and down['classification'] == 'physical',
          'disconnected Ethernet stays in the inventory as inactive')
    sat = {'info': {'type': 'mdm', 'iface': 'rmnet_sat', 'uid': 'mdm-5a7e1c90',
                    'product': 'Starlink'},
           'status': {'connection_state': 'connected'},
           'diagnostics': {'NOSIM': 'TRUE', 'CARRID': 'Verizon'}}
    r = _one(sat, 'mdm-5a7e1c90')
    check(r['kind'] == 'satellite' and
          r['display_name'] == 'Satellite WAN-1C90' and
          r['sim_presence'] == 'not_applicable' and
          r['classification'] == 'physical' and r['inventory_visible'] and
          r['modem_owner'] is None,
          'satellite with an mdm-* uid is never treated as a SIM slot')
    bare = {'info': {'type': 'mdm', 'iface': 'x', 'uid': 'mdm-abc'},
            'status': {'connection_state': 'disconnected'},
            'diagnostics': {'CARRID': ''}}
    r = _one(bare, 'mdm-abc')
    check(r['kind'] == 'unknown' and r['inventory_visible'] and
          r['sim_presence'] == 'unknown' and r['modem_owner'] is None,
          'an mdm-* uid alone is not proof of cellular (v1.0.1 behavior)')


# --- 13/14. relationships ---------------------------------------------------

def test_empty_sim_in_topology_relationships():
    print('test_empty_sim_in_topology_relationships')
    svc = _svc()
    topo = svc.query_topology()
    phys = sorted(p['uid'] for p in topo['physical'])
    check(phys == ['ethernet-wan', 'mdm-r24a1', 'mdm-rc01', 'mdm-w185b'],
          'topology.physical is the displayable inventory: {}'.format(phys))
    hidden = sorted(p['uid'] for p in topo['hidden_physical'])
    check(hidden == ['mdm-r24a2', 'mdm-rc02', 'mdm-w185a'],
          'empty SIM objects stay accessible in hidden_physical')
    inv = topo['inventory']
    check((inv['raw_physical'], inv['empty_sim'], inv['displayable_physical'],
           inv['connected'], inv['inactive']) == (7, 3, 4, 3, 1),
          'topology inventory counts')
    bond = topo['bonds'][0]
    m = _by(bond['members'], 'tunnel_iface', 'gres-hub0_r24a2')
    check(m['physical']['state'] == 'resolved' and
          m['physical']['uid'] == 'mdm-r24a2' and
          m['physical']['available'] is False and
          m['physical']['unavailable_reason'] == 'empty_sim' and
          m['physical']['display_name'] == 'R2400 Internal - SIM2',
          'bond member on an empty SIM is kept and flagged unavailable')
    ov = _by(topo['overlays'], 'uid', 'sdwan-hub0_r24a2')
    check(ov['underlay']['state'] == 'resolved' and
          ov['underlay']['available'] is False and
          ov['identity']['underlay_available'] is False,
          'overlay underlay on an empty SIM is kept and flagged')
    kinds = sorted((u['kind'], u['target_uid'])
                   for u in topo['unavailable_references'])
    check(kinds == [('bond_member_physical', 'mdm-r24a2'),
                    ('overlay_underlay', 'mdm-r24a2')],
          'unavailable_references list: {}'.format(kinds))
    check(topo['unresolved'] == [], 'nothing unresolved')
    wb = svc.query_wbond()
    wm = _by(wb['bonds'][0]['members'], 'tunnel_iface', 'gres-hub0_r24a2')
    check(len(wb['bonds'][0]['members']) == 4 and
          wm['physical_display_name'] == 'R2400 Internal - SIM2' and
          wb['bonds'][0]['observed']['resolved_member_count'] == 4,
          'WBOND analytics keep the empty-SIM member and its name')
    check([u['target_uid'] for u in wb['unavailable_references']] ==
          ['mdm-r24a2'], 'WBOND view flags the unavailable member')
    bm = _by(svc.snapshot()['bonding']['bonds'][0]['members'],
             'tunnel_iface', 'gres-hub0_r24a2')
    check(bm['identity']['available'] is False and
          bm['display_name'] == 'R2400 Internal - SIM2',
          'snapshot bonding member carries the shared identity')


def test_inactive_wan_in_wbond():
    print('test_inactive_wan_in_wbond')
    svc = _svc()
    topo = svc.query_topology()
    m = _by(topo['bonds'][0]['members'], 'tunnel_iface', 'gres-hub0_w185')
    check(m['physical']['uid'] == 'mdm-w185b' and
          m['physical']['available'] is True and
          m['physical']['operational_state'] == 'inactive',
          'inactive (standby) bond member is a resolved, available WAN')
    p = _by(topo['physical'], 'uid', 'mdm-w185b')
    check(p['identity']['sim_presence'] == 'present' and
          p['observed']['operational_state'] == 'inactive' and
          p['bond_memberships'] == ['gres_wb0-hub0'],
          'inactive WAN stays in the inventory with its bond membership')
    wm = _by(svc.query_wbond()['bonds'][0]['members'], 'tunnel_iface',
             'gres-hub0_w185')
    check(wm['physical_display_name'] == 'W1855 Captive - TMO-SIM2',
          'bond member uses the shared friendly name')


# --- 15/16. overlays -------------------------------------------------------

def test_overlay_underlay_resolution():
    print('test_overlay_underlay_resolution')
    inv = _inv(F.r2400_devices())['by_uid']
    o = inv['sdwan-hub0_wan']
    check(o['display_name'] == 'SD-WAN Tunnel - Ethernet WAN' and
          o['underlay_uid'] == 'ethernet-wan' and
          o['underlay_state'] == 'resolved' and
          o['underlay_available'] is True and o['kind'] == 'sdwan' and
          o['sim_presence'] == 'not_applicable',
          'overlay named from its resolved physical underlay')
    o = inv['sdwan-hub0_r24a2']
    check(o['underlay_state'] == 'resolved' and
          o['underlay_available'] is False,
          'resolved underlay that is an empty SIM is flagged unavailable')


def test_unresolved_overlay_underlay():
    print('test_unresolved_overlay_underlay')
    devs = {
        'ethernet-wan': F.ethernet(),
        'wbond-wbond0_hub0': F.r2400_devices()['wbond-wbond0_hub0'],
        'sdwan-none': F.overlay('sdwan-none', 'gres-none', []),
        'sdwan-missing': F.overlay('sdwan-missing', 'gres-m', ['mdm-gone'],
                                   'mdm-gone'),
        'sdwan-bond': F.overlay('sdwan-bond', 'gres-b',
                                ['wbond-wbond0_hub0'], 'wbond-wbond0_hub0'),
    }
    inv = _inv(devs)['by_uid']
    for uid, why in (('sdwan-none', 'underlay_not_reported'),
                     ('sdwan-missing', 'underlay_unresolved'),
                     ('sdwan-bond', 'underlay_unresolved')):
        r = inv[uid]
        check(r['underlay_state'] == 'unresolved' and
              r['display_name'] == 'SD-WAN Tunnel' and
              r['underlay_available'] is None and
              why in r['classification_evidence'],
              '{}: unresolved, generic name, {}'.format(uid, why))
    check(inv['sdwan-missing']['underlay_uid'] == 'mdm-gone',
          'the reported dependent uid is still carried')
    check(WI.friendly_name_ex('sdwan-x', devs['sdwan-none'], devs, '') ==
          'SD-WAN Tunnel', 'friendly_name_ex without underlay')
    wb = {'info': {'type': 'wbond', 'iface': 'gres_wb0-hub0'}}
    check(WI.friendly_name_ex('wbond-wbond0_hub0', wb, {}, '', 'Hub0') ==
          'Bonded WAN (Hub0)' and
          WI.friendly_name_ex('wbond-wbond0_hub0', wb) == 'Bonded WAN',
          'WBOND naming keeps the configured bond name')


# --- 17. traffic steering ---------------------------------------------------

def test_steering_references_use_shared_identity():
    print('test_steering_references_use_shared_identity')
    svc = _svc(devices=fixtures.WAN_DEVICES, bonding='fixture', host='R1900')
    snap = svc.snapshot()
    paths = {p['uid']: p for p in snap['wan']['paths']}
    rules = snap['traffic_steering']['logical_rules']
    hit = [r for r in rules if 'sdwan-hub0_1ea0ac' in r['steering_to']]
    check(hit, 'fixture has a rule steering to an overlay')
    r = hit[0]
    ent = _by(r['steering_to_identity'], 'ref', 'sdwan-hub0_1ea0ac')
    check('sdwan-hub0_1ea0ac' in r['steering_to'] and
          ent['uid'] == 'sdwan-hub0_1ea0ac' and ent['resolved'] and
          ent['display_name'] == paths['sdwan-hub0_1ea0ac']['display_name'],
          'raw selector kept; presentation identity added alongside')
    ev = [e for e in snap['traffic_steering']['events']
          if e.get('wan_to_uid') == 'wbond-wbond0_hub0'][0]
    check(ev['wan_to_uid'] == 'wbond-wbond0_hub0' and
          ev['wan_to_identity']['display_name'] == 'Bonded WAN' and
          ev['wan_from_identity'] is None,
          'event WAN reference resolved; raw uid untouched')
    by_uid = _inv(F.r2400_devices())['by_uid']
    lr = [{'steering_to': ['mdm-gone', 'rmnet_r24a'], 'primary_wandev':
           'gres-hub0_wan'}]
    ev = [{'wan_to_uid': 'mdm-rc01', 'wan_from': 'free text'}]
    WI.annotate_steering_refs(lr, ev, by_uid)
    check(lr[0]['steering_to_identity'][0]['resolved'] is False and
          lr[0]['steering_to'] == ['mdm-gone', 'rmnet_r24a'],
          'unknown selector stays unresolved and unchanged')
    check(ev[0]['wan_to_identity']['display_name'] ==
          'RC1250 Captive - VZW-SIM1' and
          ev[0]['wan_from_identity']['resolved'] is False,
          'event destination resolved by uid; free text never guessed')
    dup = {'a': {'uid': 'a', 'iface': 'i0', 'display_name': 'A'},
           'b': {'uid': 'b', 'iface': 'i0', 'display_name': 'B'},
           'c': {'uid': 'c', 'iface': 'i1', 'display_name': 'C'}}
    check(WI.resolve_wan_ref('i0', dup) is None and
          WI.resolve_wan_ref('i1', dup)['uid'] == 'c',
          'iface reference resolves only when unique')


# --- 18. historical continuity ---------------------------------------------

def test_history_identity_continuity():
    print('test_history_identity_continuity')
    svc = _svc()
    store = svc._config_store
    snap = svc.snapshot()
    out = svc._update_config_versions(snap, 1000)
    check(out['wan_identity']['version'] == 1, 'first identity version')
    # A link flap (volatile state only) must not create a version.
    devs = svc._runtime_raw.data['wan_devices']
    devs['mdm-r24a1']['status']['connection_state'] = 'disconnected'
    devs['mdm-r24a1']['status']['error_text'] = 'No signal'
    svc.recompose()
    out = svc._update_config_versions(svc.snapshot(), 1100)
    check(out['wan_identity']['version'] == 1 and
          out['wan_identity']['changed'] is False,
          'operational state changes never version the identity')
    # Carrier swap: names change, uid/iface (the join keys) do not.
    devs['mdm-r24a1']['diagnostics']['CARRID'] = 'T-Mobile'
    devs['mdm-r24a1']['stats'] = {'rx_bytes': 10, 'tx_bytes': 5}
    svc.recompose()
    snap2 = svc.snapshot()
    out = svc._update_config_versions(snap2, 1200)
    check(out['wan_identity']['version'] == 2 and
          out['wan_identity']['changed'] is True, 'carrier change: version 2')
    v = {x['version']: x for x in store.versions('wan_identity')}
    a = v[1]['payload']['wans']['mdm-r24a1']
    b = v[2]['payload']['wans']['mdm-r24a1']
    check(a['display_name'] == 'R2400 Internal - ATT-SIM1' and
          b['display_name'] == 'R2400 Internal - TMO-SIM1' and
          a['carrier'] == 'ATT' and b['carrier'] == 'TMO',
          'both identities retained, keyed by the same uid')
    check(set(v[1]['payload']['wans']) == set(v[2]['payload']['wans']),
          'same WAN uids in both versions')
    raw = svc._raw().get('wan_devices')
    recs = telemetry.interface_records(raw, {}, 1300)[0]
    cell = telemetry.cellular_records(raw, 1300)
    blob = json.dumps([recs, cell])
    check('display_name' not in blob and 'TMO-SIM1' not in blob and
          any(r['uid'] == 'mdm-r24a1' for r in recs),
          'persisted telemetry carries stable uids, never display names')
    # Stale gate: a pass that did not read wan_devices does not version.
    out = svc._update_config_versions(svc.snapshot(), 1400, ok_keys=set())
    check(out['wan_identity']['attribution'] == 'latest_known' and
          out['wan_identity']['changed'] is False,
          'identity is only versioned from a fresh read')
    # Window analysis: the current identity is never presented as history.
    win = WI.IdentityWindow()
    for ts, ver in ((1000, 1), (1100, 1), (1200, 2)):
        win.add({'collected_ts': ts,
                 'config_versions': {'wan_identity': {'version': ver}}})
    cur = _by(snap2['wan']['paths'], 'uid', 'mdm-r24a1')
    blk = WI.history_block('mdm-r24a1', cur, win, store.versions(
        'wan_identity'))
    check(blk['basis'] == 'versioned' and blk['changed_in_window'] is True and
          [e['version'] for e in blk['versions_in_window']] == [1, 2] and
          blk['versions_in_window'][0]['identity']['carrier'] == 'ATT' and
          blk['current']['display_name'] == 'R2400 Internal - TMO-SIM1',
          'window lists both identities; current is labelled current')
    stable = WI.history_block('ethernet-wan', _by(
        snap2['wan']['paths'], 'uid', 'ethernet-wan'), win,
        store.versions('wan_identity'))
    check(stable['changed_in_window'] is False,
          'unchanged WAN: no change reported')
    old = WI.IdentityWindow()
    old.add({'collected_ts': 5, 'config_versions': {}})
    blk = WI.history_block('mdm-r24a1', cur, old, store.versions(
        'wan_identity'))
    check(blk['basis'] == 'current_only' and
          blk['observations_without_identity_version'] == 1 and
          blk['versions_in_window'] == [],
          'pre-1.0.2 history: only the current identity, labelled so')


def test_history_apis_carry_identity_blocks():
    print('test_history_apis_carry_identity_blocks')
    now = int(time.time())
    svc = _svc()
    svc._update_config_versions(svc.snapshot(), now - 600)          # v1 ATT
    devs = svc._runtime_raw.data['wan_devices']
    devs['mdm-r24a1']['diagnostics']['CARRID'] = 'T-Mobile'
    svc.recompose()
    svc._update_config_versions(svc.snapshot(), now - 120)          # v2 TMO
    for i in range(6):
        ts = now - 600 + i * 100
        ver = 1 if i < 3 else 2
        svc._store.append({
            'collected_ts': ts, 'timestamp': ts, 'sample_source': 'scheduled',
            'collector_errors': [],
            'config_versions': {'wan_identity': {'version': ver}},
            'cellular': [cellular.normalize_cellular(
                'mdm-r24a1', dict(X.ATT_NSA, CELL_ID='111'), ts, ts,
                'collector', info={'sim': 'sim1'})],
            'interfaces': [AV._iface('mdm-r24a1', 'physical', i * 1000,
                                     i * 500, ts)]})
    cell = svc.query_cellular_analysis('')
    blk = _by(cell['modems'], 'uid', 'mdm-r24a1')['identity']
    check(blk['basis'] == 'versioned' and blk['changed_in_window'] is True and
          [v['identity']['carrier'] for v in blk['versions_in_window']] ==
          ['ATT', 'TMO'] and
          blk['current']['display_name'] == 'R2400 Internal - TMO-SIM1',
          'cellular analytics: identity as of the window, current labelled')
    sd = svc.query_sdwan_history('')
    row = _by(sd['interfaces']['by_interface'], 'uid', 'mdm-r24a1')
    check(row['identity']['changed_in_window'] is True and
          row['identity']['current']['uid'] == 'mdm-r24a1',
          'sdwan history interfaces carry identity')
    only = svc.query_cellular_analysis('uid=mdm-r24a1&start=%d&end=%d' % (
        now - 600, now - 400))
    blk = _by(only['modems'], 'uid', 'mdm-r24a1')['identity']
    check(blk['changed_in_window'] is False and
          [v['version'] for v in blk['versions_in_window']] == [1],
          'a window before the change reports only the earlier identity')
    json_blob = json.dumps([cell, sd])
    for secret in F.SENSITIVE:
        check(secret not in json_blob, 'history responses leak no identifier')


# --- 19. contradictions ----------------------------------------------------

def test_contradictory_sim_indicators():
    print('test_contradictory_sim_indicators')
    cases = {
        'NOSIM with ICCID': F.mdm('mdm-t0001', 'sim1', nosim=True,
                                  diag={'ICCID': F.SENSITIVE[2]}),
        'NOSIM with registered carrier': F.mdm(
            'mdm-t0001', 'sim1', nosim=True, carrid='Verizon'),
        'error_text NOSIM with PIN READY': F.mdm(
            'mdm-t0001', 'sim1', error_text=F.NOSIM_ERROR, pin='READY'),
        'NOSIM true and false signals': F.mdm(
            'mdm-t0001', 'sim1', pin='NOSIM', diag={'NOSIM': 'FALSE'}),
    }
    for label, dev in cases.items():
        r = _one(dev)
        check(r['sim_presence'] == 'unknown' and
              r['classification'] == 'physical' and r['inventory_visible'] and
              'contradictory_sim_indicators' in r['classification_evidence'],
              '{}: unknown and retained'.format(label))
    esim = F.e400_esim_device()
    esim['diagnostics']['NOSIM'] = 'TRUE'
    r = _one(esim, 'mdm-e400e', 'E400')
    check(r['sim_presence'] == 'unknown' and r['inventory_visible'],
          'eSIM profile plus NOSIM: unknown, retained')


# --- 20. malformed data ------------------------------------------------------

def test_missing_and_malformed_data():
    print('test_missing_and_malformed_data')
    odd = {
        'a': {'info': {'type': 'mdm', 'sim': 'sim1', 'iface': 'r'},
              'status': ['x'], 'diagnostics': 'text'},
        'mdm-b': {'info': 'oops', 'status': None, 'diagnostics': []},
        'mdm-c': {'info': {'type': 'mdm', 'remote': ['x']},
                  'diagnostics': {'CARRID': 5, 'NOSIM': None,
                                  'EUICC_PROF1': 7}},
        'mdm-d': {},
        'mdm-e': {'info': {'type': 'mdm', 'sim': 'sim2'},
                  'status': {'connection_state': None,
                             'error_text': 12345678901234},
                  'diagnostics': {'CARRID': '\x00\x1b[31m--'}},
        'bad': 'not a dict',
        'none': None,
    }
    inv = WI.build_inventory(odd, None)
    check(set(inv['by_uid']) == {'a', 'mdm-b', 'mdm-c', 'mdm-d', 'mdm-e'},
          'non-dict devices skipped, the rest identified')
    for uid, r in inv['by_uid'].items():
        check(isinstance(r['display_name'], str) and r['display_name'] and
              r['sim_presence'] in ('present', 'empty', 'unknown',
                                    'not_applicable') and
              r['operational_state'] in ('connected', 'inactive', 'unknown'),
              'well-formed record for malformed input {}'.format(uid))
    check(inv['by_uid']['mdm-e']['failure_reason'] is None or
          '12345678901234' not in inv['by_uid']['mdm-e']['failure_reason'],
          'long digit runs never leave in a failure reason')
    for bad in (None, [], 'x', 5):
        check(WI.build_inventory(bad)['by_uid'] == {},
              'inventory of {!r} is empty, not an error'.format(bad))
    check(normalize.wan_paths(None, None) == [] and
          normalize.wan_paths({}, None) == [], 'wan_paths tolerates empty')
    check(WI.public_view(None) is None, 'public_view(None)')
    svc = _svc(devices=odd, bonding=None)
    check(svc.snapshot()['wan']['inventory']['raw_physical'] >= 0,
          'service composes a snapshot from malformed devices')


# --- catalog / host model ---------------------------------------------------

def test_catalog_and_host_model():
    print('test_catalog_and_host_model')
    cat = WI.get_catalog()
    check(cat is WI.get_catalog() and cat.error is None and
          'R2400' in cat.controllers and 'RC1250' in cat.captive_models and
          'E400' in cat.controllers and 'R1900' in cat.controllers,
          'catalog loaded once, with R2400 / R1900 / E400 and RC1250')
    check(WI.match_model('R2400-5GB', cat.families) == 'R2400' and
          WI.match_model('R24000', cat.families) == '' and
          WI.match_model('E3000-5GB', cat.families) == 'E3000',
          'token based model matching')
    for host, name in (('R1900', 'R1900 Internal - ATT-SIM1'),
                       ('E400', 'E400 Internal - ATT-SIM1'),
                       ('R2400-5GB', 'R2400 Internal - ATT-SIM1'),
                       ('AER2200', 'AER2200 - ATT-SIM1')):
        r = _one(F.mdm('mdm-t0001', 'sim1', carrid='ATT'), host=host)
        check(r['display_name'] == name, '{} -> {}'.format(
            host, r['display_name']))
    # A broken catalog degrades: models just are not recognised.
    WI.set_catalog(WI.Catalog(error='test'))
    try:
        r = _one(F.mdm('mdm-t0001', 'sim1', carrid='ATT',
                       remote=F.captive_remote('RC1250')))
        check(r['modem_owner'] == 'captive' and r['modem_model'] == 'RC1250'
              and r['display_name'] == 'RC1250 Captive - ATT-SIM1',
              'without a catalog the device-reported model is used')
        r = _one(F.mdm('mdm-t0001', 'sim1', carrid='ATT'))
        check(r['display_name'] == 'R2400 - ATT-SIM1',
              'without a catalog no Internal suffix: {}'.format(
                  r['display_name']))
    finally:
        WI.set_catalog(None)
    bad = WI._load_catalog(os.path.join(os.path.dirname(__file__), 'nope'))
    check(bad.error and not bad.controllers, 'missing catalog file tolerated')


# --- snapshot / API / privacy -----------------------------------------------

def test_snapshot_summary_and_api():
    print('test_snapshot_summary_and_api')
    svc = _svc()
    snap = svc.snapshot()
    ss = snap['system_summary']
    check(ss['physical_count'] == 4 and ss['physical_raw_count'] == 7 and
          ss['empty_sim_count'] == 3 and ss['physical_connected_count'] == 3
          and ss['physical_inactive_count'] == 1 and
          ss['wan_object_count'] == 12 and ss['wan_path_count'] == 9,
          'system_summary counts: {}'.format(ss))
    wan = snap['wan']
    check(len(wan['paths']) == 12 and len(wan['physical']) == 4 and
          sorted(wan['empty_sim_uids']) == ['mdm-r24a2', 'mdm-rc02',
                                            'mdm-w185a'],
          'paths = raw inventory; physical = displayable; empty uids listed')
    modem = _by(snap['cellular']['modems'], 'uid', 'mdm-r24a1')
    check(modem['identity']['display_name'] == 'R2400 Internal - ATT-SIM1' and
          modem['identity']['modem_owner'] == 'internal',
          'cellular modems carry the shared identity')
    path = _by(wan['paths'], 'uid', 'mdm-rc01')
    for key in ('classification', 'sim_presence', 'sim_type', 'sim_slot',
                'carrier', 'modem_owner', 'modem_model', 'host_model',
                'inventory_visible', 'operational_state',
                'classification_evidence'):
        check(key in path, 'wan path exposes {}'.format(key))
    ident = svc.query_wan_identity()
    check(ident['inventory']['empty_sim'] == 3 and
          len(ident['wans']) == 12 and
          sorted(ident['displayable_physical_uids']) == [
              'ethernet-wan', 'mdm-r24a1', 'mdm-rc01', 'mdm-w185b'],
          'wan_identity API: raw list + displayable split')
    check(AnalyzerService().query_wan_identity() is None,
          'None before the first snapshot')
    # Privacy: planted subscriber / modem identifiers never leave.
    blob = json.dumps([snap['wan'], snap['cellular'], snap['bonding'], ident,
                       svc.query_topology(), svc.query_wbond()])
    for secret in F.SENSITIVE:
        check(secret not in blob,
              'no planted identifier {!r} in any response'.format(
                  secret[:6] + '...'))
    check('192.0.2' in blob, 'sanity: non-identifier fields are present')
    for field in ('IMEI', 'IMSI', 'ICCID', 'MDN', 'EID', 'SERIAL'):
        check('"{}"'.format(field) not in json.dumps(ident),
              'no {} key in wan_identity API'.format(field))
    e = _one(F.e400_esim_device(), 'mdm-e400e', 'E400')
    check(set(WI.public_view(e)) == set(WI.PUBLIC_FIELDS) and
          F.SENSITIVE[5] not in json.dumps(e) and
          F.SENSITIVE[4] not in json.dumps(e),
          'eSIM profile string and EID are never copied')


def test_wan_identity_http_endpoint():
    print('test_wan_identity_http_endpoint')
    svc = _svc()
    svc._update_config_versions(svc.snapshot(), 1000)
    empty = AnalyzerService()
    ok = HTTPServer(('127.0.0.1', _free_port()), web._make_handler(svc))
    nope = HTTPServer(('127.0.0.1', _free_port()), web._make_handler(empty))
    for s in (ok, nope):
        threading.Thread(target=s.serve_forever, daemon=True).start()

    def get(server, query=''):
        url = 'http://127.0.0.1:%d/api/analytics/wan_identity%s' % (
            server.server_address[1], query)
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())
    try:
        code, body = get(ok)
        check(code == 200 and body['inventory']['displayable_physical'] == 4
              and body['identity_versions']['current'] == 1,
              'GET /api/analytics/wan_identity')
        code, body = get(ok, '?version=1')
        check(code == 200 and
              'mdm-r24a1' in body['version_payload']['payload']['wans'],
              '?version=1 returns the retained identity payload')
        check(get(ok, '?version=99')[0] == 404, 'unretained version -> 404')
        check(get(ok, '?version=abc')[0] == 400, 'bad version -> 400')
        check(get(ok, '?foo=1')[0] == 400, 'unknown parameter -> 400')
        check(get(nope)[0] == 503, '503 before the first snapshot')
        with urllib.request.urlopen('http://127.0.0.1:%d/api/wan' %
                                    ok.server_address[1], timeout=5) as r:
            wan = json.loads(r.read().decode())
        check(len(wan['paths']) == 12 and len(wan['physical']) == 4,
              '/api/wan: raw paths plus displayable physical')
    finally:
        for s in (ok, nope):
            s.shutdown()
            s.server_close()


def test_v084_regression_unknown_sim_unchanged():
    print('test_v084_regression_unknown_sim_unchanged')
    # The v1.0.1 fixture slot (no SIM field, SERDIS 'NOSIM' only) stays a
    # physical, displayable object: nothing there is an explicit indicator.
    inv = _inv(fixtures.WAN_DEVICES, 'R1900')['by_uid']
    r = inv['mdm-f260d']
    check(r['classification'] == 'physical' and r['inventory_visible'] and
          r['kind'] == 'unknown' and r['sim_presence'] == 'unknown',
          'v1.0.1 behavior preserved for non-explicit slots')
    check(inv['mdm-1ea0ac']['display_name'] == 'R1900 Internal - TMO-SIM1',
          'fixture cellular WAN named with the STA convention')
    check(inv['sdwan-hub0_1ea0ac']['display_name'] ==
          'SD-WAN Tunnel - R1900 Internal - TMO-SIM1',
          'overlay named from the same underlay name')


def test_versions_in_sync():
    print('test_versions_in_sync')
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'package.ini')) as f:
        ini = f.read()
    got = tuple(int(re.search(r'^version_%s\s*=\s*(\d+)' % k, ini,
                              re.M).group(1))
                for k in ('major', 'minor', 'patch'))
    check('.'.join(str(n) for n in got) == VERSION == '1.0.5',
          'package.ini {} == analyzer.VERSION {} == 1.0.5'.format(got,
                                                                 VERSION))


TESTS = [
    test_r2400_multi_module_inventory,
    test_explicit_empty_sim_indicators,
    test_populated_sim_standby_and_failed,
    test_inactive_is_not_empty,
    test_e400_att_esim_failed_registration,
    test_internal_captive_flag_means_internal,
    test_captive_modem_models,
    test_unknown_captive_modem,
    test_unknown_carrier,
    test_ethernet_and_satellite,
    test_empty_sim_in_topology_relationships,
    test_inactive_wan_in_wbond,
    test_overlay_underlay_resolution,
    test_unresolved_overlay_underlay,
    test_steering_references_use_shared_identity,
    test_history_identity_continuity,
    test_history_apis_carry_identity_blocks,
    test_contradictory_sim_indicators,
    test_missing_and_malformed_data,
    test_catalog_and_host_model,
    test_snapshot_summary_and_api,
    test_wan_identity_http_endpoint,
    test_v084_regression_unknown_sim_unchanged,
    test_versions_in_sync,
]
