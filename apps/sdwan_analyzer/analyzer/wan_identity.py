"""Centralized WAN identity and classification (v1.0.2).

ONE authoritative, evidence-based identity record per NCOS WAN object. Every
other module (normalize, service, topology, wbond, cellular/steering
annotation, history) consumes these records; none of them classify, filter or
name WANs on their own.

    status/wan/devices  ->  wan_identity.build_inventory()  ->  identity records
                                                              -> normalize.wan_paths
                                                              -> snapshot / topology / wbond / APIs

Pure functions: no router I/O, no logging, no hidden mutable state beyond a
lazily loaded model catalog. Deterministic and unit-testable.

Reuse of Speedtest Analyzer (STA) logic
---------------------------------------
Concepts and naming conventions are ported from STA's validated
speedtest_web.py (_friendly_wan_name, _wan_is_cellular_device,
_carrier_abbreviation, _sim_slot_label, _satellite_wan_label,
_sdwan_underlay_uid, _get_model_family, _validation_captive_models,
_validation_match_model). They are re-implemented here against a private copy
of the model catalog (wan_hardware_catalog.json); there is no import of, or
runtime dependency on, another SDK application.

Identity axes (never conflated)
-------------------------------
  uid / iface / info_uid   immutable NCOS identifiers. Used for joins, history,
                           routing relationships. NEVER replaced by a name.
  display_name             presentation metadata only.
  object_class             physical | sdwan | wbond | unknown  (topology axis)
  kind                     cellular | ethernet | wifi | satellite | sdwan |
                           wbond | unknown                     (medium axis)
  classification           object_class, or 'empty_sim' for a confirmed empty
                           SIM-slot object (a physical object that is not
                           shown in the user-facing physical inventory).
  operational_state        connected | inactive | unknown, derived from the
                           raw NCOS connection_state and INDEPENDENT of SIM
                           presence.
  sim_presence             present | empty | unknown | not_applicable

Empty-SIM rule
--------------
A cellular object is `empty` ONLY on explicit NCOS indicators:
diagnostics.NOSIM == 'TRUE', diagnostics.PIN_STATUS == 'NOSIM', or
status.error_text naming NOSIM. Disconnected, standby, unregistered, no IP,
failing configuration, zero throughput, missing carrier name or a disabled
interface are NEVER evidence. Contradictory indicators (an empty indicator
alongside ICCID/registered-carrier/READY/eSIM-profile evidence) yield
`unknown` and the object is retained.

Privacy
-------
Sensitive values (ICCID, IMSI, IMEI, MDN, serial numbers, PINs, EID, profile
strings) are never copied into an identity record. Only the *presence* of an
ICCID or profile is used as evidence, and only evidence CODES are exported.
Free text taken from the device (carrier, model, error text) is sanitized and
length-limited before it leaves this module.

Python 3.8 compatible.
"""
import hashlib
import json
import os
import re
import threading

SCHEMA_VERSION = 1

# --- object classes (topology axis) --------------------------------------
OBJ_PHYSICAL = 'physical'
OBJ_SDWAN = 'sdwan'
OBJ_WBOND = 'wbond'
OBJ_UNKNOWN = 'unknown'
CLASS_EMPTY_SIM = 'empty_sim'

# Overlay/tunnel wan_type values that are NOT physical WAN paths.
OVERLAY_TYPES = ('sdwan', 'vpn', 'gre', 'ipsec')

# --- kinds (medium axis) --------------------------------------------------
KIND_CELLULAR = 'cellular'
KIND_ETHERNET = 'ethernet'
KIND_WIFI = 'wifi'
KIND_SATELLITE = 'satellite'
KIND_UNKNOWN = 'unknown'

# --- SIM / ownership / operational vocabularies ---------------------------
SIM_PRESENT = 'present'
SIM_EMPTY = 'empty'
SIM_UNKNOWN = 'unknown'
SIM_NOT_APPLICABLE = 'not_applicable'

SIMTYPE_PHYSICAL = 'physical'
SIMTYPE_ESIM = 'esim'
SIMTYPE_UNKNOWN = 'unknown'

OWNER_INTERNAL = 'internal'
OWNER_CAPTIVE = 'captive'
OWNER_UNKNOWN = 'unknown'

OP_CONNECTED = 'connected'
OP_INACTIVE = 'inactive'
OP_UNKNOWN = 'unknown'

UNDERLAY_RESOLVED = 'resolved'
UNDERLAY_UNRESOLVED = 'unresolved'

