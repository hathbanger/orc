"""The MCP surface for ORC.

Three primitives, mapped onto what ORC already is:

  prompts    the verbs  - entry points that read as slash commands in a chat
  resources  the evidence - `.fusion/` artifacts, addressed by URI
  tools      the operations - start a run, poll it, cancel it, orient yourself

`.fusion/` is already the spine: the CLI and the browser control room are both
readers of it. This module publishes that same spine to an MCP client, so chat
is a third reader rather than a third source of truth.

Long-running work uses durable handles over plain tools - the pattern the spec
calls "stateful tools" - rather than the tasks extension. Tasks has the right
shape, but as of spec revision 2026-07-28 it lives outside core, is absent from
the official client matrix, and no mainstream client implements it. A handle is
a workflow id, which already survives restarts because the manifest is on disk.
When clients ship tasks, `tasks/get`/`update`/`cancel` adapt onto these calls.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from typing import Any

MCP_PROTOCOL_VERSION = "2024-11-05"
RESOURCE_SCHEME = "orc"


# --------------------------------------------------------------------------
# Orientation
# --------------------------------------------------------------------------

def _manifests(workspace: Path, limit: int = 25) -> list[dict[str, Any]]:
    root = workspace / ".fusion" / "workflows"
    if not root.is_dir():
        return []
    found = []
    for directory in sorted(root.iterdir(), reverse=True)[:limit]:
        manifest = directory / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            found.append(json.loads(manifest.read_text()))
        except (OSError, ValueError):
            continue
    return found


def _live_status(manifest: dict[str, Any]) -> str:
    """Status corrected for a coordinator that is no longer running."""
    try:
        from fusion_workflow import effective_status

        return effective_status(manifest) or str(manifest.get("status") or "unknown")
    except Exception:
        return str(manifest.get("status") or "unknown")


def _node_summary(manifest: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in (manifest.get("nodes") or {}).values():
        if isinstance(node, dict):
            key = str(node.get("status") or "unknown")
            counts[key] = counts.get(key, 0) + 1
    return counts


def orientation(workspace: Path) -> dict[str, Any]:
    """Answer "where am I" for one workspace, in one object.

    Deliberately cheap: reads artifacts already on disk, starts nothing, and
    never contacts a provider.
    """
    # Scan a wider window than is displayed: a long run can be older than the
    # ten most recent and still be going, and hiding it is the one mistake an
    # orientation command must not make.
    manifests = _manifests(workspace, limit=60)
    workflows = []
    for manifest in manifests:
        workflows.append(
            {
                "workflow_id": manifest.get("workflow_id"),
                "status": _live_status(manifest),
                "task": manifest.get("task"),
                "nodes": _node_summary(manifest),
                "spent_usd": manifest.get("spent_usd"),
                "started_at_ms": manifest.get("started_at_ms"),
                "error": manifest.get("error"),
            }
        )
    active = [w for w in workflows if w["status"] in {"running", "queued"}]
    recent = workflows[:10]

    laya: dict[str, Any] = {"mode": "unknown"}
    try:
        from fusion_core import load_config
        from fusion_decisions import DecisionStore, config_for

        settings = config_for(load_config(workspace)[0])
        laya = {
            "mode": settings.get("mode", "off"),
            "model": settings.get("model"),
            "recorded_decisions": len(DecisionStore(workspace).records()),
        }
    except Exception:
        pass

    spend = sum(w["spent_usd"] or 0 for w in workflows)
    return {
        "workspace": str(workspace),
        "active_workflows": active,
        "recent_workflows": recent,
        "workflow_spend_usd": round(spend, 4),
        "laya": laya,
        "lanes": _lanes(workspace),
        "next": _suggest(active, recent),
    }


def _lanes(workspace: Path) -> list[dict[str, Any]]:
    """Workers and named routes a lead can pass to fusion_delegate.

    Only names, models and an executable check: no commands, env or keys,
    and nothing is started or contacted.
    """
    try:
        import fusion_core as core

        config = core.load_config(workspace)[0]
    except Exception:
        return []
    lanes = []
    for agent in ("claude", "codex", "agy", "grok", "opencode"):
        settings = config.get(agent) or {}
        lanes.append({"agent": agent, "route": None, "model": settings.get("model") or None,
                      "available": bool(core.executable(str(settings.get("command", agent))))})
    for name, route in (config.get("routes") or {}).items():
        if not isinstance(route, dict) or not route.get("agent"):
            continue
        agent = str(route["agent"])
        command = str(route.get("command") or (config.get(agent) or {}).get("command") or agent)
        lanes.append({"agent": agent, "route": name,
                      "model": route.get("model") or route.get("model_selector") or (config.get(agent) or {}).get("model") or None,
                      "cost_tier": route.get("cost_tier"), "available": bool(core.executable(command))})
    pool = (config.get("decisions") or {}).get("auto_routes")
    for lane in lanes:
        lane["automatic"] = pool is None or (lane["route"] or lane["agent"]) in pool
    return lanes


def _suggest(active: list[dict[str, Any]], recent: list[dict[str, Any]]) -> str:
    if active:
        return f"{len(active)} workflow(s) running. Poll fusion_run_status with workflow_id."
    for workflow in recent:
        if workflow["status"] in {"failed", "blocked", "paused_quota", "paused_budget"}:
            return (
                f"{workflow['workflow_id']} ended {workflow['status']}. Read "
                f"orc://workflow/{workflow['workflow_id']}/report, then resume it."
            )
    if recent:
        return "No workflow is running. Start one with fusion_run_start."
    return "No workflows recorded in this workspace yet."


# --------------------------------------------------------------------------
# Prompts - the verbs
# --------------------------------------------------------------------------

PROMPTS: list[dict[str, Any]] = [
    {
        "name": "where-am-i",
        "title": "Where am I",
        "description": "Summarize what is happening in this workspace right now: running workflows, what failed, what it cost, and the next command.",
        "arguments": [],
    },
    {
        "name": "ship-feature",
        "title": "Ship a feature",
        "description": "Plan, implement, verify and independently review a change, then stop for your approval before publishing.",
        "arguments": [
            {"name": "request", "description": "What to build, in one sentence.", "required": True},
            {"name": "base", "description": "Target branch for the pull request.", "required": False},
        ],
    },
    {
        "name": "review-changes",
        "title": "Review the working tree",
        "description": "Run a read-only review of the current diff across independent workers and report findings with evidence.",
        "arguments": [
            {"name": "focus", "description": "Optional area to concentrate on.", "required": False},
        ],
    },
    {
        "name": "explain-run",
        "title": "Explain a workflow run",
        "description": "Read a finished workflow's receipts and explain what each stage did, what gated, and what it cost.",
        "arguments": [
            {"name": "workflow_id", "description": "The run to explain.", "required": True},
        ],
    },
]


def prompt_messages(name: str, arguments: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Expand a prompt into messages. Starts no work; the model chooses tools."""
    args = arguments or {}
    if name == "where-am-i":
        state = orientation(workspace)
        text = (
            "Here is the current ORC state for this workspace, read from `.fusion/` "
            "artifacts:\n\n```json\n"
            + json.dumps(state, indent=2, ensure_ascii=False)
            + "\n```\n\nSummarize where things stand in a few lines: what is running, "
            "what needs attention, and the single next action. Do not start any work."
        )
    elif name == "ship-feature":
        request = str(args.get("request") or "").strip()
        if not request:
            raise ValueError("ship-feature requires a request")
        base = str(args.get("base") or "").strip()
        text = (
            f"Use ORC to implement this change: {request}\n\n"
            "Call `fusion_run_start` with kind='build' and this request. It returns a "
            "workflow_id immediately; poll `fusion_run_status` with that id rather than "
            "waiting. Every stage is gated on its declared files and acceptance checks, "
            "and an independent worker reviews the result.\n\n"
            + (f"Target branch for publication: {base}\n" if base else "")
            + "When it finishes, read the report resource and summarize what changed and "
            "what the evidence shows. Do not publish a pull request without asking first."
        )
    elif name == "review-changes":
        focus = str(args.get("focus") or "").strip()
        text = (
            "Use ORC to review the current working tree.\n\n"
            "Call `fusion_run_start` with kind='review'"
            + (f" and focus on: {focus}" if focus else "")
            + ". This is read-only - it edits nothing. Poll `fusion_run_status` for the "
            "workflow_id it returns, then read the report resource and present the "
            "findings with the evidence each one cites. Flag any finding whose cited "
            "evidence you cannot confirm."
        )
    elif name == "explain-run":
        workflow_id = str(args.get("workflow_id") or "").strip()
        if not workflow_id:
            raise ValueError("explain-run requires a workflow_id")
        text = (
            f"Read `orc://workflow/{workflow_id}/report` and "
            f"`orc://workflow/{workflow_id}/manifest`, then explain this run: what each "
            "stage was asked to do, which stages were accepted and on what evidence, "
            "which were not, and what it cost. Be explicit about the difference between "
            "what a worker reported and what the run actually verified."
        )
    else:
        raise ValueError(f"unknown prompt: {name}")

    return {
        "description": next((p["description"] for p in PROMPTS if p["name"] == name), name),
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }


