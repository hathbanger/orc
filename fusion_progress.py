"""Terminal progress and live subprocess logs, separate from machine output."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack, suppress
import json
import codecs
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid


def elapsed(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}:{seconds:02d}"


def clean(value, limit=240):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    return " ".join("".join(char if char.isprintable() else " " for char in text).split())[:limit]


class Reporter:
    def __init__(self, enabled=False, stream=None, interval=10):
        self.enabled = enabled
        self.stream = stream if stream is not None else sys.stderr
        self.interval = interval
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.active = {}
        self.latest = {}
        self.listeners = {}
        self.stopped = threading.Event()
        self.cancelled = threading.Event()
        self.thread = None

    def start(self):
        if (self.enabled or self.listeners) and not (self.thread and self.thread.is_alive()):
            self.stopped.clear()
            self.thread = threading.Thread(target=self._heartbeat, name="fusion-progress", daemon=True)
            self.thread.start()

    def close(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=1)

    def emit(self, label, message):
        with self.lock:
            for key, (active_label, _, _) in self.active.items():
                if active_label == label:
                    self.latest[key] = clean(message, 600)
            self._write(label, message)

    def _write(self, label, message):
        if self.enabled:
            with self.lock:
                try:
                    print(f"[{elapsed(time.monotonic() - self.started)}] {clean(label, 60)}  {clean(message, 600)}", file=self.stream, flush=True)
                except (OSError, ValueError):
                    self.enabled = False

    @contextmanager
    def activity(self, label, message):
        key = uuid.uuid4().hex
        with self.lock:
            self.active[key] = (label, message, time.monotonic())
            self.latest[key] = message
        self.emit(label, message)
        try:
            yield
        finally:
            with self.lock:
                self.active.pop(key, None)
                self.latest.pop(key, None)

    @contextmanager
    def listen(self, callback):
        """Subscribe for one request; removal waits for any in-flight callback."""
        key = uuid.uuid4().hex
        with self.lock:
            self.listeners[key] = (callback, time.monotonic())
            self.start()
        try:
            yield
        finally:
            with self.lock:
                self.listeners.pop(key, None)
                stop = not self.enabled and not self.listeners
                if stop:
                    self.stopped.set()
            if stop and self.thread:
                self.thread.join()

    def _heartbeat(self):
        while not self.stopped.wait(self.interval):
            with self.lock:
                now = time.monotonic()
                messages = []
                for key, (label, message, started) in self.active.items():
                    detail = f"still active ({elapsed(now - started)}) — {message}"
                    if self.latest.get(key) != message:
                        detail += f"; last event: {self.latest[key]}"
                    self._write(label, detail)
                    messages.append(f"{clean(label, 60)} {detail}")
                if messages:
                    for callback, started in self.listeners.values():
                        callback(now - started, "; ".join(messages))


_reporter = Reporter()


@contextmanager
def session(enabled, stream=None, interval=10):
    global _reporter
    previous = _reporter
    current = Reporter(enabled, stream, interval)
    _reporter = current
    current.start()
    old_handler = None
    if threading.current_thread() is threading.main_thread():
        old_handler = signal.getsignal(signal.SIGINT)
        def interrupt(signum, frame):
            current.cancelled.set()
            raise KeyboardInterrupt
        signal.signal(signal.SIGINT, interrupt)
    try:
        yield current
    finally:
        current.close()
        _reporter = previous
        if old_handler is not None:
            signal.signal(signal.SIGINT, old_handler)


def emit(label, message):
    _reporter.emit(label, message)


def activity(label, message):
    return _reporter.activity(label, message)


def heartbeat_listener(callback):
    return _reporter.listen(callback)


class WorkerCancelled(RuntimeError):
    pass


class WorkerAborted(RuntimeError):
    """A worker stopped because its output showed it could not make progress."""

    def __init__(self, reason, output="", stderr=""):
        super().__init__(reason)
        self.output, self.stderr = output, stderr


def check_cancelled():
    if _reporter.cancelled.is_set():
        raise WorkerCancelled("worker interrupted by user")


def worker_message(line):
    """Summarize public worker events; do not display raw commands or reasoning."""
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    item = event.get("item") or {}
    if not isinstance(item, dict):
        return None
    if kind == "text" and isinstance(event.get("data"), str):
        return clean(event["data"])
    if kind in {"tool_call", "tool_call_update"}:
        status = event.get("status", "in_progress")
        name = clean(str(event.get("toolName") or (str(event.get("kind")) + " tool" if event.get("kind") else "tool")), 60)
        return f"{name}: {status.replace('_', ' ')}"
    if kind == "end":
        return "worker turn completed; checking handoff"
    if kind == "thread.started":
        return "worker session connected"
    if kind in {"item.started", "item.completed"}:
        item_type = item.get("type")
        if item_type == "command_execution":
            return "running a command" if kind == "item.started" else f"command finished (exit {item.get('exit_code', '?')})"
        if item_type == "file_change":
            return "worker reported file changes"
        if item_type == "mcp_tool_call":
            return f"tool {clean(item.get('tool', 'call'), 60)}: {'started' if kind == 'item.started' else 'finished'}"
        if item_type == "agent_message" and item.get("text"):
            return clean(item["text"])
    if kind == "turn.completed":
        return "worker turn completed; checking handoff"
    if kind in {"error", "turn.failed"}:
        return "worker reported an error; details are in its logs"
    if kind == "result":
        return "worker returned its handoff"
    return None


def _stop_process(proc):
    # Workers start their own session so Ctrl-C/timeout also cleans up CLI children.
    # Teardown is best effort. Between the timeout and the kill a group can stop
    # being signalable by us — the leader is already reaped, or its pid was
    # recycled into a group we do not own — and the OS reports that as EPERM, not
    # ESRCH. Raising here would turn a handled timeout into a crash for the
    # caller, so fall back to signalling the child directly and give up quietly.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        with suppress(OSError):
            proc.terminate()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    # Still signal the group after a clean wait: descendants outlive the leader.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        with suppress(OSError):
            proc.kill()
    proc.wait()


def run_logged(argv, *, cwd, env, input, timeout, stdout_path, stderr_path, label, plain_output=False, abort_on=None):
    """Write worker output as it arrives, retaining the original result parsers.

    `abort_on`, if given, sees each complete stdout line and returns a reason
    to stop the worker early (WorkerAborted), or None to keep going.
    """
    check_cancelled()
    started = time.monotonic()
    activity_path = stdout_path.with_name("activity.json")
    proc = None

    def save(status):
        value = {"pid": proc.pid, "status": status, "started_at_ms": started_at,
                 "updated_at_ms": int(time.time() * 1000),
                 "stdout_bytes": stdout_path.stat().st_size, "stderr_bytes": stderr_path.stat().st_size}
        temporary = activity_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value))
        temporary.replace(activity_path)

    with ExitStack() as stack:
        out = stack.enter_context(stdout_path.open("wb"))
        err = stack.enter_context(stderr_path.open("wb"))
        stdin = subprocess.DEVNULL
        if input is not None:
            stdin = stack.enter_context(tempfile.TemporaryFile())
            stdin.write(input.encode("utf-8"))
            stdin.seek(0)
        reader = stack.enter_context(stdout_path.open("rb"))
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin, stdout=out, stderr=err, start_new_session=True)
        started_at = int(time.time() * 1000)
        pending = b""
        last_saved = 0.0
        last_message = ""
        abort_reason = None
        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        def inspect_output():
            nonlocal pending, last_message, abort_reason
            # Read bounded chunks; logs themselves remain complete on disk.
            chunk = reader.read(65536)
            if plain_output:
                message = clean(decoder.decode(chunk, final=not chunk and proc.poll() is not None))
                if message:
                    emit(label, message)
                return bool(chunk)
            pending += chunk
            lines = pending.split(b"\n")
            pending = lines.pop()
            for line in lines:
                if abort_on is not None and abort_reason is None:
                    abort_reason = abort_on(line)
                message = worker_message(line)
                if message and message != last_message:
                    emit(label, message)
                    last_message = message
            if len(pending) > 131072:
                pending = b""  # oversized events are retained in the raw log
            return bool(chunk)

        emit(label, f"worker started (pid {proc.pid}); logs: {stdout_path.parent}")
        save("running")
        try:
            with activity(label, "worker running; waiting for its next update"):
                while True:
                    check_cancelled()
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    try:
                        proc.wait(timeout=min(.2, remaining))
                    except subprocess.TimeoutExpired:
                        pass
                    inspect_output()
                    if abort_reason:
                        raise WorkerAborted(abort_reason)
                    if time.monotonic() - last_saved >= 2:
                        save("running")
                        last_saved = time.monotonic()
                    if proc.poll() is not None:
                        while inspect_output():
                            pass
                        break
        except BaseException as exc:
            _stop_process(proc)
            save("timed_out" if isinstance(exc, subprocess.TimeoutExpired) else "aborted" if isinstance(exc, WorkerAborted) else "interrupted")
            if isinstance(exc, (subprocess.TimeoutExpired, WorkerAborted)):
                exc.output = stdout_path.read_text(errors="replace")
                exc.stderr = stderr_path.read_text(errors="replace")
            raise
        save("finished")
    return subprocess.CompletedProcess(argv, proc.returncode, stdout_path.read_text(errors="replace"), stderr_path.read_text(errors="replace"))


def print_workflow(result):
    print(f"Workflow {result['workflow_id']}: {result['status']}")
    for node in result.get("nodes", []):
        handoff = node.get("result") or {}
        print(f"  {node['id']} [{handoff.get('agent') or node['agent']}]: {node['status']}")
        if handoff.get("summary"):
            print(f"    {clean(handoff['summary'], 600)}")
        for blocker in handoff.get("blockers", []):
            print(f"    blocker: {clean(blocker, 400)}")
    print(f"Reported spend: ${result.get('spent_usd', 0):.4f}")
    print(f"Report: {result['artifacts']['root']}")
    print(f"Details: fusion workflow report {result['workflow_id']}")
    publication = result.get("publication") or {}
    if publication.get("url"):
        print(f"PR: {publication['url']}")
    elif publication.get("status") == "failed":
        print(f"Publication needs attention: {clean(publication.get('error', ''), 600)}")
        print(f"Retry: fusion workflow publish {result['workflow_id']}")


def _watch_path(workspace, run_id):
    root = Path(workspace) / ".fusion"
    if run_id:
        if Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ValueError("invalid workflow/build id")
        workflow = root / "workflows" / run_id / "manifest.json"
        build = root / "builds" / run_id / "status.json"
        return workflow if workflow.exists() or not build.exists() else build
    candidates = []
    for path in [*root.glob("workflows/*/manifest.json"), *root.glob("builds/*/status.json")]:
        state = json.loads(path.read_text())
        if path.name == "status.json" and not state.get("execution"):
            continue
        # Old workflows have no start field. Directory creation time is a stable
        # fallback; manifest mtime changes whenever an older run makes progress.
        directory = path.parent.stat()
        started = state.get("started_at_ms", getattr(directory, "st_birthtime", directory.st_mtime) * 1000)
        candidates.append((started, getattr(directory, "st_birthtime", directory.st_mtime), str(path), path))
    if not candidates:
        raise ValueError("no saved workflow or executing build in this workspace")
    return max(candidates)[-1]


def _build_snapshot(state):
    snapshot = {"schema": "fusion.watch.v1", "build_id": state["build_id"], "workflow_id": state.get("workflow_id"),
                "status": state["status"], "phase": state["phase"], "message": state["message"],
                "started_at_ms": state["started_at_ms"], "nodes": []}
    if snapshot["status"] == "running" and state.get("coordinator_pid"):
        try:
            os.kill(state["coordinator_pid"], 0)
        except ProcessLookupError:
            snapshot.update(status="interrupted", message="build coordinator is no longer running; inspect the build artifacts")
        except PermissionError:
            pass  # A coordinator owned by another user can still be alive.
    return snapshot


def watch_workflow(workspace, run_id=None, *, as_json=False, once=False, interval=2):
    """Attach to persisted state without resuming or dispatching any work."""
    root = Path(workspace) / ".fusion" / "workflows"
    path = _watch_path(workspace, run_id)
    run_id = path.parent.name
    previous = None
    last_printed = 0.0
    previous_messages = {}
    print_line = lambda message: print(message, flush=True)
    if not as_json:
        print_line(f"Watching {run_id} in {workspace}. Ctrl-C detaches; the run continues.")
    while True:
        manifest = json.loads(path.read_text())
        if path.name == "status.json" and manifest.get("workflow_id"):
            workflow_path = _watch_path(workspace, manifest["workflow_id"])
            if workflow_path.exists():
                path, run_id = workflow_path, manifest["workflow_id"]
                if not as_json:
                    print_line(f"Following workflow {run_id}.")
                manifest = json.loads(path.read_text())
        nodes = manifest.get("nodes", {})
        from fusion_workflow import effective_status
        snapshot = (_build_snapshot(manifest) if path.name == "status.json" else
                    {"schema": "fusion.watch.v1", "workflow_id": run_id, "status": effective_status(manifest), "nodes": []})
        for key, node in nodes.items():
            active_path = root / run_id / "nodes" / key / "active.json"
            active = json.loads(active_path.read_text()) if active_path.exists() else {}
            result = node.get("result") or {}
            current = {"id": key, "status": node["status"], "attempt": node["attempts"], "agent": result.get("agent", node["agent"]),
                       "summary": result.get("summary"), "blockers": result.get("blockers", [])}
            if active.get("run_id"):
                run_dir = Path(workspace) / ".fusion" / "runs" / active["run_id"]
                task_path, activity_file = run_dir / "task.json", run_dir / "activity.json"
                if task_path.exists():
                    current["agent"] = json.loads(task_path.read_text())["agent"]
                if activity_file.exists():
                    current["worker"] = json.loads(activity_file.read_text())
                    current["logs"] = str(run_dir)
            snapshot["nodes"].append(current)
        # Activity timestamps refresh regularly; only meaningful changes need a new frame.
        signature = json.dumps({**snapshot, "nodes": [
            {**node, "worker": {key: value for key, value in node.get("worker", {}).items() if key != "updated_at_ms"}}
            for node in snapshot["nodes"]]}, sort_keys=True)
        changed = signature != previous
        if changed or time.monotonic() - last_printed >= 10:
            if as_json:
                print_line(json.dumps(snapshot))
            else:
                print_line(f"{run_id}: {snapshot['status']}")
                if snapshot.get("build_id"):
                    age = elapsed((time.time() * 1000 - snapshot["started_at_ms"]) / 1000)
                    print_line(f"  {snapshot['phase']}: {clean(snapshot['message'], 600)} ({age} elapsed)")
                for node in snapshot["nodes"]:
                    worker = node.get("worker")
                    detail = ""
                    if worker:
                        age = elapsed((time.time() * 1000 - worker["started_at_ms"]) / 1000)
                        detail = f" — worker {worker['status']}, {age} elapsed, {worker['stdout_bytes']}B stdout / {worker['stderr_bytes']}B stderr"
                    print_line(f"  {node['id']} [{node['agent']}]: {node['status']} (attempt {node['attempt']}){detail}")
                    if node.get("summary"):
                        print_line(f"    {clean(node['summary'], 400)}")
                    for blocker in node.get("blockers", []):
                        print_line(f"    blocker: {clean(blocker, 400)}")
                    if node.get("logs") and previous_messages.get(node["id"]) != node["logs"]:
                        print_line(f"    logs: {node['logs']}")
                        previous_messages[node["id"]] = node["logs"]
                if not snapshot.get("build_id") and not any(node.get("worker") for node in snapshot["nodes"]) and snapshot["status"] == "running":
                    print_line("  Showing saved stage state; this run has not written a live worker activity record.")
            previous, last_printed = signature, time.monotonic()
        if once or snapshot["status"] != "running":
            return 0
        time.sleep(interval)
