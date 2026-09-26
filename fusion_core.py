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
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Iterator

from fusion_decisions import DEFAULTS as DECISION_DEFAULTS
import fusion_progress as progress
from fusion_reasoning import EFFORTS, validate_pair


SCHEMA = "fusion.v1"
TELEMETRY_SCHEMA = "fusion.telemetry.v1"
DEFAULTS: dict[str, Any] = {
    "publish": {"mode": "off", "base": "staging", "remote": "origin", "draft": True},
    "execution_mode": "restricted",
    "decisions": DECISION_DEFAULTS,
    "lead": "claude",
    "sidekick": "codex",
    "timeout_seconds": 3600,
    "max_result_chars": 12000,
    "telemetry": {
        "enabled": True,
        "include_content": False,
        # On by default so a collaborator gets aggregate usage without
        # configuring anything. The payload is reduced before it leaves the
        # machine (see send_remote_telemetry) and the first send announces
        # itself. FUSION_TELEMETRY=0 or remote.enabled false stops the send
        # and keeps the local trace; telemetry.enabled false stops both. The
        # collector needs no token to accept a span -- publishing one in a
        # public repo would only be theatre -- so this stays empty; a token
        # here is sent if present, and is what read endpoints require.
        "remote": {
            "enabled": True,
            "endpoint": "https://orc-telemetry.fly.dev/v1/ingest",
            "token": "",
        },
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
        "git_write": True,
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
    "agy": {
        "command": "agy",
        "mode": "",
        "model": "",
        "sandbox": True,
    },
    "grok": {
        "command": "grok",
        "output_format": "streaming-json",
        "permission_mode": "plan",
        "model": "",
    },
}

AGY_MODE_ALIASES = {
    "": "",
    "default": "accept-edits",
    "acceptEdits": "accept-edits",
    "accept-edits": "accept-edits",
    "plan": "plan",
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
    global_path = Path(os.environ.get("ORC_HOME") or Path.home() / ".config/orc") / "fusion.json"
    merged, source = deep_merge({}, DEFAULTS), None
    for config_path in dict.fromkeys([global_path, path]):
        if config_path is None or (config_path == global_path and not config_path.is_file()):
            continue
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"fusion: cannot read {config_path}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SystemExit(f"fusion: {config_path} must contain a JSON object")
        merged, source = deep_merge(merged, parsed), config_path
        # Stage maps are complete workflow definitions, not additive defaults.
        if isinstance(parsed.get("ultra"), dict) and "stages" in parsed["ultra"]:
            merged.setdefault("ultra", {})["stages"] = parsed["ultra"]["stages"]
    execution_mode(merged)
    return merged, source


def execution_mode(config: dict[str, Any]) -> str:
    mode = config.get("execution_mode", "restricted")
    if not isinstance(mode, str) or mode not in {"restricted", "yolo"}:
        raise ValueError("execution_mode must be restricted or yolo")
    return mode


def now_ms() -> int:
    return int(time.time() * 1000)


def compact(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 80)] + "\n...[truncated by fusion]..."


_SENTENCE_BREAK = re.compile(r"[.!?](?:\s|$)")


def _reads_as_prose(value: str) -> bool:
    """Does this run to full sentences rather than name a list of things?

    A list item is a path, a command, a short phrase; it does not end a
    sentence. So a value carrying sentence-ending punctuation followed by
    whitespace or the end of the string is prose. `src/a.py, src/b.py` is not
    - the period there is inside a filename, not closing a sentence.
    """
    return bool(_SENTENCE_BREAK.search(value))


NONE_ANSWERS = {"none", "n/a", "na", "nil", "nothing", "-", "—"}


def parse_handoff(text: str) -> dict[str, Any]:
    """Extract the small handoff contract from a worker's final message."""
    blocks: dict[str, list[str]] = {}
    # Models decorate labels: **STATUS:** success, **STATUS**: success, ## STATUS: success.
    label_pattern = re.compile(
        r"^(?:#{1,6}\s+)?([*_]{0,3})(STATUS|SUMMARY|CHANGED|TESTS|BLOCKERS)\1?\s*:\s*\1?\s*(.*)$", re.I)
    active: str | None = None
    fence: str | None = None
    handoff_fence = False
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        marker = re.match(r"^(`{3,}|~{3,})(.*)$", stripped)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                if not handoff_fence and active:
                    blocks[active].append(line)
                fence = None
                if handoff_fence:
                    active = None
                handoff_fence = False
                continue
            if not handoff_fence:
                if active:
                    blocks[active].append(line)
                continue
        elif marker:
            fence = marker[1]
            language = marker[2].strip().lower()
            following = next((value.strip() for value in lines[index + 1:] if value.strip()), "")
            # A whole handoff may be wrapped in a text fence. Other fenced
            # examples cannot introduce/override labels. The planner's separate
            # contract is not a continuation of its final BLOCKERS: none field.
            handoff_fence = not blocks and language in {"", "text", "plaintext", "markdown"} and bool(label_pattern.match(following))
            if language == "acceptance-contract":
                active = None
            elif not handoff_fence and active:
                blocks[active].append(line)
            continue
        matched = label_pattern.match(stripped)
        if matched:
            active = matched[2].upper()
            blocks[active] = [matched[3]]
        elif active:
            blocks[active].append(line)
    fields = {label: "\n".join(value).strip() for label, value in blocks.items()}

    def list_field(name: str) -> list[str]:
        value = fields.get(name, "").strip()
        if not value or value.lower() in NONE_ANSWERS:
            return []
        # A blank line is not evidence that a worker finished listing blockers.
        # Remove only complete, familiar signoff paragraphs; arbitrary prose,
        # including an unindented failure after a blank line, remains evidence.
        paragraphs = re.split(r"\n(?:[ \t]*\n)+", value)
        value = "\n\n".join(
            paragraph for index, paragraph in enumerate(paragraphs)
            if index == 0 or paragraph[:1].isspace() or not re.fullmatch(
                r"(?:Let me know if you (?:want|need) anything else|Thanks(?: again)?|Thank you|Done)[.!]?",
                paragraph.strip(), re.I,
            )
        ).strip()
        values: list[str] = []
        for item in re.split(r"\n(?=[ \t]*(?:[-*+]|\d+[.)])\s+)", value):
            item = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", item.strip()).strip()
            if not item:
                continue
            first, _, continuation = item.partition("\n")
            # A leading none may explain a workaround. Keep explicit failure
            # signals and all later lines. This vocabulary is a conservative
            # normalization heuristic, not proof of completion or recovery;
            # provider denials and coordinator checks remain separate evidence.
            # Filename punctuation (none.py, nil-cache.json) is not a boundary.
            lead = re.split(r"[.;:,—-](?:\s+|$)", first, maxsplit=1)[0].strip().lower()
            # "none for this node's scope" is still none; the qualifier scopes it.
            scoped = re.match(r"(none|nothing|n/a|nil)\s+(?:for|in|within|on|at|outside)\b", lead)
            explanation = first[len(lead):].lstrip(".;:,—- \t") if lead in NONE_ANSWERS or scoped else ""
            if scoped:
                lead = scoped[1]
            # A sentence the worker explicitly marks as not blocking is a note,
            # even when it mentions failures elsewhere; every other sentence counts.
            explanation = " ".join(sentence for sentence in re.split(r"(?<=[.!?])\s+", explanation)
                                   if not re.search(r"\bnot\s+(?:a\s+|an\s+)?block(?:er|ing)\b|\bnon-?blocking\b", sentence, re.I))
            benign_explanation = (
                not explanation
                or not re.search(
                    r"\b(?:denied|fail\w*|block\w*|unable|can['’]t|cannot|could not|"
                    r"error\w*|timed out|still|requires?|pending|incomplete|unreviewed|"
                    r"unverified|unresolved|unavailable|broken|awaiting|outstanding|"
                    r"not\s+(?:yet\s+)?"
                    r"(?:run|verified|reviewed|happened))\b",
                    explanation, re.I,
                )
            )
            discard_none = lead in NONE_ANSWERS and (name != "BLOCKERS" or benign_explanation)
            if discard_none:
                item = continuation.strip()
                if not item:
                    continue
            if item.lower() in NONE_ANSWERS:
                continue
            if _reads_as_prose(item):
                values.append(item)
            else:
                values.extend(part.strip() for part in re.split(r"[,\n]", item) if part.strip())
        return values

    reported_status = fields.get("STATUS", "").split("\n", 1)[0].strip().strip("*_`").strip().lower()
    if reported_status not in {"success", "partial", "blocked", "error"}:
        reported_status = ""
    return {
        "reported_status": reported_status,
        "summary": fields.get("SUMMARY", text.strip()),
        "changed": list_field("CHANGED"),
        "tests": list_field("TESTS"),
        "blockers": list_field("BLOCKERS"),
    }


QUOTA_RE = re.compile(r"(session limit|usage limit|rate limit|quota|too many requests)", re.IGNORECASE)
RESETS_RE = re.compile(r"resets?(?:\s+(?:at|in))?\s+([0-9][0-9:]*\s*(?:am|pm)?[^\n.·]*)", re.IGNORECASE)
PERMISSION_RE = re.compile(r"(permission (?:check|denied|prompt)|not allowed to|requires approval|denied by policy)", re.IGNORECASE)
REFUSAL_RE = re.compile(r"(I can(?:'|no)t help with|I won't|refus(?:e|ed|al))", re.IGNORECASE)


def classify_verdict(status: str, failure: str | None, summary: str, exit_code: int) -> dict[str, Any]:
    """A structural verdict beside the worker's free-text summary: ok | error | quota | refused |
    blocked_by_permissions. A session or rate limit is `quota` with the reset text the CLI printed, so a
    harness can treat it as an unmeasured round instead of a zero score."""
    text = " ".join(part for part in (failure or "", summary or "") if part)
    quota = QUOTA_RE.search(text)
    if quota:
        match = RESETS_RE.search(text)
        return {"verdict": "quota", "reason": quota.group(1).lower(), "resets_at": match.group(1).strip() if match else None}
    permission = PERMISSION_RE.search(text)
    if permission:
        return {"verdict": "blocked_by_permissions", "reason": permission.group(1).lower()}
    if status in {"success", "partial"}:
        return {"verdict": "ok", "reason": status}
    refusal = REFUSAL_RE.search(text)
    if refusal:
        return {"verdict": "refused", "reason": refusal.group(1).lower()}
    if failure and "timeout" in failure.lower():
        return {"verdict": "error", "reason": "timeout"}
    if exit_code == 127:
        return {"verdict": "error", "reason": "missing_binary"}
    return {"verdict": "error", "reason": (failure or status or "error")[:120]}


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


MASK = "••••••••"
SECRET = re.compile(r"token|secret|password|api.?key|authorization", re.I)


def process_alive(pid: Any) -> bool | None:
    """True, False, or None when the answer is unknowable.

    None matters: a missing or unusable pid is not evidence of death, and
    callers should leave the recorded status alone rather than declare a run
    interrupted. 0 is never probed -- `os.kill(0, 0)` signals the caller's own
    process group and always succeeds, so treating it as a pid reports every
    record without one as permanently alive.
    """
    if pid is None:
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # owned by another user, so it exists
    except OSError:
        return False
    return True


def process_matches(pid: int, workflow_id: str) -> bool:
    """Is this pid still the coordinator we recorded, or a stranger wearing it?

    A pid recorded minutes ago can be recycled by the OS onto something else
    entirely, and signalling it would interrupt an unrelated program. Confirm
    the command line still looks like the fusion run we mean before acting.
    Unknowable is treated as a match, so a platform whose `ps` we cannot read
    keeps working rather than silently refusing every cancel.
    """
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    command = (out.stdout or "").strip()
    if not command:
        return False  # ps ran and found nothing: the process is gone.
    if workflow_id and workflow_id in command:
        return True
    return "fusion" in command


def redact(value: Any) -> Any:
    """Mask secret-looking values anywhere in a structure before printing it.

    Anything that dumps configuration to a terminal or a browser goes through
    this: `fusion doctor` is the first thing install.sh tells a new user to
    run, and it prints the merged config."""
    if isinstance(value, dict):
        return {key: MASK if SECRET.search(key) and item else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


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
    # Real `codex exec --json` usage reports cache_write_input_tokens under
    # that name rather than cache_creation_input_tokens (verified against a
    # live turn.completed event after Codex quota reset -- see
    # FUSION_RESEARCH.md). Map it so codex cache-write spend isn't silently
    # dropped from usage/cost aggregation.
    if "cache_creation_input_tokens" not in output and "cache_write_input_tokens" in usage:
        output["cache_creation_input_tokens"] = number(usage["cache_write_input_tokens"])
    return output


def cache_read_ratio(usage: Any) -> float | None:
    """Share of prompt tokens served from cache. Anthropic-style usage reports
    cache reads beside input_tokens; OpenAI-style cached_input_tokens is
    already inside input_tokens."""
    usage = normalized_usage(usage)
    write = usage.get("cache_creation_input_tokens", 0.0)
    if "cache_read_input_tokens" in usage:
        read = usage["cache_read_input_tokens"]
        total = usage.get("input_tokens", 0.0) + read + write
    elif "cached_input_tokens" in usage:
        read = usage["cached_input_tokens"]
        total = max(usage.get("input_tokens", 0.0), read + write)
    else:
        return None
    return round(read / total, 4) if total > 0 else None


def cache_settings(config: dict[str, Any]) -> dict[str, Any]:
    cache = config.get("cache") or {}
    ttl = cache.get("ttl_seconds", 300)
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
        raise ValueError("cache.ttl_seconds must be a positive number")
    cold = cache.get("cold_resume", "resume")
    if cold not in {"resume", "fresh"}:
        raise ValueError("cache.cold_resume must be resume or fresh")
    epsilon = cache.get("warm_epsilon", 0.05)
    if isinstance(epsilon, bool) or not isinstance(epsilon, (int, float)) or not 0 <= epsilon < 1:
        raise ValueError("cache.warm_epsilon must be a number in [0, 1)")
    return {"configured": "cache" in config, "ttl_seconds": ttl, "cold_resume": cold, "warm_epsilon": epsilon}


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
                "cache_hit": 0,
                "duration_ms": 0,
                **{field: 0.0 for field in fields},
            },
        )
        group["calls"] += 1
        if span.get("status") == "success":
            group["success"] += 1
        elif span.get("status") == "cache_hit":
            group["cache_hit"] += 1
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


