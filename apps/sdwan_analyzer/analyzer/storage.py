"""Historical storage interface + bounded in-memory default stores.

This module defines the interface the rest of the app codes against and the
in-memory implementations (used when no durable root is configured, and by
tests). The DURABLE backend (stdlib-only segmented JSONL under
``tmp/telemetry``) lives in durable.py and subclasses these; the filesystem
primitives are in persistence.py. See those modules for the layout,
retention and recovery model.

The historical notes below describe the options originally considered and
are kept for context.

------------------------------------------------------------------------
NCOS SDK PERSISTENT-STORAGE OPTIONS & CONSTRAINTS (from project docs)
------------------------------------------------------------------------
Source: docs/NCOS_SDK_Developer_Guide.md, coding-standards.md, api-reference.md.

The four NCOS trees and their persistence:
  status/   runtime only, NOT persisted (this is where we READ from).
  config/   persisted in NVRAM (read/write). Includes appdata.
  control/  actions, not persisted.
  state/    internal/diagnostic, read-only.

Candidate persistence mechanisms for HISTORY:

  1. App-written files (app-relative or tmp/):
     - Router filesystem: relative paths only ('tmp/', never '/tmp'); must
       os.makedirs(exist_ok=True) before writing.
     - NEVER modify files that shipped in the package (breaks the digital
       signature -> router deletes the app). Only write NEW files.
     - No .pyc/.so; pure data files are fine (JSON, JSONL, plain text).
     - cppython is MISSING the 'csv' module (only a stub shim) -> for CSV
       use plain string join, or use JSON/JSONL which need no shim.
     - Flash per router is ~6-14 GB total but SHARED with the OS/other apps;
       history must be bounded + have retention. Sizing DEFERRED.
     - Survives reboot; survives app restart. Lost on app uninstall/redeploy
       purge unless written outside the app dir (needs validation).

  2. appdata (config/system/sdk/appdata):
     - Persisted, NCM-visible, good for SMALL user CONFIG (poll interval,
       retention window). NOT for time-series history (size + it overrides
       NCM group config if defaults are written — forbidden).
     - Read with cp.get_appdata('field'); never write defaults.

  3. In-memory only (this module's default):
     - Lost on restart/reboot. Fine for the skeleton and for a live
       rolling window, NOT for durable history.

  4. External sink (NCM alerts / MQTT / remote HTTP):
     - Off-device retention. Out of scope for the foundation; note as a
       possible tier for the storage design.

OPEN DECISIONS for the storage design step (DEFERRED):
  - format: JSONL append log vs periodic JSON snapshots vs rotating files.
  - retention: max age / max size / max rows; rotation strategy.
  - sampling interval: how often collectors run and how often we persist.
  - durability across redeploy: confirm which paths survive make.py purge.
"""

from collections import OrderedDict, deque


class HistoryStore(object):
    """Abstract history store. Concrete backends implement these methods.

    A "sample" is a source-tagged OBSERVATION dict (see
    service.AnalyzerService.collect): sample_source, collected_ts, optional
    job_id, and timestamp-enveloped runtime records. Never a raw API snapshot.
    Kept intentionally minimal; extend during the storage design step.
    """

    def append(self, sample):
        """Persist one normalized snapshot. Must not raise."""
        raise NotImplementedError

    def recent(self, limit=100):
        """Return up to `limit` most-recent samples, newest last."""
        raise NotImplementedError

    def stats(self):
        """Return {'backend': str, 'count': int, 'durable': bool}."""
        raise NotImplementedError


class InMemoryHistoryStore(HistoryStore):
    """Bounded in-memory ring buffer. Default until a durable backend is chosen.

    NOT durable: contents are lost on app restart or reboot. `maxlen` caps
    memory use per the coding-standards memory rules (no unbounded growth).
    """

    def __init__(self, maxlen=720):
        # 720 samples is a placeholder cap only, NOT a chosen retention model.
        self._buf = deque(maxlen=maxlen)

    def append(self, sample):
        try:
            self._buf.append(sample)
        except Exception:
            pass

    def recent(self, limit=100):
        if limit is None or limit >= len(self._buf):
            return list(self._buf)
        return list(self._buf)[-limit:]

    def stats(self):
        return {
            'backend': 'in-memory',
            'count': len(self._buf),
            'durable': False,
        }

    def max_observations(self):
        return self._buf.maxlen


