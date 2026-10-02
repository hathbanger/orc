"""Gym results become per-lane, per-work-class priors that routing blends
into its local evidence as capped pseudo-counts."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_gym as gym
import fusion_policy as policy
from fusion_decisions import DecisionEngine, DecisionStore, read_jsonl
from fusion_policy import prior_for, rank_by_outcomes, route_candidates, route_task

OPUS = {"agent": "claude", "model": "claude-opus-5-5", "reasoning_effort": "high"}
AGY = {"agent": "agy"}
DOTS = {"agent": "claude", "route": "orc-free", "model": "free/dots"}
COHERE = {"agent": "claude", "route": "orc-free", "model": "free/cohere"}


def row(task, lane, spec, verdict, kind="fix", mode="hidden", cost=0.0, ms=1000, completed=True):
    suffix = "" if kind == "fix" else f":{kind}"
    return {"event": "finished", "key": f"{task}:{lane}:{mode}{suffix}", "task": task, "lane": lane, "lane_spec": spec,
            "kind": kind, "mode": mode, "verdict": verdict, "completed": completed, "cost_usd": cost, "duration_ms": ms}


def write_rows(root, rows):
    (root / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


class Isolated(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        env = patch.dict(os.environ, {"ORC_HOME": str(self.root / "orc-home"), "FUSION_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        for name in ("FUSION_DECISIONS_MODE", "FUSION_CONFIG", "FUSION_READ_ONLY"):
            os.environ.pop(name, None)
        policy._PRIORS_CACHE.clear()


class ExportTest(Isolated):
    def test_changed_check_inputs_neither_establish_solvability_nor_supply_priors(self):
        write_rows(self.root, [
            {**row("pr-1", "opus", OPUS, "solved"), "check_inputs_changed": ["grade.py"]},
            row("pr-1", "agy", AGY, "unsolved"),
            row("pr-2", "agy", AGY, "solved"),
            {**row("pr-2", "opus", OPUS, "solved"), "check_inputs_changed": ["grade.py"]},
        ])
        value = gym.lane_priors(self.root, include_unknown=True)
        self.assertEqual(list(value["priors"]), ["agy"])
        self.assertEqual(value["priors"]["agy"]["write"]["attempts"], 1)
        self.assertEqual(value["unsolved_by_all"], {"hidden": ["pr-1"]})

    def test_counts_audited_hidden_rows_per_lane_and_work_class(self):
        rows = [
            # pr-1 solvable in hidden: opus solved (twice keyed: the latest row wins), agy unsolved.
            row("pr-1", "opus", OPUS, "unsolved", cost=9.0),
            row("pr-1", "opus", OPUS, "solved", cost=2.0, ms=4000),
            row("pr-1", "agy", AGY, "unsolved", ms=2000),
            # pr-2 unsolved by all in hidden: excluded, negatives do not count.
            row("pr-2", "opus", OPUS, "unsolved"), row("pr-2", "agy", AGY, "unsolved"),
            # pr-3: agy solved; opus hit a bad baseline (excluded) and a regression (a failure).
            row("pr-3", "agy", AGY, "solved"), row("pr-3", "opus", OPUS, "invalid_baseline"),
            row("pr-4", "agy", AGY, "solved"), row("pr-4", "opus", OPUS, "regressed", cost=4.0, ms=2000),
            # Unavailable and visible rows never count.
            row("pr-5", "opus", OPUS, "unavailable", completed=False),
            row("pr-1", "agy", AGY, "solved", mode="visible"),
            # hidden+hints is audited on its own: pr-2 solved there counts.
            row("pr-2", "agy", AGY, "solved", mode="hidden+hints"),
            row("pr-2", "opus", OPUS, "unsolved", mode="hidden+hints"),
            # Localize: pr-1 localizable; pr-2 not localized by anyone.
            row("pr-1", "dots", DOTS, "localized", kind="localize", ms=10000),
            row("pr-1", "cohere", COHERE, "invalid_answer", kind="localize"),
            row("pr-1", "agy", AGY, "partial", kind="localize"),
            row("pr-2", "dots", DOTS, "missed", kind="localize"),
            row("pr-2", "cohere", COHERE, "invalid_answer", kind="localize"),
        ]
        write_rows(self.root, rows)
        value = gym.lane_priors(self.root, now=0, include_unknown=True)
        self.assertEqual(value["schema"], gym.PRIORS_SCHEMA)
        self.assertEqual(value["generated_at"], "1970-01-01T00:00:00Z")
        priors = value["priors"]
        self.assertEqual(sorted(priors), ["agy", "claude:claude-opus-5-5:high", "orc-free:free/cohere", "orc-free:free/dots"])
        opus = priors["claude:claude-opus-5-5:high"]
        self.assertEqual((opus["agent"], opus["route"], opus["model"], opus["reasoning_effort"]),
                         ("claude", None, "claude-opus-5-5", "high"))
        # pr-1 solved, pr-4 regressed, pr-2 unsolved with hints; pr-3's bad baseline is out.
        self.assertEqual({k: opus["write"][k] for k in ("attempts", "successes", "mean_cost_usd", "mean_seconds", "source")},
                         {"attempts": 3, "successes": 1, "mean_cost_usd": 2.0, "mean_seconds": 2.3, "source": "gym"})
        self.assertNotIn("read", opus)
        self.assertEqual((priors["agy"]["write"]["attempts"], priors["agy"]["write"]["successes"]), (4, 3))
        self.assertNotIn("read", priors["agy"], "a partial localization is not evidence")
        self.assertEqual((priors["orc-free:free/dots"]["read"]["attempts"], priors["orc-free:free/dots"]["read"]["successes"]), (1, 1))
        self.assertEqual((priors["orc-free:free/cohere"]["read"]["attempts"], priors["orc-free:free/cohere"]["read"]["successes"]), (1, 0),
                         "an invalid answer is a failed read task")
        self.assertEqual(priors["orc-free:free/dots"]["read"]["mean_seconds"], 10.0)
        self.assertEqual(value["unsolved_by_all"], {"hidden": ["pr-2"], "localize": ["pr-2"]})
        self.assertEqual(value["excluded"], {"invalid_baseline": 1, "partial": 1, "unsolved_by_all": 4})

    def test_cli_writes_beside_the_global_config_and_prints_a_table(self):
        write_rows(self.root, [row("pr-1", "agy", AGY, "solved")])
        parser = core.build_parser()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(core.main(["gym", "priors", str(self.root), "--include-unknown"]), 0)
        path = self.root / "orc-home" / "lane_priors.json"
        self.assertEqual(gym.default_priors_path(), path)
        self.assertEqual(json.loads(path.read_text())["priors"]["agy"]["write"]["successes"], 1)
        self.assertIn("agy", out.getvalue())
        args = parser.parse_args(["gym", "priors", str(self.root), "--out", "-"])
        text = io.StringIO()
        gym.command(args, self.root, out=text)
        self.assertNotIn("wrote", text.getvalue())


class BlendTest(Isolated):
    def index(self, priors):
        path = self.root / "priors.json"
        path.write_text(json.dumps({"schema": gym.PRIORS_SCHEMA, "generated_at": "t", "priors": priors}))
        return policy.load_priors({"path": str(path), "weight": .5, "cap": 10})

    def test_pseudo_counts_are_weighted_capped_and_per_class(self):
        index = self.index({"agy": {"agent": "agy", "write": {"attempts": 8, "successes": 6},
                                    "read": {"attempts": 40, "successes": 30}}})
        candidate = {"agent": "agy", "route": None, "model": ""}
        write = prior_for(index, candidate, "write", .5, 10)
        self.assertEqual((write["prior_attempts"], write["prior_successes"]), (4.0, 3.0))
        read = prior_for(index, candidate, "read", .5, 10)
        self.assertEqual((read["prior_attempts"], read["prior_successes"]), (10, 7.5), "capped at 10, rate kept")
        self.assertEqual(read["prior"]["match"], "exact")
        self.assertEqual(prior_for(index, {"agent": "codex", "route": None, "model": ""}, "read", .5, 10), {"prior_attempts": 0})

    def test_a_route_pinning_the_same_agent_model_and_effort_matches_by_tuple(self):
        index = self.index({"claude:claude-opus-5-5:high": {**OPUS, "write": {"attempts": 4, "successes": 2}},
                            "orc-free:free/dots": {**DOTS, "read": {"attempts": 6, "successes": 6}}})
        pinned = {"agent": "claude", "route": "deep", "model": "claude-opus-5-5", "reasoning_effort": "high"}
        match = prior_for(index, pinned, "write", .5, 10)
        self.assertEqual((match["prior"]["match"], match["prior_attempts"]), ("lane", 2.0))
        self.assertEqual(prior_for(index, {**pinned, "reasoning_effort": "low"}, "write", .5, 10), {"prior_attempts": 0})
        arm = prior_for(index, {"agent": "claude", "route": "orc-free", "model": "free/dots"}, "read", .5, 10)
        self.assertEqual((arm["prior"]["match"], arm["prior"]["key"]), ("exact", "orc-free:free/dots"))

    def test_settings_validate_and_false_disables(self):
        self.assertIsNone(policy.prior_settings({"decisions": {"priors": False}}))
        self.assertEqual(policy.prior_settings({})["path"], str(self.root / "orc-home" / "lane_priors.json"))
        with self.assertRaisesRegex(ValueError, "weight"):
            policy.prior_settings({"decisions": {"priors": {"weight": -1}}})
        bad = self.root / "bad.json"
        bad.write_text("{}")
        with self.assertRaisesRegex(ValueError, "re-export"):
            policy.load_priors({"path": str(bad), "weight": .5, "cap": 10})
        self.assertIsNone(policy.load_priors({"path": str(self.root / "absent.json"), "weight": .5, "cap": 10}))


class RoutingTest(Isolated):
    def setUp(self):
        super().setUp()
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.config = {"decisions": {"mode": "shadow", "rank_by_outcomes": 3}, "routes": {},
                       "codex": {"command": sys.executable}, "claude": {"command": sys.executable},
                       "agy": {"command": "missing-fusion-test-agent"}, "grok": {"command": "missing-fusion-test-agent"}}
        home = self.root / "orc-home"
        home.mkdir()
        (home / "lane_priors.json").write_text(json.dumps({"schema": gym.PRIORS_SCHEMA, "generated_at": "t", "priors": {
            "claude": {"agent": "claude", "write": {"attempts": 12, "successes": 9, "mean_cost_usd": 1.5,
                                                    "mean_seconds": 300.0, "source": "gym", "generated_at": "t"},
                       "read": {"attempts": 4, "successes": 1}}}}))

    def task(self, write=False):
        return core.make_task(self.workspace, "auto", "Fix the retry logic", "implementation", [], [], None, False, write)

    def test_priors_blend_into_ranking_evidence_and_the_log(self):
        store = core.RunStore(self.workspace)
        candidates = {c["key"]: c for c in route_candidates(self.config, self.task(write=True), store)}
        claude = candidates["claude"]
        self.assertEqual((claude["checked_runs"], claude["acceptance_rate"]), (6.0, .75))
        self.assertEqual((claude["checked_runs_local"], claude["acceptance_rate_local"], claude["local_class"]), (0, None, None))
        self.assertEqual((claude["prior"]["class"], claude["prior"]["gym_attempts"], claude["prior"]["mean_seconds"]), ("write", 12, 300.0))
        self.assertEqual(candidates["codex"]["prior_attempts"], 0)
        # Gym evidence reaches the minimum, so a writer goes to claude, not the unproven preferred codex.
        task = self.task(write=True)
        with patch("fusion_policy.DecisionEngine", return_value=DecisionEngine(self.workspace, self.config)):
            route_task(self.config, task, store)
        self.assertEqual(task["agent"], "claude")
        [log] = [e for e in read_jsonl(DecisionStore(self.workspace).path) if e.get("event") == "routing_log"]
        self.assertEqual(log["policy"]["priors"], {"path": str(self.root / "orc-home" / "lane_priors.json"),
                                                   "weight": .5, "cap": 10, "generated_at": "t"})
        logged = {c["key"]: c for c in log["candidates"]}
        self.assertEqual((logged["claude"]["prior_attempts"], logged["claude"]["checked_runs_local"]), (6.0, 0))
        # Read-only work reads the read class: 2 pseudo-attempts at 25%.
        read = {c["key"]: c for c in route_candidates(self.config, self.task(), store)}["claude"]
        self.assertEqual((read["checked_runs"], read["acceptance_rate"]), (2.0, .25))
        self.config["decisions"]["priors"] = False
        off = {c["key"]: c for c in route_candidates(self.config, self.task(write=True), store)}["claude"]
        self.assertEqual((off["checked_runs"], off["prior_attempts"]), (0, 0))

    def test_triage_routing_uses_interpret_prior_and_keeps_read_prior_separate(self):
        path = self.root / "orc-home" / "lane_priors.json"
        value = json.loads(path.read_text())
        value["priors"]["claude"]["interpret"] = {"attempts": 8, "successes": 0}
        path.write_text(json.dumps(value))
        task = self.task()
        task["role"] = "triage-interpret"
        candidate = {c["key"]: c for c in route_candidates(self.config, task, core.RunStore(self.workspace))}["claude"]
        self.assertEqual(candidate["prior"]["class"], "interpret")
        self.assertEqual((candidate["checked_runs"], candidate["acceptance_rate"]), (4, 0))
        task["role"] = "localization"
        reader = {c["key"]: c for c in route_candidates(self.config, task, core.RunStore(self.workspace))}["claude"]
        self.assertEqual((reader["checked_runs"], reader["acceptance_rate"]), (2, .25))

    def test_local_outcomes_dominate_as_they_accumulate(self):
        spans = [{"agent": "claude", "run_id": f"r{i}", "status": "success", "write": True} for i in range(30)]
        for i in range(30):
            DecisionStore(self.workspace).append("outcome", task_id=f"r{i}", accepted=False)
        store = core.RunStore(self.workspace)
        with patch.object(store, "traces", return_value=spans):
            claude = {c["key"]: c for c in route_candidates(self.config, self.task(write=True), store)}["claude"]
        self.assertEqual((claude["checked_runs_local"], claude["checked_runs"]), (30, 36.0))
        self.assertAlmostEqual(claude["acceptance_rate"], 4.5 / 36, places=4)
        ranked = rank_by_outcomes([{"key": "codex", "checked_runs": 3, "acceptance_rate": 2 / 3}, claude], 3, explore=False)
        self.assertEqual(ranked[0]["key"], "codex")

    def test_local_evidence_is_split_by_work_class_with_pooled_fallback(self):
        self.config["decisions"]["priors"] = False
        spans = ([{"agent": "codex", "run_id": f"w{i}", "status": "success", "write": True} for i in range(3)]
                 + [{"agent": "codex", "run_id": f"r{i}", "status": "success", "write": False} for i in range(2)])
        store = DecisionStore(self.workspace)
        for i in range(3):
            store.append("outcome", task_id=f"w{i}", accepted=False)
        for i in range(2):
            store.append("outcome", task_id=f"r{i}", accepted=True)
        runs = core.RunStore(self.workspace)
        with patch.object(runs, "traces", return_value=spans):
            write = {c["key"]: c for c in route_candidates(self.config, self.task(write=True), runs)}["codex"]
            read = {c["key"]: c for c in route_candidates(self.config, self.task(), runs)}["codex"]
        self.assertEqual((write["checked_runs"], write["acceptance_rate"], write["local_class"]), (3, 0.0, "write"))
        self.assertEqual((read["checked_runs"], read["acceptance_rate"], read["local_class"]), (2, 1.0, "read"))
        with patch.object(runs, "traces", return_value=spans[:3]):
            pooled = {c["key"]: c for c in route_candidates(self.config, self.task(), runs)}["codex"]
        self.assertEqual((pooled["checked_runs"], pooled["local_class"]), (3, "pooled"))

    def test_role_evidence_ranks_independently_and_falls_back_below_minimum(self):
        self.config["decisions"]["priors"] = False
        self.config["decisions"]["rank_by_outcomes"] = 2
        spans = []
        for agent in ("codex", "claude"):
            for role in ("Triage  Locate", "triage-interpret"):
                for n in range(2):
                    run = f"{agent}-{role}-{n}"
                    spans.append({"agent": agent, "run_id": run, "status": "success", "write": False, "role": role})
                    DecisionStore(self.workspace).append("outcome", task_id=run, stage="review",
                        accepted=(agent == "codex") == (role == "Triage  Locate"))
        runs = core.RunStore(self.workspace)
        def candidates(role, **kwargs):
            task = {**self.task(), "role": role}
            return route_candidates(self.config, task, runs, **kwargs)
        with patch.object(runs, "traces", return_value=spans):
            for role, winner in (("triage-locate", "codex"), (" TRIAGE\tINTERPRET ", "claude")):
                ranked = rank_by_outcomes(candidates(role), minimum=2)
                self.assertEqual(ranked[0]["key"], winner)
                self.assertEqual([c["acceptance_rate"] for c in ranked], [1.0, 0.0])
                self.assertTrue(all(c["evidence_scope"] == "role" and c["checked_runs"] == 2 for c in ranked))
            fallback = candidates("triage-locate", minimum=3)
            self.assertTrue(all(c["evidence_scope"] == "work" and c["checked_runs"] == 4
                                and c["acceptance_rate"] == .5 for c in fallback))
            # Missing, unknown and unseen roles preserve work-class evidence and ordering.
            for role in (None, "", " Unknown ", "unseen-role", 42):
                self.assertEqual(candidates(role), fallback)
            # An unchecked worker claim must not push a role over the minimum.
            spans.append({"agent": "codex", "run_id": "unmeasured", "status": "success",
                          "write": False, "role": "triage-locate"})
            self.assertEqual(candidates("triage-locate", minimum=3)[0]["evidence_scope"], "work")
            # Fallback is decided separately for each lane.
            spans[:] = [s for s in spans if s["run_id"] != "claude-Triage  Locate-1"]
            mixed = {c["key"]: c for c in candidates("triage-locate")}
            self.assertEqual(mixed["codex"]["evidence_scope"], "role")
            self.assertEqual(mixed["claude"]["evidence_scope"], "work")
            task = {**self.task(), "role": "Triage Locate"}
            with patch("fusion_policy.DecisionEngine", return_value=DecisionEngine(self.workspace, self.config)), \
                    patch.object(core.RunStore, "traces", return_value=spans):
                route_task(self.config, task, runs)
            log = [e for e in read_jsonl(DecisionStore(self.workspace).path) if e["event"] == "routing_log"][-1]
            self.assertEqual({c["key"]: c["evidence_scope"] for c in log["candidates"]},
                             {"codex": "role", "claude": "work"})
            self.assertEqual(log["role"], "triage-locate")

    def test_role_prior_classes_and_missing_interpret_fallback(self):
        path = self.root / "orc-home" / "lane_priors.json"
        value = json.loads(path.read_text())
        value["priors"]["claude"]["interpret"] = {"attempts": 8, "successes": 8}
        path.write_text(json.dumps(value))
        self.config["routes"]["renamed"] = {"agent": "claude"}
        runs = core.RunStore(self.workspace)
        for role, write, expected in (("Triage Interpret", False, "interpret"),
                                     ("triage-locate", False, "read"), ("triage-localize", False, "read"),
                                     ("triage-interpret", True, "write"), (None, False, "read"),
                                     ("unknown", False, "read"), ("other", False, "read")):
            candidates = {c["key"]: c for c in route_candidates(self.config, {**self.task(write), "role": role}, runs)}
            self.assertEqual(candidates["claude"]["prior"]["class"], expected)
            self.assertEqual(candidates["renamed"]["prior"]["class"], expected)
            self.assertEqual(candidates["renamed"]["prior"]["match"], "lane")
        del value["priors"]["claude"]["interpret"]
        path.write_text(json.dumps(value))
        candidates = route_candidates(self.config, {**self.task(), "role": "triage-interpret"}, runs)
        self.assertEqual(next(c for c in candidates if c["key"] == "claude")["prior"]["class"], "read")


class ExcludeModelsTest(Isolated):
    def test_orc_routes_drop_excluded_models_from_their_arms_and_dispatch(self):
        workspace = self.root / "ws"
        workspace.mkdir()
        config = {"decisions": {"mode": "shadow"}, "codex": {"command": "missing"}, "claude": {"command": "missing"},
                  "agy": {"command": "missing"}, "grok": {"command": "missing"},
                  "routes": {"orc-free": {"agent": "claude", "command": "orc", "model_selector": "free", "arms": 2,
                                          "exclude_models": ["free/cohere"]}}}
        ranked = {("--free", "--tools"): ["free/cohere", "free/dots", "free/next"], ("--fit",): ["free/cohere", "free/dots", "free/next"]}
        task = core.make_task(workspace, "auto", "Map it", "discovery", [], [], None, False, False)
        with patch.object(core, "executable", side_effect=lambda command: "/fixture/orc" if command == "orc" else None), \
                patch.object(core, "_orc_model_ids", side_effect=lambda command, args: ranked[tuple(args)]):
            keys = [c["key"] for c in route_candidates(config, task, core.RunStore(workspace))]
            self.assertEqual(keys, ["orc-free:free/dots", "orc-free:free/next"])
            self.assertEqual(core.select_orc_model("orc", "free", exclude={"free/cohere"}), "free/dots")
            self.assertEqual(core.select_orc_model("orc", "free", True, {"free/cohere"}), "free/dots")
            config["routes"]["orc-free"]["exclude_models"] = "free/cohere"
            rejected = {}
            route_candidates(config, task, core.RunStore(workspace), rejected)
        self.assertIn("exclude_models", rejected["orc-free"])


if __name__ == "__main__":
    unittest.main()
