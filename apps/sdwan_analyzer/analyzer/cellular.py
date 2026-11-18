"""Cellular / carrier-aggregation telemetry parsing for SD-WAN Analyzer.

The carrier parsing below (everything between the COPIED markers) is copied
from the validated logic in apps/speedtest_analyzer/speedtest_web.py
(_parse_band_name, _parse_bandwidth_value, _dedupe_active_carriers,
_parse_active_carriers, _determine_service_mode, _build_carrier_summary and
their helpers). It is copied, NOT imported, so the two apps stay independent.
Not carried over: _parse_aggregation_cells / _CA_KEY_RE (UI display labels for
Speedtest Analyzer; the telemetry model needs only the deduped carriers).

PRIVACY: normalize_cellular() emits a fixed whitelist of fields. IMEI, IMSI,
ICCID, MDN, GPS and secrets are never read into the output. Raw CELL_ID /
NR_CELL_ID are never stored; they are folded into a one-way local hash
(cell_key_hash).
"""

import hashlib
import re

# Diagnostics keys per normalized field, in priority order (subset of the
# Speedtest Analyzer _DIAG_FIELD_CANDIDATES that this module consumes).
_DIAG_FIELD_CANDIDATES = {
    'carrier': ('CARRID',),
    'rsrp': ('RSRP',),
    'rsrq': ('RSRQ',),
    'sinr': ('SINR',),
    'rsrp_5g': ('RSRP_5G',),
    'rsrq_5g': ('RSRQ_5G',),
    'sinr_5g': ('SINR_5G',),
    'nr_cell_id': ('NR_CELL_ID',),
    'cell_id': ('CELL_ID',),
    'phy_cell_id': ('PHY_CELL_ID',),
    'phy_cell_id_5g': ('PHY_CELL_ID_5G', 'PCI_5G'),
    'rf_band': ('RFBAND',),
    'rf_band_5g': ('BAND_5G', 'RFBAND_5G'),
}

# --- COPIED from speedtest_analyzer/speedtest_web.py (validated) ----------

def _parse_band_name(band_str):
    """Extract band number and determine RAT from a band string.

    RAT classification is based on band naming convention:
    - "Band nXX" or "nXX" = NR
    - "Band XX" or "BXX" = LTE

    Returns:
        Tuple of (rat, band_str_cleaned). rat is 'NR' or 'LTE'.
    """
    if not band_str:
        return None, None
    s = str(band_str).strip()
    # Check for NR band (n prefix)
    if re.match(r'(?i)^(band\s+)?n\d+', s):
        return 'NR', s
    # LTE band
    if re.match(r'(?i)^(band\s+)?\d+|^B\d+', s):
        return 'LTE', s
    return None, s


def _parse_bandwidth_value(bw_str):
    """Parse bandwidth string to numeric MHz value.

    Returns float MHz or 0 if unparseable. Preserves 0 MHz explicitly.
    """
    if bw_str is None:
        return None
    s = str(bw_str).strip()
    if not s:
        return None
    # Extract numeric value
    m = re.search(r'(\d+(?:\.\d+)?)', s)
    if m:
        return float(m.group(1))
    return None


def _normalize_carrier(role, rat, band, bandwidth_mhz, channel, active,
                       rsrp, rsrq, sinr, pci):
    """Build a normalized carrier dict."""
    carrier = {
        'role': role,
        'rat': rat,
        'band': band,
        'bandwidth_mhz': bandwidth_mhz,
        'channel': channel,
        'active': active,
        'rsrp': rsrp,
        'rsrq': rsrq,
        'sinr': sinr,
        'pci': pci,
    }
    return carrier


def _carrier_identity(carrier):
    """Return normalized carrier identity fields for deduplication."""
    rat = str(carrier.get('rat') or '').strip().upper()

    band = str(carrier.get('band') or '').strip().upper()
    band = re.sub(
        r'^BAND\s+',
        '',
        band,
        flags=re.IGNORECASE
    ).strip()

    channel = carrier.get('channel')
    if channel is not None:
        channel = str(channel).strip()
        if not channel:
            channel = None

    bandwidth = carrier.get('bandwidth_mhz')

    return rat, band, channel, bandwidth


