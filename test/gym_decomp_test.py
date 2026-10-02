"""Decomp gym kind: an external grader's exit codes and outcomes become gym rows and per-stratum priors."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_gym as gym
import fusion_gym_decomp as decomp
import fusion_policy as policy

TOOLCHAIN = "sha256:" + "a" * 64

FAKE_GRADER = textwrap.dedent('''
    import json, os, sys
    from pathlib import Path
    plan = json.loads(Path(os.environ["FAKE_GRADER_PLAN"]).read_text())
    Path(os.environ["FAKE_GRADER_ARGV"]).write_text(json.dumps(sys.argv[1:]))
    if sys.argv[1] == "extract":
        out = Path(sys.argv[sys.argv.index("--out") + 1])
        out.mkdir(parents=True, exist_ok=True)
        summary = {"schema": "tenet.decomp-extract.v1", "tasks": [], "skipped": plan.get("skipped", [])}
        for task in plan.get("tasks", []):
            (out / (task["task_id"] + ".json")).write_text(json.dumps(task))
            summary["tasks"].append({"task_id": task["task_id"], "commit": "c" * 40, "file": task["task_id"] + ".json"})
        print(json.dumps(plan.get("print", summary)))
        sys.exit(plan.get("exit", 0))
    task = json.loads(Path(sys.argv[sys.argv.index("--task") + 1]).read_text())
    if plan.get("print") is not None:
        print(json.dumps(plan["print"]))
        sys.exit(plan["exit"])
    reason = plan.get("fail_reason")
    flag = lambda name: sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None
    print("grading", task["task_id"])
    print(json.dumps({"schema": "tenet.decomp-outcome.v1", "task_id": task["task_id"], "game": task["game"],
                      "stratum": task["stratum"], "pass": plan["exit"] == 0, "fail_reason": reason,
                      "bytes_exact": plan["exit"] == 0, "wall_s": 1.5, "host": "h", "toolchain_sha256": "t" * 64,
                      "task_sha256": "s" * 64, "started_at": "2026-10-01T00:00:00Z", "ended_at": "2026-10-01T00:00:02Z",
                      "lane": flag("--lane"), "model": flag("--model"),
                      "cost_usd": float(flag("--cost-usd")) if flag("--cost-usd") else None,
                      "tokens_in": None, "tokens_out": None}))
    sys.exit(plan["exit"])
''')


def task(task_id="t-0001", stratum="S1", **extra):
    return {"schema": decomp.TASK_SCHEMA, "task_id": task_id, "game": "g-fixture", "stratum": stratum,
            "binary_sha256": "b" * 64, "toolchain": {"image": "img", "image_sha256": TOOLCHAIN, "cc": "gcc", "flags": ["-O2"]},
            "objdiff_config": "objdiff.json", "units": [{"name": "u0", "source": "src/u0.c"}],
            "target": {"unit": "u0", "functions": ["fn_0001"]}, **extra}


def outcome(exit_code, reason=None, **extra):
    value = {"schema": decomp.OUTCOME_SCHEMA, "task_id": "t-0001", "game": "g-fixture", "stratum": "S1",
             "pass": exit_code == 0, "fail_reason": reason, "bytes_exact": exit_code == 0, "wall_s": 1.0, "host": "h",
             "toolchain_sha256": "t" * 64, "task_sha256": "s" * 64, "started_at": "a", "ended_at": "b",
             "lane": None, "model": None, "cost_usd": None, "tokens_in": None, "tokens_out": None}
    return "noise\n" + json.dumps({**value, **extra}) + "\n"


class ImportOutcomeTest(unittest.TestCase):
    def test_exit_codes_map_to_verdicts(self):
        self.assertEqual(decomp.import_outcome(0, outcome(0))[0], "matched")
        for reason in ("f2p", "p2p:u1", "link", "closure", "fake_scan:asm", "judge"):
            self.assertEqual(decomp.import_outcome(1, outcome(1, reason))[0], "unmatched")
        for reason in ("infra", "timeout"):
            self.assertEqual(decomp.import_outcome(3, outcome(3, reason))[0], "hold")

    def test_a_refused_task_has_no_outcome(self):
        with self.assertRaisesRegex(ValueError, "exit 2.*units missing"):
            decomp.import_outcome(2, json.dumps({"error": "bad_task", "message": "units missing"}))

    def test_anything_outside_the_contract_is_refused(self):
        cases = {"outside the contract": (4, outcome(1, "f2p")),
                 "without a": (0, "not json"),
                 "contradicts": (1, outcome(1, "f2p", **{"pass": True})),
                 "does not fit exit 1": (1, outcome(1, "timeout")),
                 "does not fit exit 3": (3, outcome(3, "f2p")),
                 "does not fit exit 0": (0, outcome(0, "f2p")),
                 "fit exit 1": (1, outcome(1, "p2p:")),
                 "lacks": (0, json.dumps({"schema": decomp.OUTCOME_SCHEMA, "pass": True}))}
        for message, (code, stdout) in cases.items():
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                decomp.import_outcome(code, stdout)

    def test_the_outcome_must_be_for_the_task_graded(self):
        with self.assertRaisesRegex(ValueError, "stratum"):
            decomp.import_outcome(0, outcome(0), task(stratum="S2"))

    def test_tasks_are_opaque_beyond_the_fields_orc_reads(self):
        self.assertEqual(decomp.validate_task({"schema": decomp.TASK_SCHEMA, "task_id": "t", "game": "g",
                                               "stratum": "S4", "base_commit": "x", "editable": []})["stratum"], "S4")
        for field in ("task_id", "game", "stratum"):
            with self.subTest(field), self.assertRaisesRegex(ValueError, field):
                decomp.validate_task({**task(), field: ""})

    def test_argv_follows_the_contract_and_carries_no_binary_unless_given(self):
        argv = decomp.grade_argv("d/task.json", "tree", "out", "py -m g", lane="L", model="m", cost_usd=0.5)
        self.assertEqual(argv[:4], ["py", "-m", "g", "grade"])
        self.assertIn("--json", argv)
        self.assertNotIn("--binary", argv)
        self.assertEqual(argv[argv.index("--cost-usd") + 1], "0.5")
        self.assertIn("--binary", decomp.grade_argv("t", "c", "o", binary="bin"))


class DecompCliTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        script = self.root / "fake_grader.py"
        script.write_text(FAKE_GRADER)
        self.grader = shlex.join([sys.executable, str(script)])
        self.plan, self.argv = self.root / "plan.json", self.root / "argv.json"
        env = patch.dict(os.environ, {"FAKE_GRADER_PLAN": str(self.plan), "FAKE_GRADER_ARGV": str(self.argv),
                                      "ORC_HOME": str(self.root / "orc"), "FUSION_PROGRESS": "0",
                                      "FUSION_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.gym = self.root / "gym"
        self.tree = self.root / "tree"
        self.tree.mkdir()

    def cli_error(self, argv):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            core.main(["--workspace", str(self.root), "gym", *argv, "--grader", self.grader])
        self.assertEqual(raised.exception.code, 2)
        return stderr.getvalue()

    def write_task(self, value):
        folder = self.root / "tasks" / value["task_id"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "task.json"
        path.write_text(json.dumps(value))
        return path

    def grade(self, value, exit_code, reason=None, lane="claude-opus-high", extra=()):
        self.plan.write_text(json.dumps({"exit": exit_code, "fail_reason": reason}))
        path = self.write_task(value)
        with contextlib.redirect_stdout(io.StringIO()):
            code = core.main(["--workspace", str(self.root), "gym", "decomp-grade", str(self.gym), "--task", str(path),
                              "--candidate", str(self.tree), "--lane", lane, "--grader", self.grader, *extra])
        return code, json.loads(self.argv.read_text())

    def test_a_graded_candidate_becomes_a_gym_row(self):
        code, argv = self.grade(task(), 1, "p2p:u0", extra=("--cost-usd", "0.25"))
        self.assertEqual(code, 1)
        [row] = gym.read_results(self.gym)
        self.assertEqual((row["kind"], row["verdict"], row["stratum"], row["fail_reason"]), ("decomp", "unmatched", "S1", "p2p:u0"))
        self.assertEqual(row["key"], "t-0001:claude-opus-high:hidden:decomp")
        self.assertEqual((row["outcome"]["lane"], row["cost_usd"]), ("claude-opus-high", 0.25))
        self.assertEqual(argv[argv.index("--task") + 1], str(self.root / "tasks" / "t-0001" / "task.json"))
        self.assertEqual(argv[argv.index("--candidate") + 1], str(self.tree))
        self.assertNotIn("--binary", argv)

    def test_a_hold_is_recorded_and_exits_3(self):
        self.assertEqual(self.grade(task(), 3, "infra")[0], 3)
        self.assertEqual(gym.read_results(self.gym)[0]["verdict"], "hold")

    def test_a_refused_task_records_nothing(self):
        self.plan.write_text(json.dumps({"exit": 2, "print": {"error": "bad_task", "message": "no units"}}))
        path = self.write_task(task())
        self.assertIn("no units", self.cli_error(["decomp-grade", str(self.gym), "--task", str(path),
                                                  "--candidate", str(self.tree), "--lane", "claude-opus-high"]))
        self.assertEqual(gym.read_results(self.gym), [])

    def test_priors_are_per_stratum_and_holds_never_count(self):
        self.grade(task("t-1", "S1"), 0)
        self.grade(task("t-2", "S1"), 1, "f2p")
        self.grade(task("t-3", "S2"), 0)
        self.grade(task("t-4", "S2"), 3, "timeout")
        self.grade(task("t-1", "S1"), 0, lane="claude-sonnet-high")
        self.grade(task("t-1", "S1"), 3, "infra", lane="claude-sonnet-high")
        value = gym.lane_priors(self.gym, now=0)
        opus = value["priors"]["claude:claude-opus-5-5:high"]
        self.assertEqual({k: (v["attempts"], v["successes"]) for k, v in opus.items() if k.startswith("decomp:")},
                         {"decomp:S1": (2, 1), "decomp:S2": (1, 1)})
        self.assertNotIn("claude:claude-sonnet-5:high", value["priors"])
        self.assertEqual(value["excluded"], {"hold": 2})
        self.assertIn("decomp:S2", gym.priors_table(value))
        path = Path(gym.write_priors(value, self.root / "priors.json"))
        index = policy.load_priors({"path": str(path), "weight": .5, "cap": 10})
        self.assertIn(("claude", "claude-opus-5-5", "high"), index["lane"])

    def test_extract_runs_the_graders_extractor_and_checks_its_tasks(self):
        out = self.root / "extracted"
        self.plan.write_text(json.dumps({"tasks": [task("t-a"), task("t-b", "S3", base_commit="c" * 40)],
                                         "skipped": [{"commit": "d" * 40, "reason": "no function"}]}))
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code = core.main(["--workspace", str(self.root), "gym", "extract", "--kind", "decomp", "--repo-path",
                              str(self.root), "--commits", "c1", "c2", "--out", str(out), "--grader", self.grader])
        self.assertEqual(code, 0)
        self.assertIn("2 decomp tasks, 1 skipped", printed.getvalue())
        argv = json.loads(self.argv.read_text())
        self.assertEqual(argv[:2], ["extract", "--repo"])
        self.assertEqual(argv[argv.index("--commits") + 1:argv.index("--out")], ["c1", "c2"])
        self.assertEqual([t["task_id"] for _, t in decomp.load_tasks(out)], ["t-a", "t-b"])

    def test_extract_failures_and_bad_tasks_are_errors(self):
        out = self.root / "extracted"
        cases = {"infra: no toolchain": {"exit": 3, "print": {"error": "infra", "message": "no toolchain"}},
                 "stratum": {"tasks": [task("t-a", "")]}}
        for message, plan in cases.items():
            self.plan.write_text(json.dumps(plan))
            with self.subTest(message):
                self.assertIn(message, self.cli_error(["extract", "--kind", "decomp", "--repo-path", str(self.root),
                                                       "--commits", "c1", "--out", str(out)]))


if __name__ == "__main__":
    unittest.main()
