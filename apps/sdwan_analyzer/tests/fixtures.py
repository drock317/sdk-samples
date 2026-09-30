"""Test fixtures modeled on the ACTUAL R1900 (NCOS 7.26.41) API shape.

These mirror the real captured structures, corrected from the earlier
too-simple v0.2.0 fixtures:

  - compiled steering rules have a TOP-LEVEL runtime layer + nested `xpolicy`
    compiled-policy layer;
  - matched_devs / steering_to are DICTS keyed by WAN device ID;
  - xpolicy.trigger_strings is a LIST of DICTS
    ({_id_, trigger_priority, trigger_string});
  - the destination identity is xpolicy.dst_ip_network (a single UUID);
  - top-level intent is the friendly Traffic Class NAME; xpolicy.intent is
    the Traffic Class UUID;
  - xpolicy.src_lans is [{"lan_name": "Primary LAN"}];
  - steering events[].steering_rule and stats.rules[].steering_rule hold the
    LOGICAL RULE NAME, not the compiled UUID;
  - stats.apps is a DICT keyed by internal app/category id.

Test data only -- not shipped, no live secrets. UIDs match the brief so the
acceptance scenario runs locally; production code never hardcodes them.
"""

# --- status/wan/devices ---------------------------------------------------

WAN_DEVICES = {
    'ethernet-wan': {
        'info': {'type': 'ethernet', 'iface': 'eth0', 'uid': 'ethernet-wan'},
        'status': {'connection_state': 'connected'},
        'config': {'priority': 1.000988295},
    },
    'mdm-1ea0ac': {
        'info': {'type': 'mdm', 'iface': 'rmnet_data0', 'uid': 'mdm-1ea0ac',
                 'sim': 'sim1'},
        'status': {'connection_state': 'connected'},
        'config': {'priority': 1.500066819},
        'diagnostics': {'CARRID': 'T-Mobile', 'SERDIS': '5G', 'RSRP': -85,
                        'RSRQ': -10, 'SINR': 12,
                        # Sensitive fields that MUST be filtered out:
                        'IMEI': '350000000000001', 'ICCID': '8901000000000',
                        'IMSI': '310260000000000', 'MDN': '5551234567',
                        'GPS': '37.0,-122.0'},
    },
    'mdm-f260d': {
        'info': {'type': 'mdm', 'iface': 'rmnet_data1', 'uid': 'mdm-f260d'},
        'status': {'connection_state': 'disconnected'},
        'config': {'priority': 2.0},
        'diagnostics': {'CARRID': '', 'SERDIS': 'NOSIM'},
    },
    'sdwan-hub0_wan': {
        'info': {'type': 'sdwan', 'iface': 'gres-hub0_wan',
                 'uid': 'hub0_wan'},
        'status': {'connection_state': 'connected',
                   'active_dep_wandev': 'ethernet-wan',
                   'dep_wandevs': ['ethernet-wan'],
                   'parent_priority': 10000},
        'config': {'priority': 10000.000915582, 'parent_priority': 10000},
    },
    'sdwan-hub0_1ea0ac': {
        'info': {'type': 'sdwan', 'iface': 'gres-hub0_164d3',
                 'uid': 'hub0_1ea0ac'},
        'status': {'connection_state': 'connected',
                   'active_dep_wandev': 'mdm-1ea0ac',
                   'dep_wandevs': ['mdm-1ea0ac'],
                   'parent_priority': 10000},
        'config': {'priority': 10000.000076826, 'parent_priority': 10000},
    },
    'wbond-wbond0_hub0': {
        'info': {'type': 'wbond', 'iface': 'gres_wb0-hub0',
                 'uid': 'wbond0_hub0'},
        'status': {'connection_state': 'connected'},
        'config': {'priority': 5000.0, 'bandwidth_ingress': 1300,
                   'bandwidth_egress': 1300},
    },
}

PRIMARY_DEVICE = 'ethernet-wan'

