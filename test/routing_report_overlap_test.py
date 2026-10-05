"""routing-report identifies acceptance per policy era and per model family under Thompson sampling (#196)."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fusion_policy import routing_report

T0 = 1_790_000_000_000


def candidates(p_a, p_b):
    # Family F (model m-f) has two lanes: its lead `f-sub` and `f-api`, which Thompson always gives 0.
    return [{"key": "f-sub", "agent": "claude", "model": "m-f", "propensity": p_a},
            {"key": "f-api", "agent": "claude", "model": "m-f", "propensity": 0.0},
            {"key": "g", "agent": "claude", "model": "m-g", "propensity": p_b}]


def events():
    rows = []
    for i in range(5):  # legacy deterministic era: F's lead always chosen, g never
        rows.append({"event": "routing_log", "task_id": f"old{i}", "scope": "automatic", "write": True, "time_ms": T0 + i,
                     "chosen": "f-sub", "candidates": candidates(1.0, 0.0), "policy": {"epsilon": 0.0}})
        rows.append({"event": "outcome", "task_id": f"old{i}", "accepted": True, "source": "lead"})
    for i in range(10):  # Thompson era: F wins 60% of the time; chosen alternates
        chosen = "f-api" if i % 3 == 0 else "f-sub" if i % 2 == 0 else "g"
        cands = candidates(0.6, 0.4)
        rows.append({"event": "routing_log", "task_id": f"new{i}", "scope": "automatic", "write": True, "time_ms": T0 + 1000 + i,
                     "chosen": chosen, "candidates": cands, "policy": {"epsilon": 0.0, "gating_policy": "thompson"},
                     "sampled": {"policy": "thompson", "chosen": chosen,
                                 "families": [{"lead": "f-sub", "p_win": 0.6}, {"lead": "g", "p_win": 0.4}]}})
        rows.append({"event": "outcome", "task_id": f"new{i}", "accepted": i % 2 == 0, "source": "lead"})
    return rows


class RoutingReportOverlapTest(unittest.TestCase):
    def test_family_estimates_are_identified_under_thompson(self):
        report = routing_report(events(), policy="thompson", by="family")
        rows = {row["key"]: row for row in report["lanes"]}
        self.assertEqual(set(rows), {"claude:m-f", "claude:m-g"})
        self.assertEqual(rows["claude:m-f"]["overlap"], "ok")
        self.assertIsInstance(rows["claude:m-f"]["ips_acceptance"], float)
        self.assertEqual(rows["claude:m-f"]["available"], 10)
        self.assertEqual(rows["claude:m-f"]["lanes"], ["f-api", "f-sub"])

    def test_a_lane_that_never_leads_its_family_is_estimated_through_the_family(self):
        report = routing_report(events(), policy="thompson", by="family")
        family = next(row for row in report["lanes"] if row["key"] == "claude:m-f")
        chosen_api = sum(1 for e in events() if e.get("chosen") == "f-api")
        self.assertGreater(chosen_api, 0)
        self.assertGreaterEqual(family["chosen"], chosen_api)
        self.assertIsNotNone(family["snips_acceptance"])

    def test_the_default_call_is_unchanged(self):
        report = routing_report(events())
        self.assertEqual({row["key"] for row in report["lanes"]}, {"f-sub", "f-api", "g"})
        # The pre-existing per-lane semantics: a lane is identified only if it never had propensity 0.
        self.assertEqual({row["key"]: row["overlap"] for row in report["lanes"]},
                         {"f-sub": "ok", "f-api": "insufficient overlap", "g": "insufficient overlap"})
        self.assertEqual(report["logged_choices"], 15)

    def test_since_excludes_earlier_logs(self):
        report = routing_report(events(), since=T0 + 1000)
        self.assertEqual(report["logged_choices"], 10)
        lanes = {row["key"]: row for row in routing_report(events(), since=T0 + 1000, policy="thompson")["lanes"]}
        self.assertEqual(lanes["g"]["overlap"], "ok")

    def test_the_cli_takes_since_policy_and_by(self):
        import contextlib, io, json, os, tempfile
        from unittest.mock import patch
        import fusion_core as core
        from fusion_decisions import DecisionStore
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"ORC_HOME": d, "FUSION_TELEMETRY": "0"}):
            store = DecisionStore(Path(d))
            now = __import__("time").time() * 1000
            for row in events():
                store.append(row["event"], **{**{k: v for k, v in row.items() if k != "event"},
                                              **({"time_ms": now - 1000} if row["event"] == "routing_log" else {})})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = core.main(["--workspace", d, "decisions", "routing-report", "--since", "1h", "--policy", "thompson",
                                  "--by", "family"])
            self.assertEqual(code, 0)
            value = json.loads(out.getvalue())
            self.assertEqual(value["filters"]["by"], "family")
            self.assertEqual(value["logged_choices"], 10)

    def test_an_unknown_policy_or_grouping_is_refused(self):
        with self.assertRaisesRegex(ValueError, "policy"):
            routing_report(events(), policy="greedy")
        with self.assertRaisesRegex(ValueError, "by"):
            routing_report(events(), by="model")


if __name__ == "__main__":
    unittest.main()
