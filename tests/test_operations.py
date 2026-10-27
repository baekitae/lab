import base64,copy,csv,io,json,os,sqlite3,tempfile,threading,unittest,urllib.request,urllib.error,zipfile
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from settings_store import Settings
from operations import Operations
from backups import Backups
from league import League
from ai_league import AILeague,Config,config_status
from test_ai_league import FakeTransport,configured_env
from trading_calendar import latest_closed,session_info,sessions_between


def prices(end='2026-10-26',count=90):
    days=sessions_between('2025-01-01',end)[-count:]
    return [{'date':d,'open':40+i*.05,'high':41+i*.05,'low':39+i*.05,'close':40.2+i*.05} for i,d in enumerate(days)]

def market_csv(rows):
    out=io.StringIO();w=csv.DictWriter(out,fieldnames=['date','open','high','low','close']);w.writeheader();w.writerows(rows);return out.getvalue().encode()


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.envpatch=patch.dict(os.environ,{k:v for k,v in os.environ.items() if not k.startswith('PL_')},clear=True);self.envpatch.start();self.addCleanup(self.envpatch.stop)
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.settings=Settings(self.root,codec=False)
    def tearDown(self):self.temp.cleanup()
    def test_keys_masked_memory_only_and_settings_survive(self):
        secret='AIza-test-unique-key-A'
        result=self.settings.save({'values':{'PL_GEMINI_MODEL':'gemini-test','PL_INPUT_KRW_PER_MILLION':'0'},'keys':{'TEAM_A':secret}})
        self.assertNotIn(secret,json.dumps(result));self.assertFalse(result['persistent_keys'])
        self.assertEqual(self.settings.effective_env()['PL_TEAM_A_API_KEY'],secret)
        self.assertNotIn(secret,self.settings.path.read_text());self.assertFalse(self.settings.vault.exists())
        reloaded=Settings(self.root);self.assertEqual(reloaded.public()['PL_GEMINI_MODEL'],'gemini-test');self.assertNotIn('PL_TEAM_A_API_KEY',reloaded.effective_env())
        self.settings.save({'keys':{'TEAM_A':''}});self.assertEqual(self.settings.secrets()['TEAM_A'],secret)
        self.settings.save({'delete_keys':['TEAM_A']});self.assertNotIn('TEAM_A',self.settings.secrets())
    def test_bad_and_duplicate_keys_and_budget_rejected_atomically(self):
        self.settings.save({'keys':{'TEAM_A':'test-secret-A'}})
        for body in [{'keys':{'TEAM_B':'test-secret-A'}},{'keys':{'TEAM_B':'line\ninjection'}},{'values':{'PL_MONTHLY_BUDGET_KRW':'20001'}},{'values':{'PL_INPUT_KRW_PER_MILLION':'nan'}},{'values':{'PL_GEMINI_MODEL':'../invalid'}}]:
            with self.subTest(body=body),self.assertRaises(ValueError):self.settings.save(body)
        self.assertEqual(self.settings.secrets(),{'TEAM_A':'test-secret-A'})
    def test_environment_bindings_preserved(self):
        with patch.dict(os.environ,{'PL_TEAM_A_API_KEY':'environment-secret'}):
            with self.assertRaises(ValueError):self.settings.save({'keys':{'TEAM_A':'new-secret-value'}})
            self.assertTrue(self.settings.view()['roles'][0]['managed_by_environment'])
            self.assertEqual(self.settings.effective_env()['PL_TEAM_A_API_KEY'],'environment-secret')
    def test_encrypted_blob_contract_with_explicit_test_codec(self):
        # Tests Settings persistence wiring only. Actual DPAPI is Windows-only and separately unverified here.
        class Codec:
            def transform(self,raw,decrypt=False):return bytes(x^0x5A for x in raw)
        store=Settings(self.root,codec=Codec());store.save({'keys':{'TEAM_A':'test-secret-A'}})
        self.assertNotIn(b'test-secret-A',store.vault.read_bytes())
        self.assertEqual(Settings(self.root,codec=Codec()).secrets()['TEAM_A'],'test-secret-A')
    def test_corrupt_store_can_be_preserved_and_reset_without_plaintext_fallback(self):
        raw=b'{invalid settings';self.settings.path.write_bytes(raw)
        self.assertTrue(self.settings.view()['error'])
        with self.assertRaises(ValueError):self.settings.recover(False)
        result=self.settings.recover(True);self.assertTrue(result['reset'])
        saved=self.root/'recovery'/result['preserved'][0]['file'];self.assertEqual(saved.read_bytes(),raw)
        self.assertEqual(self.settings.view()['error'],'');self.assertEqual(self.settings.public(),{})

    def test_saved_values_feed_real_config_loader(self):
        values={'PL_GEMINI_MODEL':'gemini-test','PL_INPUT_KRW_PER_MILLION':'0','PL_OUTPUT_KRW_PER_MILLION':'0',**{f'PL_{r}_MODEL':'' for r in __import__('ai_league').ROLES}}
        keys={r:'local-test-secret-'+r for r in __import__('ai_league').ROLES}
        self.settings.save({'values':values,'keys':keys})
        with patch('ai_league.SETTINGS',self.settings):
            self.assertTrue(config_status()['ready']);self.assertEqual(Config.from_env().roles['TEAM_A'].key,keys['TEAM_A'])


