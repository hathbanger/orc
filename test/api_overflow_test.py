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
from fusion_policy import route_candidates, route_task


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

    def pinned(self, spans=(), **task):
        task = core.make_task(self.workspace, task.pop("agent", "claude"), "Fix it", "implementation", [], [], None, False, True, **task)
        with patch.object(self.store, "traces", return_value=list(spans)), patch.object(core, "executable", return_value=True):
            route_task(self.config, task, self.store)
        return task

    def test_a_pinned_lane_out_of_quota_runs_its_api_twin(self):
        self.key.write_text("k")
        quota = {"agent": "claude", "route": "sub", "failure_class": "quota", "end_time_ms": core.now_ms()}
        task = self.pinned([quota], route="sub")
        self.assertEqual(task["route"], "api")
        self.assertEqual((task["quota_twin"]["from"], task["quota_twin"]["to"], task["quota_twin"]["model"]),
                         ("sub", "api", "claude-opus-5-5"))
        self.assertTrue(task["session_key"].endswith(":api"))

    def test_a_pinned_agent_and_model_runs_its_api_twin(self):
        self.key.write_text("k")
        quota = {"agent": "claude", "model": "claude-opus-5-5", "failure_class": "quota", "end_time_ms": core.now_ms()}
        task = self.pinned([quota], settings_overrides={"model": "claude-opus-5-5"})
        self.assertEqual((task["agent"], task["route"]), ("claude", "api"))

    def test_an_exhausted_reading_moves_a_pin_before_any_failure(self):
        self.key.write_text("k")
        self.store.root.mkdir(parents=True, exist_ok=True)
        reset = core.now_ms() / 1000 + 3600
        self.store.traces_path.write_text(json.dumps({"agent": "claude", "lane_key": "claude", "end_time_ms": core.now_ms(),
            "quota": {"status": "rejected", "windows": {"seven_day": {"used": 1.0, "resets_at": reset}}}}) + "\n")
        self.assertEqual(self.pinned(route="sub")["route"], "api")

    def test_a_pin_stays_put_when_its_account_can_serve_it(self):
        self.key.write_text("k")
        task = self.pinned(route="sub")
        self.assertEqual(task["route"], "sub")
        self.assertNotIn("quota_twin", task)

    def test_a_pin_never_changes_model_or_breaks_a_cap(self):
        self.key.write_text("k")
        now = core.now_ms()
        quota = {"agent": "claude", "route": "sub", "failure_class": "quota", "end_time_ms": now}
        self.assertEqual(self.pinned([quota], route="sub", settings_overrides={"model": "claude-fable-5-1"})["route"], "sub")
        spent = {"agent": "claude", "route": "api", "end_time_ms": now - 1000, "usage": {"cost_usd": 160.0}}
        self.assertEqual(self.pinned([quota, spent], route="sub")["route"], "sub")

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


    def test_a_workspace_spend_limit_excludes_the_account_until_its_reset(self):
        import fusion_usage as usage
        self.key.write_text("k")
        worker = self.root / "claude-api-fixture"
        message = ("API Error: 400 You have reached your specified workspace API usage limits. "
                   "You will regain access on 2999-01-01 at 00:00 UTC.")
        worker.write_text(f"#!{sys.executable}\nimport json\n"
                          "print(json.dumps({'type':'system','subtype':'init','apiKeySource':'apiKeyHelper'}))\n"
                          f"print(json.dumps({{'type':'result','subtype':'success','is_error':True,'session_id':'s','result':{message!r}}}))\n")
        worker.chmod(0o755)
        self.config["routes"]["api"]["command"] = str(worker)
        task = core.make_task(self.workspace, "claude", "x", "implementation", [], [], None, False, True, route="api")
        result = core.dispatch(self.config, task, self.store)
        self.assertEqual(result["quota"], {"status": "rejected", "windows": {"spend": {"used": 1.0, "resets_at": 32472144000.0}}})
        entry = next(h for h in usage.headroom(self.workspace) if h["account"] == "claude@anthropic-api")
        self.assertEqual(entry["windows"]["spend"]["utilization"], 1.0)
        # Days later the cooldown has long expired, but the account stays out until the stated reset.
        later = core.now_ms() + 3 * 86400 * 1000
        subscription_out = {"agent": "claude", "route": "sub", "failure_class": "quota", "end_time_ms": later}
        rejected = {}
        with patch.object(core, "now_ms", return_value=later), patch("time.time", return_value=later / 1000):
            task = core.make_task(self.workspace, "auto", "Fix it", "implementation", [], [], None, False, True)
            traces = self.store.traces(limit=200) + [subscription_out]
            with patch.object(self.store, "traces", return_value=traces):
                keys = {c["key"] for c in route_candidates(self.config, task, self.store, rejected=rejected)}
        self.assertEqual(keys, set())
        self.assertIn("spend: rejected until", rejected["api"])

    def test_spend_limit_text_is_parsed_only_from_the_api_message(self):
        self.assertIsNone(core.api_spend_limit("rate limit reached, try later"))
        self.assertEqual(core.api_spend_limit("You have reached your specified workspace API usage limits. "
                                              "You will regain access on 2026-10-01 at 00:00 UTC.")["windows"]["spend"]["resets_at"],
                         1790812800.0)


if __name__ == "__main__":
    unittest.main()