# --------------------------------------------------------------------------
# Resources - the evidence
# --------------------------------------------------------------------------

RESOURCE_TEMPLATES: list[dict[str, Any]] = [
    {
        "uriTemplate": "orc://workflow/{workflow_id}/manifest",
        "name": "Workflow manifest",
        "description": "Full persisted state of one workflow: every node, its status, receipts and digests.",
        "mimeType": "application/json",
    },
    {
        "uriTemplate": "orc://workflow/{workflow_id}/report",
        "name": "Workflow report",
        "description": "The readable deliverable for one workflow: final answers, findings, acceptance and blockers.",
        "mimeType": "application/json",
    },
]


def list_resources(workspace: Path) -> list[dict[str, Any]]:
    resources = [
        {
            "uri": "orc://here",
            "name": "Where am I",
            "description": "Current state of this workspace: active workflows, failures, spend, Laya mode.",
            "mimeType": "application/json",
        },
        {
            "uri": "orc://workflows",
            "name": "Workflows",
            "description": "Recent workflow runs in this workspace with their live status.",
            "mimeType": "application/json",
        },
    ]
    for manifest in _manifests(workspace, limit=10):
        workflow_id = manifest.get("workflow_id")
        if not workflow_id:
            continue
        resources.append(
            {
                "uri": f"orc://workflow/{workflow_id}/report",
                "name": f"Report - {workflow_id}",
                "description": str(manifest.get("task") or "")[:200],
                "mimeType": "application/json",
            }
        )
    return resources


