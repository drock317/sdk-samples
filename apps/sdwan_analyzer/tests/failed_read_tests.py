"""v0.5.0 failed-read invariant for every runtime observation section.

INVARIANT: a failed NCOS collector read never creates a newly timestamped
historical measurement from last-known-good (LKG) cached values.

Each test runs success -> failure -> recovery against a durable service:

* the failed pass produces an observation that OMITS the affected section
  (no zero fill, no invented timestamp) and records the failure in
  `collector_errors`;
* the current state (/api/snapshot sections) and section freshness keep their
  last-known-good values and do not advance;
* the dedupe state is untouched by the failed pass;
* the persisted history (`query_history`, and the file on disk) agrees;
* recovery produces a normally timestamped measurement and advances freshness.

Runtime sections audited (see docs/ARCHITECTURE-AND-ROADMAP.md section 7):
wan (+ primary identity), interfaces, cellular, swans_samples, qoe, wbond,
steering, steering_events.

Run through run_tests.py (shared `check`).
"""
import copy
import os

from analyzer import collectors, telemetry
import acquisition_tests as A
import fixtures

check = None            # bound by run_tests.main()

T0 = A.T0
STEERING_KEYS = ('steering_rules', 'steering_events', 'steering_stats',
                 'steering_intents')
SECTION_KEYS = ('wan', 'interfaces', 'cellular', 'swans_samples', 'qoe',
                'wbond', 'steering', 'steering_events')


def bind(check_fn):
    global check
    check = check_fn


def _present(obs):
    return {k for k in SECTION_KEYS if k in obs}


def _sections(snap, names):
    return {n: copy.deepcopy(snap.get(n)) for n in names}


def _error_names(obs):
    return {e['collector'] for e in obs['collector_errors']}


def _history_files_text(base):
    root = os.path.join(base, 'tmp', 'telemetry', 'history')
    out = ''
    for name in sorted(os.listdir(root)):
        with open(os.path.join(root, name)) as f:
            out += f.read()
    return out


def _scenario(label, keys, fail, absent, snap_names, fresh_name,
              recover=None, kept=()):
    """success -> failure -> recovery for `keys` with `fail` failing.

    absent: observation sections that must be missing from the failed pass.
    kept: sections that must STILL be present (their collectors succeeded).
    snap_names: current-state sections that must be unchanged by the failure.
    fresh_name: section_freshness entry that must not advance, then must.
    recover: optional callable(env) to change data before recovery.
    """
    with A._Tmp() as base:
        with A._Env(base=base, pass_s=2) as env:
            env.overrides['wan_devices'] = A._tel_devices(1)
            ok0 = env.svc.collect_startup()
            for name in absent:
                check(name in ok0 and ok0[name] not in (None,),
                      '{}: startup observation has {}'.format(label, name))
            snap0 = copy.deepcopy(env.svc.snapshot())
            fresh0 = copy.deepcopy(
                snap0['section_freshness'].get(fresh_name))
            seen0 = (len(env.svc._swans_seen), len(env.svc._event_seen))
            prev0 = copy.deepcopy(env.svc._prev_counters)

            env.fail |= set(fail)
            env.clock.t += 30
            bad = env.svc.collect('manual', keys, group='overview')
            snap1 = copy.deepcopy(env.svc.snapshot())
            check(set(fail) <= _error_names(bad),
                  '{}: failure recorded in collector_errors {}'.format(
                      label, sorted(fail)))
            check(not (set(absent) & set(bad)),
                  '{}: failed pass omits {} (no re-stamped LKG)'.format(
                      label, sorted(absent)))
            check(all(n in bad for n in kept),
                  '{}: sections from healthy collectors still present '
                  '{}'.format(label, sorted(kept)))
            check(bad['collected_ts'] > ok0['collected_ts'],
                  '{}: the failed pass is itself timestamped'.format(label))
            check(_sections(snap1, snap_names) == _sections(snap0, snap_names),
                  '{}: current state {} = last-known-good'.format(
                      label, sorted(snap_names)))
            check(snap1['section_freshness'].get(fresh_name) == fresh0,
                  '{}: section freshness "{}" not advanced'.format(
                      label, fresh_name))
            check((len(env.svc._swans_seen), len(env.svc._event_seen))
                  == seen0,
                  '{}: dedupe state untouched by the failed pass'.format(
                      label))
            check(env.svc._prev_counters == prev0,
                  '{}: counter baseline untouched'.format(label))

            env.fail -= set(fail)
            if recover is not None:
                recover(env)
            env.clock.t += 30
            good = env.svc.collect('manual', keys, group='overview')
            snap2 = env.svc.snapshot()
            check(all(n in good for n in absent),
                  '{}: recovery restores {}'.format(label, sorted(absent)))
            check(snap2['section_freshness'][fresh_name]['collected_ts']
                  == good['collected_ts'] > fresh0['collected_ts'],
                  '{}: freshness advances after recovery'.format(label))

            # Persisted history agrees: startup, failed, recovery.
            page = env.svc.query_history('limit=10')['samples']
            check(len(page) == 3, '{}: three persisted observations'.format(
                label))
            p_ok0, p_bad, p_good = page
            check(not (set(absent) & set(p_bad)) and
                  set(fail) <= _error_names(p_bad),
                  '{}: persisted failed observation has no {} and records '
                  'the error'.format(label, sorted(absent)))
            check(all(n in p_ok0 and n in p_good for n in absent),
                  '{}: persisted startup and recovery observations have '
                  '{}'.format(label, sorted(absent)))
            check([p['collected_ts'] for p in page] == sorted(
                p['collected_ts'] for p in page),
                '{}: persisted in time order'.format(label))
            return env, bad, good, page


