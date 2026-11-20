"""Sanitized real-device cellular fixtures for v0.8.4 regression tests.

Captured read-only from the lab R1900 and R2400 (NCOS 7.26.81) on
2026-11-20 and reduced to RF / carrier / service fields. NO subscriber or
hardware identifiers are present; CELL_ID values are SYNTHETIC. Values are
the strings NCOS reported (everything is a string, including numbers).

AT&T_RSRP_ZERO is a constructed variant: v0.8.3 history recorded an AT&T
5 MHz LTE SCell with RSRP 0.0 labelled valid; the raw RSSI of that sample
was not captured, so the variant pairs the raw RSRP '0' with a measured RSSI.
"""
import copy

NSA_DETAILS = 'NSA - 5G Sub-6 GHz'

# R2400 Verizon: LTE anchor, LTE SCell, NR SCell, 0 MHz NR placeholder, plus
# serving-level fields that duplicate the PCell and the NR SCell1.
VERIZON = {
    'SRVC_TYPE': '5G', 'SRVC_TYPE_DETAILS': NSA_DETAILS, 'BAND': '0',
    'ACTIVE_PCELL': 'Not Registered',
    'ACTIVE_5G_PCELL': 'Active', 'ACTIVE_5G_SCELL0': 'Active',
    'ACTIVE_5G_SCELL1': 'Active', 'ACTIVE_5G_SCELL2': 'Active',
    'BANDWIDTH_5G_PCELL': '10 MHz', 'BANDWIDTH_5G_SCELL0': '10 MHz',
    'BANDWIDTH_5G_SCELL1': '60 MHz', 'BANDWIDTH_5G_SCELL2': '0 MHz',
    'BAND_5G_PCELL': 'Band 66', 'BAND_5G_SCELL0': 'Band 66',
    'BAND_5G_SCELL1': 'Band n77', 'BAND_5G_SCELL2': 'Band n77',
    'CHANNEL_5G_PCELL': '67086', 'CHANNEL_5G_SCELL0': '66586',
    'CHANNEL_5G_SCELL1': '653952', 'CHANNEL_5G_SCELL2': '647328',
    'LTEBANDWIDTH': '10 MHz', 'PHY_CELL_ID': '371',
    'RFBAND': 'Band 66', 'RFCHANNEL': '67086',
    'RFBAND_5G': 'Band n77', 'RFCHANNEL_5G': '653952',
    'RFBANDWIDTH_5G': '60 MHz',
    'RSRP': '-79', 'RSRQ': '-16', 'SINR': '16.0',
    'RSRP_5G': '-90', 'RSRQ_5G': '-11', 'SINR_5G': '26.0',
    'RSRP_5G_PCELL': '-79', 'RSRP_5G_SCELL0': '-78',
    'RSRP_5G_SCELL1': '-85', 'RSRP_5G_SCELL2': '0',
    'RSRQ_5G_PCELL': '-13', 'RSRQ_5G_SCELL0': '-3',
    'RSRQ_5G_SCELL1': '-10', 'RSRQ_5G_SCELL2': '-3',
    'RSSI_5G_PCELL': '-51', 'RSSI_5G_SCELL0': '-59',
    'RSSI_5G_SCELL1': '-75', 'RSSI_5G_SCELL2': '0',
    'SINR_5G_PCELL': '185', 'SINR_5G_SCELL1': '270', 'SINR_5G_SCELL2': '250',
    'CELL_ID': '6643981 (0x65610d)',
}
VERIZON_STATUS = {'connection_state': 'connected', 'signal_strength': '100',
                  'cellular_health_score': 82.0,
                  'cellular_health_category': 'excellent'}

# R2400 AT&T, NSA state: LTE PCell without RSRP, 5 MHz LTE SCell, 100 MHz NR
# SCell with a small integer SINR token (45), and a 0 MHz NR placeholder.
ATT_NSA = {
    'SRVC_TYPE': '5G', 'SRVC_TYPE_DETAILS': NSA_DETAILS, 'BAND': '0',
    'ACTIVE_PCELL': 'Not Registered',
    'ACTIVE_5G_PCELL': 'Active', 'ACTIVE_5G_SCELL0': 'Active',
    'ACTIVE_5G_SCELL1': 'Active', 'ACTIVE_5G_SCELL2': 'Active',
    'BANDWIDTH_5G_PCELL': '10 MHz', 'BANDWIDTH_5G_SCELL0': '5 MHz',
    'BANDWIDTH_5G_SCELL1': '100 MHz', 'BANDWIDTH_5G_SCELL2': '0 MHz',
    'BAND_5G_PCELL': 'Band 66', 'BAND_5G_SCELL0': 'Band 66',
    'BAND_5G_SCELL1': 'Band n77', 'BAND_5G_SCELL2': 'Band n77',
    'CHANNEL_5G_PCELL': '66936', 'CHANNEL_5G_SCELL0': '66661',
    'CHANNEL_5G_SCELL1': '630720', 'CHANNEL_5G_SCELL2': '658080',
    'LTEBANDWIDTH': '10 MHz', 'PHY_CELL_ID': '276',
    'RFBAND': 'Band 66', 'RFCHANNEL': '66936',
    'RSRP': '-108', 'RSRQ': '-15', 'SINR': '0.8', 'RSRP_5G': '-104',
    'RSRQ_5G': '-13',
    'RSRP_5G_PCELL': '', 'RSRP_5G_SCELL0': '-98', 'RSRP_5G_SCELL1': '-96',
    'RSRP_5G_SCELL2': '0',
    'RSRQ_5G_PCELL': '-14', 'RSRQ_5G_SCELL0': '-10', 'RSRQ_5G_SCELL1': '-11',
    'RSRQ_5G_SCELL2': '-3',
    'RSSI_5G_PCELL': '-69', 'RSSI_5G_SCELL0': '-74',
    'RSSI_5G_SCELL1': '-85', 'RSSI_5G_SCELL2': '0',
    'SINR_5G_PCELL': '91', 'SINR_5G_SCELL1': '45',
}
ATT_STATUS = {'connection_state': 'connected', 'signal_strength': '84',
              'cellular_health_score': 64.05,
              'cellular_health_category': 'good'}

