"""Explicit model/effort pairs for native Codex and Claude Code; no runtime or routing authority."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
# `claude --effort` accepts exactly these (Claude Code 2.1.x); `agy --effort` low..high.
CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
AGY_EFFORTS = frozenset({"low", "medium", "high", "max"})
# `opencode run --variant` is provider-specific (e.g. Anthropic high/max,
# OpenAI minimal..xhigh); OpenCode itself validates the pair at request time.
OPENCODE_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
HARNESS_EFFORTS = {"codex": EFFORTS, "claude": CLAUDE_EFFORTS, "agy": AGY_EFFORTS, "opencode": OPENCODE_EFFORTS}


def claude_choice(settings):
    """Claude Code's JSON result does not report the applied effort, so the
    request is recorded and the rest stays unobserved rather than assumed.
    Claude Code's model docs: Haiku takes no effort; 4.6 models lack xhigh and
    fall back to the highest supported level below it."""
    pair = validate_pair(settings.get("model"), settings.get("reasoning_effort"))
    if pair["reasoning_effort"] not in CLAUDE_EFFORTS:
        raise ValueError("Claude Code accepts reasoning_effort " + ", ".join(sorted(CLAUDE_EFFORTS)))
    if "haiku" in pair["model"].lower():
        raise ValueError(f"{pair['model']} does not support reasoning effort in Claude Code")
    downgrade = pair["reasoning_effort"] == "xhigh" and "4-6" in pair["model"]
    return recorded_choice(pair, {"status": "requested_may_downgrade", "reason": "4.6 models run xhigh as high"} if downgrade else
                                 {"status": "unchecked", "reason": "Claude Code publishes no per-model effort catalog"})


def recorded_choice(pair, catalog):
    """A requested pair a harness cannot report back: recorded, never claimed."""
    return {"schema": "fusion.execution-choice.v1", "requested": pair, "dispatch": {"status": "prepared"},
            "catalog": catalog,
            "observed": {"model": None, "reasoning_effort": None, "status": "unobserved"},
            "native_delegation": {"allowed": False, "child_trace_visibility": "unobserved"},
            "granularity": "worker_invocation"}


def validate_pair(model, effort):
    if not isinstance(model, str) or not model.strip():
        raise ValueError("reasoning_effort requires an explicit model")
    if not isinstance(effort, str) or effort not in EFFORTS:
        raise ValueError("unsupported reasoning_effort")
    return {"model": model, "reasoning_effort": effort}


def native_capability(pair):
    """Read local Codex metadata only. A cache is evidence, not live attestation.

    Missing metadata is explicitly unchecked: Codex remains responsible for
    validating its live capabilities. A known unsupported pair is refused.
    No network, auth reads, subprocess, or fabricated model capability table.
    """
    path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json"
    try:
        with path.open("rb") as handle:
            raw = handle.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError("native catalog exceeds limit")
        value = json.loads(raw)
        models = value.get("models")
        if not isinstance(models, list):
            raise ValueError("native catalog has no model list")
        model = next((item for item in models if isinstance(item, dict) and item.get("slug") == pair["model"]), None)
        evidence = {"source": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                    "fetched_at": value.get("fetched_at"), "client_version": value.get("client_version")}
        if model is None:
            return {**evidence, "status": "unchecked", "reason": "model absent from cached native catalog"}
        levels = model.get("supported_reasoning_levels")
        if not isinstance(levels, list) or not levels or any(not isinstance(item, dict) or not isinstance(item.get("effort"), str) for item in levels):
            raise ValueError("native catalog has no usable reasoning capability metadata")
        supported = [item["effort"] for item in levels]
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {"status": "unchecked", "source": str(path), "reason": str(exc)}
    if pair["reasoning_effort"] not in supported:
        raise ValueError(f"{pair['model']} does not support reasoning_effort={pair['reasoning_effort']} in the cached native catalog")
    return {**evidence, "status": "supported_in_cache", "supported_efforts": supported}


def execution_choice(settings):
    model, effort = settings.get("model") or None, settings.get("reasoning_effort")
    delegation = settings.get("allow_native_delegation", False)
    if not isinstance(delegation, bool):
        raise ValueError("allow_native_delegation must be a boolean")
    if effort == "ultra" and not delegation:
        raise ValueError("ultra requires explicit allow_native_delegation")
    capability = native_capability(validate_pair(model, effort)) if effort is not None else {
        "status": "unchecked", "reason": "effort inherited from native defaults; not pinned"}
    return {"schema": "fusion.execution-choice.v1", "requested": {"model": model, "reasoning_effort": effort},
            "dispatch": {"status": "prepared"}, "catalog": capability,
            "observed": {"model": None, "reasoning_effort": None, "status": "unobserved"},
            "native_delegation": {"allowed": delegation, "child_trace_visibility": "unobserved"},
            "granularity": "worker_invocation"}


def check_pair(agent, settings, write=False):
    """Refuse a pair the harness cannot run; the harness decides what it accepts."""
    pair = validate_pair(settings.get("model"), settings.get("reasoning_effort"))
    if agent not in HARNESS_EFFORTS:
        raise ValueError("reasoning_effort is supported for native Codex, Claude Code, agy and OpenCode")
    if pair["reasoning_effort"] not in HARNESS_EFFORTS[agent]:
        raise ValueError(f"{agent} accepts reasoning_effort " + ", ".join(sorted(HARNESS_EFFORTS[agent])))
    if agent == "codex":
        native_capability(pair)
    elif agent == "claude":
        claude_choice(settings)
    return pair


def pair_candidates(config, settings, write=False, agent="codex"):
    """Explicit alternatives for the existing router's advisory question.

    Entries may name their harness with "agent" (default codex); only pairs for
    the task's own harness are candidates, since the lane itself is pinned."""
    values = (config.get("decisions") or {}).get("model_effort_pairs") or []
    if not isinstance(values, list) or len(values) > 8:
        raise ValueError("decisions.model_effort_pairs must be a list of at most eight pairs")
    for value in values:
        if not isinstance(value, dict) or not {"model", "reasoning_effort"} <= set(value) <= {"model", "reasoning_effort", "agent"}:
            raise ValueError("model_effort_pairs entries require model and reasoning_effort, and optionally agent")
    values = [value for value in values if value.get("agent", "codex") == agent]
    if not values:
        return []
    selected = validate_pair(settings.get("model"), settings.get("reasoning_effort"))
    pairs = []
    for value in values:
        pair = check_pair(agent, value, write)
        if pair["reasoning_effort"] == "ultra" and (write or settings.get("allow_native_delegation") is not True):
            continue
        if pair not in pairs:
            pairs.append(pair)
    if selected not in pairs:
        # The list is Laya's advisory menu, not an allowlist: a pin outside it
        # runs as pinned and simply gets no advice.
        return []
    pairs.remove(selected)
    return [selected, *pairs]


def pair_key(pair):
    return "pair-" + hashlib.sha256(json.dumps(pair, sort_keys=True).encode()).hexdigest()[:16]
