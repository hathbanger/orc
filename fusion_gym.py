"""ORC gym: replay merged fix PRs as benchmark tasks, per lane.

`extract` turns a squash-merged PR (commit C, parent B) into a task in the
SWE-smith / SWE-bench shape: the task tree is B plus C's test-file changes,
FAIL_TO_PASS are the changed or new unittest tests that fail on the task tree
and pass on C, PASS_TO_PASS the other tests in those files that pass on both.
The task commit is written to `refs/gym/tasks/pr-N` and B to
`refs/gym/bases/pr-N` in the source repository; nothing else there changes
(trees are materialized with `git archive`). The task also carries a hidden
form: C's test files, as fixtures; and `interface`, the names and signatures
those tests call that the fix added or changed (fusion_gym_interface).

`run` gives each task to each lane in its own git worktree of a per-task
repository under the gym directory. In the default hidden mode that
repository holds only B and its history, the worker gets only the problem
text, and C's test files are acceptance fixtures the coordinator writes into
the tree only while the checks run (SWE-bench style). hidden+hints (the CLI
default; `--no-interface-hints` for plain hidden) adds the task's interface
to the prompt, so tests that call a new name are not unguessable. In visible mode
(`--visible-tests`) it holds the task commit, tests included. Neither holds
C. The worker runs as an authored single-node workflow whose acceptance
checks are the F2P and P2P commands with `acceptance.before: true`, so the
gate labels each run from the checks' before/after exit codes.

`run --kind localize` is the read-only kind: the worker gets the problem
text and B, names the files and symbols the fix must change, and the gym
grades that against the fix's diff (fusion_gym_localize); no checks run.

`report` reads the gym's results: per kind, mode, lane and task, from
receipts only. `priors` exports them per lane and work class (fix: write,
localize: read) as the lane priors automatic routing reads (fusion_policy).
"""
from __future__ import annotations

import ast
import base64
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from typing import Any

TASK_SCHEMA = "fusion.gym.task.v1"
RESULT_SCHEMA = "fusion.gym.result.v1"
REF_PREFIX = "refs/gym/tasks/"
BASE_REF_PREFIX = "refs/gym/bases/"
TASK_REF = "refs/gym/task"
BASE_REF = "refs/gym/base"
MODES = ("hidden", "hidden+hints", "visible")
KINDS = ("fix", "localize", "interpret")
GRADE_SOURCE = "gym_grade"
HIDDEN_MODES = ("hidden", "hidden+hints")
DEFAULT_TIMEOUT = 900
DEFAULT_P2P_LIMIT = 200
TEST_DIRS = {"test", "tests"}
# The interpreter the checks name. It is looked up on PATH, as the gate's
# acceptance checks are, so extraction and the gym run the same Python.
PYTHON = "python3"
# Lanes named on the command line. `gym.lanes` in the gym's .fusion.json adds
# or replaces entries; a configured route name is also a lane.
DEFAULT_LANES = {
    "claude-sonnet-high": {"agent": "claude", "model": "claude-sonnet-5", "reasoning_effort": "high"},
    "claude-opus-high": {"agent": "claude", "model": "claude-opus-5-5", "reasoning_effort": "high"},
    "claude-fable-medium": {"agent": "claude", "model": "claude-fable-5-1", "reasoning_effort": "medium"},
    "claude": {"agent": "claude"},
    "codex": {"agent": "codex"},
    "agy": {"agent": "agy"},
    # agy names its thinking level in the model id; its default is
    # gemini-3.8-flash-high, so these measure the same model at lower levels.
    "agy-flash-medium": {"agent": "agy", "model": "gemini-3.8-flash-medium"},
    "agy-flash-low": {"agent": "agy", "model": "gemini-3.8-flash-low"},
    "grok": {"agent": "grok"},
    "opencode": {"agent": "opencode"},
}
LANE_KEYS = {"agent", "route", "model", "reasoning_effort"}
# Run in each tree by `extract`: per-test outcomes for the given files, written
# as JSON to argv[1] so a test's own stdout cannot corrupt it.
DRIVER = r'''
import json, sys, unittest
from pathlib import Path
out = {}
class Result(unittest.TestResult):
    def addSuccess(self, test): out.setdefault(test.id(), "passed")
    def addFailure(self, test, err): out[test.id()] = "failed"
    def addError(self, test, err): out[test.id()] = "error"
    def addSkip(self, test, reason): out[test.id()] = "skipped"
    def addExpectedFailure(self, test, err): out[test.id()] = "skipped"
    def addUnexpectedSuccess(self, test): out[test.id()] = "failed"
    def addSubTest(self, test, subtest, err):
        if err is not None:
            out[test.id()] = "failed"
for relative in sys.argv[2:]:
    path = Path.cwd() / relative
    try:
        suite = unittest.TestLoader().discover(str(path.parent), pattern=path.name, top_level_dir=str(path.parent))
    except Exception:
        continue
    suite.run(Result())
Path(sys.argv[1]).write_text(json.dumps(out))
'''
_PR_SUBJECT = r"\(#{}\)\s*$"
# A PR body paragraph that starts describing the change. A bullet list is
# taken as a change list too: PR bodies list what they did, not the bug.
_FIX_CUE = re.compile(
    r"^\s*(?:[-*+]\s|\d+[.)]\s|(?:\*\*|__)?(?:this (?:pr|change|patch|commit)\b|the fix\b|fix(?:es|ed)?:|now\b|new\b|changes?\b|"
    r"validation\b|tests?\b|made with\b|\U0001F916|docs?\b|implementation\b|solution\b|approach\b|with this\b|"
    r"(?:adds?|added|introduces?|replaces?|removes?|moves?|renames?|refactors?|switch(?:es)?|makes?|uses?|"
    r"updates?|extends?|drops?|splits?|keeps?|stops?|reworks?)\b))",
    re.I)
# A line inside a kept paragraph that states the fix ("- Fix: ...").
_FIX_LINE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*|__)?(?:fix(?:es|ed)?|solution|resolution|the fix|now)\b\W", re.I)
_FIX_HEADING = re.compile(r"^#+\s*(?:fix|solution|proposal|proposed|implementation|plan|changes?|approach|validation|test)",
                          re.I | re.M)


# ---------------------------------------------------------------- git helpers

def git(repo, *args, env=None, binary=False, check=True):
    proc = subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=repo, capture_output=True,
                          env={**os.environ, **env} if env else None, stdin=subprocess.DEVNULL)
    if check and proc.returncode:
        raise ValueError(f"git {' '.join(args[:2])} failed: {proc.stderr.decode(errors='replace').strip()[:600]}")
    return proc.stdout if binary else proc.stdout.decode(errors="replace")


def archive(repo, rev, destination):
    """Materialize a commit's tree without touching the repository's worktrees."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(git(repo, "archive", "--format=tar", rev, binary=True))) as tar:
        safe_extract(tar, destination)
    return destination


def safe_extract(tar, destination):
    """`extractall(filter="data")`, also on Pythons without extraction filters.

    The filter argument exists from 3.12 (and in late 3.8-3.11 patch releases);
    macOS still ships 3.9.6 as /usr/bin/python3, where it raised TypeError and
    `gym extract` failed. The fallback keeps the data filter's guarantees that
    matter for a git archive: members stay inside destination, links may not
    point outside it, and only regular files, directories and links are written.
    """
    if hasattr(tarfile, "data_filter"):
        tar.extractall(destination, filter="data")
        return
    root = Path(destination).resolve()
    members = []
    for member in tar.getmembers():
        target = (root / member.name).resolve()
        if not target.is_relative_to(root) or not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise ValueError(f"refusing to extract {member.name!r} outside {root}")
        if member.issym() or member.islnk():
            base = target.parent if member.issym() else root
            if not (base / member.linkname).resolve().is_relative_to(root):
                raise ValueError(f"refusing to extract link {member.name!r} -> {member.linkname!r} outside {root}")
        member.mode &= 0o755  # no setuid/setgid or group/other write, like the data filter
        members.append(member)
    tar.extractall(root, members=members)


def is_test_path(path):
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    return bool(set(parts[:-1]) & TEST_DIRS) or bool(re.fullmatch(r"test_.*\.py|.*_test\.py", name))


def is_unittest_file(path):
    return bool(re.fullmatch(r"test_.*\.py|.*_test\.py", PurePosixPath(path).name))


# ---------------------------------------------------------------- PR text

def gh_json(repo, *args):
    proc = subprocess.run(["gh", *args], cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    if proc.returncode:
        raise ValueError("GitHub read failed: " + (proc.stderr.strip() or "check gh auth status")[:600])
    return json.loads(proc.stdout)


def pr_info(repo, pr, gh_repo=None, use_gh=True):
    """Title, body, linked issues and merge commit of a PR, or {} without gh."""
    if not use_gh:
        return {}
    scope = ["-R", gh_repo] if gh_repo else []
    info = gh_json(repo, "pr", "view", str(pr), *scope, "--json", "title,body,mergeCommit,closingIssuesReferences,url")
    issues = []
    for reference in info.get("closingIssuesReferences") or []:
        owner = ((reference.get("repository") or {}).get("owner") or {}).get("login")
        name = (reference.get("repository") or {}).get("name")
        issue_scope = ["-R", f"{owner}/{name}"] if owner and name else scope
        issue = gh_json(repo, "issue", "view", str(reference["number"]), *issue_scope, "--json", "title,body,url")
        issues.append({"number": reference["number"], **issue})
    return {"title": info.get("title", ""), "body": info.get("body", ""), "url": info.get("url"),
            "merge_oid": (info.get("mergeCommit") or {}).get("oid"), "issues": issues}


def _unescape(text):
    return (text or "").replace("\\`", "`").replace("\r\n", "\n")


def _drop_fix_code(text):
    """Remove fenced code blocks that show a change (diffs, +/- lines)."""
    def keep(match):
        block = match.group(0)
        language = match.group(1).strip().lower()
        lines = block.splitlines()[1:-1]
        diff = language in {"diff", "patch"} or any(line.startswith(("diff --git", "@@", "+++", "---")) for line in lines)
        return "" if diff else block
    return re.sub(r"```([^\n]*)\n.*?\n```", keep, text, flags=re.S)


def strip_title(title):
    """`fix(scope): read labels` -> `read labels`."""
    return re.sub(r"^\s*[a-z]+(?:\([^)]*\))?!?:\s*", "", title or "").strip()


def problem_text(body):
    """The paragraphs of a PR body before it starts describing the fix."""
    body = _drop_fix_code(_unescape(body))
    heading = _FIX_HEADING.search(body)
    if heading:
        body = body[:heading.start()]
    body = re.sub(r"^\s*(?:fixes|closes|resolves)\s+#\d+\.?\s*", "", body.strip(), flags=re.I)
    kept = []
    for paragraph in re.split(r"\n\s*\n", body):
        text = paragraph.strip()
        if not text.startswith("```"):
            text = "\n".join(line for line in text.splitlines()
                             if not re.match(r"\s*#+\s", line) and not _FIX_LINE.match(line)).strip()
        if not text:
            continue
        first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
        if not text.startswith("```") and (_FIX_CUE.match(text) or re.search(r"\bnow\b", first, re.I)):
            break
        kept.append(text)
    return "\n\n".join(kept).strip()


def task_prompt(info, fallback_subject):
    """(prompt, source). A linked issue states the problem without the fix;
    otherwise the PR title and the body's problem paragraphs."""
    if info.get("issues"):
        parts = []
        for issue in info["issues"]:
            body = _drop_fix_code(_unescape(issue.get("body")))
            heading = _FIX_HEADING.search(body)
            parts.append(f"{issue.get('title', '').strip()}\n\n{(body[:heading.start()] if heading else body).strip()}".strip())
        return "\n\n---\n\n".join(parts), "issue"
    if info.get("title"):
        problem = problem_text(info.get("body"))
        return (strip_title(info["title"]) + ("\n\n" + problem if problem else "")).strip(), "pr"
    return strip_title(re.sub(r"\s*\(#\d+\)(?:\s*\(#\d+\))*\s*$", "", fallback_subject)), "commit"


