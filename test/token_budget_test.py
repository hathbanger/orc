import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_decisions
from fusion_decisions import (ACCEPTANCE_QUESTIONS, STATE_HEAD_TOKENS, DecisionEngine, DecisionStore, acceptance_state,
                              estimated_tokens, labelable_record, read_jsonl, reviewed_labels, state_tokens)
from fusion_learning import decision_rows
from fusion_training_loop import readiness, tokens

FIXTURE = json.loads((Path(__file__).parent / "fixtures/laya_token_counts.json").read_text())
BUDGET = state_tokens("acceptance")


def encoded(state):
    return json.dumps(state, ensure_ascii=False, sort_keys=True)


def no_runtime(options):
    raise AssertionError("a lead verdict must never start the Laya runtime")


class TokenEstimateTest(unittest.TestCase):
    """Counts in the fixture come from Laya's tokenizer (test/laya_token_budget.py --write)."""

    def test_the_estimate_is_never_below_a_measured_count(self):
        for entry in FIXTURE["samples"] + FIXTURE["acceptance_states"]:
            text = entry.get("text", entry.get("state"))
            with self.subTest(entry["name"]):
                self.assertGreaterEqual(estimated_tokens(text), entry["tokens"])

    def test_the_state_budget_is_what_the_model_leaves_beside_the_acceptance_questions(self):
        self.assertEqual(FIXTURE["head_tokens"], {"acceptance": STATE_HEAD_TOKENS["acceptance"]})
        self.assertEqual(BUDGET, FIXTURE["max_len"] - FIXTURE["head_tokens"]["acceptance"])
        self.assertEqual(state_tokens("unmeasured"), 512 - 195)

    def test_measured_acceptance_inputs_are_rebuilt_exactly_and_fit_the_model(self):
        labelable = 0
        for entry in FIXTURE["acceptance_states"]:
            with self.subTest(entry["name"]):
                state = acceptance_state(entry["source"], entry["result"], 2200)
                self.assertEqual(encoded(state), entry["state"], "acceptance_state changed; rerun test/laya_token_budget.py --write")
                self.assertEqual(bool(state.get("source_truncated")), entry["source_truncated"])
                if not entry["source_truncated"]:
                    labelable += 1
                    self.assertFalse(entry["model_truncated"])
                    self.assertLessEqual(entry["tokens"], BUDGET)
        self.assertGreaterEqual(labelable, 4)


class AcceptanceBudgetTest(unittest.TestCase):
    def node(self, detail_words=400):
        headline = "Fixes https://github.com/hathbanger/orc/issues/46 — Woodland can show the previous survey"
        request = headline + "\nSelected from Truffle pig hunt.\n" + "evidence " * detail_words
        return headline, {"role": "implementation", "decision_context": {"request": request, "workflow_kind": "debug", "stage": "implement"}}

    def test_inputs_at_the_default_cap_stay_within_the_estimated_token_budget(self):
        headline, node = self.node()
        summaries = {"prose": "Minted the survey id before launch and navigated to it. " * 60,
                     "hex": " ".join(hashlib.sha1(str(i).encode()).hexdigest() for i in range(20)),
                     "json": json.dumps([{"path": f"src/m_{i}.py", "line": i} for i in range(80)]),
                     "short": "Fixed it."}
        lists = {"none": ([], []), "some": (["fusion_ui.py", "test/ui_test.py"], ["python3 test/run_python.py: 409 passed"]),
                 "full": ([f"src/components/module_{i}/index.tsx" for i in range(30)], [f"npm test -- --grep case_{i}" for i in range(12)])}
        for (sname, summary), (lname, (changed, tests)) in ((s, l) for s in summaries.items() for l in lists.items()):
            with self.subTest(summary=sname, lists=lname):
                state = acceptance_state(node, {"summary": summary, "changed": changed, "tests": tests}, 2200)
                if state.get("source_truncated"):
                    self.assertIn((sname, lname), {("json", "full"), ("hex", "full")})
                    self.assertTrue(state["task"]["request"].startswith(headline + "\n"))
                    continue
                self.assertLessEqual(estimated_tokens(encoded(state)), BUDGET)
                self.assertLessEqual(len(encoded(state)), 2200)
                self.assertTrue(state["task"]["request"].startswith(headline + "\n"))
                self.assertIn("[…truncated", state["task"]["request"])
                if len(summary) > len(state["summary"]):
                    self.assertTrue(state["summary"].startswith(summary[:300]))
                    self.assertIn("[…truncated", state["summary"])

    def test_lists_give_up_room_before_an_input_is_left_unlabeled(self):
        _, node = self.node()
        tests = ["PYTHONDONTWRITEBYTECODE=1 python3 test/run_python.py " + "-k test_verdict " * 20] * 8
        state = acceptance_state(node, {"summary": "Minted the survey id. " * 40, "changed": [f"src/m_{i}.py" for i in range(20)],
                                        "tests": tests}, 2200)
        self.assertNotIn("source_truncated", state)
        self.assertLess(len(state["tests"]), 9)
        self.assertTrue(state["tests"][-1].startswith("[…") and state["changed"][-1].startswith("[…"))
        self.assertLessEqual(estimated_tokens(encoded(state)), BUDGET)

    def test_a_criterion_over_the_token_budget_is_never_cut(self):
        brief = "Keep every requirement: " + "must quote commas; " * 60
        self.assertLess(len(brief), 1400)
        state = acceptance_state({"task": brief}, {"summary": "Added CSV export. " * 40}, 2200)
        self.assertTrue(state["source_truncated"])
        self.assertEqual(state["task"], brief)

    def test_a_state_within_the_character_cap_but_over_the_token_budget_is_cut_further(self):
        summary = " ".join(hashlib.sha1(str(i).encode()).hexdigest() for i in range(40))
        within_chars = acceptance_state({"task": "Record the build hashes."}, {"summary": summary}, 2200, tokens=10_000)
        self.assertGreater(estimated_tokens(encoded(within_chars)), BUDGET)
        state = acceptance_state({"task": "Record the build hashes."}, {"summary": summary}, 2200)
        self.assertLessEqual(estimated_tokens(encoded(state)), BUDGET)
        self.assertLess(len(state["summary"]), len(within_chars["summary"]))


