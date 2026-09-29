"""Permission-denial lane cooldown is scoped to baseline tools (issue #99); fixtures only."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
from fusion_policy import route_candidates


class DeniedToolsParsingTest(unittest.TestCase):
    def test_claude_permission_denials_in_both_seen_shapes(self):
        stdout = json.dumps({"result": "ok", "permission_denials": [
            {"tool_name": "Bash", "reason": "command not in allowlist"},
            {"tool_name": "mcp__example__run_status", "tool_input": {"run_id": "x"}},
            {"name": "bash"},
            {"some_unexpected_field": "value"},
        ]})
        self.assertEqual(core.provider_denied_tools("claude", stdout), ["Bash", "mcp__example__run_status"])

    def test_agy_denied_actions_prefer_display_name(self):
        stdout = json.dumps({"status": "SUCCESS", "response": "", "denied_actions": [
            {"action": "command", "display_name": "RunCommand"}, {"action": "view_file"}]})
        self.assertEqual(core.provider_denied_tools("agy", stdout), ["RunCommand", "view_file"])

    def test_non_json_and_other_agents_name_nothing(self):
        self.assertEqual(core.provider_denied_tools("claude", "plain text"), [])
        self.assertEqual(core.provider_denied_tools("grok", json.dumps({"permission_denials": [{"tool_name": "Read"}]})), [])

    def test_denied_inputs_are_compact_and_truncated_after_serialization(self):
        tool_input = {"command": "echo café " + "x" * 150, "cwd": "/repo"}
        stdout = json.dumps({"permission_denials": [
            {"tool_name": " Bash(command) ", "tool_input": tool_input},
            {"tool_name": "Bash", "tool_input": "y" * 121},
            {"tool_name": "Read", "tool_input": "z" * 120},
        ]})
        self.assertEqual(core.provider_denials("claude", stdout), [
            {"tool": "Bash", "input_head": json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))[:120]},
            {"tool": "Bash", "input_head": "y" * 120},
            {"tool": "Read", "input_head": "z" * 120},
        ])
        self.assertEqual(core.provider_denied_tools("claude", stdout), ["Bash", "Read"])

    def test_agy_denied_inputs_and_missing_inputs(self):
        stdout = json.dumps({"denied_actions": [
            {"action": "command", "display_name": "RunCommand", "input": {"command": "make", "cwd": "/repo"}},
            {"action": "view_file", "tool_input": "/repo/file"},
            {"action": "command", "input": "x" * 121},
            {"action": "view_file"},
            {"action": "view_file", "input": None},
        ]})
        self.assertEqual(core.provider_denials("agy", stdout), [
            {"tool": "RunCommand", "input_head": '{"command":"make","cwd":"/repo"}'},
            {"tool": "view_file", "input_head": "/repo/file"},
            {"tool": "command", "input_head": "x" * 120},
            {"tool": "view_file", "input_head": ""},
            {"tool": "view_file", "input_head": ""},
        ])

    def test_missing_or_malformed_denials_are_empty(self):
        for agent in ("claude", "agy"):
            key = "permission_denials" if agent == "claude" else "denied_actions"
            for stdout in ("plain text", "[]", "null", "{}", json.dumps({key: {"tool_name": "Read"}}),
                           json.dumps({key: [None, "Read", {}, {"tool_name": "()"}]})):
                with self.subTest(agent=agent, stdout=stdout):
                    self.assertEqual(core.provider_denials(agent, stdout), [])
        self.assertEqual(core.provider_denials("grok", json.dumps({"permission_denials": [{"tool_name": "Read"}]})), [])

    def test_blocker_fallback(self):
        blockers = ["permission denied: Write(/etc/hosts)", "agy denied: RunCommand: blocked",
                    "agy auto-denied tools in headless mode: ViewFile, RunCommand; configure sandboxed commands",
                    "agy auto-denied tools in headless mode: tool; configure", "permission denied",
                    'permission denied: {"some_unexpected_field": "value"}']
        self.assertEqual(core.blocker_denied_tools(blockers), ["Write", "RunCommand", "ViewFile"])

    def test_baseline_rule(self):
        for denied, blocks in ((["Bash"], False), (["mcp__example__run_status"], False), (["RunCommand"], False),
                               (["Read"], True), (["read"], True), (["Bash", "Edit"], True), (["ViewFile"], True),
                               (["view_file"], True), (["Glob"], True), (["LS"], True), ([], True)):
            with self.subTest(denied=denied):
                self.assertEqual(core.denial_blocks_lane({"denied_tools": denied}), blocks)
        self.assertTrue(core.denial_blocks_lane({}))


class PermissionCooldownRoutingTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc-home"), "FUSION_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.config = core.deep_merge(core.DEFAULTS, {"execution_mode": "yolo", "routes": {}, "decisions": {"mode": "off"}})
        for agent in ("codex", "claude", "agy", "grok"):
            self.config[agent]["command"] = sys.executable
        self.store = core.RunStore(self.workspace)

    def agents(self, span):
        span = {"agent": "claude", "execution_mode": "yolo", "end_time_ms": core.now_ms(), **span}
        task = core.make_task(self.workspace, "auto", "Check the fixture", "review", [], [], None, True, False)
        with patch.object(self.store, "traces", return_value=[span]):
            return [choice["agent"] for choice in route_candidates(self.config, task, self.store)]

    def test_bash_only_denial_keeps_the_lane(self):
        self.assertIn("claude", self.agents({"failure_class": "permission_denied", "denied_tools": ["Bash"]}))

    def test_baseline_denial_cools_the_lane_down(self):
        self.assertNotIn("claude", self.agents({"failure_class": "permission_denied", "denied_tools": ["Read"]}))
        self.assertNotIn("agy", self.agents({"agent": "agy", "failure_class": "permission_denied", "denied_tools": ["ViewFile"]}))

    def test_quota_cools_the_lane_down(self):
        self.assertNotIn("claude", self.agents({"failure_class": "quota", "denied_tools": []}))

    def test_legacy_span_without_denied_tools_keeps_the_cooldown(self):
        self.assertNotIn("claude", self.agents({"failure_class": "permission_denied"}))


class DeniedToolsDispatchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc-home"), "FUSION_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.config = core.deep_merge(core.DEFAULTS, {"routes": {}, "decisions": {"mode": "off"}})

    def dispatch(self, agent, payload):
        worker = self.root / f"{agent}-fixture"
        worker.write_text(f"#!{sys.executable}\nimport json\nprint(json.dumps({payload!r}))\n")
        worker.chmod(0o755)
        self.config[agent]["command"] = str(worker)
        store = core.RunStore(self.workspace)
        task = core.make_task(self.workspace, agent, "Check the fixture", "review", [], [], None, True, False)
        result = core.dispatch(self.config, task, store)
        self.assertEqual(json.loads((Path(result["artifacts"]["run_dir"]) / "result.json").read_text()), result)
        return result, store.traces(limit=1)[-1]

    def test_claude_denials_reach_result_and_span(self):
        result, span = self.dispatch("claude", {
            "is_error": False, "session_id": "s",
            "result": "STATUS: success\nSUMMARY: done\nCHANGED: none\nTESTS: none\nBLOCKERS: none",
            "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "make"}},
                                   {"tool_name": "Bash", "reason": "not allowed"}]})
        # Finding 199: a Bash-only denial the worker worked around is a partial result, not a failure.
        self.assertEqual(result["status"], "partial")
        self.assertEqual(core.failure_class(result), "worker_error")
        self.assertEqual(result["denied_tools"], ["Bash"])
        self.assertEqual(result["denied"], [{"tool": "Bash", "input_head": '{"command":"make"}'},
                                            {"tool": "Bash", "input_head": ""}])
        self.assertEqual(result["denied_count"], 2)
        self.assertEqual(result["verdict"], "ok")
        self.assertIn("Bash", " ".join(result["blockers"]))
        self.assertEqual(span["denied_tools"], ["Bash"])
        self.assertEqual(span["failure_class"], "worker_error")
        self.assertFalse(core.denial_blocks_lane(span))

    def test_a_denied_baseline_tool_still_fails_after_exit_zero(self):
        result, span = self.dispatch("claude", {
            "is_error": False, "session_id": "s",
            "result": "STATUS: success\nSUMMARY: done\nCHANGED: none\nTESTS: none\nBLOCKERS: none",
            "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "make"}},
                                   {"tool_name": "Edit", "tool_input": {"file_path": "/repo/a.py"}}]})
        self.assertEqual(result["status"], "error")
        self.assertEqual(core.failure_class(result), "permission_denied")
        self.assertEqual(result["verdict"], "blocked_by_permissions")
        self.assertTrue(core.denial_blocks_lane(span))

    def test_a_worked_around_denial_keeps_a_blocked_report(self):
        result, _ = self.dispatch("claude", {
            "is_error": False, "session_id": "s",
            "result": "STATUS: blocked\nSUMMARY: could not run the suite\nCHANGED: none\nTESTS: none\nBLOCKERS: needs make",
            "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "make"}}]})
        self.assertEqual(result["status"], "blocked")
        self.assertNotEqual(core.failure_class(result), "permission_denied")

    def test_agy_denials_reach_result_and_span(self):
        result, span = self.dispatch("agy", {"status": "SUCCESS", "response": "",
                                             "denied_actions": [{"action": "view_file", "display_name": "ViewFile",
                                                                 "input": {"path": "/repo/file"}}]})
        self.assertEqual(core.failure_class(result), "permission_denied")
        self.assertEqual(result["denied_tools"], ["ViewFile"])
        self.assertEqual(result["denied"], [{"tool": "ViewFile", "input_head": '{"path":"/repo/file"}'}])
        self.assertEqual(result["denied_count"], 1)
        self.assertEqual(result["verdict"], "blocked_by_permissions")
        self.assertEqual(span["denied_tools"], ["ViewFile"])
        self.assertTrue(core.denial_blocks_lane(span))

    def test_handoff_blocker_fallback_when_no_payload_names_a_tool(self):
        result, span = self.dispatch("claude", {
            "is_error": False, "session_id": "s",
            "result": "STATUS: blocked\nSUMMARY: stuck\nCHANGED: none\nTESTS: none\nBLOCKERS: permission denied: Write(/etc/hosts)"})
        self.assertEqual(result["denied_tools"], ["Write"])
        self.assertEqual(result["denied"], [])
        self.assertEqual(result["denied_count"], 0)
        self.assertEqual(span["denied_tools"], ["Write"])

    def test_absent_denials_have_empty_result_fields(self):
        for agent, payload in (("claude", {"result": "done"}), ("agy", {"status": "SUCCESS", "response": "done"})):
            with self.subTest(agent=agent):
                result, span = self.dispatch(agent, payload)
                self.assertEqual(result["denied_tools"], [])
                self.assertEqual(result["denied"], [])
                self.assertEqual(result["denied_count"], 0)
                self.assertEqual(span["denied_tools"], [])


if __name__ == "__main__":
    unittest.main()
