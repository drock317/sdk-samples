"""v0.8.5 steering-source reliability regression tests.

Live finding (R2400, v0.8.4): status/wan/steering intermittently "succeeded"
with no usable object. On-router cp.get() returns None (no exception) on a
config-store socket timeout/error, the split turned that into four ok=True
keys with None values, no collector error was recorded, and the legacy
`steering_definitions` object versioned the resulting empty rule set,
alternating v1 <-> v2 (false configuration changes).

These tests drive the REAL collectors.collect_keys / _steering_split_results
(only the lowest read, collectors._get_result, is stubbed) and cover:
  * transport failure, empty source (None / {} / non-dict), incomplete
    source (parent without usable rules) and a successful empty rules list;
  * sanitized, transition-only diagnostic logging;
  * no false legacy steering_definitions changes across the R2400
    alternating pattern, with the gap still visible in collector_errors,
    collector health and the validity view;
  * partial and unconfirmed-empty reads withheld for BOTH definition
    objects, and a confirmed empty still versioned (real change).

Run through run_tests.py (shared `check`).
"""
import copy

import cp
from analyzer import collectors
from analyzer.service import AnalyzerService

import fixtures

check = None            # bound by run_tests.main()
_Clock = None

T0 = 1790380800
STEERING_KEYS = ('steering_rules', 'steering_events', 'steering_stats',
                 'steering_intents')
_PATH_KEY = {p: k for k, p in collectors._PATHS.items()}


def bind(check_fn, clock_cls):
    global check, _Clock
    check = check_fn
    _Clock = clock_cls


class _Reads(object):
    """Stub collectors._get_result: fixture data per path; the steering
    parent is scriptable. `steering` is a value (ok read) or the sentinel
    FAIL (transport failure)."""

    FAIL = object()

    def __init__(self):
        self.data = dict(fixtures.base_raw())
        self.data.update(fixtures.heavy_raw())
        self.steering = copy.deepcopy(fixtures.STEERING_PARENT)
        self.logs = []

    def __enter__(self):
        self._saved_get = collectors._get_result
        self._saved_log = cp.log
        collectors._steering_source.update(state=None, repeat=0)

        def fake(path):
            if path == 'status/wan/steering':
                if self.steering is self.FAIL:
                    return collectors.CollectResult(
                        False, error='timeout after 8s',
                        error_type=collectors.ERR_TIMEOUT)
                return collectors.CollectResult(True, value=self.steering,
                                                acquired_at=None)
            key = _PATH_KEY.get(path)
            return collectors.CollectResult(
                True, value=self.data.get(key) if key else None)

        collectors._get_result = fake
        cp.log = lambda msg, *a, **k: self.logs.append(str(msg))
        return self

    def __exit__(self, *exc):
        collectors._get_result = self._saved_get
        cp.log = self._saved_log
        collectors._steering_source.update(state=None, repeat=0)

    def steering_logs(self):
        return [m for m in self.logs if 'status/wan/steering' in m]


def _split(reads, value):
    reads.steering = value
    return collectors._steering_split_results()


# --- collector classification --------------------------------------------

def test_split_transport_failure():
    print('test_split_transport_failure')
    with _Reads() as r:
        split = _split(r, _Reads.FAIL)
        check(all(not split[k].ok for k in STEERING_KEYS),
              'transport failure: every steering key failed')
        check({split[k].error_type for k in STEERING_KEYS}
              == {collectors.ERR_TIMEOUT},
              'transport failure: original error_type kept')
        check(collectors.steering_source_state()['state']
              == 'transport_failure', 'transport failure classified')


def test_split_empty_source_variants():
    print('test_split_empty_source_variants')
    for label, value in (('None', None), ('empty dict', {}),
                         ('list', []), ('string', 'x')):
        with _Reads() as r:
            split = _split(r, value)
            check(all(not split[k].ok for k in STEERING_KEYS),
                  'empty source ({}): every steering key failed'.format(
                      label))
            check({split[k].error_type for k in STEERING_KEYS}
                  == {collectors.ERR_EMPTY_SOURCE},
                  'empty source ({}): error_type empty_source'.format(label))
            check(all(split[k].value is None for k in STEERING_KEYS),
                  'empty source ({}): no value passed on'.format(label))


