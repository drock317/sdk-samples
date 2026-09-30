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

from collections import deque


class HistoryStore(object):
    """Abstract history store. Concrete backends implement these methods.

    A "sample" is a normalized snapshot dict (see service.build_snapshot).
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


def default_store():
    """Return the default store for the skeleton (non-durable).

    Swap this out once the storage design step selects a durable backend.
    """
    return InMemoryHistoryStore()
