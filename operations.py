"""Immutable operational records: feeds, ledger audit, portfolio and league review."""
from __future__ import annotations
import base64,calendar as monthcal,csv,hashlib,io,json,math,sqlite3,threading,zipfile
from contextlib import contextmanager
from datetime import date,datetime,timezone,timedelta
from decimal import Decimal,InvalidOperation
from pathlib import Path
from league import canonical,digest,utcnow
from ledger_formats import normalize as normalize_ledger
from trading_calendar import latest_closed,session_info,sessions_between,next_session

TEAMS='ABCD'
D=lambda x:Decimal(str(x))

class Operations:
    def __init__(self,path,league,ai=None):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self.league=league;self.ai=ai
        self.lock=threading.RLock();self.refresh_sources_callback=None
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS op_event(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,object_key TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS raw_file(id TEXT PRIMARY KEY,filename TEXT NOT NULL,encoding TEXT NOT NULL,created_at TEXT NOT NULL,content BLOB NOT NULL);
            CREATE INDEX IF NOT EXISTS op_lookup ON op_event(kind,object_key,id);''')
            for table in ('op_event','raw_file'):
                for op in ('UPDATE','DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable operational record'); END")
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=20);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()
    def append(self,db,kind,key,payload):
        prev=db.execute('SELECT event_hash FROM op_event ORDER BY id DESC LIMIT 1').fetchone()
        prev=prev[0] if prev else '0'*64;created=utcnow()
        h=digest([kind,key,created,payload,prev])
        db.execute('INSERT INTO op_event(kind,object_key,created_at,payload,previous_hash,event_hash) VALUES(?,?,?,?,?,?)',(kind,key,created,canonical(payload),prev,h))
    def add(self,kind,key,payload):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self.append(db,kind,key,payload)
        return payload
    def events(self,kind):
        with self.db() as db:return [json.loads(r[0]) for r in db.execute('SELECT payload FROM op_event WHERE kind=? ORDER BY id',(kind,))]
    def last(self,kind,key):
        with self.db() as db:r=db.execute('SELECT payload FROM op_event WHERE kind=? AND object_key=? ORDER BY id DESC LIMIT 1',(kind,key)).fetchone()
        return json.loads(r[0]) if r else None
    def verify(self):
        prev='0'*64;count=0
        with self.db() as db:
            db.execute('BEGIN')
            for r in db.execute('SELECT * FROM op_event ORDER BY id'):
                if r['previous_hash']!=prev or digest([r['kind'],r['object_key'],r['created_at'],json.loads(r['payload']),prev])!=r['event_hash']:return {'valid':False,'events':count}
                prev=r['event_hash'];count+=1
            for r in db.execute('SELECT id,content FROM raw_file'):
                if hashlib.sha256(r['content']).hexdigest()!=r['id']:return {'valid':False,'events':count}
        return {'valid':True,'events':count}
    def preserve(self,filename,raw,encoding):
        if not isinstance(filename,str) or len(filename)>200 or not filename.strip():raise ValueError('원본 파일 이름이 필요합니다.')
        if len(raw)>10_000_000:raise ValueError('파일은 10MB 이하여야 합니다.')
        key=hashlib.sha256(raw).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM raw_file WHERE id=?',(key,)).fetchone():
                db.execute('INSERT INTO raw_file VALUES(?,?,?,?,?)',(key,Path(filename).name,encoding,utcnow(),raw))
                self.append(db,'raw_preserved',key,{'id':key,'filename':Path(filename).name,'encoding':encoding,'bytes':len(raw)})
        alias=digest([key,Path(filename).name,encoding])
        if not self.last('raw_observed',alias):self.add('raw_observed',alias,{'id':key,'filename':Path(filename).name,'encoding':encoding,'observed_at':utcnow()})
        return key
    @staticmethod
    def decode(raw):
        for encoding in ('utf-8-sig','cp949'):
            try:return raw.decode(encoding),encoding
            except UnicodeError:pass
        raise ValueError('UTF-8 또는 CP949 CSV 파일이 필요합니다.')
    def feed(self,snapshot_id,metadata):
        snap=self.ai.get_snapshot(snapshot_id) if self.ai else self.snapshot(snapshot_id)
        provider=str(metadata.get('provider','')).strip().lower()
        if not provider or len(provider)>100:raise ValueError('실제 공급원 식별자를 입력하세요.')
        if metadata.get('license_confirmed') is not True:raise ValueError('공급원의 사용 조건을 확인해야 합니다.')
        basis=metadata.get('price_basis',snap.get('price_basis'))
        if basis not in ('split-adjusted','unadjusted'):raise ValueError('분할 조정 여부를 명시하세요. 미확인 가격은 검증 피드에 등록하지 않습니다.')
        asof=datetime.fromisoformat(str(metadata.get('asof','')).replace('Z','+00:00'))
        if asof.tzinfo is None or asof>datetime.now(timezone.utc)+timedelta(minutes=1):raise ValueError('공급 시각에 유효한 타임존이 필요합니다.')
        splits=metadata.get('splits',{})
        if not isinstance(splits,dict):raise ValueError('분할 이벤트는 날짜:비율 객체로 입력하세요.')
        for day,ratio in splits.items():
            date.fromisoformat(day)
            if type(ratio) not in (int,float) or not math.isfinite(ratio) or ratio<=0:raise ValueError('분할 비율이 올바르지 않습니다.')
        key=digest([snapshot_id,provider,asof.isoformat(),basis,splits])
        payload={'id':key,'snapshot_id':snapshot_id,'provider':provider,'symbol':snap['symbol'],'cutoff':snap['rows'][-1]['date'],
                 'asof':asof.isoformat(),'price_basis':basis,'splits':splits,'license_confirmed':True,'synthetic':snap['synthetic'],
                 'provenance':'user-attested provider identity and adjustment policy'}
        if not self.last('feed',key):self.add('feed',key,payload)
        return payload
    def snapshot(self,key):
        with self.league.db() as db:r=db.execute('SELECT payload FROM snapshot WHERE id=?',(key,)).fetchone()
        if not r:raise ValueError('스냅샷을 찾을 수 없습니다.')
        p=json.loads(r[0])
        if digest(p)!=key:raise ValueError('스냅샷 해시가 일치하지 않습니다.')
        return p
    def import_market_csv(self,filename,raw,metadata):
        text,enc=self.decode(raw);raw_id=self.preserve(filename,raw,enc)
        reader=csv.DictReader(io.StringIO(text));rows=[]
        for r in reader:
            normalized={str(k).strip().lower():v for k,v in r.items() if k}
            try:rows.append({'date':normalized['date'],**{k:float(normalized[k]) for k in ('open','high','low','close')}})
            except (KeyError,ValueError,TypeError):raise ValueError('시장 CSV에는 date,open,high,low,close 열이 필요합니다.') from None
        result=self.league.import_snapshot({'symbol':metadata.get('symbol','SOXL'),'source':metadata.get('provider',''),
            'synthetic':metadata.get('synthetic',False),'price_basis':metadata.get('price_basis','unspecified'),'rows':rows})
        feed=self.feed(result['id'],metadata)
        self.add('market_import',raw_id,{'raw_id':raw_id,'snapshot_id':result['id'],'feed_id':feed['id']})
        return feed
    def compare(self,left_id,right_id,now=None,require_fresh=True):
        feeds={f['id']:f for f in self.events('feed')}
        if left_id not in feeds or right_id not in feeds:raise ValueError('검증할 피드 두 개를 선택하세요.')
        a,b=feeds[left_id],feeds[right_id];sa,sb=self.snapshot(a['snapshot_id']),self.snapshot(b['snapshot_id'])
        issues=[]
        if a['provider']==b['provider']:issues.append('서로 다른 공급원이 필요합니다.')
        if a['symbol']!=b['symbol']:issues.append('종목이 다릅니다.')
        if a['price_basis']!=b['price_basis'] or a['splits']!=b['splits']:issues.append('분할 조정 방식·이벤트가 다릅니다.')
        if a['synthetic'] or b['synthetic']:issues.append('합성 데이터는 운영 게이트를 통과할 수 없습니다.')
        ma={r['date']:r for r in sa['rows']};mb={r['date']:r for r in sb['rows']}
        days=sorted(set(ma)&set(mb))[-60:]
        if len(days)<60:issues.append('공통 60거래일 데이터가 필요합니다.')
        mismatches=[]
        for day in days:
            for field in ('open','high','low','close'):
                delta=abs(ma[day][field]/mb[day][field]-1)
                if delta>.005:mismatches.append({'date':day,'field':field,'relative_difference':delta})
        if mismatches:issues.append('가격 차이가 0.5%를 초과합니다.')
        if a['cutoff']!=b['cutoff']:issues.append('최종 가격 날짜가 다릅니다.')
        if days:
            expected=sessions_between(days[0],max(a['cutoff'],b['cutoff']))
            if any(d not in ma or d not in mb for d in expected):issues.append('거래일 데이터가 누락되었습니다.')
            if any(not session_info(d) for d in sorted(set(ma)|set(mb))[-61:]):issues.append('거래일이 아닌 가격 행이 있습니다.')
        if require_fresh:
            latest=latest_closed(now)
            if a['cutoff']!=latest['date'] or b['cutoff']!=latest['date']:issues.append('최근 종료 거래일 가격이 아닙니다.')
            for f in (a,b):
                if datetime.fromisoformat(f['asof'])<datetime.fromisoformat(latest['close']):issues.append('공급 시각이 시장 종가 확정 전입니다.')
        payload={'id':digest([left_id,right_id,utcnow()]),'left':left_id,'right':right_id,'snapshot_ids':[a['snapshot_id'],b['snapshot_id']],
                 'symbol':a['symbol'],'cutoff':a['cutoff'],'checked_at':utcnow(),'passed':not issues,'issues':list(dict.fromkeys(issues)),
                 'mismatches':mismatches[:100],'tolerance':.005,'freshness_enforced':require_fresh,'source_identity':'user-attested'}
        self.add('market_comparison',payload['id'],payload);return payload
    def market_gate(self,snapshot_id,now=None):
        candidates=[c for c in self.events('market_comparison') if snapshot_id in c['snapshot_ids']]
        if not candidates:return {'gate':'dual_market_source','passed':False,'reason':'두 공급원의 최근 확정 가격 검증이 필요합니다.'}
        latest=latest_closed(now)
        c=candidates[-1]
        return {'gate':'dual_market_source','passed':bool(c['passed'] and c['freshness_enforced'] and c['cutoff']==latest['date']),'reason':'최근 종료 거래일·사용자 확인 출처·가격 정합성 정책 기준','comparison_id':c['id']}
    def import_ledger(self,filename,raw):
        text,encoding=self.decode(raw);raw_id=self.preserve(filename,raw,encoding)
        normalized,format_metadata=normalize_ledger(text)
        parsed=[];issues=[];zero=0;seen_ids=set();fingerprints={}
        reader=csv.DictReader(io.StringIO(normalized))
        required={'date','symbol','side','qty','price'}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):raise ValueError('원장 CSV 열: date,symbol,side,qty,price,fees,trade_id,order_date,settlement_date')
        positions={};realized={};fees_total=D(0);taxes_total=D(0)
        for line,r in enumerate(reader,2):
            try:
                day=date.fromisoformat(r['date']).isoformat();symbol=r['symbol'].strip().upper();side=r['side'].strip().upper()
                qty=D(r['qty'])
                if qty.is_finite() and qty==0:zero+=1;continue
                price,fees,tax=D(r['price']),D(r.get('fees') or 0),D(r.get('tax') or 0)
                line=int(r.get('source_line') or line)
                if not all(n.is_finite() for n in (qty,price,fees,tax)) or not 0<=qty<=D('1e12') or not 0<price<=D('1e12') or not 0<=fees<=D('1e12') or not 0<=tax<=D('1e12') or side not in ('BUY','SELL') or not symbol:raise ValueError()
                if qty==0:zero+=1;continue
                order=r.get('order_date') or '';settle=r.get('settlement_date') or ''
                if order:date.fromisoformat(order)
                if settle:date.fromisoformat(settle)
                trade={'date':day,'symbol':symbol,'side':side,'qty':str(qty),'price':str(price),'fees':str(fees),
                       'tax':str(tax),'trade_id':(r.get('trade_id') or '').strip(),'order_date':order,'settlement_date':settle,'line':line}
                ident=trade['trade_id'];fingerprint=(day,symbol,side,qty,price)
                if ident and ident in seen_ids:
                    previous=next(t for t in parsed if t['trade_id']==ident)
                    if any(previous[k]!=trade[k] for k in ('date','symbol','side','qty','price','fees','tax')):issues.append({'line':line,'kind':'conflicting_trade_id'})
                    continue
                if ident:seen_ids.add(ident)
                previous=fingerprints.get(fingerprint)
                if previous and (not ident or not previous[1]):issues.append({'line':line,'kind':'duplicate_candidate','other_line':previous[0]})
                elif not ident:
                    similar=next((t for t in parsed if t['symbol']==symbol and t['side']==side and D(t['qty'])==qty and D(t['price'])==price and 0<abs((date.fromisoformat(t['date'])-date.fromisoformat(day)).days)<=7),None)
                    if similar:issues.append({'line':line,'kind':'date_difference_candidate','other_line':similar['line']})
                fingerprints[fingerprint]=(line,ident);parsed.append(trade)
            except (ValueError,KeyError,InvalidOperation,TypeError):issues.append({'line':line,'kind':'invalid_row'})
        parsed.sort(key=lambda t:(t['date'],t['line']))
        for t in parsed:
            symbol=t['symbol'];q,p,f,tax=D(t['qty']),D(t['price']),D(t['fees']),D(t.get('tax',0));pos=positions.setdefault(symbol,{'qty':D(0),'cost':D(0),'price_cost':D(0),'cycles':0});realized.setdefault(symbol,D(0))
            if t['side']=='BUY':pos['qty']+=q;pos['cost']+=q*p+f+tax;pos['price_cost']+=q*p;fees_total+=f;taxes_total+=tax
            elif q>pos['qty']:issues.append({'line':t['line'],'kind':'oversell','available':str(pos['qty'])})
            else:
                avg=pos['cost']/pos['qty'];price_avg=pos['price_cost']/pos['qty'];realized[symbol]+=q*p-f-tax-avg*q
                pos['qty']-=q;pos['cost']-=avg*q;pos['price_cost']-=price_avg*q;fees_total+=f;taxes_total+=tax
                if not pos['qty']:pos.update(cost=D(0),price_cost=D(0),cycles=pos['cycles']+1)
        output={s:{'qty':str(p['qty']),'avg_excluding_fees':str(p['price_cost']/p['qty'] if p['qty'] else 0),
                   'cost_including_fees':str(p['cost']),'realized':str(realized[s]),'closed_cycles':p['cycles']} for s,p in positions.items()}
        result={'id':raw_id,'raw_id':raw_id,'created_at':utcnow(),'trades':parsed,'accepted':len(parsed),'zero_qty_skipped':zero,
                'positions':output,'issues':issues,'format_metadata':format_metadata,'total_fees':str(fees_total),'total_taxes':str(taxes_total),'gate_passed':False}
        if not self.last('ledger_import',raw_id):self.add('ledger_import',raw_id,result)
        return self.last('ledger_import',raw_id)
    def confirm_ledger(self,key,body):
        ledger=self.last('ledger_import',key)
        if not ledger:raise ValueError('원장을 먼저 가져오세요.')
        if ledger['issues']:raise ValueError('중복 후보·오류가 남아 있습니다. 원본 파일을 수정하지 말고 증권사 정정 원본으로 다시 대조하세요.')
        if body.get('full_history_confirmed') is not True or body.get('independent_calculation_confirmed') is not True:raise ValueError('전체 이력과 별도 수수료·실현손익 검산을 확인하세요.')
        refs=body.get('positions',{})
        if set(refs)!=set(ledger['positions']):raise ValueError('모든 종목의 증권사 잔고·평단·실현손익 기준이 필요합니다.')
        for symbol,p in ledger['positions'].items():
            r=refs[symbol]
            for field,target,tol in [('qty','qty',D('.000001')),('avg','avg_excluding_fees',D('.00005')),('realized','realized',D('.01'))]:
                try:expected=D(r[field])
                except (KeyError,InvalidOperation):raise ValueError('증권사 대조 기준 숫자를 확인하세요.') from None
                if not expected.is_finite() or abs(expected-D(p[target]))>tol:raise ValueError('원장 계산과 증권사 기준이 다릅니다: '+symbol+' '+field)
        try:expected_fee=D(body.get('total_fees','-1'));expected_tax=D(body.get('total_taxes','0'))
        except InvalidOperation:raise ValueError('수수료·세금은 유한 숫자로 입력하세요.') from None
        if not expected_fee.is_finite() or abs(expected_fee-D(ledger['total_fees']))>D('.01'):raise ValueError('수수료 합계가 다릅니다.')
        if not expected_tax.is_finite() or abs(expected_tax-D(ledger.get('total_taxes','0')))>D('.01'):raise ValueError('세금 합계가 다릅니다.')
        record={'ledger_id':key,'passed':True,'created_at':utcnow(),'basis':'user-attested full history and independent reconciliation','reference':refs,'total_fees':ledger['total_fees'],'total_taxes':ledger.get('total_taxes','0')}
        return self.add('ledger_confirmation',key,record)
    def gates(self,snapshot_id):
        snapshot=self.snapshot(snapshot_id);symbol=snapshot['symbol'];confirmed=[]
        for c in self.events('ledger_confirmation'):
            ledger=self.last('ledger_import',c['ledger_id'])
            if symbol in ledger['positions']:confirmed.append(c)
        return [{'gate':'ledger_G1','passed':bool(confirmed),'reason':'사용자 확인 전체 원장·증권사 기준 대조' if confirmed else '원장 미제공: 연구 모드만 가능'},self.market_gate(snapshot_id)]
    def archive_json(self,filename,raw):
        text=raw.decode('utf-8-sig');data=json.loads(text);state=data.get('state',data)
        if not isinstance(state,dict) or not isinstance(state.get('securities'),list) or not isinstance(state.get('trades'),list):raise ValueError('기존 패턴랩 전체 백업 JSON이 필요합니다.')
        source=self.preserve(filename,raw,'utf-8-sig');byid={s['id']:s for s in state['securities']};out=io.StringIO()
        writer=csv.DictWriter(out,fieldnames=['date','symbol','side','qty','price','fees','trade_id']);writer.writeheader()
        for t in state['trades']:
            sec=byid.get(t.get('securityId'))
            if not sec:raise ValueError('거래와 종목 참조가 끊어진 백업입니다.')
            writer.writerow({'date':t['date'],'symbol':sec['symbol'],'side':t['side'],'qty':t['qty'],'price':t['price'],'fees':t.get('fees',0),'trade_id':t.get('id','')})
        dry=self.import_ledger(filename+'.derived.csv',out.getvalue().encode())
        migration_issues=list(dry['issues'])
        for sid,ref in state.get('holdingReferences',{}).items():
            sec=byid.get(sid);position=dry['positions'].get(sec['symbol']) if sec else None
            if not position or abs(D(ref.get('qty',0))-D(position['qty']))>D('.000001') or abs(D(ref.get('avg',0))-D(position['avg_excluding_fees']))>D('.00005'):
                migration_issues.append({'kind':'broker_reference_mismatch','security_id':sid})
        record={'source':source,'ledger_id':dry['id'],'schema_version':data.get('version',1),'dry_run':True,'positions':dry['positions'],'issues':migration_issues}
        self.add('migration_preview',source,record);return record
    def commit_migration(self,source):
        preview=self.last('migration_preview',source)
        if not preview or preview['issues']:raise ValueError('오류 없는 드라이런을 먼저 확인하세요.')
        existing=self.last('migration_committed',source)
        if existing:return existing
        if not self.verify()['valid']:raise ValueError('원본 무결성 검사를 통과해야 합니다.')
        with self.db() as db:raw=db.execute('SELECT content FROM raw_file WHERE id=?',(source,)).fetchone()[0]
        original=json.loads(raw.decode('utf-8-sig'));state=original.get('state',original)
        accepted={k:state[k] for k in ('securities','trades','predictions','quotes','marketData','holdingReferences') if k in state}
        self.add('legacy_state',source,{'schema_version':preview['schema_version'],'state':accepted,'source':source})
        return self.add('migration_committed',source,{'source':source,'ledger_id':preview['ledger_id'],'created_at':utcnow(),'original_retained':True})
    def review_ledger(self,key,resolutions):
        original=self.last('ledger_import',key)
        if not original:raise ValueError('원장 기록이 없습니다.')
        candidates={str(i['line']) for i in original['issues'] if i['kind'] in ('duplicate_candidate','date_difference_candidate')}
        if len(candidates)!=len(original['issues']) or set(resolutions)!=candidates:raise ValueError('모든 중복 후보를 검토해야 합니다. 초과 매도·형식 오류는 정정 원본이 필요합니다.')
        if any(v not in ('separate-fill','exclude-duplicate') for v in resolutions.values()):raise ValueError('검토 결과는 별도 체결 또는 중복 제외입니다.')
        out=io.StringIO();fields=['date','symbol','side','qty','price','fees','trade_id','order_date','settlement_date','tax'];writer=csv.DictWriter(out,fieldnames=fields);writer.writeheader()
        for t in original['trades']:
            if resolutions.get(str(t['line']))=='exclude-duplicate':continue
            row={k:t.get(k,'0' if k=='tax' else '') for k in fields};row['trade_id']=row['trade_id'] or 'reviewed-'+key[:12]+'-'+str(t['line']);writer.writerow(row)
        result=self.import_ledger('reviewed-'+key[:12]+'.csv',out.getvalue().encode())
        self.add('ledger_review',result['id'],{'original':key,'result':result['id'],'resolutions':resolutions,'created_at':utcnow()})
        return result
    def queue_orders(self,run_id,conditions_checked=False):
        if self.emergency()['enabled']:raise ValueError('긴급 중지 상태에서는 모의 주문을 접수하지 않습니다.')
        if not conditions_checked:raise ValueError('모의 전략의 취소 조건을 확인한 뒤 접수하세요.')
        run=self.ai.get_run(run_id)
        if run['state']!='completed' or not run['audit_valid']:raise ValueError('완료·검증된 AI 회의만 모의 원장에 접수할 수 있습니다.')
        snapshot=self.snapshot(run['snapshot_id']);symbol=snapshot['symbol'];gate=self.market_gate(run['snapshot_id'])
        if not gate['passed']:raise ValueError('최근 이중 시세 검증을 통과해야 연속 모의 운용에 접수할 수 있습니다.')
        cutoff=snapshot['rows'][-1]['date'];due=next_session(cutoff);reports={s['step']:s['report'] for s in run['steps']}
        if self.events('paper_orders') and self.events('paper_orders')[0]['symbol']!=symbol:raise ValueError('현재 모의 시즌은 한 종목을 고정합니다. 종목을 변경하려면 새 작업 폴더의 별도 시즌을 사용하세요.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute("SELECT payload FROM op_event WHERE kind='paper_orders' AND object_key=?",(due,)).fetchone()
            if old:
                prior=json.loads(old[0])
                if prior['run_id']==run_id:return prior
                raise ValueError('해당 거래일의 모의 주문이 이미 접수되었습니다.')
            orders={team:next(c['candidate_quantity'] for c in reports['manager_eval']['comparisons'] if c['team']==team) if run['config'].get('leaders',{}).get(team)=='candidate' else reports['debate_'+team]['quantity'] for team in TEAMS}
            orders.update({'INDEPENDENT_'+team:reports['draft_'+team]['quantity'] for team in TEAMS})
            payload={'run_id':run_id,'symbol':symbol,'signal_date':cutoff,'due_date':due,'snapshot_id':run['snapshot_id'],
                     'created_at':utcnow(),'targets':orders,'conditions_confirmed_by_user':True,'research_only':True,
                     'director_opinion':reports['director']['decision'],'risk_policy':{'max_notional_fraction':0.1,'asset_exposure_multiplier':3 if symbol=='SOXL' else 1,'version':'research-risk-v1'},'execution_rule':'next-session-open-market-v1'}
            self.append(db,'paper_orders',due,payload)
        return payload
    def settle(self,snapshot_id):
        if self.emergency()['enabled']:raise ValueError('긴급 중지 상태에서는 원본 보존·복구를 먼저 확인하세요.')
        snapshot=self.snapshot(snapshot_id)
        if not self.market_gate(snapshot_id)['passed']:raise ValueError('최근 이중 시세 검증 가격만 모의 운용 정산에 사용할 수 있습니다.')
        orders=self.events('paper_orders')
        if not orders:return {'processed':0,'marks':[]}
        if any(o['symbol']!=snapshot['symbol'] for o in orders):raise ValueError('모의 시즌 종목과 다릅니다.')
        marks=[]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=[json.loads(r[0]) for r in db.execute("SELECT payload FROM op_event WHERE kind='portfolio_mark' ORDER BY id")]
            last=existing[-1] if existing else None
            lower=last['date'] if last else min(o['signal_date'] for o in orders)
            if last:
                history={r['date']:r['close'] for r in snapshot['rows']}
                if last['date'] not in history or any(m['date'] in history and not math.isclose(m['close'],history[m['date']],rel_tol=1e-9) for m in existing):raise ValueError('과거 정산 가격이 수정되었거나 직전 정산일이 빠졌습니다. 원본·분할 이력을 확인하세요.')
            rows=snapshot['rows'];indices={r['date']:i for i,r in enumerate(rows)}
            new_rows=[r for r in rows if r['date']>lower]
            if new_rows and [r['date'] for r in new_rows]!=sessions_between(next_session(lower),new_rows[-1]['date']):raise ValueError('정산 구간 거래일 가격이 빠졌습니다.')
            books={name:{'cash':10000.0,'qty':0,'fees':0.0,'turnover':0.0,'equity':10000.0} for name in list(TEAMS)+['BUY_HOLD','SIMPLE_MA']+['INDEPENDENT_'+t for t in TEAMS]}
            if last:books=last['books']
            feeds=[f for f in self.events('feed') if f['snapshot_id']==snapshot_id]
            splits=feeds[-1]['splits'] if feeds else {}
            basis=feeds[-1]['price_basis'] if feeds else 'unspecified'
            for row in new_rows:
                day=row['date'];i=indices[day];day_orders=[o for o in orders if o['due_date']==day];fills=[]
                if splits.get(day):
                    if basis!='unadjusted':raise ValueError('분할 조정 가격의 단위 재설정이 필요합니다. 원본·원장은 보존되며 정산을 보류합니다.')
                    for book in books.values():book['qty']*=splits[day]
                    self.append(db,'paper_split',day,{'date':day,'ratio':splits[day],'symbol':snapshot['symbol']})
                targets={}
                if day_orders:targets.update(day_orders[0]['targets'])
                if not last and not marks:targets['BUY_HOLD']=max(0,math.floor((10000-1)/(row['open']*1.001)))
                previous=[r['close'] for r in rows[max(0,i-20):i]]
                if len(previous)==20:
                    book=books['SIMPLE_MA'];equity=book['cash']+book['qty']*previous[-1]
                    targets['SIMPLE_MA']=math.floor(min(1000,equity*.1)/previous[-1]) if previous[-1]>sum(previous)/20 else 0
                for name,target in targets.items():
                    book=books[name];delta=target-book['qty']
                    if abs(delta)<1e-8:continue
                    price=row['open']*(1.001 if delta>0 else .999);fee=1.0
                    equity_at_open=book['cash']+book['qty']*row['open']
                    risk_cap=math.inf if name=='BUY_HOLD' else min(1000,equity_at_open*.1)
                    reason=''
                    if delta>0 and target*row['open']>risk_cap+1e-8:reason='risk_limit_after_gap'
                    if delta*price+fee>book['cash']+1e-8:reason='insufficient_cash'
                    if target<0:reason='no_short_positions'
                    if reason:
                        fills.append({'book':name,'date':day,'status':'rejected','reason':reason,'target_qty':target});continue
                    book['cash']-=delta*price+fee;book['qty']=target;book['fees']+=fee;book['turnover']+=abs(delta*price)
                    fills.append({'book':name,'date':day,'status':'filled','side':'BUY' if delta>0 else 'SELL','qty':abs(delta),'price':price,'fee':fee,
                                  'run_id':day_orders[0]['run_id'] if day_orders and name in day_orders[0]['targets'] else 'baseline-rule-v1'})
                for book in books.values():
                    book['equity']=book['cash']+book['qty']*row['close']
                    if book['cash']<-.000001 or book['qty']<0:raise ValueError('모의 원장의 현금·수량 불변식 위반입니다.')
                for fill in fills:self.append(db,'paper_fill',day+':'+fill['book'],fill)
                regime='up' if len(previous)>=20 and previous[-1]/previous[0]-1>.05 else 'down' if len(previous)>=20 and previous[-1]/previous[0]-1<-.05 else 'range'
                mark={'regime':regime,'date':day,'symbol':snapshot['symbol'],'close':row['close'],'snapshot_id':snapshot_id,'books':json.loads(canonical(books)),
                      'research_only':True,'price_basis':basis,'counterfactual':bool((marks[-1] if marks else last or {}).get('counterfactual') or (day_orders and any(datetime.fromisoformat(o['created_at'])>=datetime.fromisoformat(session_info(day)['open']) for o in day_orders)))}
                self.append(db,'portfolio_mark',day,mark);marks.append(mark)
        return {'processed':len(marks),'marks':marks}
    def portfolio(self):
        marks=self.events('portfolio_mark');orders=self.events('paper_orders');fills=self.events('paper_fill');scores=[]
        if marks:
            for name,book in marks[-1]['books'].items():
                values=[10000]+[m['books'][name]['equity'] for m in marks];peak=10000;mdd=0
                for value in values:peak=max(peak,value);mdd=max(mdd,(peak-value)/peak)
                scores.append({'book':name,**book,'net_return':book['equity']/10000-1,'mdd':mdd,'turnover_ratio':book['turnover']/10000})
        return {'scores':scores,'marks':marks,'orders':orders,'fills':fills,'initial_cash':10000,'mode':'paper-only','promotion':self.last('profile_mapping','current')}
    def rules(self,body):
        if set(body)-{'minimum_samples','minimum_regimes','minimum_months','horizon'}:raise ValueError('허용되지 않은 심사 규칙입니다.')
        rule={'minimum_samples':body.get('minimum_samples',30),'minimum_regimes':body.get('minimum_regimes',3),
              'minimum_months':body.get('minimum_months',6),'horizon':body.get('horizon',20)}
        if any(type(rule[k]) is not int for k in rule) or rule['minimum_samples']<30 or not 3<=rule['minimum_regimes']<=6 or not 6<=rule['minimum_months']<=120 or rule['horizon'] not in (5,10,20):raise ValueError('심사 기준은 최소 6개월·30개 독립 표본·3개 국면입니다.')
        rule.update(profiles=self.profiles(),leaders=self.leaders(),created_at=utcnow(),metric_policy='challenger dominates net return, close-MDD and Brier on disjoint prospective trials',calendar='XNYS')
        rule['id']=digest(rule);return self.add('promotion_rules',rule['id'],rule)
    def review(self,rule_id,now=None):
        rule=self.last('promotion_rules',rule_id)
        if not rule:raise ValueError('심사 기준을 먼저 고정하세요.')
        now=now or datetime.now(timezone.utc);created=datetime.fromisoformat(rule['created_at'])
        month=created.month-1+rule['minimum_months'];year=created.year+month//12;month=month%12+1
        eligible_date=created.replace(year=year,month=month,day=min(created.day,monthcal.monthrange(year,month)[1]))
        samples=[];last_end='';regimes=set()
        with self.ai.db() as db:
            runs=db.execute('SELECT id,created_at,snapshot_id FROM ai_run WHERE created_at>=? ORDER BY created_at',(rule['created_at'],)).fetchall()
        for r in runs:
            run=self.ai.get_run(r['id'])
            if not run['audit_valid'] or run['state']!='completed':continue
            if run['config'].get('profiles')!=rule.get('profiles') or run['config'].get('leaders',{})!=rule.get('leaders',{}):continue
            if not any(g['gate']=='dual_market_source' and g['passed'] for g in run['result'].get('python_gates',[])):continue
            snap=self.snapshot(r['snapshot_id'])
            if snap['synthetic']:continue
            signal=snap['rows'][-1]['date']
            result=next((o for o in run['outcomes'] if o['horizon']==rule['horizon'] and o['evaluation_type']=='prospective_unverified_source' and any(g['gate']=='dual_market_source' and g['passed'] for g in o.get('market_validation',[]))),None)
            if not result or signal<=last_end or any(t.get('shadow_evaluation_supported') is not True for t in result['teams']):continue
            prices=[b['close'] for b in snap['rows'][-21:]];momentum=prices[-1]/prices[0]-1
            regime='up' if momentum>.05 else 'down' if momentum<-.05 else 'range';regimes.add(regime)
            samples.append(result);last_end=result['end_date']
        reasons=[]
        if self.profiles()!=rule.get('profiles') or self.leaders()!=rule.get('leaders',{}):reasons.append('전략·팀장 버전이 바뀌었습니다. 새 심사 기준을 고정하세요.')
        if now<eligible_date:reasons.append('최초 공식 심사까지 6개월 이상의 운영 기간이 필요합니다.')
        if len(samples)<rule['minimum_samples']:reasons.append('서로 겹치지 않는 미래 평가 표본이 부족합니다.')
        if len(regimes)<rule['minimum_regimes']:reasons.append('시장 국면 다양성이 부족합니다.')
        metrics={}
        if samples:
            for team in TEAMS:
                ts=[next(t for t in s['teams'] if t['team']==team) for s in samples]
                metrics[team]={'mean_net_return':sum(t['net_return'] for t in ts)/len(ts),'mean_brier':sum(t['brier_score_up'] for t in ts)/len(ts),
                               'worst_close_mdd':max(t['close_equity_mdd'] for t in ts),
                               'shadow_mean_return':sum(t['shadow_candidate_net_return'] for t in ts)/len(ts),
                               'shadow_worst_mdd':max(t['shadow_candidate_mdd'] for t in ts),
                               'shadow_mean_brier':sum(t['shadow_candidate_brier'] for t in ts)/len(ts)}
        candidate=None;leader_candidates=[]
        if not reasons:
            for t in 'ABC':
                m=metrics[t]
                if m['shadow_mean_return']>m['mean_net_return'] and m['shadow_worst_mdd']<=m['worst_close_mdd'] and m['shadow_mean_brier']<=m['mean_brier']:leader_candidates.append(t)
            for team in 'ABC':
                a,d=metrics[team],metrics['D']
                if d['mean_net_return']>a['mean_net_return'] and d['mean_brier']<a['mean_brier'] and d['worst_close_mdd']<=a['worst_close_mdd']:
                    if candidate is None or a['mean_net_return']<metrics[candidate]['mean_net_return']:candidate=team
            if not candidate and not leader_candidates:reasons.append('위험·순수익·Brier 종합 개선 기준을 충족하지 않았습니다.')
        review={'id':digest([rule_id,utcnow()]),'rule_id':rule_id,'created_at':now.isoformat(),'eligible_date':eligible_date.isoformat(),
                'samples':len(samples),'regimes':sorted(regimes),'metrics':metrics,'status':'recommended' if candidate or leader_candidates else 'deferred',
                'replace_team':candidate,'leader_candidates':leader_candidates,'challenger':'D','reasons':reasons,'automatic_promotion':False}
        return self.add('promotion_review',review['id'],review)
    def apply_promotion(self,review_id,leader_team=None):
        review=self.last('promotion_review',review_id)
        if not review or review['status']!='recommended':raise ValueError('유효한 승격 권고가 없습니다.')
        application_key=review_id+(':'+leader_team if leader_team else ':team')
        if self.last('promotion_applied',application_key):return self.last('promotion_applied',application_key)
        if any(p['review_id']==review_id for p in self.events('promotion_applied')):raise ValueError('한 심사에서는 한 가지 전략 변경만 적용합니다. 변경 후 새 심사 기준을 고정하세요.')
        if self.events('promotion_review')[-1]['id']!=review_id:raise ValueError('가장 최근 심사 권고만 적용할 수 있습니다.')
        if leader_team:
            if leader_team not in review.get('leader_candidates',[]):raise ValueError('해당 팀장 후보의 승격 권고가 없습니다.')
            leaders=self.leaders();leaders[leader_team]='candidate'
            record={'review_id':review_id,'leaders':leaders,'created_at':utcnow(),'original_history_retained':True}
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE');self.append(db,'promotion_applied',application_key,record);self.append(db,'leader_mapping','current',record)
            return record
        if not review.get('replace_team'):raise ValueError('팀 승격 권고가 없습니다.')
        profiles=self.profiles();team=review['replace_team'];profiles[team],profiles['D']=profiles['D'],profiles[team]
        record={'review_id':review_id,'profiles':profiles,'created_at':utcnow(),'original_history_retained':True}
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self.append(db,'promotion_applied',application_key,record);self.append(db,'profile_mapping','current',record)
        return record
    def leaders(self):
        current=self.last('leader_mapping','current')
        return current['leaders'] if current else {}
    def profiles(self):
        latest=self.last('profile_mapping','current')
        return latest['profiles'] if latest else {'A':'추세·모멘텀','B':'패턴·통계','C':'국면·위험','D':'도전자'}
    def emergency(self,enabled=None,reason='operator_pause'):
        if enabled is None:return self.last('emergency_state','current') or {'enabled':False}
        if not isinstance(enabled,bool) or reason not in ('key_exposure','ledger_exposure','source_corruption','order_path','operator_pause'):raise ValueError('중지 사유와 활성화 여부를 확인하세요.')
        record=self.add('emergency_state','current',{'enabled':enabled,'reason':reason,'created_at':utcnow()})
        if enabled:
            for run in self.ai.overview()['runs']:
                if run['state']=='running':self.ai.cancel(run['id'])
        return record

    def schedule(self,body):
        if set(body)-{'enabled','symbol','auto_paper','data_policy'} or not isinstance(body.get('enabled'),bool):raise ValueError('스케줄 설정을 확인하세요.')
        if body.get('auto_paper'):raise ValueError('취소 조건 확인 전 자동 모의 주문은 지원하지 않습니다. 회의는 자동 실행하되 모의 주문은 검토 후 접수하세요.')
        symbol=str(body.get('symbol','SOXL')).strip().upper()
        if not symbol or not symbol.isascii() or not symbol.replace('.','').isalnum():raise ValueError('종목 코드를 확인하세요.')
        value={'enabled':body['enabled'],'symbol':symbol,'auto_paper':False,'data_policy':'verified-cached-feeds','created_at':utcnow(),'after_close_minutes':15}
        return self.add('schedule_config','daily',value)
    def tick(self,now=None):
        if self.emergency()['enabled']:return {'status':'emergency_stopped'}
        config=self.last('schedule_config','daily')
        if not config or not config['enabled']:return {'status':'disabled'}
        now=now or datetime.now(timezone.utc);ny=now.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York'))
        session=session_info(ny.date().isoformat())
        if not session:return {'status':'holiday','date':ny.date().isoformat()}
        if now<datetime.fromisoformat(session['close'])+timedelta(minutes=15):return {'status':'waiting_for_close','date':session['date']}
        key=config['symbol']+':'+session['date']
        if self.last('scheduled_run',key):return self.last('scheduled_run',key)
        if self.refresh_sources_callback:self.refresh_sources_callback()
        candidates=[f for f in self.events('feed') if f['symbol']==config['symbol'] and f['cutoff']==session['date'] and not f['synthetic']]
        for f in reversed(candidates):
            if not self.market_gate(f['snapshot_id'],now)['passed']:continue
            with self.lock:
                if self.last('scheduled_run',key):return self.last('scheduled_run',key)
                run=self.ai.start(f['snapshot_id'])
                return self.add('scheduled_run',key,{'status':'started','run_id':run['id'],'date':session['date'],'created_at':utcnow(),'symbol':config['symbol']})
        record={'status':'held_missing_verified_data','date':session['date'],'symbol':config['symbol']}
        # No claim on the date: a later tick may succeed after verified feeds arrive.
        if self.last('scheduler_notice',key)!=record:self.add('scheduler_notice',key,record)
        return record
    def overview(self):
        try:latest=latest_closed();cal={'ready':True,'latest_closed':latest}
        except ValueError as exc:cal={'ready':False,'error':str(exc)}
        ledgers=[{k:v for k,v in x.items() if k!='trades'} for x in self.events('ledger_import')]
        return {'feeds':self.events('feed'),'comparisons':self.events('market_comparison')[-30:],'ledgers':ledgers,
                'ledger_confirmations':self.events('ledger_confirmation'),'migrations':self.events('migration_preview'),
                'portfolio':self.portfolio(),'rules':self.events('promotion_rules'),'reviews':self.events('promotion_review'),
                'profiles':self.profiles(),'leaders':self.leaders(),'schedule':self.last('schedule_config','daily') or {'enabled':False},
                'scheduler_notices':(self.events('scheduler_notice')+self.events('scheduler_failure'))[-10:],'calendar':cal,'emergency':self.emergency(),'audit':self.verify()}
    def replay(self):
        audit=self.verify()
        if not audit['valid']:raise ValueError('무결성 검사에 실패하여 원장 재생을 중단합니다.')
        books={name:{'cash':10000.0,'qty':0.0,'fees':0.0,'turnover':0.0} for name in list(TEAMS)+['BUY_HOLD','SIMPLE_MA']+['INDEPENDENT_'+t for t in TEAMS]}
        last_day='';marks=0
        with self.db() as db:
            events=[(r['kind'],json.loads(r['payload'])) for r in db.execute("SELECT kind,payload FROM op_event WHERE kind IN ('paper_split','paper_fill','portfolio_mark') ORDER BY id")]
        for kind,payload in events:
            if kind=='paper_split':
                ratio=payload['ratio']
                if type(ratio) not in (int,float) or not math.isfinite(ratio) or ratio<=0:raise ValueError('분할 원장 비율이 잘못되었습니다.')
                for b in books.values():b['qty']*=ratio
            elif kind=='paper_fill' and payload['status']=='filled':
                b=books[payload['book']];qty,price,fee=payload['qty'],payload['price'],payload['fee']
                if any(type(x) not in (int,float) or not math.isfinite(x) or x<0 for x in (qty,price,fee)) or payload['side'] not in ('BUY','SELL'):raise ValueError('체결 원장 형식이 잘못되었습니다.')
                delta=qty if payload['side']=='BUY' else -qty
                b['cash']-=delta*price+fee;b['qty']+=delta;b['fees']+=fee;b['turnover']+=qty*price
                if b['cash']<-.000001 or b['qty']<-.000001:raise ValueError('재생 원장의 현금·수량이 음수입니다.')
            elif kind=='portfolio_mark':
                if payload['date']<=last_day or set(payload['books'])!=set(books):raise ValueError('재생 원장의 날짜·장부가 잘못되었습니다.')
                source=self.snapshot(payload['snapshot_id']);row=next((r for r in source['rows'] if r['date']==payload['date']),None)
                if not row or not math.isclose(row['close'],payload['close'],rel_tol=1e-9):raise ValueError('정산 원장과 원본 가격이 다릅니다.')
                for name,b in books.items():
                    stored=payload['books'][name]
                    expected={**b,'equity':b['cash']+b['qty']*payload['close']}
                    if any(not math.isclose(value,stored.get(field,float('nan')),rel_tol=1e-9,abs_tol=1e-6) for field,value in expected.items()):raise ValueError('체결 재생과 저장된 현금·보유량·자산이 다릅니다.')
                last_day=payload['date'];marks+=1
        return {'audit':audit,'arithmetic_valid':True,'replayed_marks':marks,'portfolio':self.portfolio(),'profiles':self.profiles(),'leaders':self.leaders(),'schedule':self.last('schedule_config','daily')}
