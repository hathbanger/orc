"""Advance and inspect Laya's learning loop without the control room open.

`learn tick` runs exactly the step the control-room server runs every three
seconds (ControlRoom.garden_tick), once, and exits. Jobs it starts are the same
detached supervisors, so repeated short invocations from launchd or cron make
the same progress a running server would. Both ticks take the per-workspace
file locks the server takes, so a scheduled tick and a live UI never launch
the same step twice.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time

LABEL = "ai.orc.fusion-learn"
WORKERS = ("claude", "codex", "agy", "grok", "opencode", "orc", "node")
BASE_PATH = ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def add_parser(sub):
    parser = sub.add_parser("learn", help="advance and inspect Laya's learning loop without the control room")
    commands = parser.add_subparsers(dest="learn_command", required=True)

    def scope(command):
        group = command.add_mutually_exclusive_group()
        group.add_argument("--workspace", dest="workspaces", action="append", metavar="W",
                           help="workspace to include (repeatable); defaults to the current workspace")
        group.add_argument("--all", action="store_true",
                           help="every workspace the control room knows about, plus the current one when it has Laya decisions")

    scope(commands.add_parser("tick", help="advance label drafting and training rounds by one step, then exit"))
    scope(commands.add_parser("status", help="read-only summary: is the loop enabled, and is Laya improving?"))
    schedule = commands.add_parser("schedule", help="run `learn tick` periodically (launchd on macOS, a crontab line elsewhere)")
    schedule.add_argument("action", choices=["install", "uninstall", "status"])
    schedule.add_argument("--interval", type=int, default=300, help="seconds between ticks (default 300, minimum 60)")
    scope(schedule)


def room(workspace):
    # ControlRoom's constructor only reads the registry; it writes nothing.
    from fusion_ui import ControlRoom
    return ControlRoom(workspace)


def targets(app, args, workspace):
    """Resolved workspace directories, in a stable order, without duplicates."""
    if args.all:
        # The server's own workspace is never written to the registry, so a
        # workspace only ever opened with `fusion ui` from its directory would
        # otherwise be missed. Other directories (e.g. launchd's cwd) are not.
        paths = [p for p in app.workspaces.values() if p != Path(workspace) or (p / ".fusion/decisions").is_dir()]
    else:
        paths = []
        for value in args.workspaces or [str(workspace)]:
            path = Path(value).expanduser().resolve()
            if not path.is_dir():
                raise ValueError(f"workspace does not exist: {value}")
            app.add_workspace(str(path), save=False)
            paths.append(path)
    return list(dict.fromkeys(paths))


def brief(job):
    return {k: job.get(k) for k in ("id", "action", "status", "decision_id", "started_at_ms")} if isinstance(job, dict) else None


def report(app, workspace, detail=False):
    """One workspace's loop state. Reads only; safe while jobs or a UI run."""
    import fusion_core as core
    import fusion_garden as garden
    import fusion_training_loop as training_loop
    from fusion_learning import decision_rows, learning_summary
    value = {"workspace": str(workspace)}
    try:
        rows = decision_rows(workspace)
        g = garden.status(app, workspace, rows)
        t = training_loop.status(app, workspace, rows)
    except (OSError, ValueError, KeyError, TypeError, SystemExit) as exc:
        return {**value, "error": str(exc)}
    last = (t["rounds"] or [{}])[0]
    value["garden"] = {"enabled": g["enabled"], "state": g["state"], "approval_mode": g["approval_mode"],
                       "labeling_mode": g["labeling_mode"], "queued": g["queued"], "active_job": brief(g["active_job"])}
    value["training"] = {"enabled": t["enabled"], "min_new_answers": t["min_new_answers"], "state": t["state"],
                         "reason": t["reason"], "new_answers": t["new_answers"],
                         "active_job": (t["active_job"] or {}).get("id"),
                         "last_round": {k: last.get(k) for k in ("id", "number", "status", "phase", "started_at_ms",
                                                                  "finished_at_ms", "error")} if last else None}
    if last.get("proof"):
        value["training"]["last_round"].update(outcome=last["proof"].get("outcome"), delta=last["proof"].get("delta"))
    if not detail:
        return value
    value["garden"]["agent"], value["garden"]["latest_job"] = g["agent"], brief(g["latest_job"])
    value["training"].update(approved_answers=t["approved_answers"], groups=t["groups"], completed_rounds=t["completed_rounds"])
    measured = next((r for r in t["rounds"] if r.get("proof")), None)
    if measured:
        proof = measured["proof"]
        value["training"]["measured_round"] = {
            "id": measured.get("id"), "outcome": proof.get("outcome"), "margin": proof.get("margin"),
            "baselines": {name: {k: v.get(k) for k in ("n", "accuracy", "candidate_accuracy")}
                          for name, v in (proof.get("baselines") or {}).items()},
            "questions": training_loop.question_lines(proof.get("questions") or {})}
    try:
        config, _ = core.load_config(workspace)
        summary = learning_summary(workspace, config, rows)
    except (OSError, ValueError, KeyError, TypeError, SystemExit) as exc:
        return {**value, "error": str(exc)}
    counts = summary["counts"]
    value["decisions"] = {"total": summary["total"], "states": counts,
                          "drafts": counts.get("needs_review", 0) + counts.get("needs_evidence", 0),
                          "approved_decisions": summary["reviewed_decisions"],
                          "approved_answers": summary["labeled_questions"],
                          "eligible_questions": summary["eligible_questions"]}
    model = summary["model"]
    value["laya"] = {"mode": model["mode"], "model_path": model["path"], "qualified_buckets": model["qualified_buckets"],
                     "gates": {name: [training_loop.gate_line(g) for g in row["gates"]]
                               for name, row in training_loop.question_table({}, {"buckets": model.get("calibration_buckets") or {}}).items()},
                     "agreement": summary["agreement"]}
    return value