NOTES = (
    'Raw NCOS identifiers (uid, iface, info_uid) are the only join keys; '
    'display_name is presentation metadata.',
    'wans[] is the RAW inventory (every NCOS WAN object). The user-facing '
    'physical inventory is displayable_physical_uids; confirmed empty SIM '
    'slots (classification empty_sim) are in empty_sim_uids.',
    'operational_state is derived from connection_state and is independent '
    'of sim_presence: an inactive WAN with a populated SIM stays in the '
    'inventory.',
    'sim_presence is empty only on explicit NCOS indicators (NOSIM, '
    'PIN_STATUS NOSIM, error text NOSIM); otherwise present or unknown.',
    'classification_evidence lists evidence codes only; no subscriber or '
    'modem identifiers are included.',
    'Identity is versioned (wan_identity config version); observations and '
    'Recordings reference the version number, so historical identity is '
    'resolved as of the sample rather than from the current snapshot.',
)

# Fields safe to expose to the frontend / public API responses.
PUBLIC_FIELDS = (
    'uid', 'iface', 'info_uid', 'display_name', 'object_class',
    'classification', 'kind', 'connection_state', 'operational_state',
    'failure_reason', 'sim_presence', 'sim_type', 'sim_slot', 'carrier',
    'carrier_name', 'carrier_source', 'modem_owner', 'modem_model',
    'host_model', 'inventory_visible', 'underlay_uid', 'underlay_state',
    'underlay_available', 'classification_evidence',
)

# Fields that define a WAN's durable identity (versioned for history). The
# volatile operational fields (connection state, failure reason, IP) are
# deliberately excluded so a link flap never creates an identity version.
VERSIONED_FIELDS = (
    'display_name', 'object_class', 'classification', 'kind', 'sim_presence',
    'sim_type', 'sim_slot', 'carrier', 'modem_owner', 'modem_model',
    'underlay_uid',
)

_BLANK = ('', '--', 'unknown', 'none', 'n/a', 'not available', 'null')
_CELLULAR_TOKEN_RE = re.compile(
    r'(^|[^a-z0-9])(lte|4g|5g|nr5g|nr|cellular|wwan)([^a-z0-9]|$)')
_NOSIM_RE = re.compile(r'(?<![A-Z0-9])NOSIM(?![A-Z0-9])')
_LONG_DIGITS_RE = re.compile(r'\d{8,}')
_IPV4_RE = re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b')
_LABEL_OK_RE = re.compile(r"[^A-Za-z0-9 &.,'()/_+\-]")
_SPACE_RE = re.compile(r'\s+')
_SIM_SLOT_RE = re.compile(r'(?:SIM)?[\s_-]*([12])\b')


# ==========================================================================
# Small helpers
# ==========================================================================

def _s(value):
    """Stripped string of a possibly-None value ('' for None)."""
    return str(value if value is not None else '').strip()


def _dict(value):
    return value if isinstance(value, dict) else {}


def _present(value):
    """True for a meaningful device-reported value (not blank/sentinel)."""
    return _s(value).lower() not in _BLANK


def _truthy(value):
    return _s(value).upper() in ('TRUE', '1', 'YES')


def _falsy(value):
    return _s(value).upper() in ('FALSE', '0', 'NO')


def clean_label(value, limit=40):
    """Sanitize device-reported free text for display/export.

    Removes control and unusual characters, collapses whitespace, truncates,
    and rejects anything that looks like an identifier (a run of 8+ digits:
    IMEI/ICCID/IMSI/phone number). Returns None when nothing safe remains.
    """
    text = _s(value)
    if not text:
        return None
    text = _LABEL_OK_RE.sub('', text)
    text = _SPACE_RE.sub(' ', text).strip()
    if not text or _LONG_DIGITS_RE.search(text):
        return None
    return text[:limit].rstrip()


def clean_reason(value, limit=80):
    """Sanitize a failure reason: one line, no IPs, no long digit runs."""
    text = _s(value)
    if not text:
        return None
    text = _IPV4_RE.sub('[ip]', text)
    text = _LONG_DIGITS_RE.sub('[id]', text)
    text = re.sub(r'[^\x20-\x7e]', ' ', text)
    text = _SPACE_RE.sub(' ', text).strip()
    return text[:limit].rstrip() or None


def stable_hash(payload):
    """Deterministic short hash of a JSON-serializable payload."""
    try:
        blob = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                          default=str)
    except (TypeError, ValueError):
        blob = str(payload)
    return hashlib.sha1(blob.encode('utf-8')).hexdigest()[:16]


# ==========================================================================
# Hardware catalog (loaded once, cached)
# ==========================================================================

_CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'wan_hardware_catalog.json')


class Catalog(object):
    """Model names used to recognise host routers and captive modems."""

    def __init__(self, controllers=(), standalone=(), captive=(),
                 version=None, error=None):
        self.controllers = frozenset(str(m).upper() for m in controllers)
        self.families = frozenset(
            str(m).upper() for m in tuple(controllers) + tuple(standalone))
        self.captive_models = frozenset(str(m).upper() for m in captive)
        self.version = version
        self.error = error

    def info(self):
        return {'catalog_version': self.version, 'load_error': self.error,
                'controllers': len(self.controllers),
                'host_families': len(self.families),
                'captive_models': len(self.captive_models)}


