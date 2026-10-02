"""`gym run --kind decomp`: lanes run on decomp tasks in fresh disposable trees, with a checker, tamper
detection and the official grade (synthetic: a fake grader and fake workers)."""
import contextlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fusion_core as core
import fusion_gym as gym
import fusion_gym_decomp as decomp
from fusion_workflow import WorkflowRunner
from gym_test import Isolated, run_git

# Passes when the target unit's source says MATCHED; holds when the plan says so.
FAKE_GRADER = '''import json, sys
from pathlib import Path
PLAN, LOG = Path(PLAN_PATH), Path(LOG_PATH)
plan = json.loads(PLAN.read_text()) if PLAN.exists() else {}
argv = sys.argv[1:]
flag = lambda name: argv[argv.index(name) + 1] if name in argv else None
with LOG.open("a") as stream:
    stream.write(json.dumps({"argv": argv, "cwd": str(Path.cwd())}) + "\\n")
task = json.loads(Path(flag("--task")).read_text())
source = (Path.cwd() / flag("--candidate")).resolve() / task["units"][0]["source"]
sys.stderr.write("log: compiling " + task["units"][0]["source"] + "\\n")
if plan.get("hold"):
    code, reason = 3, "infra"
elif source.is_file() and "MATCHED" in source.read_text():
    code, reason = 0, None
else:
    code, reason = 1, "f2p"
number = lambda name: float(flag(name)) if flag(name) else None
print("grading " + task["task_id"])
print(json.dumps({"schema": "tenet.decomp-outcome.v1", "task_id": task["task_id"], "game": task["game"],
                  "stratum": task["stratum"], "pass": code == 0, "fail_reason": reason, "bytes_exact": code == 0,
                  "wall_s": 0.1, "host": "h", "toolchain_sha256": "t" * 64, "task_sha256": "s" * 64,
                  "started_at": "a", "ended_at": "b", "lane": flag("--lane"), "model": flag("--model"),
                  "cost_usd": number("--cost-usd"), "tokens_in": None, "tokens_out": None}))
sys.exit(code)
'''
# Records what it saw, runs the checker before and after its edits.
WORKER = '''import json, os, pathlib, subprocess, sys
prompt = sys.stdin.read()
def check():
    proc = subprocess.run([PYTHON, "check.py"], capture_output=True, text=True)
    return {"code": proc.returncode, "stdout": proc.stdout}
files = sorted(str(p) for p in pathlib.Path(".").rglob("*") if p.is_file() and ".git" not in p.parts)
before = check()
for name, body in WRITES.items():
    pathlib.Path(name).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(name).write_text(body)
after = check()
pathlib.Path(RECORD).write_text(json.dumps({"prompt": prompt, "files": files, "before": before, "after": after,
                                            "cwd": os.getcwd()}))
text = "STATUS: success\\nSUMMARY: decompiled\\nCHANGED: src/u0.c\\nTESTS: python3 check.py\\nBLOCKERS: none"
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
'''
START = "int fn_0001(void) { return 0; }\n"
MATCHED = "/* MATCHED */ int fn_0001(void) { return 1; }\n"


class CostlyRunner(WorkflowRunner):
    def run(self):
        outcome = super().run()
        outcome["spent_usd"] = 0.5
        return outcome


