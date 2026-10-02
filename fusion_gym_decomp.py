"""Byte-exact decompilation tasks graded by an external grader CLI.

ORC does not compile or diff anything itself. Tasks are `tenet.decomp-task.v1`
JSON files, and a grader command (default `python3 -m decomp_gym`) does the
work:

    <grader> extract --repo <git dir> --commits <sha>... --out <dir>
    <grader> grade --task <task.json> --candidate <dir> --out <dir> --json
                   [--binary <path>] [--image <ref>] [--lane] [--model]
                   [--cost-usd] [--tokens-in] [--tokens-out]

`grade` exits 0 (pass), 1 (fail), 2 (malformed task or usage: no outcome, last
line `{"error", "message"}`) or 3 (hold: infrastructure, never a fail). On
0, 1 and 3 its last stdout line is one `tenet.decomp-outcome.v1` object.
ORC records every outcome as a gym result row of kind `decomp`; a hold is
recorded but counts as neither an attempt nor a success. The original binary
never passes through ORC: without `--binary` the grader reads its own
environment. `game` and `stratum` are opaque strings to ORC.

A task file is opaque to ORC beyond `schema`, `task_id`, `game` and
`stratum`: the grader validates the rest (exit 2). The grader resolves paths
in a task against the task file's own directory, so ORC passes task files
where they are and never copies one away from its directory. The candidate
is the whole source tree the grader compiles from.

`run` (`gym run --kind decomp`) gives each task to each lane in a fresh
disposable repository outside the gym, like interpret: the baseline commit
holds the starting tree (the task's `base_commit` of `--repo-path`, else the
`start/` directory next to the task file; nothing else from the task's
directory) and `check.py`, which runs the grader on the tree. After the
worker exits, any change outside the task's `editable` files is tampering,
and the tree is graded officially.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid

TASK_SCHEMA = "tenet.decomp-task.v1"
OUTCOME_SCHEMA = "tenet.decomp-outcome.v1"
EXTRACT_SCHEMA = "tenet.decomp-extract.v1"
DEFAULT_GRADER = "python3 -m decomp_gym"
KIND = "decomp"
# Exit code -> verdict. 2 has no outcome and is refused, not recorded.
VERDICTS = {0: "matched", 1: "unmatched", 3: "hold"}
OUTCOME_FIELDS = ("schema", "task_id", "game", "stratum", "pass", "fail_reason", "bytes_exact", "wall_s", "host",
                  "toolchain_sha256", "task_sha256", "started_at", "ended_at", "lane", "model", "cost_usd",
                  "tokens_in", "tokens_out")
# fail_reason per exit code: a fail names what failed; a hold is the machine's.
FAIL_REASONS = ("f2p", "link", "closure", "judge")
FAIL_PREFIXES = ("p2p:", "fake_scan:")
HOLD_REASONS = ("infra", "timeout")


def _text(value):
    return isinstance(value, str) and bool(value)


def validate_task(task):
    """The fields ORC reads: schema, task_id, game and stratum. The grader
    owns the rest of the task's shape and refuses a malformed one (exit 2)."""
    if not isinstance(task, dict) or task.get("schema") != TASK_SCHEMA:
        raise ValueError(f"not a {TASK_SCHEMA} task: schema")
    for field in ("task_id", "game", "stratum"):
        if not _text(task.get(field)):
            raise ValueError(f"not a {TASK_SCHEMA} task: {field}")
    return task


def load_tasks(path):
    """Decomp tasks from a task file or a directory of them, directly or one
    per subdirectory (`<task-dir>/task.json`, as the extractor writes them).
    Other JSON, such as the extractor's extract.json or a task's objdiff
    config, is skipped."""
    path = Path(path)
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    nested = sorted(path.glob("*/*.json")) if path.is_dir() else []
    tasks = []
    for file in [*files, *nested]:
        try:
            value = json.loads(file.read_text())
        except (OSError, ValueError) as exc:
            if file in nested:
                continue
            raise ValueError(f"cannot read decomp task {file}: {exc}") from exc
        if isinstance(value, dict) and value.get("schema") == TASK_SCHEMA:
            tasks.append((file, validate_task(value)))
    if not tasks:
        raise ValueError(f"no {TASK_SCHEMA} tasks in {path}")
    return tasks


def grader_command(grader=None):
    argv = shlex.split(grader or DEFAULT_GRADER)
    if not argv:
        raise ValueError("the decomp grader command is empty")
    return argv


