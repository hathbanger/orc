"""Operator control: a pause that stops new worker runs from every caller.

A control file `{"state": "pause", "reason": ..., "until": ...}` lives in the
host's ORC_HOME (`control.json`) and, when a control workspace is shared, in
`$FUSION_CONTROL_WORKSPACE/.fusion/control.json`. Either one pausing is a pause.
While it holds, `fusion delegate`, workflow nodes and gym runs return
`paused_control` without starting a worker; quota probes still run, so the
operator can see when accounts come back. `until` (ISO time) ends a pause on
its own; without it the pause holds until `fusion control resume`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

import fusion_usage as usage

SCHEMA = "fusion.control.v1"
STATES = ("pause", "run")


def host_path() -> Path:
    return Path(os.environ.get("ORC_HOME") or Path.home() / ".config/orc") / "control.json"


def workspace_path() -> Path | None:
    workspace = os.environ.get("FUSION_CONTROL_WORKSPACE")
    return Path(workspace) / ".fusion" / "control.json" if workspace else None


def paths() -> list[Path]:
    return [path for path in (host_path(), workspace_path()) if path is not None]


def read(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        # An unreadable control file fails closed: the operator meant something by it.
        return {"state": "pause", "reason": f"unreadable control file {path}: {exc}", "path": str(path)}
    if not isinstance(value, dict) or value.get("state") not in STATES:
        return {"state": "pause", "reason": f"invalid control file {path}: state must be one of {', '.join(STATES)}",
                "path": str(path)}
    return {**value, "path": str(path)}


def paused(now: float | None = None) -> dict | None:
    """The pause in force, if any: the first control file that says pause and has not passed its `until`."""
    now = time.time() if now is None else now
    for path in paths():
        value = read(path)
        if not value or value["state"] != "pause":
            continue
        until = usage.timestamp(value.get("until")) if value.get("until") else None
        if until is not None and until.timestamp() <= now:
            continue
        return {"reason": value.get("reason") or "operator pause", "until": usage.iso(until) if until else None,
                "path": value["path"], "since": value.get("since")}
    return None


def status(now: float | None = None) -> dict:
    return {"schema": SCHEMA, "paused": paused(now), "files": {str(path): read(path) for path in paths()}}


def write(state: str, reason: str | None = None, until: str | None = None, path: Path | None = None) -> Path:
    if state not in STATES:
        raise ValueError(f"control state must be one of {', '.join(STATES)}")
    if until and usage.timestamp(until) is None:
        raise ValueError("--until must be an ISO time, for example 2026-10-05T12:00:00Z")
    path = path or host_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"schema": SCHEMA, "state": state, "since": usage.iso(usage.timestamp(time.time())),
             **({"reason": reason} if reason else {}), **({"until": until} if until else {})}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def paused_result(task: dict, pause: dict) -> dict:
    """What a caller gets instead of a run: nothing was started, nothing was spent."""
    until = f" until {pause['until']}" if pause.get("until") else ""
    return {"schema": "fusion.result.v1", "run_id": task.get("run_id"), "workspace": task.get("workspace"),
            "status": "paused_control", "agent": task.get("agent"), "route": task.get("route"), "role": task.get("role"),
            "summary": f"operator pause{until}: {pause['reason']}; no worker was started",
            "blockers": [f"control: {pause['reason']} ({pause['path']})"], "control": pause,
            "changed": [], "tests": [], "artifacts": {"run_dir": None}}
