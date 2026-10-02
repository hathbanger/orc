"""Local, auditable decisions. Model suggestions never confer permissions."""
from __future__ import annotations

import atexit
import contextlib
from collections import Counter
import fcntl
import functools
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import threading
from types import SimpleNamespace
import time
import uuid

import fusion_progress as progress


DEFAULTS = {
    "mode": "shadow", "python": "", "device": "cpu", "model_path": "",
    "timeout_seconds": 120, "auto_actions": [], "threshold": 0.90,
    "calibration_file": "", "max_state_chars": 2200, "verdict_labels": True,
    "automatic_labels": True, "split": "time",
    "risk": {"alpha": 0.05, "delta": 0.1, "min_examples": 30, "min_groups": 20},
}
SPLITS = {"time", "group-hash"}
# Share of workflow groups held out, and the fewest groups kept on each side.
VALIDATION_FRACTION = 0.2
MIN_SPLIT_GROUPS = 2
KINDS = {"intake", "routing", "recovery", "review", "acceptance"}
# "unscored": the input was recorded without running the model, so a verified
# answer can be attached to it. It has no prediction and never drives an action.
LABELABLE_STATUSES = {"ok", "unscored"}
STATE_VERSION = 2
INTAKE_QUESTIONS = {
    "workflow": {"type": "choice", "instructions": "Which work is requested and permitted?",
                 "criteria": {"discovery": "investigate or plan only", "build": "implement a feature",
                              "debug": "diagnose and fix a defect", "review": "review existing work only"}},
    "needs_clarification": {"type": "noul", "instructions": "Is a consequential product decision missing?"},
}
REVIEW_QUESTIONS = {
    "specialty": {"type": "choice", "instructions": "Which reviewer should examine this work?",
                  "criteria": {"general": "ordinary code correctness", "security": "identity, authorization or secrets",
                               "payments": "money movement or accounting", "data": "schema, migrations or data integrity"}},
    "needs_review": {"type": "noul", "instructions": "Does this handoff need an independent review before acceptance?"},
}
RECOVERY_QUESTIONS = {
    "action": {"type": "choice", "instructions": "What should the coordinator do next given the actual outcome?",
               "criteria": {"continue": "accepted result with no unresolved blocker", "repair": "repair or supply missing evidence",
                            "switch": "worker unavailable; another permitted worker may help", "ask": "missing user decision or permission",
                            "stop": "external blocker; do not retry now"}},
}
# Single-clause phrasings only: a double-barreled "does it satisfy, or is it
# off-task?" inverted the live model on real workflow states. `plausible` is
# the rejection signal; `failed_task` is recorded for calibration and training
# but is not a gate -- on authored near-misses it detects "no work was done",
# not "the wrong work was done", so requiring agreement blocked the one case
# this check exists to catch.
ACCEPTANCE_QUESTIONS = {
    "plausible": {"type": "noul", "instructions": "Does the worker's reported summary and evidence plausibly satisfy the task?"},
    "failed_task": {"type": "noul", "instructions": "Did the worker fail to do what the task asked?"},
}
ACCEPTANCE_MIN_SUMMARY = 400
# `changed` and `tests` entries kept, and characters per entry. Lists shrink
# to the next level only when the criterion and minimum summary cannot fit
# beside them.
ACCEPTANCE_LIST_LEVELS = ({"changed": 12, "tests": 8, "chars": 200}, {"changed": 6, "tests": 4, "chars": 100},
                          {"changed": 3, "tests": 2, "chars": 60})


def state_cap(options):
    return max(200, min(6000, int(options["max_state_chars"])))


# Laya's encoder reads at most `max_len` tokens: the question head (type,
# instruction, options; at most `head_max_len` plus three separators), then
# the state, which gets whatever is left (fusion_laya.truncated). Both come
# from the checkpoint's rl_agent_config.json. A state is complete only if it
# fits that remainder, so a character cap cannot decide it: with the
# ModernBERT byte-level BPE tokenizer, real decision states measured 2.4-4.8
# characters per token, hashes and diffs ~1.7, CJK ~1.2.
# STATE_HEAD_TOKENS is the measured head of a kind's longest question
# (test/laya_token_budget.py re-measures it); an unmeasured kind is assumed to
# use the whole head.
LAYA_MAX_LEN = 512
LAYA_HEAD_MAX_LEN = 192
STATE_HEAD_TOKENS = {"acceptance": 40}
# The published checkpoints' limits (their rl_agent_config.json; the laya
# README's checkpoint table). test/laya_training_objective.py checks them
# against any checkpoint cached locally. A directory is read from its own config.
CHECKPOINTS = {
    "english": {"encoder": "answerdotai/ModernBERT-large", "max_len": 512, "head_max_len": 192},
    "typed-decisions": {"encoder": "answerdotai/ModernBERT-large", "max_len": 1024, "head_max_len": 256},
    "multilingual": {"encoder": "jhu-clsp/mmBERT-base", "max_len": 1024, "head_max_len": 256},
}
# estimated_tokens and STATE_HEAD_TOKENS were measured on this tokenizer only.
MEASURED_ENCODERS = {"answerdotai/ModernBERT-large"}
_PIECES = re.compile(r"'(?:s|t|re|ve|m|ll|d)| ?[^\W\d_]+| ?\d+| ?(?:[^\s\w]|_)+|\s+(?!\S)|\s+")
_CASED = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[A-Za-z]")


def checkpoint_limits(spec=None):
    """{encoder, max_len, head_max_len} of a checkpoint name or directory.

    A directory without a readable rl_agent_config.json gets the English
    limits, the smaller budget; the runtime cannot load it anyway.
    """
    if not spec or spec in CHECKPOINTS:
        return dict(CHECKPOINTS[spec or "english"])
    try:
        config = json.loads((Path(spec).expanduser() / "rl_agent_config.json").read_text())
        limits = {"encoder": str(config.get("encoder", "")), "max_len": int(config.get("max_len", LAYA_MAX_LEN)),
                  "head_max_len": int(config.get("head_max_len", LAYA_HEAD_MAX_LEN))}
    except (OSError, ValueError, TypeError, AttributeError):
        return dict(CHECKPOINTS["english"])
    if not 64 <= limits["max_len"] <= 16384 or not 16 <= limits["head_max_len"] < limits["max_len"]:
        return dict(CHECKPOINTS["english"])
    return limits


