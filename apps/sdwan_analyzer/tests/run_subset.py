"""Run selected test modules only (development convenience).

    .venv/bin/python3 apps/sdwan_analyzer/tests/run_subset.py foundation_tests
"""
import importlib
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import run_tests as R  # noqa: E402


def main(names):
    for name in names:
        mod = importlib.import_module(name)
        try:
            mod.bind(R.check, R._free_port)
        except TypeError:
            mod.bind(R.check)
        for t in mod.TESTS:
            try:
                t()
            except Exception:
                traceback.print_exc()
                R._failures.append('{} raised'.format(t.__name__))
    print('')
    if R._failures:
        print('RESULT: {} FAILURE(S)'.format(len(R._failures)))
        for f in R._failures:
            print('  - {}'.format(f))
        return 1
    print('RESULT: ALL PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:] or ['foundation_tests']))
