"""Field-aware RF validity, SINR scale provenance and cell-id canonicalization.

v0.8.4. Pure functions, no I/O, Python 3.8. Adapted (not copied wholesale)
from Speedtest Analyzer: `_canonical_cell_id`, `_normalize_plmn` and the
"aggregation SINR arrives in tenths of a dB" semantics. Speedtest's own RF
statistics retain numeric zeros and have no plausibility range, so the
validity rules here are NEW and intentionally stricter.

Principles
* The raw reading is ALWAYS preserved next to the interpreted value.
* A value is interpreted from the field it came from and the entry it belongs
  to, never from magnitude alone:
    - SINR scale comes from the source FIELD FAMILY and the token format
      (component fields are integer tenths of a dB; serving fields are
      decimal dB), corroborated against the serving value when the component
      is the serving carrier. There is NO global "above 50 means tenths" rule.
    - RSRQ -3 is not an invalid value; it is a placeholder only when the
      entry itself is unmeasured (0 MHz, inactive, or RSSI/RSRP both zero).
    - A raw 0 on a measured carrier is out of range (implausible), which is
      different from a 0 placeholder on an unmeasured entry.
* Range checks are plausibility bounds taken from the 3GPP REPORTING ranges
  (TS 36.133 / TS 38.133). They detect impossible readings; they do not
  certify that an in-range reading is accurate.
"""
import re

# --- validity vocabulary -----------------------------------------------------
VALID = 'valid'
MISSING = 'missing'
PLACEHOLDER_ZERO = 'placeholder_zero'          # 0 on an unmeasured entry
PLACEHOLDER_CONTEXT = 'placeholder_context'    # any value on an unmeasured entry
IMPLAUSIBLE = 'implausible_range'              # outside the reporting range
UNKNOWN_SCALE = 'unknown_scale'                # scale could not be established

VALIDITY_VALUES = (VALID, MISSING, PLACEHOLDER_ZERO, PLACEHOLDER_CONTEXT,
                   IMPLAUSIBLE, UNKNOWN_SCALE)

# SINR scale provenance (component carriers).
SCALE_DB_AS_REPORTED = 'db_as_reported'        # serving fields (decimal dB)
SCALE_DB_DECIMAL_TOKEN = 'db_decimal_token'    # component token had a decimal
SCALE_TENTHS_CORROBORATED = 'tenths_corroborated'
SCALE_TENTHS_FAMILY_CORROBORATED = 'tenths_family_corroborated'
SCALE_TENTHS_FAMILY_INFERRED = 'tenths_family_inferred'
SCALE_UNKNOWN = 'unknown'

BOUNDS_BASIS = ('3GPP reporting ranges (TS 36.133 LTE, TS 38.133 NR); '
                'plausibility only, not a certification of accuracy')

# (low, high) inclusive, dBm / dB.
RANGES = {
    'LTE': {'rsrp': (-140.0, -44.0), 'rsrq': (-34.0, 2.5),
            'sinr': (-23.0, 40.0), 'rssi': (-120.0, -20.0)},
    'NR': {'rsrp': (-156.0, -31.0), 'rsrq': (-43.0, 20.0),
           'sinr': (-23.0, 40.0), 'rssi': (-120.0, -20.0)},
}

# When a component IS the serving carrier, its scaled SINR is compared with
# the serving SINR. Component and serving values are separate measurements
# (a live AT&T LTE carrier differed by 8.3 dB), so two tiers are used:
#   <= CORROBORATION_TOLERANCE_DB : the tenths reading is corroborated;
#   <= CONFLICT_DB                : consistent but NOT corroborating (the scale
#                                   stays family-inferred, the difference is
#                                   reported);
#   >  CONFLICT_DB                : the tenths reading is contradicted, the
#                                   scale is unknown and the value withheld.
CORROBORATION_TOLERANCE_DB = 5.0
CONFLICT_DB = 10.0