# ---------------------------------------------------------------- extraction

def succeeds(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, stdin=subprocess.DEVNULL).returncode == 0


def find_commit(repo, pr, ref, merge_oid=None):
    if merge_oid and succeeds(repo, "cat-file", "-e", merge_oid + "^{commit}") and \
            succeeds(repo, "merge-base", "--is-ancestor", merge_oid, ref):
        return merge_oid
    pattern = re.compile(_PR_SUBJECT.format(int(pr)))
    for line in git(repo, "log", "--format=%H%x00%s", ref).splitlines():
        sha, _, subject = line.partition("\0")
        if pattern.search(subject):
            return sha
    raise ValueError(f"no commit for PR #{pr} on {ref}")


def test_methods(source):
    """{Class.method: source} for unittest-style tests, plus per-class and
    module-level fixture code, so a changed helper marks its tests changed."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    tests, fixtures = {}, {"": []}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            fixtures[node.name] = []
            for item in node.body:
                text = ast.get_source_segment(source, item) or ""
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    tests[f"{node.name}.{item.name}"] = text
                else:
                    fixtures[node.name].append(text)
        else:
            fixtures[""].append(ast.get_source_segment(source, node) or "")
    return tests, fixtures


def changed_tests(before, after, module):
    """Test ids in `after` that are new, changed, or depend on changed fixtures."""
    new = test_methods(after)
    if new is None:
        return set()
    tests, fixtures = new
    old = test_methods(before) if before is not None else None
    if old is None or old[1].get("") != fixtures.get(""):
        return {f"{module}.{name}" for name in tests}
    old_tests, old_fixtures = old
    return {f"{module}.{name}" for name, text in tests.items()
            if old_tests.get(name) != text or old_fixtures.get(name.split(".")[0]) != fixtures.get(name.split(".")[0])}


def run_tests(tree, files, timeout=DEFAULT_TIMEOUT):
    from fusion_verification import OFFLINE_ENV
    with tempfile.TemporaryDirectory(prefix="fusion-gym-") as temp:
        out = Path(temp) / "outcomes.json"
        try:
            subprocess.run([PYTHON, "-c", DRIVER, str(out), *files], cwd=tree, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=timeout, env={**os.environ, **OFFLINE_ENV})
        except subprocess.TimeoutExpired:
            return {}
        try:
            return json.loads(out.read_text())
        except (OSError, ValueError):
            return {}


def check_commands(ids, files):
    """[(argv, ids)]: one `python3 -m unittest discover` per test file.

    Test files import from the repository root via their own sys.path setup,
    so discovery starts in the file's directory; `-k '*module.Class.method'`
    is an fnmatch pattern anchored at the end of the full test name, so it
    selects exactly that test."""
    commands = []
    for path in files:
        module = PurePosixPath(path).stem
        selected = [test_id for test_id in ids if test_id.split(".")[0] == module]
        if not selected:
            continue
        directory = str(PurePosixPath(path).parent)
        argv = [PYTHON, "-m", "unittest", "discover", "-s", directory, "-t", directory, "-p", PurePosixPath(path).name]
        for test_id in selected:
            argv += ["-k", "*" + test_id]
        commands.append((argv, selected))
    return commands


def check_receipt(tree, argv, timeout):
    """Preserve real command evidence for later read-only interpretation tasks."""
    from fusion_verification import OFFLINE_ENV
    try:
        proc = subprocess.run(argv, cwd=tree, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout,
                              env={**os.environ, **OFFLINE_ENV})
        return {"argv": argv, "exit_code": proc.returncode, "status": "passed" if proc.returncode == 0 else "failed",
                "stdout": proc.stdout.decode(errors="replace"), "stderr": proc.stderr.decode(errors="replace")}
    except subprocess.TimeoutExpired:
        return {"argv": argv, "exit_code": None, "status": "timeout"}


def exit_code(tree, argv, timeout):
    return check_receipt(tree, argv, timeout)["exit_code"]


def make_task_commit(repo, base, fix, changes):
    """B plus C's test-file changes, as a commit object (no worktree touched).
    Author, committer and dates are fixed, so re-extraction gives the same sha.
    The message names no PR, so the worker cannot look the fix up from it."""
    with tempfile.TemporaryDirectory(prefix="fusion-gym-index-") as temp:
        env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
        git(repo, "read-tree", base, env=env)
        for status, path in changes:
            if status.startswith("D"):
                git(repo, "update-index", "--force-remove", "--", path, env=env)
            else:
                mode, _, rest = git(repo, "ls-tree", fix, "--", path).strip().partition(" ")
                sha = rest.split()[1]
                git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{sha},{path}", env=env)
        tree = git(repo, "write-tree", env=env).strip()
    date = git(repo, "show", "-s", "--format=%cI", base).strip()
    identity = {"GIT_AUTHOR_NAME": "orc gym", "GIT_AUTHOR_EMAIL": "gym@orc.invalid", "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_NAME": "orc gym", "GIT_COMMITTER_EMAIL": "gym@orc.invalid", "GIT_COMMITTER_DATE": date}
    return git(repo, "commit-tree", tree, "-p", base, "-m", "gym task: regression tests added", env=identity).strip()


def hidden_form(repo, task_id, base, fix, test_paths):
    """The hidden-test form of a task: the worker's tree is B, and the fix's
    test files at C (whole files, so P2P tests in them still run) are
    fixtures written only while the checks run. Files the fix deleted are not
    fixtures. B is kept reachable as `refs/gym/bases/<id>`."""
    fixtures = []
    for path in sorted(test_paths):
        if not succeeds(repo, "cat-file", "-e", f"{fix}:{path}"):
            continue
        data = git(repo, "cat-file", "blob", f"{fix}:{path}", binary=True)
        fixtures.append({"path": path, "sha256": hashlib.sha256(data).hexdigest(),
                         "content_base64": base64.b64encode(data).decode("ascii")})
    git(repo, "update-ref", BASE_REF_PREFIX + task_id, base)
    return {"base_ref": BASE_REF_PREFIX + task_id, "base_sha": base, "fixtures": fixtures}


def ensure_hidden(task):
    """Tasks extracted before the hidden form existed get it from the source
    repository (B, C and the test paths are in the task), in memory only."""
    hidden = task.get("hidden")
    if isinstance(hidden, dict) and hidden.get("base_sha") and hidden.get("fixtures"):
        return hidden
    repo = Path(task["repo_path"])
    try:
        task["hidden"] = hidden_form(repo, task["id"], task["base"], task["fix"], task.get("test_files") or [])
    except (ValueError, OSError) as exc:
        raise ValueError(f"{task['id']}: cannot derive the hidden form from {repo} ({exc}); re-extract the task") from exc
    if not task["hidden"]["fixtures"]:
        raise ValueError(f"{task['id']}: the fix left no test files to hide; re-extract the task")
    return task["hidden"]


def _reader(repo):
    def read(rev, path):
        proc = subprocess.run(["git", "-c", "core.fsmonitor=false", "cat-file", "blob", f"{rev}:{path}"], cwd=repo,
                              capture_output=True, stdin=subprocess.DEVNULL)
        return None if proc.returncode else proc.stdout.decode("utf-8", "replace")
    return read


def interface_for(repo, base, fix, test_paths, source_paths):
    """Names and signatures the fix's tests call that are new or changed at C
    (see fusion_gym_interface); never bodies, docstrings or test code."""
    from fusion_gym_interface import interface_hints
    read = _reader(repo)
    tests = [path for path in test_paths if path.endswith(".py") and read(fix, path) is not None]
    return interface_hints(read, base, fix, tests, source_paths)


def ensure_interface(task):
    """Tasks extracted before interface hints existed get them from the source
    repository (read-only), in memory only, like the hidden form."""
    if isinstance(task.get("interface"), list):
        return task["interface"]
    try:
        task["interface"] = interface_for(Path(task["repo_path"]), task["base"], task["fix"],
                                          task.get("test_files") or [], task.get("source_files") or [])
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"{task['id']}: cannot derive interface hints from {task['repo_path']} ({exc}); "
                         "re-extract the task or run with --no-interface-hints") from exc
    return task["interface"]


def localization_for(repo, base, fix):
    """Ground truth of a localization task: the fix's changed source files
    and the functions, classes and methods its hunks touch (read-only git)."""
    from fusion_gym_localize import ground_truth
    listing = git(repo, "diff", "--no-renames", "--name-status", "-z", base, fix).split("\0")
    changes = [(listing[i], listing[i + 1]) for i in range(0, len(listing) - 1, 2) if not is_test_path(listing[i + 1])]
    return ground_truth(_reader(repo), base, fix, changes,
                        lambda path: git(repo, "diff", "--no-renames", "-U0", base, fix, "--", path))


def ensure_localization(task):
    """Tasks extracted before localization existed get it from the source
    repository (read-only), in memory only, like the hidden form."""
    if isinstance(task.get("localization"), dict):
        return task["localization"]
    try:
        task["localization"] = localization_for(Path(task["repo_path"]), task["base"], task["fix"])
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"{task['id']}: cannot derive localization ground truth from {task['repo_path']} ({exc}); "
                         "re-extract the task") from exc
    return task["localization"]


def extract_one(repo, pr, ref="HEAD", gh_repo=None, use_gh=True, timeout=DEFAULT_TIMEOUT, p2p_limit=DEFAULT_P2P_LIMIT):
    """One task dict, or {"pr": N, "skipped": reason}."""
    repo = Path(repo).resolve()
    try:
        info = pr_info(repo, pr, gh_repo, use_gh)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        info = {"gh_error": str(exc)}
    fix = find_commit(repo, pr, ref, info.get("merge_oid"))
    parents = git(repo, "rev-list", "--parents", "-n", "1", fix).split()[1:]
    if len(parents) != 1:
        return {"pr": pr, "fix": fix, "skipped": "not a squash commit (needs exactly one parent)"}
    base = parents[0]
    listing = git(repo, "diff", "--no-renames", "--name-status", "-z", base, fix).split("\0")
    changes = [(listing[i], listing[i + 1]) for i in range(0, len(listing) - 1, 2)]
    tests = [(status, path) for status, path in changes if is_test_path(path)]
    sources = sorted(path for _, path in changes if not is_test_path(path))
    units = sorted(path for status, path in tests if not status.startswith("D") and is_unittest_file(path))
    if not tests:
        return {"pr": pr, "fix": fix, "skipped": "no test files changed"}
    if not sources:
        return {"pr": pr, "fix": fix, "skipped": "only test files changed"}
    if not units:
        return {"pr": pr, "fix": fix, "skipped": "no Python unittest file changed"}
    task_sha = make_task_commit(repo, base, fix, tests)
    candidates = set()
    for path in units:
        before = git(repo, "show", f"{base}:{path}", check=False) or None
        candidates |= changed_tests(before, git(repo, "show", f"{fix}:{path}"), PurePosixPath(path).stem)
    with tempfile.TemporaryDirectory(prefix="fusion-gym-trees-") as temp:
        task_tree, fix_tree = archive(repo, task_sha, Path(temp) / "task"), archive(repo, fix, Path(temp) / "fix")
        at_task, at_fix = run_tests(task_tree, units, timeout), run_tests(fix_tree, units, timeout)
        f2p = sorted(test for test in candidates if at_task.get(test, "missing") in {"failed", "error", "missing"}
                     and at_fix.get(test) == "passed")
        p2p = sorted(test for test, outcome in at_fix.items() if outcome == "passed" and at_task.get(test) == "passed"
                     and test not in f2p)[:p2p_limit]
        if not f2p:
            return {"pr": pr, "fix": fix, "skipped": "no changed test fails on the task tree and passes on the fix",
                    "candidates": len(candidates)}
        # The commands themselves are the proof: F2P fails on the task tree and
        # passes on the fix; P2P passes on both, or it is dropped.
        f2p_checks, p2p_checks, dropped = check_commands(f2p, units), [], []
        extraction_evidence = {"provenance": "Commands executed by gym extract", "task": [], "fix": []}

        def record(argv):
            before, after = check_receipt(task_tree, argv, timeout), check_receipt(fix_tree, argv, timeout)
            extraction_evidence["task"].append({**before, "head": task_sha})
            extraction_evidence["fix"].append({**after, "head": fix})
            return before["exit_code"], after["exit_code"]

        for argv, _ in f2p_checks:
            before, after = record(argv)
            if before in (0, None) or after != 0:
                return {"pr": pr, "fix": fix, "skipped": f"F2P command did not fail->pass: {' '.join(argv[:10])}"}
        for argv, ids in check_commands(p2p, units):
            if record(argv) == (0, 0):
                p2p_checks.append((argv, ids))
            else:
                dropped.append(argv)
    prompt, source = task_prompt(info, git(repo, "log", "-1", "--format=%s", fix).strip())
    task_id = f"pr-{int(pr)}"
    git(repo, "update-ref", REF_PREFIX + task_id, task_sha)
    kept_tests = [path for status, path in tests if not status.startswith("D")]
    hidden = hidden_form(repo, task_id, base, fix, kept_tests)
    interface = interface_for(repo, base, fix, kept_tests, sources)
    localization = localization_for(repo, base, fix)
    return {
        "schema": TASK_SCHEMA, "id": task_id, "pr": int(pr), "pr_url": info.get("url"),
        "issues": [issue.get("url") for issue in info.get("issues") or []],
        "repo_path": str(repo), "base": base, "fix": fix, "task_ref": REF_PREFIX + task_id, "task_sha": task_sha,
        "prompt": prompt, "prompt_source": source, **({"gh_error": info["gh_error"]} if info.get("gh_error") else {}),
        "fail_to_pass": f2p, "pass_to_pass": sorted(test for _, ids in p2p_checks for test in ids),
        "checks": {"fail_to_pass": [argv for argv, _ in f2p_checks], "pass_to_pass": [argv for argv, _ in p2p_checks]},
        **({"pass_to_pass_dropped": dropped} if dropped else {}),
        "test_files": sorted(path for _, path in tests), "source_files": sources, "hidden": hidden,
        "interface": interface, "localization": localization,
        "extraction_evidence": extraction_evidence,
        "extracted_at_ms": int(time.time() * 1000),
    }


def extract(repo, prs, out=None, ref="HEAD", gh_repo=None, use_gh=True, timeout=DEFAULT_TIMEOUT,
            p2p_limit=DEFAULT_P2P_LIMIT):
    rows = []
    for pr in prs:
        try:
            task = extract_one(repo, pr, ref, gh_repo, use_gh, timeout, p2p_limit)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            task = {"pr": pr, "skipped": str(exc)}
        if out and "schema" in task:
            Path(out).mkdir(parents=True, exist_ok=True)
            (Path(out) / f"{task['id']}.json").write_text(json.dumps(task, indent=2, ensure_ascii=False) + "\n")
        rows.append(task)
    summary = {"repo": str(Path(repo).resolve()), "ref": ref, "tasks": [
        {"id": t["id"], "pr": t["pr"], "fail_to_pass": len(t["fail_to_pass"]), "pass_to_pass": len(t["pass_to_pass"]),
         "prompt_source": t["prompt_source"], "interface": len(t.get("interface") or [])}
        for t in rows if "schema" in t],
        "skipped": [{"pr": t["pr"], "reason": t["skipped"]} for t in rows if "skipped" in t]}
    if out:
        (Path(out) / "index.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary, rows


# ---------------------------------------------------------------- running

def load_tasks(path):
    path = Path(path)
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    tasks = []
    for file in files:
        try:
            value = json.loads(file.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read gym task {file}: {exc}") from exc
        if isinstance(value, dict) and value.get("schema") == TASK_SCHEMA:
            tasks.append(value)
    if not tasks:
        raise ValueError(f"no gym tasks in {path}")
    return sorted(tasks, key=lambda t: (t.get("pr") or 0, t["id"]))


def resolve_lane(config, name):
    lanes = {**DEFAULT_LANES, **((config.get("gym") or {}).get("lanes") or {})}
    if name in lanes:
        lane = dict(lanes[name])
    elif name in (config.get("routes") or {}):
        lane = {"agent": (config["routes"][name] or {}).get("agent", "claude"), "route": name}
    else:
        raise ValueError(f"unknown gym lane {name}: use {', '.join(sorted(lanes))}, a route, or gym.lanes")
    if not isinstance(lane, dict) or set(lane) - LANE_KEYS or lane.get("agent") not in {"claude", "codex", "agy", "grok", "opencode"}:
        raise ValueError(f"gym lane {name} must be an object with agent and optional route, model, reasoning_effort")
    return lane


def _slug(value):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")[:24] or "lane"


HIDDEN_BRIEF = ("\n\nFix this in the repository. Keep the change scoped to the problem above. "
                "When you finish, tests that are not in this repository will grade the change. "
                "Add or adjust tests of your own as you see fit.")
VISIBLE_BRIEF = "\n\nFix this in the repository. Keep the change scoped to the problem above."


def interface_section(task, mode):
    """The prompt's interface section: only in hidden+hints mode, only when
    the task has hints."""
    from fusion_gym_interface import render
    return render(task.get("interface") or []) if mode == "hidden+hints" else ""


def build_spec(task, lane, budget_remaining=None, mode="visible", fixtures=None):
    """One write node. Hidden modes: the brief says hidden tests grade the work
    but names none; `fixtures` ({path, from_file}) put them in place only
    while the checks run. hidden+hints adds the interface the tests call
    (names and signatures) after the problem text."""
    checks = task["checks"]["fail_to_pass"] + task["checks"]["pass_to_pass"]
    acceptance = {"checks": checks, "before": True, "required_handoff": ["summary"],
                  "fail_to_pass": list(task["checks"]["fail_to_pass"])}
    hidden = mode in HIDDEN_MODES
    if hidden:
        acceptance["fixtures"] = fixtures or []
    node = {"id": "implement", "role": "implementation", "agent": lane["agent"], "write": True,
            "task": task["prompt"] + interface_section(task, mode) + (HIDDEN_BRIEF if hidden else VISIBLE_BRIEF),
            "decision_context": task["prompt"], "acceptance": acceptance}
    for key in ("route", "model", "reasoning_effort"):
        if lane.get(key):
            node[key] = lane[key]
    if budget_remaining is not None and lane["agent"] == "claude":
        node["max_budget_usd"] = round(max(budget_remaining, 0.01), 2)
    return {"task": f"gym {task['id']}: {task['prompt'].splitlines()[0][:200]}", "max_attempts": 1,
            "budget_usd": max(budget_remaining, 0.01) if budget_remaining is not None else 0, "nodes": [node]}


def build_localize_spec(task, lane, budget_remaining=None):
    """One read-only node: the problem text (never interface hints: they name
    the symbols the answer is graded on) and the localization brief. No
    checks run; the gym grades the answer after the run."""
    from fusion_gym_localize import BRIEF
    node = {"id": "localize", "role": "localization", "agent": lane["agent"], "write": False,
            "task": task["prompt"] + BRIEF, "decision_context": task["prompt"],
            "acceptance": {"required_handoff": ["summary"]}}
    for key in ("route", "model", "reasoning_effort"):
        if lane.get(key):
            node[key] = lane[key]
    if budget_remaining is not None and lane["agent"] == "claude":
        node["max_budget_usd"] = round(max(budget_remaining, 0.01), 2)
    return {"task": f"gym localize {task['id']}: {task['prompt'].splitlines()[0][:200]}", "max_attempts": 1,
            "max_parallel_writers": 0,
            "budget_usd": max(budget_remaining, 0.01) if budget_remaining is not None else 0, "nodes": [node]}


def build_interpret_spec(task, lane, budget_remaining=None):
    """The only worker-visible task material is the public bundle and brief."""
    from fusion_gym_interpret import BRIEF
    spec = build_localize_spec({"id": task["id"], "prompt": task["prompt"]}, lane, budget_remaining)
    spec["task"] = f"gym interpret {task['id']}"
    spec["nodes"][0].update(id="interpret", role="triage-interpret", task=BRIEF,
                             decision_context=task["prompt"])
    return spec


def summarize_interpret(task, lane_name, lane, outcome, diff_files, wall_ms):
    from fusion_gym_interpret import grade, parse_answer
    result = ((outcome.get("nodes") or [{}])[0].get("result") or {})
    status = outcome.get("status")
    row = {"schema": RESULT_SCHEMA, "event": "finished", "key": result_key(task["id"], lane_name, "hidden", "interpret"),
           "task": task["id"], "kind": "interpret", "mode": "hidden", "lane": lane_name, "lane_spec": lane,
           "period": task["period"], "seed": task["seed"], "workflow_id": outcome.get("workflow_id"), "status": status,
           "changed": diff_files, "tampered": bool(diff_files),
           "worker": {key: result.get(key) for key in ("status", "agent", "route", "model", "run_id")},
           "gate_label": result.get("gate_label"), "cost_usd": float(outcome.get("spent_usd") or 0),
           "duration_ms": wall_ms, "finished_at_ms": int(time.time() * 1000)}
    if not result.get("run_id") or status in {"paused_quota", "paused_budget", "interrupted"}:
        return {**row, "verdict": "unavailable" if outcome.get("lanes") else status or "not_run", "completed": False}
    answer, error = parse_answer(_read_answer(result))
    if answer is not None and set(answer) - set(task["interpretation"]):
        error = "answer contains unknown question ids"
    scores = grade(answer or {}, task["interpretation"], invalid=bool(error or diff_files))
    verdict = "tampered" if diff_files else "invalid_answer" if error else scores["verdict"]
    scores.pop("verdict")
    scores.pop("questions")  # Per-question trap/split labels never enter persistent run evidence.
    return {**row, "completed": True, "verdict": verdict, "answer": answer, "answer_error": error, "scores": scores}


def result_key(task_id, lane, mode, kind="fix"):
    """Fix keys keep their original form; other kinds append the kind, so a
    localization result never stands in for a fix result."""
    return f"{task_id}:{lane}:{mode}" + ("" if kind == "fix" else f":{kind}")


def read_results(gym):
    """Rows written before modes existed ran with the tests in the tree:
    they read as visible, so they never count as hidden results."""
    path = Path(gym) / "results.jsonl"
    rows = []
    if path.is_file():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "mode" not in row:
                row["mode"] = "visible"
                if row.get("key"):
                    row["key"] += ":visible"
            if isinstance(row, dict):
                row.setdefault("kind", "fix")
            rows.append(row)
    return rows


def _append(gym, row):
    with (Path(gym) / "results.jsonl").open("a") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


@contextlib.contextmanager
def _locked(gym):
    with (Path(gym) / ".gym.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f"another gym run holds {gym}") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def prepare_gym(gym, source_repo=None):
    """The gym directory is its own Fusion workspace (.fusion, .fusion.json).
    Its config is copied once from the source repository when it has one."""
    gym = Path(gym).resolve()
    gym.mkdir(parents=True, exist_ok=True)
    (gym / ".fusion").mkdir(exist_ok=True)
    config = gym / ".fusion.json"
    if not config.exists():
        source = Path(source_repo) / ".fusion.json" if source_repo else None
        config.write_text(source.read_text() if source and source.is_file() else "{}\n")
    return gym


def task_root(gym, task, mode):
    """Both hidden modes start from B and share its repository."""
    root = Path(gym) / "tasks" / task["id"]
    return root / "hidden" if mode in HIDDEN_MODES else root


WORKFLOW_TAGS = {"hidden": "h-", "hidden+hints": "hh-"}


def mode_dir(mode, kind="fix"):
    if kind in {"localize", "interpret"}:
        return kind
    return {"hidden": "hidden", "hidden+hints": "hidden-hints"}.get(mode, "")


def lanes_dir(mode, kind="fix"):
    return f"lanes-{kind}" if kind in {"localize", "interpret"} else "lanes-hints" if mode == "hidden+hints" else "lanes"


def task_repo(gym, task, mode="visible"):
    """A repository holding only the commit the worker starts from and its
    history: the task commit (visible) or B (hidden), never the fix. Hidden
    runs use their own repository, so the task commit's tests are not in it."""
    if mode in HIDDEN_MODES:
        source_ref, sha, local = task["hidden"]["base_ref"], task["hidden"]["base_sha"], BASE_REF
    else:
        source_ref, sha, local = task["task_ref"], task["task_sha"], TASK_REF
    repo = task_root(gym, task, mode) / "repo"
    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        git(repo, "init", "-q")
    if git(repo, "rev-parse", "-q", "--verify", local, check=False).strip() != sha:
        git(repo, "fetch", "-q", "--no-tags", "--update-shallow", task["repo_path"], f"+{source_ref}:{local}")
        if git(repo, "rev-parse", "-q", "--verify", local, check=False).strip() != sha:
            raise ValueError(f"{source_ref} in {task['repo_path']} is not {sha}; re-extract the task")
    return repo


