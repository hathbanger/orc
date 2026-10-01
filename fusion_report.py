"""Recover public deliverables and render useful reports without running agents."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import shlex

import fusion_progress as progress


def terminal_text(text):
    # Keep Markdown layout, but never replay terminal controls from worker output.
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", str(text))
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(char for char in text if char.isprintable() or char in "\n\t")


def command(workspace, *args):
    return "orc 'fusion' " + shlex.join(["--workspace", str(workspace), *args])


def read_answer(workspace, result):
    """Prefer the saved answer; recover legacy answers from provider envelopes."""
    import fusion_core as core
    artifacts = result.get("artifacts") or {}
    root = (Path(workspace) / ".fusion").resolve()
    candidates = []
    run_id = result.get("run_id")
    for kind, filename in (("answer", "answer.md"), ("stdout", "stdout.log")):
        paths = [Path(artifacts[kind])] if artifacts.get(kind) else []
        if run_id and Path(run_id).name == run_id and run_id not in {".", ".."}:
            paths.append(root / "runs" / run_id / filename)
        candidates.extend((kind, path) for path in paths)
    for kind, path in candidates:
        if not path.is_absolute():
            path = Path(workspace) / path
        if not path.resolve().is_relative_to(root):
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if kind == "answer":
            text = raw
        elif result.get("agent") == "codex":
            # Select only public completed messages. Commands, reasoning, and
            # truncated events must never become a report's supposed answer.
            messages = []
            for line in raw.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                item = event.get("item")
                if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    messages.append(item["text"])
            text = messages[-1] if messages else ""
        elif result.get("agent") == "grok":
            text = raw  # The native Grok adapter requests its plain headless output.
        elif result.get("agent") == "opencode":
            text = core.parse_opencode_output(raw)[1]
        elif result.get("agent") in {"claude", "agy"}:
            try:
                envelope = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(envelope, dict):
                continue
            parser = core.parse_claude_output if result["agent"] == "claude" else core.parse_agy_output
            try:
                text = parser(raw)[1]
            except (TypeError, ValueError):
                continue
        else:
            continue
        if text.strip():
            return {"text": terminal_text(text).strip(), "source": kind, "path": str(path), "warning": None}
    return {"text": terminal_text(result.get("summary") or ""), "source": "summary", "path": None,
            "warning": "Full worker answer unavailable; showing the saved summary."}


def findings(text):
    """Recognize numbered recommendation headings, excluding lists in code fences."""
    matches = []
    offset, fence = 0, None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        elif fence is None:
            match = re.match(r"^(?:#{1,4}\s+)?(\d+)[.)]\s+\*\*([^\n]+?)\*\*", line)
            if match:
                matches.append((int(match[1]), match[2].rstrip(". :"), offset))
        offset += len(line)
    if len({item[0] for item in matches}) != len(matches):
        return []  # Ambiguous numbering needs an explicit worker follow-up.
    return [{"number": number, "title": title,
             "text": text[start:matches[index + 1][2] if index + 1 < len(matches) else len(text)].strip()}
            for index, (number, title, start) in enumerate(matches)]


def reported_cost(usage):
    for key in ("cost_usd", "cost"):
        value = (usage or {}).get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            number = float(value)
        except (ValueError, TypeError):
            continue
        if math.isfinite(number) and number >= 0:
            return number
    return None


def select_report(report, *, node=None, finding=None, all_nodes=False):
    outputs = report["outputs"]
    if node:
        if node not in report["node_ids"]:
            raise ValueError(f"unknown node {node!r}; choose from: {', '.join(report['node_ids'])}")
        selected = [item for item in outputs if item["node_id"] == node]
    else:
        selected = [item for item in outputs if all_nodes or item["node_id"] in report["primary_nodes"]]
    selection = None
    if finding is not None:
        if len(selected) != 1:
            raise ValueError("--finding needs one completed output; select it with --node")
        selection = next((item for item in selected[0]["findings"] if item["number"] == finding), None)
        if selection is None:
            available = ", ".join(str(item["number"]) for item in selected[0]["findings"]) or "none"
            raise ValueError(f"finding {finding} is unavailable; numbered findings: {available}")
        if selected[0]["status"] == "success" and report["status"] == "success":
            selection = {**selection, "prepare_command": command(report["workspace"], "--progress", "build", "--from-workflow", report["workflow_id"], "--from-node", selected[0]["node_id"], "--finding", str(finding), "--plan-only")}
        else:
            selection = {**selection, "prepare_command": None}
    return {**report, "selected_outputs": selected, "selected_finding": selection}


def finding_request(workspace, run_id, number, node=None):
    from fusion_workflow import workflow_report
    report = select_report(workflow_report(workspace, run_id), node=node, finding=number)
    if not report["selected_finding"]["prepare_command"]:
        raise ValueError("prepare implementation from a successful workflow and accepted finding")
    source_command = command(workspace, "--json", "workflow", "report", run_id, "--node", report["selected_outputs"][0]["node_id"], "--finding", str(number))
    # Reference the complete evidence separately: the previous audit's scope
    # restrictions are historical data, not restrictions on this new request.
    return (f"Implement only recommendation {number} from completed workflow {run_id}. "
            f"Read its finding, evidence, acceptance criteria and caveats with: {source_command}\n"
            "This is a new implementation task. Revalidate the finding against the current checkout; "
            "follow repository ownership and worktree rules, preserve existing work, add meaningful regression coverage, "
            "run the relevant verification, and obtain an independent review.")


def format_report(report, *, brief=False):
    lines = [f"Workflow {report['workflow_id']}: {str(report['status']).upper()}"]
    stages = [node for wave in report["waves"] for node in wave["nodes"]]
    succeeded = sum(node["status"] == "success" for node in stages)
    lines.append(f"{succeeded}/{len(stages)} stages succeeded. " + ("Analysis only; implementation was not requested." if report["read_only"] else "Implementation permitted; inspect changes and verification below."))
    task = progress.clean(report.get("task") or "", 100000)
    lines.append(f"Task: {task[:297] + '...' if len(task) > 300 else task}")
    cost = report["cost"]
    if not cost["reported_calls"]:
        lines.append("Cost: not reported by workers.")
    else:
        lines.append(f"Reported cost: ${report['spent_usd']:.4f} ({cost['reported_calls']}/{cost['calls']} calls supplied cost; not a billing total).")
    if report.get("budget_usd"):
        lines.append(f"Recorded-spend budget: ${report['budget_usd']:.2f}")
    publication = report.get("publication") or {}
    if publication.get("url"):
        lines.append(f"PR: {terminal_text(publication['url'])} (base: {terminal_text(publication.get('base', ''))})")
    elif publication.get("status"):
        lines.append(f"Publication: {terminal_text(publication['status'])}")
        if publication.get("error"):
            lines.append(f"Publication error: {terminal_text(publication['error'])}")
    lines.append("")
    attempts = report.get("attempt_ledger") or []
    if any(item.get("repair") for item in attempts):
        sequence = [f"{item.get('node_id', 'unknown')} #{item.get('attempt', '?')}"
                    + (" (repair)" if item.get("repair") else "") for item in attempts]
        lines.append("Attempt sequence: " + " → ".join(sequence))
    for node in stages:
        duration = f", {progress.elapsed(node['duration_ms'] / 1000)}" if node.get("duration_ms") is not None else ""
        lines.append(f"  {node['id']} [{node['agent']}]: {node['status']} (attempts={node['attempts']}{duration})")
        if brief and node.get("summary"):
            lines.append(f"    {progress.clean(node['summary'], 400)}")
        for warning in node.get("acceptance_warnings", []):
            lines.append(f"    Warning: {terminal_text(warning)}")
        if node.get("check_inputs_changed"):
            lines.append("    Untrusted check evidence (tampered): " + ", ".join(terminal_text(p) for p in node["check_inputs_changed"]))
        for kind, decision in (node.get("decisions") or {}).items():
            if decision.get("status") != "ok":
                continue
            picks = ", ".join(f"{question}={item['value']} ({item['probability']:.2f})" for question, item in decision.get("recommendations", {}).items())
            lines.append(f"    laya {kind}: {picks} -> {decision.get('actual')} [{'applied' if decision.get('applied') else 'advisory'}]")
    changed = list(dict.fromkeys(path for node in stages for path in node.get("changed", [])))
    lines.append("Changes reported: " + (", ".join(terminal_text(path) for path in changed) if changed else "none"))
    for problem in report["acceptance_problems"]:
        lines.append(f"Acceptance: {terminal_text(problem)}")
    for blocker in report["blockers"]:
        lines.append(f"Blocker ({blocker['node_id']}): {terminal_text(blocker['blocker'])}")
    for agent, lane in report.get("lanes", {}).items():
        if lane.get("status") != "available":
            lines.append(f"Lane {agent}: {lane.get('status')} — {progress.clean(lane.get('reason', ''), 400)}")
    selection = report.get("selected_finding")
    if not brief:
        for output in report["selected_outputs"]:
            lines.extend(["", f"--- {output['node_id']} output ({output['status']}) ---", ""])
            text = selection["text"] if selection else output["text"]
            # Presentation only: the receipt's status and acceptance remain authoritative.
            text = re.sub(r"^STATUS:[^\n]*\n+", "", text, count=1, flags=re.I)
            text = re.sub(r"^SUMMARY:\s*", "", text, count=1, flags=re.I)
            lines.append(text or "No final answer recorded.")
            if output.get("warning"):
                lines.append(output["warning"])
            if selection:
                lines.append("\nThis excerpt is one recommendation. The full node output retains the audit's checks and caveats.")
            if output.get("path"):
                lines.append(f"\nSource: {output['path']}")
        if not report["selected_outputs"]:
            lines.extend(["", "No completed worker output is available for this selection."])
    groups = report.get("usage", {}).get("by_route", [])
    gates = [group for group in groups if group.get("agent") == "gate"]
    groups = [group for group in groups if group.get("agent") != "gate"]
    if gates:
        lines.append(f"Gate: {sum(g['success'] for g in gates)} accepted, {sum(g['failed'] for g in gates)} rejected")
    if groups:
        lines.extend(["", "Usage:"])
        for group in groups:
            identity = f"{group.get('agent') or 'unknown worker'} · {group.get('route') or 'native'} · {group.get('model') or 'model not reported'}"
            lines.append(f"  {identity}: {group['calls']} calls, {group['success']} succeeded, {group['failed']} unsuccessful")
    lines.extend(["", "Next:"])
    if report["status"] == "running":
        lines.append("  Watch progress: " + command(report["workspace"], "workflow", "watch", report["workflow_id"]))
    elif report.get("resume_command"):
        lines.append("  After addressing blockers, resume: " + report["resume_command"])
    if selection and selection.get("prepare_command"):
        lines.append("  Prepare this implementation (saves a brief and workflow; starts no coding agents):")
        lines.append("  " + selection["prepare_command"])
    elif not selection:
        for output in report["selected_outputs"]:
            for item in output["findings"]:
                lines.append(f"  {item['number']}. {item['title']}")
                lines.append("     " + command(report["workspace"], "workflow", "report", report["workflow_id"], "--node", output["node_id"], "--finding", str(item["number"])))
    lines.append("  All stage outputs: " + command(report["workspace"], "workflow", "report", report["workflow_id"], "--all"))
    lines.append("  Save Markdown: " + command(report["workspace"], "workflow", "report", report["workflow_id"], "--output", str(Path(report["workspace"]) / ".fusion" / "workflows" / report["workflow_id"] / "report.md")))
    return terminal_text("\n".join(lines)) + "\n"
