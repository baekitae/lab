"""Contract tests use explicit transport fixtures; they do NOT verify live Gemini access."""
import copy
import io
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path

from ai_league import AILeague, Config, MeetingError, ROLES, config_status, validate_report
from league import League
from providers import GeminiAdapter, ProviderError, Report
from test_league import sample, extended


def configured_env(**updates):
    result = {f'PL_{role}_API_KEY': f'test-only-{role}' for role in ROLES}
    result.update(PL_GEMINI_MODEL='gemini-test-model', PL_INPUT_KRW_PER_MILLION='100', PL_OUTPUT_KRW_PER_MILLION='200')
    result.update(updates)
    return result


def fixture_report(snapshot, team, manager=False):
    row = snapshot['rows'][-1]
    report = {'summary': '입력 자료 기반 시험 보고서', 'uncertainty': '미래 수익은 확인되지 않음',
              'counterarguments': ['추세가 반전될 수 있음'], 'decision': 'approve',
              'evidence': [{'date': row['date'], 'field': 'close', 'value': row['close']}]}
    if team:
        report.update(quantity=1, cancel_if='가격 근거 불일치', forecasts=[
            {'horizon': horizon, 'direction': 'up', 'probability_up': .6,
             'target_low': row['close'] * .9, 'target_high': row['close'] * 1.1} for horizon in [1, 5, 10, 20]])
    if manager:
        report['comparisons'] = [{'team': t, 'incumbent_view': '정규 판단', 'candidate_view': '후보 판단',
            'candidate_quantity': 0, 'preferred': 'defer', 'reason': '표본 부족으로 유예',
            'candidate_cancel_if':'자료 불일치','candidate_forecasts':fixture_report(snapshot,True)['forecasts']} for t in 'ABCD']
    return report


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.fail_at = None
        self.fail_code = 'authentication'
        self.retryable = False
        self.bad_price = False
        self.estimate = 10

    def factory(self, role, config):
        owner = self
        class Adapter:
            def estimate_cost(self, *args):
                return owner.estimate
            def generate_structured_report(self, system, context, schema):
                owner.calls.append({'role': role.role, 'context': copy.deepcopy(context)})
                if owner.fail_at == len(owner.calls):
                    raise ProviderError(owner.fail_code, 'Explicit test transport failure', owner.retryable)
                result = fixture_report(context['market'], role.role.startswith('TEAM_'), role.role == 'MANAGER_EVAL')
                if owner.bad_price:
                    result['evidence'][0]['value'] += 1
                return Report(result, 200, 100, 'fixture-model', True)
        return Adapter()


class ConfigurationTests(unittest.TestCase):
    def test_requires_keys_model_and_explicit_pricing_without_leaking_keys(self):
        env = configured_env()
        config = Config.from_env(env)
        self.assertNotIn('test-only-TEAM_A', repr(config))
        status = config_status(env)
        self.assertTrue(status['ready'])
        for role in ROLES:
            self.assertNotIn(env[f'PL_{role}_API_KEY'], json.dumps(status))
        for key in ('PL_TEAM_A_API_KEY', 'PL_GEMINI_MODEL', 'PL_INPUT_KRW_PER_MILLION'):
            changed = dict(env); changed.pop(key)
            self.assertFalse(config_status(changed)['ready'])

    def test_duplicate_keys_invalid_model_and_budget_rejected(self):
        for updates in [
            {'PL_TEAM_B_API_KEY': 'test-only-TEAM_A'}, {'PL_GEMINI_MODEL': '../bad'},
            {'PL_MONTHLY_BUDGET_KRW': '20001'}, {'PL_OUTPUT_KRW_PER_MILLION': 'NaN'},
            {'PL_LOCAL_RPD': '0'}, {'PL_INPUT_KRW_PER_MILLION': '-1'}]:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                Config.from_env(configured_env(**updates))
        self.assertEqual(Config.from_env(configured_env(PL_INPUT_KRW_PER_MILLION='0', PL_OUTPUT_KRW_PER_MILLION='0')).input_rate, 0)


