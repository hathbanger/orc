"""Local control room. Existing Fusion artifacts are the source of truth.

No web framework or frontend build is required. API access requires a per-server
capability and a matching loopback Host/Origin. Jobs run in separate supervisors
so closing the browser or restarting the server does not stop a workflow.
"""
from __future__ import annotations

import hashlib
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid
import webbrowser

import fusion_build as build_module
import fusion_core as core
import fusion_publish as publishing
import fusion_garden as garden
import fusion_truffle as truffle
import fusion_training_loop as training_loop
from fusion_learning import decision_rows, learning_summary
from fusion_decisions import DecisionEngine, DecisionStore, config_for, read_jsonl
from fusion_report import finding_request, format_report, reported_cost, select_report, terminal_text
from fusion_workflow import validate_spec, workflow_report, workflow_status

ASSETS = Path(__file__).with_name("fusion_ui_assets")
from fusion_core import MASK, SECRET, redact  # one definition, shared with `fusion doctor`
IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]+$")
ACTIVE = {"queued", "running", "stopping"}
# How long a cancelled job may stay in "stopping" before we stop asking nicely.
CANCEL_GRACE_SECONDS = float(os.environ.get("FUSION_CANCEL_GRACE_SECONDS", "5"))


def cancel_action(requested, cancelled_at, escalated, now, grace):
    """Decide what a cancelled job's supervisor should do on this tick.

    "interrupt" once when cancellation is first requested, then "kill" if the
    job is still alive after the grace period, then nothing.

    One SIGINT is a request, not a guarantee: it can land before the child has
    installed its handler, or the child can be wedged. Without the escalation
    a job sits in "stopping" forever, and the person who clicked Cancel waits
    on a state it will never leave.
    """
    if not requested:
        return None
    if cancelled_at is None:
        return "interrupt"
    if not escalated and now - cancelled_at > grace:
        return "kill"
    return None


def signal_job(proc, sig) -> None:
    """Signal a job's whole process group, falling back to the child alone.

    Jobs start with start_new_session=True, so the child leads its own group
    and its workers are in it. Signalling the group is what reaches those
    workers; signalling only the child leaves them running. Teardown is best
    effort and must never raise: the group can become unsignalable between the
    check and the call (leader reaped, pid recycled), which the OS reports as
    EPERM rather than ESRCH.
    """
    try:
        os.killpg(os.getpgid(proc.pid), sig)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, OSError):
        pass


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with open(temporary, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def activity_entries(stdout, *, plain=False):
    """Public updates and tool receipts only; pair starts/completions by item ID."""
    if plain:
        # Older Grok runs stream public prose, often without a newline until exit.
        text = terminal_text(stdout).strip()
        return [{"id": "plain-output", "kind": "message", "text": text[-32000:]}] if text else []
    entries = {}
    occurrences = {}
    text_group = "start"

    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue  # A bounded log tail can begin or end inside a JSON line.
        if not isinstance(event, dict):
            continue
        kind, item = event.get("type"), event.get("item") or {}
        if kind == "text" and isinstance(event.get("data"), str):
            key = f"grok-text-{text_group}"
            previous = entries.get(key, {}).get("text", "")
            entries[key] = {"id": key, "kind": "message", "text": (previous + terminal_text(event["data"]))[-32000:]}
            continue
        if kind in {"tool_call", "tool_call_update"}:
            if kind == "tool_call":
                text_group = str(event.get("toolCallId") or hashlib.sha256(line.encode()).hexdigest()[:20])
            key = str(event.get("toolCallId") or hashlib.sha256(line.encode()).hexdigest()[:20])
            previous = entries.get(key, {})
            raw = event.get("rawInput") if isinstance(event.get("rawInput"), dict) else {}
            tool_kind = event.get("kind") or previous.get("tool_kind")
            command = raw.get("command") or previous.get("command") or (event.get("title") if tool_kind == "execute" else "")
            output = event.get("rawOutput")
            if output is not None:
                output = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            else:
                output = "\n".join(str(c.get("content", {}).get("text", "")) for c in event.get("content", []) if isinstance(c, dict) and isinstance(c.get("content"), dict)) or previous.get("output", "")
            status = event.get("status") or previous.get("native_status") or "in_progress"
            entries[key] = {"id": key, "kind": "command" if tool_kind == "execute" else "tool", "tool_kind": tool_kind,
                            "name": str(event.get("toolName") or previous.get("name") or event.get("title") or "Tool"),
                            "command": terminal_text(str(command))[:8000], "output": terminal_text(output)[-12000:],
                            "status": "finished" if status in {"completed", "failed"} else "running", "native_status": status,
                            "exit_code": None, "failed": status == "failed"}
            continue
        if kind == "end":
            entries["grok-end"] = {"id": "grok-end", "kind": "status", "text": "Worker returned its handoff. Open Deliverable to read the result."}
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        fallback = hashlib.sha256(line.encode()).hexdigest()[:20]
        occurrences[fallback] = occurrences.get(fallback, 0) + 1
        key = str(item.get("id") or f"{fallback}-{occurrences[fallback]}")
        if kind == "item.completed" and item_type == "agent_message" and item.get("text"):
            entries[key] = {"id": key, "kind": "message", "text": terminal_text(str(item["text"]))[:16000]}
        elif kind in {"item.started", "item.completed"} and item_type in {"command_execution", "mcp_tool_call", "file_change"}:
            previous = entries.get(key, {})
            command = terminal_text(str(item.get("command") or previous.get("command") or ""))[:8000]
            output = terminal_text(str(item.get("aggregated_output") or previous.get("output") or ""))
            if len(output) > 12000:
                output = "[Earlier output omitted]\n" + output[-12000:]
            entries[key] = {"id": key, "kind": "command" if item_type == "command_execution" else "tool",
                            "name": "Command" if item_type == "command_execution" else str(item.get("tool") or "File changes"),
                            "status": "running" if kind == "item.started" else "finished",
                            "command": command, "output": output, "exit_code": item.get("exit_code"),
                            "failed": item.get("status") == "failed" or item.get("exit_code") not in (None, 0)}
        elif kind in {"error", "turn.failed"}:
            error = event.get("error") or {}
            text = event.get("message") or (error.get("message") if isinstance(error, dict) else error)
            entries[key] = {"id": key, "kind": "error", "text": terminal_text(str(text or "Worker reported an error"))[:4000]}
        elif kind == "result":
            entries[key] = {"id": key, "kind": "status", "text": "Worker returned its handoff. Open Deliverable to read the result."}
    return list(entries.values())[-100:]


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value) or value in {".", ".."}:
        raise ValueError("Invalid run identifier")
    return value