def test_split_incomplete_source():
    print('test_split_incomplete_source')
    with _Reads() as r:
        parent = copy.deepcopy(fixtures.STEERING_PARENT)
        del parent['rules']
        split = _split(r, parent)
        check(not split['steering_rules'].ok and
              split['steering_rules'].error_type
              == collectors.ERR_INCOMPLETE_SOURCE,
              'incomplete: rules failed as incomplete_source')
        check(split['steering_events'].ok and split['steering_stats'].ok,
              'incomplete: other children of the parent still current')
        parent['rules'] = None
        split = _split(r, parent)
        check(split['steering_rules'].error_type
              == collectors.ERR_INCOMPLETE_SOURCE,
              'incomplete: rules None is incomplete, not empty')


def test_split_successful_empty_rules():
    print('test_split_successful_empty_rules')
    with _Reads() as r:
        parent = copy.deepcopy(fixtures.STEERING_PARENT)
        parent['rules'] = []
        split = _split(r, parent)
        check(split['steering_rules'].ok and
              split['steering_rules'].value == [],
              'explicit empty rules list is a successful read')
        check(collectors.steering_source_state()['state']
              == 'ok_empty_rules', 'classified ok_empty_rules')


def test_split_logging_is_sanitized_and_transition_only():
    print('test_split_logging_is_sanitized_and_transition_only')
    with _Reads() as r:
        _split(r, copy.deepcopy(fixtures.STEERING_PARENT))
        check(r.steering_logs() == [], 'steady ok: nothing logged')
        _split(r, None)
        _split(r, None)
        _split(r, None)
        logs = r.steering_logs()
        check(len(logs) == 1 and 'no usable object (no data)' in logs[0],
              'three identical empty reads: one log line')
        _split(r, copy.deepcopy(fixtures.STEERING_PARENT))
        logs = r.steering_logs()
        check(len(logs) == 2 and 'read ok' in logs[1]
              and 'previous: empty_source x3' in logs[1],
              'recovery logged once with the held count')
        parent = copy.deepcopy(fixtures.STEERING_PARENT)
        del parent['rules']
        _split(r, parent)
        parent['rules'] = []
        _split(r, parent)
        _split(r, _Reads.FAIL)
        logs = r.steering_logs()
        check(any('incomplete (rules NoneType)' in m for m in logs) and
              any('explicit empty rules list' in m for m in logs) and
              any('read failed (timeout)' in m for m in logs),
              'four distinct states produce four distinct messages')
        raw_names = [x['xpolicy']['name'] for x in fixtures.STEERING_RULES
                     if isinstance(x.get('xpolicy'), dict)]
        check(not any(n in m for m in logs for n in raw_names),
              'log lines carry no rule names or payload')


# --- service: legacy steering_definitions --------------------------------

def _svc():
    return AnalyzerService(clock=_Clock(T0))


def _legacy(svc):
    return svc._config_store.current('steering_definitions')


def _versions(svc, name):
    return [v['version'] for v in svc._config_store.versions(name)]


def _pass(svc, reads, value):
    reads.steering = value
    svc._clock.t += 60
    return svc.collect('scheduled', keys=collectors.RUNTIME_KEYS)