def _carrier_role_rank(carrier):
    """Rank how explicitly NCOS identifies a serving-cell role.

    Higher-ranked roles are preferred when multiple NCOS diagnostics
    representations describe the same physical carrier.
    """
    role = str(carrier.get('role') or '').strip().lower()

    # Explicit primary serving cell.
    if role == 'pcell':
        return 40

    # Explicit indexed secondary serving cells from either known schema.
    if role.startswith('scell') or role.startswith('lte_scell'):
        return 30

    # Direct NR diagnostics identify a real NR carrier but do not provide
    # the same explicit serving-cell relationship as PCELL/SCELL fields.
    if role == 'nr_primary':
        return 20

    return 10


def _merge_carrier_records(existing, incoming):
    """Merge duplicate records while preserving richer diagnostics.

    Prefer the record with the strongest explicit NCOS serving-cell role,
    then fill any missing diagnostics from the alternate representation.
    This keeps deduplication order-independent while preserving PCell/SCell
    information when the same carrier is also exposed through direct fields.
    """
    if _carrier_role_rank(incoming) > _carrier_role_rank(existing):
        merged = dict(incoming)
        alternate = existing
    else:
        merged = dict(existing)
        alternate = incoming

    for key, value in alternate.items():
        current = merged.get(key)

        if (current is None or current == '') and value is not None:
            merged[key] = value

    return merged


def _dedupe_active_carriers(carriers):
    """Deduplicate carriers exposed through multiple NCOS schemas.

    Deduplication is order-independent.

    Carriers are first grouped by RAT + band + bandwidth. Within each group:

    - Records with the same explicit channel are duplicates and are merged.
    - Records with different explicit channels are always preserved.
    - A channel-less record may merge with an explicit-channel record only
      when exactly one explicit channel exists in that group.
    - If multiple explicit channels exist, channel-less records are preserved
      because their physical carrier identity is ambiguous.
    """
    active_carriers = [
        carrier for carrier in carriers
        if carrier.get('active')
    ]

    groups = {}

    for carrier in active_carriers:
        rat, band, channel, bandwidth = _carrier_identity(carrier)
        base_key = (rat, band, bandwidth)

        if base_key not in groups:
            groups[base_key] = []

        groups[base_key].append(carrier)

    deduped = []

    for group in groups.values():
        explicit_by_channel = {}
        channel_less = []

        # First resolve all records that explicitly report a channel.
        for carrier in group:
            rat, band, channel, bandwidth = _carrier_identity(carrier)

            if channel is None:
                channel_less.append(carrier)
                continue

            if channel in explicit_by_channel:
                explicit_by_channel[channel] = _merge_carrier_records(
                    explicit_by_channel[channel],
                    carrier
                )
            else:
                explicit_by_channel[channel] = carrier

        explicit_records = list(explicit_by_channel.values())

        if len(explicit_records) == 1:
            # Only one explicit physical carrier exists for this
            # RAT/band/bandwidth combination. Channel-less representations
            # can therefore be safely merged into it.
            merged = explicit_records[0]

            for carrier in channel_less:
                merged = _merge_carrier_records(
                    merged,
                    carrier
                )

            deduped.append(merged)

        elif len(explicit_records) > 1:
            # Multiple physical carriers share the same RAT/band/bandwidth.
            # Preserve every explicit channel. A channel-less PCell record
            # can still be safely merged when exactly one explicit PCell
            # exists in the group; its role identifies the serving primary.
            # Other channel-less records remain separate because their
            # physical carrier identity is ambiguous.
            explicit_pcells = [
                carrier for carrier in explicit_records
                if carrier.get('role') == 'pcell'
            ]

            remaining_channel_less = []

            for carrier in channel_less:
                if (
                    carrier.get('role') == 'pcell'
                    and len(explicit_pcells) == 1
                ):
                    target = explicit_pcells[0]
                    merged = _merge_carrier_records(
                        target,
                        carrier
                    )

                    target_index = explicit_records.index(target)
                    explicit_records[target_index] = merged
                    explicit_pcells[0] = merged
                else:
                    remaining_channel_less.append(carrier)

            deduped.extend(explicit_records)

            if remaining_channel_less:
                merged_missing = remaining_channel_less[0]

                for carrier in remaining_channel_less[1:]:
                    merged_missing = _merge_carrier_records(
                        merged_missing,
                        carrier
                    )

                deduped.append(merged_missing)

        else:
            # No explicit channel is available. Multiple representations with
            # the same RAT/band/bandwidth cannot be distinguished, so treat
            # them as duplicate views of one carrier.
            if channel_less:
                merged = channel_less[0]

                for carrier in channel_less[1:]:
                    merged = _merge_carrier_records(
                        merged,
                        carrier
                    )

                deduped.append(merged)

    return deduped


