"""Sanitized v0.8.3 fixtures built from NCOS 7.26.81 response SHAPES.

Every identifier, name, UUID and value here is synthetic. Field names and
nesting follow the Phase 1A verified structures; no raw capture is included.
Builders return fresh objects so tests can mutate them freely.
"""
import copy

# --- identities / intents / traffic classes (config) ------------------------

ID_A = '11111111-aaaa-4bbb-8ccc-000000000001'
ID_B = '11111111-aaaa-4bbb-8ccc-000000000002'
ID_C = '11111111-aaaa-4bbb-8ccc-000000000003'
INTENT_1 = '22222222-aaaa-4bbb-8ccc-000000000001'
INTENT_2 = '22222222-aaaa-4bbb-8ccc-000000000002'
TC_1 = '33333333-aaaa-4bbb-8ccc-000000000001'
TC_2 = '33333333-aaaa-4bbb-8ccc-000000000002'


def identities_ip():
    # Names are arbitrary (no a*/c* convention relied on).
    return [
        {'_id_': ID_A, 'name': 'zq-17', 'friendly_name': 'Synthetic App A'},
        {'_id_': ID_B, 'name': 'k_0042', 'friendly_name': 'Synthetic Cat B'},
        {'_id_': ID_C, 'name': 'Mx', 'friendly_name': 'Synthetic C'},
    ]


def identities_intent():
    return [
        {'_id_': INTENT_1, 'name': 'Class One', 'traffic_class': TC_1},
        {'_id_': INTENT_2, 'name': 'Class Two', 'traffic_class': TC_2},
    ]


def traffic_class_cfg():
    return [{'_id_': TC_1, 'name': 'TC One'},
            {'_id_': TC_2, 'name': 'TC Two'}]


# --- status/wan/steering/rules (two-layer) ----------------------------------

def child(name, cid, dst=ID_A, intent=INTENT_1, prio=10,
          triggers=(('type|is|sdwan', 10),), algo='none', failover=False,
          reset=False, wan_prio=False, src=('Primary LAN',), excluded=(),
          dia=False, dst_neg=False, runtime=None):
    xp = {
        '_id_': cid, 'name': name, 'trigger_priority': prio,
        'intent': intent, 'dst_neg': dst_neg, 'dst_port_neg': False,
        'src_lans': [{'lan_name': n} for n in src],
        'excluded_lans': [{'config_id': c} for c in excluded],
        'trigger_strings': [{'_id_': 'ts-{}-{}'.format(cid, i),
                             'trigger_priority': p, 'trigger_string': t}
                            for i, (t, p) in enumerate(triggers)],
        'wan_priority_enabled': wan_prio, 'loadbalance_algo': algo,
        'failover': failover, 'reset_existing_flow': reset, 'dia': dia,
    }
    if dst is not None:
        xp['dst_ip_network'] = dst
    rt = {'matched_devs': {'sdwan-x1': {}}, 'steering_to': {'sdwan-x1': {}},
          'primary_wandev': 'sdwan-x1', 'loadbalance_algo': 'none',
          'intent': 'Class One (friendly)'}
    rt.update(runtime or {})
    out = {'id': cid, 'name': '{} {}'.format(name, cid), 'xpolicy': xp}
    out.update(rt)
    return out


def rules():
    return [
        child('Rule Alpha', 'c-001', dst=ID_A),
        child('Rule Alpha', 'c-002', dst=ID_B),
        child('Rule Beta', 'c-003', dst=ID_C, intent=INTENT_2, prio=20),
    ]


def stats(began=1790000000.5, alpha_apps=None):
    apps = alpha_apps if alpha_apps is not None else {
        'zq-17': {'appname': 'Synthetic App A', 'catname': 'None',
                  'preferred_flows': 4, 'nonpreferred_flows': 0,
                  'was_flow_matched': True},
        'k_0042': {'appname': 'None', 'catname': 'Synthetic Cat B',
                   'preferred_flows': 0, 'nonpreferred_flows': 0,
                   'was_flow_matched': True},
    }
    return {'began_capture': began, 'flows_truncated': False, 'rules': [
        {'steering_rule': 'Rule Alpha', 'preferred_flows': 4,
         'nonpreferred_flows': 0, 'was_flow_matched': True,
         'total_perf_events': 1, 'apps': apps},
        {'steering_rule': 'Rule Beta', 'preferred_flows': 0,
         'nonpreferred_flows': None, 'was_flow_matched': False, 'apps': {}},
    ]}


