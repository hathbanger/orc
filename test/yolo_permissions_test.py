"""Exercise full-access launches using fixtures only; no provider calls."""
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
from fusion_policy import route_candidates


class YoloPermissionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        self.env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc-home"), "FUSION_TELEMETRY":"0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = core.deep_merge(core.DEFAULTS, {"execution_mode":"yolo", "routes":{}, "decisions":{"mode":"off"}})

    def task(self, agent, write):
        return core.make_task(self.workspace, agent, "Check the fixture", "review", [], [], None, True, write)

    def test_read_only_tasks_keep_their_read_only_mode_in_a_yolo_workspace(self):
        expected = {"codex": ("-s", "read-only"), "claude": ("--permission-mode", "plan"), "agy": ("--mode", "plan"),
                    "grok": ("--permission-mode", "plan")}
        for agent, (flag, value) in expected.items():
            for session in (None, "prior-session"):
                with self.subTest(agent=agent, resumed=bool(session)):
                    argv, _, _ = core.agent_command(self.config, self.task(agent, False), session)
                    self.assertEqual(argv[argv.index(flag) + 1], value)
                    for dangerous in ("--dangerously-bypass-approvals-and-sandbox", "--dangerously-skip-permissions", "bypassPermissions"):
                        self.assertNotIn(dangerous, argv)

    def test_all_workers_use_full_access_for_writing_and_resumed_tasks(self):
        for agent in ("codex", "claude", "agy", "grok"):
            for write in (True,):
                for session in (None, "prior-session"):
                    with self.subTest(agent=agent, write=write, resumed=bool(session)):
                        task = self.task(agent, write)
                        task["settings_overrides"] = {"sandbox":"read-only", "permission_mode":"plan", "mode":"plan", "allowed_tools":["Read"]}
                        argv, _, _ = core.agent_command(self.config, task, session)
                        self.assertNotIn("plan", argv)
                        self.assertNotIn("--allowedTools", argv)
                        self.assertNotIn("--permission-prompts", argv)
                        if agent == "codex":
                            self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
                            self.assertNotIn("-s", argv)
                            self.assertNotIn("-a", argv)
                            self.assertEqual("resume" in argv, bool(session))
                        elif agent == "grok":
                            self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")
                            self.assertEqual(argv[argv.index("--sandbox") + 1], "none")
                            self.assertIn("--no-plan", argv)
                        else:
                            self.assertIn("--dangerously-skip-permissions", argv)
                            self.assertNotIn("--sandbox", argv)
                            if agent == "claude":
                                self.assertFalse(json.loads(argv[argv.index("--settings") + 1])["sandbox"]["enabled"])
                            else:
                                self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits")

    def test_orc_route_cannot_reintroduce_a_saved_plan_mode(self):
        self.config["routes"] = {"external": {"agent":"claude", "command":"orc", "profile":"reviewer", "model":"provider/model", "permission_mode":"plan"}}
        task = self.task("claude", True)
        task["route"] = "external"
        argv, env, _ = core.agent_command(self.config, task, "existing")
        self.assertIn("@reviewer", argv)
        self.assertEqual(env["ORC_MODE"], "yolo")
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--permission-mode", argv)
        self.assertIn("--resume", argv)

    def test_interactive_and_headless_leads_use_the_same_full_access(self):
        for agent in ("codex", "claude"):
            self.config[agent]["command"] = sys.executable
            for interactive in (False, True):
                for read_only in (False, True):
                    with self.subTest(agent=agent, interactive=interactive, read_only=read_only), patch.object(core.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                        core.launch_lead(self.workspace, self.config, agent, "Check fixture", interactive, read_only)
                        argv = run.call_args.args[0]
                        dangerous = "--dangerously-bypass-approvals-and-sandbox" if agent == "codex" else "--dangerously-skip-permissions"
                        if read_only:
                            # A read-only lead stays read-only even in a YOLO workspace.
                            self.assertNotIn(dangerous, argv)
                            self.assertIn("read-only" if agent == "codex" else "plan", argv)
                        else:
                            self.assertIn(dangerous, argv)
                            self.assertNotIn("plan", argv)
                            self.assertNotIn("-s", argv)

    def test_machine_default_applies_across_workspaces_and_project_can_override(self):
        home = Path(os.environ["ORC_HOME"])
        home.mkdir()
        (home / "fusion.json").write_text(json.dumps({"execution_mode":"yolo"}))
        for workspace in (self.workspace, self.root / "another"):
            config, source = core.load_config(workspace)
            self.assertEqual(core.execution_mode(config), "yolo")
            self.assertEqual(source, home / "fusion.json")
        (self.workspace / ".fusion.json").write_text('{"execution_mode":"restricted"}')
        self.assertEqual(core.execution_mode(core.load_config(self.workspace)[0]), "restricted")

    def test_yolo_routing_drops_old_permission_cooldown_but_keeps_quota_cooldown(self):
        store = core.RunStore(self.workspace)
        traces = [{"agent":"agy", "failure_class":"permission_denied", "end_time_ms":core.now_ms()},
                  {"agent":"claude", "failure_class":"quota", "end_time_ms":core.now_ms()}]
        for agent in ("codex", "claude", "agy", "grok"):
            self.config[agent]["command"] = sys.executable
        with patch.object(store, "traces", return_value=traces), patch.object(core, "agy_headless_status", side_effect=AssertionError("YOLO must not require sandbox setup")):
            choices = route_candidates(self.config, self.task("auto", True), store)
        self.assertIn("agy", [c["agent"] for c in choices])
        self.assertIn("grok", [c["agent"] for c in choices])
        self.assertNotIn("claude", [c["agent"] for c in choices])

    def test_real_dispatch_uses_yolo_and_records_access_without_changing_task_scope(self):
        worker = self.root / "agy-fixture"
        worker.write_text(f"#!{sys.executable}\n" + '''import json, sys
assert '--dangerously-skip-permissions' in sys.argv
assert '--sandbox' not in sys.argv
assert sys.argv[sys.argv.index('--mode') + 1] == 'accept-edits'
print(json.dumps({'status':'SUCCESS','response':'STATUS: success\\nSUMMARY: checked\\nTESTS: fixture check passed\\nBLOCKERS: none'}))
''')
        worker.chmod(0o755)
        self.config["agy"]["command"] = str(worker)
        task = self.task("agy", True)
        result = core.dispatch(self.config, task, core.RunStore(self.workspace))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["execution_mode"], "yolo")
        self.assertTrue(task["write"])
        saved = json.loads(Path(result["artifacts"]["run_dir"], "task.json").read_text())
        self.assertEqual(saved["resolved"]["execution_mode"], "yolo")

    def test_a_read_only_dispatch_in_a_yolo_workspace_runs_and_records_restricted(self):
        worker = self.root / "agy-readonly-fixture"
        worker.write_text(f"#!{sys.executable}\n" + '''import json, sys
assert '--dangerously-skip-permissions' not in sys.argv
assert sys.argv[sys.argv.index('--mode') + 1] == 'plan'
print(json.dumps({'status':'SUCCESS','response':'STATUS: success\\nSUMMARY: checked\\nTESTS: none\\nBLOCKERS: none'}))
''')
        worker.chmod(0o755)
        self.config["agy"]["command"] = str(worker)
        result = core.dispatch(self.config, self.task("agy", False), core.RunStore(self.workspace))
        self.assertEqual(result["status"], "success", result.get("blockers"))
        self.assertEqual(result["execution_mode"], "restricted")


