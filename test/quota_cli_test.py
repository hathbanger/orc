"""`fusion quota`: the view, the probe at reset, and quota consumed per run."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_policy as policy
import fusion_quota as quota

NOW = 1790900000
HOUR = 3600


class QuotaCliTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off"})
        env.start()
        self.addCleanup(env.stop)
        self.store = core.RunStore(self.root)
        self.config = core.deep_merge(core.DEFAULTS, {"quota": {"soft": 0.97, "hard": 0.97, "pace_margin": 1.0},
            "decisions": {"mode": "off", "overflow_routes": ["api"]},
            "routes": {"sub": {"agent": "claude", "model": "claude-opus-5-5", "reasoning_effort": "high"},
                       "api": {"agent": "claude", "model": "claude-opus-5-5", "reasoning_effort": "high", "account": "anthropic-api"}}})

    def trace(self, when, windows, status="allowed", cost=None, lane="claude", **extra):
        span = {"agent": "claude", "lane_key": lane, "end_time_ms": when * 1000, "duration_ms": 600000,
                "quota": {"status": status, "windows": windows}, "usage": {"cost_usd": cost} if cost is not None else {}, **extra}
        self.store.root.mkdir(parents=True, exist_ok=True)
        with self.store.traces_path.open("a") as stream:
            stream.write(json.dumps(span) + "\n")

    def test_view_shows_classification_reading_age_and_lanes(self):
        self.trace(NOW - 2 * HOUR, {"seven_day": {"used": 1.0, "resets_at": NOW + HOUR}}, status="rejected")
        view = quota.status(self.config, self.store, now=NOW)
        account = view["accounts"][0]
        self.assertEqual((account["lane_key"], account["classification"], account["reading_age_s"]), ("claude", "exhausted", 2 * HOUR))
        self.assertIn("sub", account["lanes"])
        self.assertNotIn("api", account["lanes"])
        self.assertFalse(account["probe_due"])
        self.assertIn("seven_day", quota.render_status(view))

    def test_probe_is_due_only_after_the_spent_window_resets(self):
        windows = {"five_hour": {"used": 0.05, "resets_at": NOW - HOUR}, "seven_day": {"used": 1.0, "resets_at": NOW + HOUR}}
        self.trace(NOW - 2 * HOUR, windows, status="rejected")
        thresholds = policy.quota_settings(self.config)
        entry = quota.usage.headroom(self.store.workspace, include_raw=False)[0]
        self.assertFalse(quota.probe_due(entry, thresholds, NOW))
        self.assertTrue(quota.probe_due(entry, thresholds, NOW + 2 * HOUR))

    def test_probe_runs_one_tiny_read_on_the_account_itself(self):
        self.trace(NOW - 2 * HOUR, {"seven_day": {"used": 1.0, "resets_at": NOW - HOUR}}, status="rejected")
        self.assertEqual(quota.probe(self.config, self.root, self.store, dry_run=True, now=NOW)["probes"][0]["lane"], "claude")
        tasks = []
        with patch.object(core, "dispatch", side_effect=lambda config, task, store: tasks.append(task) or {"run_id": "r", "status": "success"}):
            view = quota.probe(self.config, self.root, self.store, now=NOW)
        self.assertEqual(view["probes"][0]["status"], "success")
        task = tasks[0]
        self.assertTrue(task["quota_probe"])
        self.assertFalse(task["write"])
        self.assertEqual(task["role"], "quota-probe")

    def test_a_probe_never_moves_to_the_api_twin(self):
        self.trace(NOW - 60, {"seven_day": {"used": 1.0, "resets_at": NOW + HOUR}}, status="rejected")
        task = core.make_task(self.root, "claude", "x", "quota-probe", [], [], None, False, False, route="sub")
        with patch.object(core, "executable", return_value=True), patch("time.time", return_value=NOW):
            self.assertIsNotNone(policy.quota_twin(self.config, task, self.store))
            task["quota_probe"] = True
            self.assertIsNone(policy.quota_twin(self.config, task, self.store))

    def test_nothing_is_due_without_an_exhausted_reset(self):
        self.trace(NOW - 2 * HOUR, {"seven_day": {"used": 0.5, "resets_at": NOW - HOUR}})
        self.assertEqual(quota.probe(self.config, self.root, self.store, dry_run=True, now=NOW)["probes"], [])
        self.assertEqual(len(quota.probe(self.config, self.root, self.store, dry_run=True, stale_hours=1, now=NOW)["probes"]), 1)

    def test_rates_charge_each_rise_to_the_runs_in_between(self):
        reset = NOW + 5 * 86400
        self.trace(NOW - 3 * HOUR, {"seven_day": {"used": 0.50, "resets_at": reset}}, cost=1.0, route="sub")
        self.trace(NOW - 2 * HOUR, {"seven_day": {"used": 0.52, "resets_at": reset}}, cost=4.0, route="sub")
        self.trace(NOW - 1 * HOUR, {"seven_day": {"used": 0.53, "resets_at": reset}}, cost=1.0, route="sub")
        view = quota.rates(self.store, days=1, now=NOW)
        week = view["accounts"][0]["windows"]["seven_day"]
        self.assertEqual((week["intervals"], week["rise"]), (2, 0.03))
        self.assertAlmostEqual(week["per_usd"], 0.03 / 5.0)
        lane = view["accounts"][0]["lanes"]["sub"]
        self.assertEqual((lane["runs"], lane["cost_p90"], lane["minutes_p90"]), (3, 4.0, 10.0))
        self.assertAlmostEqual(lane["p90_run_share"]["seven_day"], round(0.006 * 4.0, 4))
        self.assertIn("a p90 run uses seven_day", quota.render_rates(view))

    def test_a_new_window_is_not_a_rise(self):
        self.trace(NOW - 2 * HOUR, {"seven_day": {"used": 0.9, "resets_at": NOW - HOUR}}, cost=1.0)
        self.trace(NOW - 1 * HOUR, {"seven_day": {"used": 0.1, "resets_at": NOW + 6 * 86400}}, cost=1.0)
        self.assertNotIn("seven_day", quota.rates(self.store, days=1, now=NOW)["accounts"][0]["windows"])

    def test_cli_prints_the_view(self):
        self.trace(NOW - 60, {"seven_day": {"used": 0.4, "resets_at": NOW + HOUR}})
        with patch("sys.stdout") as out:
            self.assertEqual(core.main(["--workspace", str(self.root), "--json", "quota"]), 0)
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(json.loads(printed)["accounts"][0]["lane_key"], "claude")


if __name__ == "__main__":
    unittest.main()