def read_resource(uri: str, workspace: Path) -> dict[str, Any]:
    """Resolve one `orc://` URI. Reads artifacts; starts nothing."""
    if not uri.startswith("orc://"):
        raise ValueError(f"unsupported scheme: {uri}")
    path = uri[len("orc://"):].strip("/")
    parts = path.split("/")

    if path == "here":
        payload: Any = orientation(workspace)
    elif path == "workflows":
        payload = {"workflows": orientation(workspace)["recent_workflows"]}
    elif len(parts) == 3 and parts[0] == "workflow":
        workflow_id, leaf = parts[1], parts[2]
        if leaf == "manifest":
            manifest = workspace / ".fusion" / "workflows" / workflow_id / "manifest.json"
            if not manifest.is_file():
                raise ValueError(f"no such workflow: {workflow_id}")
            payload = json.loads(manifest.read_text())
        elif leaf == "report":
            from fusion_workflow import workflow_report

            payload = workflow_report(workspace, workflow_id)
        else:
            raise ValueError(f"unknown workflow resource: {leaf}")
    else:
        raise ValueError(f"unknown resource: {uri}")

    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(payload, indent=2, ensure_ascii=False),
            }
        ]
    }


def evidence_links(workspace: Path, workflow_id: str) -> list[dict[str, Any]]:
    """Resource links for a run, so a result hands back URIs instead of blobs."""
    return [
        {
            "type": "resource_link",
            "uri": f"orc://workflow/{workflow_id}/{leaf}",
            "name": f"{leaf} - {workflow_id}",
            "mimeType": "application/json",
        }
        for leaf in ("manifest", "report")
    ]


# --------------------------------------------------------------------------
# Tools - durable handles over long-running work
# --------------------------------------------------------------------------

RUN_KINDS = {"build", "review", "debug", "discovery"}


def _workflow_ids(workspace: Path) -> set[str]:
    root = workspace / ".fusion" / "workflows"
    return {p.name for p in root.iterdir()} if root.is_dir() else set()


# Coordinators launched by this server. They outlive the call that started
# them by design, so nothing waits on them -- and a child that is never waited
# on becomes a zombie the moment it exits, for the lifetime of this process.
# Poll them whenever we are here anyway; poll() reaps an exited child.
_LAUNCHED: list[Any] = []


def reap_launched() -> int:
    """Reap any coordinator that has exited. Returns how many were collected."""
    reaped = 0
    for proc in list(_LAUNCHED):
        try:
            if proc.poll() is not None:
                _LAUNCHED.remove(proc)
                reaped += 1
        except (OSError, ValueError):
            _LAUNCHED.remove(proc)
    return reaped


