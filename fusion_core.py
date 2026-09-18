#!/usr/bin/env python3
"""Small, provider-neutral lead/sidekick harness for Claude Code and Codex CLI."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any, Iterator


SCHEMA = "fusion.v1"
DEFAULTS: dict[str, Any] = {
    "lead": "claude",
    "sidekick": "codex",
    "timeout_seconds": 3600,
    "max_result_chars": 12000,
    "telemetry": {
        "enabled": True,
        "include_content": False,
    },
    "routes": {
        "orc-free": {
            "agent": "claude",
            "command": "orc",
            "model_selector": "free",
            "permission_mode": "plan",
            "permission_prompts": "none",
            "max_budget_usd": 0.10,
        },
        "orc-best": {
            "agent": "claude",
            "command": "orc",
            "model_selector": "best",
            "permission_mode": "plan",
            "permission_prompts": "none",
            "max_budget_usd": 0.40,
        },
        "codex-read": {
            "agent": "codex",
            "sandbox": "read-only",
            "approval": "never",
        },
        "codex-write": {
            "agent": "codex",
            "sandbox": "workspace-write",
            "approval": "never",
        },
    },
    "ultra": {
        "max_stages": 5,
        "stages": {
            "explore": {"agent": "claude", "route": "orc-free", "role": "explorer", "write": False},
            "plan": {"agent": "claude", "route": "orc-free", "role": "planner", "write": False},
            "implement": {"agent": "codex", "role": "implementer", "write": True},
            "review": {"agent": "claude", "route": "orc-free", "role": "reviewer", "write": False},
            "synthesize": {"agent": "claude", "route": "orc-best", "role": "lead reviewer", "write": False},
        },
    },
    "codex": {
        "command": "codex",
        "sandbox": "workspace-write",
        "approval": "never",
        "model": "",
    },
    "claude": {
        "command": "claude",
        "permission_mode": "acceptEdits",
        "permission_prompts": "none",
        "model": "",
        "allowed_tools": [],
    },
}


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def find_upward(name: str, start: Path) -> Path | None:
    current = start.resolve()
    while True:
        candidate = current / name
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def workspace_path(value: str | None) -> Path:
    candidate = Path(value or os.environ.get("FUSION_WORKSPACE", os.getcwd())).expanduser()
    return candidate.resolve()


def load_config(workspace: Path) -> tuple[dict[str, Any], Path | None]:
    path_value = os.environ.get("FUSION_CONFIG")
    path = Path(path_value).expanduser().resolve() if path_value else find_upward(".fusion.json", workspace)
    if path is None:
        return deep_merge({}, DEFAULTS), None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"fusion: cannot read {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit(f"fusion: {path} must contain a JSON object")
    merged = deep_merge(DEFAULTS, parsed)
    # Stage maps are a workflow definition, not additive defaults. A project
    # that supplies its own stages should replace the built-in pipeline.
    if isinstance(parsed.get("ultra"), dict) and "stages" in parsed["ultra"]:
        merged.setdefault("ultra", {})["stages"] = parsed["ultra"]["stages"]
    return merged, path


def now_ms() -> int:
    return int(time.time() * 1000)


def compact(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 80)] + "\n...[truncated by fusion]..."


def parse_handoff(text: str) -> dict[str, Any]:
    """Extract the small handoff contract from a worker's final message."""
    fields: dict[str, str] = {}
    labels = ("STATUS", "SUMMARY", "CHANGED", "TESTS", "BLOCKERS")
    for line in text.splitlines():
        stripped = line.strip()
        for label in labels:
            prefix = f"{label}:"
            if stripped.upper().startswith(prefix):
                fields[label] = stripped[len(prefix):].strip()
                break

    def list_field(name: str) -> list[str]:
        value = fields.get(name, "")
        if not value or value.lower() in {"none", "n/a", "-"}:
            return []
        return [item.strip() for item in value.split(",") if item.strip()]

    reported_status = fields.get("STATUS", "").lower()
    if reported_status not in {"success", "partial", "blocked", "error"}:
        reported_status = ""
    return {
        "reported_status": reported_status,
        "summary": fields.get("SUMMARY", text.strip()),
        "changed": list_field("CHANGED"),
        "tests": list_field("TESTS"),
        "blockers": list_field("BLOCKERS"),
    }


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalized_usage(usage: Any) -> dict[str, float]:
    if not isinstance(usage, dict):
        return {}
    output: dict[str, float] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "reasoning_output_tokens",
        "cost_usd",
        "cost",
    ):
        if key in usage:
            output[key] = number(usage[key])
    cache_creation = usage.get("cache_creation")
    if isinstance(cache_creation, dict):
        output["cache_creation_input_tokens"] = sum(
            number(cache_creation.get(key))
            for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
        )
    return output