class GymRunDecompTest(Isolated):
    def setUp(self):
        super().setUp()
        script = self.root / "fake_grader.py"
        self.plan, self.log = self.root / "plan.json", self.root / "grader.log"
        script.write_text(f"PLAN_PATH = {str(self.plan)!r}\nLOG_PATH = {str(self.log)!r}\n" + FAKE_GRADER)
        self.grader = shlex.join([sys.executable, str(script)])
        self.tasks = self.root / "tasks"
        self.gym_dir = self.root / "gym"
        self.gym_dir.mkdir()
        self.routes = {}
        self.lane("matcher", {"src/u0.c": MATCHED})
        self.lane("idler", {})
        self.lane("cheater", {"src/u0.c": MATCHED, "include/u0.h": "#define X 1\n", "check.py": "print('PASS')\n"})

    def lane(self, name, writes):
        path = self.root / name
        path.write_text("#!/usr/bin/env python3\n" + f"WRITES = {writes!r}\nPYTHON = {sys.executable!r}\n"
                        + f"RECORD = {str(self.root / (name + '.json'))!r}\n" + WORKER)
        path.chmod(0o755)
        self.routes[name] = {"agent": "codex", "command": str(path)}
        (self.gym_dir / ".fusion.json").write_text(json.dumps({
            "claude": {"command": "missing-claude"}, "agy": {"command": "missing-agy"},
            "grok": {"command": "missing-grok"}, "timeout_seconds": 60, "decisions": {"mode": "shadow"},
            "routes": self.routes}))

    def seen(self, name):
        return json.loads((self.root / (name + ".json")).read_text())

    def grader_calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def write_task(self, task_id="t-0001", stratum="S1", start=True, prompt=True, **extra):
        folder = self.tasks / task_id
        folder.mkdir(parents=True)
        value = {"schema": decomp.TASK_SCHEMA, "task_id": task_id, "game": "g-fixture", "stratum": stratum,
                 "objdiff_config": "objdiff.json", "units": [{"name": "u0", "source": "src/u0.c"}],
                 "target": {"unit": "u0", "functions": ["fn_0001"]}, "editable": ["src/u0.c"], **extra}
        (folder / "task.json").write_text(json.dumps(value))
        # Answer-bearing material that must never reach the worker.
        (folder / "objdiff.json").write_text('{"secret": "SECRET-OBJDIFF"}\n')
        (folder / "answer.c").write_text(MATCHED)
        if prompt:
            (folder / "prompt.md").write_text("Target disassembly:\n  li r3, 1\n  blr\n")
        if start:
            (folder / "start" / "src").mkdir(parents=True)
            (folder / "start" / "src" / "u0.c").write_text(START)
            (folder / "start" / "include").mkdir()
            (folder / "start" / "include" / "u0.h").write_text("int fn_0001(void);\n")
        return folder / "task.json"

    def run_gym(self, lanes, tasks=None, **options):
        runner = options.pop("runner", None)
        decomp_options = {"grader": self.grader, **{k: options.pop(k) for k in ("repo_path",) if k in options}}
        return gym.run(tasks or self.tasks, lanes, self.gym_dir, kind="decomp", runner=runner, decomp=decomp_options,
                       **options)

    def test_a_matching_worker_is_matched_and_counted_and_its_brief_names_the_target(self):
        task_path = self.write_task()
        result = self.run_gym(["matcher"])
        self.assertEqual((result["status"], result["kind"], result["mode"], result["skipped"]),
                         ("complete", "decomp", "hidden", []))
        [row] = result["runs"]
        self.assertEqual(row["key"], gym.result_key("t-0001", "matcher", "hidden", "decomp"))
        self.assertEqual((row["verdict"], row["completed"], row["tampered"], row["changed"], row["start"]),
                         ("matched", True, [], ["src/u0.c"], "start"))
        self.assertEqual(decomp.counted(row), (True, True))
        self.assertEqual((row["worker"]["status"], row["worker"]["agent"], row["worker"]["route"]),
                         ("success", "codex", "matcher"))
        self.assertTrue(row["worker"]["run_id"] and row["workflow_id"].startswith("gym-t-0001-dc-matcher-"))
        self.assertIn("MATCHED", Path(row["patch"]).read_text())
        self.assertNotIn("worktree", row)
        self.assertEqual(gym.read_results(self.gym_dir)[-1]["key"], row["key"])
        seen = self.seen("matcher")
        for text in ("fn_0001", "src/u0.c", "Stratum: S1", "python3 check.py", "PASS", "inline asm", "`register`",
                     "`#pragma`", "`__attribute__`", "GCC optimizes the whole file", "li r3, 1",
                     "You may run exactly these commands yourself: python3 check.py"):
            self.assertIn(text, seen["prompt"])
        # The checker: the grader's verdict, its log lines and its exit code.
        self.assertEqual(seen["before"]["code"], 1)
        self.assertTrue(seen["before"]["stdout"].startswith("FAIL: f2p"))
        self.assertIn("log: compiling src/u0.c", seen["before"]["stdout"])
        self.assertEqual((seen["after"]["code"], seen["after"]["stdout"].splitlines()[0]), (0, "PASS"))
        check, _, official = self.grader_calls()
        self.assertEqual(check["argv"][:7], ["grade", "--task", str(task_path.resolve()), "--candidate", ".",
                                             "--out", ".decomp-out"])
        self.assertIn("--json", check["argv"])
        self.assertEqual(official["argv"][official["argv"].index("--lane") + 1], "matcher")
        self.assertTrue(Path(official["argv"][official["argv"].index("--out") + 1]).is_relative_to(self.gym_dir))
        self.assertFalse(Path(seen["cwd"]).is_relative_to(self.gym_dir.resolve()))
        self.assertFalse(Path(seen["cwd"]).exists(), "the disposable tree is removed")
        priors = gym.lane_priors(self.gym_dir, now=0)["priors"]
        [entry] = priors.values()
        self.assertEqual((entry["decomp:S1"]["attempts"], entry["decomp:S1"]["successes"]), (1, 1))

    def test_nothing_from_the_task_directory_but_start_reaches_the_worker(self):
        self.write_task()
        [row] = self.run_gym(["matcher"], keep_worktrees=True)["runs"]
        self.assertEqual(self.seen("matcher")["files"], ["check.py", "include/u0.h", "src/u0.c"])
        kept = Path(row["worktree"])
        self.assertTrue(kept.is_relative_to(self.gym_dir))
        self.assertEqual(run_git(kept, "ls-tree", "-r", "--name-only", "HEAD").splitlines(),
                         ["check.py", "include/u0.h", "src/u0.c"])
        for path in kept.rglob("*"):
            if path.is_file() and ".git" not in path.relative_to(kept).parts:
                self.assertNotIn("SECRET-OBJDIFF", path.read_text(errors="replace"))
        self.assertNotIn("SECRET-OBJDIFF", self.seen("matcher")["prompt"])

    def test_unmatched_and_hold_are_recorded_and_a_hold_never_counts(self):
        self.write_task()
        [row] = self.run_gym(["idler"])["runs"]
        self.assertEqual((row["verdict"], row["fail_reason"], row["completed"]), ("unmatched", "f2p", True))
        self.assertEqual(decomp.counted(row), (True, False))
        self.write_task("t-0002", "S2")
        self.plan.write_text(json.dumps({"hold": True}))
        [hold] = self.run_gym(["matcher"], tasks=self.tasks / "t-0002" / "task.json")["runs"]
        self.assertEqual((hold["verdict"], hold["fail_reason"], hold["completed"]), ("hold", "infra", True))
        self.assertEqual(decomp.counted(hold), (False, False))
        value = gym.lane_priors(self.gym_dir, now=0)
        self.assertEqual(value["excluded"], {"hold": 1})
        self.assertNotIn("decomp:S2", json.dumps(value["priors"]))

    def test_edits_outside_editable_are_tampering_and_never_count(self):
        self.write_task()
        [row] = self.run_gym(["cheater"])["runs"]
        self.assertEqual((row["verdict"], row["graded_verdict"], row["completed"]), ("tampered", "matched", True))
        self.assertEqual(row["tampered"], ["check.py", "include/u0.h"])
        self.assertEqual(decomp.counted(row), (False, False))
        value = gym.lane_priors(self.gym_dir, now=0)
        self.assertEqual((value["priors"], value["excluded"]), ({}, {"tampered": 1}))

    def test_resume_skips_completed_keys_and_the_budget_stops_before_the_next_run(self):
        self.write_task()
        self.run_gym(["matcher"])
        calls = len(self.grader_calls())
        self.assertEqual(self.run_gym(["matcher"])["runs"], [])
        self.assertEqual(len(self.grader_calls()), calls)
        result = self.run_gym(["idler", "cheater"], budget_usd=0.4, runner=CostlyRunner)
        self.assertEqual(result["status"], "budget_reached")
        [row] = result["runs"]
        self.assertEqual((row["lane"], row["cost_usd"], row["outcome"]["cost_usd"]), ("idler", 0.5, 0.5))
        self.assertFalse((self.root / "cheater.json").exists())

    def test_max_tasks_limits_the_tasks_run(self):
        self.write_task()
        self.write_task("t-0002")
        result = self.run_gym(["idler"], max_tasks=1)
        self.assertEqual([row["task"] for row in result["runs"]], ["t-0001"])

    def test_the_starting_tree_comes_from_the_repo_at_base_commit(self):
        source = self.root / "source"
        (source / "src").mkdir(parents=True)
        (source / "src" / "u0.c").write_text(START)
        run_git(source, "init", "-q")
        run_git(source, "add", ".")
        run_git(source, "commit", "-qm", "base")
        base = run_git(source, "rev-parse", "HEAD")
        (source / "src" / "u0.c").write_text(MATCHED)
        run_git(source, "commit", "-qam", "match fn_0001")
        self.write_task(start=False, prompt=False, base_commit=base)
        [row] = self.run_gym(["idler"], repo_path=source, keep_worktrees=True)["runs"]
        self.assertEqual((row["start"], row["verdict"]), ("repo", "unmatched"))
        seen = self.seen("idler")
        self.assertEqual(seen["files"], ["check.py", "src/u0.c"])
        self.assertNotIn("Target disassembly", seen["prompt"])
        kept = Path(row["worktree"])
        self.assertEqual(run_git(kept, "rev-list", "--count", "--all"), "1")
        self.assertEqual((kept / "src" / "u0.c").read_text(), START)
        with self.assertRaisesRegex(ValueError, "outside the source repository"):
            gym.run(self.tasks, ["idler"], source / "gym", kind="decomp", decomp={"repo_path": source})

    def test_a_task_without_a_starting_tree_is_skipped_with_the_reason(self):
        self.write_task(start=False, base_commit="c" * 40)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = core.main(["--quiet", "gym", "run", str(self.tasks), "--kind", "decomp", "--lanes", "matcher",
                              "--workspace", str(self.gym_dir), "--grader", self.grader])
        self.assertEqual(code, 0)
        self.assertIn("t-0001 skipped: no starting tree", out.getvalue())
        self.assertFalse((self.root / "matcher.json").exists())
        self.assertEqual(gym.read_results(self.gym_dir), [])

    def test_the_workflow_lets_the_worker_run_the_checker(self):
        spec = decomp.build_spec({"task_id": "t", "stratum": "S1", "target": {"functions": ["f"], "tu": "a.c"}},
                                 {"agent": "claude"}, ["a.c"])
        node = spec["nodes"][0]
        self.assertEqual((node["write"], node["verification_argv"]), (True, [["python3", "check.py"]]))
        self.assertIn("Unit source: a.c", node["task"])
        task = core.make_task(self.root, "claude", node["task"], "implementation", [], [], None, False, True,
                              settings_overrides={"command": "claude"})
        task["verification_argv"] = node["verification_argv"]
        argv, _, _ = core.agent_command({}, task, None)
        self.assertIn("Bash(python3 check.py:*)", argv)


