"""decisions.gating_policy thompson: gating picks are sampled from each model's pooled local evidence."""
import os
from pathlib import Path
import random
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


class GatingThompsonTest(unittest.TestCase):
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
                       'decisions': {'mode': 'shadow', 'priors': False, 'rank_by_outcomes': 3, 'gating_policy': 'thompson',
                                     'auto_routes': ['opus', 'fable']},
                       'routes': {'opus': {'agent': 'claude', 'model': 'claude-opus-5-5'},
                                  'fable': {'agent': 'claude', 'model': 'claude-fable-5'},
                                  'fable-api': {'agent': 'claude', 'model': 'claude-fable-5', 'billing': 'api',
                                                'account': 'api-account'},
                                  'opus-api': {'agent': 'claude', 'model': 'claude-opus-5-5', 'billing': 'api',
                                               'account': 'api-account'},
                                  'other': {'agent': 'codex', 'model': 'gpt-fixture'}}}
        self.spans = []
        self.recommend = None

    def checked(self, route, accepted, rejected=0, cost=None, agent='claude'):
        for ok in [True] * accepted + [False] * rejected:
            run_id = f'{route}-{len(self.spans)}'
            span = {'agent': agent, 'route': route, 'run_id': run_id, 'write': True, 'role': 'implementation',
                    'status': 'success', 'end_time_ms': 0}
            if cost is not None:
                span['usage'] = {'cost_usd': cost}
            self.spans.append(span)
            DecisionStore(self.root).append('outcome', task_id=run_id, accepted=ok)

    def route(self, write=True, role='implementation', rng=None, task=None, recommend_allowed=False):
        task = task or core.make_task(self.root, 'auto', 'fixture', role, [], [], None, False, write)
        with patch.object(self.store, 'traces', return_value=list(self.spans)), \
                patch.object(core, 'executable', return_value=True), \
                patch('fusion_policy.DecisionEngine', side_effect=lambda workspace, config: DecisionEngine(
                    workspace, config, Backend(self.recommend))), \
                patch.object(DecisionEngine, 'allowed', return_value=recommend_allowed):
            policy.route_task(self.config, task, self.store, rng=rng or random.Random(7))
        return task

    def last_log(self):
        return [row for row in read_jsonl(DecisionStore(self.root).path) if row.get('event') == 'routing_log'][-1]

    def test_evidence_pools_per_model_across_lanes_even_ones_filtered_out(self):
        self.config['decisions']['auto_routes'] = ['opus', 'fable-api']
        self.checked('fable', 2)
        self.checked('fable-api', 2, 1)
        self.checked('opus', 3, 1)
        self.route()
        families = {f['lead']: f for f in self.last_log()['sampled']['families']}
        self.assertEqual((families['fable-api']['successes'], families['fable-api']['attempts']), (4, 5))
        self.assertEqual(families['fable-api']['lanes'], ['fable', 'fable-api'])
        self.assertEqual((families['opus']['successes'], families['opus']['attempts']), (3, 4))

    def test_propensities_are_each_models_chance_of_being_best(self):
        self.checked('opus', 82, 58)
        self.checked('fable', 5, 3)
        task = self.route()
        row = self.last_log()
        chances = {c['key']: c['propensity'] for c in row['candidates']}
        self.assertAlmostEqual(sum(chances.values()), 1.0, places=6)
        self.assertAlmostEqual(chances['fable'], .55, delta=.04)
        self.assertEqual(row['chosen'], task['route'])
        self.assertEqual(row['sampled']['chosen'], row['chosen'])
        self.assertEqual(row['policy']['gating_policy'], 'thompson')
        application = [e for e in read_jsonl(DecisionStore(self.root).path) if e.get('event') == 'application'][-1]
        self.assertIn('sampled by model posterior', application['reason'])

    def test_sampling_follows_the_evidence_over_time(self):
        self.checked('opus', 82, 58)
        self.checked('fable', 5, 3)
        rng = random.Random(3)
        picks = [self.route(rng=rng)['route'] for _ in range(60)]
        self.assertTrue(15 < picks.count('fable') < 45, picks.count('fable'))
        self.spans.clear()
        self.checked('opus', 82, 58)
        self.checked('fable', 5, 25)
        picks = [self.route(rng=rng)['route'] for _ in range(30)]
        self.assertEqual(picks.count('fable'), 0)

    def test_each_family_logs_cost_per_accepted_run(self):
        self.checked('fable', 2, 2, cost=5.0)
        self.checked('opus', 3, 0, cost=1.5)
        self.route()
        families = {f['lead']: f for f in self.last_log()['sampled']['families']}
        self.assertEqual(families['fable']['cost_per_accepted'], 10.0)
        self.assertEqual(families['opus']['cost_per_accepted'], 1.5)

    def test_overflow_lanes_stay_out_while_a_primary_survives(self):
        self.config['decisions'].update(auto_routes=['opus', 'fable-api'], overflow_routes=['fable-api'])
        self.checked('fable-api', 5)
        self.checked('opus', 1, 4)
        self.assertEqual(self.route()['route'], 'opus')
        self.assertNotIn('sampled', self.last_log())

    def test_a_qualified_recommendation_still_wins(self):
        self.checked('opus', 1, 9)
        self.checked('fable', 9, 1)
        self.recommend = 'opus'
        self.assertEqual(self.route(recommend_allowed=True)['route'], 'opus')
        row = self.last_log()
        self.assertNotIn('sampled', row)
        self.assertEqual({c['key']: c['propensity'] for c in row['candidates']}, {'opus': 1.0, 'fable': 0.0})

    def test_a_review_samples_only_other_agents_when_one_is_available(self):
        self.config['decisions']['auto_routes'] = ['opus', 'fable', 'other']
        self.checked('other', 1, 5, agent='codex')
        self.checked('fable', 9)
        task = core.make_task(self.root, 'auto', 'fixture', 'review', [], [], None, False, False)
        task['prefer_different_agent'] = 'claude'
        self.assertEqual(self.route(task=task)['route'], 'other')
        self.assertEqual([f['lead'] for f in self.last_log()['sampled']['families']], ['other'])

    def test_read_only_work_and_pinned_routes_are_not_sampled(self):
        self.checked('fable', 3)
        self.route(write=False, role='explore')
        self.assertNotIn('sampled', self.last_log())
        task = core.make_task(self.root, 'auto', 'fixture', 'implementation', [], [], None, False, True)
        task['route'] = 'opus'
        self.route(task=task)
        self.assertEqual(task['route'], 'opus')

    def test_write_trials_are_superseded_by_sampling(self):
        self.config['decisions']['write_trials'] = ['fable']
        self.checked('opus', 9, 1)
        self.route()
        row = self.last_log()
        self.assertNotIn('write_trial', row)
        self.assertIn('sampled', row)

    def test_rank_stays_the_default_and_bad_values_are_refused(self):
        del self.config['decisions']['gating_policy']
        self.checked('opus', 9, 1)
        self.checked('fable', 2)
        self.assertEqual(self.route()['route'], 'opus')
        self.assertNotIn('sampled', self.last_log())
        self.assertEqual(self.last_log()['policy']['gating_policy'], 'rank')
        self.config['decisions']['gating_policy'] = 'greedy'
        with self.assertRaisesRegex(ValueError, 'gating_policy'):
            self.route()


if __name__ == '__main__':
    unittest.main()