_catalog = None
_catalog_lock = threading.Lock()


def _load_catalog(path):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or data.get('schema_version') != 1:
            raise ValueError('unsupported catalog schema')
        lists = {}
        for key in ('controllers', 'standalone_devices', 'captive_modems'):
            val = data.get(key)
            if (not isinstance(val, list) or
                    not all(isinstance(v, str) and v.strip() for v in val)):
                raise ValueError('{} must be a list of strings'.format(key))
            lists[key] = [v.strip() for v in val]
        return Catalog(lists['controllers'], lists['standalone_devices'],
                       lists['captive_modems'],
                       version=_s(data.get('catalog_version')) or None)
    except Exception as e:
        # Degrade to an empty catalog: models are simply not recognised
        # (no 'Internal' suffix, captive models taken verbatim from NCOS).
        return Catalog(error='{}: {}'.format(type(e).__name__, e))


def get_catalog():
    """The process-wide catalog; parsed exactly once."""
    global _catalog
    if _catalog is None:
        with _catalog_lock:
            if _catalog is None:
                _catalog = _load_catalog(_CATALOG_PATH)
    return _catalog


def set_catalog(catalog):
    """Install a catalog (tests / reload). None forces a re-read."""
    global _catalog
    with _catalog_lock:
        _catalog = catalog


# ==========================================================================
# STA-ported primitives
# ==========================================================================

def carrier_abbreviation(carrier):
    """Supported user-facing carrier abbreviation (ATT/VZW/TMO) or ''.

    Unknown carriers and MVNOs intentionally get no abbreviation (STA
    convention): the SIM slot remains the stable user-facing identifier.
    """
    lowered = _s(carrier).lower()
    normalized = re.sub(r'[^a-z0-9]+', '', lowered)
    if not normalized:
        return ''
    if normalized in ('att', 'attwireless') or 'firstnet' in normalized:
        return 'ATT'
    if normalized == 'vzw' or 'verizon' in normalized:
        return 'VZW'
    # 'T-Mobile' must start at a word boundary: a bare substring test also
    # matches 'Mint Mobile' ('mintmobile' contains 'tmobile'), an MVNO.
    # (Deliberate, documented difference from STA's substring match.)
    if normalized == 'tmo' or re.search(
            r'(?<![a-z0-9])t[^a-z0-9]*mobile', lowered):
        return 'TMO'
    return ''


def sim_slot_label(value):
    """Normalize an NCOS SIM value ('sim1', 'SIM 2', '2') to SIM1/SIM2."""
    text = _s(value).upper()
    if not text:
        return ''
    match = _SIM_SLOT_RE.search(text)
    return 'SIM' + match.group(1) if match else ''


def satellite_label(uid, iface):
    """Stable display label for a satellite WAN (last 4 chars of the uid)."""
    identity = _s(uid) or _s(iface)
    match = re.search(r'([a-zA-Z0-9]{4})$', identity)
    if match:
        return 'Satellite WAN-' + match.group(1).upper()
    return 'Satellite WAN'


def contains_token(text, token):
    """Match a model token without partial alphanumeric matches."""
    text = _s(text).upper()
    token = _s(token).upper()
    if not text or not token:
        return False
    return re.search(r'(?<![A-Z0-9])' + re.escape(token) + r'(?![A-Z0-9])',
                     text) is not None


def match_model(text, candidates):
    """Longest canonical model name found in text ('' when none)."""
    matches = [str(c).upper() for c in candidates if contains_token(text, c)]
    if not matches:
        return ''
    return max(matches, key=lambda item: (len(item), item))


def _scalar_strings(value, depth=0):
    """Flatten (depth <= 3) the string leaves of a remote-identity block."""
    if depth > 3:
        return []
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(_scalar_strings(item, depth + 1))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_scalar_strings(item, depth + 1))
        return out
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _identity_blob(uid, info, diagnostics):
    """Lower-case token-test string. Used for matching ONLY, never exported
    (it contains every diagnostics value)."""
    parts = [_s(uid), _s(info.get('iface')), _s(info.get('type')),
             _s(info.get('product'))]
    for v in diagnostics.values():
        if v is not None:
            parts.append(_s(v))
    return ' '.join(parts).lower()


def _is_satellite(blob):
    return 'starlink' in blob or 'satellite' in blob


# ==========================================================================
# Evidence extraction
# ==========================================================================

def empty_sim_indicators(diagnostics, status):
    """Evidence codes for an explicitly empty SIM slot (authoritative)."""
    diagnostics, status = _dict(diagnostics), _dict(status)
    codes = []
    if _truthy(diagnostics.get('NOSIM')):
        codes.append('diag_nosim_true')
    if _s(diagnostics.get('PIN_STATUS')).upper() == 'NOSIM':
        codes.append('diag_pin_status_nosim')
    if _NOSIM_RE.search(_s(status.get('error_text')).upper()):
        codes.append('status_error_text_nosim')
    return codes


