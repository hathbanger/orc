#!/usr/bin/env python3
"""Persisted, bounded DAG execution for Fusion workflows.

The workflow engine deliberately keeps planning declarative. A model may
propose a graph, but Fusion validates and persists that graph before any
worker is dispatched. This makes fan-out auditable and resumable.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import contextlib
import copy
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
from pathlib import Path, PurePosixPath
import subprocess
import time
import uuid
from typing import Any

import fusion_core as core
import fusion_progress as progress


WORKFLOW_SCHEMA = "fusion.workflow.v1"
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
TERMINAL_SUCCESS = {"success"}
TERMINAL_FAILURE = {"failed", "blocked", "invalid"}
TERMINAL_PAUSED = {"paused_quota", "paused_budget"}
LANE_COOLDOWN_SECONDS = core.LANE_COOLDOWN_SECONDS


WORKFLOW_ID_ENV = "FUSION_WORKFLOW_ID"
# The id becomes a directory name under .fusion/workflows, so it must not be
# able to escape it. No dots, separators, or anything but these characters.
_SAFE_RUN_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def _run_id(prefix: str = "wf") -> str:
    """Mint a workflow id, or adopt one the caller chose.

    A caller that spawns `build --execute` cannot otherwise learn the id it
    just started: it has to watch the filesystem for a new directory and
    assume the newest one is its own, which is wrong the moment two runs start
    at once. Setting FUSION_WORKFLOW_ID lets it name the run up front. It is
    consumed, so it applies to exactly one workflow and a second run in the
    same process cannot collide with it.
    """
    chosen = os.environ.pop(WORKFLOW_ID_ENV, "").strip()
    if not chosen:
        return time.strftime("%Y%m%d-%H%M%S") + f"-{prefix}-{uuid.uuid4().hex[:8]}"
    if not _SAFE_RUN_ID.match(chosen):
        raise ValueError(
            f"{WORKFLOW_ID_ENV} must be 1-64 characters of letters, digits, dash or "
            f"underscore and start alphanumeric; got {chosen!r}"
        )
    return chosen


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _safe_relative(path: Any, field: str) -> str:
    value = str(path or "").strip()
    if not value:
        raise ValueError(f"{field} cannot be empty")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"{field} must be a workspace-relative path: {value}")
    return value


FIXTURE_KEYS = {"path", "content", "from_file"}


def _validate_fixtures(node_id: str, value: Any) -> None:
    """`acceptance.fixtures`: files the coordinator writes into the tree
    for each acceptance check run and removes again afterwards."""
    if value is None:
        return
    field = f"workflow node {node_id} acceptance.fixtures"
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    paths: list[PurePosixPath] = []
    for item in value:
        if (not isinstance(item, dict) or set(item) - FIXTURE_KEYS or ("content" in item) == ("from_file" in item)
                or not isinstance(item.get("content", item.get("from_file")), str)
                or ("from_file" in item and not item["from_file"].strip())):
            raise ValueError(f"{field} entries must be {{path, content | from_file}} with string values")
        path = PurePosixPath(_safe_relative(item.get("path"), field))
        if path.parts[0] in {".git", ".fusion"} or str(path) == ".":
            raise ValueError(f"{field} cannot write {path}")
        paths.append(path)
    for path in paths:
        if sum(other == path or path in other.parents for other in paths) != 1:
            raise ValueError(f"{field} paths must be distinct and must not contain one another: {path}")


def _fixtures(node: dict[str, Any]) -> list[dict[str, Any]]:
    acceptance = node.get("acceptance")
    return list((acceptance.get("fixtures") if isinstance(acceptance, dict) else None) or [])


def _describe(path: Path) -> dict[str, Any]:
    """What a worker left at a fixture path, recorded before it is set aside."""
    if path.is_symlink():
        return {"type": "symlink"}
    if path.is_dir():
        return {"type": "directory"}
    if path.is_file():
        return {"type": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return {"type": "absent"}


def _drop_bytecode(path: Path) -> None:
    """Checks that import a fixture leave its compiled copy in __pycache__;
    it would carry the fixture into the tree the worker sees."""
    cache = path.parent / "__pycache__"
    if path.suffix != ".py" or not cache.is_dir():
        return
    for compiled in cache.iterdir():
        if compiled.name.startswith(path.stem + ".") and compiled.name.endswith(".pyc"):
            compiled.unlink(missing_ok=True)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.is_symlink() or path.exists():
        path.unlink()


MAX_CONTRACT_FILES = 20


def parse_acceptance_contract(answer: str) -> dict[str, Any]:
    """Read the acceptance contract a planning node declared, if any.

    The plan knows what the implementation must produce; the implementation
    node is the one that gets gated on it. Without this the generated path
    gives every node the same two-field handoff check, which cannot tell that
    nothing was built.

    Returns {} for anything malformed. A plan that writes a bad contract must
    not take down the run -- the node's other gates still apply -- but it also
    cannot widen what is enforced by writing nonsense.

    `verification` is recorded as written (argv arrays or plain strings) and
    passed on for the reviewer to rerun. It is executed only for an
    implementation node that opts in with `acceptance.plan_verification`
    (generated builds do), and only what fusion_verification.plan_checks
    admits: these commands are model output and acceptance checks run
    unsandboxed with the user's privileges. Authored specs may still supply
    executable `acceptance.checks`.
    """
    blocks = re.findall(r"```acceptance-contract\s*\n(.*?)\n```", answer or "", re.S)
    if len(blocks) != 1:
        return {}
    try:
        value = json.loads(blocks[0])
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}

    files = []
    for entry in _as_list(value.get("required_files"))[:MAX_CONTRACT_FILES]:
        if not isinstance(entry, str) or not entry.strip():
            continue
        candidate = entry.strip()
        path = PurePosixPath(candidate)
        # It becomes a path under the workspace, so it may not escape it.
        if path.is_absolute() or ".." in path.parts or candidate.startswith("~"):
            continue
        if len(candidate) > 200:
            continue
        files.append(candidate)

    verification: list[Any] = []
    for item in _as_list(value.get("verification"))[:MAX_CONTRACT_FILES]:
        if isinstance(item, str) and item.strip():
            verification.append(item[:400])
        elif (isinstance(item, list) and item and all(isinstance(part, str) for part in item)
              and sum(len(part) for part in item) <= 400):
            verification.append(list(item))
    contract: dict[str, Any] = {}
    if files:
        contract["required_files"] = sorted(dict.fromkeys(files))
    if verification:
        contract["verification"] = verification
    return contract


def _fingerprint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def _format_task(template: str, item: Any) -> str:
    values: dict[str, Any] = {"item": item if isinstance(item, str) else core.json_text(item)}
    if isinstance(item, dict):
        values.update(item)
    try:
        return template.format_map({key: str(value) for key, value in values.items()})
    except (KeyError, ValueError):
        # A task may contain braces as prose. Preserve it rather than making
        # graph expansion itself a failure.
        return template


def expand_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Expand mapped nodes and resolve dependencies to concrete node IDs."""
    raw_nodes = spec.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("workflow.nodes must be a non-empty array")

    expanded: list[dict[str, Any]] = []
    base_to_ids: dict[str, list[str]] = {}
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            raise ValueError("every workflow node must be an object")
        base_id = str(raw.get("id") or "").strip()
        if not base_id or not NODE_ID_RE.fullmatch(base_id):
            raise ValueError(f"invalid workflow node id: {base_id!r}")
        if base_id in base_to_ids:
            raise ValueError(f"duplicate workflow node id: {base_id}")
        items = raw.get("items", raw.get("map"))
        if items is None:
            items_list = [None]
        elif isinstance(items, list) and items:
            items_list = items
        else:
            raise ValueError(f"workflow node {base_id} items must be a non-empty array")

        ids: list[str] = []
        for index, item in enumerate(items_list, start=1):
            node = copy.deepcopy(raw)
            node.pop("items", None)
            node.pop("map", None)
            node["base_id"] = base_id
            node["id"] = base_id if len(items_list) == 1 else f"{base_id}-{index:02d}"
            if not NODE_ID_RE.fullmatch(node["id"]):
                raise ValueError(f"invalid expanded workflow node id: {node['id']}")
            node["item"] = item
            template = str(node.get("task_template", node.get("task", "")))
            if not template:
                raise ValueError(f"workflow node {base_id} needs task or task_template")
            node["task"] = _format_task(template, item) if item is not None else template
            ids.append(node["id"])
            expanded.append(node)
        base_to_ids[base_id] = ids

    concrete_ids = {node["id"] for node in expanded}
    for node in expanded:
        needs: list[str] = []
        for dependency in _as_list(node.get("needs")):
            dependency_id = str(dependency)
            if dependency_id in base_to_ids:
                needs.extend(base_to_ids[dependency_id])
            elif dependency_id in concrete_ids:
                needs.append(dependency_id)
            else:
                raise ValueError(f"workflow node {node['id']} depends on unknown node {dependency_id}")
        node["needs"] = list(dict.fromkeys(needs))
        node["agent"] = str(node.get("agent", "claude"))
        if node["agent"] not in {"auto", "claude", "codex", "agy", "grok", "opencode"}:
            raise ValueError(f"workflow node {node['id']} has unsupported agent {node['agent']}")
        node["role"] = str(node.get("role", node["id"]))
        node["write"] = bool(node.get("write", False))
        node["required_files"] = [
            _safe_relative(path, f"workflow node {node['id']} required_files")
            for path in _as_list(node.get("required_files", node.get("required_outputs")))
        ]

    return {"nodes": expanded}, base_to_ids


def _check_argv(check: Any) -> Any:
    return check.get("argv") if isinstance(check, dict) else check


def _validate_checks(checks: Any) -> None:
    if checks is None:
        return
    if not isinstance(checks, list):
        raise ValueError("acceptance.checks must be a list")
    for check in checks:
        # Preserve the legacy argv gate's runtime validation.
        if not isinstance(check, dict):
            continue
        argv = _check_argv(check)
        if (set(check) - {"argv", "sha256", "path", "min_tests"} or not isinstance(argv, list)
                or not argv or not all(isinstance(p, str) and p and "\x00" not in p for p in argv)):
            raise ValueError("acceptance check object requires argv of non-empty strings and only argv/sha256/path/min_tests")
        if "sha256" in check and (not isinstance(check["sha256"], str)
                                  or not re.fullmatch(r"[0-9a-fA-F]{64}", check["sha256"])):
            raise ValueError("acceptance check sha256 must be a 64-character hexadecimal digest")
        if "path" in check and (not isinstance(check["path"], str) or not check["path"]
                                or "\x00" in check["path"] or "sha256" not in check):
            raise ValueError("acceptance check path must be a non-empty path with sha256")
        if "min_tests" in check and (type(check["min_tests"]) is not int or check["min_tests"] < 0):
            raise ValueError("acceptance check min_tests must be a nonnegative integer")


