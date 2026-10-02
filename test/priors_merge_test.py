"""Pooling lane priors from several machines: counts only, route names dropped, models renamable."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_gym as gym
import fusion_policy as policy


def priors(source, entries):
    return {"schema": gym.PRIORS_SCHEMA, "source": source, "generated_at": "2026-10-01T00:00:00Z", "priors": entries}


LAPTOP = priors("gym", {
    "claude-opus-medium": {"agent": "claude", "route": "claude-opus-medium", "model": "claude-opus-5-5", "reasoning_effort": "medium",
                           "gym_lanes": ["claude-opus-medium"],
                           "write": {"attempts": 10, "successes": 8, "mean_cost_usd": 1.0, "mean_seconds": 100.0}}})
STUDIO = priors("gym", {
    "opus-med": {"agent": "claude", "route": "opus-med", "model": "claude-opus-5-5", "reasoning_effort": "medium",
                 "gym_lanes": ["opus-med"],
                 "write": {"attempts": 10, "successes": 4, "mean_cost_usd": 3.0, "mean_seconds": 300.0},
                 "read": {"attempts": 2, "successes": 2, "mean_cost_usd": 0.5, "mean_seconds": 20.0}}})
PRIVATE = priors("gym", {
    "corp-gpt": {"agent": "opencode", "route": "corp-gpt", "model": "corp-proxy/gpt-x", "reasoning_effort": None,
                 "gym_lanes": ["corp-gpt"], "write": {"attempts": 4, "successes": 3, "mean_cost_usd": 0.0, "mean_seconds": 50.0}}})


class PriorsMergeTest(unittest.TestCase):
    def test_counts_add_up_per_lane_and_class_with_weights(self):
        value = gym.merge_priors([(LAPTOP, 1.0, "laptop"), (STUDIO, 0.5, "studio")])
        entry = value["priors"]["claude:claude-opus-5-5:medium"]
        self.assertIsNone(entry["route"])
        self.assertEqual((entry["write"]["attempts"], entry["write"]["successes"]), (15.0, 10.0))
        self.assertAlmostEqual(entry["write"]["mean_cost_usd"], (1.0 * 10 + 3.0 * 5) / 15, places=4)
        self.assertEqual((entry["read"]["attempts"], entry["read"]["successes"]), (1.0, 1.0))
        self.assertEqual([s["input"] for s in entry["write"]["sources"]], ["laptop", "studio"])
        self.assertEqual(value["source"], "merge")
        self.assertNotIn("opus-med", json.dumps(value["priors"]["claude:claude-opus-5-5:medium"]["write"]))

    def test_route_names_never_leave_and_models_can_be_renamed(self):
        value = gym.merge_priors([(PRIVATE, 1.0, "private")], {"corp-proxy/gpt-x": "openai/gpt-x"})
        self.assertEqual(list(value["priors"]), ["opencode:openai/gpt-x"])
        entry = value["priors"]["opencode:openai/gpt-x"]
        self.assertIsNone(entry["route"])
        self.assertNotIn("corp-proxy", json.dumps({k: v for k, v in value.items() if k != "inputs"}))

    def test_merged_file_feeds_routing_priors(self):
        with tempfile.TemporaryDirectory() as d:
            path = gym.write_priors(gym.merge_priors([(LAPTOP, 1.0, "laptop"), (STUDIO, 1.0, "studio")]), Path(d) / "merged.json")
            index = policy.load_priors({"path": str(path), "weight": 0.5, "cap": 10})
            prior = policy.prior_for(index, {"agent": "claude", "route": "anything-local", "model": "claude-opus-5-5",
                                             "reasoning_effort": "medium"}, "write", 0.5, 10)
            self.assertEqual((prior["prior_attempts"], prior["prior_successes"]), (10, 6.0))

    def test_cli_merges_weighted_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.json").write_text(json.dumps(LAPTOP))
            (root / "b.json").write_text(json.dumps(PRIVATE))
            out = root / "merged.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = core.main(["--workspace", str(root), "gym", "priors-merge", str(root / "a.json"), f"{root / 'b.json'}@0.5",
                                  "--rename", "corp-proxy/gpt-x=openai/gpt-x", "--out", str(out)])
            self.assertEqual(code, 0)
            value = json.loads(out.read_text())
            self.assertEqual(value["priors"]["opencode:openai/gpt-x"]["write"]["attempts"], 2.0)
            self.assertEqual([i["weight"] for i in value["inputs"]], [1.0, 0.5])

    def test_cli_merge_prints_by_default_and_leaves_live_priors_alone(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"ORC_HOME": d}):
            root = Path(d)
            (root / "a.json").write_text(json.dumps(LAPTOP))
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                code = core.main(["--workspace", str(root), "gym", "priors-merge", str(root / "a.json")])
            self.assertEqual(code, 0)
            self.assertFalse(gym.default_priors_path().exists())
            self.assertNotIn("wrote", printed.getvalue())

    def test_seed_from_a_leaderboard_is_small_and_names_its_source(self):
        quality = {"source": "board", "fetchedAt": "2026-08-28", "records": [{"slug": "opus", "codingIndex": 75.0}]}
        value = gym.seed_priors(quality, {("claude", "claude-opus-5-5", "high"): "opus", ("codex", "gpt-x", None): "absent"})
        cell = value["priors"]["claude:claude-opus-5-5:high"]["write"]
        self.assertEqual((cell["attempts"], cell["successes"]), (4, 3.0))
        self.assertIn("board#opus.codingIndex", cell["source"])
        self.assertEqual(value["missing"], ["absent"])
        merged = gym.merge_priors([(value, 1.0, "seed"), (LAPTOP, 1.0, "laptop")])
        self.assertIn("claude:claude-opus-5-5:high", merged["priors"])
        with self.assertRaisesRegex(ValueError, "metric"):
            gym.seed_priors(quality, {}, metric="vibes")

    def test_bad_inputs_are_refused(self):
        with self.assertRaisesRegex(ValueError, "weight"):
            gym.merge_priors([(LAPTOP, -1, "x")])
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.json"
            bad.write_text("{}")
            with self.assertRaisesRegex(ValueError, "lane_priors"):
                gym.read_priors_file(bad)


if __name__ == "__main__":
    unittest.main()
