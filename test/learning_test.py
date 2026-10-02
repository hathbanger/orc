"""Learning metrics reflect effective labels; garden work is sequential and opt-in."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_garden as garden
from fusion_decisions import DecisionStore, digest
from fusion_learning import decision_rows, learning_summary
from fusion_ui import ControlRoom, atomic_json
from ui_test import seed_workspace


class LearningTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / 'repo'
        self.config = seed_workspace(self.workspace)
        self.env = patch.dict(os.environ, {'ORC_HOME': str(Path(self.temp.name) / 'orc-home'), 'FUSION_TELEMETRY': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = ControlRoom(self.workspace)
        self.store = DecisionStore(self.workspace)

    def seed(self, key='decision', **overrides):
        questions = {'plausible': {'type': 'noul', 'instructions': 'Did the worker satisfy the task?'}}
        row = {'id': key, 'kind': 'acceptance', 'mode': 'shadow', 'status': 'ok', 'truncated': False,
               'questions': questions, 'schema_hash': digest(questions), 'state': '{"summary":"Did nothing"}',
               'prediction': {'plausible': {'false': .7, 'true': .3}}, 'context': {'group': key}, 'model_identity': 'bundled', **overrides}
        self.store.append('decision', **row)
        return row

    def fake_launch(self, workspace, body, *, garden=False):
        job = {'id': body['decision_id'], 'decision_id': body['decision_id'], 'action': 'suggest-labels',
               'started_at_ms': int(time.time() * 1000), 'status': 'queued', 'garden': garden}
        atomic_json(workspace / '.fusion/ui/jobs' / job['id'] / 'job.json', job)
        return job

    def test_effective_labels_replacements_exclusions_and_metrics_match_export(self):
        self.seed('a')
        self.seed('b')
        self.seed('bad', truncated=True)
        self.store.label('a', {'plausible': 'false'}, 'Verified original input', replace=True)
        self.store.label('a', {'plausible': 'true'}, 'Corrected after review', replace=True)
        self.store.append('label_suggestion', id='b', answers={'plausible': {'value': 'false'}}, abstentions={})
        summary = learning_summary(self.workspace, self.config)
        self.assertEqual(summary['labeled_questions'], 1)
        self.assertEqual(summary['reviewed_decisions'], 1)
        self.assertEqual(summary['eligible_questions'], 2)
        self.assertEqual(summary['counts']['needs_review'], 1)
        self.assertEqual(summary['agreement'], {'matched': 0, 'compared': 1, 'rate': 0})
        self.assertEqual(summary['candidates'], [])
        self.assertEqual(summary['model']['path'], '')
        self.assertEqual(sum(p['labels'] for p in summary['growth']), 1)
        self.store.append('label_exclusion', id='a', excluded=True)
        self.assertEqual(learning_summary(self.workspace, self.config)['labeled_questions'], 0)
        self.assertEqual(self.store.export(self.workspace / 'excluded.jsonl', include_unknown=True)['examples'], 0)
        self.store.append('label_exclusion', id='a', excluded=False)
        target = self.workspace / 'restored.jsonl'
        self.assertEqual(self.store.export(target, include_unknown=True)['examples'], 1)
        self.assertEqual(json.loads(target.read_text())['labels'], {'plausible': 'true'})

    def test_independent_group_readiness_and_no_model_improvement_from_approval(self):
        for i in range(30):
            self.seed(str(i))
            self.store.label(str(i), {'plausible': 'false'}, 'Checked')
        summary = learning_summary(self.workspace, self.config)
        self.assertTrue(summary['can_train'])
        self.assertEqual(sum(summary['groups'].values()), 30)
        self.assertEqual(summary['agreement']['rate'], 1)
        self.assertEqual(summary['evaluations'], [])
        self.assertEqual(summary['candidates'], [])

    def test_garden_disabled_by_default_and_new_only_selection_survives_restart(self):
        self.seed('old', time_ms=1)
        with patch.object(self.app, 'launch', side_effect=self.fake_launch) as launch:
            garden.tick(self.app, self.workspace)
            launch.assert_not_called()
            self.assertFalse((self.workspace / '.fusion/decisions/garden.json').exists())
            garden.save(self.app, self.workspace, {'enabled': True, 'agent': 'codex', 'daily_limit': 2})
            garden.tick(self.app, self.workspace)
            launch.assert_not_called()
            self.seed('new')
            garden.tick(self.app, self.workspace)
            self.assertEqual(launch.call_args.args[1]['decision_id'], 'new')
            garden.tick(self.app, self.workspace)
            self.assertEqual(launch.call_count, 1)
        restored = ControlRoom(self.workspace)
        self.assertEqual(garden.status(restored, self.workspace)['state'], 'drafting')
        self.assertEqual(garden.settings(self.workspace)['agent'], 'codex')

    def test_garden_no_daily_cap_pause_failure_no_retry_and_exclusion(self):
        for key in ['first', 'second', 'excluded', 'approved']:
            self.seed(key)
        self.store.label('approved', {'plausible': 'false'}, 'Checked')
        self.store.append('label_exclusion', id='excluded', excluded=True)
        garden.save(self.app, self.workspace, {'enabled': True, 'daily_limit': 1, 'include_existing': True})
        with patch.object(self.app, 'launch', side_effect=self.fake_launch) as launch:
            garden.tick(self.app, self.workspace)
            self.assertEqual(launch.call_count, 1)
            key = launch.call_args.args[1]['decision_id']
            path = self.workspace / '.fusion/ui/jobs' / key / 'job.json'
            job = json.loads(path.read_text())
            atomic_json(path, {**job, 'status': 'failed'})
            self.assertEqual(garden.status(self.app, self.workspace)['state'], 'queued')
            garden.save(self.app, self.workspace, {'enabled': False, 'daily_limit': 2})
            garden.tick(self.app, self.workspace)
            self.assertEqual(launch.call_count, 1)
            garden.save(self.app, self.workspace, {'enabled': True, 'daily_limit': 2})
            garden.tick(self.app, self.workspace)
            self.assertEqual(launch.call_count, 2)
            self.assertNotEqual(launch.call_args.args[1]['decision_id'], key)
            self.assertNotIn(launch.call_args.args[1]['decision_id'], ['approved', 'excluded'])
        self.assertEqual(self.store.export(self.workspace / 'only-approved.jsonl', include_unknown=True)['examples'], 1)

    def test_invalid_garden_configuration_cannot_enable_calls(self):
        for value in [{'enabled': 'yes'}, {'enabled': True, 'labeling_mode': 'council', 'council_agents': ['codex']}, {'enabled': True, 'labeling_mode': 'bad'}, {'enabled': True, 'agent': 'bad'}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                garden.save(self.app, self.workspace, value)
        self.assertFalse(garden.settings(self.workspace)['enabled'])

    def test_legacy_daily_limit_is_ignored_and_usage_counts_all_history(self):
        self.seed()
        now = int(time.time() * 1000)
        for i in range(110):
            atomic_json(self.workspace / '.fusion/ui/jobs' / str(i) / 'job.json',
                        {'id': str(i), 'action': 'suggest-labels', 'garden': True, 'decision_id': str(i), 'status': 'failed', 'started_at_ms': now})
        atomic_json(self.workspace / '.fusion/decisions/garden.json', {'enabled': True, 'daily_limit': 20, 'since_ms': 0})
        self.assertEqual(garden.status(self.app, self.workspace)['used_today'], 110)
        with patch.object(self.app, 'launch') as launch:
            garden.tick(self.app, self.workspace)
            launch.assert_called_once()
        self.assertNotIn('daily_limit', garden.status(self.app, self.workspace))

    def test_candidate_and_evaluation_are_distinct_from_configured_model(self):
        root = self.workspace / '.fusion/ui/jobs'
        candidate = root / 'training/candidate'
        atomic_json(root / 'training/job.json', {'id': 'training', 'action': 'train', 'status': 'success', 'started_at_ms': 1})
        atomic_json(root / 'training/request.json', {'learning': {'dataset': '/fixture/dataset.jsonl'}})
        atomic_json(candidate / 'training.json', {'steps': 10, 'model_identity': 'candidate', 'source_identity': 'base'})
        atomic_json(root / 'eval/job.json', {'id': 'eval', 'action': 'evaluate', 'status': 'success', 'started_at_ms': 2,
                                           'result': {'accuracy': .8, 'control_accuracy': .5, 'dataset_hash': 'same', 'model_identities': ['candidate']}})
        summary = learning_summary(self.workspace, self.config)
        self.assertEqual(len(summary['candidates']), 1)
        self.assertEqual(summary['evaluations'][0]['result']['accuracy'], .8)
        self.assertEqual(summary['model']['path'], '')
        self.assertEqual(summary['model']['training'], {})

    def test_council_settings_survive_pause_restart_and_reach_jobs(self):
        self.seed()
        garden.save(self.app, self.workspace, {'enabled': True, 'include_existing': True,
                    'labeling_mode': 'council', 'council_agents': ['codex', 'claude']})
        garden.save(self.app, self.workspace, {'enabled': False})
        restored = ControlRoom(self.workspace)
        saved = garden.settings(self.workspace)
        self.assertEqual(saved['council_agents'], ['codex', 'claude'])
        garden.save(restored, self.workspace, {'enabled': True})
        with patch.object(restored, 'launch', side_effect=self.fake_launch) as launch:
            garden.tick(restored, self.workspace)
        self.assertEqual(launch.call_args.args[1]['labeling_mode'], 'council')
        self.assertEqual(launch.call_args.args[1]['council_agents'], ['codex', 'claude'])


if __name__ == '__main__':
    unittest.main()
