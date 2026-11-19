"""v0.5.0: primary WAN identity provenance (U13) and configuration
reconciliation retry (U14).

U13 rules under test
  * Recording `wan` group reads wan_devices and primary_device together, in
    one serial pass.
  * A historical observation carries `primary_wan` / `is_primary` only when
    primary_device was read successfully in the SAME pass. If that read
    failed, or the pass did not request it (targeted refresh), the identity
    is omitted while the rest of the WAN telemetry is kept.
  * The snapshot keeps the last-known-good primary.
  * A primary change is observed at the first poll after it. These tests make
    no claim about the exact instant of a failover, only that it lies between
    two consecutive polls.

U14 rules under test
  * `_last_config_ts` advances only when every configuration read of the
    reconciliation succeeded. A failure leaves last-known-good config and
    version references alone and the next scheduled pass is eligible again.

Run through run_tests.py (shared `check`).
"""
import copy
import os

import cp
from analyzer import collectors
import acquisition_tests as A

check = None            # bound by run_tests.main()

T0 = A.T0
NEW_PRIMARY = 'mdm-1ea0ac'


def bind(check_fn):
    global check
    check = check_fn


def _primary_of(obs):
    wan = obs.get('wan')
    return None if wan is None else wan.get('primary_wan', 'ABSENT')


def _flags(obs):
    return [p.get('is_primary', 'ABSENT') for p in obs['wan']['paths']]


def _no_identity(obs):
    return ('primary_wan' not in obs['wan'] and
            all('is_primary' not in p for p in obs['wan']['paths']))


# --- U13 ---------------------------------------------------------------------

def test_primary_read_with_wan_group_serially():
    print('test_primary_read_with_wan_group_serially')
    from analyzer import recording
    wan = [k for g, _, k in recording.PROFILE if g == 'wan'][0]
    check(wan == ('wan_devices', 'primary_device'),
          'Recording wan group reads wan_devices then primary_device')
    # The established serial mechanism: collectors.collect_keys reads one
    # path after another through the guarded reader.
    order, depth = [], {'now': 0, 'max': 0}
    saved = cp.get

    def fake_get(path):
        depth['now'] += 1
        depth['max'] = max(depth['max'], depth['now'])
        order.append(path)
        depth['now'] -= 1
        return {'path': path}
    cp.get = fake_get
    try:
        out = collectors.collect_keys(wan)
    finally:
        cp.get = saved
    check(order == ['status/wan/devices', 'status/wan/primary_device'] and
          depth['max'] == 1 and all(r.ok for r in out.values()),
          'one status read after the other, never concurrent')


def test_recording_wan_samples_carry_primary_from_same_pass():
    print('test_recording_wan_samples_carry_primary_from_same_pass')
    with A._Env(pass_s=0) as env:
        env.overrides['wan_devices'] = A._tel_devices(1)
        job = env.svc.start_recording('t', max_duration_sec=120)[
            'recording']['job_id']
        for _ in range(16):
            env.svc.recording_tick()
            env.clock.t += 1
        wan = [s for s in env.svc.recording_samples(job)
               if s.get('recording_group') == 'wan']
        check(len(wan) >= 3, 'wan samples recorded ({})'.format(len(wan)))
        check(all(_primary_of(s) == 'ethernet-wan' for s in wan) and
              all(sum(1 for f in _flags(s) if f is True) == 1 for s in wan),
              'every wan sample states the primary, exactly one is_primary')
        check(('wan_devices', 'primary_device') in env.calls and
              ('wan_devices',) not in env.calls,
              'the wan group is collected as one two-key pass')
        check(all(s['interfaces'] and s['cellular'] for s in wan),
              'interface and cellular telemetry unchanged')


def test_primary_read_failure_keeps_wan_telemetry():
    print('test_primary_read_failure_keeps_wan_telemetry')
    with A._Env(pass_s=0) as env:
        env.overrides['wan_devices'] = A._tel_devices(1)
        job = env.svc.start_recording('t', max_duration_sec=120)[
            'recording']['job_id']
        start = env.clock.t

        def run(n):
            for _ in range(n):
                env.svc.recording_tick()
                env.clock.t += 1
        run(11)
        env.fail.add('primary_device')
        run(12)
        env.fail.discard('primary_device')
        run(12)
        wan = [s for s in env.svc.recording_samples(job)
               if s.get('recording_group') == 'wan']
        bad = [s for s in wan if start + 11 <= s['collected_ts'] < start + 23]
        good = [s for s in wan if s not in bad]
        check(bad and good, 'samples inside and outside the failure window '
              '({} / {})'.format(len(bad), len(good)))
        check(all(_no_identity(s) for s in bad),
              'primary read failed: no primary_wan, no is_primary')
        check(all(s['interfaces'] and s['cellular'] and
                  s['wan']['paths'] for s in bad),
              'successful WAN telemetry kept in those samples')
        check(all(any(e['collector'] == 'primary_device'
                      for e in s['collector_errors']) for s in bad),
              'the failure is recorded in collector_errors')
        check(all(_primary_of(s) == 'ethernet-wan' for s in good),
              'identity present again once the read succeeds')
        snap = env.svc.snapshot()
        check(snap['wan']['primary_device'] == 'ethernet-wan',
              'snapshot keeps the last-known-good primary')


