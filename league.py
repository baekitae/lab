"""Offline research league. No external AI calls and no broker order integration."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

TEAMS = [('A', '추세·모멘텀'), ('B', '패턴·통계'), ('C', '국면·위험'), ('D', '도전자')]
HORIZONS = (1, 5, 10, 20)
INITIAL_CASH = 10000
SLIPPAGE = .001
FEE = 1.0


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


class League:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS snapshot (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meeting (
                    id INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshot(id),
                    created_at TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(snapshot_id));
                CREATE TABLE IF NOT EXISTS outcome (
                    snapshot_id TEXT NOT NULL REFERENCES snapshot(id), horizon INTEGER NOT NULL,
                    evaluation_snapshot_id TEXT NOT NULL REFERENCES snapshot(id), payload TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id, horizon));
                CREATE TABLE IF NOT EXISTS audit_event (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL);
            ''')
            # Application-level append-only protection; not protection against a DB owner.
            for table in ('snapshot', 'meeting', 'outcome', 'audit_event'):
                for operation in ('UPDATE', 'DELETE'):
                    db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()}
                        BEFORE {operation} ON {table} BEGIN
                        SELECT RAISE(ABORT, 'immutable research record'); END''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def event(self, db, kind, payload):
        previous = db.execute('SELECT event_hash FROM audit_event ORDER BY id DESC LIMIT 1').fetchone()
        previous = previous[0] if previous else '0' * 64
        created = utcnow()
        event_hash = digest([previous, created, kind, payload])
        db.execute('INSERT INTO audit_event(created_at,kind,payload,previous_hash,event_hash) VALUES(?,?,?,?,?)',
                   (created, kind, canonical(payload), previous, event_hash))

    def import_snapshot(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('스냅샷은 JSON 객체여야 합니다.')
        symbol = str(payload.get('symbol', '')).upper().strip()
        if not symbol or len(symbol) > 12 or not all(c.isascii() and (c.isalnum() or c in '.-') for c in symbol):
            raise ValueError('유효한 종목 코드를 입력하세요.')
        if not isinstance(payload.get('source'), str):
            raise ValueError('출처는 문자열이어야 합니다.')
        source = payload['source'].strip()
        if not source or len(source) > 200:
            raise ValueError('데이터 출처를 입력하세요 (최대 200자).')
        if not isinstance(payload.get('synthetic'), bool):
            raise ValueError('synthetic 필드에 테스트 데이터 여부를 명시하세요.')
        rows = payload.get('rows')
        if not isinstance(rows, list) or not 61 <= len(rows) <= 10000:
            raise ValueError('일봉 데이터는 61~10,000행이 필요합니다.')
        clean = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('일봉 행은 객체여야 합니다.')
            day = str(row.get('date', ''))
            try:
                parsed = date.fromisoformat(day)
            except ValueError as exc:
                raise ValueError('날짜는 YYYY-MM-DD 형식이어야 합니다.') from exc
            if parsed.isoformat() != day or parsed > datetime.now(timezone.utc).date():
                raise ValueError('유효한 과거 날짜만 입력하세요.')
            if clean and day <= clean[-1]['date']:
                raise ValueError('날짜는 중복 없이 오름차순이어야 합니다.')
            values = {}
            for field in ('open', 'high', 'low', 'close'):
                raw = row.get(field)
                if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 1e-8 <= raw <= 1e12 or not math.isfinite(raw):
                    raise ValueError('OHLC 가격은 0.00000001~1,000,000,000,000 범위의 유한 숫자여야 합니다.')
                values[field] = float(raw)
            if not (values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']):
                raise ValueError('고가·저가 범위가 시가·종가와 일치하지 않습니다.')
            clean.append({'date': day, **values})
        normalized = {'symbol': symbol, 'source': source, 'synthetic': payload['synthetic'],
                      'price_basis': str(payload.get('price_basis', 'unspecified'))[:100], 'rows': clean}
        key = digest(normalized)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM snapshot WHERE id=?', (key,)).fetchone():
                db.execute('INSERT INTO snapshot VALUES(?,?,?)', (key, utcnow(), canonical(normalized)))
                self.event(db, 'snapshot_imported', {'snapshot_id': key, 'source': source, 'synthetic': normalized['synthetic']})
        return {'id': key, 'count': len(clean), 'cutoff': clean[-1]['date']}

    def run(self, snapshot_id):
        if not isinstance(snapshot_id, str):
            raise ValueError('스냅샷 ID가 필요합니다.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT payload FROM meeting WHERE snapshot_id=?', (snapshot_id,)).fetchone()
            if previous:
                return json.loads(previous[0])
            record = db.execute('SELECT payload FROM snapshot WHERE id=?', (snapshot_id,)).fetchone()
            if not record:
                raise ValueError('스냅샷을 찾을 수 없습니다.')
            snapshot = json.loads(record[0])
            close = [r['close'] for r in snapshot['rows']]
            returns = [math.log(b / a) for a, b in zip(close, close[1:])]
            mean = sum(returns[-20:]) / 20
            vol = math.sqrt(sum((r - mean) ** 2 for r in returns[-20:]) / 20)
            momentum = close[-1] / close[-21] - 1
            trend = close[-1] / (sum(close[-20:]) / 20) - 1
            # Deliberately simple, versioned baselines; not calibrated AI probabilities.
            scores = {'A': momentum, 'B': -trend, 'C': mean if vol < .03 else 0, 'D': momentum / 2 - trend / 2}
            briefs = []
            for team_id, name in TEAMS:
                score = max(-.1, min(.1, scores[team_id]))
                allocation = .1 if score > .002 else 0
                qty = math.floor(INITIAL_CASH * allocation / close[-1])
                forecasts = []
                for horizon in HORIZONS:
                    expected = max(-.5, min(.5, score * math.sqrt(horizon)))
                    width = max(.02, vol * math.sqrt(horizon) * 2)
                    forecasts.append({'horizon': horizon, 'direction': 'up' if expected > .002 else 'down' if expected < -.002 else 'flat',
                                      'expected_return': expected, 'target_low': close[-1] * max(.01, 1 + expected - width),
                                      'target_high': close[-1] * (1 + expected + width),
                                      'probability': None, 'probability_note': '확률 보정 전: 산출하지 않음'})
                briefs.append({'team': team_id, 'name': name, 'initial_cash': INITIAL_CASH,
                               'proposed_qty': qty, 'cash': INITIAL_CASH, 'position_qty': 0,
                               'reason': f'20봉 모멘텀 {momentum:.2%}, 평균 대비 {trend:.2%}, 일별 변동성 {vol:.2%}',
                               'cancel_if': '가격 데이터 불일치·지연, 원장 검증 미완료 또는 위험 한도 초과',
                               'forecasts': forecasts})
            result = {'snapshot_id': snapshot_id, 'symbol': snapshot['symbol'], 'cutoff': snapshot['rows'][-1]['date'],
                      'created_at': utcnow(), 'mode': 'research', 'provider': 'local-baseline', 'strategy_version': 'baseline-v1',
                      'synthetic': snapshot['synthetic'], 'status': 'held',
                      'gates': [{'name': 'G1 원장 검증', 'passed': False, 'reason': '과거 원장 검증 미완료'},
                                {'name': '이중 시세 검증', 'passed': False, 'reason': '독립 공급원 비교 미구현'}],
                      'stages': {'independent_drafts': 'completed_local_baseline', 'debate': 'not_run',
                                 'manager': 'not_run', 'red_team': 'not_run', 'ai_audit': 'not_run'},
                      'teams': briefs, 'fills': [], 'warning': '로컬 기준 전략의 연구 결과입니다. AI 회의·실제 체결·검증된 투자 성과가 아닙니다.'}
            db.execute('INSERT INTO meeting(snapshot_id,created_at,payload) VALUES(?,?,?)',
                       (snapshot_id, result['created_at'], canonical(result)))
            self.event(db, 'research_run', result)
            return result

    def evaluate(self, snapshot_id, evaluation_snapshot_id):
        """Independent horizon experiments; never compound overlapping forecasts."""
        if not isinstance(snapshot_id, str) or not isinstance(evaluation_snapshot_id, str):
            raise ValueError('기준 연구와 후속 스냅샷 ID가 필요합니다.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            run_row = db.execute('SELECT payload FROM meeting WHERE snapshot_id=?', (snapshot_id,)).fetchone()
            base_row = db.execute('SELECT payload FROM snapshot WHERE id=?', (snapshot_id,)).fetchone()
            future_row = db.execute('SELECT payload FROM snapshot WHERE id=?', (evaluation_snapshot_id,)).fetchone()
            if not run_row or not base_row or not future_row:
                raise ValueError('연구 결과와 두 스냅샷이 모두 필요합니다.')
            run, base, future = [json.loads(r[0]) for r in (run_row, base_row, future_row)]
            for field in ('symbol', 'source', 'synthetic', 'price_basis'):
                if base[field] != future[field]:
                    raise ValueError('종목·출처·합성 여부·가격 기준이 같은 후속 데이터가 필요합니다.')
            length = len(base['rows'])
            if future['rows'][:length] != base['rows'] or len(future['rows']) <= length:
                raise ValueError('기존 가격을 변경하지 않고 미래 행만 추가한 스냅샷이 필요합니다.')
            output = []
            pending = []
            for horizon in HORIZONS:
                old = db.execute('SELECT payload FROM outcome WHERE snapshot_id=? AND horizon=?',
                                 (snapshot_id, horizon)).fetchone()
                if old:
                    output.append(json.loads(old[0]))
                    continue
                if len(future['rows']) < length + horizon:
                    pending.append(horizon)
                    continue
                segment = future['rows'][length:length + horizon]
                start = base['rows'][-1]['close']
                end = segment[-1]['close']
                actual = end / start - 1
                direction = 'up' if actual > .002 else 'down' if actual < -.002 else 'flat'
                entry = segment[0]['open'] * (1 + SLIPPAGE)
                exit_price = end * (1 - SLIPPAGE)
                scores = []
                for team in run['teams']:
                    forecast = next(f for f in team['forecasts'] if f['horizon'] == horizon)
                    # Gaps must not turn a fixed submitted quantity into an unaffordable fill.
                    qty = team['proposed_qty']
                    filled = qty if qty > 0 and qty * entry + FEE <= INITIAL_CASH else 0
                    cash = INITIAL_CASH - filled * entry - (FEE if filled else 0)
                    pnl = filled * (exit_price - entry) - (2 * FEE if filled else 0)
                    equity = [INITIAL_CASH] + [cash + filled * r['close'] for r in segment]
                    equity[-1] = INITIAL_CASH + pnl
                    peak, mdd = INITIAL_CASH, 0
                    for value in equity:
                        peak = max(peak, value)
                        mdd = max(mdd, (peak - value) / peak)
                    scores.append({'team': team['team'], 'direction_correct': forecast['direction'] == direction,
                                   'actual_in_target': forecast['target_low'] <= end <= forecast['target_high'],
                                   'filled_qty': filled, 'proposed_qty': qty,
                                   'fill_status': 'filled' if filled else 'cash' if qty == 0 else 'rejected_insufficient_cash',
                                   'entry_price': entry if filled else None, 'exit_price': exit_price if filled else None,
                                   'fees': 2 * FEE if filled else 0, 'net_pnl': pnl, 'net_return': pnl / INITIAL_CASH,
                                   'close_equity_mdd': mdd, 'end_equity': INITIAL_CASH + pnl})
                result = {'snapshot_id': snapshot_id, 'evaluation_snapshot_id': evaluation_snapshot_id,
                          'horizon': horizon, 'end_date': segment[-1]['date'], 'actual_return': actual,
                          'actual_direction': direction, 'created_at': utcnow(), 'synthetic': base['synthetic'],
                          'evaluation_type': 'synthetic' if base['synthetic'] else
                              'retrospective' if segment[0]['date'] <= run['created_at'][:10] else 'prospective_unverified_source',
                          'rules_version': 'independent-horizon-v1', 'slippage_per_side': SLIPPAGE, 'fee_per_side': FEE,
                          'baseline_buy_hold_return': actual, 'baseline_cash_return': 0, 'teams': scores,
                          'note': '각 기간은 별도 $10,000 가정 실험입니다. 중첩 수익률을 복리 합성하지 않으며 리그 잔고를 바꾸지 않습니다. MDD는 일별 종가 평가 기준이며 장중 낙폭은 포함하지 않습니다.'}
                db.execute('INSERT INTO outcome VALUES(?,?,?,?)',
                           (snapshot_id, horizon, evaluation_snapshot_id, canonical(result)))
                self.event(db, 'research_evaluation', result)
                output.append(result)
        return {'outcomes': output, 'pending_horizons': pending}

    def overview(self):
        with self.db() as db:
            snapshots = []
            for r in db.execute('SELECT * FROM snapshot ORDER BY created_at DESC'):
                p = json.loads(r['payload'])
                snapshots.append({'id': r['id'], 'symbol': p['symbol'], 'source': p['source'],
                                  'synthetic': p['synthetic'], 'count': len(p['rows']), 'cutoff': p['rows'][-1]['date']})
            meetings = [json.loads(r[0]) for r in db.execute('SELECT payload FROM meeting ORDER BY id DESC LIMIT 50')]
        return {'mode': 'research', 'teams': [{'id': t, 'name': n, 'initial_cash': INITIAL_CASH} for t, n in TEAMS],
                'snapshots': snapshots, 'meetings': meetings, 'outcomes': self.outcomes(), 'audit': self.verify(),
                'capabilities': {'external_ai': False, 'real_orders': False, 'paper_execution': False, 'horizon_evaluation': True, 'ledger_verified': False}}

    def outcomes(self):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM outcome ORDER BY rowid DESC LIMIT 200')]

    def verify(self):
        previous = '0' * 64
        count = 0
        with self.db() as db:
            for r in db.execute('SELECT * FROM audit_event ORDER BY id'):
                payload = json.loads(r['payload'])
                expected = digest([previous, r['created_at'], r['kind'], payload])
                if r['previous_hash'] != previous or r['event_hash'] != expected:
                    return {'valid': False, 'events': count}
                previous = expected
                count += 1
            # Also bind stored application records to the logged content.
            for r in db.execute('SELECT id,payload FROM snapshot'):
                if digest(json.loads(r['payload'])) != r['id']:
                    return {'valid': False, 'events': count}
            for r in db.execute('SELECT snapshot_id,payload FROM meeting'):
                found = db.execute("SELECT 1 FROM audit_event WHERE kind='research_run' AND payload=?", (r['payload'],)).fetchone()
                if not found or json.loads(r['payload'])['snapshot_id'] != r['snapshot_id']:
                    return {'valid': False, 'events': count}
            for r in db.execute('SELECT payload FROM outcome'):
                if not db.execute("SELECT 1 FROM audit_event WHERE kind='research_evaluation' AND payload=?", (r['payload'],)).fetchone():
                    return {'valid': False, 'events': count}
        return {'valid': True, 'events': count, 'scope': 'local_hash_chain_not_external_attestation'}