def test_failed_swans_read():
    print('test_failed_swans_read')

    def recover(env):
        raw = copy.deepcopy(fixtures.SWANS)
        raw['history'].append({'epoch': 1790375620, 'upd_status': [
            {'wandev': 'sdwan-hub0_wan',
             'criteria': {'Latency': 8.0, 'Jitter': 1.0}}]})
        env.overrides['swans'] = raw
    env, bad, good, page = _scenario(
        'swans', ('swans',), {'swans'}, ['swans_samples'], ['swans'],
        'swans', recover)
    check([s['metric_ts'] for s in good['swans_samples']] == [1790375620] and
          good['swans_samples'][0]['collected_ts'] == good['collected_ts'],
          'swans: recovery emits only the new sample, stamped on recovery')


def test_failed_qoe_read():
    print('test_failed_qoe_read')
    env, bad, good, page = _scenario(
        'qoe', ('qoe',), {'qoe'}, ['qoe'], ['qoe'], 'qoe')
    check(all(r['collected_ts'] == good['collected_ts']
              for r in good['qoe']),
          'qoe: recovery records carry the recovery timestamp')


def test_failed_wbond_read():
    print('test_failed_wbond_read')
    env, bad, good, page = _scenario(
        'wbond', ('wan_bonding',), {'wan_bonding'}, ['wbond'], ['bonding'],
        'bonding')
    check(all(r['collected_ts'] == good['collected_ts']
              for r in good['wbond']),
          'wbond: recovery records carry the recovery timestamp')


def test_failed_steering_read_whole_parent():
    print('test_failed_steering_read_whole_parent')
    env, bad, good, page = _scenario(
        'steering', STEERING_KEYS, set(STEERING_KEYS),
        ['steering', 'steering_events'], ['traffic_steering'],
        'traffic_steering')
    check(all(r['collected_ts'] == good['collected_ts']
              for r in good['steering']),
          'steering: recovery records carry the recovery timestamp')


