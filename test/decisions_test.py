import contextlib
import argparse
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_decisions
from fusion_build import prepare
from fusion_decisions import (DecisionEngine, DecisionStore, ACCEPTANCE_QUESTIONS, INTAKE_QUESTIONS, RECOVERY_QUESTIONS,
                              REVIEW_QUESTIONS, LayaRuntime, DEFAULTS, digest, fit_calibration, read_jsonl,
                              temperature_scale)
from fusion_laya import dataset_rows
from fusion_policy import accept_node, rank_by_outcomes, route_task, route_candidates, recovery, review_task
from fusion_report import format_report, select_report
from fusion_workflow import WorkflowRunner, expand_spec, resume_workflow, validate_spec, workflow_report


class Backend:
    def __init__(self, choices=None, malformed=False):
        self.choices = choices or {}
        self.malformed = malformed
        self.calls = 0

    def predict(self, state, questions):
        self.calls += 1
        answers = {}
        for key, q in questions.items():
            if q["type"] == "noul":
                answers[key] = {"noul": .99 if self.choices.get(key) == "true" else .01}
            else:
                selected = self.choices.get(key, next(iter(q["criteria"])))
                n = len(q["criteria"])
                answers[key] = {"probabilities": {label: (1 if n == 1 else .99) if label == selected else .01 / (n - 1) for label in q["criteria"]}, "confidence": 0}
        if self.malformed:
            answers = {"workflow": {"probabilities": {"invented": float("nan")}}}
        return {"answers": answers, "model_identity": "fixture-model"}


class DecisionsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        env = patch.dict(os.environ, {"ORC_HOME": str(self.workspace / "orc-home")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FUSION_DECISIONS_MODE", None)
        os.environ.pop("FUSION_READ_ONLY", None)
        self.config = {"decisions": {"mode": "shadow"}, "routes": {},
                       "codex": {"command": sys.executable}, "claude": {"command": sys.executable},
                       "agy": {"command": "missing-fusion-test-agent"}, "grok": {"command": "missing-fusion-test-agent"}}

    def engine(self, choices=None, mode="shadow"):
        config = {**self.config, "decisions": {"mode": mode, "auto_actions": ["intake", "routing", "recovery", "review", "acceptance"], "calibration_file": "calibration.json"}}
        return DecisionEngine(self.workspace, config, Backend(choices))

    def qualify(self, engine, kind, questions, identity="fixture-model"):
        report = {"schema": "fusion.calibration.v1", "model_identity": identity, "buckets": {
            f"{kind}:{digest(questions)}:{key}": {"qualified": True, "temperature": 1, "threshold": .9}
            for key in questions}}
        (self.workspace / "calibration.json").write_text(json.dumps(report))
        return report

    def task(self, agent="auto", write=False):
        return core.make_task(self.workspace, agent, "Implement CSV export", "implementation", [], [], None, False, write)

    def test_shadow_records_probability_without_acting(self):
        engine = self.engine({"workflow": "build"})
        self.qualify(engine, "intake", INTAKE_QUESTIONS)
        result = engine.decide("intake", "Build CSV export", INTAKE_QUESTIONS)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["recommendations"]["workflow"]["probability"], .99)
        self.assertFalse(engine.allowed(result, "workflow"))
        self.assertEqual(engine.store.get(result["id"])["state"], '"Build CSV export"')
        self.assertEqual(engine.store.path.stat().st_mode & 0o777, 0o600)

    def test_active_requires_matching_qualified_calibration_and_complete_state(self):
        engine = self.engine(mode="active")
        record = engine.decide("intake", "plan only", INTAKE_QUESTIONS)
        self.assertFalse(engine.allowed(record, "workflow"))
        self.qualify(engine, "intake", INTAKE_QUESTIONS, "stale-weights")
        self.assertFalse(engine.allowed(record, "workflow"))
        self.qualify(engine, "intake", INTAKE_QUESTIONS)
        self.assertTrue(engine.allowed(record, "workflow"))
        record["truncated"] = True
        self.assertFalse(engine.allowed(record, "workflow"))
        record = engine.decide("intake", "x" * 7000, INTAKE_QUESTIONS)
        self.assertFalse(engine.allowed(record, "workflow"))

    def test_recalibration_between_decide_and_allowed_is_not_ignored(self):
        # The weekly loop rewrites calibration between runs, so the record a
        # long-lived process holds was scaled by the previous temperature.
        # Gating on that stored number applies actions at a confidence the
        # current calibration would reject.
        engine = self.engine({"workflow": "build"}, "active")

        def calibrate(temperature):
            report = {"schema": "fusion.calibration.v1", "model_identity": "fixture-model", "buckets": {
                f"intake:{digest(INTAKE_QUESTIONS)}:{key}": {"qualified": True, "temperature": temperature, "threshold": .9}
                for key in INTAKE_QUESTIONS}}
            (self.workspace / "calibration.json").write_text(json.dumps(report))

        calibrate(.25)
        record = engine.decide("intake", "Build CSV export", INTAKE_QUESTIONS)
        self.assertTrue(engine.allowed(record, "workflow"))
        stored = record["recommendations"]["workflow"]["probability"]

        calibrate(4)
        current = max(temperature_scale(record["prediction"]["workflow"], 4).values())
        self.assertLess(current, .9, "the fixture must actually fall below the threshold")
        self.assertGreater(stored, current, "the stored probability must be the stale, sharper one")
        self.assertFalse(engine.allowed(record, "workflow"), "gated on a probability the current calibration rejects")

        # Recalibrating the other way admits it again, so this is a live read
        # rather than a one-way refusal.
        calibrate(.25)
        self.assertTrue(engine.allowed(record, "workflow"))

    def test_malformed_calibration_abstains_without_breaking_the_run(self):
        engine = self.engine(mode="active")
        report = self.qualify(engine, "intake", INTAKE_QUESTIONS)
        next(iter(report["buckets"].values()))["temperature"] = 0
        (self.workspace / "calibration.json").write_text(json.dumps(report))
        record = engine.decide("intake", "Build exports", INTAKE_QUESTIONS)
        self.assertEqual(record["status"], "ok")
        self.assertFalse(engine.allowed(record, "workflow"))

    def test_off_never_loads_runtime_or_creates_log(self):
        engine = self.engine(mode="off")
        self.assertEqual(engine.decide("intake", "build", INTAKE_QUESTIONS)["status"], "off")
        self.assertEqual(engine.backend.calls, 0)
        self.assertFalse(engine.store.path.exists())

    def test_candidate_cli_resolves_configured_model_against_workspace(self):
        from fusion_decision_cli import run
        self.config["decisions"]["model_path"] = ".fusion/candidate"
        args = argparse.Namespace(decision_command="evaluate", dataset="data.jsonl", output="predictions.jsonl", model_path="", device="cpu")
        with patch("fusion_decision_cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as execute:
            self.assertEqual(run(args, self.workspace, self.config), 0)
        argv = execute.call_args.args[0]
        self.assertEqual(argv[argv.index("--model-path") + 1], str((self.workspace / ".fusion/candidate").resolve()))

    def test_bad_output_and_unavailable_model_abstain(self):
        engine = self.engine(mode="active")
        engine.backend = Backend(malformed=True)
        self.assertEqual(engine.decide("intake", "build", INTAKE_QUESTIONS)["status"], "unavailable")
        with patch.object(engine.backend, "predict", side_effect=RuntimeError("model not cached")):
            self.assertEqual(engine.decide("intake", "build", INTAKE_QUESTIONS)["status"], "unavailable")

    def test_runtime_timeout_reaps_process_and_does_not_loop(self):
        helper = self.workspace / "runtime"
        helper.write_text("#!/bin/sh\nexec sleep 30\n")
        helper.chmod(0o755)
        runtime = LayaRuntime({**DEFAULTS, "python": str(helper), "timeout_seconds": .05})
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            runtime.predict("x", INTAKE_QUESTIONS)
        self.assertIsNone(runtime.process)
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            runtime.predict("x", INTAKE_QUESTIONS)

    def test_sweep_fans_out_one_worker_per_dimension(self):
        self.config["decisions"]["mode"] = "off"
        prepared = prepare(self.workspace, self.config, "Audit this service", across=["auth, payments", "data"])
        self.assertEqual((prepared["kind"], prepared["read_only"]), ("sweep", True))
        self.assertEqual(prepared["across"], ["auth", "payments", "data"])
        spec = json.loads(Path(prepared["workflow"]).read_text())
        sweep = next(node for node in spec["nodes"] if node["id"] == "sweep")
        self.assertEqual(sweep["items"], ["auth", "payments", "data"])
        self.assertIn("{item}", sweep["task_template"])
        self.assertEqual(spec["max_parallel"], 3, "the fan-out must actually run concurrently")
        self.assertEqual(spec["max_parallel_writers"], 0, "a sweep never writes")
        # A mapped node expands to sweep-01..N, so requiring "sweep" by name
        # would fail acceptance as an unknown node.
        self.assertEqual(spec["acceptance"]["required_nodes"], ["explore", "synthesize"])
        graph = expand_spec(spec)[0]["nodes"]
        self.assertEqual([n["id"] for n in graph if n["base_id"] == "sweep"], ["sweep-01", "sweep-02", "sweep-03"])
        synthesize = next(n for n in graph if n["id"] == "synthesize")
        self.assertEqual(synthesize["needs"], ["sweep-01", "sweep-02", "sweep-03"])
        validate_spec(spec)

    def test_sweep_rejects_contradictory_or_empty_scope(self):
        self.config["decisions"]["mode"] = "off"
        with self.assertRaisesRegex(ValueError, "at least one --across"):
            prepare(self.workspace, self.config, "Audit", kind="sweep")
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            prepare(self.workspace, self.config, "Audit", kind="build", across=["auth"])
        with self.assertRaisesRegex(ValueError, "at most"):
            prepare(self.workspace, self.config, "Audit", across=[str(n) for n in range(20)])

    def test_sweep_scope_survives_an_active_classifier(self):
        # Naming the dimensions is explicit scope; a confident "build" must not
        # turn a read-only sweep into one that writes.
        engine = self.engine({"workflow": "build"}, "active")
        self.qualify(engine, "intake", INTAKE_QUESTIONS)
        with patch("fusion_build.DecisionEngine", return_value=engine):
            prepared = prepare(self.workspace, self.config, "Audit this service", across=["auth"])
        self.assertEqual((prepared["kind"], prepared["read_only"]), ("sweep", True))
        spec = json.loads(Path(prepared["workflow"]).read_text())
        self.assertFalse(any(node.get("write") for node in spec["nodes"]))

    def test_intake_preserves_planning_scope_from_full_issue(self):
        issue = {"title": "New payments feature", "body": "Background. " * 1000 + "\nDiscovery/planning only. Do not implement.", "url": "https://github.com/org/repo/issues/1"}
        engine = self.engine({"workflow": "build"}, "active")
        self.qualify(engine, "intake", INTAKE_QUESTIONS)
        with patch("fusion_build.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(issue), "")), patch("fusion_build.DecisionEngine", return_value=engine):
            prepared = prepare(self.workspace, self.config, issue["url"], kind="build")
        self.assertEqual(prepared["kind"], "discovery")
        spec = json.loads(Path(prepared["workflow"]).read_text())
        self.assertFalse(any(node["write"] for node in spec["nodes"]))
        self.assertIn(issue["body"], Path(prepared["brief"]).read_text())

    def test_build_template_has_single_writer_and_required_independent_review(self):
        self.config["decisions"]["mode"] = "off"
        prepared = prepare(self.workspace, self.config, "Build CSV export")
        spec = json.loads(Path(prepared["workflow"]).read_text())
        self.assertEqual(sum(n["write"] for n in spec["nodes"]), 1)
        self.assertEqual(spec["nodes"][-1]["independent_of"], "implement")
        self.assertIn("review", spec["acceptance"]["required_nodes"])

    def test_plan_command_does_not_dispatch_coding_agent(self):
        (self.workspace / ".fusion.json").write_text(json.dumps({"decisions": {"mode": "off"}}))
        output = io.StringIO()
        with patch.object(core, "launch_lead") as launch, contextlib.redirect_stdout(output):
            self.assertEqual(core.main(["--workspace", str(self.workspace), "build", "--plan-only", "Add exports"]), 0)
        launch.assert_not_called()
        self.assertTrue(Path(json.loads(output.getvalue())["workflow"]).exists())

    def test_read_only_build_lead_and_mcp_enforce_scope(self):
        with patch("fusion_core.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            core.launch_lead(self.workspace, self.config, "codex", "plan", read_only=True)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("-s") + 1], "read-only")
        self.assertEqual(args[args.index("-a") + 1], "never")
        self.assertEqual(run.call_args.kwargs["env"]["FUSION_READ_ONLY"], "1")
        with patch.dict(os.environ, {"FUSION_READ_ONLY": "1"}):
            with self.assertRaisesRegex(ValueError, "read-only"):
                core.dispatch(self.config, self.task(write=True), core.RunStore(self.workspace))

    def test_auto_routing_fallback_and_explicit_lane_unchanged(self):
        engine = self.engine({"route": "claude"})
        with patch("fusion_policy.DecisionEngine", return_value=engine):
            automatic = self.task()
            route_task(self.config, automatic, core.RunStore(self.workspace))
            self.assertEqual(automatic["agent"], "codex")
            explicit = self.task("claude")
            route_task(self.config, explicit, core.RunStore(self.workspace))
            self.assertEqual(explicit["agent"], "claude")
            self.assertNotIn("requested_agent", explicit)

    def test_qualified_route_can_select_only_available_candidates(self):
        engine = self.engine({"route": "claude"}, "active")
        original = engine.decide
        def decide(kind, state, questions, context):
            self.qualify(engine, kind, questions)
            return original(kind, state, questions, context)
        with patch.object(engine, "decide", side_effect=decide), patch("fusion_policy.DecisionEngine", return_value=engine):
            task = self.task()
            route_task(self.config, task, core.RunStore(self.workspace))
        self.assertEqual(task["agent"], "claude")
        self.assertIn(":claude:", task["session_key"])

    def test_writer_routes_filter_plan_only_missing_unfit_and_cooldown(self):
        self.config["routes"] = {"reader": {"agent": "codex", "sandbox": "read-only"}, "dead": {"agent": "claude", "command": "missing-worker"}}
        store = core.RunStore(self.workspace)
        with patch.object(store, "traces", return_value=[{"agent": "claude", "failure_class": "quota", "end_time_ms": core.now_ms()}]):
            candidates = route_candidates(self.config, self.task(write=True), store)
        self.assertEqual([c["key"] for c in candidates], ["codex"])

    def test_quota_cooldown_and_switch_exclude_native_aliases(self):
        self.config["routes"] = {"alias": {"agent": "codex"}}
        task = self.task()
        task["excluded_routes"] = ["codex"]
        candidates = route_candidates(self.config, task, core.RunStore(self.workspace))
        self.assertEqual([c["key"] for c in candidates], ["claude"])

    def test_automatic_orc_routes_require_passing_fit_and_budget_evidence(self):
        self.config["routes"] = {"orc-model": {"agent": "claude", "command": "orc", "model": "provider/model"}}
        store = core.RunStore(self.workspace)
        with patch.object(core, "executable", return_value="/fixture/agent"), patch.object(core, "_orc_model_ids", return_value=[]):
            self.assertNotIn("orc-model", [c["key"] for c in route_candidates(self.config, self.task(), store)])
        self.config["routes"] = {}
        task = self.task()
        task["budget_remaining_usd"] = .25
        with patch.object(store, "traces", return_value=[{"agent": "codex", "usage": {"cost": .5}, "status": "success"}]):
            candidates = route_candidates(self.config, task, store)
        self.assertNotIn("codex", [c["key"] for c in candidates])
        self.assertIsNone(next(c for c in candidates if c["key"] == "claude")["mean_cost_usd"])

    def test_orc_route_arms_offer_each_fitted_model_with_its_own_history(self):
        self.config["routes"] = {"orc-free": {"agent": "claude", "command": "orc", "model_selector": "free", "arms": 3}}
        ranked = {("--free", "--tools"): ["free/a", "free/b", "free/untested", "free/c"], ("--fit",): ["free/c", "free/b", "free/a"]}
        store = core.RunStore(self.workspace)
        spans = [{"agent": "claude", "route": "orc-free", "model": "free/b", "run_id": "r1", "status": "success"}]
        DecisionStore(self.workspace).append("outcome", task_id="r1", accepted=True)
        with patch.object(core, "executable", return_value="/fixture/agent"), \
                patch.object(core, "_orc_model_ids", side_effect=lambda command, args: ranked[tuple(args)]), \
                patch.object(store, "traces", return_value=spans):
            candidates = {c["key"]: c for c in route_candidates(self.config, self.task(), store)}
        # orc's quality order, fitted models only, capped at arms.
        self.assertEqual([k for k in candidates if k.startswith("orc-free")], ["orc-free:free/a", "orc-free:free/b", "orc-free:free/c"])
        self.assertEqual(candidates["orc-free:free/b"]["checked_runs"], 1)
        self.assertEqual(candidates["orc-free:free/a"]["runs"], 0)
        self.config["routes"]["orc-free"]["arms"] = 1
        with patch.object(core, "executable", return_value="/fixture/agent"), \
                patch.object(core, "_orc_model_ids", side_effect=lambda command, args: ranked[tuple(args)]):
            keys = [c["key"] for c in route_candidates(self.config, self.task(), store)]
        self.assertIn("orc-free", keys)

    def test_rank_by_outcomes_explores_then_prefers_verified_acceptance(self):
        candidates = [{"key": "weak", "checked_runs": 5, "acceptance_rate": .2},
                      {"key": "strong", "checked_runs": 5, "acceptance_rate": .8},
                      {"key": "new", "checked_runs": 0, "acceptance_rate": None},
                      {"key": "claimed", "checked_runs": 2, "acceptance_rate": 1.0, "reported_success_rate": 1.0}]
        self.assertEqual([c["key"] for c in rank_by_outcomes(candidates)], ["new", "claimed", "strong", "weak"])
        self.assertEqual([c["key"] for c in rank_by_outcomes(candidates, 1)], ["new", "claimed", "strong", "weak"])
        self.assertEqual([c["key"] for c in rank_by_outcomes(candidates, 0)], ["claimed", "strong", "new", "weak"])

    def test_rank_by_outcomes_explore_false_never_lets_untested_lead_a_verified_one(self):
        candidates = [{"key": "weak", "checked_runs": 5, "acceptance_rate": .2},
                      {"key": "strong", "checked_runs": 5, "acceptance_rate": .8},
                      {"key": "new", "checked_runs": 0, "acceptance_rate": None},
                      {"key": "claimed", "checked_runs": 2, "acceptance_rate": 1.0, "reported_success_rate": 1.0}]
        # explore=False skips the tier-0 short-circuit: "new" no longer jumps ahead just for being untested.
        self.assertEqual([c["key"] for c in rank_by_outcomes(candidates, explore=False)], ["claimed", "strong", "new", "weak"])
        self.assertEqual([c["key"] for c in rank_by_outcomes(candidates, 1, explore=False)], ["claimed", "strong", "new", "weak"])

    def test_route_task_prefers_verified_arm_over_untested_once_a_lane_qualifies_on_write(self):
        self.config["routes"] = {"orc-free": {"agent": "claude", "command": "orc", "model_selector": "free", "arms": 3}}
        self.config["decisions"]["rank_by_outcomes"] = 1
        ranked = {("--free", "--tools"): ["free/untested", "free/top", "free/next"], ("--fit",): ["free/untested", "free/top", "free/next"]}
        store = core.RunStore(self.workspace)
        spans = [{"agent": "claude", "route": "orc-free", "model": "free/top", "run_id": "t1", "status": "success"}]
        DecisionStore(self.workspace).append("outcome", task_id="t1", accepted=True)
        write_task = core.make_task(self.workspace, "claude", "Scout issues", "discovery", [], [], None, False, True, route="orc-free")
        with patch.object(core, "executable", return_value="/fixture/agent"), \
                patch.object(core, "_orc_model_ids", side_effect=lambda command, args: ranked[tuple(args)]), \
                patch.object(store, "traces", return_value=spans):
            route_task(self.config, write_task, store)
        # free/top has reached the minimum with a verified acceptance, so a write task
        # prefers it over the untested free/untested lane that would otherwise sort first.
        self.assertEqual(write_task["settings_overrides"]["model"], "free/top")
        read_task = core.make_task(self.workspace, "claude", "Scout issues", "discovery", [], [], None, False, False, route="orc-free")
        with patch.object(core, "executable", return_value="/fixture/agent"), \
                patch.object(core, "_orc_model_ids", side_effect=lambda command, args: ranked[tuple(args)]), \
                patch.object(store, "traces", return_value=spans):
            route_task(self.config, read_task, store)
        # Read-only tasks keep exploring under-tested lanes first, unchanged from today.
        self.assertEqual(read_task["settings_overrides"]["model"], "free/untested")
        # A review gates acceptance, so it also goes to verified evidence (seen live:
        # an unproven free model returned empty reviews twice on 2026-09-24).
        review_task = core.make_task(self.workspace, "claude", "Review the diff", "review", [], [], None, False, False, route="orc-free")
        with patch.object(core, "executable", return_value="/fixture/agent"), \
                patch.object(core, "_orc_model_ids", side_effect=lambda command, args: ranked[tuple(args)]), \
                patch.object(store, "traces", return_value=spans):
            route_task(self.config, review_task, store)
        self.assertEqual(review_task["settings_overrides"]["model"], "free/top")

    def test_shadow_automatic_routing_follows_verified_outcomes_when_enabled(self):
        store = core.RunStore(self.workspace)
        spans = [{"agent": agent, "run_id": f"{agent}-{n}", "status": "success"} for agent in ("codex", "claude") for n in range(3)]
        for n in range(3):
            DecisionStore(self.workspace).append("outcome", task_id=f"codex-{n}", accepted=False)
            DecisionStore(self.workspace).append("outcome", task_id=f"claude-{n}", accepted=True)
        engine = self.engine({"route": "codex"})
        with patch("fusion_policy.DecisionEngine", return_value=engine), patch.object(store, "traces", return_value=spans):
            preference = self.task()
            route_task(self.config, preference, store)
            self.assertEqual(preference["agent"], "codex")
            self.config["decisions"]["rank_by_outcomes"] = True
            ranked = self.task()
            route_task(self.config, ranked, store)
        self.assertEqual(ranked["agent"], "claude")
        application = [e for e in read_jsonl(DecisionStore(self.workspace).path) if e.get("event") == "application"][-1]
        self.assertFalse(application["applied"])
        self.assertIn("verified outcomes", application["reason"])

    def test_observed_errors_count_as_rejections_but_claims_and_quota_do_not(self):
        store = core.RunStore(self.workspace)
        spans = [{"agent": "codex", "run_id": "e1", "status": "error", "failure_class": None},
                 {"agent": "codex", "run_id": "q1", "status": "error", "failure_class": "quota", "end_time_ms": 0},
                 {"agent": "codex", "run_id": "s1", "status": "success"},
                 {"agent": "codex", "run_id": "e2", "status": "error"}]
        DecisionStore(self.workspace).append("outcome", task_id="e2", accepted=True, source="lead")
        with patch.object(store, "traces", return_value=spans):
            codex = next(c for c in route_candidates(self.config, self.task(), store) if c["key"] == "codex")
        self.assertEqual((codex["checked_runs"], codex["acceptance_rate"]), (2, .5))

    def test_named_route_with_arms_picks_its_model_by_outcomes(self):
        self.config["routes"] = {"orc-free": {"agent": "claude", "command": "orc", "model_selector": "free", "arms": 2}}
        self.config["decisions"]["rank_by_outcomes"] = 1
        ranked = {("--free", "--tools"): ["free/top", "free/next"], ("--fit",): ["free/top", "free/next"]}
        store = core.RunStore(self.workspace)
        spans = [{"agent": "claude", "route": "orc-free", "model": "free/top", "run_id": "t1", "status": "error"},
                 {"agent": "claude", "route": "orc-free", "model": "free/next", "run_id": "n1", "status": "success"}]
        DecisionStore(self.workspace).append("outcome", task_id="n1", accepted=True)
        task = core.make_task(self.workspace, "claude", "Scout issues", "discovery", [], [], None, False, False, route="orc-free")
        with patch.object(core, "executable", return_value="/fixture/agent"), \
                patch.object(core, "_orc_model_ids", side_effect=lambda command, args: ranked[tuple(args)]), \
                patch.object(store, "traces", return_value=spans):
            route_task(self.config, task, store)
        self.assertEqual((task["agent"], task["route"], task["settings_overrides"]), ("claude", "orc-free", {"model": "free/next"}))
        application = [e for e in read_jsonl(DecisionStore(self.workspace).path) if e.get("event") == "application"][-1]
        self.assertIn("inside the named route", application["reason"])
        pinned = core.make_task(self.workspace, "claude", "Scout issues", "discovery", [], [], None, False, False,
                                route="orc-free", settings_overrides={"model": "free/top"})
        with patch.object(core, "executable", return_value="/fixture/agent"), \
                patch.object(core, "_orc_model_ids", side_effect=lambda command, args: ranked[tuple(args)]):
            route_task(self.config, pinned, store)
        self.assertEqual(pinned["settings_overrides"], {"model": "free/top"})

    def test_native_model_pin_does_not_leak_into_orc_routes(self):
        config = {**self.config, "claude": {"command": sys.executable, "model": "claude-sonnet-5"},
                  "routes": {"orc-free": {"agent": "claude", "command": "orc", "model_selector": "free"},
                             "pinned": {"agent": "claude", "command": "orc", "model": "free/pinned"},
                             "native": {"agent": "claude", "permission_mode": "plan"}}}
        self.assertNotIn("model", core.agent_settings(config, {"agent": "claude", "route": "orc-free"}))
        self.assertEqual(core.agent_settings(config, {"agent": "claude", "route": "pinned"})["model"], "free/pinned")
        self.assertEqual(core.agent_settings(config, {"agent": "claude", "route": "native"})["model"], "claude-sonnet-5")
        self.assertEqual(core.agent_settings(config, {"agent": "claude"})["model"], "claude-sonnet-5")

    def test_outcome_ranking_keeps_review_independent_of_the_implementer(self):
        store = core.RunStore(self.workspace)
        spans = [{"agent": "claude", "run_id": f"c{n}", "status": "success"} for n in range(3)]
        for n in range(3):
            DecisionStore(self.workspace).append("outcome", task_id=f"c{n}", accepted=True)
        self.config["decisions"]["rank_by_outcomes"] = 0
        self.config["codex"] = {"command": "missing-fusion-test-agent"}
        self.config["agy"] = {"command": sys.executable, "dangerously_skip_permissions": True}
        engine = self.engine({"route": "claude"})
        review = self.task()
        review["prefer_different_agent"] = "claude"
        with patch("fusion_policy.DecisionEngine", return_value=engine), patch.object(store, "traces", return_value=spans), \
                patch.object(core, "agy_headless_status", return_value={"automatic_ready": True}):
            route_task(self.config, review, store)
        self.assertEqual(review["agent"], "agy")

    def test_lead_outcome_is_recorded_for_an_existing_run_only(self):
        run = core.RunStore(self.workspace).runs / "20260924-000000-abcdef12"
        run.mkdir(parents=True)
        (run / "result.json").write_text(json.dumps({"status": "success", "route": "orc-free", "agent": "claude", "model": "free/a"}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(core.main(["--workspace", str(self.workspace), "outcome", run.name, "--rejected", "--reason", "tests fail"]), 0)
        event = [e for e in read_jsonl(DecisionStore(self.workspace).path) if e.get("event") == "outcome"][-1]
        self.assertEqual((event["task_id"], event["accepted"], event["source"], event["model"]), (run.name, False, "lead", "free/a"))
        with self.assertRaisesRegex(ValueError, "no completed Fusion run"):
            core.record_outcome(self.workspace, "missing-run", True)
        with self.assertRaisesRegex(ValueError, "run id"):
            core.record_outcome(self.workspace, "../escape", True)

    def test_automatic_worker_cannot_override_permissions_or_commands(self):
        task = self.task()
        task["settings_overrides"] = {"command": "unreviewed-command"}
        with self.assertRaisesRegex(ValueError, "settings overrides"):
            route_task(self.config, task, core.RunStore(self.workspace))

    def test_agy_read_only_ignores_write_mode_configuration(self):
        task = self.task("agy")
        argv, _, _ = core.agent_command({"agy": {"mode": "yolo"}}, task, None)
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        self.assertIn("--sandbox", argv)

    def test_auto_routing_requires_agy_headless_setup_but_explicit_selection_survives(self):
        self.config["agy"] = {"command":sys.executable}
        with patch.object(core, "agy_headless_status", return_value={"automatic_ready":False}):
            choices = route_candidates(self.config, self.task(), core.RunStore(self.workspace))
            self.assertNotIn("agy", [c["agent"] for c in choices])
            explicit = self.task("agy")
            with patch("fusion_policy.DecisionEngine", return_value=self.engine(mode="off")):
                route_task(self.config, explicit, core.RunStore(self.workspace))
            self.assertEqual(explicit["agent"], "agy")
        # A headless-ready sandbox alone does not admit agy to automatic
        # restricted-mode candidacy: read-only tools like ViewFile still have
        # no auto-approve setting and get auto-denied (issue #100).
        with patch.object(core, "agy_headless_status", return_value={"automatic_ready":True}):
            choices = route_candidates(self.config, self.task(), core.RunStore(self.workspace))
            self.assertNotIn("agy", [c["agent"] for c in choices])
        self.config["agy"]["dangerously_skip_permissions"] = True
        with patch.object(core, "agy_headless_status", return_value={"automatic_ready":True}):
            choices = route_candidates(self.config, self.task(), core.RunStore(self.workspace))
            self.assertIn("agy", [c["agent"] for c in choices])

    def test_specialist_review_adds_scrutiny_only_when_qualified(self):
        engine = self.engine({"specialty": "payments"}, "active")
        self.qualify(engine, "review", REVIEW_QUESTIONS)
        task = self.task("claude")
        task["role"] = "review"
        with patch("fusion_policy.DecisionEngine", return_value=engine):
            review_task(self.config, task)
        self.assertIn("idempotency", task["task"])
        self.assertFalse(task["write"])

    def test_recovery_never_converts_failed_check_to_success_or_retries_denial(self):
        engine = self.engine({"action": "continue"}, "active")
        self.qualify(engine, "recovery", RECOVERY_QUESTIONS)
        node = {"agent": "auto", "attempts": 1}
        with patch("fusion_policy.DecisionEngine", return_value=engine):
            action, _ = recovery(self.config, self.workspace, "workflow", node, {"status": "success", "blockers": ["test failed"]}, False, 2)
            self.assertEqual(action, "repair")
            engine.backend.choices["action"] = "switch"
            action, _ = recovery(self.config, self.workspace, "workflow", node, {"status": "error", "blockers": ["permission denied"]}, False, 2)
            self.assertEqual(action, "stop")

    def test_recovery_switch_respects_explicit_routes_and_attempt_limit(self):
        engine = self.engine({"action": "switch"}, "active")
        self.qualify(engine, "recovery", RECOVERY_QUESTIONS)
        result = {"agent": "codex", "status": "error", "blockers": ["quota exceeded"]}
        with patch("fusion_policy.DecisionEngine", return_value=engine):
            for node in [{"agent": "codex", "attempts": 1}, {"agent": "auto", "route": "pinned", "attempts": 1}, {"agent": "auto", "attempts": 2}]:
                self.assertEqual(recovery(self.config, self.workspace, "w", node, result, False, 2)[0], "stop")
            node = {"agent": "auto", "attempts": 1}
            self.assertEqual(recovery(self.config, self.workspace, "w", node, result, False, 2)[0], "switch")
            self.assertEqual(node["excluded_routes"], ["codex"])

    def test_runtime_request_error_does_not_latch_the_serving_process(self):
        serve = self.workspace / "serve.py"
        serve.write_text("import json, sys\n"
                         "for line in sys.stdin:\n"
                         "    qs = json.loads(line)['questions']\n"
                         "    if any(q['type'] == 'choice' and len(q['criteria']) < 2 for q in qs.values()):\n"
                         "        print(json.dumps({'error': 'RuntimeError: selected index k out of range'}), flush=True)\n"
                         "    else:\n"
                         "        print(json.dumps({'answers': {k: {'noul': .9} for k in qs}, 'model_identity': 'fake'}), flush=True)\n")
        helper = self.workspace / "runtime"
        helper.write_text(f"#!/bin/sh\nexec {sys.executable} {serve}\n")
        helper.chmod(0o755)
        runtime = LayaRuntime({**DEFAULTS, "python": str(helper), "timeout_seconds": 10})
        self.addCleanup(runtime.close)
        one = {"route": {"type": "choice", "instructions": "pick", "criteria": {"only": "codex"}}}
        with self.assertRaisesRegex(RuntimeError, "out of range"):
            runtime.predict("x", one)
        self.assertIsNone(runtime.error)
        self.assertIsNotNone(runtime.process)
        self.assertEqual(runtime.predict("x", INTAKE_QUESTIONS)["answers"]["needs_clarification"], {"noul": .9})

    def test_one_runtime_per_interpreter_sends_each_workspaces_options_and_closes_the_stale_one(self):
        log, serve = self.workspace / "requests.jsonl", self.workspace / "serve.py"
        serve.write_text("import json, os, sys\n"
                         f"log = open({str(log)!r}, 'a')\n"
                         "for line in sys.stdin:\n"
                         "    request = json.loads(line)\n"
                         "    log.write(json.dumps({'pid': os.getpid(), 'model_path': request['model_path']}) + '\\n'); log.flush()\n"
                         "    print(json.dumps({'answers': {}, 'model_identity': 'fake'}), flush=True)\n")
        helper = self.workspace / "runtime"
        helper.write_text(f"#!/bin/sh\nexec {sys.executable} {serve}\n")
        helper.chmod(0o755)
        other = self.workspace / "other-runtime"
        other.write_text(helper.read_text())
        other.chmod(0o755)
        with patch.dict("fusion_decisions._runtimes", clear=True):
            try:
                for python, name in ((helper, "one"), (helper, "two"), (other, "three")):
                    options = {**DEFAULTS, "python": str(python), "timeout_seconds": 10, "model_path": name, "threshold": .5}
                    fusion_decisions.runtime_for(options).predict("x", INTAKE_QUESTIONS)
                    if name == "two":
                        first = fusion_decisions._runtimes[str(helper)]
                self.assertEqual(list(fusion_decisions._runtimes), [str(other)])
                self.assertIsNone(first.process)
            finally:
                fusion_decisions.close_runtimes()
        rows = read_jsonl(log)
        self.assertEqual([row["model_path"] for row in rows], ["one", "two", "three"])
        self.assertEqual(rows[0]["pid"], rows[1]["pid"])
        self.assertNotEqual(rows[1]["pid"], rows[2]["pid"])

    def test_one_option_choice_abstains_before_reaching_the_backend(self):
        engine = self.engine({"route": "only"})
        one = {"route": {"type": "choice", "instructions": "pick", "criteria": {"only": "codex"}}}
        record = engine.decide("routing", "x", one)
        self.assertEqual(record["status"], "unavailable")
        self.assertIn("two options", record["error"])
        self.assertEqual(engine.backend.calls, 0)

    def test_single_candidate_lane_records_no_routing_decision(self):
        engine = self.engine({"route": "claude"})
        explicit = self.task("codex")
        with patch("fusion_policy.DecisionEngine", return_value=engine):
            route_task(self.config, explicit, core.RunStore(self.workspace))
        self.assertEqual(engine.backend.calls, 0)
        self.assertNotIn("routing", explicit.get("decisions", {}))
        self.assertEqual(explicit["agent"], "codex")
        self.assertFalse(engine.store.path.exists())

    def test_gate_span_and_report_expose_acceptance_and_shadow_verdicts(self):
        engine = self.engine({"action": "continue"})
        spec = {"max_attempts": 1, "nodes": [{"id": "build", "agent": "codex", "task": "Fix defect"}]}
        accepted = {"run_id": "r-ok", "status": "success", "summary": "fixed", "changed": ["a.py"], "tests": ["ok"], "usage": {"cost_usd": .1}}
        with patch.object(core, "dispatch", return_value=dict(accepted)), patch("fusion_policy.DecisionEngine", return_value=engine):
            first = WorkflowRunner(self.workspace, self.config, spec).run()
        rejected = {**accepted, "run_id": "r-bad", "blockers": ["test failed"]}
        with patch.object(core, "dispatch", return_value=dict(rejected)), patch("fusion_policy.DecisionEngine", return_value=engine):
            second = WorkflowRunner(self.workspace, self.config, spec).run()
        gates = {span["run_id"]: span for span in core.RunStore(self.workspace).traces(limit=100) if span["agent"] == "gate"}
        self.assertEqual(gates["r-ok"]["status"], "success")
        self.assertIsNone(gates["r-ok"]["failure_class"])
        self.assertEqual(gates["r-ok"]["trace_id"], first["workflow_id"])
        self.assertEqual(gates["r-bad"]["status"], "failed")
        self.assertEqual(gates["r-bad"]["failure_class"], "worker_error")
        self.assertIn("worker reported unresolved blockers", gates["r-bad"]["blockers"])
        report = workflow_report(self.workspace, second["workflow_id"])
        gate_group = next(g for g in report["usage"]["by_route"] if g["agent"] == "gate")
        self.assertEqual((gate_group["success"], gate_group["failed"]), (0, 1))
        node = report["waves"][0]["nodes"][0]
        self.assertEqual(node["decisions"]["recovery"]["recommendations"]["action"]["value"], "continue")
        self.assertFalse(node["decisions"]["recovery"]["applied"])
        text = format_report(select_report(report))
        self.assertIn("Gate: 0 accepted, 1 rejected", text)
        self.assertIn("laya recovery: action=continue", text)
        self.assertIn("[advisory]", text)
        # A resume must not recover the gate span as a second paid call.
        with patch.object(core, "dispatch", side_effect=AssertionError("cached node must not redispatch")):
            resumed = resume_workflow(self.workspace, self.config, first["workflow_id"])
        self.assertEqual(len(resumed["attempt_ledger"]), 1)

    def test_quota_vocabulary_is_shared_by_classification_cooldown_and_pause(self):
        for result in [{"status": "error", "blockers": ["Insufficient credits"]},
                       {"status": "error", "summary": "HTTP 429 Too Many Requests", "blockers": []}]:
            self.assertEqual(core.failure_class(result), "quota")
            self.assertTrue(core.quota_failure(result))
        self.assertIsNone(core.failure_class({"status": "success", "summary": "quota notes updated", "blockers": []}))
        store = core.RunStore(self.workspace)
        stale = core.now_ms() - (core.LANE_COOLDOWN_SECONDS + 1) * 1000
        with patch.object(store, "traces", return_value=[{"agent": "codex", "failure_class": "quota", "end_time_ms": stale}]):
            self.assertIn("codex", [c["key"] for c in route_candidates(self.config, self.task(), store)])
        with patch.object(store, "traces", return_value=[{"agent": "codex", "failure_class": "quota", "end_time_ms": core.now_ms()}]):
            self.assertNotIn("codex", [c["key"] for c in route_candidates(self.config, self.task(), store)])

    def test_repeated_identical_failure_stops_before_the_attempt_limit(self):
        config = {**self.config, "decisions": {"mode": "off"}}
        spec = {"max_attempts": 3, "nodes": [{"id": "build", "agent": "codex", "task": "Fix defect"}]}
        with patch.object(core, "dispatch", side_effect=lambda *a, **k: {"status": "error", "blockers": ["ModuleNotFoundError: laya"], "usage": {}}):
            result = WorkflowRunner(self.workspace, config, spec).run()
        node = result["nodes"][0]
        self.assertEqual((node["attempts"], node["status"]), (2, "failed"))
        self.assertTrue(any("repeated the previous attempt" in blocker for blocker in node["result"]["blockers"]))
        distinct = iter(["first", "second", "third"])
        with patch.object(core, "dispatch", side_effect=lambda *a, **k: {"status": "error", "blockers": [next(distinct)], "usage": {}}):
            result = WorkflowRunner(self.workspace, config, spec).run()
        self.assertEqual((result["nodes"][0]["attempts"], result["nodes"][0]["status"]), (3, "failed"))

    def test_recovery_cannot_repair_a_repeated_failure(self):
        engine = self.engine({"action": "repair"}, "active")
        self.qualify(engine, "recovery", RECOVERY_QUESTIONS)
        node = {"agent": "auto", "attempts": 2, "repeated_failure": True}
        with patch("fusion_policy.DecisionEngine", return_value=engine):
            action, _ = recovery(self.config, self.workspace, "w", node, {"status": "error", "blockers": ["same"]}, False, 3)
        self.assertEqual(action, "stop")
        self.assertIn('"repeated_failure": true', engine.store.records()[-1]["state"])

    def test_acceptance_check_is_advisory_in_shadow_mode(self):
        engine = self.engine({"plausible": "false", "failed_task": "true"})
        self.qualify(engine, "acceptance", ACCEPTANCE_QUESTIONS)
        node = {"task": "Add CSV export", "attempts": 1}
        result = {"run_id": "r1", "status": "success", "summary": "Renamed a variable", "changed": ["a.py"], "tests": []}
        with patch("fusion_policy.DecisionEngine", return_value=engine):
            plausible, decision_id = accept_node(self.config, self.workspace, "w", node, result)
        self.assertTrue(plausible)
        record = engine.store.get(decision_id)
        self.assertEqual(record["kind"], "acceptance")
        self.assertEqual(record["recommendations"]["plausible"]["value"], "false")
        applications = [e for e in read_jsonl(engine.store.path) if e.get("event") == "application" and e["id"] == decision_id]
        self.assertEqual(applications[0]["actual"], "accept")
        self.assertFalse(applications[0]["applied"])

    def test_acceptance_check_rejects_only_on_a_qualified_implausible_verdict(self):
        engine = self.engine({"plausible": "false", "failed_task": "false"}, "active")
        node = {"task": "Add CSV export", "attempts": 1}
        result = {"run_id": "r1", "status": "success", "summary": "Renamed a variable", "changed": ["a.py"], "tests": []}
        with patch("fusion_policy.DecisionEngine", return_value=engine):
            self.assertTrue(accept_node(self.config, self.workspace, "w", node, result)[0])
            self.qualify(engine, "acceptance", ACCEPTANCE_QUESTIONS)
            plausible, decision_id = accept_node(self.config, self.workspace, "w", node, result)
            self.assertFalse(plausible)
            # The second question is recorded, never a gate in either direction.
            engine.backend.choices.update(plausible="true", failed_task="true")
            self.assertTrue(accept_node(self.config, self.workspace, "w", node, result)[0])
        applications = [e for e in read_jsonl(engine.store.path) if e.get("event") == "application" and e["id"] == decision_id]
        self.assertEqual((applications[0]["actual"], applications[0]["applied"]), ("reject", True))

    def test_workflow_acceptance_check_can_reject_but_never_accept(self):
        engine = self.engine({"plausible": "false"}, "active")
        self.qualify(engine, "acceptance", ACCEPTANCE_QUESTIONS)
        spec = {"max_attempts": 1, "nodes": [{"id": "build", "agent": "codex", "task": "Add CSV export"}]}
        passing = {"run_id": "r1", "status": "success", "summary": "Renamed a variable", "changed": ["a.py"], "tests": [], "usage": {}}
        with patch.object(core, "dispatch", return_value=dict(passing)), patch("fusion_policy.DecisionEngine", return_value=engine):
            result = WorkflowRunner(self.workspace, self.config, spec).run()
        node = result["nodes"][0]
        self.assertEqual(node["status"], "invalid")
        self.assertIn("Laya acceptance check: reported success does not plausibly match the task", node["result"]["blockers"])
        self.assertIn("acceptance", node["result"]["decisions"])
        self.assertNotIn("digest", node["result"])
        # A structural failure never reaches the classifier, so a "true" verdict cannot rescue it.
        engine.backend.choices["plausible"] = "true"
        engine.backend.calls = 0
        failing = {**passing, "run_id": "r2", "blockers": ["test failed"]}
        with patch.object(core, "dispatch", return_value=dict(failing)), patch("fusion_policy.DecisionEngine", return_value=engine):
            result = WorkflowRunner(self.workspace, self.config, spec).run()
        node = result["nodes"][0]
        self.assertEqual(node["status"], "invalid")
        self.assertNotIn("acceptance", node["result"].get("decisions", {}))
        records = [e for e in engine.store.records() if e["context"].get("task_id") == "r2"]
        self.assertEqual([e["kind"] for e in records if e["status"] != "unscored"], ["recovery"])
        # The gate's own input is recorded before it decides, never scored.
        self.assertEqual([(e["kind"], e.get("source")) for e in records if e["status"] == "unscored"],
                         [("acceptance", "structural_gate")])

    def test_retry_costs_survive_budget_pause_and_resume(self):
        config = {**self.config, "decisions": {"mode": "off"}}
        spec = {"max_attempts": 3, "budget_usd": 1, "nodes": [{"id": "build", "agent": "codex", "task": "Fix defect"}]}
        # Distinct failures per attempt: identical ones now stop as a stuck loop before the budget matters.
        failures = iter(["failed test", "failed a different test", "still failing"])
        with patch.object(core, "dispatch", side_effect=lambda *a, **k: {"status": "error", "blockers": [next(failures)], "usage": {"cost_usd": .6}}) as dispatch:
            runner = WorkflowRunner(self.workspace, config, spec)
            result = runner.run()
            self.assertEqual(dispatch.call_count, 2)
            self.assertEqual(result["status"], "paused_budget")
            self.assertAlmostEqual(result["spent_usd"], 1.2)
            self.assertAlmostEqual(workflow_report(self.workspace, result["workflow_id"])["spent_usd"], 1.2)
            result = resume_workflow(self.workspace, config, result["workflow_id"])
            self.assertEqual(dispatch.call_count, 2)
            self.assertAlmostEqual(result["spent_usd"], 1.2)

    def test_retry_receives_prior_failure_artifacts(self):
        self.config["decisions"]["mode"] = "off"
        def dispatch(config, task, store):
            if "missing assertion" in task["task"]:
                return {"status": "success", "summary": "fixed", "blockers": []}
            return {"status": "error", "blockers": ["missing assertion"], "artifacts": {"stderr": "/tmp/example.log"}}
        with patch.object(core, "dispatch", side_effect=dispatch):
            result = WorkflowRunner(self.workspace, self.config, {"max_attempts": 2, "nodes": [{"id": "build", "agent": "codex", "task": "Fix it"}]}).run()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["nodes"][0]["attempts"], 2)

    def test_legacy_receipt_digests_and_interrupted_spend_survive_upgrade(self):
        self.config["decisions"]["mode"] = "off"
        runner = WorkflowRunner(self.workspace, self.config, {"nodes": [{"id": "one", "agent": "codex", "task": "Inspect"}]})
        node = runner.nodes["one"]
        old_payload = {"task": "Inspect", "role": "one", "agent": "codex", "route": None, "write": False, "required_files": [], "acceptance": {}}
        expected = hashlib.sha256(core.json_text(old_payload).encode()).hexdigest()
        self.assertEqual(runner._definition_digest(node), expected)
        store = core.RunStore(self.workspace)
        store.root.mkdir(parents=True, exist_ok=True)
        store.traces_path.write_text(json.dumps({"trace_id": runner.run_id, "run_id": "completed-before-crash", "usage": {"cost_usd": .75}}) + "\n")
        restored = WorkflowRunner(self.workspace, self.config, runner.spec, run_id=runner.run_id, resume=True)
        self.assertAlmostEqual(restored._spent(), .75)

    def test_generated_workflow_uses_real_subprocess_handoffs_and_rejects_review_findings(self):
        self.config["decisions"]["mode"] = "off"
        for agent in ["claude", "codex"]:
            path = self.workspace / agent
            path.write_text("#!" + sys.executable + "\n" +
                            "import json, sys\n" +
                            "prompt = sys.stdin.read() if sys.argv[-1] == '-' else sys.argv[-1]\n" +
                            "status = 'blocked' if 'Role: review' in prompt else 'success'\n" +
                            "message = 'STATUS: ' + status + '\\nSUMMARY: fixture handoff\\nTESTS: fixture assertions passed\\nBLOCKERS: ' + ('regression found' if status == 'blocked' else 'none')\n" +
                            ("print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':message}}))\n" if agent == "codex" else
                             "print(json.dumps({'type':'result','result':message,'is_error':False}))\n"))
            path.chmod(0o755)
            self.config[agent]["command"] = str(path)
        prepared = prepare(self.workspace, self.config, "Add CSV export")
        result = WorkflowRunner(self.workspace, self.config, json.loads(Path(prepared["workflow"]).read_text())).run()
        self.assertEqual(result["status"], "failed")
        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertEqual(nodes["implement"]["status"], "success")
        self.assertEqual(nodes["implement"]["result"]["agent"], "codex")
        self.assertEqual(nodes["review"]["result"]["agent"], "claude")
        self.assertNotEqual(nodes["review"]["status"], "success")

    def test_export_requires_reviewed_evidence_and_keeps_groups_together(self):
        engine = self.engine()
        records = [engine.decide("intake", f"task {n}", INTAKE_QUESTIONS, {"group": "same-workflow"}) for n in range(3)]
        with self.assertRaises(ValueError):
            engine.store.label(records[0]["id"], {"workflow": "build"}, "")
        for record in records[:2]:
            engine.store.label(record["id"], {"workflow": "build"}, "Reviewed request and accepted diff")
        output = self.workspace / "data.jsonl"
        engine.store.export(output)
        rows = dataset_rows(output)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["split"] for row in rows}), 1)
        self.assertTrue(all(row["labels"] == {"workflow": "build"} for row in rows))

    def calibration_data(self, repeated_group=False, half=20):
        questions = {"action": {"type": "choice", "instructions": "Next?", "criteria": {"repair": "repair", "stop": "stop"}}}
        rows = [{"schema": "fusion.training.v1", "id": str(i), "group": ("train-group" if i < half else "val-group") if repeated_group else str(i),
                 "split": "train" if i < half else "validation", "kind": "recovery", "state": "test failure", "questions": questions,
                 "schema_hash": digest(questions), "labels": {"action": "repair"},
                 "prediction": {"action": {"repair": .95, "stop": .05}}, "model_identity": "fixture-model"} for i in range(2 * half)]
        path = self.workspace / "data.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path, rows

    def test_calibration_requires_independent_heldout_groups(self):
        # 50 error-free held-out answers: enough for Learn-then-Test at alpha .05, delta .1 (needs 45).
        path, rows = self.calibration_data(repeated_group=True, half=50)
        report = fit_calibration(path, self.workspace / "report.json")
        self.assertFalse(next(iter(report["buckets"].values()))["qualified"])
        for i, row in enumerate(rows):
            row["group"] = str(i)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        report = fit_calibration(path, self.workspace / "qualified.json")
        self.assertTrue(next(iter(report["buckets"].values()))["qualified"])

    def test_calibration_reports_ece_reliability_and_certain_errors(self):
        path, rows = self.calibration_data()
        rows[25]["labels"] = {"action": "stop"}
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        bucket = next(iter(fit_calibration(path, self.workspace / "report.json")["buckets"].values()))
        self.assertEqual(bucket["certain_errors"], {"0.9": 1, "0.95": 1, "0.99": 1})
        self.assertEqual(len(bucket["reliability"]), 1)
        self.assertEqual((bucket["reliability"][0]["floor"], bucket["reliability"][0]["count"], bucket["reliability"][0]["accuracy"]), (0.9, 20, 0.95))
        self.assertAlmostEqual(bucket["ece"], bucket["reliability"][0]["confidence"] - 0.95, places=6)

    def test_evaluate_control_exposes_a_backend_that_ignores_the_state(self):
        from fusion_laya import evaluate
        path, rows = self.calibration_data()
        class Blind:
            def predict(self, request):
                return {"answers": {"action": {"probabilities": {"repair": .9, "stop": .1}}}, "model_identity": "blind", "truncated": False}
        args = argparse.Namespace(dataset=str(path), output=str(self.workspace / "predictions.jsonl"), model_path="", device="cpu", control=True)
        with patch("fusion_laya.Backend", return_value=Blind()):
            report = evaluate(args)
        self.assertEqual((report["validation_questions"], report["accuracy"], report["control_accuracy"]), (20, 1.0, 1.0))
        args = argparse.Namespace(dataset=str(path), output=str(self.workspace / "plain.jsonl"), model_path="", device="cpu")
        with patch("fusion_laya.Backend", return_value=Blind()):
            self.assertIsNone(evaluate(args)["control_accuracy"])

    def test_calibration_rejects_train_validation_leakage(self):
        path, rows = self.calibration_data()
        rows[-1]["group"] = rows[0]["group"]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        with self.assertRaisesRegex(ValueError, "leakage"):
            fit_calibration(path, self.workspace / "report.json")


if __name__ == "__main__":
    unittest.main()


class PlanningOnlyScopeTest(unittest.TestCase):
    """Only a directive restricts scope; describing read-only behavior does not."""
    def test_directives_restrict_and_descriptions_do_not(self):
        from fusion_build import PLANNING_ONLY
        for text in ("Discovery/planning only. Do not implement.", "Keep this read-only.",
                     "This should stay read-only", "Read-only investigation of the cache", "Research only"):
            with self.subTest(text=text):
                self.assertTrue(PLANNING_ONLY.search(text))
        # From #85, which truffle refused to implement: it describes routing policy.
        for text in ("Expected: explore on read-only tasks; for write tasks, prefer proven lanes.",
                     "a read-only route cannot be repurposed into a writer", "Named read-only routes"):
            with self.subTest(text=text):
                self.assertFalse(PLANNING_ONLY.search(text))