def populated_sim_indicators(diagnostics):
    """Evidence codes that a SIM/eSIM profile IS present. Only the presence
    of a value is used; the value itself is never kept."""
    diagnostics = _dict(diagnostics)
    codes = []
    if _falsy(diagnostics.get('NOSIM')):
        codes.append('diag_nosim_false')
    if _present(diagnostics.get('ICCID')):
        codes.append('diag_iccid_present')
    if _present(diagnostics.get('CARRID')):
        codes.append('diag_registered_carrier')
    if _s(diagnostics.get('PIN_STATUS')).upper() == 'READY':
        codes.append('diag_pin_status_ready')
    return codes


def _euicc_profiles(diagnostics):
    """[(active_flag_str)] for each EUICC_PROF<n> value; contents dropped."""
    flags = []
    for key, value in diagnostics.items():
        if not str(key).upper().startswith('EUICC_PROF'):
            continue
        parts = _s(value).split(',')
        flags.append(parts[2].strip() if len(parts) > 2 else '')
    return flags


def esim_indicators(info, diagnostics):
    """Evidence codes for an eSIM-backed WAN.

    - the device's own model label says eSIM (e.g. 'Internal 5GE-AM (eSIM)')
    - an eUICC-capable module reports an ACTIVE operational profile
    Merely being eUICC-capable is not enough: such a module may be running
    a physical SIM.
    """
    info, diagnostics = _dict(info), _dict(diagnostics)
    codes = []
    if 'esim' in _s(info.get('product')).lower():
        codes.append('product_label_esim')
    if _truthy(diagnostics.get('EUICC_SGP22')) and '1' in _euicc_profiles(
            diagnostics):
        codes.append('euicc_active_profile')
    return codes


def operational_state(connection_state):
    """connected | inactive | unknown, from the RAW NCOS connection_state."""
    text = _s(connection_state).lower()
    if text == 'connected':
        return OP_CONNECTED
    if text in ('', 'unknown'):
        return OP_UNKNOWN
    return OP_INACTIVE


# ==========================================================================
# Classification
# ==========================================================================

def _is_cellular(uid, info, diagnostics, status):
    """Positive-evidence cellular test (STA _wan_is_cellular_device)
    extended with an explicit empty-SIM indicator, which is itself proof of
    a modem SIM slot."""
    if _is_satellite(_identity_blob(uid, info, diagnostics)):
        return False
    modem_identity = (_s(info.get('type')).lower() == 'mdm'
                      or _s(uid).lower().startswith('mdm-'))
    if not modem_identity:
        return False
    identity = _identity_blob(uid, info, diagnostics)
    carrier_present = _present(diagnostics.get('CARRID'))
    sim_slot = bool(sim_slot_label(info.get('sim')))
    service_token = _CELLULAR_TOKEN_RE.search(identity) is not None
    explicit_empty = bool(empty_sim_indicators(diagnostics, status))
    return carrier_present or sim_slot or service_token or explicit_empty


def classify_wan_kind(uid, device):
    """cellular | ethernet | wifi | satellite | unknown."""
    device = _dict(device)
    info = _dict(device.get('info'))
    diagnostics = _dict(device.get('diagnostics'))
    status = _dict(device.get('status'))
    blob = _identity_blob(uid, info, diagnostics)
    if _is_satellite(blob):
        return KIND_SATELLITE
    if _is_cellular(uid, info, diagnostics, status):
        return KIND_CELLULAR
    wan_type = _s(info.get('type')).lower()
    iface = _s(info.get('iface')).lower()
    if wan_type in ('wifi', 'wi-fi', 'wlan') or 'wifi' in blob:
        return KIND_WIFI
    if wan_type in ('ethernet', 'eth') or iface in ('wan', 'ethernet-wan'):
        return KIND_ETHERNET
    return KIND_UNKNOWN


def object_class(uid, device):
    """physical | sdwan | wbond | unknown (info.type first, uid prefix next)."""
    device = _dict(device)
    info = _dict(device.get('info'))
    wan_type = _s(info.get('type')).lower()
    u = _s(uid).lower()
    if wan_type == 'wbond' or u.startswith('wbond'):
        return OBJ_WBOND
    if wan_type == 'sdwan' or u.startswith('sdwan'):
        return OBJ_SDWAN
    if wan_type in ('mdm', 'ethernet', 'eth', 'wwan', 'wifi', 'wi-fi',
                    'wlan'):
        return OBJ_PHYSICAL
    if wan_type in OVERLAY_TYPES:
        return OBJ_UNKNOWN
    if classify_wan_kind(uid, device) in (KIND_CELLULAR, KIND_ETHERNET,
                                          KIND_WIFI, KIND_SATELLITE):
        return OBJ_PHYSICAL
    return OBJ_UNKNOWN