if __name__ == "__main__":
    unittest.main()


class AgySkipPermissionsTest(unittest.TestCase):
    """Restricted mode: only an explicit agy opt-out drops agy's sandbox."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc-home"), "FUSION_TELEMETRY": "0", "HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.config = core.deep_merge(core.DEFAULTS, {"routes": {}, "decisions": {"mode": "off"},
                                                      "agy": {"command": sys.executable}, "codex": {"command": sys.executable}})

    def argv(self, agent, overrides):
        task = core.make_task(self.root, agent, "Check the fixture", "review", [], [], None, False, True, settings_overrides=overrides)
        return core.agent_command(self.config, task, None)[0]

    def test_only_the_explicit_agy_flag_skips_its_sandbox(self):
        self.assertIn("--sandbox", self.argv("agy", {}))
        self.assertNotIn("--dangerously-skip-permissions", self.argv("agy", {"approval": "never"}))
        argv = self.argv("agy", {"dangerously_skip_permissions": True})
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--sandbox", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", self.argv("codex", {"approval": "never"}))

    def test_availability_and_routing_follow_the_flag_for_agy_only(self):
        self.assertFalse(core.worker_availability(self.config, "agy")["automatic_ready"])
        self.config["agy"]["dangerously_skip_permissions"] = True
        state = core.worker_availability(self.config, "agy")
        self.assertTrue(state["automatic_ready"])
        self.assertIn("without its sandbox", state["reason"])
        task = core.make_task(self.root, "auto", "Check the fixture", "review", [], [], None, False, False)
        self.assertIn("agy", [c["agent"] for c in route_candidates(self.config, task, core.RunStore(self.root))])
        self.config["codex"]["approval"] = "never"
        self.assertNotIn("sandbox", core.worker_availability(self.config, "codex")["reason"])