class CalendarTests(unittest.TestCase):
    def test_nyse_holidays_early_closes_and_dst(self):
        self.assertIsNone(session_info('2026-07-03'));self.assertIsNone(session_info('2026-11-26'))
        self.assertTrue(session_info('2026-11-27')['early_close'])
        self.assertTrue(session_info('2026-12-24')['early_close'])
        self.assertEqual(session_info('2026-03-06')['open'][11:16],'14:30')
        self.assertEqual(session_info('2026-03-09')['open'][11:16],'13:30')
        self.assertEqual(latest_closed(datetime(2026,11,27,17,tzinfo=timezone.utc))['date'],'2026-11-25')


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.league=League(self.root/'league.sqlite3')
        self.transport=FakeTransport();config=Config.from_env(configured_env())
        self.ai=AILeague(self.root/'ai_league.sqlite3',self.league,lambda:config,self.transport.factory)
        self.ops=Operations(self.root/'operations.sqlite3',self.league,self.ai)
        self.ai.profile_loader=self.ops.profiles;self.ai.leader_loader=self.ops.leaders
        self.now=datetime(2026,10,26,21,tzinfo=timezone.utc)
    def tearDown(self):self.ai.executor.shutdown(wait=True);self.temp.cleanup()
    def feed(self,provider,rows=None,**extra):
        rows=rows or prices();p={'provider':provider,'symbol':'SOXL','price_basis':'unadjusted','license_confirmed':True,'asof':self.now.isoformat(),**extra}
        return self.ops.import_market_csv(provider+'.csv',market_csv(rows),p)
    def test_dual_feeds_freshness_mismatch_and_revocation(self):
        a,b=self.feed('source-a'),self.feed('source-b')
        passed=self.ops.compare(a['id'],b['id'],self.now);self.assertTrue(passed['passed'])
        self.assertTrue(self.ops.market_gate(a['snapshot_id'],self.now)['passed'])
        changed=prices();changed[-1]['high']*=1.5
        bad=self.feed('source-c',changed)
        self.assertFalse(self.ops.compare(a['id'],bad['id'],self.now)['passed'])
        self.assertFalse(self.ops.market_gate(a['snapshot_id'],self.now)['passed'])
        self.assertFalse(self.ops.compare(a['id'],a['id'],self.now)['passed'])
        self.assertTrue(self.ops.verify()['valid'])
    def test_delayed_adjusted_missing_dates_and_synthetic_feeds_hold(self):
        a=self.feed('source-a');old=self.feed('old-source',prices(end='2026-10-23'))
        self.assertFalse(self.ops.compare(a['id'],old['id'],self.now)['passed'])
        adjusted=self.feed('adjusted-source',price_basis='split-adjusted')
        self.assertFalse(self.ops.compare(a['id'],adjusted['id'],self.now)['passed'])
        missing=prices();missing.pop(-3);b=self.feed('missing-source',missing)
        self.assertFalse(self.ops.compare(a['id'],b['id'],self.now)['passed'])
        synth=self.feed('synthetic-source',synthetic=True)
        self.assertFalse(self.ops.compare(a['id'],synth['id'],self.now)['passed'])
    def test_raw_bytes_preserved_immutable_and_replay_detects_tamper(self):
        self.feed('source-a')
        with sqlite3.connect(self.ops.path) as db:
            original=db.execute('SELECT content FROM raw_file LIMIT 1').fetchone()[0]
            self.assertEqual(original,market_csv(prices()))
            with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM raw_file')
            db.execute('DROP TRIGGER op_event_no_update');db.execute("UPDATE op_event SET payload='{}' WHERE id=1")
        self.assertFalse(self.ops.verify()['valid'])
        with self.assertRaises(ValueError):self.ops.replay()
    def test_ledger_exact_costs_zeros_duplicates_and_confirmation(self):
        raw=b'date,symbol,side,qty,price,fees,trade_id,order_date,settlement_date\n2026-01-02,SOXL,BUY,10,100,2,b1,2026-01-02,2026-01-05\n2026-01-05,SOXL,SELL,4,120,1,s1,,\n2026-01-05,SOXL,SELL,4,120,1,s1,,\n2026-01-06,SOXL,BUY,0,100,0,z,,\n'
        ledger=self.ops.import_ledger('fills.csv',raw);self.assertEqual(ledger['accepted'],2);self.assertEqual(ledger['zero_qty_skipped'],1)
        p=ledger['positions']['SOXL'];self.assertEqual(p['qty'],'6');self.assertEqual(p['realized'],'78.2');self.assertEqual(ledger['total_fees'],'3')
        confirmed=self.ops.confirm_ledger(ledger['id'],{'full_history_confirmed':True,'independent_calculation_confirmed':True,
            'positions':{'SOXL':{'qty':6,'avg':100,'realized':'78.2'}},'total_fees':3})
        self.assertTrue(confirmed['passed'])
        with self.assertRaises(ValueError):self.ops.confirm_ledger(ledger['id'],{'full_history_confirmed':False})
    def test_ledger_duplicate_resolution_keeps_original_and_oversell_is_not_invented(self):
        raw=b'date,symbol,side,qty,price\n2026-01-02,SOXL,BUY,10,100\n2026-01-02,SOXL,BUY,10,100\n'
        ledger=self.ops.import_ledger('duplicate.csv',raw);self.assertEqual(len(ledger['issues']),1)
        reviewed=self.ops.review_ledger(ledger['id'],{'3':'exclude-duplicate'});self.assertEqual(reviewed['positions']['SOXL']['qty'],'10');self.assertEqual(reviewed['issues'],[])
        self.assertEqual(self.ops.last('ledger_import',ledger['id'])['positions']['SOXL']['qty'],'20')
        bad=self.ops.import_ledger('oversell.csv',b'date,symbol,side,qty,price\n2026-01-02,SOXL,SELL,5,100\n')
        self.assertEqual(bad['issues'][0]['kind'],'oversell');self.assertEqual(bad['positions']['SOXL']['qty'],'0')
    def test_json_migration_has_dry_run_commit_and_original(self):
        payload={'version':1,'state':{'securities':[{'id':'s','symbol':'SOXL'}],'trades':[{'id':'t','securityId':'s','date':'2026-01-02','side':'BUY','qty':2,'price':10}]}}
        raw=json.dumps(payload).encode();preview=self.ops.archive_json('backup.json',raw)
        self.assertTrue(preview['dry_run']);self.assertEqual(preview['positions']['SOXL']['qty'],'2')
        committed=self.ops.commit_migration(preview['source']);self.assertTrue(committed['original_retained'])
    def test_continuous_paper_cash_holdings_fees_and_idempotent_settlement(self):
        a,b=self.feed('source-a'),self.feed('source-b');self.ops.compare(a['id'],b['id'],self.now)
        run=self.ai.start(a['snapshot_id'],inline=True)
        with patch.object(self.ops,'market_gate',return_value={'gate':'dual_market_source','passed':True}):
            order=self.ops.queue_orders(run['id'],True);self.assertEqual(order['due_date'],'2026-10-27')
            rows=prices();rows.extend([{'date':'2026-10-27','open':45,'high':47,'low':44,'close':46}, {'date':'2026-10-28','open':46,'high':48,'low':45,'close':47}])
            class FutureClock(datetime):
                @classmethod
                def now(cls,tz=None):return datetime(2026,11,1,tzinfo=timezone.utc)
            with patch('league.datetime',FutureClock):
                snap=self.league.import_snapshot({'symbol':'SOXL','source':'source-a','synthetic':False,'price_basis':'unadjusted','rows':rows})
            # Explicit future-date fixture: test inputs are injected directly rather than bypassing production import restrictions.
            settled=self.ops.settle(snap['id'])
            self.assertEqual(settled['processed'],2);book=settled['marks'][-1]['books']['A']
            self.assertEqual(book['qty'],1);self.assertAlmostEqual(book['cash'],10000-45*1.001-1);self.assertAlmostEqual(book['equity'],book['cash']+47)
            self.assertEqual(self.ops.settle(snap['id'])['processed'],0)
            self.assertEqual(self.ops.queue_orders(run['id'],True),order)
            self.assertTrue(self.ops.portfolio()['scores']);self.assertTrue(self.ops.replay()['arithmetic_valid'])
    def test_emergency_stop_blocks_new_generation_and_paper_until_resumed(self):
        a=self.feed('source-a');self.ai.safety_checker=lambda:self.ops.emergency()['enabled']
        self.ops.emergency(True,'key_exposure');self.assertEqual(self.ops.tick()['status'],'emergency_stopped')
        with self.assertRaises(ValueError):self.ai.start(a['snapshot_id'],inline=True)
        with self.assertRaises(ValueError):self.ops.queue_orders('missing',True)
        self.assertEqual(self.transport.calls,[])
        self.ops.emergency(False)
        self.assertEqual(self.ai.start(a['snapshot_id'],inline=True)['state'],'completed')

    def test_schedule_holiday_wait_missing_feed_and_single_claim(self):
        self.ops.schedule({'enabled':True,'symbol':'SOXL'})
        self.assertEqual(self.ops.tick(datetime(2026,11,26,22,tzinfo=timezone.utc))['status'],'holiday')
        self.assertEqual(self.ops.tick(datetime(2026,10,26,19,tzinfo=timezone.utc))['status'],'waiting_for_close')
        self.assertEqual(self.ops.tick(self.now)['status'],'held_missing_verified_data')
        a,b=self.feed('source-a'),self.feed('source-b');self.ops.compare(a['id'],b['id'],self.now)
        result=self.ops.tick(self.now);self.assertEqual(result['status'],'started');self.ai.executor.shutdown(wait=True)
        self.assertEqual(self.ops.tick(self.now),result);self.assertEqual(len(self.ops.events('scheduled_run')),1)
    def test_promotion_is_preregistered_and_defers_insufficient_samples(self):
        rule=self.ops.rules({});review=self.ops.review(rule['id']);self.assertEqual(review['status'],'deferred');self.assertEqual(review['samples'],0)
        with self.assertRaises(ValueError):self.ops.apply_promotion(review['id'])
        with self.assertRaises(ValueError):self.ops.rules({'minimum_months':1})
    def test_backup_restore_hashes_and_credential_exclusion(self):
        self.feed('source-a');backups=Backups(self.root);backup=backups.create();raw=(backups.directory/backup['name']).read_bytes()
        self.assertTrue(backups.restore(raw)['dry_run'])
        self.ops.add('test_marker','x',{'value':1});self.assertIsNotNone(self.ops.last('test_marker','x'))
        restored=backups.restore(raw,True);self.assertTrue(restored['restored']);self.assertIsNone(self.ops.last('test_marker','x'));self.assertTrue(self.ops.verify()['valid'])
        with zipfile.ZipFile(io.BytesIO(raw)) as z:self.assertNotIn('credentials.dpapi',z.namelist())
        output=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as z,zipfile.ZipFile(output,'w') as modified:
            for n in z.namelist():modified.writestr(n,b'bad' if n=='operations.sqlite3' else z.read(n))
        with self.assertRaises(ValueError):backups.restore(output.getvalue(),True)

