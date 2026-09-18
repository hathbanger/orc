import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fusion_core  # noqa: E402


class FusionHarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "repo"
        self.workspace.mkdir()
        self.bin_dir = Path(self.temp.name) / "bin"
        self.bin_dir.mkdir()
        self.calls = Path(self.temp.name) / "calls.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def write_agent(self, name: str, body: str) -> Path:
        path = self.bin_dir / name
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def config(self, codex: Path | None = None, claude: Path | None = None) -> None:
        value = {
            "codex": {"command": str(codex or "missing-codex")},
            "claude": {"command": str(claude or "missing-claude")},
            "timeout_seconds": 30,
        }
        (self.workspace / ".fusion.json").write_text(json.dumps(value), encoding="utf-8")

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
        names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertEqual(names, {"fusion_delegate", "fusion_status"})

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


if __name__ == "__main__":
    unittest.main()
