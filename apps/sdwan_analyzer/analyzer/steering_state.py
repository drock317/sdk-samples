"""Versioning + change detection state for `steering_rule_defs` (v0.8.3).

Owns no storage of its own: versions live in the existing
ConfigVersionStore, and persisted change detail is DERIVED from adjacent
version payloads. The only extra state is a bounded in-memory ring of the
transitions detected while this process ran, which also captures returns to
an earlier retained version (A -> B -> A), a case adjacent-version
derivation alone cannot see.

Withheld reads (never versioned, never interpreted as removals):
  * unavailable : the read succeeded but carried no rules object;
  * partial     : some compiled children unusable (no xpolicy / no name);
  * empty       : zero rules after a non-empty definition set is withheld
                  until a SECOND consecutive empty read confirms it.
A capture restart or a runtime flow change never reaches this code.

Python 3.8 compatible.
"""
from collections import deque

from . import steering_changes
from . import steering_defs

NAME = 'steering_rule_defs'
EMPTY_CONFIRM_READS = 2
RING = 50


class SteeringDefsTracker(object):
    def __init__(self, ring=RING):
        self._ring = deque(maxlen=ring)
        self._initialized = False
        self.last = None            # {'version', 'ts'} of last observation
        self.state = None           # source state of the latest read
        self.checked_ts = None
        self.last_complete_ts = None
        self.invalid_children = 0
        self.empty_streak = 0
        self.withheld = None        # reason the latest read was not versioned
        self.baseline_note = None

    # --- baseline ---------------------------------------------------------

    def _init_baseline(self, store, history_recent):
        """Pick the version the previous process last observed.

        Prefer the newest persisted observation that referenced a
        steering_rule_defs version (it carries the observation time). Fall
        back to the store's newest version with an unknown observation time
        (the interval is then reported incomplete). With neither, this is
        the first baseline and nothing is announced.
        """
        self._initialized = True
        try:
            for obs in reversed(history_recent or []):
                cv = (obs or {}).get('config_versions') or {}
                e = cv.get(NAME)
                v = e.get('version') if isinstance(e, dict) else e
                if isinstance(v, int) and not isinstance(v, bool):
                    if store.fingerprint_for(NAME, v) is not None:
                        self.last = {'version': v,
                                     'ts': obs.get('collected_ts')}
                        self.baseline_note = 'history'
                        return
                    break
        except Exception:
            pass
        cur = store.current(NAME)
        if cur is not None:
            self.last = {'version': cur['version'], 'ts': None}
            self.baseline_note = 'store_current_unknown_time'
        else:
            self.baseline_note = 'first_baseline'

    # --- observation ------------------------------------------------------

    def observe(self, store, raw_rules, now, history_recent=None,
                kind_of=None):
        """Process one successful read of status/wan/steering/rules.

        `history_recent` and `kind_of` are zero-arg FACTORIES, evaluated only
        when needed (first baseline / a detected transition), so an unchanged
        pass costs one extract + one hash.

        Returns the ConfigVersionStore result dict, or None when the read
        was withheld (see module docstring). Never raises for bad input.
        """
        state, payload, invalid = steering_defs.extract(raw_rules)
        self.state = state
        self.checked_ts = now
        self.invalid_children = invalid
        self.withheld = None
        if state in (steering_defs.ST_PARTIAL, steering_defs.ST_UNAVAILABLE):
            self.empty_streak = 0
            self.withheld = state
            return None
        cur = store.current(NAME)
        if state == steering_defs.ST_EMPTY:
            had = cur is not None and (cur.get('payload') or {}).get(
                'rule_count')
            if had:
                self.empty_streak += 1
                if self.empty_streak < EMPTY_CONFIRM_READS:
                    self.state = 'empty_unconfirmed'
                    self.withheld = 'empty_unconfirmed'
                    return None
        else:
            self.empty_streak = 0
        if not self._initialized:
            self._init_baseline(store, history_recent() if callable(
                history_recent) else history_recent)
        fp = steering_defs.payload_fingerprint(payload)
        res = store.update(NAME, fp, payload, now)
        prev = self.last
        if prev is not None and res['version'] != prev['version']:
            self._record(store, prev, res, payload, now, state,
                         kind_of() if callable(kind_of) else None)
        self.last = {'version': res['version'], 'ts': now}
        self.last_complete_ts = now
        return res

    def _payload_of(self, store, version):
        for v in store.versions(NAME):
            if v['version'] == version:
                return v
        return None

    def _record(self, store, prev, res, cur_payload, now, state, kind_of):
        prev_v = self._payload_of(store, prev['version'])
        ctx = {'from_version': prev['version'],
               'to_version': res['version'],
               'previous_observed_ts': prev['ts'],
               'detected_ts': now,
               'source_completeness': ('empty_confirmed'
                                       if state == steering_defs.ST_EMPTY
                                       else state),
               'source_valid': True,
               'derived_from': 'live_observation'}
        if prev_v is None:
            tr = steering_changes.build_events([], ctx)
            tr['note'] = 'previous version payload no longer retained'
            tr['events'] = []
            tr['event_count'] = None
        else:
            changes = steering_changes.diff(prev_v['payload'], cur_payload,
                                            kind_of)
            tr = steering_changes.build_events(changes, ctx)
        self._ring.append(tr)

    # --- views ------------------------------------------------------------

    def live_transitions(self):
        return list(self._ring)

    def derived_transitions(self, store, kind_of=None):
        """Transitions derivable from adjacent retained versions.

        previous_observed_ts is unknown (only the version's first-seen time
        is stored), so these intervals are always incomplete.
        """
        versions = sorted(store.versions(NAME), key=lambda v: v['version'])
        out = []
        for a, b in zip(versions, versions[1:]):
            if b['version'] != a['version'] + 1:
                continue
            ctx = {'from_version': a['version'], 'to_version': b['version'],
                   'previous_observed_ts': None,
                   'detected_ts': b.get('ts'),
                   'source_completeness': 'complete', 'source_valid': True,
                   'derived_from': 'adjacent_versions'}
            out.append(steering_changes.build_events(
                steering_changes.diff(a['payload'], b['payload'], kind_of),
                ctx))
        return out

    def status(self, store):
        cur = store.current(NAME)
        return {
            'object': NAME,
            'source_state': self.state,
            'withheld_reason': self.withheld,
            'checked_ts': self.checked_ts,
            'last_complete_ts': self.last_complete_ts,
            'invalid_children': self.invalid_children,
            'empty_streak': self.empty_streak,
            'current_version': cur['version'] if cur else None,
            'current_fingerprint': cur['fingerprint'] if cur else None,
            'current_version_first_seen_ts': cur['ts'] if cur else None,
            'baseline': self.baseline_note,
            'identity_basis': steering_defs.IDENTITY_BASIS,
            'fingerprint_definition': 'steering_rule_defs fp v{}'.format(
                steering_defs.SRD_FP_VERSION),
        }
