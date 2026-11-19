"""Durable telemetry backend: segmented, append-oriented JSONL on local disk.

Layout (all under the app-local runtime root, default ``tmp/telemetry``)::

    state.json                 schema + run markers (atomic, reconstructible)
    config_versions.jsonl      one line per NEW semantic config version
    events.jsonl               app/storage/recovery/retention events
    history/YYYY-MM-DD.jsonl   scheduled/startup/manual observations (UTC day)
    recordings/<job_id>/
        metadata.json          atomic; job record + lifecycle events
        samples.jsonl          append-only Recording samples

Lifecycle: survives an app restart and a router reboot. An application
update/reinstall MAY wipe it (accepted; no migration across versions).

Design rules
------------
* High-volume data is append-only, one independently decodable JSON object per
  line. A torn trailing line is repaired on open; earlier records are never
  rewritten.
* Small rewritten files (state.json, metadata.json) are written atomically.
* Persistence failure NEVER stops collection: errors are counted and exposed
  through the Storage Inspector; the in-memory ring keeps working.
* RAM stays bounded: a ~20 observation ring for history, Recording METADATA
  and counts only (samples are read back from disk on demand).
* Retention deletes whole day files / whole Recording directories, never
  lines. Active Recording and the current UTC history file are never purged.

Only this module and persistence.py touch the filesystem for telemetry.
"""

import calendar
import os
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict, deque

import cp

from . import VERSION
from . import persistence as P
from . import query as Q
from . import recording
from . import storage

ROOT_REL = 'tmp/telemetry'
SCHEMA = 1

# Observations stamped before this are clearly pre-NTP (e.g. 1970): they stay
# in the RAM ring but are never written into a (wrong) UTC day file.
SANE_MIN_TS = 1704067200            # 2024-01-01T00:00:00Z

MIB = 1024 * 1024
DEFAULT_RETENTION = {
    'history_max_age_s': 7 * 86400,
    'history_max_bytes': 32 * MIB,
    'recordings_max_jobs': 10,
    'recordings_max_bytes': 32 * MIB,
}

HISTORY_RING = 20
CONFIG_COMPACT_BYTES = 512 * 1024
CONFIG_KEEP_VERSIONS = 20
EVENTS_ROTATE_BYTES = 256 * 1024
RECORDING_FSYNC_S = 30
RECORDING_CHECKPOINT_S = 60
DEDUPE_TAIL_BYTES = 65536
DEDUPE_RECORDING_MAX_AGE_S = 1800
# Recording tail for dedupe is TIME based (SWANS history is a ~5 min rolling
# window; 10 min leaves a margin) with a hard startup bound on bytes scanned.
DEDUPE_RECORDING_HORIZON_S = 600
DEDUPE_RECORDING_MAX_BYTES = 4 * 1024 * 1024
# While the previous process is still alive and shutting down (NCOS may start
# the new instance before the old one has finished), wait up to this long for
# it to mark its exit before judging how it ended.
OVERLAP_WAIT_S = 5.0
OVERLAP_POLL_S = 0.25
DEFAULT_QUERY_LIMIT = Q.DEFAULT_LIMIT
DEFAULT_QUERY_BYTES = Q.MAX_SCAN_BYTES

_DAY_FILE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})\.jsonl\Z')
_DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}\Z')


def utc_day(ts):
    return time.strftime('%Y-%m-%d', time.gmtime(ts))


def day_start_ts(day):
    return calendar.timegm(time.strptime(day, '%Y-%m-%d'))


def _sleep(seconds):
    time.sleep(seconds)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def sane_ts(ts):
    return (isinstance(ts, (int, float)) and not isinstance(ts, bool)
            and ts >= SANE_MIN_TS)


class Health(object):
    """Write-health counters exposed through the Storage Inspector."""

    def __init__(self):
        self.write_errors = 0
        self.consecutive_errors = 0
        self.last_write_error = None
        self.last_write_error_ts = None
        self.last_fsync_ts = None
        self.held_observations = 0
        self.persisted_observations = 0
        self.clock_sane = True


