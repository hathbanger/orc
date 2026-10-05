"""Outcomes and delegations carry a target issue, a rejection class and a reporter (#189)."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
from fusion_decisions import DecisionStore, read_jsonl
from fusion_policy import routing_report


class OutcomeFieldsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "CODEX_HOME": str(self.root / "codex"),
                                      "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off"})
        env.start()
        self.addCleanup(env.stop)
        worker = self.root / "claude-fixture"
        worker.write_text(f"#!{sys.executable}\nimport json\n"
                          "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'s',"
                          "'result':'STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))\n")
        worker.chmod(0o755)
        self.config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}, "claude": {"command": str(worker)}})

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.object(core, "load_config", return_value=(self.config, None)):
            try:
                code = core.main(["--workspace", str(self.workspace), *argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def delegate(self, *extra):
        code, out, _ = self.cli("--json", "delegate", "--agent", "claude", "--role", "suite-author", *extra, "author it")
        self.assertEqual(code, 0, out)
        return json.loads(out)

    def outcomes(self):
        return [e for e in read_jsonl(DecisionStore(self.workspace).path) if e.get("event", "").startswith("outcome")]

    def test_a_rejection_records_issue_class_and_reporter_and_stays_a_lead_verdict(self):
        run = self.delegate()["run_id"]
        code, _, _ = self.cli("outcome", run, "--rejected", "--stage", "gate", "--issue", "o/r#7",
                              "--rejection-class", "suite_red", "--reporter", "tenet", "--reason", "x")
        self.assertEqual(code, 0)
        [event] = self.outcomes()
        self.assertEqual((event["issue"], event["rejection_class"], event["reporter"], event["source"]),
                         ("o/r#7", "suite_red", "tenet", "lead"))

    def test_an_unknown_class_or_malformed_issue_is_refused_and_records_nothing(self):
        run = self.delegate()["run_id"]
        for extra in (("--rejection-class", "vibes"), ("--issue", "repo-7"), ("--issue", "o/r#x")):
            with self.subTest(extra=extra):
                code, _, _ = self.cli("outcome", run, "--rejected", *extra)
                self.assertEqual(code, 2)
        self.assertEqual(self.outcomes(), [])
        with self.assertRaisesRegex(ValueError, "rejection class"):
            core.record_outcome(self.workspace, run, True, "ok", rejection_class="suite_red")

    def test_delegate_records_the_issue_and_an_outcome_inherits_it(self):
        result = self.delegate("--issue", "o/r#7")
        self.assertEqual(result["issue"], "o/r#7")
        saved = json.loads((Path(result["artifacts"]["run_dir"]) / "result.json").read_text())
        self.assertEqual(saved["issue"], "o/r#7")
        [span] = [s for s in core.RunStore(self.workspace).traces(10) if s.get("run_id") == result["run_id"]]
        self.assertEqual(span["issue"], "o/r#7")
        self.assertEqual(self.cli("outcome", result["run_id"], "--rejected", "--rejection-class", "no_diff")[0], 0)
        self.assertEqual(self.outcomes()[-1]["issue"], "o/r#7")

    def test_routing_report_counts_rejections_per_issue_latest_wins_and_withdrawn_excluded(self):
        runs = [self.delegate("--issue", "o/r#7")["run_id"] for _ in range(4)]
        for run in runs:
            self.cli("outcome", run, "--rejected", "--rejection-class", "suite_red", "--reason", "red")
        self.cli("outcome", runs[0], "--withdraw", "--reason", "graded the wrong tree")
        self.cli("outcome", runs[1], "--accepted", "--reason", "green on re-grade")
        report = routing_report(read_jsonl(DecisionStore(self.workspace).path))
        self.assertEqual(report["rejections"], [{"issue": "o/r#7", "rejection_class": "suite_red", "count": 2}])

    def test_withdraw_still_removes_a_verdict_with_the_new_fields(self):
        run = self.delegate()["run_id"]
        self.cli("outcome", run, "--rejected", "--issue", "o/r#7", "--reporter", "tenet", "--reason", "x")
        self.assertEqual(self.cli("outcome", run, "--withdraw", "--reason", "mistake")[0], 0)
        from fusion_policy import effective_outcomes
        self.assertNotIn(run, effective_outcomes(read_jsonl(DecisionStore(self.workspace).path)))

    def test_old_calls_are_unchanged(self):
        run = self.delegate()["run_id"]
        self.assertEqual(self.cli("outcome", run, "--accepted")[0], 0)
        [event] = self.outcomes()
        self.assertTrue(event["accepted"])
        for field in ("issue", "rejection_class", "reporter"):
            self.assertNotIn(field, event)

    def test_the_mcp_tool_takes_the_same_fields(self):
        tools = {tool["name"]: tool for tool in core.tool_definitions()}
        properties = tools["fusion_outcome"]["inputSchema"]["properties"]
        self.assertEqual(set(properties["rejection_class"]["enum"]), set(core.REJECTION_CLASSES))
        self.assertIn("issue", properties)
        self.assertIn("reporter", properties)
        self.assertIn("issue", tools["fusion_delegate"]["inputSchema"]["properties"])


if __name__ == "__main__":
    unittest.main()
