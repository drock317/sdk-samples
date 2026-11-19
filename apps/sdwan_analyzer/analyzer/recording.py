"""Recording jobs: backend-owned, temporary, high-resolution capture.

The BACKEND owns the job and its clock. The browser only starts/stops/reads
it, so closing or refreshing the browser never affects a running job. Only
one job may be active, with a hard 30-minute cap.

Collection (driven by tick(), called ~1/s by the app's main loop; every read
is SERIAL because it goes through AnalyzerService.collect under its lock):

    group     source                         cadence
    wan       status/wan/devices             5 s   (state, counters, cellular)
    wbond     status/sdwan_adv/wan_bonding   5 s
    steering  status/wan/steering (1 read)   10 s
    qoe       status/sdwan_adv/qoe           10 s
    swans     status/wan/swans               10 s  (unseen (uid, epoch) only)

Static config is read once at start and once at end (version refs compared;
a change emits a recording_config_changed event). Loadbalancers are never
polled during a Recording. Samples are tagged sample_source='recording' with
job_id and are stored in the RecordingStore, separate from job metadata.

Scheduled interaction: while a job is active the Recording worker is the
source of runtime observations, so AnalyzerService.collect_scheduled() SKIPS
(no duplicate NCOS reads, no scheduled observation) and just advances the
unchanged schedule. No analytics are computed here.

LIMITATION: jobs/samples are in memory only (lost on app restart/reboot).
"""

import cp

from . import collectors

MAX_DURATION_SEC = 1800
DEFAULT_DURATION_SEC = 600
PROFILE_NAME = 'default_v1'

STATES = ('recording', 'completed', 'stopped', 'expired', 'failed')

# group -> (cadence seconds, collector keys). Order = serial read order.
PROFILE = (
    ('wan', 5, ('wan_devices',)),
    ('wbond', 5, ('wan_bonding',)),
    ('steering', 10, ('steering_rules', 'steering_events', 'steering_stats',
                      'steering_intents')),
    ('qoe', 10, ('qoe',)),
    ('swans', 10, ('swans',)),
)

# Valid sample groups (``recording_group``): the profile groups + 'config'.
GROUPS = tuple(g for g, _, _ in PROFILE) + ('config',)

EV_STARTED = 'recording_started'
EV_STOPPED = 'recording_stopped'
EV_EXPIRED = 'recording_expired'
EV_FAILED = 'recording_failed'
EV_CONFIG = 'recording_config_changed'

_MAX_LABEL = 80
_MAX_NOTES = 500


def _err(code, message):
    return {'ok': False, 'error': code, 'message': message}


