"""Read-only WBOND runtime analysis (v0.8.1, provenance fields v0.8.2).

GET /api/analytics/wbond: a pure function over data the service already
holds. It reuses the validated topology relationships (topology.build) and
adds the normalized runtime fields from the cached bonding snapshot. It
collects nothing, stores nothing and writes nothing.

Configured values and observed runtime values are kept apart:
  config    configured_weight (what was configured)
  observed  link/attach state, router-reported current weights, router-
            reported current flow counts, the router's loss indicator

What is deliberately NOT here: traffic distribution inferred from weights,
bandwidth utilization, bonding efficiency, any "carrying traffic" claim and
any packet-loss percentage. `loss_indicator` is the router's raw has_loss
value, passed through unchanged. A measurement the router did not report is
None, never 0.

Python 3.8 compatible.
"""
from . import normalize
from . import topology

SCHEMA_VERSION = 1

NOTES = (
    'Current weights are values reported by the router. They are not traffic '
    'shares and traffic distribution is not inferred from them.',
    'Flow counts are the router\'s current flow populations per direction, '
    'not byte or packet counts.',
    'An attached member is not evidence that it is carrying user traffic.',
    'loss_indicator is the raw has_loss value reported by the router. It is '
    'not a packet-loss percentage.',
    'No bandwidth utilization or bonding efficiency is calculated.',
    'A null value means the router did not report it; it is not zero.',
    'config.configured_weight is a legacy name: its source is the runtime '
    'members_list config_weight. observed.runtime_config_weight is the same '
    'value, correctly labelled. Configured criterion weights are in '
    '/api/analytics/wbond_config.',
)


def _fresh_state(source):
    """fresh | stale | unknown for one source block of topology as_of."""
    if source.get('read_error'):
        return 'stale'
    if source.get('last_success_ts') is None:
        return 'unknown'
    return 'fresh'


def _data_state(as_of):
    """Overall state: worst of the sources a bond analysis depends on."""
    # primary_device does not feed the bond analysis; wan and bonding do.
    states = [_fresh_state(as_of['sources'][k]) for k in ('wan', 'bonding')]
    if 'stale' in states:
        return 'stale'
    if 'unknown' in states:
        return 'unknown'
    return 'fresh'


def _runtime_index(snapshot):
    """{bond iface: {tunnel_iface: normalized member}} plus bond records."""
    bonding = snapshot.get('bonding') if isinstance(
        snapshot.get('bonding'), dict) else {}
    out = {}
    for b in (bonding.get('bonds') or []):
        if isinstance(b, dict):
            out[b.get('iface')] = (b, {
                m.get('tunnel_iface'): m for m in (b.get('members') or [])
                if isinstance(m, dict)})
    return out


def build(snapshot, raw_devices, health):
    """Compose the WBOND runtime analysis.

    Same inputs as topology.build; relationships come from it unchanged.
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    topo = topology.build(snapshot, raw_devices, health)
    runtime = _runtime_index(snapshot)
    phys_names = {p['uid']: p.get('display_name')
                  for p in topo['physical']}
    bonds = []
    for tb in topo['bonds']:
        rb, rmembers = runtime.get(tb['iface'], ({}, {}))
        obs = tb['observed']
        members = []
        for tm in tb['members']:
            rm = rmembers.get(tm['tunnel_iface'], {})
            puid = (tm['physical']['uid']
                    if tm['physical']['state'] == 'resolved' else None)
            members.append({
                'tunnel_iface': tm['tunnel_iface'],
                'overlay': tm['overlay'],
                'physical': tm['physical'],
                'physical_display_name': phys_names.get(puid),
                'underlay_consistency': tm['underlay_consistency'],
                # LEGACY name (kept for compatibility): its source is the
                # RUNTIME status members_list[].config_weight, not
                # config/sdwan_adv/wan_bonding. See observed.
                # runtime_config_weight and /api/analytics/wbond_config.
                'config': {
                    'configured_weight': rm.get('configured_weight')},
                'observed': {
                    'runtime_config_weight': rm.get('configured_weight'),
                    'link_state': rm.get('link_state'),
                    'link_attach_state': rm.get('link_attach_state'),
                    'current_download_weight': rm.get(
                        'current_download_weight'),
                    'current_upload_weight': rm.get('current_upload_weight'),
                    'download_flows': rm.get('download_flows'),
                    'upload_flows': rm.get('upload_flows'),
                    'loss_indicator': rm.get('has_loss'),
                },
            })
        bonds.append({
            'iface': tb['iface'],
            'wbond_device': tb['wbond_device'],
            'display_name': tb['display_name'],
            'observed': {
                'connection_state': obs['connection_state'],
                'link_state': obs['link_state'],
                'bonding_mode': obs['bonding_mode'],
                'negotiated_capabilities': obs['negotiated_capabilities'],
                'reported_member_count': obs['reported_member_count'],
                'listed_member_count': obs['listed_member_count'],
                'resolved_member_count': sum(
                    1 for m in members
                    if m['overlay']['state'] == 'resolved'
                    and m['physical']['state'] == 'resolved'),
                'member_count_matches': obs['member_count_matches'],
                'total_download_flows': rb.get('total_download_flows'),
                'total_upload_flows': rb.get('total_upload_flows'),
                # reported (NCOS) | summed (member flows) | unknown
                'total_download_flows_source': rb.get(
                    'total_download_flows_source') or 'unknown',
                'total_upload_flows_source': rb.get(
                    'total_upload_flows_source') or 'unknown',
            },
            'members': members,
        })
    as_of = topo['as_of']
    return {
        'schema_version': SCHEMA_VERSION,
        'as_of': as_of,
        'data_state': _data_state(as_of),
        'bonding_runtime': topo['bonding_runtime'],
        'bonds': bonds,
        'unresolved': [u for u in topo['unresolved']
                       if u['kind'] in ('bond_device', 'member_overlay',
                                        'member_physical',
                                        'member_underlay_conflict',
                                        'wbond_runtime')],
        'counters_additive': False,
        'notes': list(NOTES),
    }
