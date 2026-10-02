"""HTTP boundary, artifact recovery, settings preservation and real job lifecycle."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import signal
import fusion_ui
from fusion_ui import ControlRoom, Server, atomic_json, read_json, activity_entries, MASK


def seed_workspace(workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    worker = workspace / "codex-fixture"
    worker.write_text(f"#!{sys.executable}\n" + '''import json, sys, time
prompt = sys.stdin.read()
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Inspecting fixture source files'}}),flush=True)
if 'wedged fixture' in prompt:
    # A child that asks to be left alone. Cancellation must not depend on a
    # worker's cooperation, so this is the case escalation exists for.
    import signal as _signal
    _signal.signal(_signal.SIGINT, _signal.SIG_IGN)
    time.sleep(120)
if 'wait fixture' in prompt:
    time.sleep(30)
if 'LABEL_SUGGESTION_V1' in prompt:
    suggestion = {'answers': {'plausible': {'value': 'false', 'reason': 'The input reports no work on the requested fix.', 'evidence': ['E1']}}, 'abstentions': {}}
    message = 'STATUS: success\\nSUMMARY: Drafted labels from the supplied input\\n```label-suggestion\\n' + json.dumps(suggestion) + '\\n```\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':message}}),flush=True)
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':120,'output_tokens':80}}),flush=True)
    sys.exit(0)
message = "STATUS: success\\nSUMMARY: Fixture investigation complete\\n\\n1. **Read payment input once**\\n\\nThe CLI consumes stdin twice. Read once and reuse the parsed value.\\n\\n| Impact | Effort | Risk |\\n| --- | --- | --- |\\n| High | Small | Low |\\n\\n```bash\\npython -m unittest\\n```\\n\\n2. **Preserve typed API errors**\\n\\nKeep retryable transport failures distinct from authentication errors.\\n\\nCHANGED: none\\nTESTS: fixture checks passed\\nBLOCKERS: none"
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':message}}),flush=True)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':120,'output_tokens':80}}),flush=True)
''')
    worker.chmod(0o755)
    config = {"sidekick": "codex", "decisions": {"mode": "off"}, "telemetry": {"remote": {"enabled": False}},
              "codex": {"command": str(worker)}, "claude": {"command": "missing-ui-test-worker"}, "agy": {"command": "missing-ui-test-worker"}, "grok": {"command": "missing-ui-test-worker"},
              "routes": {"orc-free": {"command": "missing-ui-test-route"}, "orc-best": {"command": "missing-ui-test-route"}}}
    atomic_json(workspace / ".fusion.json", config)
    return config


class ControlRoomTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "repo"
        self.config = seed_workspace(self.workspace)
        self.env = patch.dict(os.environ, {"FUSION_TELEMETRY": "0", "ORC_HOME": str(self.root / "orc-home")})
        self.env.start()
        self.app = ControlRoom(self.workspace, self.root / "registry.json")
        self.server = Server(0, self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        for job in self.app.jobs(self.workspace):
            if job["status"] in {"queued", "running", "stopping"}:
                (self.workspace / ".fusion/ui/jobs" / job["id"] / "cancel").touch()
        for proc in self.app.children:
            proc.wait(timeout=10)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.env.stop()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        request = Request(self.server.origin + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"X-Fusion-Token": self.server.token, "Content-Type": "application/json", **(headers or {})})
        try:
            response = urlopen(request, timeout=8)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read(), dict(response.headers)

    def wait_job(self, job_id, predicate=lambda j: j["status"] not in {"queued", "running", "stopping"}):
        until = time.monotonic() + 12
        while time.monotonic() < until:
            job = self.app.job(self.workspace, job_id)
            if predicate(job):
                return job
            time.sleep(.05)
        self.fail(f"job did not reach expected state: {job}")

    def test_capability_host_origin_and_mutation_boundary(self):
        status, _, _ = self.request("/api/overview", headers={"X-Fusion-Token": ""})
        self.assertEqual(status, 401)
        self.assertEqual(self.request("/api/overview", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("/api/overview", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/launch", {"action": "setup"}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/overview")[0], 200)
        self.assertEqual(self.app.jobs(self.workspace), [])

    def test_ui_ships_offline_assets_and_does_not_serve_arbitrary_files(self):
        status, page, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"ORC", page)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("/vendor/marked.js")[0], 200)
        self.assertEqual(self.request("/vendor/purify.js")[0], 200)
        self.assertEqual(self.request("/../fusion_core.py")[0], 404)
        self.assertEqual(self.request("/api/file?path=../registry.json")[0], 400)
        self.assertEqual(self.request("/api/file?path=.fusion.json")[0], 400)
        (self.root / "outside.txt").write_text("private")
        (self.workspace / "link.txt").symlink_to(self.root / "outside.txt")
        self.assertEqual(self.request("/api/file?path=link.txt")[0], 400)

    def test_settings_preserve_secrets_and_reject_stale_saves(self):
        self.config["telemetry"]["remote"]["token"] = "secret-never-returned"
        self.config["custom"] = {"keep": True}
        atomic_json(self.workspace / ".fusion.json", self.config)
        original = self.app.config(self.workspace)
        self.assertNotIn("secret-never-returned", json.dumps(original))
        self.assertEqual(original["local"]["telemetry"]["remote"]["token"], MASK)
        edited = original["local"]
        edited["decisions"]["mode"] = "shadow"
        saved = self.app.save_config(self.workspace, {"value": edited, "revision": original["revision"]})
        on_disk = read_json(self.workspace / ".fusion.json")
        self.assertEqual(on_disk["telemetry"]["remote"]["token"], "secret-never-returned")
        self.assertTrue(on_disk["custom"]["keep"])
        self.assertNotEqual(saved["revision"], original["revision"])
        with self.assertRaisesRegex(ValueError, "changed on disk"):
            self.app.save_config(self.workspace, {"value": {}, "revision": original["revision"]})

    def test_launch_validates_scope_and_arguments_before_starting_workers(self):
        for body in [{"action":"build","kind":"build","text":"Implement CSV export"},
                     {"action":"build","kind":"debug","text":"Fix defect","budget":float('nan')},
                     {"action":"shell","text":"touch bad"},
                     {"action":"build","text":"Inspect","attempts":9},
                     {"action":"delegate","agent":"codex","text":"Inspect","route":"invented"},
                     {"action":"resume","run_id":"../outside"}]:
            with self.subTest(body=body), self.assertRaises((ValueError, TypeError)):
                self.app.launch(self.workspace, body)
        self.assertEqual(self.app.jobs(self.workspace), [])

    def test_preparation_launch_is_literal_and_creates_no_worker_runs(self):
        text = 'Inspect $(touch INJECTED) `touch ALSO_INJECTED` and file names'
        job = self.app.launch(self.workspace, {"action":"build","kind":"discovery","prepare":True,"text":text,"mode":"off"})
        result = self.wait_job(job["id"])
        self.assertEqual(result["status"], "success", result)
        self.assertFalse((self.workspace / "INJECTED").exists())
        self.assertFalse((self.workspace / "ALSO_INJECTED").exists())
        self.assertFalse((self.workspace / ".fusion/runs").exists())
        self.assertTrue(Path(result["result"]["workflow"]).is_file())
        saved = read_json(Path(result["result"]["request"]))
        self.assertEqual(saved["text"], text)

    def test_live_workflow_recovers_deliverables_and_can_resume_without_reexecution(self):
        job = self.app.launch(self.workspace, {"action":"build","kind":"discovery","text":"Inspect fixture","mode":"off","attempts":1})
        finished = self.wait_job(job["id"])
        self.assertEqual(finished["status"], "success", finished)
        report = self.app.workflow(self.workspace, finished["workflow_id"])
        self.assertEqual(report["status"], "success")
        self.assertIn("| Impact | Effort | Risk |", report["outputs"][-1]["text"])
        self.assertEqual(report["outputs"][-1]["findings"][0]["number"], 1)
        self.assertIn("Inspecting fixture source files", report["live_nodes"][0]["messages"])
        self.assertEqual(report["live_nodes"][0]["activity_entries"][0]["kind"], "message")
        self.assertEqual(len(report["live_nodes"]), 2)
        before = list((self.workspace / ".fusion/runs").iterdir())
        resumed = self.app.launch(self.workspace, {"action":"resume","run_id":finished["workflow_id"],"mode":"off"})
        self.assertEqual(self.wait_job(resumed["id"])["status"], "success")
        self.assertEqual(len(list((self.workspace / ".fusion/runs").iterdir())), len(before))

    def test_legacy_grok_workflow_displays_unterminated_plain_output(self):
        job = self.app.launch(self.workspace, {"action":"build", "kind":"discovery", "text":"Inspect fixture", "mode":"off", "attempts":1})
        finished = self.wait_job(job["id"])
        report = self.app.workflow(self.workspace, finished["workflow_id"])
        run = Path(report['live_nodes'][0]['result']['artifacts']['run_dir'])
        task = read_json(run / 'task.json')
        task['agent'] = 'grok'
        task['resolved'].pop('output_format', None)  # Existing run from before streaming support.
        atomic_json(run / 'task.json', task)
        (run / 'stdout.log').write_text('Inspected the diff. Running **Postgres** checks.')
        node = self.app.workflow(self.workspace, finished['workflow_id'])['live_nodes'][0]
        self.assertEqual(node['output_format'], 'plain')
        self.assertIn('Postgres', node['activity_entries'][0]['text'])
        self.assertIn('Postgres', node['messages'][0])
        self.assertGreater(node['last_output_at_ms'], 0)

    def test_activity_pairs_public_commands_and_keeps_failures_inspectable(self):
        events = [
            {"type": "item.completed", "item": {"id": "private", "type": "reasoning", "text": "not public"}},
            {"type": "item.completed", "item": {"id": "note", "type": "agent_message", "text": "Checking **tests**.\n\nNext step."}},
            {"type": "item.started", "item": {"id": "cmd", "type": "command_execution", "command": "pytest"}},
            {"type": "item.completed", "item": {"id": "cmd", "type": "command_execution", "exit_code": 1, "aggregated_output": "x" * 13000}},
            {"type": "item.started", "item": {"id": "next", "type": "command_execution", "command": "pytest -q"}},
            {"type": "item.completed", "item": "malformed"},
        ]
        entries = activity_entries('partial-json\n' + '\n'.join(map(json.dumps, events)) + '\n{"type":')
        self.assertEqual([e['id'] for e in entries], ['note', 'cmd', 'next'])
        self.assertIn('\n\n', entries[0]['text'])
        self.assertEqual(entries[1]['command'], 'pytest')
        self.assertTrue(entries[1]['failed'])
        self.assertEqual(entries[1]['exit_code'], 1)
        self.assertTrue(entries[1]['output'].startswith('[Earlier output omitted]'))
        self.assertLess(len(entries[1]['output']), 12100)
        self.assertEqual(entries[2]['status'], 'running')
        self.assertNotIn('not public', json.dumps(entries))
        self.assertEqual(activity_entries(json.dumps(events[3]))[0]['id'], 'cmd')

    def test_cancellation_survives_server_reconstruction_and_cleans_worker(self):
        job = self.app.launch(self.workspace, {"action":"delegate","agent":"codex","text":"wait fixture","mode":"off"})
        directory = self.workspace / ".fusion/ui/jobs" / job["id"]
        self.wait_job(job["id"], lambda j: "worker started" in j.get("console", ""))
        with self.assertRaisesRegex(ValueError, "already active"):
            self.app.launch(self.workspace, {"action":"delegate","agent":"codex","text":"duplicate","mode":"off"})
        fresh_app = ControlRoom(self.workspace, self.root / "registry.json")
        self.assertEqual(fresh_app.jobs(self.workspace)[0]["id"], job["id"])
        status, _, _ = self.request("/api/cancel", {"id": job["id"]})
        self.assertEqual(status, 200)
        result = self.wait_job(job["id"])
        self.assertEqual(result["status"], "cancelled")
        activity = read_json(next((self.workspace / ".fusion/runs").glob("*/activity.json")))
        self.assertEqual(activity["status"], "interrupted")
        with self.assertRaises(ProcessLookupError):
            os.kill(activity["pid"], 0)

    def test_cancelling_a_worker_that_ignores_sigint_still_finishes(self):
        """A job whose worker refuses SIGINT must still reach a terminal state."""
        with patch.dict(os.environ, {"FUSION_CANCEL_GRACE_SECONDS": "1"}):
            job = self.app.launch(
                self.workspace,
                {"action": "delegate", "agent": "codex", "text": "wedged fixture", "mode": "off"},
            )
            self.wait_job(job["id"], lambda j: "worker started" in j.get("console", ""))
            status, _, _ = self.request("/api/cancel", {"id": job["id"]})
            self.assertEqual(status, 200)
            result = self.wait_job(job["id"])
        self.assertEqual(result["status"], "cancelled")


    def test_launch_dispatches_the_configured_checkpoint_from_outside_the_workspace(self):
        """The wiring, not just the helper.

        A test that only calls model_path_for proves nothing about whether
        launch() uses it — reverting the call site is invisible to it. This
        reads the argv launch actually persists.
        """
        checkpoint = self.root / "hf-cache" / "models--laya-en"
        checkpoint.mkdir(parents=True)
        config = read_json(self.workspace / ".fusion.json")
        config["decisions"] = {**config.get("decisions", {}), "model_path": str(checkpoint)}
        atomic_json(self.workspace / ".fusion.json", config)
        dataset = self.workspace / ".fusion" / "decisions" / "dataset.jsonl"
        dataset.parent.mkdir(parents=True, exist_ok=True)
        dataset.write_text("{}\n")

        with patch("fusion_ui.subprocess.Popen") as spawn:
            spawn.return_value.poll.return_value = None
            job = self.app.launch(self.workspace, {
                "action": "evaluate",
                "dataset": "decisions/dataset.jsonl",
                "model_path": str(checkpoint),
            })
        argv = read_json(self.workspace / ".fusion/ui/jobs" / job["id"] / "request.json")["argv"]
        self.assertIn("--model-path", argv)
        self.assertEqual(argv[argv.index("--model-path") + 1], str(checkpoint.resolve()))

    def test_launch_still_refuses_a_checkpoint_nothing_declares(self):
        stranger = self.root / "somewhere-else"
        stranger.mkdir()
        dataset = self.workspace / ".fusion" / "decisions" / "dataset.jsonl"
        dataset.parent.mkdir(parents=True, exist_ok=True)
        dataset.write_text("{}\n")
        with self.assertRaises(ValueError):
            self.app.launch(self.workspace, {
                "action": "evaluate",
                "dataset": "decisions/dataset.jsonl",
                "model_path": str(stranger),
            })

    def test_workspace_registration_does_not_scan_or_launch_other_repositories(self):
        second = self.root / "another-repo"
        second.mkdir()
        added = self.app.add_workspace(str(second))
        self.assertEqual(self.app.workspace(added["id"]), second.resolve())
        self.assertEqual(self.app.overview(second)["workflows"], [])
        self.assertFalse((second / ".fusion").exists())
        restored = ControlRoom(self.workspace, self.root / "registry.json")
        self.assertEqual(restored.workspace(added["id"]), second.resolve())

    def seed_decision(self):
        from fusion_decisions import DecisionStore
        store = DecisionStore(self.workspace)
        store.append("decision", id="label-test", kind="acceptance", mode="shadow", status="ok",
                     state='{"task":"Fix retries","summary":"Did nothing"}', truncated=False,
                     questions={"plausible": {"type": "noul", "instructions": "Does the evidence satisfy the task?"}},
                     prediction={"plausible": {"false": .7, "true": .3}}, schema_hash="fixture", context={})
        return store

    def test_label_suggestion_job_requires_approval_and_records_edits(self):
        from fusion_decisions import read_jsonl
        store = self.seed_decision()
        status, response, _ = self.request("/api/launch", {"action": "suggest-labels", "decision_id": "label-test", "agent": "codex"})
        self.assertEqual(status, 200, response)
        job = self.wait_job(json.loads(response)["id"])
        self.assertEqual(job["status"], "success", job)
        self.assertEqual(job["decision_id"], "label-test")
        self.assertEqual(store.export(self.root / "before.jsonl", include_unknown=True)["examples"], 0)
        events = read_jsonl(store.path)
        draft = next(e for e in events if e["event"] == "label_suggestion")
        self.assertFalse(draft["verified"])
        self.assertEqual(len(store.records()), 1)  # no recursive Laya decisions
        _, response, _ = self.request("/api/decisions")
        record = json.loads(response)["records"][0]
        self.assertEqual(record["suggestion_job"]["id"], job["id"])
        self.assertEqual(record["suggestions"][0]["suggestion_id"], draft["suggestion_id"])
        body = {"id": "label-test", "answers": {"plausible": "true"}, "evidence": "Human corrected the answer after checking the fixture.", "suggestion_id": draft["suggestion_id"]}
        self.assertEqual(self.request("/api/label", body)[0], 400)
        self.assertEqual(self.request("/api/label", {**body, "approved": True})[0], 200)
        self.assertEqual(store.export(self.root / "after.jsonl", include_unknown=True)["examples"], 1)
        label = read_jsonl(store.path)[-1]
        self.assertTrue(label["answers_edited"])
        self.assertEqual(label["suggested_by"]["run_id"], draft["run_id"])
        self.assertEqual(label["source"], "human_approved_suggestion")

    def test_duplicate_suggestion_jobs_are_rejected_and_cancellable(self):
        store = self.seed_decision()
        record = store.get("label-test")
        record.pop("schema", None)
        record.pop("event", None)
        record["state"] = "wait fixture"
        store.append("decision", **record)
        job = self.app.launch(self.workspace, {"action": "suggest-labels", "decision_id": "label-test", "agent": "codex"})
        self.wait_job(job["id"], lambda j: "worker started" in j.get("console", ""))
        with self.assertRaisesRegex(ValueError, "already being drafted"):
            self.app.launch(self.workspace, {"action": "suggest-labels", "decision_id": "label-test"})
        other_app = ControlRoom(self.workspace, self.root / 'registry.json')
        with self.assertRaisesRegex(ValueError, "already being drafted"):
            other_app.launch(self.workspace, {"action": "suggest-labels", "decision_id": "label-test"})
        self.assertEqual(self.request("/api/cancel", {"id": job["id"]})[0], 200)
        self.assertEqual(self.wait_job(job["id"])["status"], "cancelled")
        self.assertEqual(store.export(self.root / "cancelled.jsonl", include_unknown=True)["examples"], 0)

    def test_garden_runs_without_browser_polling_and_requires_human_approval(self):
        from fusion_decisions import read_jsonl
        store = self.seed_decision()
        self.assertEqual(self.request('/api/garden', {'enabled': True}, {'X-Fusion-Token': ''})[0], 401)
        status, response, _ = self.request('/api/garden', {'enabled': True, 'agent': 'codex', 'daily_limit': 1, 'include_existing': True})
        self.assertEqual(status, 200, response)
        # The server scheduler, not an open/polling browser, starts the draft.
        deadline = time.monotonic() + 10
        while not self.app.jobs(self.workspace) and time.monotonic() < deadline:
            time.sleep(.05)
        job = self.wait_job(self.app.jobs(self.workspace)[0]['id'])
        self.assertEqual(job['status'], 'success', job)
        self.assertTrue(job['garden'])
        self.assertEqual(store.export(self.root / 'draft-only.jsonl', include_unknown=True)['examples'], 0)
        _, response, _ = self.request('/api/decisions')
        view = json.loads(response)
        self.assertEqual(view['learning']['counts']['needs_review'], 1)
        self.assertEqual(view['learning']['labeled_questions'], 0)
        draft = next(e for e in read_jsonl(store.path) if e.get('event') == 'label_suggestion')
        self.request('/api/label', {'id': 'label-test', 'answers': {'plausible': 'false'}, 'evidence': 'Checked original task', 'suggestion_id': draft['suggestion_id'], 'approved': True})
        self.assertEqual(self.request('/api/label-exclusion', {'id': 'label-test', 'excluded': True})[0], 200)
        self.assertEqual(store.export(self.root / 'excluded.jsonl', include_unknown=True)['examples'], 0)
        self.assertEqual(self.request('/api/label-exclusion', {'id': 'label-test', 'excluded': False})[0], 200)
        self.assertEqual(store.export(self.root / 'restored.jsonl', include_unknown=True)['examples'], 1)
        self.assertEqual(self.request('/api/garden', {'enabled': False})[0], 200)


class CancelEscalationTest(unittest.TestCase):
    """"Stopping" must be a state a job passes through, not one it sits in.

    The supervisor sent exactly one SIGINT, to the direct child, with no
    escalation and no upper bound. A child that had not installed its handler
    yet, or would not honour it, left the job in "stopping" forever and the
    person who clicked Cancel waiting on it. This is the decision that was
    missing, tested directly because the surrounding loop owns a live
    subprocess and cannot be driven to that state reliably.
    """

    def test_does_nothing_until_cancellation_is_requested(self):
        self.assertIsNone(fusion_ui.cancel_action(False, None, False, 100.0, 5))

    def test_interrupts_once_when_first_requested(self):
        self.assertEqual(fusion_ui.cancel_action(True, None, False, 100.0, 5), "interrupt")

    def test_waits_out_the_grace_period_before_escalating(self):
        self.assertIsNone(fusion_ui.cancel_action(True, 100.0, False, 104.9, 5))

    def test_escalates_once_the_grace_period_expires(self):
        self.assertEqual(fusion_ui.cancel_action(True, 100.0, False, 105.1, 5), "kill")

    def test_never_escalates_twice(self):
        self.assertIsNone(fusion_ui.cancel_action(True, 100.0, True, 1e6, 5))

    def test_signal_job_prefers_the_group_so_workers_are_reached(self):
        # Workers start in the child's session, so signalling only the child
        # leaves them running. The group is what reaches them.
        sent = []

        class Proc:
            pid = 4242
            def send_signal(self, sig):
                sent.append(("child", sig))

        with patch("fusion_ui.os.getpgid", return_value=4242), \
             patch("fusion_ui.os.killpg", side_effect=lambda pgid, sig: sent.append(("group", sig))):
            fusion_ui.signal_job(Proc(), signal.SIGINT)
        self.assertEqual(sent, [("group", signal.SIGINT)])

    def test_signal_job_falls_back_to_the_child_and_never_raises(self):
        sent = []

        class Proc:
            pid = 4242
            def send_signal(self, sig):
                sent.append(sig)

        with patch("fusion_ui.os.getpgid", side_effect=PermissionError(1, "not permitted")):
            fusion_ui.signal_job(Proc(), signal.SIGKILL)
        self.assertEqual(sent, [signal.SIGKILL])

        class GoneProc:
            pid = 1
            def send_signal(self, sig):
                raise ProcessLookupError

        with patch("fusion_ui.os.getpgid", side_effect=ProcessLookupError):
            fusion_ui.signal_job(GoneProc(), signal.SIGKILL)  # must not raise


class ModelPathTest(unittest.TestCase):
    """A checkpoint is a read-only input and may live outside the workspace.

    `decisions setup` downloads one through snapshot_download into the Hugging
    Face cache, and a machine sharing one checkpoint across repositories keeps
    it somewhere central. Requiring it under .fusion refused both, and the
    automatic training loop turned that refusal into a permanent stall: it
    re-dispatches the identical request on retry, so the round never recovers.

    Containment still matters, because the value arrives in an HTTP body. The
    rule is to trust the project's config, not the request.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "repo"
        (self.workspace / ".fusion").mkdir(parents=True)
        self.outside = Path(self.temp.name) / "shared-checkpoints" / "laya-en"
        self.outside.mkdir(parents=True)

    def configure(self, model_path=None):
        body = {"decisions": {"model_path": str(model_path)}} if model_path else {}
        atomic_json(self.workspace / ".fusion.json", body)

    def test_a_checkpoint_inside_fusion_is_always_allowed(self):
        self.configure()
        resolved = fusion_ui.model_path_for(self.workspace, ".fusion/decisions/candidate")
        self.assertTrue(resolved.is_relative_to((self.workspace / ".fusion").resolve()))

    def test_an_outside_checkpoint_is_refused_when_nothing_declares_it(self):
        self.configure()
        with self.assertRaisesRegex(ValueError, "must be inside"):
            fusion_ui.model_path_for(self.workspace, str(self.outside))

    def test_the_configured_checkpoint_is_allowed_even_outside(self):
        self.configure(self.outside)
        self.assertEqual(
            fusion_ui.model_path_for(self.workspace, str(self.outside)), self.outside.resolve()
        )

    def test_a_subdirectory_of_the_configured_checkpoint_is_allowed(self):
        self.configure(self.outside)
        snapshot = self.outside / "snapshots" / "abc"
        self.assertEqual(
            fusion_ui.model_path_for(self.workspace, str(snapshot)), snapshot.resolve()
        )

    def test_declaring_one_checkpoint_does_not_open_the_filesystem(self):
        # The whole point of keeping a containment check: a request may not
        # name some other path just because the config names one.
        self.configure(self.outside)
        for other in ("/etc/passwd", str(self.outside.parent / "unrelated"), "../../secrets"):
            with self.subTest(path=other):
                with self.assertRaises(ValueError):
                    fusion_ui.model_path_for(self.workspace, other)

    def test_an_unreadable_config_refuses_rather_than_opening_up(self):
        (self.workspace / ".fusion.json").write_text("{ not json")
        with self.assertRaises(ValueError):
            fusion_ui.model_path_for(self.workspace, str(self.outside))


if __name__ == "__main__":
    unittest.main()
