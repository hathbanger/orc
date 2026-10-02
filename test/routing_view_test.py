"""Per-task routing view: chosen model, propensity, posterior, cost, outcome and labels per routed run."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
from fusion_decisions import DecisionStore
from fusion_routing_view import routing_tasks


def log(run, chosen, model, time_ms, sampled=None, scope="automatic", **extra):
    candidates = [{"key": chosen, "agent": "claude", "model": model, "propensity": 0.6},
                  {"key": "other-lane", "agent": "codex", "model": "model-b", "propensity": 0.4}]
    return {"event": "routing_log", "task_id": run, "scope": scope, "chosen": chosen, "role": "implementation",
            "write": True, "time_ms": time_ms, "decision_id": "route-" + run, "candidates": candidates,
            **({"sampled": sampled} if sampled else {}), **extra}


EVENTS = [
    log("run-1", "lane-a", "model-a", 1000, sampled={"policy": "thompson", "chosen": "lane-a", "families": [
        {"lead": "lane-a", "successes": 5, "attempts": 8, "p_win": .55, "cost_per_accepted": 8.0, "lanes": ["lane-a", "lane-a-api"]},
        {"lead": "other-lane", "successes": 82, "attempts": 140, "p_win": .45, "cost_per_accepted": 3.0, "lanes": ["other-lane"]}]}),
    log("run-2", "other-lane", "model-b", 2000),
    log("run-3", "lane-a", "model-a", 3000),
    log("run-4", "lane-a", "model-a", 4000, scope="quota_twin"),
    {"event": "decision", "id": "gate-1", "kind": "acceptance", "context": {"task_id": "run-1"}},
    {"event": "decision", "id": "gate-2", "kind": "acceptance", "context": {"task_id": "run-2"}},
    {"event": "label", "id": "gate-1", "answers": {"failed_task": "false"}, "source": "lead_verdict", "verified": True},
    {"event": "outcome", "task_id": "run-1", "accepted": True, "source": "lead", "stage": "verify", "reason": "landed"},
    {"event": "outcome", "task_id": "run-2", "accepted": False, "source": "gate"},
]
SPANS = [{"run_id": "run-1", "status": "success", "usage": {"cost_usd": 5.0}, "duration_ms": 60000},
         {"run_id": "run-2", "status": "partial", "usage": {"cost_usd": 1.5}}]


class RoutingTasksTest(unittest.TestCase):
    def test_each_routed_run_becomes_one_row_newest_first(self):
        value = routing_tasks(EVENTS, SPANS)
        self.assertEqual([r["run"] for r in value["rows"]], ["run-3", "run-2", "run-1"])
        self.assertEqual(value["total"], 3)
        first = value["rows"][-1]
        self.assertEqual((first["chosen"], first["model"], first["propensity"], first["cost_usd"]), ("lane-a", "model-a", .6, 5.0))
        self.assertEqual(first["posterior"], {"successes": 5, "attempts": 8, "p_win": .55, "cost_per_accepted": 8.0,
                                              "lanes": ["lane-a", "lane-a-api"]})
        self.assertEqual(first["outcome"], {"accepted": True, "source": "lead", "stage": "verify", "reason": "landed"})
        self.assertEqual(first["labels"], [{"decision_id": "gate-1", "kind": "acceptance", "labeled": True, "source": "lead_verdict",
                                            "answers": {"failed_task": "false"}, "verified": True}])
        self.assertEqual(first["run_dir"], ".fusion/runs/run-1")

    def test_unlabeled_unsampled_and_pending_runs_say_so(self):
        rows = {r["run"]: r for r in routing_tasks(EVENTS, SPANS)["rows"]}
        self.assertIsNone(rows["run-2"]["posterior"])
        self.assertEqual(rows["run-2"]["labels"], [{"decision_id": "gate-2", "kind": "acceptance", "labeled": False}])
        self.assertIsNone(rows["run-3"]["outcome"])
        self.assertEqual(rows["run-3"]["labels"], [])
        self.assertIsNone(rows["run-3"]["cost_usd"])

    def test_models_are_summarized_and_filterable(self):
        value = routing_tasks(EVENTS, SPANS)
        self.assertEqual(value["models"]["model-a"], {"picks": 2, "accepted": 1, "rejected": 0, "pending": 1, "cost_usd": 5.0})
        self.assertEqual(value["models"]["model-b"], {"picks": 1, "accepted": 0, "rejected": 1, "pending": 0, "cost_usd": 1.5})
        filtered = routing_tasks(EVENTS, SPANS, model="MODEL-B")
        self.assertEqual([r["run"] for r in filtered["rows"]], ["run-2"])
        self.assertEqual(list(filtered["models"]), ["model-b"])
        self.assertEqual(len(routing_tasks(EVENTS, SPANS, limit=1)["rows"]), 1)


class RoutingSurfacesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc"), "FUSION_PROGRESS": "0", "FUSION_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        store = DecisionStore(self.root)
        for event in EVENTS:
            store.append(event["event"], **{k: v for k, v in event.items() if k != "event"})

    def test_cli_lists_tasks(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = core.main(["--workspace", str(self.root), "decisions", "routing-report", "--tasks", "--model", "model-a"])
        self.assertEqual(code, 0)
        value = json.loads(out.getvalue())
        self.assertEqual([r["run"] for r in value["rows"]], ["run-3", "run-1"])

    def test_control_room_serves_the_view_and_its_data(self):
        from fusion_ui import ControlRoom, Server
        server = Server(0, ControlRoom(self.root, self.root / "registry.json"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        request = urllib.request.Request(server.origin + "/api/routing-tasks?model=model-b",
                                         headers={"X-Fusion-Token": server.token})
        with urllib.request.urlopen(request, timeout=10) as response:
            value = json.loads(response.read())
        self.assertEqual([r["run"] for r in value["rows"]], ["run-2"])
        with urllib.request.urlopen(server.origin + "/routing.js", timeout=10) as response:
            self.assertIn(b"function routingView", response.read())
        with urllib.request.urlopen(server.origin + "/", timeout=10) as response:
            page = response.read()
        self.assertIn(b'data-view="routing"', page)
        self.assertIn(b"/routing.js", page)


if __name__ == "__main__":
    unittest.main()
