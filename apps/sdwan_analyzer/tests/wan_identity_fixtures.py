"""Sanitized fixtures for the v1.0.2 WAN identity tests.

Shapes follow the documented NCOS status/wan/devices layout
(docs/ncos-api/status/wan/devices/diagnostics.md: NOSIM, SIM_SLOT_ID,
HOMECARRID; control/wan.md: EUICC_*) and the R2400 evidence supplied for
v1.0.2 (diagnostics NOSIM == 'TRUE', PIN_STATUS == 'NOSIM', status.error_text
== 'SIM error: NOSIM', info.remote.internal_captive for the internal modem).

Everything below is SYNTHETIC: no real subscriber identifiers, IP addresses,
MAC addresses or serial numbers. The `SENSITIVE` values are planted in the
raw devices on purpose, so tests can prove they never reach an identity
record or any API response.
"""
import copy

# Planted raw values that MUST NOT appear in any identity / API output.
SENSITIVE = (
    '350000000000001',        # IMEI
    '310260000000001',        # IMSI
    '8901000000000000001',    # ICCID
    '15555550100',            # MDN
    'EID-0000-SYNTHETIC',     # eUICC identifier
    '8901882000000000004F',   # eUICC profile ICCID
    'SN-SYNTHETIC-0001',      # serial
    '1234',                   # PIN
)

NOSIM_ERROR = 'SIM error: NOSIM'


def _diag_base(extra=None):
    d = {'DISP_IMEI': SENSITIVE[0], 'IMSI': SENSITIVE[1],
         'MDN': SENSITIVE[3], 'RSRP': '-85', 'RSRQ': '-10', 'SINR': '12'}
    d.update(extra or {})
    return d


def mdm(uid, sim, state='connected', carrid=None, home=None, iccid=True,
        nosim=False, pin=None, error_text=None, remote=None, product=None,
        diag=None, port='int1', priority=2.0, extra_info=None):
    """One status/wan/devices mdm-* entry."""
    d = {}
    if nosim:
        d['NOSIM'] = 'TRUE'
    if pin is not None:
        d['PIN_STATUS'] = pin
    if carrid:
        d['CARRID'] = carrid
    if home:
        d['HOMECARRID'] = home
    if iccid and not nosim:
        d['ICCID'] = SENSITIVE[2]
    d.update(diag or {})
    info = {'type': 'mdm', 'iface': 'rmnet_' + uid[-4:], 'uid': uid,
            'sim': sim, 'port': port}
    if product:
        info['product'] = product
    if remote is not None:
        info['remote'] = remote
    info.update(extra_info or {})
    status = {'connection_state': state}
    if error_text:
        status['error_text'] = error_text
    return {'info': info, 'status': status, 'config': {'priority': priority},
            'diagnostics': _diag_base(d)}


def ethernet(state='connected', priority=1.0):
    return {'info': {'type': 'ethernet', 'iface': 'eth0',
                     'uid': 'ethernet-wan'},
            'status': {'connection_state': state,
                       'ipinfo': {'ip_address': '192.0.2.10'}},
            'config': {'priority': priority}}


def overlay(uid, iface, dep, active=None, priority=10000.0):
    st = {'connection_state': 'connected', 'dep_wandevs': list(dep)}
    if active is not None:
        st['active_dep_wandev'] = active
    return {'info': {'type': 'sdwan', 'iface': iface, 'uid': uid[len('sdwan-'):]},
            'status': st, 'config': {'priority': priority}}


INTERNAL_REMOTE = {'internal_captive': True, 'product_name': 'R2400SDX',
                   'mac0': '02:00:00:00:00:01'}


def captive_remote(model, mac='02:00:00:00:00:02'):
    return {'internal_captive': False, 'product_name': model, 'mac0': mac}


