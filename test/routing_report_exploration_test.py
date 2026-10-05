"""routing-report counts every kind of exploration and says why epsilon does not apply to gating work (#193)."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fusion_policy import routing_report


def log(task_id, write, role="implementation", explored=False, sampled=None, trial=None, epsilon=0.0, routing_epsilon=0.2):
    chosen = "a"
    candidates = [{"key": "a", "agent": "claude", "model": "m-a", "propensity": (sampled or {}).get("a", 1.0)},
                  {"key": "b", "agent": "claude", "model": "m-b", "propensity": (sampled or {}).get("b", 0.0)}]
    row = {"event": "routing_log", "task_id": task_id, "scope": "automatic", "write": write, "role": role, "chosen": chosen,
           "explored": explored, "candidates": candidates,
           "policy": {"epsilon": epsilon, "routing_epsilon": routing_epsilon,
                      **({"gating_policy": "thompson"} if sampled else {})}}
    if sampled:
        row["sampled"] = {"policy": "thompson", "chosen": chosen, "families": [
            {"lead": key, "p_win": p} for key, p in sampled.items()]}
    if trial:
        row["write_trial"] = trial
    return row


def with_outcomes(*logs):
    return [event for row in logs for event in (row, {"event": "outcome", "task_id": row["task_id"], "accepted": True,
                                                      "source": "lead"})]


class RoutingReportExplorationTest(unittest.TestCase):
    def test_epsilon_thompson_and_trials_are_all_counted(self):
        report = routing_report([log("r1", False, explored=True, epsilon=0.2), log("r2", True, sampled={"a": .4, "b": .6}),
                                 log("r3", True), log("r4", True, trial={"route": "a", "checked_runs": 1, "minimum": 3})])
        self.assertEqual(report["exploration"], {"epsilon": 1, "thompson": 1, "write_trial": 1})
        self.assertEqual(report["explored"], 3)

    def test_a_sure_thompson_pick_is_not_exploration(self):
        report = routing_report([log("r1", True, sampled={"a": 1.0})])
        self.assertEqual(report["exploration"]["thompson"], 0)

    def test_gating_work_explains_itself_instead_of_asking_for_epsilon(self):
        report = routing_report(with_outcomes(log("r1", True), log("r2", True, role="review"), log("r3", True)))
        self.assertEqual(report["epsilon_ineligible"], 3)
        text = " ".join(report["warnings"])
        self.assertIn("gating", text)
        self.assertNotIn("Set decisions.routing_epsilon above 0", text)

    def test_read_work_without_epsilon_still_gets_the_epsilon_advice(self):
        report = routing_report(with_outcomes(log("r1", False, routing_epsilon=0.0)))
        self.assertEqual(report["epsilon_ineligible"], 0)
        self.assertIn("routing_epsilon", " ".join(report["warnings"]))


if __name__ == "__main__":
    unittest.main()