def events(began=1790000000.5):
    return {'began_capture': began, 'total_perf_events': 1,
            'event_overflow': False, 'steering_events': [
                {'epoch': 1790000100.0, 'steering_rule': 'Rule Alpha',
                 'reason': 'synthetic reason', 'wan_to_uid': 'sdwan-x1',
                 'total_flows_moved': 3,
                 'destinations': [{'appname': 'Synthetic App A',
                                   'catname': 'None', 'flows_moved': 3}]}]}


# --- WAN bonding (config + runtime) -----------------------------------------

def bond_cfg(dynamic=False, criteria=None, bonds=None):
    if bonds is not None:
        return bonds
    return [{'_id_': 'bond-cfg-1', 'name': 'Bond One',
             'dynamic_weight': dynamic,
             'members': criteria if criteria is not None else [
                 {'_id_': 'crit-1', 'trigger_string': 'type|is|ethernet',
                  'weight': 70},
                 {'_id_': 'crit-2',
                  'trigger_string': 'type|is|none%config_name|is|Cell Profile',
                  'weight': 100}]}]


def rules2():
    return [
        {'_id_': 'r2-eth', 'trigger_name': 'Wired',
         'trigger_string': 'type|is|ethernet'},
        {'_id_': 'r2-cell', 'trigger_name': 'Cell Profile',
         'trigger_string': 'type|is|mdm'},
    ]


def wan_devices(n_cell=2, extra_disconnected=False):
    """ethernet + n_cell modems, each with an sdwan overlay."""
    devs = {
        'ethernet-wan': {'info': {'type': 'ethernet', 'iface': 'eth0',
                                  'uid': 'ethernet-wan',
                                  'config_id': 'r2-eth'},
                         'status': {'connection_state': 'connected'}},
        'sdwan-o_eth': {'info': {'type': 'sdwan', 'iface': 'gre-o_eth',
                                 'uid': 'o_eth'},
                        'status': {'connection_state': 'connected',
                                   'active_dep_wandev': 'ethernet-wan',
                                   'dep_wandevs': ['ethernet-wan']}},
        'wbond-wb0': {'info': {'type': 'wbond', 'iface': 'gres_wb0',
                               'uid': 'wb0'},
                      'status': {'connection_state': 'connected'}},
    }
    for i in range(n_cell):
        uid = 'mdm-c{}'.format(i)
        devs[uid] = {'info': {'type': 'mdm', 'iface': 'rmnet{}'.format(i),
                              'uid': uid, 'config_id': 'r2-cell',
                              'sim': 'sim1'},
                     'status': {'connection_state': 'connected'}}
        devs['sdwan-o_c{}'.format(i)] = {
            'info': {'type': 'sdwan', 'iface': 'gre-o_c{}'.format(i),
                     'uid': 'o_c{}'.format(i)},
            'status': {'connection_state': 'connected',
                       'active_dep_wandev': uid, 'dep_wandevs': [uid]}}
    if extra_disconnected:
        devs['mdm-idle'] = {'info': {'type': 'mdm', 'iface': 'rmnet9',
                                     'uid': 'mdm-idle',
                                     'config_id': 'r2-cell'},
                            'status': {'connection_state': 'disconnected'}}
    return devs