_EMPTY = ('', 'none', 'n/a', 'unknown', '--', 'not registered',
          'not available', 'not reported', 'unavailable')
_NUM_RE = re.compile(r'^[+-]?\d+(\.\d+)?$')


def _ranges(rat):
    return RANGES['NR' if str(rat or '').upper() == 'NR' else 'LTE']


def parse_token(value):
    """Raw diagnostics value -> (number | None, has_decimal_point).

    Never raises. Empty, sentinel, bool and non-finite values give
    (None, False). A missing value is never turned into 0.
    """
    if value is None or isinstance(value, bool):
        return None, False
    if isinstance(value, (int, float)):
        num = float(value)
        if num != num or num in (float('inf'), float('-inf')):
            return None, False
        return num, isinstance(value, float) and num != int(num)
    text = str(value).strip()
    if text.lower() in _EMPTY:
        return None, False
    if not _NUM_RE.match(text):
        return None, False
    return float(text), '.' in text


def in_range(metric, value, rat):
    lo, hi = _ranges(rat)[metric]
    return lo <= value <= hi


def entry_unmeasured(active, bandwidth_mhz, rssi, rsrp):
    """Why a component entry carries no usable measurement, else None.

    Evidence is entry-local: a 0 MHz carrier, an inactive carrier, or an
    entry whose RSSI is 0 while RSRP is also 0/absent. RSSI is the
    companion field SD-WAN Analyzer v0.8.3 ignored.
    """
    if active is False:
        return 'inactive'
    if bandwidth_mhz is not None and bandwidth_mhz == 0:
        return 'zero_bandwidth'
    if rssi is not None and rssi == 0 and (rsrp is None or rsrp == 0):
        return 'rssi_zero'
    return None


def validate_metric(metric, raw, rat, unmeasured_reason=None):
    """-> (value | None, validity). `raw` is already a number or None."""
    if raw is None:
        return None, MISSING
    if unmeasured_reason is not None:
        return None, (PLACEHOLDER_ZERO if raw == 0 else PLACEHOLDER_CONTEXT)
    if not in_range(metric, raw, rat):
        return None, IMPLAUSIBLE
    return raw, VALID


def serving_metric(raw_text, metric, rat):
    """Serving-level RSRP/RSRQ/SINR (already decimal dB/dBm).

    -> {'value', 'raw', 'validity'}. `raw` is the parsed number as reported.
    """
    num, _ = parse_token(raw_text)
    val, validity = validate_metric(metric, num, rat)
    return {'value': val, 'raw': num, 'validity': validity}


def component_sinr(raw_text, rat, unmeasured_reason=None,
                   serving_ref_db=None):
    """Component-carrier SINR with explicit scale provenance.

    -> {'value', 'raw', 'validity', 'scale', 'ref_diff_db'}.

    Rules (source semantics, not magnitude):
    * decimal token -> already dB (scale db_decimal_token);
    * integer token -> tenths of a dB (Speedtest-documented semantics for the
      indexed component family, corroborated live where the component IS the
      serving carrier). With a serving reference of the same carrier: within
      CORROBORATION_TOLERANCE_DB -> tenths_corroborated; up to CONFLICT_DB ->
      still family-inferred (difference reported); beyond CONFLICT_DB the
      reading is contradicted: scale unknown, value withheld;
      without a reference the scale is tenths_family_inferred;
    * the interpreted value is range-checked after scaling.
    """
    num, has_dec = parse_token(raw_text)
    out = {'value': None, 'raw': num, 'validity': MISSING,
           'scale': None, 'ref_diff_db': None}
    if num is None:
        return out
    if unmeasured_reason is not None:
        out['validity'] = (PLACEHOLDER_ZERO if num == 0
                           else PLACEHOLDER_CONTEXT)
        return out
    if has_dec:
        scaled, scale = num, SCALE_DB_DECIMAL_TOKEN
    else:
        scaled, scale = num / 10.0, SCALE_TENTHS_FAMILY_INFERRED
        if serving_ref_db is not None:
            d_t = abs(scaled - serving_ref_db)
            d_raw = abs(num - serving_ref_db)
            out['ref_diff_db'] = round(d_t, 1)
            if d_t <= CORROBORATION_TOLERANCE_DB:
                scale = SCALE_TENTHS_CORROBORATED
            elif d_t <= CONFLICT_DB:
                scale = SCALE_TENTHS_FAMILY_INFERRED
            else:
                # The reference does not support the tenths reading.
                out['scale'] = SCALE_UNKNOWN
                out['validity'] = UNKNOWN_SCALE
                out['ref_conflict_unscaled_diff_db'] = round(d_raw, 1)
                return out
    out['scale'] = scale
    scaled = round(scaled, 1)
    if not in_range('sinr', scaled, rat):
        out['validity'] = IMPLAUSIBLE
        return out
    out['value'] = scaled
    out['validity'] = VALID
    return out