def test_failover_is_observed_at_the_next_poll():
    print('test_failover_is_observed_at_the_next_poll')
    with A._Env(pass_s=0) as env:
        env.overrides['wan_devices'] = A._tel_devices(1)
        job = env.svc.start_recording('t', max_duration_sec=120)[
            'recording']['job_id']
        change_at = None
        for i in range(30):
            if i == 12:
                env.overrides['primary_device'] = NEW_PRIMARY
                change_at = env.clock.t
            env.svc.recording_tick()
            env.clock.t += 1
        wan = [s for s in env.svc.recording_samples(job)
               if s.get('recording_group') == 'wan']
        seq = [_primary_of(s) for s in wan]
        flips = [i for i in range(1, len(seq)) if seq[i] != seq[i - 1]]
        check(seq[0] == 'ethernet-wan' and seq[-1] == NEW_PRIMARY and
              len(flips) == 1, 'one change in the recorded sequence: '
              '{}'.format(seq))
        i = flips[0]
        first_new, last_old = wan[i], wan[i - 1]
        check(last_old['collected_ts'] < change_at + 1 and
              change_at <= first_new['collected_ts'] <= change_at + 5,
              'first sample with the new primary is a normal poll within one '
              '5 s cadence of the change (the instant itself is not known)')
        check(first_new['collected_ts'] - last_old['collected_ts'] == 5,
              'the change is bracketed by two consecutive polls 5 s apart')
        check([p['uid'] for p in first_new['wan']['paths']
               if p['is_primary']] == [NEW_PRIMARY] and
              [p['uid'] for p in last_old['wan']['paths']
               if p['is_primary']] == ['ethernet-wan'],
              'is_primary moves with primary_wan')
        check(env.svc.snapshot()['wan']['primary_device'] == NEW_PRIMARY,
              'snapshot follows the new primary')


def test_targeted_refresh_omits_primary_identity():
    print('test_targeted_refresh_omits_primary_identity')
    with A._Tmp() as base:
        with A._Env(base=base, pass_s=2) as env:
            env.overrides['wan_devices'] = A._tel_devices(1)
            ok0 = env.svc.collect_startup()
            check(_primary_of(ok0) == 'ethernet-wan',
                  'startup pass records the primary')
            env.clock.t += 30
            env.svc.refresh('bonding')
            b = env.svc.history(1)[-1]
            check(env.calls[-1] == ('wan_devices', 'wan_bonding'),
                  'bonding refresh does not request primary_device')
            check(_no_identity(b),
                  'bonding refresh: identity omitted, cached value not '
                  're-stamped')
            check(b['interfaces'] and b['cellular'] and
                  b['wan']['paths'] and 'wbond' in b,
                  'bonding refresh keeps wan, interface, cellular and wbond')
            snap = env.svc.snapshot()
            check(snap['wan']['primary_device'] == 'ethernet-wan' and
                  snap['system_summary']['primary_device'] == 'ethernet-wan',
                  'snapshot keeps the last-known-good primary')
            env.clock.t += 30
            env.svc.refresh('wan_paths')
            check(_primary_of(env.svc.history(1)[-1]) == 'ethernet-wan',
                  'wan_paths refresh requests and records the primary')
            env.clock.t += 900
            sched = env.svc.collect_scheduled()
            check(_primary_of(sched) == 'ethernet-wan',
                  'scheduled pass records the primary')
            page = env.svc.query_history('limit=20')['samples']
            check(len(page) == 4 and _no_identity(page[1]) and
                  all(not _no_identity(page[i]) for i in (0, 2, 3)),
                  'persisted history agrees: only the bonding pass lacks '
                  'the identity')


# --- U14 ---------------------------------------------------------------------

def _config_lines(base):
    path = os.path.join(base, 'tmp', 'telemetry', 'config_versions.jsonl')
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return sum(1 for ln in f if ln.strip())


def _has_config_keys(keys):
    return set(collectors.CONFIG_KEYS) <= set(keys)


