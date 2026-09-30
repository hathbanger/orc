import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
from fusion_policy import route_candidates
from fusion_workflow import WorkflowRunner


class RouteEnvTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        environment = patch.dict(os.environ, {
            "HOME": str(self.workspace), "ORC_HOME": str(self.workspace / "orc-home"),
            "FUSION_TELEMETRY": "0", "FUSION_DECISIONS_MODE": "off",
            "ROUTE_TEST_VALUE": "inherited", "ROUTE_TEST_KEEP": "keep",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.config = core.deep_merge(core.DEFAULTS, {"decisions": {"mode": "off", "priors": False}})
        self.config["routes"] = {}
        for agent in ("claude", "codex", "agy", "grok"):
            self.config[agent]["command"] = sys.executable
        self.store = core.RunStore(self.workspace)

    def task(self, agent="claude", route=None):
        task = core.make_task(self.workspace, agent, "Check the fixture", "worker", [], [], None, False, False)
        task["route"] = route
        return task

    def runner(self, nodes=None):
        return WorkflowRunner(self.workspace, self.config, {
            "max_parallel": 1, "max_attempts": 2,
            "nodes": nodes or [{"id": "probe", "agent": "claude", "task": "Check the fixture"}],
        })

    def candidates(self, spans=(), excluded=()):
        task = {**self.task("auto"), "excluded_routes": list(excluded)}
        with patch.object(self.store, "traces", return_value=list(spans)):
            return {choice["key"] for choice in route_candidates(self.config, task, self.store)}

    def test_worker_environments_expand_merge_and_do_not_leak(self):
        for agent in ("claude", "codex", "agy", "grok"):
            with self.subTest(agent=agent):
                self.config["routes"]["second"] = {"agent": agent, "env": {
                    "ROUTE_TEST_VALUE": "override", "CLAUDE_CONFIG_DIR": "~/acct2",
                    "CODEX_HOME": "$HOME/codex2", "ROUTE_TEST_BRACES": "${HOME}/data",
                }}
                _, env, _ = core.agent_command(self.config, self.task(agent, "second"), None)
                self.assertEqual(env["ROUTE_TEST_VALUE"], "override")
                self.assertEqual(env["ROUTE_TEST_KEEP"], "keep")
                self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.workspace / "acct2"))
                self.assertEqual(env["CODEX_HOME"], str(self.workspace / "codex2"))
                self.assertEqual(env["ROUTE_TEST_BRACES"], str(self.workspace / "data"))
                self.assertEqual(os.environ["ROUTE_TEST_VALUE"], "inherited")
                _, native_env, _ = core.agent_command(self.config, self.task(agent), None)
                self.assertEqual(native_env["ROUTE_TEST_VALUE"], "inherited")

    def test_env_requires_string_values(self):
        for env in ([], None, {"PORT": 123}, {1: "value"}):
            with self.subTest(env=env), self.assertRaisesRegex(ValueError, "string"):
                core.route_env({"env": env})

    def test_workflow_keys_preserve_legacy_and_distinguish_accounts(self):
        self.config["routes"] = {
            "plain": {"agent": "claude"},
            "unrelated": {"agent": "claude", "env": {"OTHER": "value"}},
            "orc": {"agent": "claude", "command": "orc"},
            "explicit": {"agent": "claude", "account": "acct2", "env": {"CLAUDE_CONFIG_DIR": "~/ignored"}},
            "directory": {"agent": "claude", "env": {"CLAUDE_CONFIG_DIR": "~/acct2"}},
            "alias": {"agent": "claude", "env": {"CLAUDE_CONFIG_DIR": "$HOME/acct2"}},
            "different": {"agent": "claude", "env": {"CLAUDE_CONFIG_DIR": "~/other/acct2"}},
            "codex": {"agent": "codex", "env": {"CODEX_HOME": "~/codex2"}},
            "orc-second": {"agent": "claude", "command": "orc", "account": "acct2"},
        }
        runner = self.runner()
        for route in (None, "plain", "unrelated"):
            self.assertEqual(runner._lane_key("claude", route), "claude")
        self.assertEqual(runner._lane_key("claude", "orc"), "claude@orc")
        self.assertEqual(runner._lane_key("claude", "explicit"), "claude@acct2")
        self.assertEqual(runner._lane_key("claude", "directory"), f"claude@{self.workspace}/acct2")
        self.assertEqual(runner._lane_key("claude", "alias"), runner._lane_key("claude", "directory"))
        self.assertNotEqual(runner._lane_key("claude", "different"), runner._lane_key("claude", "directory"))
        self.assertEqual(runner._lane_key("codex", "codex"), f"codex@{self.workspace}/codex2")
        self.assertEqual(runner._lane_key("claude", "orc-second"), "claude@orc@acct2")

    def test_cooldowns_and_exclusions_are_scoped_to_account(self):
        for agent, identity in (("claude", {"env": {"CLAUDE_CONFIG_DIR": "~/acct2"}}),
                                ("codex", {"env": {"CODEX_HOME": "$HOME/acct2"}}),
                                ("claude", {"account": "acct2"})):
            with self.subTest(agent=agent, identity=identity):
                self.config["routes"] = {
                    "plain": {"agent": agent},
                    "second": {"agent": agent, **identity},
                    "alias": {"agent": agent, **identity},
                }
                span = {"agent": agent, "failure_class": "quota", "end_time_ms": core.now_ms(), "denied_tools": ["Read"]}
                native = self.candidates([span])
                self.assertNotIn(agent, native)
                self.assertNotIn("plain", native)
                self.assertTrue({"second", "alias"} <= native)
                second = self.candidates([{**span, "route": "second"}])
                self.assertTrue({agent, "plain"} <= second)
                self.assertNotIn("second", second)
                self.assertNotIn("alias", second)
                # A permission denial cools only the lane whose run was denied.
                denied = {**span, "failure_class": "permission_denied"}
                native = self.candidates([denied])
                self.assertNotIn(agent, native)
                self.assertTrue({"plain", "second", "alias"} <= native)
                second = self.candidates([{**denied, "route": "second"}])
                self.assertNotIn("second", second)
                self.assertTrue({agent, "plain", "alias"} <= second)
                self.assertIn("second", self.candidates(excluded=[agent]))
                excluded = self.candidates(excluded=["second"])
                self.assertIn(agent, excluded)
                self.assertNotIn("alias", excluded)

    def test_success_on_another_account_does_not_clear_cooldown(self):
        self.config["routes"] = {
            "first": {"agent": "claude", "account": "acct1"},
            "alias": {"agent": "claude", "account": "acct1"},
            "second": {"agent": "claude", "account": "acct2"},
        }
        spans = [{"agent": "claude", "route": "second", "end_time_ms": core.now_ms()},
                 {"agent": "claude", "route": "first", "failure_class": "quota", "status": "error",
                  "blockers": ["API usage limit reached"], "end_time_ms": core.now_ms() - 1}]
        choices = self.candidates(spans)
        self.assertIn("second", choices)
        self.assertNotIn("alias", choices)
        runner = self.runner()
        with patch.object(core.RunStore, "traces", return_value=spans):
            runner._preflight_lanes()
        self.assertEqual(runner.lane_health["claude@acct1"]["status"], "cooldown")
        self.assertNotIn("claude@acct2", runner.lane_health)

    def test_orc_account_cooldown_preserves_legacy_route_isolation(self):
        self.config["routes"] = {
            name: {"agent": "claude", "command": "orc", "model": "vendor/model", **identity}
            for name, identity in (("legacy", {}), ("other-legacy", {}),
                                   ("first", {"account": "acct1"}), ("alias", {"account": "acct1"}),
                                   ("second", {"account": "acct2"}))
        }
        span = {"agent": "claude", "route": "first", "failure_class": "quota", "end_time_ms": core.now_ms()}
        with patch.object(core, "executable", return_value="orc"), \
                patch.object(core, "_orc_model_ids", return_value=["vendor/model"]):
            choices = self.candidates([span])
            self.assertNotIn("alias", choices)
            self.assertTrue({"claude", "legacy", "second"} <= choices)
            choices = self.candidates([{**span, "route": "legacy"}])
            self.assertNotIn("legacy", choices)
            self.assertTrue({"claude", "other-legacy", "first", "second"} <= choices)

    def test_workflow_dispatch_falls_back_to_second_subscription(self):
        worker = self.workspace / "claude-fixture"
        worker.write_text(f"#!{sys.executable}\n" + """
import json, os
second = os.environ.get('CLAUDE_CONFIG_DIR') == os.path.join(os.environ['HOME'], 'acct2')
answer = ('STATUS: success\\nSUMMARY: second subscription\\nCHANGED: none\\nTESTS: fixture passed\\nBLOCKERS: none'
          if second else 'API usage limit reached; resets at next month')
print(json.dumps({'type': 'result', 'is_error': not second, 'result': answer}))
""")
        worker.chmod(0o755)
        for agent in ("codex", "agy", "grok"):
            self.config[agent]["command"] = "missing-route-env-test-worker"
        self.config["claude"]["command"] = str(worker)
        self.config["sidekick"] = "claude"
        self.config["routes"]["second"] = {"agent": "claude", "env": {"CLAUDE_CONFIG_DIR": "~/acct2"}}
        result = self.runner([{"id": "probe", "agent": "auto", "task": "Check the fixture"}]).run()
        self.assertEqual(result["status"], "success")
        node = result["nodes"][0]
        self.assertEqual(node["attempts"], 2)
        self.assertEqual(node["result"]["route"], "second")
        self.assertEqual(node["excluded_routes"], ["claude"])


if __name__ == "__main__":
    unittest.main()