def usage_summary(spans: list[dict[str, Any]]) -> dict[str, Any]:
    fields = (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "reasoning_output_tokens",
        "cost_usd",
        "cost",
    )
    total: dict[str, float] = {field: 0.0 for field in fields}
    groups: dict[str, dict[str, Any]] = {}
    for span in spans:
        usage = normalized_usage(span.get("usage"))
        key = "|".join(
            str(span.get(field) or "-") for field in ("agent", "route", "model")
        )
        group = groups.setdefault(
            key,
            {
                "agent": span.get("agent"),
                "route": span.get("route"),
                "model": span.get("model"),
                "calls": 0,
                "success": 0,
                "failed": 0,
                "duration_ms": 0,
                **{field: 0.0 for field in fields},
            },
        )
        group["calls"] += 1
        if span.get("status") == "success":
            group["success"] += 1
        else:
            group["failed"] += 1
        group["duration_ms"] += int(number(span.get("duration_ms")))
        for field in fields:
            value = usage.get(field, 0.0)
            group[field] += value
            total[field] += value
    for item in [total, *groups.values()]:
        for field in fields:
            if item[field].is_integer():
                item[field] = int(item[field])
    return {"spans": len(spans), "total": total, "by_route": list(groups.values())}


class RunStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.root = workspace / ".fusion"
        self.runs = self.root / "runs"
        self.sessions_path = self.root / "sessions.json"
        self.traces_path = self.root / "traces.jsonl"

    def create(self, task: dict[str, Any]) -> Path:
        run_id = task["run_id"]
        run_dir = self.runs / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        self.write_json(run_dir / "task.json", task)
        self.event(run_dir, "run.created", {"task": task})
        return run_dir

    def write_json(self, path: Path, value: Any) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json_text(value) + "\n", encoding="utf-8")
        temp.replace(path)

    def event(self, run_dir: Path, event_type: str, payload: dict[str, Any]) -> None:
        event = {"ts": now_ms(), "type": event_type, **payload}
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def trace_span(
        self,
        config: dict[str, Any],
        task: dict[str, Any],
        result: dict[str, Any],
        started_at_ms: int,
        ended_at_ms: int,
        metadata: dict[str, Any],
    ) -> None:
        telemetry = config.get("telemetry") or {}
        if telemetry.get("enabled", True) is False:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        span = {
            "schema": "fusion.trace.v1",
            "trace_id": task.get("trace_id", task["run_id"]),
            "span_id": uuid.uuid4().hex,
            "parent_span_id": task.get("parent_task_id"),
            "name": f"fusion.{task['agent']}.{task['role']}",
            "start_time_ms": started_at_ms,
            "end_time_ms": ended_at_ms,
            "duration_ms": max(0, ended_at_ms - started_at_ms),
            "status": result.get("status"),
            "agent": task["agent"],
            "role": task["role"],
            "route": task.get("route"),
            "model": metadata.get("model"),
            "write": task.get("write", False),
            "usage": result.get("usage") or {},
            "changed": result.get("changed", []),
            "tests": result.get("tests", []),
            "blockers": result.get("blockers", []),
            "run_id": task["run_id"],
            "artifacts": result.get("artifacts", {}),
        }
        with self.traces_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(span, ensure_ascii=False) + "\n")
        run_dir = result.get("artifacts", {}).get("run_dir")
        if run_dir:
            Path(run_dir, "trace.json").write_text(json_text(span) + "\n", encoding="utf-8")

    def traces(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.traces_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            lines = self.traces_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return rows
        for line in reversed(lines):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
            if len(rows) >= limit:
                break
        return rows

    def sessions(self) -> dict[str, str]:
        if not self.sessions_path.exists():
            return {}
        try:
            value = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def set_session(self, key: str, session_id: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        sessions = self.sessions()
        sessions[key] = session_id
        self.write_json(self.sessions_path, sessions)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.runs.exists():
            return []
        output: list[dict[str, Any]] = []
        for task_path in sorted(self.runs.glob("*/task.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                task = json.loads(task_path.read_text(encoding="utf-8"))
                result_path = task_path.parent / "result.json"
                result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else None
                output.append({"task": task, "result": result})
            except (OSError, json.JSONDecodeError):
                continue
            if len(output) >= limit:
                break
        return output


@contextlib.contextmanager
def writer_lock(workspace: Path, enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return
    lock_dir = workspace / ".fusion"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "workspace-writer.lock"
    with lock_path.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise RuntimeError(f"could not acquire workspace writer lock {lock_path}: {exc}") from exc
        yield
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def make_task(
    workspace: Path,
    agent: str,
    task: str,
    role: str,
    success_criteria: list[str],
    constraints: list[str],
    session_key: str | None,
    resume: bool,
    write: bool,
    parent_task_id: str | None = None,
    route: str | None = None,
    settings_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "trace_id": parent_task_id or run_id,
        "parent_task_id": parent_task_id,
        "agent": agent,
        "role": role,
        "workspace": str(workspace),
        "write": write,
        "resume": resume,
        "session_key": session_key or f"{agent}:{role}",
        "task": task,
        "success_criteria": success_criteria,
        "constraints": constraints,
        "route": route,
        "settings_overrides": settings_overrides or {},
        "created_at": now_ms(),
    }


def brief_for(task: dict[str, Any]) -> str:
    criteria = "\n".join(f"- {item}" for item in task["success_criteria"]) or "- Report what you verified."
    constraints = "\n".join(f"- {item}" for item in task["constraints"]) or "- Keep the change scoped to the task."
    return f"""You are the {task['role']} sidekick in a Fusion coding harness.

Workspace: {task['workspace']}
Task: {task['task']}

Success criteria:
{criteria}

Constraints:
{constraints}

Work directly in the workspace when the task permits writes. Inspect the repository before editing. Run the narrowest meaningful verification. Do not wait for a human response; make reasonable assumptions and report them.

Return a compact handoff with these exact labels:
STATUS: success | partial | blocked | error
SUMMARY: what you did and the current result
CHANGED: comma-separated paths, or none
TESTS: commands run and their outcome, or none
BLOCKERS: unresolved issues, or none
"""


def executable(command: str) -> str | None:
    if os.path.sep in command:
        return command if os.access(command, os.X_OK) else None
    return shutil.which(command)


def parse_codex_events(stdout: str) -> tuple[str | None, str, str | None, dict[str, Any], str | None]:
    thread_id = None
    model = None
    messages: list[str] = []
    failure: str | None = None
    usage: dict[str, Any] = {}
    non_json: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                non_json.append(line)
            continue
        event_type = event.get("type")
        model = model or event.get("model") or event.get("model_id")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
        elif event_type == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                messages.append(str(item["text"]))
        elif event_type == "turn.completed":
            usage = event.get("usage") or {}
        elif event_type == "turn.failed":
            failure = str((event.get("error") or {}).get("message") or "Codex turn failed")
        elif event_type == "error":
            failure = str(event.get("message") or "Codex emitted an error")
    text = messages[-1] if messages else "\n".join(non_json)
    return thread_id, text, failure, usage, model


def parse_claude_output(stdout: str) -> tuple[str | None, str, str | None, dict[str, Any], str | None]:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return None, stdout.strip(), None, {}, None
    if not isinstance(value, dict):
        return None, stdout.strip(), None, {}, None
    session_id = value.get("session_id")
    text = str(value.get("result") or value.get("message") or "")
    failure = text if value.get("is_error") else None
    usage = value.get("usage") or {}
    model = value.get("model") or value.get("model_id")
    return session_id, text, failure, usage, model


def agent_settings(config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    agent = task["agent"]
    settings = deep_merge({}, config.get(agent, {}))
    route_name = task.get("route")
    if route_name:
        route = config.get("routes", {}).get(route_name)
        if not isinstance(route, dict):
            raise ValueError(f"unknown Fusion route: {route_name}")
        route_agent = route.get("agent")
        if route_agent and route_agent != agent:
            raise ValueError(f"route {route_name} is for {route_agent}, not {agent}")
        settings = deep_merge(settings, route)
    overrides = task.get("settings_overrides") or {}
    if isinstance(overrides, dict):
        settings = deep_merge(settings, overrides)
    return settings


def select_orc_model(command: str, selector: str) -> str | None:
    """Ask orc for its current ranked model; never hard-code a volatile model id."""
    if selector not in {"free", "best"}:
        return None
    filter_args = ["--free", "--tools"] if selector == "free" else ["--tools"]
    try:
        completed = subprocess.run(
            [command, "models", *filter_args],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        candidate = line.strip().split(maxsplit=1)
        if candidate and "/" in candidate[0] and not candidate[0].startswith("-"):
            return candidate[0]
    return None


def agent_command(
    config: dict[str, Any],
    task: dict[str, Any],
    session_id: str | None,
) -> tuple[list[str], dict[str, str], dict[str, Any]]:
    agent = task["agent"]
    settings = agent_settings(config, task)
    if agent == "codex":
        command = settings.get("command", "codex")
        argv = [command, "-C", task["workspace"]]
        sandbox = "workspace-write" if task["write"] else "read-only"
        argv += ["-s", sandbox, "-a", settings.get("approval", "never"), "exec", "--json", "--skip-git-repo-check"]
        model = settings.get("model")
        if model:
            argv += ["-m", model]
        if session_id:
            argv = [command, "-C", task["workspace"], "-s", sandbox, "-a", settings.get("approval", "never"), "exec", "resume", session_id, "--json"]
            if model:
                argv += ["-m", model]
        argv.append("-")
        return argv, os.environ.copy(), {"command": command, "model": settings.get("model") or ""}
    if agent == "claude":
        command = settings.get("command", "claude")
        command_name = Path(command).name
        launcher_args = [str(item) for item in settings.get("launcher_args", [])]
        profile = str(settings.get("profile", ""))
        if command_name == "orc" and profile:
            launcher_args.append(profile if profile.startswith("@") else f"@{profile}")
        argv = [command, *launcher_args]
        selected_model = str(settings.get("model", ""))
        if command_name == "orc" and not selected_model:
            selected_model = select_orc_model(command, str(settings.get("model_selector", ""))) or ""
        if command_name == "orc" and selected_model:
            argv += ["-m", selected_model]
        mode = settings.get("permission_mode", "acceptEdits") if task["write"] else "plan"
        argv += ["-p", "--output-format", "json", "--permission-mode", mode]
        permission_prompts = settings.get("permission_prompts")
        if permission_prompts:
            argv += ["--permission-prompts", permission_prompts]
        if command_name != "orc" and selected_model:
            argv += ["--model", selected_model]
        max_budget = settings.get("max_budget_usd")
        if max_budget is not None:
            argv += ["--max-budget-usd", str(max_budget)]
        allowed = settings.get("allowed_tools") or []
        if allowed:
            argv += ["--allowedTools", *allowed]
        if session_id:
            argv += ["--resume", session_id]
        argv.append(brief_for(task))
        return argv, os.environ.copy(), {"command": command, "model": selected_model}
    raise ValueError(f"unsupported agent: {agent}")


def dispatch(
    config: dict[str, Any],
    task: dict[str, Any],
    store: RunStore,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    run_dir = run_dir or store.create(task)
    store.event(run_dir, "run.started", {"agent": task["agent"], "write": task["write"]})
    started_at_ms = now_ms()
    session_id = None
    if task["resume"]:
        session_id = store.sessions().get(task["session_key"])
    argv, env, metadata = agent_command(config, task, session_id)
    task["resolved"] = metadata
    store.write_json(run_dir / "task.json", task)
    binary = executable(argv[0])
    if binary is None:
        result = {
            "schema": SCHEMA,
            "run_id": task["run_id"],
            "status": "error",
            "agent": task["agent"],
            "route": task.get("route"),
            "model": metadata.get("model"),
            "summary": f"{argv[0]} is not available on PATH",
            "changed": [],
            "tests": [],
            "blockers": [f"install or expose {argv[0]} before dispatching"],
            "exit_code": 127,
            "duration_ms": 0,
            "usage": {},
            "artifacts": {"run_dir": str(run_dir)},
        }
        store.write_json(run_dir / "result.json", result)
        store.event(run_dir, "run.finished", {"result": result})
        store.trace_span(config, task, result, started_at_ms, now_ms(), metadata)
        return result
    argv[0] = binary
    prompt = brief_for(task) if task["agent"] == "codex" else None
    started = time.monotonic()
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    status = "error"
    summary = ""
    failure = None
    usage: dict[str, Any] = {}
    model = metadata.get("model")
    handoff: dict[str, Any] = {}
    exit_code = 1
    try:
        with writer_lock(Path(task["workspace"]), task["write"]):
            store.event(run_dir, "worker.started", {"argv": argv, "resumed_session": bool(session_id)})
            completed = subprocess.run(
                argv,
                cwd=task["workspace"],
                env=env,
                input=prompt if task["agent"] == "codex" else None,
                capture_output=True,
                text=True,
                timeout=int(config.get("timeout_seconds", 3600)),
                check=False,
            )
        exit_code = completed.returncode
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        if task["agent"] == "codex":
            new_session, summary, failure, usage, event_model = parse_codex_events(completed.stdout)
        else:
            new_session, summary, failure, usage, event_model = parse_claude_output(completed.stdout)
        model = event_model or model
        handoff = parse_handoff(summary)
        if new_session:
            store.set_session(task["session_key"], new_session)
        if failure:
            status = "error"
        elif exit_code == 0:
            status = handoff.get("reported_status") or "success"
        else:
            status = "error"
            failure = failure or f"worker exited with code {exit_code}"
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        summary = "worker timed out"
        failure = f"timeout after {config.get('timeout_seconds', 3600)} seconds"
        status = "blocked"
        exit_code = 124
    except OSError as exc:
        summary = "could not start worker"
        failure = str(exc)
        status = "error"
        exit_code = 126
    duration_ms = int((time.monotonic() - started) * 1000)
    result = {
        "schema": SCHEMA,
        "run_id": task["run_id"],
        "status": status,
        "agent": task["agent"],
        "role": task["role"],
        "route": task.get("route"),
        "model": model,
        "summary": compact(str(handoff.get("summary") or summary).strip(), int(config.get("max_result_chars", 12000))),
        "changed": handoff.get("changed", []),
        "tests": handoff.get("tests", []),
        "blockers": handoff.get("blockers", []) + ([failure] if failure else []),
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "usage": usage,
        "artifacts": {
            "run_dir": str(run_dir),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
        },
    }
    store.write_json(run_dir / "result.json", result)
    store.event(run_dir, "run.finished", {"result": result})
    store.trace_span(config, task, result, started_at_ms, now_ms(), {**metadata, "model": model})
    return result


ULTRA_STAGE_INSTRUCTIONS: dict[str, str] = {
    "explore": "Map the repository and fact-check the request. Do not edit files. Record relevant files, existing patterns, risks, and the smallest viable change.",
    "spec": "Turn the exploration into a concise implementation specification. Do not edit files. Call out acceptance criteria and unresolved assumptions.",
    "plan": "Create a phased implementation plan from the prior artifacts. Do not edit files. Keep the plan small enough for the stated task.",
    "implement": "Implement the approved scope in the workspace. Inspect the prior artifacts first, keep the diff focused, and run the narrowest meaningful tests.",
    "review": "Review the current diff against the task and prior artifacts. Do not edit files. Check correctness, tests, scope, and regressions.",
    "synthesize": "Act as the lead reviewer. Read all prior stage artifacts, inspect the final diff and tests, and produce the final decision and handoff. Do not edit files.",
}


def ultra_stage_overrides(stage: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "command",
        "model",
        "model_selector",
        "profile",
        "launcher_args",
        "max_budget_usd",
        "permission_mode",
        "permission_prompts",
        "allowed_tools",
    )
    return {key: stage[key] for key in keys if key in stage}


def run_ultra(
    workspace: Path,
    config: dict[str, Any],
    user_task: str,
    max_stages: int | None = None,
    cheap_only: bool = False,
    harness: str | None = None,
) -> dict[str, Any]:
    """Run a bounded UltraCode-style pipeline over shared artifact files."""
    ultra = config.get("ultra") or {}
    configured_stages = ultra.get("stages") or DEFAULTS["ultra"]["stages"]
    if not isinstance(configured_stages, dict):
        raise ValueError("ultra.stages must be an object")
    stage_names = list(configured_stages)
    limit = max_stages if max_stages is not None else int(ultra.get("max_stages", len(stage_names)))
    if limit < 1:
        raise ValueError("ultra stage limit must be at least 1")
    stage_names = stage_names[:limit]

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    ultra_dir = workspace / ".fusion" / "ultra" / run_id
    ultra_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = ultra_dir / "manifest.json"
    manifest: dict[str, Any] = {
        "schema": "fusion.ultra.v1",
        "run_id": run_id,
        "task": user_task,
        "stages": stage_names,
        "artifacts": [],
    }
    manifest_path.write_text(json_text(manifest) + "\n", encoding="utf-8")

    store = RunStore(workspace)
    stage_results: list[dict[str, Any]] = []
    previous_paths: list[str] = []
    pipeline_status = "success"
    for index, stage_name in enumerate(stage_names, start=1):
        raw_stage = configured_stages.get(stage_name) or {}
        if not isinstance(raw_stage, dict):
            raise ValueError(f"ultra stage {stage_name} must be an object")
        stage = dict(raw_stage)
        agent = harness or str(stage.get("agent", "claude"))
        write = bool(stage.get("write", stage_name == "implement"))
        route = stage.get("route")
        if harness == "codex":
            route = "codex-write" if write else "codex-read"
        elif cheap_only and agent == "claude":
            route = "orc-free"
        context_lines = "\n".join(f"- {path}" for path in previous_paths) or "- none; start by inspecting the repository"
        instruction = ULTRA_STAGE_INSTRUCTIONS.get(stage_name, "Complete the assigned bounded stage and report evidence.")
        prompt = f"""You are stage {index}/{len(stage_names)} ({stage_name}) in a bounded Ultra/Fusion coding workflow.

User task:
{user_task}

Stage assignment:
{instruction}

Prior stage artifacts (JSON handoffs; read them from disk when present):
{context_lines}

The lead controls the final decision. Do not broaden the task. Return the exact STATUS, SUMMARY, CHANGED, TESTS, and BLOCKERS handoff labels."""
        stage_settings = stage
        if harness:
            stage_settings = stage.get(harness, {}) if isinstance(stage.get(harness), dict) else {}
        task = make_task(
            workspace,
            agent,
            prompt,
            str(stage.get("role", stage_name)),
            [
                "complete only the assigned stage",
                "return a structured handoff with evidence",
            ],
            [
                "keep the scope bounded to the user task",
                "do not run parallel writers in this workspace",
            ],
            f"ultra:{run_id}:{stage_name}",
            False,
            write,
            parent_task_id=run_id,
            route=route,
            settings_overrides=ultra_stage_overrides(stage_settings),
        )
        try:
            result = dispatch(config, task, store)
        except (OSError, ValueError, RuntimeError) as exc:
            result = {
                "schema": SCHEMA,
                "run_id": task["run_id"],
                "status": "error",
                "agent": agent,
                "role": task["role"],
                "route": route,
                "summary": "stage could not be dispatched",
                "changed": [],
                "tests": [],
                "blockers": [str(exc)],
                "exit_code": 126,
                "duration_ms": 0,
                "usage": {},
                "artifacts": {},
            }
        artifact_path = ultra_dir / f"{index:02d}-{stage_name}.json"
        artifact = {"stage": stage_name, "task": task, "result": result}
        artifact_path.write_text(json_text(artifact) + "\n", encoding="utf-8")
        previous_paths.append(str(artifact_path))
        manifest["artifacts"] = previous_paths
        manifest_path.write_text(json_text(manifest) + "\n", encoding="utf-8")
        stage_results.append(artifact)
        if result.get("status") in {"error", "blocked"}:
            pipeline_status = result["status"]
            break
        if result.get("status") == "partial":
            pipeline_status = "partial"

    final = stage_results[-1]["result"] if stage_results else None
    if final and final.get("status") in {"error", "blocked"}:
        pipeline_status = final["status"]
    return {
        "schema": "fusion.ultra.v1",
        "run_id": run_id,
        "status": pipeline_status,
        "task": user_task,
        "stages": stage_results,
        "final": final,
        "artifacts": {"root": str(ultra_dir), "manifest": str(manifest_path)},
    }


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "fusion_delegate",
            "description": "Delegate a bounded task to the other coding agent and receive a structured handoff. The lead keeps final judgment.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "enum": ["codex", "claude"]},
                    "task": {"type": "string"},
                    "role": {"type": "string", "default": "implementation"},
                    "success_criteria": {"type": "array", "items": {"type": "string"}},
                    "constraints": {"type": "array", "items": {"type": "string"}},
                    "write": {"type": "boolean", "default": True},
                    "resume": {"type": "boolean", "default": True},
                    "session_key": {"type": "string"},
                    "workspace": {"type": "string", "description": "Optional workspace path for the delegated task."},
                    "route": {"type": "string", "description": "Optional named route such as orc-free or orc-best."},
                },
                "required": ["agent", "task"],
            },
        },
        {
            "name": "fusion_status",
            "description": "List recent Fusion runs and their structured results.",
            "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}}},
        },
    ]


def mcp_result(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "structuredContent": payload}


def mcp_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def run_mcp(workspace: Path, config: dict[str, Any]) -> int:
    store = RunStore(workspace)
    for raw in sys.stdin:
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            continue
        request_id = request.get("id")
        method = request.get("method")
        if method == "notifications/initialized":
            continue
        response: dict[str, Any] | None = None
        if method == "initialize":
            response = {
                "protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fusion", "version": "0.1.0"},
            }
        elif method == "tools/list":
            response = {"tools": tool_definitions()}
        elif method == "tools/call":
            params = request.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                if name == "fusion_status":
                    payload = store.recent(int(args.get("limit", 10)))
                elif name == "fusion_delegate":
                    agent = args.get("agent")
                    if agent not in {"codex", "claude"}:
                        raise ValueError("agent must be codex or claude")
                    target = workspace_path(args["workspace"]) if args.get("workspace") else workspace
                    if target != workspace:
                        target_config, _ = load_config(target)
                        target_store = RunStore(target)
                    else:
                        target_config, target_store = config, store
                    task = make_task(
                        target,
                        agent,
                        str(args.get("task", "")),
                        str(args.get("role", "implementation")),
                        [str(item) for item in args.get("success_criteria", [])],
                        [str(item) for item in args.get("constraints", [])],
                        args.get("session_key"),
                        bool(args.get("resume", True)),
                        bool(args.get("write", True)),
                        route=args.get("route"),
                    )
                    if not task["task"]:
                        raise ValueError("task is required")
                    payload = dispatch(target_config, task, target_store)
                else:
                    raise ValueError(f"unknown tool: {name}")
                response = mcp_result(payload)
            except Exception as exc:  # MCP must return a tool error instead of corrupting stdout.
                response = mcp_error(str(exc))
        elif method == "ping":
            response = {}
        if response is not None and request_id is not None:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": response}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


LEAD_PROMPT = """You are the lead agent in a Fusion harness. Own the user conversation, the plan, ambiguity, and final judgment. Use fusion_delegate for bounded work that benefits from a fresh context or a cheaper sidekick. Send a precise brief with success criteria and constraints. Keep writes single-threaded in the shared workspace. Review the returned handoff, inspect the diff and tests yourself, and take control back when the sidekick is out of depth. Do not delegate the final decision or silently accept an unverified result."""


def mcp_config_file(workspace: Path) -> tuple[tempfile.NamedTemporaryFile, Path]:
    script = Path(__file__).resolve().parent / "fusion"
    config = {
        "mcpServers": {
            "fusion": {
                "command": sys.executable,
                "args": [str(script), "mcp-serve"],
                "env": {"FUSION_WORKSPACE": str(workspace)},
            }
        }
    }
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="fusion-mcp-", delete=False, encoding="utf-8")
    json.dump(config, handle)
    handle.write("\n")
    handle.flush()
    return handle, Path(handle.name)


def launch_lead(workspace: Path, config: dict[str, Any], agent: str, task: str | None, interactive: bool = True) -> int:
    if agent == "claude":
        settings = config["claude"]
        command = executable(settings.get("command", "claude"))
        if not command:
            print("fusion: claude is not available on PATH", file=sys.stderr)
            return 127
        handle, config_path = mcp_config_file(workspace)
        handle.close()
        argv = [command, "--mcp-config", str(config_path), "--append-system-prompt", LEAD_PROMPT]
        if not interactive:
            argv += ["-p", "--output-format", "json", "--permission-mode", settings.get("permission_mode", "acceptEdits")]
            permission_prompts = settings.get("permission_prompts")
            if permission_prompts:
                argv += ["--permission-prompts", permission_prompts]
        if task:
            argv += [task]
        try:
            env = os.environ.copy()
            env["FUSION_WORKSPACE"] = str(workspace)
            return subprocess.run(argv, cwd=workspace, env=env, check=False).returncode
        finally:
            config_path.unlink(missing_ok=True)
    if agent == "codex":
        settings = config["codex"]
        command = executable(settings.get("command", "codex"))
        if not command:
            print("fusion: codex is not available on PATH", file=sys.stderr)
            return 127
        script = Path(__file__).resolve().parent / "fusion"
        args_toml = json.dumps([str(script), "mcp-serve"])
        argv = [command, "-C", str(workspace), "-s", settings.get("sandbox", "workspace-write"), "-a", "on-request", "-c", f"mcp_servers.fusion.command={json.dumps(sys.executable)}", "-c", f"mcp_servers.fusion.args={args_toml}"]
        if settings.get("model"):
            argv += ["-m", settings["model"]]
        if interactive:
            if task:
                argv.append(task)
        else:
            argv += ["exec", "--json", "--skip-git-repo-check"]
            if task:
                argv.append(task)
        env = os.environ.copy()
        env["FUSION_WORKSPACE"] = str(workspace)
        return subprocess.run(argv, cwd=workspace, env=env, check=False).returncode
    raise SystemExit(f"fusion: unsupported lead agent {agent}")


def print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json_text(result))
        return
    print(f"{result['status']}: {result.get('summary') or 'no summary'}")
    if result.get("blockers"):
        print("blockers:")
        for blocker in result["blockers"]:
            print(f"  - {blocker}")
    print(f"run: {result['artifacts']['run_dir']}")


def print_ultra_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json_text(result))
        return
    print(f"{result['status']}: Ultra pipeline for {result['task']}")
    for stage in result.get("stages", []):
        stage_result = stage["result"]
        print(f"  {stage['stage']}: {stage_result.get('status')} — {stage_result.get('summary') or 'no summary'}")
    final = result.get("final") or {}
    for blocker in final.get("blockers", []):
        print(f"  blocker: {blocker}")
    print(f"artifacts: {result['artifacts']['root']}")