def test_config_reconciliation_failure_does_not_postpone_retry():
    print('test_config_reconciliation_failure_does_not_postpone_retry')
    from analyzer import service as service_mod
    due = service_mod.CONFIG_RECONCILE_S
    with A._Tmp() as base:
        with A._Env(base=base, pass_s=2) as env:
            env.overrides['wan_devices'] = A._tel_devices(1)
            ok0 = env.svc.collect_startup()
            ts0 = env.svc._last_config_ts
            check(ts0 == ok0['collected_ts'],
                  'startup reconciliation recorded')
            cv0 = copy.deepcopy(ok0['config_versions'])
            snap0 = copy.deepcopy(env.svc.snapshot())
            lines0 = _config_lines(base)

            # Inside the hour: runtime only.
            env.clock.t += 900
            env.svc.collect_scheduled()
            check(not _has_config_keys(env.calls[-1]) and
                  env.svc._last_config_ts == ts0,
                  'before the hour: no config read, timestamp unchanged')

            # Due, and one config read fails.
            env.clock.t = ts0 + due + 5
            env.fail.add('identities_ip')
            bad = env.svc.collect_scheduled()
            check(_has_config_keys(env.calls[-1]),
                  'due pass requests every config key')
            check(env.svc._last_config_ts == ts0,
                  'failed reconciliation does not advance _last_config_ts')
            check({n: v['version'] for n, v in bad['config_versions'].items()}
                  == {n: v['version'] for n, v in cv0.items()} and
                  not any(v.get('changed') for v in
                          bad['config_versions'].values()),
                  'version references unchanged and none marked changed')
            check(_config_lines(base) == lines0,
                  'no new config version stored by the failed pass')
            snap1 = env.svc.snapshot()
            check(snap1['identities'] == snap0['identities'] and
                  snap1['wan']['config'] == snap0['wan']['config'] and
                  snap1['traffic_classes'] == snap0['traffic_classes'],
                  'last-known-good configuration retained')

            # Still failing at the next scheduled pass: eligible again.
            env.clock.t += 900
            env.svc.collect_scheduled()
            check(_has_config_keys(env.calls[-1]) and
                  env.svc._last_config_ts == ts0,
                  'next pass retries the reconciliation (not postponed an '
                  'hour); still failing, still unchanged')

            # Recovery.
            env.fail.discard('identities_ip')
            env.clock.t += 900
            good = env.svc.collect_scheduled()
            check(_has_config_keys(env.calls[-1]) and
                  env.svc._last_config_ts == good['collected_ts'],
                  'recovery pass reconciles and advances _last_config_ts')
            check({n: v['version'] for n, v in good['config_versions'].items()}
                  == {n: v['version'] for n, v in cv0.items()},
                  'unchanged configuration keeps its version numbers')

            # After success the normal hourly cadence resumes.
            env.clock.t += 900
            env.svc.collect_scheduled()
            check(not _has_config_keys(env.calls[-1]),
                  'after success the next pass is runtime only again')
            check(_config_lines(base) == lines0,
                  'durable config history gained no spurious versions')


def test_failed_expensive_config_read_is_retried():
    print('test_failed_expensive_config_read_is_retried')
    # loadbalancers is part of the reconciliation set: its failure counts.
    with A._Env(pass_s=2) as env:
        env.svc.collect_startup()
        ts0 = env.svc._last_config_ts
        env.clock.t = ts0 + 3600 + 5
        env.fail.add('loadbalancers')
        env.svc.collect_scheduled()
        check(env.svc._last_config_ts == ts0,
              'loadbalancers failure leaves _last_config_ts unchanged')
        env.fail.discard('loadbalancers')
        env.clock.t += 900
        env.svc.collect_scheduled()
        check(env.svc._last_config_ts > ts0,
              'retry succeeds and advances it')


def test_startup_config_failure_retried_on_first_scheduled_pass():
    print('test_startup_config_failure_retried_on_first_scheduled_pass')
    with A._Env(pass_s=2) as env:
        env.fail.add('identities_intent')
        env.svc.collect_startup()
        check(env.svc._last_config_ts is None,
              'startup with a failed config read is not recorded as '
              'reconciled')
        env.fail.discard('identities_intent')
        env.clock.t += 900
        env.svc.collect_scheduled()
        check(_has_config_keys(env.calls[-1]) and
              env.svc._last_config_ts is not None,
              'first scheduled pass (15 min, not 1 h later) reads config '
              'and records the reconciliation')


TESTS = [
    test_primary_read_with_wan_group_serially,
    test_recording_wan_samples_carry_primary_from_same_pass,
    test_primary_read_failure_keeps_wan_telemetry,
    test_failover_is_observed_at_the_next_poll,
    test_targeted_refresh_omits_primary_identity,
    test_config_reconciliation_failure_does_not_postpone_retry,
    test_failed_expensive_config_read_is_retried,
    test_startup_config_failure_retried_on_first_scheduled_pass,
]
