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


if __name__ == '__main__':
    unittest.main()