def write_fixtures(task, directory):
    """[{path, from_file}] for the task's hidden test files, written under
    `directory` by index (not by path) and checked against their digests."""
    specs = []
    for index, fixture in enumerate(task["hidden"]["fixtures"]):
        data = base64.b64decode(fixture["content_base64"])
        if hashlib.sha256(data).hexdigest() != fixture["sha256"]:
            raise ValueError(f"{task['id']}: hidden fixture {fixture['path']} does not match its digest")
        source = Path(directory) / str(index)
        source.write_bytes(data)
        specs.append({"path": fixture["path"], "from_file": str(source)})
    return specs


def _remove_worktree(repo, path):
    if path.exists() or path.is_symlink():
        git(repo, "worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)


def withdraw_untrusted_label(gym, row):
    """A gate label is only as good as the checks it read. A worker that edited
    test files graded itself, and a baseline that did not behave as extracted
    measured something else, so the gate's label is retracted (append-only)."""
    label = row.get("gate_label") or {}
    if row.get("verdict") not in {"tampered", "invalid_baseline", "invalid_snapshot"} or label.get("status") != "labeled":
        return label
    from fusion_decisions import DecisionStore, read_jsonl
    from fusion_labeling import withdraw_untrusted_label as retract_label
    store = DecisionStore(gym)
    with store.review_lock():
        retract_label(store, label["decision_id"], "structural_gate",
                      f"Retracted by gym: verdict {row['verdict']} on {row['key']}.", read_jsonl(store.path))
    return {**label, "status": "retracted", "reason": f"gym verdict {row['verdict']}"}


def summarize(task, lane_name, lane, outcome, diff_files, wall_ms, mode="visible"):
    node = (outcome.get("nodes") or [{}])[0]
    result = node.get("result") or {}
    receipts = result.get("acceptance_checks") or []
    by_argv = {json.dumps(r.get("argv")): r for r in receipts}

    def statuses(kind):
        return [by_argv.get(json.dumps(argv), {}) for argv in task["checks"][kind]]

    f2p, p2p = statuses("fail_to_pass"), statuses("pass_to_pass")
    f2p_passed = bool(f2p) and all(r.get("status") == "passed" for r in f2p)
    p2p_regressed = any(r.get("status") == "failed" for r in p2p)
    baseline_ok = (all((r.get("before") or {}).get("status") == "failed" for r in f2p)
                   and all((r.get("before") or {}).get("status") == "passed" for r in p2p))
    if mode in HIDDEN_MODES:
        # The fixtures overwrite the hidden test paths for every check run, so
        # a worker cannot grade itself there; its edits are only recorded.
        hidden_paths = {fixture["path"] for fixture in task["hidden"]["fixtures"]}
        touched_fixtures = sorted(path for path in diff_files if path in hidden_paths)
        worker_tests = sorted(path for path in diff_files if is_test_path(path) and path not in hidden_paths)
        tampered = []
    else:
        tampered = sorted(path for path in diff_files if is_test_path(path))
    tampered = sorted(set(tampered) | set(result.get("check_inputs_changed") or []))
    dispatched = bool(result.get("run_id"))
    status = outcome.get("status")
    if not dispatched or status in {"paused_quota", "paused_budget", "interrupted"}:
        verdict, completed = ("unavailable" if outcome.get("lanes") else status or "not_run"), False
    elif not receipts:
        verdict, completed = "no_checks", False
    elif not baseline_ok:
        verdict, completed = "invalid_baseline", True
    elif tampered:
        verdict, completed = "tampered", True
    elif f2p_passed and not p2p_regressed:
        verdict, completed = "solved", True
    elif f2p_passed:
        verdict, completed = "regressed", True
    else:
        verdict, completed = "unsolved", True
    return {"schema": RESULT_SCHEMA, "event": "finished", "key": result_key(task["id"], lane_name, mode),
            "task": task["id"], "kind": "fix", "mode": mode, "lane": lane_name, "lane_spec": lane,
            "hinted": bool(interface_section(task, mode)),
            **({"interface": len(task.get("interface") or [])} if mode == "hidden+hints" else {}),
            "workflow_id": outcome.get("workflow_id"), "status": status,
            "verdict": verdict, "completed": completed, "f2p_passed": f2p_passed, "p2p_regressed": p2p_regressed,
            "baseline_ok": baseline_ok, "tampered": tampered, "changed": diff_files,
            "check_inputs_changed": result.get("check_inputs_changed", []),
            **({"touched_fixtures": touched_fixtures, "worker_tests": worker_tests} if mode in HIDDEN_MODES else {}),
            "worker": {"status": result.get("status"), "agent": result.get("agent"), "route": result.get("route"),
                       "model": result.get("model"), "run_id": result.get("run_id")},
            "gate_label": result.get("gate_label"), "cost_usd": float(outcome.get("spent_usd") or 0),
            "duration_ms": wall_ms, "finished_at_ms": int(time.time() * 1000)}


def _read_answer(result):
    path = (result.get("artifacts") or {}).get("answer")
    try:
        return Path(path).read_text(encoding="utf-8") if path else ""
    except OSError:
        return ""


def summarize_localize(task, lane_name, lane, outcome, diff_files, wall_ms):
    """A localization row: the worker's answer (from answer.md, its whole
    final message) graded against the task's ground truth. A dispatched run
    is complete whether or not it answered; no answer is `invalid_answer`."""
    from fusion_gym_localize import ZERO, gradeable, grade, parse_answer
    node = (outcome.get("nodes") or [{}])[0]
    result = node.get("result") or {}
    status = outcome.get("status")
    files, symbols = gradeable(task["localization"])
    row = {"schema": RESULT_SCHEMA, "event": "finished", "key": result_key(task["id"], lane_name, "hidden", "localize"),
           "task": task["id"], "kind": "localize", "mode": "hidden", "lane": lane_name, "lane_spec": lane,
           "hinted": False, "workflow_id": outcome.get("workflow_id"), "status": status,
           "truth": {"files": files, "symbols": symbols}, "changed": diff_files,
           "worker": {"status": result.get("status"), "agent": result.get("agent"), "route": result.get("route"),
                      "model": result.get("model"), "run_id": result.get("run_id")},
           "gate_label": result.get("gate_label"), "cost_usd": float(outcome.get("spent_usd") or 0),
           "duration_ms": wall_ms, "finished_at_ms": int(time.time() * 1000)}
    if not result.get("run_id") or status in {"paused_quota", "paused_budget", "interrupted"}:
        return {**row, "verdict": "unavailable" if outcome.get("lanes") else status or "not_run", "completed": False}
    answer, error = parse_answer(_read_answer(result))
    if error:
        return {**row, "verdict": "invalid_answer", "completed": True, "answer_error": error, "scores": dict(ZERO)}
    scores = grade(answer, task["localization"])
    return {**row, "verdict": scores.pop("verdict"), "completed": True, "answer": answer, "scores": scores}


GRADE_LABELS = {"localized": {"failed_task": "false"}, "missed": {"failed_task": "true"},
                "correct": {"failed_task": "false"}, "misread": {"failed_task": "true"}}


def grade_label(gym, row):
    """The gym's grade as an acceptance label (source gym_grade) on the input
    the workflow recorded for the node's reported success. localized ->
    failed_task=false; missed (no changed file in the top 3) ->
    failed_task=true; partial and invalid answers stay unlabeled, as does a
    read-only run that changed files or any input someone already labeled."""
    decision = (row.get("gate_label") or {}).get("decision_id")
    answers = GRADE_LABELS.get(row.get("verdict"))
    if (row.get("worker") or {}).get("status") != "success":
        return {"status": "skipped", "reason": "the worker did not report success"}
    if not decision:
        return {"status": "skipped", "reason": "no acceptance input was recorded (automatic labels off?)"}
    if row.get("changed"):
        return {"status": "skipped", "decision_id": decision, "reason": "the read-only worker changed files"}
    if not answers:
        return {"status": "unlabeled", "decision_id": decision, "reason": f"verdict {row.get('verdict')} is not evidence"}
    from fusion_decisions import DecisionStore, read_jsonl
    store = DecisionStore(gym)
    with store.review_lock():
        if any(e.get("id") == decision and e.get("event") == "label" and e.get("verified") for e in read_jsonl(store.path)):
            return {"status": "preserved", "decision_id": decision, "reason": "an existing label on this input was kept"}
        scores = row.get("scores") or {}
        evidence = (f"Gym grade on {row['key']}: {row['verdict']}; scores {json.dumps(scores, sort_keys=True)}.")
        store.append("label", id=decision, answers=answers, verified=True, replace=False, source=GRADE_SOURCE,
                     evidence=evidence,
                     reviewers=[{"agent": "gym", "run_id": (row.get("worker") or {}).get("run_id"), "key": row["key"]}])
    return {"status": "labeled", "decision_id": decision, "answers": answers, "source": GRADE_SOURCE}


def run(tasks_path, lanes, gym, max_tasks=None, budget_usd=None, keep_worktrees=False, runner=None, mode="hidden",
        kind="fix"):
    """Sequential task x lane runs; resumable (completed pairs are skipped).
    Results are keyed by task, lane, mode and kind, so a visible run never
    stands in for a hidden one, nor a localization for a fix.

    kind "localize" runs the read-only localization task on B in mode
    hidden only: interface hints name the symbols the answer is graded on,
    and visible tests point at the files. Tasks whose fix changed no source
    file that exists in B cannot be localized from B and are skipped."""
    import fusion_core as core
    from fusion_gym_localize import gradeable
    from fusion_publish import snapshot
    from fusion_workflow import WorkflowRunner
    if mode not in MODES:
        raise ValueError(f"gym mode must be one of {', '.join(MODES)}")
    if kind not in KINDS:
        raise ValueError(f"gym kind must be one of {', '.join(KINDS)}")
    localize = kind == "localize"
    interpret = kind == "interpret"
    if interpret and mode != "hidden":
        raise ValueError("interpret runs are read-only evidence bundles (mode hidden)")
    if localize and mode != "hidden":
        raise ValueError("localize runs start from B with the problem text only (mode hidden): interface hints "
                         "name the symbols the answer is graded on, and visible tests point at the files")
    runner = runner or WorkflowRunner
    tasks = load_tasks(tasks_path)
    if any((t.get("kind") == "interpret") != interpret for t in tasks):
        raise ValueError("interpret tasks require --kind interpret; fix/localize require fix tasks")
    gym = Path(gym).resolve()
    for task in tasks:
        source = Path(task["repo_path"]).resolve()
        if gym == source or gym.is_relative_to(source):
            raise ValueError(f"the gym directory must be outside the source repository {source}")
    gym = prepare_gym(gym, tasks[0]["repo_path"])
    config, _ = core.load_config(gym)
    resolved = {name: resolve_lane(config, name) for name in lanes}
    with _locked(gym):
        done = {row["key"] for row in read_results(gym) if row.get("event") == "finished" and row.get("completed")}
        spent, processed, finished, skipped = 0.0, 0, [], []
        for task in tasks:
            # Without hints, hidden+hints sends the same prompt as hidden: reuse
            # those results instead of paying for identical runs again.
            task_mode = mode
            if mode == "hidden+hints":
                ensure_hidden(task)
                ensure_interface(task)
                if not task.get("interface"):
                    task_mode = "hidden"
            pending = [name for name in lanes if result_key(task["id"], name, task_mode, kind) not in done]
            if not pending:
                continue
            if localize and not gradeable(ensure_localization(task))[0]:
                skipped.append({"task": task["id"], "reason": "the fix changed no source file that exists in B"})
                continue
            if max_tasks is not None and processed >= max_tasks:
                break
            processed += 1
            if task_mode in HIDDEN_MODES and not interpret:
                ensure_hidden(task)
            if task_mode == "hidden+hints":
                ensure_interface(task)
            if not interpret:
                start_sha = task["hidden"]["base_sha"] if task_mode in HIDDEN_MODES else task["task_sha"]
                repo = task_repo(gym, task, task_mode)
            for name in pending:
                if budget_usd is not None and spent >= budget_usd:
                    return {"status": "budget_reached", "kind": kind, "spent_usd": spent, "runs": finished,
                            "skipped": skipped}
                lane = resolved[name]
                key = result_key(task["id"], name, task_mode, kind)
                tag = "it-" if interpret else "lz-" if localize else WORKFLOW_TAGS.get(task_mode, "")
                workflow_id = f"gym-{task['id']}-{tag}{_slug(name)}-{uuid.uuid4().hex[:8]}"
                if interpret:
                    from fusion_gym_interpret import run_lane
                    remaining = None if budget_usd is None else budget_usd - spent
                    row = run_lane(task, name, lane, gym, config, runner, workflow_id, remaining, keep_worktrees)
                    _append(gym, row)
                    finished.append(row)
                    spent += row["cost_usd"]
                    continue
                worktree = task_root(gym, task, task_mode) / lanes_dir(task_mode, kind) / _slug(name)
                _remove_worktree(repo, worktree)
                worktree.parent.mkdir(parents=True, exist_ok=True)
                git(repo, "worktree", "add", "-q", "--detach", str(worktree), start_sha)
                if task_mode not in HIDDEN_MODES:
                    # Hidden runs keep no link: the gym's .fusion holds the manifest
                    # (hidden test ids) and before-run output (test names), and
                    # workflow state already goes to the control workspace (#81).
                    (worktree / ".fusion").symlink_to(gym / ".fusion", target_is_directory=True)
                _append(gym, {"schema": RESULT_SCHEMA, "event": "started", "key": key, "task_mode": task_mode,
                              "kind": kind, "workflow_id": workflow_id, "started_at_ms": int(time.time() * 1000)})
                # Hidden test files live outside the gym only for this run.
                fixture_dir = (Path(tempfile.mkdtemp(prefix="fusion-gym-fixtures-"))
                               if task_mode in HIDDEN_MODES and not (localize or interpret) else None)
                started = time.monotonic()
                remaining = None if budget_usd is None else budget_usd - spent
                try:
                    if interpret:
                        spec = build_interpret_spec(task, lane, remaining)
                    elif localize:
                        spec = build_localize_spec(task, lane, remaining)
                    else:
                        fixtures = write_fixtures(task, fixture_dir) if fixture_dir else None
                        spec = build_spec(task, lane, remaining, task_mode, fixtures)
                    outcome = runner(gym, config, spec, run_id=workflow_id,
                                     worktree={"workspace": str(worktree), "base_sha": start_sha}).run()
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    outcome = {"workflow_id": workflow_id, "status": "error", "error": str(exc), "nodes": []}
                finally:
                    if fixture_dir:
                        shutil.rmtree(fixture_dir, ignore_errors=True)
                wall_ms = round((time.monotonic() - started) * 1000)
                diff_files, patch, snapshot_error = [], b"", None
                try:
                    tree = snapshot(worktree)
                    diff_files = [p for p in git(repo, "diff", "--name-only", "-z", start_sha, tree).split("\0") if p]
                    patch = git(repo, "diff", "--binary", start_sha, tree, binary=True)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    snapshot_error = str(exc)
                evidence = gym / "results" / task["id"] / mode_dir(task_mode, kind) / _slug(name)
                evidence.mkdir(parents=True, exist_ok=True)
                if localize or interpret:
                    summarizer = summarize_interpret if interpret else summarize_localize
                    row = summarizer(task, name, lane, outcome, diff_files, wall_ms)
                    if snapshot_error is not None:
                        row.update(verdict="invalid_snapshot", completed=False, snapshot_error=snapshot_error, scores={})
                    if row["completed"]:
                        row["grade_label"] = grade_label(gym, row)
                    answer = _read_answer(((outcome.get("nodes") or [{}])[0].get("result") or {}))
                    if answer:
                        (evidence / f"{workflow_id}.answer.md").write_text(answer, encoding="utf-8")
                        row["answer_file"] = str(evidence / f"{workflow_id}.answer.md")
                else:
                    row = summarize(task, name, lane, outcome, diff_files, wall_ms, task_mode)
                    if snapshot_error is not None:
                        row.update(verdict="invalid_snapshot", completed=False, snapshot_error=snapshot_error)
                    row["gate_label"] = withdraw_untrusted_label(gym, row)
                if outcome.get("error"):
                    row["error"] = outcome["error"]
                (evidence / f"{workflow_id}.patch").write_bytes(patch)
                row["patch"] = str(evidence / f"{workflow_id}.patch")
                _append(gym, row)
                finished.append(row)
                spent += row["cost_usd"]
                if not keep_worktrees:
                    _remove_worktree(repo, worktree)
        audit(gym)
        return {"status": "complete", "mode": mode, "kind": kind, "spent_usd": spent, "runs": finished,
                "skipped": skipped}


AUDIT_EVIDENCE = "gym audit: no lane has solved this task from its prompt"
LOCALIZE_AUDIT_EVIDENCE = "gym audit: no lane has localized this task from its prompt"
# Per kind: the verdict proving a task can be done from its prompt, the
# verdict a negative label comes from, that label's source, the row field
# holding it, and the retraction's evidence.
AUDIT_KINDS = {
    "interpret": ("correct", "misread", GRADE_SOURCE, "grade_label",
                  "gym audit: interpret", ""),
    "fix": ("solved", "unsolved", "structural_gate", "gate_label",
            AUDIT_EVIDENCE, "its hidden tests may expect what the prompt never states"),
    "localize": ("localized", "missed", GRADE_SOURCE, "grade_label",
                 LOCALIZE_AUDIT_EVIDENCE, "its prompt may not identify the code the fix changed"),
}


def _latest_hidden(gym):
    """The latest completed hidden-mode row per key, for the kinds the audit knows."""
    latest = {}
    for row in read_results(gym):
        if (row.get("event") == "finished" and row.get("mode") in HIDDEN_MODES and row.get("completed")
                and row.get("kind") in AUDIT_KINDS):
            latest[row["key"]] = row
    return latest


def _solved(latest):
    """{(kind, mode, task)} some lane solved (fix) or localized (localize)."""
    return {(row["kind"], row["mode"], row["task"]) for row in latest.values()
            if row.get("verdict") == AUDIT_KINDS[row["kind"]][0]
            and not row.get("tampered") and not row.get("check_inputs_changed")}


def audit(gym):
    """A negative gym label claims the worker failed a task that could be done.
    SWE-bench Verified drops tasks whose hidden tests expect something the
    issue never states; the gym's equivalent evidence is that some lane solved
    the task from the same prompt. Until one has, a task's failed_task=true
    gate labels are retracted (append-only) and the task is flagged; once a
    lane solves it, labels this audit retracted are restored. Positives and
    labels from any other source are never touched. Each hidden mode is its
    own evidence: a task solved with interface hints says nothing about
    whether its bare prompt was enough, and vice versa. Each kind is its own
    evidence too: a localization negative (gym_grade) counts only once some
    lane localized that task, whatever the fix runs did."""
    from fusion_decisions import DecisionStore, label_provenance, read_jsonl, reviewed_labels
    store = DecisionStore(gym)
    latest = _latest_hidden(gym)
    solved = _solved(latest)
    events = read_jsonl(store.path)
    answers, _ = reviewed_labels(events)
    sources = label_provenance(events)
    audited = {e["id"] for e in events if e.get("event") == "label"
               and str(e.get("evidence", "")).startswith((AUDIT_EVIDENCE, LOCALIZE_AUDIT_EVIDENCE))}
    retracted, restored = [], []
    for row in latest.values():
        if row["kind"] == "interpret":
            continue  # Curated factual gold is independent of whether any lane got it right.
        _, negative, label_source, field, evidence, caveat = AUDIT_KINDS[row["kind"]]
        decision = (row.get(field) or {}).get("decision_id") or (row.get(field) or {}).get("id")
        if not decision:
            continue
        current = answers.get(decision, {}).get("failed_task")
        source = (sources.get(decision, {}).get("failed_task") or {}).get("source")
        solvable = (row["kind"], row["mode"], row["task"]) in solved
        if not solvable and current == "true" and source == label_source:
            store.append("label", id=decision, answers={}, verified=True, replace=True, source=label_source,
                         evidence=f"{evidence} in mode {row['mode']} ({row['key']}); {caveat}.")
            retracted.append(row["key"])
        elif solvable and decision in audited and current is None and row.get("verdict") == negative:
            done = "localized" if row["kind"] == "localize" else "solved"
            store.append("label", id=decision, answers={"failed_task": "true"}, verified=True, replace=False,
                         source=label_source, evidence=f"Restored: a lane {done} {row['task']} from the same prompt ({row['mode']}).")
            restored.append(row["key"])
    modes = {}
    for mode in HIDDEN_MODES:
        attempted = {row["task"] for row in latest.values() if row["kind"] == "fix" and row["mode"] == mode}
        if attempted:
            done = {task for kind, which, task in solved if kind == "fix" and which == mode}
            modes[mode] = {"solved_tasks": sorted(done), "unsolved_tasks": sorted(attempted - done)}
    summary = {"modes": modes, "retracted": retracted, "restored": restored}
    attempted = {row["task"] for row in latest.values() if row["kind"] == "localize"}
    if attempted:
        done = {task for kind, _, task in solved if kind == "localize"}
        summary["localize"] = {"localized_tasks": sorted(done), "unlocalized_tasks": sorted(attempted - done)}
    (Path(gym) / "audit.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


# ---------------------------------------------------------------- lane priors

PRIORS_SCHEMA = "fusion.lane_priors.v1"
# Work class per kind: a fix writes, a localization only reads.
WORK_CLASSES = {"fix": "write", "localize": "read", "interpret": "interpret"}
# Per kind: verdicts that count as a success and as a failure. Everything
# else is excluded: invalid_baseline measured the machine, tampered graded
# itself, and a partial localization is neither (grade_label leaves it
# unlabeled too). invalid_answer is a failure here, unlike for labels: a lane
# that returns no usable answer failed the read-only task it was routed.
PRIOR_VERDICTS = {"interpret": ({"correct"}, {"misread", "invalid_answer", "abstained"}),
                 "fix": ({"solved"}, {"unsolved", "regressed"}),
                  "localize": ({"localized"}, {"missed", "invalid_answer"})}


def default_priors_path():
    """Beside the global fusion.json, under ORC_HOME."""
    return Path(os.environ.get("ORC_HOME") or Path.home() / ".config/orc") / "lane_priors.json"


def lane_tuple(spec):
    """(agent, route, model, reasoning_effort) with empty values as None."""
    spec = spec or {}
    return tuple(spec.get(name) or None for name in ("agent", "route", "model", "reasoning_effort"))


def lane_prior_key(spec):
    """The routing key a candidate for this lane would carry: the route (with
    `:model` for an arm), else the agent, then any pinned model and effort.
    Informational only: routing matches on the tuple, not the name."""
    agent, route, model, effort = lane_tuple(spec)
    return ":".join(part for part in (route or agent, model, effort) if part)


def lane_priors(gym, now=None):
    """Per lane and work class, from completed hidden-mode rows (latest per
    key): attempts, successes, mean cost and wall seconds. Rows on a task no
    lane solved (fix, per hidden mode) or localized are excluded, as the audit
    withholds their negatives: an underspecified task says nothing about a
    lane. Both hidden modes count toward `write`, each audited on its own."""
    latest = _latest_hidden(gym)
    solved = _solved(latest)
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time()))
    priors, excluded, unsolvable = {}, {}, {}
    for row in sorted(latest.values(), key=lambda r: r["key"]):
        kind, verdict = row["kind"], row.get("verdict")
        wins, losses = PRIOR_VERDICTS[kind]
        scope = row["mode"] if kind == "fix" else "localize"
        if kind != "interpret" and (kind, row["mode"], row["task"]) not in solved:
            unsolvable.setdefault(scope, set()).add(row["task"])
            excluded["unsolved_by_all"] = excluded.get("unsolved_by_all", 0) + 1
            continue
        if verdict not in wins | losses or row.get("tampered") or row.get("check_inputs_changed"):
            excluded[verdict or "none"] = excluded.get(verdict or "none", 0) + 1
            continue
        if kind == "interpret" and (row.get("scores") or {}).get("train", {}).get("total") == 0:
            excluded["holdout_only"] = excluded.get("holdout_only", 0) + 1
            continue
        spec = row.get("lane_spec") or {}
        if not spec.get("agent"):
            excluded["no_lane_spec"] = excluded.get("no_lane_spec", 0) + 1
            continue
        agent, route, model, effort = lane_tuple(spec)
        entry = priors.setdefault(lane_prior_key(spec), {"agent": agent, "route": route, "model": model,
                                                          "reasoning_effort": effort, "gym_lanes": []})
        if row["lane"] not in entry["gym_lanes"]:
            entry["gym_lanes"].append(row["lane"])
        stats = entry.setdefault(WORK_CLASSES[kind], {"attempts": 0, "successes": 0, "_cost": 0.0, "_ms": 0})
        attempts, successes = 1, int(verdict in wins)
        if kind == "interpret":
            # Holdout scores are evaluation-only. Training questions supply
            # fractional pseudo-counts; uncertainty is half a rejection.
            train = (row.get("scores") or {}).get("train")
            if train is not None:
                attempts = train["total"] - .5 * train["abstained"]
                successes = train["correct"]
            else:
                attempts = .5 if verdict == "abstained" else 1
        stats["attempts"] += attempts
        stats["successes"] += successes
        stats["_cost"] += float(row.get("cost_usd") or 0) * attempts
        stats["_ms"] += int(row.get("duration_ms") or 0) * attempts
    for entry in priors.values():
        entry["gym_lanes"].sort()
        for work in WORK_CLASSES.values():
            stats = entry.get(work)
            if stats:
                cost, ms = stats.pop("_cost"), stats.pop("_ms")
                stats.update(mean_cost_usd=round(cost / stats["attempts"], 4),
                             mean_seconds=round(ms / stats["attempts"] / 1000, 1),
                             source="gym", generated_at=generated_at)
    return {"schema": PRIORS_SCHEMA, "source": "gym", "gym": str(Path(gym).resolve()), "generated_at": generated_at,
            "counted": {"write": "completed hidden and hidden+hints fix runs: solved vs unsolved/regressed",
                        "read": "completed localize runs: localized vs missed/invalid_answer",
                        "interpret": "train questions: correct accepted, misread/invalid rejected, abstain half rejection; holdout excluded"},
            "excluded": dict(sorted(excluded.items())),
            "unsolved_by_all": {scope: sorted(tasks) for scope, tasks in sorted(unsolvable.items())},
            "priors": dict(sorted(priors.items()))}