class ConfigVersionStore(object):
    """Change-versioned static/config objects (in-memory, bounded).

    A version number identifies one DISTINCT semantic configuration of an
    object (by fingerprint), not a point in time:

      * fingerprint == the CURRENT version's      -> unchanged, same version;
      * fingerprint == ANY retained earlier version -> that existing version
        becomes current again (no new version, no new payload stored);
      * a fingerprint never retained before        -> a new version.

    So A -> B -> A yields v1, v2, v1 (not v3). Only the newest `max_versions`
    per object are retained: a fingerprint that has aged out of that window
    may legitimately get a new number later (deleted history is never
    searched). Identical Traffic Classes, WAN rules, steering definitions,
    etc. are never re-stored per sample; observations carry only the number.
    """

    def __init__(self, max_versions=20):
        self._max = max_versions
        self._versions = {}   # name -> deque of version dicts (oldest first)
        self._counter = {}    # name -> last version number issued
        self._current = {}    # name -> version number that is CURRENT

    def update(self, name, fingerprint, payload, ts):
        """Return {'version': int, 'fingerprint': str, 'changed': bool}.

        `changed` is True whenever the current version moved (a new version
        OR a return to an earlier retained one).
        """
        return self._update(name, fingerprint, payload, ts)[0]

    def _update(self, name, fingerprint, payload, ts):
        """Core of update(); returns (result, is_new_version).

        Subclasses persist only when is_new_version is True: reusing a
        retained version appends nothing.
        """
        buf = self._versions.setdefault(name, deque(maxlen=self._max))
        cur = self.current(name)
        if cur is not None and cur['fingerprint'] == fingerprint:
            return ({'version': cur['version'],
                     'fingerprint': fingerprint, 'changed': False}, False)
        for v in buf:
            if v['fingerprint'] == fingerprint:
                self._current[name] = v['version']
                return ({'version': v['version'],
                         'fingerprint': fingerprint, 'changed': True},
                        False)
        number = self._counter.get(name, 0) + 1
        self._counter[name] = number
        buf.append({'version': number, 'fingerprint': fingerprint,
                    'ts': ts, 'payload': payload})
        self._current[name] = number
        return ({'version': number, 'fingerprint': fingerprint,
                 'changed': True}, True)

    def current(self, name):
        buf = self._versions.get(name)
        if not buf:
            return None
        number = self._current.get(name)
        if number is not None:
            for v in buf:
                if v['version'] == number:
                    return v
        return buf[-1]

    def fingerprint_for(self, name, version):
        """Fingerprint of a retained version, or None when not retained."""
        for v in self._versions.get(name, ()):
            if v['version'] == version:
                return v['fingerprint']
        return None

    def versions(self, name):
        return list(self._versions.get(name, ()))

    def info(self):
        """Counts only (Storage Inspector): never payloads."""
        by_name = {n: len(b) for n, b in list(self._versions.items())}
        return {'objects': len(by_name),
                'versions_total': sum(by_name.values()),
                'by_name': by_name,
                'max_versions_per_object': self._max}

    def summary(self):
        out = {}
        for n, b in list(self._versions.items()):
            cur = self.current(n)
            if cur is not None:
                out[n] = {'version': cur['version'],
                          'fingerprint': cur['fingerprint'],
                          'ts': cur['ts']}
        return out


class RecordingStore(object):
    """Recording jobs, their lifecycle events and their samples (in-memory).

    Recording METADATA (jobs, events) is kept separate from telemetry SAMPLES.
    NOT durable: lost on app restart or router reboot (known v0.3.2
    limitation). Bounded: at most `max_jobs` jobs (oldest finished evicted
    with their samples/events) and `max_samples` observations per job.
    """

    def __init__(self, max_jobs=20, max_samples=3000):
        self._max_jobs = max_jobs
        self._max_samples = max_samples
        self._jobs = OrderedDict()     # job_id -> job dict (live object)
        self._samples = {}             # job_id -> [observation, ...]
        self._events = []              # lifecycle events, oldest first

    def max_samples(self):
        return self._max_samples

    # Durability hooks. The in-memory store has nothing to persist; the
    # durable store (durable.DurableRecordingStore) overrides them. The
    # controller calls them at lifecycle points, so RecordingController stays
    # free of filesystem logic.
    def job_changed(self, job, final=False, force_sync=False):
        pass

    def housekeeping(self, now):
        pass

    def add_job(self, job):
        self._jobs[job['job_id']] = job
        self._samples[job['job_id']] = []
        while len(self._jobs) > self._max_jobs:
            victim = next((j for j, v in self._jobs.items()
                           if v['state'] != 'recording'), None)
            if victim is None:
                break
            self._jobs.pop(victim)
            self._samples.pop(victim, None)
            self._events = [e for e in self._events
                            if e['job_id'] != victim]

    def append_sample(self, job_id, obs):
        """Store one observation. Returns False if unknown job or cap hit."""
        buf = self._samples.get(job_id)
        if buf is None or len(buf) >= self._max_samples:
            return False
        buf.append(obs)
        return True

    def add_event(self, event):
        self._events.append(event)
        if len(self._events) > 500:
            del self._events[:len(self._events) - 500]

    def events(self, job_id=None):
        return [dict(e) for e in self._events
                if job_id is None or e['job_id'] == job_id]

    def list_recordings(self):
        return list(self._jobs.values())

    def get_recording(self, job_id):
        return self._jobs.get(job_id)

    def active_recording(self):
        for job in self._jobs.values():
            if job['state'] == 'recording':
                return job
        return None

    def recording_samples(self, job_id, limit=None):
        buf = self._samples.get(job_id, [])
        return list(buf if limit is None else buf[-limit:])

    def sample_count(self, job_id):
        return len(self._samples.get(job_id, ()))

    def info(self):
        """Counts only (Storage Inspector): no jobs' contents or samples."""
        by_job = {j: len(b) for j, b in list(self._samples.items())}
        active = self.active_recording()
        return {'job_count': len(by_job),
                'active_job_id': active['job_id'] if active else None,
                'total_sample_count': sum(by_job.values()),
                'samples_by_job': by_job,
                'event_count': len(self._events),
                'configured_max_jobs': self._max_jobs,
                'configured_max_samples_per_job': self._max_samples}


def default_store():
    """Return the default store for the skeleton (non-durable).

    Swap this out once the storage design step selects a durable backend.
    """
    return InMemoryHistoryStore()
