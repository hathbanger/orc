"""Declared per-route prices fill in a cost the worker did not report, and never override one."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core

PRICES = {"price_per_mtok": {"input": 1.0, "output": 4.0, "cache_read": 0.1}}


class PriceFallbackTest(unittest.TestCase):
    def test_tokens_without_cost_are_priced(self):
        usage = {"input_tokens": 2_000_000, "output_tokens": 500_000, "reasoning_output_tokens": 250_000,
                 "cache_read_input_tokens": 1_000_000}
        self.assertAlmostEqual(core.price_fallback(PRICES, usage), 2.0 + 3.0 + 0.1)

    def test_reported_cost_wins_and_no_table_changes_nothing(self):
        self.assertIsNone(core.price_fallback(PRICES, {"input_tokens": 10, "cost_usd": 0.5}))
        self.assertIsNone(core.price_fallback({}, {"input_tokens": 10}))
        self.assertIsNone(core.price_fallback(PRICES, {}))

    def test_an_unpriced_token_kind_keeps_the_cost_unknown(self):
        self.assertIsNone(core.price_fallback(PRICES, {"input_tokens": 10, "cache_creation_input_tokens": 5}))

    def test_malformed_tables_are_refused(self):
        for value in ({}, {"input": -1}, {"input": True}, {"inputs": 1.0}, [1, 2], "cheap"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "price_per_mtok"):
                core.price_per_mtok({"price_per_mtok": value})

    def test_dispatch_records_an_estimated_cost(self):
        events = [{"type": "text", "sessionID": "s", "part": {"type": "text", "text": "STATUS: success\nSUMMARY: ok"}},
                  {"type": "step_finish", "sessionID": "s", "part": {"reason": "stop", "tokens": {"input": 1000, "output": 500},
                                                                     "cost": 0}}]
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "off", "FUSION_TELEMETRY": "0"}):
            root = Path(d)
            worker = root / "opencode-fixture"
            worker.write_text(f"#!{sys.executable}\nimport json\nfor e in {events!r}:\n    print(json.dumps(e))\n")
            worker.chmod(0o755)
            config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"}, "opencode": {"command": str(worker)},
                                                     "routes": {"oc": {"agent": "opencode", "model": "x/y", **PRICES}}})
            task = core.make_task(root, "opencode", "Review", "reviewer", [], [], None, False, False, route="oc")
            result = core.dispatch(config, task, core.RunStore(root))
        self.assertEqual(result["status"], "success", result)
        self.assertAlmostEqual(result["usage"]["cost_usd"], 0.001 + 0.002)
        self.assertTrue(result["usage"]["cost_estimated"])

    def test_a_malformed_table_fails_before_the_worker_runs(self):
        config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off"},
                                                 "routes": {"oc": {"agent": "opencode", "model": "x/y", "price_per_mtok": {"input": -1}}}})
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "off", "FUSION_TELEMETRY": "0"}):
            task = core.make_task(Path(d), "opencode", "Review", "reviewer", [], [], None, False, False, route="oc")
            with self.assertRaisesRegex(ValueError, "price_per_mtok"):
                core.dispatch(config, task, core.RunStore(Path(d)))


if __name__ == "__main__":
    unittest.main()
