"""Filesystem primitives for durable telemetry (stdlib only, no app logic).

Everything that touches the disk lives here or in durable.py; web.py,
collectors.py and normalize.py never do file I/O.

Contents
--------
* Atomic small-file JSON (state.json, metadata.json): write .tmp -> flush ->
  fsync -> parse/validate -> os.replace -> best-effort directory fsync, with
  recovery (valid primary wins; valid .tmp promoted when the primary is bad;
  otherwise the bad primary is quarantined to <file>.corrupt).
* JSONL append-only files: JsonlAppender (open-handle reuse, flush per record,
  configurable fsync cadence), tail repair (torn last line), and readers
  (newest-N, tail, ranged with byte-offset cursors). A reader never raises on
  a bad line: it skips it and reports a count.
* The persisted OBSERVATION FORM: encode_observation()/decode_observation().
  The disk form is compact; the API/in-memory form is unchanged.
* Path safety: job IDs are validated against the generated format before they
  are ever joined into a path.

Lifecycle note: files live under the app-local ``tmp/``. They survive an app
restart and a router reboot, and may be wiped by an application update.
"""

import json
import os
import re
import time

SEP = (',', ':')

# Persisted lines never carry spaces: they are compact JSON, one per line.
MAX_CORRUPT_FRAGMENT = 65536       # cap kept in <file>.corrupt per repair
MAX_RECORD_BYTES = 1048576         # a line longer than this is never a record

# \Z (not $) so a trailing newline can never sneak into a path component.
JOB_ID_RE = re.compile(r'^rec-\d{1,12}-\d{3,6}\Z')


def dumps(obj):
    """Compact, deterministic-enough JSON text (never raises on odd types)."""
    return json.dumps(obj, separators=SEP, default=str)


# --- paths --------------------------------------------------------------------

def valid_job_id(job_id):
    return isinstance(job_id, str) and JOB_ID_RE.match(job_id) is not None


def safe_job_dir(recordings_dir, job_id):
    """Directory for a job. Raises ValueError unless job_id is well formed.

    Only the generated ``rec-<epoch>-<seq>`` form is accepted, so no path
    fragment (``..``, separators, absolute paths) can ever reach a join.
    """
    if not valid_job_id(job_id):
        raise ValueError('invalid recording id')
    return os.path.join(recordings_dir, job_id)


