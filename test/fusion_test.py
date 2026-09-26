import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fusion_core  # noqa: E402
from fusion_workflow import (  # noqa: E402
    WorkflowRunner, run_workflow, resume_workflow, workflow_report,
    parse_acceptance_contract, MAX_CONTRACT_FILES,
)


class FusionHarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # Remote telemetry is on by default and points at the live collector, so
        # the suite must opt out or `make test` would report from every machine
        # that runs it. ORC_HOME is redirected too, to keep the install id and
        # the first-send notice out of the real one.
        environment = patch.dict(os.environ, {
            "FUSION_DECISIONS_MODE": "off",
            "FUSION_TELEMETRY": "0",
            "ORC_HOME": str(Path(self.temp.name) / "orc-home"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.workspace = Path(self.temp.name) / "repo"
        self.workspace.mkdir()
        self.bin_dir = Path(self.temp.name) / "bin"
        self.bin_dir.mkdir()
        self.calls = Path(self.temp.name) / "calls.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def allow_telemetry(self):
        """Undo setUp's opt-out for the tests that exercise sending."""
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("FUSION_TELEMETRY", None)

    def write_agent(self, name: str, body: str) -> Path:
        path = self.bin_dir / name
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def config(self, codex: Path | None = None, claude: Path | None = None, agy: Path | None = None) -> None:
        value = {
            "codex": {"command": str(codex or "missing-codex")},
            "claude": {"command": str(claude or "missing-claude")},
            "agy": {"command": str(agy or "missing-agy")},
            "grok": {"command": "missing-fusion-test-grok"},
            "timeout_seconds": 30,
        }
        (self.workspace / ".fusion.json").write_text(json.dumps(value), encoding="utf-8")

    def test_build_launches_interactive_lead_with_feature_and_fusion_tools(self):
        idea = 'Export "filtered rows" as CSV; preserve $labels and `literal text`.\nInclude empty results.'
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                harness = self.write_agent(
                    f"{agent}-lead",
                    f"""
import json, os, pathlib, sys
argv = sys.argv[1:]
payload = {{'argv': argv, 'cwd': os.getcwd(), 'workspace': os.environ.get('FUSION_WORKSPACE')}}
if '--mcp-config' in argv:
    payload['mcp'] = json.loads(pathlib.Path(argv[argv.index('--mcp-config') + 1]).read_text())
pathlib.Path({str(self.calls)!r}).write_text(json.dumps(payload))
sys.exit(7)
""",
                )
                self.config(**{agent: harness})
                config_path = self.workspace / ".fusion.json"
                original_config = config_path.read_bytes()
                # Exercise the default Codex lead and explicit Claude override
                # through the CLI, without invoking a real provider.
                args = [] if agent == "codex" else ["--agent", agent]
                proc = subprocess.run(
                    [sys.executable, str(ROOT / "fusion"), "--workspace", str(self.workspace), "build", *args, idea],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 7, proc.stderr)
                call = json.loads(self.calls.read_text())
                self.assertEqual(Path(call["cwd"]).resolve(), self.workspace.resolve())
                self.assertEqual(Path(call["workspace"]).resolve(), self.workspace.resolve())
                argv = call["argv"]
                self.assertIn(idea, argv[-1])
                self.assertIn("Prepared workflow:", argv[-1])
                self.assertIn("fusion_delegate", argv[-1])
                self.assertEqual(config_path.read_bytes(), original_config)
                if agent == "codex":
                    self.assertNotIn("exec", argv)
                    self.assertIn("mcp_servers.fusion.command=", " ".join(argv))
                    self.assertIn("mcp-serve", " ".join(argv))
                    self.assertEqual(argv[argv.index("-a") + 1], "on-request")
                else:
                    self.assertNotIn("-p", argv)
                    self.assertIn("--append-system-prompt", argv)
                    self.assertEqual(call["mcp"]["mcpServers"]["fusion"]["args"][-1], "mcp-serve")
                    self.assertFalse(Path(argv[argv.index("--mcp-config") + 1]).exists())

    def test_build_rejects_blank_idea_before_launch(self):
        harness = self.write_agent(
            "unused-lead",
            f"import pathlib\npathlib.Path({str(self.calls)!r}).touch()\n",
        )
        self.config(codex=harness)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "fusion"), "--workspace", str(self.workspace), "build", " \n "],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("feature idea must not be empty", proc.stderr)
        self.assertFalse(self.calls.exists())

    def test_codex_result_is_structured_and_session_is_resumed(self):
        calls = str(self.calls)
        codex = self.write_agent(
            "codex-fake",
            f"""
import json, pathlib, sys
pathlib.Path({calls!r}).open('a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')
prompt = sys.stdin.read()
print(json.dumps({{'type':'thread.started','thread_id':'thread-123'}}))
print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':'STATUS: success\\nSUMMARY: worker completed'}}}}))
print(json.dumps({{'type':'turn.completed','usage':{{'input_tokens':12,'output_tokens':4}}}}))
""",
        )
        self.config(codex=codex)

        first = io.StringIO()
        with contextlib.redirect_stdout(first):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "codex", "--role", "implementation", "do work"]
                ),
                0,
            )
        first_result = json.loads(first.getvalue())
        self.assertEqual(first_result["status"], "success")
        self.assertEqual(json.loads((self.workspace / ".fusion" / "sessions.json").read_text())["codex:implementation"], "thread-123")

        second = io.StringIO()
        with contextlib.redirect_stdout(second):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "codex", "--role", "implementation", "follow up"]
                ),
                0,
            )
        self.assertEqual(json.loads(second.getvalue())["status"], "success")
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertIn("resume", calls[1])
        self.assertIn("thread-123", calls[1])

    def test_normalized_usage_maps_codex_cache_write_input_tokens(self):
        # Matches the real `codex exec --json` turn.completed usage shape
        # (verified live, post quota-reset): cache_write_input_tokens, not
        # cache_creation_input_tokens or a cache_creation sub-object. This is
        # applied when aggregating (usage_summary/fusion usage) and when
        # sending remote telemetry -- the raw per-dispatch result keeps the
        # provider's own field names unnormalized.
        normalized = fusion_core.normalized_usage({
            "input_tokens": 28442,
            "cached_input_tokens": 26112,
            "cache_write_input_tokens": 512,
            "output_tokens": 72,
            "reasoning_output_tokens": 0,
        })
        self.assertEqual(normalized["cache_creation_input_tokens"], 512)

    def quota_workflow_fixture(self, max_attempts):
        codex = self.write_agent("fallback-codex", f'''
import json, pathlib, sys
prompt = sys.stdin.read()
role = 'implement' if 'Role: implementation' in prompt else 'review'
with pathlib.Path({str(self.calls)!r}).open('a') as out:
    out.write(json.dumps({{'role': role, 'argv': sys.argv[1:]}}) + '\\n')
print(json.dumps({{'type':'thread.started','thread_id': role + '-session'}}))
print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':'STATUS: success\\nSUMMARY: fixture complete\\nCHANGED: none\\nTESTS: fixture verification passed\\nBLOCKERS: none'}}}}))
''')
        claude = self.write_agent("fallback-claude", '''
import json
print(json.dumps({'type':'result','is_error':True,'session_id':'exhausted-claude-session','result':'API usage limit reached; resets at next month'}))
''')
        self.config(codex=codex, claude=claude)
        config, _ = fusion_core.load_config(self.workspace)
        config["routes"] = {}
        path = self.workspace / "workflow.json"
        path.write_text(json.dumps({"max_attempts": max_attempts, "nodes": [
            {"id":"implement", "agent":"codex", "write":True, "role":"implementation", "task":"Implement the fixture"},
            {"id":"review", "agent":"auto", "write":False, "role":"review", "needs":["implement"],
             "independent_of":"implement", "task":"Independently verify the fixture", "acceptance":{"required_handoff":["tests"]}},
        ]}))
        return config, path

    def test_automatic_quota_fallback_works_without_active_laya(self):
        config, path = self.quota_workflow_fixture(2)
        result = run_workflow(self.workspace, config, path)
        self.assertEqual(result["status"], "success")
        review = next(n for n in result["nodes"] if n["id"] == "review")
        self.assertEqual(review["attempts"], 2)
        self.assertEqual(review["result"]["agent"], "codex")
        self.assertEqual(review["excluded_routes"], ["claude"])
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([c["role"] for c in calls], ["implement", "review"])
        argv = calls[-1]["argv"]
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertNotIn("resume", argv)

    def test_resume_can_switch_only_the_unfinished_stage_and_preserves_attempts(self):
        config, path = self.quota_workflow_fixture(1)
        first = run_workflow(self.workspace, config, path)
        self.assertEqual(first["status"], "paused_quota")
        run_id = first["workflow_id"]
        with self.assertRaisesRegex(ValueError, "attempt limit"):
            resume_workflow(self.workspace, config, run_id, node_id="review", agent="codex")
        with self.assertRaisesRegex(ValueError, "accepted stages"):
            resume_workflow(self.workspace, config, run_id, node_id="implement", agent="claude", max_attempts=2)
        result = resume_workflow(self.workspace, config, run_id, node_id="review", agent="codex", max_attempts=2)
        self.assertEqual(result["status"], "success")
        review = next(n for n in result["nodes"] if n["id"] == "review")
        self.assertEqual(review["attempts"], 2)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([c["role"] for c in calls], ["implement", "review"])
        self.assertNotIn("exhausted-claude-session", calls[-1]["argv"])

    def test_grok_headless_review_uses_plan_mode_and_reports_unknown_usage(self):
        grok = self.write_agent("grok-fake", f'''
import json, pathlib, sys
pathlib.Path({str(self.calls)!r}).write_text(json.dumps(sys.argv[1:]))
print('STATUS: success\\nSUMMARY: Grok review complete\\nCHANGED: none\\nTESTS: fixture check passed\\nBLOCKERS: none')
''')
        config = fusion_core.deep_merge(fusion_core.DEFAULTS, {"grok":{"command":str(grok), "output_format":"plain"}})
        task = fusion_core.make_task(self.workspace, "grok", "Review fixture", "review", [], [], None, False, False)
        result = fusion_core.dispatch(config, task, fusion_core.RunStore(self.workspace))
        self.assertEqual(result["status"], "success")
        argv = json.loads(self.calls.read_text())
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--output-format") + 1], "plain")
        self.assertIn("--no-subagents", argv)
        self.assertEqual(result["usage"], {})

    def test_resume_auto_excludes_permission_denied_worker_and_preserves_implementation(self):
        config, path = self.quota_workflow_fixture(2)
        agy = self.write_agent("agy-headless-denied", '''
import json
print(json.dumps({'status':'SUCCESS','response':'','denied_actions':[{'action':'command','display_name':'RunCommand'}]}))
''')
        config["agy"]["command"] = str(agy)
        config["agy"]["dangerously_skip_permissions"] = True
        with patch.object(fusion_core, "agy_headless_status", return_value={"automatic_ready": True}):
            first = run_workflow(self.workspace, config, path)
            self.assertEqual(first["status"], "failed")
            review = next(n for n in first["nodes"] if n["id"] == "review")
            self.assertEqual(review["result"]["agent"], "agy")
            self.assertEqual(fusion_core.failure_class(review["result"]), "permission_denied")
            # Old manifests misclassified this error. Re-read their original
            # blocker text on resume; do not rely on the old trace category.
            result = resume_workflow(self.workspace, config, first["workflow_id"],
                                     node_id="review", agent="auto", max_attempts=3)
        self.assertEqual(result["status"], "success")
        review = next(n for n in result["nodes"] if n["id"] == "review")
        self.assertEqual(review["attempts"], 3)
        self.assertEqual(review["excluded_routes"], ["claude", "agy"])
        self.assertEqual(review["result"]["agent"], "codex")
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([c["role"] for c in calls], ["implement", "review"])
        self.assertNotIn("resume", calls[-1]["argv"])
        self.assertEqual(calls[-1]["argv"][calls[-1]["argv"].index("-s") + 1], "read-only")

    def test_agy_headless_setup_check_does_not_mutate_permissions(self):
        home = self.workspace / "home"
        path = home / ".gemini/antigravity-cli/settings.json"
        path.parent.mkdir(parents=True)
        with patch.object(Path, "home", return_value=home):
            self.assertFalse(fusion_core.agy_headless_status({})["automatic_ready"])
            path.write_text(json.dumps({"toolPermission":"proceed-in-sandbox",
                                        "permissions":{"deny":["command(git push)"]}}))
            original = path.read_bytes()
            self.assertTrue(fusion_core.agy_headless_status({})["automatic_ready"])
            self.assertFalse(fusion_core.agy_headless_status({"sandbox":False})["automatic_ready"])
            self.assertEqual(path.read_bytes(), original)
            path.write_text('[]')
            self.assertFalse(fusion_core.agy_headless_status({})["automatic_ready"])

    def test_explicit_permission_retry_starts_a_fresh_session(self):
        config, path = self.quota_workflow_fixture(1)
        denied = self.write_agent("agy-denied-then-ready", f'''
import json, pathlib, sys
with pathlib.Path({str(self.calls)!r}).open('a') as out:
    out.write(json.dumps({{'role':'agy', 'argv':sys.argv[1:]}}) + '\\n')
print(json.dumps({{'conversation_id':'denied-session', 'status':'SUCCESS', 'response':'', 'denied_actions':[{{'action':'command'}}]}}))
''')
        config["agy"]["command"] = str(denied)
        spec = json.loads(path.read_text())
        spec["nodes"][1]["agent"] = "agy"
        path.write_text(json.dumps(spec))
        first = run_workflow(self.workspace, config, path)
        self.assertEqual(first["status"], "failed")
        denied.write_text(denied.read_text().replace(
            "'response':'', 'denied_actions':[{'action':'command'}]",
            "'response':'STATUS: success\\nSUMMARY: checked\\nTESTS: checks passed\\nBLOCKERS: none'"))
        result = resume_workflow(self.workspace, config, first["workflow_id"],
                                 node_id="review", agent="agy", max_attempts=2)
        self.assertEqual(result["status"], "success")
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([c["role"] for c in calls], ["implement", "agy", "agy"])
        self.assertNotIn("--conversation", calls[-1]["argv"])

    def test_codex_command_execution_failure_is_surfaced_as_evidence(self):
        # Nonzero command exits stay visible without becoming unresolved
        # blockers. Real provider failures and handoff blockers remain fatal.
        codex = self.write_agent(
            "codex-command-failure",
            """
import json
print(json.dumps({'type':'thread.started','thread_id':'evidence-thread'}))
print(json.dumps({'type':'item.completed','item':{'id':'item_0','type':'command_execution','command':'pytest','aggregated_output':'','exit_code':1,'status':'completed'}}))
print(json.dumps({'type':'item.completed','item':{'id':'item_1','type':'command_execution','command':'ls','aggregated_output':'','exit_code':0,'status':'completed'}}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'STATUS: success\\nSUMMARY: all good\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
""",
        )
        self.config(codex=codex)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "codex", "--role", "implementation", "run tests"]
                ),
                0,
            )
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["blockers"], [])
        self.assertEqual(len(result["command_evidence"]), 1)
        self.assertIn("command exited 1", result["command_evidence"][0])
        self.assertIn("pytest", result["command_evidence"][0])

    def write_node_workflow(self, *, writes, acceptance=None, write=True):
        """One node that either touches a file or does not, and reports success."""
        body = ("open('made.txt','w').write('x')\n" if writes else "")
        handoff = ("STATUS: success\\nSUMMARY: No implementation was performed\\n"
                   "CHANGED: none\\nTESTS: not run\\nBLOCKERS: none")
        codex = self.write_agent("codex-write", (
            body +
            "import json\n"
            f"print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':'{handoff}'}}}}))\n"
            "print(json.dumps({'type':'turn.completed','usage':{}}))\n"
        ))
        self.config(codex=codex)
        node = {"id": "implement", "role": "implementation", "agent": "codex",
                "write": write, "task": "Implement it",
                "acceptance": acceptance if acceptance is not None else {"required_handoff": ["summary", "tests"]}}
        spec = {"task": "build", "max_attempts": 1, "nodes": [node]}
        path = self.workspace / "writenode.json"
        path.write_text(json.dumps(spec))
        return run_workflow(self.workspace, json.loads((self.workspace / ".fusion.json").read_text()), path)

    def git_workspace(self):
        """The gate reads the repository, so the fixture needs to be one."""
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True)
        (self.workspace / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "add", "seed.txt"], cwd=self.workspace, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "seed"], cwd=self.workspace, check=True)

    CONTRACT_WORKER = '''import json, sys
prompt = sys.stdin.read()
is_plan = "node plan" in prompt
if not is_plan and CREATE:
    open("hello.py", "w").write("print(1)\\n")
contract = ("```acceptance-contract\\n"
            '{"required_files": ["hello.py"], "verification": ["pytest -q"]}\\n'
            "```")
plan = "STATUS: success\\nSUMMARY: planned\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none\\n\\n" + contract
impl = "STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none"
msg = plan if is_plan else impl
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": msg}}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
'''

    def contract_workflow(self, *, implement_creates):
        """plan declares an acceptance contract; implement may or may not honour it."""
        script = f"CREATE = {implement_creates!r}\n" + self.CONTRACT_WORKER
        codex = self.write_agent("codex-contract", script)
        self.config(codex=codex)
        self.git_workspace()
        spec = {"task": "build", "max_attempts": 1, "nodes": [
            {"id": "plan", "role": "planning", "agent": "codex", "write": False,
             "task": "Plan it", "acceptance": {"required_handoff": ["summary"]}},
            {"id": "implement", "role": "implementation", "agent": "codex", "write": True,
             "needs": ["plan"], "task": "Implement it",
             "acceptance": {"required_handoff": ["summary"]}},
        ]}
        path = self.workspace / "contract.json"
        path.write_text(json.dumps(spec))
        return run_workflow(self.workspace, json.loads((self.workspace / ".fusion.json").read_text()), path)

    def test_the_plan_contract_gates_the_implementation(self):
        """The generated path's missing link.

        Every generated node got the same two-field handoff check, so nothing
        connected what the plan said to build with what the implementation
        actually produced. The plan names hello.py; the implementation does
        not create it and must not be accepted.
        """
        outcome = self.contract_workflow(implement_creates=False)
        self.assertEqual(outcome["status"], "failed")
        blockers = " ".join(outcome["nodes"][1]["result"]["blockers"])
        self.assertIn("hello.py", blockers)

    def test_honouring_the_contract_passes(self):
        outcome = self.contract_workflow(implement_creates=True)
        self.assertEqual(outcome["status"], "success", outcome)
        self.assertTrue((self.workspace / "hello.py").exists())

    def test_a_reviewer_does_not_inherit_the_contract(self):
        """Only the node that writes is gated on producing the artifacts.

        A reviewer is not the one creating them, so inheriting would blame the
        wrong node for work that never happened upstream.
        """
        node = {"id": "review", "write": False, "needs": ["plan"]}
        self.assertEqual(WorkflowRunner._inherited_required_files.__get__(
            type("R", (), {"nodes": {"plan": {"_contract": {"required_files": ["x.py"]}}}})()
        )(node), [])

    def test_a_write_node_that_changes_nothing_is_not_success(self):
        """The primary path's whole promise.

        `required_handoff` only asks whether a field is non-empty, so a worker
        answering "TESTS: not run" satisfies it, and generated builds declare
        no required_files and no acceptance checks. A node whose summary read
        "No implementation was performed" was accepted as success.
        """
        self.git_workspace()
        outcome = self.write_node_workflow(writes=False)
        self.assertEqual(outcome["status"], "failed")
        self.assertIn("write node finished without changing any file",
                      outcome["nodes"][0]["result"]["blockers"])

    def test_a_workspace_without_git_is_not_blocked_by_the_gate(self):
        """Unavailable is not evidence of no change.

        The gate compares the repository against its pre-dispatch tree. Where
        there is no repository there is no tree, and treating that as "nothing
        changed" would fail every write node in a non-git workspace. The other
        gates still apply; this one abstains.
        """
        outcome = self.write_node_workflow(writes=False)   # deliberately no git init
        self.assertEqual(outcome["status"], "success", outcome)
        self.assertNotIn("write node finished without changing any file",
                         outcome["nodes"][0]["result"].get("blockers") or [])

    def test_a_write_node_that_changes_a_file_is_accepted(self):
        self.git_workspace()
        outcome = self.write_node_workflow(writes=True)
        self.assertEqual(outcome["status"], "success", outcome)

    def test_a_read_only_node_may_legitimately_change_nothing(self):
        # Discovery and review nodes are supposed to leave the tree alone.
        self.git_workspace()
        outcome = self.write_node_workflow(writes=False, write=False)
        self.assertEqual(outcome["status"], "success", outcome)

    def test_allow_no_changes_opts_a_write_node_out(self):
        # A writer asked to fix something already fixed has nothing to write.
        self.git_workspace()
        outcome = self.write_node_workflow(
            writes=False,
            acceptance={"required_handoff": ["summary"], "allow_no_changes": True},
        )
        self.assertEqual(outcome["status"], "success", outcome)

    def test_workflow_command_observations_do_not_override_acceptance(self):
        for case in ("recovered", "unresolved", "failed_check", "missing_tests", "provider_error"):
            with self.subTest(case=case):
                handoff = ("STATUS: success\nSUMMARY: reviewed\nCHANGED: none\n"
                           f"TESTS: {'none' if case == 'missing_tests' else 'tests passed'}\n"
                           f"BLOCKERS: {'tests still fail' if case == 'unresolved' else 'none'}")
                events = [
                    {"type": "item.completed", "item": {"type": "command_execution",
                     "command": "cat incorrect-path", "exit_code": 1}},
                    {"type": "item.completed", "item": {"type": "command_execution",
                     "command": "cat correct-path", "exit_code": 0}},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": handoff}},
                    {"type": "turn.failed", "error": {"message": "provider unavailable"}}
                    if case == "provider_error" else {"type": "turn.completed"},
                ]
                codex = self.write_agent("codex-observations", f"print({chr(10).join(json.dumps(e) for e in events)!r})\n")
                self.config(codex=codex)
                spec = {"task": "Review with recovered lookup", "max_attempts": 1,
                        "nodes": [{"id": "review", "role": "review", "agent": "codex", "task": "Review",
                                   "acceptance": {"required_handoff": ["summary", "tests"],
                                                  "checks": [[sys.executable, "-c", f"raise SystemExit({1 if case == 'failed_check' else 0})"]]}}]}
                path = self.workspace / "observations.json"
                path.write_text(json.dumps(spec))
                outcome = run_workflow(self.workspace, json.loads((self.workspace / ".fusion.json").read_text()), path)
                self.assertEqual(outcome["status"], "success" if case == "recovered" else "failed")
                result = outcome["nodes"][0]["result"]
                self.assertEqual(result["command_evidence"], ["command exited 1: cat incorrect-path"])
                expected = {"unresolved": "tests still fail", "failed_check": "acceptance check failed",
                            "missing_tests": "required handoff field is empty: tests", "provider_error": "provider unavailable"}
                if case in expected:
                    self.assertIn(expected[case], "\n".join(result["blockers"]))
                else:
                    self.assertEqual(result["blockers"], [])

    def test_claude_json_result_is_structured(self):
        claude = self.write_agent(
            "claude-fake",
            """
import json
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'claude-123','result':'STATUS: success\\nSUMMARY: reviewed\\nCHANGED: src/app.py, README.md\\nTESTS: python -m unittest\\nBLOCKERS: none'}))
""",
        )
        self.config(claude=claude)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "claude", "--read-only", "review it"]
                ),
                0,
            )
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["summary"], "reviewed")
        self.assertEqual(result["changed"], ["src/app.py", "README.md"])
        self.assertEqual(result["tests"], ["python -m unittest"])
        self.assertEqual(result["blockers"], [])

    def test_claude_json_result_extracts_model_and_cost_from_real_cli_shape(self):
        # Matches the actual shape of `claude -p --output-format json` output:
        # no top-level model/model_id, cost as total_cost_usd (not inside
        # usage), and the model name as a key under modelUsage.
        claude = self.write_agent(
            "claude-real-shape",
            """
import json
print(json.dumps({
    'type': 'result',
    'subtype': 'success',
    'is_error': False,
    'session_id': 'real-shape-session',
    'result': 'STATUS: success\\nSUMMARY: did real work\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none',
    'total_cost_usd': 0.183226,
    'usage': {'input_tokens': 10, 'output_tokens': 2003, 'cache_creation_input_tokens': 31076, 'cache_read_input_tokens': 194360},
    'modelUsage': {'claude-sonnet-5': {'inputTokens': 10, 'outputTokens': 2003, 'costUSD': 0.183226}},
}))
""",
        )
        self.config(claude=claude)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "claude", "--read-only", "do real work"]
                ),
                0,
            )
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["model"], "claude-sonnet-5")
        self.assertEqual(result["usage"]["cost_usd"], 0.183226)
        self.assertEqual(result["usage"]["input_tokens"], 10)

    def test_agy_result_is_structured_and_session_is_resumed(self):
        calls = str(self.calls)
        agy = self.write_agent(
            "agy-fake",
            f"""
import json, pathlib, sys
pathlib.Path({calls!r}).open('a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')
print(json.dumps({{'conversation_id':'conv-123','status':'SUCCESS','response':'STATUS: success\\nSUMMARY: agy completed\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none','num_turns':1,'usage':{{'input_tokens':20,'output_tokens':5,'thinking_tokens':3,'cache_read_tokens':10}}}}))
""",
        )
        self.config(agy=agy)

        first = io.StringIO()
        with contextlib.redirect_stdout(first):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "agy", "--read-only", "--fresh", "--role", "investigator", "inspect"]
                ),
                0,
            )
        first_result = json.loads(first.getvalue())
        self.assertEqual(first_result["status"], "success")
        self.assertEqual(first_result["summary"], "agy completed")
        self.assertEqual(first_result["usage"]["cache_read_input_tokens"], 10)
        self.assertEqual(first_result["usage"]["reasoning_output_tokens"], 3)
        self.assertEqual(json.loads((self.workspace / ".fusion" / "sessions.json").read_text())["agy:investigator"], "conv-123")

        argv = json.loads(self.calls.read_text().splitlines()[0])
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)
        self.assertIn("--mode", argv)
        self.assertIn("plan", argv)
        self.assertIn("--sandbox", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)

        second = io.StringIO()
        with contextlib.redirect_stdout(second):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "agy", "--read-only", "--role", "investigator", "follow up"]
                ),
                0,
            )
        self.assertEqual(json.loads(second.getvalue())["status"], "success")
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertIn("--conversation", calls[1])
        self.assertIn("conv-123", calls[1])

    def test_agy_denied_tools_and_failure_are_reported(self):
        agy = self.write_agent(
            "agy-denied",
            """
import json
print(json.dumps({'conversation_id':'conv-denied','status':'SUCCESS','response':'','denied_actions':[{'action':'command','display_name':'RunCommand'}],'usage':{'input_tokens':9,'output_tokens':1}}))
""",
        )
        self.config(agy=agy)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "agy", "--fresh", "run something"]
                ),
                1,
            )
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "error")
        self.assertIn("auto-denied", " ".join(result["blockers"]))
        self.assertIn("RunCommand", " ".join(result["blockers"]))
        self.assertEqual(fusion_core.failure_class(result), "permission_denied")

    def test_agy_denial_alongside_success_text_is_still_surfaced(self):
        # Unlike the no-text case above, a denial alongside an otherwise
        # successful turn used to be silently dropped since the old failure
        # branch only fired when there was no other text.
        agy = self.write_agent(
            "agy-partial-denied",
            """
import json
print(json.dumps({'conversation_id':'conv-partial','status':'SUCCESS','response':'STATUS: success\\nSUMMARY: did the work anyway\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none','denied_actions':[{'action':'command','display_name':'RunCommand'}],'usage':{'input_tokens':9,'output_tokens':1}}))
""",
        )
        self.config(agy=agy)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "agy", "--fresh", "run something"]
                ),
                1,
            )
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "error")
        self.assertIn("RunCommand", " ".join(result["blockers"]))

    def test_claude_permission_denials_are_surfaced_as_blockers(self):
        # Real populated permission_denials entries are unverified (see
        # FUSION_RESEARCH.md), so this exercises the defensive extraction
        # with a plausible shape rather than a confirmed-real one.
        claude = self.write_agent(
            "claude-denied",
            """
import json
print(json.dumps({
    'type': 'result', 'subtype': 'success', 'is_error': False,
    'session_id': 'denied-session',
    'result': 'STATUS: success\\nSUMMARY: did the work\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none',
    'permission_denials': [{'tool_name': 'Bash', 'reason': 'command not in allowlist'}],
}))
""",
        )
        self.config(claude=claude)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "claude", "--read-only", "do it"]
                ),
                1,
            )
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "error")
        self.assertIn("Bash", " ".join(result["blockers"]))
        self.assertIn("command not in allowlist", " ".join(result["blockers"]))

    def test_empty_answer_with_clean_exit_is_an_error(self):
        # Observed live: route orc-free on cohere/north-mini-code:free, 2026-09-24.
        claude = self.write_agent(
            "claude-empty",
            """
import json
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                  'session_id': 'empty', 'num_turns': 2, 'result': ''}))
""",
        )
        self.config(claude=claude)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "claude", "--read-only", "do it"]
                ),
                1,
            )
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "error")
        self.assertIn("worker returned an empty answer", result["blockers"])

    def test_reasoning_only_answer_is_saved_as_evidence_but_stays_an_error(self):
        # Observed live: cohere/north-mini-code:free on orc-free put its reply in
        # a thinking block. A STATUS inside reasoning is a draft, not a report.
        state = self.workspace / "claude-state"
        transcript = state / "projects" / "repo" / "reasoner.jsonl"
        transcript.parent.mkdir(parents=True)
        turns = [{"message": {"role": "user", "content": "earlier task"}},
                 {"message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "an older turn"}]}},
                 {"message": {"role": "user", "content": "do it"}},
                 {"message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "Plan: read calc.py"},
                                                                {"type": "tool_use", "id": "t", "name": "Read", "input": {}}]}},
                 {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}},
                 {"message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "STATUS: success\nBLOCKERS: none"}]}}]
        transcript.write_text("\n".join(json.dumps(t) for t in turns) + "\n")
        claude = self.write_agent(
            "claude-reasoner",
            """
import json
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 'reasoner', 'result': ''}))
""",
        )
        self.config(claude=claude)
        output = io.StringIO()
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(state)}), contextlib.redirect_stdout(output):
            self.assertEqual(fusion_core.main(["--workspace", str(self.workspace), "--json", "delegate",
                                               "--agent", "claude", "--read-only", "do it"]), 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "error")
        self.assertIn("thinking.md as evidence, not a handoff", " ".join(result["blockers"]))
        saved = Path(result["artifacts"]["thinking"]).read_text()
        self.assertIn("Plan: read calc.py", saved)
        self.assertIn("STATUS: success", saved)
        self.assertNotIn("an older turn", saved)
        self.assertNotIn("answer", result["artifacts"])

    def test_free_openrouter_models_cost_nothing_whatever_claude_code_estimates(self):
        orc = self.write_agent(
            "orc",
            """
import json
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 'free',
                  'total_cost_usd': 3.57, 'result': 'STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))
""",
        )
        self.config(claude=orc)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            fusion_core.main(["--workspace", str(self.workspace), "--json", "delegate", "--agent", "claude",
                              "--read-only", "--model", "vendor/model:free", "do it"])
        usage = json.loads(output.getvalue())["usage"]
        self.assertEqual((usage["cost_usd"], usage["cost_estimate_usd"]), (0.0, 3.57))

    def test_claude_permission_denials_fall_back_to_raw_entry_when_unrecognized(self):
        claude = self.write_agent(
            "claude-denied-unknown-shape",
            """
import json
print(json.dumps({
    'type': 'result', 'subtype': 'success', 'is_error': False,
    'session_id': 's',
    'result': 'STATUS: success\\nSUMMARY: did the work\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none',
    'permission_denials': [{'some_unexpected_field': 'value'}],
}))
""",
        )
        self.config(claude=claude)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "claude", "--read-only", "do it"]
                ),
                1,
            )
        result = json.loads(output.getvalue())
        # Doesn't crash or silently drop the entry when the shape doesn't
        # match any recognized field name -- dumps the raw entry instead.
        self.assertIn("some_unexpected_field", " ".join(result["blockers"]))

    def test_orc_free_routes_skip_max_budget_but_paid_routes_keep_it(self):
        free_task = fusion_core.make_task(
            self.workspace, "claude", "t", "r", [], [], None, False, False,
            settings_overrides={"command": "orc", "model": "cohere/north-mini-code:free", "max_budget_usd": 0.1},
        )
        argv, _, _ = fusion_core.agent_command({}, free_task, None)
        self.assertNotIn("--max-budget-usd", argv)
        paid_task = fusion_core.make_task(
            self.workspace, "claude", "t", "r", [], [], None, False, False,
            settings_overrides={"command": "orc", "model": "anthropic/claude-opus-5", "max_budget_usd": 0.4},
        )
        argv, _, _ = fusion_core.agent_command({}, paid_task, None)
        self.assertIn("--max-budget-usd", argv)

    def test_mcp_lists_tools(self):
        self.config()
        requests = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            ]
        ) + "\n"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "fusion"), "--workspace", str(self.workspace), "mcp-serve"],
            input=requests,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "fusion")
        tools = responses[1]["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertEqual(
            names,
            {
                "fusion_delegate",
                "fusion_outcome",
                "fusion_status",
                "fusion_decisions",
                "fusion_here",
                "fusion_run_start",
                "fusion_run_status",
                "fusion_run_cancel",
            },
        )
        # Every advertised tool must be usable: a described name and a schema.
        for tool in tools:
            self.assertTrue(tool.get("description", "").strip(), tool["name"])
            self.assertEqual(tool.get("inputSchema", {}).get("type"), "object", tool["name"])

    def test_mcp_delegates_with_the_task_contract(self):
        codex = self.write_agent(
            "codex-fake",
            """
import json
print(json.dumps({'type':'thread.started','thread_id':'mcp-thread'}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'STATUS: success\\nSUMMARY: delegated\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}}))
print(json.dumps({'type':'turn.completed','usage':{}}))
""",
        )
        self.config(codex=codex)
        request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "fusion_delegate",
                    "arguments": {
                        "agent": "codex",
                        "task": "review the change",
                        "role": "reviewer",
                        "success_criteria": ["return a handoff"],
                        "constraints": ["do not edit files"],
                        "write": False,
                        "resume": False,
                    },
                },
            }
        ) + "\n"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "fusion"), "--workspace", str(self.workspace), "mcp-serve"],
            input=request,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        response = json.loads(proc.stdout)
        result = response["result"]["structuredContent"]
        self.assertEqual(result["status"], "success", response)
        self.assertEqual(result["summary"], "delegated")
        task_files = list((self.workspace / ".fusion" / "runs").glob("*/task.json"))
        self.assertEqual(len(task_files), 1)
        task = json.loads(task_files[0].read_text())
        self.assertEqual(task["task"], "review the change")
        self.assertEqual(task["role"], "reviewer")
        self.assertEqual(task["constraints"], ["do not edit files"])

    def test_delegate_pins_model_and_effort_from_mcp_and_cli(self):
        argv_log = self.workspace / "argv.jsonl"
        codex = self.write_agent(
            "codex-fake",
            f"""
import json, sys
open({str(argv_log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')
print(json.dumps({{'type':'thread.started','thread_id':'pin-thread'}}))
print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':'STATUS: success\\nSUMMARY: pinned\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}}}}))
print(json.dumps({{'type':'turn.completed','usage':{{}}}}))
""",
        )
        self.config(codex=codex)

        def call(arguments):
            request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "fusion_delegate", "arguments": arguments}}) + "\n"
            proc = subprocess.run(
                [sys.executable, str(ROOT / "fusion"), "--workspace", str(self.workspace), "mcp-serve"],
                input=request, text=True, capture_output=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)["result"]

        result = call({"agent": "codex", "task": "inspect", "write": False, "resume": False,
                       "model": "gpt-pinned", "reasoning_effort": "low"})
        self.assertEqual(result["structuredContent"]["status"], "success", result)
        argv = json.loads(argv_log.read_text().splitlines()[-1])
        self.assertIn('model_reasoning_effort="low"', argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-pinned")

        refused = call({"agent": "codex", "task": "inspect", "reasoning_effort": "low"})
        self.assertTrue(refused.get("isError"), refused)
        self.assertIn("explicit model", json.dumps(refused))

        proc = subprocess.run(
            [sys.executable, str(ROOT / "fusion"), "--workspace", str(self.workspace), "delegate",
             "--agent", "codex", "--read-only", "--fresh", "--model", "gpt-cli", "--reasoning-effort", "high", "inspect"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        argv = json.loads(argv_log.read_text().splitlines()[-1])
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-cli")

    def test_ultra_pipeline_keeps_stage_artifacts_and_stops_at_limit(self):
        claude = self.write_agent(
            "claude-fake",
            """
import json
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'ultra-claude','result':'STATUS: success\\nSUMMARY: stage complete\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))
""",
        )
        codex = self.write_agent(
            "codex-fake",
            """
import json
print(json.dumps({'type':'thread.started','thread_id':'ultra-codex'}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'STATUS: success\\nSUMMARY: implementation complete\\nCHANGED: src/app.py\\nTESTS: python -m unittest\\nBLOCKERS: none'}}))
print(json.dumps({'type':'turn.completed','usage':{}}))
""",
        )
        value = {
            "claude": {"command": str(claude)},
            "codex": {"command": str(codex)},
            "routes": {"cheap": {"agent": "claude", "command": str(claude)}},
            "ultra": {
                "max_stages": 5,
                "stages": {
                    "explore": {"agent": "claude", "route": "cheap", "write": False},
                    "implement": {"agent": "codex", "write": True},
                    "review": {"agent": "claude", "route": "cheap", "write": False},
                    "synthesize": {"agent": "claude", "route": "cheap", "write": False},
                },
            },
        }
        (self.workspace / ".fusion.json").write_text(json.dumps(value), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            actual = fusion_core.main(
                ["--workspace", str(self.workspace), "--json", "ultra", "--stages", "3", "ship the bounded feature"]
            )
        self.assertEqual(actual, 0, output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "success")
        self.assertEqual([item["stage"] for item in result["stages"]], ["explore", "implement", "review"])
        self.assertEqual(len(list(Path(result["artifacts"]["root"]).glob("*.json"))), 4)
        self.assertEqual(result["stages"][1]["result"]["changed"], ["src/app.py"])

        codex_output = io.StringIO()
        with contextlib.redirect_stdout(codex_output):
            self.assertEqual(
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "ultra", "--harness", "codex", "--stages", "1", "codex-only"]
                ),
                0,
            )
        codex_result = json.loads(codex_output.getvalue())
        self.assertEqual(codex_result["stages"][0]["task"]["agent"], "codex")
        self.assertEqual(codex_result["stages"][0]["task"]["route"], "codex-read")

    def test_doctor_checks_named_route_commands(self):
        claude = self.write_agent("claude-fake", "print('unused')\n")
        codex = self.write_agent("codex-fake", "print('unused')\n")
        self.config(codex=codex, claude=claude)
        value = json.loads((self.workspace / ".fusion.json").read_text())
        value["routes"] = {
            "good": {"agent": "claude", "command": str(claude)},
            "missing": {"agent": "claude", "command": "missing-orc"},
        }
        (self.workspace / ".fusion.json").write_text(json.dumps(value), encoding="utf-8")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(fusion_core.main(["--workspace", str(self.workspace), "doctor"]), 1)
        result = json.loads(output.getvalue())
        route_checks = {item["route"]: item for item in result["route_checks"]}
        self.assertTrue(route_checks["good"]["ok"])
        self.assertFalse(route_checks["missing"]["ok"])

    def test_workflow_fanout_fanin_and_artifact_gate(self):
        claude = self.write_agent(
            "workflow-claude",
            """
import json, pathlib, sys
prompt = sys.argv[-1]
if 'final.md' in prompt:
    pathlib.Path('final.md').write_text('synthesized workflow output\\n', encoding='utf-8')
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'workflow-claude','result':'STATUS: success\\nSUMMARY: node completed\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))
""",
        )
        self.config(claude=claude)
        spec = {
            "task": "evaluate the repository",
            "max_parallel": 2,
            "max_attempts": 1,
            "nodes": [
                {
                    "id": "research",
                    "items": ["alpha", "beta", "gamma"],
                    "task_template": "Research {item}",
                    "role": "researcher",
                    "agent": "claude",
                },
                {
                    "id": "synthesize",
                    "needs": ["research"],
                    "task": "Synthesize the research into final.md",
                    "role": "synthesizer",
                    "agent": "claude",
                    "write": True,
                    "required_files": ["final.md"],
                    "acceptance": {"required_handoff": ["summary"]},
                },
            ],
            "acceptance": {"required_files": ["final.md"], "required_nodes": ["synthesize"]},
        }
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        result = run_workflow(self.workspace, json.loads((self.workspace / ".fusion.json").read_text()), spec_path)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["nodes"]), 4)
        self.assertTrue(all(node["status"] == "success" for node in result["nodes"]))
        self.assertTrue((self.workspace / "final.md").is_file())
        events = (Path(result["artifacts"]["events"])).read_text(encoding="utf-8")
        self.assertIn('"type": "node.succeeded"', events)

    def test_workflow_pauses_on_quota_and_resumes_with_persisted_state(self):
        claude = self.write_agent(
            "quota-claude",
            """
import json
print(json.dumps({'type':'result','subtype':'error','is_error':True,'session_id':'quota-claude','result':\"You've hit your session limit; resets at 5:10am\"}))
""",
        )
        self.config(claude=claude)
        spec = {
            "task": "quota test",
            "nodes": [{"id": "probe", "task": "probe", "agent": "claude"}],
        }
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        config = json.loads((self.workspace / ".fusion.json").read_text())
        first = run_workflow(self.workspace, config, spec_path)
        self.assertEqual(first["status"], "paused_quota")
        run_id = first["workflow_id"]

        claude.write_text(
            "#!/usr/bin/env python3\nimport json\nprint(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'quota-claude','result':'STATUS: success\\nSUMMARY: resumed\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))\n",
            encoding="utf-8",
        )
        claude.chmod(0o755)
        resumed = resume_workflow(self.workspace, config, run_id)
        self.assertEqual(resumed["status"], "success")
        self.assertEqual(resumed["nodes"][0]["attempts"], 2)

    def test_workflow_rejects_stale_required_artifact(self):
        claude = self.write_agent(
            "stale-claude",
            """
import json
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'stale-claude','result':'STATUS: success\\nSUMMARY: reported success\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))
""",
        )
        self.config(claude=claude)
        (self.workspace / "FINAL.md").write_text("old output\\n", encoding="utf-8")
        spec = {
            "task": "stale artifact test",
            "nodes": [{"id": "writer", "task": "write the final artifact", "agent": "claude", "required_files": ["FINAL.md"]}],
            "acceptance": {"required_files": ["FINAL.md"]},
        }
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        result = run_workflow(self.workspace, json.loads((self.workspace / ".fusion.json").read_text()), spec_path)
        self.assertEqual(result["status"], "failed")
        self.assertIn("did not change", " ".join(result["nodes"][0]["result"]["blockers"]))

    def test_workflow_report_groups_waves_and_usage(self):
        claude = self.write_agent(
            "report-claude",
            """
import json, pathlib, sys
prompt = sys.argv[-1]
if 'final.md' in prompt:
    pathlib.Path('final.md').write_text('synthesized workflow output\\n', encoding='utf-8')
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'report-claude','result':'STATUS: success\\nSUMMARY: node completed\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))
""",
        )
        self.config(claude=claude)
        spec = {
            "task": "evaluate the repository",
            "max_parallel": 2,
            "nodes": [
                {"id": "research", "items": ["alpha", "beta", "gamma"], "task_template": "Research {item}", "agent": "claude"},
                {
                    "id": "synthesize",
                    "needs": ["research"],
                    "task": "Synthesize the research into final.md",
                    "agent": "claude",
                    "write": True,
                    "required_files": ["final.md"],
                },
            ],
            "acceptance": {"required_files": ["final.md"]},
        }
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        config = json.loads((self.workspace / ".fusion.json").read_text())
        result = run_workflow(self.workspace, config, spec_path)
        self.assertEqual(result["status"], "success")

        report = workflow_report(self.workspace, result["workflow_id"])
        self.assertEqual(report["status"], "success")
        waves = {item["wave"]: {node["id"] for node in item["nodes"]} for item in report["waves"]}
        self.assertEqual(waves[0], {"research-01", "research-02", "research-03"})
        self.assertEqual(waves[1], {"synthesize"})
        # Four worker spans plus one gate span per accepted node.
        self.assertEqual(report["usage"]["spans"], 8)
        gate = next(group for group in report["usage"]["by_route"] if group["agent"] == "gate")
        self.assertEqual((gate["success"], gate["failed"]), (4, 0))
        self.assertEqual(sum(g["calls"] for g in report["usage"]["by_route"] if g["agent"] != "gate"), 4)
        self.assertEqual(report["blockers"], [])
        self.assertIsNone(report["resume_command"])

    def test_workflow_report_surfaces_blockers_and_resume_command(self):
        claude = self.write_agent(
            "report-quota-claude",
            """
import json
print(json.dumps({'type':'result','subtype':'error','is_error':True,'session_id':'s','result':\"You've hit your session limit; resets at 5:10am\"}))
""",
        )
        self.config(claude=claude)
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps({"task": "report quota test", "nodes": [{"id": "probe", "task": "probe", "agent": "claude"}]}), encoding="utf-8")
        config = json.loads((self.workspace / ".fusion.json").read_text())
        result = run_workflow(self.workspace, config, spec_path)
        self.assertEqual(result["status"], "paused_quota")

        report = workflow_report(self.workspace, result["workflow_id"])
        self.assertEqual(report["status"], "paused_quota")
        self.assertTrue(report["blockers"])
        self.assertTrue(all(item["node_id"] == "probe" for item in report["blockers"]))
        self.assertTrue(any("session limit" in item["blocker"] for item in report["blockers"]))
        self.assertIn("workflow resume", report["resume_command"])
        self.assertIn(result["workflow_id"], report["resume_command"])

    def test_preflight_blocks_missing_executable_before_any_dispatch(self):
        self.config(claude=Path("missing-claude-binary"))
        spec = {
            "task": "preflight test",
            "max_parallel": 3,
            "nodes": [
                {"id": "a", "task": "a", "agent": "claude"},
                {"id": "b", "task": "b", "agent": "claude"},
                {"id": "c", "task": "c", "agent": "claude"},
            ],
        }
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        result = run_workflow(self.workspace, json.loads((self.workspace / ".fusion.json").read_text()), spec_path)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(all(node["status"] == "blocked" for node in result["nodes"]))
        self.assertTrue(all("not available on PATH" in node["result"]["blockers"][0] for node in result["nodes"]))
        # No node was actually dispatched: the preflight caught the missing
        # binary once instead of three separate worker attempts discovering it.
        self.assertFalse((self.workspace / ".fusion" / "runs").exists())

    def test_a_claude_quota_does_not_cool_down_orc_routes(self):
        claude = self.write_agent(
            "quota-native-claude",
            """
import json
print(json.dumps({'type':'result','subtype':'error','is_error':True,'session_id':'s','result':"You've hit your session limit; resets at 5:10am"}))
""",
        )
        orc = self.write_agent(
            "orc",
            """
import json
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'o',
                  'result':'STATUS: success\\nSUMMARY: done on OpenRouter\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))
""",
        )
        self.config(claude=claude)
        config = json.loads((self.workspace / ".fusion.json").read_text())
        config.setdefault("routes", {})["free"] = {"agent": "claude", "command": str(orc), "model": "vendor/model:free"}
        (self.workspace / ".fusion.json").write_text(json.dumps(config))
        spec = {"task": "accounts", "max_parallel": 1, "nodes": [
            {"id": "native", "task": "a", "agent": "claude"},
            {"id": "native2", "task": "b", "agent": "claude", "needs": ["native"]},
            {"id": "free", "task": "c", "agent": "claude", "route": "free"}]}
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        result = run_workflow(self.workspace, config, spec_path)
        statuses = {node["id"]: node["status"] for node in result["nodes"]}
        # The claude.ai limit pauses native Claude only; the OpenRouter lane runs.
        self.assertEqual(statuses["native"], "paused_quota")
        self.assertEqual(statuses["free"], "success")

    def test_lane_cooldown_prevents_further_dispatch_in_same_run(self):
        calls = self.bin_dir / "calls.txt"
        claude = self.write_agent(
            "quota-lane-claude",
            f"""
import json, pathlib
pathlib.Path({str(calls)!r}).open('a', encoding='utf-8').write('call\\n')
print(json.dumps({{'type':'result','subtype':'error','is_error':True,'session_id':'s','result':\"You've hit your session limit; resets at 5:10am\"}}))
""",
        )
        self.config(claude=claude)
        spec = {
            "task": "lane cooldown test",
            "max_parallel": 1,
            "nodes": [
                {"id": "a", "task": "a", "agent": "claude"},
                {"id": "b", "task": "b", "agent": "claude"},
            ],
        }
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        result = run_workflow(self.workspace, json.loads((self.workspace / ".fusion.json").read_text()), spec_path)
        self.assertEqual(result["status"], "paused_quota")
        statuses = {node["id"]: node["status"] for node in result["nodes"]}
        self.assertEqual(statuses["a"], "paused_quota")
        self.assertEqual(statuses["b"], "paused_quota")
        node_b = next(node for node in result["nodes"] if node["id"] == "b")
        self.assertIn("lane", node_b["result"]["summary"])
        # Node b never actually ran a worker; only node a's failure did.
        self.assertEqual(calls.read_text().count("call"), 1)

    def test_fresh_run_seeds_lane_cooldown_from_recent_trace_but_resume_does_not(self):
        claude = self.write_agent(
            "history-claude",
            """
import json
print(json.dumps({'type':'result','subtype':'error','is_error':True,'session_id':'s','result':\"You've hit your usage limit; resets in 10 minutes\"}))
""",
        )
        self.config(claude=claude)
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps({"task": "seed", "nodes": [{"id": "a", "task": "a", "agent": "claude"}]}), encoding="utf-8")
        config = json.loads((self.workspace / ".fusion.json").read_text())
        first = run_workflow(self.workspace, config, spec_path)
        self.assertEqual(first["status"], "paused_quota")

        # A brand new workflow in the same workspace should see the still-fresh
        # quota trace and refuse to dispatch into the same dead lane.
        spec_path_2 = self.workspace / "workflow2.json"
        spec_path_2.write_text(json.dumps({"task": "seed2", "nodes": [{"id": "b", "task": "b", "agent": "claude"}]}), encoding="utf-8")
        second = run_workflow(self.workspace, config, spec_path_2)
        self.assertEqual(second["status"], "paused_quota")
        self.assertEqual(second["nodes"][0]["status"], "paused_quota")
        self.assertIn("quota", second["nodes"][0]["result"]["blockers"][0])

        # But an explicit resume of the first run is a "try again now" signal
        # and must not be blocked by that same trace history.
        claude.write_text(
            "#!/usr/bin/env python3\nimport json\nprint(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'s','result':'STATUS: success\\nSUMMARY: resumed\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))\n",
            encoding="utf-8",
        )
        claude.chmod(0o755)
        resumed = resume_workflow(self.workspace, config, first["workflow_id"])
        self.assertEqual(resumed["status"], "success")

    def test_select_orc_model_requires_fit_unless_allow_untested(self):
        orc = self.write_agent(
            "orc",
            """
import sys
argv = sys.argv[1:]
if argv[:1] == ['models']:
    if '--fit' in argv:
        print('providerA/good-model\\tFIT stuff')
    else:
        # Untested candidate ranks ahead of the fit one.
        print('providerB/untested-model\\tstuff')
        print('providerA/good-model\\tstuff')
    sys.exit(0)
sys.exit(1)
""",
        )
        self.assertEqual(fusion_core.select_orc_model(str(orc), "free"), "providerA/good-model")
        self.assertEqual(
            fusion_core.select_orc_model(str(orc), "free", allow_untested=True),
            "providerB/untested-model",
        )

    def test_select_orc_model_returns_none_when_nothing_is_fit(self):
        orc = self.write_agent(
            "orc",
            """
import sys
argv = sys.argv[1:]
if argv[:1] == ['models']:
    if '--fit' not in argv:
        print('providerB/untested-model\\tstuff')
    sys.exit(0)
sys.exit(1)
""",
        )
        self.assertIsNone(fusion_core.select_orc_model(str(orc), "free"))

    def test_select_orc_model_best_selector_never_resolves_to_free_model(self):
        orc_free_only = self.write_agent(
            "orc-free-only",
            """
import sys
argv = sys.argv[1:]
if argv[:1] == ['models']:
    if '--fit' in argv:
        print('providerA/mini-code:free\\tFIT stuff')
    else:
        # Free model ranks above untested paid model in tools ranking.
        print('providerA/mini-code:free\\tstuff')
        print('providerB/paid-model\\tstuff')
    sys.exit(0)
sys.exit(1)
""",
        )
        # When only free models have passed probe --fit, 'best' must return None, not the free model.
        self.assertIsNone(fusion_core.select_orc_model(str(orc_free_only), "best"))
        self.assertEqual(fusion_core.fitted_orc_models(str(orc_free_only), "best", 1), [])
        # 'free' selector is unchanged and still resolves to the fitted free model.
        self.assertEqual(fusion_core.select_orc_model(str(orc_free_only), "free"), "providerA/mini-code:free")

        orc_with_paid = self.write_agent(
            "orc-with-paid",
            """
import sys
argv = sys.argv[1:]
if argv[:1] == ['models']:
    if '--fit' in argv:
        print('providerA/mini-code:free\\tFIT stuff')
        print('providerB/paid-model\\tFIT stuff')
    else:
        # Free model still ranks higher in tools ranking.
        print('providerA/mini-code:free\\tstuff')
        print('providerB/paid-model\\tstuff')
    sys.exit(0)
sys.exit(1)
""",
        )
        # Once a paid model is also fit, 'best' picks the paid model over the higher-ranked free model.
        self.assertEqual(fusion_core.select_orc_model(str(orc_with_paid), "best"), "providerB/paid-model")
        self.assertEqual(fusion_core.fitted_orc_models(str(orc_with_paid), "best", 1), ["providerB/paid-model"])

    def test_workflow_waits_for_all_dependencies_before_blocking_fanin(self):
        spec = {
            "nodes": [
                {"id": "quota", "task": "quota", "agent": "claude"},
                {"id": "sibling", "task": "sibling", "agent": "claude"},
                {"id": "fanin", "needs": ["quota", "sibling"], "task": "fanin", "agent": "claude"},
            ]
        }
        # Pin the claude lane to a resolvable command: WorkflowRunner's
        # preflight blocks lanes whose executable is missing, which would
        # otherwise depend on whether claude happens to be on PATH.
        runner = WorkflowRunner(self.workspace, {"claude": {"command": "true"}}, spec)
        runner.nodes["quota"]["status"] = "paused_quota"
        runner.nodes["sibling"]["status"] = "running"
        runner._block_unrunnable()
        self.assertEqual(runner.nodes["fanin"]["status"], "pending")

        runner.nodes["sibling"]["status"] = "failed"
        runner._block_unrunnable()
        self.assertEqual(runner.nodes["fanin"]["status"], "blocked")
        self.assertEqual(
            runner.nodes["fanin"]["result"]["blockers"],
            ["quota: paused_quota", "sibling: failed"],
        )

    def test_resume_no_op_does_not_redispatch_any_node(self):
        calls = self.bin_dir / "calls.txt"
        claude = self.write_agent(
            "digest-claude",
            f"""
import json, pathlib
pathlib.Path({str(calls)!r}).open('a', encoding='utf-8').write('call\\n')
print(json.dumps({{'type':'result','subtype':'success','is_error':False,'session_id':'s','result':'STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}}))
""",
        )
        self.config(claude=claude)
        spec = {
            "task": "no-op resume test",
            "nodes": [
                {"id": "a", "task": "task a", "agent": "claude"},
                {"id": "b", "needs": ["a"], "task": "task b", "agent": "claude"},
            ],
        }
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        config = json.loads((self.workspace / ".fusion.json").read_text())
        first = run_workflow(self.workspace, config, spec_path)
        self.assertEqual(first["status"], "success")
        self.assertEqual(calls.read_text().count("call"), 2)
        first_digests = {node["id"]: node["result"]["digest"] for node in first["nodes"]}

        resumed = resume_workflow(self.workspace, config, first["workflow_id"])
        self.assertEqual(resumed["status"], "success")
        # Nothing was dispatched again: both nodes were reused from their
        # cached, digest-matched receipts instead of rerunning.
        self.assertEqual(calls.read_text().count("call"), 2)
        for node in resumed["nodes"]:
            self.assertEqual(node["attempts"], 1)
            self.assertEqual(node["result"]["digest"], first_digests[node["id"]])

        # A digest-matched node skips dispatch entirely, so it would
        # otherwise never produce a trace span -- verify it does anyway,
        # tagged distinctly as "cache_hit" rather than success or failure.
        traces = [json.loads(line) for line in (self.workspace / ".fusion" / "traces.jsonl").read_text().splitlines()]
        cache_hit_spans = [span for span in traces if span.get("status") == "cache_hit"]
        self.assertEqual(len(cache_hit_spans), 2)
        self.assertEqual({span["trace_id"] for span in cache_hit_spans}, {first["workflow_id"]})
        self.assertIsNone(fusion_core.failure_class({"status": "cache_hit", "blockers": []}))

        # usage_summary must not lump a cache hit in with real failures.
        summary = fusion_core.usage_summary(traces)
        self.assertEqual(sum(group["cache_hit"] for group in summary["by_route"]), 2)
        self.assertEqual(sum(group["failed"] for group in summary["by_route"]), 0)

    def test_resume_with_edited_spec_reruns_only_changed_node_and_downstream(self):
        calls = self.bin_dir / "calls.txt"
        claude = self.write_agent(
            "digest-edit-claude",
            f"""
import json, pathlib, sys
pathlib.Path({str(calls)!r}).open('a', encoding='utf-8').write(sys.argv[-1][:1] + '\\n')
print(json.dumps({{'type':'result','subtype':'success','is_error':False,'session_id':'s','result':'STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}}))
""",
        )
        self.config(claude=claude)
        spec = {
            "task": "edited resume test",
            "nodes": [
                {"id": "a", "task": "task a v1", "agent": "claude"},
                {"id": "b", "task": "task b", "agent": "claude"},
                {"id": "c", "needs": ["b"], "task": "task c", "agent": "claude"},
            ],
        }
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        config = json.loads((self.workspace / ".fusion.json").read_text())
        first = run_workflow(self.workspace, config, spec_path)
        self.assertEqual(first["status"], "success")
        self.assertEqual(calls.read_text().count("\n"), 3)

        # Edit only node "a"; "b" and its dependent "c" are untouched.
        spec["nodes"][0]["task"] = "task a v2"
        edited_spec_path = self.workspace / "workflow-edited.json"
        edited_spec_path.write_text(json.dumps(spec), encoding="utf-8")
        resumed = resume_workflow(self.workspace, config, first["workflow_id"], edited_spec_path)
        self.assertEqual(resumed["status"], "success")
        statuses = {node["id"]: node for node in resumed["nodes"]}
        self.assertEqual(statuses["a"]["attempts"], 2)
        self.assertEqual(statuses["b"]["attempts"], 1)
        self.assertEqual(statuses["c"]["attempts"], 1)
        # Only "a" was actually redispatched; "b" and "c" were reused.
        self.assertEqual(calls.read_text().count("\n"), 4)

    def test_resume_with_edited_upstream_reruns_downstream_dependent(self):
        calls = self.bin_dir / "calls.txt"
        claude = self.write_agent(
            "digest-cascade-claude",
            f"""
import json, pathlib
pathlib.Path({str(calls)!r}).open('a', encoding='utf-8').write('call\\n')
print(json.dumps({{'type':'result','subtype':'success','is_error':False,'session_id':'s','result':'STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}}))
""",
        )
        self.config(claude=claude)
        spec = {
            "task": "cascade resume test",
            "nodes": [
                {"id": "a", "task": "task a v1", "agent": "claude"},
                {"id": "b", "needs": ["a"], "task": "task b", "agent": "claude"},
            ],
        }
        spec_path = self.workspace / "workflow.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        config = json.loads((self.workspace / ".fusion.json").read_text())
        first = run_workflow(self.workspace, config, spec_path)
        self.assertEqual(first["status"], "success")
        self.assertEqual(calls.read_text().count("call"), 2)

        spec["nodes"][0]["task"] = "task a v2"
        edited_spec_path = self.workspace / "workflow-edited.json"
        edited_spec_path.write_text(json.dumps(spec), encoding="utf-8")
        resumed = resume_workflow(self.workspace, config, first["workflow_id"], edited_spec_path)
        self.assertEqual(resumed["status"], "success")
        statuses = {node["id"]: node for node in resumed["nodes"]}
        # "a" changed directly; "b" reran too even though its own task text
        # did not change, because its recorded digest embedded "a"'s old one.
        self.assertEqual(statuses["a"]["attempts"], 2)
        self.assertEqual(statuses["b"]["attempts"], 2)
        self.assertEqual(calls.read_text().count("call"), 4)
        self.assertNotEqual(statuses["a"]["result"]["digest"], first["nodes"][0]["result"]["digest"])
        self.assertNotEqual(statuses["b"]["result"]["digest"], first["nodes"][1]["result"]["digest"])

    def test_failure_class_categorizes_common_failure_reasons(self):
        self.assertIsNone(fusion_core.failure_class({"status": "success", "blockers": []}))
        self.assertEqual(
            fusion_core.failure_class({"status": "error", "blockers": ["You've hit your session limit; resets at 5pm"]}),
            "quota",
        )
        self.assertEqual(
            fusion_core.failure_class({"status": "error", "blockers": ["permission denied: Bash"]}),
            "permission_denied",
        )
        self.assertEqual(
            fusion_core.failure_class({"status": "blocked", "exit_code": 124, "blockers": ["timeout after 60 seconds"]}),
            "timeout",
        )
        self.assertEqual(
            fusion_core.failure_class({"status": "error", "blockers": ["orc is not available on PATH"]}),
            "missing_executable",
        )
        self.assertEqual(
            fusion_core.failure_class({"status": "error", "blockers": ["something else broke"]}),
            "worker_error",
        )

    def test_telemetry_install_id_is_stable_and_not_per_workspace(self):
        with tempfile.TemporaryDirectory() as home:
            old_home = os.environ.get("ORC_HOME")
            os.environ["ORC_HOME"] = home
            try:
                first = fusion_core.telemetry_install_id()
                second = fusion_core.telemetry_install_id()
                self.assertEqual(first, second)
                self.assertTrue((Path(home) / "telemetry_id").is_file())
            finally:
                if old_home is None:
                    os.environ.pop("ORC_HOME", None)
                else:
                    os.environ["ORC_HOME"] = old_home

    def test_remote_telemetry_defaults_on_and_environment_opt_out_stops_sends(self):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(True)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            claude = self.write_agent(
                "claude-default-telemetry",
                """
import json
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'s','result':'STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))
""",
            )
            value = {
                "claude": {"command": str(claude)},
                # remote.enabled deliberately omitted -- must default to on.
                "telemetry": {"remote": {"endpoint": f"http://127.0.0.1:{port}/v1/ingest"}},
            }
            (self.workspace / ".fusion.json").write_text(json.dumps(value), encoding="utf-8")
            self.allow_telemetry()
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "claude", "--read-only", "do it"]
                )
            self.assertEqual(len(received), 1, "an unconfigured workspace must still report")
            # ...and one switch stops it, with no config edit.
            with patch.dict(os.environ, {"FUSION_TELEMETRY": "0"}), contextlib.redirect_stdout(io.StringIO()):
                fusion_core.main(
                    ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "claude", "--read-only", "again"]
                )
            self.assertEqual(len(received), 1, "FUSION_TELEMETRY=0 must stop the send")
        finally:
            server.shutdown()
            server.server_close()

    def test_remote_telemetry_sends_reduced_payload_when_enabled(self):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                received.append({
                    "body": json.loads(self.rfile.read(length)),
                    "auth": self.headers.get("Authorization"),
                })
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            claude = self.write_agent(
                "claude-remote-telemetry",
                """
import json
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'s','result':'STATUS: success\\nSUMMARY: done\\nCHANGED: secret/path.py\\nTESTS: pytest\\nBLOCKERS: none'}))
""",
            )
            value = {
                "claude": {"command": str(claude)},
                "telemetry": {"remote": {"enabled": True, "endpoint": f"http://127.0.0.1:{port}/v1/ingest", "token": "sekret"}},
            }
            (self.workspace / ".fusion.json").write_text(json.dumps(value), encoding="utf-8")
            self.allow_telemetry()
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    fusion_core.main(
                        ["--workspace", str(self.workspace), "--json", "delegate", "--agent", "claude", "--read-only", "do secret/path.py work"]
                    ),
                    0,
                )
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["auth"], "Bearer sekret")
            body = received[0]["body"]
            self.assertEqual(body["schema"], "fusion.telemetry.v1")
            span = body["spans"][0]
            self.assertEqual(span["status"], "success")
            self.assertIsNone(span["failure_class"])
            # A whitelist, not a spot check: a field added to the span has to
            # be added here too, which is the moment to ask whether it should
            # be leaving the machine at all.
            self.assertEqual(set(span), {
                "trace_id", "span_id", "parent_span_id", "agent", "role", "route", "model",
                "write", "status", "failure_class", "start_time_ms", "end_time_ms",
                "duration_ms", "usage",
            })
            self.assertEqual(set(body), {"schema", "install_id", "spans"})
            # And nothing project-identifying anywhere in the serialized payload,
            # whatever field it might have travelled in.
            wire = json.dumps(body)
            for secret in ("secret/path.py", "pytest", "do secret/path.py work",
                           str(self.workspace), str(self.temp.name), "claude-remote-telemetry"):
                self.assertNotIn(secret, wire, f"{secret!r} reached the collector")
            # `telemetry status` promises a field list; it must be the real one.
            status = io.StringIO()
            with contextlib.redirect_stdout(status):
                fusion_core.main(["--workspace", str(self.workspace), "telemetry", "status"])
            self.assertEqual(set(json.loads(status.getvalue())["fields_sent"]), set(span))
        finally:
            server.shutdown()
            server.server_close()

    def test_telemetry_status_reports_configuration(self):
        self.config()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(fusion_core.main(["--workspace", str(self.workspace), "telemetry", "status"]), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["remote_enabled"])
        self.assertEqual(result["remote_endpoint"], "https://orc-telemetry.fly.dev/v1/ingest")
        self.assertIsNotNone(result["install_id"])
        self.assertIn("agent", result["fields_sent"])
        self.assertNotIn("blockers", result["fields_sent"])
        self.assertIn("prompt/task text", result["fields_never_sent"])

    def test_first_send_announces_itself_once_per_machine(self):
        home = Path(os.environ["ORC_HOME"])
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            fusion_core.announce_remote_telemetry("https://example.invalid/v1/ingest")
        first = errors.getvalue()
        self.assertIn("anonymous usage telemetry", first)
        self.assertIn("FUSION_TELEMETRY=0", first)
        self.assertTrue((home / "telemetry_announced").exists())
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            fusion_core.announce_remote_telemetry("https://example.invalid/v1/ingest")
        self.assertEqual(errors.getvalue(), "", "the notice must not repeat on later runs")

    def test_telemetry_report_fetches_and_prints_remote_summary(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                scoped = "install_id=" in self.path
                # The collector mirrors the real one: your own rows need only
                # the install id, every install needs the shared token.
                if not scoped and self.headers.get("Authorization") != "Bearer sekret":
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.dumps({
                    "scope": "this install" if scoped else "all installs",
                    "window_hours": 24,
                    "total_spans": 2,
                    "unique_installs": 1,
                    "by_group": [
                        {"agent": "claude", "route": "orc-free", "model": "claude-sonnet-5",
                         "status": "success", "failure_class": None, "calls": 2,
                         "total_cost_usd": 0.1, "avg_duration_ms": 500},
                    ],
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            value = {
                "telemetry": {"remote": {"enabled": True, "endpoint": f"http://127.0.0.1:{port}/v1/ingest", "token": "sekret"}},
            }
            (self.workspace / ".fusion.json").write_text(json.dumps(value), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    fusion_core.main(["--workspace", str(self.workspace), "--json", "telemetry", "report", "--hours", "24"]),
                    0,
                )
            result = json.loads(output.getvalue())
            self.assertEqual(result["total_spans"], 2)
            self.assertEqual(result["by_group"][0]["agent"], "claude")
            # Reading your own rows carried the install id and needed no token.
            self.assertIn("install_id=", seen[0][0])
            self.assertEqual(result["scope"], "this install")

            # --all asks for every install and is the only path that needs one.
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    fusion_core.main(["--workspace", str(self.workspace), "--json", "telemetry", "report", "--hours", "24", "--all"]),
                    0,
                )
            self.assertNotIn("install_id=", seen[1][0])
            self.assertEqual(seen[1][1], "Bearer sekret")
            self.assertEqual(json.loads(output.getvalue())["scope"], "all installs")

            # Without a token, --all refuses locally instead of 401ing remotely.
            no_token = {"telemetry": {"remote": {"enabled": True, "endpoint": f"http://127.0.0.1:{port}/v1/ingest"}}}
            (self.workspace / ".fusion.json").write_text(json.dumps(no_token), encoding="utf-8")
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    fusion_core.main(["--workspace", str(self.workspace), "telemetry", "report", "--all"]),
                    1,
                )
            self.assertIn("telemetry.remote.token", errors.getvalue())
            self.assertEqual(len(seen), 2, "--all without a token must not hit the network")
        finally:
            server.shutdown()
            server.server_close()

    def test_telemetry_on_off_persists_without_hand_editing_json(self):
        (self.workspace / ".fusion.json").write_text(json.dumps({"claude": {"command": "claude"}}), encoding="utf-8")
        for command, expected in (("off", False), ("on", True)):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fusion_core.main(["--workspace", str(self.workspace), "telemetry", command]), 0)
            saved = json.loads((self.workspace / ".fusion.json").read_text())
            self.assertIs(saved["telemetry"]["remote"]["enabled"], expected)
            self.assertEqual(saved["claude"]["command"], "claude", "unrelated config must survive")

    def test_doctor_never_prints_a_token(self):
        value = {"claude": {"command": "claude"},
                 "telemetry": {"remote": {"enabled": True, "endpoint": "https://x/v1/ingest", "token": "sekret-do-not-print"}}}
        (self.workspace / ".fusion.json").write_text(json.dumps(value), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            fusion_core.main(["--workspace", str(self.workspace), "doctor"])
        printed = output.getvalue()
        self.assertNotIn("sekret-do-not-print", printed)
        self.assertIn("https://x/v1/ingest", printed, "only secrets are masked, not the whole block")

    def test_unwritable_home_never_breaks_a_run(self):
        # Reporting is on by default, so telemetry touching an unwritable
        # ORC_HOME must not reach the dispatch path.
        blocked = Path(self.temp.name) / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        with patch.dict(os.environ, {"ORC_HOME": str(blocked / "orc")}):
            first = fusion_core.telemetry_install_id()
            self.assertEqual(first, fusion_core.telemetry_install_id(), "one install, not one per span")
            with contextlib.redirect_stderr(io.StringIO()):
                fusion_core.announce_remote_telemetry("https://example.invalid/v1/ingest")
            fusion_core.send_remote_telemetry(
                {"enabled": True, "endpoint": "https://127.0.0.1:1/v1/ingest"},
                {"agent": "claude", "status": "success", "usage": {}},
            )
    def test_ui_token_survives_a_restart_and_stays_private(self):
        import fusion_ui
        home = Path(self.temp.name) / "orc-home"
        with patch.dict(os.environ, {"ORC_HOME": str(home)}):
            first = fusion_ui.persistent_token()
            self.assertTrue(first)
            # A restart must not 401 an open tab or a bookmark.
            self.assertEqual(first, fusion_ui.persistent_token())
            self.assertEqual((home / "ui-token").stat().st_mode & 0o777, 0o600)
        other = Path(self.temp.name) / "other-home"
        with patch.dict(os.environ, {"ORC_HOME": str(other)}):
            self.assertNotEqual(first, fusion_ui.persistent_token(), "separate homes are separate capabilities")
        blocked = Path(self.temp.name) / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        with patch.dict(os.environ, {"ORC_HOME": str(blocked / "orc")}):
            self.assertTrue(fusion_ui.persistent_token(), "an unwritable home still serves this session")
    def test_a_killed_coordinator_reads_as_interrupted_not_running(self):
        from fusion_workflow import effective_status
        # A manifest keeps whatever the coordinator last flushed, so one that
        # was killed says "running" forever: watch never exits and report
        # offers no way forward.
        self.assertEqual(effective_status({"status": "running", "coordinator_pid": 2 ** 22 - 1}), "interrupted")
        self.assertEqual(effective_status({"status": "running", "coordinator_pid": os.getpid()}), "running")
        self.assertEqual(effective_status({"status": "running"}), "running", "no pid recorded is not evidence of death")
        for terminal in ("success", "failed", "paused_quota"):
            self.assertEqual(effective_status({"status": terminal, "coordinator_pid": 2 ** 22 - 1}), terminal)

    def test_report_offers_a_way_forward_after_the_coordinator_dies(self):
        from fusion_workflow import workflow_report
        manifest = {
            "schema": "fusion.workflow.v1", "workflow_id": "wf-dead", "status": "running",
            "coordinator_pid": 2 ** 22 - 1, "task": "t", "spec": {"graph": {"nodes": []}},
            "nodes": {}, "lanes": {}, "attempt_ledger": [], "artifacts": {},
        }
        path = self.workspace / ".fusion" / "workflows" / "wf-dead"
        path.mkdir(parents=True)
        (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        report = workflow_report(self.workspace, "wf-dead")
        self.assertEqual(report["status"], "interrupted")
        self.assertIn("resume", report["resume_command"] or "", "an interrupted run must offer resume")

    def test_no_available_worker_names_which_lane_and_why(self):
        from fusion_policy import no_route_reason
        config = {"codex": {"command": "missing-codex-binary"}, "claude": {"command": "missing-claude-binary"},
                  "agy": {"command": "missing-agy"}, "grok": {"command": "missing-grok"},
                  "routes": {"codex-read": {"agent": "codex", "sandbox": "read-only"}}}
        task = fusion_core.make_task(self.workspace, "auto", "do it", "implementation", [], [], None, False, True)
        reason = no_route_reason(config, task, fusion_core.RunStore(self.workspace))
        self.assertIn("that can write", reason)
        self.assertIn("missing-codex-binary is not on PATH", reason, "name the binary, not just the failure")
        self.assertIn("fusion doctor", reason)

        # A writer is also refused by a lane that exists but cannot write, and
        # that reason has to be distinguishable from a missing binary.
        installed = {**config, "codex": {"command": sys.executable}}
        writer_reason = no_route_reason(installed, task, fusion_core.RunStore(self.workspace))
        self.assertIn("cannot take a task that writes", writer_reason)

    def test_telemetry_report_fails_clearly_when_remote_disabled(self):
        (self.workspace / ".fusion.json").write_text(json.dumps({"telemetry": {"remote": {"enabled": False}}}))
        errors = io.StringIO()
        with patch("fusion_core.fetch_remote_summary") as fetch, contextlib.redirect_stderr(errors):
            self.assertEqual(fusion_core.main(["--workspace", str(self.workspace), "telemetry", "report"]), 1)
        fetch.assert_not_called()
        self.assertIn("nothing to fetch", errors.getvalue())


class WorkflowIdAdoptionTest(unittest.TestCase):
    """A caller that spawns a run must be able to name it up front.

    Without this, the only way to learn the id of a run you just started is to
    watch .fusion/workflows for a new directory and assume the newest one is
    yours — which is wrong as soon as two runs start at once.
    """

    def setUp(self):
        import fusion_workflow

        self.workflow = fusion_workflow
        self.addCleanup(os.environ.pop, fusion_workflow.WORKFLOW_ID_ENV, None)

    def test_generates_an_id_when_none_is_given(self):
        first, second = self.workflow._run_id(), self.workflow._run_id()
        self.assertNotEqual(first, second)
        self.assertIn("-wf-", first)

    def test_adopts_the_id_from_the_environment(self):
        os.environ[self.workflow.WORKFLOW_ID_ENV] = "chosen-run-01"
        self.assertEqual(self.workflow._run_id(), "chosen-run-01")

    def test_consumes_it_so_a_second_run_cannot_collide(self):
        os.environ[self.workflow.WORKFLOW_ID_ENV] = "chosen-run-01"
        self.assertEqual(self.workflow._run_id(), "chosen-run-01")
        self.assertNotEqual(self.workflow._run_id(), "chosen-run-01")

    def test_refuses_an_id_that_could_escape_the_workflows_directory(self):
        # The id becomes a directory name under .fusion/workflows.
        for bad in ("../escape", "a/b", ".hidden", "x" * 70, "with space", "-leading"):
            with self.subTest(bad=bad):
                os.environ[self.workflow.WORKFLOW_ID_ENV] = bad
                with self.assertRaises(ValueError):
                    self.workflow._run_id()


class HandoffParsingTest(unittest.TestCase):
    """Plain no-blocker markers are empty; arbitrary explanations are retained.

    Prose must remain coherent rather than becoming comma fragments. The parser
    does not infer that an explained tool failure was sufficiently recovered.
    """

    def blockers(self, value: str) -> list[str]:
        return fusion_core.parse_handoff(f"STATUS: success\nBLOCKERS: {value}")["blockers"]

    def test_none_answers_are_not_blockers(self):
        for value in (
            "none",
            "None",
            "NONE",
            "none.",
            "N/A",
            "N/A.",
            "nothing",
            "nil",
            "-",
            "none - read-only node",
        ):
            with self.subTest(value=value):
                self.assertEqual(self.blockers(value), [])

    def test_prose_is_one_statement_not_comma_fragments(self):
        """These fields become a Laya decision's input state, not just display.

        A worker wrote one coherent sentence about what it ran. Comma-splitting
        turned it into three fragments, and those fragments were fed to the
        acceptance classifier as the state it judged. Debris in, nonsense out.
        """
        written = ('`./test/run.sh` \u2192 exit 0, "all 70 tests passed". '
                   'I did not run `make test`, `make dogfood` or the browser tests.')
        parsed = fusion_core.parse_handoff("STATUS: success\nTESTS: " + written)["tests"]
        self.assertEqual(parsed, [written])

    def test_genuine_lists_still_split(self):
        for value, expected in [
            ("missing fixture, broken import", ["missing fixture", "broken import"]),
            ("src/a.py, src/b.py", ["src/a.py", "src/b.py"]),
            ("pytest -q, ruff check", ["pytest -q", "ruff check"]),
        ]:
            with self.subTest(value=value):
                parsed = fusion_core.parse_handoff("STATUS: success\nCHANGED: " + value)["changed"]
                self.assertEqual(parsed, expected)

    def test_a_filename_period_does_not_make_it_prose(self):
        # The period in src/a.py closes a name, not a sentence.
        self.assertFalse(fusion_core._reads_as_prose("src/a.py, src/b.py"))
        self.assertTrue(fusion_core._reads_as_prose("Ran the suite. It passed."))

    def test_real_blockers_survive(self):
        self.assertEqual(
            self.blockers("the suite fails and I could not fix it"),
            ["the suite fails and I could not fix it"],
        )
        self.assertEqual(
            self.blockers("missing fixture, broken import"),
            ["missing fixture", "broken import"],
        )

    def test_none_prefixed_prose_is_still_a_blocker(self):
        # "none of the tests pass" starts with "none" but is a real failure.
        self.assertEqual(
            self.blockers("none of the tests pass"),
            ["none of the tests pass"],
        )

    def test_none_prefixed_filenames_and_hyphenated_blockers_survive(self):
        for value in ("none.py", "nil-cache.json", "na.test.ts", "nothing.md", "none-of-the-tests-pass"):
            with self.subTest(value=value):
                parsed = fusion_core.parse_handoff(f"CHANGED: {value}\nBLOCKERS: {value}")
                self.assertEqual(parsed['changed'], [value])
                self.assertEqual(parsed['blockers'], [value])

    def test_tests_field_uses_the_same_rule(self):
        parsed = fusion_core.parse_handoff(
            "STATUS: success\nTESTS: none. This node is read-only; I only ran `git status`."
        )
        self.assertEqual(parsed["tests"], [])


class AcceptanceContractTest(unittest.TestCase):
    """The contract is model output that becomes a gate, so it is parsed strictly.

    A malformed contract must not take a run down -- the node's other gates
    still apply -- but it also must not widen what is enforced, and it must
    never name a path outside the workspace.
    """

    def block(self, payload):
        return "planning prose\n\n```acceptance-contract\n" + json.dumps(payload) + "\n```\n"

    def files(self, payload):
        return parse_acceptance_contract(self.block(payload)).get("required_files")

    def test_reads_files_and_verification(self):
        parsed = parse_acceptance_contract(
            self.block({"required_files": ["a.py", "src/b.py"], "verification": ["pytest -q"]})
        )
        self.assertEqual(parsed["required_files"], ["a.py", "src/b.py"])
        self.assertEqual(parsed["verification"], ["pytest -q"])

    def test_refuses_paths_that_leave_the_workspace(self):
        for bad in ("/etc/passwd", "~/.ssh/id_rsa", "../../etc/passwd", "a/../../b"):
            with self.subTest(path=bad):
                self.assertIsNone(self.files({"required_files": [bad]}))

    def test_keeps_the_safe_entries_and_drops_the_rest(self):
        self.assertEqual(self.files({"required_files": ["ok.py", "/etc/passwd", "../x"]}), ["ok.py"])

    def test_ignores_non_strings_and_blanks(self):
        self.assertEqual(self.files({"required_files": [1, None, {"a": 1}, "", "  ", "ok.py"]}), ["ok.py"])

    def test_caps_how_many_files_a_plan_can_demand(self):
        parsed = self.files({"required_files": [f"f{i}.py" for i in range(60)]})
        self.assertEqual(len(parsed), MAX_CONTRACT_FILES)

    def test_requires_exactly_one_block(self):
        one = self.block({"required_files": ["a.py"]})
        self.assertEqual(parse_acceptance_contract(one)["required_files"], ["a.py"])
        # Two blocks is ambiguous: enforcing either one silently picks for the user.
        self.assertEqual(parse_acceptance_contract(one + one), {})
        self.assertEqual(parse_acceptance_contract("no block here"), {})

    def test_malformed_json_yields_no_contract_rather_than_raising(self):
        self.assertEqual(parse_acceptance_contract("```acceptance-contract\n{not json\n```"), {})
        self.assertEqual(parse_acceptance_contract('```acceptance-contract\n["a"]\n```'), {})

    def test_empty_required_files_is_a_valid_contract(self):
        # "this request needs no file change" is a legitimate thing to declare.
        self.assertEqual(parse_acceptance_contract(self.block({"required_files": []})), {})


class McpResponseShapeTest(unittest.TestCase):
    """MCP requires structuredContent to be a JSON object, not an array.

    fusion_status returned a bare list, which a strict client rejects. The
    contract is enforced in mcp_result so a tool cannot reintroduce it, and
    driven here through the real stdio server so the wiring is covered too --
    testing mcp_result alone would not notice a tool that bypassed it.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "repo"
        self.workspace.mkdir(parents=True)
        (self.workspace / ".fusion.json").write_text(json.dumps(
            {"decisions": {"mode": "off"}, "telemetry": {"remote": {"enabled": False}}}))
        self.env = patch.dict(os.environ, {
            "FUSION_TELEMETRY": "0",
            "ORC_HOME": str(Path(self.temp.name) / "orc-home"),
        })
        self.env.start(); self.addCleanup(self.env.stop)

    def test_mcp_result_refuses_a_non_object(self):
        for payload in ([1, 2], "text", 7, None):
            with self.subTest(payload=payload):
                with self.assertRaises(TypeError):
                    fusion_core.mcp_result(payload)

    def test_mcp_result_accepts_an_object(self):
        self.assertEqual(fusion_core.mcp_result({"runs": []})["structuredContent"], {"runs": []})

    def test_every_tool_declares_an_object_output_schema(self):
        tools = fusion_core.tool_definitions()
        self.assertTrue(tools)
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertIn("outputSchema", tool)
                self.assertEqual(tool["outputSchema"].get("type"), "object")
                self.assertEqual(tool["inputSchema"].get("type"), "object")

    def test_the_real_server_returns_objects_for_read_only_tools(self):
        requests = "\n".join(json.dumps(r) for r in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "fusion_status", "arguments": {"limit": 1}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "fusion_here", "arguments": {}}},
        ]) + "\n"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "fusion"), "--workspace", str(self.workspace), "mcp-serve"],
            input=requests, text=True, capture_output=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        responses = {json.loads(line)["id"]: json.loads(line) for line in proc.stdout.splitlines()}
        for call_id, tool in ((2, "fusion_status"), (3, "fusion_here")):
            with self.subTest(tool=tool):
                result = responses[call_id]["result"]
                self.assertFalse(result.get("isError"), result)
                self.assertIsInstance(result["structuredContent"], dict)


if __name__ == "__main__":
    unittest.main()


class VerdictTest(unittest.TestCase):
    def test_session_limit_is_quota_with_reset(self):
        verdict = fusion_core.classify_verdict("error", None, "You've hit your session limit · resets 5:40pm", 1)
        self.assertEqual(verdict["verdict"], "quota")
        self.assertEqual(verdict["reason"], "session limit")
        self.assertTrue(verdict["resets_at"].startswith("5:40pm"))

    def test_success_is_ok(self):
        self.assertEqual(fusion_core.classify_verdict("success", None, "STATUS: success", 0)["verdict"], "ok")

    def test_permission_block(self):
        verdict = fusion_core.classify_verdict("error", None, "the permission check blocked it: this session has no way to approve commands", 1)
        self.assertEqual(verdict["verdict"], "blocked_by_permissions")

    def test_timeout_and_missing_binary_are_errors_with_reasons(self):
        self.assertEqual(fusion_core.classify_verdict("blocked", "timeout after 3600 seconds", "worker timed out", 124)["reason"], "timeout")
        self.assertEqual(fusion_core.classify_verdict("error", None, "claude is not available on PATH", 127)["reason"], "missing_binary")

    def test_refusal(self):
        self.assertEqual(fusion_core.classify_verdict("error", None, "I can't help with that request.", 1)["verdict"], "refused")