class RecordingController(object):
    def __init__(self, service, store):
        self._svc = service
        self._store = store
        self._seq = 0
        self._rt = {}            # job_id -> private runtime state
        self._finalize = []      # job_ids awaiting the end-config pass

    # --- views -----------------------------------------------------------

    def _now(self):
        return self._svc._clock()

    def view(self, job):
        """Public status view of one job (metadata + live timing)."""
        if job is None:
            return None
        now = int(self._now())
        end = job['ended_ts'] if job['ended_ts'] is not None else now
        elapsed = max(0, end - job['started_ts'])
        active = job['state'] == 'recording'
        out = {k: job[k] for k in (
            'job_id', 'label', 'notes', 'state', 'started_ts', 'ended_ts',
            'max_duration_sec', 'sample_profile', 'created_by_ui',
            'stop_reason')}
        out.update({
            'elapsed_sec': elapsed,
            'remaining_sec': (max(0, job['max_duration_sec'] - elapsed)
                              if active else 0),
            'sample_count': self._store.sample_count(job['job_id']),
            'collector_errors': job['collector_error_count'],
        })
        return out

    def status_block(self):
        job = self._store.active_recording()
        if job is None:
            return {'active': False, 'job_id': None, 'state': None,
                    'started_ts': None, 'elapsed_sec': 0,
                    'remaining_sec': 0, 'max_duration_sec': None,
                    'sample_count': 0, 'collector_errors': 0}
        v = self.view(job)
        v['active'] = True
        return {k: v[k] for k in (
            'active', 'job_id', 'state', 'started_ts', 'elapsed_sec',
            'remaining_sec', 'max_duration_sec', 'sample_count',
            'collector_errors')}

    def list_recordings(self):
        return [self.view(j) for j in self._store.list_recordings()]

    def active_recording(self):
        return self.view(self._store.active_recording())

    def get_recording(self, job_id):
        job = self._store.get_recording(job_id)
        if job is None:
            return None
        out = self.view(job)
        out.update({
            'sample_counts': dict(job['sample_counts']),
            'collector_health': {k: dict(v) for k, v in
                                 job['collector_health'].items()},
            'config_versions_start': job['config_versions_start'],
            'config_versions_end': job['config_versions_end'],
            'config_changes': list(job['config_changes']),
            'events': self._store.events(job_id),
            'profile': {g: {'interval_sec': i, 'collectors': list(k)}
                        for g, i, k in PROFILE},
        })
        return out

    def recording_samples(self, job_id, limit=None):
        return self._store.recording_samples(job_id, limit)

    # --- operations ------------------------------------------------------

    def start(self, label='', notes='', max_duration_sec=None,
              created_by_ui=False):
        dur = DEFAULT_DURATION_SEC if max_duration_sec is None \
            else max_duration_sec
        if isinstance(dur, bool) or not isinstance(dur, (int, float)):
            return _err('invalid', 'max_duration_sec must be a number')
        if dur <= 0:
            return _err('invalid', 'max_duration_sec must be > 0')
        if dur > MAX_DURATION_SEC:
            return _err('invalid', 'max_duration_sec must be <= {}'.format(
                MAX_DURATION_SEC))
        if not isinstance(label, str) or not isinstance(notes, str):
            return _err('invalid', 'label and notes must be strings')
        if self._store.active_recording() is not None:
            return _err('active_exists', 'a recording is already active')
        now = self._now()
        self._seq += 1
        job_id = 'rec-{}-{:03d}'.format(int(now), self._seq)
        while self._store.get_recording(job_id) is not None:
            # A durable store reloads earlier jobs after a restart; never
            # reuse an ID (and so a directory) that already exists.
            self._seq += 1
            job_id = 'rec-{}-{:03d}'.format(int(now), self._seq)
        job = {
            'job_id': job_id,
            'label': label.strip()[:_MAX_LABEL],
            'notes': notes.strip()[:_MAX_NOTES],
            'state': 'recording',
            'started_ts': int(now),
            'ended_ts': None,
            'max_duration_sec': int(dur),
            'sample_profile': PROFILE_NAME,
            'created_by_ui': bool(created_by_ui),
            'stop_reason': None,
            'collector_error_count': 0,
            'collector_health': {},
            'sample_counts': {g + '_samples': 0 for g, _, _ in PROFILE},
            'config_versions_start': None,
            'config_versions_end': None,
            'config_changes': [],
        }
        job['sample_counts']['config_samples'] = 0
        self._store.add_job(job)
        self._rt[job['job_id']] = {
            'next': {g: now for g, _, _ in PROFILE}, 'start_cfg': False}
        self._event(job, EV_STARTED, 'started')
        self._store.job_changed(job)
        return {'ok': True, 'recording': self.view(job)}

    def stop(self, job_id):
        job = self._store.get_recording(job_id)
        if job is None:
            return _err('not_found', 'unknown recording')
        if job['state'] != 'recording':
            return _err('not_active', 'recording is not active')
        self._end(job, 'stopped', EV_STOPPED, 'user')
        return {'ok': True, 'recording': self.view(job)}

    # --- worker ----------------------------------------------------------

    def tick(self):
        """Advance the backend-owned job. Cheap when idle. Never raises."""
        job = None
        try:
            self._run_finalize()
            job = self._store.active_recording()
            if job is None:
                return
            now = self._now()
            # Durable store: periodic fsync + metadata checkpoint (no-op for
            # the in-memory store).
            self._store.housekeeping(now)
            if now - job['started_ts'] >= job['max_duration_sec']:
                self._end(job, 'expired', EV_EXPIRED, 'max_duration')
                self._run_finalize()
                return
            rt = self._rt[job['job_id']]
            if not rt['start_cfg']:
                rt['start_cfg'] = True
                self._collect(job, 'config', collectors.STATIC_CONFIG_KEYS)
            for group, interval, keys in PROFILE:
                if job['state'] != 'recording':
                    break
                if now >= rt['next'][group]:
                    nxt = rt['next'][group] + interval
                    rt['next'][group] = nxt if nxt > now else now + interval
                    self._collect(job, group, keys)
            if (job['state'] == 'recording'
                    and job['config_versions_start'] is None):
                # Reference point = config versions active once the first
                # full pass (static + runtime-derived) has been collected.
                job['config_versions_start'] = self._versions()
            if (job['state'] == 'recording' and self._store.sample_count(
                    job['job_id']) >= self._store.max_samples()):
                self._end(job, 'expired', EV_EXPIRED, 'sample_cap')
                self._run_finalize()
        except Exception as e:
            self._fail(job, 'recording infrastructure error: {}'.format(e))

    def _collect(self, job, group, keys):
        obs = self._svc.collect('recording', keys, group=group,
                                job_id=job['job_id'])
        job['sample_counts'][group + '_samples'] += 1
        for err in obs.get('collector_errors', []):
            job['collector_error_count'] += 1
            h = job['collector_health'].setdefault(
                err['collector'], {'errors': 0})
            h.update({'errors': h['errors'] + 1,
                      'last_error_type': err.get('error_type'),
                      'last_error': err.get('error'),
                      'last_error_ts': obs['collected_ts']})
        return obs

    def _versions(self):
        return {n: v['version'] for n, v in
                self._svc._config_store.summary().items()}

    def _run_finalize(self):
        """End-of-job static config pass + semantic comparison."""
        while self._finalize:
            job = self._store.get_recording(self._finalize.pop(0))
            if job is None:
                continue
            self._collect(job, 'config', collectors.STATIC_CONFIG_KEYS)
            end = self._versions()
            job['config_versions_end'] = end
            start = job['config_versions_start']
            changed = [] if start is None else sorted(
                n for n in end if end[n] != start.get(n))
            if changed:
                job['config_changes'] = [
                    {'name': n, 'start_version': start.get(n),
                     'end_version': end[n]} for n in changed]
                self._event(job, EV_CONFIG,
                            'config changed during recording: {}'.format(
                                ', '.join(changed)),
                            detail=job['config_changes'])
            # Job complete: fsync samples, final metadata, close, retention.
            self._store.job_changed(job, final=True)

    def _end(self, job, state, event, reason):
        job['state'] = state
        job['stop_reason'] = reason
        job['ended_ts'] = int(self._now())
        self._event(job, event, reason)
        self._finalize.append(job['job_id'])
        self._store.job_changed(job, force_sync=True)

    def _fail(self, job, reason):
        try:
            if job is not None and job['state'] == 'recording':
                job['state'] = 'failed'
                job['stop_reason'] = reason
                job['ended_ts'] = int(self._now())
                self._event(job, EV_FAILED, reason)
                self._store.job_changed(job, final=True)
        except Exception:
            pass

    def shutdown(self):
        """Graceful app stop: end any active job as stopped/app_shutdown.

        No end-of-job config pass (no router I/O while shutting down); the
        store fsyncs the samples and writes the final metadata.
        """
        try:
            job = self._store.active_recording()
            if job is None:
                return
            job['state'] = 'stopped'
            job['stop_reason'] = 'app_shutdown'
            job['ended_ts'] = int(self._now())
            self._event(job, EV_STOPPED, 'app_shutdown')
            if job['job_id'] in self._finalize:
                self._finalize.remove(job['job_id'])
            self._store.job_changed(job, final=True)
        except Exception as e:
            cp.log('recording: shutdown error: {}'.format(e))

    def _event(self, job, event_type, reason, detail=None):
        ev = {'job_id': job['job_id'], 'event_ts': int(self._now()),
              'event_type': event_type, 'reason': reason}
        if detail is not None:
            ev['detail'] = detail
        self._store.add_event(ev)

    def store_sample(self, job_id, obs):
        return self._store.append_sample(job_id, obs)
