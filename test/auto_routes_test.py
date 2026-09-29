"""decisions.auto_routes bounds automatic routing, never a named lane."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_policy as policy
from fusion_decisions import DecisionStore, read_jsonl


class AutoRoutesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex'), 'ORC_HOME': str(self.root / 'orc'),
                                      'FUSION_PROGRESS': '0', 'FUSION_TELEMETRY': '0', 'FUSION_DECISIONS_MODE': 'off'})
        env.start()
        self.addCleanup(env.stop)
        self.store = core.RunStore(self.root)
        self.config = {'sidekick': 'claude', 'decisions': {'mode': 'off', 'priors': False},
                       'routes': {'opus': {'agent': 'claude', 'model': 'claude-opus-5-5', 'cost_tier': 2},
                                  'astra': {'agent': 'codex', 'model': 'gpt-6-astra', 'cost_tier': 1},
                                  'flash': {'agent': 'claude', 'model': 'claude-haiku-4-5', 'cost_tier': 0}}}

    def task(self, agent='auto', route=None):
        task = core.make_task(self.root, agent, 'fixture', 'implementation', [], [], None, False, True)
        if route:
            task['route'] = route
        return task

    def keys(self, task, rejected=None):
        with patch.object(core, 'executable', return_value=True):
            return sorted(c['key'] for c in policy.route_candidates(self.config, task, self.store, rejected=rejected))

    def test_unset_offers_every_lane(self):
        self.assertEqual(self.keys(self.task()), ['astra', 'claude', 'codex', 'flash', 'grok', 'opus'])

    def test_pool_limits_automatic_candidates_and_says_why(self):
        self.config['decisions']['auto_routes'] = ['opus', 'astra']
        rejected = {}
        self.assertEqual(self.keys(self.task(), rejected), ['astra', 'opus'])
        self.assertEqual(rejected['flash'], 'not in decisions.auto_routes')
        self.assertEqual(rejected['claude'], 'not in decisions.auto_routes')

    def test_bare_agents_may_be_pooled(self):
        self.config['decisions']['auto_routes'] = ['codex', 'opus']
        self.assertEqual(self.keys(self.task()), ['codex', 'opus'])

    def test_a_named_lane_is_not_bound(self):
        self.config['decisions']['auto_routes'] = ['opus']
        self.assertIsNone(policy.auto_pool(self.config, self.task('claude')))
        self.assertIsNone(policy.auto_pool(self.config, self.task('auto', route='flash')))

    def test_invalid_pool_is_refused(self):
        for pool in ([], ['nope'], 'opus', [1]):
            self.config['decisions']['auto_routes'] = pool
            with self.assertRaisesRegex(ValueError, 'decisions.auto_routes'):
                policy.auto_pool(self.config, self.task())


    def test_needs_skip_lanes_that_lack_them(self):
        self.config['decisions']['auto_routes'] = ['opus', 'astra']
        self.config['routes']['astra']['lacks'] = ['local_server']
        task = self.task()
        task['needs'] = ['local_server']
        rejected = {}
        self.assertEqual(self.keys(task, rejected), ['opus'])
        self.assertEqual(rejected['astra'], 'lacks local_server this task needs')

    def test_agent_level_lacks_apply_to_its_routes(self):
        self.config['decisions']['auto_routes'] = ['opus', 'astra']
        self.config['codex'] = {'lacks': ['local_server']}
        task = self.task()
        task['needs'] = ['local_server']
        self.assertEqual(self.keys(task), ['opus'])

    def test_needs_do_not_bind_a_named_lane(self):
        self.config['routes']['astra']['lacks'] = ['local_server']
        task = self.task('auto', route='astra')
        task['needs'] = ['local_server']
        self.assertIn('astra', self.keys(task))

    def test_no_lane_meets_the_needs_runs_blind_and_logs_it(self):
        self.config['decisions'].update({'auto_routes': ['opus', 'astra'], 'mode': 'shadow'})
        self.config['routes']['astra']['lacks'] = ['local_server']
        self.config['routes']['opus']['lacks'] = ['local_server']
        task = self.task()
        task['needs'] = ['local_server']
        with patch.dict(os.environ, {'FUSION_DECISIONS_MODE': 'shadow'}), patch.object(core, 'executable', return_value=True):
            policy.route_task(self.config, task, self.store)
        self.assertIn(task['route'], {'opus', 'astra'})
        log = [row for row in read_jsonl(DecisionStore(self.root).path) if row.get('event') == 'routing_log']
        self.assertEqual((log[-1]['needs'], log[-1]['needs_unmet']), (['local_server'], True))

    def test_make_task_normalizes_and_validates_needs(self):
        task = core.make_task(self.root, 'auto', 'x', 'implementation', [], [], None, False, True, needs=['b', 'a', 'b'])
        self.assertEqual(task['needs'], ['a', 'b'])
        self.assertNotIn('needs', core.make_task(self.root, 'auto', 'x', 'implementation', [], [], None, False, True))
        with self.assertRaisesRegex(ValueError, 'needs'):
            core.make_task(self.root, 'auto', 'x', 'implementation', [], [], None, False, True, needs=['Local-Server'])


if __name__ == '__main__':
    unittest.main()