def sdwan_underlay_uid(device):
    """Dependent (underlay) WAN key of an SD-WAN device: active_dep_wandev,
    else the first dep_wandevs entry (list or dict), else ''."""
    status = _dict(_dict(device).get('status'))
    active = _s(status.get('active_dep_wandev'))
    if active:
        return active
    dep = status.get('dep_wandevs')
    if isinstance(dep, (list, dict)):
        for d in dep:
            d = _s(d)
            if d:
                return d
    return ''


def _sim_state(kind, info, status, diagnostics):
    """-> (presence, sim_type, evidence codes) for one WAN object."""
    if kind != KIND_CELLULAR:
        return SIM_NOT_APPLICABLE, None, []
    empty = empty_sim_indicators(diagnostics, status)
    populated = populated_sim_indicators(diagnostics)
    esim = esim_indicators(info, diagnostics)
    evidence = list(empty) + list(populated) + list(esim)

    if empty and (populated or esim):
        evidence.append('contradictory_sim_indicators')
        return SIM_UNKNOWN, SIMTYPE_UNKNOWN, evidence
    if empty:
        return SIM_EMPTY, SIMTYPE_PHYSICAL, evidence
    if esim:
        # An eSIM is embedded: it is present when the device reports an
        # active profile, otherwise its state is not established.
        presence = SIM_PRESENT if (
            'euicc_active_profile' in esim or populated or
            _present(diagnostics.get('HOMECARRID'))) else SIM_UNKNOWN
        return presence, SIMTYPE_ESIM, evidence
    if populated:
        euicc_capable = _truthy(diagnostics.get('EUICC_SGP22'))
        return (SIM_PRESENT,
                SIMTYPE_UNKNOWN if euicc_capable else SIMTYPE_PHYSICAL,
                evidence)
    return SIM_UNKNOWN, SIMTYPE_UNKNOWN, evidence


def _carrier(diagnostics, presence):
    """-> (abbreviation|None, name|None, source|None).

    Registered carrier (CARRID) first; HOMECARRID is the documented fallback
    for a populated but unregistered SIM. An empty slot has no carrier.
    """
    if presence in (SIM_EMPTY, SIM_NOT_APPLICABLE):
        return None, None, None
    for key, source in (('CARRID', 'registered'), ('HOMECARRID', 'home')):
        value = diagnostics.get(key)
        if _present(value):
            name = clean_label(value, 32)
            if name is None:
                continue
            return carrier_abbreviation(value) or None, name, source
    return None, None, None


def _modem_ownership(info, diagnostics, host_family, cat):
    """-> (owner, model|None, evidence codes). Never relies on UID prefix."""
    remote = info.get('remote')
    if remote is not None and not isinstance(remote, dict):
        return OWNER_UNKNOWN, None, ['remote_block_malformed']
    if not isinstance(info, dict) or not info:
        return OWNER_UNKNOWN, None, ['info_block_missing']
    remote = remote or {}
    product = remote.get('product_name')

    if remote and remote.get('internal_captive') is True:
        # NCOS models some internal modems (e.g. R2400) through the remote
        # data model. The explicit flag is authoritative: not captive.
        evidence = ['internal_captive_true']
        if match_model(_s(product), cat.captive_models):
            evidence.append('remote_product_matches_captive_catalog')
        return OWNER_INTERNAL, clean_label(product, 24), evidence

    if remote:
        raw_product = _s(product)
        model = match_model(raw_product, cat.captive_models)
        evidence = ['remote_block_present']
        if model:
            evidence.append('remote_product_catalog_match')
        elif raw_product:
            model = (clean_label(raw_product, 24) or '').upper()
            evidence.append('remote_product_unlisted')
        if not model:
            model = match_model(' | '.join(_scalar_strings(remote)),
                                cat.captive_models)
            if model:
                evidence.append('remote_text_catalog_match')
        if not model:
            evidence.append('remote_model_unknown')
        return OWNER_CAPTIVE, (model or None), evidence

    model = None
    for key in ('MFG_MDL', 'MDL'):
        if _present(diagnostics.get(key)):
            model = clean_label(diagnostics.get(key), 24)
            if model:
                break
    return OWNER_INTERNAL, model, ['no_remote_block']


def _owner_label(owner, model, host_family, host_model, cat):
    """STA ownership prefix: '<MODEL> Captive' / '<HOST> Internal' / ''."""
    if owner == OWNER_CAPTIVE:
        return (model + ' Captive') if model else 'Captive Modem'
    if owner == OWNER_INTERNAL:
        name = host_family or _s(host_model).upper()
        if name and host_family in cat.controllers:
            name += ' Internal'
        return name
    return ''