# One vocabulary for provider quota/rate-limit text, shared by lane cooldown,
# recovery classification and telemetry so the three can never disagree.
QUOTA_MARKERS = ("usage limit", "session limit", "rate limit", "quota", "credits",
                 "resets at", "resets ", "too many requests")
LANE_COOLDOWN_SECONDS = 900


def quota_failure(result: dict[str, Any]) -> bool:
    text = " ".join(str(item) for item in result.get("blockers", []))
    text = f"{text} {result.get('summary', '')}".lower()
    return any(marker in text for marker in QUOTA_MARKERS)


BASELINE_TOOLS = frozenset({
    "read", "edit", "write", "multiedit", "glob", "grep", "ls", "notebookread", "notebookedit",
    "viewfile", "viewfileoutline", "viewcodeitem", "listdir", "findbyname", "grepsearch", "codebasesearch",
    "writetofile", "replacefilecontent", "multireplacefilecontent",
    "readfile", "editfile", "writefile", "listfiles", "listdirectory",
})
_DENIED_TOOL_BLOCKER = re.compile(r"(?:permission denied|agy denied):\s*([A-Za-z_][\w.-]*)", re.IGNORECASE)
_AGY_AUTO_DENIED = re.compile(r"agy auto-denied tools in headless mode:\s*([^;]*)", re.IGNORECASE)


def _tool_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def normalize_tools(names: Any) -> list[str]:
    seen: dict[str, str] = {}
    for name in names or []:
        name = str(name or "").split("(", 1)[0].strip()
        if _tool_key(name) and _tool_key(name) not in seen:
            seen[_tool_key(name)] = name
    return list(seen.values())


def _denial_name(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    name = item.get("tool_name") or item.get("name") or item.get("tool") or item.get("display_name") or item.get("action")
    return str(name) if name else None


def provider_denied_tools(agent: str, stdout: str) -> list[str]:
    if agent not in {"claude", "agy"}:
        return []
    try:
        value = json.loads(stdout)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, dict):
        return []
    items = value.get("permission_denials" if agent == "claude" else "denied_actions")
    return normalize_tools(_denial_name(item) for item in items) if isinstance(items, list) else []


def blocker_denied_tools(blockers: Any) -> list[str]:
    names: list[str] = []
    for blocker in blockers or []:
        text = str(blocker)
        auto = _AGY_AUTO_DENIED.search(text)
        if auto:
            names += [part.strip() for part in auto.group(1).split(",")]
        names += _DENIED_TOOL_BLOCKER.findall(text)
    return normalize_tools(name for name in names if name.lower() != "tool")


def denial_blocks_lane(span: dict[str, Any]) -> bool:
    denied = span.get("denied_tools")
    if not denied:
        return True
    return any(_tool_key(str(name)) in BASELINE_TOOLS for name in denied)


def failure_class(result: dict[str, Any]) -> str | None:
    """Coarse, non-identifying category for a non-success result. Used both
    to make local `fusion usage` slicing easier and as the only failure
    signal sent in a remote telemetry payload -- raw blocker text can
    contain project-specific detail and is never sent remotely."""
    if result.get("failure_phase") in {"snapshot_before_review", "snapshot_after_review"}:
        return "coordinator_error"
    text = " ".join(str(item) for item in result.get("blockers", [])).lower()
    if "permission denied" in text or "agy denied" in text or "agy auto-denied" in text:
        return "permission_denied"
    if result.get("status") in {"success", "cache_hit"}:
        return None
    if quota_failure(result):
        return "quota"
    if result.get("exit_code") == 124 or "timeout" in text:
        return "timeout"
    if "not available on path" in text:
        return "missing_executable"
    return "worker_error"


def agy_headless_status(settings: dict[str, Any]) -> dict[str, Any]:
    """Check local permission setup without starting a model or changing policy.

    A scoped allowlist may work for an explicit task, but does not establish
    readiness for arbitrary automatic reviews. Explicit AGY selections remain
    possible, and actual permission denials still stop the workflow.
    """
    path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(value, dict):
            raise ValueError("settings must be a JSON object")
    except (OSError, ValueError) as exc:
        return {"automatic_ready": False, "reason": f"Cannot read AGY permission settings: {exc}"}
    enabled = settings.get("sandbox", True) is True or value.get("enableTerminalSandbox") is True
    policy = value.get("toolPermission", "request-review")
    ready = enabled and policy == "proceed-in-sandbox"
    return {"automatic_ready": ready,
            "reason": "Sandboxed commands configured; explicit permission rules still apply" if ready else
                      f'Headless command setup needed: set toolPermission to "proceed-in-sandbox" and enableTerminalSandbox to true in {path}'}


def worker_availability(config: dict[str, Any], agent: str) -> dict[str, Any]:
    settings = config.get(agent, {})
    available = bool(executable(settings.get("command", agent)))
    state = {"agent": agent, "available": available, "automatic_ready": available,
             "reason": "Installed; account availability is checked during execution" if available else "Not on PATH"}
    if available and execution_mode(config) == "yolo":
        state.update(automatic_ready=True, reason="YOLO: runtime permission prompts and sandbox disabled")
    elif available and agent == "agy" and settings.get("dangerously_skip_permissions") is True:
        state.update(automatic_ready=True, reason="dangerously_skip_permissions: agy runs without its sandbox or permission prompts")
    elif available and agent == "agy":
        # Sandboxed commands do not cover read tools like ViewFile, which headless
        # agy auto-denies, so automatic routing needs the explicit opt-out (#100).
        state.update(automatic_ready=False, reason="Headless agy auto-denies read tools (e.g. ViewFile); set dangerously_skip_permissions on the agy lane for automatic use")
    return state


# Stands in for the cached id when ORC_HOME cannot be written, so one such
# machine reports as one install for the life of the process rather than as a
# new install per span.
_EPHEMERAL_INSTALL_ID = uuid.uuid4().hex


def telemetry_install_id() -> str:
    """A random id stable across a machine's projects, not tied to a
    person's identity. Generated once and cached under ORC_HOME (matching
    the orc CLI's own convention), not per-workspace."""
    home = Path(os.environ.get("ORC_HOME") or (Path.home() / ".config" / "orc")).expanduser()
    id_path = home / "telemetry_id"
    try:
        # An unwritable home must never reach the caller: this runs on the
        # dispatch path now that reporting is on by default, and telemetry
        # failing is never a reason for a worker run to fail.
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _EPHEMERAL_INSTALL_ID
    try:
        existing = id_path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    new_id = uuid.uuid4().hex
    try:
        id_path.write_text(new_id + "\n", encoding="utf-8")
    except OSError:
        # Can't persist it, so don't mint a fresh one per span and report as
        # a thousand separate installs; reuse this process's id instead.
        return _EPHEMERAL_INSTALL_ID
    return new_id


def announce_remote_telemetry(endpoint: str) -> None:
    """Say it out loud, once per machine, the first time anything is sent.

    On-by-default collection that never announces itself is the thing
    people are right to resent, and this ships in a public repo where the
    sender may be a stranger rather than a collaborator.
    """
    home = Path(os.environ.get("ORC_HOME") or (Path.home() / ".config" / "orc")).expanduser()
    marker = home / "telemetry_announced"
    try:
        home.mkdir(parents=True, exist_ok=True)
        if marker.exists():
            return
        marker.write_text(endpoint + "\n", encoding="utf-8")
    except OSError:
        return
    print(
        f"fusion: sending anonymous usage telemetry to {endpoint}\n"
        f"        agent, model, outcome, timing and token counts -- never prompts,\n"
        f"        output, file paths or repository names. Stop it with\n"
        f"        `fusion telemetry off` (or FUSION_TELEMETRY=0) -- local traces keep working.\n"
        f"        Said once; see `fusion telemetry status` any time.",
        file=sys.stderr,
    )


