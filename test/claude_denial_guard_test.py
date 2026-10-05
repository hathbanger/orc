"""A Claude worker denied a baseline tool, or Bash too many times in a row, stops early (#190)."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HANDOFF = "STATUS: partial\nSUMMARY: worked around a denied command\nCHANGED: none\nTESTS: none\nBLOCKERS: none"


def fixture(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


def bash_denial(n):
    """The captured denied Bash call, with ids made unique for its n-th repetition."""
    _, *events = copy.deepcopy(fixture("claude_denial_bash.jsonl"))
    text = json.dumps(events).replace("toolu_01XJfvAGwJjf8Eut5AUMSUVT", f"toolu_bash_denied_{n}")
    return json.loads(text)


def bash_success(n):
    use = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": f"toolu_ok_{n}", "name": "Bash", "input": {"command": "ls"}}]}}
    done = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"toolu_ok_{n}", "content": "README.md", "is_error": False}]}}
    return [use, done]


APPROVAL = ("Permission for this tool use was denied. It requires approval, and this session has no approval surface "
            "\u2014 nobody can answer a permission prompt here \u2014 so it was denied automatically.")


def read_needing_approval(n):
    """A Read refused because it needed approval, in the shape of a real headless (asyncAgent) denial."""
    use = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": f"toolu_read_{n}", "name": "Read", "input": {"file_path": f"/workspace/src/{n}.py"}}]}}
    event = {"type": "system", "subtype": "permission_denied", "tool_name": "Read", "tool_use_id": f"toolu_read_{n}",
             "decision_reason_type": "asyncAgent", "decision_reason": "no approval surface in this session; permission request denied automatically",
             "message": APPROVAL}
    done = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"toolu_read_{n}", "content": APPROVAL, "is_error": True}]}}
    return [use, event, done]


def result(denials):
    return {"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "result": HANDOFF,
            "permission_denials": denials}


class ClaudeDenialGuardTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0",
                                      "FUSION_DECISIONS_MODE": "off"})
        env.start()
        self.addCleanup(env.stop)

    def run_stream(self, events, hang=True, **claude):
        """Run a fake `claude` that prints `events` and then hangs (as a stuck run would) or exits 0."""
        stream = self.root / "stream.jsonl"
        stream.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        worker = self.root / "claude-fixture"
        worker.write_text(f"#!{sys.executable}\nimport sys, time\n"
                          f"sys.stdout.write(open({str(stream)!r}).read()); sys.stdout.flush()\n"
                          + ("time.sleep(60)\n" if hang else ""))
        worker.chmod(0o755)
        config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}, "claude": {"command": str(worker), **claude}})
        task = core.make_task(self.workspace, "claude", "x", "implementation", [], [], None, False, True)
        started = time.monotonic()
        value = core.dispatch(config, task, core.RunStore(self.workspace))
        return value, time.monotonic() - started

    def test_baseline_denials_needing_approval_stop_the_run_at_the_limit(self):
        init = fixture("claude_denial_bash.jsonl")[0]
        value, elapsed = self.run_stream([init, *read_needing_approval(0), *read_needing_approval(1)])
        self.assertLess(elapsed, 20)
        self.assertEqual((value["status"], value["exit_code"]), ("error", 125))
        self.assertEqual(core.failure_class(value), "permission_denied")
        self.assertIn("Read", value["denied_tools"])
        self.assertEqual([d["reason_type"] for d in value["denied"]], ["asyncAgent", "asyncAgent"])

    def test_one_baseline_denial_does_not_stop_the_run(self):
        init = fixture("claude_denial_bash.jsonl")[0]
        events = [init, *read_needing_approval(0), result([{"tool_name": "Read", "tool_use_id": "toolu_read_0", "tool_input": {}}])]
        value, _ = self.run_stream(events, hang=False)
        self.assertNotEqual(value["exit_code"], 125)

    def test_a_deny_rule_on_a_protected_path_is_recorded_but_never_stops_the_run(self):
        read = fixture("claude_denial_read.jsonl")
        denial = {"tool_name": "Read", "tool_use_id": "toolu_01QYYWDHagb76pn1Yx124m2q", "tool_input": {}}
        value, _ = self.run_stream([*read, *read[1:], result([denial])], hang=False)
        self.assertNotEqual(value["exit_code"], 125)
        self.assertEqual(value["denied"][0]["tool"], "Read")
        self.assertIn("denied by your permission settings", value["denied"][0]["message"])

    def test_command_output_that_mentions_approval_is_not_a_denial(self):
        init = fixture("claude_denial_bash.jsonl")[0]
        use = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_test", "name": "Bash", "input": {"command": "npm test"}}]}}
        failed = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_test", "is_error": True,
             "content": "FAIL test/guard.test.ts: expected 'requires approval' and 'has been denied' to be reported"}]}}
        value, _ = self.run_stream([init, use, failed, result([])], hang=False)
        self.assertEqual(value["denied"], [])

    def test_a_worked_around_bash_denial_runs_to_its_handoff(self):
        denials = [{"tool_name": "Bash", "tool_use_id": f"toolu_bash_denied_{n}", "tool_input": {"command": "cat secret.txt"}}
                   for n in range(2)]
        events = [fixture("claude_denial_bash.jsonl")[0], *bash_denial(0), *bash_denial(1), result(denials)]
        value, _ = self.run_stream(events, hang=False)
        self.assertEqual(value["status"], "partial")
        self.assertEqual(core.failure_class(value), "worker_error")
        self.assertEqual([d.get("reason_type") for d in value["denied"]], ["subcommandResults", "subcommandResults"])

    def test_consecutive_bash_denials_stop_the_run(self):
        events = [fixture("claude_denial_bash.jsonl")[0], *[e for n in range(6) for e in bash_denial(n)]]
        value, elapsed = self.run_stream(events)
        self.assertLess(elapsed, 20)
        self.assertEqual(value["exit_code"], 125)
        self.assertEqual(core.failure_class(value), "permission_denied")
        self.assertEqual(len(value["denied"]), 6)
        self.assertIn("6 Bash calls in a row", value["blockers"][-1])

    def test_a_successful_bash_call_resets_the_count(self):
        events = [fixture("claude_denial_bash.jsonl")[0], *[e for n in range(5) for e in bash_denial(n)], *bash_success(0),
                  *[e for n in range(5, 10) for e in bash_denial(n)],
                  result([{"tool_name": "Bash", "tool_use_id": f"toolu_bash_denied_{n}", "tool_input": {}} for n in range(10)])]
        value, _ = self.run_stream(events, hang=False)
        self.assertEqual(value["status"], "partial")

    def test_zero_disables_the_bash_limit(self):
        events = [fixture("claude_denial_bash.jsonl")[0], *[e for n in range(8) for e in bash_denial(n)],
                  result([{"tool_name": "Bash", "tool_use_id": f"toolu_bash_denied_{n}", "tool_input": {}} for n in range(8)])]
        value, _ = self.run_stream(events, hang=False, max_bash_denials=0)
        self.assertEqual(value["status"], "partial")

    def test_a_bad_limit_is_refused(self):
        with self.assertRaisesRegex(ValueError, "max_bash_denials"):
            self.run_stream([], hang=False, max_bash_denials=-1)


if __name__ == "__main__":
    unittest.main()