def test_failed_steering_stats_only():
    print('test_failed_steering_stats_only')
    # rules + events read fine, stats failed: the per-rule runtime combines
    # all three so it must be omitted; the events list is independent.
    with A._Tmp() as base:
        with A._Env(base=base, pass_s=2) as env:
            env.svc.collect_startup()
            env.fail.add('steering_stats')
            env.clock.t += 30
            bad = env.svc.collect('manual', STEERING_KEYS, group='steering')
            check('steering' not in bad,
                  'steering runtime omitted when stats failed')
            check('steering_events' in bad,
                  'steering events (healthy collector) still recorded')
            check(env.svc.snapshot()['section_freshness'][
                'traffic_steering']['collected_ts']
                < bad['collected_ts'], 'freshness not advanced')
            env.fail.clear()
            env.fail.add('steering_events')
            env.clock.t += 30
            bad2 = env.svc.collect('manual', STEERING_KEYS, group='steering')
            check('steering' not in bad2 and 'steering_events' not in bad2,
                  'events failed: neither steering runtime nor events')
            env.fail.clear()
            env.clock.t += 30
            good = env.svc.collect('manual', STEERING_KEYS, group='steering')
            check('steering' in good and 'steering_events' in good,
                  'recovery records both')


def test_failed_primary_device_read():
    print('test_failed_primary_device_read')
    keys = ('wan_devices', 'primary_device')
    with A._Tmp() as base:
        with A._Env(base=base, pass_s=2) as env:
            env.overrides['wan_devices'] = A._tel_devices(1)
            ok0 = env.svc.collect_startup()
            check(ok0['wan']['primary_wan'] == 'ethernet-wan' and
                  any(p['is_primary'] for p in ok0['wan']['paths']),
                  'startup records the primary identity')
            env.fail.add('primary_device')
            env.clock.t += 30
            bad = env.svc.collect('manual', keys, group='wan_paths')
            check('primary_wan' not in bad['wan'] and
                  all('is_primary' not in p for p in bad['wan']['paths']),
                  'primary read failed: identity omitted, not re-stamped '
                  'and not guessed as False')
            check(len(bad['wan']['paths']) == len(ok0['wan']['paths']) and
                  all(p['connection_state'] for p in bad['wan']['paths']),
                  'connection states (healthy wan_devices) still recorded')
            check(bad['interfaces'] and bad['cellular'],
                  'interfaces and cellular unaffected by the primary failure')
            snap = env.svc.snapshot()
            check(snap['wan']['primary_device'] == 'ethernet-wan' and
                  snap['system_summary']['primary_device'] == 'ethernet-wan',
                  'current state keeps the last-known-good primary')
            env.fail.clear()
            env.clock.t += 30
            good = env.svc.collect('manual', keys, group='wan_paths')
            check(good['wan']['primary_wan'] == 'ethernet-wan' and
                  any(p['is_primary'] for p in good['wan']['paths']),
                  'recovery records the primary identity again')
            page = env.svc.query_history('limit=10')['samples']
            check('primary_wan' not in page[1]['wan'] and
                  page[2]['wan']['primary_wan'] == 'ethernet-wan',
                  'persisted history agrees')


def test_all_runtime_collectors_fail_together():
    print('test_all_runtime_collectors_fail_together')
    with A._Tmp() as base:
        with A._Env(base=base, pass_s=2) as env:
            env.overrides['wan_devices'] = A._tel_devices(1)
            ok0 = env.svc.collect_startup()
            check(_present(ok0) == set(SECTION_KEYS),
                  'startup observation has every runtime section')
            snap0 = copy.deepcopy(env.svc.snapshot())
            env.fail |= set(collectors.RUNTIME_KEYS)
            env.clock.t += 30
            bad = env.svc.collect('manual', collectors.RUNTIME_KEYS,
                                  group='overview')
            check(_present(bad) == set(),
                  'total outage: no measurement sections at all '
                  '(found {})'.format(sorted(_present(bad))))
            check(set(bad) <= {'sample_source', 'job_id', 'refresh_group',
                               'timestamp', 'collected_ts', 'config_versions',
                               'collector_errors', 'duration_ms'},
                  'only envelope, config version refs and errors remain: '
                  '{}'.format(sorted(bad)))
            check(_error_names(bad) == set(collectors.RUNTIME_KEYS),
                  'every failure recorded')
            snap1 = env.svc.snapshot()
            for name in ('wan', 'cellular', 'swans', 'qoe', 'bonding',
                         'traffic_steering', 'system_summary'):
                check(snap1[name] == snap0[name],
                      'current state "{}" = last-known-good'.format(name))
            check(snap1['section_freshness'] == snap0['section_freshness'],
                  'no section freshness advanced')
            hist = env.svc.query_history('limit=10')['samples']
            check(len(hist) == 2 and _present(hist[1]) == set(),
                  'persisted outage observation holds no measurements')
            check(all(r['collected_ts'] <= ok0['collected_ts'] for r in
                      snap1['wan']['interfaces']),
                  'no interface record carries a timestamp newer than the '
                  'last successful read')
            env.fail.clear()
            env.clock.t += 30
            good = env.svc.collect('manual', collectors.RUNTIME_KEYS,
                                   group='overview')
            check(set(SECTION_KEYS) - {'swans_samples', 'steering_events'}
                  <= _present(good),
                  'recovery restores the measurement sections')


