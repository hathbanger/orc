"""Phase 2 evaluation and gating: time-split holdout, per-question baselines, Learn-then-Test thresholds."""
import copy
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_training_loop as loop
from fusion_decisions import (DecisionEngine, DecisionStore, assign_splits, config_for, digest, fit_calibration,
                              heuristic_answers, temperature_scale)
from fusion_laya import dataset_rows
from fusion_quality import baseline_comparison, unbeaten
from fusion_risk import learn_then_test, minimum_acted, p_value, upper_bound

NOUL = {"ok": {"type": "noul", "instructions": "Did it pass?"}}


class LearnThenTestTest(unittest.TestCase):
    def test_certifies_the_widest_coverage_whose_bound_holds(self):
        pairs = [(0.95, True)] * 200 + [(0.85, i >= 30) for i in range(100)] + [(0.6, i >= 50) for i in range(100)]
        gate = learn_then_test(pairs, alpha=0.05, delta=0.1, min_examples=30)
        # Every threshold in (0.85, 0.95] acts on the same 200 error-free answers and
        # is certified; 0.85 adds a 30% error band. The highest of them is published.
        self.assertEqual(gate["threshold"], 0.95)
        self.assertEqual((gate["coverage"], gate["risk"]), (0.5, 0.0))
        self.assertLessEqual(gate["upper_bound"], 0.05)
        at = {point["threshold"]: point for point in gate["curve"]}
        self.assertEqual((at[0.85]["acted"], at[0.85]["errors"]), (300, 30))
        self.assertGreater(at[0.85]["upper_bound"], 0.05)
        self.assertEqual(at[0.5]["coverage"], 1.0)

    def test_refuses_small_or_insufficient_holdouts(self):
        small = learn_then_test([(0.99, True)] * 29, min_examples=30)
        self.assertIsNone(small["threshold"])
        self.assertEqual(small["reason"], "not qualified (n<30)")
        # 40 error-free answers clear min_examples, but zero errors in 40 cannot certify 5% at 90%.
        self.assertEqual(minimum_acted(0.05, 0.1), 45)
        short = learn_then_test([(0.99, True)] * 40, min_examples=30)
        self.assertIsNone(short["threshold"])
        self.assertIn("fewer than 45", short["reason"])
        self.assertEqual(learn_then_test([(0.99, True)] * 45)["threshold"], 0.99)
        noisy = learn_then_test([(0.99, i % 5 != 0) for i in range(200)])
        self.assertIsNone(noisy["threshold"])
        self.assertIn("no threshold certifies", noisy["reason"])

    def test_exact_binomial_bound(self):
        self.assertAlmostEqual(p_value(0, 45, 0.05), 0.95 ** 45)
        self.assertLessEqual(upper_bound(0, 45, 0.1), 0.05)
        self.assertGreater(upper_bound(0, 44, 0.1), 0.05)
        self.assertAlmostEqual(upper_bound(0, 10, 0.1), 1 - 0.1 ** (1 / 10), places=6)
        self.assertEqual(upper_bound(3, 3, 0.1), 1.0)

    def test_certified_thresholds_rarely_exceed_the_risk(self):
        # Confidence c ~ U(0.5, 1) and P(correct | c) = c, so the acted error
        # rate at t is (1 - t) / 2: at most 0.05 exactly when t >= 0.90.
        generator = random.Random(7)
        chosen = []
        for _ in range(25):
            pairs = []
            for _ in range(2000):
                confidence = generator.uniform(0.5, 1)
                pairs.append((confidence, generator.random() < confidence))
            chosen.append(learn_then_test(pairs, alpha=0.05, delta=0.1)["threshold"])
        violations = sum(t is not None and t < 0.90 for t in chosen)
        self.assertLessEqual(violations / len(chosen), 0.1)
        self.assertGreater(sum(t is not None for t in chosen), 12, "the gate should usually certify something")


class TimeSplitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.store = DecisionStore(self.workspace)

    def decision(self, key, group, time_ms, labeled=True):
        self.store.append("decision", id=key, kind="acceptance", mode="shadow", status="ok", truncated=False,
                          state=f"state {key}", questions=NOUL, schema_hash=digest(NOUL), prediction={},
                          context={"group": group}, time_ms=time_ms)
        if labeled:
            self.store.label(key, {"ok": "true"}, "Verified")

    def test_newest_groups_are_held_out_and_no_group_is_split(self):
        for index in range(10):
            self.decision(f"g{index}-a", f"group-{index}", 1000 * (index + 1))
            self.decision(f"g{index}-b", f"group-{index}", 1000 * (index + 1) + 500)
        # A late decision in the oldest group does not move it: groups are dated by their first decision.
        self.decision("g0-late", "group-0", 10 ** 9)
        # An unlabeled newer group dates nothing and is not held out.
        self.decision("unlabeled", "group-new", 10 ** 9, labeled=False)
        exported = self.store.export(self.workspace / "time.jsonl", include_unknown=True)
        rows = dataset_rows(self.workspace / "time.jsonl")
        self.assertEqual(exported["split_method"], "time")
        by_group = {}
        for row in rows:
            by_group.setdefault(row["group"], set()).add(row["split"])
        self.assertTrue(all(len(splits) == 1 for splits in by_group.values()))
        held = sorted(group for group, splits in by_group.items() if splits == {"validation"})
        self.assertEqual(held, ["group-8", "group-9"])
        newest_train = max(r["group_first_ms"] for r in rows if r["split"] == "train")
        oldest_held = min(r["group_first_ms"] for r in rows if r["split"] == "validation")
        self.assertLess(newest_train, oldest_held)
        hashed = self.store.export(self.workspace / "hash.jsonl", split="group-hash", include_unknown=True)
        self.assertEqual(hashed["split_method"], "group-hash")

    def test_split_sizes_keep_both_sides(self):
        for count, held in ((1, 0), (2, 1), (3, 1), (4, 2), (10, 2), (20, 4), (21, 5)):
            splits = assign_splits({f"g{i}": i for i in range(count)})
            self.assertEqual(sum(v == "validation" for v in splits.values()), held, count)
            if held:
                self.assertEqual(splits[f"g{count - 1}"], "validation")
                self.assertEqual(splits["g0"], "train" if count > 1 else "validation")

    def test_config_validates_split_and_risk(self):
        options = config_for({})
        self.assertEqual((options["split"], options["risk"]["alpha"], options["risk"]["min_examples"]), ("time", 0.05, 30))
        self.assertEqual(config_for({"decisions": {"risk": {"alpha": 0.1}}})["risk"]["delta"], 0.1)
        for bad in ({"split": "random"}, {"risk": {"alpha": 1.5}}, {"risk": {"min_examples": 0}}, {"risk": []}):
            with self.assertRaises(ValueError):
                config_for({"decisions": bad})