def tick(app, workspaces):
    before = {w: {j["id"] for j in app.jobs(w, limit=None)} for w in workspaces}
    app.garden_tick(workspaces)
    results = []
    for workspace in workspaces:
        value = {"at_ms": int(time.time() * 1000), **report(app, workspace)}
        launched = [brief(j) for j in app.jobs(workspace, limit=None) if j["id"] not in before[workspace]]
        value["launched"] = launched
        if app.garden_errors.get(str(workspace)):
            value["error"] = app.garden_errors[str(workspace)]
        results.append(value)
    return results


def launchctl(*argv):
    return subprocess.run(["launchctl", *argv], capture_output=True, text=True)


def fusion_program():
    # The interpreter running now, and the `fusion` beside this module: the
    # same pair ControlRoom.launch uses. launchd's PATH is minimal, so a bare
    # `#!/usr/bin/env python3` could resolve to an older system Python.
    return [sys.executable, str(Path(__file__).resolve().with_name("fusion"))]


def search_path(which=shutil.which):
    dirs = [str(Path(sys.executable).parent)]
    dirs += [str(Path(found).parent) for name in WORKERS if (found := which(name))]
    return ":".join(dict.fromkeys([*dirs, *BASE_PATH]))


def schedule_arguments(args, workspace):
    program = fusion_program()
    if args.all:
        return [*program, "--workspace", str(workspace), "learn", "tick", "--all"]
    paths = [str(Path(w).expanduser().resolve()) for w in (args.workspaces or [str(workspace)])]
    return [*program, "learn", "tick", *[x for p in paths for x in ("--workspace", p)]]


def paths(home=None):
    home = Path(home or Path.home())
    return home / "Library/LaunchAgents" / (LABEL + ".plist"), home / ".local/share/orc/learn.log"


def rotate_learn_log(log_path, max_size_bytes=5 * 1024 * 1024):
    log_path = Path(log_path)
    if log_path.exists() and log_path.stat().st_size > max_size_bytes:
        rotated = log_path.with_name(log_path.name + ".1")
        rotated.unlink(missing_ok=True)
        log_path.rename(rotated)