# Traffic Class UUIDs (xpolicy.intent).
TC_BEST_EFFORT = '00000004-0e93-4371-a6a5-2648354310cc'
TC_MANAGEMENT = '00000001-0e93-4371-a6a5-2648354310cc'

# Destination identity UUIDs (xpolicy.dst_ip_network).
DST_NETFLIX = '5202f45a-ab2e-5110-a864-252a41b8cd01'
DST_HULU = 'a3988412-6e04-5e30-8ff1-98ddefbcabab'
DST_ONLYFANS = 'fa55cbdb-9f6b-5172-b721-3f1a941f98cd'
DST_GAMING = '85c4fc32-c958-5920-9044-baafbe3e6a75'
DST_MULTIMEDIA = '5dafcf94-0e84-5b73-91c6-424c3fa12377'
DST_YT_COM = 'c1000001-0000-0000-0000-0000000000y1'
DST_YT_HD = 'c1000002-0000-0000-0000-0000000000y2'
DST_YT_MUSIC = 'c1000003-0000-0000-0000-0000000000y3'
DST_YT_KIDS = 'c1000004-0000-0000-0000-0000000000y4'


def _dev(*ids):
    """Build a matched_devs/steering_to DICT keyed by WAN id (real shape)."""
    return {i: {} for i in ids}


# --- status/wan/steering/rules (real two-layer shape) ---------------------