def model_path_for(workspace, requested):
    """Resolve a checkpoint path for a train/evaluate job.

    A checkpoint is a read-only input, not a workspace artifact, and it
    legitimately lives outside the workspace: `decisions setup` downloads one
    through snapshot_download into the Hugging Face cache, and a machine that
    shares one checkpoint across repositories keeps it somewhere central.
    Requiring it under .fusion rejected every one of those, and the automatic
    training loop turned that into a permanent stall — it re-dispatched the
    identical request on retry, so the round never recovered.

    Containment still matters, because `requested` arrives in an HTTP body.
    So trust the project's own configuration rather than the request: a path is
    allowed when it is inside .fusion, or when the config already names that
    checkpoint. Anything else is refused as before.
    """
    fusion_root = (Path(workspace) / ".fusion").resolve()
    candidate = Path(requested).expanduser()
    candidate = (candidate if candidate.is_absolute() else fusion_root / candidate).resolve()
    if candidate.is_relative_to(fusion_root):
        return candidate
    configured = ""
    try:
        config, _ = core.load_config(workspace)
        configured = (config.get("decisions") or {}).get("model_path") or ""
    except (OSError, ValueError, SystemExit):
        # load_config exits the process on malformed JSON, which suits a CLI
        # and not a server. Either way, a config we cannot read grants nothing:
        # fail closed rather than fall back to trusting the request.
        configured = ""
    if configured:
        declared = Path(configured).expanduser()
        declared = (declared if declared.is_absolute() else Path(workspace) / declared).resolve()
        if candidate == declared or candidate.is_relative_to(declared):
            return candidate
    raise ValueError(
        "A checkpoint must be inside this workspace's .fusion directory, or the "
        "one named by decisions.model_path in .fusion.json"
    )


def inside(root, path):
    root = Path(root).resolve()
    path = Path(path).expanduser()
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Path must stay inside this workspace")
    return path


def restore_secrets(value, previous):
    if isinstance(value, dict):
        previous = previous if isinstance(previous, dict) else {}
        return {key: (previous.get(key, "") if item == MASK and SECRET.search(key)
                      else restore_secrets(item, previous.get(key, {}))) for key, item in value.items()}
    if isinstance(value, list):
        return [restore_secrets(item, previous[index] if isinstance(previous, list) and index < len(previous) else {})
                for index, item in enumerate(value)]
    return value


def revision(path):
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def tail(path, limit=100_000):
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            return terminal_text(stream.read().decode("utf-8", "replace"))
    except OSError:
        return ""


def alive(pid):
    # One definition, in fusion_core; this keeps the boolean contract callers expect.
    return core.process_alive(pid) is True