def _sha256(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except OSError:
        return None


def _tests_run(paths: list[Path]) -> int | None:
    counts = []
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = re.search(r"\bRan (\d+) tests?\b", line)
                if match:
                    counts.append(int(match[1]))
                # Only pytest's terminal summary, not collection/progress lines.
                if re.search(r"\bin \d+(?:\.\d+)?s\b", line):
                    parts = re.findall(r"\b(\d+) (?:passed|failed|errors?|xfailed|xpassed)\b", line)
                    if parts:
                        counts.append(sum(map(int, parts)))
    return max(counts) if counts else None


def validate_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("workflow spec must be an object")
    max_parallel = int(spec.get("max_parallel", 4))
    max_attempts = int(spec.get("max_attempts", 1))
    max_writers = int(spec.get("max_parallel_writers", 1))
    if not 1 <= max_parallel <= 64:
        raise ValueError("workflow.max_parallel must be between 1 and 64")
    if not 1 <= max_attempts <= 5:
        raise ValueError("workflow.max_attempts must be between 1 and 5")
    if not 0 <= max_writers <= 1:
        raise ValueError("workflow.max_parallel_writers must be 0 or 1 until worktree workers are enabled")
    budget = float(spec.get("budget_usd", 0) or 0)
    if budget < 0:
        raise ValueError("workflow.budget_usd cannot be negative")
    graph, _ = expand_spec(spec)
    nodes = graph["nodes"]
    if max_writers == 0 and any(node["write"] for node in nodes):
        raise ValueError("workflow.max_parallel_writers must be at least 1 when a node writes")
    ids = {node["id"] for node in nodes}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError(f"workflow contains a dependency cycle at {node_id}")
        if node_id in visited:
            return
        visiting.add(node_id)
        node = next(item for item in nodes if item["id"] == node_id)
        for dependency in node["needs"]:
            if dependency not in ids:
                raise ValueError(f"workflow node {node_id} depends on unknown node {dependency}")
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node in nodes:
        visit(node["id"])
        if "reasoning_effort" in node:
            from fusion_reasoning import EFFORTS
            if node["agent"] not in {"codex", "claude", "agy", "opencode"} or not isinstance(node["reasoning_effort"], str) or node["reasoning_effort"] not in EFFORTS:
                raise ValueError("workflow reasoning_effort requires an explicit Codex, Claude, agy or OpenCode node and a supported effort")
        if "allow_native_delegation" in node and (node["agent"] != "codex" or not isinstance(node["allow_native_delegation"], bool)):
            raise ValueError("allow_native_delegation requires an explicit Codex node and boolean")
        before = node["acceptance"].get("before") if isinstance(node.get("acceptance"), dict) else None
        if before is not None and (not isinstance(before, bool) or (before and not node["write"])):
            raise ValueError(f"workflow node {node['id']} acceptance.before must be a boolean on a write node")
        if isinstance(node.get("acceptance"), dict):
            _validate_checks(node["acceptance"].get("checks"))
            _validate_fixtures(node["id"], node["acceptance"].get("fixtures"))
            targeted = node["acceptance"].get("fail_to_pass")
            if targeted is not None and (before is not True or not isinstance(targeted, list)
                                         or any(_check_argv(check) not in [_check_argv(c) for c in node["acceptance"].get("checks") or []]
                                                for check in targeted)):
                raise ValueError(f"workflow node {node['id']} acceptance.fail_to_pass must list some of its checks and needs acceptance.before")
            _validate_checks(targeted)
        if node.get("independent_of") and node["independent_of"] not in node["needs"]:
            raise ValueError("independent_of must name a direct dependency")
        if "verification_argv" in node:
            commands = node["verification_argv"]
            if (not node["write"] or not isinstance(commands, list)
                    or not all(isinstance(argv, list) and argv and all(isinstance(p, str) and p and "\x00" not in p for p in argv)
                               for argv in commands)):
                raise ValueError(f"workflow node {node['id']} verification_argv must be a list of argv lists on a write node")

    acceptance = spec.get("acceptance") or {}
    if not isinstance(acceptance, dict):
        raise ValueError("workflow.acceptance must be an object")
    acceptance = copy.deepcopy(acceptance)
    acceptance["required_files"] = [
        _safe_relative(path, "workflow acceptance required_files")
        for path in _as_list(acceptance.get("required_files", acceptance.get("required_outputs")))
    ]
    normalized = copy.deepcopy(spec)
    normalized["schema"] = WORKFLOW_SCHEMA
    normalized["max_parallel"] = max_parallel
    normalized["max_attempts"] = max_attempts
    normalized["max_parallel_writers"] = max_writers
    normalized["budget_usd"] = budget
    normalized["acceptance"] = acceptance
    normalized["graph"] = graph
    if "publish" in spec:
        from fusion_publish import options
        normalized["publish"] = options({}, spec["publish"])
        if normalized["publish"]["mode"] != "off" and not any(n["write"] for n in nodes):
            raise ValueError("Publishing requires an implementation workflow")
    return normalized


def load_spec(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read workflow spec {path}: {exc}") from exc
    return validate_spec(value)


def _authored_before_checks(node: dict[str, Any]) -> list[Any]:
    """A write node's authored argv checks that opted into a pre-change run."""
    acceptance = node.get("acceptance") or {}
    if not node.get("write") or not isinstance(acceptance, dict) or acceptance.get("before") is not True:
        return []
    return [command for command in _as_list(acceptance.get("checks"))
            if isinstance(_check_argv(command), list) and _check_argv(command)
            and all(isinstance(part, str) for part in _check_argv(command))]


def _result_cost(result: dict[str, Any]) -> float:
    usage = result.get("usage") or {}
    return core.number(usage.get("cost_usd", usage.get("cost", 0)))


class WorkflowRunner:
    def __init__(
        self,
        workspace: Path,
        config: dict[str, Any],
        spec: dict[str, Any],
        run_id: str | None = None,
        resume: bool = False,
        worktree: dict[str, Any] | None = None,
        control_workspace: Path | None = None,
    ):
        """`worktree` is a checkout the caller already prepared (`workspace`
        and `base_sha`), as `fusion gym` does: workers and checks run there,
        while runs, traces and decisions stay in the selected control workspace
        (by default `workspace`). Capture the store before starting threads so
        a CLI override wins over the environment in every node."""
        self.workspace = workspace
        self.store = core.RunStore(workspace, control_workspace)
        self.control_workspace = self.store.workspace
        self.config = config
        self.spec = validate_spec(spec)
        self.run_id = run_id or _run_id()
        self.started_at_ms = core.now_ms()
        self.resume = resume
        self.root = self.store.root / "workflows" / self.run_id
        self.nodes_root = self.root / "nodes"
        self.manifest_path = self.root / "manifest.json"
        self.events_path = self.root / "events.jsonl"
        self.git_context = {}
        if resume:
            from fusion_publish import read
            self.git_context = read(self.root / "git.json")
        elif self.spec.get("publish", {}).get("mode", "off") != "off":
            from fusion_publish import setup_worktree
            self.git_context = setup_worktree(workspace, self.run_id, self.spec.get("task", ""), self.spec["publish"])
        elif worktree:
            from fusion_publish import save
            self.git_context = {**worktree, "mode": "off", "isolated": True}
            save(self.root / "git.json", self.git_context)
        if self.git_context:
            self.workspace = Path(self.git_context["workspace"])
        self.workflow_baseline = {
            relative: _fingerprint(self.workspace / relative)
            for relative in self.spec.get("acceptance", {}).get("required_files", [])
        }
        self.nodes: dict[str, dict[str, Any]] = {}
        for node in self.spec["graph"]["nodes"]:
            self.nodes[node["id"]] = {
                **node,
                "status": "pending",
                "attempts": 0,
                "result": None,
                "artifact": str(self.nodes_root / node["id"] / "node.json"),
            }
        self.lane_health: dict[str, dict[str, Any]] = {}
        self.attempt_ledger: list[dict[str, Any]] = []
        if resume:
            self._load_existing()
        else:
            for node in self.nodes.values():
                self._bind_check_contracts(node)
            self.root.mkdir(parents=True, exist_ok=bool(self.git_context))
            self.nodes_root.mkdir(parents=True, exist_ok=True)
            self._write_manifest("running")
            self._event("workflow.created", {"task": self.spec.get("task", "")})
        self._preflight_lanes()

    def _load_existing(self) -> None:
        if not self.manifest_path.exists():
            raise ValueError(f"workflow run does not exist: {self.run_id}")
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read workflow manifest {self.manifest_path}: {exc}") from exc
        if manifest.get("schema") != WORKFLOW_SCHEMA:
            raise ValueError(f"unsupported workflow manifest schema in {self.manifest_id}")
        if isinstance(manifest.get("workflow_baseline"), dict):
            self.workflow_baseline = manifest["workflow_baseline"]
        self.attempt_ledger = manifest.get("attempt_ledger", [])
        if "attempt_ledger" not in manifest:
            # Preserve costs when resuming manifests produced before cumulative accounting.
            self.attempt_ledger = [{"run_id": (node.get("result") or {}).get("run_id"),
                                    "node_id": key, "cost_usd": _result_cost(node.get("result") or {})}
                                   for key, node in manifest.get("nodes", {}).items()]
        recorded = {item.get("run_id") for item in self.attempt_ledger}
        for span in self.store.traces(limit=100000):
            # Recover a receipt written after the last manifest flush (e.g. interrupted coordinator).
            if span.get("trace_id") == self.run_id and span.get("run_id") not in recorded:
                self.attempt_ledger.append({"run_id": span["run_id"], "cost_usd": _result_cost(span),
                                            "usage": span.get("usage", {}), "recovered_from_trace": True})
                recorded.add(span["run_id"])
        for node_id, old in (manifest.get("nodes") or {}).items():
            if node_id not in self.nodes:
                continue
            self.nodes[node_id]["attempts"] = int(old.get("attempts", 0))
            result = old.get("result")
            self.nodes[node_id]["result"] = result
            self.nodes[node_id]["excluded_routes"] = old.get("excluded_routes", [])
            for key in ("_artifact_baseline", "_tree_baseline", "_check_input_pins", "_declared_check_paths",
                        "acceptance_warnings", "review_repair", "repair_feedback"):
                if key in old:
                    self.nodes[node_id][key] = old[key]
            status = str(old.get("status", "pending"))
            denied = core.failure_class(result or {}) == "permission_denied"
            access_changed = (result or {}).get("execution_mode", "restricted") != core.execution_mode(self.config)
            if denied and access_changed:
                failed_lane = (result or {}).get("route") or (result or {}).get("agent")
                self.nodes[node_id]["excluded_routes"] = [lane for lane in self.nodes[node_id]["excluded_routes"] if lane != failed_lane]
            if (status == "paused_quota" or (denied and not access_changed)) and self.nodes[node_id]["agent"] == "auto" and not self.nodes[node_id].get("route"):
                lane = (result or {}).get("route") or (result or {}).get("agent")
                if lane and lane not in self.nodes[node_id]["excluded_routes"]:
                    self.nodes[node_id]["excluded_routes"].append(lane)
            self.nodes[node_id]["status"] = "success" if status == "success" else "pending"
        for node in self.nodes.values():
            self._bind_check_contracts(node, infer=node["attempts"] == 0)
        self._invalidate_stale_receipts()
        self._write_manifest("running")
        self._event("workflow.resumed", {})

    def _definition_digest(self, node: dict[str, Any]) -> str:
        """Hash of everything that defines what a node does, independent of
        who ran it or what it depends on. Deliberately excludes the resolved
        command/model: orc-free/orc-best are meant to re-resolve to a
        different model over time, and invalidating a cached receipt every
        time that catalog reshuffles would make resume useless for them."""
        payload = {
            "task": node["task"],
            "role": node["role"],
            "agent": node["agent"],
            "route": node.get("route"),
            "write": node["write"],
            "required_files": node["required_files"],
            "acceptance": node.get("acceptance") or {},
        }
        # Preserve legacy receipt digests when these new optional fields are absent.
        for key in ("independent_of", "decision_context"):
            if key in node:
                payload[key] = node[key]
        if _fixtures(node):
            payload["fixture_digests"] = self._fixture_digests(node)
        if node["agent"] == "codex":
            from fusion_reasoning import validate_pair
            settings = core.agent_settings(self.config, {"agent": "codex", "route": node.get("route"),
                "settings_overrides": {key: node[key] for key in ("model", "reasoning_effort", "allow_native_delegation") if key in node}})
            if settings.get("reasoning_effort") is not None:
                payload["execution_pair"] = validate_pair(settings.get("model"), settings["reasoning_effort"])
                payload["allow_native_delegation"] = settings.get("allow_native_delegation", False)
        return hashlib.sha256(core.json_text(payload).encode("utf-8")).hexdigest()

    def _input_digest(self, definition_digest: str, dependency_digests: dict[str, str]) -> str:
        payload = {"definition": definition_digest, "dependencies": dependency_digests}
        return hashlib.sha256(core.json_text(payload).encode("utf-8")).hexdigest()

    def _topological_order(self) -> list[str]:
        order: list[str] = []
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            visited.add(node_id)
            for dependency in self.nodes[node_id]["needs"]:
                if dependency in self.nodes:
                    visit(dependency)
            order.append(node_id)

        for node_id in self.nodes:
            visit(node_id)
        return order

    def _invalidate_stale_receipts(self) -> None:
        """Content-address every "success" node against its current
        definition and its dependencies' current digests, processed in
        dependency order. A node whose own definition changed, or whose
        digest no longer matches what was recorded when it last succeeded,
        goes back to pending; that invalidation cascades to dependents in
        the same pass since a dependent's expected digest embeds its
        dependencies' digests. This makes a no-op resume dispatch nothing,
        and an edited node (plus everything downstream of it) rerun."""
        current_digest: dict[str, str] = {}
        for node_id in self._topological_order():
            node = self.nodes[node_id]
            if node["status"] != "success":
                continue
            result = node.get("result") or {}
            if node["write"]:
                self._prepare_plan_checks(node)
                self._baseline_plan_checks(node, run=False)
                self._baseline_authored_checks(node, run=False)
            accepted, problems = self._accept_node(node, result)
            if result.get("check_inputs_changed"):
                from fusion_decisions import DecisionStore
                from fusion_labeling import withdraw_gate_labels
                withdrawal = withdraw_gate_labels(self.control_workspace, result.get("run_id"), result["check_inputs_changed"])
                if withdrawal["decision_ids"]:
                    result["gate_label"] = {**(result.get("gate_label") or {}), **withdrawal}
                DecisionStore(self.control_workspace).append(
                    "outcome_excluded", task_id=result.get("run_id"), group=self.run_id,
                    check_inputs_changed=result["check_inputs_changed"], excluded_reason="check inputs changed (tampered)")
                self._record_gate({"role": node["role"], "trace_id": self.run_id, "write": node["write"]}, result, accepted, problems)
            if result.get("acceptance_checks"):
                # Resume rechecks have their own receipts; keep node.json and
                # the manifest pointing at the same latest observations.
                try:
                    payload = json.loads(Path(node["artifact"]).read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    payload = {}
                payload["result"] = result
                payload["acceptance"] = {"ok": accepted, "problems": problems,
                                         "checks": result["acceptance_checks"]}
                self._save_node(node_id, payload)
            if not accepted:
                node["status"] = "pending"
                continue
            dependency_digests = {dep: current_digest.get(dep) for dep in node["needs"]}
            if any(value is None for value in dependency_digests.values()):
                node["status"] = "pending"
                self._event("node.stale", {"node_id": node_id, "reason": "a dependency was invalidated"})
                continue
            expected = self._input_digest(self._definition_digest(node), dependency_digests)
            if result.get("digest") != expected:
                node["status"] = "pending"
                self._event("node.stale", {"node_id": node_id, "reason": "definition or dependency evidence changed"})
                continue
            current_digest[node_id] = expected
            # A reused plan still declares what its dependents must produce and run.
            node["_contract"] = self._contract_from(result)
            self._event("node.reused", {"node_id": node_id})
            self._emit_cache_hit_telemetry(node_id, node)

    def _emit_cache_hit_telemetry(self, node_id: str, node: dict[str, Any]) -> None:
        """A digest-matched node skips dispatch entirely, so it would
        otherwise never produce a trace span -- locally or remotely. Without
        this, "how much is caching actually saving the group" is invisible
        in the exact data source built to answer questions like that."""
        result = node.get("result") or {}
        resolved = result.get("resolved") or {}
        now = core.now_ms()
        task = {
            "agent": node["agent"],
            "role": node["role"],
            "route": node.get("route"),
            "run_id": f"{self.run_id}:{node_id}:cached",
            "trace_id": self.run_id,
            "parent_task_id": self.run_id,
            "write": node["write"],
        }
        cache_result = {"status": "cache_hit", "usage": {}, "changed": [], "tests": [], "blockers": [], "artifacts": {}}
        metadata = {"model": resolved.get("model")}
        try:
            self.store.trace_span(self.config, task, cache_result, now, now, metadata)
        except OSError:
            pass  # telemetry is best-effort; never let it break a resume.

    @property
    def manifest_id(self) -> str:
        return str(self.manifest_path)

    def _event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        event = {"ts": core.now_ms(), "type": event_type, "workflow_id": self.run_id, **payload}
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        node_id = payload.get("node_id")
        if event_type == "workflow.started":
            progress.emit("workflow", f"{self.run_id}: {' → '.join(self.nodes)} ({len(self.nodes)} nodes)")
            progress.emit("workflow", f"watch from another terminal: fusion workflow watch {self.run_id}")
        elif event_type == "node.started":
            progress.emit(node_id, f"starting node {list(self.nodes).index(node_id) + 1}/{len(self.nodes)}; attempt {payload['attempt']}/{self.spec['max_attempts']}")
        elif event_type == "node.succeeded":
            progress.emit(node_id, f"accepted; {sum(node['status'] == 'success' for node in self.nodes.values())}/{len(self.nodes)} nodes complete")
        elif event_type == "node.switching":
            progress.emit(node_id, f"{payload.get('from_route', 'worker')} reached its provider quota; selecting another healthy worker within the attempt limit" if payload.get("reason") == "quota" else "switching to another permitted worker")
        elif event_type.startswith("node.") and event_type not in {"node.started", "node.succeeded"}:
            detail = "; ".join(str(item) for item in (payload.get("problems") or (payload.get("result") or {}).get("blockers", [])))
            progress.emit(node_id, event_type.removeprefix("node.") + (f": {detail}" if detail else ""))
        elif event_type == "lane.status":
            progress.emit("routing", f"{payload['agent']}: {payload['status']} — {payload['reason']}")
        elif event_type == "workflow.finished":
            progress.emit("workflow", f"{payload['status']}; reported spend ${payload['spent_usd']:.4f}")

    def _write_manifest(self, status: str, error: str | None = None) -> None:
        manifest = {
            "schema": WORKFLOW_SCHEMA,
            "workflow_id": self.run_id,
            "status": status,
            "task": self.spec.get("task", ""),
            "spec": self.spec,
            "workflow_baseline": self.workflow_baseline,
            "git": self.git_context,
            "nodes": self.nodes,
            "lanes": self.lane_health,
            "attempt_ledger": self.attempt_ledger,
            "spent_usd": self._spent(),
            "error": error,
            "artifacts": {
                "root": str(self.root),
                "manifest": str(self.manifest_path),
                "events": str(self.events_path),
            },
            "updated_at": core.now_ms(),
            "started_at_ms": self.started_at_ms,
            "coordinator_pid": os.getpid(),
        }
        if self.store.control_workspace is not None:
            manifest.update(workspace=str(self.workspace.resolve()), control_workspace=str(self.control_workspace))
        temp = self.manifest_path.with_suffix(".tmp")
        temp.write_text(core.json_text(manifest) + "\n", encoding="utf-8")
        temp.replace(self.manifest_path)

    def _node_dir(self, node_id: str) -> Path:
        path = self.nodes_root / node_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _dependency_context(self, node: dict[str, Any]) -> str:
        if not node["needs"]:
            return "- none; begin with repository inspection"
        lines = []
        for dependency in node["needs"]:
            item = self.nodes[dependency]
            lines.append(
                f"- {dependency}: status={item['status']}, artifact={item['artifact']}, "
                f"summary={str((item.get('result') or {}).get('summary', ''))[:500]}"
            )
        return "\n".join(lines)

    def _agent_command_for(self, agent: str, route: str | None = None) -> str:
        settings = core.agent_settings(self.config, {"agent": agent, "route": route, "settings_overrides": {}})
        return str(settings.get("command", agent))

    def _lane_key(self, agent: str, route: str | None = None) -> str:
        """Lane health belongs to an account, not a harness name: `orc` routes
        run Claude Code against OpenRouter, so a claude.ai quota must not cool
        them down (and theirs must not cool native Claude)."""
        try:
            settings = core.agent_settings(self.config, {"agent": agent, "route": route})
        except ValueError:
            settings = {}
        return core.lane_key(agent, settings)

    def _set_lane(self, agent: str, status: str, reason: str) -> None:
        if self.lane_health.get(agent, {}).get("status") == status:
            return
        self.lane_health[agent] = {"status": status, "reason": reason}
        self._event("lane.status", {"agent": agent, "status": status, "reason": reason})

    def _preflight_lanes(self) -> None:
        """Check agent lanes once before dispatch instead of discovering a dead
        lane N times in parallel. Executable checks are free; the quota/session
        cooldown reuses the most recent trace per agent rather than spending a
        real call to find out a lane is already blocked. A resume is an
        explicit "try again now", so it skips the trace-history cooldown but
        still gets the executable check and its own in-run cooldown."""
        lanes = {(node["agent"], node.get("route")) for node in self.nodes.values() if node["agent"] != "auto"}
        for agent, route in lanes:
            command = self._agent_command_for(agent, route)
            if core.executable(command) is None:
                self._set_lane(self._lane_key(agent, route), "blocked", f"{command} is not available on PATH")
        if self.resume:
            return
        store = self.store
        now = core.now_ms()
        seen: set[str] = set()
        for span in store.traces(limit=50):
            if not span.get("agent"):
                continue
            try:
                agent = self._lane_key(span["agent"], span.get("route"))
            except (KeyError, ValueError):
                agent = span["agent"]
            if agent in seen:
                continue
            seen.add(agent)
            if agent in self.lane_health:
                continue
            end_time = span.get("end_time_ms")
            if not isinstance(end_time, (int, float)):
                continue
            age_ms = now - end_time
            if 0 <= age_ms <= LANE_COOLDOWN_SECONDS * 1000 and (span.get("failure_class") or core.failure_class(span)) == "quota":
                self._set_lane(agent, "cooldown", f"a {agent} run reported a quota/session limit {int(age_ms / 1000)}s ago")

    def _prompt(self, node: dict[str, Any]) -> str:
        required = ", ".join(node.get("required_files", [])) or "none"
        feedback = node.get("repair_feedback")
        repair = ("\n\nRepair the following review blockers, then rerun relevant verification:\n"
                  + core.json_text(feedback)) if feedback else ""
        return f"""You are node {node['id']} in a persisted Fusion workflow.

Workflow: {self.run_id}
Role: {node['role']}
Task: {node['task']}{repair}

Dependency artifacts:
{self._dependency_context(node)}

Previous attempt (repair the reported failure; inspect its logs):
{core.json_text(node.get('result')) if node.get('result') else 'none'}

Required workspace artifacts: {required}
Read dependency artifacts before acting. Keep the scope limited to this node.
If this node writes code, run the narrowest meaningful verification. The
orchestrator will validate required artifacts after your turn.

Return the exact labels:
STATUS: success | partial | blocked | error
SUMMARY: what you did and the current result
CHANGED: comma-separated paths, or none
TESTS: commands run and their outcome, or none
BLOCKERS: unresolved issues, or none
"""

    def _contract_from(self, result: dict[str, Any]) -> dict[str, Any]:
        """The acceptance contract this node's answer declared, if any."""
        path = ((result or {}).get("artifacts") or {}).get("answer")
        if not path:
            return {}
        try:
            return parse_acceptance_contract(Path(path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return {}

    def _inherited_required_files(self, node: dict[str, Any]) -> list[str]:
        """Artifacts a dependency's plan says this node must produce.

        Only write nodes inherit: a reviewer is not the one creating the files,
        and gating it on their existence would blame the wrong node.
        """
        if not node.get("write"):
            return []
        files: list[str] = []
        for dependency in node.get("needs") or []:
            contract = (self.nodes.get(dependency) or {}).get("_contract") or {}
            files.extend(contract.get("required_files") or [])
        return files

    def _tree(self) -> str | None:
        """Git tree hash of the working state, or None when it cannot be read.

        Unavailable is not evidence of no change, so callers must treat None
        as "unknown" and leave the node's other gates to decide.
        """
        try:
            from fusion_publish import snapshot

            return snapshot(self.workspace)
        except Exception:
            return None

    def _check_inputs(self, check: Any, *, include_directories: bool = False) -> list[str]:
        """Existing workspace inputs, including unittest discovery's test files."""
        argv = _check_argv(check)
        if not isinstance(argv, list) or not all(isinstance(p, str) for p in argv):
            return []
        root = self.workspace.resolve()
        workspace_alias = Path(os.path.abspath(self.workspace))
        candidates = list(argv)
        if isinstance(check, dict) and check.get("path"):
            candidates.append(check["path"])
        if len(argv) >= 4 and Path(argv[0]).name.startswith("python") and argv[1:4] == ["-m", "unittest", "discover"]:
            start, pattern = ".", "test*.py"
            args = iter(argv[4:])
            for arg in args:
                if arg in {"-s", "--start-directory"}:
                    start = next(args, ".")
                elif arg in {"-p", "--pattern"}:
                    pattern = next(args, "test*.py")
                elif arg.startswith("--start-directory="):
                    start = arg.split("=", 1)[1]
                elif arg.startswith("--pattern="):
                    pattern = arg.split("=", 1)[1]
            directory = root / start
            if directory.is_dir() and directory.resolve().is_relative_to(root):
                candidates.extend(str(p) for p in directory.rglob("*") if fnmatch.fnmatch(p.name, pattern))
        inputs = set()
        for part in candidates:
            try:
                path = Path(os.path.abspath(self.workspace / part))
                if ((path.is_file() or (include_directories and path.is_dir()))
                        and path.resolve().is_relative_to(root)):
                    # Keep symlinks inside the workspace in the saved path:
                    # redirecting a link must be observed at the gate too.
                    base = workspace_alias if path.is_relative_to(workspace_alias) else root
                    relative = path.relative_to(base) if path.is_relative_to(base) else path.resolve().relative_to(root)
                    inputs.add(str(relative))
            except (OSError, ValueError):
                continue
        return sorted(inputs)

    def _pin_check_inputs(self, node: dict[str, Any]) -> None:
        checks = [*_as_list((node.get("acceptance") or {}).get("checks")), *(node.get("_plan_checks") or [])]
        # Never recapture after a worker has run, including older manifests
        # without pins. An empty dictionary is still a saved baseline.
        if node["attempts"] == 1 and "_check_input_pins" not in node:
            paths = {path for check in checks for path in self._check_inputs(check)}
            node["_check_input_pins"] = {path: _sha256(self.workspace / path) for path in sorted(paths)}
        warnings = [f"Unpinned acceptance check uses workspace input: {path}"
                    for path in sorted({path for check in checks if isinstance(check, list)
                                        for path in self._check_inputs(check, include_directories=True)})]
        node["acceptance_warnings"] = warnings
        for warning in warnings:
            self._event("acceptance.check.warning", {"node_id": node["id"], "message": warning})

    def _bind_check_contracts(self, node: dict[str, Any], *, infer: bool = True) -> None:
        """Choose evaluator paths before dispatch; never infer from a worker's tree.

        Explicit paths also support fixtures or evaluators that do not exist yet.
        Older resumes lacking a binding must declare a path instead of letting
        newly planted argv files silently select a different evaluator.
        """
        acceptance = node.get("acceptance") or {}
        if not isinstance(acceptance, dict):
            return
        paths = node.setdefault("_declared_check_paths", {})
        for check in _as_list(acceptance.get("checks")):
            if not isinstance(check, dict) or "sha256" not in check:
                continue
            key = json.dumps(check, sort_keys=True)
            if key in paths:
                continue
            path = self.workspace / check["path"] if "path" in check else None
            if path is None and infer:
                # Bare argv[0] is resolved by the subprocess through PATH,
                # never by treating a same-named workspace file as the program.
                path = next((self.workspace / part for index, part in enumerate(check["argv"])
                             if (index != 0 or os.path.dirname(part))
                             and (self.workspace / part).is_file()), None)
            if path is None:
                raise ValueError(f"workflow node {node['id']} cannot bind acceptance sha256 to a file before dispatch; "
                                 'declare an explicit "path"')
            # Keep symlinks in the path: hashing a resolved target would miss a
            # worker redirecting the path that the command actually executes.
            paths[key] = os.path.abspath(path)

    @contextlib.contextmanager
    def _declared_check_contract(self, check: dict[str, Any], receipt: dict[str, Any], bound_path: str | None):
        """Check the actual evaluator after fixtures are installed, before launch."""
        if "sha256" in check:
            path = Path(bound_path) if bound_path is not None else None
            observed = _sha256(path) if path is not None else None
            receipt["contract"] = {"path": str(path) if path else None, "sha256": check["sha256"].lower(),
                                   "observed_sha256": observed}
            if observed != check["sha256"].lower():
                receipt["problem_code"] = "check_contract_changed"
                raise ValueError("acceptance check contract changed; review and version the contract explicitly")
        yield

    def _acceptance_check(self, node: dict[str, Any], result: dict[str, Any],
                          command: Any, index: int, *, phase: str = "after",
                          env: dict[str, str] | None = None, timeout: int | None = None,
                          annotations: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record coordinator observations independently of a worker's handoff.

        Each invocation has its own directory, including resume rechecks. Output
        goes straight to files so a verbose check cannot exhaust coordinator
        memory. The initial receipt survives an interrupted coordinator; only a
        finalized, successful receipt can pass the gate. A direct exit is not
        evidence that an entire process family finished: POSIX checks with a
        surviving process group fail and that group is terminated. Descendants
        that escape into another session are not observed or claimed terminated.
        Final logs are detached prefix snapshots, so inherited output descriptors
        cannot keep modifying the files referenced by a finalized receipt.

        `phase="before"` runs a plan's verification on the tree before the
        implementation starts (its receipt is the vacuity baseline); `env`
        adds variables to the inherited environment and only their names are
        recorded; `annotations` are saved in the receipt (origin, vacuity).
        """
        declaration = command if isinstance(command, dict) else {}
        command = _check_argv(command)
        attempt = int(result.get("attempt") or node.get("attempts") or 0)
        directory = (self._node_dir(node["id"]) / "acceptance" / f"attempt-{attempt}"
                     / f"{'before-' if phase == 'before' else ''}check-{index + 1}-{uuid.uuid4().hex[:12]}")
        directory.mkdir(parents=True)
        receipt_path = directory / "receipt.json"
        stdout_path, stderr_path = directory / "stdout.log", directory / "stderr.log"
        started = time.monotonic()
        receipt = {
            "schema": "fusion.acceptance-check.v1",
            "workflow_id": self.run_id,
            "node_id": node["id"],
            "run_id": result.get("run_id"),
            "attempt": attempt,
            "check_index": index,
            **({"phase": phase} if phase != "after" else {}),
            **({"env_overrides": sorted(env)} if env else {}),
            **(annotations or {}),
            "argv": command,
            **({"declaration": declaration} if declaration else {}),
            **({"check_inputs_changed": result["check_inputs_changed"]} if result.get("check_inputs_changed") else {}),
            "cwd": str(self.workspace.resolve()),
            "started_at_ms": core.now_ms(),
            "finished_at_ms": None,
            "duration_ms": None,
            "status": "running",
            "exit_code": None,
            "timed_out": False,
            "error": None,
            "process": {"pid": None, "exit_observed": False, "group_id": None,
                        "group_survivors_after_exit": None, "group_termination_requested": False,
                        "group_survivors_after_cleanup": None,
                        "descendant_scope": "same_posix_process_group_only" if os.name == "posix" else "unobserved",
                        "escaped_sessions": "unobserved"},
            "output_scope": "detached_observed_prefix_snapshot",
            "artifacts": {"receipt": str(receipt_path), "stdout": str(stdout_path), "stderr": str(stderr_path)},
        }

        def save() -> None:
            temp = receipt_path.with_suffix(".tmp")
            temp.write_text(core.json_text(receipt) + "\n", encoding="utf-8")
            temp.replace(receipt_path)

        save()
        self._event("acceptance.check.started", {"node_id": node["id"], "attempt": attempt,
                                                 "receipt": str(receipt_path)})
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
                    raise ValueError("acceptance checks must be argv arrays")
                timeout = int(timeout or self.config.get("timeout_seconds", 3600))
                receipt["timeout_seconds"] = timeout
                with self._fixtures_applied(node, directory, receipt), \
                        self._declared_check_contract(declaration, receipt,
                            (node.get("_declared_check_paths") or {}).get(json.dumps(declaration, sort_keys=True))), \
                        subprocess.Popen(command, cwd=self.workspace, stdin=subprocess.DEVNULL,
                                         env={**os.environ, **env} if env else None, stdout=stdout, stderr=stderr,
                                         start_new_session=(os.name == "posix")) as process:
                    observed = receipt["process"]
                    observed["pid"] = process.pid
                    observed["group_id"] = process.pid if os.name == "posix" else None

                    def group_alive() -> bool:
                        try:
                            os.killpg(process.pid, 0)
                            return True
                        except ProcessLookupError:
                            return False

                    def terminate_group() -> None:
                        observed["group_termination_requested"] = True
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

                    try:
                        process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        receipt["timed_out"] = True
                        receipt["error"] = f"acceptance check timed out after {timeout} seconds"
                        if os.name == "posix":
                            terminate_group()
                        else:
                            process.kill()
                        process.wait()
                    observed["exit_observed"] = True
                    receipt["exit_code"] = process.returncode
                    if os.name == "posix":
                        observed["group_survivors_after_exit"] = group_alive()
                        if observed["group_survivors_after_exit"]:
                            if not receipt["error"]:
                                receipt["error"] = "acceptance check exited with processes remaining in its process group"
                            terminate_group()
                        # This is an observation, not a process-family guarantee:
                        # killed but unreaped descendants may remain visible.
                        observed["group_survivors_after_cleanup"] = group_alive()
                receipt["status"] = ("timed_out" if receipt["timed_out"] else
                                     "error" if receipt["error"] else
                                     "passed" if receipt["exit_code"] == 0 else "failed")
            except (OSError, ValueError) as exc:
                receipt["status"] = "error"
                receipt["error"] = f"{type(exc).__name__}: {exc}"
        receipt["finished_at_ms"] = core.now_ms()
        receipt["duration_ms"] = max(0, round((time.monotonic() - started) * 1000))
        receipt["outputs"] = {}
        for name, path in (("stdout", stdout_path), ("stderr", stderr_path)):
            digest = hashlib.sha256()
            size = 0
            snapshot = path.with_suffix(".snapshot")
            with path.open("rb") as handle, snapshot.open("xb") as target:
                observed_size = os.fstat(handle.fileno()).st_size
                remaining = observed_size
                # A descendant may still hold the old inode, including one
                # outside the observed group. Bound the copy to this prefix;
                # following a growing capture until EOF could run indefinitely.
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise OSError("acceptance output was truncated while snapshotting")
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    remaining -= len(chunk)
            snapshot.replace(path)
            receipt["outputs"][name] = {"path": str(path), "sha256": digest.hexdigest(), "bytes": size,
                                        "capture_bytes_observed": observed_size}
        if "min_tests" in declaration:
            receipt["min_tests"] = declaration["min_tests"]
            receipt["tests_run"] = _tests_run([stdout_path, stderr_path])
            if receipt["status"] in {"passed", "failed"} and (receipt["tests_run"] is None or receipt["tests_run"] < declaration["min_tests"]):
                receipt.update(status="failed", problem_code="check_tests_missing",
                               error=f"acceptance check ran {receipt['tests_run']} tests; expected at least {declaration['min_tests']}")
        save()
        self._event("acceptance.check.finished", {"node_id": node["id"], "attempt": attempt,
                                                  "status": receipt["status"], "receipt": str(receipt_path)})
        return receipt

    def _fixture_bytes(self, item: dict[str, Any]) -> bytes:
        if "content" in item:
            return item["content"].encode("utf-8")
        source = Path(item["from_file"]).expanduser()
        base = self.workspace if self.store.control_workspace is not None else self.control_workspace
        return (source if source.is_absolute() else base / source).read_bytes()

    def _fixture_digests(self, node: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Path and content digest of each fixture, or None when one is unreadable."""
        try:
            return [{"path": item["path"], "sha256": hashlib.sha256(self._fixture_bytes(item)).hexdigest()}
                    for item in _fixtures(node)]
        except OSError:
            return None

    @contextlib.contextmanager
    def _fixtures_applied(self, node: dict[str, Any], directory: Path, receipt: dict[str, Any]):
        """Write `acceptance.fixtures` for one check run, then put the tree back.

        Whatever the worker left at a fixture path is moved aside first and
        restored afterwards, so the check always runs the fixture's content
        and the worker never sees it: not before its turn, between attempts,
        nor in the diff. The receipt records each fixture's digest and what
        was found at its path.
        """
        fixtures = _fixtures(node)
        if not fixtures:
            yield
            return
        loaded = [(item["path"], self._fixture_bytes(item)) for item in fixtures]
        root = self.workspace.resolve()
        applied: list[tuple[Path, Path | None, Path | None]] = []
        records: list[dict[str, Any]] = []
        try:
            for index, (relative, data) in enumerate(loaded):
                target = self.workspace / relative
                if not target.parent.resolve().is_relative_to(root):
                    raise ValueError(f"acceptance fixture {relative} resolves outside the workspace")
                found = _describe(target)
                moved = None
                if found["type"] != "absent":
                    moved = directory / "fixture-stash" / str(index)
                    moved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(moved))
                created, probe = None, target.parent
                while probe != self.workspace and not probe.exists():
                    created, probe = probe, probe.parent
                applied.append((target, moved, created))
                target.parent.mkdir(parents=True, exist_ok=True)
                _drop_bytecode(target)
                target.write_bytes(data)
                records.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "found": found})
            receipt["fixtures"] = records
            yield
        finally:
            for target, moved, created in reversed(applied):
                _remove(target)
                _drop_bytecode(target)
                if created is not None:
                    shutil.rmtree(created, ignore_errors=True)
                if moved is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(moved), str(target))

    def _plan_verification(self, node: dict[str, Any]) -> list[Any]:
        """Verification commands a dependency's plan declared for this node.

        Only a write node that opts in with `acceptance.plan_verification`
        (generated builds) inherits them, so authored workflows keep running
        exactly the `acceptance.checks` they wrote.
        """
        acceptance = node.get("acceptance") or {}
        if not node.get("write") or not isinstance(acceptance, dict) or not acceptance.get("plan_verification"):
            return []
        items: list[Any] = []
        for dependency in node.get("needs") or []:
            contract = (self.nodes.get(dependency) or {}).get("_contract") or {}
            items.extend(contract.get("verification") or [])
        return items

    def _prepare_plan_checks(self, node: dict[str, Any]) -> None:
        from fusion_verification import plan_checks

        items = self._plan_verification(node)
        checks, rejected = plan_checks(items, self.config) if items else ([], [])
        node["_plan_checks"], node["_plan_checks_rejected"] = checks, rejected
        if rejected:
            self._event("acceptance.plan_checks.rejected", {"node_id": node["id"], "rejected": rejected})

    def _baseline_plan_checks(self, node: dict[str, Any], run: bool = True) -> None:
        """Run the plan's checks once, on the tree before the first attempt.

        A check that already passes there cannot tell whether the work was
        done: its later pass is recorded `vacuous` and never labels the node.
        The baseline is saved beside the node and reused by retries and
        resumes, whose tree already holds an earlier attempt's changes; a
        later attempt without a saved baseline runs none and leaves vacuity
        unknown.
        """
        from fusion_verification import OFFLINE_ENV, settings

        self._baseline(node, node.get("_plan_checks") or [], "before.json", "_plan_before",
                       "running the plan's verification on the tree before the change",
                       env=OFFLINE_ENV, timeout=settings(self.config)["timeout_seconds"], run=run)
        self._drop_unstartable(node)

    def _baseline_authored_checks(self, node: dict[str, Any], run: bool = True) -> None:
        """`acceptance.before: true` gives a write node's authored checks the
        same pre-change run as plan checks, so their vacuity is known and the
        gate can label them. They run with the same environment and timeout
        before and after, so only the tree differs between the two runs."""
        self._baseline(node, _authored_before_checks(node), "before-authored.json", "_authored_before",
                       "running the acceptance checks on the tree before the change", run=run)

    def _baseline(self, node: dict[str, Any], checks: list[Any], filename: str, key: str, activity: str, *,
                  env: dict[str, str] | None = None, timeout: int | None = None, run: bool = True) -> None:
        path = self._node_dir(node["id"]) / "acceptance" / filename
        node[key] = {}
        if not checks:
            return
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        fixtures = self._fixture_digests(node) if _fixtures(node) else None
        if isinstance(saved, dict) and saved.get("checks") == checks and saved.get("fixtures") == fixtures:
            node[key] = saved
            return
        if node["attempts"] > 1 or not run:
            return
        receipts = []
        with progress.activity(node["id"], activity):
            for index, command in enumerate(checks):
                try:
                    receipts.append(self._acceptance_check(node, {"attempt": node["attempts"]}, command, index,
                                                           phase="before", env=env, timeout=timeout))
                except OSError as exc:
                    receipts.append({"argv": _check_argv(command), "phase": "before", "status": "error", "error": str(exc)})
        node[key] = {"checks": checks, "receipts": receipts, **({"fixtures": fixtures} if fixtures else {})}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(core.json_text(node[key]) + "\n", encoding="utf-8")

    def _drop_unstartable(self, node: dict[str, Any]) -> None:
        """A plan command whose program could not even start before the change
        (not installed, not executable) says nothing about the implementation
        and would only fail its gate: it is moved to the rejected list."""
        receipts = (node.get("_plan_before") or {}).get("receipts") or []
        if not receipts:
            return
        started = []
        for receipt in receipts:
            command = receipt.get("argv")
            if receipt.get("status") == "error" and not (receipt.get("process") or {}).get("pid"):
                node["_plan_checks_rejected"] = [*(node.get("_plan_checks_rejected") or []), {
                    "command": command, "reason": f"could not start before the change: {receipt.get('error')}"}]
            else:
                started.append(command)
        node["_plan_checks"] = [command for command in node.get("_plan_checks") or [] if command in started]

    def _accept_node(self, node: dict[str, Any], result: dict[str, Any]) -> tuple[bool, list[str]]:
        accepted, problems, _ = self._gate(node, result)
        return accepted, problems

    def _gate(self, node: dict[str, Any], result: dict[str, Any]) -> tuple[bool, list[str], list[dict[str, Any]]]:
        """Structural acceptance: (accepted, problems, codes).

        Each problem string has one structured code beside it
        ({"code": ..., plus details}), also saved as result["gate_codes"], so
        labeling and outcomes can tell objective evidence (a check's exit,
        an unchanged tree, a missing file) from what came out of parsing the
        worker's handoff (blockers, empty handoff fields).
        """
        # Worker-provided fields cannot masquerade as coordinator evidence.
        # Older/rechecked receipts remain on disk in their unique directories.
        result["acceptance_checks"] = []
        result["gate_codes"] = []
        result["check_inputs_changed"] = sorted(path for path, digest in (node.get("_check_input_pins") or {}).items()
                                                if _sha256(self.workspace / path) != digest)
        if result["check_inputs_changed"]:
            self._event("node.check_inputs_changed", {"node_id": node["id"], "check_inputs_changed": result["check_inputs_changed"]})
        if core.failure_class(result) == "coordinator_error":
            # The coordinator failed to establish the review evidence. Worker
            # handoff checks cannot repair this and only obscure the real error.
            result["gate_codes"] = [{"code": "coordinator_error"}]
            return False, [], result["gate_codes"]
        problems: list[str] = []
        codes: list[dict[str, Any]] = result["gate_codes"]

        def problem(code: str, text: str, **detail: Any) -> None:
            problems.append(text)
            codes.append({"code": code, **detail})

        if result.get("status") not in TERMINAL_SUCCESS:
            problem("worker_status", f"worker status is {result.get('status', 'unknown')}", status=result.get("status"))
        if result.get("blockers"):
            problem("worker_blockers", "worker reported unresolved blockers")
        for relative in node.get("required_files", []):
            path = self.workspace / relative
            if not path.is_file():
                problem("required_file_missing", f"required artifact is missing: {relative}", path=relative)
            else:
                baseline = (node.get("_artifact_baseline") or {}).get(relative)
                if baseline and _fingerprint(path) == baseline:
                    problem("required_file_unchanged", f"required artifact did not change during node: {relative}", path=relative)
        acceptance = node.get("acceptance") or {}
        if not isinstance(acceptance, dict):
            acceptance = {}
        # Compare the tree before any check runs: a check's own caches would
        # otherwise make an untouched tree look changed.
        unchanged = False
        if node.get("write") and not acceptance.get("allow_no_changes"):
            # required_handoff only asks whether a field is non-empty, so a
            # worker reporting "TESTS: not run" satisfies it. Generated builds
            # declare no required_files, which left the primary path unable to
            # notice that an implementation node implemented nothing. Compare
            # the repository against its own pre-dispatch tree: a worker
            # cannot misreport that the way it can CHANGED.
            baseline = node.get("_tree_baseline")
            unchanged = bool(baseline) and self._tree() == baseline
        for field in _as_list(acceptance.get("required_handoff")):
            if not result.get(str(field)):
                problem("required_handoff_empty", f"required handoff field is empty: {field}", field=str(field))
        from fusion_verification import OFFLINE_ENV, counts_as_failure, settings

        before = {origin: {json.dumps(receipt.get("argv")): receipt for receipt in (node.get(key) or {}).get("receipts", [])}
                  for origin, key in (("plan", "_plan_before"), ("authored", "_authored_before"))}
        authored = [(command, "authored") for command in _as_list(acceptance.get("checks"))]
        planned = [(command, "plan") for command in node.get("_plan_checks") or []]
        with_before = _authored_before_checks(node)
        if node.get("_plan_checks_rejected"):
            result["verification_rejected"] = node["_plan_checks_rejected"]
        for index, (command, origin) in enumerate(authored + planned):
            extra: dict[str, Any] = {}
            vacuous = None
            if origin == "plan" or command in with_before:
                baseline = before[origin].get(json.dumps(_check_argv(command)))
                # Vacuous: it already passed before the change. Known only
                # when a baseline ran; a baseline that could not run is unknown.
                vacuous = {"passed": True, "failed": False}.get((baseline or {}).get("status"))
                extra = {"annotations": {
                    "origin": origin, "vacuous": vacuous,
                    "before": {"status": baseline.get("status"), "exit_code": baseline.get("exit_code"),
                               "receipt": (baseline.get("artifacts") or {}).get("receipt")} if baseline else None}}
                if origin == "plan":
                    extra.update(env=OFFLINE_ENV, timeout=settings(self.config)["timeout_seconds"])
            try:
                receipt = self._acceptance_check(node, result, command, index, **extra)
            except OSError as exc:
                problem("check_unpersisted", f"acceptance check evidence could not be persisted: {exc}", origin=origin)
                continue
            result["acceptance_checks"].append(receipt)
            if receipt["status"] != "passed":
                argv = _check_argv(command)
                label = " ".join(argv) if isinstance(argv, list) and all(isinstance(part, str) for part in argv) else str(command)
                detail = {"origin": origin, "check_index": index, "status": receipt["status"], "vacuous": vacuous,
                          "exit_code": receipt.get("exit_code"),
                          "targeted": origin == "authored" and argv in [_check_argv(c) for c in acceptance.get("fail_to_pass") or []],
                          "test_failure": receipt["status"] == "failed" and isinstance(argv, list)
                          and counts_as_failure(argv, receipt.get("exit_code"))}
                if receipt.get("problem_code"):
                    problem(receipt["problem_code"], receipt["error"], **detail)
                elif receipt["error"]:
                    problem("check_error", f"acceptance check could not run: {label} ({receipt['error']})", **detail)
                else:
                    problem("check_failed", f"acceptance check failed: {label}", **detail)
        if unchanged:
            problem("write_no_change", "write node finished without changing any file",
                    attempt=int(result.get("attempt") or node.get("attempts") or 1))
        return not problems, problems, codes

    def _run_node(self, node_id: str, attempt: int) -> dict[str, Any]:
        node = self.nodes[node_id]
        route = node.get("route")
        agent = node["agent"]
        write = bool(node.get("write", False))
        settings = {key: node[key] for key in (
            "command", "model", "reasoning_effort", "allow_native_delegation", "model_selector", "profile", "launcher_args",
            "max_budget_usd", "permission_mode", "permission_prompts", "allowed_tools",
            "allow_untested",
        ) if key in node}
        task = core.make_task(
            self.workspace,
            agent,
            self._prompt(node) + (f"\nWork in this dedicated worktree. The starting Git commit is {self.git_context['base_sha']}. "
                                  "Inspect the full diff against that commit, including newly created files. "
                                  + ("Do not commit or push; Fusion publishes the reviewed changes after completion."
                                     if self.git_context.get("mode") in {"manual", "auto"} else "Do not commit or push.")
                                  if self.git_context else ""),
            node["role"],
            ["complete the assigned node", "return evidence in the required handoff format"],
            ["do not broaden the workflow task", "do not run parallel writers in this workspace"],
            f"workflow:{self.run_id}:{node_id}" + (f":{agent}:{route or 'native'}:{settings.get('model', '')}" if agent != "auto" else ""),
            (bool(node.get("resume", False)) or attempt > 1) and core.failure_class(node.get("result") or {}) != "permission_denied",
            write,
            parent_task_id=self.run_id,
            route=route,
            settings_overrides=settings,
        )
        if node.get("_plan_checks"):
            # The coordinator runs these after the worker finishes. A worker that
            # cannot run them reports its own work unverified, so let it run
            # exactly these vetted commands (fusion_verification) and nothing else.
            task["verification_argv"] = [list(check) for check in node["_plan_checks"]]
            task["task"] += ("\nAfter you finish, the coordinator runs these checks: "
                             + "; ".join(shlex.join(check) for check in task["verification_argv"])
                             + ". You may run exactly these commands yourself.")
        if node.get("verification_argv"):
            # Authored commands the worker may run itself (the gym's decomp
            # checker); the coordinator does not run them.
            authored = [list(argv) for argv in node["verification_argv"]]
            task["verification_argv"] = [*(task.get("verification_argv") or []), *authored]
            task["task"] += ("\nYou may run exactly these commands yourself: "
                             + "; ".join(shlex.join(argv) for argv in authored) + ".")
        store = self.store
        task["progress_label"] = node_id
        task["node_task"] = node["task"]
        store.write_json(self._node_dir(node_id) / "active.json", {"run_id": task["run_id"], "attempt": attempt})
        task["excluded_routes"] = list(node.get("excluded_routes", []))
        task["decision_context"] = {"request": node.get("decision_context", node["task"]),
                                    "dependencies": [{"status": self.nodes[dep]["status"],
                                                      "changed": (self.nodes[dep].get("result") or {}).get("changed", []),
                                                      "blockers": (self.nodes[dep].get("result") or {}).get("blockers", [])}
                                                     for dep in node["needs"]]}
        if node.get("independent_of"):
            prior = (self.nodes[node["independent_of"]].get("result") or {})
            task["prefer_different_agent"] = prior.get("agent")
        if self.spec["budget_usd"]:
            task["budget_remaining_usd"] = max(0, self.spec["budget_usd"] - self._spent())
        phase, result = "worker_dispatch", None
        try:
            review_tree = None
            if self.git_context and not write and "review" in node["role"].lower():
                from fusion_publish import snapshot
                phase = "snapshot_before_review"
                review_tree = snapshot(self.workspace)
            phase = "worker_dispatch"
            result = core.dispatch(self.config, task, store)
            if review_tree:
                phase = "snapshot_after_review"
                if snapshot(self.workspace) != review_tree:
                    result.update(status="error", blockers=[*result.get("blockers", []), "Files changed during review; review a stable tree before publishing"])
                else:
                    result["reviewed_tree"] = review_tree
        except (OSError, ValueError, RuntimeError) as exc:
            if phase == "snapshot_after_review" and result:
                result.update(status="error", failure_phase=phase,
                              summary="Review finished, but Fusion could not verify its Git snapshot",
                              blockers=[*result.get("blockers", []), str(exc)])
            else:
                result = {
                    "schema": core.SCHEMA,
                    "run_id": task["run_id"],
                    "status": "error",
                    "agent": agent,
                    "role": node["role"],
                    "route": route,
                    "summary": "Review did not start: Fusion could not snapshot the repository" if phase == "snapshot_before_review" else "workflow node could not be dispatched",
                    "failure_phase": phase,
                    "changed": [],
                    "tests": [],
                    "blockers": [str(exc)],
                    "exit_code": 126,
                    "duration_ms": 0,
                    "usage": {},
                    "artifacts": {},
                }
        result["workspace"] = str(self.workspace.resolve())
        result["workflow_id"] = self.run_id
        result["node_id"] = node_id
        result["attempt"] = attempt
        return {"task": task, "result": result}

    def _record_gate(self, task: dict[str, Any], result: dict[str, Any], accepted: bool, problems: list[str]) -> None:
        """The worker span is written inside dispatch(), before the gate runs, so it
        only carries the worker's own claim. One gate span per acceptance decision
        lets `usage` and remote telemetry count accepted vs rejected nodes. It
        shares the receipt's run_id so resume never recovers it as a second call."""
        run_id = result.get("run_id") or task.get("run_id")
        if not run_id:
            return
        now = core.now_ms()
        self.store.trace_span(
            self.config, {**task, "run_id": run_id, "agent": "gate", "route": None},
            {"status": "success" if accepted else "failed", "blockers": list(problems), "usage": {},
             "check_inputs_changed": result.get("check_inputs_changed", [])},
            now, now, {},
        )

    def _save_node(self, node_id: str, payload: dict[str, Any]) -> None:
        path = self._node_dir(node_id) / "node.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(core.json_text(payload) + "\n", encoding="utf-8")
        temp.replace(path)

    def _spent(self) -> float:
        return sum(item["cost_usd"] for item in self.attempt_ledger)

    def _ready(self, node: dict[str, Any]) -> bool:
        return node["status"] == "pending" and all(self.nodes[dependency]["status"] == "success" for dependency in node["needs"])

    def _repair_review_dependency(self, node: dict[str, Any], result: dict[str, Any],
                                  blockers: list[str], codes: list[dict[str, Any]]) -> bool:
        """Redirect an ordinary repair to a writer once, using worker evidence only."""
        if node["write"] or "review" not in node["role"].lower() or node.get("review_repair") or not blockers:
            return False
        failure = result.get("failure_class") or core.failure_class(result)
        # A blocked handoff has verdict=error/failure_class=worker_error even
        # when the CLI completed normally. Provider/transport errors must not
        # be mistaken for that substantive review outcome.
        if (result.get("status") not in {"success", "cache_hit", "partial", "blocked"}
                or failure not in {None, "worker_error"}
                or result.get("verdict") not in {None, "ok", "error"}
                or (result.get("verdict") == "error" and result.get("status") != "blocked")
                or result.get("provider_failure") or result.get("exit_code") not in {None, 0}):
            return False
        gate_codes = {item["code"] for item in codes}
        if "worker_blockers" not in gate_codes or gate_codes - {
                "worker_status", "worker_blockers", "check_failed",
                "required_file_missing", "required_file_unchanged"}:
            return False
        if node["attempts"] >= self.spec["max_attempts"]:
            return False
        # independent_of identifies the intended writer; otherwise use the
        # first write dependency in authored needs order, without falling
        # through to unrelated writers when that target is exhausted.
        target = node.get("independent_of")
        if not target or not self.nodes[target]["write"]:
            target = next((dep for dep in node["needs"] if self.nodes[dep]["write"]), None)
        if target is None:
            return False
        writer = self.nodes[target]
        if (writer["status"] != "success" or writer.get("repair_feedback")
                or writer["attempts"] >= self.spec["max_attempts"]):
            return False
        downstream = {target}
        for node_id in self._topological_order():
            if any(dep in downstream for dep in self.nodes[node_id]["needs"]):
                downstream.add(node_id)
        downstream.difference_update({target, node["id"]})
        if any(self.nodes[node_id]["status"] == "running" for node_id in downstream):
            return False
        for node_id in self.nodes:
            dependent = self.nodes[node_id]
            if node_id in downstream and dependent["status"] == "success":
                dependent["status"] = "pending"
                dependent.pop("_contract", None)
                self._event("node.stale", {"node_id": node_id, "reason": "dependency repaired by review"})
        feedback = {"review_node_id": node["id"], "review_attempt": node["attempts"],
                    "review_run_id": result.get("run_id"), "writer_node_id": target,
                    "writer_attempt": writer["attempts"] + 1, "blockers": blockers}
        node["review_repair"] = feedback
        writer["repair_feedback"] = feedback
        writer["status"] = "pending"
        # Attempts, routes, receipts and node baselines survive reopening.
        self._event("node.repair_requested", feedback)
        return True

    def _block_unrunnable(self) -> None:
        changed = True
        while changed:
            changed = False
            for node in self.nodes.values():
                if node["status"] != "pending":
                    continue
                lane = self.lane_health.get(self._lane_key(node["agent"], node.get("route")))
                if lane:
                    status = "paused_quota" if lane["status"] == "cooldown" else "blocked"
                    node["status"] = status
                    node["result"] = {
                        "status": status,
                        "summary": f"agent lane {node['agent']} is {lane['status']}",
                        "blockers": [lane["reason"]],
                    }
                    self._save_node(node["id"], {
                        "task": {},
                        "result": node["result"],
                        "acceptance": {"ok": False, "problems": node["result"]["blockers"]},
                    })
                    self._event(f"node.{status}", {"node_id": node["id"], "result": node["result"]})
                    changed = True
                    continue
                dependency_statuses = [self.nodes[dependency]["status"] for dependency in node["needs"]]
                if any(status in TERMINAL_FAILURE or status in TERMINAL_PAUSED for status in dependency_statuses):
                    # Wait until every dependency is terminal before taking
                    # the blocker snapshot. This keeps a downstream receipt
                    # from saying one sibling is still running when another
                    # sibling already made the fan-in impossible.
                    if any(status in {"pending", "running"} for status in dependency_statuses):
                        continue
                    node["status"] = "blocked"
                    node["result"] = {
                        "status": "blocked",
                        "summary": "dependency did not reach an accepted success state",
                        "blockers": [
                            f"{dependency}: {self.nodes[dependency]['status']}"
                            for dependency in node["needs"]
                            if self.nodes[dependency]["status"] != "success"
                        ],
                    }
                    self._save_node(node["id"], {
                        "task": {},
                        "result": node["result"],
                        "acceptance": {"ok": False, "problems": node["result"]["blockers"]},
                    })
                    self._event("node.blocked", {"node_id": node["id"], "result": node["result"]})
                    changed = True

    def _final_status(self) -> str:
        statuses = {node["status"] for node in self.nodes.values()}
        if "paused_quota" in statuses:
            return "paused_quota"
        if "paused_budget" in statuses:
            return "paused_budget"
        if statuses & TERMINAL_FAILURE:
            return "failed"
        if statuses == {"success"}:
            return "success" if self._accept_workflow()[0] else "failed"
        return "running"

    def _accept_workflow(self) -> tuple[bool, list[str]]:
        problems: list[str] = []
        acceptance = self.spec.get("acceptance") or {}
        for relative in acceptance.get("required_files", []):
            path = self.workspace / relative
            if not path.is_file():
                problems.append(f"required workflow artifact is missing: {relative}")
            elif relative in self.workflow_baseline and _fingerprint(path) == self.workflow_baseline[relative]:
                problems.append(f"required workflow artifact did not change during run: {relative}")
        for node_id in _as_list(acceptance.get("required_nodes")):
            if node_id not in self.nodes:
                problems.append(f"required workflow node is unknown: {node_id}")
            elif self.nodes[node_id]["status"] != "success":
                problems.append(f"required workflow node is not successful: {node_id}")
        return not problems, problems

    def run(self) -> dict[str, Any]:
        try:
            return self._execute()
        except KeyboardInterrupt:
            for node in self.nodes.values():
                if node["status"] == "running":
                    node["status"] = "blocked"
                    node["result"] = {"status": "blocked", "summary": "interrupted by user", "blockers": ["workflow interrupted; inspect partial worker logs before resuming"]}
            self._write_manifest("interrupted")
            self._event("workflow.interrupted", {})
            raise

    def _execute(self) -> dict[str, Any]:
        self._event("workflow.started", {"max_parallel": self.spec["max_parallel"]})
        max_parallel = self.spec["max_parallel"]
        max_writers = self.spec["max_parallel_writers"]
        active: dict[Future[dict[str, Any]], tuple[str, bool]] = {}
        active_writers = 0
        with ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="fusion") as executor:
            while True:
                self._block_unrunnable()
                if not active:
                    ready = [node for node in self.nodes.values() if self._ready(node)]
                    if not ready:
                        break
                while len(active) < max_parallel:
                    ready = [node for node in self.nodes.values() if self._ready(node)]
                    selected = None
                    for node in ready:
                        if node["write"] and active_writers >= max_writers:
                            continue
                        selected = node
                        break
                    if selected is None:
                        break
                    if self.spec["budget_usd"] and self._spent() >= self.spec["budget_usd"]:
                        for node in ready:
                            node["status"] = "paused_budget"
                            node["result"] = {
                                "status": "paused_budget",
                                "summary": "workflow budget reached before dispatch",
                                "blockers": [f"budget_usd={self.spec['budget_usd']}"]
                            }
                            self._event("node.paused_budget", {"node_id": node["id"]})
                        break
                    selected["status"] = "running"
                    selected["attempts"] += 1
                    # A planning node can declare what the implementation must
                    # produce. Apply it before the baseline is taken, so the
                    # artifacts it names are actually gated on this node.
                    inherited = self._inherited_required_files(selected)
                    if inherited:
                        selected["required_files"] = sorted(
                            dict.fromkeys(list(selected.get("required_files") or []) + inherited)
                        )
                    # Baselines belong to the node, not the attempt: a retry
                    # that finds its predecessor's work already done changed
                    # the tree relative to where the node started.
                    first_attempt = selected["attempts"] == 1 or "_artifact_baseline" not in selected
                    if first_attempt:
                        selected["_artifact_baseline"] = {
                            relative: _fingerprint(self.workspace / relative)
                            for relative in selected.get("required_files", [])
                        }
                    if selected["write"]:
                        # The plan's verification becomes this node's checks;
                        # their pre-change run must precede the tree baseline
                        # so its caches never count as the worker's change.
                        self._prepare_plan_checks(selected)
                        self._pin_check_inputs(selected)
                        self._baseline_plan_checks(selected)
                        self._baseline_authored_checks(selected)
                    # A writer that writes nothing did not do the work. Record
                    # the tree so acceptance can check the repository itself
                    # rather than the worker's account of it.
                    if first_attempt or "_tree_baseline" not in selected:
                        selected["_tree_baseline"] = self._tree() if selected["write"] else None
                    attempt = selected["attempts"]
                    writer = bool(selected["write"])
                    if writer:
                        active_writers += 1
                    self._event("node.started", {"node_id": selected["id"], "attempt": attempt})
                    future = executor.submit(self._run_node, selected["id"], attempt)
                    active[future] = (selected["id"], writer)
                    self._write_manifest("running")
                if not active:
                    break
                completed, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in completed:
                    node_id, writer = active.pop(future)
                    if writer:
                        active_writers -= 1
                    try:
                        payload = future.result()
                    except Exception as exc:  # pragma: no cover - defensive worker boundary
                        payload = {"task": {}, "result": {"status": "error", "summary": "worker thread failed", "blockers": [str(exc)]}}
                    node = self.nodes[node_id]
                    result = payload.get("result") or {}
                    worker_blockers = list(result.get("blockers") or [])
                    provenance = {}
                    if node.get("repair_feedback"):
                        provenance["repair"] = node["repair_feedback"]
                    if node.get("review_repair"):
                        provenance["re_review"] = node["review_repair"]
                    self.attempt_ledger.append({"run_id": result.get("run_id"), "node_id": node_id,
                                                "attempt": node["attempts"], "cost_usd": _result_cost(result),
                                                "usage": result.get("usage", {}), **provenance})
                    from fusion_labeling import gate_label, record_gate_input
                    with progress.activity(node_id, "checking handoff and acceptance criteria"):
                        accepted, problems, codes = self._gate(node, result)
                    # Include executed receipts for passing and failing gates.
                    # Save the same evidence used by artifact-based input rebuilding.
                    directory = core.run_directory(self.control_workspace, result.get("run_id"))
                    if directory:
                        self.store.write_json(directory / "result.json", result)
                    gate_input = record_gate_input(self.config, self.control_workspace, self.run_id, node, result)
                    laya_veto = False
                    if accepted:
                        # A semantic Done-check leg, spent only on a node that already passed
                        # every structural check. It can add a problem; it cannot clear one.
                        from fusion_policy import accept_node
                        plausible, acceptance_decision_id = accept_node(self.config, self.control_workspace, self.run_id, node, result)
                        result.setdefault("decisions", {})["acceptance"] = acceptance_decision_id
                        if not plausible:
                            accepted, laya_veto = False, True
                            problems = problems + ["Laya acceptance check: reported success does not plausibly match the task"]
                    if gate_input:
                        # Only objective gate codes label; the veto above never does.
                        result["gate_label"] = gate_label(self.config, self.control_workspace, gate_input, codes,
                                                          result.get("acceptance_checks", []), result.get("check_inputs_changed"))
                    if problems:
                        result.setdefault("blockers", []).extend(problems)
                    previous = node.get("result") or {}
                    if (not accepted and node["attempts"] > 1 and previous.get("blockers")
                            and previous.get("blockers") == result.get("blockers")):
                        # Same failure twice is a stuck loop, decided here from the
                        # receipts rather than asked of a classifier.
                        node["repeated_failure"] = True
                        problems = problems + ["attempt repeated the previous attempt's blockers exactly; stopping instead of retrying"]
                        result["blockers"].append(problems[-1])
                        codes.append({"code": "repeated_failure"})
                    if accepted:
                        node["_contract"] = self._contract_from(result)
                        dependency_digests = {dep: (self.nodes[dep].get("result") or {}).get("digest") for dep in node["needs"]}
                        result["digest"] = self._input_digest(self._definition_digest(node), dependency_digests)
                        result["resolved"] = payload.get("task", {}).get("resolved") or {}
                    node["result"] = result
                    from fusion_policy import recovery
                    action, decision_id = recovery(self.config, self.control_workspace, self.run_id, node, result, accepted,
                                                   self.spec["max_attempts"], gate_codes=codes, laya_veto=laya_veto)
                    result.setdefault("decisions", {})["recovery"] = decision_id
                    self._record_gate(payload.get("task") or {}, result, accepted, problems)
                    payload["acceptance"] = {"ok": accepted, "problems": problems,
                                             "checks": result.get("acceptance_checks", [])}
                    self._save_node(node_id, payload)
                    if accepted:
                        node["status"] = "success"
                        self._event("node.succeeded", {"node_id": node_id, "attempt": node["attempts"]})
                    elif action == "switch":
                        node["status"] = "pending"
                        self._event("node.switching", {"node_id": node_id, "excluded_routes": node["excluded_routes"],
                                                       "from_route": result.get("route") or result.get("agent"), "reason": core.failure_class(result)})
                    elif core.failure_class(result) == "quota":
                        node["status"] = "paused_quota"
                        self._event("node.paused_quota", {"node_id": node_id, "attempt": node["attempts"]})
                        if node["agent"] != "auto":
                            self._set_lane(self._lane_key(node["agent"], node.get("route")), "cooldown",
                                           f"node {node_id} reported a quota/session limit")
                    elif action == "ask":
                        node["status"] = "blocked"
                        self._event("node.needs_input", {"node_id": node_id, "problems": problems})
                    elif action == "repair" and node["attempts"] < self.spec["max_attempts"]:
                        node["status"] = "pending"
                        if not self._repair_review_dependency(node, result, worker_blockers, codes):
                            self._event("node.retrying", {"node_id": node_id, "attempt": node["attempts"], "problems": problems})
                    else:
                        node["status"] = "invalid" if problems and result.get("status") == "success" else "failed"
                        self._event("node.failed", {"node_id": node_id, "attempt": node["attempts"], "problems": problems})
                    self._write_manifest("running")

        self._block_unrunnable()
        status = self._final_status()
        acceptance_ok, acceptance_problems = self._accept_workflow()
        if status == "success" and not acceptance_ok:
            status = "failed"
        if acceptance_problems:
            self._event("workflow.acceptance_failed", {"problems": acceptance_problems})
        self._write_manifest(status, "; ".join(acceptance_problems) if acceptance_problems else None)
        self._event("workflow.finished", {"status": status, "spent_usd": self._spent()})
        if status == "success" and self.spec.get("publish", {}).get("mode") == "auto":
            from fusion_publish import auto_publish
            auto_publish(self.workspace if self.store.control_workspace else self.control_workspace, self.run_id, self.config)
        return self.result(status, acceptance_problems)

    def result(self, status: str | None = None, acceptance_problems: list[str] | None = None) -> dict[str, Any]:
        from fusion_publish import public_status
        return {
            "schema": WORKFLOW_SCHEMA,
            "workflow_id": self.run_id,
            "publication": public_status(self.control_workspace, self.run_id),
            "status": status or self._final_status(),
            "task": self.spec.get("task", ""),
            "spent_usd": self._spent(),
            "attempt_ledger": self.attempt_ledger,
            "nodes": [self.nodes[node_id] for node_id in self.nodes],
            "lanes": self.lane_health,
            "acceptance": {
                "ok": status == "success" and not acceptance_problems,
                "problems": acceptance_problems or [],
            },
            "artifacts": {
                "root": str(self.root),
                "manifest": str(self.manifest_path),
                "events": str(self.events_path),
            },
        }


def run_workflow(workspace: Path, config: dict[str, Any], spec_path: Path, task: str | None = None) -> dict[str, Any]:
    spec = load_spec(spec_path)
    if task:
        spec["task"] = task
    return WorkflowRunner(workspace, config, spec).run()


def reroute_resume_spec(manifest: dict[str, Any], config: dict[str, Any], node_id: str | None = None,
                       agent: str | None = None, route: str | None = None, max_attempts: int | None = None) -> dict[str, Any]:
    spec = copy.deepcopy(manifest.get("spec") or {})
    if agent is not None or route is not None:
        if not node_id:
            raise ValueError("Choose a stage to change its worker")
    if node_id:
        saved = manifest.get("nodes", {}).get(node_id)
        if not saved or saved.get("status") == "success":
            raise ValueError("Choose an unfinished stage; accepted stages are preserved")
        spec["nodes"] = copy.deepcopy(spec["graph"]["nodes"])
        for node in spec["nodes"]:
            for key in ("task_template", "items", "map"):
                node.pop(key, None)
            if node["id"] != node_id:
                continue
            if agent is not None or route is not None:
                for key in ("route", "command", "model", "model_selector", "profile", "launcher_args",
                            "max_budget_usd", "permission_mode", "permission_prompts", "allowed_tools", "allow_untested"):
                    node.pop(key, None)
                node["agent"] = agent or "auto"
                if route:
                    lane = config.get("routes", {}).get(route)
                    if not lane or (agent not in {None, "auto", lane.get("agent")}):
                        raise ValueError("Choose a configured route matching the selected worker")
                    node["route"] = route
                    node["agent"] = lane["agent"]
        limit = max_attempts if max_attempts is not None else spec.get("max_attempts", 1)
        if limit <= saved.get("attempts", 0):
            raise ValueError("Raise the attempt limit to permit another attempt for this stage")
    if max_attempts is not None:
        if not 1 <= max_attempts <= 100:
            raise ValueError("Attempt limit must be between 1 and 100")
        spec["max_attempts"] = max_attempts
    return validate_spec(spec)


def resume_workflow(workspace: Path, config: dict[str, Any], run_id: str, spec_path: Path | None = None,
                    node_id: str | None = None, agent: str | None = None, route: str | None = None,
                    max_attempts: int | None = None) -> dict[str, Any]:
    store = core.RunStore(workspace)
    manifest_path = store.root / "workflows" / run_id / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read workflow run {run_id}: {exc}") from exc
    # A resume normally replays the persisted spec unchanged. Passing spec_path
    # lets a caller resume with an edited workflow.json; the digest check in
    # _invalidate_stale_receipts() then reruns only the nodes whose definition
    # or dependency evidence actually changed, not the whole graph.
    if spec_path and any(value is not None for value in (node_id, agent, route, max_attempts)):
        raise ValueError("Use either --spec or stage retry options")
    control = store.control_workspace
    if manifest.get("control_workspace") and manifest.get("workspace"):
        worker = Path(manifest["workspace"])
        control = store.workspace
        if worker != workspace:
            config, _ = core.load_config(worker, control)
        workspace = worker
    spec = load_spec(spec_path) if spec_path else reroute_resume_spec(manifest, config, node_id, agent, route, max_attempts)
    return WorkflowRunner(workspace, config, spec, run_id=run_id, resume=True, control_workspace=control).run()


def workflow_status(workspace: Path, run_id: str) -> dict[str, Any]:
    path = core.RunStore(workspace).root / "workflows" / run_id / "manifest.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read workflow run {run_id}: {exc}") from exc