def _display_name_physical(uid, info, kind, sim, ctx):
    """STA-compatible name for a physical WAN. `sim` is the computed dict."""
    wan_type = _s(info.get('type')).lower()
    iface = _s(info.get('iface'))
    product = _s(info.get('product'))
    if kind == KIND_SATELLITE:
        return satellite_label(uid, iface)
    if kind == KIND_CELLULAR:
        slot = 'eSIM' if sim['sim_type'] == SIMTYPE_ESIM else (
            sim['sim_slot'] or '')
        connection = '-'.join(
            p for p in (sim['carrier'] or '', slot) if p) or 'Cellular WAN'
        owner = _owner_label(sim['modem_owner'], sim['modem_model'],
                             ctx['host_family'], ctx['host_model'],
                             ctx['catalog'])
        return owner + ' - ' + connection if owner else connection
    if kind == KIND_WIFI:
        return 'Wi-Fi as WAN'
    if kind == KIND_ETHERNET:
        return 'Ethernet WAN'
    return product or iface or _s(uid) or 'Unknown WAN'


def _context(host_model, catalog):
    cat = catalog if catalog is not None else get_catalog()
    host = clean_label(host_model, 40)
    host_up = host.upper() if host else None
    return {'catalog': cat, 'host_model': host_up,
            'host_family': match_model(host_up or '', cat.families) or ''}


def _identify(uid, device, ctx):
    """Identity record for one device (underlay fields filled later)."""
    device = _dict(device)
    info = _dict(device.get('info'))
    status = _dict(device.get('status'))
    diagnostics = _dict(device.get('diagnostics'))
    oclass = object_class(uid, device)
    kind = classify_wan_kind(uid, device)
    connection_state = _s(status.get('connection_state')) or 'unknown'
    op = operational_state(connection_state)

    rec = {
        'uid': _s(uid),
        'iface': _s(info.get('iface')),
        'info_uid': _s(info.get('uid')),
        'object_class': oclass,
        'classification': oclass,
        'kind': oclass if oclass in (OBJ_SDWAN, OBJ_WBOND) else kind,
        'connection_state': connection_state,
        'operational_state': op,
        'failure_reason': (clean_reason(status.get('error_text'))
                           if op != OP_CONNECTED else None),
        'sim_presence': SIM_NOT_APPLICABLE,
        'sim_type': None, 'sim_slot': None,
        'carrier': None, 'carrier_name': None, 'carrier_source': None,
        'modem_owner': None, 'modem_model': None,
        'host_model': ctx['host_model'],
        'inventory_visible': False,
        'underlay_uid': None, 'underlay_state': None,
        'underlay_available': None,
        'classification_evidence': [],
    }
    evidence = rec['classification_evidence']

    if oclass != OBJ_PHYSICAL:
        evidence.append('object_class_' + oclass)
        rec['display_name'] = (
            'Bonded WAN' if oclass == OBJ_WBOND else
            'SD-WAN Tunnel' if oclass == OBJ_SDWAN else
            (_s(info.get('product')) or _s(info.get('iface')) or _s(uid)
             or 'Unknown WAN'))
        return rec

    if kind == KIND_SATELLITE:
        evidence.append('satellite_token')
    elif kind == KIND_CELLULAR:
        evidence.append('cellular_evidence')

    presence, sim_type, sim_evidence = _sim_state(
        kind, info, status, diagnostics)
    if (kind == KIND_UNKNOWN and (_s(info.get('type')).lower() == 'mdm'
                                  or _s(uid).lower().startswith('mdm-'))):
        # A modem object without enough evidence to call it cellular: the
        # SIM state is simply not established (never 'not applicable').
        presence = SIM_UNKNOWN
    rec['sim_presence'] = presence
    evidence.extend(sim_evidence)

    if kind == KIND_CELLULAR:
        rec['sim_type'] = sim_type
        slot = sim_slot_label(info.get('sim'))
        rec['sim_slot'] = slot or None
        abbr, name, source = _carrier(diagnostics, presence)
        rec['carrier'], rec['carrier_name'], rec['carrier_source'] = (
            abbr, name, source)
        if source:
            evidence.append('carrier_from_' + (
                'carrid' if source == 'registered' else 'homecarrid'))
        owner, model, own_ev = _modem_ownership(
            info, diagnostics, ctx['host_family'], ctx['catalog'])
        rec['modem_owner'], rec['modem_model'] = owner, model
        evidence.extend(own_ev)

    if presence == SIM_EMPTY:
        rec['classification'] = CLASS_EMPTY_SIM
    rec['inventory_visible'] = rec['classification'] == OBJ_PHYSICAL
    rec['display_name'] = _display_name_physical(
        uid, info, kind, rec, ctx)
    return rec


