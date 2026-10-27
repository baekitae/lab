import copy
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from league import League


def sample():
    rows = []
    day = date(2025, 1, 2)
    for i in range(80):
        price = 40 + i * .1
        rows.append({'date': (day + timedelta(days=i)).isoformat(),
                     'open': price, 'high': price + 1, 'low': price - 1, 'close': price + .2})
    return {'symbol': 'SOXL', 'source': 'unit-test synthetic', 'synthetic': True, 'rows': rows}


def extended(payload, count=20):
    result = copy.deepcopy(payload)
    day = date.fromisoformat(result['rows'][-1]['date'])
    for i in range(count):
        price = result['rows'][-1]['close'] + .1
        result['rows'].append({'date': (day + timedelta(days=i+1)).isoformat(),
                              'open': price, 'high': price+1, 'low': price-1, 'close': price+.2})
    return result


class LeagueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'league.sqlite3'
        self.league = League(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_repeatable_four_teams_and_reopen(self):
        snapshot = self.league.import_snapshot(sample())
        self.assertEqual(snapshot, self.league.import_snapshot(sample()))
        run = self.league.run(snapshot['id'])
        self.assertEqual(run, self.league.run(snapshot['id']))
        self.assertEqual([t['team'] for t in run['teams']], list('ABCD'))
        for team in run['teams']:
            self.assertEqual(team['cash'], 10000)
            self.assertEqual(team['position_qty'], 0)
            self.assertEqual([f['horizon'] for f in team['forecasts']], [1, 5, 10, 20])
            self.assertTrue(all(f['probability'] is None for f in team['forecasts']))
        self.assertEqual(run['status'], 'held')
        self.assertTrue(run['synthetic'])
        self.assertEqual(run['stages']['ai_audit'], 'not_run')
        self.assertEqual(League(self.path).overview()['meetings'], [run])
        self.assertEqual(self.league.verify()['events'], 2)
        self.assertTrue(self.league.verify()['valid'])

    def test_invalid_snapshots_leave_no_records(self):
        variants = []
        for field, value in [('rows', []), ('synthetic', 'false'), ('source', ''), ('symbol', '<script>')]:
            p = sample(); p[field] = value; variants.append(p)
        for field, value in [('close', float('nan')), ('open', True), ('low', 10000), ('date', '2099-01-01')]:
            p = sample(); p['rows'][-1][field] = value; variants.append(p)
        p = sample(); p['rows'][1]['date'] = p['rows'][0]['date']; variants.append(p)
        p = sample(); p['rows'][0]['date'] = '2025-02-30'; variants.append(p)
        for p in variants:
            with self.subTest(payload=str(p)[:70]), self.assertRaises(ValueError):
                self.league.import_snapshot(p)
        self.assertEqual(self.league.overview()['snapshots'], [])
        self.assertEqual(self.league.verify()['events'], 0)

    def test_concurrent_retry_creates_one_run(self):
        key = self.league.import_snapshot(sample())['id']
        with ThreadPoolExecutor(max_workers=4) as executor:
            runs = list(executor.map(lambda _: self.league.run(key), range(8)))
        self.assertTrue(all(r == runs[0] for r in runs))
        self.assertEqual(len(self.league.overview()['meetings']), 1)
        self.assertEqual(self.league.verify()['events'], 2)

    def test_immutable_records(self):
        key = self.league.import_snapshot(sample())['id']
        self.league.run(key)
        for table in ('snapshot', 'meeting', 'audit_event'):
            with self.subTest(table=table), sqlite3.connect(self.path) as db:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(f'DELETE FROM {table}')
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(f"UPDATE {table} SET payload='{{}}'")
        self.assertTrue(self.league.verify()['valid'])

    def test_audit_detects_payload_tamper_when_trigger_bypassed(self):
        self.league.import_snapshot(sample())
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TRIGGER audit_event_no_update')
            db.execute("UPDATE audit_event SET payload='{}'")
        self.assertFalse(self.league.verify()['valid'])

    def test_forward_evaluation_costs_and_idempotency(self):
        payload = sample()
        key = self.league.import_snapshot(payload)['id']
        run = self.league.run(key)
        future = extended(payload)
        future_key = self.league.import_snapshot(future)['id']
        result = self.league.evaluate(key, future_key)
        self.assertEqual(result['pending_horizons'], [])
        self.assertEqual(len(result['outcomes']), 4)
        first = result['outcomes'][0]
        self.assertEqual(first['evaluation_type'], 'synthetic')
        a = first['teams'][0]
        qty = run['teams'][0]['proposed_qty']
        self.assertGreater(qty, 0)
        entry = future['rows'][80]['open'] * 1.001
        exit_price = future['rows'][80]['close'] * .999
        self.assertAlmostEqual(a['net_pnl'], qty*(exit_price-entry)-2)
        self.assertEqual(a['fees'], 2)
        self.assertAlmostEqual(a['end_equity'], 10000+a['net_pnl'])
        self.assertEqual(result, self.league.evaluate(key, future_key))
        self.assertEqual(self.league.overview()['meetings'][0], run)
        self.assertTrue(self.league.verify()['valid'])
        self.assertEqual(self.league.verify()['events'], 7)
        with sqlite3.connect(self.path) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM outcome')

    def test_partial_evaluation_and_changed_history_rejection(self):
        payload = sample()
        key = self.league.import_snapshot(payload)['id']
        self.league.run(key)
        future = extended(payload, 3)
        future_key = self.league.import_snapshot(future)['id']
        result = self.league.evaluate(key, future_key)
        self.assertEqual(result['pending_horizons'], [5, 10, 20])
        self.assertEqual(len(result['outcomes']), 1)
        changed = copy.deepcopy(future)
        changed['rows'][0]['close'] += .1
        changed_key = self.league.import_snapshot(changed)['id']
        with self.assertRaises(ValueError):
            self.league.evaluate(key, changed_key)
        changed = copy.deepcopy(future); changed['synthetic'] = False
        changed_key = self.league.import_snapshot(changed)['id']
        with self.assertRaises(ValueError):
            self.league.evaluate(key, changed_key)

    def test_gap_cannot_create_negative_cash(self):
        payload = sample()
        key = self.league.import_snapshot(payload)['id']
        self.league.run(key)
        future = extended(payload, 1)
        future['rows'][-1].update(open=10000, high=11000, low=9000, close=10000)
        future_key = self.league.import_snapshot(future)['id']
        result = self.league.evaluate(key, future_key)['outcomes'][0]
        team_a = result['teams'][0]
        self.assertEqual(team_a['fill_status'], 'rejected_insufficient_cash')
        self.assertEqual(team_a['filled_qty'], 0)
        self.assertEqual(team_a['net_pnl'], 0)
        self.assertEqual(team_a['end_equity'], 10000)

    def test_changed_input_is_distinct_and_unknown_run_rejected(self):
        p = sample()
        first = self.league.import_snapshot(p)
        p['source'] = 'another-source'
        second = self.league.import_snapshot(p)
        self.assertNotEqual(first['id'], second['id'])
        with self.assertRaises(ValueError):
            self.league.run('unknown')
        self.assertEqual(len(self.league.overview()['meetings']), 0)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.module = server
        cls.temp = tempfile.TemporaryDirectory()
        cls.original = server.LEAGUE
        server.LEAGUE = League(Path(cls.temp.name) / 'league.sqlite3')
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.httpd.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.thread.join()
        cls.module.LEAGUE = cls.original
        cls.temp.cleanup()

    def request(self, path, payload=None, headers=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_end_to_end(self):
        code, body = self.request('/league')
        self.assertEqual(code, 200)
        self.assertIn('팀 리그'.encode(), body)
        code, body = self.request('/')
        self.assertEqual(code, 200)
        self.assertIn(b'href="/league"', body)
        headers = {'Content-Type': 'application/json'}
        code, body = self.request('/api/league/snapshots', sample(), headers)
        self.assertEqual(code, 200)
        key = json.loads(body)['id']
        code, body = self.request('/api/league/runs', {'snapshot_id': key}, headers)
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(body)['teams']), 4)
        code, body = self.request('/api/league/snapshots', extended(sample()), headers)
        self.assertEqual(code, 200)
        future_key = json.loads(body)['id']
        code, body = self.request('/api/league/evaluations', {'snapshot_id': key, 'evaluation_snapshot_id': future_key}, headers)
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(body)['outcomes']), 4)
        code, body = self.request('/api/league')
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)['audit']['valid'])

    def test_rejects_origin_content_type_and_bad_payload(self):
        path = '/api/league/snapshots'
        code, _ = self.request(path, sample(), {'Origin': 'https://other.example', 'Content-Type': 'application/json'})
        self.assertEqual(code, 403)
        code, _ = self.request(path, sample())
        self.assertEqual(code, 415)
        code, _ = self.request(path, [], {'Content-Type': 'application/json'})
        self.assertEqual(code, 400)
        code, _ = self.request('/api/league/runs', {'snapshot_id': 'missing'}, {'Content-Type': 'application/json'})
        self.assertEqual(code, 400)
        code, _ = self.request('/api/status')
        self.assertEqual(code, 200)
        self.assertIn('configured', json.loads(self.request('/api/status')[1]))


if __name__ == '__main__':
    unittest.main()