def write_priors(value, path):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)
    return path


def priors_table(value):
    lines = [f"{'key':<60} {'class':<5} {'n':>3} {'ok':>3} {'rate':>5} {'cost$':>7} {'mean s':>7}"]
    for key, entry in value["priors"].items():
        for work in WORK_CLASSES.values():
            stats = entry.get(work)
            if stats:
                lines.append(f"{key:<60} {work:<5} {stats['attempts']:>3} {stats['successes']:>3} "
                             f"{stats['successes'] / stats['attempts']:>5.2f} {stats['mean_cost_usd']:>7.4f} "
                             f"{stats['mean_seconds']:>7.1f}")
    lines.append("excluded: " + (", ".join(f"{k} {v}" for k, v in value["excluded"].items()) or "none"))
    lines += [f"unsolved by all ({scope}): {', '.join(tasks)}" for scope, tasks in value["unsolved_by_all"].items()]
    return "\n".join(lines)


# ---------------------------------------------------------------- report

def _localize_section(rows):
    """Per lane: verdict counts, file acc@1, mean file recall@3 and
    precision, mean symbol recall, cost and time. Invalid answers score 0;
    symbol recall averages only tasks whose fix changed a symbol in B."""
    section = {"lanes": {}, "tasks": {}}
    for row in rows:
        lane = section["lanes"].setdefault(row["lane"], {
            "attempted": 0, "localized": 0, "partial": 0, "missed": 0, "invalid_answer": 0, "wrote_files": 0,
            "file_acc_at_1": 0, "file_recall_at_3": 0.0, "file_precision": 0.0, "symbol_recall": 0.0,
            "symbol_tasks": 0, "cost_usd": 0.0, "duration_ms": 0, "grade_labels": {}})
        scores = row.get("scores") or {}
        lane["attempted"] += 1
        lane[row["verdict"]] = lane.get(row["verdict"], 0) + 1
        lane["wrote_files"] += bool(row.get("changed"))
        lane["file_acc_at_1"] += bool(scores.get("file_acc_at_1"))
        lane["file_recall_at_3"] += scores.get("file_recall_at_3") or 0
        lane["file_precision"] += scores.get("file_precision") or 0
        if (row.get("truth") or {}).get("symbols"):
            lane["symbol_tasks"] += 1
            lane["symbol_recall"] += scores.get("symbol_recall") or 0
        lane["cost_usd"] += row.get("cost_usd") or 0
        lane["duration_ms"] += row.get("duration_ms") or 0
        answer = json.dumps(((row.get("grade_label") or {}).get("answers")) or None)
        lane["grade_labels"][answer] = lane["grade_labels"].get(answer, 0) + 1
        task = section["tasks"].setdefault(row["task"], {"localized_by": [], "attempted_by": []})
        task["attempted_by"].append(row["lane"])
        if row["verdict"] == "localized":
            task["localized_by"].append(row["lane"])
    for lane in section["lanes"].values():
        n, m = lane["attempted"], lane.pop("symbol_tasks")
        lane["file_acc_at_1"] = round(lane["file_acc_at_1"] / n, 3)
        lane["file_recall_at_3"] = round(lane["file_recall_at_3"] / n, 3)
        lane["file_precision"] = round(lane["file_precision"] / n, 3)
        lane["symbol_recall"] = round(lane["symbol_recall"] / m, 3) if m else None
        lane["mean_duration_s"] = round(lane["duration_ms"] / n / 1000, 1)
        lane["cost_usd"] = round(lane["cost_usd"], 4)
    for task in section["tasks"].values():
        task["localized_by"].sort()
        task["attempted_by"].sort()
    section["lanes"] = dict(sorted(section["lanes"].items()))
    section["tasks"] = dict(sorted(section["tasks"].items()))
    return section


