"""OpenCode (`opencode run --format json`) as a Fusion worker and lead."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_policy


def stream(*events):
    return "\n".join(json.dumps(event) for event in events)


def text(value, session="ses_1"):
    return {"type": "text", "sessionID": session, "part": {"type": "text", "text": value}}


def tool(name, status="completed", error=None, tool_input=None, session="ses_1"):
    state = {"status": status, "input": tool_input or {}}
    if error:
        state["error"] = error
    return {"type": "tool_use", "sessionID": session, "part": {"type": "tool", "tool": name, "state": state}}


def finish(reason, cost=0.01, session="ses_1", **tokens):
    counts = {"input": 3, "output": 7, "reasoning": 0, "cache": {"read": 100, "write": 5}, **tokens}
    return {"type": "step_finish", "sessionID": session,
            "part": {"type": "step-finish", "reason": reason, "tokens": counts, "cost": cost}}


HANDOFF = "STATUS: success\nSUMMARY: reviewed\nCHANGED: none\nTESTS: none\nBLOCKERS: none"


class OpenCodeParseTest(unittest.TestCase):
    def test_final_step_text_is_the_handoff_and_usage_sums_steps(self):
        output = stream(
            {"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}},
            text("I'll read the file first."),
            tool("read", tool_input={"filePath": "a.txt"}),
            finish("tool-calls", cost=0.05),
            text(HANDOFF),
            finish("stop", cost=0.002, input=5, output=4),
        )
        session, answer, failure, usage, model, notes = core.parse_opencode_output(output)
        self.assertEqual(session, "ses_1")
        self.assertEqual(answer, HANDOFF)
        self.assertIsNone(failure)
        self.assertIsNone(model)
        self.assertEqual(notes, [])
        self.assertEqual(usage["input_tokens"], 8)
        self.assertEqual(usage["output_tokens"], 11)
        self.assertEqual(usage["cache_read_input_tokens"], 200)
        self.assertEqual(usage["cache_creation_input_tokens"], 10)
        self.assertAlmostEqual(usage["cost_usd"], 0.052)

    def test_zero_cost_with_tokens_is_unknown_not_free(self):
        # A model OpenCode has no price for reports cost 0 after spending tokens.
        _, _, _, usage, _, _ = core.parse_opencode_output(stream(text(HANDOFF), finish("stop", cost=0)))
        self.assertNotIn("cost_usd", usage)
        self.assertEqual(usage["output_tokens"], 7)
        # A step that spent nothing really cost nothing.
        _, _, _, usage, _, _ = core.parse_opencode_output(stream(
            text(HANDOFF), finish("stop", cost=0, input=0, output=0, cache={"read": 0, "write": 0})))
        self.assertEqual(usage["cost_usd"], 0)

    def test_error_event_is_a_failure(self):
        output = stream({"type": "error", "sessionID": "ses_2",
                         "error": {"name": "APIError", "data": {"message": "429 Too Many Requests: rate limit"}}})
        _, _, failure, _, _, _ = core.parse_opencode_output(output)
        self.assertIn("rate limit", failure)
        self.assertTrue(core.quota_failure({"provider_failure": failure}))

    def test_rejected_and_denied_tools_are_permission_evidence(self):
        output = stream(
            text("Creating the file."),
            tool("write", "error", "The user rejected permission to use this specific tool call.", {"filePath": "b.txt"}),
            tool("bash", "error", "The user has specified a rule which prevents you from using this specific tool call.",
                 {"command": "rm keep.txt"}),
            finish("stop"),
        )
        _, answer, failure, _, _, notes = core.parse_opencode_output(output)
        self.assertEqual(answer, "")
        self.assertIsNone(failure)
        self.assertEqual(len(notes), 2)
        self.assertTrue(all(note.startswith("permission denied: ") for note in notes))
        self.assertEqual([item["tool"] for item in core.provider_denials("opencode", output)], ["write", "bash"])
        self.assertEqual(core.blocker_denied_tools(notes), ["write", "bash"])

    def test_stream_that_stops_mid_tool_call_is_incomplete(self):
        _, _, failure, _, _, _ = core.parse_opencode_output(stream(text("Let me check."), tool("read"), finish("tool-calls")))
        self.assertIn("without a final answer", failure)

    def test_non_json_output_is_plain_text(self):
        self.assertEqual(core.parse_opencode_output("plain answer\n")[1], "plain answer")

    def test_unknown_reason_with_text_is_not_a_failure(self):
        # 'unknown' is a valid terminal reason per the OpenCode spec; it should
        # not generate a "stopped without a final answer" failure when text is present.
        output = stream(text(HANDOFF), finish("unknown"))
        _, answer, failure, _, _, _ = core.parse_opencode_output(output)
        self.assertEqual(answer, HANDOFF)
        self.assertIsNone(failure)

    def test_unknown_reason_without_text_is_still_incomplete(self):
        # If the last step reason is 'tool-calls' with no text, it IS a failure.
        output = stream(tool("read"), finish("tool-calls"))
        _, _, failure, _, _, _ = core.parse_opencode_output(output)
        self.assertIsNotNone(failure)
        self.assertIn("tool-calls", failure)


class OpenCodeCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, write=False, mode="restricted", **settings):
        config = core.deep_merge(core.DEFAULTS, {"execution_mode": mode, "opencode": settings})
        task = core.make_task(self.root, "opencode", "Review", "reviewer", [], [], None, False, write)
        return core.agent_command(config, task, None)

    def test_reader_is_deny_by_default_and_blocks_shell_writes(self):
        argv, env, metadata = self.command(model="anthropic/claude-sonnet-4-6", disable_mcp=["example-mcp"])
        self.assertEqual(argv[:4], ["opencode", "run", "--format", "json"])
        self.assertEqual(argv[argv.index("-m") + 1], "anthropic/claude-sonnet-4-6")
        self.assertNotIn("--auto", argv)
        self.assertEqual(argv[-2], "--")
        self.assertIn("Fusion coding harness", argv[-1])
        policy = json.loads(env["OPENCODE_PERMISSION"])
        self.assertEqual((policy["*"], policy["edit"], policy["task"], policy["read"]), ("deny", "deny", "deny", "allow"))
        self.assertEqual(policy["bash"]["*"], "deny")
        self.assertEqual(policy["bash"]["git diff *"], "allow")
        # Trailing denies must come after any allow they override.
        keys = list(policy["bash"])
        self.assertGreater(keys.index("*>*"), keys.index("git diff *"))
        content = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(content["mcp"], {"example-mcp": {"enabled": False}})
        # The policy is pinned on the worker's own agent too: OpenCode applies
        # agent permission over global permission.
        self.assertEqual(argv[argv.index("--agent") + 1], "fusion-worker")
        self.assertEqual(content["agent"]["fusion-worker"]["permission"], policy)
        self.assertEqual(content["agent"]["fusion-worker"]["mode"], "primary")
        self.assertEqual(metadata["model"], "anthropic/claude-sonnet-4-6")

    def test_writer_edits_and_runs_allowed_commands_only(self):
        _, env, _ = self.command(write=True, model="openai/gpt-5.5", bash_allow=["make test"])
        policy = json.loads(env["OPENCODE_PERMISSION"])
        self.assertEqual((policy["*"], policy["edit"], policy["bash"]["*"]), ("deny", "allow", "deny"))
        self.assertEqual(policy["bash"]["make test"], "allow")
        self.assertEqual(policy["bash"]["git *"], "allow")
        self.assertEqual(list(policy["bash"])[-1], "git push*")
        self.assertEqual(policy["bash"]["git push*"], "deny")

    def test_yolo_auto_approves_and_replaces_shell_rules(self):
        argv, env, _ = self.command(mode="yolo", model="xai/grok-4.5")
        self.assertIn("--auto", argv)
        policy = json.loads(env["OPENCODE_PERMISSION"])
        self.assertEqual((policy["*"], policy["bash"], policy["external_directory"]), ("allow", "allow", "allow"))

    def test_effort_is_a_variant_and_session_resumes(self):
        config = core.deep_merge(core.DEFAULTS, {"opencode": {"model": "anthropic/claude-opus-5-5", "reasoning_effort": "max"}})
        task = core.make_task(self.root, "opencode", "Review", "reviewer", [], [], None, False, False)
        argv, _, metadata = core.agent_command(config, task, "ses_prev")
        self.assertEqual(argv[argv.index("--variant") + 1], "max")
        self.assertEqual(argv[argv.index("--session") + 1], "ses_prev")
        self.assertEqual(metadata["execution_choice"]["requested"]["reasoning_effort"], "max")

    def test_route_harness_name_is_not_passed_as_the_opencode_agent(self):
        # A route's "agent" names the Fusion harness; it once leaked through as
        # `--agent opencode`, so OpenCode fell back to the user's default agent
        # and that agent's own permissions overrode the worker policy.
        config = core.deep_merge(core.DEFAULTS, {"routes": {"oc": {"agent": "opencode", "model": "openai/gpt-5.5"}}})
        task = core.make_task(self.root, "opencode", "Review", "reviewer", [], [], None, False, False, route="oc")
        argv, env, _ = core.agent_command(config, task, None)
        self.assertEqual(argv[argv.index("--agent") + 1], "fusion-worker")
        self.assertNotIn("opencode", argv[argv.index("--agent") + 1:argv.index("--agent") + 2])

    def test_named_opencode_agent_gets_the_policy(self):
        argv, env, _ = self.command(opencode_agent="review")
        self.assertEqual(argv[argv.index("--agent") + 1], "review")
        agent = json.loads(env["OPENCODE_CONFIG_CONTENT"])["agent"]["review"]
        self.assertEqual(agent["permission"]["edit"], "deny")
        self.assertNotIn("mode", agent)

    def test_model_must_name_its_provider(self):
        with self.assertRaisesRegex(ValueError, "provider/model"):
            self.command(model="claude-sonnet-4-6")

    def test_user_permission_overrides_merge_last(self):
        _, env, _ = self.command(permission={"webfetch": "allow"})
        self.assertEqual(json.loads(env["OPENCODE_PERMISSION"])["webfetch"], "allow")

    def test_route_permission_cannot_unlock_edit_on_read_only_worker(self):
        # A misconfigured route that sets permission: {edit: allow} must not
        # grant a read-only worker write access.  Core read-only denies are
        # re-applied after user extras.
        _, env, _ = self.command(write=False, permission={"edit": "allow", "external_directory": "allow"})
        policy = json.loads(env["OPENCODE_PERMISSION"])
        self.assertEqual(policy["edit"], "deny")
        self.assertEqual(policy["external_directory"], "deny")

    def test_route_permission_cannot_unlock_edit_on_write_worker(self):
        # git push must stay denied even if permission: {"git push*": "allow"} is set.
        _, env, _ = self.command(write=True, model="openai/gpt-5.5",
                                  permission={"bash": {"git push*": "allow"}})
        policy = json.loads(env["OPENCODE_PERMISSION"])
        self.assertEqual(policy["bash"]["git push*"], "deny")

    def test_reader_shell_overrides_keep_trailing_write_denies(self):
        _, env, _ = self.command(permission={"bash": "allow"})
        bash = json.loads(env["OPENCODE_PERMISSION"])["bash"]
        self.assertEqual(bash["*"], "allow")
        self.assertEqual(list(bash)[-1], "git push*")
        self.assertGreater(list(bash).index("*>*"), list(bash).index("*"))

    def test_user_rules_cannot_follow_the_push_deny(self):
        _, env, _ = self.command(write=True, model="openai/gpt-5.5",
                                 permission={"bash": {"git push origin*": "allow"}})
        bash = json.loads(env["OPENCODE_PERMISSION"])["bash"]
        self.assertEqual(list(bash)[-1], "git push*")

    def test_lead_refuses_unknown_variant(self):
        config = core.deep_merge(core.DEFAULTS, {"opencode": {"model": "openai/gpt-5.5", "reasoning_effort": "ultra"}})
        with self.assertRaisesRegex(ValueError, "opencode accepts reasoning_effort"):
            core.launch_lead(self.root, config, "opencode", None, interactive=False)

    def test_lead_model_must_name_its_provider(self):
        config = core.deep_merge(core.DEFAULTS, {"opencode": {"model": "claude-sonnet-4-6"}})
        with self.assertRaisesRegex(ValueError, "provider/model"):
            core.launch_lead(self.root, config, "opencode", None, interactive=False)

    def test_lead_passes_variant_and_agent(self):
        # Non-interactive lead should pass --variant and --agent when configured.
        import unittest.mock as mock
        config = core.deep_merge(core.DEFAULTS, {
            "opencode": {"model": "anthropic/claude-opus-5-5", "reasoning_effort": "high",
                         "opencode_agent": "coding", "command": "opencode"}
        })
        launched = []
        with mock.patch("subprocess.run", side_effect=lambda a, **kw: launched.append(a) or mock.MagicMock(returncode=0)), \
             mock.patch.object(core, "executable", return_value="opencode"):
            core.launch_lead(self.root, config, "opencode", "Do the work", interactive=False)
        self.assertTrue(launched)
        argv = launched[0]
        self.assertIn("--variant", argv)
        self.assertEqual(argv[argv.index("--variant") + 1], "high")
        self.assertIn("--agent", argv)
        self.assertEqual(argv[argv.index("--agent") + 1], "coding")

    def test_auto_routing_requires_an_explicit_model(self):
        config = {"sidekick": "codex", "decisions": {"mode": "off", "priors": False},
                  "routes": {"oc-sonnet": {"agent": "opencode", "model": "anthropic/claude-sonnet-4-6",
                                           "account": "gateway/anthropic"}}}
        task = core.make_task(self.root, "auto", "fixture", "review", [], [], None, False, False)
        rejected = {}
        env = {"CODEX_HOME": str(self.root / "codex"), "ORC_HOME": str(self.root / "orc"),
               "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off"}
        with patch.dict(os.environ, env), patch.object(core, "executable", return_value=True):
            keys = {c["key"] for c in fusion_policy.route_candidates(config, task, core.RunStore(self.root), rejected=rejected)}
        self.assertIn("oc-sonnet", keys)
        self.assertNotIn("opencode", keys)
        self.assertIn("explicit provider/model", rejected["opencode"])
        self.assertEqual(core.lane_key("opencode", config["routes"]["oc-sonnet"]), "opencode@gateway/anthropic")


class OpenCodeEmptyStepTest(unittest.TestCase):
    def empty(self):
        return json.dumps(finish("unknown", cost=0, input=0, output=0, reasoning=0, cache={"read": 0, "write": 0})).encode()

    def test_guard_counts_only_consecutive_empty_steps(self):
        guard = core.opencode_empty_step_guard(3)
        self.assertIsNone(guard(self.empty()))
        self.assertIsNone(guard(self.empty()))
        self.assertIsNone(guard(json.dumps(text("progress")).encode()))
        self.assertIsNone(guard(self.empty()))  # resets: that step produced text
        self.assertIsNone(guard(self.empty()))
        self.assertIsNone(guard(self.empty()))
        self.assertIn("3 empty responses", guard(self.empty()))
        self.assertIsNone(core.opencode_empty_step_guard(1)(json.dumps(finish("stop")).encode()))
        self.assertIsNone(guard(b"not json"))

    def test_dispatch_stops_a_silently_failing_provider_early(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "off", "FUSION_TELEMETRY": "0"}):
            root = Path(d)
            worker = root / "opencode-fixture"
            empty = finish("unknown", cost=0, input=0, output=0, reasoning=0, cache={"read": 0, "write": 0})
            worker.write_text(f"#!{sys.executable}\nimport json,sys,time\nwhile True:\n    print(json.dumps({empty!r}), flush=True)\n    time.sleep(.05)\n")
            worker.chmod(0o755)
            config = core.deep_merge(core.DEFAULTS, {"opencode": {"command": str(worker), "model": "google/gemini-x",
                                                                  "empty_step_limit": 4}, "decisions": {"mode": "off"}})
            task = core.make_task(root, "opencode", "Review", "review", [], [], None, False, False)
            task["timeout_seconds"] = 120
            started = time.monotonic()
            result = core.dispatch(config, task, core.RunStore(root))
            self.assertLess(time.monotonic() - started, 30)
            self.assertEqual((result["status"], result["exit_code"]), ("error", 125), result)
            self.assertIn("empty responses", " ".join(result["blockers"]))
            self.assertEqual(core.failure_class(result), "worker_error")

    def test_limit_is_validated(self):
        config = core.deep_merge(core.DEFAULTS, {"opencode": {"empty_step_limit": -1}})
        task = core.make_task(Path(tempfile.gettempdir()), "opencode", "x", "review", [], [], None, False, False)
        with self.assertRaisesRegex(ValueError, "empty_step_limit"):
            core.agent_command(config, task, None)


class OpenCodeDispatchTest(unittest.TestCase):
    def test_dispatch_records_session_usage_and_public_handoff(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "off", "FUSION_TELEMETRY": "0"}):
            root = Path(d)
            worker = root / "opencode-fixture"
            events = [text("Inspecting first."), tool("read"), finish("tool-calls"), text(HANDOFF), finish("stop", cost=0.003)]
            worker.write_text(f"#!{sys.executable}\n" + f"""import json,os,sys
