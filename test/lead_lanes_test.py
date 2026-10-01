"""A lead can discover lanes through fusion_here and call Fusion headlessly."""
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
import fusion_mcp


class LeadLanesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        env = patch.dict(os.environ, {"ORC_HOME": str(self.workspace / ".orc-home"), "FUSION_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FUSION_CONFIG", None)

    def test_orientation_lists_workers_and_routes_without_secrets(self):
        (self.workspace / ".fusion.json").write_text(json.dumps({
            "decisions": {"auto_routes": ["oc-gemini"]},
            "routes": {"oc-gemini": {"agent": "opencode", "command": sys.executable, "model": "google/gemini-2.5-pro",
                                     "cost_tier": 1, "env": {"API_KEY": "secret"}},
                       "gone": {"agent": "opencode", "command": "/nonexistent/opencode", "model": "x/y"}}}))
        lanes = {lane["route"] or lane["agent"]: lane for lane in fusion_mcp.orientation(self.workspace)["lanes"]}
        self.assertEqual({"claude", "codex", "agy", "grok", "opencode"} - set(lanes), set())
        self.assertEqual(lanes["oc-gemini"], {"agent": "opencode", "route": "oc-gemini", "model": "google/gemini-2.5-pro",
                                              "cost_tier": 1, "available": True, "automatic": True})
        self.assertFalse(lanes["gone"]["available"])
        self.assertFalse(lanes["gone"]["automatic"])
        self.assertNotIn("secret", json.dumps(lanes))

    def test_claude_lead_may_call_its_own_fusion_tools_headlessly(self):
        config = core.deep_merge(core.DEFAULTS, {"claude": {"command": sys.executable}})
        for interactive in (False, True):
            with self.subTest(interactive=interactive), \
                    patch.object(core.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                core.launch_lead(self.workspace, config, "claude", "Delegate a review", interactive)
                argv = run.call_args.args[0]
                self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__fusion")
                self.assertEqual(argv[-2:], ["--", "Delegate a review"])

    def test_claude_lead_uses_configured_model(self):
        config = core.deep_merge(core.DEFAULTS, {"claude": {"command": sys.executable, "model": "claude-opus-5-5"}})
        with patch.object(core.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            core.launch_lead(self.workspace, config, "claude", "Plan", False)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5-5")

    def test_lead_prompt_points_at_lanes(self):
        self.assertIn("fusion_here", core.LEAD_PROMPT)
        self.assertIn("route", core.LEAD_PROMPT)


if __name__ == "__main__":
    unittest.main()