def report(gym):
    """Per kind, then per mode (hidden, visible): per-lane rates and per-task
    solvers. `modes` is the fix kind; `localize` the localization kind. The
    kinds and modes measure different things and are never pooled."""
    latest = {}
    for row in read_results(gym):
        if row.get("event") == "finished":
            latest[row["key"]] = row
    modes, localized, interpreted = {}, [], []
    for row in latest.values():
        if not row.get("completed"):
            continue
        if row["kind"] == "interpret":
            interpreted.append(row)
            continue
        if row["kind"] == "localize":
            localized.append(row)
            continue
        if row["kind"] != "fix":
            continue
        section = modes.setdefault(row["mode"], {"lanes": {}, "tasks": {}})
        lane = section["lanes"].setdefault(row["lane"], {
            "attempted": 0, "solved": 0, "f2p_passed": 0, "p2p_regressions": 0, "tampered": 0, "touched_fixtures": 0,
            "invalid_baseline": 0, "cost_usd": 0.0, "duration_ms": 0, "gate_labels": {}})
        valid = row["verdict"] != "invalid_baseline"
        lane["attempted"] += valid
        lane["invalid_baseline"] += not valid
        lane["solved"] += row["verdict"] == "solved"
        lane["f2p_passed"] += bool(valid and row["f2p_passed"] and not row["tampered"])
        lane["p2p_regressions"] += bool(valid and row["p2p_regressed"])
        lane["tampered"] += bool(row["tampered"])
        lane["touched_fixtures"] += bool(row.get("touched_fixtures"))
        lane["cost_usd"] += row.get("cost_usd") or 0
        lane["duration_ms"] += row.get("duration_ms") or 0
        answer = json.dumps(((row.get("gate_label") or {}).get("answers")) or None)
        lane["gate_labels"][answer] = lane["gate_labels"].get(answer, 0) + 1
        task = section["tasks"].setdefault(row["task"], {"solved_by": [], "attempted_by": []})
        task["attempted_by"].append(row["lane"])
        if row["verdict"] == "solved":
            task["solved_by"].append(row["lane"])
    for section in modes.values():
        for lane in section["lanes"].values():
            n = lane["attempted"]
            lane["f2p_pass_rate"] = round(lane["f2p_passed"] / n, 3) if n else None
            lane["mean_duration_s"] = round(lane["duration_ms"] / n / 1000, 1) if n else None
            lane["cost_usd"] = round(lane["cost_usd"], 4)
        for task in section["tasks"].values():
            task["solved_by"].sort()
            task["attempted_by"].sort()
        section["lanes"] = dict(sorted(section["lanes"].items()))
        section["tasks"] = dict(sorted(section["tasks"].items()))
    pending = sorted(key for key, row in latest.items() if not row.get("completed"))
    value = {"gym": str(Path(gym).resolve()), "modes": {mode: modes[mode] for mode in MODES if mode in modes}}
    if localized:
        value["localize"] = _localize_section(localized)
    if interpreted:
        from fusion_gym_interpret import report_section
        value["interpret"] = report_section(interpreted)
    return {**value, "incomplete": pending}