def fsync_directory(path):
    """Best-effort fsync of a directory (never raises)."""
    try:
        fd = os.open(path or '.', os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def remove_quiet(path):
    try:
        if os.path.exists(path):
            os.remove(path)
        return True
    except Exception:
        return False


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# --- atomic JSON ----------------------------------------------------------------

def read_json(path, expect=dict):
    """Parse a JSON file; raises ValueError if the root is not `expect`."""
    with open(path, 'r') as f:
        obj = json.load(f)
    if expect is not None and not isinstance(obj, expect):
        raise ValueError('unexpected JSON root type')
    return obj


def quarantine(path):
    """Move an invalid file aside to <path>.corrupt (replaces an older one)."""
    if not os.path.exists(path):
        return None
    dest = path + '.corrupt'
    os.replace(path, dest)
    fsync_directory(os.path.dirname(path))
    return dest


def write_json_atomic(path, obj, create_parent=True):
    """Atomically replace `path` with `obj`.

    write .tmp, flush, fsync, re-parse and compare, os.replace, directory
    fsync. A failure leaves the previous primary untouched.

    create_parent=False never creates directories (FileNotFoundError if the
    parent is gone): used on shutdown so a dying process cannot resurrect a
    directory tree that an installer just removed.
    """
    parent = os.path.dirname(path)
    if parent and create_parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + '.tmp'
    text = dumps(obj)
    with open(tmp, 'w') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if read_json(tmp, expect=None) != json.loads(text):
        raise IOError('temporary JSON validation failed')
    os.replace(tmp, path)
    fsync_directory(parent)


def recover_json(path, expect=dict):
    """Recover an atomically written JSON file.

    Returns (object_or_None, status) where status is one of:
      'ok'            valid primary (a leftover .tmp is discarded)
      'promoted_tmp'  primary missing/invalid, valid .tmp promoted
      'quarantined'   primary invalid and no valid .tmp: moved to .corrupt
      'missing'       nothing on disk
    """
    tmp = path + '.tmp'
    bad_primary = False
    if os.path.exists(path):
        try:
            obj = read_json(path, expect)
            remove_quiet(tmp)
            return obj, 'ok'
        except Exception:
            bad_primary = True
    if os.path.exists(tmp):
        try:
            obj = read_json(tmp, expect)
            if bad_primary:
                quarantine(path)
            os.replace(tmp, path)
            fsync_directory(os.path.dirname(path))
            return obj, 'promoted_tmp'
        except Exception:
            remove_quiet(tmp)
    if bad_primary:
        quarantine(path)
        return None, 'quarantined'
    return None, 'missing'


# --- JSONL: tail repair ---------------------------------------------------------

def repair_jsonl_tail(path, max_corrupt=MAX_CORRUPT_FRAGMENT):
    """Make a JSONL file end on a record boundary without rewriting it.

    * ends with newline            -> {'action': 'none'}
    * trailing fragment is a valid JSON object -> newline appended
      ('newline_added')
    * otherwise the fragment (capped) is appended to <path>.corrupt and the
      file is truncated back to the last newline ('truncated'); fsynced.
    Never touches earlier records.
    """
    out = {'action': 'none', 'dropped_bytes': 0}
    try:
        size = os.path.getsize(path)
    except OSError:
        return out
    if size == 0:
        return out
    with open(path, 'rb+') as f:
        f.seek(size - 1)
        if f.read(1) == b'\n':
            return out
        # Find the start of the last (unterminated) line.
        pos = size
        start = 0
        chunk = 65536
        while pos > 0:
            lo = max(0, pos - chunk)
            f.seek(lo)
            data = f.read(pos - lo)
            idx = data.rfind(b'\n')
            if idx >= 0:
                start = lo + idx + 1
                break
            pos = lo
        length = size - start
        frag = b''
        if length <= MAX_RECORD_BYTES:
            f.seek(start)
            frag = f.read(length)
        ok = False
        if frag:
            try:
                ok = isinstance(json.loads(frag.decode('utf-8')), dict)
            except Exception:
                ok = False
        if ok:
            f.seek(size)
            f.write(b'\n')
            f.flush()
            os.fsync(f.fileno())
            out['action'] = 'newline_added'
            return out
        keep = frag
        if not keep:
            f.seek(start)
            keep = f.read(min(length, max_corrupt))
        with open(path + '.corrupt', 'ab') as c:
            c.write(keep[:max_corrupt] + b'\n')
            c.flush()
            os.fsync(c.fileno())
        f.truncate(start)
        f.flush()
        os.fsync(f.fileno())
        out['action'] = 'truncated'
        out['dropped_bytes'] = length
    fsync_directory(os.path.dirname(path))
    return out


# --- JSONL: appender ------------------------------------------------------------

class JsonlAppender(object):
    """Append compact JSON records to one file.

    * The handle is opened lazily and kept open (cheap per-sample writes).
    * The tail is repaired the first time the file is opened for append, so a
      torn last line from a crash can never be glued to a new record.
    * Every append is flushed to the OS. fsync policy:
        fsync_interval = 0      fsync after every append
        fsync_interval = N > 0  fsync when N seconds passed since the last one
                                (checked on append and by maybe_sync())
        fsync_interval = None   only explicit sync()/close()
    A write error closes the handle (the next append reopens and repairs) and
    re-raises; callers decide how to degrade.
    """

    def __init__(self, path, fsync_interval=None, clock=time.time,
                 on_repair=None, on_sync=None):
        self.path = path
        self._interval = fsync_interval
        self._clock = clock
        self._on_repair = on_repair
        self._on_sync = on_sync
        self._f = None
        self._dirty = False
        self._last_sync = None
        self.records_written = 0

    def is_open(self):
        return self._f is not None

    def open(self):
        if self._f is not None:
            return
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        existed = os.path.exists(self.path)
        rep = repair_jsonl_tail(self.path)
        if rep['action'] != 'none' and self._on_repair is not None:
            try:
                self._on_repair(self.path, rep)
            except Exception:
                pass
        self._f = open(self.path, 'ab')
        if not existed:
            fsync_directory(parent)
        self._last_sync = self._clock()
        self._dirty = False

    def append(self, record):
        """Write one record. Returns the number of bytes written."""
        line = dumps(record).encode('utf-8') + b'\n'
        try:
            self.open()
            self._f.write(line)
            self._f.flush()
        except Exception:
            self._abandon()
            raise
        self._dirty = True
        self.records_written += 1
        if self._interval is not None:
            if self._interval <= 0:
                self.sync()
            else:
                self.maybe_sync()
        return len(line)

    def maybe_sync(self):
        """fsync if dirty and the interval elapsed. Returns True if synced."""
        if (self._f is None or not self._dirty or not self._interval
                or self._interval <= 0):
            return False
        if self._clock() - self._last_sync >= self._interval:
            return self.sync()
        return False

    def sync(self):
        """Explicit fsync of pending data. Returns True if a sync happened."""
        if self._f is None or not self._dirty:
            return False
        try:
            self._f.flush()
            os.fsync(self._f.fileno())
        except Exception:
            self._abandon()
            raise
        self._dirty = False
        self._last_sync = self._clock()
        if self._on_sync is not None:
            try:
                self._on_sync(self._last_sync)
            except Exception:
                pass
        return True

    def close(self):
        """fsync pending data and close. Safe to call repeatedly."""
        f = self._f
        if f is None:
            return
        try:
            self.sync()
        finally:
            self._abandon()

    def _abandon(self):
        f, self._f = self._f, None
        self._dirty = False
        if f is not None:
            try:
                f.close()
            except Exception:
                pass


# --- JSONL: readers -------------------------------------------------------------

_TS_PREFIX = re.compile(rb'^\{"collected_ts":(-?\d+(?:\.\d+)?)[,}]')


def peek_ts(line):
    """collected_ts from a persisted line's prefix without a full parse."""
    m = _TS_PREFIX.match(line)
    if m is None:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _parse_line(line):
    try:
        rec = json.loads(line.decode('utf-8'))
    except Exception:
        return None
    return rec if isinstance(rec, dict) else None


def iter_jsonl(path, offset=0, stats=None):
    """Yield (start_offset, next_offset, record) for each valid COMPLETE line.

    Invalid lines are skipped (counted in stats['skipped'] when given). A
    final line without a newline is incomplete and skipped (it is repaired
    on the next append-open).
    """
    try:
        f = open(path, 'rb')
    except OSError:
        return
    with f:
        f.seek(offset)
        pos = offset
        for line in f:
            start = pos
            pos += len(line)
            if not line.endswith(b'\n'):
                break
            if not line.strip():
                continue
            rec = _parse_line(line)
            if rec is None:
                if stats is not None:
                    stats['skipped'] = stats.get('skipped', 0) + 1
                continue
            yield start, pos, rec


def read_tail_records(path, max_bytes=65536, stats=None):
    """Valid records within the last `max_bytes` of the file, oldest first."""
    size = file_size(path)
    if size == 0:
        return []
    start = max(0, size - max_bytes)
    # Start one byte early so a window beginning exactly on a line boundary
    # keeps that first (complete) line.
    begin = max(0, start - 1)
    with open(path, 'rb') as f:
        f.seek(begin)
        data = f.read(size - begin)
    if start > 0:
        nl = data.find(b'\n')
        data = b'' if nl < 0 else data[nl + 1:]
    lines = data.split(b'\n')
    lines = lines[:-1]       # last element is '' or an incomplete fragment
    out = []
    for line in lines:
        if not line.strip():
            continue
        rec = _parse_line(line)
        if rec is None:
            if stats is not None:
                stats['skipped'] = stats.get('skipped', 0) + 1
            continue
        out.append(rec)
    return out


def read_recent_window(path, horizon_s, max_bytes, cutoff_ts=None):
    """Records from the newest `horizon_s` seconds of a JSONL file.

    Scans BACKWARD from the end in chunks and stops at whichever comes
    first: a record older than the horizon, or `max_bytes` scanned. The
    horizon is measured from the newest valid `collected_ts` in the file
    (never from the filesystem mtime): cutoff = newest_ts - horizon_s.
    `cutoff_ts` overrides that (used to apply one shared cutoff across
    several files). Lines outside the window are never JSON-parsed (their
    timestamp is read from the line prefix). Malformed lines are skipped and
    counted; an unterminated final line is ignored.

    Returns (records_oldest_first, info) where info has: scanned_bytes,
    skipped, capped (True if the byte cap ended the scan before the horizon
    was reached) and newest_ts.
    """
    info = {'scanned_bytes': 0, 'skipped': 0, 'capped': False,
            'newest_ts': None}
    size = file_size(path)
    if size == 0:
        return [], info
    try:
        f = open(path, 'rb')
    except OSError:
        return [], info
    floor = max(0, size - max_bytes)
    chunk = 65536
    out = []
    state = {'cutoff': cutoff_ts}

    def handle(line):
        """Process one complete line (newest first). True => window done."""
        if not line.strip():
            return False
        ts = peek_ts(line)
        rec = None
        if ts is None:
            rec = _parse_line(line)
            t = rec.get('collected_ts') if rec is not None else None
            if not isinstance(t, (int, float)) or isinstance(t, bool):
                info['skipped'] += 1
                return False
            ts = float(t)
        if info['newest_ts'] is None:
            info['newest_ts'] = ts
        if state['cutoff'] is None:
            state['cutoff'] = ts - horizon_s
        if ts < state['cutoff']:
            return True
        if rec is None:
            rec = _parse_line(line)
            if rec is None:
                info['skipped'] += 1
                return False
        out.append(rec)
        return False

    done = False
    with f:
        pos = size
        carry = b''
        first = True
        while pos > floor and not done:
            lo = max(floor, pos - chunk)
            f.seek(lo)
            data = f.read(pos - lo)
            info['scanned_bytes'] += len(data)
            pos = lo
            parts = (data + carry).split(b'\n')
            if first:
                parts = parts[:-1]      # unterminated / empty final piece
                first = False
            if pos > 0:
                # The first piece may be the tail of a line that starts in
                # an earlier chunk: hold it until that chunk is read.
                carry = parts[0] if parts else b''
                parts = parts[1:]
            else:
                carry = b''
            for line in reversed(parts):
                if handle(line):
                    done = True
                    break
    if not done and floor > 0:
        info['capped'] = True           # cap hit before the horizon
    out.reverse()
    return out, info


def read_newest(path, n, stats=None):
    """The newest `n` valid records (oldest first). Reads from the end."""
    if n <= 0:
        return []
    size = file_size(path)
    window = 65536
    while True:
        local = {} if stats is not None else None
        recs = read_tail_records(path, window, local)
        if len(recs) >= n or window >= size:
            if stats is not None and local:
                stats['skipped'] = stats.get('skipped', 0) + local.get(
                    'skipped', 0)
            return recs[-n:]
        window *= 4


QUERY_MAX_LINE = 262144            # a line longer than this is never served
QUERY_SKIP_CHUNK = 65536           # chunk size while skipping an oversized line


def scan_range(path, offset=0, start_ts=None, end_ts=None, after_ts=None,
               limit=None, max_bytes=None, max_accept_bytes=None,
               accept=None, resync=False, stop_past_end=False,
               max_line=QUERY_MAX_LINE, progress_floor=False):
    """Bounded forward scan of a JSONL file from a byte offset.

    Every byte read from the file is counted in ``scanned_bytes`` (accepted,
    filtered-out, malformed, oversized and skipped alike) and the total never
    exceeds ``max_bytes`` (unless ``progress_floor``, see below). Lines are
    read with a length cap, so one huge line can never be pulled into memory.

    Filters (applied on the cheap line prefix when possible):
      start_ts <= ts <= end_ts   inclusive bounds
      ts > after_ts              exclusive lower bound
      accept(record) -> bool     extra predicate on the parsed record
    stop_past_end: the file is in collection order, so the first line with
      ts > end_ts ends the scan for good (``past_end``): remaining lines are
      not examined.

    Limits: ``limit`` accepted records, ``max_accept_bytes`` of accepted line
    bytes (response bound). A record that would break either bound is NOT
    consumed: ``next_offset`` stays at its start so the next page returns it.

    Lines longer than ``max_line`` are oversized: counted as skipped and never
    parsed. If the byte budget ends inside an oversized line, ``resync`` is
    returned True and ``next_offset`` is mid-line: pass resync=True with that
    offset to skip the rest of the line before reading records again.

    Only COMPLETE (newline-terminated) lines are consumed; an unterminated
    tail (a record being appended right now) is left for the next call.

    progress_floor: guarantee that the first line is examined even when
    max_bytes is smaller than a line (legacy read_range behaviour). Query
    callers leave it False so the byte ceiling is strict.

    Returns a dict: records, next_offset, eof, scanned_bytes, skipped,
    accepted_bytes, resync, past_end, missing.
    """
    res = {'records': [], 'next_offset': offset, 'eof': True,
           'scanned_bytes': 0, 'skipped': 0, 'accepted_bytes': 0,
           'resync': False, 'past_end': False, 'missing': False}
    try:
        f = open(path, 'rb')
    except OSError:
        res['missing'] = True
        return res
    budget = max_bytes if max_bytes is not None else float('inf')
    out = res['records']
    scanned = 0
    pos = offset
    tail = False
    with f:
        f.seek(offset)
        if resync:
            # Finish skipping an oversized line begun by a previous call.
            while True:
                room = budget - scanned
                if room <= 0:
                    res.update(next_offset=pos, scanned_bytes=scanned,
                               resync=True, eof=False)
                    return res
                chunk = f.readline(int(min(QUERY_SKIP_CHUNK, room)))
                if not chunk:
                    # EOF inside the oversized line (still being written).
                    res.update(next_offset=pos, scanned_bytes=scanned,
                               eof=True)
                    return res
                scanned += len(chunk)
                pos += len(chunk)
                if chunk.endswith(b'\n'):
                    break
        while True:
            if limit is not None and len(out) >= limit:
                break
            room = budget - scanned
            if progress_floor and scanned == 0 and room < max_line + 1:
                room = max_line + 1
            if room <= 0:
                break
            want = int(min(max_line + 1, room))
            line = f.readline(want)
            if not line:
                break                           # EOF
            scanned += len(line)
            if not line.endswith(b'\n'):
                if len(line) > max_line:
                    # Oversized: skip the rest of the line within budget.
                    res['skipped'] += 1
                    pos += len(line)
                    done = False
                    while True:
                        room = budget - scanned
                        if room <= 0:
                            res['resync'] = True
                            break
                        chunk = f.readline(int(min(QUERY_SKIP_CHUNK, room)))
                        if not chunk:
                            break               # EOF mid-line: incomplete
                        scanned += len(chunk)
                        pos += len(chunk)
                        if chunk.endswith(b'\n'):
                            done = True
                            break
                    if done:
                        continue
                    if not res['resync']:
                        tail = True     # EOF inside an unterminated line
                    break
                # No newline and short: an incomplete tail (EOF) or the byte
                # budget cut this line short. Neither is consumed.
                tail = len(line) < want
                break
            start = pos
            pos += len(line)
            if not line.strip():
                continue
            ts = peek_ts(line)
            if ts is not None:
                if stop_past_end and end_ts is not None and ts > end_ts:
                    res['past_end'] = True
                    pos = start                 # not consumed
                    break
                if ((start_ts is not None and ts < start_ts)
                        or (end_ts is not None and ts > end_ts)
                        or (after_ts is not None and ts <= after_ts)):
                    continue
            rec = _parse_line(line)
            if rec is None:
                res['skipped'] += 1
                continue
            if ts is None:
                t = rec.get('collected_ts')
                if not isinstance(t, (int, float)) or isinstance(t, bool):
                    res['skipped'] += 1
                    continue
                if ((start_ts is not None and t < start_ts)
                        or (end_ts is not None and t > end_ts)
                        or (after_ts is not None and t <= after_ts)):
                    continue
            if accept is not None and not accept(rec):
                continue
            if (max_accept_bytes is not None and out
                    and res['accepted_bytes'] + len(line) > max_accept_bytes):
                pos = start                     # not consumed: next page
                break
            out.append(rec)
            res['accepted_bytes'] += len(line)
        try:
            size = os.fstat(f.fileno()).st_size
        except OSError:
            size = pos
    res['next_offset'] = pos
    res['scanned_bytes'] = scanned
    res['eof'] = res['past_end'] or tail or (
        not res['resync'] and pos >= size)
    return res


def offset_state(path, offset):
    """Is `offset` a valid resume point in a JSONL file?

    'missing' (no file), 'beyond' (past the end), 'mid' (inside a line) or
    'ok' (start of file, or directly after a newline).
    """
    try:
        f = open(path, 'rb')
    except OSError:
        return 'missing'
    with f:
        if offset > os.fstat(f.fileno()).st_size:
            return 'beyond'
        if offset == 0:
            return 'ok'
        f.seek(offset - 1)
        return 'ok' if f.read(1) == b'\n' else 'mid'


def read_range(path, start_ts=None, end_ts=None, offset=0, limit=None,
               max_bytes=None, stats=None):
    """Records with start_ts <= collected_ts <= end_ts, from a byte offset.

    Thin wrapper over scan_range (kept for compatibility). The first line is
    always examined even if it is longer than max_bytes (progress floor);
    after that the byte budget is enforced on bytes actually read, including
    filtered-out and malformed lines. ``stats`` receives ``skipped`` and
    ``scanned_bytes``.
    Returns (records, next_offset, eof). `next_offset` is the cursor to
    resume from.
    """
    res = scan_range(path, offset, start_ts=start_ts, end_ts=end_ts,
                     limit=limit, max_bytes=max_bytes, progress_floor=True)
    if stats is not None:
        stats['skipped'] = stats.get('skipped', 0) + res['skipped']
        stats['scanned_bytes'] = (stats.get('scanned_bytes', 0)
                                  + res['scanned_bytes'])
    return res['records'], res['next_offset'], res['eof']


# --- persisted observation form -----------------------------------------------

_ENV = ('metric_ts', 'collected_ts', 'timestamp_source')
_ENV_LISTS = ('interfaces', 'cellular', 'swans_samples', 'qoe', 'wbond',
              'steering', 'steering_events')
_HANDLED = frozenset(('collected_ts', 'sample_source', 'job_id',
                      'refresh_group', 'timestamp', 'duration_ms',
                      'collector_errors', 'config_versions',
                      'recording_group'))


def _strip_env(rec, ts):
    """Drop the timestamp envelope when it equals the observation's."""
    if (isinstance(rec, dict) and rec.get('timestamp_source') == 'collector'
            and rec.get('metric_ts') == ts and rec.get('collected_ts') == ts):
        return {k: v for k, v in rec.items() if k not in _ENV}
    return rec


def _restore_env(rec, ts):
    if (not isinstance(rec, dict) or 'timestamp_source' in rec
            or 'metric_ts' in rec):
        return rec
    out = dict(rec)
    out['metric_ts'] = ts
    out['collected_ts'] = ts
    out['timestamp_source'] = 'collector'
    return out


def encode_observation(obs, recording=False):
    """Compact persisted form of one observation.

    * collected_ts is FIRST (line-prefix timestamp scans).
    * sample_source/job_id are implied by the file for Recording samples.
    * null/empty envelope fields, duration_ms and the duplicate timestamp are
      dropped; config version refs become {name: version} (+ the names whose
      version just changed); collector envelopes equal to the observation
      timestamp are hoisted.
    """
    ts = obs.get('collected_ts')
    out = {'collected_ts': ts}
    if recording:
        out['recording_group'] = obs.get('recording_group')
    else:
        out['sample_source'] = obs.get('sample_source')
        if obs.get('job_id') is not None:
            out['job_id'] = obs.get('job_id')
        if obs.get('refresh_group') is not None:
            out['refresh_group'] = obs.get('refresh_group')
    if obs.get('timestamp') not in (None, ts):
        out['timestamp'] = obs.get('timestamp')
    for key, val in obs.items():
        if key in _HANDLED:
            continue
        if key == 'wan' and isinstance(val, dict):
            wan = dict(val)
            if isinstance(wan.get('paths'), list):
                wan['paths'] = [_strip_env(r, ts) for r in wan['paths']]
            out[key] = wan
        elif key in _ENV_LISTS and isinstance(val, list):
            out[key] = [_strip_env(r, ts) for r in val]
        else:
            out[key] = val
    if obs.get('collector_errors'):
        out['collector_errors'] = obs['collector_errors']
    cv = obs.get('config_versions')
    if cv and not recording:
        out['cv'] = {n: (e.get('version') if isinstance(e, dict) else e)
                     for n, e in cv.items()}
        changed = sorted(n for n, e in cv.items()
                         if isinstance(e, dict) and e.get('changed'))
        if changed:
            out['cvc'] = changed
    return out


def decode_observation(rec, job_id=None, recording=False,
                       fingerprint_lookup=None):
    """Rebuild the in-memory/API observation from its persisted form.

    Not reconstructable (never persisted): duration_ms (set to None) and,
    for Recording samples, config_versions (the job metadata carries the
    start/end version refs).
    """
    ts = rec.get('collected_ts')
    obs = {
        'sample_source': 'recording' if recording else rec.get(
            'sample_source'),
        'job_id': job_id if recording else rec.get('job_id'),
        'refresh_group': None if recording else rec.get('refresh_group'),
        'timestamp': rec.get('timestamp', ts),
        'collected_ts': ts,
    }
    if recording:
        obs['recording_group'] = rec.get('recording_group')
    for key, val in rec.items():
        if key in ('collected_ts', 'sample_source', 'job_id',
                   'refresh_group', 'timestamp', 'recording_group', 'cv',
                   'cvc', 'collector_errors'):
            continue
        if key == 'wan' and isinstance(val, dict):
            wan = dict(val)
            if isinstance(wan.get('paths'), list):
                wan['paths'] = [_restore_env(r, ts) for r in wan['paths']]
            obs[key] = wan
        elif key in _ENV_LISTS and isinstance(val, list):
            obs[key] = [_restore_env(r, ts) for r in val]
        else:
            obs[key] = val
    obs['collector_errors'] = rec.get('collector_errors', [])
    obs['duration_ms'] = None
    if 'cv' in rec and isinstance(rec['cv'], dict):
        changed = set(rec.get('cvc') or ())
        cfg = {}
        for n, v in rec['cv'].items():
            fp = None
            if fingerprint_lookup is not None:
                try:
                    fp = fingerprint_lookup(n, v)
                except Exception:
                    fp = None
            cfg[n] = {'version': v, 'fingerprint': fp,
                      'changed': n in changed}
        obs['config_versions'] = cfg
    return obs