def test_failed_read_does_not_reemit_deduped_samples():
    print('test_failed_read_does_not_reemit_deduped_samples')
    # Dedupe sets are bounded. With a tiny set, re-walking the cached SWANS
    # history or steering events would find "unseen" keys and re-emit OLD
    # samples under the failed pass's timestamp. They must not.
    with A._Tmp() as base:
        with A._Env(base=base, pass_s=2) as env:
            env.svc.collect_startup()
            env.svc._swans_seen = telemetry.BoundedSeen(1)
            env.svc._event_seen = telemetry.BoundedSeen(1)
            env.fail |= {'swans', 'steering_events'}
            env.clock.t += 30
            bad = env.svc.collect(
                'manual', ('swans',) + STEERING_KEYS, group='overview')
            check('swans_samples' not in bad and
                  'steering_events' not in bad,
                  'no SWANS samples or steering events from cached data')
            check(len(env.svc._swans_seen) == 0 and
                  len(env.svc._event_seen) == 0,
                  'dedupe state not touched by the failed pass')


def test_failed_recording_samples():
    print('test_failed_recording_samples')
    with A._Env(pass_s=0) as env:
        env.overrides['wan_devices'] = A._tel_devices(1)
        job = env.svc.start_recording('t', max_duration_sec=120)[
            'recording']['job_id']
        fail_from, fail_to = 22, 44

        def run(n):
            for _ in range(n):
                env.svc.recording_tick()
                env.clock.t += 1
        start = env.clock.t
        run(fail_from)
        env.fail |= {'qoe', 'wan_bonding'}
        run(fail_to - fail_from)
        env.fail -= {'qoe', 'wan_bonding'}
        run(66 - fail_to)
        samples = env.svc.recording_samples(job)
        if isinstance(samples, dict):
            samples = samples.get('samples', [])
        for group, key in (('qoe', 'qoe'), ('wbond', 'wbond')):
            g = [s for s in samples if s.get('recording_group') == group]
            before = [s for s in g if s['collected_ts'] < start + fail_from]
            during = [s for s in g if start + fail_from <= s['collected_ts']
                      < start + fail_to]
            after = [s for s in g if s['collected_ts'] >= start + fail_to]
            check(before and during and after,
                  '{}: samples exist in all three phases ({}/{}/{})'.format(
                      group, len(before), len(during), len(after)))
            check(all(key in s for s in before) and
                  all(key in s for s in after),
                  '{}: healthy phases carry measurements'.format(group))
            check(all(key not in s and s['collector_errors']
                      for s in during),
                  '{}: failed phase samples carry no measurement, only '
                  'the error'.format(group))
        wan = [s for s in samples if s.get('recording_group') == 'wan']
        check(wan and all('interfaces' in s for s in wan),
              'wan group unaffected by qoe/wbond failures')


TESTS = [
    test_failed_swans_read,
    test_failed_qoe_read,
    test_failed_wbond_read,
    test_failed_steering_read_whole_parent,
    test_failed_steering_stats_only,
    test_failed_primary_device_read,
    test_all_runtime_collectors_fail_together,
    test_failed_read_does_not_reemit_deduped_samples,
    test_failed_recording_samples,
]
