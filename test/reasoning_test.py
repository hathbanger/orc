"""Real local worker fixtures; no model, network, install, or native auth calls."""
import copy
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
from fusion_decisions import DecisionEngine, read_jsonl
from fusion_policy import route_task
from fusion_reasoning import native_capability, pair_key
from fusion_workflow import WorkflowRunner, validate_spec

ASTRA = {"model": "gpt-6-astra", "reasoning_effort": "high"}
SOL = {"model": "gpt-6-sol", "reasoning_effort": "xhigh"}


class Advice:
    def __init__(self, selected=None):
        self.selected = selected or pair_key(SOL)

    def predict(self, state, questions):
        keys = questions["route"]["criteria"]
        return {"model_identity": "fixture-only", "answers": {"route": {"probabilities": {
            key: .99 if key == self.selected else .01 / (len(keys) - 1) for key in keys}}}}


class ReasoningTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.home = self.root / "codex-home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.home), "FUSION_TELEMETRY": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("FUSION_DECISIONS_MODE", None)
        self.catalog = {"client_version": "fixture", "fetched_at": "fixture", "models": [
            {"slug": pair["model"], "supported_reasoning_levels": [{"effort": pair["reasoning_effort"]}]}
            for pair in (ASTRA, SOL)]}
        (self.home / "models_cache.json").write_text(json.dumps(self.catalog))
        self.worker = self.root / "fixture-codex"
        self.worker.write_text(f'#!{sys.executable}\n' + '''import json, sys
sys.stdin.read()
print(json.dumps({"type":"thread.started","thread_id":"fixture-thread"}))
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"STATUS: success\\nSUMMARY: fixture completed\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none"}}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":3}}))
''')
        self.worker.chmod(0o700)
        self.config = core.deep_merge(core.DEFAULTS, {"codex": {"command": str(self.worker), **ASTRA, "git_write": False},
            "decisions": {"mode": "off"}, "telemetry": {"remote": {"enabled": False}}, "timeout_seconds": 10})

    def task(self, pair=None, resume=False):
        return core.make_task(self.workspace, "codex", "Inspect fixture", "inspect", [], [], "same-task", resume, False,
                              settings_overrides=pair or {})

    def test_pair_is_explicit_in_fresh_and_resume_argv_without_weakening_permissions(self):
        for session in (None, "fixture-session"):
            argv, _, metadata = core.agent_command(self.config, self.task(SOL), session)
            self.assertEqual(argv[argv.index("-m") + 1], SOL["model"])
            self.assertIn('model_reasoning_effort="xhigh"', argv)
            self.assertLess(argv.index('model_reasoning_effort="xhigh"'), argv.index("exec"))
            self.assertEqual(argv[argv.index("-s") + 1], "read-only")
            self.assertEqual(argv[-1], "-")
            self.assertEqual(metadata["execution_choice"]["requested"], SOL)
            self.assertEqual(metadata["execution_choice"]["observed"]["status"], "unobserved")

    def test_cached_capabilities_reject_invalid_pair_and_missing_cache_stays_unknown(self):
        with self.assertRaisesRegex(ValueError, "does not support"):
            core.agent_command(self.config, self.task({**ASTRA, "reasoning_effort": "max"}), None)
        with self.assertRaisesRegex(ValueError, "unsupported reasoning_effort"):
            core.agent_command(self.config, self.task({**ASTRA, "reasoning_effort": "high -s danger-full-access"}), None)
        (self.home / "models_cache.json").unlink()
        self.assertEqual(native_capability(ASTRA)["status"], "unchecked")
        (self.home / "models_cache.json").write_text("invalid")
        self.assertEqual(native_capability(ASTRA)["status"], "unchecked")

    def test_missing_model_and_other_harness_effort_are_refused(self):
        with self.assertRaisesRegex(ValueError, "explicit model"):
            core.agent_command(self.config, self.task({"model": "", "reasoning_effort": "high"}), None)
        task = self.task(ASTRA)
        task["agent"] = "grok"
        with self.assertRaisesRegex(ValueError, "native Codex, Claude Code, agy and OpenCode"):
            core.agent_command(self.config, task, None)
        with self.assertRaisesRegex(ValueError, "explicit Codex, Claude, agy or OpenCode"):
            validate_spec({"nodes": [{"id": "one", "task": "inspect", "agent": "auto", **ASTRA}]})

    def test_claude_effort_is_passed_and_recorded_but_not_claimed(self):
        task = self.task({"model": "claude-sonnet-5", "reasoning_effort": "high"})
        task["agent"] = "claude"
        argv, _, metadata = core.agent_command(self.config, task, None)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertLess(argv.index("--effort"), argv.index("--"))
        choice = metadata["execution_choice"]
        self.assertEqual(choice["requested"], {"model": "claude-sonnet-5", "reasoning_effort": "high"})
        self.assertEqual((choice["catalog"]["status"], choice["observed"]["status"]), ("unchecked", "unobserved"))
        for effort in ("ultra", "minimal", "none"):
            with self.subTest(effort=effort):
                task = self.task({"model": "claude-sonnet-5", "reasoning_effort": effort})
                task["agent"] = "claude"
                with self.assertRaisesRegex(ValueError, "Claude Code accepts"):
                    core.agent_command(self.config, task, None)
        plain = self.task({"model": "claude-sonnet-5"})
        plain["agent"] = "claude"
        argv, _, metadata = core.agent_command(self.config, plain, None)
        self.assertNotIn("--effort", argv)
        self.assertNotIn("execution_choice", metadata)

    def test_real_fixture_dispatch_persists_choice_without_claiming_effective_effort(self):
        task = self.task(SOL)
        result = core.dispatch(self.config, task, core.RunStore(self.workspace))
        self.assertEqual(result["status"], "success")
        choice = result["execution_choice"]
        self.assertEqual(choice["requested"], SOL)
        self.assertEqual(choice["dispatch"]["status"], "returned")
        self.assertIn('model_reasoning_effort="xhigh"', choice["dispatch"]["argv"])
        self.assertEqual(choice["observed"]["status"], "unobserved")
        self.assertIsNone(choice["observed"]["reasoning_effort"])
        stored = json.loads(Path(result["artifacts"]["run_dir"], "result.json").read_text())
        self.assertEqual(stored["execution_choice"], choice)
        self.assertEqual(core.RunStore(self.workspace).traces()[0]["execution_choice"], choice)

    def test_changed_pair_cannot_resume_the_previous_pair_session(self):
        first = self.task(ASTRA)
        core.dispatch(self.config, first, core.RunStore(self.workspace))
        same = self.task(ASTRA, True)
        result = core.dispatch(self.config, same, core.RunStore(self.workspace))
        self.assertIn("resume", result["execution_choice"]["dispatch"]["argv"])
        other = self.task(SOL, True)
        result = core.dispatch(self.config, other, core.RunStore(self.workspace))
        self.assertNotIn("resume", result["execution_choice"]["dispatch"]["argv"])
        self.assertNotEqual(first["session_key"], other["session_key"])

    def test_shadow_and_active_advice_never_replace_explicit_pair(self):
        for mode in ("shadow", "active"):
            config = core.deep_merge(self.config, {"decisions": {"mode": mode, "model_effort_pairs": [ASTRA, SOL]}})
            engine = DecisionEngine(self.workspace, config, Advice())
            task = self.task(ASTRA)
            with patch("fusion_policy.DecisionEngine", return_value=engine), patch.object(engine, "allowed", return_value=True):
                route_task(config, task, core.RunStore(self.workspace))
            self.assertEqual(task["settings_overrides"], ASTRA)
            events = read_jsonl(engine.store.path)
            decision = next(e for e in events if e.get("id") == task["decisions"]["routing"] and e["event"] == "decision")
            self.assertEqual(decision["recommendations"]["route"]["value"], pair_key(SOL))
            applied = events[-1]
            self.assertFalse(applied["applied"])
            self.assertEqual(applied["actual"], pair_key(ASTRA))

    def test_shadow_failure_and_legacy_unspecified_do_not_invent_effort(self):
        config = copy.deepcopy(self.config)
        config["codex"].pop("reasoning_effort")
        argv, _, metadata = core.agent_command(config, self.task(), None)
        self.assertFalse(any(item.startswith("model_reasoning_effort=") for item in argv))
        self.assertIsNone(metadata["execution_choice"]["requested"]["reasoning_effort"])
        self.assertEqual(metadata["execution_choice"]["catalog"]["status"], "unchecked")
        config = core.deep_merge(self.config, {"decisions": {"mode": "shadow", "model_effort_pairs": [ASTRA, SOL]}})
        engine = DecisionEngine(self.workspace, config, Advice())
        with patch.object(engine.backend, "predict", side_effect=RuntimeError("fixture unavailable")), patch("fusion_policy.DecisionEngine", return_value=engine):
            task = self.task(ASTRA)
            route_task(config, task, core.RunStore(self.workspace))
        self.assertEqual(task["settings_overrides"], ASTRA)
        self.assertFalse(read_jsonl(engine.store.path)[-1]["applied"])

    def test_laya_advises_among_claude_pairs_and_keeps_the_pin(self):
        opus = {"agent": "claude", "model": "claude-opus-5-5", "reasoning_effort": "high"}
        fable = {"agent": "claude", "model": "claude-fable-5-1", "reasoning_effort": "medium"}
        config = core.deep_merge(self.config, {"claude": {"command": sys.executable},
                                               "decisions": {"mode": "shadow", "model_effort_pairs": [ASTRA, opus, fable]}})
        engine = DecisionEngine(self.workspace, config, Advice())
        task = core.make_task(self.workspace, "claude", "Inspect fixture", "inspect", [], [], None, False, False,
                              settings_overrides={"model": opus["model"], "reasoning_effort": "high"})
        with patch("fusion_policy.DecisionEngine", return_value=engine):
            route_task(config, task, core.RunStore(self.workspace))
        decision = next(e for e in read_jsonl(engine.store.path) if e.get("event") == "decision")
        criteria = decision["questions"]["route"]["criteria"]
        self.assertEqual(len(criteria), 2)
        self.assertTrue(all(text.startswith("claude ") for text in criteria.values()))
        self.assertEqual(task["settings_overrides"], {"model": opus["model"], "reasoning_effort": "high"})
        self.assertFalse(read_jsonl(engine.store.path)[-1]["applied"])
        # Pairs for another harness are not candidates, and no claude pairs means no question.
        codex_only = core.deep_merge(self.config, {"claude": {"command": sys.executable},
                                                   "decisions": {"mode": "shadow", "model_effort_pairs": [ASTRA, SOL]}})
        task = core.make_task(self.workspace, "claude", "Inspect fixture", "inspect", [], [], None, False, False,
                              settings_overrides={"model": opus["model"], "reasoning_effort": "high"})
        with patch("fusion_policy.DecisionEngine", return_value=DecisionEngine(self.workspace, codex_only, Advice())):
            route_task(codex_only, task, core.RunStore(self.workspace))
        self.assertEqual(task["settings_overrides"], {"model": opus["model"], "reasoning_effort": "high"})

    def test_harness_specific_effort_rules(self):
        from fusion_reasoning import check_pair, claude_choice
        with self.assertRaisesRegex(ValueError, "does not support reasoning effort"):
            check_pair("claude", {"model": "claude-haiku-4-5-20251001", "reasoning_effort": "low"})
        self.assertEqual(claude_choice({"model": "claude-opus-4-6", "reasoning_effort": "xhigh"})["catalog"]["status"],
                         "requested_may_downgrade")
        with self.assertRaisesRegex(ValueError, "agy accepts reasoning_effort high, low, max, medium"):
            check_pair("agy", {"model": "gemini-3-pro", "reasoning_effort": "xhigh"})
        config = core.deep_merge(self.config, {"agy": {"command": sys.executable}})
        task = core.make_task(self.workspace, "agy", "Inspect fixture", "inspect", [], [], None, False, False,
                              settings_overrides={"model": "gemini-3-pro", "reasoning_effort": "medium"})
        argv, _, metadata = core.agent_command(config, task, None)
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.assertEqual(metadata["execution_choice"]["observed"]["status"], "unobserved")

    def test_opencode_effort_does_not_read_codex_native_catalog(self):
        """OpenCode --variant is validated by the provider; Codex models_cache.json must not be consulted."""
        import fusion_policy
        # An opencode lane with a gateway model id (not in the Codex catalog)
        # and a valid OPENCODE_EFFORT must be a routing candidate.
        opencode_worker = self.root / "opencode-fixture"
        opencode_worker.write_text(f"#!{sys.executable}\npass\n")
        opencode_worker.chmod(0o700)
        config = core.deep_merge(self.config, {
            "routes": {"oc-gateway": {"agent": "opencode", "command": str(opencode_worker),
                                   "model": "anthropic/claude-opus-5-5", "reasoning_effort": "high"}},
            "decisions": {"mode": "off"},
        })
        task = core.make_task(self.workspace, "auto", "fixture", "review", [], [], None, False, False)
        rejected = {}
        with patch.object(core, "executable", return_value=str(opencode_worker)):
            candidates = fusion_policy.route_candidates(config, task, core.RunStore(self.workspace), rejected=rejected)
        keys = {c["key"] for c in candidates}
        self.assertIn("oc-gateway", keys, f"oc-gateway was rejected: {rejected.get('oc-gateway')}")
        # ultra must still be rejected for opencode (it is a Codex-only effort level).
        config2 = core.deep_merge(config, {"routes": {"oc-ultra": {"agent": "opencode",
                                                                     "command": str(opencode_worker),
                                                                     "model": "anthropic/claude-opus-5-5",
                                                                     "reasoning_effort": "ultra"}}})
        rejected2 = {}
        with patch.object(core, "executable", return_value=str(opencode_worker)):
            fusion_policy.route_candidates(config2, task, core.RunStore(self.workspace), rejected=rejected2)
        self.assertIn("oc-ultra", rejected2)

    def test_named_route_keeps_model_and_effort_together(self):
        config = core.deep_merge(self.config, {"decisions": {"mode": "active"}, "routes": {
            "sol": {"agent": "codex", "command": str(self.worker), **SOL}}})
        engine = DecisionEngine(self.workspace, config, Advice("sol"))
        task = self.task()
        task["agent"] = "auto"
        with patch("fusion_policy.DecisionEngine", return_value=engine), patch.object(engine, "allowed", return_value=True):
            route_task(config, task, core.RunStore(self.workspace))
        self.assertEqual(task["route"], "sol")
        self.assertEqual(task["settings_overrides"], SOL)

    def test_ultra_requires_native_delegation_and_read_only_scope(self):
        self.catalog["models"][0]["supported_reasoning_levels"].append({"effort": "ultra"})
        (self.home / "models_cache.json").write_text(json.dumps(self.catalog))
        ultra = {**ASTRA, "reasoning_effort": "ultra"}
        with self.assertRaisesRegex(ValueError, "explicit allow_native_delegation"):
            core.agent_command(self.config, self.task(ultra), None)
        task = self.task({**ultra, "allow_native_delegation": True})
        _, _, metadata = core.agent_command(self.config, task, None)
        self.assertEqual(metadata["execution_choice"]["native_delegation"], {"allowed": True, "child_trace_visibility": "unobserved"})
        task["write"] = True
        with self.assertRaisesRegex(ValueError, "read-only workers"):
            core.agent_command(self.config, task, None)
        from fusion_reasoning import pair_candidates
        config = {"decisions": {"model_effort_pairs": [ASTRA, ultra, SOL]}}
        self.assertNotIn(ultra, pair_candidates(config, {**ASTRA, "allow_native_delegation": False}))
        self.assertNotIn(ultra, pair_candidates(config, {**ASTRA, "allow_native_delegation": True}, write=True))
        self.assertIn(ultra, pair_candidates(config, {**ASTRA, "allow_native_delegation": True}, write=False))

    def test_workflow_node_overrides_execute_and_pair_changes_invalidate_receipts(self):
        spec = {"task": "inspect", "nodes": [{"id": "inspect", "agent": "codex", "task": "inspect", **SOL}],
                "acceptance": {"required_handoff": ["summary"]}}
        runner = WorkflowRunner(self.workspace, self.config, spec)
        original = runner._definition_digest(runner.nodes["inspect"])
        report = runner.run()
        result = runner.nodes["inspect"]["result"]
        self.assertEqual(result["execution_choice"]["requested"], SOL)
        self.assertEqual(result["execution_choice"]["dispatch"]["status"], "returned")
        runner.nodes["inspect"].update(ASTRA)
        self.assertNotEqual(original, runner._definition_digest(runner.nodes["inspect"]))
        legacy = copy.deepcopy(runner.nodes["inspect"])
        legacy.pop("reasoning_effort")
        runner.config["codex"].pop("reasoning_effort")
        payload = {"task": legacy["task"], "role": legacy["role"], "agent": legacy["agent"], "route": legacy.get("route"),
                   "write": legacy["write"], "required_files": legacy["required_files"], "acceptance": legacy.get("acceptance") or {}}
        self.assertEqual(runner._definition_digest(legacy), hashlib.sha256(core.json_text(payload).encode()).hexdigest())


if __name__ == "__main__":
    unittest.main()


class PinOutsideAdviceListTest(unittest.TestCase):
    def test_a_pin_outside_the_advice_list_runs_without_advice(self):
        from fusion_reasoning import pair_candidates
        config = {"decisions": {"model_effort_pairs": [
            {"agent": "claude", "model": "claude-opus-5-5", "reasoning_effort": "high"},
            {"agent": "claude", "model": "claude-sonnet-5", "reasoning_effort": "high"}]}}
        pinned = {"model": "claude-opus-5-5", "reasoning_effort": "medium"}
        self.assertEqual(pair_candidates(config, pinned, agent="claude"), [])
        listed = {"model": "claude-opus-5-5", "reasoning_effort": "high"}
        self.assertEqual(pair_candidates(config, listed, agent="claude")[0], listed)