def _parse_active_carriers(diagnostics):
    """Parse all active serving carriers from modem diagnostics.

    Discovers carriers from all known field families without
    model/carrier-specific branching. Deduplicates carriers that appear
    in multiple field families simultaneously.

    Args:
        diagnostics: Flat dict of modem diagnostic fields.

    Returns:
        List of normalized carrier dicts, deduplicated.
    """
    if not diagnostics or not isinstance(diagnostics, dict):
        return []

    carriers = []
    service_mode = _determine_service_mode(diagnostics)

    def _get(key):
        v = diagnostics.get(key)
        if v is None or str(v).strip().lower() in ('', 'none', 'n/a', 'unknown', '--'):
            return None
        return v

    def _is_active(val):
        """Check if an ACTIVE field indicates the carrier is active."""
        if val is None:
            return True  # No active field means assume active
        s = str(val).strip().lower()
        return s in ('active', '1', 'true', 'yes')

    # --- Family A: Generic serving-radio fields ---

    rfband = _get('RFBAND')

    if rfband:
        rat, band = _parse_band_name(rfband)

        if rat:
            if rat == 'NR':
                bw = _parse_bandwidth_value(
                    _get('RFBANDWIDTH')
                    or _get('RFBANDWIDTH_5G')
                )

                role = (
                    'pcell'
                    if service_mode == '5G SA'
                    else 'nr_primary'
                )

                rsrp = _get('RSRP_5G')
                rsrq = _get('RSRQ_5G')
                sinr = _get('SINR_5G')
                pci = _get('PHY_CELL_ID_5G')

            else:
                bw = _parse_bandwidth_value(
                    _get('LTEBANDWIDTH')
                )

                role = 'pcell'
                rsrp = _get('RSRP')
                rsrq = _get('RSRQ')
                sinr = _get('SINR')
                pci = _get('PHY_CELL_ID')

            carriers.append(_normalize_carrier(
                role=role,
                rat=rat,
                band=rfband,
                bandwidth_mhz=bw,
                channel=_get('RFCHANNEL'),
                active=True,
                rsrp=rsrp,
                rsrq=rsrq,
                sinr=sinr,
                pci=pci
            ))

    # --- Family B: Direct NR fields ---
    rfband_5g = _get('RFBAND_5G')
    if rfband_5g:
        rat, band = _parse_band_name(rfband_5g)
        if rat:
            bw = _parse_bandwidth_value(_get('RFBANDWIDTH_5G'))
            carriers.append(_normalize_carrier(
                role=(
                    'pcell'
                    if service_mode == '5G SA' and rat == 'NR'
                    else 'nr_primary'
                ), rat=rat, band=rfband_5g,
                bandwidth_mhz=bw,
                channel=_get('RFCHANNEL_5G'),
                active=True,
                rsrp=_get('RSRP_5G'), rsrq=_get('RSRQ_5G'),
                sinr=_get('SINR_5G'), pci=_get('PHY_CELL_ID_5G')
            ))

    # --- Family C: Indexed PCELL/SCELL ---
    # BAND_5G_PCELL, BANDWIDTH_5G_PCELL, etc.
    pcell_band = _get('BAND_5G_PCELL')
    if pcell_band:
        rat, band = _parse_band_name(pcell_band)
        if rat:
            bw = _parse_bandwidth_value(_get('BANDWIDTH_5G_PCELL'))
            active_val = _get('ACTIVE_5G_PCELL')
            carriers.append(_normalize_carrier(
                role='pcell', rat=rat, band=pcell_band,
                bandwidth_mhz=bw,
                channel=_get('CHANNEL_5G_PCELL'),
                active=_is_active(active_val),
                rsrp=_get('RSRP_5G_PCELL'), rsrq=_get('RSRQ_5G_PCELL'),
                sinr=_get('SINR_5G_PCELL'), pci=None
            ))

    # Discover indexed _5G_ secondary cells from keys actually returned
    # by NCOS. Do not assume contiguous indexes or a maximum SCELL number.
    scell_indexes = set()
    for raw_key in diagnostics:
        match = re.match(r'^BAND_5G_SCELL(\d+)$', str(raw_key).upper())
        if match:
            scell_indexes.add(int(match.group(1)))

    for scell_idx in sorted(scell_indexes):
        band_val = _get(f'BAND_5G_SCELL{scell_idx}')
        if band_val is None:
            continue

        rat, band = _parse_band_name(band_val)
        if not rat:
            continue

        bw = _parse_bandwidth_value(
            _get(f'BANDWIDTH_5G_SCELL{scell_idx}'))
        active_val = _get(f'ACTIVE_5G_SCELL{scell_idx}')

        carriers.append(_normalize_carrier(
            role=f'scell{scell_idx}',
            rat=rat,
            band=band_val,
            bandwidth_mhz=bw,
            channel=_get(f'CHANNEL_5G_SCELL{scell_idx}'),
            active=_is_active(active_val),
            rsrp=_get(f'RSRP_5G_SCELL{scell_idx}'),
            rsrq=_get(f'RSRQ_5G_SCELL{scell_idx}'),
            sinr=_get(f'SINR_5G_SCELL{scell_idx}'),
            pci=None
        ))

    # --- Family D: Generic LTE secondary-cell family ---
    # Discover BAND_SCELL<n> keys directly so sparse indexes are supported
    # and no arbitrary maximum carrier number is imposed.
    lte_scell_indexes = set()
    for raw_key in diagnostics:
        match = re.match(r'^BAND_SCELL(\d+)$', str(raw_key).upper())
        if match:
            lte_scell_indexes.add(int(match.group(1)))

    for lte_scell_idx in sorted(lte_scell_indexes):
        band_val = _get(f'BAND_SCELL{lte_scell_idx}')
        if band_val is None:
            continue

        rat, band = _parse_band_name(band_val)
        if not rat:
            continue

        active_val = _get(f'ACTIVE_SCELL{lte_scell_idx}')
        if not _is_active(active_val):
            continue

        bw = _parse_bandwidth_value(
            _get(f'BANDWIDTH_SCELL{lte_scell_idx}'))

        carriers.append(_normalize_carrier(
            role=f'lte_scell{lte_scell_idx}',
            rat=rat,
            band=band_val,
            bandwidth_mhz=bw,
            channel=_get(f'CHANNEL_SCELL{lte_scell_idx}'),
            active=True,
            rsrp=None,
            rsrq=None,
            sinr=None,
            pci=None
        ))

    # --- Deduplication ---
    # Multiple NCOS schemas may expose the same physical serving carrier.
    return _dedupe_active_carriers(carriers)


