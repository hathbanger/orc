"""Offline quota capture and routing regressions."""
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
import fusion_usage as usage
from fusion_decisions import DecisionStore, read_jsonl

NOW = 1790500000
WEEK = 7 * 24 * 3600


class QuotaTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex'),
                                     'ORC_HOME': str(self.root / 'orc'),
                                     'FUSION_PROGRESS': '0', 'FUSION_TELEMETRY': '0',
                                     'FUSION_DECISIONS_MODE': 'off'})
        env.start()
        self.addCleanup(env.stop)
        self.store = core.RunStore(self.root)
        self.config = {'sidekick': 'claude', 'decisions': {'mode': 'off', 'priors': False},
                       'routes': {name: {'agent': 'claude', 'account': name} for name in ('A', 'B')}}
        self.task = core.make_task(self.root, 'auto', 'fixture', 'implementation', [], [], None, False, True)

    def quota(self, used, status='allowed', reset=None, name='seven_day'):
        return {'status': status, 'windows': {name: {
            'used': used, 'resets_at': reset if reset is not None else NOW + WEEK / 2}}}

    def trace(self, lane, quota=None, when=NOW, **extra):
        span = {'agent': 'claude', 'lane_key': lane, 'end_time_ms': when * 1000, **extra}
        if quota is not None:
            span['quota'] = quota
        self.store.root.mkdir(parents=True, exist_ok=True)
        with self.store.traces_path.open('a') as stream:
            stream.write(json.dumps(span) + '\n')

    def candidates(self, **kwargs):
        with patch.object(core, 'now_ms', return_value=NOW * 1000), patch.object(core, 'executable', return_value=True):
            return policy.route_candidates(self.config, self.task, self.store, **kwargs)

    def test_stream_final_result_preserves_parser_and_denials(self):
        final = {'type': 'result', 'session_id': 'session', 'result': 'done', 'is_error': True,
                 'usage': {'input_tokens': 20, 'output_tokens': 3}, 'total_cost_usd': .04,
                 'modelUsage': {'claude-fixture': {}},
                 'permission_denials': [{'tool_name': 'Bash', 'tool_input': {'command': 'touch nope'}}]}
        legacy = json.dumps(final)
        stream = '\n'.join(json.dumps(item) for item in [
            {'type': 'system', 'session_id': 'session'}, final, {'type': 'rate_limit_event'}])
        self.assertEqual(core.parse_claude_output(stream), core.parse_claude_output(legacy))
        self.assertEqual(core.provider_denials('claude', stream), core.provider_denials('claude', legacy))

    def test_tokens_cover_the_whole_session_like_the_cost(self):
        # A session that wakes for background work emits a result per wake;
        # the last one's usage covers one short turn, its cost the whole run.
        def result(turn_out, total_out, cache_read, cost):
            return {'type': 'result', 'session_id': 's', 'result': 'done', 'total_cost_usd': cost,
                    'usage': {'input_tokens': 4, 'output_tokens': turn_out, 'cache_read_input_tokens': 251099},
                    'modelUsage': {'claude-opus-5-5': {'inputTokens': 154, 'outputTokens': total_out,
                                                       'cacheReadInputTokens': cache_read,
                                                       'cacheCreationInputTokens': 240747, 'costUSD': cost}}}
        stream = '\n'.join(json.dumps(item) for item in [
            result(85498, 85498, 10141156, 5.6167), result(96, 87751, 11879360, 6.057484)])
        usage = core.parse_claude_output(stream)[3]
        self.assertEqual(usage['cost_usd'], 6.057484)
        self.assertEqual((usage['input_tokens'], usage['output_tokens']), (154, 87751))
        self.assertEqual((usage['cache_read_input_tokens'], usage['cache_creation_input_tokens']), (11879360, 240747))

    def test_account_headroom_demotes_and_expires_rejection(self):
        self.trace('claude@A', self.quota(.9))
        self.trace('claude@B', self.quota(.2))
        keys = [c['key'] for c in self.candidates()]
        self.assertLess(keys.index('B'), keys.index('A'))
        self.trace('claude@A', self.quota(.1, 'rejected'), when=NOW + 1)
        rejected = {}
        self.assertNotIn('A', [c['key'] for c in self.candidates(rejected=rejected)])
        self.assertIn('rejected', rejected['A'])
        with patch.object(core, 'now_ms', return_value=(NOW + WEEK / 2) * 1000), \
             patch.object(core, 'executable', return_value=True):
            after_reset = policy.route_candidates(self.config, self.task, self.store)
        self.assertIn('A', [c['key'] for c in after_reset])

    def test_fake_claude_result_json_parity_including_errors_and_resume(self):
        fixture = self.root / 'output.json'
        agent = self.root / 'fake-claude'
        agent.write_text('#!/usr/bin/env python3\nimport pathlib, sys\n'
                         'assert sys.argv[sys.argv.index("--output-format") + 1] == "stream-json"\n'
                         'assert "--verbose" in sys.argv\n'
                         f'print(pathlib.Path({str(fixture)!r}).read_text())\n')
        agent.chmod(0o755)
        config = {**self.config, 'claude': {'command': str(agent)}, 'timeout_seconds': 10}
        task = core.make_task(self.root, 'claude', 'fixture', 'implementation', [], [], None, True, False, route='A')
        self.store.set_session(task['session_key'], 'prior-session')
        run_dir = self.store.create(task)
        quota_event = {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed',
                       'unifiedWindows': {'five_hour': {'utilization': .4, 'resetsAt': NOW + 3600}}}}
        for error, denials in [(False, []), (True, []), (False, [{'tool_name': 'Bash', 'tool_input': {'command': 'touch nope'}}])]:
            with self.subTest(error=error, denials=denials):
                final = {'type': 'result', 'session_id': 'new-session', 'is_error': error,
                         'result': 'STATUS: success\nSUMMARY: done\nCHANGED: a.py\nTESTS: fixture\nBLOCKERS: none',
                         'usage': {'input_tokens': 8, 'output_tokens': 2, 'cache_read_input_tokens': 10},
                         'modelUsage': {'claude-fixture': {}}, 'total_cost_usd': .025, 'permission_denials': denials}
                outputs = [json.dumps(final, indent=2), '\n'.join(json.dumps(e) for e in [
                    {'type': 'system', 'session_id': 'initial'},
                    {'type': 'result', 'result': 'earlier result'},
                    {**quota_event, 'rate_limit_info': {'status': 'allowed', 'unifiedWindows': {
                        'five_hour': {'utilization': .1, 'resetsAt': NOW + 3600}}}},
                    final, quota_event, {'type': 'system', 'subtype': 'trailing'}])]
                results = []
                for output in outputs:
                    fixture.write_text(output)
                    with patch.object(core, 'now_ms', return_value=NOW * 1000), \
                         patch.object(core.time, 'monotonic', return_value=100), \
                         patch.object(core.RunStore, 'session_last_used', return_value=(NOW - 10) * 1000):
                        results.append(core.dispatch(config, dict(task), self.store, run_dir=run_dir))
                normalized = results[1].pop('quota')
                self.assertEqual(results[0], results[1])
                self.assertTrue(results[1]['resumed'])
                self.assertEqual(results[1]['usage']['cost_usd'], .025)
                self.assertEqual(results[1]['model'], 'claude-fixture')
                self.assertEqual(results[1]['denied_count'], len(denials))
                self.assertEqual(self.store.sessions()[task['session_key']], 'new-session')
                self.assertEqual(normalized['windows']['five_hour']['used'], .4)
                span = self.store.traces(1)[0]
                self.assertEqual(span['quota'], normalized)
                saved = json.loads(Path(results[1]['artifacts']['run_dir'], 'result.json').read_text())
                self.assertEqual(saved, {**results[1], 'quota': normalized})

    def test_codex_exec_and_rollout_quota(self):
        limits = {'primary': {'used_percent': 90, 'window_minutes': 10080, 'resets_at': NOW + WEEK / 2},
                  'secondary': {'used_percent': 20, 'window_minutes': 300, 'resets_at': NOW + 1000}}
        expected = {'status': None, 'windows': {name: {
            'used': w['used_percent'] / 100, 'resets_at': w['resets_at'], 'window_minutes': w['window_minutes']}
            for name, w in limits.items()}}
        for event in [{'type': 'turn.completed', 'rate_limits': limits},
                      {'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': limits}},
                      {'event': {'payload': {'rate_limits': limits}}}]:
            with self.subTest(event=event):
                self.assertEqual(core.parse_quota('codex', json.dumps(event)), expected)
        self.assertIsNone(core.parse_quota('codex', '{broken\n[]\n{}'))
        self.assertIsNone(core.parse_quota('claude', json.dumps({'rate_limits': limits})))

    def test_codex_dispatch_records_quota_without_changing_usage(self):
        agent = self.root / 'fake-codex'
        events = [{'type': 'thread.started', 'thread_id': 'codex-session'},
                  {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}},
                  {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 2},
                   'rate_limits': {'primary': {'used_percent': 20, 'window_minutes': 10080, 'resets_at': NOW + WEEK}}}]
        agent.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n' +
                         '\n'.join(f'print({json.dumps(e)!r})' for e in events) + '\n')
        agent.chmod(0o755)
        config = {**self.config, 'codex': {'command': str(agent)}, 'timeout_seconds': 10}
        task = core.make_task(self.root, 'codex', 'fixture', 'implementation', [], [], None, False, False)
        result = core.dispatch(config, task, self.store)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['usage']['input_tokens'], 10)
        self.assertEqual(result['quota']['windows']['primary']['used'], .2)
        self.assertEqual(self.store.traces(1)[0]['quota'], result['quota'])

    def assessment(self, quota, **thresholds):
        clean = usage.normalize_quota('claude', quota, normalized=True)
        return policy.quota_assessment({'lane_key': 'claude@A', 'quota': clean},
                                       policy.quota_settings({'quota': thresholds}), NOW)

    def test_pace_threshold_boundaries_custom_settings_and_multiple_windows(self):
        cases = [(.65, {}, 'available'), (.65001, {}, 'tight'),
                 (.85, {'pace_margin': 1}, 'available'), (.85001, {'pace_margin': 1}, 'tight'),
                 (.97, {}, 'tight'), (.97001, {}, 'exhausted'),
                 (.9, {'soft': .95, 'hard': .99, 'pace_margin': .5}, 'available')]
        for used, thresholds, expected in cases:
            with self.subTest(used=used, thresholds=thresholds):
                self.assertEqual(self.assessment(self.quota(used), **thresholds)['classification'], expected)
        quota = self.quota(.2)
        quota['windows']['five_hour'] = {'used': .5, 'resets_at': NOW + 5 * 3600}
        result = self.assessment(quota)
        self.assertEqual(result['classification'], 'tight')
        self.assertEqual(result['windows']['five_hour']['elapsed'], 0)
        self.assertIn('pace_margin', result['reasons'][0])
        self.assertEqual(self.assessment(self.quota(.7, name='unknown'))['classification'], 'available')
        self.assertEqual(self.assessment(self.quota(.9, name='unknown'))['classification'], 'tight')
        self.assertEqual(self.assessment(self.quota(1, 'rejected', NOW))['classification'], 'available')
        for value in [-.1, 1.1, True, float('nan'), '0.5']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.quota_settings({'quota': {'soft': value}})

    def test_headroom_latest_timestamp_identity_and_quota_free_traces(self):
        self.trace('claude@A', self.quota(.9), when=NOW - 10)
        self.trace('claude@B', self.quota(.2), when=NOW - 20)
        self.trace('claude@A', when=NOW)
        self.trace('claude@A', self.quota(.1), when=NOW - 30)
        self.trace(None, self.quota(1))
        self.trace('claude@A', {'windows': {'seven_day': {'used': 'bad', 'resets_at': 'bad'}}})
        observed = {e['lane_key']: e for e in usage.headroom(self.root, include_raw=False) if e.get('lane_key')}
        self.assertEqual(set(observed), {'claude@A', 'claude@B'})
        self.assertEqual(observed['claude@A']['quota']['windows']['seven_day']['used'], .9)
        self.assertEqual(observed['claude@B']['quota']['windows']['seven_day']['used'], .2)
        candidates = {c['key']: c for c in self.candidates()}
        self.assertNotIn('quota', candidates['claude'])
        self.assertEqual(candidates['A']['quota']['classification'], 'tight')

    def test_invalid_window_numbers_are_unknown(self):
        for value in [True, -.1, 1.1, float('nan'), float('inf'), '0.9', None]:
            with self.subTest(value=value):
                self.assertIsNone(usage.normalize_quota('claude', {'unifiedWindows': {
                    'five_hour': {'utilization': value, 'resetsAt': 'invalid'}}}))
        # A reset without utilization still carries a valid rejection.
        result = self.assessment(self.quota(None, 'rejected'))
        self.assertEqual(result['classification'], 'exhausted')

    def test_no_quota_order_unchanged_and_outcomes_cannot_undo_demotion(self):
        self.assertEqual([c['key'] for c in self.candidates()], ['claude', 'codex', 'grok', 'A', 'B'])
        candidates = [{'key': 'A', 'checked_runs': 20, 'acceptance_rate': 1, 'warm': True},
                      {'key': 'B', 'checked_runs': 3, 'acceptance_rate': .5, 'warm': False}]
        self.assertEqual(policy.rank_by_outcomes(candidates, explore=False)[0]['key'], 'A')
        candidates[0]['quota'] = {'classification': 'tight'}
        for warmth in (None, .8):
            self.assertEqual(policy.rank_by_outcomes(candidates, warm_epsilon=warmth, explore=False)[0]['key'], 'B')

    def test_routing_log_and_report_explain_quota_without_outcomes(self):
        self.trace('claude@A', self.quota(.9))
        self.trace('claude@B', self.quota(.1, 'rejected'))
        with patch.object(core, 'now_ms', return_value=NOW * 1000), patch.object(core, 'executable', return_value=True):
            policy.route_task(self.config, self.task, self.store)
        logs = [e for e in read_jsonl(DecisionStore(self.root).path) if e.get('event') == 'routing_log']
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['quota']['A']['classification'], 'tight')
        self.assertEqual(logs[0]['quota']['B']['classification'], 'exhausted')
        self.assertIn('rejected', logs[0]['rejected']['B'])
        self.assertNotIn('B', [c['key'] for c in logs[0]['candidates']])
        report = policy.routing_report(logs)
        self.assertEqual(report['with_outcome'], 0)
        self.assertEqual(report['quota_decisions'][0]['quota'], logs[0]['quota'])
        self.assertEqual(report['lanes'], [])

    def test_rejection_expires_with_binding_window(self):
        thresholds = policy.quota_settings({})
        quota = {'status': 'rejected', 'windows': {'five_hour': {'used': 1.0, 'resets_at': NOW + 60},
                                                   'seven_day': {'used': .47, 'resets_at': NOW + WEEK / 2}}}
        observation = {'lane_key': 'claude@A', 'quota': quota}
        self.assertEqual(policy.quota_assessment(observation, thresholds, NOW)['classification'], 'exhausted')
        self.assertEqual(policy.quota_assessment(observation, thresholds, NOW + 61)['classification'], 'available')

    def test_explicit_route_is_retained(self):
        self.trace('claude@A', self.quota(1, 'rejected'))
        task = {**self.task, 'route': 'A'}
        policy.route_task(self.config, task, self.store)
        self.assertEqual((task['agent'], task['route']), ('claude', 'A'))


if __name__ == '__main__':
    unittest.main()