class TelemetryStorage(object):
    """Owns the storage root, health, events, state, retention, recovery."""

    def __init__(self, root, display_root=ROOT_REL, clock=time.time,
                 ring=HISTORY_RING, retention=None):
        self.root = root
        self.display_root = display_root
        self._clock = clock
        self.lock = threading.RLock()
        self.health = Health()
        self.ok = False
        self.limits = dict(DEFAULT_RETENTION)
        if retention:
            self.limits.update(retention)
        self.state_path = os.path.join(root, 'state.json')
        self.config_path = os.path.join(root, 'config_versions.jsonl')
        self.events_path = os.path.join(root, 'events.jsonl')
        self.history_dir = os.path.join(root, 'history')
        self.recordings_dir = os.path.join(root, 'recordings')
        self.recovery = {
            'previous_exit': None, 'tail_repairs': 0,
            'metadata_recovered': 0, 'recordings_interrupted': 0,
            'config_versions_restored': 0, 'history_restored': 0,
            'recordings_loaded': 0, 'dedupe_swans': 0, 'dedupe_steering': 0,
            'dedupe_recording_scanned_bytes': 0, 'dedupe_skipped_lines': 0,
            'dedupe_recording_capped': False,
        }
        self.last_retention = None
        # Identity of THIS process in state.json. Overlapping old/new
        # processes share no memory, so ownership is decided by comparing
        # this ID with the one currently stored on disk.
        self.run_id = uuid.uuid4().hex
        self.state_owned = True
        # Set as soon as shutdown begins (SIGTERM). From then on nothing may
        # RECREATE the telemetry tree: during an upgrade the installer can
        # remove it while this (old) process is still finishing, and a
        # recreated directory would be owned by the old process's user and
        # block the new version.
        self.closing = False
        self.init_error = None
        self._state = {}
        self._in_error_event = False
        self._events = P.JsonlAppender(
            self.events_path, fsync_interval=0, clock=clock,
            on_repair=self.on_repair, on_sync=self.on_sync)
        self.config_store = DurableConfigVersionStore(self)
        self.history_store = DurableHistoryStore(self, ring)
        self.recording_store = DurableRecordingStore(self)

    # --- health / events ----------------------------------------------------

    def on_sync(self, ts):
        self.health.last_fsync_ts = int(ts)

    def on_repair(self, path, rep):
        self.recovery['tail_repairs'] += 1
        self.log_event('tail_repaired', file=self.rel(path),
                       action=rep.get('action'),
                       dropped_bytes=rep.get('dropped_bytes', 0))

    def rel(self, path):
        try:
            return os.path.relpath(path, self.root).replace(os.sep, '/')
        except Exception:
            return os.path.basename(path)

    def record_error(self, where, exc):
        h = self.health
        h.write_errors += 1
        h.consecutive_errors += 1
        h.last_write_error = '{}: {}'.format(where, exc)[:200]
        h.last_write_error_ts = int(self._clock())
        if h.write_errors <= 3 or h.write_errors % 100 == 0:
            cp.log('sdwan_analyzer storage: write error #{} ({}): {}'.format(
                h.write_errors, where, exc))
        if (not self._in_error_event
                and (h.write_errors == 1 or h.write_errors % 100 == 0)):
            self._in_error_event = True
            try:
                self.log_event('write_error', where=where,
                               error=str(exc)[:160],
                               write_errors=h.write_errors)
            finally:
                self._in_error_event = False

    def begin_shutdown(self):
        self.closing = True

    def root_alive(self):
        return os.path.isdir(self.root)

    def _write_forbidden(self):
        """True when shutting down and the root has been removed."""
        return self.closing and not self.root_alive()

    def guarded(self, where, fn):
        """Run a write; count and swallow failures. True on success."""
        if self._write_forbidden():
            return False        # benign: storage was removed under us
        try:
            with self.lock:
                fn()
            self.health.consecutive_errors = 0
            return True
        except Exception as e:
            self.record_error(where, e)
            return False

    def log_event(self, event_type, **detail):
        if self._write_forbidden():
            return
        rec = {'ts': int(self._clock()), 'event': event_type}
        rec.update(detail)
        try:
            with self.lock:
                if P.file_size(self.events_path) >= EVENTS_ROTATE_BYTES:
                    self._events.close()
                    os.replace(self.events_path,
                               os.path.join(self.root, 'events.1.jsonl'))
                    P.fsync_directory(self.root)
                self._events.append(rec)
        except Exception as e:
            if not self._in_error_event:
                self.record_error('events', e)

    # --- lifecycle ----------------------------------------------------------

    def prepare(self):
        """Create the storage tree. False => caller keeps in-memory stores."""
        try:
            for d in (self.root, self.history_dir, self.recordings_dir):
                os.makedirs(d, exist_ok=True)
            self.ok = True
        except Exception as e:
            self.ok = False
            self.init_error = {'type': type(e).__name__,
                               'message': self._sanitize(e)}
            self.record_error('prepare', e)
        return self.ok

    def _sanitize(self, exc):
        """Error text without absolute filesystem paths."""
        msg = str(exc)
        app_root = os.path.dirname(os.path.dirname(self.root))
        msg = msg.replace(self.root, self.display_root)
        if app_root:
            msg = msg.replace(app_root + os.sep, '')
        return msg[:200]

    def recover(self, swans_seen, event_seen):
        """Startup recovery; every step is isolated so one failure degrades
        only that area."""
        prev = self._read_previous_state()
        # Claim state.json immediately (not after the slower recovery steps):
        # a still-running predecessor then sees a foreign run_id at once.
        self._write_state(prev, running=True)
        for name, fn in (
                ('config_versions', self.config_store.load),
                ('recordings', self.recording_store.load),
                ('history', self.history_store.load),
                ('dedupe', lambda: self.rebuild_dedupe(swans_seen,
                                                       event_seen)),
                ('retention', lambda: self.retention_sweep('startup')),
        ):
            try:
                fn()
            except Exception as e:
                self.record_error('recover_' + name, e)
        self._write_state(prev, running=True)
        return dict(self.recovery)

    def _read_previous_state(self):
        try:
            obj, status = P.recover_json(self.state_path)
        except Exception as e:
            obj, status = None, 'error'
            self.record_error('state_read', e)
        prev = obj if isinstance(obj, dict) else {}
        if status in ('ok', 'promoted_tmp'):
            prev = self._wait_for_previous_instance(prev)
        if status == 'missing':
            exit_kind = 'first_run'
        elif obj is None:
            exit_kind = 'unknown'
            self.log_event('state_recovered', status=status)
        elif prev.get('running') or not prev.get('clean_stop'):
            exit_kind = 'unclean'
            self.log_event('unclean_prior_exit',
                           last_start_ts=prev.get('last_start_ts'))
        else:
            exit_kind = 'clean'
        if status == 'promoted_tmp':
            self.log_event('state_recovered', status=status)
        self.recovery['previous_exit'] = exit_kind
        return prev

    def _wait_for_previous_instance(self, prev):
        """Give a still-running predecessor time to finish its shutdown.

        Only when state.json says "running" with another LIVE pid. Returns
        the (possibly updated) previous state. Bounded by OVERLAP_WAIT_S.
        """
        pid = prev.get('pid')
        if (not prev.get('running') or not isinstance(pid, int)
                or pid == os.getpid() or not _pid_alive(pid)):
            return prev
        self.recovery['waited_for_previous_instance'] = True
        deadline = time.monotonic() + OVERLAP_WAIT_S
        while time.monotonic() < deadline:
            _sleep(OVERLAP_POLL_S)
            try:
                cur = P.read_json(self.state_path)
            except Exception:
                continue
            if not cur.get('running') or not _pid_alive(pid):
                return cur
        return prev

    def _owns_state(self):
        """(owned, stored_run_id): does state.json still carry OUR run_id?

        A missing/unreadable file has no other owner, so we may write it.
        """
        try:
            cur = P.read_json(self.state_path)
        except Exception:
            return True, None
        rid = cur.get('run_id')
        return rid == self.run_id, rid

    def _write_state(self, prev, running, clean=False, create_parent=True):
        now = int(self._clock())
        st = {
            'schema': SCHEMA,
            'run_id': self.run_id,
            'pid': os.getpid(),
            'app_version': VERSION,
            'created_ts': prev.get('created_ts') or now,
            'last_start_ts': (now if running and not clean else
                              prev.get('last_start_ts')
                              or self._state.get('last_start_ts')),
            'running': bool(running),
            'clean_stop': bool(clean),
            'last_clean_stop_ts': (now if clean else
                                   prev.get('last_clean_stop_ts')),
            'previous_exit': self.recovery.get('previous_exit'),
            'recovery': dict(self.recovery),
        }
        self._state = st
        self.guarded('state', lambda: P.write_json_atomic(
            self.state_path, st, create_parent=create_parent))

    def shutdown(self):
        """Graceful stop: fsync/close everything, mark a clean exit.

        The clean marker is written ONLY while the telemetry root still exists
        (shutdown never creates directories) AND state.json still carries this
        process's run_id. If a newer process has already claimed the file,
        its state is left untouched (benign mismatch event) so its
        running/clean_stop markers stay truthful.
        """
        self.begin_shutdown()
        for fn in (self.recording_store.close, self.history_store.close):
            try:
                fn()
            except Exception as e:
                self.record_error('shutdown', e)
        if not self.root_alive():
            # The installer (or anything else) removed the telemetry root
            # while we were stopping. Never recreate it: skip the clean
            # marker entirely.
            self.state_owned = False
            cp.log('sdwan_analyzer storage: telemetry root is gone at '
                   'shutdown; clean-state write skipped')
        else:
            owned, stored = self._owns_state()
            if owned:
                self._write_state(self._state, running=False, clean=True,
                                  create_parent=False)
            else:
                self.state_owned = False
                self.log_event('state_ownership_mismatch',
                               own_run_id=self.run_id,
                               current_run_id=stored)
        try:
            self._events.close()
        except Exception as e:
            self.record_error('shutdown', e)

    # --- dedupe -------------------------------------------------------------

    def rebuild_dedupe(self, swans_seen, event_seen):
        """Rebuild recent SWANS / steering-event dedupe keys from disk.

        History: the last ~64 KB of the newest non-empty day file (a few
        sparse observations are enough).
        Recordings: TIME based. Starting from the newest recent Recording,
        scan its samples backward until a sample is older than
        (newest sample - 10 min) or the shared 4 MiB budget is spent. More
        than one Recording can matter (two short jobs back to back inside the
        10-minute window), so older jobs are visited newest-first with the
        SAME cutoff, until one ends before it. Key shapes are exactly those
        built by telemetry.swans_new_samples / steering_new_events.
        """
        now = self._clock()
        recs = []
        for name in reversed(self.history_store.day_files()):
            path = os.path.join(self.history_dir, name)
            if P.file_size(path) > 0:
                recs.extend(P.read_tail_records(path, DEDUPE_TAIL_BYTES))
                break
        scanned = skipped = 0
        capped = False
        cutoff = None
        for job in self.recording_store.recent_jobs():
            end = job.get('ended_ts') or now
            if cutoff is None:
                if (job['state'] != 'recording'
                        and now - end > DEDUPE_RECORDING_MAX_AGE_S):
                    break               # newest job is too old to matter
            elif end < cutoff:
                break                   # wholly outside the shared window
            budget = DEDUPE_RECORDING_MAX_BYTES - scanned
            if budget <= 0:
                capped = True
                break
            rr, info = P.read_recent_window(
                self.recording_store.samples_path(job['job_id']),
                DEDUPE_RECORDING_HORIZON_S, budget, cutoff_ts=cutoff)
            recs.extend(rr)
            scanned += info['scanned_bytes']
            skipped += info['skipped']
            capped = capped or info['capped']
            if cutoff is None and info['newest_ts'] is not None:
                cutoff = info['newest_ts'] - DEDUPE_RECORDING_HORIZON_S
        self.recovery['dedupe_recording_scanned_bytes'] = scanned
        self.recovery['dedupe_skipped_lines'] = skipped
        self.recovery['dedupe_recording_capped'] = capped
        if skipped:
            self.log_event('dedupe_skipped_lines', count=skipped)
        recs.sort(key=lambda r: r.get('collected_ts') or 0)
        n_sw = n_ev = 0
        for rec in recs:
            for s in rec.get('swans_samples') or ():
                if (isinstance(s, dict) and s.get('uid')
                        and s.get('metric_ts') is not None):
                    swans_seen.add((s['uid'], s['metric_ts']))
                    n_sw += 1
            for e in rec.get('steering_events') or ():
                if isinstance(e, dict) and e.get('metric_ts') is not None:
                    event_seen.add((e.get('steering_rule'), e['metric_ts'],
                                    str(e.get('reason')),
                                    e.get('wan_from_uid'),
                                    e.get('wan_to_uid')))
                    n_ev += 1
        self.recovery['dedupe_swans'] = n_sw
        self.recovery['dedupe_steering'] = n_ev

    # --- retention ----------------------------------------------------------

    def retention_sweep(self, reason):
        """Whole-file retention for history and Recordings. Never raises."""
        if self._write_forbidden():
            return
        try:
            with self.lock:
                now = self._clock()
                h = self.history_store.sweep(now)
                r = self.recording_store.sweep(now)
                self.last_retention = {
                    'ts': int(now), 'reason': reason,
                    'history_deleted': len(h), 'recordings_deleted': len(r)}
        except Exception as e:
            self.record_error('retention', e)

    # --- inspector ----------------------------------------------------------

    def free_space(self):
        try:
            if not hasattr(os, 'statvfs'):
                return {'available': False, 'error': 'statvfs unsupported'}
            probe = self.root
            while probe and not os.path.exists(probe):
                parent = os.path.dirname(probe)
                if parent == probe:
                    break
                probe = parent
            st = os.statvfs(probe or '.')
            return {'available': True,
                    'free_bytes': int(st.f_bavail * st.f_frsize),
                    'total_bytes': int(st.f_blocks * st.f_frsize)}
        except Exception as e:
            return {'available': False, 'error': str(e)[:80]}

    def inspector_info(self):
        h = self.health
        return {
            'enabled': bool(self.ok),
            'root': self.display_root,
            'abs_root': self.root,
            'run_id': self.run_id,
            'status': ('degraded' if h.consecutive_errors else 'ok'),
            'write_errors': h.write_errors,
            'last_write_error': h.last_write_error,
            'last_write_error_ts': h.last_write_error_ts,
            'last_fsync_ts': h.last_fsync_ts,
            'clock_sane': h.clock_sane,
            'held_observations': h.held_observations,
            'persisted_observations': h.persisted_observations,
            'retention': dict(self.limits, last=self.last_retention),
            'recovery': dict(self.recovery),
            'free_space': self.free_space(),
        }