def grade_argv(task_path, candidate, out, grader=None, binary=None, image=None, lane=None, model=None,
               cost_usd=None, tokens_in=None, tokens_out=None):
    argv = [*grader_command(grader), "grade", "--task", str(task_path), "--candidate", str(candidate),
            "--out", str(out), "--json"]
    for flag, value in (("--binary", binary), ("--image", image), ("--lane", lane), ("--model", model),
                        ("--cost-usd", cost_usd), ("--tokens-in", tokens_in), ("--tokens-out", tokens_out)):
        if value is not None:
            argv += [flag, str(value)]
    return argv


def _last_json(stdout):
    lines = [line for line in (stdout or "").splitlines() if line.strip()]
    if not lines:
        return None
    try:
        value = json.loads(lines[-1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _fits(exit_code, reason):
    if exit_code == 0:
        return reason is None
    if exit_code == 3:
        return reason in HOLD_REASONS
    return reason in FAIL_REASONS or (
        isinstance(reason, str) and any(reason.startswith(p) and len(reason) > len(p) for p in FAIL_PREFIXES))


def import_outcome(exit_code, stdout, task=None):
    """(verdict, outcome) from the grader's exit code and stdout. Exit 2 and
    anything outside the contract raise ValueError: there is no outcome to
    record, and guessing one would mislabel a lane."""
    last = _last_json(stdout)
    if exit_code == 2:
        detail = (last or {}).get("message") or (last or {}).get("error") or "no message"
        raise ValueError(f"decomp grader refused the task (exit 2): {detail}")
    if exit_code not in VERDICTS:
        raise ValueError(f"decomp grader exited {exit_code}, outside the contract (0, 1, 2, 3)")
    if not last or last.get("schema") != OUTCOME_SCHEMA:
        raise ValueError(f"decomp grader exit {exit_code} without a {OUTCOME_SCHEMA} last line")
    missing = [field for field in OUTCOME_FIELDS if field not in last]
    if missing:
        raise ValueError(f"decomp outcome lacks {', '.join(missing)}")
    if not isinstance(last["pass"], bool) or last["pass"] != (exit_code == 0):
        raise ValueError(f"decomp outcome pass={last['pass']!r} contradicts exit {exit_code}")
    if not _fits(exit_code, last["fail_reason"]):
        raise ValueError(f"decomp outcome fail_reason {last['fail_reason']!r} does not fit exit {exit_code}")
    if task is not None:
        for field in ("task_id", "game", "stratum"):
            if last[field] != task[field]:
                raise ValueError(f"decomp outcome {field} {last[field]!r} is not the task's {task[field]!r}")
    return VERDICTS[exit_code], {field: last[field] for field in OUTCOME_FIELDS}


def result_row(task, lane_name, lane_spec, verdict, outcome, key, duration_ms):
    return {"schema": "fusion.gym.result.v1", "event": "finished", "key": key, "task": task["task_id"],
            "kind": KIND, "mode": "hidden", "lane": lane_name, "lane_spec": lane_spec,
            "stratum": task["stratum"], "completed": True, "verdict": verdict,
            "fail_reason": outcome["fail_reason"], "bytes_exact": outcome["bytes_exact"],
            "cost_usd": float(outcome.get("cost_usd") or 0), "duration_ms": duration_ms,
            "outcome": outcome, "finished_at_ms": int(time.time() * 1000)}


def grade(task_path, task, candidate, out, lane_name, lane_spec, key, grader=None, binary=None, image=None,
          cost_usd=None, tokens_in=None, tokens_out=None, timeout=None, runner=subprocess.run):
    argv = grade_argv(task_path, candidate, out, grader, binary, image, lane_name, lane_spec.get("model"),
                      cost_usd, tokens_in, tokens_out)
    started = time.monotonic()
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ValueError(f"cannot run the decomp grader {argv[0]}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"the decomp grader ran past {timeout}s and was abandoned; nothing was recorded") from exc
    verdict, outcome = import_outcome(proc.returncode, proc.stdout, task)
    return result_row(task, lane_name, lane_spec, verdict, outcome, key, int((time.monotonic() - started) * 1000))


def extract(repo, commits, out, grader=None, runner=subprocess.run):
    """Run the grader's extractor and check the fields ORC reads in each task
    it wrote. Exit 2 and 3 are errors here: extraction has no hold to record."""
    argv = [*grader_command(grader), "extract", "--repo", str(repo), "--commits", *commits, "--out", str(out)]
    try:
        proc = runner(argv, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ValueError(f"cannot run the decomp grader {argv[0]}: {exc}") from exc
    last = _last_json(proc.stdout) or {}
    if proc.returncode != 0:
        raise ValueError(f"decomp extraction failed (exit {proc.returncode}): "
                         f"{last.get('error', 'error')}: {last.get('message', (proc.stderr or '').strip()[-400:])}")
    if last.get("schema") != EXTRACT_SCHEMA:
        raise ValueError(f"decomp extraction printed no {EXTRACT_SCHEMA} summary")
    for entry in last.get("tasks") or []:
        file = Path(out) / entry["file"]
        try:
            validate_task(json.loads(file.read_text()))
        except (OSError, ValueError) as exc:
            raise ValueError(f"decomp extraction wrote an unreadable task {file}: {exc}") from exc
    return last


def counted(row):
    """Whether a decomp row counts toward priors, and whether it succeeded.
    Holds measured the machine, not the lane; a tampered run graded itself."""
    if row.get("tampered"):
        return False, False
    return row.get("verdict") in {"matched", "unmatched"}, row.get("verdict") == "matched"


# ---------------------------------------------------------------- running lanes

CHECKER = "check.py"
SCRATCH = ".decomp-out"
CHECK_ARGV = ["python3", CHECKER]
# Paths a worker tree never reports as its own edits: the checker's scratch
# output and the ORC/Git metadata snapshots never include.
_UNTRACKED_OK = {SCRATCH, ".git", ".fusion", ".fusion.json", ".orc.json"}
CHECK_SCRIPT = '''"""Grade this tree with the decomp grader: `python3 check.py` prints PASS when the target matches."""
import json
import subprocess
import sys
from pathlib import Path

ARGV = {argv}
root = Path(__file__).resolve().parent
(root / {scratch!r}).mkdir(exist_ok=True)
proc = subprocess.run(ARGV, cwd=str(root), capture_output=True, text=True)
last = {{}}
for line in reversed((proc.stdout or "").splitlines()):
    if line.strip():
        try:
            last = json.loads(line)
        except ValueError:
            last = {{}}
        break
if not isinstance(last, dict):
    last = {{}}
if proc.returncode == 0:
    print("PASS")
elif proc.returncode == 1:
    print("FAIL: " + str(last.get("fail_reason")))
elif proc.returncode == 3:
    print("HOLD (the grader's machine, not your code): " + str(last.get("fail_reason")))
else:
    print("ERROR (exit %d): %s" % (proc.returncode, last.get("message") or last.get("error") or "no message"))
log = [line for line in (proc.stderr or "").splitlines() if line.strip()]
if log:
    print("grader log:")
    for line in log[-200:]:
        print("  " + line)
sys.exit(proc.returncode)
'''


def _target(task):
    """(functions, unit source) the brief names."""
    target = task.get("target") if isinstance(task.get("target"), dict) else {}
    functions = target.get("functions") or ([target["function"]] if target.get("function") else [])
    source = target.get("tu")
    if not source:
        for unit in task.get("units") or []:
            if isinstance(unit, dict) and unit.get("name") == target.get("unit"):
                source = unit.get("source")
                break
    return [str(name) for name in functions], source


def editable_paths(task):
    """The files a worker may change: the task's `editable`, else the target
    unit's source."""
    editable = task.get("editable")
    if isinstance(editable, list) and editable:
        paths = [str(path) for path in editable]
    else:
        _, source = _target(task)
        paths = [source] if source else []
    return sorted({str(PurePosixPath(path)) for path in paths})


def brief(task, editable, extra=""):
    functions, source = _target(task)
    unit = (task.get("target") or {}).get("unit") if isinstance(task.get("target"), dict) else None
    lines = [f"Decompile {', '.join(functions) or 'the target function'} so it compiles to exactly the original bytes.",
             "",
             f"Target function(s): {', '.join(functions) or 'see below'}",
             f"Unit source: {source or 'see below'}" + (f" (unit {unit})" if unit else ""),
             f"Stratum: {task['stratum']}",
             "Editable files (edit only these; any other change, including to check.py, invalidates the run):",
             *[f"- {path}" for path in editable],
             "",
             "Rules: no inline asm, no `register`, no `#pragma` and no `__attribute__`.",
             "Run `python3 check.py` to grade the tree; it must print PASS. It prints the grader's verdict, "
             "fail_reason and log lines.",
             "Other functions in the same file may show as not exact while the target is wrong: GCC optimizes the "
             "whole file, and they return to exact once the target matches."]
    text = "\n".join(lines)
    if extra.strip():
        text += "\n\n" + extra.strip() + "\n"
    return text


def build_spec(task, lane, editable, extra="", budget_remaining=None):
    """One write node. The checker is the only command the worker is allowed
    beyond the agent's defaults; the coordinator grades after the run."""
    task_text = brief(task, editable, extra)
    node = {"id": "decompile", "role": "implementation", "agent": lane["agent"], "write": True,
            "task": task_text, "decision_context": task_text.split("\n\n")[0],
            "acceptance": {"required_handoff": ["summary"]}, "verification_argv": [list(CHECK_ARGV)]}
    for key in ("route", "model", "reasoning_effort"):
        if lane.get(key):
            node[key] = lane[key]
    if budget_remaining is not None and lane["agent"] == "claude":
        node["max_budget_usd"] = round(max(budget_remaining, 0.01), 2)
    return {"task": f"gym decomp {task['task_id']} ({task['stratum']})", "max_attempts": 1,
            "budget_usd": max(budget_remaining, 0.01) if budget_remaining is not None else 0, "nodes": [node]}


def start_source(task_path, task, repo_path=None):
    """Where a task's starting tree comes from: ("repo", commit), ("start",
    directory next to the task file), or None."""
    if repo_path and task.get("base_commit"):
        return "repo", str(task["base_commit"])
    start = Path(task_path).parent / "start"
    if start.is_dir():
        return "start", start
    return None


def copy_start(start, destination):
    """Copy start/ (and nothing else from the task directory). Links must stay
    inside start/, so none can expose the task's answer-bearing files."""
    start = Path(start).resolve()
    destination = Path(destination)
    for directory, dirs, files in os.walk(start, followlinks=False):
        here = Path(directory)
        dirs[:] = [name for name in dirs if name != ".git"]
        for name in sorted([*dirs, *files]):
            source = here / name
            relative = source.relative_to(start)
            target = destination / relative
            if source.is_symlink():
                link = os.readlink(source)
                if os.path.isabs(link) or not (source.parent / link).resolve().is_relative_to(start):
                    raise ValueError(f"start/{relative} links outside start/")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(link, target)
                if name in dirs:
                    dirs.remove(name)
            elif source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)


def prepare_tree(tree, source, repo_path, task_path, grader=None, binary=None, image=None):
    """A fresh repository holding only the starting tree and check.py, in one
    baseline commit. Returns the baseline sha."""
    from fusion_gym import archive, git
    tree.mkdir(parents=True)
    if source[0] == "repo":
        archive(Path(repo_path), source[1] + "^{commit}", tree)
    else:
        copy_start(source[1], tree)
    argv = grade_argv(Path(task_path).resolve(), ".", SCRATCH, grader, binary, image)
    (tree / CHECKER).write_text(CHECK_SCRIPT.format(argv=json.dumps(argv), scratch=SCRATCH))
    git(tree, "init", "-q")
    exclude = tree / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(f"/{SCRATCH}/\n")
    git(tree, "add", "-A", "-f")
    git(tree, "-c", "user.name=Fusion Gym", "-c", "user.email=gym@localhost", "-c", "commit.gpgsign=false",
        "commit", "-qm", "decomp task baseline", "--no-verify")
    return git(tree, "rev-parse", "HEAD").strip()


def changed_paths(tree, baseline):
    """(changed paths against the baseline, patch bytes). New files the tree's
    own .gitignore hides are changes too: the grader compiles the whole tree."""
    from fusion_gym import git
    from fusion_publish import snapshot
    snap = snapshot(tree)
    changed = {p for p in git(tree, "diff", "--name-only", "--no-renames", "-z", baseline, snap).split("\0") if p}
    ignored = git(tree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").split("\0")
    changed |= {p for p in ignored if p and not set(PurePosixPath(p).parts) & _UNTRACKED_OK}
    return sorted(changed), git(tree, "diff", "--binary", baseline, snap, binary=True)


def _isolate(config, lane, control, tree):
    """Interpret's isolation: the worker's Fusion environment names only the
    disposable tree and control store, never the gym or the task directory."""
    isolated = copy.deepcopy(config)
    env = {"FUSION_CONTROL_WORKSPACE": str(control), "FUSION_WORKSPACE": str(tree),
           "FUSION_CONFIG": str(control / ".fusion.json"), "PWD": str(tree)}
    settings = isolated.setdefault(lane["agent"], {})
    settings["env"] = {**settings.get("env", {}), **env}
    if lane.get("route"):
        settings = isolated["routes"][lane["route"]]
        settings["env"] = {**settings.get("env", {}), **env}
    (control / ".fusion.json").write_text(json.dumps(isolated))
    return isolated


def _tokens(result):
    usage = result.get("usage") or {}
    values = []
    for field in ("input_tokens", "output_tokens"):
        value = usage.get(field)
        values.append(int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value else None)
    return values


def run_lane(task_path, task, source, name, lane, gym, config, runner, workflow_id, remaining, repo_path=None,
             grader=None, binary=None, image=None, keep_worktrees=False, timeout=None):
    """One lane on one task in a fresh disposable repository outside the gym,
    then the official grade of the tree it left. Raises ValueError when the
    starting tree cannot be made (nothing ran)."""
    import fusion_core as core
    import fusion_gym as gym_api
    gym = Path(gym)
    key = gym_api.result_key(task["task_id"], name, "hidden", KIND)
    evidence = gym / "results" / gym_api._slug(task["task_id"]) / KIND / gym_api._slug(name)
    editable = editable_paths(task)
    prompt = Path(task_path).parent / "prompt.md"
    extra = prompt.read_text(encoding="utf-8", errors="replace") if prompt.is_file() else ""
    with tempfile.TemporaryDirectory(prefix="fusion-gym-decomp-") as temporary:
        root = Path(temporary).resolve()
        tree = root / "tree"
        try:
            baseline = prepare_tree(tree, source, repo_path, task_path, grader, binary, image)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise ValueError(f"cannot make the starting tree: {exc}") from exc
        evidence.mkdir(parents=True, exist_ok=True)
        control = root / "control"
        control.mkdir()
        isolated = _isolate(config, lane, control, tree)
        started = time.monotonic()
        token = core._CONTROL_WORKSPACE.set(control)
        try:
            spec = build_spec(task, lane, editable, extra, remaining)
            outcome = runner(control, isolated, spec, run_id=workflow_id,
                             worktree={"workspace": str(tree), "base_sha": baseline}).run()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            outcome = {"workflow_id": workflow_id, "status": "error", "error": str(exc), "nodes": []}
        finally:
            core._CONTROL_WORKSPACE.reset(token)
        worker_ms = round((time.monotonic() - started) * 1000)
        result = ((outcome.get("nodes") or [{}])[0].get("result") or {})
        status = outcome.get("status")
        cost = float(outcome.get("spent_usd") or 0)
        tokens_in, tokens_out = _tokens(result)
        row = {"schema": "fusion.gym.result.v1", "event": "finished", "key": key, "task": task["task_id"],
               "kind": KIND, "mode": "hidden", "lane": name, "lane_spec": lane, "stratum": task["stratum"],
               "workflow_id": outcome.get("workflow_id") or workflow_id, "status": status,
               "start": source[0], "editable": editable,
               "worker": {k: result.get(k) for k in ("status", "agent", "route", "model", "run_id")},
               "cost_usd": cost, "tokens_in": tokens_in, "tokens_out": tokens_out, "worker_ms": worker_ms,
               "duration_ms": worker_ms, "finished_at_ms": int(time.time() * 1000)}
        if outcome.get("error"):
            row["error"] = outcome["error"]
        diff, patch = [], b""
        try:
            diff, patch = changed_paths(tree, baseline)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            row.update(verdict="invalid_snapshot", completed=False, snapshot_error=str(exc))
        allowed = set(editable)
        tampered = [path for path in diff if path not in allowed]
        row.update(changed=diff, tampered=tampered)
        patch_path = evidence / f"{workflow_id}.patch"
        patch_path.write_bytes(patch)
        row["patch"] = str(patch_path)
        if "verdict" in row:
            pass
        elif not result.get("run_id") or status in {"paused_quota", "paused_budget", "interrupted"}:
            # The lane never worked on the tree: grading it would mislabel the lane.
            row.update(verdict="unavailable" if outcome.get("lanes") else status or "not_run", completed=False)
        else:
            shutil.rmtree(tree / SCRATCH, ignore_errors=True)
            out = evidence / f"{workflow_id}.grade"
            out.mkdir(parents=True, exist_ok=True)
            try:
                graded = grade(Path(task_path).resolve(), task, tree, out, name, lane, key, grader, binary, image,
                               cost, tokens_in, tokens_out, timeout)
            except ValueError as exc:
                row.update(verdict="grade_error", completed=False, grade_error=str(exc))
            else:
                verdict = graded["verdict"]
                if tampered and verdict != "hold":
                    row["graded_verdict"] = verdict
                    verdict = "tampered"
                row.update(verdict=verdict, completed=True, fail_reason=graded["fail_reason"],
                           bytes_exact=graded["bytes_exact"], outcome=graded["outcome"], grade_ms=graded["duration_ms"],
                           grade_out=str(out), duration_ms=worker_ms + graded["duration_ms"])
        archive = evidence / f"{workflow_id}.runtime"
        shutil.copytree(control, archive, symlinks=True)
        row["runtime_archive"] = str(archive)
        if keep_worktrees:
            kept = evidence / f"{workflow_id}.tree"
            shutil.copytree(tree, kept, symlinks=True)
            row["worktree"] = str(kept)
        return row


def run(tasks_path, lanes, gym, max_tasks=None, budget_usd=None, keep_worktrees=False, runner=None, grader=None,
        binary=None, image=None, repo_path=None, timeout=None):
    """`gym run --kind decomp`: each task on each lane, sequential and
    resumable (completed keys, as decomp-grade writes them, are skipped).
    Mode is always hidden: the worker sees the brief, the starting tree and
    the checker, never the task directory."""
    import fusion_core as core
    import fusion_gym as gym_api
    from fusion_workflow import WorkflowRunner
    runner = runner or WorkflowRunner
    tasks = [(Path(path).resolve(), task) for path, task in load_tasks(tasks_path)]
    gym = Path(gym).resolve()
    if repo_path is not None:
        repo_path = Path(repo_path).resolve()
        if gym == repo_path or gym.is_relative_to(repo_path):
            raise ValueError(f"the gym directory must be outside the source repository {repo_path}")
    gym = gym_api.prepare_gym(gym, repo_path)
    config, _ = core.load_config(gym)
    resolved = {name: gym_api.resolve_lane(config, name) for name in lanes}
    timeout = timeout or gym_api.DEFAULT_TIMEOUT
    with gym_api._locked(gym):
        done = {row["key"] for row in gym_api.read_results(gym)
                if row.get("event") == "finished" and row.get("completed")}
        spent, processed, finished, skipped = 0.0, 0, [], []
        for task_path, task in tasks:
            pending = [name for name in lanes
                       if gym_api.result_key(task["task_id"], name, "hidden", KIND) not in done]
            if not pending:
                continue
            source = start_source(task_path, task, repo_path)
            if source is None:
                skipped.append({"task": task["task_id"], "reason": "no starting tree: no --repo-path with base_commit "
                                                                   "and no start/ next to the task file"})
                continue
            if max_tasks is not None and processed >= max_tasks:
                break
            processed += 1
            for name in pending:
                if budget_usd is not None and spent >= budget_usd:
                    return {"status": "budget_reached", "kind": KIND, "mode": "hidden", "spent_usd": spent,
                            "runs": finished, "skipped": skipped}
                workflow_id = f"gym-{gym_api._slug(task['task_id'])}-dc-{gym_api._slug(name)}-{uuid.uuid4().hex[:8]}"
                remaining = None if budget_usd is None else budget_usd - spent
                try:
                    row = run_lane(task_path, task, source, name, resolved[name], gym, config, runner, workflow_id,
                                   remaining, repo_path, grader, binary, image, keep_worktrees, timeout)
                except ValueError as exc:
                    skipped.append({"task": task["task_id"], "reason": str(exc)})
                    break
                gym_api._append(gym, row)
                finished.append(row)
                spent += row["cost_usd"]
        return {"status": "complete", "kind": KIND, "mode": "hidden", "spent_usd": spent, "runs": finished,
                "skipped": skipped}


def work_class(row):
    return f"{KIND}:{row.get('stratum')}"
