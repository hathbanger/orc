"""`fusion quota`: what each account has left, a probe at each reset, and what a run costs in quota.

Readings are passive: they arrive on the traces of runs that used the account.
The view shows how old each reading is, the probe sends one tiny run to an
account whose exhausted window has reset with no reading since, and the rates
measure how much of a window ORC runs consume, so routing can later decline a
run the remaining headroom cannot hold.
"""
import argparse
import json
import time

import fusion_core as core
import fusion_policy as policy
import fusion_usage as usage

PROBE_TASK = "Reply with the single word OK. Do not read files or run commands."
WINDOWS = ("five_hour", "seven_day", "spend", "primary", "secondary")


def lanes_for(config, lane_key):
    """Routes and bare agents whose account is `lane_key`."""
    names = []
    for agent in ("claude", "codex"):
        if core.lane_key(agent, config.get(agent) or {}) == lane_key:
            names.append(agent)
    for name, route in (config.get("routes") or {}).items():
        agent = route.get("agent") if isinstance(route, dict) else None
        if agent in {"claude", "codex"} and core.lane_key(agent, lane_settings(config, name)) == lane_key:
            names.append(name)
    return names


def lane_settings(config, name):
    route = (config.get("routes") or {}).get(name)
    return core.agent_settings(config, {"agent": route["agent"], "route": name} if route else {"agent": name})


def cooling(config, store, now_ms):
    """Account and model pairs a quota failure is cooling right now."""
    rows = {}
    for span in store.traces(limit=200):
        ended = span.get("end_time_ms") or 0
        if span.get("failure_class") != "quota" or not 0 <= now_ms - ended < core.LANE_COOLDOWN_SECONDS * 1000:
            continue
        lane = (config.get("routes") or {}).get(span.get("route")) or {}
        settings = core.deep_merge(config.get(span.get("agent")) or {}, lane)
        key = (core.lane_key(span.get("agent"), settings), settings.get("model") or span.get("model") or "*")
        rows[key] = max(rows.get(key, 0), ended)
    return [{"lane_key": key, "model": model, "until": usage.iso(usage.timestamp(ended + core.LANE_COOLDOWN_SECONDS * 1000))}
            for (key, model), ended in sorted(rows.items())]


def status(config, store, now=None):
    now = time.time() if now is None else now
    thresholds = policy.quota_settings(config)
    accounts = []
    for entry in usage.headroom(store.workspace, include_raw=False):
        if not entry.get("lane_key") or not entry.get("quota"):
            continue
        assessed = policy.quota_assessment(entry, thresholds, now)
        observed = usage.timestamp(entry.get("observed_at"))
        windows = {}
        for name, window in assessed["windows"].items():
            reset = usage.timestamp(window.get("resets_at"))
            windows[name] = {"used": window.get("used"), "resets_at": usage.iso(reset) if reset else None,
                             "active": window["active"], "elapsed": round(window["elapsed"], 3) if window.get("elapsed") is not None else None,
                             "observed_at": window.get("observed_at")}
        accounts.append({"lane_key": entry["lane_key"], "provider": entry.get("provider"), "lanes": lanes_for(config, entry["lane_key"]),
                         "classification": assessed["classification"], "reasons": assessed["reasons"], "status": assessed["status"],
                         "observed_at": entry.get("observed_at"),
                         "reading_age_s": round(now - observed.timestamp()) if observed else None,
                         "windows": windows, "probe_due": probe_due(entry, thresholds, now)})
    return {"thresholds": thresholds, "accounts": accounts, "cooling": cooling(config, store, now * 1000)}


def probe_due(entry, thresholds, now):
    """A window read exhausted has since reset and no reading came after the reset.

    Only the spent window counts: an account blocked on seven_day is not back
    because its five_hour window reset."""
    quota, observed = entry.get("quota") or {}, usage.timestamp(entry.get("observed_at"))
    for window in (quota.get("windows") or {}).values():
        reset = usage.timestamp(window.get("resets_at"))
        used = window.get("used")
        spent = used > thresholds["hard"] if used is not None else quota.get("status") == "rejected"
        if spent and reset and observed and observed < reset and reset.timestamp() <= now:
            return True
    return False