# --- config versions ---------------------------------------------------------

def _valid_cfg_record(r):
    return (isinstance(r, dict) and isinstance(r.get('name'), str)
            and isinstance(r.get('version'), int)
            and not isinstance(r.get('version'), bool) and r['version'] >= 1
            and 'fingerprint' in r and 'payload' in r)


class DurableConfigVersionStore(storage.ConfigVersionStore):
    """ConfigVersionStore that appends each NEW version to disk (fsync)."""

    def __init__(self, tel, max_versions=CONFIG_KEEP_VERSIONS):
        storage.ConfigVersionStore.__init__(self, max_versions)
        self._t = tel
        self._app = P.JsonlAppender(
            tel.config_path, fsync_interval=0, clock=tel._clock,
            on_repair=tel.on_repair, on_sync=tel.on_sync)

    def update(self, name, fingerprint, payload, ts):
        # Only a genuinely NEW version is appended; returning to a retained
        # version just moves the in-memory current pointer (no duplicate
        # payload record on disk).
        res, is_new = self._update(name, fingerprint, payload, ts)
        if is_new:
            rec = {'name': name, 'version': res['version'],
                   'fingerprint': fingerprint, 'ts': ts, 'payload': payload}
            self._t.guarded('config_versions',
                            lambda: self._app.append(rec))
            if P.file_size(self._t.config_path) > CONFIG_COMPACT_BYTES:
                self._t.guarded('config_compact', self.compact)
        return res

    def _read_valid(self, path):
        stats = {}
        recs = [r for _, _, r in P.iter_jsonl(path, stats=stats)
                if _valid_cfg_record(r)]
        return recs, stats.get('skipped', 0)

    def load(self):
        """Restore versions + counters. Version numbers stay continuous."""
        path = self._t.config_path
        tmp, bak = path + '.tmp', path + '.bak'
        recs, skipped, source = [], 0, 'primary'
        if os.path.exists(path):
            P.repair_jsonl_tail(path)
            recs, skipped = self._read_valid(path)
            if recs or P.file_size(path) == 0:
                P.remove_quiet(tmp)
            else:
                P.quarantine(path)
        if not recs and not os.path.exists(path):
            for cand, label in ((tmp, 'tmp'), (bak, 'bak')):
                if not os.path.exists(cand):
                    continue
                recs, skipped = self._read_valid(cand)
                if recs:
                    if label == 'tmp':
                        os.replace(cand, path)
                    else:
                        shutil.copyfile(cand, path)
                    P.fsync_directory(self._t.root)
                    source = label
                    self._t.log_event('config_versions_recovered',
                                      source=label, records=len(recs))
                    break
        if skipped:
            self._t.log_event('config_versions_skipped_lines', count=skipped)
        by_name = {}
        for r in recs:
            by_name.setdefault(r['name'], {})[r['version']] = r
        self._versions = {}
        self._counter = {}
        self._current = {}
        for name, versions in by_name.items():
            ordered = [versions[v] for v in sorted(versions)]
            buf = deque(maxlen=self._max)
            for r in ordered[-self._max:]:
                buf.append({'version': r['version'],
                            'fingerprint': r['fingerprint'],
                            'ts': r.get('ts'), 'payload': r['payload']})
            self._versions[name] = buf
            self._counter[name] = ordered[-1]['version']
            # Which version was current at shutdown is not persisted (reuse
            # appends nothing); the newest number is the best estimate and
            # the first collection corrects it.
            self._current[name] = ordered[-1]['version']
        total = sum(len(b) for b in self._versions.values())
        self._t.recovery['config_versions_restored'] = total
        return source

    def compact(self):
        """Rewrite keeping the last N versions/object: tmp, fsync, validate,
        .bak, replace. Not called per write."""
        path = self._t.config_path
        tmp, bak = path + '.tmp', path + '.bak'
        self._app.close()
        rows = []
        for name, buf in self._versions.items():
            for v in buf:
                rows.append({'name': name, 'version': v['version'],
                             'fingerprint': v['fingerprint'],
                             'ts': v['ts'], 'payload': v['payload']})
        rows.sort(key=lambda r: (r['ts'] or 0, r['name'], r['version']))
        with open(tmp, 'wb') as f:
            for r in rows:
                f.write(P.dumps(r).encode('utf-8') + b'\n')
            f.flush()
            os.fsync(f.fileno())
        good, _ = self._read_valid(tmp)
        if len(good) != len(rows):
            P.remove_quiet(tmp)
            raise IOError('compacted config versions failed validation')
        if os.path.exists(path):
            os.replace(path, bak)
            P.fsync_directory(self._t.root)
        try:
            os.replace(tmp, path)
        except Exception:
            if not os.path.exists(path) and os.path.exists(bak):
                os.replace(bak, path)
            raise
        P.fsync_directory(self._t.root)
        self._t.log_event('config_versions_compacted', records=len(rows))


