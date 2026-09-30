"""Synthetic provider transcripts exercise offline usage accounting end to end."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fusion_core
import fusion_usage as usage

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'repo'
        self.workspace.mkdir()
        self.claude = self.root / 'claude'
        self.codex = self.root / 'codex'
        env = patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.claude), 'CODEX_HOME': str(self.codex),
                                     'FUSION_PROGRESS': '0', 'FUSION_TELEMETRY': '0'})
        env.start()
        self.addCleanup(env.stop)

    def write(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
        return path

    def claude_message(self, ident, hours=1, input_tokens=10, read=20, write=5, output=2, **extra):
        return {'type': 'assistant', 'timestamp': usage.iso(NOW - timedelta(hours=hours)),
                'requestId': 'req-' + ident,
                'message': {'id': ident, 'model': 'claude-model', 'usage': {
                    'input_tokens': input_tokens, 'cache_read_input_tokens': read,
                    'cache_creation_input_tokens': write, 'output_tokens': output}}, **extra}

    def codex_event(self, hours, incoming, cached, output, **extra):
        return {'type': 'event_msg', 'timestamp': usage.iso(NOW - timedelta(hours=hours)), 'payload': {
            'type': 'token_count', 'info': {'total_token_usage': {
                'input_tokens': incoming, 'cached_input_tokens': cached, 'output_tokens': output,
                'reasoning_output_tokens': output, 'total_tokens': incoming + output}}, **extra}}

    def fixtures(self):
        a = self.claude_message('a')
        streamed = self.claude_message('a', output=4)
        request_only = self.claude_message('a', output=4)
        request_only['message'].pop('id')
        self.write(self.claude / 'projects' / 'project' / 'session.jsonl',
                   [a, streamed, request_only, self.claude_message('b', input_tokens=30)])
        self.write(self.codex / 'sessions' / '2026' / 'rollout.jsonl', [
            {'type': 'session_meta', 'payload': {'cwd': '/project', 'id': 'codex-session'}},
            {'type': 'turn_context', 'payload': {'model': 'codex-model'}},
            self.codex_event(2, 100, 40, 10), self.codex_event(1, 150, 60, 15),
            self.codex_event(1, 150, 60, 15)])
        self.write(self.workspace / '.fusion' / 'traces.jsonl', [{
            'span_id': 'span', 'run_id': 'run', 'end_time_ms': int((NOW - timedelta(hours=1)).timestamp() * 1000),
            'agent': 'codex', 'route': 'cheap', 'model': 'worker-model', 'session_key': 'worker-session',
            'lane_key': 'codex@worker', 'usage': {'input_tokens': 80, 'cached_input_tokens': 20,
                                               'output_tokens': 3, 'cost_usd': 0.01}}])

    def report(self, **kwargs):
        return usage.report(self.workspace, now=NOW, **kwargs)

    def test_exact_totals_and_grouping(self):
        self.fixtures()
        report = self.report()
        self.assertEqual(report['total']['calls'], 5)
        self.assertEqual([report['total'][key] for key in usage.TOKEN_FIELDS], [190, 120, 10, 24])
        self.assertEqual(report['total']['context_tokens'], 320)
        self.assertEqual(report['total']['average_context_per_call'], 64)
        self.assertEqual(report['total']['calls_per_hour'], 5 / 24)
        self.assertEqual(report['total']['cost_usd'], .01)
        for by in usage.GROUPS:
            with self.subTest(by=by):
                result = self.report(by=by)
                self.assertEqual(result['total'], report['total'])
                for field in ('calls', *usage.TOKEN_FIELDS):
                    self.assertEqual(sum(group[field] for group in result['groups']), report['total'][field])
        models = {g['key']: g for g in self.report(by='model')['groups']}
        self.assertEqual([models['claude-model'][k] for k in usage.TOKEN_FIELDS], [40, 40, 10, 6])
        self.assertEqual([models['codex-model'][k] for k in usage.TOKEN_FIELDS], [90, 60, 0, 15])
        worker = models['worker-model']
        self.assertEqual(worker['routes'], ['cheap'])
        self.assertEqual(worker['lane_keys'], ['codex@worker'])
        self.assertEqual(worker['session_keys'], ['worker-session'])
        self.assertEqual(worker['sources'], ['orc'])

    def test_since_boundary_and_codex_baseline(self):
        self.write(self.codex / 'sessions' / 'r.jsonl', [
            self.codex_event(25, 100, 40, 10), self.codex_event(24, 150, 60, 15),
            self.codex_event(23, 150, 60, 15), self.codex_event(-1, 200, 80, 20)])
        result = self.report()
        self.assertEqual(result['total']['calls'], 1)
        self.assertEqual([result['total'][key] for key in usage.TOKEN_FIELDS], [30, 20, 0, 5])
        self.assertEqual(self.report(since='2026-09-26T12:00:00')['total'], result['total'])
        self.assertEqual(self.report(since='2026-09-26T14:00:00+02:00')['total'], result['total'])
        self.assertEqual(self.report(since='7d')['total']['input_tokens'], 90)

    def test_dedup_is_per_session_and_partial_stream_keeps_usage(self):
        a = self.claude_message('same')
        final = self.claude_message('same')
        final['message']['usage'] = {'output_tokens': 8}
        for name in ('one', 'two'):
            self.write(self.claude / 'projects' / 'p' / (name + '.jsonl'), [a, final])
        total = self.report()['total']
        self.assertEqual(total['calls'], 2)
        self.assertEqual([total[key] for key in usage.TOKEN_FIELDS], [20, 40, 10, 16])

    def test_stream_alias_bridge_preserves_earlier_token_fields(self):
        first = self.claude_message('a', hours=3, output=2)
        first.pop('requestId')
        second = self.claude_message('b', hours=2, input_tokens=30, output=8)
        second['message'].pop('id')
        bridge = self.claude_message('a')
        bridge['requestId'] = 'req-b'
        bridge['message']['usage'] = {'output_tokens': 4}
        self.write(self.claude / 'projects' / 'p' / 's.jsonl', [first, second, bridge])
        total = self.report()['total']
        self.assertEqual(total['calls'], 1)
        self.assertEqual([total[key] for key in usage.TOKEN_FIELDS], [30, 20, 5, 8])
        self.assertEqual(usage.read_claude()[0]['timestamp'], NOW - timedelta(hours=3))

    def test_malformed_codex_counter_does_not_reset_baseline(self):
        broken = self.codex_event(2, 0, 0, 0)
        broken['payload']['info']['total_token_usage'] = {}
        self.write(self.codex / 'sessions' / 'r.jsonl', [
            self.codex_event(3, 100, 40, 10), broken, self.codex_event(1, 100, 40, 10)])
        total = self.report()['total']
        self.assertEqual(total['calls'], 1)
        self.assertEqual(total['context_tokens'], 100)

    def test_coordinator_classification_precedes_grouping_and_top(self):
        self.write(self.claude / 'projects' / 'p' / 'coordinator.jsonl',
                   [self.claude_message(str(i), input_tokens=10000, read=480000, write=10000) for i in range(100)])
        self.write(self.claude / 'projects' / 'p' / 'worker.jsonl',
                   [self.claude_message('worker', input_tokens=50000, read=0, write=0)])
        result = self.report()
        self.assertTrue(result['groups'][0]['coordinator'])
        self.assertEqual(result['groups'][0]['average_context_per_call'], 500000)
        self.assertFalse(result['groups'][1]['coordinator'])
        combined = self.report(by='model')
        self.assertEqual(len(combined['groups']), 1)
        self.assertEqual(len(combined['groups'][0]['coordinator_sessions']), 1)
        top = self.report(top=1)
        self.assertEqual(len(top['groups']), 1)
        self.assertEqual(top['group_count'], 2)
        self.assertEqual(top['total'], result['total'])
        equal = self.report(context_threshold=500000, calls_per_hour_threshold=100 / 24)
        self.assertFalse(any(g['coordinator'] for g in equal['groups']))
        rate = self.report(context_threshold=1000000, calls_per_hour_threshold=4)
        self.assertTrue(rate['groups'][0]['coordinator'])
        self.assertFalse(rate['groups'][1]['coordinator'])

    def test_model_changes_and_unknown_models(self):
        self.write(self.codex / 'sessions' / 'r.jsonl', [self.codex_event(3, 100, 40, 10),
            {'type': 'turn_context', 'payload': {'model': 'new'}}, self.codex_event(2, 150, 60, 15)])
        models = {g['key']: g for g in self.report(by='model')['groups']}
        self.assertEqual(models['unknown']['input_tokens'], 60)
        self.assertEqual(models['new']['input_tokens'], 30)

    def test_missing_malformed_and_partial_files(self):
        empty = self.report()
        self.assertEqual(empty['total']['calls'], 0)
        self.assertIsNone(empty['total']['cost_usd'])
        self.assertEqual([item['status'] for item in empty['headroom']], ['unknown', 'unknown'])
        self.assertTrue(all(all(window is None for window in item['windows'].values()) for item in empty['headroom']))
        path = self.write(self.claude / 'projects' / 'p' / 's.jsonl', [None, [], {'message': []},
                         self.claude_message('valid'), {'type': 'assistant', 'message': {'usage': []}}])
        with path.open('a') as stream:
            stream.write('not json\n{"unfinished":\n')
            stream.write(json.dumps(self.claude_message('valid-two')) + '\n')
        self.write(self.codex / 'sessions' / 's.jsonl', [{'payload': []}, self.codex_event(1, 0, 0, 0)])
        self.write(self.workspace / '.fusion' / 'traces.jsonl', [{'usage': [], 'end_time_ms': 4}])
        self.assertEqual(self.report()['total']['calls'], 2)

    def test_orc_duplicate_spans_and_limit(self):
        self.fixtures()
        path = self.workspace / '.fusion' / 'traces.jsonl'
        span = json.loads(path.read_text())
        another = {**span, 'span_id': 'another', 'session_key': 'another-session'}
        self.write(path, [span, span, another])
        self.assertEqual(self.report()['sources']['orc']['calls'], 2)
        self.assertEqual(self.report(limit=1)['sources']['orc']['calls'], 1)

    def test_codex_and_claude_headroom_latest_windows_per_account(self):
        def quota(hours, account, **windows):
            return {'timestamp': usage.iso(NOW - timedelta(hours=hours)), 'type': 'event_msg',
                    'payload': {'type': 'token_count', 'account_id': account, 'rate_limits': windows}}
        self.write(self.codex / 'sessions' / 'r.jsonl', [
            quota(30, 'a', primary={'used_percent': 80, 'window_minutes': 300, 'resets_at': 123}),
            quota(40, 'a', primary={'used_percent': 10}, secondary={'used_percent': 20}),
            quota(29, 'b', primary={'used_percent': 0})])
        def claude_quota(hours, **windows):
            return {'type': 'rate_limit_event', 'timestamp': usage.iso(NOW - timedelta(hours=hours)),
                    'rate_limit_info': {'unifiedWindows': windows}}
        for run_name, account in [('one', 'a'), ('two', 'b')]:
            run = self.workspace / '.fusion' / 'runs' / run_name
            self.write(run / 'stdout.log', [
                claude_quota(30, five_hour={'utilization': .7, 'resetsAt': 123}),
                claude_quota(40, five_hour={'utilization': .2}, seven_day={'utilization': .1})])
            (run / 'task.json').write_text(json.dumps({'account': account}))
        accounts = {(a['provider'], a['account']): a for a in self.report()['headroom']}
        self.assertEqual(len(accounts), 4)
        self.assertEqual(accounts['codex', 'a']['windows']['primary']['used_percent'], 80)
        self.assertEqual(accounts['codex', 'a']['windows']['secondary']['used_percent'], 20)
        self.assertIsNone(accounts['codex', 'b']['windows']['secondary'])
        self.assertEqual(accounts['codex', 'b']['windows']['primary']['used_percent'], 0)
        self.assertEqual(accounts['claude', 'a']['windows']['five_hour']['utilization'], .7)
        self.assertEqual(accounts['claude', 'b']['windows']['seven_day']['utilization'], .1)
        self.assertEqual(self.report()['total']['calls'], 0)  # quota is independent of usage window

    def test_headroom_lane_metadata_wrapper_and_unknown_identity(self):
        run = self.workspace / '.fusion' / 'runs' / 'worker'
        self.write(run / 'events.jsonl', [{'ts': int(NOW.timestamp() * 1000), 'event': {
            'type': 'rate_limit_event', 'rate_limit_info': {'unifiedWindows': {
                'five_hour': {'utilization': .5, 'resetsAt': 'later'}}}}}])
        (run / 'trace.json').write_text(json.dumps({'lane_key': 'claude@work'}))
        self.write(self.codex / 'sessions' / 'r.jsonl', [self.codex_event(1, 0, 0, 0,
                   rate_limits={'primary': {'used_percent': 12}})])
        accounts = {(a['provider'], a['account']): a for a in usage.headroom(self.workspace)}
        self.assertIn(('claude', 'claude@work'), accounts)
        self.assertIn(('codex', None), accounts)

    def test_unidentified_rollout_windows_join_the_only_identified_account(self):
        run = self.workspace / '.fusion' / 'runs' / 'worker'
        run.mkdir(parents=True)
        (run / 'trace.json').write_text(json.dumps({
            'agent': 'codex', 'lane_key': 'codex', 'end_time_ms': int((NOW - timedelta(hours=2)).timestamp() * 1000),
            'quota': {'windows': {'primary': {'used': .5, 'resets_at': 200}}}}))
        self.write(self.codex / 'sessions' / 'r.jsonl', [{'timestamp': usage.iso(NOW - timedelta(hours=1)), 'type': 'event_msg',
                   'payload': {'type': 'token_count', 'rate_limits': {'primary': {'used_percent': 100, 'resets_at': 300}}}}])
        codex = [a for a in usage.headroom(self.workspace) if a['provider'] == 'codex']
        self.assertEqual([a['account'] for a in codex], ['codex'])
        self.assertEqual(codex[0]['windows']['primary']['used_percent'], 100)

    def snapshot_files(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = fusion_core.main(['--workspace', str(self.workspace), *args])
        self.assertEqual(result, 0)
        return output.getvalue()

    def test_cli_json_positions_no_network_no_state_and_text(self):
        self.fixtures()
        before = self.snapshot_files()
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
             patch.object(fusion_core, 'load_config', side_effect=AssertionError('usage needs no config')):
            for args in [('--json', 'usage'), ('usage', '--json')]:
                result = json.loads(self.cli(*args, '--since', '2026-09-20', '--by', 'model', '--top', '1'))
                self.assertEqual(result['schema'], 'fusion.usage.v1')
                self.assertEqual(result['total']['calls'], 5)
                self.assertEqual(len(result['groups']), 1)
            rendered = self.cli('usage', '--since', '2026-09-20')
            self.assertIn('Headroom', rendered)
            self.assertIn('CACHE-READ', rendered)
        self.assertEqual(before, self.snapshot_files())

    def test_cli_invalid_inputs_write_nothing(self):
        for option, value in [('--since', 'bad'), ('--since', '0h'), ('--since', '3000-01-01'),
                              ('--top', '0'), ('--limit', '-1'), ('--context-threshold', '-1'),
                              ('--calls-per-hour-threshold', 'nan'), ('--by', 'route')]:
            with self.subTest(option=option, value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    self.cli('usage', '--record', option, value)
                self.assertEqual(caught.exception.code, 2)
                self.assertFalse((self.workspace / '.fusion').exists())

    def test_daily_record_is_idempotent_complete_and_survives_rotation(self):
        self.fixtures()
        before = self.snapshot_files()
        first = json.loads(self.cli('usage', '--since', '2026-09-20', '--top', '1', '--record', '--json'))
        path = self.workspace / '.fusion' / 'usage.jsonl'
        saved = path.read_bytes()
        snapshot = json.loads(saved)
        self.assertEqual(len(first['groups']), 1)
        self.assertEqual(len(snapshot['groups']), 3)
        after = self.snapshot_files()
        self.assertEqual(set(after) - set(before), {'repo/.fusion/usage.jsonl'})
        for key in before:
            self.assertEqual(before[key], after[key])
        for source in (self.claude, self.codex):
            for file in source.rglob('*.jsonl'):
                file.unlink()
        self.cli('usage', '--record', '--json')
        self.assertEqual(path.read_bytes(), saved)
        next_day = {**snapshot, 'generated_at': usage.iso(datetime.now(timezone.utc) + timedelta(days=1))}
        next_day.pop('date')
        self.assertTrue(usage.record_daily(self.workspace, next_day))
        self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_deterministic_ranking_and_token_normalization(self):
        for name in ('z', 'a'):
            self.write(self.claude / 'projects' / 'p' / (name + '.jsonl'), [self.claude_message('same')])
        self.assertTrue(self.report()['groups'][0]['key'].endswith('/a.jsonl'))
        self.assertEqual(usage.tokens({'input_tokens': 100, 'cached_input_tokens': 40,
            'cache_write_input_tokens': 10, 'output_tokens': 20, 'reasoning_output_tokens': 15}),
            dict(zip(usage.TOKEN_FIELDS, [50, 40, 10, 20])))
        self.assertEqual(usage.tokens({'input_tokens': 1, 'cache_creation': {
            'ephemeral_5m_input_tokens': 2, 'ephemeral_1h_input_tokens': 3}})['cache_creation_input_tokens'], 5)


    def test_dispatched_runs_record_lane_key_for_headroom_and_orc_rows(self):
        agent = self.root / 'claude-fake'
        agent.write_text("""#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'unifiedWindows': {
    'five_hour': {'utilization': 0.4, 'resetsAt': 1790560000}}}}))
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 's',
    'usage': {'input_tokens': 7, 'output_tokens': 1},
    'result': 'STATUS: success\\nSUMMARY: done\\nCHANGED: none\\nTESTS: none\\nBLOCKERS: none'}))
""")
        agent.chmod(0o755)
        config = {'claude': {'command': str(agent)}, 'routes': {'work': {'agent': 'claude', 'account': 'work'}},
                  'timeout_seconds': 30}
        task = fusion_core.make_task(self.workspace, 'claude', 'probe', 'implementation', [], [], None,
                                     False, False, route='work')
        with patch.dict(os.environ, {'FUSION_DECISIONS_MODE': 'off', 'ORC_HOME': str(self.root / 'orc-home')}):
            fusion_core.dispatch(config, task, fusion_core.RunStore(self.workspace))
        span = fusion_core.RunStore(self.workspace).traces(limit=1)[0]
        self.assertEqual(span['lane_key'], 'claude@work')
        self.assertEqual([row['lane_key'] for row in usage.read_orc(self.workspace)], ['claude@work'])
        claude = [entry for entry in usage.headroom(self.workspace) if entry['provider'] == 'claude']
        self.assertEqual([(entry['account'], entry['status']) for entry in claude], [('claude@work', 'known')])
        self.assertEqual(claude[0]['windows']['five_hour']['utilization'], 0.4)


if __name__ == '__main__':
    unittest.main()
