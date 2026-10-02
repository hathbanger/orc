"""Keep unexpected coverage gaps fatal, with one explicit local-runtime exception."""
from pathlib import Path
import os
import shutil
import sys
import unittest

MANAGED_LAYA = Path.home() / '.local/share/orc/laya/bin/python'
SANDBOX_PROBE = ('codex_permissions_test.CodexPermissionsTest.'
                 'test_real_sandbox_allows_git_in_repos_and_worktrees_but_protects_other_paths')


def expected_skip(test, reason):
    if test.id() != SANDBOX_PROBE:
        return False
    if reason == 'requires the macOS Codex sandbox':
        return sys.platform != 'darwin' or not shutil.which('codex')
    return reason == 'installed Codex does not support named permission profiles'


def check_result(result, stream=sys.stderr):
    valid = result.wasSuccessful() and result.testsRun > 0 and not result.expectedFailures
    for test, reason in result.skipped:
        expected = expected_skip(test, reason)
        print(f"{'Expected integration skip' if expected else 'UNEXPECTED SKIP'}: {test.id()}: {reason}", file=stream)
        valid = valid and expected
    if not valid:
        print('Python suite failed, ran no tests, or has unexpected missing coverage.', file=stream)
    return valid


def record_runtimes(module):
    """Record every interpreter the Laya runtime resolves during the run."""
    resolved, original = [], module.runtime_python

    def runtime_python(options):
        python = original(options)
        resolved.append(python)
        return python
    module.runtime_python = runtime_python
    return resolved


def managed_runtimes(resolved, stream=sys.stderr):
    managed = sorted({python for python in resolved if Path(python).expanduser().absolute() == MANAGED_LAYA})
    for python in managed:
        print(f'Tests resolved the installed Laya runtime: {python}', file=stream)
    return managed


if __name__ == '__main__':
    os.environ['FUSION_LAYA_PYTHON'] = shutil.which('false') or '/usr/bin/false'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import fusion_decisions
    resolved = record_runtimes(fusion_decisions)
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern='*_test.py')
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if check_result(result) and not managed_runtimes(resolved) else 1)
