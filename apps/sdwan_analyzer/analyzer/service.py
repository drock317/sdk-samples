"""Application API: composes collectors + normalize + storage.

This is the ONLY layer the presentation (web) tier is allowed to call. It
owns router collection, caches the latest normalized snapshot, and exposes
plain-dict sections ready for JSON. The browser never reaches NCOS APIs.
Web requests NEVER trigger scheduled collection; the only web-triggered
collection is an explicit user manual refresh (refresh()).

Data-acquisition model
----------------------
There are exactly three ways data enters the system, and NO background
cache-refresh poller:

1. Scheduled collection -- one normal observation every N minutes, N in
   SCHEDULE_CHOICES (5/15/30/60). Aligned to the local clock. A startup
   observation is collected first so the UI is never empty.
2. Manual refresh -- targeted, per-group, ~5 s cooldown, tagged 'manual'.
   It never changes the schedule.
3. Recording -- (next phase) collect(source='recording', job_id=...).

Every collection pass produces a source-tagged observation (compact,
timestamp-enveloped; see telemetry.py), updates the in-memory cache that the
UI reads, and stores the observation. Static/config objects are stored only
as change-versions (storage.ConfigVersionStore), never per sample.

A collector failure keeps the last-known-good raw for that key plus an
error/timestamp, so one failing optional collector never blanks the whole
snapshot or stops the app.

Business logic lives here (and in normalize/telemetry), never in web.
"""

import os
import threading
import time

import cp

from . import VERSION
from . import collectors
from . import durable
from . import inspector
from . import normalize
from . import query
from . import recording
from . import storage
from . import telemetry

# User-selectable continuous-history intervals (minutes). No 1-minute mode.
SCHEDULE_CHOICES = (5, 15, 30, 60)
DEFAULT_SCHEDULE_MINUTES = 15

# Config/static objects are re-read on ~hourly reconciliation (plus startup
# and targeted manual refresh).
CONFIG_RECONCILE_S = 3600

# Cooldown per manual refresh group (seconds).
REFRESH_COOLDOWN_S = 5

SAMPLE_SOURCES = ('startup', 'scheduled', 'manual', 'recording')

# Current-state section -> collector keys that must ALL be read successfully
# in a pass for that section's freshness to advance.
SECTION_KEYS = {
    'wan': ('wan_devices',),
    'cellular': ('wan_devices',),
    'swans': ('swans',),
    'qoe': ('qoe',),
    'bonding': ('wan_bonding',),
    'traffic_steering': ('steering_rules', 'steering_events',
                         'steering_stats'),
    'traffic_classes': ('identities_intent', 'traffic_class_config'),
    'loadbalancers': ('loadbalancers',),
}


class _RawCache(object):
    """Per-collector last-known-good cache for one cadence group.

    A collection pass returns {key: collectors.CollectResult}. Each key is
    merged INDEPENDENTLY:
      * a successful read (ok=True, even value None/{}/[]) replaces that
        key's value and clears its error;
      * a failed read (ok=False) RETAINS the previous value for that key and
        records the error + error timestamp.
    A single failing endpoint therefore never erases unrelated cached data.
    Never raises.
    """

    def __init__(self):
        self.data = {}                # key -> last-known-good value
        self._updated = {}            # key -> ts of last successful read
        self._errors = {}            # key -> last error string
        self._error_ts = {}          # key -> ts of last error
        self._error_type = {}        # key -> type of current error
        self._last_error = {}        # key -> (ts, type, msg); never cleared
        self.updated_ts = None        # ts of last pass that updated any key

    def update(self, fetch_callable):
        try:
            results = fetch_callable()
        except Exception as e:  # collection framework failure; never crash
            cp.log('service: raw collection pass error: {}'.format(e))
            return
        if not isinstance(results, dict):
            return
        now = int(time.time())
        any_success = False
        for key, res in results.items():
            ok = getattr(res, 'ok', None)
            if ok is None:
                # Back-compat: a bare value (not a CollectResult) is a success.
                self.data[key] = res
                self._updated[key] = now
                self._errors.pop(key, None)
                self._error_ts.pop(key, None)
                self._error_type.pop(key, None)
                any_success = True
                continue
            if res.ok:
                self.data[key] = res.value
                self._updated[key] = now
                self._errors.pop(key, None)
                self._error_ts.pop(key, None)
                self._error_type.pop(key, None)
                any_success = True
            else:
                # Retain previous value; record per-key error + timestamp.
                self._errors[key] = res.error
                self._error_ts[key] = now
                etype = getattr(res, 'error_type', None) or 'api_error'
                self._error_type[key] = etype
                self._last_error[key] = (now, etype, res.error)
                cp.log('service: collector {} failed ({}); '
                       'keeping last-known-good'.format(key, res.error))
        if any_success:
            self.updated_ts = now

    def health(self):
        """Per-collector health: {key: {updated_ts, error, error_ts}}."""
        keys = set(self.data) | set(self._errors) | set(self._last_error)
        out = {}
        for k in keys:
            le = self._last_error.get(k) or (None, None, None)
            out[k] = {
                'updated_ts': self._updated.get(k),
                'error': self._errors.get(k),
                'error_ts': self._error_ts.get(k),
                # error_type: timeout | busy | api_error (current error).
                'error_type': self._error_type.get(k),
                'last_success_ts': self._updated.get(k),
                'last_error_ts': le[0],
                'last_error_type': le[1],
                'last_error': le[2],
            }
        return out