def doctor(workspace: Path, config: dict[str, Any]) -> int:
    checks = []
    for agent in ("claude", "codex"):
        command = config[agent].get("command", agent)
        path = executable(command)
        checks.append({"agent": agent, "command": command, "path": path, "ok": bool(path)})
    payload = {"workspace": str(workspace), "config": config, "checks": checks}
    print(json_text(payload))
    return 0 if all(item["ok"] for item in checks) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fusion", description="Lead/sidekick orchestration for Claude Code and Codex CLI")
    parser.add_argument("--workspace", help="workspace to operate in; defaults to the current directory")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    lead = sub.add_parser("lead", help="launch an interactive lead agent with the Fusion MCP server")
    lead.add_argument("--agent", choices=["claude", "codex"], help="lead agent; defaults to .fusion.json or claude")
    lead.add_argument("task", nargs="?", help="optional initial task")

    run = sub.add_parser("run", help="launch a non-interactive lead turn with the Fusion MCP server")
    run.add_argument("--agent", choices=["claude", "codex"], help="lead agent; defaults to .fusion.json or claude")
    run.add_argument("task", help="initial task for the lead")

    delegate = sub.add_parser("delegate", help="run one bounded sidekick task")
    delegate.add_argument("--agent", choices=["claude", "codex"], required=True)
    delegate.add_argument("--role", default="implementation")
    delegate.add_argument("--read-only", action="store_true", help="give the worker a read-only workspace")
    delegate.add_argument("--fresh", action="store_true", help="start a fresh agent session")
    delegate.add_argument("--session-key", help="persistent lane name; defaults to agent:role")
    delegate.add_argument("--route", help="named route from .fusion.json, for example orc-free or orc-best")
    delegate.add_argument("--success", action="append", default=[])
    delegate.add_argument("--constraint", action="append", default=[])
    delegate.add_argument("task")

    ultra = sub.add_parser("ultra", help="run a bounded UltraCode-style explore/plan/implement/review pipeline")
    ultra.add_argument("--stages", type=int, help="maximum number of configured stages")
    ultra.add_argument("--cheap-only", action="store_true", help="force Claude stages onto the orc-free route")
    ultra.add_argument("--harness", choices=["claude", "codex"], help="run every Ultra stage through one harness")
    ultra.add_argument("task", help="task for the pipeline")

    sub.add_parser("doctor", help="check the local CLI prerequisites")
    status = sub.add_parser("status", help="show recent runs")
    status.add_argument("--limit", type=int, default=20)
    trace = sub.add_parser("trace", help="show recent telemetry spans")
    trace.add_argument("--limit", type=int, default=100)
    usage = sub.add_parser("usage", help="summarize token usage and latency from telemetry")
    usage.add_argument("--limit", type=int, default=10000)
    sub.add_parser("mcp-serve", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = workspace_path(args.workspace)
    config, config_path = load_config(workspace)
    if args.command == "mcp-serve":
        return run_mcp(workspace, config)
    if args.command == "doctor":
        return doctor(workspace, config)
    if args.command == "status":
        payload = RunStore(workspace).recent(args.limit)
        print(json_text(payload) if args.json else json_text(payload))
        return 0
    if args.command == "trace":
        payload = RunStore(workspace).traces(args.limit)
        print(json_text(payload))
        return 0
    if args.command == "usage":
        payload = usage_summary(RunStore(workspace).traces(args.limit))
        print(json_text(payload))
        return 0
    if args.command == "ultra":
        result = run_ultra(workspace, config, args.task, args.stages, args.cheap_only, args.harness)
        print_ultra_result(result, args.json)
        return 0 if result["status"] == "success" else 1
    if args.command in {"lead", "run"}:
        lead = args.agent or config.get("lead", "claude")
        return launch_lead(workspace, config, lead, args.task, interactive=args.command == "lead")
    if args.command == "delegate":
        task = make_task(
            workspace,
            args.agent,
            args.task,
            args.role,
            args.success,
            args.constraint,
            args.session_key,
            not args.fresh,
            not args.read_only,
            route=args.route,
        )
        result = dispatch(config, task, RunStore(workspace))
        print_result(result, args.json)
        return 0 if result["status"] == "success" else 1
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