class BaselineTest(unittest.TestCase):
    def test_heuristic_answers_come_from_the_policy_not_from_laya(self):
        intake = {"kind": "intake", "questions": {"workflow": {"type": "choice", "criteria": {"build": "", "debug": ""}},
                                                  "needs_clarification": {"type": "noul"}}}
        self.assertEqual(heuristic_answers(intake, {"actual": "debug", "applied": False}),
                         {"workflow": "debug", "needs_clarification": "false"})
        self.assertEqual(heuristic_answers(intake, {"actual": "debug", "applied": True}), {"needs_clarification": "false"})
        self.assertEqual(heuristic_answers(intake, {"actual": "debug", "applied": False, "explicit_kind": "debug"}),
                         {"needs_clarification": "false"})
        acceptance = {"kind": "acceptance", "questions": {"plausible": {"type": "noul"}, "failed_task": {"type": "noul"}}}
        self.assertEqual(heuristic_answers(acceptance), {"plausible": "true", "failed_task": "false"})

    def rows(self):
        rows = []
        for index in range(12):
            split = "train" if index < 6 else "validation"
            label = "true" if index % 3 else "false"
            rows.append({"id": str(index), "group": str(index), "split": split, "kind": "acceptance",
                         "questions": NOUL, "labels": {"ok": label}, "heuristic": {"ok": label if index != 6 else "true"}})
        return rows

    def test_candidate_must_beat_every_applicable_baseline(self):
        rows = self.rows()
        predictions = {row["id"]: {"ok": {"true": 1.0, "false": 0.0} if row["labels"]["ok"] == "true" else {"true": 0.0, "false": 1.0}}
                       for row in rows}
        controls = {row["id"]: {"ok": {"true": 0.9, "false": 0.1}} for row in rows if row["split"] == "validation"}
        report = baseline_comparison(rows, predictions, controls)
        question = report["by_question"]["acceptance:ok"]
        self.assertEqual((question["n"], question["groups"], question["accuracy"]), (6, 6, 1.0))
        self.assertAlmostEqual(report["baselines"]["majority"]["accuracy"], 4 / 6)
        self.assertAlmostEqual(report["baselines"]["heuristic"]["accuracy"], 5 / 6)
        self.assertAlmostEqual(report["baselines"]["control"]["accuracy"], 4 / 6)
        self.assertEqual(unbeaten(report["baselines"], 0.02), [])
        self.assertEqual(unbeaten(report["baselines"], 0.2), ["heuristic"])

    def round(self, **evaluate):
        base = {"model_identities": ["source"], "accuracy": 0.6, "benchmark_hash": "b", "holdout": {"status": "checked"},
                "validation_questions": 40, "validation_groups": 12}
        candidate = {**base, "model_identities": ["candidate"], "accuracy": 0.8, **evaluate}
        calibration = {"buckets": {"acceptance:abc:ok": {"validation": 12, "qualified": False, "status": "not qualified (n<30)",
                                                        "risk": {"threshold": None, "alpha": 0.05, "delta": 0.1}}}}
        return {"jobs": {"train": "t", "baseline": "b", "evaluate": "e"},
                "results": {"train": {"model_identity": "candidate", "source_identity": "source"},
                            "baseline": base, "evaluate": candidate, "calibrate": calibration}}

    def test_proof_is_flat_unless_every_baseline_is_beaten_by_the_margin(self):
        beaten = {"majority": {"n": 40, "accuracy": 0.5, "candidate_accuracy": 0.8, "margin": 0.3},
                  "heuristic": {"n": 40, "accuracy": 0.79, "candidate_accuracy": 0.8, "margin": 0.01}}
        proof = loop.proof(self.round(baselines=beaten, by_question={"acceptance:ok": {"n": 40, "groups": 12, "accuracy": 0.8,
                                                                                         "baselines": beaten}}))
        self.assertEqual(proof["outcome"], "flat")
        self.assertTrue(any("deterministic-policy baseline" in note for note in proof["notes"]))
        line = loop.question_lines(proof["questions"])["acceptance:ok"]
        self.assertEqual((line["holdout_n"], line["candidate"], line["heuristic"]), (40, 0.8, 0.79))
        self.assertEqual(line["gate"], ["not qualified (n<30)"])
        beaten["heuristic"].update(accuracy=0.7, margin=0.1)
        self.assertEqual(loop.proof(self.round(baselines=beaten))["outcome"], "gain")
        self.assertEqual(loop.proof(self.round(baselines=beaten, accuracy=0.61))["outcome"], "flat")
        self.assertEqual(loop.proof(self.round(baselines=beaten, accuracy=0.5))["outcome"], "regression")
        # An evaluation without per-question baselines still compares against majority and control.
        legacy = loop.proof(self.round(majority_accuracy=0.9, control_accuracy=0.5))
        self.assertEqual(legacy["outcome"], "flat")
        self.assertIn("majority", legacy["baselines"])


class GatingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    def dataset(self, validation, train=60):
        questions = {"action": {"type": "choice", "instructions": "Next?", "criteria": {"repair": "repair", "stop": "stop"}}}
        rows = []
        for index in range(train + len(validation)):
            held = index >= train
            confidence, correct = validation[index - train] if held else (0.9, True)
            label = "repair" if correct else "stop"
            rows.append({"schema": "fusion.training.v1", "id": str(index), "group": str(index),
                         "split": "validation" if held else "train", "kind": "recovery", "state": "s",
                         "questions": questions, "schema_hash": digest(questions), "labels": {"action": label},
                         "prediction": {"action": {"repair": confidence, "stop": 1 - confidence}}, "model_identity": "m"})
        path = self.workspace / f"data-{len(validation)}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path, questions

    def test_calibration_publishes_the_gate_and_allowed_uses_it(self):
        validation = [(0.97, True)] * 60 + [(0.7, i % 2 == 0) for i in range(40)]
        path, questions = self.dataset(validation)
        report = fit_calibration(path, self.workspace / "calibration.json", risk={"min_groups": 5})
        bucket = next(iter(report["buckets"].values()))
        self.assertTrue(bucket["qualified"], bucket["status"])
        self.assertEqual(report["risk"]["alpha"], 0.05)
        threshold = bucket["risk"]["threshold"]
        self.assertIsNotNone(threshold)
        self.assertEqual(bucket["threshold"], threshold)
        self.assertEqual(bucket["risk"]["n"], 100)
        self.assertAlmostEqual(bucket["risk"]["coverage"], 0.6)
        self.assertTrue(bucket["risk"]["curve"])
        engine = DecisionEngine(self.workspace, {"decisions": {"mode": "active", "auto_actions": ["recovery"],
                                                               "calibration_file": "calibration.json", "threshold": 0.99}})
        record = {"kind": "recovery", "status": "ok", "schema_hash": digest(questions), "model_identity": "m",
                  "prediction": {"action": {"repair": 0.97, "stop": 0.03}}}
        # The certified threshold replaces the fixed 0.99; temperature scaling still applies.
        scaled = max(temperature_scale({"repair": 0.97, "stop": 0.03}, bucket["temperature"]).values())
        self.assertEqual(engine.allowed(record, "action"), scaled >= threshold)
        self.assertTrue(engine.allowed(record, "action"))
        low = copy.deepcopy(record)
        low["prediction"]["action"] = {"repair": 0.7, "stop": 0.3}
        self.assertFalse(engine.allowed(low, "action"))

    def test_role_buckets_require_independent_qualified_groups(self):
        path, questions = self.dataset([(0.97, True)] * 90)
        rows = dataset_rows(path)
        base = f"recovery:{digest(questions)}"
        # A qualified role, a tiny holdout, and many answers from only one group.
        for row in rows:
            n = int(row["id"])
            row["role"] = " TRIAGE  Interpret " if n < 60 or n < 120 else "triage-locate" if n < 125 else "implementer"
            if row["role"] == "implementer":
                row["group"] = "shared-validation"
        # Supply train evidence for all roles; only held-out group support differs.
        for role in ("triage-locate", "implementer"):
            rows += [{**row, "id": f"{role}-{row['id']}", "role": role} for row in rows[:60]]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        report = fit_calibration(path, self.workspace / "roles.json", risk={"min_groups": 5})
        self.assertEqual(set(report["buckets"]), {f"{base}:action", f"{base}:triage-interpret:action"})
        bucket = report["buckets"][f"{base}:triage-interpret:action"]
        self.assertTrue(bucket["qualified"])
        self.assertEqual((bucket["train_groups"], bucket["confident_validation_groups"]), (60, 60))
        # Enough answers from too few held-out groups must not qualify a role.
        for row in rows:
            if row["split"] == "validation" and row["role"].strip().startswith("TRIAGE"):
                row["group"] = "one-held-out-group"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        report = fit_calibration(path, self.workspace / "grouped.json", risk={"min_groups": 5})
        self.assertNotIn(f"{base}:triage-interpret:action", report["buckets"])
        # Train groups are also required even with enough independent holdouts.
        for row in rows:
            if row["role"].strip().startswith("TRIAGE"):
                row["group"] = "one-train-group" if row["split"] == "train" else row["id"]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        report = fit_calibration(path, self.workspace / "train-grouped.json", risk={"min_groups": 5})
        self.assertNotIn(f"{base}:triage-interpret:action", report["buckets"])

    def test_decide_and_allowed_prefer_role_bucket_with_legacy_fallback(self):
        questions = {"action": {"type": "choice", "instructions": "Next?", "criteria": {"repair": "repair", "stop": "stop"}}}
        class Backend:
            def predict(self, state, questions):
                return {"answers": {"action": {"probabilities": {"repair": .9, "stop": .1}}}, "model_identity": "m"}
        base = f"recovery:{digest(questions)}"
        report = {"model_identity": "m", "buckets": {
            f"{base}:action": {"temperature": 1, "threshold": .85, "qualified": True},
            f"{base}:triage-interpret:action": {"temperature": 4, "threshold": .85, "qualified": True}}}
        engine = DecisionEngine(self.workspace, {"decisions": {"mode": "active", "threshold": .85,
                                                               "auto_actions": ["recovery"]}}, Backend())
        with patch.object(engine, "calibration", return_value=report):
            for role in (None, "unknown", "triage-locate", " Triage\tInterpret "):
                record = engine.decide("recovery", {"role": role}, questions, {"group": str(role)})
                self.assertEqual(record["status"], "ok")
                specific = role == " Triage\tInterpret "
                expected = temperature_scale({"repair": .9, "stop": .1}, 4 if specific else 1)["repair"]
                self.assertAlmostEqual(record["recommendations"]["action"]["probability"], expected)
                self.assertEqual(engine.allowed(record, "action"), not specific)
                if specific:
                    self.assertEqual(record["context"]["role"], "triage-interpret")
                    engine.store.label(record["id"], {"action": "repair"}, "Reviewed")
            exported = self.workspace / "export.jsonl"
            engine.store.export(exported, include_unknown=True)
            self.assertEqual(dataset_rows(exported)[0]["role"], "triage-interpret")
            # allowed re-reads the current role calibration, just as for legacy buckets.
            report["buckets"][f"{base}:triage-interpret:action"]["temperature"] = 1
            self.assertTrue(engine.allowed(record, "action"))
            report["model_identity"] = "other-model"
            self.assertFalse(engine.allowed(record, "action"))
        context = {"role": " IMPLEMENTER "}
        unscored = engine.record_unscored("recovery", {}, questions, context)
        self.assertEqual(unscored["context"]["role"], "implementer")
        self.assertEqual(context["role"], " IMPLEMENTER ")

    def test_small_holdout_is_never_qualified(self):
        path, questions = self.dataset([(0.99, True)] * 20)
        report = fit_calibration(path, self.workspace / "small.json", risk={"min_groups": 1})
        bucket = next(iter(report["buckets"].values()))
        self.assertFalse(bucket["qualified"])
        self.assertEqual(bucket["status"], "not qualified (n<30)")
        self.assertIsNone(bucket["risk"]["threshold"])
        engine = DecisionEngine(self.workspace, {"decisions": {"mode": "active", "auto_actions": ["recovery"],
                                                               "calibration_file": "small.json"}})
        record = {"kind": "recovery", "status": "ok", "schema_hash": digest(questions), "model_identity": "m",
                  "prediction": {"action": {"repair": 0.999, "stop": 0.001}}}
        self.assertFalse(engine.allowed(record, "action"))


if __name__ == "__main__":
    unittest.main()
