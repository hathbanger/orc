"""Fusion's deterministic constraints around optional learned decisions."""
from __future__ import annotations

from collections import defaultdict
import json
import math
import os
from pathlib import Path
import random
import time

from fusion_decisions import (DecisionEngine, DecisionStore, ACCEPTANCE_QUESTIONS, RECOVERY_QUESTIONS, REVIEW_QUESTIONS,
                              acceptance_state, normalize_role, read_jsonl, state_cap)
import fusion_progress as progress
import fusion_usage as usage


def context(task):
    role = normalize_role(task.get("role"))
    return {"task_id": task["run_id"], "group": task.get("parent_task_id") or task["run_id"],
            **({"role": role} if role else {})}


PRIOR_WEIGHT = 0.5
PRIOR_CAP = 10
_PRIORS_CACHE = {}


def prior_settings(config):
    """`decisions.priors`: {path, weight, cap}, or None when set to false.

    `weight` pseudo-counts per gym attempt (default 0.5: a gym task is ORC's
    own repository with hidden tests, not this workspace's work, so it is
    worth half a local verified outcome) and at most `cap` per lane and work
    class (default 10: a lane's gym record never outweighs ten local
    outcomes, so local evidence dominates as it accumulates)."""
    import fusion_gym
    value = (config.get("decisions") or {}).get("priors", {})
    if value is False:
        return None
    if not isinstance(value, dict):
        raise ValueError("decisions.priors must be an object ({path, weight, cap}) or false")
    settings = {"path": str(Path(value["path"]).expanduser()) if value.get("path") else str(fusion_gym.default_priors_path()),
                "weight": value.get("weight", PRIOR_WEIGHT), "cap": value.get("cap", PRIOR_CAP)}
    for name in ("weight", "cap"):
        if isinstance(settings[name], bool) or not isinstance(settings[name], (int, float)) or settings[name] < 0:
            raise ValueError(f"decisions.priors.{name} must be a non-negative number")
    return settings