MODE_TITLES = {"hidden": "hidden tests (worker sees only the problem text)",
               "hidden+hints": "hidden tests + interface hints (problem text plus the names and signatures the tests call)",
               "visible": "visible tests (the fix's tests are in the worker's tree)"}
LOCALIZE_TITLE = "localize (read-only: name the files and symbols the fix changes, graded against the fix)"


def _percent(value):
    return "-" if value is None else f"{value * 100:.0f}"


def table(value):
    lines = []
    for mode, section in value["modes"].items():
        lines += [f"== {MODE_TITLES[mode]}",
                  f"{'lane':<22} {'tasks':>5} {'solved':>6} {'f2p%':>6} {'p2p-reg':>7} {'tamper':>6} {'cost$':>8} {'mean s':>7}"]
        for name, lane in section["lanes"].items():
            rate = "-" if lane["f2p_pass_rate"] is None else f"{lane['f2p_pass_rate'] * 100:.0f}"
            lines.append(f"{name:<22} {lane['attempted']:>5} {lane['solved']:>6} {rate:>6} {lane['p2p_regressions']:>7} "
                         f"{lane['tampered']:>6} {lane['cost_usd']:>8.2f} {lane['mean_duration_s'] or 0:>7.1f}")
        lines.append("")
        for task_id, task in section["tasks"].items():
            lines.append(f"{task_id:<12} solved by: {', '.join(task['solved_by']) or 'none'}"
                         f"  (of {', '.join(task['attempted_by'])})")
        lines.append("")
    section = value.get("localize")
    if section:
        lines += [f"== {LOCALIZE_TITLE}",
                  f"{'lane':<22} {'tasks':>5} {'local':>5} {'part':>4} {'miss':>4} {'inval':>5} {'acc@1%':>6} "
                  f"{'rec@3%':>6} {'prec%':>5} {'sym%':>5} {'cost$':>8} {'mean s':>7}"]
        for name, lane in section["lanes"].items():
            lines.append(f"{name:<22} {lane['attempted']:>5} {lane['localized']:>5} {lane['partial']:>4} {lane['missed']:>4} "
                         f"{lane['invalid_answer']:>5} {_percent(lane['file_acc_at_1']):>6} "
                         f"{_percent(lane['file_recall_at_3']):>6} {_percent(lane['file_precision']):>5} "
                         f"{_percent(lane['symbol_recall']):>5} {lane['cost_usd']:>8.2f} {lane['mean_duration_s']:>7.1f}")
        lines.append("")
        for task_id, task in section["tasks"].items():
            lines.append(f"{task_id:<12} localized by: {', '.join(task['localized_by']) or 'none'}"
                         f"  (of {', '.join(task['attempted_by'])})")
        lines.append("")
    section = value.get("interpret")
    if section:
        lines += ["== interpret (read-only evidence questions; correct=1, abstain=0.5, misread=0)",
                  f"{'lane':<22} {'correct':>7} {'abstain':>7} {'misread':>7} {'invalid':>7} {'score%':>7} {'trap err%':>9} {'holdout%':>8} {'hold err%':>9}"]
        for name, lane in section["lanes"].items():
            lines.append(f"{name:<22} {lane['correct']:>7} {lane['abstained']:>7} {lane['misread']:>7} "
                         f"{lane['invalid_answer']:>7} {_percent(lane['score']):>7} {_percent(lane['trap_misread_rate']):>9} "
                         f"{_percent(lane['holdout']['score']):>8} {_percent(lane['holdout']['trap_misread_rate']):>9}")
        lines.append("")
    if value["incomplete"]:
        lines.append("incomplete (rerun `gym run` to retry): " + ", ".join(value["incomplete"]))
    return "\n".join(lines).rstrip() or "no completed gym runs"