class LegacyOverBudgetLabelsTest(unittest.TestCase):
    """Verdict labels recorded when acceptance inputs were bounded to 2200 characters, not tokens."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ["ORC_HOME"] = str(self.workspace / "orc-home")
        os.environ.pop("FUSION_DECISIONS_MODE", None)
        config = self.workspace / "fusion-config.json"
        config.write_text(json.dumps({"decisions": {"mode": "shadow"}}))
        os.environ["FUSION_CONFIG"] = str(config)
        runtime = patch.object(fusion_decisions, "runtime_for", no_runtime)
        runtime.start()
        self.addCleanup(runtime.stop)
        self.store = DecisionStore(self.workspace)
        self.run_id = "20260924-192016-0f040af8"
        run = core.RunStore(self.workspace).runs / self.run_id
        run.mkdir(parents=True)
        self.task = {"run_id": self.run_id, "task": "Fix the survey id race in Woodland", "parent_task_id": "wf-legacy"}
        self.result = {"status": "success", "summary": "Minted the survey id before launch and navigated to it. " * 60,
                       "changed": ["fusion_ui.py"], "tests": ["python3 test/run_python.py: 409 passed"]}
        (run / "task.json").write_text(json.dumps(self.task))
        (run / "result.json").write_text(json.dumps(self.result))
        legacy = acceptance_state(self.task, self.result, 2200, tokens=10_000)
        self.legacy_state = encoded(legacy)
        self.assertGreater(len(self.legacy_state), 2000)
        self.assertGreater(estimated_tokens(self.legacy_state), BUDGET)
        self.legacy = {"id": "legacy", "kind": "acceptance", "mode": "shadow", "status": "unscored", "truncated": False,
                       "state": self.legacy_state, "questions": ACCEPTANCE_QUESTIONS, "schema_hash": "h", "prediction": {},
                       "recommendations": {}, "context": {"task_id": self.run_id, "group": "wf-legacy"}, "source": "lead_verdict"}
        self.store.append("decision", **self.legacy)
        self.store.append("label", id="legacy", answers={"plausible": "true", "failed_task": "false"}, evidence="Lead accepted",
                          verified=True, replace=True, source="lead_verdict", reviewers=[{"agent": "lead"}])

    def test_an_over_budget_unscored_input_is_not_labelable_and_is_skipped_by_export(self):
        self.assertFalse(labelable_record(self.legacy))
        [row] = decision_rows(self.workspace)
        self.assertEqual(row["garden_state"], "ineligible")
        self.assertEqual((tokens([row]), readiness([row])), ({}, {"train": 0, "validation": 0}))
        exported = self.store.export(self.workspace / "legacy.jsonl", include_unknown=True)
        self.assertEqual((exported["examples"], exported["skipped_over_token_budget"]), (0, 1))
        with self.assertRaisesRegex(ValueError, "label only successful, complete model inputs"):
            self.store.label("legacy", {"plausible": "true"}, "Human check")

    def test_a_new_verdict_retracts_the_legacy_label_and_labels_a_bounded_input(self):
        payload = core.record_outcome(self.workspace, self.run_id, True, "Read the diff; the id is minted before launch")
        label = payload["label"]
        self.assertEqual(label["status"], "labeled", label)
        self.assertNotEqual(label["decision_id"], "legacy")
        answers, _ = reviewed_labels(read_jsonl(self.store.path))
        self.assertFalse(answers.get("legacy"))
        self.assertEqual(answers[label["decision_id"]], {"plausible": "true", "failed_task": "false"})
        fresh = self.store.get(label["decision_id"])
        self.assertEqual(fresh["state"], encoded(acceptance_state(self.task, self.result, 2200)))
        self.assertLessEqual(estimated_tokens(fresh["state"]), BUDGET)
        self.assertTrue(labelable_record(fresh))
        exported = self.store.export(self.workspace / "repaired.jsonl", include_unknown=True)
        self.assertEqual((exported["examples"], exported["skipped_over_token_budget"]), (1, 0))
        core.record_outcome(self.workspace, self.run_id, False, "Second look: the race remains")
        self.assertEqual(len([e for e in read_jsonl(self.store.path) if e.get("event") == "decision"]), 2)

    def test_record_unscored_flags_an_input_the_model_would_truncate(self):
        engine = DecisionEngine(self.workspace, {"decisions": {"mode": "shadow"}})
        record = engine.record_unscored("acceptance", None, ACCEPTANCE_QUESTIONS, {"task_id": "x"}, encoded=self.legacy_state)
        self.assertTrue(record["truncated"])
        small = engine.record_unscored("acceptance", {"task": "t", "summary": "s", "changed": [], "tests": []}, ACCEPTANCE_QUESTIONS)
        self.assertFalse(small["truncated"])


if __name__ == "__main__":
    unittest.main()