def start_run(
    workspace: Path,
    request: str,
    kind: str = "build",
    *,
    base: str | None = None,
    wait_seconds: float = 90.0,
    spawn=subprocess.Popen,
    now=time.monotonic,
    sleep=time.sleep,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """Start a workflow without blocking, and return a durable handle.

    The caller names the run up front and passes it down, so the handle is
    known before the coordinator has written anything. Watching for a new
    directory instead - and assuming the newest one is ours - returns the wrong
    handle the moment two runs start at once.

    The handle is the workflow id, which is on disk, so it survives a restart
    of both this server and the client. `spawn`/`now`/`sleep` are injected so
    the registration logic is testable without launching real workers.
    """
    reap_launched()
    request = (request or "").strip()
    if not request:
        raise ValueError("request is required")
    if kind not in RUN_KINDS:
        raise ValueError(f"kind must be one of {', '.join(sorted(RUN_KINDS))}")

    workflow_id = workflow_id or (
        time.strftime("%Y%m%d-%H%M%S") + f"-wf-{uuid.uuid4().hex[:8]}"
    )
    argv = [
        sys.executable,
        str(Path(__file__).resolve().parent / "fusion"),
        "--workspace",
        str(workspace),
        "--json",
        "build",
        "--kind",
        kind,
        "--kind-source",
        "agent",
        "--execute",
        request,
    ]
    if base:
        argv += ["--base", base]

    from fusion_workflow import WORKFLOW_ID_ENV

    from fusion_core import RunStore
    store = RunStore(workspace)
    environment = {**os.environ, WORKFLOW_ID_ENV: workflow_id}
    if store.control_workspace is not None:
        environment["FUSION_CONTROL_WORKSPACE"] = str(store.control_workspace)
    logs = store.root
    logs.mkdir(parents=True, exist_ok=True)
    handle = open(logs / "mcp-launch.log", "ab")
    try:
        proc = spawn(
            argv,
            cwd=str(workspace),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
    finally:
        handle.close()

    _LAUNCHED.append(proc)
    pid = getattr(proc, "pid", None)
    evidence = [f"orc://workflow/{workflow_id}/{leaf}" for leaf in ("manifest", "report")]
    registered = store.root / "workflows" / workflow_id

    # Confirm the run actually came up, so a launch that dies immediately is
    # reported as failed rather than as a handle that will never resolve.
    deadline = now() + wait_seconds
    while now() < deadline:
        if registered.is_dir():
            return {
                "workflow_id": workflow_id,
                "status": "running",
                "kind": kind,
                "pid": pid,
                "poll_with": "fusion_run_status",
                "evidence": evidence,
            }
        if _launch_died(proc):
            return {
                "workflow_id": workflow_id,
                "status": "failed",
                "kind": kind,
                "pid": pid,
                "error": "the run exited before registering a workflow",
                "log": _tail(logs / "mcp-launch.log"),
            }
        sleep(0.25)

    # Slow, but the handle is real either way - it was chosen, not guessed.
    return {
        "workflow_id": workflow_id,
        "status": "starting",
        "kind": kind,
        "pid": pid,
        "evidence": evidence,
        "note": "Launched and still starting. Poll fusion_run_status with this workflow_id.",
    }


def _launch_died(proc) -> bool:
    poll = getattr(proc, "poll", None)
    return bool(poll and poll() is not None)


def _tail(path: Path, limit: int = 2000) -> str:
    try:
        return path.read_text(errors="replace")[-limit:]
    except OSError:
        return ""


def run_status(workspace: Path, workflow_id: str) -> dict[str, Any]:
    """Poll a handle. Cheap, read-only, safe to call in a loop."""
    reap_launched()
    manifest_path = workspace / ".fusion" / "workflows" / workflow_id / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"no such workflow: {workflow_id}")
    manifest = json.loads(manifest_path.read_text())
    status = _live_status(manifest)
    nodes = {
        node_id: {
            "status": node.get("status"),
            "agent": (node.get("result") or {}).get("agent") or node.get("agent"),
            "attempts": node.get("attempts"),
        }
        for node_id, node in (manifest.get("nodes") or {}).items()
        if isinstance(node, dict)
    }
    return {
        "workflow_id": workflow_id,
        "status": status,
        "done": status not in {"running", "queued"},
        "nodes": nodes,
        "node_counts": _node_summary(manifest),
        "spent_usd": manifest.get("spent_usd"),
        "error": manifest.get("error"),
        "evidence": [f"orc://workflow/{workflow_id}/{leaf}" for leaf in ("manifest", "report")],
    }


def cancel_run(workspace: Path, workflow_id: str, *, kill=None, matches=None) -> dict[str, Any]:
    """Ask a run's coordinator to stop. Cooperative: the run may already be over."""
    import os
    import signal

    from fusion_core import process_alive, process_matches as core_process_matches

    manifest_path = workspace / ".fusion" / "workflows" / workflow_id / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"no such workflow: {workflow_id}")
    manifest = json.loads(manifest_path.read_text())
    pid = manifest.get("coordinator_pid")
    if not pid or process_alive(pid) is False:
        return {"workflow_id": workflow_id, "cancelled": False, "reason": "coordinator is not running"}
    # SIGINT, not SIGTERM. The coordinator installs a handler for SIGINT only
    # (fusion_progress.py), which sets the cancelled flag and unwinds through
    # the cleanup path that stops each worker. Workers start their own session,
    # so killing the coordinator does not cascade to them: a SIGTERM here would
    # take down the coordinator and leave its workers running, still billing.
    identify = matches or core_process_matches
    if not identify(int(pid), workflow_id):
        return {
            "workflow_id": workflow_id,
            "cancelled": False,
            "reason": f"pid {pid} is no longer this workflow's coordinator",
        }
    killer = kill or (lambda target, sig: os.kill(target, sig))
    try:
        killer(int(pid), signal.SIGINT)
    except (OSError, ValueError) as exc:
        return {"workflow_id": workflow_id, "cancelled": False, "reason": str(exc)}
    return {"workflow_id": workflow_id, "cancelled": True, "pid": pid, "signal": "SIGINT"}