def upgrade_family_scale(entries):
    """If any component in one snapshot was corroborated, the others that
    were only family-inferred become tenths_family_corroborated. Mutates and
    returns `entries` (list of component_sinr results)."""
    if any(e.get('scale') == SCALE_TENTHS_CORROBORATED for e in entries):
        for e in entries:
            if e.get('scale') == SCALE_TENTHS_FAMILY_INFERRED:
                e['scale'] = SCALE_TENTHS_FAMILY_CORROBORATED
    return entries


# --- serving-cell identity ---------------------------------------------------

def canonical_cell_id(value):
    """Canonical positive decimal serving-cell id string, or ''.

    Adapted from Speedtest `_canonical_cell_id`. NCOS emits forms such as
    "6643981 (0x65610d)"; the leading decimal is preferred so formatting
    differences bucket alike. Never invents an id from PCI/TAC/band.
    """
    if value is None or isinstance(value, bool):
        return ''
    text = str(value).strip()
    if text.lower() in _EMPTY:
        return ''
    m = re.match(r'^\s*(\d+)\b', text)
    if m:
        n = int(m.group(1))
        # "0x..." hex forms start with a digit 0: handle explicitly first.
        if not re.match(r'^\s*0[xX]', text):
            return str(n) if n > 0 else ''
    m = re.match(r'^\s*0[xX]([0-9a-fA-F]+)\b', text)
    if m:
        n = int(m.group(1), 16)
        return str(n) if n > 0 else ''
    return ''


def normalize_plmn(value):
    """Digits-only PLMN (5 or 6 digits), else ''."""
    if value is None:
        return ''
    digits = ''.join(ch for ch in str(value) if ch.isdigit())
    return digits if len(digits) in (5, 6) else ''


# --- NCOS-reported health ----------------------------------------------------

def modem_health(status):
    """NCOS `status` health fields, validated, or None when none present.

    cellular_health_score and signal_strength are reported by NCOS; their
    derivation is NCOS's, not computed here. Out-of-range numbers are
    withheld (raw kept) rather than clamped.
    """
    if not isinstance(status, dict):
        return None
    out = {}
    present = False
    score, _ = parse_token(status.get('cellular_health_score'))
    if score is not None:
        present = True
        ok = 0.0 <= score <= 100.0
        out['score'] = round(score, 2) if ok else None
        if not ok:
            out['score_raw'] = score
    cat = status.get('cellular_health_category')
    if isinstance(cat, str) and cat.strip().lower() not in _EMPTY:
        present = True
        out['category'] = cat.strip().lower()[:24]
    sig, _ = parse_token(status.get('signal_strength'))
    if sig is not None:
        present = True
        ok = 0.0 <= sig <= 100.0
        out['signal_strength'] = int(round(sig)) if ok else None
        if not ok:
            out['signal_strength_raw'] = sig
    if not present:
        return None
    out['source'] = 'ncos_status'
    return out