class AppHTTPTests(unittest.TestCase):
    def setUp(self):
        import server,ai_league
        self.server=server;self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{k:v for k,v in os.environ.items() if not k.startswith('PL_')},clear=True);self.env.start()
        self.store=Settings(root,codec=False);self.league=League(root/'league.sqlite3');self.transport=FakeTransport()
        self.ai=AILeague(root/'ai_league.sqlite3',self.league,adapter_factory=self.transport.factory)
        self.ops=Operations(root/'operations.sqlite3',self.league,self.ai);self.ai.gate_checker=self.ops.gates
        from connectors import MarketConnectors
        self.patches=[patch.object(server,k,v) for k,v in {'SETTINGS':self.store,'LEAGUE':self.league,'AI_LEAGUE':self.ai,'OPS':self.ops,'BACKUPS':Backups(root),'CONNECTORS':MarketConnectors(self.ops,lambda *a,**kw:None)}.items()]
        self.patches.append(patch.object(ai_league,'SETTINGS',self.store))
        for p in self.patches:p.start()
        self.httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.thread=threading.Thread(target=self.httpd.serve_forever,daemon=True);self.thread.start();self.base=f'http://127.0.0.1:{self.httpd.server_port}'
        self.token=self.request('/api/app/state')[1]['csrf']
    def tearDown(self):
        self.httpd.shutdown();self.httpd.server_close();self.thread.join();self.ai.executor.shutdown(wait=True)
        for p in reversed(self.patches):p.stop()
        self.env.stop();self.temp.cleanup()
    def request(self,path,body=None,headers=None):
        h={'Content-Type':'application/json','X-Pattern-Lab-Token':getattr(self,'token','')};h.update(headers or {})
        req=urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=h)
        try:
            with urllib.request.urlopen(req,timeout=10) as response:
                raw=response.read();return response.status,json.loads(raw) if 'application/json' in response.headers.get('Content-Type','') else raw
        except urllib.error.HTTPError as exc:
            with exc:return exc.code,json.load(exc)
    def test_key_registration_visible_masks_values_and_models_use_saved_key(self):
        secret='AIza-local-user-input-A'
        code,response=self.request('/api/app/settings',{'keys':{'TEAM_A':secret}});self.assertEqual(code,200);self.assertNotIn(secret,json.dumps(response))
        state=self.request('/api/app/state')[1];self.assertTrue(state['settings']['roles'][0]['configured']);self.assertNotIn(secret,json.dumps(state))
        class ModelProvider:
            def __init__(inner,key,model,*args):self.assertEqual(key,secret)
            def list_models(inner):return ['gemini-test']
        with patch.object(self.server,'GeminiAdapter',ModelProvider):
            code,response=self.request('/api/app/models',{'role':'TEAM_A'});self.assertEqual(code,200);self.assertEqual(response['models'],['gemini-test'])
        class FailingProvider(ModelProvider):
            def list_models(inner):raise __import__('providers').ProviderError('test','Echoed credential: '+secret)
        with patch.object(self.server,'GeminiAdapter',FailingProvider):
            code,response=self.request('/api/app/models',{'role':'TEAM_A'});self.assertEqual(code,502);self.assertNotIn(secret,json.dumps(response))
    def test_csrf_host_origin_and_malformed_body_guards(self):
        self.assertEqual(self.request('/api/app/settings',{'keys':{'TEAM_A':'test-key-A'}},{'X-Pattern-Lab-Token':''})[0],403)
        self.assertEqual(self.request('/api/app/settings',{}, {'Origin':'https://foreign.example'})[0],403)
        self.assertEqual(self.request('/api/app/state',headers={'Host':'foreign.example'})[0],421)
        self.assertEqual(self.request('/api/app/settings',[])[0],400)
    def test_ledger_backup_and_restore_through_http(self):
        raw=b'date,symbol,side,qty,price\n2026-01-02,SOXL,BUY,2,40\n'
        code,result=self.request('/api/app/ledger/import',{'filename':'ledger.csv','content':base64.b64encode(raw).decode()});self.assertEqual(code,200);self.assertEqual(result['positions']['SOXL']['qty'],'2')
        code,backup=self.request('/api/app/backup/create',{});self.assertEqual(code,200)
        code,content=self.request('/api/app/backup/download?name='+backup['name']);self.assertEqual(code,200)
        code,preview=self.request('/api/app/backup/restore',{'content':base64.b64encode(content).decode(),'confirm':False});self.assertEqual(code,200);self.assertTrue(preview['dry_run'])
        code,restored=self.request('/api/app/backup/restore',{'content':base64.b64encode(content).decode(),'confirm':True});self.assertEqual(code,200);self.assertTrue(restored['restored'])
        self.assertTrue(self.request('/api/app/state')[1]['operations']['audit']['valid'])

