"""`fusion route --explain` previews automatic routing without logging or dispatching (#194)."""
import contextlib
import io
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
import fusion_progress
from fusion_decisions import DecisionStore

WEEK = 7 * 24 * 3600


class RouteExplainTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "CODEX_HOME": str(self.root / "codex"),
                                      "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off"})
        env.start()
        self.addCleanup(env.stop)
        self.store = core.RunStore(self.root)
        self.config = core.deep_merge(core.DEFAULTS, {
            "decisions": {"mode": "off", "priors": False, "rank_by_outcomes": 3, "auto_routes": ["A", "B"]},
            "routes": {"A": {"agent": "claude", "account": "A", "model": "model-a"},
                       "B": {"agent": "claude", "account": "B", "model": "model-b"}}})

    def quota(self, lane, used, reset):
        self.store.root.mkdir(parents=True, exist_ok=True)
        span = {"agent": "claude", "lane_key": lane, "end_time_ms": (time.time() - 60) * 1000,
                "quota": {"status": "allowed", "windows": {"seven_day": {"used": used, "resets_at": reset}}}}
        with self.store.traces_path.open("a") as stream:
            stream.write(json.dumps(span) + "\n")

    def explain(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(core, "load_config", return_value=(self.config, None)), \
                patch.object(core, "executable", return_value=True), \
                patch.object(fusion_progress, "run_logged", side_effect=AssertionError("a worker was started")):
            code = core.main(["--workspace", str(self.root), "--json", "route", "--explain", *argv])
        return code, json.loads(out.getvalue())

    def test_a_lane_past_its_reset_is_a_candidate(self):
        self.quota("claude@A", 1.0, time.time() - 60)
        code, value = self.explain()
        self.assertEqual(code, 0)
        self.assertIn("A", [c["key"] for c in value["candidates"]])

    def test_a_lane_before_its_reset_is_dropped_with_the_reason(self):
        self.quota("claude@A", 1.0, time.time() + WEEK / 2)
        _, value = self.explain()
        self.assertNotIn("A", [c["key"] for c in value["candidates"]])
        self.assertIn("seven_day: used 1.000 exceeds hard 0.970", value["rejected"]["A"])
        self.assertEqual(value["quota"]["A"]["classification"], "exhausted")
        self.assertEqual(value["chosen"], "B")

    def test_nothing_is_logged_or_dispatched(self):
        path = DecisionStore(self.root).path
        before = path.read_bytes() if path.exists() else None
        self.explain("--write", "--role", "implementation")
        self.assertEqual(path.read_bytes() if path.exists() else None, before)
        self.assertEqual(self.store.traces(10), [])

    def test_thompson_chances_are_listed_and_reproducible_with_a_seed(self):
        self.config["decisions"]["gating_policy"] = "thompson"
        _, first = self.explain("--write", "--seed", "7")
        _, again = self.explain("--write", "--seed", "7")
        self.assertEqual(first["chosen"], again["chosen"])
        self.assertEqual(first["sampled"]["chosen"], again["sampled"]["chosen"])
        self.assertAlmostEqual(sum(c["propensity"] for c in first["candidates"]), 1.0, places=6)
        self.assertEqual({f["lead"] for f in first["sampled"]["families"]}, {"A", "B"})

    def test_read_only_work_shows_the_epsilon_it_would_use(self):
        self.config["decisions"]["routing_epsilon"] = 0.2
        _, value = self.explain("--role", "explore")
        self.assertFalse(value["task"]["gating"])
        self.assertEqual(value["policy"]["epsilon"], 0.2)
        self.assertAlmostEqual(sum(c["propensity"] for c in value["candidates"]), 1.0, places=6)

    def test_no_route_explains_why(self):
        self.config["decisions"]["auto_routes"] = ["A"]
        self.quota("claude@A", 1.0, time.time() + WEEK / 2)
        code, value = self.explain()
        self.assertEqual(code, 1)
        self.assertIsNone(value["chosen"])
        self.assertIn("no permitted worker", value["no_route"])


if __name__ == "__main__":
    unittest.main()
