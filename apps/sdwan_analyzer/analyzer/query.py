"""Read-query helpers shared by the stores and the web layer (v0.4.4).

Pure functions, no file I/O, no NCOS access:

* QueryError          one exception type carrying the HTTP status.
* parse_history_params / parse_samples_params
                      strict validation of query-string parameters.
* encode_cursor / decode_cursor
                      opaque, query-bound pagination tokens.

Cursors are base64url JSON with a short integrity check. They are opaque to
clients and carry NO trust: everything inside is re-validated against the
real files on every use (day name format and existence, job ID format, byte
offset on a record boundary), so a forged or edited cursor can at worst be
rejected, never steer a read outside the telemetry tree.
"""
import base64
import hashlib
import json
import re
from urllib.parse import parse_qs

# Hard ceilings (see README "Query APIs").
DEFAULT_LIMIT = 100
MAX_LIMIT = 250
MAX_SCAN_BYTES = 1048576          # disk bytes examined per HTTP request
MAX_RESPONSE_BYTES = 524288       # persisted bytes of accepted records
DEFAULT_WINDOW_S = 86400          # default history window
MAX_TS = 4102444800               # 2100-01-01; far beyond any real clock
MAX_CURSOR_CHARS = 512

SAMPLE_SOURCES = ('scheduled', 'manual', 'startup')

_INT_RE = re.compile(r'^\d{1,6}\Z')
_TS_RE = re.compile(r'^\d{1,11}(\.\d{1,6})?\Z')
_TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]+\Z')
_GROUP_RE = re.compile(r'^[a-z_]{1,24}\Z')


class QueryError(ValueError):
    """A rejected query. ``status`` is the HTTP status to answer with."""

    def __init__(self, status, message):
        ValueError.__init__(self, message)
        self.status = status
        self.message = message


def bad(message):
    return QueryError(400, message)


def expired(message='cursor expired'):
    return QueryError(410, message)


# --- parameters ----------------------------------------------------------------

def split_query(raw_query, allowed):
    """Query string -> {name: single str}. 400 on unknown/duplicate/blank."""
    out = {}
    if not raw_query:
        return out
    if len(raw_query) > 2048:
        raise bad('query string too long')
    try:
        pairs = parse_qs(raw_query, keep_blank_values=True,
                         strict_parsing=False, max_num_fields=16)
    except ValueError:
        raise bad('invalid query string')
    for name, values in pairs.items():
        if name not in allowed:
            raise bad('unknown parameter: {}'.format(name[:32]))
        if len(values) != 1:
            raise bad('duplicate parameter: {}'.format(name))
        out[name] = values[0]
    return out


def parse_limit(text):
    if text is None:
        return DEFAULT_LIMIT
    if not _INT_RE.match(text):
        raise bad('limit must be an integer between 1 and {}'.format(
            MAX_LIMIT))
    n = int(text)
    if n < 1 or n > MAX_LIMIT:
        raise bad('limit must be an integer between 1 and {}'.format(
            MAX_LIMIT))
    return n


def parse_ts(name, text):
    """UTC Unix epoch seconds (integer or decimal) -> float, or None."""
    if text is None:
        return None
    if not _TS_RE.match(text):
        raise bad('{} must be a UTC Unix epoch timestamp'.format(name))
    val = float(text)
    if val > MAX_TS:
        raise bad('{} is out of range'.format(name))
    return val


def parse_cursor_arg(text):
    if text is None:
        return None
    if not text or len(text) > MAX_CURSOR_CHARS or not _TOKEN_RE.match(text):
        raise bad('invalid cursor')
    return text


def parse_history_params(raw_query):
    """-> dict(limit, start, end, sample_source, cursor), all validated."""
    q = split_query(raw_query, ('limit', 'start', 'end', 'sample_source',
                                'cursor'))
    out = {'limit': parse_limit(q.get('limit')),
           'start': parse_ts('start', q.get('start')),
           'end': parse_ts('end', q.get('end')),
           'sample_source': q.get('sample_source'),
           'cursor': parse_cursor_arg(q.get('cursor'))}
    if out['sample_source'] is not None and (
            out['sample_source'] not in SAMPLE_SOURCES):
        raise bad('sample_source must be one of: {}'.format(
            ', '.join(SAMPLE_SOURCES)))
    if (out['start'] is not None and out['end'] is not None
            and out['start'] > out['end']):
        raise bad('start must not be after end')
    return out


def parse_samples_params(raw_query, groups):
    """-> dict(limit, group, after, cursor), all validated."""
    q = split_query(raw_query, ('limit', 'group', 'after', 'cursor'))
    out = {'limit': parse_limit(q.get('limit')),
           'group': q.get('group'),
           'after': parse_ts('after', q.get('after')),
           'cursor': parse_cursor_arg(q.get('cursor'))}
    if out['group'] is not None and (
            not _GROUP_RE.match(out['group'])
            or out['group'] not in groups):
        raise bad('group must be one of: {}'.format(
            ', '.join(sorted(groups))))
    return out


# --- cursors -------------------------------------------------------------------

def context_hash(*parts):
    """Short stable digest of the query/filter parameters a cursor is bound to."""
    text = '\x1f'.join('' if p is None else str(p) for p in parts)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]


def _check(payload):
    body = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(body.encode('utf-8')).hexdigest()[:8]


def encode_cursor(payload):
    doc = dict(payload)
    doc['v'] = 1
    doc['c'] = _check(doc)
    raw = json.dumps(doc, sort_keys=True, separators=(',', ':'))
    return base64.urlsafe_b64encode(raw.encode('utf-8')).decode(
        'ascii').rstrip('=')


def decode_cursor(token, kind, ctx):
    """Validate and return a cursor payload (dict). 400 on any defect.

    kind: 'h' history, 'r' recording, 'm'/'n' in-memory fallbacks.
    ctx:  context_hash() of the CURRENT request's query parameters; a cursor
          minted for different parameters is rejected.
    """
    try:
        raw = base64.urlsafe_b64decode(
            token.encode('ascii') + b'=' * (-len(token) % 4))
        doc = json.loads(raw.decode('utf-8'))
    except Exception:
        raise bad('invalid cursor')
    if not isinstance(doc, dict) or doc.get('v') != 1:
        raise bad('invalid cursor')
    check = doc.pop('c', None)
    if check != _check(doc):
        raise bad('invalid cursor')
    if doc.get('t') != kind:
        raise bad('cursor does not match this endpoint')
    if doc.get('q') != ctx:
        raise bad('cursor does not match the query parameters')
    return doc


def int_field(doc, name, lo=0, hi=1 << 40):
    val = doc.get(name)
    if isinstance(val, bool) or not isinstance(val, int) or not (
            lo <= val <= hi):
        raise bad('invalid cursor')
    return val


def envelope(samples, next_cursor, skipped):
    return {'samples': samples, 'count': len(samples),
            'next_cursor': next_cursor,
            'has_more': next_cursor is not None,
            'skipped_lines': skipped}