class AdapterTests(unittest.TestCase):
    def test_rest_contract_and_thinking_usage(self):
        requests = []
        payload = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '{"ok":true}'}]}}],
                   'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 9, 'thoughtsTokenCount': 5}, 'modelVersion': 'version-1'}
        def opener(req, timeout):
            requests.append(req)
            return io.BytesIO(json.dumps(payload).encode())
        adapter = GeminiAdapter('secret-test', 'gemini-test', 100, 200, opener)
        report = adapter.generate_structured_report('system', {'market': 'test'}, {'type': 'OBJECT'})
        request = requests[0]
        self.assertEqual(request.full_url, 'https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent')
        self.assertEqual(request.get_header('X-goog-api-key'), 'secret-test')
        self.assertNotIn('secret-test', request.full_url)
        self.assertNotIn('secret-test', request.data.decode())
        self.assertEqual(json.loads(request.data)['generationConfig']['responseMimeType'], 'application/json')
        self.assertEqual(report.data, {'ok': True})
        self.assertEqual(report.output_tokens, 14)
        self.assertTrue(report.usage_known)
        self.assertGreater(adapter.estimate_cost('system', {}, {}), 0)

    def test_error_body_never_leaks_secret_and_no_silent_retry(self):
        calls = []
        def opener(req, timeout):
            calls.append(req)
            raise urllib.error.HTTPError(req.full_url, 403, 'denied', {}, io.BytesIO(b'secret-test'))
        adapter = GeminiAdapter('secret-test', 'gemini-test', 0, 0, opener)
        with self.assertRaises(ProviderError) as ctx:
            adapter.generate_structured_report('system', {}, {})
        self.assertEqual(ctx.exception.code, 'access_denied')
        self.assertNotIn('secret-test', str(ctx.exception))
        self.assertEqual(len(calls), 1)

    def test_incomplete_response_and_missing_usage(self):
        payload = {'candidates': [{'finishReason': 'MAX_TOKENS', 'content': {'parts': []}}]}
        adapter = GeminiAdapter('test', 'gemini-test', 0, 0, lambda *a, **kw: io.BytesIO(json.dumps(payload).encode()))
        with self.assertRaises(ProviderError):
            adapter.generate_structured_report('sys', {}, {})
        payload['candidates'][0] = {'finishReason': 'STOP', 'content': {'parts': [{'text': '{}'}]}}
        self.assertFalse(adapter.generate_structured_report('sys', {}, {}).usage_known)

    def test_model_list_pagination_and_health_check(self):
        def opener(req, timeout):
            if 'pageToken' not in req.full_url:
                data = {'models': [{'name': 'models/not-generate', 'supportedGenerationMethods': ['embedContent']}], 'nextPageToken': 'next/token'}
            else:
                self.assertIn('next%2Ftoken', req.full_url)
                data = {'models': [{'name': 'models/gemini-test', 'supportedGenerationMethods': ['generateContent']}]}
            return io.BytesIO(json.dumps(data).encode())
        adapter = GeminiAdapter('test', 'gemini-test', 0, 0, opener)
        self.assertEqual(adapter.list_models(), ['gemini-test'])
        self.assertTrue(adapter.health_check()['available'])


class MeetingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.league = League(self.path / 'research.sqlite3')
        self.key = self.league.import_snapshot(sample())['id']
        self.transport = FakeTransport()
        self.config = Config.from_env(configured_env())
        self.delays = []
        self.ai = AILeague(self.path / 'ai.sqlite3', self.league, lambda: self.config,
                           self.transport.factory, self.delays.append)

    def tearDown(self):
        self.ai.executor.shutdown(wait=True)
        self.temp.cleanup()

    def test_full_pipeline_blind_drafts_visibility_gates_and_persistence(self):
        result = self.ai.start(self.key, inline=True)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(len(self.transport.calls), 12)
        self.assertTrue(all(s['state'] == 'completed' for s in result['steps']))
        self.assertEqual(result['result']['decision'], 'hold')
        self.assertEqual(result['result']['ai_director_decision'], 'approve')
        for call in self.transport.calls[:4]:
            self.assertEqual(call['context']['visible_reports'], {})
            self.assertNotIn('key', json.dumps(call['context']))
        for call in self.transport.calls[4:8]:
            self.assertEqual(set(call['context']['visible_reports']), {'draft_A', 'draft_B', 'draft_C', 'draft_D'})
        self.assertEqual(set(self.transport.calls[8]['context']['visible_reports']),
                         {'draft_A', 'draft_B', 'draft_C', 'draft_D', 'debate_A', 'debate_B', 'debate_C', 'debate_D'})
        for call in self.transport.calls[9:]:
            self.assertTrue(all(not k.startswith('draft_') for k in call['context']['visible_reports']))
        self.assertTrue(result['audit_valid'])
        self.assertEqual(self.ai.start(self.key, inline=True)['id'], result['id'])
        self.assertEqual(len(self.transport.calls), 12)
        reopened = AILeague(self.path / 'ai.sqlite3', self.league, lambda: self.config, self.transport.factory)
        self.assertEqual(reopened.get_run(result['id']), result)
        reopened.executor.shutdown()

    def test_failure_preserves_completed_stages_and_no_key_rotation(self):
        self.transport.fail_at = 3
        result = self.ai.start(self.key, inline=True)
        self.assertEqual(result['state'], 'held')
        self.assertEqual(sum(s['state'] == 'completed' for s in result['steps']), 2)
        self.assertEqual([c['role'] for c in self.transport.calls], ['TEAM_A', 'TEAM_B', 'TEAM_C'])
        self.assertEqual(len(result['result']['unrun_steps']), 9)
        self.assertEqual(len(result['result']['incomplete_steps']), 10)
        self.assertEqual(result['steps'][2]['state'], 'held')
        self.assertEqual(self.delays, [])
        self.assertGreater(result['reserved_or_estimated_krw'], 0)

    def test_rate_limit_backoff_same_role_and_total_request_cap(self):
        self.config = replace(self.config, local_rpm=4)  # Isolate global request budget from the independently tested RPM cap.
        self.transport.fail_at = 1; self.transport.fail_code = 'rate_limit'; self.transport.retryable = True
        result = self.ai.start(self.key, inline=True)
        self.assertEqual(self.delays, [2])
        self.assertEqual([c['role'] for c in self.transport.calls[:2]], ['TEAM_A', 'TEAM_A'])
        self.assertEqual(len(self.transport.calls), 12)
        self.assertEqual(result['state'], 'held')
        self.assertEqual(result['steps'][-1]['state'], 'held')

    def test_bad_numeric_citation_held(self):
        self.transport.bad_price = True
        result = self.ai.start(self.key, inline=True)
        self.assertEqual(result['state'], 'held')
        self.assertIn('원본 스냅샷', result['result']['reason'])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(result['steps'][0]['state'], 'held')
        self.assertTrue(all(s['state'] == 'unrun' for s in result['steps'][1:]))

    def test_budget_blocks_before_network(self):
        self.transport.estimate = 20001
        result = self.ai.start(self.key, inline=True)
        self.assertEqual(result['state'], 'held')
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(result['reserved_or_estimated_krw'], 0)

    def test_reexamination_capped_at_two_and_records_immutable(self):
        self.transport.bad_price = True
        first = self.ai.start(self.key, inline=True)
        second = self.ai.start(self.key, retry=True, inline=True)
        third = self.ai.start(self.key, retry=True, inline=True)
        self.assertEqual([first['attempt'], second['attempt'], third['attempt']], [0, 1, 2])
        with self.assertRaises(MeetingError):
            self.ai.start(self.key, retry=True, inline=True)
        for table in ['ai_run', 'ai_event', 'api_charge']:
            with sqlite3.connect(self.ai.path) as db:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(f'DELETE FROM {table}')
        self.assertTrue(self.ai.verify(first['id']))

    def test_restart_recovers_without_regenerating(self):
        self.transport.fail_at = 1
        result = self.ai.start(self.key, inline=True)
        with sqlite3.connect(self.ai.path) as db:
            db.execute('DROP TRIGGER ai_event_no_delete')
            db.execute("DELETE FROM ai_event WHERE run_id=? AND kind='run_held'", (result['id'],))
        self.ai.recover_interrupted()
        recovered = self.ai.get_run(result['id'])
        self.assertEqual(recovered['state'], 'interrupted')
        self.assertEqual(len(self.transport.calls), 1)

    def test_ai_outcomes_brier_costs_and_repeat_evaluation(self):
        run = self.ai.start(self.key, inline=True)
        future = self.league.import_snapshot(extended(sample()))['id']
        result = self.ai.evaluate(run['id'], future)
        self.assertEqual(len(result['outcomes']), 4)
        self.assertEqual(result['pending_horizons'], [])
        outcome = result['outcomes'][0]
        self.assertEqual(outcome['evaluation_type'], 'synthetic')
        for team in outcome['teams']:
            self.assertAlmostEqual(team['brier_score_up'], .16)
            self.assertEqual(team['fees'], 2)
            self.assertEqual(team['debate_net_return_change'], 0)
            self.assertEqual(len(team['daily_close_equity']), 2)
        self.assertEqual(result, self.ai.evaluate(run['id'], future))
        self.assertTrue(self.ai.verify(run['id']))
        self.assertEqual(len(self.ai.get_run(run['id'])['outcomes']), 4)
        with sqlite3.connect(self.ai.path) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM ai_outcome')

    def test_ai_outcomes_reject_changed_history_and_incomplete_meetings(self):
        run = self.ai.start(self.key, inline=True)
        changed = extended(sample()); changed['rows'][0]['close'] += .1
        key = self.league.import_snapshot(changed)['id']
        with self.assertRaises(MeetingError):
            self.ai.evaluate(run['id'], key)
        future = self.league.import_snapshot(extended(sample(), 3))['id']
        result = self.ai.evaluate(run['id'], future)
        self.assertEqual(result['pending_horizons'], [5, 10, 20])
        self.assertEqual(len(result['outcomes']), 1)
        other = sample(); other['source'] = 'other-source'
        other_key = self.league.import_snapshot(other)['id']
        self.transport.bad_price = True
        held = self.ai.start(other_key, inline=True)
        with self.assertRaises(MeetingError):
            self.ai.evaluate(held['id'], future)

    def test_local_daily_limit_is_independent_of_provider_errors(self):
        self.config = replace(self.config, local_rpd=1)
        result = self.ai.start(self.key, inline=True)
        self.assertEqual(result['state'], 'held')
        self.assertEqual(len(self.transport.calls), 4)
        self.assertIn('일일 호출 한도', result['result']['reason'])

    def test_run_identity_and_timestamp_are_audited(self):
        run = self.ai.start(self.key, inline=True)
        with sqlite3.connect(self.ai.path) as db:
            db.execute('DROP TRIGGER ai_run_no_update')
            db.execute("UPDATE ai_run SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (run['id'],))
        self.assertFalse(self.ai.verify(run['id']))

    def test_accounting_tamper_is_detected(self):
        result = self.ai.start(self.key, inline=True)
        with sqlite3.connect(self.ai.path) as db:
            db.execute('DROP TRIGGER api_charge_no_update')
            db.execute('UPDATE api_charge SET amount_microkrw=0')
        self.assertFalse(self.ai.verify(result['id']))
        with self.assertRaises(MeetingError):
            self.ai.start(self.key, retry=True, inline=True)

    def test_report_rejects_quantity_probability_duplicate_horizons(self):
        report = fixture_report(sample(), True)
        for mutation in ['quantity', 'probability', 'horizon', 'boolean']:
            bad = copy.deepcopy(report)
            if mutation == 'quantity': bad['quantity'] = 1000000
            if mutation == 'probability': bad['forecasts'][0]['probability_up'] = 1.1
            if mutation == 'horizon': bad['forecasts'][1]['horizon'] = 1
            if mutation == 'boolean': bad['quantity'] = True
            with self.subTest(mutation=mutation), self.assertRaises(MeetingError):
                validate_report(bad, True, sample())


class AIHTTPTests(unittest.TestCase):
    def test_background_run_http_and_missing_config(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            league = League(root / 'research.sqlite3')
            key = league.import_snapshot(sample())['id']
            transport = FakeTransport()
            config = Config.from_env(configured_env())
            ai = AILeague(root / 'ai.sqlite3', league, lambda: config, transport.factory)
            old_ai, old_league = server.AI_LEAGUE, server.LEAGUE
            server.AI_LEAGUE, server.LEAGUE = ai, league
            httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
            base = f'http://127.0.0.1:{httpd.server_port}'
            try:
                with urllib.request.urlopen(base+'/ai') as response:
                    self.assertEqual(response.status, 200)
                req = urllib.request.Request(base+'/api/ai/runs', data=json.dumps({'snapshot_id': key}).encode(),
                                             headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req) as response:
                    self.assertEqual(response.status, 202)
                    run_id = json.load(response)['id']
                ai.executor.shutdown(wait=True)
                with urllib.request.urlopen(base+'/api/ai/run?id='+run_id) as response:
                    result = json.load(response)
                self.assertEqual(result['state'], 'completed')
                self.assertEqual(len(transport.calls), 12)
                with patch.dict(os.environ, {k: '' for k in configured_env()}, clear=False):
                    with urllib.request.urlopen(base+'/api/ai') as response:
                        status = json.load(response)
                    self.assertFalse(status['configuration']['ready'])
                    self.assertEqual(len(status['runs']), 1)
                req = urllib.request.Request(base+'/api/ai/runs', data=b'{}', headers={
                    'Content-Type': 'application/json', 'Origin': 'https://foreign.example'})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(req)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
            finally:
                httpd.shutdown(); httpd.server_close(); thread.join()
                ai.executor.shutdown(wait=True)
                server.AI_LEAGUE, server.LEAGUE = old_ai, old_league