STEERING_RULES = [
    # LAB01-Best Effort -> Bonded WAN. Single compiled child.
    {
        'id': '885823d0-1111-2222-3333-444455556666',
        'name': 'LAB01-Best Effort 885823d0-1111-2222-3333-444455556666',
        'intent': 'Best Effort (Data traffic)',   # friendly TC name
        'loadbalance_algo': 'none',                # RUNTIME
        'failover': False,
        'primary_wandev': 'wbond-wbond0_hub0',
        'matched_devs': _dev('wbond-wbond0_hub0'),   # DICT
        'steering_to': _dev('wbond-wbond0_hub0'),     # DICT
        'xpolicy': {
            '_id_': '885823d0-1111-2222-3333-444455556666',
            'name': 'LAB01-Best Effort',
            'src_lans': [{'lan_name': 'Primary LAN'}],
            'trigger_field': 'type',
            'trigger_predicate': 'is',
            'trigger_value': 'wbond',
            'trigger_priority': 2,
            'wan_priority_enabled': False,
            'trigger_strings': [
                {'_id_': 't1', 'trigger_priority': 2,
                 'trigger_string': 'type|is|wbond'}],
            'intent': TC_BEST_EFFORT,               # TC UUID
            'loadbalance_algo': 'none',             # CONFIGURED
            'dst_ip_network': '',
        },
    },
    # Lab01 - Youtube -> All. FOUR compiled children (one per YT app).
    {
        'id': 'yt-child-1',
        'name': 'Lab01 - Youtube yt-child-1',
        'intent': 'Best Effort (Data traffic)',
        'loadbalance_algo': 'none',
        'matched_devs': _dev('sdwan-hub0_1ea0ac', 'sdwan-hub0_wan'),
        'steering_to': _dev('sdwan-hub0_1ea0ac'),
        'xpolicy': {
            '_id_': 'yt-child-1',
            'name': 'Lab01 - Youtube',
            'src_lans': [{'lan_name': 'Primary LAN'}],
            'trigger_priority': 5,
            'wan_priority_enabled': False,
            'trigger_strings': [
                {'_id_': 'ts', 'trigger_priority': 5,
                 'trigger_string': 'type|is|sdwan'}],
            'intent': TC_BEST_EFFORT,
            'dst_ip_network': DST_YT_COM,
        },
    },
    {
        'id': 'yt-child-2',
        'name': 'Lab01 - Youtube yt-child-2',
        'intent': 'Best Effort (Data traffic)',
        'loadbalance_algo': 'none',
        'matched_devs': _dev('sdwan-hub0_1ea0ac', 'sdwan-hub0_wan'),
        'steering_to': _dev('sdwan-hub0_1ea0ac'),
        'xpolicy': {
            '_id_': 'yt-child-2', 'name': 'Lab01 - Youtube',
            'src_lans': [{'lan_name': 'Primary LAN'}],
            'trigger_priority': 5, 'wan_priority_enabled': False,
            'trigger_strings': [
                {'_id_': 'ts', 'trigger_priority': 5,
                 'trigger_string': 'type|is|sdwan'}],
            'intent': TC_BEST_EFFORT, 'dst_ip_network': DST_YT_HD,
        },
    },
    {
        'id': 'yt-child-3',
        'name': 'Lab01 - Youtube yt-child-3',
        'intent': 'Best Effort (Data traffic)',
        'loadbalance_algo': 'none',
        'matched_devs': _dev('sdwan-hub0_1ea0ac', 'sdwan-hub0_wan'),
        'steering_to': _dev('sdwan-hub0_1ea0ac'),
        'xpolicy': {
            '_id_': 'yt-child-3', 'name': 'Lab01 - Youtube',
            'src_lans': [{'lan_name': 'Primary LAN'}],
            'trigger_priority': 5, 'wan_priority_enabled': False,
            'trigger_strings': [
                {'_id_': 'ts', 'trigger_priority': 5,
                 'trigger_string': 'type|is|sdwan'}],
            'intent': TC_BEST_EFFORT, 'dst_ip_network': DST_YT_MUSIC,
        },
    },
    {
        'id': 'yt-child-4',
        'name': 'Lab01 - Youtube yt-child-4',
        'intent': 'Best Effort (Data traffic)',
        'loadbalance_algo': 'none',
        'matched_devs': _dev('sdwan-hub0_1ea0ac', 'sdwan-hub0_wan'),
        'steering_to': _dev('sdwan-hub0_1ea0ac'),
        'xpolicy': {
            '_id_': 'yt-child-4', 'name': 'Lab01 - Youtube',
            'src_lans': [{'lan_name': 'Primary LAN'}],
            'trigger_priority': 5, 'wan_priority_enabled': False,
            'trigger_strings': [
                {'_id_': 'ts', 'trigger_priority': 5,
                 'trigger_string': 'type|is|sdwan'}],
            'intent': TC_BEST_EFFORT, 'dst_ip_network': DST_YT_KIDS,
        },
    },
    # TEST - Applications -> All. Netflix + Hulu + OnlyFans children.
    {
        'id': 'test-1', 'name': 'TEST - Applications test-1',
        'intent': 'Best Effort (Data traffic)', 'loadbalance_algo': 'none',
        'matched_devs': _dev('sdwan-hub0_1ea0ac', 'sdwan-hub0_wan'),
        'steering_to': _dev('sdwan-hub0_1ea0ac'),
        'xpolicy': {
            '_id_': 'test-1', 'name': 'TEST - Applications',
            'src_lans': [{'lan_name': 'Primary LAN'}],
            'trigger_priority': 8, 'wan_priority_enabled': False,
            'trigger_strings': [
                {'_id_': 'ts', 'trigger_priority': 8,
                 'trigger_string': 'type|is|sdwan'}],
            'intent': TC_BEST_EFFORT, 'dst_ip_network': DST_NETFLIX,
        },
    },
    {
        'id': 'test-2', 'name': 'TEST - Applications test-2',
        'intent': 'Best Effort (Data traffic)', 'loadbalance_algo': 'none',
        'matched_devs': _dev('sdwan-hub0_1ea0ac', 'sdwan-hub0_wan'),
        'steering_to': _dev('sdwan-hub0_1ea0ac'),
        'xpolicy': {
            '_id_': 'test-2', 'name': 'TEST - Applications',
            'src_lans': [{'lan_name': 'Primary LAN'}],
            'trigger_priority': 8, 'wan_priority_enabled': False,
            'trigger_strings': [
                {'_id_': 'ts', 'trigger_priority': 8,
                 'trigger_string': 'type|is|sdwan'}],
            'intent': TC_BEST_EFFORT, 'dst_ip_network': DST_HULU,
        },
    },
    {
        'id': 'test-3', 'name': 'TEST - Applications test-3',
        'intent': 'Best Effort (Data traffic)', 'loadbalance_algo': 'none',
        'matched_devs': _dev('sdwan-hub0_1ea0ac', 'sdwan-hub0_wan'),
        'steering_to': _dev('sdwan-hub0_1ea0ac'),
        'xpolicy': {
            '_id_': 'test-3', 'name': 'TEST - Applications',
            'src_lans': [{'lan_name': 'Primary LAN'}],
            'trigger_priority': 8, 'wan_priority_enabled': False,
            'trigger_strings': [
                {'_id_': 'ts', 'trigger_priority': 8,
                 'trigger_string': 'type|is|sdwan'}],
            'intent': TC_BEST_EFFORT, 'dst_ip_network': DST_ONLYFANS,
        },
    },
    # Management Steering -> explicit WAN priority.
    {
        'id': 'mgmt-1', 'name': 'Management Steering mgmt-1',
        'intent': 'Management (Management traffic)',
        'loadbalance_algo': 'none',
        'primary_wandev': 'wbond-wbond0_hub0',
        'matched_devs': _dev('wbond-wbond0_hub0', 'sdwan-hub0_1ea0ac',
                             'sdwan-hub0_wan'),
        'steering_to': _dev('wbond-wbond0_hub0'),
        'xpolicy': {
            '_id_': 'mgmt-1', 'name': 'Management Steering',
            'src_lans': [{'lan_name': 'NCX DNS LAN'}],
            'trigger_priority': 1,
            'wan_priority_enabled': True,
            'trigger_strings': [
                {'_id_': 'm1', 'trigger_priority': 10,
                 'trigger_string': 'type|is|wbond'},
                {'_id_': 'm2', 'trigger_priority': 20,
                 'trigger_string':
                     'type|is|sdwan%subtype|is|mdm%service_type|is|5G'},
                {'_id_': 'm3', 'trigger_priority': 30,
                 'trigger_string':
                     'type|is|sdwan%subtype|is|mdm%service_type|is|LTE'},
                {'_id_': 'm4', 'trigger_priority': 40,
                 'trigger_string': 'type|is|sdwan%subtype|is|ethernet'},
                {'_id_': 'm5', 'trigger_priority': 50,
                 'trigger_string': 'type|is|sdwan%subtype|is|wwan'},
            ],
            'intent': TC_MANAGEMENT,
            'dst_ip_network': '',
        },
    },
]