def load_priors(settings):
    """The index of an exported priors file, or None when there is none.

    `exact` keys entries by (agent, route, model, reasoning_effort). `lane`
    pools entries by (agent, model, reasoning_effort): a route name is a
    workspace-local label, and a route that pins the same agent, model and
    effort as a gym lane runs the same lane."""
    import fusion_gym
    if not settings:
        return None
    path = Path(settings["path"])
    try:
        stat = path.stat()
    except OSError:
        return None
    stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    if stamp in _PRIORS_CACHE:
        return _PRIORS_CACHE[stamp]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read lane priors {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != fusion_gym.PRIORS_SCHEMA or not isinstance(value.get("priors"), dict):
        raise ValueError(f"{path} is not a {fusion_gym.PRIORS_SCHEMA} file; re-export it with `fusion gym priors`")
    exact, lane = {}, {}
    for key, entry in value["priors"].items():
        agent, route, model, effort = fusion_gym.lane_tuple(entry)
        exact[(agent, route, model, effort)] = {**entry, "key": key}
        pooled = lane.setdefault((agent, model, effort), {"key": []})
        pooled["key"].append(key)
        for work in sorted(set(fusion_gym.WORK_CLASSES.values()) | {"interpret"}):
            stats = entry.get(work)
            if not stats or not stats.get("attempts"):
                continue
            into = pooled.setdefault(work, {"attempts": 0, "successes": 0, "mean_cost_usd": 0.0, "mean_seconds": 0.0,
                                            "generated_at": stats.get("generated_at")})
            total = into["attempts"] + stats["attempts"]
            for mean in ("mean_cost_usd", "mean_seconds"):
                into[mean] = round((into[mean] * into["attempts"] + (stats.get(mean) or 0) * stats["attempts"]) / total, 4)
            into["attempts"], into["successes"] = total, into["successes"] + stats["successes"]
    for pooled in lane.values():
        pooled["key"] = "+".join(sorted(pooled["key"]))
    index = {"path": str(path), "generated_at": value.get("generated_at"), "exact": exact, "lane": lane}
    _PRIORS_CACHE.clear()
    _PRIORS_CACHE[stamp] = index
    return index


def prior_for(index, candidate, work, weight, cap):
    """Pseudo-counts for one candidate and prior class ("write", "read" or "interpret"):
    min(weight * gym attempts, cap) attempts at the gym's success rate. The
    exact (agent, route, model, effort) entry first, else the lane pooled
    across route names."""
    if not index:
        return {"prior_attempts": 0}
    agent, model, effort = candidate["agent"], candidate.get("model") or None, candidate.get("reasoning_effort") or None
    match, entry = "exact", index["exact"].get((agent, candidate.get("route") or None, model, effort))
    if not ((entry or {}).get(work) or {}).get("attempts"):
        match, entry = "lane", index["lane"].get((agent, model, effort))
    stats = (entry or {}).get(work) or {}
    if not stats.get("attempts"):
        return {"prior_attempts": 0}
    attempts = min(weight * stats["attempts"], cap)
    return {"prior_attempts": round(attempts, 3),
            "prior_successes": round(attempts * stats["successes"] / stats["attempts"], 3),
            "prior": {"key": entry["key"], "match": match, "class": work, "gym_attempts": stats["attempts"],
                      "gym_successes": stats["successes"], "mean_cost_usd": stats.get("mean_cost_usd"),
                      "mean_seconds": stats.get("mean_seconds"), "source": "gym",
                      "generated_at": stats.get("generated_at") or index.get("generated_at")}}


def priors_policy(config):
    """What the routing log records about priors: None when none applied."""
    settings = prior_settings(config)
    index = load_priors(settings)
    return {**settings, "generated_at": index["generated_at"]} if index else None


def quota_settings(config):
    settings = {"pace_margin": .15, "soft": .85, "hard": .97, **(config.get("quota") or {})}
    for name in ("pace_margin", "soft", "hard"):
        value = settings[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"quota.{name} must be a fraction between 0 and 1")
    if settings["soft"] > settings["hard"]:
        raise ValueError("quota.soft must not exceed quota.hard")
    return settings


def quota_assessment(observation, thresholds, now):
    """Expired windows impose no constraint; unknown duration disables pacing."""
    quota = observation["quota"]
    reasons, tight, exhausted = [], False, False
    windows = {}
    # A rejection lasts until the most-used window resets, not the longest one.
    timed = [(name, window) for name, window in quota["windows"].items() if usage.timestamp(window.get("resets_at"))]
    binding = min(timed, key=lambda item: (-(item[1].get("used") if item[1].get("used") is not None else -1),
                                           usage.timestamp(item[1]["resets_at"]).timestamp()), default=(None, None))[0]
    for name, window in quota["windows"].items():
        reset = usage.timestamp(window.get("resets_at"))
        active = reset is None or reset.timestamp() > now
        minutes = window.get("window_minutes") or {"five_hour": 300, "seven_day": 10080}.get(name)
        elapsed = max(0, min(1, 1 - (reset.timestamp() - now) / (minutes * 60))) if reset and minutes else None
        windows[name] = {**window, "active": active, "elapsed": elapsed}
        if not active:
            continue
        if quota.get("status") == "rejected" and name == binding:
            exhausted = True
            reasons.append(f"{name}: rejected until {usage.iso(reset)}")
        used = window.get("used")
        if used is None:
            continue
        if used > thresholds["hard"]:
            exhausted = True
            reasons.append(f"{name}: used {used:.3f} exceeds hard {thresholds['hard']:.3f}")
        if used > thresholds["soft"]:
            tight = True
            reasons.append(f"{name}: used {used:.3f} exceeds soft {thresholds['soft']:.3f}")
        if elapsed is not None and used > elapsed + thresholds["pace_margin"]:
            tight = True
            reasons.append(f"{name}: used {used:.3f} exceeds elapsed {elapsed:.3f} + pace_margin {thresholds['pace_margin']:.3f}")
    return {"lane_key": observation["lane_key"], "windows": windows, "status": quota.get("status"),
            "classification": "exhausted" if exhausted else "tight" if tight else "available",
            "reasons": reasons or ["within quota thresholds" if any(w["active"] for w in windows.values()) else "no active quota windows"],
            "thresholds": thresholds, "observed_at": observation.get("observed_at"), "source": observation.get("source")}


def rank_by_quota(candidates):
    # All candidates in a routing decision have already passed this work class's
    # eligibility checks. Stable sorting preserves every existing tie breaker.
    return sorted(candidates, key=lambda c: c.get("quota", {}).get("classification") == "tight")


def role_prior_class(work, role):
    return "interpret" if work == "read" and role and "interpret" in role else work


def spent_last_day(store, key):
    """Reported USD of this route's runs that ended in the last 24 hours."""
    since = time.time() * 1000 - 86400 * 1000
    total = 0.0
    for span in store.traces(limit=5000):
        if (span.get("route") or span.get("agent")) == key and (span.get("end_time_ms") or 0) >= since:
            cost = (span.get("usage") or {}).get("cost_usd")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                total += cost
    return total


def overflow_only(config, choices, drop):
    """`decisions.overflow_routes` are candidates only when no other lane is:
    they carry automatic work when the primary lanes are excluded or cooling down."""
    overflow = set((config.get("decisions") or {}).get("overflow_routes") or [])
    if not overflow or not any(c["route"] not in overflow for c in choices):
        return choices
    for c in choices:
        if c["route"] in overflow:
            drop(c["key"], "overflow lane: used only when no primary lane is available")
    return [c for c in choices if c["route"] not in overflow]


def route_candidates(config, task, store, rejected=None, quota_audit=None, minimum=None):
    """Pass `rejected` to collect why each lane was dropped. The reasons live
    beside the checks that produce them so an explanation can never drift from
    the filter it is explaining."""
    import fusion_core as core

    def drop(key, why):
        if rejected is not None:
            rejected.setdefault(key, why)
    yolo = core.execution_mode(config) == "yolo"
    priors = prior_settings(config)
    index = load_priors(priors)
    work = "write" if task.get("write") else "read"
    role = normalize_role(task.get("role"))
    if minimum is None:
        ranking = (config.get("decisions") or {}).get("rank_by_outcomes")
        minimum = int(ranking) if ranking and not isinstance(ranking, bool) else 3
    ttl_ms = core.cache_settings(config)["ttl_seconds"] * 1000
    now = core.now_ms()
    thresholds = quota_settings(config)
    headroom = {entry["lane_key"]: quota_assessment(entry, thresholds, now / 1000)
                for entry in usage.headroom(store.workspace, include_raw=False) if entry.get("lane_key") and entry.get("quota")}
    history = defaultdict(list)
    outcomes = {run_id: event["accepted"] for run_id, event in
                effective_outcomes(read_jsonl(DecisionStore(store.workspace).path)).items()}
    unhealthy = set(task.get("excluded_routes", []))
    unavailable_commands = set()
    for excluded in unhealthy:
        lane = config.get("routes", {}).get(excluded, {})
        agent = lane.get("agent", excluded)
        settings = core.deep_merge(config.get(agent, {}), lane)
        if Path(str(settings.get("command", agent))).name != "orc" or core.route_account(settings):
            unavailable_commands.add((core.lane_key(agent, settings), settings.get("command", agent), settings.get("model") or "*"))
    seen = set()
    seen_commands = set()
    untrusted = set()
    for span in store.traces(limit=200):
        if span.get("check_inputs_changed"):
            untrusted.add(span.get("run_id"))
        key = span.get("route") or span.get("agent")
        if not key:
            continue
        history[key].append(span)
        agent = span.get("agent")
        lane = config.get("routes", {}).get(span.get("route"), {})
        settings = core.deep_merge(config.get(agent, {}), lane)
        # Quota is per account, and a plan may cap one model (Fable) while
        # another (Opus) still has room: a failure cools lanes on the same
        # account and model, or the whole account when the model is unknown.
        family = (core.lane_key(agent, settings), settings.get("command", agent), settings.get("model") or span.get("model") or "*")
        if key not in seen and 0 <= time.time() * 1000 - span.get("end_time_ms", 0) < core.LANE_COOLDOWN_SECONDS * 1000:
            if span.get("failure_class") == "quota" or (span.get("failure_class") == "permission_denied"
                                                        and span.get("execution_mode", "restricted") == core.execution_mode(config)
                                                        and core.denial_blocks_lane(span)):
                unhealthy.add(key)
                # Quota belongs to the account behind the command, so every lane on
                # it cools down. A permission denial is about what that lane's run
                # tried; other lanes on the same command stay candidates.
                if span.get("failure_class") == "quota" and family not in seen_commands \
                        and (Path(str(family[1])).name != "orc" or core.route_account(settings)):
                    unavailable_commands.add(family)
        seen.add(key)
        seen_commands.add(family)
    choices = []
    pool = auto_pool(config, task)
    automatic = task.get("agent", "auto") == "auto" and not task.get("route")
    # Every writing task needs `write`, in every execution mode, so a lane that
    # lacks it stays on reads; the blind fallback for unmet needs cannot drop it.
    needs = (set(task.get("needs") or []) | ({"write"} if task.get("write") else set())) if automatic else set()
    preferred = config.get("sidekick", "codex")
    candidates = [(name, name, None) for name in dict.fromkeys([preferred, "codex", "claude", "agy", "grok", "opencode"])]
    candidates += [(name, settings.get("agent"), name) for name, settings in config.get("routes", {}).items()]
    if task.get("prefer_different_agent"):
        candidates.sort(key=lambda item: item[1] == task["prefer_different_agent"])
    for key, agent, route in candidates:
        if pool is not None and key not in pool:
            drop(key, "not in decisions.auto_routes")
            continue
        if key in unhealthy:
            drop(key, "a recent run on this lane hit a quota or permission limit")
            continue
        if agent not in {"claude", "codex", "agy", "grok", "opencode"}:
            continue
        settings = core.agent_settings(config, {"agent": agent, "route": route})
        lacks = settings.get("lacks") or []
        if not isinstance(lacks, list) or not all(isinstance(item, str) for item in lacks):
            raise ValueError(f"{key}: lacks must be a list of capability names")
        unmet = sorted(needs & set(lacks))
        if unmet:
            drop(key, "lacks " + ", ".join(unmet) + " this task needs")
            continue
        if agent == "opencode" and not settings.get("model"):
            # OpenCode's own default model depends on user config and could be
            # any provider; automatic routing needs a lane that names one.
            drop(key, "OpenCode lanes need an explicit provider/model for automatic routing")
            continue
        quota = headroom.get(core.lane_key(agent, settings))
        if quota:
            if quota_audit is not None:
                quota_audit[key] = quota
            if quota["classification"] == "exhausted":
                drop(key, "; ".join(quota["reasons"]))
                continue
        if settings.get("reasoning_effort") is not None:
            from fusion_reasoning import native_capability, validate_pair
            try:
                if agent not in {"codex", "claude", "agy", "opencode"}:
                    raise ValueError("reasoning effort requires native Codex, Claude Code, agy or OpenCode")
                if agent in {"claude", "agy", "opencode"}:
                    from fusion_reasoning import check_pair
                    check_pair(agent, settings)
                if settings["reasoning_effort"] == "ultra" and (task.get("write") or settings.get("allow_native_delegation") is not True):
                    raise ValueError("ultra requires read-only scope and explicit native delegation")
                # OpenCode passes reasoning_effort as --variant; the provider validates
                # it at request time.  Reading Codex's local models_cache.json for an
                # OpenCode model id would always return 'unchecked' and is misleading.
                if agent != "opencode":
                    native_capability(validate_pair(settings.get("model"), settings["reasoning_effort"]))
            except ValueError as exc:
                drop(key, str(exc))
                continue
        base = (core.lane_key(agent, settings), settings.get("command", agent))
        if (*base, settings.get("model") or "*") in unavailable_commands or (*base, "*") in unavailable_commands:
            drop(key, f"{settings.get('command', agent)} is already known to be unavailable this run")
            continue
        missing = [path for path in settings.get("requires") or [] if not Path(os.path.expanduser(str(path))).exists()]
        if missing:
            drop(key, "requires " + ", ".join(str(path) for path in missing))
            continue
        budget = settings.get("daily_budget_usd")
        if budget is not None:
            spent = spent_last_day(store, key)
            if spent >= float(budget):
                drop(key, f"spent ${spent:.2f} of its ${float(budget):.2f} daily budget in the last 24 hours")
                continue
        if not core.executable(settings.get("command", agent)):
            drop(key, f"{settings.get('command', agent)} is not on PATH")
            continue
        if not yolo and agent == "agy" and settings.get("dangerously_skip_permissions") is not True:
            drop(key, "agy requires dangerously_skip_permissions for automatic restricted-mode candidacy; "
                      "sandboxed command readiness does not cover non-command tools like ViewFile")
            continue
        # Named read-only routes cannot be repurposed into writer routes.
        if not yolo and task.get("write") and (settings.get("sandbox") == "read-only" or settings.get("permission_mode") == "plan" or settings.get("mode") == "plan"):
            drop(key, "read-only or plan-mode lane cannot take a task that writes")
            continue
        if not yolo and not task.get("write") and settings.get("mode") in {"yolo", "auto", "accept-edits"}:
            drop(key, f"{settings.get('mode')} mode is too permissive for a read-only task")
            continue
        command = str(settings.get("command", agent))
        arms = max(1, int(settings.get("arms", 1)))
        if Path(command).name == "orc":
            # Automatic ORC routes require passing tool-fit evidence even for explicit models.
            model = settings.get("model")
            try:
                exclude = core.excluded_models(settings)
            except ValueError as exc:
                drop(key, str(exc))
                continue
            if model:
                if model not in core._orc_model_ids(command, ["--fit"]):
                    drop(key, f"{model} has no passing `orc probe --fit` evidence")
                    continue
                models = [model]
            else:
                try:
                    models = core.fitted_orc_models(command, str(settings.get("model_selector", "best")), arms, exclude)
                except (ValueError, RuntimeError, OSError) as exc:
                    drop(key, f"no ORC model passed tool-fit for this route ({exc})")
                    continue
            if not models:
                drop(key, "no paid ORC model has `orc probe --fit` evidence; run `orc probe --fit <model>`"
                     if settings.get("model_selector", "best") == "best" else "no ORC model passed tool-fit for this route")
                continue
        else:
            models = [settings.get("model", "")]
        # A route with arms offers each fitted model as its own candidate, with
        # its own history; one arm keeps the route's historical key and stats.
        split = arms > 1 and len(models) > 1
        for model in models:
            spans = [span for span in history[key] if not split or span.get("model") == model]
            # A success is the worker's claim until a gate or lead checks it; an
            # error is observed. Quota and permission failures are lane health
            # (cooldown), not evidence about quality.
            evidence = [(span, outcomes[span["run_id"]] if span.get("run_id") in outcomes else False) for span in spans
                        if span.get("run_id") not in untrusted and (span.get("run_id") in outcomes or
                            (span.get("status") == "error" and span.get("failure_class") not in {"quota", "permission_denied"}))]
            # Read-only and writing work differ: this task's class counts when
            # it has any evidence, else every class pooled.
            same = [ok for span, ok in evidence if "write" in span and bool(span["write"]) == (work == "write")]
            role_evidence = [ok for span, ok in evidence if role and normalize_role(span.get("role")) == role]
            evidence_scope = "role" if role and len(role_evidence) >= minimum else "work"
            verified = role_evidence if evidence_scope == "role" else same or [ok for _, ok in evidence]
            costs = [core.number(span["usage"].get("cost_usd", span["usage"].get("cost", 0))) for span in spans
                     if "cost_usd" in span.get("usage", {}) or "cost" in span.get("usage", {})]
            mean_cost = sum(costs) / len(costs) if costs else None
            # Warm and cold split by the span's own session idle time; spans
            # recorded before it existed have no key and join neither side.
            split_costs = {True: [], False: []}
            for span in spans:
                if "session_idle_s" in span and ("cost_usd" in span.get("usage", {}) or "cost" in span.get("usage", {})):
                    idle = span["session_idle_s"]
                    split_costs[idle is not None and idle * 1000 < ttl_ms].append(
                        core.number(span["usage"].get("cost_usd", span["usage"].get("cost", 0))))
            last_end = spans[0].get("end_time_ms") if spans else None
            lane_idle = round(max(0, now - last_end) / 1000, 3) if isinstance(last_end, (int, float)) and last_end > 0 else None
            arm = f"{key}:{model}" if split else key
            if task.get("budget_remaining_usd") is not None and mean_cost is not None and mean_cost > task["budget_remaining_usd"]:
                drop(arm, f"its average reported cost ${mean_cost:.4f} exceeds the ${task['budget_remaining_usd']:.4f} left in the budget")
                continue
            effort = {"reasoning_effort": settings["reasoning_effort"]} if settings.get("reasoning_effort") is not None else {}
            prior_class = role_prior_class(work, role)
            prior_candidate = {"agent": agent, "route": route, "model": model, **effort}
            prior = prior_for(index, prior_candidate, prior_class,
                              priors["weight"], priors["cap"]) if index else {"prior_attempts": 0}
            if index and prior_class != work and not prior.get("prior"):
                prior = prior_for(index, prior_candidate, work, priors["weight"], priors["cap"])
            # Gym priors are pseudo-counts: checked_runs and acceptance_rate,
            # which ranking reads, include them; the *_local fields do not.
            checked = len(verified) + prior["prior_attempts"]
            accepted = sum(verified) + prior.get("prior_successes", 0)
            choices.append({"key": arm, "agent": agent, "route": route, "model": model, **effort,
                            "cost_tier": settings.get("cost_tier"),
                            **({"quota": quota} if quota else {}),
                            "runs": len(spans), "reported_success_rate": sum(s.get("status") == "success" for s in spans) / len(spans) if spans else None,
                            "checked_runs": round(checked, 3) if prior["prior_attempts"] else checked,
                            "acceptance_rate": (round(accepted / checked, 4) if prior["prior_attempts"] else accepted / checked) if checked else None,
                            "checked_runs_local": len(verified), "acceptance_rate_local": sum(verified) / len(verified) if verified else None,
                            "evidence_scope": evidence_scope,
                            "local_class": role if evidence_scope == "role" else work if same else "pooled" if verified else None, **prior,
                            "mean_cost_usd": mean_cost,
                            "mean_cost_usd_warm": sum(split_costs[True]) / len(split_costs[True]) if split_costs[True] else None,
                            "mean_cost_usd_cold": sum(split_costs[False]) / len(split_costs[False]) if split_costs[False] else None,
                            "session_idle_s": lane_idle, "warm": lane_idle is not None and lane_idle * 1000 < ttl_ms,
                            "mean_ms": sum(s.get("duration_ms", 0) for s in spans) / len(spans) if spans else None})
    return rank_by_quota(overflow_only(config, choices, drop) if automatic else choices)


AGENTS = ("codex", "claude", "agy", "grok", "opencode")


def auto_pool(config, task):
    """The lanes automatic routing may choose, or None for every lane.

    `decisions.auto_routes` names routes (or bare agents); it binds automatic
    choices only, never a lane the task named itself."""
    pool = (config.get("decisions") or {}).get("auto_routes")
    if pool is None or task.get("agent", "auto") != "auto" or task.get("route"):
        return None
    known = set(config.get("routes", {})) | set(AGENTS)
    if not isinstance(pool, list) or not pool or not all(isinstance(name, str) and name in known for name in pool):
        raise ValueError("decisions.auto_routes must be a non-empty list of configured route or agent names")
    return set(pool)


def no_route_reason(config, task, store):
    """Say which lane was dropped and why, instead of only that none survived.

    Without this the first real failure on an installed machine is an
    implementation node reporting that no route is available, with nothing
    naming the binary that is missing or the read-only lane that cannot write.
    """
    rejected = {}
    route_candidates(config, task, store, rejected)
    detail = "; ".join(f"{key}: {why}" for key, why in sorted(rejected.items())) or "no lane is configured"
    scope = "that can write" if task.get("write") else "for a read-only task"
    return (f"no permitted worker is available {scope} -- {detail}. "
            "Run `fusion doctor` to see every lane and its command.")


def gating(task):
    """Work that ships or gates: a writer or a review."""
    return bool(task.get("write") or "review" in str(task.get("role", "")).lower())


def exploration(ranking, task, candidates):
    """(minimum, explore) for rank_by_outcomes. Unproven lanes earn evidence on
    ordinary read-only work, not on work that ships or gates: a writer or a
    review goes to a lane with verified evidence once any lane has it."""
    minimum = int(ranking) if not isinstance(ranking, bool) else 3
    return minimum, not gating(task) or not any((c.get("checked_runs") or 0) >= minimum for c in candidates)


ROUTING_RNG = random.Random()


def routing_epsilon(config):
    value = (config.get("decisions") or {}).get("routing_epsilon", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError("decisions.routing_epsilon must be a number in [0, 1]")
    return float(value)


def propensities(keys, chosen, epsilon):
    """Probability the logging policy picks each key: the ranked top with
    (1 - epsilon) + epsilon / k, every candidate with epsilon / k. A
    deterministic pick (epsilon 0) is 1.0 for `chosen`, 0.0 for the rest."""
    if not epsilon:
        return {key: float(key == chosen) for key in keys}
    share = epsilon / len(keys)
    return {key: share + (1 - epsilon if index == 0 else 0) for index, key in enumerate(keys)}


def rank_by_outcomes(candidates, minimum=3, warm_epsilon=None, explore=True, cost_epsilon=0.05):
    """Order automatic candidates by verified outcomes, not by worker self-reports.

    With `explore` (the default), a candidate with fewer than `minimum` checked
    runs is tried first, in the configured preference order, so every arm earns
    evidence. The rest follow by smoothed acceptance rate
    (accepted + 1) / (checked + 2). Stable: ties keep preference order. Only
    gate and lead outcomes count; `status: success` alone is a worker's claim.

    With `explore=False`, the tier-0 short-circuit is skipped and every
    candidate is scored by smoothed acceptance rate, so an unproven lane never
    outranks one with verified evidence -- callers pass this once a lane has
    already earned enough evidence to trust.

    With `warm_epsilon`, prompt-cache warmth breaks ties only: candidates in the
    same bucket whose smoothed rate is within epsilon of the best rate in their
    run are one tier, and a warm candidate leads its tier. Warmth never moves a
    candidate past one whose rate is more than epsilon better.

    Configured cost tiers break ties before warmth, lower first, with unset
    costs tied after configured costs. When any cost is configured, tiers use
    the wider of warm_epsilon and cost_epsilon. Exploration of unproven
    lanes also tries cheaper tiers first. Cost never moves a lane across
    evidence tiers. Quota demotion
    retains priority over all outcome, cost and warmth ordering.
    """
    def score(item):
        index, candidate = item
        checked = candidate.get("checked_runs") or 0
        if explore and checked < minimum:
            return (0, 0.0, index)
        accepted = (candidate.get("acceptance_rate") or 0) * checked
        return (1, -(accepted + 1) / (checked + 2), index)
    ranked = sorted(enumerate(candidates), key=score)
    has_cost = any(c.get("cost_tier") is not None for c in candidates)
    epsilon = warm_epsilon
    if has_cost:
        if isinstance(cost_epsilon, bool) or not isinstance(cost_epsilon, (int, float)) or not 0 <= cost_epsilon < 1:
            raise ValueError("decisions.cost_epsilon must be a number in [0, 1)")
        # Warmth and cost both only break ties, so the wider band defines a tie.
        epsilon = cost_epsilon if epsilon is None else max(epsilon, cost_epsilon)
    if epsilon is None:
        return rank_by_quota([candidate for _, candidate in ranked])
    tiers = []
    for item in ranked:
        bucket, rate, _ = score(item)
        if tiers and tiers[-1][0] == bucket and rate - tiers[-1][1] <= epsilon + 1e-9:
            tiers[-1][2].append(item[1])
        else:
            tiers.append((bucket, rate, [item[1]]))
    def tie_key(candidate, bucket):
        cost = candidate.get("cost_tier")
        cost = cost if cost is not None else math.inf
        if bucket == 0:
            return (cost, not candidate.get("warm") if warm_epsilon is not None else False)
        return (cost, not candidate.get("warm"))
    return rank_by_quota([candidate for bucket, _, tier in tiers
                          for candidate in sorted(tier, key=lambda c: tie_key(c, bucket))])


def quota_blocked(config, task, store):
    """Why the pinned lane's account cannot serve it now: exhausted quota, or a
    quota failure on the same account and model within the cooldown."""
    import fusion_core as core
    settings = core.agent_settings(config, task)
    key, model = core.lane_key(task["agent"], settings), settings.get("model")
    thresholds = quota_settings(config)
    for entry in usage.headroom(store.workspace, include_raw=False):
        if entry.get("lane_key") == key and entry.get("quota"):
            quota = quota_assessment(entry, thresholds, time.time())
            if quota["classification"] == "exhausted":
                return "; ".join(quota["reasons"])
    now = time.time() * 1000
    for span in store.traces(limit=200):
        if span.get("failure_class") != "quota" or not 0 <= now - span.get("end_time_ms", 0) < core.LANE_COOLDOWN_SECONDS * 1000:
            continue
        lane = config.get("routes", {}).get(span.get("route"), {})
        span_settings = core.deep_merge(config.get(span.get("agent"), {}), lane)
        span_model = span_settings.get("model") or span.get("model")
        if core.lane_key(span.get("agent"), span_settings) == key and (not span_model or not model or span_model == model):
            return f"a run on {key} hit its quota within the last {core.LANE_COOLDOWN_SECONDS // 60} minutes"
    return None


def quota_twin(config, task, store):
    """A pin names a model and effort, not an account. When the pinned lane's
    account cannot serve it, the same model and effort runs on an overflow route,
    which still passes every automatic check (caps, requires, its own quota)."""
    import fusion_core as core
    overflow = [name for name in (config.get("decisions") or {}).get("overflow_routes") or [] if name != task.get("route")]
    if not overflow:
        return None
    settings = core.agent_settings(config, task)
    model, effort = settings.get("model"), settings.get("reasoning_effort")
    if not model:
        return None
    twins = []
    for name in overflow:
        route = config.get("routes", {}).get(name) or {}
        if route.get("agent") != task["agent"]:
            continue
        twin = core.agent_settings(config, {"agent": task["agent"], "route": name})
        if twin.get("model") == model and twin.get("reasoning_effort") == effort \
                and core.lane_key(task["agent"], twin) != core.lane_key(task["agent"], settings):
            twins.append(name)
    if not twins:
        return None
    reason = quota_blocked(config, task, store)
    if not reason:
        return None
    pool = core.deep_merge(config, {"decisions": {"auto_routes": twins, "overflow_routes": []}})
    probe = {**task, "agent": "auto", "route": None, "settings_overrides": {}}
    eligible = [c["route"] for c in route_candidates(pool, probe, store) if c["route"] in twins]
    if not eligible:
        return None
    return {"from": task.get("route") or task["agent"], "to": eligible[0], "model": model,
            "reasoning_effort": effort, "reason": reason}


def route_task(config, task, store, rng=None):
    """Record advice for explicit routing, apply only to a genuinely automatic lane.

    Decisions and outcomes live beside `store`, which is the control workspace
    even when the worker runs in a workflow's worktree."""
    engine = DecisionEngine(store.workspace, config)
    epsilon = routing_epsilon(config)
    automatic = task["agent"] == "auto" and not task.get("route")
    if not automatic:
        twin = quota_twin(config, task, store)
        if twin:
            task["quota_twin"] = twin
            task["route"] = twin["to"]
            task["session_key"] += ":" + twin["to"]
            engine.store.append("routing_log", **context(task), scope="quota_twin", write=bool(task.get("write")),
                                chosen=twin["to"], quota_twin=twin)
    quota_audit, rejected = {}, {}
    if task["agent"] == "auto" and task.get("route"):
        task["agent"] = config.get("routes", {}).get(task["route"], {}).get("agent")
        if task["agent"] not in {"codex", "claude", "agy", "grok", "opencode"}:
            raise ValueError("automatic task specifies an unknown route")
    if not automatic and engine.options["mode"] == "off":
        return
    if automatic and task.get("settings_overrides"):
        raise ValueError("agent=auto cannot use per-agent settings overrides; configure named routes")
    # Explicit lanes are never silently substituted, even if unhealthy.
    with progress.activity(task.get("progress_label", task["role"]), "checking available workers and model fit" if automatic else "checking selected worker"):
        ranking = config.get("decisions", {}).get("rank_by_outcomes")
        import fusion_core as core
        cache = core.cache_settings(config)
        warm_epsilon = cache["warm_epsilon"] if cache["configured"] else None
        cost_epsilon = config.get("decisions", {}).get("cost_epsilon", 0.05)
        within_route = False
        minimum = explore = None
        needs_unmet = False
        if automatic:
            candidates = route_candidates(config, task, store, rejected=rejected, quota_audit=quota_audit)
            if not candidates and task.get("needs"):
                # No lane can meet the needs: run blind on the full pool rather than refuse the work.
                needs_unmet = True
                candidates = route_candidates(config, {**task, "needs": []}, store, rejected=rejected, quota_audit=quota_audit)
            if ranking:
                minimum, explore = exploration(ranking, task, candidates)
                candidates = rank_by_outcomes(candidates, minimum, warm_epsilon, explore, cost_epsilon)
                if task.get("prefer_different_agent"):
                    # Independence outranks track record: a review stays with a
                    # different harness than the implementer when one is available.
                    candidates.sort(key=lambda item: item["agent"] == task["prefer_different_agent"])
            candidates = rank_by_quota(candidates)
        else:
            from fusion_reasoning import pair_candidates, pair_key
            settings = core.agent_settings(config, task)
            pairs = pair_candidates(config, settings, task.get("write", False), task["agent"]) if task["agent"] in {"codex", "claude", "agy", "opencode"} and settings.get("reasoning_effort") is not None else []
            # A named route with several arms still chooses a model inside that
            # route; the lane itself is never substituted.
            arms = [] if pairs or not task.get("route") or settings.get("model") or int(settings.get("arms", 1)) < 2 else [
                c for c in route_candidates(config, task, store) if c["route"] == task["route"]]
            if arms and ranking:
                minimum, explore = exploration(ranking, task, arms)
                arms = rank_by_outcomes(arms, minimum, warm_epsilon, explore, cost_epsilon)
            within_route = len(arms) > 1
            candidates = arms or [{"key": pair_key(pair), "agent": task["agent"], "route": task.get("route"), **pair} for pair in pairs] or [{
                "key": task.get("route") or task["agent"], "agent": task["agent"], "route": task.get("route"),
                "model": settings.get("model", ""),
                **({"reasoning_effort": settings["reasoning_effort"]} if settings.get("reasoning_effort") is not None else {}),
            }]
            for candidate in candidates:
                candidate["cost_tier"] = settings.get("cost_tier")
        # Rank every eligible lane before bounding Laya options and the audit log.
        candidates = candidates[:8]
    if not candidates:
        if automatic and quota_audit:
            engine.store.append("routing_log", **context(task), scope="automatic", candidates=[], chosen=None,
                                quota=quota_audit, rejected=rejected, policy={"quota": quota_settings(config)})
        raise ValueError(no_route_reason(config, task, store))
    selected, applied, record = candidates[0], False, None
    # A single candidate is not a choice: nothing to record, and Laya rejects one-option choices.
    if len(candidates) > 1:
        questions = {"route": {"type": "choice", "instructions": "Choose a capable permitted worker for the goal and observed evidence; unknown metrics are unknown.",
                               "criteria": {c["key"]: f"{c['agent']} {c.get('model') or 'configured default'}" +
                                            (f" / {c['reasoning_effort']} effort" if c.get("reasoning_effort") else "") for c in candidates}}}
        if any(c.get("reasoning_effort") for c in candidates):
            questions["route"]["instructions"] = "Choose one model and effort pair for the task. Prioritize correctness; effort names are not comparable across models. Unknown outcomes, latency and cost are unknown."
        record = engine.decide("routing", {"task": task.get("decision_context", task["task"]), "write": task["write"],
                                          "goal": config.get("decisions", {}).get("routing_goal", "quality"),
                                          "budget_remaining_usd": task.get("budget_remaining_usd"), "candidates": candidates}, questions, context(task))
        if automatic and engine.allowed(record, "route"):
            value = record["recommendations"]["route"]["value"]
            selected = next(c for c in candidates if c["key"] == value)
            applied = True
    # Epsilon exploration: ordinary read-only work only, among lanes that
    # already passed every filter, never over a qualified recommendation or a
    # pinned lane, and only when the routing log that makes it useful is kept.
    scope = "automatic" if automatic else "route_arms" if within_route else None
    effective = (epsilon if scope and not applied and not gating(task) and not task.get("prefer_different_agent")
                 and engine.options["mode"] != "off" and len(candidates) > 1 else 0.0)
    explored = bool(effective) and (rng or ROUTING_RNG).random() < effective
    if explored:
        selected = candidates[(rng or ROUTING_RNG).randrange(len(candidates))]
    if within_route:
        task.setdefault("settings_overrides", {})["model"] = selected["model"]
        task["session_key"] += ":" + selected["key"]
    if automatic:
        task["requested_agent"] = "auto"
        task["agent"], task["route"] = selected["agent"], selected["route"]
        # Sessions belong to a worker/model. A switched lane starts a separate session.
        task["session_key"] += ":" + selected["key"] + ":" + str(selected.get("model", ""))
        if selected.get("model"):
            task["settings_overrides"] = {"model": selected["model"]}
            if selected.get("reasoning_effort"):
                task["settings_overrides"]["reasoning_effort"] = selected["reasoning_effort"]
    if record:
        task.setdefault("decisions", {})["routing"] = record["id"]
        reason = ("qualified automatic route" if applied else
                  ("model chosen inside the named route by " + ("verified outcomes" if ranking else "orc's quality order")
                   + "; advice does not change dispatch") if within_route else
                  "explicit route or pair retained; advice does not change dispatch" if not automatic else
                  "ranked by verified outcomes; advice does not change dispatch" if config.get("decisions", {}).get("rank_by_outcomes") else
                  "configured preference order; advice does not change dispatch")
        engine.applied(record, selected["key"], applied, reason + ("; epsilon exploration picked this lane" if explored else ""))
    if scope and (engine.options["mode"] != "off" or quota_audit):
        keys = [c["key"] for c in candidates]
        chances = propensities(keys, selected["key"], 0.0 if applied else effective)
        engine.store.append("routing_log", **context(task), decision_id=record["id"] if record else None, scope=scope,
                            write=bool(task.get("write")),
                            **({"needs": task["needs"], "needs_unmet": needs_unmet} if task.get("needs") else {}),
                            policy={"rank_by_outcomes": minimum, "explore": explore, "warm_epsilon": warm_epsilon if ranking else None,
                                    "epsilon": effective, "routing_epsilon": epsilon, "laya_applied": applied,
                                    "priors": priors_policy(config),
                                    **({"cost_epsilon": cost_epsilon if warm_epsilon is None else max(warm_epsilon, cost_epsilon)}
                                       if ranking and any(c.get("cost_tier") is not None for c in candidates) else {}),
                                    **({"quota": quota_settings(config)} if quota_audit else {})},
                            **({"quota": quota_audit, "rejected": rejected} if quota_audit else {}),
                            candidates=[{**c, "propensity": chances[c["key"]]} for c in candidates],
                            chosen=selected["key"], explored=explored)


def effective_outcomes(events):
    """One measured outcome per run, in append order, excluding withdrawn leads.

    Keep independent gate evidence so withdrawing the external lifecycle restores
    it, without reviving an earlier external stage. Unmeasured events are audit-only.
    Changed check inputs exclude the run, including previously recorded successes.
    """
    independent, external = {}, {}
    untrusted = set()
    for index, event in enumerate(events):
        run_id = event.get("task_id")
        if not run_id:
            continue
        if event.get("event") in {"outcome", "outcome_excluded"} and event.get("check_inputs_changed"):
            untrusted.add(run_id)
            independent.pop(run_id, None)
        elif event.get("event") == "outcome_withdraw" and event.get("source") == "lead":
            external.pop(run_id, None)
        elif event.get("event") == "outcome" and isinstance(event.get("accepted"), bool):
            target = external if event.get("source") == "lead" else independent
            target[run_id] = (index, event)
    return {run_id: max((table[run_id] for table in (independent, external) if run_id in table),
                        key=lambda item: item[0])[1]
            for run_id in independent.keys() | external.keys() if run_id not in untrusted}


def routing_report(events):
    """Per-lane acceptance from logged routing choices, inverse-propensity weighted.

    Joins each run's latest `routing_log` to its latest outcome (gate or lead)
    by run id; outcomes marked `laya_veto` are skipped, since the model would
    be grading itself. For each lane, over the logged choices where it was a
    candidate: `ips_acceptance` is sum(accepted / propensity for choices of the
    lane) / available, `snips_acceptance` normalises by the summed weights, and
    `ess` is (sum w)^2 / sum w^2. A lane given propensity 0 in any of those
    choices has no overlap there, so neither estimate is reported for it."""
    events = list(events)
    logs, outcomes, vetoed, sources = {}, effective_outcomes(events), 0, {}
    for event in events:
        if event.get("event") == "routing_log" and event.get("task_id"):
            logs[event["task_id"]] = event
    lanes = {}
    joined = 0
    for task_id, log in logs.items():
        outcome = outcomes.get(task_id)
        if outcome is None:
            continue
        if outcome.get("laya_veto"):
            vetoed += 1
            continue
        joined += 1
        source = outcome.get("source") or "gate"
        sources[source] = sources.get(source, 0) + 1
        reward = 1.0 if outcome.get("accepted") else 0.0
        for candidate in log.get("candidates") or []:
            lane = lanes.setdefault(candidate["key"], {"key": candidate["key"], "available": 0, "chosen": 0, "accepted": 0,
                                                       "zero_propensity": 0, "_w": 0.0, "_w2": 0.0, "_wr": 0.0})
            propensity = float(candidate.get("propensity") or 0)
            lane["available"] += 1
            if propensity <= 0:
                lane["zero_propensity"] += 1
            if log.get("chosen") == candidate["key"] and propensity > 0:
                weight = 1 / propensity
                lane["chosen"] += 1
                lane["accepted"] += int(reward)
                lane["_w"] += weight
                lane["_w2"] += weight * weight
                lane["_wr"] += weight * reward
    rows = []
    for lane in sorted(lanes.values(), key=lambda item: (-item["available"], item["key"])):
        weight, square, weighted = lane.pop("_w"), lane.pop("_w2"), lane.pop("_wr")
        overlap = lane["zero_propensity"] == 0 and lane["chosen"] > 0
        lane.update(observed_acceptance=lane["accepted"] / lane["chosen"] if lane["chosen"] else None,
                    ips_acceptance=weighted / lane["available"] if overlap else None,
                    snips_acceptance=weighted / weight if overlap else None,
                    ess=round(weight * weight / square, 3) if square else 0.0,
                    overlap="ok" if overlap else "insufficient overlap")
        rows.append(lane)
    warnings = []
    if any(lane["overlap"] != "ok" for lane in rows):
        warnings.append("insufficient overlap: some lanes had propensity 0 where they were available or were never chosen; "
                        "their counterfactual acceptance is not identified. Set decisions.routing_epsilon above 0 to explore.")
    if len(logs) - joined - vetoed:
        warnings.append(f"{len(logs) - joined - vetoed} logged routing choices have no outcome yet")
    return {"schema": "fusion.routing_report.v1", "logged_choices": len(logs), "with_outcome": joined,
            "routing_policies": [{"task_id": task_id, "policy": log.get("policy", {}),
                                  "cost_tiers": {c["key"]: c.get("cost_tier") for c in log.get("candidates") or []}}
                                 for task_id, log in logs.items()],
            "quota_decisions": [{"task_id": task_id, "chosen": log.get("chosen"), "quota": log["quota"],
                                 "rejected": log.get("rejected", {})} for task_id, log in logs.items() if log.get("quota")],
            "vetoed_outcomes_skipped": vetoed, "outcome_sources": sources,
            "explored": sum(bool(log.get("explored")) for log in logs.values()), "lanes": rows, "warnings": warnings}


def review_task(config, task, store=None):
    engine = DecisionEngine(store.workspace if store else task["workspace"], config)
    record = engine.decide("review", {"task": task.get("decision_context", task["task"]), "role": task["role"], "write": task["write"]}, REVIEW_QUESTIONS, context(task))
    selected = "general"
    applied = engine.allowed(record, "specialty")
    if applied:
        selected = record["recommendations"]["specialty"]["value"]
    instructions = {
        "general": "Inspect correctness, scope, regressions and test evidence.",
        "security": "Also inspect authentication, authorization, secret handling, injection and trust boundaries.",
        "payments": "Also inspect amounts, currencies, rounding, idempotency, retries, reconciliation and failure atomicity.",
        "data": "Also inspect schema compatibility, migrations, rollback, concurrency and data integrity.",
    }
    # Classification only adds scrutiny; it never removes the requested review or grants writes.
    if applied:
        import fusion_core as core
        delegation = core.agent_settings(config, task).get("allow_native_delegation") is True
        task["task"] += "\nReview focus: " + instructions[selected] + "\nReport unresolved findings as STATUS: blocked." + ("" if delegation else " Do not delegate further.")
    task.setdefault("decisions", {})["review"] = record["id"]
    engine.applied(record, selected, applied)


def accept_node(config, workspace, workflow_id, node, result):
    """A semantic Done-check, run only after every structural acceptance check
    already passed. A classifier verdict can add scrutiny -- reject a node
    whose artifact is syntactically valid but substantively off-task -- but
    it can never accept on its own; a structural failure is decided before
    this is ever called, and this function has no path back to True from one."""
    engine = DecisionEngine(workspace, config)
    record = engine.decide("acceptance", acceptance_state(node, result, state_cap(engine.options), engine.state_tokens("acceptance")),
                           ACCEPTANCE_QUESTIONS, {"task_id": result.get("run_id"), "group": workflow_id, "role": node.get("role")})
    plausible, applied = True, False
    if engine.allowed(record, "plausible") and record["recommendations"]["plausible"]["value"] == "false":
        plausible, applied = False, True
    engine.applied(record, "accept" if plausible else "reject", applied,
                   "qualified active classification" if applied else "shadow mode or unqualified recommendation; structural acceptance stands")
    return plausible, record["id"]


# Gate codes that come from the worker's own report or its parse, not from an
# observation of the work. A rejection resting only on these (or on Laya's own
# veto) is not evidence about the lane.
REPORT_CODES = frozenset({"worker_blockers", "required_handoff_empty", "repeated_failure"})


def outcome_counts(accepted, status, gate_codes=(), laya_veto=False, check_inputs_changed=None):
    """Whether a workflow outcome is evidence for route ranking and labeling.

    An acceptance counts, and so does a rejection with any observed cause (a
    failed or unrunnable check, a missing file, an unchanged tree, a worker
    that did not report success). A reported success rejected only by Laya's
    own veto or by blocker/handoff parsing does not: counting it would let an
    active acceptance head label itself, or turn a parser artifact into a
    lane's failure.
    """
    if check_inputs_changed:
        return False
    if accepted or status != "success":
        return True
    codes = {code.get("code") if isinstance(code, dict) else code for code in gate_codes or ()}
    return bool(codes - REPORT_CODES) or not (codes or laya_veto)


def recovery(config, workspace, workflow_id, node, result, accepted, max_attempts, gate_codes=None, laya_veto=False):
    import fusion_core as core
    failure = core.failure_class(result)
    if failure == "coordinator_error":
        progress.emit(node.get("id", "workflow"), "Fusion snapshot failed; repair the coordinator error, then resume this stage. Accepted stages remain saved.")
        return "stop", None
    engine = DecisionEngine(workspace, config)
    repeated = bool(node.get("repeated_failure"))
    can_retry = node["attempts"] < max_attempts and not repeated
    actual = "continue" if accepted else "stop" if failure in {"quota", "permission_denied"} or not can_retry else "repair"
    automatic = node["agent"] == "auto" and not node.get("route")
    if not accepted and failure == "quota" and automatic:
        failed_route = result.get("route") or result.get("agent")
        excluded = node.setdefault("excluded_routes", [])
        if failed_route and failed_route not in excluded:
            excluded.append(failed_route)
        candidate_task = {"workspace": workspace, "write": node.get("write", False), "excluded_routes": excluded}
        if can_retry and route_candidates(config, candidate_task, core.RunStore(workspace)):
            actual = "switch"
    record = engine.decide("recovery", {"status": result.get("status"), "accepted": accepted,
                                       "failure": failure, "blockers": result.get("blockers", []),
                                       "attempt": node["attempts"], "max_attempts": max_attempts,
                                       "repeated_failure": repeated,
                                       "automatic_lane": node["agent"] == "auto"}, RECOVERY_QUESTIONS,
                           {"task_id": result.get("run_id"), "group": workflow_id, "role": node.get("role")})
    applied = False
    if not accepted and failure != "permission_denied" and engine.allowed(record, "action"):
        suggested = record["recommendations"]["action"]["value"]
        if suggested in {"ask", "stop"} or (can_retry and suggested == "repair" and failure != "quota"):
            actual, applied = suggested, True
        elif can_retry and suggested == "switch" and node["agent"] == "auto" and not node.get("route"):
            actual, applied = "switch", True
            excluded = node.setdefault("excluded_routes", [])
            lane = result.get("route") or result.get("agent")
            if lane not in excluded:
                excluded.append(lane)
    engine.applied(record, actual, applied, "acceptance, permissions and attempt limits enforced")
    if engine.options["mode"] != "off":
        # An outcome that is not evidence is written under another event name,
        # so every reader of "outcome" events (route ranking) skips it.
        changed = result.get("check_inputs_changed", [])
        counted = outcome_counts(accepted, result.get("status"), gate_codes, laya_veto, changed)
        codes = [code.get("code") if isinstance(code, dict) else code for code in gate_codes or ()]
        engine.store.append("outcome" if counted else "outcome_excluded", task_id=result.get("run_id"), group=workflow_id,
                            accepted=accepted, status=result.get("status"), laya_veto=bool(laya_veto), gate_codes=codes,
                            check_inputs_changed=changed,
                            **({} if counted else {"excluded_reason": "check inputs changed (tampered)" if changed else
                                                  "rejected only by Laya's veto or by the worker's own report"}),
                            evidence=str(workspace / ".fusion" / "workflows" / workflow_id / "nodes" / node.get("id", "unknown") / "node.json"))
    return actual, record["id"]