# ==========================================================================
# Inventory
# ==========================================================================

def build_inventory(raw_devices, host_model=None, catalog=None):
    """Identity records for every WAN object in status/wan/devices.

    Returns {'schema_version', 'host_model', 'host_family', 'catalog',
    'order': [uid...], 'by_uid': {uid: record}, 'summary': {...}}.
    `order` follows the input mapping; nothing is dropped (raw inventory).
    """
    ctx = _context(host_model, catalog)
    by_uid, order = {}, []
    devices = raw_devices if isinstance(raw_devices, dict) else {}
    for uid, device in devices.items():
        if not isinstance(device, dict):
            continue
        by_uid[uid] = _identify(uid, device, ctx)
        order.append(uid)
    # Second pass: overlays borrow their physical underlay's identity.
    for uid in order:
        rec = by_uid[uid]
        if rec['object_class'] != OBJ_SDWAN:
            continue
        under_uid = sdwan_underlay_uid(devices.get(uid))
        under = by_uid.get(under_uid) if under_uid else None
        rec['underlay_uid'] = under_uid or None
        if under is not None and under['object_class'] == OBJ_PHYSICAL:
            rec['underlay_state'] = UNDERLAY_RESOLVED
            rec['underlay_available'] = under['classification'] != \
                CLASS_EMPTY_SIM
            rec['display_name'] = 'SD-WAN Tunnel - ' + under['display_name']
            rec['classification_evidence'].append('underlay_resolved')
        else:
            rec['underlay_state'] = UNDERLAY_UNRESOLVED
            rec['classification_evidence'].append(
                'underlay_unresolved' if under_uid
                else 'underlay_not_reported')
    return {
        'schema_version': SCHEMA_VERSION,
        'host_model': ctx['host_model'],
        'host_family': ctx['host_family'] or None,
        'catalog': ctx['catalog'].info(),
        'order': order,
        'by_uid': by_uid,
        'summary': summarize(by_uid.values()),
    }


def summarize(records):
    """Inventory counts. Raw and displayable physical views kept distinct."""
    raw = empty = connected = inactive = unknown = 0
    overlays = bonds = other = 0
    for r in records:
        oc = r.get('object_class')
        if oc == OBJ_PHYSICAL:
            raw += 1
            if r.get('classification') == CLASS_EMPTY_SIM:
                empty += 1
                continue
            op = r.get('operational_state')
            if op == OP_CONNECTED:
                connected += 1
            elif op == OP_INACTIVE:
                inactive += 1
            else:
                unknown += 1
        elif oc == OBJ_SDWAN:
            overlays += 1
        elif oc == OBJ_WBOND:
            bonds += 1
        else:
            other += 1
    return {
        'raw_physical': raw,
        'empty_sim': empty,
        'displayable_physical': raw - empty,
        'connected': connected,
        'inactive': inactive,
        'unknown_state': unknown,
        'overlays': overlays,
        'wbond': bonds,
        'unclassified': other,
    }


def public_view(record):
    """The exportable subset of an identity record (a copy)."""
    if not isinstance(record, dict):
        return None
    out = {}
    for key in PUBLIC_FIELDS:
        val = record.get(key)
        out[key] = list(val) if isinstance(val, list) else val
    return out


def is_displayable_physical(record):
    return (isinstance(record, dict)
            and record.get('object_class') == OBJ_PHYSICAL
            and record.get('classification') != CLASS_EMPTY_SIM)


def resolve_wan_ref(ref, by_uid):
    """Identity record for a raw WAN reference (uid, else a UNIQUE iface).

    The raw reference is never altered; callers keep it for joins and add
    the resolved name as presentation. None when absent or ambiguous.
    """
    ref = _s(ref)
    if not ref or not isinstance(by_uid, dict):
        return None
    if ref in by_uid:
        return by_uid[ref]
    hits = [r for r in by_uid.values() if r.get('iface') == ref]
    return hits[0] if len(hits) == 1 else None


# ==========================================================================
# Names for single objects (normalize.friendly_name* delegate here)
# ==========================================================================

def friendly_name(uid, device, host_model=None, catalog=None):
    """Display-only name of ONE physical-style device."""
    return _identify(uid, device, _context(host_model, catalog))[
        'display_name']


def friendly_name_ex(uid, device, devices=None, underlay_uid='',
                     bond_config_name='', host_model=None, catalog=None):
    """Object-aware name: WBOND, SD-WAN tunnel (borrowing its underlay's
    name) or physical."""
    device = _dict(device)
    devices = devices if isinstance(devices, dict) else {}
    oclass = object_class(uid, device)
    if oclass == OBJ_WBOND:
        name = _s(bond_config_name)
        return 'Bonded WAN ({})'.format(name) if name else 'Bonded WAN'
    if oclass == OBJ_SDWAN:
        under_uid = _s(underlay_uid)
        under = devices.get(under_uid) if under_uid else None
        if isinstance(under, dict):
            return 'SD-WAN Tunnel - ' + friendly_name(
                under_uid, under, host_model, catalog)
        return 'SD-WAN Tunnel'
    return friendly_name(uid, device, host_model, catalog)


