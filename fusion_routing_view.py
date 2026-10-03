"""Per-task routing view: what each automatic routing decision chose, why, what it cost and how it ended.

One row per run that automatic routing (or a named route's arms) dispatched:
the chosen lane and model, its logged propensity, the sampled posterior when
`decisions.gating_policy` is `thompson`, the run's reported cost and status,
its measured outcome (gate or lead), and the labels on every decision about
that run. The labels are what a person reviews and overrules, through the
existing label flow keyed by decision id. Read-only.
"""
from __future__ import annotations

ROUTED_SCOPES = {"automatic", "route_arms"}


def _cost(span):
    usage = (span or {}).get("usage") or {}
    value = usage.get("cost_usd", usage.get("cost"))
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def routing_tasks(events, spans=(), model=None, limit=200):
    """{"rows", "models", "filters"} from the decision log and the run spans.

    `model` keeps rows whose chosen lane key or model contains it (case
    insensitive). Rows are newest first; `limit` bounds them after
    filtering. `models` summarizes the filtered rows per chosen model:
    picks, accepted, rejected, pending (no outcome yet) and reported cost
    (None when no run reported one)."""
    from fusion_policy import effective_outcomes
    events = list(events)
    outcomes = effective_outcomes(events)
    logs, decisions, labels = {}, {}, {}
    for event in events:
        kind = event.get("event")
        if kind == "routing_log" and event.get("task_id") and event.get("scope") in ROUTED_SCOPES and event.get("chosen"):
            logs[event["task_id"]] = event
        elif kind == "decision" and event.get("id"):
            run = (event.get("context") or {}).get("task_id")
            if run:
                decisions.setdefault(run, {})[event["id"]] = event.get("kind")
        elif kind == "label" and event.get("id"):
            labels[event["id"]] = event
    by_run = {}
    for span in spans or ():
        if span.get("run_id"):
            by_run.setdefault(span["run_id"], span)
    needle = (model or "").lower()
    rows = []
    for run, log in logs.items():
        chosen = next((c for c in log.get("candidates") or [] if c.get("key") == log["chosen"]), {})
        chosen_model = chosen.get("model") or ""
        if needle and needle not in log["chosen"].lower() and needle not in chosen_model.lower():
            continue
        sampled = log.get("sampled") or {}
        family = next((f for f in sampled.get("families") or [] if f.get("lead") == log["chosen"]), None)
        outcome = outcomes.get(run)
        span = by_run.get(run)
        run_labels = []
        for decision_id, decision_kind in sorted((decisions.get(run) or {}).items()):
            label = labels.get(decision_id)
            run_labels.append({"decision_id": decision_id, "kind": decision_kind,
                               "labeled": label is not None,
                               **({"source": label.get("source"), "answers": label.get("answers") or {},
                                   "verified": label.get("verified")} if label else {})})
        rows.append({"run": run, "time_ms": log.get("time_ms"), "role": log.get("role"), "write": log.get("write"),
                     "scope": log.get("scope"), "chosen": log["chosen"], "agent": chosen.get("agent"),
                     "model": chosen_model, "reasoning_effort": chosen.get("reasoning_effort"),
                     "propensity": chosen.get("propensity"), "candidates": len(log.get("candidates") or []),
                     "explored": bool(log.get("explored")), "write_trial": log.get("write_trial"),
                     "posterior": ({k: family.get(k) for k in ("successes", "attempts", "p_win", "cost_per_accepted", "lanes")}
                                   if family else None),
                     "cost_usd": _cost(span), "status": (span or {}).get("status"),
                     "duration_ms": (span or {}).get("duration_ms"),
                     "outcome": ({"accepted": outcome.get("accepted"), "source": outcome.get("source") or "gate",
                                  "stage": outcome.get("stage"), "reason": outcome.get("reason")} if outcome else None),
                     "labels": run_labels, "routing_decision": log.get("decision_id"),
                     "run_dir": f".fusion/runs/{run}", "result": f".fusion/runs/{run}/result.json"})
    rows.sort(key=lambda row: row["time_ms"] or 0, reverse=True)
    summary = {}
    for row in rows:
        entry = summary.setdefault(row["model"] or row["chosen"], {"picks": 0, "accepted": 0, "rejected": 0,
                                                                   "pending": 0, "cost_usd": None})
        entry["picks"] += 1
        accepted = (row["outcome"] or {}).get("accepted")
        entry["accepted" if accepted is True else "rejected" if accepted is False else "pending"] += 1
        if row["cost_usd"] is not None:
            entry["cost_usd"] = round((entry["cost_usd"] or 0) + row["cost_usd"], 4)
    return {"rows": rows[:max(0, int(limit))], "total": len(rows),
            "models": dict(sorted(summary.items(), key=lambda item: -item[1]["picks"])),
            "filters": {"model": model or None, "limit": limit}}