def _determine_service_mode(diagnostics):
    """Determine user-friendly connection mode from diagnostics.

    Uses SRVC_TYPE / SRVC_TYPE_DETAILS to distinguish LTE, 5G NSA, 5G SA.
    Does not rely solely on MODEMSYSMODE because some NSA devices report
    MODEMSYSMODE=LTE even when on 5G NSA.

    Returns:
        String like 'LTE', '5G NSA', '5G SA', or '' if unknown.
    """
    if not diagnostics:
        return ''
    try:
        srvc = str(diagnostics.get('SRVC_TYPE', '') or '').upper()
        details = str(diagnostics.get('SRVC_TYPE_DETAILS', '') or '').upper()
        sysmode = str(diagnostics.get('MODEMSYSMODE', '') or '').upper()
        serdis = str(diagnostics.get('SERDIS', '') or '').upper()

        # Check for 5G SA first
        if 'SA' in details and 'NSA' not in details:
            return '5G SA'
        if '5G-SA' in srvc or '5G SA' in srvc:
            return '5G SA'
        if 'NR5G-SA' in serdis:
            return '5G SA'

        # Check for 5G NSA
        if 'NSA' in details or 'NR' in details:
            return '5G NSA'
        if '5G' in srvc or 'NR' in srvc:
            return '5G NSA'
        if 'ENDC' in details or 'EN-DC' in details:
            return '5G NSA'
        if 'NR5G' in serdis or '5G' in serdis:
            return '5G NSA'
        if sysmode == 'NR5G-NSA' or 'NR' in sysmode:
            return '5G NSA'

        # LTE
        if 'LTE' in srvc or 'LTE' in sysmode or 'LTE' in serdis:
            return 'LTE'

        return ''
    except Exception:
        return ''


