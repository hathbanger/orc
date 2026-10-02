"""Read-only evidence interpretation: data-driven cases, private truth and exact grades.

Templates replay observed misreads or perturb verified gym material. A bundle
contains only evidence, summaries and questions. Template ids, splits and gold
live in the coordinator's task JSON, never in the worker repository.
"""
from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path, PurePosixPath
import random
import shutil
import subprocess
import tempfile
import time
import re

BLOCK = re.compile(r"^[ \t]*```[ \t]*(?:interpret|json)[ \t]*\n(.*?)\n[ \t]*```[ \t]*$", re.M | re.S)
BRIEF = """Read evidence/questions.json and answer every factual question using its case directory.
Each case is independent. Read the receipts and logs as well as summary.md.
The source/ directory contains the original gym checks, receipts and diff from
which these replay scenarios were built. Do not change any file; this is read-only.

End with exactly one fenced `interpret` JSON block mapping question ids to
answers, followed by the handoff. Use booleans or yes/no for binary questions,
numbers for counts or percentages (32 or "32%"), and null or "abstain" when unsure.
Example:
```interpret
{"q001": "yes", "q002": 12, "q003": null}
```
"""


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def period_seed():
    year, week, _ = date.today().isocalendar()
    return f"{year}-W{week:02d}"


def load_trap_templates(path=None):
    value = json.loads(Path(path or Path(__file__).parent / "gym/interpret_traps.json").read_text())
    templates = value["templates"]
    if len(templates) < 2 or len({t["id"] for t in templates}) != len(templates):
        raise ValueError("interpret needs at least two uniquely named templates")
    for item in templates:
        for key in ("source", "evidence", "question", "summary", "gold", "control"):
            if key not in item:
                raise ValueError(f"interpret template {item['id']} missing {key}")
    return templates