assert sys.argv[1:4]==['run','--format','json'], sys.argv
assert json.loads(os.environ['OPENCODE_PERMISSION'])['edit']=='deny'
for event in {events!r}:
    print(json.dumps(event))
""")
            worker.chmod(0o755)
            config = core.deep_merge(core.DEFAULTS, {"opencode": {"command": str(worker), "model": "google/gemini-2.5-pro"},
                                                     "decisions": {"mode": "off"}})
            task = core.make_task(root, "opencode", "Review fixture", "review", [], [], None, False, False)
            store = core.RunStore(root)
            result = core.dispatch(config, task, store)
            self.assertEqual(result["status"], "success", result)
            self.assertEqual(result["summary"], "reviewed")
            self.assertEqual(result["model"], "google/gemini-2.5-pro")
            self.assertAlmostEqual(result["usage"]["cost_usd"], 0.013)
            self.assertNotIn("Inspecting", Path(result["artifacts"]["answer"]).read_text())
            self.assertIn("ses_1", store.sessions().values())

    def test_denied_edit_fails_as_permission_denied(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "off", "FUSION_TELEMETRY": "0"}):
            root = Path(d)
            worker = root / "opencode-fixture"
            events = [tool("edit", "error", "The user rejected permission to use this specific tool call.", {"filePath": "x"}),
                      finish("stop")]
            worker.write_text(f"#!{sys.executable}\nimport json\nfor event in {events!r}:\n    print(json.dumps(event))\n")
            worker.chmod(0o755)
            config = core.deep_merge(core.DEFAULTS, {"opencode": {"command": str(worker), "model": "openai/gpt-5.5"},
                                                     "decisions": {"mode": "off"}})
            task = core.make_task(root, "opencode", "Edit x", "implementer", [], [], None, False, True)
            result = core.dispatch(config, task, core.RunStore(root))
            self.assertEqual(result["status"], "error", result)
            self.assertEqual(core.failure_class(result), "permission_denied")


if __name__ == "__main__":
    unittest.main()


class OpenCodeCouncilTest(unittest.TestCase):
    """Several OpenCode routes (one per provider) can sit on one labeling council."""
    config = {"routes": {"oc-gpt": {"agent": "opencode", "model": "openai/gpt-5.5"},
                         "oc-gemini": {"agent": "opencode", "model": "google/gemini-2.5-pro"},
                         "bad": {"agent": "nope"}}}

    def test_routes_are_council_members(self):
        import fusion_labeling as labeling
        self.assertEqual(labeling.member_lane(self.config, "oc-gpt"), ("opencode", "oc-gpt"))
        self.assertEqual(labeling.member_lane(self.config, "claude"), ("claude", None))
        options = labeling.labeling_options("council", ["oc-gpt", "oc-gemini", "claude"], self.config)
        self.assertEqual(options["council_agents"], ["oc-gpt", "oc-gemini", "claude"])
        for member in ("bad", "missing"):
            with self.assertRaisesRegex(ValueError, "configured routes"):
                labeling.labeling_options("council", ["claude", member], self.config)

    def test_assessment_dispatches_the_route(self):
        import fusion_labeling as labeling
        seen = []

        def capture(config, task, store):
            seen.append(task)
            raise RuntimeError("stop after dispatch")
        with tempfile.TemporaryDirectory() as d, patch.object(core, "dispatch", side_effect=capture):
            record = {"kind": "review", "questions": {}}
            with self.assertRaises(RuntimeError):
                labeling.assessment(Path(d), self.config, record, [], "oc-gemini")
        self.assertEqual((seen[0]["agent"], seen[0]["route"]), ("opencode", "oc-gemini"))


class PinnedLaneSessionTest(unittest.TestCase):
    """Delegates pinned to different routes of one harness keep separate sessions."""

    def test_routes_and_models_do_not_share_a_resumed_session(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"FUSION_DECISIONS_MODE": "off", "FUSION_TELEMETRY": "0"}):
            root = Path(d)
            worker = root / "opencode-fixture"
            worker.write_text(f"#!{sys.executable}\n" + """import json,sys
