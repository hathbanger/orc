"""Offline usage readers. Reading never creates state; only record_daily writes."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re

UTC = timezone.utc
TOKEN_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
GROUPS = ("session", "project", "model", "agent")


def timestamp(value):
    """Parse ISO timestamps or Unix seconds/milliseconds; invalid values are unknown."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
        if isinstance(value, str):
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def since_time(value="24h", now=None):
    now = now or datetime.now(UTC)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([hd])", value)
    if match:
        amount = float(match[1])
        try:
            result = now - timedelta(hours=amount * (24 if match[2] == "d" else 1))
        except OverflowError:
            raise ValueError("--since duration is too large") from None
    else:
        result = timestamp(value)
    if result is None or result >= now:
        raise ValueError("--since must be a positive duration (24h, 7d) or a past ISO timestamp")
    return result


def json_lines(path):
    """Ignore missing/unreadable files, malformed lines, and unfinished JSON tails."""
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(item, dict):
                    yield item
    except OSError:
        return


def json_object(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return max(0, value)
    return 0


def tokens(usage):
    """Use disjoint token buckets. OpenAI cached input is included in input."""
    usage = usage if isinstance(usage, dict) else {}
    read = number(usage.get("cache_read_input_tokens", usage.get("cached_input_tokens", 0)))
    write = number(usage.get("cache_creation_input_tokens", usage.get("cache_write_input_tokens", 0)))
    if not write and isinstance(usage.get("cache_creation"), dict):
        write = sum(number(usage["cache_creation"].get(k)) for k in
                    ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"))
    incoming = number(usage.get("input_tokens"))
    if "cached_input_tokens" in usage and "cache_read_input_tokens" not in usage:
        incoming = max(0, incoming - read - write)
    return dict(zip(TOKEN_FIELDS, (incoming, read, write, number(usage.get("output_tokens")))))


def row(source, session, project, agent, model, when, usage, **metadata):
    cost = usage.get("cost_usd", usage.get("cost"))
    return {"source": source, "session": session, "project": project,
            "agent": agent or "unknown", "model": model or "unknown", "timestamp": when,
            "calls": 1, **tokens(usage),
            "cost_usd": number(cost) if isinstance(cost, (int, float)) else None, **metadata}


def read_orc(workspace, limit=None):
    """Read RunStore's ledger without constructing state or invoking telemetry."""
    spans = json_lines(Path(workspace) / ".fusion" / "traces.jsonl")
    if limit is not None:
        if limit <= 0:
            raise ValueError("--limit must be positive")
        spans = deque(spans, maxlen=limit)
    result = []
    seen = set()
    for index, span in enumerate(spans):
        identity = span.get("span_id")
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        when = (timestamp(span.get("end_time_ms")) or timestamp(span.get("start_time_ms"))
                or timestamp(span.get("timestamp")))
        usage = span.get("usage")
        if when is None or not isinstance(usage, dict):
            continue
        session = span.get("session_key") or span.get("session_id") or span.get("run_id") or identity or str(index)
        result.append(row("orc", "orc:" + str(session), str(Path(workspace).resolve()),
                          span.get("agent"), span.get("model"), when, usage,
                          route=span.get("route"), lane_key=span.get("lane_key"),
                          session_key=span.get("session_key")))
    return result


def claude_root(config_dir=None):
    return Path(config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def codex_root(home=None):
    return Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def read_claude(config_dir=None):
    """One row per assistant message; streamed usage updates replace earlier copies."""
    result = []
    for path in sorted((claude_root(config_dir) / "projects").glob("*/*.jsonl")):
        messages = {}
        aliases = {}
        for index, event in enumerate(json_lines(path)):
            message = event.get("message")
            if not isinstance(message, dict) or (event.get("type") != "assistant" and message.get("role") != "assistant"):
                continue
            usage = message.get("usage")
            when = timestamp(event.get("timestamp"))
            if not isinstance(usage, dict) or when is None:
                continue
            ids = [(name, str(value)) for name, value in
                   (("message", message.get("id")), ("request", event.get("requestId") or message.get("requestId"))) if value]
            matches = {aliases[key] for key in ids if key in aliases}
            identity = min(matches) if matches else index
            previous_chunks = [messages[key] for key in sorted(matches)]
            # A later record can link previously separate message/request IDs.
            for old in matches - {identity}:
                messages.pop(old, None)
                aliases = {key: identity if value == old else value for key, value in aliases.items()}
            for key in ids:
                aliases[key] = identity
            candidate = row("claude", "claude:" + str(path.resolve()), str(path.parent.resolve()),
                            "claude", message.get("model"), when, usage)
            for previous in previous_chunks:
                candidate["timestamp"] = min(previous["timestamp"], candidate["timestamp"])
                # Stream chunks can omit fields already reported on earlier chunks.
                for field in TOKEN_FIELDS:
                    candidate[field] = max(previous[field], candidate[field])
                if candidate["model"] == "unknown":
                    candidate["model"] = previous["model"]
            messages[identity] = candidate
        result.extend(messages.values())
    return result


def read_codex(home=None):
    """Convert cumulative token counters into increments before time filtering."""
    result = []
    for path in sorted((codex_root(home) / "sessions").rglob("*.jsonl")):
        previous = dict.fromkeys(TOKEN_FIELDS, 0)
        model, project = "unknown", "unknown"
        for event in json_lines(path):
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if event.get("type") in ("session_meta", "turn_context"):
                model = payload.get("model") or model
                project = payload.get("cwd") or project
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            total = info.get("total_token_usage") if isinstance(info, dict) else None
            if not isinstance(total, dict) or not all(
                isinstance(total.get(key), (int, float)) and not isinstance(total[key], bool)
                and math.isfinite(total[key]) and total[key] >= 0
                for key in ("input_tokens", "output_tokens")
            ):
                continue
            current = tokens(total)
            delta = {key: max(0, current[key] - previous[key]) for key in TOKEN_FIELDS}
            previous = current
            when = timestamp(event.get("timestamp"))
            if when is None or not any(delta.values()):
                continue
            result.append(row("codex", "codex:" + str(path.resolve()), project, "codex",
                              payload.get("model") or model, when, delta))
    return result


def account_identity(*items):
    """Only recorded identity is authoritative; never read authentication files."""
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("account_id", "accountId", "account", "lane_key"):
            if isinstance(item.get(key), str) and item[key]:
                return item[key]
        for key in ("metadata", "resolved"):
            if isinstance(item.get(key), dict):
                identity = account_identity(item[key])
                if identity is not None:
                    return identity
    return None


def normalize_quota(provider, value, *, normalized=False):
    """Normalize recorded provider windows, rejecting malformed numeric values."""
    if not isinstance(value, dict):
        return None
    raw = value.get("windows" if normalized else "unifiedWindows", {}) if provider == "claude" or normalized else value
    windows = {}
    for name, window in raw.items() if isinstance(raw, dict) else []:
        if not isinstance(window, dict):
            continue
        used = window.get("used" if normalized else "utilization" if provider == "claude" else "used_percent")
        scale = 100 if provider == "codex" and not normalized else 1
        # A rejected window reports past its limit (utilization 1.02), so only
        # the floor is checked; an upper bound would erase the rejection's cause.
        valid = isinstance(used, (int, float)) and not isinstance(used, bool) and math.isfinite(used) and 0 <= used
        reset = timestamp(window.get("resetsAt" if provider == "claude" and not normalized else "resets_at"))
        if not valid and reset is None:
            continue
        entry = {"used": used / scale if valid else None, "resets_at": reset.timestamp() if reset else None}
        minutes = window.get("window_minutes")
        if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and math.isfinite(minutes) and minutes > 0:
            entry["window_minutes"] = minutes
        windows[name] = entry
    status = value.get("status")
    status = status if isinstance(status, str) else None
    # The window a rejection names: Claude's rateLimitType, kept once normalized.
    named = value.get("rejected_window" if normalized else "rateLimitType")
    named = named if status == "rejected" and isinstance(named, str) and named in windows else None
    return ({"windows": windows, "status": status, **({"rejected_window": named} if named else {})}
            if windows or status else None)


def event_quota(provider, event):
    """Exec events and rollout/RunStore envelopes share the same normalizer."""
    if not isinstance(event, dict):
        return None
    if provider == "claude" and event.get("type") == "rate_limit_event":
        return normalize_quota(provider, event.get("rate_limit_info"))
    if provider == "codex" and isinstance(event.get("rate_limits"), dict):
        return normalize_quota(provider, event["rate_limits"])
    for key in ("payload", "event"):
        if isinstance(event.get(key), dict):
            quota = event_quota(provider, event[key])
            if quota is not None:
                return quota
    return None


def headroom(workspace=None, *, codex_home=None, include_raw=True):
    """Latest known quota windows per recorded account, independent of --since.

    Missing identities/windows stay null. Observations without timestamps use
    their containing run's end time, then file mtime, solely for quota freshness.
    """
    accounts = {}

    def observe(provider, account, windows, when, source):
        names = ("primary", "secondary") if provider == "codex" else ("five_hour", "seven_day", "spend")
        key = (provider, account)
        entry = accounts.setdefault(key, {"provider": provider, "account": account,
                                          "windows": dict.fromkeys(names, None)})
        for name in names:
            window = windows.get(name)
            if not isinstance(window, dict):
                continue
            old = entry["windows"][name]
            if old and timestamp(old["observed_at"]) > when:
                continue
            fields = ("used_percent", "window_minutes", "resets_at") if provider == "codex" else ("utilization", "resetsAt")
            entry["windows"][name] = {field: window.get(field) for field in fields}
            entry["windows"][name].update(observed_at=iso(when), source=str(source))

    def file_time(path):
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            return datetime.min.replace(tzinfo=UTC)

    def observe_trace(trace, source):
        provider, lane = trace.get("agent"), trace.get("lane_key")
        quota = normalize_quota(provider, trace.get("quota"), normalized=True)
        when = timestamp(trace.get("end_time_ms")) or timestamp(trace.get("timestamp"))
        if (provider not in {"claude", "codex"} or not isinstance(lane, str)
                or not (lane == provider or lane.startswith(provider + "@")) or quota is None or when is None):
            return
        # Keep the usage reader's original provider-shaped windows for callers.
        windows = {}
        for name, window in quota["windows"].items():
            windows[name] = ({"utilization": window["used"], "resetsAt": window["resets_at"]}
                             if provider == "claude" else
                             {"used_percent": window["used"] * 100 if window["used"] is not None else None,
                              "window_minutes": window.get("window_minutes"), "resets_at": window["resets_at"]})
        observe(provider, lane, windows, when, source)
        entry = accounts[provider, lane]
        # A reading may carry only some windows (Claude reports the windows its
        # last rate-limit event named), so windows merge per name, newest
        # reading of each, with its own observed_at. A window past its reset
        # stays: routing treats it as inactive and probe_due still needs it.
        # The status (and a rejection's window) is the newest reading's.
        merged = entry.setdefault("_windows", {})
        for name, window in quota["windows"].items():
            old = merged.get(name)
            if old is None or timestamp(old["observed_at"]) <= when:
                merged[name] = {**window, "observed_at": iso(when)}
        if not entry.get("observed_at") or timestamp(entry["observed_at"]) <= when:
            entry.update(lane_key=lane, observed_at=iso(when), source=str(source),
                         _newest={key: value for key, value in quota.items() if key != "windows"})
        entry["quota"] = {**entry["_newest"], "windows": dict(merged)}

    ledger = Path(workspace or Path.cwd()) / ".fusion" / "traces.jsonl"
    for trace in json_lines(ledger):
        observe_trace(trace, ledger)

    for path in sorted((codex_root(codex_home) / "sessions").rglob("*.jsonl")) if include_raw else []:
        account = None
        for event in json_lines(path):
            payload = event.get("payload")
            payload = payload if isinstance(payload, dict) else event
            account = account_identity(payload, event) or account
            limits = payload.get("rate_limits")
            if isinstance(limits, dict):
                observe("codex", account_identity(limits) or account, limits,
                        timestamp(event.get("timestamp")) or file_time(path), path)

    root = Path(workspace or Path.cwd()) / ".fusion" / "runs"
    for run in sorted(root.glob("*")):
        if not run.is_dir():
            continue
        trace = json_object(run / "trace.json")
        observe_trace(trace, run / "trace.json")
        if not include_raw:
            continue
        task = json_object(run / "task.json")
        result = json_object(run / "result.json")
        account = account_identity(trace, task, result)
        fallback = timestamp(trace.get("end_time_ms")) or timestamp(task.get("created_at"))
        for name in ("stdout.log", "stderr.log", "events.jsonl"):
            path = run / name
            for event in json_lines(path):
                # RunStore events may wrap a provider event under event/payload.
                nested = event.get("event", event.get("payload"))
                record = nested if isinstance(nested, dict) and nested.get("type") == "rate_limit_event" else event
                if record.get("type") != "rate_limit_event":
                    continue
                info = record.get("rate_limit_info")
                if not isinstance(info, dict):
                    continue
                windows = info.get("unifiedWindows")
                if not isinstance(windows, dict):
                    continue
                when = timestamp(record.get("timestamp")) or timestamp(event.get("ts")) or fallback or file_time(path)
                observe("claude", account_identity(info, record, event) or account, windows, when, path)
    for provider in ("codex", "claude"):
        # Rollouts carry no account id. With exactly one identified account for
        # the provider they are that account's windows: one account, one row.
        named = [key for key in accounts if key[0] == provider and key[1] is not None]
        if (provider, None) in accounts and len(named) == 1:
            loose = accounts.pop((provider, None))
            target = accounts[named[0]]
            for name, window in loose["windows"].items():
                old = target["windows"].get(name)
                if window and (not old or timestamp(old["observed_at"]) < timestamp(window["observed_at"])):
                    target["windows"][name] = window
    for provider in ("codex", "claude"):
        if not any(key[0] == provider for key in accounts):
            observe(provider, None, {}, datetime.now(UTC), "")
    output = []
    for key in sorted(accounts, key=lambda key: (key[0], key[1] or "")):
        entry = accounts[key]
        entry.pop("_windows", None)
        entry.pop("_newest", None)
        entry["status"] = "known" if any(entry["windows"].values()) else "unknown"
        output.append(entry)
    return output


def metrics(rows, hours):
    calls = sum(item["calls"] for item in rows)
    result = {"calls": calls, **{key: sum(item[key] for item in rows) for key in TOKEN_FIELDS}}
    context = sum(result[key] for key in TOKEN_FIELDS[:3])
    costs = [item["cost_usd"] for item in rows if item["cost_usd"] is not None]
    result.update(context_tokens=context, average_context_per_call=context / calls if calls else 0,
                  calls_per_hour=calls / hours, cost_usd=sum(costs) if costs else None,
                  cost_reported_calls=len(costs))
    return result


def report(workspace, *, since="24h", by="session", top=None, limit=None,
           context_threshold=200000, calls_per_hour_threshold=60, now=None,
           claude_config_dir=None, codex_home=None):
    now = now or datetime.now(UTC)
    start = since_time(since, now)
    if by not in GROUPS:
        raise ValueError("--by must be session, project, model, or agent")
    if top is not None and top <= 0:
        raise ValueError("--top must be positive")
    for value in (context_threshold, calls_per_hour_threshold):
        if not math.isfinite(value) or value < 0:
            raise ValueError("coordinator thresholds must be finite nonnegative numbers")
    rows = read_orc(workspace, limit) + read_claude(claude_config_dir) + read_codex(codex_home)
    rows = [item for item in rows if start <= item["timestamp"] <= now]
    hours = (now - start).total_seconds() / 3600
    sessions = defaultdict(list)
    groups = defaultdict(list)
    for item in rows:
        sessions[item["session"]].append(item)
        groups[item[by]].append(item)
    coordinators = set()
    for session, items in sessions.items():
        values = metrics(items, hours)
        if values["average_context_per_call"] > context_threshold or values["calls_per_hour"] > calls_per_hour_threshold:
            coordinators.add(session)
    output = []
    for key, items in groups.items():
        flags = sorted({item["session"] for item in items} & coordinators)
        group = {"key": key, **metrics(items, hours), "coordinator": bool(flags), "coordinator_sessions": flags}
        for field in ("source", "session", "project", "model", "agent", "route", "lane_key", "session_key"):
            group[field + "s"] = sorted({item[field] for item in items if item.get(field) is not None})
        output.append(group)
    output.sort(key=lambda item: (-item["context_tokens"], item["key"]))
    return {"schema": "fusion.usage.v1", "generated_at": iso(now), "since": iso(start), "until": iso(now),
            "by": by, "window_hours": hours,
            "thresholds": {"average_context_per_call": context_threshold, "calls_per_hour": calls_per_hour_threshold},
            "total": metrics(rows, hours), "groups": output[:top], "group_count": len(output),
            "coordinator_sessions": sorted(coordinators),
            "sources": {source: metrics([item for item in rows if item["source"] == source], hours)
                        for source in ("orc", "claude", "codex")},
            "headroom": headroom(workspace, codex_home=codex_home)}


def record_daily(workspace, snapshot):
    """Append at most one full snapshot per UTC day, serialized with a file lock."""
    path = Path(workspace) / ".fusion" / "usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    day = snapshot["generated_at"][:10]
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        content = stream.read()
        for line in content.splitlines():
            try:
                existing = json.loads(line)
            except ValueError:
                continue
            if isinstance(existing, dict) and existing.get("date") == day:
                return False
        if content and not content.endswith("\n"):
            stream.write("\n")
        stream.write(json.dumps({"date": day, **snapshot}, ensure_ascii=False, allow_nan=False) + "\n")
    return True


def render(snapshot):
    lines = [f"Usage {snapshot['since']} – {snapshot['until']} (by {snapshot['by']})",
             "CALLS  INPUT  CACHE-READ  CACHE-WRITE  OUTPUT  AVG-CONTEXT  CALLS/H  COORDINATOR  GROUP"]
    for group in [*snapshot["groups"], {**snapshot["total"], "key": "TOTAL", "coordinator": bool(snapshot["coordinator_sessions"])}]:
        values = [str(group[key]) for key in ("calls", *TOKEN_FIELDS)]
        lines.append("  ".join([*values, f"{group['average_context_per_call']:.1f}",
                                f"{group['calls_per_hour']:.2f}", "yes" if group["coordinator"] else "no", group["key"]]))
    lines.append("Headroom (latest recorded observations):")
    for account in snapshot["headroom"]:
        lines.append(f"  {account['provider']} account={account['account'] or 'unknown'}")
        for name, window in account["windows"].items():
            lines.append(f"    {name}: " + (json.dumps(window, ensure_ascii=False) if window else "unknown"))
    return "\n".join(lines)


def command(args, workspace):
    if args.top is not None and args.top <= 0:
        raise ValueError("--top must be positive")
    snapshot = report(workspace, since=args.since, by=args.by, limit=args.limit,
                      context_threshold=args.context_threshold, calls_per_hour_threshold=args.calls_per_hour_threshold)
    if args.record:
        record_daily(workspace, snapshot)
    snapshot["groups"] = snapshot["groups"][:args.top]
    print(json.dumps(snapshot, indent=2, ensure_ascii=False, allow_nan=False) if args.json else render(snapshot))
    return 0