# ==========================================================================
# Historical identity (versioned payload + window sink)
# ==========================================================================

def versioned_payload(records, host_model=None):
    """Compact, durable identity payload for the config version store.

    `records` is any iterable of identity-shaped dicts (inventory records or
    snapshot wan.paths). Excludes volatile operational fields; keyed by raw
    uid so a rename or a SIM swap yields a new version while joins by uid
    keep working.
    """
    wans = {}
    for rec in records or ():
        if isinstance(rec, dict) and rec.get('uid'):
            wans[rec['uid']] = {k: rec.get(k) for k in VERSIONED_FIELDS}
    return {
        'schema_version': SCHEMA_VERSION,
        'host_model': host_model,
        'wans': {uid: wans[uid] for uid in sorted(wans)},
    }


def annotate_steering_refs(logical_rules, events, by_uid):
    """Add presentation identities next to RAW steering WAN references.

    Raw selector values (steering_to, primary_wandev, wan_from*/wan_to*) are
    never modified; `*_identity` fields are additive. A reference that does
    not resolve to exactly one WAN stays unresolved (never guessed).
    """
    def ref_identity(ref):
        raw = _s(ref)
        if not raw:
            return None
        rec = resolve_wan_ref(raw, by_uid)
        if rec is None:
            return {'ref': raw, 'uid': None, 'display_name': None,
                    'classification': None, 'resolved': False}
        return {'ref': raw, 'uid': rec['uid'],
                'display_name': rec['display_name'],
                'classification': rec['classification'], 'resolved': True}

    for lr in logical_rules or ():
        if not isinstance(lr, dict):
            continue
        lr['steering_to_identity'] = [
            ref_identity(u) for u in (lr.get('steering_to') or []) if _s(u)]
        lr['primary_wandev_identity'] = ref_identity(
            lr.get('primary_wandev'))
    for ev in events or ():
        if not isinstance(ev, dict):
            continue
        ev['wan_from_identity'] = ref_identity(
            ev.get('wan_from_uid') or ev.get('wan_from'))
        ev['wan_to_identity'] = ref_identity(
            ev.get('wan_to_uid') or ev.get('wan_to'))


class IdentityWindow(object):
    """Sink for a history scan: which wan_identity versions the scanned
    observations referenced, and over which time span."""

    def __init__(self):
        self.versions = {}          # version -> [first_ts, last_ts]
        self.observations = 0
        self.unversioned = 0

    def add(self, obs):
        self.observations += 1
        refs = obs.get('config_versions') if isinstance(obs, dict) else None
        ref = refs.get('wan_identity') if isinstance(refs, dict) else None
        ver = ref.get('version') if isinstance(ref, dict) else None
        if isinstance(ver, bool) or not isinstance(ver, int):
            self.unversioned += 1
            return
        ts = obs.get('collected_ts')
        span = self.versions.setdefault(ver, [ts, ts])
        if isinstance(ts, (int, float)):
            if not isinstance(span[0], (int, float)) or ts < span[0]:
                span[0] = ts
            if not isinstance(span[1], (int, float)) or ts > span[1]:
                span[1] = ts


def history_block(uid, current_record, window, versions):
    """Identity of `uid` as it applied during a scanned window.

    `versions` = ConfigVersionStore.versions('wan_identity') (retained).
    The CURRENT identity is always labelled as such; when the window saw
    other identity versions for this uid, they are listed so a consumer
    never presents today's carrier/SIM placement as historical fact.
    """
    by_ver = {v.get('version'): v for v in (versions or [])
              if isinstance(v, dict)}
    seen = []
    for ver in sorted(window.versions):
        stored = by_ver.get(ver)
        wans = ((stored or {}).get('payload') or {}).get('wans') or {}
        entry = wans.get(uid)
        seen.append({
            'version': ver,
            'first_ts': window.versions[ver][0],
            'last_ts': window.versions[ver][1],
            'retained': stored is not None,
            'identity': dict(entry) if isinstance(entry, dict) else None,
        })
    distinct = {stable_hash(e['identity']) for e in seen if e['retained']}
    if window.versions and window.unversioned == 0 and seen and all(
            e['retained'] for e in seen):
        basis = 'versioned'
    elif window.versions:
        basis = 'partially_versioned'
    else:
        basis = 'current_only'
    return {
        'current': public_view(current_record),
        'basis': basis,
        'changed_in_window': len(distinct) > 1,
        'observations_without_identity_version': window.unversioned,
        'versions_in_window': seen,
    }