def effective_status(manifest: dict[str, Any]) -> str | None:
    """A manifest reads "running" until the coordinator writes again, so one
    that was killed says "running" forever -- `workflow watch` never exits and
    `report` offers no way forward. The coordinator's pid is recorded on every
    flush, so a dead pid means interrupted, not running."""
    status = manifest.get("status")
    if status != "running":
        return status
    return "interrupted" if core.process_alive(manifest.get("coordinator_pid")) is False else status


def workflow_report(workspace: Path, run_id: str) -> dict[str, Any]:
    """One combined view of a workflow run: waves, lanes, usage, and blockers.

    Assessing the Saloon run required manually joining `workflow status`,
    `trace`, `usage`, and events by hand. This reconstructs that same picture
    from the persisted manifest and the trace ledger in one read-only call.
    """
    from fusion_report import command, findings, read_answer, reported_cost
    from fusion_publish import public_status
    workspace = core.RunStore(workspace).workspace
    manifest = workflow_status(workspace, run_id)
    nodes = manifest.get("nodes") or {}
    spec_nodes = {node["id"]: node for node in (manifest.get("spec", {}).get("graph", {}).get("nodes") or [])}

    wave_cache: dict[str, int] = {}

    def wave_of(node_id: str) -> int:
        if node_id in wave_cache:
            return wave_cache[node_id]
        wave_cache[node_id] = 0  # guard against a spec that slipped a cycle past validation
        needs = spec_nodes.get(node_id, {}).get("needs") or []
        depth = 0 if not needs else 1 + max((wave_of(dep) for dep in needs), default=-1)
        wave_cache[node_id] = depth
        return depth

    from fusion_decisions import DecisionStore
    decision_store = DecisionStore(workspace)
    waves: dict[int, list[dict[str, Any]]] = {}
    blockers: list[dict[str, Any]] = []
    outputs = []
    for node_id, node in nodes.items():
        result = node.get("result") or {}
        digest = result.get("digest")
        waves.setdefault(wave_of(node_id), []).append({
            "id": node_id,
            "agent": result.get("agent") or node.get("agent"),
            "requested_agent": node.get("agent"),
            "status": node.get("status"),
            "attempts": node.get("attempts"),
            "summary": result.get("summary"),
            "duration_ms": result.get("duration_ms"),
            "execution_choice": result.get("execution_choice"),
            "changed": result.get("changed", []),
            "tests": result.get("tests", []),
            "acceptance_warnings": node.get("acceptance_warnings", []),
            "check_inputs_changed": result.get("check_inputs_changed", []),
            "digest": digest[:12] if digest else None,
            "decisions": decision_store.summaries(result.get("decisions")) if result.get("decisions") else {},
        })
        if result and (node.get("attempts", 0) or result.get("run_id")):
            answer = read_answer(workspace, result)
            outputs.append({"node_id": node_id, "status": node.get("status"), "agent": result.get("agent") or node.get("agent"),
                            **answer, "findings": findings(answer["text"])})
        if node.get("status") not in {"success", "pending", "running"}:
            for blocker in result.get("blockers") or []:
                blockers.append({"node_id": node_id, "status": node.get("status"), "blocker": blocker})

    spans = [span for span in core.RunStore(workspace).traces(limit=10000) if span.get("trace_id") == run_id]
    receipts = [node["result"] for node in nodes.values() if node.get("result") and node.get("attempts")]
    cost_records = manifest.get("attempt_ledger") or spans or receipts
    dependencies = {dependency for node in spec_nodes.values() for dependency in node.get("needs", [])}
    primary_nodes = [item["node_id"] for item in outputs if item["node_id"] not in dependencies]
    if not primary_nodes and outputs:
        latest_wave = max(wave_of(item["node_id"]) for item in outputs)
        primary_nodes = [item["node_id"] for item in outputs if wave_of(item["node_id"]) == latest_wave]
    status = effective_status(manifest)
    error = manifest.get("error")
    return {
        "schema": "fusion.workflow.report.v1",
        "workflow_id": run_id,
        "workspace": str(workspace),
        "git": manifest.get("git") or {},
        "publication": public_status(workspace, run_id),
        "status": status,
        "task": manifest.get("task"),
        "spent_usd": manifest.get("spent_usd", sum(_result_cost(node.get("result") or {}) for node in nodes.values())),
        "budget_usd": (manifest.get("spec") or {}).get("budget_usd") or 0,
        "waves": [{"wave": wave, "nodes": waves[wave]} for wave in sorted(waves)],
        "attempt_ledger": manifest.get("attempt_ledger", []),
        "node_ids": list(nodes),
        "outputs": outputs,
        "primary_nodes": primary_nodes,
        "read_only": all(not node.get("write") for node in nodes.values()),
        "cost": {"calls": len(cost_records), "reported_calls": sum(reported_cost(item.get("usage")) is not None for item in cost_records)},
        "lanes": manifest.get("lanes") or {},
        "usage": core.usage_summary(spans or receipts),
        "blockers": blockers,
        "acceptance_problems": [item for item in (error or "").split("; ") if item],
        "artifacts": manifest.get("artifacts") or {},
        "resume_command": (
            command(workspace, "--progress", "workflow", "resume", run_id)
            if status in {"paused_quota", "paused_budget", "interrupted", "failed"}
            else None
        ),
    }
