"""Seeded evidence interpretation and read-only execution, without live models."""
import contextlib
import io
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fusion_core as core
import fusion_gym as gym
import fusion_gym_interpret as interp
import fusion_policy as policy
from fusion_decisions import DecisionStore, read_jsonl
from fusion_workflow import validate_spec
from gym_test import Isolated, run_git, BUGGY, FIXED, TEST_BASE, TEST_FIX


class InterpretTest(Isolated):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "source"
        (self.repo / "test").mkdir(parents=True)
        (self.repo / "calc.py").write_text(BUGGY)
        (self.repo / "test/calc_test.py").write_text(TEST_BASE)
        run_git(self.repo, "init", "-q")
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "seed")
        (self.repo / "calc.py").write_text(FIXED)
        (self.repo / "test/calc_test.py").write_text(TEST_FIX)
        run_git(self.repo, "commit", "-qam", "fix(calc): keep sign (#7)")
        self.config = {"routes": {}, "timeout_seconds": 60, "decisions": {"mode": "shadow"}}
        (self.repo / ".fusion.json").write_text(json.dumps(self.config))
        self.tasks = self.root / "tasks"
        self.gym_dir = self.root / "gym"
        _, [self.source] = gym.extract(self.repo, [7], self.tasks, use_gh=False)
        self.output = self.root / "interpret-tasks"

    def seed(self, seed=37, period="2026-W39", split="all"):
        return interp.extract(self.tasks, self.output, seed, period, split=split)[1][0]

    def test_deterministic_real_material_and_all_templates(self):
        task = self.seed()
        first = {p.name: p.read_bytes() for p in self.output.glob("*.json")}
        self.assertEqual(task, self.seed())
        self.assertEqual(first, {p.name: p.read_bytes() for p in self.output.glob("*.json")})
        self.assertNotEqual(task["id"], self.seed(seed=38)["id"])
        self.assertEqual(len(task["interpretation"]), 12)
        self.assertEqual({v["template"] for v in task["interpretation"].values()},
                         {t["id"] for t in interp.load_trap_templates()})
        bundle = task["bundle"]
        self.assertEqual(json.loads(bundle["evidence/source/checks.json"]), self.source["checks"])
        self.assertIn("-    return abs(a) + b", bundle["evidence/source/change.patch"])
        receipts = json.loads(bundle["evidence/source/receipts.json"])
        self.assertTrue(receipts["fix"])
        for qid, gold in task["interpretation"].items():
            case = {Path(p).name: v for p, v in bundle.items() if p.startswith(f"evidence/{qid}/")}
            if not gold["trap"]:
                self.assertNotEqual(gold["answer"], "no")
                continue
            if gold["template"] == "stale-verify-head":
                self.assertNotEqual(json.loads(case["verify.json"])["head"], json.loads(case["pr.json"])["head"])
            elif gold["template"] == "off-by-one-tests":
                n = int(re.search(r"Ran (\d+) tests", case["test.log"])[1])
                self.assertIn(f"Ran {n+1} tests", case["summary.md"])
                self.assertEqual(str(n), gold["answer"])
            elif gold["template"] == "laptop-73-behind":
                self.assertEqual(json.loads(case["checkout.json"])["commits_behind_origin"], 73)
                self.assertIn("current", case["summary.md"])
            elif gold["template"] == "throughput-32-as-15":
                self.assertEqual(gold["answer"], "32%")
                self.assertIn("15%", case["summary.md"])
            else:
                self.assertEqual(json.loads(case["receipt.json"])["exit_code"], 1)
                self.assertEqual(json.loads(case["receipt.json"])["status"], "failed")
                self.assertIn("OK", case["output.log"])
        spec = gym.build_interpret_spec(task, {"agent": "codex"})
        validate_spec(spec)
        self.assertFalse(spec["nodes"][0]["write"])
        public = json.dumps({"bundle": bundle, "spec": spec})
        for private in ("holdout", '"gold"', "stale-verify-head", "laptop-73-behind"):
            self.assertNotIn(private, public)

    def test_split_rotates_and_controls_follow_template(self):
        templates = interp.load_trap_templates()
        splits = [interp.split_traps(templates, 37, f"2026-W{week:02}") for week in range(1, 10)]
        self.assertGreater(len({json.dumps(s, sort_keys=True) for s in splits}), 1)
        for split in splits:
            self.assertEqual(list(split.values()).count("holdout"), 2)
            self.assertEqual(list(split.values()).count("train"), 4)
        train = interp.build_interpret_task(self.source, 37, "2026-W39", split="train")
        hold = interp.build_interpret_task(self.source, 37, "2026-W39", split="holdout")
        self.assertFalse({g["template"] for g in train["interpretation"].values()} &
                         {g["template"] for g in hold["interpretation"].values()})
        self.assertEqual({g["split"] for g in hold["interpretation"].values()}, {"holdout"})
        new = {**templates[0], "id": "new-observation", "source": "new incident"}
        extra = interp.build_interpret_task(self.source, templates=templates + [new])
        self.assertEqual(len(extra["interpretation"]), 14)

    def test_exact_grading_abstentions_controls_and_invalid(self):
        task = self.seed()
        truth = task["interpretation"]
        correct = {qid: g["answer"] for qid, g in truth.items()}
        self.assertEqual(interp.grade(correct, truth)["score"], 1)
        self.assertEqual(interp.grade({}, truth)["score"], .5)
        self.assertEqual(interp.grade({}, truth)["verdict"], "abstained")
        no = interp.grade(dict.fromkeys(truth, "no"), truth)
        self.assertLess(no["score"], .5)
        self.assertEqual(no["verdict"], "misread")
        one = next(iter(truth))
        mixed = {**correct, one: "a confidently wrong answer"}
        self.assertEqual(interp.grade(mixed, truth)["misread"], 1)
        self.assertEqual(interp.grade({**correct, one: None}, truth)["abstained"], 1)
        self.assertEqual(interp.grade(correct, truth, invalid=True)["score"], 0)
        for raw, normalized in [(True, "yes"), (" FALSE ", "no"), ("32%", 32), ("12.0", 12), ("unknown", None)]:
            self.assertEqual(interp.normalize_answer(raw), normalized)
        for answer in ("", "```interpret\n[]\n```", "```interpret\n{bad}\n```", "```interpret\n{\"q\": []}\n```",
                       "```interpret\n{\"q\": NaN}\n```", "```interpret\n{}\n```\n```interpret\n{}\n```"):
            self.assertIsNotNone(interp.parse_answer(answer)[1])
        self.assertEqual(interp.parse_answer('```json\n{"q001": null}\n```')[0], {"q001": None})

    def fake_runner(self, behavior):
        outer = self
        class Runner:
            def __init__(self, workspace, config, spec, run_id, worktree):
                self.workspace, self.spec, self.run_id = workspace, spec, run_id
                self.tree = Path(worktree["workspace"])
            def run(self):
                node = self.spec["nodes"][0]
                outer.assertFalse(node["write"])
                outer.assertNotIn("checks", node["acceptance"])
                outer.assertFalse((self.tree / ".fusion").exists())
                questions = json.loads((self.tree / "evidence/questions.json").read_text())
                answers = {}
                for question in questions:
                    text = (self.tree / question["directory"] / "summary.md").read_text()
                    answers[question["id"]] = re.search(r"Answer: (.+)\.\s*$", text)[1] if behavior == "trust" else None
                if behavior == "tamper":
                    (self.tree / "evidence/questions.json").write_text("[]")
                answer = self.workspace / f"{self.run_id}.md"
                answer.write_text("```interpret\n" + json.dumps(answers) + "\n```" if behavior != "invalid" else "No JSON.")
                DecisionStore(self.workspace).append("input", id=self.run_id)
                return {"workflow_id": self.run_id, "status": "success", "spent_usd": 0,
                        "nodes": [{"result": {"run_id": self.run_id, "status": "success", "agent": "codex",
                                  "gate_label": {"decision_id": self.run_id}, "artifacts": {"answer": str(answer)}}}]}
        return Runner

    def test_summary_trusting_lane_misreads_every_trap_and_loses_prior(self):
        task = self.seed()
        result = gym.run(self.output, ["codex"], self.gym_dir, kind="interpret", runner=self.fake_runner("trust"))
        [row] = result["runs"]
        self.assertEqual(row["verdict"], "misread")
        self.assertEqual((row["scores"]["correct"], row["scores"]["misread"], row["scores"]["trap_misread_rate"]), (6, 6, 1))
        self.assertEqual(row["scores"]["holdout"]["trap_misread_rate"], 1)
        self.assertEqual(row["changed"], [])
        self.assertEqual(row["grade_label"]["answers"], {"failed_task": "true"})
        self.assertEqual(gym.audit(self.gym_dir)["retracted"], [])
        self.assertTrue(Path(row["answer_file"]).exists())
        self.assertFalse((self.gym_dir / "tasks" / task["id"] / "hidden/lanes-interpret/codex").exists())
        report = gym.report(self.gym_dir)
        self.assertEqual(report["modes"], {})
        self.assertEqual(report["interpret"]["lanes"]["codex"]["holdout"]["total"], 4)
        self.assertIn("== interpret", gym.table(report))
        self.assertIn("holdout%", gym.table(report))
        priors = gym.lane_priors(self.gym_dir, include_unknown=True)
        stats = priors["priors"]["codex"]["interpret"]
        self.assertEqual((stats["successes"], stats["attempts"]), (4, 8))
        self.assertNotIn("read", priors["priors"]["codex"])
        path = gym.write_priors(priors, self.root / "priors.json")
        index = policy.load_priors({"path": str(path)})
        got = policy.prior_for(index, {"agent": "codex"}, "interpret", 1, 100)
        self.assertEqual((got["prior_attempts"], got["prior_successes"]), (8, 4))
        self.assertEqual(gym.run(self.output, ["codex"], self.gym_dir, kind="interpret", runner=self.fake_runner("trust"))["runs"], [])

    def test_abstain_half_weight_invalid_failure_tamper_excluded_and_holdout_excluded(self):
        self.seed()
        for behavior, verdict in (("abstain", "abstained"), ("invalid", "invalid_answer"), ("tamper", "tampered")):
            directory = self.root / behavior
            [row] = gym.run(self.output, ["codex"], directory, kind="interpret", runner=self.fake_runner(behavior))["runs"]
            self.assertEqual(row["verdict"], verdict)
            self.assertEqual(row["scores"]["misread"], 0)
            value = gym.lane_priors(directory, include_unknown=True)
            if behavior == "tamper":
                self.assertEqual(value["priors"], {})
            else:
                stats = value["priors"]["codex"]["interpret"]
                self.assertEqual((stats["attempts"], stats["successes"]), (4 if behavior == "abstain" else 8, 0))
        holdout_dir = self.root / "holdout-tasks"
        interp.extract(self.tasks, holdout_dir, 37, "2026-W39", split="holdout")
        gym.run(holdout_dir, ["codex"], self.root / "holdout-gym", kind="interpret", runner=self.fake_runner("trust"))
        self.assertEqual(gym.lane_priors(self.root / "holdout-gym", include_unknown=True)["priors"], {})

    def test_cli_alias_extract_report_priors_and_mode_rejection(self):
        parser = core.build_parser()
        for command in (["extract", "--kind", "interpret"], ["interpret-seed"]):
            args = parser.parse_args(["gym", *command, "--tasks", str(self.tasks), "--out", str(self.output),
                                     "--seed", "37", "--period", "2026-W39"])
            self.assertEqual(gym.command(args, self.repo, out=io.StringIO()), 0)
        args = parser.parse_args(["gym", "run", str(self.output), "--kind", "interpret", "--lanes", "codex",
                                 "--workspace", str(self.gym_dir)])
        with patch.object(gym, "run", wraps=lambda *a, **kw: {"status": "complete", "runs": [], "spent_usd": 0}) as run:
            gym.command(args, self.repo, out=io.StringIO())
        self.assertEqual(run.call_args.kwargs["mode"], "hidden")
        for mode in ("visible", "hidden+hints"):
            with self.assertRaisesRegex(ValueError, "interpret"):
                gym.run(self.output, ["codex"], self.gym_dir, kind="interpret", mode=mode)
        with self.assertRaisesRegex(ValueError, "require"):
            gym.run(self.output, ["codex"], self.gym_dir)
        with self.assertRaisesRegex(ValueError, "require"):
            gym.run(self.tasks, ["codex"], self.gym_dir, kind="interpret")

    def test_later_lanes_have_no_path_to_prior_answers_or_grading_records(self):
        self.seed()
        seen = []
        outer = self
        base = self.fake_runner("trust")

        class InspectingRunner(base):
            def run(self):
                # The old topology made ../../../../../results.jsonl readable.
                root = self.workspace.parent
                outer.assertFalse(self.tree.is_relative_to(outer.gym_dir))
                outer.assertFalse(self.workspace.is_relative_to(outer.gym_dir))
                outer.assertTrue((self.tree / ".git").is_dir(), "no Git file pointing to the coordinator")
                outer.assertFalse((root / "results.jsonl").exists())
                outer.assertEqual(list(root.rglob("*.answer.md")), [])
                outer.assertNotIn(str(outer.gym_dir), json.dumps(self.spec))
                outer.assertEqual(core.selected_control_workspace(), self.workspace)
                if seen:
                    for previous in seen:
                        outer.assertFalse(previous.exists(), "previous lane's runtime must be destroyed")
                    records = gym.read_results(outer.gym_dir)
                    outer.assertTrue(records, "first lane has already persisted coordinator results")
                    outer.assertNotIn("questions", records[-1]["scores"])
                    outer.assertNotIn('"trap":', json.dumps(records))
                seen.append(root)
                return super().run()

        with patch.dict("os.environ", {"FUSION_CONTROL_WORKSPACE": str(self.gym_dir)}):
            result = gym.run(self.output, ["codex", "agy"], self.gym_dir, kind="interpret",
                             runner=InspectingRunner, keep_worktrees=True)
        self.assertEqual(len(result["runs"]), 2)
        self.assertTrue(all(not root.exists() for root in seen))
        for row in result["runs"]:
            self.assertTrue(Path(row["answer_file"]).is_file())
            self.assertTrue(Path(row["runtime_archive"]).is_dir())
            self.assertTrue(Path(row["worktree"]).is_relative_to(self.gym_dir))
        # A resumed invocation also gets a fresh namespace, with old records present.
        gym.run(self.output, ["grok"], self.gym_dir, kind="interpret", runner=InspectingRunner)
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(not root.exists() for root in seen))

    def test_snapshot_failures_never_grade_or_supply_priors_and_can_retry(self):
        self.seed()
        for error in (OSError("cannot inspect tree"), ValueError(""), RuntimeError("snapshot failed")):
            with self.subTest(error=type(error).__name__):
                directory = self.root / type(error).__name__
                with patch("fusion_publish.snapshot", side_effect=error):
                    [row] = gym.run(self.output, ["codex"], directory, kind="interpret",
                                    runner=self.fake_runner("tamper"))["runs"]
                self.assertEqual(row["verdict"], "invalid_snapshot")
                self.assertFalse(row["completed"])
                self.assertEqual(row["scores"], {})
                self.assertEqual(row["grade_label"]["status"], "skipped")
                self.assertIn("snapshot_error", row)
                self.assertEqual(gym.lane_priors(directory, include_unknown=True)["priors"], {})
                self.assertEqual(gym.report(directory)["incomplete"], [row["key"]])
                self.assertFalse([e for e in read_jsonl(DecisionStore(directory).path)
                                  if e.get("source") == "gym_grade"])
                [retry] = gym.run(self.output, ["codex"], directory, kind="interpret",
                                 runner=self.fake_runner("trust"))["runs"]
                self.assertTrue(retry["completed"])
                self.assertEqual(retry["verdict"], "misread")
                self.assertTrue(gym.lane_priors(directory, include_unknown=True)["priors"])

    def test_actual_fake_worker_runs_with_read_only_spec(self):
        # Exercise WorkflowRunner and the CLI adapter, beyond the injected runner.
        self.seed()
        worker = self.root / "summary-worker"
        worker.write_text('''#!/usr/bin/env python3
import json, pathlib, re, sys
sys.stdin.read()
answers = {}
for q in json.loads(pathlib.Path("evidence/questions.json").read_text()):
    text = (pathlib.Path(q["directory"]) / "summary.md").read_text()
    answers[q["id"]] = re.search(r"Answer: (.+)\\.\\s*$", text)[1]
text = "```interpret\\n" + json.dumps(answers) + "\\n```\\nSTATUS: success\\nSUMMARY: read evidence\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none"
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
''')
        worker.chmod(0o755)
        self.config["routes"]["summary"] = {"agent": "codex", "command": str(worker)}
        (self.repo / ".fusion.json").write_text(json.dumps(self.config))
        [row] = gym.run(self.output, ["summary"], self.gym_dir, kind="interpret")["runs"]
        self.assertEqual(row["verdict"], "misread")
        self.assertEqual(row["scores"]["trap_misread_rate"], 1)
        self.assertEqual(row["changed"], [])


class RoutingClassTest(unittest.TestCase):
    def test_only_read_only_interpret_roles_use_interpret_class(self):
        for role in ("triage-interpret", "evidence-interpretation"):
            self.assertEqual(policy.role_prior_class("read", policy.normalize_role(role)), "interpret")
            self.assertEqual(policy.role_prior_class("write", policy.normalize_role(role)), "write")
        for role in ("triage-locate", "localization", "triage", None):
            self.assertEqual(policy.role_prior_class("read", policy.normalize_role(role)), "read")


if __name__ == "__main__":
    unittest.main()