class AdditionalOperationsTests(unittest.TestCase):
    setUp=OperationsTests.setUp
    tearDown=OperationsTests.tearDown
    feed=OperationsTests.feed
    def test_tax_accounting_and_distinct_execution_ids(self):
        raw=b'date,symbol,side,qty,price,fees,tax,trade_id\n2026-01-02,SOXL,BUY,10,100,2,1,b1\n2026-01-05,SOXL,SELL,4,120,1,2,s1\n'
        result=self.ops.import_ledger('tax.csv',raw)
        self.assertEqual(result['positions']['SOXL']['realized'],'75.8');self.assertEqual(result['total_taxes'],'3')
        repeated=b'date,symbol,side,qty,price,trade_id\n2026-01-02,SOXL,BUY,1,40,a\n2026-01-02,SOXL,BUY,1,40,b\n'
        self.assertEqual(self.ops.import_ledger('distinct.csv',repeated)['issues'],[])
    def test_different_dates_require_review_and_migration_reference_mismatch(self):
        raw=b'date,symbol,side,qty,price\n2026-01-02,SOXL,BUY,1,40\n2026-01-05,SOXL,BUY,1,40\n'
        result=self.ops.import_ledger('date.csv',raw);self.assertEqual(result['issues'][0]['kind'],'date_difference_candidate')
        payload={'state':{'securities':[{'id':'s','symbol':'SOXL'}],'trades':[{'id':'t','securityId':'s','date':'2026-01-02','side':'BUY','qty':1,'price':40}], 'holdingReferences':{'s':{'qty':38,'avg':164.2326}}}}
        preview=self.ops.archive_json('mismatch.json',json.dumps(payload).encode());self.assertTrue(preview['issues'])
        with self.assertRaises(ValueError):self.ops.commit_migration(preview['source'])
    def test_cancellation_preserves_received_response_and_stops_following_calls(self):
        key=self.league.import_snapshot({'symbol':'SOXL','source':'test','synthetic':True,'price_basis':'synthetic','rows':prices()})['id']
        original=self.transport.factory
        def factory(role,config):
            adapter=original(role,config);generate=adapter.generate_structured_report
            def call(*args):
                with self.ai.db() as db:run_id=db.execute('SELECT id FROM ai_run').fetchone()[0]
                self.ai.cancel(run_id);return generate(*args)
            adapter.generate_structured_report=call;return adapter
        self.ai.adapter_factory=factory
        result=self.ai.start(key,inline=True);self.assertEqual(result['state'],'held');self.assertEqual(len(self.transport.calls),1)
        self.assertEqual(sum(s['state']=='completed' for s in result['steps']),1);self.assertEqual(len(result['result']['unrun_steps']),11)
    def test_replay_recomputes_cash_and_detects_validly_hashed_arithmetic_error(self):
        a=self.feed('source-a');books={name:{'cash':10000.0,'qty':0.0,'fees':0.0,'turnover':0.0,'equity':10000.0} for name in list('ABCD')+['BUY_HOLD','SIMPLE_MA']+['INDEPENDENT_'+t for t in 'ABCD']}
        books['A']['cash']=9999.0
        self.ops.add('portfolio_mark','2026-10-26',{'date':'2026-10-26','snapshot_id':a['snapshot_id'],'close':prices()[-1]['close'],'books':books})
        self.assertTrue(self.ops.verify()['valid'])
        with self.assertRaises(ValueError):self.ops.replay()

    def test_restore_rejects_rehashed_but_semantically_corrupt_log(self):
        self.feed('source-a');backups=Backups(self.root);backup=backups.create();path=backups.directory/backup['name']
        with zipfile.ZipFile(path) as z:entries={n:z.read(n) for n in z.namelist()}
        temp=self.root/'corrupt.sqlite3';temp.write_bytes(entries['operations.sqlite3'])
        with sqlite3.connect(temp) as db:db.execute('DROP TRIGGER op_event_no_update');db.execute("UPDATE op_event SET payload='{}' WHERE id=1")
        entries['operations.sqlite3']=temp.read_bytes();manifest=json.loads(entries['manifest.json']);manifest['files']['operations.sqlite3']=__import__('hashlib').sha256(entries['operations.sqlite3']).hexdigest();entries['manifest.json']=json.dumps(manifest).encode()
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            for n,raw in entries.items():z.writestr(n,raw)
        with self.assertRaises(ValueError):backups.restore(out.getvalue(),True)

