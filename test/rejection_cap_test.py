"""An opt-in cap on rejections per (issue, role) stops blind retries (#192)."""
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


class RejectionCapTest(unittest.TestCase):
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
        self.sentinel = self.root / "spawned"
        worker = self.root / "claude-fixture"
        worker.write_text(f"#!{sys.executable}\nimport json, pathlib\n"
                          f"p = pathlib.Path({str(self.sentinel)!r}); p.write_text(str(int(p.read_text() if p.exists() else 0) + 1))\n"
                          "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'s',"
                          "'result':'STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))\n")
        worker.chmod(0o755)
        self.config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off", "max_rejections_per_issue": {"suite-author": 2}},
                                                      "claude": {"command": str(worker)}})

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(core, "load_config", return_value=(self.config, None)):
            try:
                code = core.main(["--workspace", str(self.workspace), *argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def delegate(self, role="suite-author", *extra):
        code, out = self.cli("--json", "delegate", "--agent", "claude", "--role", role, "--issue", "o/r#7", *extra, "author it")
        return code, json.loads(out) if out.strip().startswith("{") else out

    def spawned(self):
        return int(self.sentinel.read_text()) if self.sentinel.exists() else 0

    def reject(self, n, role="suite-author"):
        runs = []
        for i in range(n):
            code, result = self.delegate(role)
            self.assertEqual(code, 0)
            self.cli("outcome", result["run_id"], "--rejected", "--rejection-class", "suite_red", "--reason", f"red {i}")
            runs.append(result["run_id"])
        return runs

    def test_the_cap_refuses_a_third_attempt_without_spawning(self):
        self.reject(2)
        before = self.spawned()
        code, result = self.delegate()
        self.assertEqual(code, 4)
        self.assertEqual((result["status"], result["issue"], result["role"], result["rejections"]), ("capped", "o/r#7", "suite-author", 2))
        self.assertEqual(sorted(result["reasons"]), ["red 0", "red 1"])
        self.assertEqual(self.spawned(), before)

    def test_another_role_on_the_same_issue_still_runs(self):
        self.reject(2)
        code, result = self.delegate("implementation")
        self.assertEqual((code, result["status"]), (0, "success"))

    def test_a_withdrawn_rejection_lowers_the_count(self):
        runs = self.reject(2)
        self.cli("outcome", runs[0], "--withdraw", "--reason", "graded the wrong tree")
        code, result = self.delegate()
        self.assertEqual((code, result["status"]), (0, "success"))

    def test_an_override_runs_once_and_is_logged(self):
        self.reject(2)
        code, result = self.delegate("suite-author", "--override-cap", "new suite fixes the oracle")
        self.assertEqual((code, result["status"]), (0, "success"))
        [event] = [e for e in read_jsonl(DecisionStore(self.workspace).path) if e.get("event") == "cap_override"]
        self.assertEqual((event["issue"], event["role"], event["reason"]), ("o/r#7", "suite-author", "new suite fixes the oracle"))
        self.assertEqual(self.delegate()[0], 4)

    def test_without_config_nothing_is_capped(self):
        del self.config["decisions"]["max_rejections_per_issue"]
        self.reject(3)
        self.assertEqual(self.delegate()[0], 0)

    def test_routing_report_lists_capped_refusals_and_bad_config_is_refused(self):
        self.reject(2)
        self.delegate()
        self.delegate()
        report = routing_report(read_jsonl(DecisionStore(self.workspace).path))
        self.assertEqual(report["capped"], [{"issue": "o/r#7", "role": "suite-author", "count": 2}])
        self.assertEqual(report["logged_choices"], 0)
        self.config["decisions"]["max_rejections_per_issue"] = {"suite-author": 0}
        with self.assertRaisesRegex(ValueError, "max_rejections_per_issue"):
            core.dispatch(self.config, {**core.make_task(self.workspace, "claude", "x", "suite-author", [], [], None, False, True),
                                        "issue": "o/r#7"}, core.RunStore(self.workspace))


if __name__ == "__main__":
    unittest.main()