def extract_table(summary):
    lines = [f"{'task':<10} {'F2P':>4} {'P2P':>5} prompt"]
    lines += [f"{t['id']:<10} {t['fail_to_pass']:>4} {t['pass_to_pass']:>5} {t['prompt_source']}" for t in summary["tasks"]]
    lines += [f"PR #{s['pr']:<6} skipped: {s['reason']}" for s in summary["skipped"]]
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def add_parser(sub):
    parser = sub.add_parser("gym", help="replay merged fix PRs as benchmark tasks across lanes")
    commands = parser.add_subparsers(dest="gym_command", required=True)
    extract_cmd = commands.add_parser("extract", help="turn squash-merged fix PRs into tasks with FAIL_TO_PASS tests")
    extract_cmd.add_argument("--repo-path", default=".", help="source repository (default: current directory)")
    extract_cmd.add_argument("--prs", type=int, nargs="+")
    extract_cmd.add_argument("--kind", choices=("fix", "interpret"), default="fix")
    seed_cmd = commands.add_parser("interpret-seed", help="build read-only evidence tasks from existing gym fix tasks")
    seed_cmd.add_argument("--out", required=True, help="directory for generated interpret tasks")
    for cmd in (extract_cmd, seed_cmd):
        cmd.add_argument("--tasks", help="existing fix task JSON file or directory (required for interpret)")
        cmd.add_argument("--seed", type=int, default=0, help="deterministic scenario seed")
        cmd.add_argument("--period", help="holdout rotation seed (default: current ISO week)")
        cmd.add_argument("--split", choices=("all", "train", "holdout"), default="all")
        cmd.add_argument("--trap-templates", help="trap template JSON (default: gym/interpret_traps.json)")
    extract_cmd.add_argument("--out", help="directory for task JSON files and index.json")
    extract_cmd.add_argument("--ref", default="HEAD", help="history to search for the PR commits (default HEAD)")
    extract_cmd.add_argument("--github-repo", help="OWNER/REPO for gh (default: inferred from the repository)")
    extract_cmd.add_argument("--no-gh", action="store_true", help="prompt from the commit subject; no GitHub reads")
    extract_cmd.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds per test run")
    extract_cmd.add_argument("--p2p-limit", type=int, default=DEFAULT_P2P_LIMIT)
    run_cmd = commands.add_parser("run", help="run each task on each lane in its own worktree (resumable)")
    run_cmd.add_argument("tasks", help="task JSON file or directory from `gym extract`")
    run_cmd.add_argument("--lanes", nargs="+", required=True)
    run_cmd.add_argument("--workspace", dest="gym_workspace", required=True, metavar="GYMDIR",
                         help="gym directory: its own Fusion workspace, outside the source repository")
    run_cmd.add_argument("--max-tasks", type=int, help="at most N tasks with pending lanes in this invocation")
    run_cmd.add_argument("--budget-usd", type=float, help="stop starting runs once this invocation spent this much")
    run_cmd.add_argument("--keep-worktrees", action="store_true", help="keep each lane's worktree after its run")
    run_cmd.add_argument("--visible-tests", action="store_true",
                         help="old mode: the fix's tests are in the worker's tree (default: hidden, graded by fixtures)")
    run_cmd.add_argument("--no-interface-hints", action="store_true",
                         help="hidden mode without the interface section (names and signatures the tests call); "
                              "results are keyed as mode hidden, hinted runs as hidden+hints")
    run_cmd.add_argument("--kind", choices=KINDS, default="fix",
                         help="fix (default): implement the fix, graded by the hidden tests; localize: read-only, "
                              "name the files and symbols the fix changes (no hints); interpret: read-only evidence questions")
    report_cmd = commands.add_parser("report", help="per-lane and per-task results of a gym directory")
    report_cmd.add_argument("gym_dir")
    audit_cmd = commands.add_parser("audit", help="retract negatives from tasks no lane has solved; restore them once one does")
    audit_cmd.add_argument("gym_dir")
    priors_cmd = commands.add_parser("priors", help="export per-lane, per-work-class success priors for routing")
    priors_cmd.add_argument("gym_dir")
    priors_cmd.add_argument("--out", help="where to write them (default: lane_priors.json under ORC_HOME, "
                                          "~/.config/orc; `-` prints only)")