def send_remote_telemetry(remote: dict[str, Any], span: dict[str, Any]) -> None:
    """Best-effort, deliberately reduced telemetry send. Never raises: a
    down or misconfigured collector must never affect the actual dispatch.
    Strips everything the local trace span carries that could be
    identifying or project-specific -- changed file paths, test commands,
    raw blocker text, and local filesystem artifact paths -- keeping only
    what real Anthropic Cost & Usage-style dashboards need: agent, route,
    model, outcome, timing, and token/cost usage."""
    endpoint = str(remote.get("endpoint") or "")
    if not endpoint:
        return
    announce_remote_telemetry(endpoint)
    payload = {
        "schema": TELEMETRY_SCHEMA,
        "install_id": telemetry_install_id(),
        "spans": [{
            "trace_id": span.get("trace_id"),
            "span_id": span.get("span_id"),
            "parent_span_id": span.get("parent_span_id"),
            "agent": span.get("agent"),
            "role": span.get("role"),
            "route": span.get("route"),
            "model": span.get("model"),
            "write": span.get("write"),
            "status": span.get("status"),
            "failure_class": span.get("failure_class"),
            "start_time_ms": span.get("start_time_ms"),
            "end_time_ms": span.get("end_time_ms"),
            "duration_ms": span.get("duration_ms"),
            "usage": normalized_usage(span.get("usage")),
        }],
    }
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(endpoint, data=body, method="POST", headers={"Content-Type": "application/json"})
        token = str(remote.get("token") or "")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        urllib.request.urlopen(request, timeout=3).close()
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        pass


def _summary_endpoint(ingest_endpoint: str) -> str:
    """The summary endpoint is always the ingest endpoint's sibling path
    (.../v1/ingest -> .../v1/summary)."""
    base, _, _ = ingest_endpoint.rstrip("/").rpartition("/")
    return f"{base}/summary" if base else ingest_endpoint


def set_remote_telemetry(workspace: Path, enabled: bool) -> Path:
    """Persist the reporting choice so the opt-out in the first-run notice is
    a command, not an instruction to hand-author JSON."""
    path = find_upward(".fusion.json", workspace) or (workspace / ".fusion.json")
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(current, dict):
        raise ValueError(f"{path} must contain a JSON object")
    telemetry = current.setdefault("telemetry", {})
    if not isinstance(telemetry, dict):
        raise ValueError(f"{path} has a non-object telemetry block")
    remote = telemetry.setdefault("remote", {})
    if not isinstance(remote, dict):
        raise ValueError(f"{path} has a non-object telemetry.remote block")
    remote["enabled"] = enabled
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    return path


def fetch_remote_summary(remote: dict[str, Any], hours: int, every_install: bool = False) -> dict[str, Any]:
    """Unlike send_remote_telemetry, this is an explicit interactive request
    (`fusion telemetry report`) so it surfaces errors instead of swallowing
    them -- a user asking to see their data wants to know if the collector is
    unreachable, not silently get nothing.

    Reading your own rows needs no credential: this machine already holds an
    unguessable install id and already sends it with every span, so it can ask
    for its own rows back. Only the cross-install view needs the shared token,
    because that one reveals how many people are running this and what they
    spend."""
    endpoint = str(remote.get("endpoint") or "")
    if not endpoint:
        raise ValueError("telemetry.remote.endpoint is not configured")
    url = f"{_summary_endpoint(endpoint)}?hours={int(hours)}"
    token = str(remote.get("token") or "")
    if every_install:
        if not token:
            raise ValueError("reading every install needs telemetry.remote.token; without it you still see your own rows")
    else:
        url += "&install_id=" + urllib.parse.quote(telemetry_install_id(), safe="")
    request = urllib.request.Request(url, method="GET")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


class RunStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.root = workspace / ".fusion"
        self.runs = self.root / "runs"
        self.sessions_path = self.root / "sessions.json"
        self.session_use_path = self.root / "session_use.json"
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
            "failure_class": failure_class(result),
            "execution_mode": execution_mode(config),
            "agent": task["agent"],
            "role": task["role"],
            "route": task.get("route"),
            "model": metadata.get("model"),
            "write": task.get("write", False),
            "execution_choice": result.get("execution_choice"),
            "usage": result.get("usage") or {},
            "session_key": task.get("session_key"),
            "resumed": bool(result.get("resumed")),
            "session_idle_s": result.get("session_idle_s"),
            **({"resume_skipped": result["resume_skipped"]} if result.get("resume_skipped") else {}),
            "cache_read_ratio": cache_read_ratio(result.get("usage")),
            "changed": result.get("changed", []),
            "tests": result.get("tests", []),
            "blockers": result.get("blockers", []),
            "denied_tools": result.get("denied_tools") or [],
            "run_id": task["run_id"],
            "artifacts": result.get("artifacts", {}),
        }
        with self.traces_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(span, ensure_ascii=False) + "\n")
        run_dir = result.get("artifacts", {}).get("run_dir")
        if run_dir:
            Path(run_dir, "trace.json").write_text(json_text(span) + "\n", encoding="utf-8")
        remote = telemetry.get("remote") or {}
        # FUSION_TELEMETRY=0 stops the send without touching the local trace:
        # lane cooldown, resume and `fusion usage` all read that file, and
        # opting out of reporting should not cost the machine its own records.
        if remote.get("enabled") and os.environ.get("FUSION_TELEMETRY") != "0":
            send_remote_telemetry(remote, span)

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

    def _update_json(self, path: Path, key: str, value: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / "sessions.lock"
        with lock_path.open("w", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                except (OSError, json.JSONDecodeError):
                    current = {}
                current = current if isinstance(current, dict) else {}
                current[key] = value
                self.write_json(path, current)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def set_session(self, key: str, session_id: str) -> None:
        self._update_json(self.sessions_path, key, session_id)

    def session_last_used(self, key: str) -> int | None:
        """End time (ms) of the last worker run under this session key."""
        try:
            value = json.loads(self.session_use_path.read_text(encoding="utf-8")).get(key)
        except (OSError, json.JSONDecodeError, AttributeError):
            return None
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def touch_session(self, key: str, ended_at_ms: int) -> None:
        self._update_json(self.session_use_path, key, ended_at_ms)

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


def choice_overrides(model: Any, reasoning_effort: Any) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if model:
        overrides["model"] = str(model)
    if reasoning_effort:
        validate_pair(overrides.get("model"), reasoning_effort)
        overrides["reasoning_effort"] = reasoning_effort
    return overrides


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


def _denial_note(item: Any) -> str:
    """Best-effort label for one denied-tool-call entry. The exact shape of a
    real populated permission_denials entry is unverified (every live attempt
    to trigger one came back empty -- see FUSION_RESEARCH.md), so this tries
    several plausible field names and falls back to the raw entry rather than
    guessing wrong and silently dropping information."""
    if not isinstance(item, dict):
        return str(item)
    name = _denial_name(item)
    reason = item.get("reason") or item.get("message")
    if name and reason:
        return f"{name}: {reason}"
    if name:
        return str(name)
    return json.dumps(item, sort_keys=True)


def parse_codex_events(stdout: str) -> tuple[str | None, str, str | None, dict[str, Any], str | None, list[str]]:
    thread_id = None
    model = None
    messages: list[str] = []
    failure: str | None = None
    usage: dict[str, Any] = {}
    non_json: list[str] = []
    command_evidence: list[str] = []
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
            elif item.get("type") == "command_execution":
                # Keep command exits for inspection separately from unresolved
                # handoff blockers. Searches can return 1, red tests can precede
                # a fix, and failed lookups can be corrected within this turn.
                exit_code = item.get("exit_code")
                if isinstance(exit_code, int) and exit_code != 0:
                    command = compact(str(item.get("command") or "command"), 200)
                    command_evidence.append(f"command exited {exit_code}: {command}")
        elif event_type == "turn.completed":
            usage = event.get("usage") or {}
        elif event_type == "turn.failed":
            failure = str((event.get("error") or {}).get("message") or "Codex turn failed")
        elif event_type == "error":
            failure = str(event.get("message") or "Codex emitted an error")
    text = messages[-1] if messages else "\n".join(non_json)
    return thread_id, text, failure, usage, model, command_evidence


def claude_final_thinking(session_id: str) -> str:
    """Reasoning from a Claude Code session's final assistant turn, as evidence only.

    Some OpenRouter reasoning models put their whole reply in a thinking block
    and return an empty result. Reasoning is where a model drafts, so a
    `STATUS: success` in it is a plan, not a report: the caller saves this for
    inspection and still treats the run as an empty answer. Only this session's
    own transcript is read, and only turns after the last user message.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id or ""):
        return ""
    roots = [Path(os.environ["CLAUDE_CONFIG_DIR"])] if os.environ.get("CLAUDE_CONFIG_DIR") else []
    roots += [Path(os.environ.get("ORC_HOME") or Path.home() / ".config/orc") / "claude-state", Path.home() / ".claude"]
    for root in dict.fromkeys(roots):
        for transcript in (root / "projects").glob(f"*/{session_id}.jsonl"):
            try:
                lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            final = []
            for line in reversed(lines):
                try:
                    message = (json.loads(line) or {}).get("message") or {}
                except (ValueError, AttributeError):
                    continue
                if message.get("role") == "user" and not any(
                        isinstance(part, dict) and part.get("type") == "tool_result" for part in message.get("content") or []):
                    break
                if message.get("role") == "assistant":
                    final[:0] = [part["thinking"] for part in message.get("content") or []
                                 if isinstance(part, dict) and part.get("type") == "thinking" and part.get("thinking")]
            return "\n\n".join(final).strip()
    return ""


def parse_claude_output(stdout: str) -> tuple[str | None, str, str | None, dict[str, Any], str | None, list[str]]:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return None, stdout.strip(), None, {}, None, []
    if not isinstance(value, dict):
        return None, stdout.strip(), None, {}, None, []
    session_id = value.get("session_id")
    text = str(value.get("result") or value.get("message") or "")
    failure = text if value.get("is_error") else None
    usage = dict(value.get("usage") or {})
    # Real `claude -p --output-format json` puts cost at the top level as
    # total_cost_usd, not inside usage, and does not have a top-level model/
    # model_id field at all -- the model lives as a key in modelUsage. Older
    # or synthetic output that already sets these directly is left alone.
    if "cost_usd" not in usage and "cost" not in usage and value.get("total_cost_usd") is not None:
        usage["cost_usd"] = value["total_cost_usd"]
    model = value.get("model") or value.get("model_id")
    if not model:
        model_usage = value.get("modelUsage")
        if isinstance(model_usage, dict) and model_usage:
            model = next(iter(model_usage))
    denials = value.get("permission_denials")
    denial_notes = [f"permission denied: {_denial_note(item)}" for item in denials] if isinstance(denials, list) and denials else []
    return session_id, text, failure, usage, model, denial_notes


def parse_grok_output(stdout: str, output_format: str = "streaming-json"):
    """Grok's documented headless ACP projection; never include thought events."""
    if output_format == "plain":
        return None, stdout.strip(), None, {}, None, []
    text, failure, final = [], None, {}
    ended = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "text" and isinstance(event.get("data"), str):
            text.append(event["data"])
        elif kind == "tool_call":
            text = []  # Commentary before tools is not the final handoff.
        elif kind == "error":
            failure = str(event.get("message") or "Grok reported an error")
            final = event
        elif kind == "end":
            ended, final = True, event
            if event.get("stopReason") not in (None, "end_turn"):
                failure = failure or "Grok stopped: " + str(event["stopReason"])
    if not ended:
        failure = failure or "Grok stream ended without its final completion event"
    usage = dict(final.get("usage") or {})
    if final.get("total_cost_usd") is not None and not final.get("cost_is_partial"):
        usage["cost_usd"] = final["total_cost_usd"]
    models = final.get("modelUsage") or {}
    model = next(iter(models)) if len(models) == 1 else None
    return final.get("sessionId"), "".join(text).strip(), failure, usage, model, []


def parse_agy_output(stdout: str) -> tuple[str | None, str, str | None, dict[str, Any], str | None, list[str]]:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return None, stdout.strip(), None, {}, None, []
    if not isinstance(value, dict):
        return None, stdout.strip(), None, {}, None, []
    session_id = value.get("conversation_id")
    text = str(value.get("response") or "")
    status = str(value.get("status") or "").upper()
    denied = value.get("denied_actions") or []
    failure = None
    if status not in {"SUCCESS", ""}:
        failure = str(value.get("error") or f"agy reported status {status.lower() or 'unknown'}")
    elif not text and denied:
        names = ", ".join(
            str(item.get("display_name") or item.get("action") or "tool")
            for item in denied
            if isinstance(item, dict)
        ) or "tool"
        failure = (f"agy auto-denied tools in headless mode: {names}; configure sandboxed commands "
                   '(enableTerminalSandbox: true, toolPermission: "proceed-in-sandbox") in '
                   "~/.gemini/antigravity-cli/settings.json, or retry this stage with another worker. "
                   "Explicit deny/ask rules still require attention.")
    elif not text:
        failure = "agy returned an empty response"
    raw_usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
    usage = {
        "input_tokens": raw_usage.get("input_tokens", 0),
        "output_tokens": raw_usage.get("output_tokens", 0),
        "cache_read_input_tokens": raw_usage.get("cache_read_tokens", 0),
        "reasoning_output_tokens": raw_usage.get("thinking_tokens", 0),
    }
    model = value.get("model") or value.get("model_id")
    # The failure branch above already covers a denial-with-no-other-text
    # turn. Surface denials here only when there IS other text, so a denial
    # alongside an otherwise-successful turn isn't silently dropped without
    # duplicating the failure message above.
    denial_notes = (
        [f"agy denied: {_denial_note(item)}" for item in denied]
        if text and isinstance(denied, list) and denied
        else []
    )
    return session_id, text, failure, usage, model, denial_notes


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
        if Path(str(route.get("command", settings.get("command", agent)))).name == "orc" and "model" not in route:
            # A native model id (claude.model) means nothing on OpenRouter and
            # has no fit evidence there; an orc route picks its own model.
            settings.pop("model", None)
        settings = deep_merge(settings, route)
    overrides = task.get("settings_overrides") or {}
    if isinstance(overrides, dict):
        settings = deep_merge(settings, overrides)
    return settings


def _orc_model_ids(command: str, filter_args: list[str]) -> list[str]:
    try:
        completed = subprocess.run(
            [command, "models", *filter_args],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    ids = []
    for line in completed.stdout.splitlines():
        candidate = line.strip().split(maxsplit=1)
        if candidate and "/" in candidate[0] and not candidate[0].startswith("-"):
            ids.append(candidate[0])
    return ids


def select_orc_model(command: str, selector: str, allow_untested: bool = False) -> str | None:
    """Ask orc for its current ranked model; never hard-code a volatile model id.

    Free/best selectors require a model that already passed `orc probe --fit`
    unless allow_untested is set. Picking the first tool-capable model
    regardless of fit let a workflow route to a model that rejects every
    call before generation even starts; pass allow_untested=True to opt back
    into that best-effort behavior.
    """
    if selector not in {"free", "best"}:
        return None
    if allow_untested:
        ranked = _orc_model_ids(command, ["--free", "--tools"] if selector == "free" else ["--tools"])
        return ranked[0] if ranked else None
    fitted = fitted_orc_models(command, selector, 1)
    return fitted[0] if fitted else None


def fitted_orc_models(command: str, selector: str, limit: int) -> list[str]:
    """orc's quality ranking, restricted to models that passed `orc probe --fit`."""
    if selector not in {"free", "best"}:
        return []
    ranked = _orc_model_ids(command, ["--free", "--tools"] if selector == "free" else ["--tools"])
    if selector == "best":
        ranked = [candidate for candidate in ranked if not candidate.endswith(":free")]
    fit_ids = set(_orc_model_ids(command, ["--fit"]))
    return [candidate for candidate in ranked if candidate in fit_ids][:max(0, limit)]


def codex_permission_args(workspace: Path, settings: dict[str, Any], write: bool) -> list[str]:
    """Allow repository Git operations for writers without unrestricted access."""
    sandbox = settings.get("sandbox", "workspace-write") if write else "read-only"
    if sandbox != "workspace-write" or not settings.get("git_write", True):
        return ["-s", sandbox]
    # Inherit Codex's workspace/temp/network boundary and protected .codex paths.
    # Only Git metadata is added, including the shared directory of a worktree.
    roots = set()
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if result.returncode == 0:
            roots = {str(Path(line).resolve()) for line in result.stdout.splitlines() if line.strip()}
    except (OSError, subprocess.TimeoutExpired):
        pass  # A new, non-Git workspace may still initialize its own .git.
    paths = [json.dumps(path) + '="write"' for path in sorted(roots)]
    filesystem = ','.join(['":workspace_roots"={".git"="write"}', *paths])
    profile = '{extends=":workspace",filesystem={' + filesystem + '}}'
    return ["--strict-config", "-c", 'default_permissions="fusion_git_write"',
            "-c", "permissions.fusion_git_write=" + profile]


def agent_command(
    config: dict[str, Any],
    task: dict[str, Any],
    session_id: str | None,
) -> tuple[list[str], dict[str, str], dict[str, Any]]:
    agent = task["agent"]
    settings = agent_settings(config, task)
    if settings.get("reasoning_effort") is not None and agent not in {"codex", "claude", "agy"}:
        raise ValueError("reasoning_effort is currently supported only for native Codex, Claude Code and agy")
    yolo = execution_mode(config) == "yolo"
    env = os.environ.copy()
    if agent == "codex":
        from fusion_reasoning import execution_choice
        choice = execution_choice(settings)
        if task["write"] and settings.get("reasoning_effort") == "ultra":
            raise ValueError("ultra is currently limited to read-only workers; nested writer visibility is unqualified")
        command = settings.get("command", "codex")
        permissions = ["--dangerously-bypass-approvals-and-sandbox"] if yolo else [
            *codex_permission_args(Path(task["workspace"]), settings, task["write"]),
            "-a", settings.get("approval", "never") if task["write"] else "never"]
        argv = [command, "-C", task["workspace"], *permissions]
        if settings.get("reasoning_effort") is not None:
            argv += ["-c", "model_reasoning_effort=" + json.dumps(settings["reasoning_effort"])]
        argv += ["exec", "resume", session_id, "--json"] if session_id else ["exec", "--json", "--skip-git-repo-check"]
        model = settings.get("model")
        if model:
            argv += ["-m", model]
        argv.append("-")
        return argv, os.environ.copy(), {"command": command, "model": settings.get("model") or "",
                                         "execution_choice": choice}
    if agent == "agy":
        command = settings.get("command", "agy")
        mode_key = str(settings.get("mode") or settings.get("permission_mode") or "")
        mode = "accept-edits" if yolo else (AGY_MODE_ALIASES.get(mode_key) or "accept-edits") if task["write"] else "plan"
        argv = [command, "-p", brief_for(task), "--output-format", "json", "--mode", mode]
        # Headless agy auto-denies tools it would prompt for. This per-lane opt-out
        # is the only restricted-mode path that drops its sandbox, and says so.
        if yolo or settings.get("dangerously_skip_permissions") is True:
            argv.append("--dangerously-skip-permissions")
        elif settings.get("sandbox", True) is True:
            argv.append("--sandbox")
        selected_model = str(settings.get("model", ""))
        if selected_model:
            argv += ["--model", selected_model]
        choice = None
        if settings.get("reasoning_effort") is not None:
            from fusion_reasoning import check_pair, recorded_choice
            choice = recorded_choice(check_pair("agy", settings), {"status": "unchecked", "reason": "agy publishes no per-model effort catalog"})
            argv += ["--effort", settings["reasoning_effort"]]
        print_timeout = settings.get("print_timeout")
        if print_timeout:
            argv += ["--print-timeout", str(print_timeout)]
        if session_id:
            argv += ["--conversation", session_id]
        return argv, os.environ.copy(), {"command": command, "model": selected_model,
                                         **({"execution_choice": choice} if choice else {})}
    if agent == "grok":
        command = settings.get("command", "grok")
        mode = "bypassPermissions" if yolo else settings.get("permission_mode", "plan") if task["write"] else "plan"
        if mode not in {"plan", "acceptEdits", "dontAsk", "bypassPermissions"}:
            raise ValueError("Grok workers require plan, acceptEdits, or dontAsk permissions")
        output_format = settings.get("output_format", "streaming-json")
        if output_format not in {"plain", "streaming-json"}:
            raise ValueError("Grok output_format must be plain or streaming-json")
        argv = [command, "--cwd", task["workspace"], "--no-subagents", "--permission-mode", mode,
                "--output-format", output_format, "-p", brief_for(task)]
        if yolo:
            argv += ["--sandbox", "none", "--no-plan"]
        if settings.get("model"):
            argv += ["--model", settings["model"]]
        return argv, os.environ.copy(), {"command": command, "model": settings.get("model") or "", "output_format": output_format}
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
            selected_model = select_orc_model(
                command,
                str(settings.get("model_selector", "")),
                bool(settings.get("allow_untested", False)),
            ) or ""
        if command_name == "orc" and selected_model:
            argv += ["-m", selected_model]
        argv += ["-p", "--output-format", "json"]
        if yolo:
            argv += ["--dangerously-skip-permissions", "--settings", '{"sandbox":{"enabled":false}}']
            if command_name == "orc":
                env["ORC_MODE"] = "yolo"
        else:
            mode = settings.get("permission_mode", "acceptEdits") if task["write"] else "plan"
            argv += ["--permission-mode", mode]
        permission_prompts = settings.get("permission_prompts")
        if permission_prompts and not yolo:
            argv += ["--permission-prompts", permission_prompts]
        if command_name != "orc" and selected_model:
            argv += ["--model", selected_model]
        choice = None
        if settings.get("reasoning_effort") is not None:
            from fusion_reasoning import claude_choice
            choice = claude_choice({**settings, "model": selected_model or settings.get("model")})
            argv += ["--effort", settings["reasoning_effort"]]
        max_budget = settings.get("max_budget_usd")
        free_route = command_name == "orc" and (
            str(settings.get("model_selector", "")) == "free" or selected_model.endswith(":free")
        )
        if max_budget is not None and not free_route:
            # Claude Code prices --max-budget-usd at Anthropic list rates, so on
            # :free OpenRouter routes the guard trips on phantom cost while real
            # spend is zero; it would only sabotage cheap lanes.
            argv += ["--max-budget-usd", str(max_budget)]
        allowed = [*(settings.get("allowed_tools") or []),
                   *(f"Bash({shlex.join(argv)}:*)" for argv in task.get("verification_argv") or [])]
        if allowed and not yolo:
            argv += ["--allowedTools", *allowed]
        if session_id:
            argv += ["--resume", session_id]
        # Claude's --allowedTools consumes variadic values. Terminate options
        # explicitly so a fresh task's prompt cannot be swallowed as a tool.
        argv += ["--", brief_for(task)]
        return argv, env, {"command": command, "model": selected_model,
                           **({"execution_choice": choice} if choice else {})}
    raise ValueError(f"unsupported agent: {agent}")


def run_directory(workspace: Path, run_id: str) -> Path | None:
    """The directory of a completed run of this workspace, or None.

    Workflow worktrees link `.fusion` back to the workspace, but a worker that
    can write its worktree can also replace that link, and older runs then
    landed in `<worktree>/.fusion/runs`. Those are still this workspace's
    runs, so they are found there after the main runs directory. Only paths
    that resolve inside this workspace's `.fusion` are accepted.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id or ""):
        return None
    root = (Path(workspace) / ".fusion").resolve()
    for directory in [RunStore(workspace).runs / run_id,
                      *sorted((Path(workspace) / ".fusion" / "worktrees").glob(f"*/.fusion/runs/{run_id}"))]:
        result = directory / "result.json"
        if result.is_file() and result.resolve().is_relative_to(root):
            return directory
    return None


def record_outcome(workspace: Path, run_id: str, accepted: bool, reason: str = "") -> dict[str, Any]:
    """The lead's verdict on a delegation, after inspecting its diff and tests.

    Delegations have no coordinator gate, so without this their only signal is
    the worker's own claim. Outcomes feed decisions.rank_by_outcomes; the
    latest verdict for a run wins. A verdict with a reason on a reported
    success also becomes an acceptance label (fusion_labeling.verdict_label).
    A run found in a workflow worktree is recorded here, in this workspace's
    decision store, with evidence pointing at its worktree path.
    """
    from fusion_decisions import DecisionStore
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id or ""):
        raise ValueError("run_id must be a Fusion run id")
    result_path = (run_directory(workspace, run_id) or RunStore(workspace).runs / run_id) / "result.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"no completed Fusion run {run_id} in this workspace") from exc
    event = {"task_id": run_id, "group": result.get("trace_id") or run_id, "accepted": bool(accepted),
             "status": result.get("status"), "source": "lead", "reason": str(reason)[:2000],
             "route": result.get("route"), "agent": result.get("agent"), "model": result.get("model"),
             "evidence": str(result_path)}
    DecisionStore(workspace).append("outcome", **event)
    from fusion_labeling import verdict_label
    label = verdict_label(workspace, load_config(workspace)[0], run_id, result, bool(accepted), reason, str(result_path))
    return {"recorded": True, **event, "label": label}


def dispatch(
    config: dict[str, Any],
    task: dict[str, Any],
    store: RunStore,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    from fusion_policy import route_task, review_task
    if os.environ.get("FUSION_READ_ONLY") == "1" and task["write"]:
        raise ValueError("this Fusion session permits read-only work only")
    route_task(config, task, store)
    # Pinned choices do not resume a session created for a different pair.
    # Keep legacy session keys unchanged when effort is inherited.
    if task["agent"] in {"codex", "claude", "agy"}:
        settings = agent_settings(config, task)
        if settings.get("reasoning_effort") is not None:
            from fusion_reasoning import pair_key, validate_pair
            task["session_key"] += ":" + pair_key(validate_pair(settings.get("model"), settings["reasoning_effort"])) + ":delegation=" + str(settings.get("allow_native_delegation", False)).lower()
    if "review" in task["role"].lower() and not task["write"]:
        review_task(config, task, store)
    run_dir = run_dir or store.create(task)
    store.event(run_dir, "run.started", {"agent": task["agent"], "write": task["write"]})
    started_at_ms = now_ms()
    cache = cache_settings(config)
    last_used = store.session_last_used(task["session_key"])
    session = {"session_idle_s": round(max(0, started_at_ms - last_used) / 1000, 3) if last_used is not None else None}
    session_id = None
    if task["resume"]:
        session_id = store.sessions().get(task["session_key"])
        # A resume past the cache TTL re-sends the whole history at write price.
        if session_id and cache["cold_resume"] == "fresh" and session["session_idle_s"] is not None \
                and session["session_idle_s"] > cache["ttl_seconds"]:
            session_id, session["resume_skipped"] = None, "cold"
    session["resumed"] = bool(session_id)
    argv, env, metadata = agent_command(config, task, session_id)
    metadata["execution_mode"] = execution_mode(config)
    task["resolved"] = metadata
    store.write_json(run_dir / "task.json", task)
    binary = executable(argv[0])
    if binary is None:
        progress.emit(task.get("progress_label", task["role"]), f"cannot start {task['agent']}: executable unavailable")
        result = {
            "schema": SCHEMA,
            "run_id": task["run_id"],
            "status": "error",
            "agent": task["agent"],
            "route": task.get("route"),
            "model": metadata.get("model"),
            "execution_choice": metadata.get("execution_choice"),
            "summary": f"{argv[0]} is not available on PATH",
            "verdict": "error",
            "verdict_reason": "missing_binary",
            "changed": [],
            "tests": [],
            "blockers": [f"install or expose {argv[0]} before dispatching"],
            "exit_code": 127,
            "duration_ms": 0,
            "usage": {},
            **session,
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
    evidence_notes: list[str] = []
    worker_stdout = ""
    exit_code = 1
    label = task.get("progress_label", task["role"])
    scope = "implementation" if task["write"] else "review/investigation only"
    access = "YOLO: no runtime permission prompts or sandbox" if execution_mode(config) == "yolo" else "restricted runtime"
    requested = (metadata.get("execution_choice") or {}).get("requested") or {}
    selection = f"; requested {requested['model']} / {requested['reasoning_effort']} effort" if requested.get("reasoning_effort") else ""
    progress.emit(label, f"selected {task['agent']} ({task.get('route') or 'native'}){selection}; {scope}; {access}")
    try:
        with contextlib.ExitStack() as stack:
            with progress.activity(label, "acquiring workspace writer lock" if task["write"] else "preparing read-only worker"):
                stack.enter_context(writer_lock(Path(task["workspace"]), task["write"]))
            store.event(run_dir, "worker.started", {"argv": argv, "resumed_session": bool(session_id), **session})
            if metadata.get("execution_choice"):
                metadata["execution_choice"]["dispatch"] = {"status": "attempted", "argv": argv}
                store.write_json(run_dir / "task.json", task)
            completed = progress.run_logged(
                argv,
                cwd=task["workspace"],
                env=env,
                input=prompt if task["agent"] == "codex" else None,
                timeout=int(config.get("timeout_seconds", 3600)),
                stdout_path=stdout_path, stderr_path=stderr_path, label=label,
                plain_output=task["agent"] == "grok" and metadata.get("output_format") == "plain",
            )
        exit_code, worker_stdout = completed.returncode, completed.stdout
        if metadata.get("execution_choice"):
            metadata["execution_choice"]["dispatch"]["status"] = "returned"
        if task["agent"] == "codex":
            new_session, summary, failure, usage, event_model, evidence_notes = parse_codex_events(completed.stdout)
        elif task["agent"] == "agy":
            new_session, summary, failure, usage, event_model, evidence_notes = parse_agy_output(completed.stdout)
        elif task["agent"] == "grok":
            new_session, summary, failure, usage, event_model, evidence_notes = parse_grok_output(completed.stdout, metadata.get("output_format", "plain"))
            if exit_code != 0 and completed.stderr.strip():
                failure = failure or compact(progress.clean(completed.stderr), 1500)
        else:
            new_session, summary, failure, usage, event_model, evidence_notes = parse_claude_output(completed.stdout)
        # Preserve the public deliverable outside the compact receipt and trace.
        if summary.strip():
            answer_path = run_dir / "answer.md"
            with open(answer_path, "w", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
                stream.write(summary.strip() + "\n")
        model = event_model or model
        if str(model or "").endswith(":free") and "cost_usd" in usage:
            # Claude Code prices every model at Anthropic list rates; an
            # OpenRouter :free model costs nothing, so the estimate must not
            # count toward budgets or routing cost.
            usage["cost_estimate_usd"] = usage.pop("cost_usd")
            usage["cost_usd"] = 0.0
        if metadata.get("execution_choice") and event_model:
            metadata["execution_choice"]["observed"] = {
                "model": event_model, "reasoning_effort": None, "status": "model_reported",
                "source": "codex_json", "note": "effort is not attested by the stock exec event stream"}
        handoff = parse_handoff(summary)
        if new_session:
            store.set_session(task["session_key"], new_session)
        # Codex command exits remain evidence; provider permission denials fail.
        if failure or (task["agent"] != "codex" and evidence_notes):
            status = "error"
        elif exit_code == 0 and not summary.strip():
            # A clean exit with nothing said is not a result. Free OpenRouter
            # models do this; without an acceptance gate it read as success.
            status = "error"
            failure = "worker returned an empty answer"
            thinking = claude_final_thinking(new_session or "") if task["agent"] == "claude" else ""
            if thinking:
                with open(run_dir / "thinking.md", "w", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
                    stream.write(thinking + "\n")
                failure += "; its final reasoning is saved in thinking.md as evidence, not a handoff"
        elif exit_code == 0:
            status = handoff.get("reported_status") or "success"
        else:
            status = "error"
            failure = failure or f"worker exited with code {exit_code}"
    except subprocess.TimeoutExpired as exc:
        summary = "worker timed out"
        failure = f"timeout after {config.get('timeout_seconds', 3600)} seconds"
        status = "blocked"
        exit_code = 124
    except OSError as exc:
        summary = "could not start worker"
        failure = str(exc)
        status = "error"
        exit_code = 126
    except progress.WorkerCancelled as exc:
        summary, failure, status, exit_code = "worker interrupted", str(exc), "blocked", 130
    duration_ms = int((time.monotonic() - started) * 1000)
    verdict = classify_verdict(status, failure, str(handoff.get("summary") or summary), exit_code)
    progress.emit(label, f"worker {status} after {progress.elapsed(duration_ms / 1000)}; exit {exit_code}")
    blockers = handoff.get("blockers", []) + (evidence_notes if task["agent"] != "codex" else []) + ([failure] if failure else [])
    result = {
        "schema": SCHEMA,
        "run_id": task["run_id"],
        "status": status,
        "execution_mode": execution_mode(config),
        "agent": task["agent"],
        "role": task["role"],
        "route": task.get("route"),
        "model": model,
        "execution_choice": metadata.get("execution_choice"),
        "summary": compact(str(handoff.get("summary") or summary).strip(), int(config.get("max_result_chars", 12000))),
        "verdict": verdict["verdict"],
        "verdict_reason": verdict.get("reason"),
        "resets_at": verdict.get("resets_at"),
        "changed": handoff.get("changed", []),
        "tests": handoff.get("tests", []),
        "blockers": blockers,
        "denied_tools": provider_denied_tools(task["agent"], worker_stdout) or blocker_denied_tools(blockers),
        "command_evidence": evidence_notes if task["agent"] == "codex" else [],
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "usage": usage,
        **session,
        "decisions": task.get("decisions", {}),
        "artifacts": {
            "run_dir": str(run_dir),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            **({"answer": str(run_dir / "answer.md")} if (run_dir / "answer.md").exists() else {}),
            **({"thinking": str(run_dir / "thinking.md")} if (run_dir / "thinking.md").exists() else {}),
        },
    }
    ended_at_ms = now_ms()
    store.touch_session(task["session_key"], ended_at_ms)
    store.write_json(run_dir / "result.json", result)
    store.event(run_dir, "run.finished", {"result": result})
    store.trace_span(config, task, result, started_at_ms, ended_at_ms, {**metadata, "model": model})
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
        "mode",
        "print_timeout",
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
        progress.emit("ultra", f"stage {index}/{len(stage_names)}: {stage_name}")
        raw_stage = configured_stages.get(stage_name) or {}
        if not isinstance(raw_stage, dict):
            raise ValueError(f"ultra stage {stage_name} must be an object")
        stage = dict(raw_stage)
        agent = harness or str(stage.get("agent", "claude"))
        write = bool(stage.get("write", stage_name == "implement"))
        route = stage.get("route")
        if harness == "codex":
            route = "codex-write" if write else "codex-read"
        elif harness:
            route = None
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
    return fusion_mcp.ASYNC_TOOLS + [
        {
            "name": "fusion_delegate",
            "outputSchema": {"type": "object", "properties": {"status": {"type": "string"}, "summary": {"type": "string"}, "blockers": {"type": "array", "items": {"type": "string"}}, "artifacts": {"type": "object"}}, "required": ["status"]},
            "description": "Delegate a bounded task to the other coding agent and receive a structured handoff. The lead keeps final judgment.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "enum": ["auto", "codex", "claude", "agy", "grok"]},
                    "task": {"type": "string"},
                    "role": {"type": "string", "default": "implementation"},
                    "success_criteria": {"type": "array", "items": {"type": "string"}},
                    "constraints": {"type": "array", "items": {"type": "string"}},
                    "write": {"type": "boolean", "default": True},
                    "resume": {"type": "boolean", "default": True},
                    "session_key": {"type": "string"},
                    "workspace": {"type": "string", "description": "Optional workspace path for the delegated task."},
                    "route": {"type": "string", "description": "Optional named route such as orc-free or orc-best."},
                    "model": {"type": "string", "description": "Model for this task, overriding the route and agent settings."},
                    "reasoning_effort": {"type": "string", "enum": sorted(EFFORTS), "description": "Codex, or Claude Code (low-max); requires model."},
                },
                "required": ["agent", "task"],
            },
        },
        {
            "name": "fusion_outcome",
            "outputSchema": {"type": "object", "properties": {"recorded": {"type": "boolean"}, "task_id": {"type": "string"}, "accepted": {"type": "boolean"}, "label": {"type": "object"}}, "required": ["recorded"]},
            "description": "Record your verdict on a delegated run after inspecting its diff and tests. Accepted/rejected outcomes rank future automatic routes; a worker's own success claim does not. With a reason, a verdict on a reported success also becomes a local acceptance training label (source lead_verdict).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "run_id from the fusion_delegate result."},
                    "accepted": {"type": "boolean"},
                    "reason": {"type": "string", "description": "What you verified or why you rejected it. Required for the verdict to become a training label."},
                },
                "required": ["run_id", "accepted"],
            },
        },
        {
            "name": "fusion_decisions",
            "outputSchema": {"type": "object", "properties": {"decisions": {"type": "array", "items": {"type": "object"}}}, "required": ["decisions"]},
            "description": "Inspect local classifier advice; recommendations never confer permissions or replace verification.",
            "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}}},
        },
        {
            "name": "fusion_status",
            "outputSchema": {"type": "object", "properties": {"runs": {"type": "array", "items": {"type": "object"}}}, "required": ["runs"]},
            "description": "List recent Fusion runs and their structured results.",
            "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}}},
        },
    ]


import fusion_mcp  # noqa: E402  (module-level cycle-free helper)


def _reported_cost(row: dict[str, Any]) -> str:
    """Render a group's cost without passing an absence off as a zero.

    Providers differ: Claude reports cost_usd, Codex does not. Summing a column
    of nulls gives 0.0, and printing that as $0.0000 says "these calls were
    free" about calls that burned hundreds of thousands of tokens - in the one
    tool built to answer what this costs.
    """
    calls = int(row.get("calls") or 0)
    reported = row.get("cost_reported_calls")
    total = float(row.get("total_cost_usd") or 0)
    if reported is None:
        # An older collector that does not report coverage. Say so rather than
        # implying the zero is measured.
        return f"${total:.4f}" if total else "cost not reported"
    reported = int(reported)
    if reported == 0:
        return "cost not reported"
    if reported < calls:
        return f"${total:.4f} ({reported}/{calls} reported)"
    return f"${total:.4f}"


def mcp_result(payload: Any) -> dict[str, Any]:
    """Wrap a tool payload. `structuredContent` must be a JSON object.

    MCP requires an object there, not an array or a scalar, so a tool that
    returns a bare list produces a response a strict client rejects. Refusing
    here surfaces it as a tool error during development instead of shipping an
    invalid response; every tool names its collection instead.
    """
    if not isinstance(payload, dict):
        raise TypeError(
            f"MCP structuredContent must be a JSON object, got {type(payload).__name__}; "
            "name the collection, e.g. {\"runs\": [...]}"
        )
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
                "protocolVersion": request.get("params", {}).get("protocolVersion", fusion_mcp.MCP_PROTOCOL_VERSION),
                "capabilities": fusion_mcp.SERVER_CAPABILITIES,
                "serverInfo": {"name": "fusion", "version": "0.1.0"},
            }
        elif method == "tools/list":
            response = {"tools": tool_definitions()}
        elif method == "prompts/list":
            response = {"prompts": fusion_mcp.PROMPTS}
        elif method == "prompts/get":
            params = request.get("params") or {}
            try:
                response = fusion_mcp.prompt_messages(params.get("name"), params.get("arguments") or {}, workspace)
            except Exception as exc:
                response = mcp_error(str(exc))
        elif method == "resources/list":
            response = {"resources": fusion_mcp.list_resources(workspace)}
        elif method == "resources/templates/list":
            response = {"resourceTemplates": fusion_mcp.RESOURCE_TEMPLATES}
        elif method == "resources/read":
            try:
                response = fusion_mcp.read_resource((request.get("params") or {}).get("uri", ""), workspace)
            except Exception as exc:
                response = mcp_error(str(exc))
        elif method == "tools/call":
            params = request.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                if name == "fusion_status":
                    payload = {"runs": store.recent(int(args.get("limit", 10)))}
                elif name == "fusion_decisions":
                    from fusion_decisions import DecisionStore
                    payload = {"decisions": DecisionStore(workspace).records()[-max(1, min(50, int(args.get("limit", 10)))):]}
                elif name == "fusion_outcome":
                    if not isinstance(args.get("accepted"), bool):
                        raise ValueError("accepted must be true or false")
                    payload = record_outcome(workspace, str(args.get("run_id", "")), args["accepted"], str(args.get("reason", "")))
                elif name == "fusion_delegate":
                    agent = args.get("agent")
                    if agent not in {"auto", "codex", "claude", "agy", "grok"}:
                        raise ValueError("agent must be auto, codex, claude, agy, or grok")
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
                        settings_overrides=choice_overrides(args.get("model"), args.get("reasoning_effort")),
                    )
                    if not task["task"]:
                        raise ValueError("task is required")
                    payload = dispatch(target_config, task, target_store)
                else:
                    payload = fusion_mcp.dispatch_async_tool(name, args, workspace)
                response = mcp_result(payload)
                if isinstance(payload, dict) and payload.get("workflow_id"):
                    # Hand back evidence URIs rather than inlining artifacts.
                    response["content"] = response["content"] + fusion_mcp.evidence_links(
                        workspace, str(payload["workflow_id"])
                    )
            except Exception as exc:  # MCP must return a tool error instead of corrupting stdout.
                response = mcp_error(str(exc))
        elif method == "ping":
            response = {}
        if response is not None and request_id is not None:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": response}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


LEAD_PROMPT = """You are the lead agent in a Fusion harness. Own the user conversation, the plan, ambiguity, and final judgment. Use fusion_delegate for bounded work that benefits from a fresh context or a cheaper sidekick. Send a precise brief with success criteria and constraints. Keep writes single-threaded in the shared workspace. Review the returned handoff, inspect the diff and tests yourself, and take control back when the sidekick is out of depth. Do not delegate the final decision or silently accept an unverified result. After you inspect a delegated result, record your verdict with fusion_outcome."""


BUILD_PROMPT = """You are the lead for a Fusion feature build. Take the user's idea through requirements, implementation, verification, and review fixes. The user supplies the outcome; you own the coordination. Follow the repository instructions.

1. Inspect the repository, existing behavior, and tests. Infer implementation details from its conventions. Turn the idea into a concise feature brief with observable acceptance criteria, scope, edge cases, and a small implementation plan. Save the brief and plan in a new directory under .fusion/builds/ so they remain available during the build.
2. Ask only when a missing product decision materially changes behavior or scope, using at most three focused questions at a time. State reasonable assumptions and proceed when the scope is clear. Do not require the user to write a specification, select workers, or approve routine implementation decisions.
3. Use fusion_delegate for a bounded investigation when it helps and for an independent review of the implementation. Choose an available Claude, Codex, or agy worker; prefer a different agent for review. Give each worker precise questions, success criteria, constraints, and the relevant brief or artifact paths. Set write=false for investigations and reviews. Tell workers not to delegate further. If a worker is unavailable, continue with another available worker or do the work yourself and disclose the missing independent review. Do not repeatedly retry an unavailable provider.
4. Implement the feature across the relevant layers, including user-facing states and failure behavior where applicable. Keep the change scoped to the brief and preserve unrelated work. Keep one writer in the workspace at a time, including yourself: wait for any delegated writer before editing. Do not stop after planning or scaffolding.
5. Run meaningful verification for the acceptance criteria and the repository's required checks. Add tests for changed behavior where useful. Have the reviewer inspect the actual diff and verification evidence. Verify review findings yourself, fix real issues, and rerun affected checks. Continue until the acceptance criteria are met or a concrete blocker prevents progress.
6. Keep the user informed with concise progress updates. Finish with what shipped, what was tested, any remaining blockers, and how to try the feature. Never claim an unrun check passed or an unresolved requirement is complete.

Feature idea:
"""


def mcp_config_file(workspace: Path, read_only: bool = False) -> tuple[tempfile.NamedTemporaryFile, Path]:
    script = Path(__file__).resolve().parent / "fusion"
    config = {
        "mcpServers": {
            "fusion": {
                "command": sys.executable,
                "args": [str(script), "mcp-serve"],
                "env": {"FUSION_WORKSPACE": str(workspace), **({"FUSION_READ_ONLY": "1"} if read_only else {})},
            }
        }
    }
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="fusion-mcp-", delete=False, encoding="utf-8")
    json.dump(config, handle)
    handle.write("\n")
    handle.flush()
    return handle, Path(handle.name)


def launch_lead(workspace: Path, config: dict[str, Any], agent: str, task: str | None, interactive: bool = True, read_only: bool = False) -> int:
    yolo = execution_mode(config) == "yolo"
    if agent == "claude":
        settings = config["claude"]
        command = executable(settings.get("command", "claude"))
        if not command:
            print("fusion: claude is not available on PATH", file=sys.stderr)
            return 127
        handle, config_path = mcp_config_file(workspace, read_only)
        handle.close()
        argv = [command, "--mcp-config", str(config_path), "--append-system-prompt", LEAD_PROMPT]
        if yolo:
            argv += ["--dangerously-skip-permissions", "--settings", '{"sandbox":{"enabled":false}}']
        elif read_only and interactive:
            argv += ["--permission-mode", "plan"]
        if not interactive:
            argv += ["-p", "--output-format", "json"]
            if not yolo:
                argv += ["--permission-mode", "plan" if read_only else settings.get("permission_mode", "acceptEdits")]
            permission_prompts = settings.get("permission_prompts")
            if permission_prompts and not yolo:
                argv += ["--permission-prompts", permission_prompts]
        if task:
            argv += [task]
        try:
            env = os.environ.copy()
            env["FUSION_WORKSPACE"] = str(workspace)
            if read_only:
                env["FUSION_READ_ONLY"] = "1"
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
        permissions = ["--dangerously-bypass-approvals-and-sandbox"] if yolo else [
            *codex_permission_args(workspace, settings, not read_only), "-a", "never" if read_only else "on-request"]
        argv = [command, "-C", str(workspace), *permissions, "-c", f"mcp_servers.fusion.command={json.dumps(sys.executable)}", "-c", f"mcp_servers.fusion.args={args_toml}"]
        argv += ["-c", f"mcp_servers.fusion.env.FUSION_WORKSPACE={json.dumps(str(workspace))}"]
        if read_only:
            argv += ["-c", 'mcp_servers.fusion.env.FUSION_READ_ONLY="1"']
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
        if read_only:
            env["FUSION_READ_ONLY"] = "1"
        return subprocess.run(argv, cwd=workspace, env=env, check=False).returncode
    if agent == "agy":
        raise SystemExit("fusion: agy can be a sidekick, workflow node, or Ultra harness, but not (yet) the interactive lead")
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
    for agent in ("claude", "codex", "agy", "grok"):
        command = config.get(agent, {}).get("command", agent)
        path = executable(command)
        checks.append(
            {
                "agent": agent,
                "command": command,
                "path": path,
                "ok": bool(path),
                "required": agent in {"claude", "codex"},
                "execution_mode": execution_mode(config),
                **({"headless": worker_availability(config, agent)} if agent == "agy" and path else {}),
            }
        )
    route_checks = []
    for name, route in (config.get("routes") or {}).items():
        if not isinstance(route, dict):
            route_checks.append({"route": name, "ok": False, "error": "route must be an object"})
            continue
        agent = str(route.get("agent") or "")
        command = str(route.get("command") or config.get(agent, {}).get("command") or agent)
        path = executable(command)
        route_checks.append(
            {
                "route": name,
                "agent": agent,
                "command": command,
                "path": path,
                "ok": bool(path),
                **({"error": "command is not executable"} if not path else {}),
            }
        )
    payload = {"workspace": str(workspace), "config": redact(config), "checks": checks, "route_checks": route_checks}
    print(json_text(payload))
    return 0 if all(item["ok"] for item in checks if item["required"]) and all(item["ok"] for item in route_checks) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fusion", description="Lead/sidekick orchestration for Claude Code, Codex CLI, and Antigravity CLI")
    parser.add_argument("--workspace", help="workspace to operate in; defaults to the current directory")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    display = parser.add_mutually_exclusive_group()
    display.add_argument("--progress", dest="progress", action="store_true", default=None, help="show live progress on stderr, including when redirected")
    display.add_argument("--quiet", dest="progress", action="store_false", help="hide progress; keep the final result")
    sub = parser.add_subparsers(dest="command", required=True)
    from fusion_truffle import add_parser as truffle_parser
    truffle_parser(sub)

    ui = sub.add_parser("ui", help="open the local ORC/Fusion control room in your browser")
    ui.add_argument("--port", type=int, default=8765, help="local port (0 selects an available port)")
    ui.add_argument("--no-open", action="store_true", help="print the URL without opening a browser")

    lead = sub.add_parser("lead", help="launch an interactive lead agent with the Fusion MCP server")
    lead.add_argument("--agent", choices=["claude", "codex"], help="lead agent; defaults to .fusion.json or claude")
    lead.add_argument("task", nargs="?", help="optional initial task")

    build = sub.add_parser("build", help="turn a feature idea into an interactive build, with planning and review instructions included")
    build.add_argument("--agent", choices=["claude", "codex"], default="codex", help="lead agent (default: codex)")
    build.add_argument("--kind", choices=["discovery", "build", "debug", "review", "sweep"], help="explicit workflow; planning-only requests remain read-only")
    build.add_argument("--kind-source", choices=["user", "agent"], default="user", help=argparse.SUPPRESS)
    build.add_argument(
        "--across", action="append", metavar="DIMENSION", default=[],
        help="fan one read-only worker out per dimension, then synthesize their findings; repeat the flag "
             "or comma-separate. Implies --kind sweep.",
    )
    build_mode = build.add_mutually_exclusive_group()
    build_mode.add_argument("--plan-only", action="store_true", help="save the brief and workflow without starting coding agents")
    build_mode.add_argument("--execute", action="store_true", help="execute the generated bounded workflow instead of an interactive lead")
    build.add_argument("--budget-usd", type=float, default=0, help="stop workflow dispatches after recorded spend reaches this amount (0 disables)")
    build.add_argument("--max-attempts", type=int, default=2)
    build.add_argument("--publish", choices=["off", "manual", "auto"], help="PR publication mode; manual/auto use an isolated worktree")
    build.add_argument("--base", help="PR target branch")
    build.add_argument("--remote", help="Git publication remote")
    build_pr = build.add_mutually_exclusive_group()
    build_pr.add_argument("--draft", dest="draft", action="store_true", default=None)
    build_pr.add_argument("--ready", dest="draft", action="store_false")
    build.add_argument("--from-workflow", metavar="RUN_ID", help="use a completed workflow's numbered recommendation as the new implementation request")
    build.add_argument("--from-node", help="stage containing the recommendation; defaults to the final output")
    build.add_argument("--finding", type=int, help="recommendation number from --from-workflow")
    build.add_argument("idea", nargs="?", help="describe a feature, or add constraints to --from-workflow")

    run = sub.add_parser("run", help="launch a non-interactive lead turn with the Fusion MCP server")
    run.add_argument("--agent", choices=["claude", "codex"], help="lead agent; defaults to .fusion.json or claude")
    run.add_argument("task", help="initial task for the lead")

    delegate = sub.add_parser("delegate", help="run one bounded sidekick task")
    delegate.add_argument("--agent", choices=["auto", "claude", "codex", "agy", "grok"], required=True)
    delegate.add_argument("--role", default="implementation")
    delegate.add_argument("--read-only", action="store_true", help="give the worker a read-only workspace")
    delegate.add_argument("--fresh", action="store_true", help="start a fresh agent session")
    delegate.add_argument("--session-key", help="persistent lane name; defaults to agent:role")
    delegate.add_argument("--route", help="named route from .fusion.json, for example orc-free or orc-best")
    delegate.add_argument("--model", help="model for this task, overriding the route and agent settings")
    delegate.add_argument("--reasoning-effort", choices=sorted(EFFORTS), help="Codex, or Claude Code (low-max); requires --model")
    delegate.add_argument("--success", action="append", default=[])
    delegate.add_argument("--constraint", action="append", default=[])
    delegate.add_argument("task")

    outcome = sub.add_parser("outcome", help="record the lead's verdict on a delegated run")
    outcome.add_argument("run_id")
    verdict = outcome.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--accepted", dest="accepted", action="store_true")
    verdict.add_argument("--rejected", dest="accepted", action="store_false")
    outcome.add_argument("--reason", default="", help="what you verified; required for the verdict to become an acceptance label")

    ultra = sub.add_parser("ultra", help="run a bounded UltraCode-style explore/plan/implement/review pipeline")
    ultra.add_argument("--stages", type=int, help="maximum number of configured stages")
    ultra.add_argument("--cheap-only", action="store_true", help="force Claude stages onto the orc-free route")
    ultra.add_argument("--harness", choices=["claude", "codex", "agy", "grok"], help="run every Ultra stage through one harness")
    ultra.add_argument("task", help="task for the pipeline")

    workflow = sub.add_parser("workflow", help="run a persisted bounded Fusion DAG")
    workflow_sub = workflow.add_subparsers(dest="workflow_command", required=True)
    from fusion_publish import add_parser as publish_parser
    publish_parser(workflow_sub)
    workflow_run = workflow_sub.add_parser("run", help="validate and run a workflow JSON spec")
    workflow_run.add_argument("spec", help="path to a fusion.workflow.v1 JSON spec")
    workflow_run.add_argument("--task", help="override the task in the spec")
    workflow_resume = workflow_sub.add_parser("resume", help="resume a paused or failed workflow")
    workflow_resume.add_argument("run_id")
    workflow_resume.add_argument("--node", help="unfinished stage to retry with a different worker")
    workflow_resume.add_argument("--agent", choices=["auto", "claude", "codex", "agy", "grok"])
    workflow_resume.add_argument("--route", help="configured route to use for the selected stage")
    workflow_resume.add_argument("--max-attempts", type=int, help="new explicit attempt limit per stage")
    workflow_resume.add_argument(
        "--spec", help="re-validate against this workflow JSON instead of replaying the persisted spec unchanged"
    )
    workflow_status = workflow_sub.add_parser("status", help="show a persisted workflow manifest")
    workflow_status.add_argument("run_id")
    workflow_report = workflow_sub.add_parser(
        "report", help="show final deliverables, findings, evidence, usage and next commands"
    )
    workflow_report.add_argument("run_id")
    report_selection = workflow_report.add_mutually_exclusive_group()
    report_selection.add_argument("--node", help="show a specific stage's full answer")
    report_selection.add_argument("--all", action="store_true", help="show every stage's full answer")
    workflow_report.add_argument("--finding", type=int, help="focus on a numbered recommendation and show its implementation preparation command")
    workflow_report.add_argument("--brief", action="store_true", help="show status and summaries without the full answers")
    workflow_report.add_argument("--output", help="save the displayed report as Markdown; refuses to overwrite an existing file")
    workflow_watch = workflow_sub.add_parser("watch", help="watch saved progress without starting or restarting workers")
    workflow_watch.add_argument("run_id", nargs="?", help="workflow or build ID; defaults to the most recently started workflow/build --execute, including intake")
    workflow_watch.add_argument("--once", action="store_true", help="show a snapshot and exit")

    sub.add_parser("doctor", help="check the local CLI prerequisites")
    status = sub.add_parser("status", help="show recent runs")
    status.add_argument("--limit", type=int, default=20)
    trace = sub.add_parser("trace", help="show recent telemetry spans")
    trace.add_argument("--limit", type=int, default=100)
    usage = sub.add_parser("usage", help="summarize token usage and latency from telemetry")
    usage.add_argument("--limit", type=int, default=10000)

    telemetry = sub.add_parser("telemetry", help="local and remote telemetry configuration")
    telemetry_sub = telemetry.add_subparsers(dest="telemetry_command", required=True)
    telemetry_sub.add_parser("status", help="show what remote telemetry is configured to send, if any")
    telemetry_sub.add_parser("on", help="turn remote reporting on for this workspace")
    telemetry_sub.add_parser("off", help="turn remote reporting off for this workspace; local traces keep working")
    telemetry_report = telemetry_sub.add_parser(
        "report", help="show the usage and failure patterns this machine reported; --all needs a shared token"
    )
    telemetry_report.add_argument("--hours", type=int, default=168, help="lookback window in hours (default: 168, 7 days)")
    telemetry_report.add_argument(
        "--all", action="store_true",
        help="every install, not just this one; needs telemetry.remote.token, which only the collector's operator has",
    )

    from fusion_decision_cli import add_parser
    add_parser(sub)
    from fusion_learn_cli import add_parser as learn_parser
    learn_parser(sub)
    from fusion_gym import add_parser as gym_parser
    gym_parser(sub)
    sub.add_parser("mcp-serve", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    enabled = args.progress
    if enabled is None:
        setting = os.environ.get("FUSION_PROGRESS", "auto")
        enabled = setting == "1" or setting == "auto" and sys.stderr.isatty() and not args.json
    if args.command == "mcp-serve":
        enabled = False
    with progress.session(enabled):
        try:
            return _main(args, parser)
        except KeyboardInterrupt:
            progress.emit("fusion", "detached from watcher" if args.command == "workflow" and args.workflow_command == "watch" else "interrupted; partial logs remain in .fusion")
            return 130


def _main(args, parser) -> int:
    workspace = workspace_path(args.workspace)
    if args.command == "ui":
        from fusion_ui import serve
        return serve(workspace, args.port, not args.no_open)
    if args.command == "learn":
        # Before load_config: each ticked workspace loads its own configuration.
        from fusion_learn_cli import run as run_learn
        try:
            return run_learn(args, workspace)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            parser.error(str(exc))
    if args.command == "gym":
        # Before load_config: `gym run` loads the gym directory's own configuration.
        from fusion_gym import command as gym_command
        try:
            return gym_command(args, workspace, args.json)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            parser.error(str(exc))
    config, config_path = load_config(workspace)
    if args.command == "truffle":
        from fusion_truffle import command as truffle_command
        try:
            return truffle_command(workspace, config, args)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            parser.error(str(exc))
    if args.command in {"build", "delegate", "ultra"} or args.command == "workflow" and args.workflow_command in {"run", "resume"}:
        from fusion_decisions import config_for
        progress.emit("fusion", f"{args.command} in {workspace}; Laya {config_for(config)['mode']}")
    if args.command == "decisions":
        from fusion_decision_cli import run as run_decisions
        try:
            return run_decisions(args, workspace, config)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            parser.error(str(exc))
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
    if args.command == "telemetry":
        remote = (config.get("telemetry") or {}).get("remote") or {}
        enabled = bool(remote.get("enabled"))
        if args.telemetry_command == "report":
            if not enabled:
                print("fusion telemetry report: telemetry.remote.enabled is not set; nothing to fetch", file=sys.stderr)
                return 1
            try:
                summary = fetch_remote_summary(remote, args.hours, every_install=args.all)
            except urllib.error.HTTPError as exc:
                detail = (
                    "the collector refused the shared token in telemetry.remote.token"
                    if args.all else
                    "the collector refused this request; reading your own rows should need no credential"
                ) if exc.code == 401 else f"the collector returned {exc.code}"
                print(f"fusion telemetry report: {detail}", file=sys.stderr)
                return 1
            except ValueError as exc:
                # Raised locally, before any request -- saying the collector is
                # unreachable would send someone debugging the network.
                print(f"fusion telemetry report: {exc}", file=sys.stderr)
                return 1
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                print(f"fusion telemetry report: could not reach the collector: {exc}", file=sys.stderr)
                return 1
            if args.json:
                print(json_text(summary))
            else:
                print(f"telemetry report: last {summary.get('window_hours')}h, {summary.get('total_spans')} calls from {summary.get('unique_installs')} installs")
                for row in summary.get("by_group", []):
                    label = f"{row.get('agent')}/{row.get('route')}/{row.get('model')}"
                    print(
                        f"  {label}: {row.get('status')}"
                        + (f" ({row.get('failure_class')})" if row.get("failure_class") else "")
                        + f" -- {row.get('calls')} calls, {_reported_cost(row)}, "
                        + f"avg {row.get('avg_duration_ms', 0):.0f}ms"
                    )
            return 0
        if args.telemetry_command in {"on", "off"}:
            wanted = args.telemetry_command == "on"
            path = set_remote_telemetry(workspace, wanted)
            state = "on" if wanted else "off"
            print(f"remote telemetry {state} for this workspace ({path})"
                  + ("" if wanted else "; local traces keep working"))
            return 0
        payload = {
            "local_enabled": (config.get("telemetry") or {}).get("enabled", True),
            "local_path": str(workspace / ".fusion" / "traces.jsonl"),
            "remote_enabled": enabled,
            "remote_token_configured": bool(remote.get("token")),
            "remote_endpoint": remote.get("endpoint") or None,
            "install_id": telemetry_install_id() if enabled else None,
            "fields_sent": (
                ["trace_id", "span_id", "parent_span_id", "agent", "role", "route", "model",
                 "write", "status", "failure_class", "start_time_ms", "end_time_ms",
                 "duration_ms", "usage"]
                if enabled else []
            ),
            "fields_never_sent": ["changed", "tests", "blockers", "artifacts", "workspace path", "prompt/task text", "model output"],
        }
        print(json_text(payload))
        return 0
    if args.command == "ultra":
        result = run_ultra(workspace, config, args.task, args.stages, args.cheap_only, args.harness)
        print_ultra_result(result, args.json)
        return 0 if result["status"] == "success" else 1
    if args.command == "workflow":
        from fusion_workflow import resume_workflow, run_workflow, workflow_report, workflow_status

        try:
            if args.workflow_command == "watch":
                return progress.watch_workflow(workspace, args.run_id, as_json=args.json, once=args.once)
            if args.workflow_command == "run":
                result = run_workflow(workspace, config, Path(args.spec).expanduser().resolve(), args.task)
            elif args.workflow_command == "publish":
                from fusion_publish import command
                result = command(workspace, config, args)
            elif args.workflow_command == "resume":
                spec_path = Path(args.spec).expanduser().resolve() if getattr(args, "spec", None) else None
                result = resume_workflow(workspace, config, args.run_id, spec_path, args.node, args.agent, args.route, args.max_attempts)
            elif args.workflow_command == "report":
                from fusion_report import format_report, select_report
                result = workflow_report(workspace, args.run_id)
                result = select_report(result, node=args.node, finding=args.finding, all_nodes=args.all)
                rendered_report = format_report(result, brief=args.brief)
                if args.output:
                    output = Path(args.output).expanduser().resolve()
                    with open(output, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
                        stream.write(rendered_report)
                    result["report_artifact"] = str(output)
                    if not args.json:
                        print(f"Saved report: {output}", file=sys.stderr)
            else:
                result = workflow_status(workspace, args.run_id)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            if args.json:
                print(json_text({"schema": "fusion.workflow.v1", "status": "error", "error": str(exc)}))
            else:
                print(f"fusion workflow: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json_text(result))
        elif args.workflow_command == "publish":
            print(result.get("url") or json_text(result))
        elif args.workflow_command == "report":
            print(rendered_report, end="")
        elif args.workflow_command in {"run", "resume"}:
            progress.print_workflow(result)
        else:
            workflow_id = result.get("workflow_id") or getattr(args, "run_id", "unknown")
            print(f"workflow {workflow_id}: {result.get('status', 'unknown')}")
            if result.get("artifacts", {}).get("manifest"):
                print(f"  manifest: {result['artifacts']['manifest']}")
            for problem in result.get("acceptance", {}).get("problems", []):
                print(f"  acceptance: {problem}")
        status = result.get("status")
        if result.get("publication", {}).get("status") == "failed":
            return 1
        return 0 if status in {"success", "published", "preview"} else 2 if status in {"paused_quota", "paused_budget", "running"} else 1
    if args.command == "build":
        from fusion_publish import options as publish_options
        overrides = {key: getattr(args, arg) for key, arg in (("mode", "publish"), ("base", "base"), ("remote", "remote"), ("draft", "draft")) if getattr(args, arg) is not None}
        config = {**config, "publish": publish_options(config, overrides)}
        if args.from_workflow:
            if args.finding is None:
                parser.error("--from-workflow requires --finding NUMBER")
            from fusion_report import finding_request
            try:
                idea = finding_request(workspace, args.from_workflow, args.finding, args.from_node)
            except (OSError, ValueError, RuntimeError) as exc:
                parser.error(str(exc))
            args.idea = idea + (f"\nAdditional user constraints: {args.idea}" if args.idea else "")
            if not args.kind:
                args.kind, args.kind_source = "build", "default"
        elif args.finding is not None or args.from_node:
            parser.error("--finding and --from-node require --from-workflow")
        if not args.idea or not args.idea.strip():
            parser.error("feature idea must not be empty")
        from fusion_build import prepare, run_prepared
        try:
            prepared = prepare(workspace, config, args.idea, args.kind, args.budget_usd, args.max_attempts,
                               execute=args.execute, across=args.across, kind_source=args.kind_source if args.kind else None)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            parser.error(str(exc))
        if args.plan_only:
            print(json_text(prepared))
            return 0
        if args.execute:
            result = run_prepared(workspace, config, prepared)
            if args.json:
                print(json_text(result))
            else:
                progress.print_workflow(result)
            return 0 if result["status"] == "success" and result.get("publication", {}).get("status") != "failed" else 1
        scope = "This request is read-only. Investigate or review, and do not implement.\n" if prepared["read_only"] else ""
        prompt = scope + BUILD_PROMPT + args.idea + f"\nRead the complete request and brief: {prepared['brief']}\nPrepared workflow: {prepared['workflow']}"
        return launch_lead(workspace, config, args.agent, prompt, interactive=True, read_only=prepared["read_only"])
    if args.command in {"lead", "run"}:
        lead = args.agent or config.get("lead", "claude")
        return launch_lead(workspace, config, lead, args.task, interactive=args.command == "lead")
    if args.command == "outcome":
        print(json_text(record_outcome(workspace, args.run_id, args.accepted, args.reason)))
        return 0
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
            settings_overrides=choice_overrides(args.model, args.reasoning_effort),
        )
        result = dispatch(config, task, RunStore(workspace))
        print_result(result, args.json)
        return 0 if result["status"] == "success" else 1
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