ASYNC_TOOLS: list[dict[str, Any]] = [
    {
        "name": "fusion_here",
        "description": (
            "Where am I: active workflows, what failed, what it cost, and the next action "
            "for this workspace. Reads artifacts only - starts nothing, contacts no provider."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "active_workflows": {"type": "array", "items": {"type": "object"}},
                "recent_workflows": {"type": "array", "items": {"type": "object"}},
                "workflow_spend_usd": {"type": "number"},
                "laya": {"type": "object"},
                "next": {"type": "string"},
            },
            "required": ["workspace", "next"],
        },
    },
    {
        "name": "fusion_run_start",
        "description": (
            "Start a bounded ORC workflow and return immediately with a durable handle. "
            "Does NOT block: poll fusion_run_status with the returned workflow_id. The "
            "handle is on disk, so it survives a restart of this server or your client. "
            "Every stage is gated on declared files and acceptance checks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request": {"type": "string", "description": "What to do, in one sentence."},
                "kind": {"type": "string", "enum": sorted(RUN_KINDS), "default": "build"},
                "base": {"type": "string", "description": "Target branch, when publishing."},
            },
            "required": ["request"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": ["string", "null"]},
                "status": {"type": "string"},
                "kind": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["status"],
        },
    },
    {
        "name": "fusion_run_status",
        "description": (
            "Poll a workflow handle returned by fusion_run_start. Read-only and cheap; "
            "safe to call repeatedly. `done` is true once the run reached a terminal state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "status": {"type": "string"},
                "done": {"type": "boolean"},
                "nodes": {"type": "object"},
                "spent_usd": {"type": ["number", "null"]},
                "evidence": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["workflow_id", "status", "done"],
        },
    },
    {
        "name": "fusion_run_cancel",
        "description": (
            "Ask a running workflow's coordinator to stop. Cooperative: accepted stages "
            "stay on disk and the run remains resumable."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "cancelled": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["workflow_id", "cancelled"],
        },
    },
]


def dispatch_async_tool(name: str, args: dict[str, Any], workspace: Path) -> Any:
    """Handle the tools defined in this module. Raises ValueError for others."""
    if name == "fusion_here":
        return orientation(workspace)
    if name == "fusion_run_start":
        return start_run(
            workspace,
            str(args.get("request") or ""),
            str(args.get("kind") or "build"),
            base=args.get("base"),
        )
    if name == "fusion_run_status":
        return run_status(workspace, str(args.get("workflow_id") or ""))
    if name == "fusion_run_cancel":
        return cancel_run(workspace, str(args.get("workflow_id") or ""))
    raise ValueError(f"unknown tool: {name}")


SERVER_CAPABILITIES: dict[str, Any] = {
    "tools": {"listChanged": False},
    "prompts": {"listChanged": False},
    "resources": {"listChanged": False, "subscribe": False},
}