def test_service_r2400_alternation_no_false_changes():
    print('test_service_r2400_alternation_no_false_changes')
    good = copy.deepcopy(fixtures.STEERING_PARENT)
    with _Reads() as r:
        svc = _svc()
        svc.collect_startup()
        v0 = _legacy(svc)
        d0 = svc._config_store.current('steering_rule_defs')
        check(v0 is not None and d0 is not None, 'startup versions both')
        # The observed R2400 pattern plus the other empty-source shapes.
        pattern = [None, good, None, None, good, {}, good, 'x', good,
                   _Reads.FAIL, good]
        for value in pattern:
            obs = _pass(svc, r, copy.deepcopy(value)
                        if isinstance(value, dict) else value)
            ref = obs['config_versions']['steering_definitions']
            failed = value is _Reads.FAIL or not (
                isinstance(value, dict) and value)
            check(ref['version'] == v0['version'] and
                  ref['changed'] is False,
                  'legacy reference stays v{} unchanged ({})'.format(
                      v0['version'], 'failed' if failed else 'ok'))
            errs = {e['collector']: e['error_type']
                    for e in obs['collector_errors']}
            if failed:
                check(errs.get('steering_rules') in (
                    collectors.ERR_EMPTY_SOURCE, collectors.ERR_TIMEOUT),
                    'failed read visible in collector_errors')
                check(ref['attribution'] == 'latest_known',
                      'failed read: legacy attributed latest_known')
                check('steering' not in obs and 'steering_events' not in obs,
                      'failed read: no re-stamped steering runtime')
            else:
                check('steering_rules' not in errs and 'steering' in obs,
                      'ok read: steering runtime recorded, no error')
        check(_versions(svc, 'steering_definitions') == [v0['version']],
              'legacy store holds exactly one version')
        check(_versions(svc, 'steering_rule_defs') == [d0['version']],
              'steering_rule_defs untouched (semantics unchanged)')
        check(svc._defs_tracker.live_transitions() == [],
              'no steering_rule_defs transition recorded')
        # The last pass recovered; force one more failed pass and inspect
        # the source-health surfaces.
        _pass(svc, r, None)
        health = svc._runtime_raw.health()['steering_rules']
        check(health['error_type'] == collectors.ERR_EMPTY_SOURCE and
              health['error'] is not None,
              'collector health exposes the current empty_source error')
        val = svc.query_validity()
        check(val['steering_source']['state'] == collectors.ERR_EMPTY_SOURCE
              and val['steering_source']['repeat'] == 1,
              'validity view reports steering_source empty_source x1')
        check(val['collectors']['steering_rules']['error_type']
              == collectors.ERR_EMPTY_SOURCE,
              'validity collectors section exposes empty_source')
        check(val['steering_definitions']['current_version']
              == d0['version'], 'validity: definitions still last good')


def _defs_status(svc):
    return svc._defs_tracker.status(svc._config_store)


def test_service_partial_read_withholds_legacy():
    print('test_service_partial_read_withholds_legacy')
    with _Reads() as r:
        svc = _svc()
        svc.collect_startup()
        v0 = _legacy(svc)['version']
        partial = copy.deepcopy(fixtures.STEERING_PARENT)
        partial['rules'] = partial['rules'][:1] + [{'name': 'broken'}]
        obs = _pass(svc, r, partial)
        check(_defs_status(svc)['withheld_reason'] == 'partial',
              'tracker withholds the partial read')
        ref = obs['config_versions']['steering_definitions']
        check(_legacy(svc)['version'] == v0 and ref['changed'] is False,
              'partial read: legacy not versioned')
        check(any('withheld (partial)' in m for m in r.logs),
              'partial read: withheld diagnostic logged')
        _pass(svc, r, copy.deepcopy(fixtures.STEERING_PARENT))
        check(_versions(svc, 'steering_definitions') == [v0],
              'recovery: still one legacy version')
        check(any('accepted again (previously withheld: partial)' in m
                  for m in r.logs), 'acceptance after withholding logged')


def test_service_empty_rules_needs_confirmation_for_legacy():
    print('test_service_empty_rules_needs_confirmation_for_legacy')
    with _Reads() as r:
        svc = _svc()
        svc.collect_startup()
        v0 = _legacy(svc)['version']
        empty = copy.deepcopy(fixtures.STEERING_PARENT)
        empty['rules'] = []
        obs1 = _pass(svc, r, copy.deepcopy(empty))
        check(_defs_status(svc)['withheld_reason'] == 'empty_unconfirmed',
              'first empty rules read withheld (unconfirmed)')
        check(_legacy(svc)['version'] == v0 and
              obs1['config_versions']['steering_definitions']['changed']
              is False, 'unconfirmed empty: legacy not versioned')
        check(not any(e['collector'] == 'steering_rules'
                      for e in obs1['collector_errors']),
              'a successful empty read is not a collector error')
        obs2 = _pass(svc, r, copy.deepcopy(empty))
        ref = obs2['config_versions']['steering_definitions']
        check(_legacy(svc)['version'] != v0 and ref['changed'] is True,
              'confirmed empty: legacy versions the real change')
        check(obs2['config_versions']['steering_rule_defs']['changed']
              is True, 'confirmed empty: steering_rule_defs agrees')


TESTS = [
    test_split_transport_failure,
    test_split_empty_source_variants,
    test_split_incomplete_source,
    test_split_successful_empty_rules,
    test_split_logging_is_sanitized_and_transition_only,
    test_service_r2400_alternation_no_false_changes,
    test_service_partial_read_withholds_legacy,
    test_service_empty_rules_needs_confirmation_for_legacy,
]