# --- status/wan/steering/stats (steering_rule = LOGICAL NAME) -------------

STEERING_STATS = {
    'began_capture': 1772148824.45,
    'flows_truncated': False,
    'rules': [
        {'steering_rule': 'LAB01-Best Effort',
         'preferred_wan': 'wbond-wbond0_hub0',
         'preferred_flows': 54, 'nonpreferred_flows': 26,
         'was_flow_matched': True, 'apps': {}},
        {'steering_rule': 'Lab01 - Youtube',
         'preferred_wan': 'sdwan-hub0_1ea0ac',
         'preferred_flows': 18, 'nonpreferred_flows': 4,
         'was_flow_matched': True,
         'apps': {'a000015A8': {'preferred_flows': 18, 'nonpreferred_flows': 4,
                                'appname': 'YouTube', 'catname': 'None',
                                'was_flow_matched': True}}},
        {'steering_rule': 'TEST - Applications',
         'preferred_wan': 'sdwan-hub0_1ea0ac',
         'preferred_flows': 6, 'nonpreferred_flows': 1,
         'was_flow_matched': True,
         'apps': {'a000012DE': {'preferred_flows': 3, 'nonpreferred_flows': 0,
                                'appname': 'Netflix.com', 'catname': 'None',
                                'was_flow_matched': True},
                  'a00001496': {'preferred_flows': 3, 'nonpreferred_flows': 1,
                                'appname': 'Hulu', 'catname': 'None',
                                'was_flow_matched': True}}},
    ],
}