def bond_runtime(n_cell=2, weights=None):
    """status/sdwan_adv/wan_bonding with eth + n_cell members."""
    members = {'gre-o_eth': {'intf name': 'ethernet-wan',
                             'profile name': 'Wired',
                             'config_weight': 70, 'ds_weight': 70,
                             'us_weight': 70, 'DL current flows': 3,
                             'UL current flows': 1, 'link state': 'UP',
                             'link attach state': 'ATTACHED',
                             'ds_curr_bw': 1000, 'us_curr_bw': 500}}
    for i in range(n_cell):
        w = (weights or {}).get(i, {'config_weight': 33, 'ds_weight': 33,
                                    'us_weight': 33})
        m = {'intf name': 'mdm-c{}'.format(i),
             'profile name': 'Cell Profile', 'link state': 'UP',
             'link attach state': 'ATTACHED', 'DL current flows': 1,
             'UL current flows': 0}
        m.update(w)
        members['gre-o_c{}'.format(i)] = m
    return {'supported_capabilities': [], 'interfaces': {'gres_wb0': {
        'Bonding Mode': 'GRE', 'link state': 'UP',
        'Total interfaces': 1 + n_cell, 'members_list': members}}}


# --- cellular diagnostics (synthetic values) --------------------------------

LTE_ONLY = {
    'CARRID': 'CarrierX', 'SRVC_TYPE': 'LTE', 'RFBAND': 'Band 2',
    'RFCHANNEL': '900', 'LTEBANDWIDTH': '10 MHz', 'RSRP': '-95',
    'RSRQ': '-10', 'SINR': '12.0', 'PHY_CELL_ID': '7', 'CELL_ID': '7777777',
    'ROAM': '1', 'SIM_SLOT_ID': '1',
}

NSA = {
    'CARRID': 'CarrierY', 'SRVC_TYPE': '5G', 'SRVC_TYPE_DETAILS': 'NSA',
    'RFBAND': 'Band 66', 'RFCHANNEL': '66486', 'LTEBANDWIDTH': '20 MHz',
    'RSRP': '-90', 'RSRQ': '-11', 'SINR': '10.0', 'PHY_CELL_ID': '101',
    'CELL_ID': '9999999',
    # Component family: `_5G_` in the KEY, LTE band in the VALUE.
    'BAND_5G_PCELL': 'Band 66', 'BANDWIDTH_5G_PCELL': '20 MHz',
    'CHANNEL_5G_PCELL': '66486', 'ACTIVE_5G_PCELL': 'active',
    'SINR_5G_PCELL': '181',
    'BAND_5G_SCELL0': 'n41', 'BANDWIDTH_5G_SCELL0': '100 MHz',
    'CHANNEL_5G_SCELL0': '520110', 'ACTIVE_5G_SCELL0': 'active',
    'RSRP_5G_SCELL0': '-88', 'RSRQ_5G_SCELL0': '-12',
    'SINR_5G_SCELL0': '290',
    'BAND_5G_SCELL1': 'n41', 'BANDWIDTH_5G_SCELL1': '0 MHz',
    'CHANNEL_5G_SCELL1': '501390', 'ACTIVE_5G_SCELL1': 'active',
    'RSRP_5G_SCELL1': '0', 'RSRQ_5G_SCELL1': '0', 'SINR_5G_SCELL1': '0',
    'BAND_5G_SCELL2': 'n71', 'BANDWIDTH_5G_SCELL2': '15 MHz',
    'CHANNEL_5G_SCELL2': '126270', 'ACTIVE_5G_SCELL2': 'active',
    'SINR_5G_SCELL2': '25',
    # Sensitive (synthetic) values that must never be persisted.
    'IMEI': '000000000000011', 'IMEI2': '000000000000022',
    'IMEISV': '0000000000000033', 'IMSI': '000000000000044',
    'ICCID': '0000000000000000055', 'MDN': '0000000066',
    'CGSN': '000000000000077', 'GSN': '000000000000088',
    'DISP_IMEI': '000000000000099', 'NR_CELL_ID': '123123123',
    'GPS': '0.123,-0.456', 'ROAM': '3',
}


def nsa_dup_scell():
    d = copy.deepcopy(NSA)
    # SCELL3 identical to SCELL0 in RAT/band/channel/bandwidth.
    d.update({'BAND_5G_SCELL3': 'n41', 'BANDWIDTH_5G_SCELL3': '100 MHz',
              'CHANNEL_5G_SCELL3': '520110', 'ACTIVE_5G_SCELL3': 'active',
              'SINR_5G_SCELL3': '250'})
    return d


NO_SIM = {'NOSIM': 'TRUE', 'HOMECARRID': 'CarrierZ', 'SIM_SLOT_ID': '2'}
