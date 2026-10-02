"""decisions.write_trials lets a listed, unproven lane earn evidence on gating work."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_policy as policy
from fusion_decisions import DecisionEngine, DecisionStore, read_jsonl


class Backend:
    def __init__(self, route=None):
        self.route = route

    def predict(self, state, questions):
        answers = {}
        for key, question in questions.items():
            labels = list(question["criteria"])
            selected = self.route if self.route in labels else labels[0]
            answers[key] = {"probabilities": {label: .99 if label == selected else .01 / (len(labels) - 1) for label in labels}}
        return {"answers": answers, "model_identity": "fixture-model"}


class WriteTrialsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex'), 'ORC_HOME': str(self.root / 'orc'),
                                      'FUSION_PROGRESS': '0', 'FUSION_TELEMETRY': '0', 'FUSION_DECISIONS_MODE': 'shadow'})
        env.start()
        self.addCleanup(env.stop)
        self.store = core.RunStore(self.root)
        self.config = {'sidekick': 'claude', 'execution_mode': 'yolo',
                       'decisions': {'mode': 'shadow', 'priors': False, 'rank_by_outcomes': 3,
                                     'auto_routes': ['opus', 'fable', 'flash']},
                       'routes': {'opus': {'agent': 'claude', 'model': 'claude-opus-5-5'},
                                  'fable': {'agent': 'claude', 'model': 'claude-fable-5'},
                                  'flash': {'agent': 'claude', 'model': 'claude-haiku-4-5', 'cost_tier': 0},
                                  'fable-api': {'agent': 'claude', 'model': 'claude-fable-5', 'billing': 'api',
                                                'account': 'anthropic-api'}}}
        self.spans = []
        self.recommend = None

    def checked(self, route, runs, accepted=True):
        for n in range(runs):
            run_id = f'{route}-{len(self.spans)}'
            self.spans.append({'agent': 'claude', 'route': route, 'run_id': run_id, 'write': True,
                               'status': 'success', 'end_time_ms': 0})
            DecisionStore(self.root).append('outcome', task_id=run_id, accepted=accepted)

    def route(self, write=True, role='implementation'):
        task = core.make_task(self.root, 'auto', 'fixture', role, [], [], None, False, write)
        with patch.object(self.store, 'traces', return_value=list(self.spans)), \
                patch.object(core, 'executable', return_value=True), \
                patch('fusion_policy.DecisionEngine', side_effect=lambda workspace, config: DecisionEngine(
                    workspace, config, Backend(self.recommend))):
            policy.route_task(self.config, task, self.store)
        return task

    def last_log(self):
        return [row for row in read_jsonl(DecisionStore(self.root).path) if row.get('event') == 'routing_log'][-1]

    def test_a_listed_unproven_lane_takes_the_write(self):
        self.checked('opus', 3)
        self.assertEqual(self.route()['route'], 'opus')
        self.config['decisions']['write_trials'] = ['fable']
        self.assertEqual(self.route()['route'], 'fable')

    def test_the_trial_ends_once_the_lane_has_evidence(self):
        self.config['decisions']['write_trials'] = ['fable']
        self.checked('opus', 5)
        self.checked('fable', 3, accepted=False)
        self.assertEqual(self.route()['route'], 'opus')
        self.assertNotIn('write_trial', self.last_log())

    def test_an_unlisted_unproven_lane_is_never_promoted(self):
        self.config['decisions']['write_trials'] = ['fable']
        self.checked('opus', 3)
        self.checked('fable', 3)
        for _ in range(5):
            self.assertNotEqual(self.route()['route'], 'flash')

    def test_a_review_is_gating_work_and_takes_a_trial(self):
        self.config['decisions']['write_trials'] = ['fable']
        self.checked('opus', 3)
        self.assertEqual(self.route(write=False, role='review')['route'], 'fable')
        self.assertEqual(self.last_log()['write_trial']['route'], 'fable')

    def test_read_only_work_is_not_a_trial(self):
        self.config['decisions']['write_trials'] = ['fable']
        self.checked('opus', 3)
        self.route(write=False)
        self.assertNotIn('write_trial', self.last_log())

    def test_an_overflow_lane_is_not_promoted_while_a_primary_survives(self):
        self.config['decisions'].update({'auto_routes': ['opus', 'fable-api'], 'overflow_routes': ['fable-api'],
                                         'write_trials': ['fable-api']})
        self.checked('opus', 3)
        self.assertEqual(self.route()['route'], 'opus')
        self.assertNotIn('write_trial', self.last_log())

    def test_a_qualified_recommendation_still_wins(self):
        self.config['decisions']['write_trials'] = ['fable']
        self.checked('opus', 3)
        self.recommend = 'opus'
        with patch.object(DecisionEngine, 'allowed', return_value=True):
            self.assertEqual(self.route()['route'], 'opus')
        self.assertNotIn('write_trial', self.last_log())

    def test_a_pinned_route_is_unaffected(self):
        self.config['decisions']['write_trials'] = ['fable']
        task = core.make_task(self.root, 'auto', 'fixture', 'implementation', [], [], None, False, True)
        task['route'] = 'opus'
        with patch.object(self.store, 'traces', return_value=[]), patch.object(core, 'executable', return_value=True), \
                patch('fusion_policy.DecisionEngine', side_effect=lambda workspace, config: DecisionEngine(
                    workspace, config, Backend('fable'))):
            policy.route_task(self.config, task, self.store)
        self.assertEqual(task['route'], 'opus')

    def test_invalid_write_trials_are_refused(self):
        self.checked('opus', 3)
        for trials in ('fable', ['nope'], [1], {'fable': True}):
            with self.subTest(trials=trials):
                self.config['decisions']['write_trials'] = trials
                with self.assertRaisesRegex(ValueError, 'decisions.write_trials'):
                    self.route()

    def test_the_routing_log_records_the_trial(self):
        self.config['decisions']['write_trials'] = ['fable']
        self.checked('opus', 3)
        self.checked('fable', 1)
        self.route()
        row = self.last_log()
        self.assertEqual(row['write_trial'], {'route': 'fable', 'checked_runs': 1, 'minimum': 3})
        self.assertEqual(row['chosen'], 'fable')
        self.assertEqual({c['key']: c['propensity'] for c in row['candidates']}, {'fable': 1.0, 'opus': 0.0, 'flash': 0.0})
        application = [e for e in read_jsonl(DecisionStore(self.root).path) if e.get('event') == 'application'][-1]
        self.assertIn('write trial', application['reason'])


if __name__ == '__main__':
    unittest.main()