# --- status/wan/steering/events (steering_rule = LOGICAL NAME) ------------

STEERING_EVENTS = {
    'began_capture': 1772148824.45,
    'total_perf_events': 3,
    'event_overflow': False,
    'steering_events': [
        {'epoch': 1772148900.0, 'steering_rule': 'Lab01 - Youtube',
         'reason': 'WANs equal, chose default',
         'wan_to_uid': 'sdwan-hub0_1ea0ac', 'total_flows_moved': 4,
         'destinations': [], 'src_lans': ['Primary LAN']},
        {'epoch': 1772148950.0, 'steering_rule': 'LAB01-Best Effort',
         'reason': 'Single selected WAN is connected',
         'wan_to_uid': 'wbond-wbond0_hub0', 'total_flows_moved': 60},
        {'epoch': 1772149000.0, 'steering_rule': 'Management Steering',
         'reason': 'preferred WAN selected based on WAN priority',
         'wan_to_uid': 'wbond-wbond0_hub0', 'total_flows_moved': 1},
    ],
}

# --- status/wan/steering (single parent object, split locally) ------------

STEERING_PARENT = {
    'events': STEERING_EVENTS,
    'stats': STEERING_STATS,
    'perf': {'event_overflow': False},
    'rules': STEERING_RULES,
    'intents': {},
}

# --- config/identities/ip (destination identities) ------------------------

IDENTITIES_IP = {
    DST_YT_COM: {'_id_': DST_YT_COM, 'friendly_name': 'Youtube.com',
                 'internal_name': 'a00001AAA', 'type': 'application'},
    DST_YT_HD: {'_id_': DST_YT_HD, 'friendly_name': 'Youtube HD',
                'internal_name': 'a00001BBB', 'type': 'application'},
    DST_YT_MUSIC: {'_id_': DST_YT_MUSIC, 'friendly_name': 'Youtube Music',
                   'internal_name': 'a00001CCC', 'type': 'application'},
    DST_YT_KIDS: {'_id_': DST_YT_KIDS, 'friendly_name': 'YouTube Kids',
                  'internal_name': 'a00001DDD', 'type': 'application'},
    DST_NETFLIX: {'_id_': DST_NETFLIX, 'friendly_name': 'Netflix.com',
                  'internal_name': 'a000012DE', 'type': 'application'},
    DST_HULU: {'_id_': DST_HULU, 'friendly_name': 'Hulu',
               'internal_name': 'a00001496', 'type': 'application'},
    DST_ONLYFANS: {'_id_': DST_ONLYFANS, 'friendly_name': 'OnlyFans',
                   'internal_name': 'a00001F30', 'type': 'application'},
    DST_GAMING: {'_id_': DST_GAMING, 'friendly_name': 'Gaming',
                 'internal_name': 'c0116', 'type': 'category'},
    DST_MULTIMEDIA: {'_id_': DST_MULTIMEDIA,
                     'friendly_name': 'Multimedia streaming',
                     'internal_name': 'c0119', 'type': 'category'},
}

# --- status/wan/loadbalancers (spillover example) -------------------------

LOADBALANCERS = {
    'spillover-hub0': {
        'type': 'spillover',
        'active_device': 'sdwan-hub0_1ea0ac',
        'aggregate_max': 78.125,
        'ingress_max': 39.0625, 'egress_max': 39.0625,
        'ingress_current': 12.0, 'egress_current': 8.0,
        'ingress_adj': 1.0, 'ingress_run': 5.0,
        'devices': {
            'sdwan-hub0_wan': {'uid': 'sdwan-hub0_wan', 'score': 31,
                               'active': False},
            'sdwan-hub0_1ea0ac': {'uid': 'sdwan-hub0_1ea0ac', 'score': 39,
                                  'active': True},
        },
    },
}

# --- status/sdwan_adv/wan_bonding (REAL detailed runtime) -----------------