# --- history -----------------------------------------------------------------

class DurableHistoryStore(storage.HistoryStore):
    """Scheduled/startup/manual observations: RAM ring + UTC day files."""

    def __init__(self, tel, ring=HISTORY_RING):
        self._t = tel
        self._ring = deque(maxlen=ring)
        self._app = None
        self._day = None

    # interface ---------------------------------------------------------------

    def append(self, sample):
        """Ring always; disk only when the clock is sane. Never raises."""
        try:
            self._ring.append(sample)
        except Exception:
            pass
        h = self._t.health
        ts = sample.get('collected_ts') if isinstance(sample, dict) else None
        if not sane_ts(ts):
            h.held_observations += 1
            if h.clock_sane:
                h.clock_sane = False
                cp.log('sdwan_analyzer storage: clock not sane (ts={}); '
                       'observation kept in memory only'.format(ts))
                self._t.log_event('clock_insane', collected_ts=ts)
            return
        h.clock_sane = True
        if self._t._write_forbidden():
            return
        try:
            rec = P.encode_observation(sample)
            day = utc_day(ts)
            with self._t.lock:
                if day != self._day:
                    self._switch_day(day)
                self._app.append(rec)
            h.persisted_observations += 1
            h.consecutive_errors = 0
        except Exception as e:
            self._t.record_error('history', e)

    def recent(self, limit=100):
        items = list(self._ring)
        if limit is None or limit >= len(items):
            return items
        return items[-limit:]

    def stats(self):
        return {'backend': 'durable-jsonl', 'count': len(self._ring),
                'durable': True}

    def max_observations(self):
        return self._ring.maxlen

    # files ---------------------------------------------------------------------

    def day_path(self, day):
        return os.path.join(self._t.history_dir, day + '.jsonl')

    def day_files(self):
        try:
            names = os.listdir(self._t.history_dir)
        except OSError:
            return []
        return sorted(n for n in names if _DAY_FILE_RE.match(n))

    def _switch_day(self, day):
        rollover = self._day is not None
        if self._app is not None:
            self._app.close()
        self._app = P.JsonlAppender(
            self.day_path(day), fsync_interval=0, clock=self._t._clock,
            on_repair=self._t.on_repair, on_sync=self._t.on_sync)
        self._day = day
        if rollover:
            self._t.retention_sweep('day_rollover')

    def close(self):
        if self._app is not None:
            self._app.close()

    # recovery ------------------------------------------------------------------

    def load(self):
        """Repair the newest file's tail and fill the RAM ring (~20 obs)."""
        files = self.day_files()
        if files:
            path = os.path.join(self._t.history_dir, files[-1])
            rep = P.repair_jsonl_tail(path)
            if rep['action'] != 'none':
                self._t.on_repair(path, rep)
        need = self._ring.maxlen
        collected = []
        lookup = self._t.config_store.fingerprint_for
        for name in reversed(files):
            if need <= 0:
                break
            recs = P.read_newest(os.path.join(self._t.history_dir, name),
                                 need)
            collected = recs + collected
            need -= len(recs)
        for rec in collected[-self._ring.maxlen:]:
            self._ring.append(P.decode_observation(
                rec, fingerprint_lookup=lookup))
        self._t.recovery['history_restored'] = len(self._ring)

    # paged durable reads (GET /api/history) ----------------------------------------

    def query(self, start=None, end=None, limit=DEFAULT_QUERY_LIMIT,
              cursor=None, max_bytes=DEFAULT_QUERY_BYTES, sample_source=None,
              window_start=None, max_accept_bytes=Q.MAX_RESPONSE_BYTES):
        """Decoded observations in [start, end], oldest first, paged.

        Returns {'samples', 'count', 'next_cursor', 'skipped_lines',
        'scanned_bytes'}. Read-only: files are opened 'rb', nothing is
        repaired, created or written.

        start/end:      inclusive UTC epoch bounds (None = open).
        window_start:   default lower bound used ONLY when neither start nor
                        end was given and there is no cursor.
        sample_source:  exact filter on the persisted source tag.
        cursor:         opaque token from a previous call (same start/end/
                        sample_source). It pins the resolved lower bound, so
                        a default 24 h window does not slide between pages.
        Bounds per call: `limit` records, `max_bytes` of file data examined
        (filtered-out and malformed lines included), `max_accept_bytes` of
        accepted records. A page may hold fewer than `limit` records, or none
        while next_cursor is set.
        Raises query.QueryError: 400 invalid cursor, 410 cursor's day file
        has been removed by retention.
        """
        limit = max(1, int(limit))
        ctx = Q.context_hash('h', start, end, sample_source)
        eff_start = start if start is not None else (
            window_start if end is None else None)
        c_day, c_off, resync = None, 0, False
        if cursor is not None:
            doc = Q.decode_cursor(cursor, 'h', ctx)
            c_day = doc.get('d')
            if not isinstance(c_day, str) or not _DAY_RE.match(c_day):
                raise Q.bad('invalid cursor')
            try:
                day_start_ts(c_day)
            except ValueError:
                raise Q.bad('invalid cursor')
            c_off = Q.int_field(doc, 'o')
            resync = doc.get('r') is True
            eff_start = doc.get('s')
            if eff_start is not None and (
                    isinstance(eff_start, bool)
                    or not isinstance(eff_start, (int, float))):
                raise Q.bad('invalid cursor')
            if ((eff_start is not None and c_day < utc_day(eff_start))
                    or (end is not None and c_day > utc_day(end))):
                raise Q.bad('cursor is outside the query range')
            now = self._t._clock()
            if sane_ts(now) and c_day > utc_day(now + 86400):
                raise Q.bad('invalid cursor')       # a day not yet reached
        files = self.day_files()
        selected = []
        for name in files:
            day = name[:-6]
            if c_day is not None and day < c_day:
                continue
            ds = day_start_ts(day)
            if end is not None and ds > end:
                continue
            if eff_start is not None and ds + 86400 <= eff_start:
                continue
            selected.append(day)
        if c_day is not None:
            if c_day not in selected:
                raise Q.expired('cursor expired: that history segment has '
                                'been removed')
            state = P.offset_state(self.day_path(c_day), c_off)
            if state == 'missing':
                raise Q.expired('cursor expired: that history segment has '
                                'been removed')
            if state == 'beyond' or (state == 'mid' and not resync):
                raise Q.bad('invalid cursor')
        accept = None
        if sample_source is not None:
            accept = lambda r: r.get('sample_source') == sample_source
        floor = max_bytes < P.QUERY_MAX_LINE + 1
        lookup = self._t.config_store.fingerprint_for
        out, skipped, scanned, accepted = [], 0, 0, 0
        next_cursor = None

        def mint(day, off, rs):
            return Q.encode_cursor({'t': 'h', 'q': ctx, 'd': day, 'o': off,
                                    's': eff_start, 'r': bool(rs)})

        for day in selected:
            path = self.day_path(day)
            off = c_off if day == c_day else 0
            rs = resync if day == c_day else False
            if (scanned >= max_bytes or len(out) >= limit
                    or accepted >= max_accept_bytes):
                if P.file_size(path) > off:
                    next_cursor = mint(day, off, rs)
                    break
                continue
            res = P.scan_range(
                path, off, start_ts=eff_start, end_ts=end, limit=limit - len(out),
                max_bytes=max_bytes - scanned,
                max_accept_bytes=max_accept_bytes - accepted, accept=accept,
                resync=rs, stop_past_end=True, progress_floor=floor)
            if res['missing']:
                continue                # removed by retention mid-request
            scanned += res['scanned_bytes']
            accepted += res['accepted_bytes']
            skipped += res['skipped']
            for r in res['records']:
                out.append(P.decode_observation(r, fingerprint_lookup=lookup))
            if res['past_end']:
                break
            if not res['eof']:
                next_cursor = mint(day, res['next_offset'], res['resync'])
                break
        return {'samples': out, 'count': len(out),
                'next_cursor': next_cursor, 'skipped_lines': skipped,
                'scanned_bytes': scanned}

    # retention -----------------------------------------------------------------

    def sweep(self, now):
        """Delete whole day files past the age / size limits (oldest first).
        Today's UTC file and the file being appended are never deleted."""
        lim = self._t.limits
        protected = {utc_day(now), self._day}
        entries = []
        for name in self.day_files():
            path = os.path.join(self._t.history_dir, name)
            size = P.file_size(path) + P.file_size(path + '.corrupt')
            entries.append([name, name[:-6], size])
        deleted = []

        def drop(entry, reason):
            path = os.path.join(self._t.history_dir, entry[0])
            P.remove_quiet(path)
            P.remove_quiet(path + '.corrupt')
            entries.remove(entry)
            deleted.append(entry[0])
            self._t.log_event('retention_delete', kind='history',
                              file=entry[0], reason=reason, bytes=entry[2])

        cutoff = now - lim['history_max_age_s']
        for entry in list(entries):
            if entry[1] in protected:
                continue
            if day_start_ts(entry[1]) + 86400 <= cutoff:
                drop(entry, 'age')
        total = sum(e[2] for e in entries)
        for entry in list(entries):
            if total <= lim['history_max_bytes']:
                break
            if entry[1] in protected:
                continue
            total -= entry[2]
            drop(entry, 'size')
        if deleted:
            P.fsync_directory(self._t.history_dir)
        return deleted


