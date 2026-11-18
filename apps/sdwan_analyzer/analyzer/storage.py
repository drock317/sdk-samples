"""Historical storage interface — NO persistence format chosen yet.

Historical recording is a major SD-WAN Analyzer requirement, but the final
persistence format, retention model, and sampling interval have NOT been
selected. This module therefore defines ONLY the interface the rest of the
app codes against, plus a bounded in-memory default so the skeleton runs
without committing to a storage mechanism.

Do NOT add SQLite (or any concrete backend) here until the storage design
step is authorized.

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


class ConfigVersionStore(object):
    """Change-versioned static/config objects (in-memory, bounded).

    update() persists a new version ONLY when the semantic fingerprint
    differs from the current version, so identical Traffic Classes, WAN
    rules, steering definitions, etc. are not re-stored on every sample.
    Observations carry just the version number.
    """

    def __init__(self, max_versions=20):
        self._max = max_versions
        self._versions = {}   # name -> deque of version dicts (oldest first)
        self._counter = {}    # name -> last version number issued

    def update(self, name, fingerprint, payload, ts):
        """Record `payload` as a new version if `fingerprint` changed.

        Returns {'version': int, 'fingerprint': str, 'changed': bool}.
        """
        buf = self._versions.setdefault(name, deque(maxlen=self._max))
        if buf and buf[-1]['fingerprint'] == fingerprint:
            return {'version': buf[-1]['version'],
                    'fingerprint': fingerprint, 'changed': False}
        number = self._counter.get(name, 0) + 1
        self._counter[name] = number
        buf.append({'version': number, 'fingerprint': fingerprint,
                    'ts': ts, 'payload': payload})
        return {'version': number, 'fingerprint': fingerprint,
                'changed': True}

    def current(self, name):
        buf = self._versions.get(name)
        return buf[-1] if buf else None

    def versions(self, name):
        return list(self._versions.get(name, ()))

    def summary(self):
        return {n: {'version': b[-1]['version'],
                    'fingerprint': b[-1]['fingerprint'], 'ts': b[-1]['ts']}
                for n, b in self._versions.items() if b}


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


def default_store():
    """Return the default store for the skeleton (non-durable).

    Swap this out once the storage design step selects a durable backend.
    """
    return InMemoryHistoryStore()