WAN_BONDING = {
    'supported_capabilities': ['FLOW_BALANCE', 'FLOW_DUPLICATE',
                               'BW_AGGREGATION',
                               'WBOND_RCVD_WEIGHT_CALCULATION'],
    'interfaces': {
        'gres_wb0-hub0': {
            'negotiated_capabilities': ['FLOW_BALANCE', 'BW_AGGREGATION'],
            'gre key': 16448,
            'UL total flows': 13,
            'DL total flows': 29,
            'link state': 'UP',
            'mtu': '1248',
            'Total interfaces': 2,
            'Bonding Mode': 'GRE',
            'Priority': 0,
            'members_list': {
                'gres-hub0_wan': {
                    'profile name': 'Ethernet',
                    'intf name': 'ethernet-wan',
                    'config_weight': 80,
                    'DL current flows': 24,
                    'UL current flows': 11,
                    'us_weight': 80,
                    'ds_weight': 75,
                    'us_curr_bw': 1048576, 'ds_curr_bw': 1048576,
                    'ds_bw_trend': 0, 'us_bw_trend': 0,
                    'ds_last_bw_sample': 204800,
                    'us_last_bw_sample': 1048576,
                    'has_loss': 0,
                    'link state': 'UP',
                    'link attach state': 'ATTACHED',
                    'gre_key': 16384, 'mtu': 1312,
                },
                'gres-hub0_164d3': {
                    'profile name': 'Cellular',
                    'intf name': 'mdm-1ea0ac',
                    'config_weight': 20,
                    'DL current flows': 5,
                    'UL current flows': 2,
                    'us_weight': 20,
                    'ds_weight': 24,
                    'us_curr_bw': 262144, 'ds_curr_bw': 262144,
                    'ds_bw_trend': 0, 'us_bw_trend': 0,
                    'ds_last_bw_sample': 51200,
                    'us_last_bw_sample': 262144,
                    'has_loss': 0,
                    'link state': 'UP',
                    'link attach state': 'ATTACHED',
                    'gre_key': 16385, 'mtu': 1312,
                },
            },
        },
    },
}

# --- status/sdwan_adv/qoe (REAL: singular 'interface') --------------------

QOE = {
    'interface': {
        'hub0_wan': {
            'pkt-loss-stats': {'pkts-lost': 0, 'pkts-total': 1000},
            'qoe-latency-stats': {
                'pkts-intf-qoe-latency-last-sample': 8,
                'pkts-intf-qoe-latency-average': 7,
            },
        },
        'hub0_1ea0ac': {
            'pkt-loss-stats': {'pkts-lost': 1, 'pkts-total': 1000},
            'qoe-latency-stats': {
                'pkts-intf-qoe-latency-last-sample': 112,
                'pkts-intf-qoe-latency-average': 113,
            },
        },
    },
}

# --- status/wan/swans (REAL: history[].upd_status[]) ----------------------

SWANS = {
    'cm': {'priority': {}},
    'dataused': {'monthly': 0},
    'history': [
        {'epoch': 1790375600,
         'upd_status': [
             {'wandev': 'sdwan-hub0_wan',
              'criteria': {'Latency': 10.0, 'Jitter': 2.0}},
             {'wandev': 'sdwan-hub0_1ea0ac',
              'criteria': {'Latency': 120.0, 'Jitter': 11.0}},
         ]},
        {'epoch': 1790375610,
         'upd_status': [
             {'wandev': 'sdwan-hub0_wan',
              'criteria': {'Latency': 9.48, 'Jitter': 1.79}},
             {'wandev': 'sdwan-hub0_1ea0ac',
              'criteria': {'Latency': 111.93, 'Jitter': 9.59}},
         ]},
    ],
}

# --- config/sdwan_adv/traffic_class (REAL NCX class config) ---------------

TC_BE_ID = '00000003-694a-35d1-b32f-cda47a89d773'
TC_RT_ID = '00000003-694a-35d1-b32f-cda47a89d774'