def plist(args, workspace, which=shutil.which, home=None):
    _, log = paths(home)
    environment = {"PATH": search_path(which)}
    if os.environ.get("ORC_HOME"):
        environment["ORC_HOME"] = os.environ["ORC_HOME"]  # Where the registry and global config live.
    # AbandonProcessGroup: jobs a tick launches must outlive the tick. They
    # already start in their own session; this keeps launchd from reaping them.
    return {"Label": LABEL, "ProgramArguments": schedule_arguments(args, workspace), "StartInterval": args.interval,
            "RunAtLoad": True, "AbandonProcessGroup": True, "ProcessType": "Background",
            "WorkingDirectory": str(workspace), "EnvironmentVariables": environment,
            "StandardOutPath": str(log), "StandardErrorPath": str(log)}


def crontab_line(args, workspace, which=shutil.which, home=None):
    import shlex
    _, log = paths(home)
    minutes = max(1, args.interval // 60)
    when = f"*/{minutes} * * * *" if minutes < 60 else f"0 */{max(1, minutes // 60)} * * *"
    command = " ".join(shlex.quote(a) for a in schedule_arguments(args, workspace))
    return f"{when} cd {shlex.quote(str(workspace))} && PATH={shlex.quote(search_path(which))} {command} >> {shlex.quote(str(log))} 2>&1"


def schedule(args, workspace, *, run=launchctl, platform=None, which=shutil.which, home=None, out=None):
    out = out or sys.stdout
    if args.interval < 60:
        raise ValueError("--interval must be at least 60 seconds")
    if (platform or sys.platform) != "darwin":
        line = crontab_line(args, workspace, which, home)
        if args.action == "install":
            print("launchd is macOS-only. Add this line with `crontab -e`:\n" + line, file=out)
        elif args.action == "uninstall":
            print("Remove the `learn tick` line from `crontab -e`. It looks like:\n" + line, file=out)
        else:
            print(line, file=out)
        return 0
    target, log = paths(home)
    domain = f"gui/{os.getuid()}"
    if args.action == "status":
        loaded = run("print", f"{domain}/{LABEL}").returncode == 0
        value = {"installed": target.exists(), "loaded": loaded, "plist": str(target), "log": str(log)}
        if target.exists():
            saved = plistlib.loads(target.read_bytes())
            value.update(program=saved.get("ProgramArguments"), interval=saved.get("StartInterval"))
        try:
            value["log_tail"] = log.read_text(errors="replace").splitlines()[-5:]
        except OSError:
            value["log_tail"] = []
        print(json.dumps(value, indent=2), file=out)
        return 0
    # bootstrap refuses a label that is already loaded; unload first, ignoring "not loaded".
    run("bootout", f"{domain}/{LABEL}")
    if args.action == "uninstall":
        existed = target.exists()
        target.unlink(missing_ok=True)
        print(f"removed {target}" if existed else f"not installed: {target}", file=out)
        return 0
    value = plist(args, workspace, which, home)
    log.parent.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(value))
    result = run("bootstrap", domain, str(target))
    print(f"wrote {target}:\n" + target.read_text() + f"\nlaunchctl bootstrap {domain} {target}: "
          + ("loaded" if result.returncode == 0 else f"failed ({result.returncode}) {result.stderr.strip()}"), file=out)
    return result.returncode


def run(args, workspace, out=None):
    out = out or sys.stdout
    command = args.learn_command
    if command == "schedule":
        return schedule(args, workspace, out=out)
    if command == "tick":
        _, log = paths()
        rotate_learn_log(log)
    app = room(workspace)
    selected = targets(app, args, workspace)
    if command == "tick":
        results = tick(app, selected)
        for value in results:
            print(json.dumps(value, ensure_ascii=False, sort_keys=True), file=out, flush=True)
    else:
        results = [report(app, w, detail=True) for w in selected]
        print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True), file=out)
    return 1 if any(r.get("error") for r in results) else 0