class AnalyzerService(object):
    def __init__(self, store=None, schedule_minutes=DEFAULT_SCHEDULE_MINUTES,
                 config_store=None, clock=None, app_root=None,
                 telemetry_root=None, telemetry_retention=None):
        self._clock = clock if clock is not None else time.time
        # Root of the files the Storage Inspector may describe (the app's own
        # directory). Injectable for tests.
        self._app_root = (app_root if app_root is not None else
                          os.path.dirname(os.path.dirname(
                              os.path.abspath(__file__))))

        # Durable telemetry (tmp/telemetry). None => in-memory stores only
        # (the default; tests and any init failure). A relative root is
        # resolved against the app directory so the location never depends on
        # the process cwd.
        self._durable = None
        self._durable_init_error = None
        if telemetry_root:
            try:
                if os.path.isabs(telemetry_root):
                    root = telemetry_root
                    shown = telemetry_root
                    try:
                        rel = os.path.relpath(root, self._app_root)
                        if not rel.startswith('..'):
                            shown = rel.replace(os.sep, '/')
                    except ValueError:
                        pass
                else:
                    root = os.path.join(self._app_root, telemetry_root)
                    shown = telemetry_root.replace(os.sep, '/')
                tel = durable.TelemetryStorage(
                    root, display_root=shown, clock=self._clock,
                    retention=telemetry_retention)
                if tel.prepare():
                    self._durable = tel
                else:
                    self._durable_init_error = dict(tel.init_error or {
                        'type': 'Error', 'message': 'prepare failed'})
                    cp.log('service: durable telemetry unavailable ({}); '
                           'using in-memory stores'.format(
                               tel.health.last_write_error))
            except Exception as e:
                cp.log('service: durable telemetry init error: {}'.format(e))
                self._durable = None
                self._durable_init_error = {'type': type(e).__name__,
                                            'message': str(e)[:200]}
        d = self._durable
        self._store = store if store is not None else (
            d.history_store if d else storage.default_store())
        self._config_store = (config_store if config_store is not None
                              else (d.config_store if d
                                    else storage.ConfigVersionStore()))
        self._rec_store = (d.recording_store if d
                           else storage.RecordingStore())
        self._recording = recording.RecordingController(self, self._rec_store)
        self._web_port_status = None

        if schedule_minutes not in SCHEDULE_CHOICES:
            schedule_minutes = DEFAULT_SCHEDULE_MINUTES
        self._schedule_minutes = schedule_minutes
        self._next_due = None            # epoch of next scheduled collection

        # Raw last-known-good caches: runtime keys vs static/config keys.
        self._runtime_raw = _RawCache()
        self._config_raw = _RawCache()
        self._last_config_ts = None

        # Composed normalized snapshot cache + guards.
        self._lock = threading.Lock()
        self._collect_lock = threading.Lock()
        self._snapshot = None
        self._snapshot_ts = None

        # Fingerprints from the previous composed snapshot.
        self._prev_fingerprints = {}

        # Cross-observation telemetry state (all bounded).
        self._prev_counters = {}
        # Latest normalized runtime telemetry for CURRENT STATE (merged
        # last-known-good): replaced only by a pass whose wan_devices read
        # succeeded, retained otherwise (partial refresh / failed read).
        self._cur_interfaces = []
        self._cur_cellular = []
        self._swans_seen = telemetry.BoundedSeen()
        self._event_seen = telemetry.BoundedSeen()
        self._last_refresh = {}          # group -> ts of last accepted refresh
        # section -> {collected_ts, sample_source} of the pass that last
        # refreshed it. Advances only for sections actually collected.
        self._freshness = {}

        # Startup recovery (config versions, Recording metadata, history ring,
        # dedupe tails, retention). Loads NO Recording samples into RAM.
        if self._durable is not None:
            try:
                summary = self._durable.recover(self._swans_seen,
                                                self._event_seen)
                cp.log('service: durable telemetry ready ({}); recovery: '
                       '{}'.format(self._durable.display_root, summary))
            except Exception as e:
                cp.log('service: durable recovery error: {}'.format(e))

    def begin_shutdown(self):
        """Shutdown has been requested (SIGTERM): storage must no longer
        create directories. Only sets a flag; safe from a signal handler."""
        if self._durable is not None:
            self._durable.begin_shutdown()

    def shutdown(self):
        """Graceful stop: finish any Recording and fsync/close storage."""
        self.begin_shutdown()
        try:
            self._recording.shutdown()
        finally:
            if self._durable is not None:
                self._durable.shutdown()

    def set_web_port_provider(self, provider):
        """Register a zero-arg callable that returns web port status."""
        self._web_port_status = provider

    # --- schedule ---------------------------------------------------------

    def schedule_minutes(self):
        return self._schedule_minutes

    def set_schedule_minutes(self, minutes):
        """Select the scheduled interval. Returns False if not allowed."""
        if minutes not in SCHEDULE_CHOICES:
            return False
        self._schedule_minutes = minutes
        self._plan_next_due(self._clock())
        return True

    def _plan_next_due(self, now):
        """Next local-clock-aligned boundary strictly after `now`."""
        step = self._schedule_minutes * 60
        try:
            offset = time.localtime(now).tm_gmtoff or 0
        except Exception:
            offset = 0
        self._next_due = ((int(now) + offset) // step + 1) * step - offset

    def next_scheduled_ts(self):
        return self._next_due

    def scheduled_due(self, now=None):
        """True when the scheduled interval boundary has been reached."""
        now = self._clock() if now is None else now
        return self._next_due is not None and now >= self._next_due

    # --- collection (the ONLY router I/O; no background cache poller) -----

    def collect_startup(self):
        """One initial full current-state observation (source=startup)."""
        obs = self.collect('startup', keys=(collectors.RUNTIME_KEYS +
                                            collectors.CONFIG_KEYS))
        self._plan_next_due(self._clock())
        return obs

    def collect_scheduled(self):
        """One scheduled observation; config re-read when reconcile is due."""
        if self._rec_store.active_recording() is not None:
            # The Recording worker is already producing fresher runtime
            # observations; skip the duplicate poll, keep the schedule.
            self._plan_next_due(self._clock())
            return None
        now = self._clock()
        keys = collectors.RUNTIME_KEYS
        if (self._last_config_ts is None
                or now - self._last_config_ts >= CONFIG_RECONCILE_S):
            keys = keys + collectors.CONFIG_KEYS
        obs = self.collect('scheduled', keys=keys)
        self._plan_next_due(self._clock())
        return obs

    # --- Recording (see recording.py) -------------------------------------

    def start_recording(self, label='', notes='', max_duration_sec=None,
                        created_by_ui=False):
        return self._recording.start(label, notes, max_duration_sec,
                                     created_by_ui)

    def stop_recording(self, job_id):
        return self._recording.stop(job_id)

    def recording_tick(self):
        """Advance the backend-owned Recording job (main loop, ~1/s)."""
        self._recording.tick()

    def list_recordings(self):
        return self._recording.list_recordings()

    def get_recording(self, job_id):
        return self._recording.get_recording(job_id)

    def active_recording(self):
        return self._recording.active_recording()

    def recording_samples(self, job_id, limit=None):
        return self._recording.recording_samples(job_id, limit)

    def refresh(self, group):
        """Targeted manual refresh. Does NOT touch the schedule.

        Returns {ok, refresh_ts, refresh_group, duration_ms,
        collector_errors} (plus error/retry_after_s when rejected).
        """
        keys = collectors.REFRESH_GROUPS.get(group)
        now = self._clock()
        if group == 'loadbalancers' and (
                self._rec_store.active_recording() is not None):
            return {'ok': False, 'refresh_group': group,
                    'refresh_ts': int(now), 'duration_ms': 0,
                    'collector_errors': [],
                    'error': 'not allowed during a Recording'}
        if keys is None:
            return {'ok': False, 'refresh_group': group,
                    'refresh_ts': int(now), 'duration_ms': 0,
                    'collector_errors': [], 'error': 'unknown refresh group'}
        last = self._last_refresh.get(group)
        if last is not None and now - last < REFRESH_COOLDOWN_S:
            return {'ok': False, 'refresh_group': group,
                    'refresh_ts': int(now), 'duration_ms': 0,
                    'collector_errors': [], 'error': 'cooldown',
                    'retry_after_s': round(
                        REFRESH_COOLDOWN_S - (now - last), 1)}
        self._last_refresh[group] = now
        obs = self.collect('manual', keys=keys, group=group)
        return {'ok': True, 'refresh_ts': obs['collected_ts'],
                'refresh_group': group, 'duration_ms': obs['duration_ms'],
                'collector_errors': obs['collector_errors']}

    def collect(self, source, keys, group=None, job_id=None):
        """Run one collection pass, update the cache, store an observation."""
        with self._collect_lock:
            t0 = time.time()
            keys = tuple(keys)
            results = collectors.collect_keys(keys)
            errors = [{'collector': k, 'error': r.error,
                       'error_type': getattr(r, 'error_type', None)}
                      for k, r in sorted(results.items())
                      if not getattr(r, 'ok', True)]
            rt = {k: r for k, r in results.items()
                  if k not in collectors.CONFIG_KEYS}
            cf = {k: r for k, r in results.items()
                  if k in collectors.CONFIG_KEYS}
            if rt:
                self._runtime_raw.update(lambda: rt)
            if cf:
                self._config_raw.update(lambda: cf)
            now = int(self._clock())
            for sec, sec_keys in SECTION_KEYS.items():
                if all(k in results and getattr(results[k], 'ok', True)
                       for k in sec_keys):
                    self._freshness[sec] = {'collected_ts': now,
                                            'sample_source': source}
            if all(k in results and getattr(results[k], 'ok', True)
                   for k in collectors.CONFIG_KEYS):
                # Reconciliation counts only when EVERY config read
                # succeeded; a failed one leaves the timestamp alone so the
                # next scheduled pass is again eligible (no hour of delay).
                self._last_config_ts = now
            tel = self._wan_telemetry(keys, results, now)
            snap = self.recompose()
            # Collectors read SUCCESSFULLY in this pass. Only these may feed
            # a newly timestamped historical measurement.
            ok_keys = frozenset(k for k, r in results.items()
                                if getattr(r, 'ok', True))
            obs = self._build_observation(source, keys, snap, now,
                                          group, job_id, tel, ok_keys)
            obs['collector_errors'] = errors
            obs['duration_ms'] = int((time.time() - t0) * 1000)
            if source == 'recording' and job_id:
                # Recording samples live in the RecordingStore, tagged.
                obs['recording_group'] = group
                obs['refresh_group'] = None
                if not self._recording.store_sample(job_id, obs):
                    cp.log('service: recording sample dropped (cap)')
            else:
                self._store.append(obs)
            return obs

    def recompose(self):
        """Recompose the CURRENT STATE from cached raw (the UI cache).

        OBSERVATION vs CURRENT STATE: an observation (see _build_observation)
        is the persisted, possibly PARTIAL record of one acquisition. The
        current state composed here is the merged last-known-good view built
        from per-key raw caches, so a partial manual refresh only replaces
        the keys it collected; other sections keep their earlier data. Each
        section's age is in `section_freshness`.

        Pure over cached raw: no router I/O except per-bond name resolution.
        """
        snap = self._compose_snapshot()
        with self._lock:
            self._snapshot = snap
            self._snapshot_ts = snap.get('timestamp')
        return snap

    def _wan_telemetry(self, keys, results, now):
        """Normalize interface + cellular telemetry ONCE per pass.

        The same records feed the persisted observation and the current-state
        cache, so there is a single parsing path (telemetry.py). Counter
        reset detection state advances exactly once per pass.

        Returns (interfaces, cellular), or None when wan_devices was not
        requested OR its read FAILED. A failed read must never produce
        newly timestamped measurements from last-known-good raw data: the
        counter baseline (`_prev_counters`) is not advanced, the current-state
        caches keep their last-known-good values, and the observation simply
        omits wan/interfaces/cellular (the failure is in collector_errors).
        """
        if 'wan_devices' not in keys:
            return None
        res = results.get('wan_devices')
        if res is None or not getattr(res, 'ok', True):
            return None
        devices = self._raw().get('wan_devices')
        interfaces, self._prev_counters = telemetry.interface_records(
            devices, self._prev_counters, now,
            acquired_at=getattr(res, 'acquired_at', None))
        cell = telemetry.cellular_records(devices, now)
        self._cur_interfaces = interfaces
        self._cur_cellular = cell
        return interfaces, cell

    def _build_observation(self, source, keys, snap, now, group, job_id,
                           tel=None, ok_keys=None):
        """Source-tagged compact observation. Never a raw API snapshot.

        Only sections whose collectors ran in this pass are populated (a
        targeted refresh yields a partial observation). Static/config data is
        referenced by version number, never copied per sample.

        FAILED-READ INVARIANT: a collector whose read failed in this pass
        (not in `ok_keys`) never contributes a measurement to the
        observation. The cached last-known-good value keeps serving the
        current state (/api/snapshot, section freshness) but is not
        re-stamped with `now`. The affected section is omitted, not zero
        filled; the failure is in `collector_errors`. `ok_keys=None` means
        every requested key succeeded (direct callers/tests).
        """
        data = self._raw()
        ok = set(keys) if ok_keys is None else set(ok_keys) & set(keys)
        obs = {'sample_source': source, 'job_id': job_id,
               'refresh_group': group, 'timestamp': now,
               'collected_ts': now}
        if 'wan_devices' in ok and tel is not None:
            # tel is None when the wan_devices read failed.
            devices = data.get('wan_devices')
            # Primary identity only from a primary_device read that
            # succeeded in THIS pass; otherwise omitted (never re-stamped).
            primary_known = 'primary_device' in ok
            obs['wan'] = telemetry.wan_state(
                devices, data.get('primary_device'), now,
                primary_known=primary_known)
            obs['interfaces'], obs['cellular'] = tel
        if 'swans' in ok:
            obs['swans_samples'] = telemetry.swans_new_samples(
                data.get('swans'), self._swans_seen, now)
        if 'qoe' in ok:
            obs['qoe'] = telemetry.qoe_records(data.get('qoe'), now)
        if 'wan_bonding' in ok:
            obs['wbond'] = telemetry.wbond_runtime(snap.get('bonding'), now)
        ts = snap.get('traffic_steering', {})
        if {'steering_rules', 'steering_stats', 'steering_events'} <= ok:
            # Runtime per rule combines rules (steering_to), stats (flows,
            # preferred WAN) and events (latest reason): all three must be
            # fresh, otherwise cached parts would be re-stamped.
            obs['steering'] = telemetry.steering_runtime(
                ts.get('logical_rules'), now)
        if 'steering_events' in ok:
            obs['steering_events'] = telemetry.steering_new_events(
                ts.get('events'), self._event_seen, now)
        obs['config_versions'] = self._update_config_versions(snap, now)
        return obs

    def _update_config_versions(self, snap, now):
        """Version static/config objects; a new version only on fp change."""
        fps = snap.get('fingerprints', {})
        wan = snap.get('wan', {})
        bonds = snap.get('bonding', {}).get('bonds', [])
        steering_defs = [{
            'logical_name': r.get('logical_name'),
            'fingerprint': r.get('semantic_fingerprint'),
            'selector': r.get('wan_selector_type'),
            'preferred_wan_raw': r.get('preferred_wan_raw'),
            'branches': [{'traffic_class': b.get('traffic_class_name'),
                          'trigger_priority': b.get('trigger_priority'),
                          'selector': b.get('wan_selector_type')}
                         for b in r.get('branches', [])
                         if isinstance(b, dict)],
        } for r in snap.get('traffic_steering', {}).get('logical_rules', [])]
        wbond_cfg = [{'iface': b.get('iface'), 'name': b.get('name'),
                      'members': [{'member_uid': m.get('member_uid'),
                                   'configured_weight':
                                   m.get('configured_weight')}
                                  for m in b.get('members', [])]}
                     for b in bonds]
        caps = {'supported': snap.get('bonding', {}).get(
                    'supported_capabilities', []),
                'negotiated': {b.get('iface'): b.get('negotiated_capabilities')
                               for b in bonds}}
        underlay = wan.get('underlay_map', [])
        items = (
            ('traffic_classes', fps.get('traffic_classes'),
             snap.get('traffic_classes')),
            ('destinations', None, snap.get('identities', {}).get(
                'destinations')),
            ('wan_rules', None, wan.get('config')),
            ('steering_definitions', fps.get('logical_rules'),
             steering_defs),
            ('wbond_config', fps.get('bonding_membership'), wbond_cfg),
            ('overlay_underlay', None, underlay),
            ('capabilities', None, caps),
        )
        out = {}
        for name, fp, payload in items:
            if fp is None:
                fp = normalize._stable_hash(payload)
            out[name] = self._config_store.update(name, fp, payload, now)
        return out

    def _raw(self):
        """Merged last-known-good raw view (config + runtime)."""
        data = dict(self._config_raw.data)
        data.update(self._runtime_raw.data)
        return data

    # --- composition (pure over cached raw; no router I/O except WBOND) ---

    def _compose_snapshot(self):
        base = heavy = self._raw()

        primary = base.get('primary_device')
        raw_devices = base.get('wan_devices')
        devices = raw_devices if isinstance(raw_devices, dict) else {}

        # SD-WAN underlay correlation (per overlay device).
        sdwan_underlays = self._resolve_sdwan_underlays(devices)
        underlay_map = {u['sdwan_uid']: u['underlay_uid']
                        for u in sdwan_underlays if u.get('underlay_uid')}

        # WAN path inventory with object_class + underlay-aware naming.
        wan_paths = normalize.wan_paths(raw_devices, primary, underlay_map)
        physical = [p for p in wan_paths
                    if p.get('object_class') == normalize.OBJ_PHYSICAL]
        overlays = [p for p in wan_paths
                    if p.get('object_class') == normalize.OBJ_SDWAN]

        # Destination identity index (from cached config).
        dest_index = normalize.destination_index(heavy.get('identities_ip'))

        # Logical Traffic Steering grouping + correlation.
        logical = normalize.logical_rules(
            base.get('steering_rules'),
            base.get('steering_stats'),
            base.get('steering_events'),
            dest_index, wan_paths)

        events = normalize.steering_events(base.get('steering_events'))
        stats = normalize.steering_stats(base.get('steering_stats'))

        # SWANS + QoE (kept separate concepts).
        swans = normalize.swans_paths(base.get('swans'))
        qoe = normalize.qoe_paths(heavy.get('qoe'))

        # Traffic Classes (config-authoritative; merges both config trees).
        traffic_classes = normalize.traffic_classes(
            heavy.get('identities_intent'),
            heavy.get('traffic_class_config'))

        # Loadbalancers (heavy tree).
        lbs = normalize.loadbalancers(heavy.get('loadbalancers'))

        # WAN bonding detailed runtime + member friendly names.
        bonding = self._compose_bonding(heavy.get('wan_bonding'), devices)

        # WAN config correlation (config/wan/rules2) -- read-only.
        wan_rules2 = normalize.wan_rules2_index(heavy.get('wan_rules2'))

        compiled_rules = base.get('steering_rules') or []
        if isinstance(compiled_rules, dict):
            compiled_rules = list(compiled_rules.values())

        kinds = {p.get('uid'): p.get('kind') for p in wan_paths}
        interfaces = [dict(r, kind=kinds.get(r.get('uid')))
                      for r in self._cur_interfaces]

        snapshot = {
            'timestamp': int(time.time()),
            'system_summary': {
                'primary_device': primary,
                'wan_path_count': len(wan_paths),
                'physical_count': len(physical),
                'overlay_count': len(overlays),
                'bond_count': len(bonding.get('bonds', [])),
                'logical_rule_count': len(logical),
            },
            'wan': {
                'primary_device': primary,
                'paths': wan_paths,
                'physical': physical,
                'overlays': overlays,
                'underlay_map': sdwan_underlays,
                'config': wan_rules2,
                # Latest normalized counters per interface (raw counters
                # only: no rates, no interpretation of errors/drops).
                'interfaces': interfaces,
            },
            'cellular': {
                'modems': [dict(r, carriers=[dict(c) for c in
                                             r.get('carriers', [])])
                           for r in self._cur_cellular],
            },
            'swans': swans,
            'traffic_classes': traffic_classes,
            'traffic_steering': {
                'logical_rules': logical,
                'compiled_rules': compiled_rules,
                'events': events.get('events', []),
                'events_meta': {
                    'began_capture': events.get('began_capture'),
                    'total_perf_events': events.get('total_perf_events'),
                    'event_overflow': events.get('event_overflow'),
                },
                'stats': stats.get('rules', []),
                'flow_alignment': self._flow_alignment(logical),
            },
            'loadbalancers': lbs,
            'bonding': bonding,
            'qoe': qoe,
            'identities': {
                'destinations': list(dest_index.values()),
            },
            'fingerprints': self._compute_fingerprints(wan_paths, logical,
                                                        bonding, traffic_classes),
            'collectors': self._collector_health(),
            'section_freshness': {k: dict(v) for k, v in
                                  self._freshness.items()},
        }

        snapshot['changes'] = self._detect_changes(snapshot['fingerprints'])
        self._prev_fingerprints = dict(snapshot['fingerprints'])
        return snapshot

    def _flow_alignment(self, logical_rules):
        """Per-logical-rule preferred vs nonpreferred flow population.

        These are a current/aging population, NOT lifetime totals (named
        plainly here to avoid implying cumulative semantics).
        """
        out = []
        for lr in logical_rules:
            pref = lr.get('preferred_flows')
            nonpref = lr.get('nonpreferred_flows')
            if pref is None and nonpref is None:
                continue
            out.append({
                'logical_name': lr.get('logical_name'),
                'trigger_priority': lr.get('trigger_priority'),
                'branch_count': len(lr.get('branches', [])),
                'preferred_wan': lr.get('preferred_wan'),
                'preferred_wan_raw': lr.get('preferred_wan_raw'),
                'preferred_wan_uid': lr.get('preferred_wan_uid'),
                'preferred_wan_name': lr.get('preferred_wan_name'),
                'preferred_flows': pref,
                'nonpreferred_flows': nonpref,
                'was_flow_matched': lr.get('was_flow_matched'),
            })
        return out

    def _compose_bonding(self, raw_wan_bonding, devices):
        """Normalize bonding runtime and resolve member/bond friendly names.

        Member friendly names reuse normalize.friendly_name over the devices
        map. WBOND is kept a separate forwarding object; members are NOT
        flattened away -- they are attached to their bond.
        """
        detail = normalize.wan_bonding_detail(raw_wan_bonding)
        for bond in detail.get('bonds', []):
            bond_name = bond.get('name') or ''
            # Attach a bond display name (configured name preferred).
            bond['display_name'] = (
                normalize.friendly_name_ex('wbond', {}, devices, '',
                                           bond_name))
            for m in bond.get('members', []):
                muid = m.get('member_uid')
                dev = devices.get(muid) if muid else None
                resolved_uid = muid
                if not isinstance(dev, dict) and muid:
                    # Fall back to an info.iface match (STA behavior).
                    for duid, d in devices.items():
                        if not isinstance(d, dict):
                            continue
                        if muid == (d.get('info', {}) or {}).get('iface', ''):
                            dev, resolved_uid = d, duid
                            break
                if isinstance(dev, dict):
                    m['display_name'] = normalize.friendly_name(
                        resolved_uid, dev)
                else:
                    m['display_name'] = muid
        return detail

    def _resolve_sdwan_underlays(self, devices):
        """Map each SD-WAN device to its dependent (underlay) WAN.

        STA-validated: active_dep_wandev / dep_wandevs.
        """
        out = []
        for uid, device in devices.items():
            if not isinstance(device, dict):
                continue
            info = device.get('info') if isinstance(
                device.get('info'), dict) else {}
            if str(info.get('type') or '') != 'sdwan':
                continue
            underlay_uid = normalize.sdwan_underlay_uid(device)
            underlay_name = ''
            underlay_dev = devices.get(underlay_uid)
            if isinstance(underlay_dev, dict):
                underlay_name = normalize.friendly_name(
                    underlay_uid, underlay_dev)
            out.append({
                'sdwan_uid': uid,
                'sdwan_iface': info.get('iface', ''),
                'underlay_uid': underlay_uid,
                'underlay_name': underlay_name,
            })
        return out

    def _compute_fingerprints(self, wan_paths, logical, bonding,
                              traffic_classes):
        """Build change-ready fingerprints for topology/config/policy."""
        rule_fps = {}
        for lr in logical:
            name = lr.get('logical_name') or 'rule'
            # One logical policy == one name (branches live inside it).
            rule_fps[name] = lr.get('semantic_fingerprint')
        return {
            'topology': normalize.topology_fingerprint(wan_paths),
            'bonding_membership': normalize.bonding_membership_fingerprint(
                bonding.get('bonds', [])),
            'logical_rules': normalize._stable_hash(rule_fps),
            'logical_rule_map': rule_fps,
            'traffic_classes': normalize._stable_hash(
                sorted(
                    (normalize.traffic_class_fingerprint_payload(tc)
                     for tc in traffic_classes),
                    key=lambda p: (p.get('intent_id') or '',
                                   p.get('traffic_class_id') or ''))),
        }

    def _detect_changes(self, fingerprints):
        """Compare fingerprints to the previous snapshot.

        Returns lists categorized as TOPOLOGY / CONFIG / (runtime is tracked
        by value elsewhere). Compiled UUID churn alone never lands here
        because fingerprints exclude compiled UUIDs.
        """
        prev = self._prev_fingerprints or {}
        topology = []
        config = []
        if not prev:
            return {'topology': topology, 'config': config,
                    'first_snapshot': True}

        if prev.get('topology') != fingerprints.get('topology'):
            topology.append('wan_topology_changed')

        if prev.get('bonding_membership') != fingerprints.get(
                'bonding_membership'):
            config.append('bond_membership_or_config_changed')
        if prev.get('traffic_classes') != fingerprints.get('traffic_classes'):
            config.append('traffic_class_set_changed')

        prev_map = prev.get('logical_rule_map') or {}
        cur_map = fingerprints.get('logical_rule_map') or {}
        for key in cur_map:
            if key not in prev_map:
                config.append('logical_rule_added:{}'.format(key))
            elif prev_map[key] != cur_map[key]:
                config.append('logical_rule_changed:{}'.format(key))
        for key in prev_map:
            if key not in cur_map:
                config.append('logical_rule_removed:{}'.format(key))

        return {'topology': topology, 'config': config,
                'first_snapshot': False}

    def _collector_health(self):
        """Per-collector health (last-known-good ts, error, error ts).

        Shows exactly which optional endpoint failed while the rest of the
        snapshot stays valid.
        """
        return {
            'schedule_minutes': self._schedule_minutes,
            'next_scheduled_ts': self._next_due,
            'runtime': {
                'last_pass_ts': self._runtime_raw.updated_ts,
                'collectors': self._runtime_raw.health(),
            },
            'config': {
                'last_pass_ts': self._config_raw.updated_ts,
                'collectors': self._config_raw.health(),
            },
        }

    # --- cached read paths (web serves these; NO router I/O) --------------

    def snapshot(self):
        """Return the latest CACHED composed snapshot (may be None early).

        Web requests use this. It never triggers collection.
        """
        with self._lock:
            return self._snapshot

    def section(self, name):
        """Return one top-level section of the cached snapshot, or None."""
        snap = self.snapshot()
        if not isinstance(snap, dict):
            return None
        return snap.get(name)

    def cellular_detail(self, device_id):
        """Per-modem diagnostics/stats for one cellular device UID.

        Returns None for non-mdm devices. Sensitive identity/GPS fields are
        NOT collected here (SECURITY / PRIVACY): only SD-WAN-useful cellular
        metrics are surfaced by the caller; raw is filtered by the web layer.
        """
        diag = collectors.modem_diagnostics(device_id)
        stats = collectors.modem_stats(device_id)
        if diag is None and stats is None:
            return None
        return {
            'uid': device_id,
            'cellular': normalize_cellular_safe(diag),
        }

    def storage_report(self):
        """Storage Inspector: a FRESH counts/metadata report per call.

        Read-only and side-effect free: no observation is stored, no router
        I/O, no schedule/Recording change, nothing is cached or persisted.
        """
        memory = None
        for _ in range(3):      # stores are mutated by the main-loop thread
            try:
                memory = self._storage_memory()
                break
            except RuntimeError:
                continue
        if memory is None:
            memory = {'error': 'memory counts unavailable'}
        return inspector.build_report(
            memory, self._app_root, now=self._clock(),
            persistent=(self._durable.inspector_info()
                        if self._durable is not None else
                        ({'init_error': self._durable_init_error.get(
                            'message'),
                          'init_error_type': self._durable_init_error.get(
                              'type')}
                         if self._durable_init_error else None)))

    def _storage_memory(self):
        hist = self._store.stats()
        max_obs = getattr(self._store, 'max_observations', None)
        cfg = self._config_store.info()
        rec = self._rec_store.info()
        return {
            'history_observations': hist.get('count'),
            'config_versions': cfg['versions_total'],
            'recording_jobs': rec['job_count'],
            'recording_samples': rec['total_sample_count'],
            'active_recording': rec['active_job_id'],
            'swans_dedupe_entries': len(self._swans_seen),
            'steering_event_dedupe_entries': len(self._event_seen),
            'details': {
                'history': {'backend': hist.get('backend'),
                            'durable': hist.get('durable'),
                            'max_observations': (max_obs() if max_obs
                                                 else None)},
                'config_versions': cfg,
                'recording': rec,
                'dedupe': {'swans_cap': self._swans_seen.cap(),
                           'steering_events_cap': self._event_seen.cap()},
            },
        }

    def history(self, limit=100):
        return self._store.recent(limit=limit)

    # --- bounded read queries (GET /api/history, recording samples) -------
    # Read-only: no collection, no schedule change, no writes. Raise
    # query.QueryError (400/404/410) for client mistakes; anything else is an
    # unexpected failure for the web layer to log and answer with 500.

    def query_history(self, raw_query=''):
        """Durable history page: normalized observations, oldest first."""
        p = query.parse_history_params(raw_query)
        now = self._clock()
        window = (now - query.DEFAULT_WINDOW_S
                  if durable.sane_ts(now) else None)
        res = self._store.query(
            start=p['start'], end=p['end'], limit=p['limit'],
            cursor=p['cursor'], sample_source=p['sample_source'],
            window_start=window)
        return query.envelope(res['samples'], res['next_cursor'],
                              res['skipped_lines'])

    def query_recording_samples(self, job_id, raw_query=''):
        """One bounded page of a Recording's samples, oldest first."""
        p = query.parse_samples_params(raw_query, recording.GROUPS)
        res = self._rec_store.query_samples(
            job_id, limit=p['limit'], group=p['group'], after=p['after'],
            cursor=p['cursor'])
        return query.envelope(res['samples'], res['next_cursor'],
                              res['skipped_lines'])

    def status(self):
        """Lightweight health/introspection payload for the dev/status page.

        Serves cached metadata only -- no router I/O.
        """
        web = None
        if self._web_port_status is not None:
            try:
                web = self._web_port_status()
            except Exception:
                web = None
        snap = self.snapshot()
        return {
            'app': 'sdwan_analyzer',
            'phase': 'backend',
            'version': VERSION,
            'recording': self._recording.status_block(),
            'storage': self._store.stats(),
            'config_versions': self._config_store.summary(),
            'section_freshness': {k: dict(v) for k, v in
                                  self._freshness.items()},
            'web': web,
            'snapshot_ts': self._snapshot_ts,
            'collectors': self._collector_health(),
            'system_summary': (snap.get('system_summary')
                               if isinstance(snap, dict) else None),
        }


# Cellular fields safe to surface (SECURITY / PRIVACY). Excludes IMSI, ICCID,
# MDN, GPS, credentials/secrets. Only SD-WAN-useful metrics.
_SAFE_CELLULAR_KEYS = (
    'CARRID', 'SERDIS', 'HOMECARRID', 'RFBAND', 'RFCHANNEL',
    'RSRP', 'RSRQ', 'SINR', 'DBM', 'SS', 'CELLTECH',
    'PRD', 'MDL', 'DISP_CONN', 'SIMSLOT', 'CONNTEXT',
)


def normalize_cellular_safe(diagnostics):
    """Return a privacy-filtered subset of modem diagnostics.

    Only known-safe SD-WAN-relevant metrics pass through. IMEI/IMSI/ICCID/
    MDN/GPS/secrets are never included even if present in the raw object.
    """
    if not isinstance(diagnostics, dict):
        return None
    out = {}
    for k in _SAFE_CELLULAR_KEYS:
        if k in diagnostics and diagnostics.get(k) is not None:
            out[k] = diagnostics.get(k)
    return out
