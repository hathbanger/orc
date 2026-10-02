"""The CI runner must not hide skipped tests or expected failures."""
import io
import unittest
from unittest.mock import patch
import run_python


class RunnerTest(unittest.TestCase):
    def result(self, *, skip=None, failure=False, expected_failure=False, identity=None):
        class Probe(unittest.TestCase):
            def runTest(self):
                if skip:
                    self.skipTest(skip)
                if failure:
                    self.fail('fixture regression')
        if expected_failure:
            Probe.runTest = unittest.expectedFailure(Probe.runTest)
        probe = Probe()
        if identity:
            probe.id = lambda: identity
        return unittest.TextTestRunner(stream=io.StringIO()).run(probe)

    def test_success_and_real_failures(self):
        self.assertTrue(run_python.check_result(self.result(), io.StringIO()))
        self.assertFalse(run_python.check_result(self.result(failure=True), io.StringIO()))
        self.assertFalse(run_python.check_result(self.result(failure=True, expected_failure=True), io.StringIO()))
        self.assertFalse(run_python.check_result(self.result(expected_failure=True), io.StringIO()))

    def test_only_the_exact_unavailable_sandbox_probe_is_allowed(self):
        with patch.object(run_python.sys, 'platform', 'linux'):
            output = io.StringIO()
            self.assertTrue(run_python.check_result(self.result(skip='requires the macOS Codex sandbox', identity=run_python.SANDBOX_PROBE), output))
            self.assertIn('Expected integration skip', output.getvalue())
            self.assertFalse(run_python.check_result(self.result(skip='requires the macOS Codex sandbox'), io.StringIO()))
            self.assertFalse(run_python.check_result(self.result(skip='unexpected reason', identity=run_python.SANDBOX_PROBE), io.StringIO()))

    def test_an_available_macos_runtime_cannot_claim_the_unavailable_skip(self):
        with patch.object(run_python.sys, 'platform', 'darwin'), patch.object(run_python.shutil, 'which', return_value='/bin/codex'):
            self.assertFalse(run_python.check_result(self.result(skip='requires the macOS Codex sandbox', identity=run_python.SANDBOX_PROBE), io.StringIO()))
            self.assertTrue(run_python.check_result(self.result(skip='installed Codex does not support named permission profiles', identity=run_python.SANDBOX_PROBE), io.StringIO()))

    def test_resolving_the_installed_laya_runtime_fails_the_run(self):
        module = type('Decisions', (), {'runtime_python': staticmethod(lambda options: options['python'])})
        resolved = run_python.record_runtimes(module)
        module.runtime_python({'python': 'false'})
        self.assertEqual(run_python.managed_runtimes(resolved, io.StringIO()), [])
        module.runtime_python({'python': str(run_python.MANAGED_LAYA)})
        output = io.StringIO()
        self.assertEqual(run_python.managed_runtimes(resolved, output), [str(run_python.MANAGED_LAYA)])
        self.assertIn('installed Laya runtime', output.getvalue())

    def test_empty_suite_fails(self):
        self.assertFalse(run_python.check_result(unittest.TestResult(), io.StringIO()))
