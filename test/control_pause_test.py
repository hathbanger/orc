"""Operator control pause (#197): new worker runs stop from every caller; quota probes still run."""
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
import fusion_control
import fusion_core as core
from fusion_decisions import DecisionStore, read_jsonl


class ControlPauseTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        self.home = self.root / "orc"
        env = patch.dict(os.environ, {"ORC_HOME": str(self.home), "CODEX_HOME": str(self.root / "codex"),
                                      "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FUSION_CONTROL_WORKSPACE", None)
        self.marker = self.root / "worker-ran"
        worker = self.root / "claude-fixture"
        worker.write_text(f"#!{sys.executable}\nimport json, pathlib\n"
                          f"pathlib.Path({str(self.marker)!r}).write_text('ran')\n"
                          "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'s',"
                          "'result':'STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))\n")
        worker.chmod(0o755)
        self.config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}, "claude": {"command": str(worker)}})

    def pause(self, **extra):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "control.json").write_text(json.dumps({"state": "pause", "reason": "outage", **extra}))

    def task(self, **extra):
        return {**core.make_task(self.workspace, "claude", "x", "review", [], [], None, False, False), **extra}

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(core, "load_config", return_value=(self.config, None)):
            code = core.main(["--workspace", str(self.workspace), *argv])
        return code, out.getvalue()

    def test_a_pause_refuses_delegate_without_starting_a_worker(self):
        self.pause()
        code, out = self.cli("delegate", "--agent", "claude", "--role", "review", "inspect it")
        self.assertEqual(code, 2)
        self.assertIn("paused_control", out)
        self.assertFalse(self.marker.exists())
        [row] = [e for e in read_jsonl(DecisionStore(self.workspace).path) if e.get("event") == "routing_log"]
        self.assertEqual((row["scope"], row["reason"], row["chosen"]), ("control", "control", None))
        self.assertEqual(row["control"]["reason"], "outage")

    def test_quota_probes_still_run(self):
        self.pause()
        result = core.dispatch(self.config, self.task(quota_probe=True), core.RunStore(self.workspace))
        self.assertEqual(result["status"], "success")
        self.assertTrue(self.marker.exists())

    def test_an_expired_pause_and_no_file_dispatch_normally(self):
        self.pause(until="2000-01-01T00:00:00Z")
        self.assertEqual(core.dispatch(self.config, self.task(), core.RunStore(self.workspace))["status"], "success")
        self.marker.unlink()
        (self.home / "control.json").unlink()
        self.assertEqual(core.dispatch(self.config, self.task(), core.RunStore(self.workspace))["status"], "success")
        self.assertTrue(self.marker.exists())

    def test_either_control_file_pauses_and_an_unreadable_one_fails_closed(self):
        shared = self.root / "control-ws"
        (shared / ".fusion").mkdir(parents=True)
        (shared / ".fusion" / "control.json").write_text(json.dumps({"state": "pause", "reason": "shared"}))
        with patch.dict(os.environ, {"FUSION_CONTROL_WORKSPACE": str(shared)}):
            self.assertEqual(fusion_control.paused()["reason"], "shared")
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "control.json").write_text("{not json")
        self.assertIn("unreadable", fusion_control.paused()["reason"])

    def test_cli_pauses_resumes_and_reports(self):
        code, out = self.cli("control", "pause", "--reason", "studio down", "--until", "2999-01-01T00:00:00Z")
        self.assertEqual(code, 0)
        code, out = self.cli("control", "status", "--json")
        self.assertEqual(code, 2)
        status = json.loads(out)
        self.assertEqual((status["paused"]["reason"], status["paused"]["until"]), ("studio down", "2999-01-01T00:00:00Z"))
        self.assertEqual(self.cli("control", "resume")[0], 0)
        code, out = self.cli("control", "status")
        self.assertEqual(code, 0)
        self.assertIn("running", out)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.cli("control", "pause", "--until", "tomorrow")

    def test_a_paused_workflow_dispatches_no_node_and_can_resume(self):
        from fusion_workflow import run_workflow
        spec = self.root / "wf.json"
        spec.write_text(json.dumps({"task": "inspect", "max_attempts": 1, "nodes": [
            {"id": "look", "task": "Inspect the repository.", "agent": "claude", "role": "read-only investigator"}]}))
        self.pause()
        outcome = run_workflow(self.workspace, self.config, spec)
        self.assertEqual(outcome["status"], "paused_control", outcome.get("status"))
        self.assertEqual(outcome["nodes"][0]["status"], "paused_control")
        self.assertEqual(outcome["nodes"][0]["attempts"], 0)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