class PromotionPolicyTests(unittest.TestCase):
    setUp=OperationsTests.setUp
    tearDown=OperationsTests.tearDown
    def history(self):
        # Constructed, scored history tests eligibility policy, not live financial performance.
        with patch('operations.utcnow',return_value='2019-01-02T00:00:00+00:00'):rule=self.ops.rules({})
        days=sessions_between('2019-01-02','2024-01-02');records={};original=self.ai
        for i in range(30):
            index=65+i*21;signal=days[index];rows=[]
            change=[-.004,0,.004][i%3]
            for j,day in enumerate(days[index-60:index+1]):
                price=round(100*(1+change)**j,6);rows.append({'date':day,'open':price,'high':price+1,'low':max(.01,price-1),'close':price})
            snap=self.league.import_snapshot({'symbol':'SOXL','source':'constructed verified history','synthetic':False,'price_basis':'unadjusted','rows':rows})['id']
            run_id='constructed-history-'+str(i);teams=[]
            for t,ret,brier in [('A',.01,.30),('B',.02,.25),('C',.03,.20),('D',.04,.10)]:
                teams.append({'team':t,'net_return':ret,'brier_score_up':brier,'close_equity_mdd':.1 if t=='D' else .2,
                    'shadow_candidate_net_return':.02 if t=='A' else ret-.01,'shadow_candidate_mdd':.1,
                    'shadow_candidate_brier':.2 if t=='A' else brier,'shadow_evaluation_supported':True})
            outcome={'horizon':20,'evaluation_type':'prospective_unverified_source','end_date':days[index+20],'teams':teams,'market_validation':[{'gate':'dual_market_source','passed':True}]}
            config={'profiles':self.ops.profiles(),'leaders':self.ops.leaders()}
            created=signal+'T21:00:00+00:00'
            with self.ai.db() as db:db.execute('INSERT INTO ai_run VALUES(?,?,?,?,?)',(run_id,snap,0,created,json.dumps(config)))
            records[run_id]={'id':run_id,'state':'completed','audit_valid':True,'config':config,'outcomes':[outcome],
                            'result':{'python_gates':[{'gate':'dual_market_source','passed':True}]}}
        class History:
            def db(inner):return original.db()
            def get_run(inner,key):return records[key]
        self.ops.ai=History();return rule
    def test_eligible_disjoint_future_history_recommends_team_and_shadow_leader(self):
        rule=self.history();review=self.ops.review(rule['id'],datetime(2026,10,27,tzinfo=timezone.utc))
        self.assertEqual(review['samples'],30);self.assertEqual(len(review['regimes']),3)
        self.assertEqual(review['status'],'recommended');self.assertEqual(review['replace_team'],'A');self.assertIn('A',review['leader_candidates'])
        result=self.ops.apply_promotion(review['id']);self.assertEqual(result['profiles']['A'],'도전자');self.assertEqual(self.ops.profiles()['D'],'추세·모멘텀')
        self.assertEqual(self.ops.apply_promotion(review['id']),result)
        with self.assertRaises(ValueError):self.ops.apply_promotion(review['id'],'A')
        self.assertTrue(self.ops.verify()['valid'])
    def test_shadow_leader_switches_only_after_eligible_explicit_review(self):
        rule=self.history();review=self.ops.review(rule['id'],datetime(2026,10,27,tzinfo=timezone.utc))
        record=self.ops.apply_promotion(review['id'],'A');self.assertEqual(record['leaders']['A'],'candidate')
        self.assertEqual(self.ops.leaders()['A'],'candidate')
        repeated=self.ops.review(rule['id'],datetime(2026,10,27,tzinfo=timezone.utc));self.assertEqual(repeated['status'],'deferred')
        self.assertTrue(any('버전' in reason for reason in repeated['reasons']))