def run_job(directory):
    """Detached supervisor owns its child; cancellation never targets a saved PID."""
    directory = Path(directory)
    request = read_json(directory / "request.json")
    state = {"id": directory.name, "action": request["action"], "title": request["title"],
             "decision_id": request.get("decision_id"),
             "garden": request.get("garden", False),
             "garden_policy": request.get("garden_policy"),
             "learning_round": request.get("learning_round"), "learning_dispatch": request.get("learning_dispatch"),
             "started_at_ms": core.now_ms(), "status": "running", "supervisor_pid": os.getpid()}
    atomic_json(directory / "job.json", state)
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "FUSION_PROGRESS": "1"}
    if request.get("mode"):
        environment["FUSION_DECISIONS_MODE"] = request["mode"]
    if request.get("survey_id"):
        environment["FUSION_TRUFFLE_SURVEY_ID"] = request["survey_id"]
    try:
        with (directory / "stdout.log").open("wb") as out, (directory / "stderr.log").open("wb") as err:
            proc = subprocess.Popen(request["argv"], cwd=request["workspace"], env=environment,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
            cancelled_at = None
            escalated = False
            while proc.poll() is None:
                action = cancel_action(
                    (directory / "cancel").exists(), cancelled_at, escalated,
                    time.monotonic(), CANCEL_GRACE_SECONDS,
                )
                if action == "interrupt":
                    cancelled_at = time.monotonic()
                    state["status"] = "stopping"
                    signal_job(proc, signal.SIGINT)
                elif action == "kill":
                    escalated = True
                    signal_job(proc, signal.SIGKILL)
                state["updated_at_ms"] = core.now_ms()
                if not state.get("workflow_id"):
                    match = re.search(r"(\d{8}-\d{6}-wf-[a-f0-9]+)", tail(directory / "stderr.log", 12000))
                    if match:
                        state["workflow_id"] = match[1]
                atomic_json(directory / "job.json", state)
                time.sleep(.25)
            cancelled = cancelled_at is not None
            state.update(exit_code=proc.returncode, status="cancelled" if cancelled else "success" if proc.returncode == 0 else "failed")
        result = read_json(directory / "stdout.log")
        state["result"] = result
        if isinstance(result, dict):
            state["workflow_id"] = result.get("workflow_id")
        if not state.get("workflow_id"):
            match = re.search(r"(\d{8}-\d{6}-wf-[a-f0-9]+)", tail(directory / "stderr.log"))
            if match:
                state["workflow_id"] = match[1]
    except Exception as exc:
        state.update(status="failed", error=str(exc))
    state["finished_at_ms"] = core.now_ms()
    atomic_json(directory / "job.json", state)
    return 0


class ControlRoom:
    def __init__(self, workspace, registry=None):
        home = Path(os.environ.get("ORC_HOME") or Path.home() / ".config/orc")
        self.registry = Path(registry) if registry else home / "ui-workspaces.json"
        self.lock = threading.RLock()
        self.workspaces = {}
        for path in read_json(self.registry, {}).get("paths", []):
            if Path(path).is_dir():
                self.add_workspace(path, save=False)
        self.default = self.add_workspace(str(workspace), save=False)["id"]
        self.children = []
        self.garden_errors = {}

    def add_workspace(self, path, save=True):
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Enter a workspace directory")
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("Workspace directory does not exist")
        key = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        with self.lock:
            self.workspaces[key] = root
            if save:
                atomic_json(self.registry, {"paths": [str(p) for p in self.workspaces.values()]})
        return {"id": key, "name": root.name, "path": str(root)}

    def workspace(self, key=None):
        try:
            return self.workspaces[key or self.default]
        except KeyError:
            raise ValueError("Unknown workspace; add it using the workspace switcher")

    def config(self, workspace):
        try:
            value, source = core.load_config(workspace)
            options = config_for(value)
        except SystemExit as exc:
            raise ValueError(str(exc))
        calibration = DecisionEngine(workspace, value).calibration()
        local = workspace / ".fusion.json"
        orc = workspace / ".orc.json"
        return {"local": redact(read_json(local)), "effective": redact(value), "source": str(source) if source else "Built-in defaults",
                "revision": revision(local), "orc": redact(read_json(orc)), "orc_revision": revision(orc),
                "mode": options["mode"], "execution_mode": core.execution_mode(value),
                "publish": publishing.git_options(workspace, value),
                "qualified_buckets": sum(bool(b.get("qualified")) for b in calibration.get("buckets", {}).values()),
                "environment": {key: os.environ[key] for key in ("FUSION_DECISIONS_MODE", "FUSION_TELEMETRY", "FUSION_CONFIG") if key in os.environ},
                "workers": [core.worker_availability(value, name)
                            for name in ("codex", "claude", "agy", "grok", "opencode")]}

    def save_config(self, workspace, body):
        filename = ".orc.json" if body.get("target") == "orc" else ".fusion.json"
        path = inside(workspace, filename)
        value = body.get("value")
        if not isinstance(value, dict):
            raise ValueError("Settings must be a JSON object")
        with self.lock:
            if body.get("revision") != revision(path):
                raise ValueError("Settings changed on disk. Reload before saving to preserve those edits.")
            old = read_json(path)
            value = restore_secrets(value, old)
            if filename == ".fusion.json":
                merged = core.deep_merge(core.DEFAULTS, value)
                core.execution_mode(merged)
                publishing.options(merged)
                config_for(merged)
                if not isinstance(merged.get("routes"), dict):
                    raise ValueError("routes must be a JSON object")
                for name in ("codex", "claude", "agy", "grok", "opencode", "telemetry", "decisions"):
                    if not isinstance(merged.get(name), dict):
                        raise ValueError(f"{name} must be a JSON object")
            atomic_json(path, value)
        return self.config(workspace)

    def jobs(self, workspace, limit=100):
        results = []
        for path in (workspace / ".fusion/ui/jobs").glob("*/job.json"):
            job = read_json(path)
            if not job:
                continue
            if job.get("status") in ACTIVE and job.get("supervisor_pid") and not alive(job["supervisor_pid"]):
                job["status"] = "interrupted"
            result = job.pop("result", None)
            if isinstance(result, dict):
                job["workflow_id"] = job.get("workflow_id") or result.get("workflow_id")
            results.append(job)
        return sorted(results, key=lambda row: row.get("started_at_ms", 0), reverse=True)[:limit]

    def routing_tasks(self, workspace, model=None, limit=200):
        from fusion_routing_view import routing_tasks
        return routing_tasks(read_jsonl(DecisionStore(workspace).path), core.RunStore(workspace).traces(10000), model, limit)

    def decisions(self, workspace):
        rows = decision_rows(workspace)
        jobs = self.jobs(workspace, limit=None)
        label_runs = self.label_runs(workspace)
        for row in rows:
            row['suggestions'] = row['suggestions'][-5:]
            row['suggestion_job'] = next((j for j in jobs if j.get('decision_id') == row['id']), None)
            row['label_run'] = next((r for r in label_runs if r['decision_id'] == row['id']), None)
            if row['suggestion_job'] and row['garden_state'] not in {'excluded', 'ineligible'}:
                if row['suggestion_job']['status'] in ACTIVE:
                    row['garden_state'] = 'drafting'
                elif row['garden_state'] == 'needs_draft':
                    row['garden_state'] = 'needs_attention'
        config, _ = core.load_config(workspace)
        return {'records': rows, 'learning': learning_summary(workspace, config, rows), 'label_runs': label_runs,
                'garden': {**garden.status(self, workspace, rows), 'error': self.garden_errors.get(str(workspace))},
                'training_loop': training_loop.status(self, workspace, rows)}

    def label_runs(self, workspace, decision_id=None, since=0):
        paths = sorted((workspace / '.fusion/decisions/assessments').glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        runs = []
        for path in paths:
            run = read_json(path)
            if not run or run.get('started_at_ms', 0) < since or (decision_id and run.get('decision_id') != decision_id):
                continue
            if run.get('status') == 'running' and not alive(run.get('pid')):
                run.update(status='interrupted', phase='interrupted')
                for member in run.get('members', []):
                    if member.get('status') == 'running':
                        member['status'] = 'interrupted'
            for member in run.get('members', []):
                if member.get('status') != 'running' or not member.get('run_id'):
                    continue
                directory = inside(workspace / '.fusion', 'runs/' + identifier(member['run_id']))
                member['activity'] = read_json(directory / 'activity.json')
                stdout = tail(directory / 'stdout.log', 60000)
                member['messages'] = [message for line in stdout.splitlines() if (message := core.progress.worker_message(line))][-6:]
                member['stderr'] = tail(directory / 'stderr.log', 4000)
            runs.append(run)
            if len(runs) >= (1 if decision_id else 12):
                break
        return runs

    def garden_tick(self, workspaces=None):
        with self.lock:
            workspaces = list(self.workspaces.values() if workspaces is None else workspaces)
        for workspace in workspaces:
            try:
                garden.tick(self, workspace)
                training_loop.tick(self, workspace)
                self.garden_errors.pop(str(workspace), None)
            except Exception as exc:
                self.garden_errors[str(workspace)] = str(exc)

    def overview(self, workspace):
        workflows = []
        for path in (workspace / ".fusion/workflows").glob("*/manifest.json"):
            manifest = read_json(path)
            if not manifest:
                continue
            nodes = list((manifest.get("nodes") or {}).values())
            status = manifest.get("status")
            if status == "running" and manifest.get("coordinator_pid") and not alive(manifest["coordinator_pid"]):
                status = "interrupted"
            workflows.append({"id": path.parent.name, "task": manifest.get("task", "Untitled workflow"), "status": status,
                              "started_at_ms": manifest.get("started_at_ms", path.stat().st_mtime * 1000),
                              "completed": sum(n.get("status") == "success" for n in nodes), "total": len(nodes),
                              "read_only": all(not n.get("write") for n in nodes), "spent_usd": manifest.get("spent_usd"),
                              "agents": sorted({(n.get("result") or {}).get("agent", n.get("agent", "auto")) for n in nodes})})
        workflows.sort(key=lambda row: row["started_at_ms"], reverse=True)
        builds = []
        for path in (workspace / ".fusion/builds").glob("*/status.json"):
            state = read_json(path)
            if state and not state.get("workflow_id"):
                if state.get("status") == "running" and state.get("coordinator_pid") and not alive(state["coordinator_pid"]):
                    state["status"] = "interrupted"
                builds.append(state)
        spans = core.RunStore(workspace).traces(10000)
        worker_spans = [span for span in spans if span.get("agent") != "gate" and span.get("status") != "cache_hit"]
        return {"workflows": workflows[:200], "builds": sorted(builds, key=lambda b: b.get("started_at_ms", 0), reverse=True)[:30],
                "jobs": self.jobs(workspace), "hunts": truffle.history(workspace), "usage": core.usage_summary(spans),
                "cost": {"calls": len(worker_spans), "reported_calls": sum(reported_cost(span.get("usage")) is not None for span in worker_spans)},
                "now_ms": core.now_ms()}

    def workflow(self, workspace, run_id):
        run_id = identifier(run_id)
        manifest_path = inside(workspace / ".fusion", f"workflows/{run_id}/manifest.json")
        if not manifest_path.is_file():
            raise ValueError("Workflow does not exist")
        manifest = workflow_status(workspace, run_id)
        report = workflow_report(workspace, run_id)
        nodes = []
        saved_nodes = manifest.get("nodes", {})
        order = [n["id"] for n in manifest.get("spec", {}).get("graph", {}).get("nodes", [])]
        for key in dict.fromkeys([*order, *saved_nodes]):
            node = saved_nodes[key]
            key = identifier(key)
            current = dict(node, id=key)
            if node.get("status") == "paused_quota":
                result = node.get("result") or {}
                current["quota"] = {"agent": result.get("agent", node.get("agent")),
                                    "route": result.get("route") or "native", "message": result.get("summary", "Provider quota exhausted")}
            result = node.get("result") or {}
            if core.failure_class(result) == "coordinator_error":
                current["coordinator_failure"] = {
                    "phase": result.get("failure_phase"), "message": result.get("summary"),
                    "detail": next(iter(result.get("blockers", [])), "Git snapshot failed"),
                }
            if core.failure_class(result) == "permission_denied":
                current["permission_failure"] = {
                    "agent": result.get("agent", node.get("agent")),
                    "message": next((b for b in result.get("blockers", []) if "denied" in str(b).lower()), "Worker permission denied"),
                }
            active = read_json(inside(workspace / ".fusion", f"workflows/{run_id}/nodes/{key}/active.json"))
            worker_id = active.get("run_id") or (node.get("result") or {}).get("run_id")
            current["messages"] = []
            current["activity_entries"] = []
            if worker_id:
                directory = inside(workspace / ".fusion", "runs/" + identifier(worker_id))
                current["activity"] = read_json(directory / "activity.json")
                task = read_json(directory / "task.json")
                current["agent"] = task.get("agent", current.get("agent"))
                stdout = tail(directory / "stdout.log", 500_000)
                plain = current["agent"] == "grok" and task.get("resolved", {}).get("output_format", "plain") == "plain"
                current["activity_entries"] = activity_entries(stdout, plain=plain)
                current["output_format"] = "plain" if plain else "events"
                current["last_output_at_ms"] = max((int(p.stat().st_mtime * 1000) for p in (directory / "stdout.log", directory / "stderr.log") if p.is_file() and p.stat().st_size), default=None)
                if plain and stdout.strip():
                    current["messages"] = [core.progress.clean(stdout[-2000:])]

                for line in stdout.splitlines():
                    message = core.progress.worker_message(line)
                    if message:
                        current["messages"].append(message)
                current["messages"] = current["messages"][-60:]
                current["stderr"] = tail(directory / "stderr.log", 12000)
            nodes.append(current)
        if report["status"] == "running" and manifest.get("coordinator_pid") and not alive(manifest["coordinator_pid"]):
            report["status"] = "interrupted"
        report["live_nodes"] = nodes
        report["started_at_ms"] = manifest.get("started_at_ms")
        report["spec"] = manifest.get("spec")
        report["events"] = read_jsonl(manifest_path.with_name("events.jsonl"))[-150:]
        report["markdown"] = format_report(select_report(report, all_nodes=True))
        return report

    def job(self, workspace, job_id):
        directory = inside(workspace / ".fusion", "ui/jobs/" + identifier(job_id))
        if not (directory / "job.json").exists():
            raise ValueError("Job does not exist")
        job = read_json(directory / "job.json")
        if job.get("action") in {"train", "evaluate"}:
            job["progress"] = read_json(directory / ("candidate.progress.json" if job["action"] == "train" else "dataset.jsonl.progress.json"))
        job["console"] = tail(directory / "stderr.log", 60000)
        job["output"] = tail(directory / "stdout.log", 80000)
        if job.get('action') == 'suggest-labels':
            job['label_run'] = next(iter(self.label_runs(workspace, job.get('decision_id'), job.get('started_at_ms', 0))), None)
        if job.get("status") in ACTIVE and job.get("supervisor_pid") and not alive(job["supervisor_pid"]):
            job["status"] = "interrupted"
        return job

    def launch(self, workspace, body, *, garden=False):
        action = body.get("action", "build")
        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        directory = inside(workspace / ".fusion", "ui/jobs/" + job_id)
        argv = [sys.executable, str(Path(__file__).with_name("fusion")), "--workspace", str(workspace), "--json", "--progress"]
        text = body.get("text", "")
        if not isinstance(text, str) or len(text) > 50000:
            raise ValueError("Request must be text, at most 50,000 characters")
        mode = body.get("mode") or None
        if mode not in {None, "off", "shadow", "active"}:
            raise ValueError("Invalid Laya mode")
        writes = False
        spec = None
        survey_id = None
        if action == "build":
            kind = body.get("kind", "discovery")
            if kind not in {"discovery", "review", "build", "debug", "sweep"}:
                raise ValueError("Choose discovery, review, build, debug or sweep")
            budget, attempts = float(body.get("budget", 0)), int(body.get("attempts", 2))
            if not math.isfinite(budget) or budget < 0 or not 1 <= attempts <= 5:
                raise ValueError("Budget must be nonnegative; attempts must be between 1 and 5")
            prepare = body.get("prepare") is True
            argv += ["build", "--kind", kind, "--plan-only" if prepare else "--execute", "--budget-usd", str(budget), "--max-attempts", str(attempts)]
            if kind == "sweep":
                across = build_module.dimensions(body.get("across") or [])
                if not across:
                    raise ValueError("A sweep needs at least one dimension to fan out across")
                for dimension in across:
                    argv += ["--across", dimension]
            if kind in {"build", "debug"} and body.get("publish") is not None:
                config, _ = core.load_config(workspace)
                publish_options = publishing.options(config, body["publish"])
                argv += ["--publish", publish_options["mode"], "--base", publish_options["base"], "--remote", publish_options["remote"], "--draft" if publish_options["draft"] else "--ready"]
            if body.get("from_workflow"):
                run_id = identifier(body["from_workflow"])
                finding = int(body.get("finding", 0))
                node = body.get("from_node") or None
                finding_request(workspace, run_id, finding, node)
                argv += ["--from-workflow", run_id, "--finding", str(finding)]
                if node:
                    argv += ["--from-node", identifier(node)]
            elif not text.strip():
                raise ValueError("Describe the task or paste a GitHub issue URL")
            argv += ["--", text]
            writes = kind in {"build", "debug"} and not prepare
        elif action == "truffle-survey":
            agent, remote = body.get("agent", "auto"), body.get("remote", "origin")
            truffle.hunt_options(agent=agent, remote=remote)
            argv += ["truffle", "survey", "--agent", agent, "--remote", remote]
            if body.get("resume"):
                saved = truffle.receipt(workspace, body["resume"])
                if saved.get("kind") != "survey":
                    raise ValueError("Choose an issue woodland to resume")
                argv += ["--resume", saved["id"]]
            else:
                # Mint the survey id here rather than letting the caller infer it from the
                # newest hunt.json afterward: between this process returning and the detached
                # survey writing its first record, the previous survey would still be "latest".
                survey_id = "truffle-" + uuid.uuid4().hex[:12]
            for key in ("sync_only", "include_assigned"):
                if key in body and type(body[key]) is not bool:
                    raise ValueError(f"{key} must be boolean")
                if body.get(key) is True:
                    argv += ["--" + key.replace("_", "-")]
            text = "Truffle pig · " + ("map all open issues" if body.get("sync_only") else "grade every open issue")
        elif action == "truffle-hunt":
            settings = truffle.hunt_options(count=int(body.get("count", 5)), scan_limit=int(body.get("scan_limit", 40)),
                                            search=body.get("search", ""), agent=body.get("agent", "auto"),
                                            remote=body.get("remote", "origin"), include_assigned=body.get("include_assigned", False))
            argv += ["truffle", "hunt", "--count", str(settings["count"]), "--scan-limit", str(settings["scan_limit"]),
                     "--search", settings["search"], "--agent", settings["agent"], "--remote", settings["remote"]]
            if settings["include_assigned"]:
                argv += ["--include-assigned"]
            text = f"Truffle pig · find up to {settings['count']} issues"
        elif action == "truffle-run":
            truffle.selection(workspace, body.get("scout_id"), body.get("issues"))
            config, _ = core.load_config(workspace)
            opts = publishing.options(config, body.get("publish"))
            if opts["mode"] == "off":
                raise ValueError("Choose manual or automatic PR mode for isolated issue fixes")
            attempts = int(body.get("attempts", 2))
            if not 1 <= attempts <= 5:
                raise ValueError("Attempts must be 1–5")
            argv += ["truffle", "run", body["scout_id"], "--issues", *map(str, body["issues"]),
                     "--publish", opts["mode"], "--base", opts["base"], "--remote", opts["remote"], "--max-attempts", str(attempts)]
            if not opts["draft"]:
                argv += ["--ready"]
            writes = True
            text = f"Truffle pig · resolve {len(body['issues'])} issues"
        elif action == "publish":
            run_id = identifier(body.get("run_id"))
            publishing.eligible(workflow_status(workspace, run_id))
            if not isinstance(body.get("snapshot_id"), str):
                raise ValueError("Preview the PR before publishing")
            argv += ["workflow", "publish", run_id, "--request", str(directory / "publication-request.json")]
        elif action == "delegate":
            agent = body.get("agent", "auto")
            if agent not in {"auto", "codex", "claude", "agy", "grok", "opencode"} or not text.strip():
                raise ValueError("Choose a worker and describe its task")
            role = body.get("role", "review")
            if role not in {"review", "discovery", "planning", "implementation"}:
                raise ValueError("Invalid worker role")
            argv += ["delegate", "--agent", agent, "--role", role, "--fresh"]
            writes = body.get("allow_write") is True
            if not writes:
                argv += ["--read-only"]
            if body.get("route"):
                config, _ = core.load_config(workspace)
                if body["route"] not in config.get("routes", {}):
                    raise ValueError("Unknown route")
                argv += ["--route", body["route"]]
            argv += ["--", text]
        elif action == "resume":
            run_id = identifier(body.get("run_id"))
            manifest = workflow_status(workspace, run_id)
            if manifest.get("status") == "running" and alive(manifest.get("coordinator_pid")):
                raise ValueError("This workflow is already running")
            writes = any(n.get("write") for n in manifest.get("nodes", {}).values())
            argv += ["workflow", "resume", run_id]
            if any(body.get(key) is not None for key in ("node", "agent", "route", "max_attempts")):
                from fusion_workflow import reroute_resume_spec
                config, _ = core.load_config(workspace)
                limit = int(body["max_attempts"]) if body.get("max_attempts") is not None else None
                reroute_resume_spec(manifest, config, body.get("node"), body.get("agent"), body.get("route"), limit)
                for key in ("node", "agent", "route"):
                    if body.get(key):
                        argv += ["--" + key, body[key]]
                if limit is not None:
                    argv += ["--max-attempts", str(limit)]
        elif action == "workflow":
            spec = validate_spec(body.get("spec"))
            writes = any(n.get("write") for n in spec["graph"]["nodes"])
            argv += ["workflow", "run", str(directory / "workflow.json")]
        elif action == "probe":
            kind = body.get("kind", "intake")
            if kind not in {"intake", "review", "acceptance", "recovery"} or not text.strip():
                raise ValueError("Choose a classifier and provide input")
            argv += ["decisions", "probe", "--kind", kind, "--", text]
        elif action == "suggest-labels":
            from fusion_labeling import labelable, labeling_options, approval_options, council_rule, member_lane, _local_config
            labelable(DecisionStore(workspace), body.get("decision_id"))
            agent = body.get("agent", "auto")
            local = _local_config(workspace)
            if agent != "auto":
                member_lane(local, agent)
            options = labeling_options(body.get("labeling_mode", "single"), body.get("council_agents"), local)
            approval = approval_options(body.get("approval_mode", "human"), options['labeling_mode'])
            argv += ["decisions", "suggest", "--agent", agent, "--approval", approval,
                     "--council-rule", council_rule(body.get("council_rule", "unanimous"))]
            if garden and body.get('garden_policy'):
                argv += ['--garden-policy', body['garden_policy']]
            if options["labeling_mode"] == "council":
                argv += ["--council", *options["council_agents"]]
            argv += ["--", body["decision_id"]]
            mode = "off"
        elif action == "setup":
            checkpoint = body.get("checkpoint", "english")
            if checkpoint not in {"english", "multilingual", "typed-decisions"}:
                raise ValueError("Invalid checkpoint")
            argv += ["decisions", "setup", "--checkpoint", checkpoint]
        elif action in {"export", "calibrate", "train", "evaluate"}:
            argv += ["decisions", action]
            if action != "export":
                dataset = inside(workspace / ".fusion", body.get("dataset", ""))
                if not dataset.is_file():
                    raise ValueError("Choose an existing dataset inside this workspace's .fusion directory")
                argv += [str(dataset)]
            destination = directory / ("candidate" if action == "train" else "dataset.jsonl" if action in {"export", "evaluate"} else "calibration.json")
            argv += [str(destination)]
            if action == "evaluate":
                argv += ["--control"]
            if action in {"train", "evaluate"} and body.get("model_path"):
                argv += ["--model-path", str(model_path_for(workspace, body["model_path"]))]
        else:
            raise ValueError("Unsupported action")
        if writes and body.get("allow_write") is not True:
            raise ValueError("This workflow includes implementation. Enable workspace edits before launching.")
        with self.lock, garden_lock(workspace, action):
            if action in {"export", "train", "evaluate", "calibrate"} and any(j["status"] in ACTIVE and j["action"] in {"export", "train", "evaluate", "calibrate"} for j in self.jobs(workspace, limit=None)):
                raise ValueError("A local learning job is already active. Follow or stop it before starting another.")
            if action == "publish" and any(j["status"] in ACTIVE and j["action"] == "publish" for j in self.jobs(workspace)):
                raise ValueError("A publication job is already active in this workspace")
            if action == "suggest-labels" and any(j["status"] in ACTIVE and j.get("decision_id") == body["decision_id"] for j in self.jobs(workspace, limit=None)):
                raise ValueError("Labels are already being drafted for this decision")
            if action in {"build", "delegate", "resume", "workflow", "truffle-hunt", "truffle-run", "truffle-survey"} and not (action == "truffle-survey" and body.get("sync_only") and not body.get("resume")) and any(j["status"] in ACTIVE and j["action"] in {"build", "delegate", "resume", "workflow", "truffle-hunt", "truffle-run", "truffle-survey"} for j in self.jobs(workspace)):
                raise ValueError("A UI workflow is already active in this workspace. Follow or stop it before launching another.")
            directory.mkdir(parents=True, mode=0o700)
            if spec:
                atomic_json(directory / "workflow.json", spec)
            if action == "publish":
                atomic_json(directory / "publication-request.json", {k: body[k] for k in ("snapshot_id", "title", "body", "draft", "accept_legacy_diff") if k in body})
            title = text[:200] or (f"Resume {body.get('run_id')}" if action == "resume" else f"{action.title()} · {body.get('kind', 'Laya')}")
            decision_id = body.get("decision_id") if action == "suggest-labels" else None
            learning = {"dataset": str(dataset) if action in {"train", "evaluate", "calibrate"} else "",
                        "model_path": body.get("model_path", "")}
            atomic_json(directory / "request.json", {"action": action, "title": title, "argv": argv, "workspace": str(workspace), "mode": mode, "decision_id": decision_id, "garden": garden, "learning": learning,
                                                      "garden_policy": body.get('garden_policy') if garden else None, "survey_id": survey_id,
                                                  "learning_round": body.get("learning_round"), "learning_dispatch": body.get("learning_dispatch")})
            atomic_json(directory / "job.json", {"id": job_id, "action": action, "title": title, "status": "queued", "started_at_ms": core.now_ms(), "decision_id": decision_id, "garden": garden,
                                                  "garden_policy": body.get('garden_policy') if garden else None, "survey_id": survey_id,
                                                  "learning_round": body.get("learning_round"), "learning_dispatch": body.get("learning_dispatch")})
            with (directory / "supervisor.log").open("wb") as log:
                proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--job", str(directory)],
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            self.children = [child for child in self.children if child.poll() is None] + [proc]
        return self.job(workspace, job_id)


def persistent_token():
    """One capability per machine, not per server start.

    Minting a fresh one each start meant a restart, a second tab, or any
    bookmark hit a 401 the browser could not act on. The token still exists:
    the loopback Host/Origin check only constrains browsers, so any local
    process could otherwise reach /api/launch and spend provider quota.
    """
    home = Path(os.environ.get("ORC_HOME") or Path.home() / ".config/orc")
    path = home / "ui-token"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        home.mkdir(parents=True, exist_ok=True)
        with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
            stream.write(token + "\n")
    except FileExistsError:  # another server won the race; use what it wrote
        try:
            return path.read_text(encoding="utf-8").strip() or token
        except OSError:
            return token
    except OSError:
        pass  # unwritable home: a per-start token still works for this session
    return token
def garden_lock(workspace, action):
    return garden.locked(workspace, 'label-jobs') if action == 'suggest-labels' else contextlib.nullcontext()


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, app):
        self.app = app
        self.token = persistent_token()
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.url = self.origin + "/#token=" + self.token
        self.garden_checked_at = 0

    def service_actions(self):
        if time.monotonic() - self.garden_checked_at >= 3:
            self.garden_checked_at = time.monotonic()
            self.app.garden_tick()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Task text and capability credentials do not belong in access logs.

    def send(self, status, value, content_type="application/json"):
        raw = json.dumps(value, ensure_ascii=False).encode() if content_type == "application/json" else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected or self.headers.get("Origin", self.server.origin) != self.server.origin:
            self.send(403, {"error": "Use the local URL printed by orc fusion ui"})
            return False
        if not secrets.compare_digest(self.headers.get("X-Fusion-Token", "").encode(), self.server.token.encode()):
            self.send(401, {"error": "Open the full local URL from your terminal to connect this browser"})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if not path.startswith("/api/"):
            try:
                file = inside(ASSETS, "index.html" if path == "/" else path.lstrip("/"))
                if not file.is_file():
                    raise ValueError("Not found")
                self.send(200, file.read_bytes(), mimetypes.guess_type(str(file))[0] or "application/octet-stream")
            except (ValueError, OSError):
                self.send(404, {"error": "Not found"})
            return
        if not self.authorized():
            return
        query = {key: values[0] for key, values in parse_qs(urlsplit(self.path).query).items()}
        app = self.server.app
        try:
            workspace = app.workspace(query.get("w"))
            if path == "/api/bootstrap":
                result = {"default": app.default, "workspaces": [{"id": key, "name": p.name, "path": str(p)} for key, p in app.workspaces.items()]}
            elif path == "/api/overview":
                result = app.overview(workspace)
            elif path == "/api/truffle":
                from fusion_truffle_survey import latest
                result = truffle.receipt(workspace, query["id"]) if query.get("id") else latest(workspace)
            elif path == "/api/workflow":
                result = app.workflow(workspace, query.get("id"))
            elif path == "/api/job":
                result = app.job(workspace, query.get("id"))
            elif path == "/api/config":
                result = app.config(workspace)
            elif path == "/api/decisions":
                result = app.decisions(workspace)
            elif path == "/api/routing-tasks":
                result = app.routing_tasks(workspace, query.get("model"), int(query.get("limit") or 200))
            elif path == "/api/file":
                file = inside(workspace, query.get("path", ""))
                relative = file.relative_to(workspace)
                if any(part.startswith(".") and part != ".fusion" for part in relative.parts) or file.suffix not in {".md", ".txt", ".json", ".jsonl", ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".sh", ".yaml", ".yml", ".css", ".html", ".toml", ".sql"}:
                    raise ValueError("This file is not a supported source or report artifact")
                if not file.is_file() or file.stat().st_size > 2_000_000:
                    raise ValueError("File is missing or larger than 2 MB")
                result = {"path": str(file), "text": file.read_text(errors="replace")}
            elif path == "/api/orc":
                command = query.get("command", "status")
                arguments = {"status": ["status", "--json"], "models": ["models", "--tools"], "free": ["models", "--free", "--tools"], "profiles": ["profiles"], "quality": ["quality"]}.get(command)
                if arguments is None:
                    raise ValueError("Unsupported ORC view")
                orc = str(Path(__file__).with_name("orc"))
                proc = subprocess.run([orc, *arguments], cwd=workspace, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                                      env={**os.environ, "NO_COLOR": "1"})
                result = {"text": terminal_text(proc.stdout), "error": terminal_text(proc.stderr), "exit_code": proc.returncode}
            else:
                self.send(404, {"error": "Unknown API route"})
                return
            self.send(200, result)
        except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError) as exc:
            self.send(400, {"error": str(exc)})

    def do_POST(self):
        if not self.authorized():
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1_000_000 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("Send a JSON request smaller than 1 MB")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            app = self.server.app
            workspace = app.workspace(body.get("workspace"))
            path = urlsplit(self.path).path
            if path == "/api/workspaces":
                result = app.add_workspace(body.get("path", ""))
            elif path == "/api/config":
                result = app.save_config(workspace, body)
            elif path == "/api/publish-preview":
                config, _ = core.load_config(workspace)
                result = publishing.preview(workspace, identifier(body.get("run_id")), config, body.get("publish"))
            elif path == "/api/pr-refresh":
                result = publishing.refresh_pr(workspace, identifier(body.get("run_id")))
            elif path == "/api/launch":
                result = app.launch(workspace, body)
            elif path == "/api/cancel":
                job = app.job(workspace, body.get("id"))
                if job["status"] not in ACTIVE:
                    raise ValueError("This job is no longer running")
                path = inside(workspace / ".fusion", "ui/jobs/" + identifier(body["id"]) + "/cancel")
                path.touch(mode=0o600)
                result = {"status": "stopping"}
            elif path == "/api/label":
                if body.get("suggestion_id") and body.get("approved") is not True:
                    raise ValueError("Suggested labels require explicit human approval")
                DecisionStore(workspace).label(body.get("id"), body.get("answers", {}), body.get("evidence", ""), body.get("suggestion_id"), replace=True)
                result = {"saved": True}
            elif path == "/api/training-loop":
                result = training_loop.configure(app, workspace, body)
            elif path == "/api/garden":
                result = garden.save(app, workspace, body)
            elif path == "/api/label-exclusion":
                store = DecisionStore(workspace)
                store.get(body.get('id'))
                if type(body.get('excluded')) is not bool:
                    raise ValueError('Choose whether to exclude this decision')
                with store.review_lock():
                    store.append('label_exclusion', id=body['id'], excluded=body['excluded'], source='human')
                result = {'saved': True}
            else:
                self.send(404, {"error": "Unknown API route"})
                return
            self.send(200, result)
        except (ValueError, OSError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as exc:
            self.send(400, {"error": str(exc)})


def serve(workspace, port=8765, open_browser=True):
    if not 0 <= port <= 65535:
        print("fusion ui: port must be between 0 and 65535", file=sys.stderr)
        return 1
    app = ControlRoom(workspace)
    try:
        server = Server(port, app)
    except OSError as exc:
        print(f"fusion ui: {exc}. Choose another port with ui --port 8766.", file=sys.stderr)
        return 1
    print(f"\nORC control room\n{server.url}\nWorkspace: {workspace}\nCtrl-C closes the server; launched workflows continue.\n", flush=True)
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--job":
        raise SystemExit(run_job(sys.argv[2]))