def probe(config, workspace, store, dry_run=False, stale_hours=None, now=None):
    """One tiny read-only run per account that is due; `stale_hours` also probes any older reading."""
    now = time.time() if now is None else now
    thresholds = policy.quota_settings(config)
    pool = (config.get("decisions") or {}).get("auto_routes")
    rows = []
    for entry in usage.headroom(store.workspace, include_raw=False):
        if not entry.get("lane_key") or not entry.get("quota"):
            continue
        observed = usage.timestamp(entry.get("observed_at"))
        stale = stale_hours is not None and observed is not None and now - observed.timestamp() > stale_hours * 3600
        if not probe_due(entry, thresholds, now) and not stale:
            continue
        lanes = lanes_for(config, entry["lane_key"])
        lanes.sort(key=lambda name: (pool is not None and name not in pool, lane_settings(config, name).get("cost_tier") or 0))
        row = {"lane_key": entry["lane_key"], "reason": "reset passed with no reading since" if not stale else f"reading older than {stale_hours}h",
               "lane": lanes[0] if lanes else None}
        if not lanes:
            row["skipped"] = "no configured lane uses this account"
        elif not dry_run:
            route = (config.get("routes") or {}).get(row["lane"])
            agent = route["agent"] if route else row["lane"]
            task = core.make_task(workspace, agent, PROBE_TASK, "quota-probe", [], [], None, False, False,
                                  route=row["lane"] if route else None, timeout_seconds=300)
            task["quota_probe"] = True
            result = core.dispatch(config, task, store)
            row.update(run_id=result.get("run_id"), status=result.get("status"), failure_class=result.get("failure_class"),
                       quota=(result.get("quota") or {}).get("status"), cost_usd=(result.get("usage") or {}).get("cost_usd"))
        rows.append(row)
    return {"dry_run": dry_run, "probes": rows}


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else None


def rates(store, days=7, now=None):
    """How much of each window ORC runs consume, from consecutive readings of one account and window.

    A rise between two readings is charged to the ORC runs on that account that
    ended in between. Readings arrive only on ORC runs, so usage by interactive
    or coordinator sessions on the same subscription is folded in: the per-$
    rates are upper bounds on what an ORC run consumes.
    """
    now = time.time() if now is None else now
    since = (now - days * 86400) * 1000
    spans = sorted((s for s in store.traces(limit=1_000_000) if (s.get("end_time_ms") or 0) >= since and s.get("parent_span_id") is None),
                   key=lambda s: s.get("end_time_ms") or 0)
    accounts = {}
    for span in spans:
        lane = span.get("lane_key")
        if not lane:
            continue
        acc = accounts.setdefault(lane, {"runs": [], "readings": []})
        acc["runs"].append(span)
        quota = usage.normalize_quota(span.get("agent"), span.get("quota"), normalized=True)
        if quota:
            acc["readings"].append((span.get("end_time_ms"), quota["windows"]))
    out = []
    for lane, acc in sorted(accounts.items()):
        runs = acc["runs"]
        windows = {}
        for name in WINDOWS:
            series = [(t, w[name]["used"], w[name].get("resets_at")) for t, w in acc["readings"]
                      if name in w and w[name].get("used") is not None]
            rise = 0.0
            cost = tokens = 0.0
            intervals = 0
            for (t1, u1, r1), (t2, u2, r2) in zip(series, series[1:]):
                if r1 != r2 or u2 < u1:
                    continue
                between = [s for s in runs if t1 < (s.get("end_time_ms") or 0) <= t2]
                intervals += 1
                rise += u2 - u1
                for s in between:
                    u = s.get("usage") or {}
                    cost += u.get("cost_usd") or 0
                    tokens += sum(u.get(k) or 0 for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))
            if intervals:
                windows[name] = {"intervals": intervals, "rise": round(rise, 4),
                                 "per_usd": round(rise / cost, 6) if cost else None,
                                 "per_mtok": round(rise / tokens * 1e6, 6) if tokens else None}
        by_lane = {}
        for s in runs:
            by_lane.setdefault(s.get("route") or s.get("agent"), []).append(s)
        lanes = {}
        for name, items in sorted(by_lane.items()):
            costs = [(s.get("usage") or {}).get("cost_usd") for s in items]
            costs = [c for c in costs if isinstance(c, (int, float))]
            minutes = [(s.get("duration_ms") or 0) / 60000 for s in items]
            lanes[name] = {"runs": len(items), "cost_p50": round(pct(costs, .5), 2) if costs else None,
                           "cost_p90": round(pct(costs, .9), 2) if costs else None,
                           "minutes_p50": round(pct(minutes, .5), 1), "minutes_p90": round(pct(minutes, .9), 1)}
            for window, rate in windows.items():
                if rate.get("per_usd") and lanes[name]["cost_p90"] is not None:
                    lanes[name].setdefault("p90_run_share", {})[window] = round(rate["per_usd"] * lanes[name]["cost_p90"], 4)
        out.append({"lane_key": lane, "runs": len(runs), "windows": windows, "lanes": lanes})
    return {"days": days, "accounts": out}