def split_traps(templates, seed=0, period=None):
    """Rank by seeded, period-salted hash; hold out one third (at least one).

    Ranking rather than a modulus guarantees nonempty train and holdout sets.
    A template and its controls always belong to the same split.
    """
    period = period or period_seed()
    ranked = sorted(t["id"] for t in templates)
    ranked.sort(key=lambda name: digest(f"{seed}:{period}:{name}"))
    held = set(ranked[:max(1, len(ranked) // 3)])
    return {name: "holdout" if name in held else "train" for name in ranked}


def _render(text, values):
    # Only replace named placeholders; JSON braces in evidence stay literal.
    return re.sub(r"\{([a-z_]+)\}", lambda m: str(values[m[1]]) if m[1] in values else m[0], str(text))


def build_interpret_task(source, seed=0, period=None, templates=None, split="all"):
    from fusion_gym import TASK_SCHEMA, git
    if split not in {"all", "train", "holdout"}:
        raise ValueError("interpret split must be all, train or holdout")
    templates = templates if templates is not None else load_trap_templates()
    period = period or period_seed()
    splits = split_traps(templates, seed, period)
    checks = source["checks"]
    commands = checks["fail_to_pass"] + checks["pass_to_pass"]
    if not commands:
        raise ValueError(f"{source['id']}: no verified checks to seed interpretation")
    n = len(source["fail_to_pass"]) + len(source["pass_to_pass"])
    if not n or source["base"] == source["fix"]:
        raise ValueError(f"{source['id']}: need verified tests and distinct base/fix revisions")
    choice = int(digest(f"{seed}:{source['id']}")[:8], 16) % len(commands)
    values = {"base": source["base"], "fix": source["fix"], "n": n, "n_plus_one": n + 1,
              "argv": json.dumps(commands[choice])}
    receipts = source.get("extraction_evidence") or {
        "provenance": "Reconstructed from the gym extractor's verified fail-to-pass/pass-to-pass contract",
        "fix": [{"argv": argv, "exit_code": 0, "status": "passed"} for argv in commands]}
    bundle = {"evidence/source/checks.json": json.dumps(checks, indent=2) + "\n",
              "evidence/source/receipts.json": json.dumps(receipts, indent=2, sort_keys=True) + "\n",
              "evidence/source/change.patch": git(source["repo_path"], "diff", "--no-ext-diff", source["base"], source["fix"])}
    cases = [(t, trapped) for t in templates if split == "all" or splits[t["id"]] == split for trapped in (True, False)]
    random.Random(digest(f"{seed}:{period}:{source['id']}")).shuffle(cases)
    questions, truth = [], {}
    for index, (template, trapped) in enumerate(cases, 1):
        qid = f"q{index:03d}"
        variant = template if trapped else {**template, **template["control"],
                                            "evidence": {**template["evidence"], **template["control"]["evidence"]}}
        directory = f"evidence/{qid}"
        for path, text in {**variant["evidence"], "summary.md": variant["summary"]}.items():
            relative = PurePosixPath(path)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe interpret evidence path: {path}")
            bundle[f"{directory}/{path}"] = _render(text, values) + "\n"
        questions.append({"id": qid, "directory": directory, "question": _render(variant["question"], values)})
        truth[qid] = {"answer": _render(variant["gold"], values), "trap": trapped,
                      "template": template["id"], "split": splits[template["id"]]}
    bundle["evidence/questions.json"] = json.dumps(questions, indent=2) + "\n"
    identity = digest(json.dumps({"bundle": bundle, "truth": truth, "seed": seed, "period": period}, sort_keys=True))[:16]
    return {"schema": TASK_SCHEMA, "kind": "interpret", "id": f"{source['id']}-interpret-{identity}",
            "pr": source.get("pr"), "repo": source.get("repo"), "repo_path": source["repo_path"], "source_task": source["id"],
            "seed": seed, "period": period, "split": split, "bundle": bundle, "interpretation": truth,
            "prompt": "Interpret the evidence bundle and answer its factual questions."}


def extract(tasks_path, out, seed=0, period=None, templates_path=None, split="all"):
    from fusion_gym import load_tasks
    templates = load_trap_templates(templates_path)
    sources = [t for t in load_tasks(tasks_path) if t.get("kind", "fix") == "fix"]
    if not sources:
        raise ValueError("interpret extraction needs existing fix gym tasks")
    tasks = [build_interpret_task(t, seed, period, templates, split) for t in sources]
    directory = Path(out)
    directory.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        (directory / f"{task['id']}.json").write_text(json.dumps(task, indent=2, sort_keys=True) + "\n")
    summary = {"kind": "interpret", "seed": seed, "period": tasks[0]["period"], "split": split,
               "tasks": [{"id": t["id"], "questions": len(t["interpretation"])} for t in tasks]}
    (directory / "index.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary, tasks


def prepare_repo(gym, task):
    """A standalone evidence-only repository, with no source history or gold."""
    from fusion_gym import git
    repo = Path(gym) / "tasks" / task["id"] / "hidden" / "repo"
    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        git(repo, "init", "-q")
        for path, content in task["bundle"].items():
            relative = PurePosixPath(path)
            if not relative.parts or relative.parts[0] != "evidence" or ".." in relative.parts or ".git" in relative.parts:
                raise ValueError(f"unsafe interpret bundle path: {path}")
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        git(repo, "add", "--all")
        git(repo, "-c", "user.name=Fusion Gym", "-c", "user.email=gym@localhost", "commit", "-qm", "Evidence bundle")
    return repo, git(repo, "rev-parse", "HEAD").strip()


def run_lane(task, name, lane, gym, config, runner, workflow_id, remaining, keep_worktrees=False):
    """A fresh worker namespace per invocation, outside the persistent gym.

    Neither Git metadata nor the workflow control store links back to the gym,
    task JSON or previous lanes. Archive evidence only after the worker exits;
    always destroy the disposable namespace before starting another lane.
    """
    import fusion_core as core
    import fusion_gym as gym_api
    from fusion_decisions import DecisionStore
    from fusion_publish import snapshot

    evidence = gym / "results" / task["id"] / "interpret" / gym_api._slug(name)
    evidence.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="fusion-gym-interpret-") as temporary:
        root = Path(temporary).resolve()
        repo, start_sha = prepare_repo(root, task)
        control = root / "control"
        control.mkdir()
        isolated_config = copy.deepcopy(config)
        # Do not inherit coordinator paths through Fusion's routing environment.
        env = {"FUSION_CONTROL_WORKSPACE": str(control), "FUSION_WORKSPACE": str(repo),
               "FUSION_CONFIG": str(control / ".fusion.json"), "PWD": str(repo)}
        settings = isolated_config.setdefault(lane["agent"], {})
        settings["env"] = {**settings.get("env", {}), **env}
        if lane.get("route"):
            settings = isolated_config["routes"][lane["route"]]
            settings["env"] = {**settings.get("env", {}), **env}
        (control / ".fusion.json").write_text(json.dumps(isolated_config))
        token = core._CONTROL_WORKSPACE.set(control)
        try:
            spec = gym_api.build_interpret_spec(task, lane, remaining)
            outcome = runner(control, isolated_config, spec, run_id=workflow_id,
                             worktree={"workspace": str(repo), "base_sha": start_sha}).run()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            outcome = {"workflow_id": workflow_id, "status": "error", "error": str(exc), "nodes": []}
        finally:
            core._CONTROL_WORKSPACE.reset(token)
        wall_ms = round((time.monotonic() - started) * 1000)
        diff_files, patch, snapshot_error = [], b"", None
        try:
            tree = snapshot(repo)
            diff_files = [p for p in gym_api.git(repo, "diff", "--name-only", "-z", start_sha, tree).split("\0") if p]
            patch = gym_api.git(repo, "diff", "--binary", start_sha, tree, binary=True)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            snapshot_error = str(exc)
        row = gym_api.summarize_interpret(task, name, lane, outcome, diff_files, wall_ms)
        if snapshot_error is not None:
            # Unknown integrity is never a clean tree. Retry after inspection works.
            row.update(verdict="invalid_snapshot", completed=False, snapshot_error=snapshot_error, scores={})
            row["grade_label"] = {"status": "skipped", "reason": "read-only tree integrity could not be verified"}
        elif row["completed"]:
            row["grade_label"] = gym_api.grade_label(control, row)
        answer = gym_api._read_answer(((outcome.get("nodes") or [{}])[0].get("result") or {}))
        if answer:
            path = evidence / f"{workflow_id}.answer.md"
            path.write_text(answer, encoding="utf-8")
            row["answer_file"] = str(path)
        if outcome.get("error"):
            row["error"] = outcome["error"]
        path = evidence / f"{workflow_id}.patch"
        path.write_bytes(patch)
        row["patch"] = str(path)
        # Coordinator-only archive; it is never reused as a worker control store.
        archive = evidence / f"{workflow_id}.runtime"
        shutil.copytree(control, archive, symlinks=True)
        row["runtime_archive"] = str(archive)
        decisions = DecisionStore(control).path
        if decisions.exists():
            target = DecisionStore(gym).path
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a") as stream:
                stream.write(decisions.read_text())
        if keep_worktrees:
            kept = evidence / f"{workflow_id}.tree"
            shutil.copytree(repo, kept, symlinks=True)
            row["worktree"] = str(kept)
        return row


def normalize_answer(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value).strip().casefold()
    if text in {"", "abstain", "unknown", "unsure", "uncertain", "null", "i don't know"}:
        return None
    if text in {"yes", "true"}:
        return "yes"
    if text in {"no", "false"}:
        return "no"
    try:
        number = Decimal(text.removesuffix("%").strip())
        if number.is_finite():
            return number
    except InvalidOperation:
        pass
    return text


def parse_answer(text):
    blocks = BLOCK.findall(text or "")
    if len(blocks) != 1:
        return None, "exactly one fenced interpret or JSON block is required"
    try:
        value = json.loads(blocks[0], parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
    except ValueError as exc:
        return None, f"invalid interpret JSON: {exc}"
    if not isinstance(value, dict) or any(isinstance(v, (dict, list)) for v in value.values()):
        return None, "interpret answers must be a JSON object of scalar answers"
    return value, None


def metrics(items):
    items = list(items)
    counts = {name: sum(i["verdict"] == name for i in items) for name in ("correct", "abstained", "misread")}
    traps = [i for i in items if i["trap"]]
    return {**counts, "total": len(items), "score": (counts["correct"] + .5 * counts["abstained"]) / len(items) if items else None,
            "traps": len(traps), "trap_misreads": sum(i["verdict"] == "misread" for i in traps),
            "trap_misread_rate": sum(i["verdict"] == "misread" for i in traps) / len(traps) if traps else None}


def grade(answer, truth, invalid=False):
    items = {}
    for qid, gold in truth.items():
        actual = normalize_answer(answer.get(qid))
        verdict = "invalid_answer" if invalid else "abstained" if actual is None else "correct" if actual == normalize_answer(gold["answer"]) else "misread"
        items[qid] = {"verdict": verdict, "trap": gold["trap"], "split": gold["split"]}
    scores = metrics(items.values())
    return {**scores, "verdict": "invalid_answer" if invalid else "misread" if scores["misread"] else "abstained" if scores["abstained"] else "correct",
            "questions": items, **{split: metrics(i for i in items.values() if i["split"] == split)
                                  for split in ("train", "holdout")}}


def _combined_metrics(scores):
    """Aggregate counts, never persist per-question split/trap annotations."""
    counts = {key: sum(s.get(key, 0) for s in scores)
              for key in ("correct", "abstained", "misread", "total", "traps", "trap_misreads")}
    counts["score"] = (counts["correct"] + .5 * counts["abstained"]) / counts["total"] if counts["total"] else None
    counts["trap_misread_rate"] = counts["trap_misreads"] / counts["traps"] if counts["traps"] else None
    return counts


def report_section(rows):
    section = {"lanes": {}, "tasks": {}}
    for row in rows:
        lane = section["lanes"].setdefault(row["lane"], {"attempted": 0, "invalid_answer": 0, "tampered": 0,
                                                        "cost_usd": 0, "_scores": []})
        lane["attempted"] += 1
        lane["invalid_answer"] += row["verdict"] == "invalid_answer"
        lane["tampered"] += row["verdict"] == "tampered"
        lane["cost_usd"] += row.get("cost_usd", 0)
        lane["_scores"].append(row.get("scores") or {})
        section["tasks"].setdefault(row["task"], {})[row["lane"]] = row["verdict"]
    for lane in section["lanes"].values():
        scores = lane.pop("_scores")
        lane.update(_combined_metrics(scores))
        for split in ("train", "holdout"):
            lane[split] = _combined_metrics([s.get(split, {}) for s in scores])
    section["lanes"] = dict(sorted(section["lanes"].items()))
    return section