session = sys.argv[sys.argv.index('--session') + 1] if '--session' in sys.argv else None
model = sys.argv[sys.argv.index('-m') + 1]
print(json.dumps({'type': 'text', 'sessionID': 'ses-' + model.replace('/', '-'), 'part': {'type': 'text', 'text': 'STATUS: success' + chr(10) + 'SUMMARY: ' + str(session)}}))
print(json.dumps({'type': 'step_finish', 'sessionID': 'ses-' + model.replace('/', '-'), 'part': {'reason': 'stop', 'tokens': {'input': 1, 'output': 1}, 'cost': 0.001}}))
""")
            worker.chmod(0o755)
            config = core.deep_merge(core.DEFAULTS, {"opencode": {"command": str(worker)}, "decisions": {"mode": "off"},
                                                     "routes": {"a": {"agent": "opencode", "model": "x/a"},
                                                                "b": {"agent": "opencode", "model": "x/b"}}})
            store = core.RunStore(root)

            def delegate(route, model=None):
                task = core.make_task(root, "opencode", "Review", "reviewer", [], [], None, True, False, route=route,
                                      settings_overrides={"model": model} if model else None)
                return core.dispatch(config, task, store), task

            first, task_a = delegate("a")
            second, task_b = delegate("b")
            again, _ = delegate("a")
            self.assertNotEqual(task_a["session_key"], task_b["session_key"])
            self.assertEqual(first["summary"], "None")
            self.assertEqual(second["summary"], "None")          # b did not resume a's session
            self.assertEqual(again["summary"], "ses-x-a")        # a resumes its own
            override, task_c = delegate("a", "x/c")
            self.assertEqual(override["summary"], "None")        # a model override is its own lane
            self.assertIn(":lane=a:x/c", task_c["session_key"])