# --- recordings --------------------------------------------------------------

def _valid_job(job, name):
    return (isinstance(job, dict) and job.get('job_id') == name
            and job.get('state') in recording.STATES
            and isinstance(job.get('started_ts'), (int, float)))


class DurableRecordingStore(storage.RecordingStore):
    """Recording jobs on disk: metadata + counts in RAM, samples on disk."""

    def __init__(self, tel, max_samples=3000):
        storage.RecordingStore.__init__(
            self, max_jobs=tel.limits['recordings_max_jobs'],
            max_samples=max_samples)
        self._t = tel
        self._counts = {}
        self._app = {}
        self._fallback = {}      # failed-write samples (degraded, bounded)
        self._last_ckpt = {}

    # paths ---------------------------------------------------------------------

    def job_dir(self, job_id):
        return P.safe_job_dir(self._t.recordings_dir, job_id)

    def samples_path(self, job_id):
        return os.path.join(self.job_dir(job_id), 'samples.jsonl')

    def meta_path(self, job_id):
        return os.path.join(self.job_dir(job_id), 'metadata.json')

    # metadata ------------------------------------------------------------------

    def _write_meta(self, job):
        jid = job['job_id']
        now = int(self._t._clock())
        doc = dict(job)
        doc['events'] = [dict(e) for e in self._events
                         if e.get('job_id') == jid]
        doc['sample_count'] = self._counts.get(jid, 0)
        doc['schema'] = SCHEMA
        doc['checkpoint_ts'] = now
        ok = self._t.guarded('recording_metadata', lambda: P.write_json_atomic(
            self.meta_path(jid), doc))
        self._last_ckpt[jid] = self._t._clock()
        return ok

    def add_job(self, job):
        jid = job['job_id']
        self.job_dir(jid)                       # validates the ID
        self._jobs[jid] = job
        self._counts[jid] = 0
        self._write_meta(job)

    def job_changed(self, job, final=False, force_sync=False):
        jid = job['job_id']
        app = self._app.get(jid)
        if app is not None and (final or force_sync):
            self._t.guarded('recording_fsync', app.sync)
        self._write_meta(job)
        if final:
            if app is not None:
                self._t.guarded('recording_close', app.close)
                self._app.pop(jid, None)
            self._t.retention_sweep('recording_complete')

    def housekeeping(self, now):
        """From the 1 Hz tick: periodic fsync + metadata checkpoint."""
        for jid, job in list(self._jobs.items()):
            if job.get('state') != 'recording':
                continue
            app = self._app.get(jid)
            if app is not None:
                self._t.guarded('recording_fsync', app.maybe_sync)
            if now - self._last_ckpt.get(jid, 0) >= RECORDING_CHECKPOINT_S:
                self._write_meta(job)

    # samples -------------------------------------------------------------------

    def _appender(self, jid):
        app = self._app.get(jid)
        if app is None:
            app = P.JsonlAppender(
                self.samples_path(jid), fsync_interval=RECORDING_FSYNC_S,
                clock=self._t._clock, on_repair=self._t.on_repair,
                on_sync=self._t.on_sync)
            self._app[jid] = app
        return app

    def append_sample(self, job_id, obs):
        if job_id not in self._jobs:
            return False
        n = self._counts.get(job_id, 0)
        if n >= self._max_samples:
            return False
        rec = P.encode_observation(obs, recording=True)
        ok = self._t.guarded(
            'recording_sample', lambda: self._appender(job_id).append(rec))
        if not ok:
            buf = self._fallback.setdefault(job_id, [])
            if len(buf) < self._max_samples:
                buf.append(obs)
        self._counts[job_id] = n + 1
        return True

    def recording_samples(self, job_id, limit=None):
        if job_id not in self._jobs:
            return []
        path = self.samples_path(job_id)
        if limit is None:
            recs = [r for _, _, r in P.iter_jsonl(path)]
        else:
            recs = P.read_newest(path, limit)
        out = [P.decode_observation(r, job_id=job_id, recording=True)
               for r in recs]
        fb = self._fallback.get(job_id)
        if fb:
            out = sorted(out + list(fb),
                         key=lambda o: o.get('collected_ts') or 0)
            if limit is not None:
                out = out[-limit:]
        return out

    def query_samples(self, job_id, limit=DEFAULT_QUERY_LIMIT, group=None,
                      after=None, cursor=None, max_bytes=DEFAULT_QUERY_BYTES,
                      max_accept_bytes=Q.MAX_RESPONSE_BYTES):
        """Bounded, disk-backed Recording sample page (oldest first).

        Same return shape as DurableHistoryStore.query. Never loads the whole
        sample file and never reads RAM fallback samples. Complete JSONL
        lines only, so an active Recording can be paged while it appends.
        Raises query.QueryError: 404 unknown job (or malformed id), 400 bad
        cursor, 410 the cursor's Recording/sample file no longer exists.
        """
        limit = max(1, int(limit))
        if not P.valid_job_id(job_id):
            raise Q.QueryError(404, 'not found')
        ctx = Q.context_hash('r', job_id, group, after)
        doc = None
        if cursor is not None:
            doc = Q.decode_cursor(cursor, 'r', ctx)
        if job_id not in self._jobs:
            if doc is not None:
                raise Q.expired('cursor expired: that Recording has been '
                                'removed')
            raise Q.QueryError(404, 'not found')
        path = self.samples_path(job_id)
        c_off, resync = 0, False
        if doc is not None:
            c_off = Q.int_field(doc, 'o')
            resync = doc.get('r') is True
            state = P.offset_state(path, c_off)
            if state == 'missing':
                raise Q.expired('cursor expired: Recording samples are '
                                'gone')
            if state == 'beyond' or (state == 'mid' and not resync):
                raise Q.bad('invalid cursor')
        accept = None
        if group is not None:
            accept = lambda r: r.get('recording_group') == group
        res = P.scan_range(
            path, c_off, after_ts=after, limit=limit, max_bytes=max_bytes,
            max_accept_bytes=max_accept_bytes, accept=accept, resync=resync,
            progress_floor=max_bytes < P.QUERY_MAX_LINE + 1)
        out = [P.decode_observation(r, job_id=job_id, recording=True)
               for r in res['records']]
        nxt = None
        if not res['eof']:
            nxt = Q.encode_cursor({'t': 'r', 'q': ctx, 'o': res['next_offset'],
                                   'r': bool(res['resync'])})
        return {'samples': out, 'count': len(out), 'next_cursor': nxt,
                'skipped_lines': res['skipped'],
                'scanned_bytes': res['scanned_bytes']}

    def sample_count(self, job_id):
        return self._counts.get(job_id, 0)

    def info(self):
        active = self.active_recording()
        by_job = dict(self._counts)
        return {'job_count': len(by_job),
                'active_job_id': active['job_id'] if active else None,
                'total_sample_count': sum(by_job.values()),
                'samples_by_job': by_job,
                'event_count': len(self._events),
                'configured_max_jobs': self._max_jobs,
                'configured_max_samples_per_job': self._max_samples,
                'samples_in_ram': sum(len(v) for v in
                                      self._fallback.values())}

    def recent_jobs(self):
        """Jobs newest first (by start time)."""
        return sorted(self._jobs.values(), key=lambda j: j['started_ts'],
                      reverse=True)

    def latest_job(self):
        best = None
        for job in self._jobs.values():
            if best is None or job['started_ts'] >= best['started_ts']:
                best = job
        return best

    def close(self):
        for jid, app in list(self._app.items()):
            self._t.guarded('recording_close', app.close)
        self._app.clear()

    # recovery ------------------------------------------------------------------

    def _scan_samples(self, path):
        counts, first, last = {}, None, None
        for _, _, r in P.iter_jsonl(path):
            g = r.get('recording_group')
            counts[g] = counts.get(g, 0) + 1
            ts = r.get('collected_ts')
            if isinstance(ts, (int, float)):
                first = ts if first is None else min(first, ts)
                last = ts if last is None else max(last, ts)
        return counts, first, last

    def _blank_counts(self, by_group):
        out = {g + '_samples': 0 for g, _, _ in recording.PROFILE}
        out['config_samples'] = 0
        for g, n in by_group.items():
            if g:
                out[g + '_samples'] = n
        return out

    def _rebuild_job(self, name, samples_path):
        counts, first, last = self._scan_samples(samples_path)
        try:
            name_ts = int(name.split('-')[1])
        except Exception:
            name_ts = 0
        start = int(first) if first is not None else name_ts
        return {
            'job_id': name, 'label': '', 'notes': '', 'state': 'failed',
            'started_ts': start,
            'ended_ts': int(last) if last is not None else start,
            'max_duration_sec': recording.MAX_DURATION_SEC,
            'sample_profile': recording.PROFILE_NAME,
            'created_by_ui': False, 'stop_reason': 'metadata_recovered',
            'collector_error_count': 0, 'collector_health': {},
            'sample_counts': self._blank_counts(counts),
            'config_versions_start': None, 'config_versions_end': None,
            'config_changes': [],
        }, sum(counts.values())

    def load(self):
        """Load Recording METADATA only; recover interrupted/damaged jobs."""
        rdir = self._t.recordings_dir
        try:
            names = sorted(n for n in os.listdir(rdir)
                           if P.valid_job_id(n)
                           and os.path.isdir(os.path.join(rdir, n)))
        except OSError:
            names = []
        loaded = []
        now = int(self._t._clock())
        for name in names:
            spath = self.samples_path(name)
            if os.path.exists(spath):
                rep = P.repair_jsonl_tail(spath)
                if rep['action'] != 'none':
                    self._t.on_repair(spath, rep)
            try:
                job, status = P.recover_json(self.meta_path(name))
            except Exception:
                job, status = None, 'error'
            count = None
            events = []
            if _valid_job(job, name):
                events = [e for e in (job.pop('events', None) or ())
                          if isinstance(e, dict)]
                count = job.pop('sample_count', None)
                ckpt = job.pop('checkpoint_ts', None)
                job.pop('schema', None)
                if status == 'promoted_tmp':
                    self._t.log_event('metadata_promoted', job_id=name)
            else:
                if P.file_size(spath) == 0:
                    # Nothing recoverable: drop the empty orphan directory.
                    shutil.rmtree(self.job_dir(name), ignore_errors=True)
                    continue
                job, count = self._rebuild_job(name, spath)
                ckpt = None
                self._t.recovery['metadata_recovered'] += 1
                self._t.log_event('metadata_recovered', job_id=name,
                                  status=status)
                events = [self._event(job, recording.EV_FAILED,
                                      'metadata_recovered')]
                self._write_meta_direct(job, events, count)
            job['sample_counts'] = dict(
                self._blank_counts({}), **(job.get('sample_counts') or {}))
            if job['state'] == 'recording':
                counts, _, last = self._scan_samples(spath)
                job['sample_counts'] = self._blank_counts(counts)
                count = sum(counts.values())
                best = max(job['started_ts'], ckpt or 0,
                           int(last) if last is not None else 0)
                job['state'] = 'failed'
                job['stop_reason'] = 'app_restart'
                job['ended_ts'] = int(best)
                events.append(self._event(
                    job, recording.EV_FAILED, 'app_restart',
                    detail={'detected_ts': now}))
                self._t.recovery['recordings_interrupted'] += 1
                self._t.log_event('recording_interrupted', job_id=name,
                                  ended_ts=job['ended_ts'], samples=count)
                self._write_meta_direct(job, events, count)
            if not isinstance(count, int):
                counts, _, _ = self._scan_samples(spath)
                count = sum(counts.values())
            loaded.append((job, events, count))
        loaded.sort(key=lambda t: (t[0]['started_ts'], t[0]['job_id']))
        for job, events, count in loaded:
            self._jobs[job['job_id']] = job
            self._counts[job['job_id']] = count
            self._events.extend(events)
        if len(self._events) > 500:
            del self._events[:len(self._events) - 500]
        self._t.recovery['recordings_loaded'] = len(self._jobs)

    def _event(self, job, event_type, reason, detail=None):
        ev = {'job_id': job['job_id'],
              'event_ts': int(job.get('ended_ts') or job['started_ts']),
              'event_type': event_type, 'reason': reason}
        if detail is not None:
            ev['detail'] = detail
        return ev

    def _write_meta_direct(self, job, events, count):
        doc = dict(job)
        doc['events'] = events
        doc['sample_count'] = count
        doc['schema'] = SCHEMA
        doc['checkpoint_ts'] = int(self._t._clock())
        self._t.guarded('recording_metadata', lambda: P.write_json_atomic(
            self.meta_path(job['job_id']), doc))

    # retention -----------------------------------------------------------------

    def _dir_bytes(self, jid):
        d = self.job_dir(jid)
        total = 0
        try:
            for n in os.listdir(d):
                total += P.file_size(os.path.join(d, n))
        except OSError:
            pass
        return total

    def sweep(self, now):
        """Delete oldest COMPLETED Recordings past the job-count / size
        limits. The active Recording is never removed."""
        lim = self._t.limits
        sizes = {jid: self._dir_bytes(jid) for jid in self._jobs}
        total = sum(sizes.values())
        completed = sorted(
            ((j.get('ended_ts') or j['started_ts'], jid)
             for jid, j in self._jobs.items() if j['state'] != 'recording'))
        deleted = []
        while completed and (len(completed) > lim['recordings_max_jobs']
                             or total > lim['recordings_max_bytes']):
            reason = ('count' if len(completed) >
                      lim['recordings_max_jobs'] else 'size')
            _, jid = completed.pop(0)
            try:
                shutil.rmtree(self.job_dir(jid))
            except Exception as e:
                self._t.record_error('retention_rmtree', e)
                continue
            total -= sizes.get(jid, 0)
            self._jobs.pop(jid, None)
            self._counts.pop(jid, None)
            self._fallback.pop(jid, None)
            self._last_ckpt.pop(jid, None)
            app = self._app.pop(jid, None)
            if app is not None:
                try:
                    app.close()
                except Exception:
                    pass
            self._events = [e for e in self._events
                            if e.get('job_id') != jid]
            deleted.append(jid)
            self._t.log_event('retention_delete', kind='recording',
                              job_id=jid, reason=reason,
                              bytes=sizes.get(jid, 0))
        if deleted:
            P.fsync_directory(self._t.recordings_dir)
        return deleted