def render_status(view):
    lines = []
    for a in view["accounts"]:
        age = a["reading_age_s"]
        lines.append(f"{a['lane_key']}: {a['classification']}  (reading {age // 3600}h{age % 3600 // 60:02d}m old)" if age is not None
                     else f"{a['lane_key']}: {a['classification']}")
        for name, w in a["windows"].items():
            used = "unknown" if w["used"] is None else f"{w['used']:.0%}"
            lines.append(f"    {name}: {used} used, resets {w['resets_at'] or 'unknown'}{'' if w['active'] else ' (reset passed)'}")
        lines.append(f"    lanes: {', '.join(a['lanes']) or 'none configured'}" + ("  -- probe due" if a["probe_due"] else ""))
        if a["classification"] != "available":
            lines.append(f"    why: {'; '.join(a['reasons'])}")
    for c in view["cooling"]:
        lines.append(f"cooling: {c['lane_key']} model {c['model']} until {c['until']}")
    t = view["thresholds"]
    lines.append(f"thresholds: soft {t['soft']}, hard {t['hard']}, pace_margin {t['pace_margin']}")
    return "\n".join(lines)


def render_rates(view):
    lines = [f"quota consumed by ORC runs, last {view['days']} days. Upper bounds: other sessions on the same account are folded in."]
    for a in view["accounts"]:
        if not a["windows"]:
            continue
        lines.append(f"{a['lane_key']}: {a['runs']} runs")
        for name, w in a["windows"].items():
            per = f"{w['per_usd']:.4f} of the window per $" if w["per_usd"] else (f"{w['per_mtok']:.4f} per M tokens" if w["per_mtok"] else "no attributed rise")
            lines.append(f"    {name}: {per}; {w['intervals']} intervals, total rise {w['rise']:.2f}")
        for name, lane in a["lanes"].items():
            share = ", ".join(f"{k} {v:.1%}" for k, v in (lane.get("p90_run_share") or {}).items())
            cost = f", ${lane['cost_p90']}" if lane["cost_p90"] is not None else ""
            lines.append(f"    {name}: {lane['runs']} runs, p90 {lane['minutes_p90']} min{cost}" + (f", a p90 run uses {share}" if share else ""))
    return "\n".join(lines)


def render_probe(view):
    lines = []
    for p in view["probes"]:
        if p.get("skipped"):
            done = p["skipped"]
        elif view["dry_run"]:
            done = f"would probe {p['lane']}"
        else:
            done = f"probed {p['lane']}: {p.get('status')}" + (f" ({p['failure_class']})" if p.get("failure_class") else "")
        lines.append(f"{p['lane_key']}: {done} -- {p['reason']}")
    return "\n".join(lines) or "no account is due for a probe"


def add_parser(sub):
    quota = sub.add_parser("quota", help="show what each account has left, probe accounts at reset, and measure quota per run")
    quota.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    quota_sub = quota.add_subparsers(dest="quota_command")
    probe_parser = quota_sub.add_parser("probe", help="send one tiny run to each account whose exhausted window reset with no reading since")
    probe_parser.add_argument("--dry-run", action="store_true", help="list the accounts that would be probed")
    probe_parser.add_argument("--stale", type=float, metavar="HOURS", help="also probe accounts whose latest reading is older than HOURS")
    probe_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    rates_parser = quota_sub.add_parser("rates", help="how much of each quota window ORC runs consume")
    rates_parser.add_argument("--days", type=float, default=7)
    rates_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


def run(args, workspace):
    config = core.load_config(workspace)[0]
    store = core.RunStore(workspace)
    if args.quota_command == "probe":
        view = probe(config, workspace, store, dry_run=args.dry_run, stale_hours=args.stale)
        print(json.dumps(view, indent=2) if args.json else render_probe(view))
        return 0 if all(p.get("status") in (None, "success") for p in view["probes"]) else 1
    if args.quota_command == "rates":
        view = rates(store, days=args.days)
        print(json.dumps(view, indent=2) if args.json else render_rates(view))
        return 0
    view = status(config, store)
    print(json.dumps(view, indent=2) if args.json else render_status(view))
    return 0
