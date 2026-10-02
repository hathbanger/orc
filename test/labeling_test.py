"""Grounded drafts, abstentions, and explicit approval before training export."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fusion_decisions import DecisionStore, digest, read_jsonl
from fusion_labeling import evidence_bundle, labelable, parse_suggestion, suggest, teacher_questions


class LabelingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.store = DecisionStore(self.workspace)
        self.record = dict(id="decision", kind="review", status="ok", truncated=False,
                           state='{"task":"Review payment rounding"}',
                           questions={"specialty": {"type": "choice", "criteria": {"general": "ordinary correctness", "payments": "money movement"}},
                                      "needs_review": {"type": "noul"}},
                           schema_hash="fixture", prediction={}, context={"task_id": "run-1"})
        self.store.append("decision", **self.record)
        self.sources = evidence_bundle(self.workspace, self.record)
        self.valid = {"answers": {"specialty": {"value": "payments", "reason": "The task concerns payment rounding.", "evidence": ["E1"]}},
                      "abstentions": {"needs_review": "No handoff evidence supplied."}}

    def block(self, value):
        return '```label-suggestion\n' + json.dumps(value) + '\n```'

    def test_schema_requires_valid_labels_grounded_citations_and_full_coverage(self):
        self.assertEqual(parse_suggestion(self.block(self.valid), self.record, self.sources), self.valid)
        for field, value in (("value", "made-up"), ("value", True), ("reason", ""), ("evidence", []), ("evidence", ["E99"]), ("evidence", [["E1"]])):
            invalid = copy.deepcopy(self.valid)
            invalid["answers"]["specialty"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                parse_suggestion(self.block(invalid), self.record, self.sources)
        for value in ({"answers": {}, "abstentions": {}}, {"answers": [], "abstentions": {}}, [],
                      {"answers": {}, "abstentions": {"specialty": "Unknown", "needs_review": ""}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_suggestion(self.block(value), self.record, self.sources)
        with self.assertRaises(ValueError):
            parse_suggestion(self.block(self.valid) * 2, self.record, self.sources)

    def test_abstaining_is_valid_and_does_not_automatically_create_labels(self):
        value = {"answers": {}, "abstentions": {k: "Evidence is missing." for k in self.record["questions"]}}
        self.assertEqual(parse_suggestion(self.block(value), self.record, self.sources), value)
        self.store.append("label_suggestion", id="decision", verified=False, **value)
        self.assertEqual(self.store.export(self.workspace / "dataset.jsonl", include_unknown=True)["examples"], 0)

    def test_teacher_receives_explicit_boolean_labels_and_can_partially_answer(self):
        import fusion_core as core
        record = {**self.record, "id": "partial-decision", "state": '{"request":"Obtain an independent review."}'}
        record["questions"]["needs_review"]["instructions"] = "Does this handoff need an independent review?"
        self.store.append("decision", **record)
        original = copy.deepcopy(record)
        answer = {"answers": {"needs_review": {"value": "true", "reason": "The request explicitly requires independent review.", "evidence": ["E1"]}},
                  "abstentions": {"specialty": "The input omits implementation details."}}

        def dispatch(config, task, store):
            prompt = task["task"]
            questions = json.loads(prompt.split("\nQuestions:\n")[1].split("\nEvidence packet:\n")[0])
            self.assertEqual(questions["needs_review"]["type"], "boolean")
            self.assertEqual(questions["needs_review"]["allowed_labels"], ["false", "true"])
            self.assertIn("Yes:", questions["needs_review"]["label_meanings"]["true"])
            self.assertEqual(questions["needs_review"]["instructions"], record["questions"]["needs_review"]["instructions"])
            self.assertEqual(questions["specialty"]["allowed_labels"], ["general", "payments"])
            self.assertEqual(questions["specialty"]["criteria"], record["questions"]["specialty"]["criteria"])
            self.assertIn("Assess each question independently", prompt)
            self.assertIn('Unknown is an abstention, not "false"', prompt)
            self.assertEqual(config["decisions"]["mode"], "off")
            directory = self.workspace / ".fusion/runs/teacher"
            directory.mkdir(parents=True)
            (directory / "answer.md").write_text(self.block(answer))
            return {"status": "success", "exit_code": 0, "run_id": "teacher", "agent": "codex"}

        with patch.object(core, "dispatch", side_effect=dispatch):
            draft = suggest(self.workspace, core.DEFAULTS, record["id"], "codex")
        self.assertEqual(draft["answers"], answer["answers"])
        self.assertEqual(record, original)
        self.assertEqual(self.store.get(record["id"])["questions"], original["questions"])
        self.assertEqual(self.store.export(self.workspace / "before.jsonl", include_unknown=True)["examples"], 0)
        for wrong in (True, False, "unknown", 1):
            invalid = copy.deepcopy(answer)
            invalid["answers"]["needs_review"]["value"] = wrong
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                parse_suggestion(self.block(invalid), record, self.sources)
        self.store.label(record["id"], {"needs_review": "true"}, "E1 explicitly requests independent review", draft["suggestion_id"], replace=True)
        destination = self.workspace / "after.jsonl"
        self.store.export(destination, include_unknown=True)
        self.assertEqual(json.loads(destination.read_text())["labels"], {"needs_review": "true"})

    def test_score_contract_uses_string_indices_without_changing_criteria(self):
        questions = {"risk": {"type": "score", "criteria": ["low", "medium", "high"]}}
        contract = teacher_questions(questions)["risk"]
        self.assertEqual(contract["allowed_labels"], ["0", "1", "2"])
        self.assertEqual(contract["criteria"], questions["risk"]["criteria"])
        self.assertIn("zero-based", contract["encoding"])

    def test_evidence_is_scoped_to_attempt_and_withholds_predictions(self):
        directory = self.workspace / ".fusion/runs/run-1"
        directory.mkdir(parents=True)
        (directory / "answer.md").write_text("Worker claims a test passed.")
        (directory / "result.json").write_text(json.dumps({"status": "success", "summary": "Checked", "decisions": {"review": "hidden"}, "tests": ["self-report"]}))
        sources = evidence_bundle(self.workspace, self.record)
        self.assertEqual(len(sources), 3)
        self.assertNotIn("decisions", sources[1]["text"])
        self.assertIn("postdate", sources[2]["timing"])
        (directory / "answer.md").unlink()
        secret = self.workspace / "secret"
        secret.write_text("Do not include")
        (directory / "answer.md").symlink_to(secret)
        self.assertEqual(len(evidence_bundle(self.workspace, self.record)), 2)
        self.assertEqual(len(evidence_bundle(self.workspace, {**self.record, "context": {"task_id": "../../outside"}})), 1)

    def test_unavailable_and_truncated_decisions_cannot_be_suggested(self):
        for overrides in ({"status": "unavailable"}, {"truncated": True}):
            self.store.append("decision", **{**self.record, **overrides})
            with self.assertRaises(ValueError):
                labelable(self.store, "decision")

    def test_worker_failure_or_invalid_output_cannot_save_draft(self):
        import fusion_core as core
        for result in ({"status": "error", "summary": "quota exhausted"}, {"status": "success", "exit_code": 0, "run_id": "bad"}):
            directory = self.workspace / ".fusion/runs/bad"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "answer.md").write_text("Unstructured answer")
            with patch.object(core, "dispatch", return_value=result), self.assertRaises(ValueError):
                suggest(self.workspace, core.DEFAULTS, "decision", "codex")
        self.assertFalse(any(e["event"] == "label_suggestion" for e in read_jsonl(self.store.path)))

    def test_approval_tracks_draft_and_rejects_wrong_or_stale_decision(self):
        self.store.append("label_suggestion", id="decision", suggestion_id="draft", **self.valid,
                          decision_hash=digest({k: self.record.get(k) for k in ("state", "questions", "schema_hash")}), agent="codex", run_id="teacher")
        with self.assertRaisesRegex(ValueError, "Unknown suggestion"):
            self.store.label("decision", {"specialty": "payments"}, "Checked", "other")
        self.store.label("decision", {"specialty": "payments"}, "Checked E1", "draft")
        self.assertFalse(read_jsonl(self.store.path)[-1]["answers_edited"])
        self.store.append("decision", **{**self.record, "state": "Changed input"})
        with self.assertRaisesRegex(ValueError, "changed"):
            self.store.label("decision", {"specialty": "payments"}, "Checked", "draft")

    def test_replacing_review_omits_questions_the_human_left_unlabeled(self):
        self.store.label("decision", {"specialty": "payments", "needs_review": "true"}, "Initial assessment")
        self.store.label("decision", {"specialty": "payments"}, "No evidence for needs_review", replace=True)
        path = self.workspace / "revised.jsonl"
        self.store.export(path, include_unknown=True)
        self.assertEqual(json.loads(path.read_text())["labels"], {"specialty": "payments"})

    def test_council_independent_prompts_consensus_and_approval_provenance(self):
        import fusion_core as core
        tasks = []
        def dispatch(config, task, store):
            tasks.append(task)
            directory = self.workspace / '.fusion/runs' / task['agent']
            directory.mkdir(parents=True)
            (directory / 'answer.md').write_text(self.block(self.valid))
            return {'status': 'success', 'exit_code': 0, 'agent': task['agent'], 'run_id': task['agent']}
        with patch.object(core, 'dispatch', side_effect=dispatch):
            draft = suggest(self.workspace, core.DEFAULTS, 'decision', labeling_mode='council', council_agents=['codex', 'claude'])
        self.assertEqual(tasks[0]['task'], tasks[1]['task'])
        self.assertNotEqual(tasks[0]['session_key'], tasks[1]['session_key'])
        self.assertTrue(all(not task['write'] and not task['resume'] for task in tasks))
        self.assertNotIn('prediction', tasks[0]['task'])
        self.assertEqual(draft['answers']['specialty']['value'], 'payments')
        self.assertEqual(draft['council']['questions']['needs_review']['state'], 'insufficient')
        self.assertEqual(len(draft['council']['members']), 2)
        self.assertEqual(self.store.export(self.workspace / 'before.jsonl', include_unknown=True)['examples'], 0)
        self.store.label('decision', {'specialty': 'payments'}, 'Checked E1', draft['suggestion_id'])
        event = read_jsonl(self.store.path)[-1]
        self.assertEqual(event['suggested_by']['labeling_mode'], 'council')
        self.assertFalse(event['answers_edited'])

    def test_council_disagreement_and_failure_cannot_silently_become_single_worker(self):
        import fusion_core as core
        good = {**self.valid, 'status': 'success', 'requested_agent': 'codex', 'agent': 'codex'}
        other = copy.deepcopy(good)
        other.update(requested_agent='claude', agent='claude')
        other['answers']['specialty']['value'] = 'general'
        for member, expected in [(other, 'disputed'), ({'requested_agent': 'claude', 'status': 'error', 'error': 'quota'}, 'insufficient')]:
            with patch('fusion_labeling.assessment', side_effect=[good, member]):
                draft = suggest(self.workspace, core.DEFAULTS, 'decision', labeling_mode='council', council_agents=['codex', 'claude'])
            self.assertEqual(draft['answers'], {})
            self.assertEqual(draft['council']['questions']['specialty']['state'], expected)
            self.assertIn('specialty', draft['abstentions'])
        self.assertEqual(self.store.export(self.workspace / 'none.jsonl', include_unknown=True)['examples'], 0)

    def test_invalid_council_never_dispatches(self):
        import fusion_core as core
        for members in ([], ['codex'], ['codex', 'codex'], ['auto', 'claude'], 'codex,claude'):
            with patch.object(core, 'dispatch') as dispatch, self.assertRaises(ValueError):
                suggest(self.workspace, core.DEFAULTS, 'decision', labeling_mode='council', council_agents=members)
            dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