TRAFFIC_CLASS_CFG = {
    TC_BE_ID: {
        '_id_': TC_BE_ID, 'name': 'Best Effort (Data traffic)',
        'dscp': 8, 'forward_error_correction': False,
        'reset_existing_flow': False,
        'wan_bonding': {'algo': 'flow_balance'},
    },
    TC_RT_ID: {
        '_id_': TC_RT_ID, 'name': 'Real Time (Voice traffic)',
        'dscp': 46, 'forward_error_correction': True,
        'reset_existing_flow': True,
        'wan_bonding': {'algo': 'flow_duplicate'},
    },
}

# --- config/identities/intent (REAL: criteria[] + traffic_class link) -----

INTENT_RT_ID = '00000005-0e93-4371-a6a5-2648354310cc'

IDENTITIES_INTENT = {
    TC_BEST_EFFORT: {
        '_id_': TC_BEST_EFFORT,
        'name': 'Best Effort (Data traffic)',
        'accept_age': 30,
        'loadbalance_algo': 'none',
        'traffic_class': TC_BE_ID,
        'criteria': [
            {'criterion': 'Latency', 'enabled': True, 'hysteresis': 50,
             'priority': 10, 'target4': '100.127.255.254',
             'target6': 'fd00::1', 'threshold': 1000},
            {'criterion': 'Jitter', 'enabled': True, 'hysteresis': 50,
             'priority': 20, 'threshold': 600},
            {'criterion': 'SS', 'enabled': False, 'hysteresis': 50,
             'priority': 30, 'threshold': 70},
        ],
    },
    INTENT_RT_ID: {
        '_id_': INTENT_RT_ID,
        'name': 'Real Time (Voice traffic)',
        'accept_age': 30,
        'loadbalance_algo': 'round-robin',
        'traffic_class': TC_RT_ID,
        'criteria': [
            {'criterion': 'Latency', 'enabled': True, 'hysteresis': 50,
             'priority': 10, 'threshold': 1000},
            {'criterion': 'Jitter', 'enabled': True, 'hysteresis': 50,
             'priority': 20, 'threshold': 600},
        ],
    },
    # Legacy identity intent WITHOUT a traffic_class field -> must be
    # EXCLUDED from the normalized NCX Traffic Class view.
    'legacy-voice': {
        '_id_': 'legacy-voice', 'name': 'Voice',
        'criteria': [{'criterion': 'Latency', 'enabled': True,
                      'threshold': 150}],
    },
}

# --- config/wan/rules2 ----------------------------------------------------

WAN_RULES2 = {
    '0': {'_id_': 'rule-eth', 'name': 'Ethernet',
          'trigger_string': 'type|is|ethernet', 'priority': 1.000988295,
          'bandwidth_ingress': 40000, 'bandwidth_egress': 40000},
    '1': {'_id_': 'rule-cell', 'name': 'Cellular',
          'trigger_string': 'type|is|mdm', 'priority': 1.500066819,
          'bandwidth_ingress': 40000, 'bandwidth_egress': 40000},
}


def base_raw():
    """Fast-cadence raw dict as collect_base_snapshot would compose it
    (post per-key merge in _RawCache -> plain values)."""
    return {
        'wan_devices': WAN_DEVICES,
        'primary_device': PRIMARY_DEVICE,
        'wan_policies': {},
        'swans': SWANS,
        'sdwan_links': {},
        'steering_rules': STEERING_RULES,
        'steering_events': STEERING_EVENTS,
        'steering_stats': STEERING_STATS,
        'steering_intents': {},
    }


def heavy_raw():
    """Slow-cadence raw dict as collect_heavy_snapshot would compose it.

    Note: NO 'sdwan_adv' key -- the broad status/sdwan_adv read was removed;
    only targeted subtrees (wan_bonding, qoe) are collected.
    """
    return {
        'wan_bonding': WAN_BONDING,
        'qoe': QOE,
        'loadbalancers': LOADBALANCERS,
        'wan_rules2': WAN_RULES2,
        'identities_ip': IDENTITIES_IP,
        'identities_intent': IDENTITIES_INTENT,
        'traffic_class_config': TRAFFIC_CLASS_CFG,
    }
