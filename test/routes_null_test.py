"""A null route removes it, including a built-in one; a later layer may restore it."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core


class NullRouteTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "orc-home"
        self.home.mkdir()
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        env = patch.dict(os.environ, {"ORC_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        for name in ("FUSION_CONFIG", "FUSION_CONTROL_WORKSPACE"):
            os.environ.pop(name, None)

    def write(self, path, value):
        path.write_text(json.dumps(value))

    def test_null_removes_builtin_routes(self):
        self.write(self.home / "fusion.json", {"routes": {"orc-free": None, "orc-best": None}})
        routes = core.load_config(self.workspace)[0]["routes"]
        self.assertNotIn("orc-free", routes)
        self.assertNotIn("orc-best", routes)
        self.assertIn("codex-read", routes)
        self.assertTrue(all(isinstance(route, dict) for route in routes.values()))

    def test_project_can_restore_or_remove_a_route(self):
        self.write(self.home / "fusion.json", {"routes": {"orc-free": None, "mine": {"agent": "claude"}}})
        self.write(self.workspace / ".fusion.json", {"routes": {"orc-free": {"agent": "claude", "command": "orc"}, "mine": None}})
        routes = core.load_config(self.workspace)[0]["routes"]
        self.assertEqual(routes["orc-free"]["command"], "orc")
        self.assertNotIn("mine", routes)

    def test_removed_route_is_unknown_to_dispatch(self):
        self.write(self.home / "fusion.json", {"routes": {"orc-free": None}})
        config = core.load_config(self.workspace)[0]
        with self.assertRaisesRegex(ValueError, "unknown Fusion route"):
            core.agent_settings(config, {"agent": "claude", "route": "orc-free"})


if __name__ == "__main__":
    unittest.main()
