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
"""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import time

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
    """Decomp tasks from a task file or a directory of them (other JSON, such
    as the extractor's extract.json, is skipped)."""
    path = Path(path)
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    tasks = []
    for file in files:
        try:
            value = json.loads(file.read_text())
        except (OSError, ValueError) as exc:
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
    Holds measured the machine, not the lane."""
    return row.get("verdict") in {"matched", "unmatched"}, row.get("verdict") == "matched"


def work_class(row):
    return f"{KIND}:{row.get('stratum')}"