def command(args, workspace, as_json=False, out=None):
    out = out or sys.stdout
    if args.gym_command == "interpret-seed" or (args.gym_command == "extract" and args.kind == "interpret"):
        from fusion_gym_interpret import extract as extract_interpret
        if not args.tasks or not args.out:
            raise ValueError("interpret extraction requires --tasks and --out")
        summary, _ = extract_interpret(args.tasks, args.out, args.seed, args.period, args.trap_templates, args.split)
        print(json.dumps(summary, indent=2), file=out)
        return 0
    if args.gym_command == "extract":
        if not args.prs:
            raise ValueError("fix extraction requires --prs")
        repo = Path(args.repo_path if Path(args.repo_path).is_absolute() else Path(workspace) / args.repo_path)
        summary, _ = extract(repo, args.prs, args.out, args.ref, args.github_repo, not args.no_gh, args.timeout, args.p2p_limit)
        print(json.dumps(summary, indent=2) if as_json else extract_table(summary), file=out)
        return 0 if summary["tasks"] else 1
    if args.gym_command == "run":
        if args.max_tasks is not None and args.max_tasks < 1:
            raise ValueError("--max-tasks must be at least 1")
        localize = args.kind in {"localize", "interpret"}
        if localize and args.visible_tests:
            raise ValueError(f"--kind {args.kind} requires hidden read-only mode; drop --visible-tests")
        mode = ("visible" if args.visible_tests else "hidden" if args.no_interface_hints or localize
                else "hidden+hints")
        result = run(args.tasks, args.lanes, args.gym_workspace, args.max_tasks, args.budget_usd, args.keep_worktrees,
                     mode=mode, kind=args.kind)
        print(json.dumps(result, indent=2) if as_json else
              f"{result['status']}: {len(result['runs'])} runs, ${result['spent_usd']:.2f}\n" + table(report(args.gym_workspace)),
              file=out)
        return 0
    if args.gym_command == "priors":
        value = lane_priors(args.gym_dir)
        path = None if args.out == "-" else write_priors(value, args.out or default_priors_path())
        print(json.dumps(value, indent=2) if as_json else
              priors_table(value) + (f"\nwrote {path}" if path else ""), file=out)
        return 0
    if args.gym_command == "audit":
        value = audit(args.gym_dir)
        print(json.dumps(value, indent=2), file=out)
        return 0
    value = report(args.gym_dir)
    print(json.dumps(value, indent=2) if as_json else table(value), file=out)
    return 0