def state_tokens(kind, checkpoint=None):
    """Tokens the state of a `kind` decision may use on `checkpoint` (default English).

    estimated_tokens was measured on the ModernBERT tokenizer only, so a
    checkpoint with another encoder never gets more than the English budget;
    its runtime truncation report stays authoritative.
    """
    limits = checkpoint_limits(checkpoint)
    budget = limits["max_len"] - STATE_HEAD_TOKENS.get(kind, limits["head_max_len"] + 3)
    if limits["encoder"] not in MEASURED_ENCODERS:
        budget = min(budget, state_tokens(kind))
    return budget


def estimated_tokens(text):
    """A cheap upper estimate of Laya's token count for `text`, without the tokenizer.

    Splits like the tokenizer's GPT-2 pre-tokenizer, then charges each piece
    more than BPE usually spends: a cased run of letters one token per 4
    letters, digits one per 2, each punctuation character, tab or newline
    one, a run of spaces one, and each non-ASCII character its UTF-8 length
    less one (four-byte characters, e.g. emoji, their full length). Measured with the
    tokenizer on 1,596 texts -- this repository's code, docs, JSON and shell
    in 1,500-character chunks, raw and JSON-encoded; 88 recorded decision
    states; hashes, UUIDs, digits, accented text, CJK, emoji -- the true count
    was at most 0.89 of the estimate on JSON-encoded text (decision states
    are JSON), 0.81 on recorded acceptance states (0.72 on average), and at
    most 1.0 on raw text and the synthetic hash and whitespace runs. Text
    built to defeat BPE (random consonant strings, base64, alternating case)
    can exceed it; the runtime still reports that as truncation.
    test/laya_token_budget.py re-measures it; test/fixtures/
    laya_token_counts.json keeps the counts the default suite checks against.
    """
    total = 0
    for piece in _PIECES.findall(text):
        piece = piece.lstrip(" ") or " "
        for char in piece:
            if ord(char) > 127:
                width = len(char.encode())
                total += width if width == 4 else max(1, width - 1)
        plain = "".join(char for char in piece if ord(char) < 128)
        if not plain:
            continue
        if plain.isspace():
            total += sum(char != " " for char in plain) + (" " in plain)
        elif plain[0].isalpha():
            total += sum(-(-len(run) // 4) for run in _CASED.findall(plain))
        elif plain[0].isdigit():
            total += -(-len(plain) // 2)
        else:
            total += len(plain)
    return total


def exceeds_token_budget(record):
    """An input recorded without inference (`unscored`) whose estimated size
    exceeds the model's state budget. The model never checked it, so it counts
    as truncated: an acceptance input of up to 2200 characters recorded before
    inputs were bounded by tokens is one. The budget is the one recorded with
    the input (`state_tokens`, from its kind's checkpoint), else English's."""
    return record.get("status") == "unscored" and over_token_budget(record.get("kind"), record.get("state"),
                                                                     record.get("state_tokens"))


def over_token_budget(kind, text, tokens=None):
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens <= 0:
        tokens = state_tokens(kind)
    return isinstance(text, str) and estimated_tokens(text) > tokens


def _encoded(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _marked(text, keep):
    return text[:keep] + f" […truncated {len(text) - keep} chars]"


def excerpt(text, budget):
    """`text` if its JSON-escaped form fits `budget` characters, else a head
    excerpt that says how much was cut. Never silently shortened."""
    def size(value):
        return _encoded(value) - 2
    if size(text) <= budget:
        return text
    keep = max(0, budget - 40)
    while keep and size(_marked(text, keep)) > budget:
        keep = max(0, keep - max(1, size(_marked(text, keep)) - budget))
    return _marked(text, keep)


def _listed(items, limit, chars):
    items = [text if len(text) <= chars else _marked(text, chars)
             for text in (str(item) for item in (items or []))]
    return items if len(items) <= limit else items[:limit] + [f"[…{len(items) - limit} more]"]


def acceptance_task(source):
    """What the acceptance question is judged against, as (fields, criterion, detail).

    `source` is a workflow node or a run's task.json. Its decision_context is
    unwrapped through nested `request`s (a workflow run wraps its node's
    context and adds dependency receipts, which the node's own acceptance does
    not depend on). A Fusion-built request carries its workflow kind and stage;
    its first line is the ask and the rest is supporting detail. Anything else
    -- a short decision_context string, a hand-written node task, a delegation
    brief -- is the criterion as a whole.
    """
    context = source.get("decision_context", source.get("task"))
    fields = {}
    while isinstance(context, dict) and "request" in context:
        fields.update({key: context[key] for key in ("workflow_kind", "stage") if isinstance(context.get(key), str)})
        context = context["request"]
    text = context if isinstance(context, str) else json.dumps(context, ensure_ascii=False, sort_keys=True)
    if not fields:
        return None, text, ""
    if isinstance(source.get("role"), str):
        fields["role"] = source["role"]
    criterion, _, detail = text.strip().partition("\n")
    return fields, criterion, detail


def deliverable_digest(source, result, answer_text=None):
    """Compact reported work; coordinator receipts stay distinct from claimed tests."""
    if source.get("write"):
        return "\n".join(
            ["Changed files: " + json.dumps(result.get("changed", []), ensure_ascii=False)]
            + ["Acceptance check: " + json.dumps({k: receipt.get(k) for k in ("argv", "status", "exit_code")},
                                                ensure_ascii=False)
               for receipt in result.get("acceptance_checks", [])])
    if answer_text is None:
        path = (result.get("artifacts") or {}).get("answer")
        try:
            answer_text = Path(path).read_text(errors="replace") if path else ""
        except OSError:
            answer_text = ""
    lines, paragraph_start = [], True
    for line in answer_text.splitlines():
        text = line.strip()
        if not text:
            paragraph_start = True
            continue
        if re.match(r"^(STATUS|SUMMARY|CHANGED|TESTS|BLOCKERS):", text):
            paragraph_start = True
            continue
        if paragraph_start or re.match(r"^(#{1,6}\s|[-*+]\s|\d+[.)]\s)", text):
            lines.append(text)
        paragraph_start = bool(re.match(r"^#{1,6}\s", text))
    return "\n".join(lines)


def acceptance_state(source, result, cap, tokens=None, *, answer_text=None):
    """The input for the ACCEPTANCE questions, bounded to `cap` encoded
    characters and to `tokens` estimated tokens (default: what Laya leaves the
    state beside the acceptance questions, `state_tokens("acceptance")`).

    Both the workflow gate (fusion_policy.accept_node) and lead verdicts
    (fusion_labeling.verdict_label) build it here, so one run always yields
    the same input. What is asked is whether the reported summary plausibly
    satisfies the task, so the criterion is never cut: a task that does not
    fit whole beside a minimal summary is marked `source_truncated` and stays
    unlabeled. The summary is kept from its start -- where a handoff states
    what was done -- then the node assignment and a request's supporting detail
    after its first line may be excerpted; each cut carries a visible "[…truncated N chars]"
    marker, as do capped `changed` and `tests` lists. An input with visible
    markers records exactly what the classifier saw; questions depending on
    omitted evidence require abstention. The deliverable fills space remaining
    after the summary, node assignment and request detail (in that order).

    The token bound is met by lowering the character cap in proportion to the
    estimate (estimated_tokens) until the input fits, so the rule above holds
    unchanged at whatever cap the tokens allow. When even that leaves no room
    for the criterion and minimum summary, the lists shrink a level
    (ACCEPTANCE_LIST_LEVELS) and the cap is lowered again from `cap`.
    """
    tokens = state_tokens("acceptance") if tokens is None else tokens
    result = {**result, "deliverable": deliverable_digest(source, result, answer_text)}
    for lists in ACCEPTANCE_LIST_LEVELS:
        state = _acceptance_within_tokens(source, result, cap, tokens, lists)
        if not state.get("source_truncated"):
            return state
    return state


def _acceptance_within_tokens(source, result, cap, tokens, lists):
    while True:
        state = _acceptance_within(source, result, cap, lists)
        used = estimated_tokens(json.dumps(state, ensure_ascii=False, sort_keys=True))
        if used <= tokens or state.get("source_truncated"):
            return state
        smaller = min(cap - 1, cap * tokens // used)
        if smaller < 200:
            return {**state, "source_truncated": True}
        cap = smaller


def _acceptance_within(source, result, cap, limits):
    fields, criterion, detail = acceptance_task(source)
    lists = {key: _listed(result.get(key), limits[key], limits["chars"]) for key in ("changed", "tests")}
    summary = str(result.get("summary") or "")
    deliverable = result.get("deliverable", "")
    # A workflow node's task is the assignment; a run's task is a worker prompt.
    # A plain-string decision_context is the caller's exact statement of what
    # decisions may see (admitted frozen context, hidden-test briefs), so the
    # worker task is only added beside a structured Fusion-built request.
    context = source.get("decision_context")
    structured = isinstance(context, dict) or "decision_context" not in source
    node_task = source.get("node_task", source.get("task")
                           if structured and ("decision_context" in source or "id" in source) and "run_id" not in source else None)

    def build(request, text, assignment="", work=""):
        return {"task": request if fields is None else {**fields, "request": request}, "summary": text, **lists,
                **({"node_task": assignment} if node_task else {}),
                **({"deliverable": work} if deliverable else {})}
    joined = "\n" + detail if detail else ""
    whole = build(criterion + joined, summary, node_task, deliverable)
    if _encoded(whole) <= cap:
        return whole
    # Reserve marker space, not the full assignment, beside the minimum summary.
    detail_marker = 40 if detail else 0
    work_marker = 40 if deliverable else 0
    spare = cap - _encoded(build(criterion, "")) - detail_marker - work_marker - (40 if node_task else 0)
    size = _encoded(summary) - 2
    supporting_size = _encoded(joined) - 2 + (_encoded(node_task) - 2 if node_task else 0)
    budget = min(size, max(spare * 3 // 5, min(spare, ACCEPTANCE_MIN_SUMMARY), spare - supporting_size))
    if budget < min(size, ACCEPTANCE_MIN_SUMMARY):
        return {**whole, "source_truncated": True}
    summary = excerpt(summary, budget)
    assignment = excerpt(node_task, cap - _encoded(build(criterion, summary)) - detail_marker - work_marker) if node_task else ""
    if detail:
        joined = excerpt(joined, cap - _encoded(build(criterion, summary, assignment)) - work_marker)
    work = excerpt(deliverable, cap - _encoded(build(criterion + joined, summary, assignment))) if deliverable else ""
    state = build(criterion + joined, summary, assignment, work)
    return state if _encoded(state) <= cap else {**state, "source_truncated": True}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def config_for(config):
    options = {**DEFAULTS, **(config.get("decisions") or {})}
    if os.environ.get("FUSION_DECISIONS_MODE"):
        options["mode"] = os.environ["FUSION_DECISIONS_MODE"]
    if options["mode"] not in {"off", "shadow", "active"}:
        raise ValueError("decisions.mode must be off, shadow, or active")
    if not isinstance(options["auto_actions"], list) or set(options["auto_actions"]) - KINDS:
        raise ValueError("decisions.auto_actions must contain only intake, routing, recovery, review, acceptance")
    if not 0 < float(options["threshold"]) <= 1:
        raise ValueError("decisions.threshold must be in (0, 1]")
    if not isinstance(options["verdict_labels"], bool):
        raise ValueError("decisions.verdict_labels must be true or false")
    if not isinstance(options["automatic_labels"], bool):
        raise ValueError("decisions.automatic_labels must be true or false")
    if options["split"] not in SPLITS:
        raise ValueError("decisions.split must be time or group-hash")
    options["risk"] = risk_options(options["risk"])
    checkpoints = options.get("checkpoints") or {}
    if (not isinstance(checkpoints, dict) or set(checkpoints) - KINDS
            or any(not isinstance(value, str) or not value.strip() for value in checkpoints.values())):
        raise ValueError("decisions.checkpoints must map decision kinds to a checkpoint name "
                         f"({', '.join(CHECKPOINTS)}) or a checkpoint directory")
    from fusion_laya_objective import training_options
    options["checkpoints"] = {kind: value.strip() for kind, value in checkpoints.items()}
    options["training"] = training_options(options.get("training"))
    return options


def risk_options(value):
    if not isinstance(value, dict):
        raise ValueError("decisions.risk must be an object")
    risk = {**DEFAULTS["risk"], **value}
    for key in ("alpha", "delta"):
        if isinstance(risk[key], bool) or not isinstance(risk[key], (int, float)) or not 0 < risk[key] < 1:
            raise ValueError(f"decisions.risk.{key} must be in (0, 1)")
    for key in ("min_examples", "min_groups"):
        if type(risk[key]) is not int or risk[key] < 1:
            raise ValueError(f"decisions.risk.{key} must be a positive integer")
    return risk


def record_group(record):
    context = record.get("context") or {}
    return context.get("group") or context.get("task_id") or digest(record["state"])


def hash_split(group):
    return "validation" if int(digest(group)[:8], 16) % 5 == 0 else "train"


def assign_splits(first_seen, method="time"):
    """{group: "train" | "validation"} for the labeled workflow groups in `first_seen`.

    `first_seen` maps each group to the time of its first decision. "time"
    holds out the newest groups -- a model is trained on older work and judged
    on newer work, as it will be deployed -- and never splits a group: about
    VALIDATION_FRACTION of groups, at least MIN_SPLIT_GROUPS on each side once
    there are enough. "group-hash" is the earlier assignment by a hash of the
    group name, kept for comparison with old rounds.
    """
    if method == "group-hash":
        return {group: hash_split(group) for group in first_seen}
    if method != "time":
        raise ValueError("split must be time or group-hash")
    ordered = sorted(first_seen, key=lambda group: (first_seen[group] or 0, str(group)))
    count = len(ordered)
    held = 0 if count < 2 else max(1, min(count - MIN_SPLIT_GROUPS, max(MIN_SPLIT_GROUPS, math.ceil(count * VALIDATION_FRACTION))))
    return {group: "validation" if index >= count - held else "train" for index, group in enumerate(ordered)}


def group_first_seen(records):
    """Time of each workflow group's first recorded decision, labeled or not, so it never moves."""
    first = {}
    for record in records:
        group, time_ms = record_group(record), record.get("time_ms") or 0
        first[group] = min(first.get(group, time_ms), time_ms)
    return first


def labeled_splits(rows, method="time"):
    """Splits for decision rows (fusion_learning.decision_rows): every row dates
    its group, and only groups with an approved, labelable answer are split."""
    first = group_first_seen(rows)
    labeled = {record_group(row) for row in rows if row.get("reviewed_answers") and not row.get("excluded")
               and labelable_record(row)}
    return assign_splits({group: first[group] for group in labeled}, method)


# What the deterministic policy answers for a question without Laya: the
# baseline a trained head must beat before it may replace it.
# intake.workflow -- fusion_build._prepare's fallback (planning-only scope,
#   fix/bug words, a leading "review"), read from the application event, and
#   skipped when the caller named the kind (that is intent, not a heuristic);
# intake.needs_clarification -- intake never stops to ask: "false";
# review.specialty -- fusion_policy.review_task's default focus: "general";
# review.needs_review -- a requested review always runs: "true";
# recovery.action -- fusion_policy.recovery's rule (continue / stop on quota,
#   permission or attempt limit / switch / repair), from the application event;
# acceptance.plausible / failed_task -- trust the worker's reported success:
#   "true" / "false". Not the structural gate's result: a gate-sourced label
#   is that result, so it would be a perfect "baseline" by construction.
# Routing has no labeled questions: routes are bandit feedback (fusion_policy).
# An answer the policy took from Laya (applied=true) is not a heuristic answer.
CONSTANT_HEURISTICS = {("intake", "needs_clarification"): "false", ("review", "specialty"): "general",
                       ("review", "needs_review"): "true", ("acceptance", "plausible"): "true",
                       ("acceptance", "failed_task"): "false"}
APPLIED_HEURISTICS = {("intake", "workflow"), ("recovery", "action")}


def heuristic_answers(record, application=None):
    answers = {}
    for key, question in (record.get("questions") or {}).items():
        pair = (record.get("kind"), key)
        value = CONSTANT_HEURISTICS.get(pair)
        if (pair in APPLIED_HEURISTICS and application and not application.get("applied")
                and not application.get("explicit_kind")):
            value = application.get("actual")
        if value is not None and value in labels_for(question):
            answers[key] = value
    return answers


def labelable_record(record):
    return (record.get("status") in LABELABLE_STATUSES and not record.get("truncated")
            and not exceeds_token_budget(record))


def runtime_python(options):
    configured = options.get("python") or os.environ.get("FUSION_LAYA_PYTHON")
    managed = Path.home() / ".local/share/orc/laya/bin/python"
    return str(configured or (managed if managed.is_file() else sys.executable))


class LayaRuntime:
    """One resident SDK process; serialized requests, bounded waits, clean stdout."""
    def __init__(self, options):
        self.options = options
        self.process = None
        self.lock = threading.Lock()
        self.error = None
        self.buffer = b""

    def close(self):
        proc, self.process = self.process, None
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            for stream in (proc.stdin, proc.stdout):
                if stream:
                    stream.close()
        self.buffer = b""

    def predict(self, state, questions, checkpoint=None, options=None):
        options = options or self.options
        with self.lock:
            if self.error:
                raise RuntimeError(self.error)
            try:
                if self.process is None:
                    progress.emit("laya", f"starting local runtime on {options['device']}; the first checkpoint load may take tens of seconds")
                    env = os.environ.copy()
                    env.update(HF_HUB_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
                    self.process = subprocess.Popen(
                        [runtime_python(self.options), "-u", str(Path(__file__).with_name("fusion_laya.py")), "serve"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                    )
                request = {"state": state, "questions": questions, "device": options["device"],
                           "model_path": options["model_path"]}
                if checkpoint in CHECKPOINTS:
                    request.update(checkpoint=checkpoint, model_path="")
                elif checkpoint:
                    request["model_path"] = checkpoint
                self.process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
                self.process.stdin.flush()
                deadline = time.monotonic() + float(options["timeout_seconds"])
                with selectors.DefaultSelector() as selector:
                    selector.register(self.process.stdout, selectors.EVENT_READ)
                    while b"\n" not in self.buffer:
                        progress.check_cancelled()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Laya inference timed out")
                        if not selector.select(min(.2, remaining)):
                            continue
                        chunk = os.read(self.process.stdout.fileno(), 65536)
                        if not chunk:
                            raise RuntimeError("Laya runtime exited; run fusion decisions setup")
                        self.buffer += chunk
                        if len(self.buffer) > 1_000_000:
                            raise ValueError("Laya response exceeds limit")
                line, self.buffer = self.buffer.split(b"\n", 1)
                response = json.loads(line)
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                self.error = str(exc)
                self.close()
                raise RuntimeError(self.error) from exc
            # The runtime answered, so it is still serving; only this request failed.
            if response.get("error"):
                raise RuntimeError(response["error"])
            return response


_runtimes = {}
_runtime_lock = threading.Lock()


def runtime_for(options):
    """One resident process per interpreter; device, checkpoint and timeout travel with each request."""
    python = runtime_python(options)
    with _runtime_lock:
        if python not in _runtimes:
            for stale in _runtimes.values():
                with stale.lock:
                    stale.close()
            _runtimes.clear()
            _runtimes[python] = LayaRuntime({**options, "python": python})
        runtime = _runtimes[python]
    return SimpleNamespace(predict=functools.partial(runtime.predict, options=options))


@atexit.register
def close_runtimes():
    for runtime in _runtimes.values():
        runtime.close()


def append_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
        handle.flush()
        fcntl.flock(handle, fcntl.LOCK_UN)


def read_jsonl(path):
    if not path.exists():
        return []
    records = []
    with path.open() as handle:
        for line in handle:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def reviewed_labels(events):
    """Effective approved answers and explicit exclusions, shared by UI and export."""
    labels, exclusions = {}, {}
    for event in events:
        key = event.get("id")
        if event.get("event") == "label" and event.get("verified"):
            if event.get("replace"):
                labels[key] = {}
            labels.setdefault(key, {}).update(event["answers"])
        elif event.get("event") == "label_exclusion":
            exclusions[key] = event.get("excluded") is True
    return labels, exclusions


def label_provenance(events):
    effective = {}
    for event in events:
        if event.get("event") != "label" or not event.get("verified"):
            continue
        if event.get("replace"):
            effective[event["id"]] = {}
        for key in event["answers"]:
            effective.setdefault(event["id"], {})[key] = {
                "source": event.get("source", "human"), "suggestion_id": event.get("suggestion_id"),
                "reviewers": event.get("reviewers", []), "time_ms": event.get("time_ms"),
                "approval_rule": event.get("approval_rule"), "unavailable_members": event.get("unavailable_members", []),
            }
    return effective


class DecisionStore:
    def __init__(self, workspace):
        self.root = Path(workspace) / ".fusion" / "decisions"
        self.path = self.root / "events.jsonl"

    def append(self, event, **payload):
        append_json(self.path, {"schema": "fusion.decision.v1", "event": event,
                               "time_ms": int(time.time() * 1000), **payload})

    @contextlib.contextmanager
    def review_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "reviews.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def records(self):
        return [record for record in read_jsonl(self.path) if record.get("event") == "decision"]

    def get(self, decision_id):
        for record in reversed(self.records()):
            if record.get("id") == decision_id:
                return record
        raise ValueError(f"unknown decision: {decision_id}")

    def summaries(self, decisions):
        """Recommendation and application per kind for a receipt's decision ids."""
        by_id = {str(identifier): kind for kind, identifier in (decisions or {}).items()}
        found = {}
        for event in read_jsonl(self.path):
            kind = by_id.get(event.get("id"))
            if kind is None:
                continue
            entry = found.setdefault(kind, {"id": event["id"]})
            if event.get("event") == "decision":
                entry.update(status=event.get("status"), recommendations=event.get("recommendations") or {})
            elif event.get("event") == "application":
                entry.update(actual=event.get("actual"), applied=bool(event.get("applied")))
        return found

    def label(self, decision_id, answers, evidence, suggestion_id=None, replace=False):
        with self.review_lock():
            return self._label(decision_id, answers, evidence, suggestion_id, replace)

    def _label(self, decision_id, answers, evidence, suggestion_id=None, replace=False):
        record = self.get(decision_id)
        if not labelable_record(record):
            raise ValueError("label only successful, complete model inputs; shorten truncated inputs and run again")
        if not isinstance(evidence, str) or not evidence.strip() or not isinstance(answers, dict) or not answers:
            raise ValueError("reviewed labels require answers and verification evidence")
        for key, label in answers.items():
            question = record["questions"].get(key)
            if not question or label not in labels_for(question):
                raise ValueError(f"invalid label {key}={label}")
        provenance = {"source": "human"}
        if suggestion_id:
            from fusion_labeling import approval_provenance
            provenance = approval_provenance(self, record, suggestion_id, answers)
        self.append("label", id=decision_id, answers=answers, evidence=evidence, verified=True, replace=replace, **provenance)

    def export(self, destination, exclude_sources=(), split="time"):
        """Labeled examples as fusion.training.v1 rows, split by workflow group
        (assign_splits). Each row carries the deterministic policy's answers
        (`heuristic`) so evaluation can report that baseline."""
        events = read_jsonl(self.path)
        labels, exclusions = reviewed_labels(events)
        provenance = label_provenance(events)
        excluded_sources = set(exclude_sources)
        rows, over_budget = [], 0
        records = {e["id"]: e for e in events if e.get("event") == "decision"}
        applications = {e.get("id"): e for e in events if e.get("event") == "application"}
        first_seen = group_first_seen(records.values())
        for record in records.values():
            origin = provenance.get(record["id"], {})
            kept = {key: value for key, value in labels.get(record["id"], {}).items()
                    if origin.get(key, {}).get("source", "human") not in excluded_sources}
            if kept and not exclusions.get(record["id"]) and not record.get("truncated") and exceeds_token_budget(record):
                over_budget += 1
            if not kept or exclusions.get(record["id"]) or not labelable_record(record):
                continue
            group = record_group(record)
            rows.append({"schema": "fusion.training.v1", "id": record["id"], "group": group,
                         "group_first_ms": first_seen.get(group),
                         "kind": record["kind"], "state": record["state"], "questions": record["questions"],
                         "role": normalize_role((record.get("context") or {}).get("role")),
                         "labels": kept, "prediction": record["prediction"],
                         "heuristic": heuristic_answers(record, applications.get(record["id"])),
                         "label_provenance": {key: origin[key] for key in kept if key in origin},
                         "model_identity": record.get("model_identity"), "schema_hash": record["schema_hash"]})
        splits = assign_splits({row["group"]: row["group_first_ms"] for row in rows}, split)
        rows = [{**row, "split": splits[row["group"]]} for row in rows]
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        destination.chmod(0o600)
        from fusion_quality import dataset_quality
        return {"examples": len(rows), "splits": dict(Counter(row["split"] for row in rows)), "split_method": split,
                "path": str(destination),
                "skipped_over_token_budget": over_budget,
                "data_quality": dataset_quality(rows), "dataset_hash": hashlib.sha256(destination.read_bytes()).hexdigest()}


def labels_for(question):
    if question["type"] == "noul":
        return ["false", "true"]
    if question["type"] == "score":
        return [str(index) for index in range(len(question["criteria"]))]
    return list(question["criteria"])


def distribution(answer, question):
    labels = labels_for(question)
    if question["type"] == "noul":
        p = answer.get("noul")
        values = {"false": 1 - p, "true": p} if isinstance(p, (int, float)) and not isinstance(p, bool) else {}
    else:
        values = answer.get("probabilities") or {}
    if set(values) != set(labels):
        raise ValueError("model returned an unexpected label set")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in values.values()):
        raise ValueError("model returned invalid probabilities")
    total = sum(values.values())
    if not 0.98 <= total <= 1.02:
        raise ValueError("model returned an invalid probability sum")
    return {key: value / total for key, value in values.items()}


def temperature_scale(probs, temperature):
    scores = {key: math.log(max(value, 1e-9)) / temperature for key, value in probs.items()}
    maximum = max(scores.values())
    values = {key: math.exp(value - maximum) for key, value in scores.items()}
    total = sum(values.values())
    return {key: value / total for key, value in values.items()}


def normalize_role(role):
    """Stable evidence class; absent, empty and unknown roles use legacy evidence."""
    value = "-".join(role.lower().split()) if isinstance(role, str) else ""
    return value if value and value != "unknown" else None


def calibration_bucket(report, record, question):
    buckets = report.get("buckets", {})
    prefix = f"{record['kind']}:{record['schema_hash']}"
    role = normalize_role((record.get("context") or {}).get("role"))
    if role and f"{prefix}:{role}:{question}" in buckets:
        return buckets[f"{prefix}:{role}:{question}"]
    return buckets.get(f"{prefix}:{question}", {})


class DecisionEngine:
    def __init__(self, workspace, config, backend=None):
        self.workspace = Path(workspace)
        self.options = config_for(config)
        if self.options["model_path"]:
            path = Path(self.options["model_path"]).expanduser()
            self.options["model_path"] = str((self.workspace / path).resolve())
        self.options["checkpoints"] = {kind: value if value in CHECKPOINTS else str((self.workspace / Path(value).expanduser()).resolve())
                                       for kind, value in self.options["checkpoints"].items()}
        self.store = DecisionStore(workspace)
        self.backend = backend

    def checkpoint(self, kind):
        """The checkpoint a `kind` decision runs on: its `decisions.checkpoints`
        entry, else `model_path`, else the English checkpoint (None: the
        runtime's default routing)."""
        return self.options["checkpoints"].get(kind) or self.options["model_path"] or None

    def state_tokens(self, kind):
        return state_tokens(kind, self.checkpoint(kind))

    def calibration(self):
        path = self.options.get("calibration_file")
        if not path:
            return {}
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            report = json.loads(candidate.read_text())
            if report.get("schema") != "fusion.calibration.v1" or not report.get("model_identity"):
                return {}
            for bucket in report.get("buckets", {}).values():
                temperature, threshold = float(bucket["temperature"]), float(bucket["threshold"])
                if not math.isfinite(temperature) or temperature <= 0 or not 0 < threshold <= 1 or not isinstance(bucket.get("qualified"), bool):
                    return {}
                certified = (bucket.get("risk") or {}).get("threshold")
                if certified is not None and not 0 < float(certified) <= 1:
                    return {}
            return report
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return {}

    def new_record(self, kind, questions, context, state=None):
        if kind not in KINDS:
            raise ValueError(f"unknown decision kind: {kind}")
        context = dict(context or {})
        role = normalize_role(context.get("role", state.get("role") if isinstance(state, dict) else None))
        if role:
            context["role"] = role
        else:
            context.pop("role", None)
        return {"id": uuid.uuid4().hex, "kind": kind, "mode": self.options["mode"],
                "state_version": STATE_VERSION,
                "context": context or {}, "questions": questions, "schema_hash": digest(questions),
                "status": "off", "recommendations": {}, "prediction": {}, "state_tokens": self.state_tokens(kind)}

    def encode(self, record, state):
        text = json.dumps(state, ensure_ascii=False, sort_keys=True)
        cap = state_cap(self.options)
        record["state"] = text[:cap]
        record["truncated"] = len(text) > cap or bool(isinstance(state, dict) and state.get("source_truncated"))
        record["source_truncated"] = bool(isinstance(state, dict) and state.get("source_truncated"))

    def record_unscored(self, kind, state, questions, context=None, encoded=None, **extra):
        """Record a decision input without inference, so a verified answer can be
        attached to it. Same encoding and truncation as decide(), or an already
        encoded complete input; no prediction, no recommendation, and allowed()
        can never act on it. No model checks its fit, so an input over the
        estimated token budget (exceeds_token_budget) is recorded truncated."""
        record = {**self.new_record(kind, questions, context, state), "status": "unscored", "duration_ms": 0, **extra}
        if encoded is None:
            self.encode(record, state)
        else:
            record.update(state=encoded, truncated=False)
        record["truncated"] = record["truncated"] or exceeds_token_budget(record)
        self.store.append("decision", **record)
        return record

    def decide(self, kind, state, questions, context=None):
        record = self.new_record(kind, questions, context, state)
        if self.options["mode"] == "off":
            return record
        self.encode(record, state)
        started = time.monotonic()
        try:
            for key, question in questions.items():
                if question.get("type") == "choice" and len(question.get("criteria") or {}) < 2:
                    raise ValueError(f"choice question {key} needs at least two options")
            with progress.activity("laya", f"{kind}: waiting for local classification ({self.options['mode']})"):
                configured = self.options["checkpoints"].get(kind)
                prediction = (self.backend or runtime_for(self.options)).predict(
                    record["state"], questions, **({"checkpoint": configured} if configured else {}))
            answers = prediction.get("answers") or {}
            record["model_identity"] = prediction["model_identity"]
            record["routing"] = prediction.get("routing", {})
            record["truncated"] |= bool(prediction.get("truncated"))
            calibration = self.calibration()
            for key, question in questions.items():
                probs = distribution(answers.get(key, {}), question)
                record["prediction"][key] = probs
                bucket = calibration_bucket(calibration, record, key)
                if calibration.get("model_identity") == record["model_identity"]:
                    probs = temperature_scale(probs, float(bucket.get("temperature", 1)))
                selected = max(probs, key=probs.get)
                record["recommendations"][key] = {"value": selected, "probability": probs[selected]}
            record["status"] = "ok"
        except (ImportError, OSError, RuntimeError, ValueError, KeyError, TypeError, ArithmeticError, AttributeError) as exc:
            record.update(status="unavailable", error=str(exc), recommendations={})
        record["duration_ms"] = round((time.monotonic() - started) * 1000)
        self.store.append("decision", **record)
        if record["status"] == "ok":
            choices = ", ".join(f"{key}={value['value']} (p={value['probability']:.2f})" for key, value in record["recommendations"].items())
            progress.emit("laya", f"{kind}: {choices}; {record['duration_ms']}ms" + ("; input truncated, automatic action disabled" if record["truncated"] else ""))
        else:
            progress.emit("laya", f"{kind}: unavailable; using deterministic policy — {record.get('error', 'unknown error')}")
        return record

    def allowed(self, record, question):
        if (self.options["mode"] != "active" or record["kind"] not in self.options["auto_actions"]
                or record.get("status") != "ok" or record.get("truncated")):
            return False
        report = self.calibration()
        if report.get("model_identity") != record.get("model_identity"):
            return False
        bucket = calibration_bucket(report, record, question)
        # A Learn-then-Test threshold certified on held-out groups replaces the
        # fixed decisions.threshold; a report without one keeps the older rule.
        certified = (bucket.get("risk") or {}).get("threshold")
        threshold = (float(certified) if certified is not None
                     else max(float(self.options["threshold"]), float(bucket.get("threshold", 1))))
        # Re-derive the probability from the raw distribution under the
        # calibration being read *now*, rather than trusting the one decide()
        # stored. Those can be different files: the weekly loop rewrites
        # calibration between runs, and a long-lived process holds records
        # scaled by the old temperature. Comparing a stale probability to a
        # fresh threshold is how an action gets applied at a confidence the
        # current calibration would reject. Temperature scaling is monotonic,
        # so the selected label is unchanged -- only its magnitude moves.
        probabilities = record.get("prediction", {}).get(question) or {}
        if not probabilities:
            return False
        try:
            scaled = temperature_scale(probabilities, float(bucket.get("temperature", 1)))
        except (ArithmeticError, TypeError, ValueError):
            return False
        return bool(bucket.get("qualified") and max(scaled.values()) >= threshold)

    def applied(self, record, actual, applied=False, reason="shadow mode or unqualified recommendation", **extra):
        if record.get("status") != "off":
            self.store.append("application", id=record["id"], kind=record["kind"], actual=actual, applied=applied, reason=reason, **extra)
            progress.emit("laya", f"{record['kind']}: action={actual}; {'recommendation applied' if applied else 'advisory only'} ({reason})")


def fit_calibration(dataset, output, threshold=0.9, risk=None):
    """Fit a temperature per question bucket on train groups, then decide on
    held-out groups whether and above which confidence the bucket may act.

    The acting threshold comes from Learn-then-Test (fusion_risk) on held-out
    (confidence, correct) pairs, scaled by the train-fitted temperature --
    fitting it on held-out answers too would reuse them. A bucket qualifies
    only with a certified threshold, at least `risk.min_examples` held-out
    answers, and at least `risk.min_groups` train groups and acted held-out
    groups (answers within one workflow are not independent). `threshold` is
    only the fallback for reporting when nothing is certified.
    """
    from fusion_laya import dataset_rows
    from fusion_risk import learn_then_test
    risk = risk_options(risk or {})
    rows = dataset_rows(dataset)
    if not 0 < threshold <= 1:
        raise ValueError("threshold must be in (0, 1]")
    # Examples recorded without inference (verdict labels) carry no prediction
    # to calibrate; evaluate a checkpoint on the dataset to score them.
    scored = [row for row in rows if all(key in (row.get("prediction") or {}) for key in row["labels"])]
    unscored, rows = len(rows) - len(scored), scored
    if not rows:
        raise ValueError("no example in this dataset has stored predictions; run evaluate on it, then calibrate the evaluation output")
    identities = {row["model_identity"] for row in rows}
    if len(identities) != 1:
        raise ValueError("calibration requires reviewed examples from exactly one model identity")
    groups = {}
    buckets = {}
    role_buckets = set()
    for row in rows:
        group, split = row["group"], row["split"]
        if split not in {"train", "validation"} or group in groups and groups[group] != split:
            raise ValueError("train/validation group leakage or invalid split")
        groups[group] = split
        for key, label in row["labels"].items():
            probs = row["prediction"][key]
            if label not in probs:
                raise ValueError("label not present in model probabilities")
            prefix = f"{row['kind']}:{row['schema_hash']}"
            keys = [f"{prefix}:{key}"]
            role = normalize_role(row.get("role"))
            if role:
                role_key = f"{prefix}:{role}:{key}"
                keys.append(role_key)
                role_buckets.add(role_key)
            if any(not isinstance(p, (int, float)) or not math.isfinite(p) or p < 0 or p > 1 for p in probs.values()) or abs(sum(probs.values()) - 1) > 0.02:
                raise ValueError("invalid stored probabilities")
            for bucket_key in keys:
                bucket = buckets.setdefault(bucket_key, {"train": [], "validation": []})
                bucket[split].append((probs, label, group))
    report = {"schema": "fusion.calibration.v1", "model_identity": next(iter(identities)),
              "dataset_hash": hashlib.sha256(Path(dataset).read_bytes()).hexdigest(), "buckets": {},
              "unscored_examples": unscored, "risk": risk}
    for key, samples in buckets.items():
        train, validation = samples["train"], samples["validation"]
        candidates = [0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8]
        temperature = min(candidates, key=lambda t: sum(-math.log(max(temperature_scale(p, t)[y], 1e-9)) for p, y, _ in train)) if train else 1
        pairs = []
        for probs, label, _ in validation:
            scaled = temperature_scale(probs, temperature)
            selected = max(scaled, key=scaled.get)
            pairs.append((scaled[selected], selected == label))
        gate = learn_then_test(pairs, risk["alpha"], risk["delta"], risk["min_examples"])
        acting = gate["threshold"] if gate["threshold"] is not None else threshold
        correct = confident = confident_correct = 0
        confident_groups = set()
        brier = 0.0
        bins = [[0, 0.0, 0] for _ in range(10)]
        certain_errors = {"0.9": 0, "0.95": 0, "0.99": 0}
        for probs, label, group in validation:
            p = temperature_scale(probs, temperature)
            selected = max(p, key=p.get)
            hit = selected == label
            correct += hit
            brier += sum((v - (k == label)) ** 2 for k, v in p.items())
            top = p[selected]
            bucket_bin = bins[min(9, int(top * 10))]
            bucket_bin[0] += 1
            bucket_bin[1] += top
            bucket_bin[2] += hit
            for floor in certain_errors:
                if top >= float(floor) and not hit:
                    certain_errors[floor] += 1
            if top >= acting:
                confident += 1
                confident_correct += hit
                confident_groups.add(group)
        selective_accuracy = confident_correct / confident if confident else None
        # Expected calibration error over the selected label's probability, ten equal bins.
        reliability = [{"floor": index / 10, "count": count, "confidence": total / count, "accuracy": hits / count}
                       for index, (count, total, hits) in enumerate(bins) if count]
        ece = sum(item["count"] / len(validation) * abs(item["accuracy"] - item["confidence"]) for item in reliability) if validation else None
        train_groups = len({group for _, _, group in train})
        reasons = [gate["reason"]] if gate["threshold"] is None else []
        if train_groups < risk["min_groups"]:
            reasons.append(f"not qualified (train groups {train_groups}<{risk['min_groups']})")
        if gate["threshold"] is not None and len(confident_groups) < risk["min_groups"]:
            reasons.append(f"not qualified (acted held-out groups {len(confident_groups)}<{risk['min_groups']})")
        # Sparse or uncertified roles retain the role-less fallback.
        if key in role_buckets and reasons:
            continue
        report["buckets"][key] = {
            "temperature": temperature, "threshold": acting, "train": len(train), "validation": len(validation),
            "risk": {name: value for name, value in gate.items() if name != "reason"},
            "status": "; ".join(reasons) or "qualified",
            "accuracy": correct / len(validation) if validation else None,
            "brier": brier / len(validation) if validation else None,
            "ece": ece, "reliability": reliability, "certain_errors": certain_errors,
            "coverage": confident / len(validation) if validation else 0,
            "selective_accuracy": selective_accuracy,
            "train_groups": train_groups, "confident_validation_groups": len(confident_groups),
            "qualified": not reasons,
        }
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with Path(output).open("x") as handle:
        json.dump(report, handle, indent=2)
    return report
