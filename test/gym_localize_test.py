"""ORC gym localization kind: ground truth from the fix's diff, answer
parsing and grading, read-only runs with fake workers, keys and reports by
kind, the per-kind solvability audit, and gym_grade labels."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fusion_core as core
import fusion_gym as gym
import fusion_gym_localize as localize
import fusion_labeling
from fusion_decisions import DecisionStore, label_provenance, read_jsonl, reviewed_labels
from fusion_workflow import validate_spec
from gym_test import Isolated, run_git

LIB_BASE = '''import os


def helper(x):
    return x


def gone(y):
    return y


class Calc:
    def add(self, a, b):
        return abs(a) + b

    def sub(self, a, b):
        return a - b


def add(a, b):
    return Calc().add(a, b)
'''
LIB_FIX = '''import os

from util import zero


def helper(x):
    return x


class Calc:
    def add(self, a, b):
        return a + b

    def sub(self, a, b):
        return a - b


def add(a, b):
    return Calc().add(a, b) + zero()
'''
UTIL = "def zero():\n    return 0\n"
TEST_BASE = '''import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import add


class LibTest(unittest.TestCase):
    def test_add_positive(self):
        self.assertEqual(add(2, 3), 5)
'''
TEST_FIX = TEST_BASE + '''
    def test_add_negative(self):
        self.assertEqual(add(-2, 3), 1)
'''
TRUTH = {"files": ["lib.py", "util.py"], "symbols": ["lib.Calc.add", "lib.add", "lib.gone", "util.zero"],
         "new_files": ["util.py"], "new_symbols": ["util.zero"], "docs": ["NOTES.md"]}
WORKER = '''import json, pathlib, sys
prompt = sys.stdin.read()
pathlib.Path(PROMPT_FILE).write_text(prompt)
for name, body in WRITES.items():
    pathlib.Path(name).write_text(body)
text = ANSWER + "\\n\\nSTATUS: success\\nSUMMARY: located the problem\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none"
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
'''


def block(files, symbols=()):
    return "Here is where the bug is.\n\n```localization\n" + json.dumps({"files": files, "symbols": list(symbols)}) + "\n```"


class GroundTruthTest(Isolated):
    """A fix that changes a function and a method, deletes a function, adds a
    module, touches a doc and adds a test."""

    def setUp(self):
        super().setUp()
        self.repo = self.root / "source"
        (self.repo / "test").mkdir(parents=True)
        (self.repo / "lib.py").write_text(LIB_BASE)
        (self.repo / "NOTES.md").write_text("notes\n")
        (self.repo / "test/lib_test.py").write_text(TEST_BASE)
        (self.repo / ".gitignore").write_text(".fusion/\n.fusion.json\n__pycache__/\n")
        run_git(self.repo, "init", "-q")
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "seed")
        self.base = run_git(self.repo, "rev-parse", "HEAD")
        (self.repo / "lib.py").write_text(LIB_FIX)
        (self.repo / "util.py").write_text(UTIL)
        (self.repo / "NOTES.md").write_text("notes, more\n")
        (self.repo / "test/lib_test.py").write_text(TEST_FIX)
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "fix(lib): add keeps the sign of a negative first operand (#7)")
        self.fix = run_git(self.repo, "rev-parse", "HEAD")
        self.config = {"claude": {"command": "missing-claude"}, "agy": {"command": "missing-agy"},
                       "grok": {"command": "missing-grok"}, "timeout_seconds": 60, "decisions": {"mode": "shadow"},
                       "routes": {
                           "right": self.lane("right", block(["lib.py", "README"], ["lib.Calc.add", "lib.py::add"])),
                           "wrong": self.lane("wrong", block(["README", "setup.py", "other.py", "lib.py"], ["lib.helper"])),
                           "vague": self.lane("vague", "It is somewhere in the arithmetic."),
                           "writer": self.lane("writer", block(["lib.py"]), {"notes.txt": "x\n"}),
                           "fixer": {"agent": "codex", "command": self.worker("fixer", {"lib.py": LIB_FIX, "util.py": UTIL})},
                           "idler": {"agent": "codex", "command": self.worker("idler", {"notes.txt": "looked\n"})},
                       }}
        (self.repo / ".fusion.json").write_text(json.dumps(self.config))
        self.tasks = self.root / "tasks"
        self.gym_dir = self.root / "gym"

    def lane(self, name, answer, writes=None):
        path = self.root / name
        path.write_text("#!/usr/bin/env python3\n" + f"ANSWER = {answer!r}\nWRITES = {writes or {}!r}\n"
                        + f"PROMPT_FILE = {str(self.root / (name + '.prompt'))!r}\n" + WORKER)
        path.chmod(0o755)
        return {"agent": "codex", "command": str(path)}

    def extract(self):
        _, [task] = gym.extract(self.repo, [7], self.tasks, use_gh=False)
        return task

    def grade_labels(self):
        return [e for e in read_jsonl(DecisionStore(self.gym_dir).path)
                if e.get("event") == "label" and e.get("source") == "gym_grade"]

    def test_ground_truth_names_changed_files_and_symbols_read_only(self):
        status = run_git(self.repo, "status", "--porcelain")
        refs = run_git(self.repo, "for-each-ref")
        self.assertEqual(gym.localization_for(self.repo, self.base, self.fix), TRUTH)
        self.assertEqual((run_git(self.repo, "status", "--porcelain"), run_git(self.repo, "for-each-ref")), (status, refs))
        task = self.extract()
        self.assertEqual(task["localization"], TRUTH)
        self.assertEqual(localize.gradeable(TRUTH), (["lib.py"], ["lib.Calc.add", "lib.add", "lib.gone"]))

    def test_right_and_wrong_workers_are_graded_and_labeled(self):
        self.extract()
        result = gym.run(self.tasks, ["right", "wrong", "vague", "writer"], self.gym_dir, kind="localize")
        self.assertEqual((result["status"], result["kind"], result["mode"]), ("complete", "localize", "hidden"))
        runs = {row["lane"]: row for row in result["runs"]}
        self.assertEqual({lane: row["verdict"] for lane, row in runs.items()},
                         {"right": "localized", "wrong": "missed", "vague": "invalid_answer", "writer": "localized"})
        self.assertTrue(all(row["key"] == f"pr-7:{lane}:hidden:localize" and row["kind"] == "localize"
                            for lane, row in runs.items()))
        right = runs["right"]
        self.assertEqual(right["answer"], {"files": ["lib.py", "README"], "symbols": ["lib.Calc.add", "lib.add"]})
        self.assertEqual(right["scores"], {"file_acc_at_1": True, "file_recall_at_1": 1.0, "file_recall_at_3": 1.0,
                                           "file_recall_at_5": 1.0, "file_precision": 0.5, "symbol_recall": 0.667,
                                           "graded_files": 1, "graded_symbols": 3})
        self.assertEqual(right["truth"], {"files": ["lib.py"], "symbols": ["lib.Calc.add", "lib.add", "lib.gone"]})
        self.assertEqual((runs["wrong"]["scores"]["file_recall_at_5"], runs["wrong"]["scores"]["file_acc_at_1"]), (1.0, False))
        self.assertIn("no ```localization block", runs["vague"]["answer_error"])
        self.assertEqual(runs["writer"]["changed"], ["notes.txt"])
        self.assertIn("```localization", Path(right["answer_file"]).read_text())
        # The prompt is the problem text plus the brief: no hints, no test names.
        prompt = (self.root / "right.prompt").read_text()
        self.assertIn("add keeps the sign of a negative first operand", prompt)
        self.assertIn("```localization", prompt)
        for leak in ("Interface the change", "test_add_negative", "lib_test", "util"):
            self.assertNotIn(leak, prompt)
        # Labels: only a reported success, only localized or missed, never a writer's.
        self.assertEqual(right["grade_label"]["answers"], {"failed_task": "false"})
        self.assertEqual(runs["wrong"]["grade_label"]["answers"], {"failed_task": "true"})
        self.assertEqual(runs["vague"]["grade_label"]["status"], "unlabeled")
        self.assertEqual(runs["writer"]["grade_label"]["status"], "skipped")
        labels = self.grade_labels()
        self.assertEqual([label["answers"] for label in labels], [{"failed_task": "false"}, {"failed_task": "true"}])
        events = read_jsonl(DecisionStore(self.gym_dir).path)
        provenance = label_provenance(events)
        decision = right["grade_label"]["decision_id"]
        self.assertEqual(provenance[decision]["failed_task"]["source"], "gym_grade")
        record = next(e for e in events if e.get("event") == "decision" and e["id"] == decision)
        self.assertEqual((record["kind"], record["context"]["task_id"]), ("acceptance", right["worker"]["run_id"]))
        self.assertFalse([e for e in events if e.get("event") == "label" and e.get("source") == "structural_gate"])
        exported = DecisionStore(self.gym_dir).export(self.root / "all.jsonl", include_unknown=True)
        without = DecisionStore(self.gym_dir).export(self.root / "no-grade.jsonl", ["gym_grade"], include_unknown=True)
        self.assertEqual((exported["examples"], without["examples"]), (2, 0))
        # Resumable, and read-only lanes never leave a worktree behind.
        self.assertEqual(gym.run(self.tasks, ["right", "wrong"], self.gym_dir, kind="localize")["runs"], [])
        self.assertEqual(list((self.gym_dir / "tasks/pr-7/hidden/lanes-localize").iterdir()), [])
        value = gym.report(self.gym_dir)
        self.assertEqual(value["modes"], {})
        section = value["localize"]
        self.assertEqual(section["tasks"]["pr-7"], {"localized_by": ["right", "writer"],
                                                    "attempted_by": ["right", "vague", "writer", "wrong"]})
        self.assertEqual({k: section["lanes"]["right"][k] for k in ("attempted", "localized", "file_acc_at_1",
                                                                     "file_recall_at_3", "symbol_recall")},
                         {"attempted": 1, "localized": 1, "file_acc_at_1": 1.0, "file_recall_at_3": 1.0,
                          "symbol_recall": 0.667})
        self.assertEqual((section["lanes"]["vague"]["invalid_answer"], section["lanes"]["vague"]["file_acc_at_1"],
                          section["lanes"]["vague"]["symbol_recall"]), (1, 0.0, 0.0))
        self.assertEqual(section["lanes"]["writer"]["wrote_files"], 1)
        text = gym.table(value)
        self.assertIn("localize (read-only", text)
        self.assertIn("pr-7         localized by: right, writer", text)

    def test_keys_reports_and_audit_keep_kinds_apart(self):
        self.extract()
        [fix] = gym.run(self.tasks, ["idler"], self.gym_dir)["runs"]
        self.assertEqual((fix["key"], fix["kind"], fix["verdict"]), ("pr-7:idler:hidden", "fix", "unsolved"))
        [located] = gym.run(self.tasks, ["idler"], self.gym_dir, kind="localize")["runs"]
        self.assertEqual(located["key"], "pr-7:idler:hidden:localize")
        # The fix negative stays withheld: a localization says nothing about
        # whether the fix could be done from the prompt.
        audit = json.loads((self.gym_dir / "audit.json").read_text())
        self.assertEqual(audit["modes"], {"hidden": {"solved_tasks": [], "unsolved_tasks": ["pr-7"]}})
        answers, _ = reviewed_labels(read_jsonl(DecisionStore(self.gym_dir).path))
        self.assertFalse(answers.get(fix["gate_label"]["decision_id"]))
        value = gym.report(self.gym_dir)
        self.assertEqual(sorted(value["modes"]["hidden"]["lanes"]), ["idler"])
        self.assertEqual(value["localize"]["lanes"]["idler"]["invalid_answer"], 1)

    def test_a_localize_negative_counts_only_once_some_lane_localized_the_task(self):
        self.extract()
        [wrong] = gym.run(self.tasks, ["wrong"], self.gym_dir, kind="localize")["runs"]
        decision = wrong["grade_label"]["decision_id"]
        summary = json.loads((self.gym_dir / "audit.json").read_text())
        self.assertEqual(summary["localize"], {"localized_tasks": [], "unlocalized_tasks": ["pr-7"]})
        self.assertEqual(summary["retracted"], ["pr-7:wrong:hidden:localize"])
        answers, _ = reviewed_labels(read_jsonl(DecisionStore(self.gym_dir).path))
        self.assertFalse(answers.get(decision))
        # A fix solved on the same task is not evidence for localization.
        gym.run(self.tasks, ["fixer"], self.gym_dir)
        self.assertFalse(reviewed_labels(read_jsonl(DecisionStore(self.gym_dir).path))[0].get(decision))
        gym.run(self.tasks, ["right"], self.gym_dir, kind="localize")
        summary = json.loads((self.gym_dir / "audit.json").read_text())
        self.assertEqual(summary["localize"], {"localized_tasks": ["pr-7"], "unlocalized_tasks": []})
        self.assertEqual(summary["restored"], ["pr-7:wrong:hidden:localize"])
        answers, _ = reviewed_labels(read_jsonl(DecisionStore(self.gym_dir).path))
        self.assertEqual(answers[decision], {"failed_task": "true"})
        self.assertEqual(label_provenance(read_jsonl(DecisionStore(self.gym_dir).path))[decision]["failed_task"]["source"],
                         "gym_grade")

    def test_old_tasks_get_ground_truth_lazily_and_the_cli_forbids_hints_and_visible_tests(self):
        self.extract()
        path = self.tasks / "pr-7.json"
        task = json.loads(path.read_text())
        task.pop("localization")
        path.write_text(json.dumps(task))
        for mode in ("hidden+hints", "visible"):
            with self.assertRaises(ValueError):
                gym.run(self.tasks, ["right"], self.gym_dir, mode=mode, kind="localize")
        with self.assertRaises(ValueError):
            gym.run(self.tasks, ["right"], self.gym_dir, kind="explore")
        out = io.StringIO()
        base = ["--quiet", "gym", "run", str(self.tasks), "--lanes", "right", "--workspace", str(self.gym_dir),
                "--kind", "localize"]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(core.main(base), 0)
            with self.assertRaises(SystemExit):
                core.main(base + ["--visible-tests"])
        self.assertIn("localized by: right", out.getvalue())
        [row] = [row for row in gym.read_results(self.gym_dir) if row.get("event") == "finished"]
        self.assertEqual((row["key"], row["verdict"]), ("pr-7:right:hidden:localize", "localized"))
        self.assertNotIn("localization", json.loads(path.read_text()))
        spec = validate_spec(gym.build_localize_spec(task | {"localization": TRUTH, "interface": [{"kind": "name"}]},
                                                     {"agent": "claude", "model": "m"}, 1.0))
        [node] = spec["graph"]["nodes"]
        self.assertEqual((node["write"], node["max_budget_usd"], node["acceptance"]), (False, 1.0, {"required_handoff": ["summary"]}))
        self.assertNotIn("Interface the change", node["task"])

    def test_a_task_whose_fix_only_adds_files_is_skipped(self):
        task = self.extract()
        path = self.tasks / "pr-7.json"
        path.write_text(json.dumps({**task, "localization": {**TRUTH, "files": ["util.py"]}}))
        result = gym.run(self.tasks, ["right"], self.gym_dir, kind="localize")
        self.assertEqual((result["runs"], result["skipped"]),
                         ([], [{"task": "pr-7", "reason": "the fix changed no source file that exists in B"}]))


class GradeTest(unittest.TestCase):
    truth = {"files": ["a.py", "b.py", "new.py"], "symbols": ["a.f", "b.C.m", "new.g"], "new_files": ["new.py"],
             "new_symbols": ["new.g"], "docs": []}

    def grade(self, files, symbols=()):
        return localize.grade({"files": files, "symbols": list(symbols)}, self.truth)

    def test_recall_precision_and_verdicts(self):
        value = self.grade(["a.py", "x.py", "b.py"], ["a.f", "zzz"])
        self.assertEqual(value, {"verdict": "localized", "file_acc_at_1": True, "file_recall_at_1": 0.5,
                                 "file_recall_at_3": 1.0, "file_recall_at_5": 1.0, "file_precision": 0.667,
                                 "symbol_recall": 0.5, "graded_files": 2, "graded_symbols": 2})
        self.assertEqual(self.grade(["x.py", "a.py"])["verdict"], "partial")
        missed = self.grade(["x.py", "y.py", "new.py", "b.py"])
        self.assertEqual((missed["verdict"], missed["file_recall_at_3"], missed["file_recall_at_5"]), ("missed", 0.0, 0.5))
        self.assertEqual(missed["file_precision"], 0.25)
        # More changed files than 3: all of them must be in the top n.
        truth = {"files": ["a", "b", "c", "d"], "symbols": []}
        value = localize.grade({"files": ["a", "b", "c", "d"], "symbols": []}, truth)
        self.assertEqual((value["verdict"], value["file_recall_at_3"], value["symbol_recall"]), ("localized", 0.75, None))
        self.assertEqual(localize.grade({"files": ["a", "b", "c", "x", "d"], "symbols": []}, truth)["verdict"], "partial")

    def test_answers_are_parsed_strictly_and_normalized(self):
        for text, error in (("", "no ```localization block"), ("```json\n{}\n```", "no ```localization block"),
                            (block(["a.py"]) + "\n" + block(["b.py"]), "exactly one"),
                            ("```localization\n{files: 1}\n```", "not JSON"),
                            ("```localization\n[1]\n```", "not a JSON object"),
                            (block([]), "non-empty list"), ("```localization\n{\"files\": [\"a\", 2]}\n```", "non-empty list"),
                            ("```localization\n{\"files\": [\"a\"], \"symbols\": \"a.f\"}\n```", "list of strings")):
            with self.subTest(text=text):
                answer, message = localize.parse_answer(text)
                self.assertIsNone(answer)
                self.assertIn(error, message)
        files = ["./a.py", "a.py", "`b.py`"] + [f"f{i}.py" for i in range(20)]
        answer, error = localize.parse_answer(block(files, ["a.py::C.m", "pkg/mod.py:f", "a.f()", "a.f"]))
        self.assertIsNone(error)
        self.assertEqual(answer["files"], ["a.py", "b.py"] + [f"f{i}.py" for i in range(8)])
        self.assertEqual(answer["symbols"], ["a.C.m", "pkg.mod.f", "a.f"])
        answer, _ = localize.parse_answer("```localization\n{\"files\": [\"a.py\"]}\n```")
        self.assertEqual(answer, {"files": ["a.py"], "symbols": []})

    def test_symbols_resolve_to_the_innermost_definition(self):
        source = "X = 1\n\n\n@dec\ndef f():\n    def inner():\n        pass\n\n\nclass C:\n    y = 2\n\n    def m(self):\n        pass\n"
        spans = localize.intervals(source)
        self.assertEqual([localize.enclosing(spans, line) for line in (1, 4, 7, 11, 14)], [None, "f", "f", "C", "C.m"])
        self.assertIsNone(localize.intervals("def ("))
        self.assertEqual(localize.module_name("src/pkg/__init__.py"), "pkg")
        self.assertEqual(localize.hunks("@@ -3,0 +4,2 @@\n@@ -9 +10 @@\n"), [(range(3, 3), range(4, 6)), (range(9, 10), range(10, 11))])
        self.assertTrue(localize.is_doc_path("docs/x.py") and localize.is_doc_path("README.md"))
        self.assertFalse(localize.is_doc_path("install.sh"))


class GradeSourceTest(unittest.TestCase):
    def test_gym_grade_is_an_automatic_source_below_lead_verdict(self):
        self.assertEqual(gym.GRADE_SOURCE, fusion_labeling.GYM_SOURCE)
        precedence = fusion_labeling.LABEL_PRECEDENCE
        self.assertLess(precedence["gym_grade"], precedence["lead_verdict"])
        self.assertIn("gym_grade", fusion_labeling.AUTOMATIC_SOURCES)

    def test_a_verdict_keeps_the_grade_it_does_not_answer_but_not_a_retracted_one(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DecisionStore(temp)
            store.append("label", id="d1", answers={"failed_task": "true"}, verified=True, replace=False, source="gym_grade")
            store.append("label", id="d1", answers={"plausible": "false"}, verified=True, replace=True, source="lead_verdict")
            fusion_labeling.restore_gate_labels(store, "d1", read_jsonl(store.path), {"plausible"})
            events = read_jsonl(store.path)
            self.assertEqual(reviewed_labels(events)[0]["d1"], {"plausible": "false", "failed_task": "true"})
            self.assertEqual(label_provenance(events)["d1"]["failed_task"]["source"], "gym_grade")
            store.append("label", id="d2", answers={"failed_task": "true"}, verified=True, replace=False, source="gym_grade")
            store.append("label", id="d2", answers={}, verified=True, replace=True, source="gym_grade")
            store.append("label", id="d2", answers={"plausible": "false"}, verified=True, replace=True, source="lead_verdict")
            fusion_labeling.restore_gate_labels(store, "d2", [e for e in read_jsonl(store.path) if e["id"] == "d2"],
                                                {"plausible"})
            self.assertEqual(reviewed_labels(read_jsonl(store.path))[0]["d2"], {"plausible": "false"})

    def test_a_label_on_a_labeled_input_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            DecisionStore(root).append("label", id="d1", answers={"failed_task": "false"}, verified=True, replace=False,
                                       source="human")
            row = {"key": "t:l:hidden:localize", "verdict": "missed", "changed": [], "truth": {"files": ["a.py"]},
                   "worker": {"status": "success", "run_id": "r"}, "gate_label": {"decision_id": "d1"}}
            self.assertEqual(gym.grade_label(root, row)["status"], "preserved")
            self.assertEqual(gym.grade_label(root, {**row, "worker": {"status": "partial"}})["status"], "skipped")
            self.assertEqual(gym.grade_label(root, {**row, "gate_label": {"status": "disabled"}})["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
