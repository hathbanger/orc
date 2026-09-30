"""Subscription first, a metered API lane only as overflow; keys never leak into subscription lanes."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
from fusion_policy import route_candidates


class ApiOverflowTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc-home"), "FUSION_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.key = self.root / "anthropic-api-key"
        self.config = core.deep_merge(core.DEFAULTS, {"execution_mode": "yolo", "decisions": {
            "mode": "off", "auto_routes": ["sub", "api"], "overflow_routes": ["api"]}, "routes": {
            "sub": {"agent": "claude", "model": "claude-opus-5-5"},
            "api": {"agent": "claude", "model": "claude-opus-5-5", "billing": "api", "account": "anthropic-api",
                    "requires": [str(self.key)], "daily_budget_usd": 150}}})
        self.config["claude"]["command"] = sys.executable
        self.store = core.RunStore(self.workspace)

    def keys(self, spans=(), rejected=None):
        task = core.make_task(self.workspace, "auto", "Fix it", "implementation", [], [], None, False, True)
        with patch.object(self.store, "traces", return_value=list(spans)):
            return {c["key"] for c in route_candidates(self.config, task, self.store, rejected=rejected)}

    def test_overflow_waits_while_the_subscription_lane_is_available(self):
        self.key.write_text("k")
        rejected = {}
        self.assertEqual(self.keys(rejected=rejected), {"sub"})
        self.assertIn("overflow", rejected["api"])

    def test_overflow_carries_work_when_the_subscription_lane_is_out(self):
        self.key.write_text("k")
        quota = {"agent": "claude", "route": "sub", "failure_class": "quota", "end_time_ms": core.now_ms()}
        self.assertEqual(self.keys([quota]), {"api"})

    def test_a_lane_waits_for_its_required_file(self):
        rejected = {}
        quota = {"agent": "claude", "route": "sub", "failure_class": "quota", "end_time_ms": core.now_ms()}
        self.assertEqual(self.keys([quota], rejected), set())
        self.assertIn("requires", rejected["api"])

    def test_daily_budget_parks_the_lane(self):
        self.key.write_text("k")
        now = core.now_ms()
        spans = [{"agent": "claude", "route": "sub", "failure_class": "quota", "end_time_ms": now},
                 {"agent": "claude", "route": "api", "end_time_ms": now - 1000, "usage": {"cost_usd": 100.0}},
                 {"agent": "claude", "route": "api", "end_time_ms": now - 2000, "usage": {"cost_usd": 60.0}},
                 {"agent": "claude", "route": "api", "end_time_ms": now - 2 * 86400 * 1000, "usage": {"cost_usd": 500.0}}]
        rejected = {}
        self.assertEqual(self.keys(spans, rejected), set())
        self.assertIn("$160.00 of its $150.00 daily budget", rejected["api"])

    def test_keys_reach_only_lanes_declared_metered(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "leaked", "ANTHROPIC_AUTH_TOKEN": "leaked"}):
            for route, expected in (("sub", None), ("api", "leaked")):
                task = core.make_task(self.workspace, "claude", "x", "implementation", [], [], None, False, True, route=route)
                _, env, _ = core.agent_command(self.config, task, None)
                self.assertEqual(env.get("ANTHROPIC_API_KEY"), expected)
                self.assertEqual(env.get("ANTHROPIC_AUTH_TOKEN"), expected)
        self.config["routes"]["sub"]["env"] = {"ANTHROPIC_API_KEY": "configured"}
        task = core.make_task(self.workspace, "claude", "x", "implementation", [], [], None, False, True, route="sub")
        self.assertNotIn("ANTHROPIC_API_KEY", core.agent_command(self.config, task, None)[1])
        with self.assertRaisesRegex(ValueError, "billing"):
            core.metered({"billing": "prepaid"})

    def test_a_subscription_run_on_an_api_key_is_flagged(self):
        worker = self.root / "claude-fixture"
        worker.write_text(f"#!{sys.executable}\nimport json\n"
                          "print(json.dumps({'type':'system','subtype':'init','apiKeySource':'ANTHROPIC_API_KEY'}))\n"
                          "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'s',"
                          "'result':'STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))\n")
        worker.chmod(0o755)
        config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}})
        config["claude"]["command"] = str(worker)
        task = core.make_task(self.workspace, "claude", "x", "review", [], [], None, False, False)
        result = core.dispatch(config, task, core.RunStore(self.workspace))
        self.assertEqual(result["api_key_source"], "ANTHROPIC_API_KEY")
        self.assertTrue(any(b.startswith("billing: a subscription lane ran on ANTHROPIC_API_KEY") for b in result["blockers"]))


if __name__ == "__main__":
    unittest.main()