# Constructed: raw RSRP '0' on the ACTIVE 5 MHz LTE SCell0 with a measured
# RSSI (see module docstring).
ATT_RSRP_ZERO = dict(ATT_NSA, RSRP_5G_SCELL0='0')

# R2400 AT&T, LTE-only state: serving LTE, "Not Registered" aggregation, no
# component RF at all.
ATT_LTE = {
    'SRVC_TYPE': '4G', 'SRVC_TYPE_DETAILS': 'None', 'BAND': '0',
    'MODEMSYSMODE': 'LTE',
    'ACTIVE_PCELL': 'Not Registered', 'ACTIVE_5G_PCELL': 'Not Registered',
    'LTEBANDWIDTH': '10 MHz', 'PHY_CELL_ID': '276',
    'RFBAND': 'Band 66', 'RFCHANNEL': '66936',
    'RSRP': '-107', 'RSRQ': '-15', 'SINR': '0.8',
    'CELL_ID': '6643981 (0x65610d)',
}

# R2400 T-Mobile: SCell0 and SCell1 report an IDENTICAL NR identity and the
# direct NR serving fields carry no channel.
TMOBILE = {
    'SRVC_TYPE': '5G', 'SRVC_TYPE_DETAILS': NSA_DETAILS, 'BAND': '0',
    'ACTIVE_PCELL': 'Not Registered',
    'ACTIVE_5G_PCELL': 'Active', 'ACTIVE_5G_SCELL0': 'Active',
    'ACTIVE_5G_SCELL1': 'Active',
    'BANDWIDTH_5G_PCELL': '10 MHz', 'BANDWIDTH_5G_SCELL0': '100 MHz',
    'BANDWIDTH_5G_SCELL1': '100 MHz',
    'BAND_5G_PCELL': 'Band 2', 'BAND_5G_SCELL0': 'Band n41',
    'BAND_5G_SCELL1': 'Band n41',
    'CHANNEL_5G_PCELL': '1001', 'CHANNEL_5G_SCELL0': '501390',
    'CHANNEL_5G_SCELL1': '501390',
    'LTEBANDWIDTH': '10 MHz', 'PHY_CELL_ID': '346', 'PHY_CELL_ID_5G': '624',
    'RFBAND': 'Band 2', 'RFCHANNEL': '1001',
    'RFBAND_5G': 'Band n41', 'RFBANDWIDTH_5G': '100 MHz',
    'RSRP': '-72', 'RSRQ': '-11', 'SINR': '26.4',
    'RSRP_5G': '-69', 'RSRQ_5G': '-11', 'SINR_5G': '30.5',
}

# R1900 T-Mobile: direct serving fields only (no indexed component RF),
# SCELL0 inactive.
R1900_TMOBILE = {
    'SRVC_TYPE': '5G', 'SRVC_TYPE_DETAILS': NSA_DETAILS, 'BAND': '0',
    'ACTIVE_PCELL': 'Active', 'ACTIVE_SCELL0': 'Inactive',
    'LTEBANDWIDTH': '10 MHz', 'PHY_CELL_ID': '353', 'PHY_CELL_ID_5G': '624',
    'RFBAND': 'Band 66', 'RFCHANNEL': '66736',
    'RFBAND_5G': 'Band n41', 'RFCHANNEL_5G': '528144',
    'RFBANDWIDTH_5G': '80 MHz',
    'RSRP': '-82', 'RSRQ': '-14', 'SINR': '20.0',
    'RSRP_5G': '-75', 'RSRQ_5G': '-11', 'SINR_5G': '33.0',
}


def copy_of(d, **over):
    out = copy.deepcopy(d)
    out.update(over)
    return out