class BrokerFormatTests(unittest.TestCase):
    setUp=OperationsTests.setUp
    tearDown=OperationsTests.tearDown
    def test_kiwoom_order_execution_and_unfilled_rows_preserve_lineage(self):
        text='Version=1.0\n주문일자,종목번호,체결수량\n주문시간,매도수,체결단가\n2026/1/2,SOXL,10\n09:30:00,매수,100\n2026/1/5,SOXL,0\n09:30:00,미체결,0\n'
        result=self.ops.import_ledger('주문.csv',text.encode('cp949'))
        self.assertEqual(result['accepted'],1);self.assertEqual(result['zero_qty_skipped'],1)
        self.assertEqual(result['trades'][0]['line'],4);self.assertEqual(result['positions']['SOXL']['qty'],'10')
        self.assertTrue(result['format_metadata']['warnings'])
        with self.ops.db() as db:self.assertEqual(db.execute('SELECT encoding FROM raw_file').fetchone()[0],'cp949')
    def test_kiwoom_three_row_trade_fee_tax_and_settlement_date(self):
        text='Version=1.0\n거래일자,구분,거래수량,\n종목코드,매도수,거래단가,\n거래소,결제일자,수수료(외),세금(외)\n2026/1/2,매매,10,\nSOXL,매수,100,\nNYS,2026/1/5,2,1\n'
        result=self.ops.import_ledger('거래정산.csv',text.encode('cp949'))
        self.assertEqual(result['accepted'],1);self.assertEqual(result['trades'][0]['line'],5)
        self.assertEqual(result['trades'][0]['order_date'],'');self.assertEqual(result['trades'][0]['settlement_date'],'2026-01-05')
        self.assertEqual(result['positions']['SOXL']['cost_including_fees'],'1003');self.assertEqual(result['total_taxes'],'1')
    def test_incomplete_broker_detail_is_not_fabricated(self):
        text='Version=1.0\n거래일자,구분,거래수량\n종목코드,매도수,거래단가\n거래소,정산일자,수수료(외)\n2026/1/2,매매,10\nSOXL,매수,100\n'
        with self.assertRaises(ValueError):self.ops.import_ledger('incomplete.csv',text.encode('cp949'))
        with self.ops.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM raw_file').fetchone()[0],1)
