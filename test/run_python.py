"""Keep unexpected coverage gaps fatal, with one explicit local-runtime exception."""
from pathlib import Path
import os
import shutil
import sys
import unittest

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


if __name__ == '__main__':
    os.environ['FUSION_LAYA_PYTHON'] = shutil.which('false') or '/usr/bin/false'
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern='*_test.py')
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if check_result(result) else 1)
