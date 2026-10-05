"""A newer quota reading with fewer windows does not erase an older window that still binds (#191)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_policy as policy
import fusion_quota
import fusion_usage as usage

NOW = 1790500000
HOUR = 3600
WEEK = 7 * 24 * HOUR


class QuotaWindowMergeTest(unittest.TestCase):
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
                       'routes': {'A': {'agent': 'claude', 'account': 'A'}}}
        self.task = core.make_task(self.root, 'auto', 'fixture', 'implementation', [], [], None, False, True)

    def trace(self, windows, when, status='allowed'):
        span = {'agent': 'claude', 'lane_key': 'claude@A', 'end_time_ms': when * 1000,
                'quota': {'status': status, 'windows': windows}}
        self.store.root.mkdir(parents=True, exist_ok=True)
        with self.store.traces_path.open('a') as stream:
            stream.write(json.dumps(span) + '\n')

    def candidates(self, now=NOW):
        rejected = {}
        with patch.object(core, 'now_ms', return_value=now * 1000), patch.object(core, 'executable', return_value=True), \
                patch('time.time', return_value=now):
            keys = [c['key'] for c in policy.route_candidates(self.config, self.task, self.store, rejected=rejected)]
        return keys, rejected

    def two_readings(self, weekly_reset):
        self.trace({'five_hour': {'used': .10, 'resets_at': NOW + HOUR}, 'seven_day': {'used': .99, 'resets_at': weekly_reset}},
                   when=NOW - 600)
        self.trace({'five_hour': {'used': .12, 'resets_at': NOW + HOUR}}, when=NOW - 60)

    def test_an_older_binding_window_still_excludes_the_lane(self):
        self.two_readings(NOW + WEEK / 2)
        keys, rejected = self.candidates()
        self.assertNotIn('A', keys)
        self.assertIn('seven_day', rejected['A'])

    def test_an_older_window_past_its_reset_no_longer_binds(self):
        self.two_readings(NOW - 1)
        keys, _ = self.candidates()
        self.assertIn('A', keys)

    def test_quota_lists_every_window_with_its_own_reading_time(self):
        self.two_readings(NOW + WEEK / 2)
        [account] = [a for a in fusion_quota.status(self.config, self.store, now=NOW)['accounts'] if a['lane_key'] == 'claude@A']
        self.assertEqual(set(account['windows']), {'five_hour', 'seven_day'})
        self.assertEqual(account['classification'], 'exhausted')
        self.assertEqual(account['windows']['five_hour']['observed_at'], usage.iso(usage.timestamp((NOW - 60) * 1000)))
        self.assertEqual(account['windows']['seven_day']['observed_at'], usage.iso(usage.timestamp((NOW - 600) * 1000)))

    def test_a_window_seen_only_before_keeps_its_older_reading_and_the_newest_status_wins(self):
        self.trace({'seven_day': {'used': .99, 'resets_at': NOW + WEEK / 2}}, when=NOW - 600, status='rejected')
        self.trace({'five_hour': {'used': .12, 'resets_at': NOW + HOUR}}, when=NOW - 60)
        [entry] = [e for e in usage.headroom(self.root, include_raw=False) if e.get('lane_key') == 'claude@A']
        self.assertEqual(entry['quota']['status'], 'allowed')
        self.assertEqual(entry['quota']['windows']['seven_day']['observed_at'], usage.iso(usage.timestamp((NOW - 600) * 1000)))
        self.assertEqual(entry['observed_at'], usage.iso(usage.timestamp((NOW - 60) * 1000)))
        self.assertEqual(entry['quota']['windows']['seven_day']['used'], .99)


if __name__ == '__main__':
    unittest.main()