if __name__ == "__main__":
    unittest.main()


class DecompReportTest(unittest.TestCase):
    def test_report_has_a_decomp_section_excluding_holds_and_tampered(self):
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path
        import fusion_gym as gym_api
        rows = [
            {"key": "t1:a:hidden:decomp", "verdict": "matched", "stratum": "S1", "cost_usd": 0.2, "duration_ms": 1000},
            {"key": "t2:a:hidden:decomp", "verdict": "unmatched", "stratum": "S2", "cost_usd": 0.1, "duration_ms": 3000},
            {"key": "t3:a:hidden:decomp", "verdict": "hold", "stratum": "S2", "cost_usd": 0.0, "duration_ms": 500},
            {"key": "t4:a:hidden:decomp", "verdict": "tampered", "stratum": "S1", "cost_usd": 0.3, "duration_ms": 500},
        ]
        with _tempfile.TemporaryDirectory() as directory:
            lines = []
            for row in rows:
                task = row["key"].split(":")[0]
                lines.append(_json.dumps({"schema": gym_api.RESULT_SCHEMA, "event": "finished", "kind": "decomp",
                                          "mode": "hidden", "lane": "a", "task": task, "completed": True, **row}))
            (_Path(directory) / "results.jsonl").write_text("\n".join(lines) + "\n")
            value = gym_api.report(directory)
            text = gym_api.table(value)
        lane = value["decomp"]["lanes"]["a"]
        self.assertEqual((lane["attempted"], lane["matched"], lane["holds"], lane["tampered"]), (2, 1, 1, 1))
        self.assertEqual(lane["match_rate"], 0.5)
        self.assertEqual(lane["strata"], {"S1": {"matched": 1, "attempted": 1}, "S2": {"matched": 0, "attempted": 1}})
        self.assertAlmostEqual(lane["cost_per_match"], 0.6)
        self.assertIn("== decomp", text)