def _build_carrier_summary(carriers):
    """Build a summary of active carriers.

    Returns dict with:
        carrier_count: int
        bands: str (e.g. 'B66 + n41 + n41')
        bandwidth_mhz: float (sum of >0 MHz carriers)
        zero_mhz_count: int (carriers reporting 0 MHz)
    """
    if not carriers:
        return {
            'carrier_count': 0,
            'bands': '',
            'bandwidth_mhz': 0,
            'zero_mhz_count': 0
        }

    bands = []
    total_bw = 0.0
    zero_count = 0

    for c in carriers:
        # Format band name compactly.
        # Examples:
        #   "Band 66"  -> "B66"
        #   "B66"      -> "B66"
        #   "Band n41" -> "n41"
        #   "n41"      -> "n41"
        band_str = str(c.get('band') or '').strip()
        band_short = re.sub(
            r'^Band\s+',
            '',
            band_str,
            flags=re.IGNORECASE
        ).strip()

        if re.match(r'(?i)^n\d+', band_short):
            band_short = 'n' + band_short[1:]
        elif re.match(r'(?i)^b\d+', band_short):
            band_short = 'B' + band_short[1:]
        elif re.match(r'^\d+', band_short):
            band_short = 'B' + band_short

        bands.append(band_short or '?')

        bw = c.get('bandwidth_mhz')
        if bw is not None and bw > 0:
            total_bw += bw
        elif bw is not None and bw == 0:
            zero_count += 1
        # None bandwidth: carrier exists but BW unknown
        elif bw is None:
            pass

    return {
        'carrier_count': len(carriers),
        'bands': ' + '.join(bands),
        'bandwidth_mhz': round(total_bw, 1),
        'zero_mhz_count': zero_count
    }


# --- END COPIED ------------------------------------------------------------

_EMPTY = ('', 'none', 'n/a', 'unknown', '--')

# Prefix of the cell-key hash. Isolated here so the composition can change.
CELL_KEY_HASH_LEN = 16


def _present(value):
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _EMPTY
    return True


def _diag(diagnostics, field):
    """First present diagnostics value for a normalized field, else None."""
    for key in _DIAG_FIELD_CANDIDATES.get(field, ()):
        val = diagnostics.get(key)
        if _present(val):
            return val
    return None