def r2400_devices():
    """R2400 with an internal modem plus RC1250 and W1855 captive modems.

    7 raw physical objects: ethernet-wan, internal SIM1 (connected), RC1250
    SIM1 (connected), W1855 SIM2 (standby, populated) and three explicitly
    empty SIM slots (internal SIM2, RC1250 SIM2, W1855 SIM1).
    """
    nosim = dict(nosim=True, pin='NOSIM', error_text=NOSIM_ERROR,
                 state='disconnected')
    devs = {
        'ethernet-wan': ethernet(),
        'mdm-r24a1': mdm('mdm-r24a1', 'sim1', carrid='ATT',
                         remote=copy.deepcopy(INTERNAL_REMOTE),
                         priority=1.5, port='int1'),
        'mdm-r24a2': mdm('mdm-r24a2', 'sim2',
                         remote=copy.deepcopy(INTERNAL_REMOTE),
                         port='int1', **nosim),
        'mdm-rc01': mdm('mdm-rc01', 'sim1', carrid='Verizon',
                        remote=captive_remote('RC1250'), priority=1.6,
                        port='usb1'),
        'mdm-rc02': mdm('mdm-rc02', 'sim2',
                        remote=captive_remote('RC1250'), port='usb1',
                        **nosim),
        'mdm-w185a': mdm('mdm-w185a', 'sim1',
                         remote=captive_remote('W1855', '02:00:00:00:00:03'),
                         port='usb2', **nosim),
        # Populated SIM in standby: ICCID present, home carrier only.
        'mdm-w185b': mdm('mdm-w185b', 'sim2', state='standby',
                         home='T-Mobile',
                         remote=captive_remote('W1855', '02:00:00:00:00:03'),
                         port='usb2', priority=3.0),
        'sdwan-hub0_wan': overlay('sdwan-hub0_wan', 'gres-hub0_wan',
                                  ['ethernet-wan'], 'ethernet-wan'),
        'sdwan-hub0_rc01': overlay('sdwan-hub0_rc01', 'gres-hub0_rc01',
                                   ['mdm-rc01'], 'mdm-rc01'),
        'sdwan-hub0_w185': overlay('sdwan-hub0_w185', 'gres-hub0_w185',
                                   ['mdm-w185b'], 'mdm-w185b'),
        'sdwan-hub0_r24a2': overlay('sdwan-hub0_r24a2', 'gres-hub0_r24a2',
                                    ['mdm-r24a2'], 'mdm-r24a2'),
        'wbond-wbond0_hub0': {
            'info': {'type': 'wbond', 'iface': 'gres_wb0-hub0',
                     'uid': 'wbond0_hub0'},
            'status': {'connection_state': 'connected'},
            'config': {'priority': 5000.0}},
    }
    return devs


def r2400_bonding():
    """WBOND whose members include an inactive WAN and an empty SIM slot."""
    def member(intf, weight):
        return {'profile name': 'p', 'intf name': intf,
                'config_weight': weight, 'DL current flows': 1,
                'UL current flows': 1, 'us_weight': weight,
                'ds_weight': weight, 'has_loss': 0, 'link state': 'UP',
                'link attach state': 'ATTACHED'}
    return {
        'supported_capabilities': ['FLOW_BALANCE'],
        'interfaces': {
            'gres_wb0-hub0': {
                'negotiated_capabilities': ['FLOW_BALANCE'],
                'UL total flows': 4, 'DL total flows': 4,
                'link state': 'UP', 'Total interfaces': 4,
                'Bonding Mode': 'GRE',
                'members_list': {
                    'gres-hub0_wan': member('ethernet-wan', 50),
                    'gres-hub0_rc01': member('mdm-rc01', 20),
                    'gres-hub0_w185': member('mdm-w185b', 20),
                    'gres-hub0_r24a2': member('mdm-r24a2', 10),
                }}}}


def e400_esim_device(state='available'):
    """Documented E400 AT&T eSIM: model 'Internal 5GE-AM (eSIM)', home carrier
    AT&T, not registered, error 'CPPM failed: no carrier'."""
    dev = mdm('mdm-e400e', 'sim1', state=state, home='AT&T', iccid=False,
              product='Internal 5GE-AM (eSIM)',
              error_text='CPPM failed: no carrier',
              diag={'EUICC_SGP22': 'TRUE', 'EID': SENSITIVE[4],
                    'EUICC_PROF1': '{},AT&T,1,E400-eSIM,AT&T,2,'
                                   '0000000000000000'.format(SENSITIVE[5]),
                    'SERIAL_NUM': SENSITIVE[6]})
    return dev
