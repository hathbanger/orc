"""Personal repos never leave the host: dispatch records the source repo, and
training exports, curation and gym priors drop excluded and unknown repos
unless explicitly included. Remote telemetry never carries the repo."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_gym as gym
import fusion_training_loop as loop
from fusion_decisions import DecisionStore, digest
from fusion_publish import save
import gym_test

OASIS = "git@github.com:402goose/Oasis.git"
QUESTIONS = {"plausible": {"type": "noul", "instructions": "Does the work satisfy the task?"}}


def git_repo(path, remote=None):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if remote:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    return path


class Isolated(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc-home"), "FUSION_TELEMETRY": "0",
                                      "FUSION_DECISIONS_MODE": "off"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FUSION_CONTROL_WORKSPACE", None)


class SlugTest(Isolated):
    def test_remote_urls_normalize_to_lowercase_owner_name(self):
        for url in ("git@github.com:402goose/Oasis.git", "git@github.com:402goose/oasis", "https://github.com/402Goose/oasis.git",
                    "https://github.com/402goose/oasis/", "ssh://git@github.com:22/402goose/OASIS.git"):
            self.assertEqual(core.remote_slug(url), "402goose/oasis", url)
        for url in ("", "/srv/git/oasis", "file:///srv/git/oasis.git", "oasis"):
            self.assertIsNone(core.remote_slug(url), url)

    def test_repo_slug_reads_origin_and_is_none_without_one(self):
        self.assertEqual(core.repo_slug(git_repo(self.root / "a", "https://github.com/402goose/tenet-work.git")), "402goose/tenet-work")
        self.assertIsNone(core.repo_slug(git_repo(self.root / "b")))
        self.assertIsNone(core.repo_slug(self.root / "missing"))
        self.assertIsNone(core.repo_slug(None))

    def test_excluded_repo_defaults_fail_closed_and_compares_case_insensitively(self):
        config = core.deep_merge(core.DEFAULTS, {})
        self.assertEqual(core.excluded_repo(config, "402Goose/OASIS"), "excluded repo 402goose/oasis")
        self.assertEqual(core.excluded_repo({}, "402goose/tenet-work"), "excluded repo 402goose/tenet-work")
        self.assertEqual(core.excluded_repo(config, None), "unknown repo")
        self.assertIsNone(core.excluded_repo(config, None, include_unknown=True))
        self.assertIsNone(core.excluded_repo(config, "402goose/oasis", ["402GOOSE/Oasis"]))
        self.assertIsNone(core.excluded_repo(config, "hathbanger/orc"))
        self.assertIsNone(core.excluded_repo({"export": {"exclude_repos": []}}, "402goose/oasis"))


class DispatchTest(Isolated):
    def test_dispatch_records_repo_locally_and_remote_telemetry_never_sends_it(self):
        workspace = git_repo(self.root / "oasis", OASIS)
        agent = self.root / "grok-fake"
        agent.write_text("#!/usr/bin/env python3\nprint('STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none')\n")
        agent.chmod(0o755)
        config = core.deep_merge(core.DEFAULTS, {"grok": {"command": str(agent), "output_format": "plain"},
                                                 "decisions": {"mode": "off"}})
        task = core.make_task(workspace, "grok", "Review fixture", "review", [], [], None, False, False)
        result = core.dispatch(config, task, core.RunStore(workspace))
        self.assertEqual(result["repo"], "402goose/oasis")
        run_dir = Path(result["artifacts"]["run_dir"])
        self.assertEqual(json.loads((run_dir / "task.json").read_text())["repo"], "402goose/oasis")
        span = json.loads((run_dir / "trace.json").read_text())
        self.assertEqual(span["repo"], "402goose/oasis")
        self.assertEqual(core.run_repo(workspace, result["run_id"]), "402goose/oasis")
        child = unittest.mock.MagicMock()
        with patch("fusion_core.subprocess.Popen", return_value=child), patch("fusion_core.os.set_blocking"), \
                contextlib.redirect_stderr(io.StringIO()):
            core._send_remote_telemetry({"endpoint": "https://example.invalid/v1/ingest"}, span)
        body = json.loads(child.stdin.write.call_args.args[0])
        self.assertNotIn("repo", body["payload"]["spans"][0])
        self.assertNotIn("oasis", json.dumps(body).lower())
        (workspace / ".fusion.json").write_text(json.dumps({"telemetry": {"remote": {"endpoint": "https://example.invalid/v1/ingest"}}}))
        os.environ.pop("FUSION_TELEMETRY")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            core.main(["--workspace", str(workspace), "--json", "telemetry", "status"])
        status = json.loads(out.getvalue())
        self.assertTrue(status["fields_sent"])
        self.assertNotIn("repo", status["fields_sent"])
        self.assertIn("source repo", status["fields_never_sent"])

    def test_run_repo_falls_back_to_the_workspace_remote(self):
        workspace = git_repo(self.root / "w", "https://github.com/acme/widgets.git")
        save(workspace / ".fusion/runs/r1/task.json", {"run_id": "r1", "workspace": str(workspace)})
        save(workspace / ".fusion/runs/r2/task.json", {"run_id": "r2", "workspace": str(self.root / "gone")})
        self.assertEqual(core.run_repo(workspace, "r1"), "acme/widgets")
        self.assertIsNone(core.run_repo(workspace, "r2"))
        self.assertIsNone(core.run_repo(workspace, "r3"))
        self.assertIsNone(core.run_repo(workspace, "../r1"))


class ExportTest(Isolated):
    def setUp(self):
        super().setUp()
        self.workspace = git_repo(self.root / "control", "https://github.com/hathbanger/orc.git")
        (self.workspace / ".fusion.json").write_text(json.dumps({"decisions": {"mode": "off"},
                                                                 "telemetry": {"remote": {"enabled": False}}}))
        self.store = DecisionStore(self.workspace)
        for key, repo in (("oasis", "402goose/oasis"), ("orc", "hathbanger/orc"), ("unknown", None)):
            if repo:
                save(self.workspace / ".fusion/runs" / f"run-{key}/task.json", {"run_id": f"run-{key}", "repo": repo})
            self.store.append("decision", id=key, kind="acceptance", mode="shadow", status="ok", truncated=False,
                              state="Outcome " + key, questions=QUESTIONS, schema_hash=digest(QUESTIONS), prediction={},
                              context={"group": key, "task_id": f"run-{key}"})
            self.store.label(key, {"plausible": "true"}, "Verified outcome", replace=True)

    def export(self, name, *flags):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(core.main(["--workspace", str(self.workspace), "decisions", "export",
                                        str(self.root / name), *flags]), 0)
        payload = json.loads(out.getvalue())
        rows = [json.loads(line) for line in (self.root / name).read_text().splitlines()]
        return payload, sorted(row["id"] for row in rows)

    def test_export_drops_excluded_and_unknown_repos_by_default_and_reports_why(self):
        payload, ids = self.export("default.jsonl")
        self.assertEqual(ids, ["orc"])
        self.assertEqual(payload["examples"], 1)
        self.assertEqual(payload["excluded_repos"], {"excluded repo 402goose/oasis": 1, "unknown repo": 1})

    def test_flags_include_named_repos_and_unknown_ones(self):
        payload, ids = self.export("oasis.jsonl", "--include-repo", "402GOOSE/Oasis")
        self.assertEqual((ids, payload["excluded_repos"]), (["oasis", "orc"], {"unknown repo": 1}))
        payload, ids = self.export("all.jsonl", "--include-repo", "402goose/oasis", "--include-unknown")
        self.assertEqual((ids, payload["excluded_repos"]), (["oasis", "orc", "unknown"], {}))

    def test_curation_withholds_excluded_rows_whatever_the_export_allowed(self):
        raw = self.root / "raw.jsonl"
        self.store.export(raw, include_repos=["402goose/oasis"], include_unknown=True)
        withheld = loop.repo_exclusions(self.workspace, {}, [json.loads(line) for line in raw.read_text().splitlines()])
        self.assertEqual(withheld, {"oasis": "excluded repo 402goose/oasis", "unknown": "unknown repo"})


class GymPriorsTest(gym_test.Isolated):
    extract = gym_test.GymTest.extract

    def setUp(self):
        super().setUp()
        self.repo, self.tasks, self.gym_dir = self.root / "source", self.root / "tasks", self.root / "gym"
        (self.repo / "test").mkdir(parents=True)
        (self.repo / "calc.py").write_text(gym_test.BUGGY)
        (self.repo / "test/calc_test.py").write_text(gym_test.TEST_BASE)
        (self.repo / ".gitignore").write_text(".fusion/\n.fusion.json\n__pycache__/\n")
        gym_test.run_git(self.repo, "init", "-q")
        gym_test.run_git(self.repo, "remote", "add", "origin", OASIS)
        gym_test.run_git(self.repo, "add", ".")
        gym_test.run_git(self.repo, "commit", "-qm", "seed")
        (self.repo / "calc.py").write_text(gym_test.FIXED)
        (self.repo / "test/calc_test.py").write_text(gym_test.TEST_FIX)
        gym_test.run_git(self.repo, "commit", "-qam", "fix(calc): add keeps the sign of a negative first operand (#7)")
        (self.repo / ".fusion.json").write_text(json.dumps({
            "claude": {"command": "missing-claude"}, "agy": {"command": "missing-agy"}, "grok": {"command": "missing-grok"},
            "timeout_seconds": 60, "decisions": {"mode": "shadow"},
            "routes": {"fixer": {"agent": "codex", "command": self.worker("fixer", {"calc.py": gym_test.FIXED})}}}))

    def test_extract_records_repo_and_priors_skip_an_oasis_task_unless_included(self):
        summary, rows = self.extract()
        self.assertEqual((rows[7]["repo"], summary["repo"]), ("402goose/oasis", "402goose/oasis"))
        self.assertEqual(json.loads((self.tasks / "index.json").read_text())["repo"], "402goose/oasis")
        self.assertEqual(json.loads((self.tasks / "pr-7.json").read_text())["repo"], "402goose/oasis")
        [row] = gym.run(self.tasks, ["fixer"], self.gym_dir, mode="hidden")["runs"]
        self.assertEqual((row["verdict"], row["repo"]), ("solved", "402goose/oasis"))
        value = gym.lane_priors(self.gym_dir)
        self.assertEqual((value["priors"], value["repos"]), ({}, []))
        self.assertEqual(value["excluded"], {"excluded repo 402goose/oasis": 1})
        value = gym.lane_priors(self.gym_dir, include_repos=["402goose/oasis"])
        self.assertEqual(value["repos"], ["402goose/oasis"])
        self.assertEqual(value["priors"]["fixer"]["write"]["successes"], 1)
        args = core.build_parser().parse_args(["gym", "priors", str(self.gym_dir), "--out", "-",
                                               "--include-repo", "402goose/oasis"])
        out = io.StringIO()
        gym.command(args, self.repo, as_json=True, out=out)
        self.assertEqual(json.loads(out.getvalue())["repos"], ["402goose/oasis"])

    def test_priors_skip_rows_without_a_recorded_repo_unless_unknown_is_included(self):
        gym_test_rows = [{"event": "finished", "key": "pr-1:agy:hidden", "task": "pr-1", "lane": "agy",
                          "lane_spec": {"agent": "agy"}, "kind": "fix", "mode": "hidden", "verdict": "solved",
                          "completed": True, "cost_usd": 0.0, "duration_ms": 1000}]
        (self.root / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in gym_test_rows))
        self.assertEqual(gym.lane_priors(self.root)["excluded"], {"unknown repo": 1})
        value = gym.lane_priors(self.root, include_unknown=True)
        self.assertEqual((sorted(value["priors"]), value["repos"]), (["agy"], ["unknown"]))


if __name__ == "__main__":
    unittest.main()