def rf_number(value):
    """RF metric (RSRP/RSRQ/SINR, LTE or 5G) as a JSON number, else None.

    NCOS reports these as strings ("-89", "16.2"). Valid numeric text and
    numbers become float; missing/empty/unparseable/non-finite/bool become
    None. A missing value is NEVER turned into 0. Never raises.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value.strip() if isinstance(value, str) else value)
    except (TypeError, ValueError, OverflowError):
        return None
    if num != num or num in (float('inf'), float('-inf')):
        return None
    return num


def cell_key_hash(carrier, service_mode, cell_id, pci, band):
    """Deterministic shortened SHA-256 of the serving-cell identity.

    The exact composition lives in this one function so it can change later.
    Never uses Python's hash() (salted per process). Returns None when there
    is no cell id to derive from.
    """
    if not _present(cell_id):
        return None
    parts = [carrier, service_mode, cell_id, pci, band]
    text = '|'.join('' if p is None else str(p).strip() for p in parts)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:CELL_KEY_HASH_LEN]


def ca_signature(carriers):
    """Carrier-set signature from RAT/band/channel/bandwidth only.

    Role names (pcell/scell/nr_primary...) are deliberately ignored so the
    same physical carrier set yields the same signature across schemas.
    """
    items = []
    for c in carriers:
        rat, band, channel, bandwidth = _carrier_identity(c)
        items.append('{}:{}:{}:{}'.format(
            rat, band, channel or '', '' if bandwidth is None else bandwidth))
    return '|'.join(sorted(items))


def normalize_cellular(uid, diagnostics, metric_ts, collected_ts,
                       timestamp_source='collector'):
    """Normalize one cellular WAN's diagnostics into a telemetry record.

    Returns None for a missing/non-dict diagnostics object.
    """
    if not isinstance(diagnostics, dict) or not diagnostics:
        return None
    carriers = _parse_active_carriers(diagnostics)
    service_mode = _determine_service_mode(diagnostics)
    summary = _build_carrier_summary(carriers)
    sa = service_mode == '5G SA'

    carrier = _diag(diagnostics, 'carrier')
    serving = None
    for c in carriers:
        if c.get('role') == 'pcell':
            serving = c
            break
    if serving is None and carriers:
        serving = carriers[0]

    if sa:
        cell_id = (_diag(diagnostics, 'nr_cell_id')
                   or _diag(diagnostics, 'cell_id'))
        pci = _diag(diagnostics, 'phy_cell_id_5g') or _diag(
            diagnostics, 'phy_cell_id')
        band = _diag(diagnostics, 'rf_band_5g') or _diag(
            diagnostics, 'rf_band')
    else:
        cell_id = _diag(diagnostics, 'cell_id')
        pci = _diag(diagnostics, 'phy_cell_id')
        band = _diag(diagnostics, 'rf_band')

    return {
        'uid': uid,
        'carrier': carrier,
        'service_mode': service_mode,
        'rsrp': rf_number(_diag(diagnostics, 'rsrp')),
        'rsrq': rf_number(_diag(diagnostics, 'rsrq')),
        'sinr': rf_number(_diag(diagnostics, 'sinr')),
        'rsrp_5g': rf_number(_diag(diagnostics, 'rsrp_5g')),
        'rsrq_5g': rf_number(_diag(diagnostics, 'rsrq_5g')),
        'sinr_5g': rf_number(_diag(diagnostics, 'sinr_5g')),
        'serving_band': serving.get('band') if serving else None,
        'serving_bandwidth_mhz': (serving.get('bandwidth_mhz')
                                  if serving else None),
        'carrier_count': summary['carrier_count'],
        'total_active_mhz': summary['bandwidth_mhz'],
        'zero_mhz_count': summary['zero_mhz_count'],
        'carriers': [{
            'role': c.get('role'),
            'rat': c.get('rat'),
            'band': c.get('band'),
            'bandwidth_mhz': c.get('bandwidth_mhz'),
            'channel': c.get('channel'),
            'pci': c.get('pci'),
            'active': c.get('active'),
        } for c in carriers],
        'ca_signature': ca_signature(carriers),
        'cell_key_hash': cell_key_hash(carrier, service_mode, cell_id, pci,
                                       band),
        'metric_ts': metric_ts,
        'collected_ts': collected_ts,
        'timestamp_source': timestamp_source,
    }
