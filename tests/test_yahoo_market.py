import copy,json,unittest
from datetime import datetime,timezone,timedelta
from unittest.mock import Mock,patch
from yahoo_market import parse_chart,fetch_chart
from connectors import MarketConnectors

class YahooTests(unittest.TestCase):
    def payload(self):
        start=datetime(2026,1,1,14,30,tzinfo=timezone.utc)
        return {'chart':{'error':None,'result':[{'meta':{'symbol':'SOXL','currency':'USD','exchangeTimezoneName':'America/New_York'},'timestamp':[int((start+timedelta(days=i)).timestamp()) for i in range(62)],'indicators':{'quote':[{k:[40.]*62 for k in ('open','high','low','close')}],'adjclose':[{'adjclose':[1.]*62}]},'events':{}}]}}
    def parse(self,p):return parse_chart(json.dumps(p).encode(),'SOXL','2026-03-01')
    def test_reads_ohlc_without_substituting_dividend_adjusted_close(self):
        rows,splits=self.parse(self.payload());self.assertEqual(len(rows),60);self.assertEqual(rows[-1]['date'],'2026-03-01');self.assertEqual(rows[0]['close'],40);self.assertEqual(splits,{})
    def test_new_york_dates_and_future_bars_excluded(self):
        p=self.payload();p['chart']['result'][0]['timestamp'][0]=int(datetime(2026,1,2,1,tzinfo=timezone.utc).timestamp())
        rows,_=self.parse(p);self.assertEqual(rows[0]['date'],'2026-01-01')
    def test_split_events_come_from_response_and_exclude_future(self):
        p=self.payload();r=p['chart']['result'][0]
        r['events']={'splits':{'past':{'date':r['timestamp'][0],'numerator':2,'denominator':1},'future':{'date':r['timestamp'][-1],'numerator':3,'denominator':1}}}
        _,splits=self.parse(p);self.assertEqual(splits,{'2026-01-01':2.})
    def test_rejects_bad_symbol_currency_lengths_null_and_duplicate(self):
        base=self.payload()
        variants=[]
        p=copy.deepcopy(base);p['chart']['result'][0]['meta']['symbol']='OTHER';variants.append(p)
        p=copy.deepcopy(base);p['chart']['result'][0]['meta']['currency']='KRW';variants.append(p)
        p=copy.deepcopy(base);p['chart']['result'][0]['indicators']['quote'][0]['open']=[1];variants.append(p)
        p=copy.deepcopy(base);p['chart']['result'][0]['indicators']['quote'][0]['open'][0]=None;variants.append(p)
        p=copy.deepcopy(base);p['chart']['result'][0]['timestamp'][1]=p['chart']['result'][0]['timestamp'][0];variants.append(p)
        for p in variants:
            with self.assertRaises(ValueError):self.parse(p)
        with self.assertRaises(ValueError):parse_chart(b'<html>challenge</html>','SOXL','2026-03-01')
    def test_config_requires_terms_and_correct_basis(self):
        connector=MarketConnectors(Mock(),None)
        for body in ({'provider':'yahoo','enabled':True,'price_basis':'split-adjusted'},{'provider':'yahoo','enabled':True,'license_confirmed':True,'price_basis':'unadjusted'}):
            with self.assertRaises(ValueError):connector.configure(body)
    def test_fetch_preserves_raw_provenance_and_actual_split_metadata(self):
        ops=Mock();ops.emergency.return_value={'enabled':False};ops.last.return_value={'provider':'yahoo','symbol':'SOXL','enabled':True,'license_confirmed':True,'price_basis':'split-adjusted'}
        ops.league.import_snapshot.return_value={'id':'fixture-snapshot'}
        rows=[{'date':'2026-01-01','open':40,'high':41,'low':39,'close':40}]
        with patch('connectors.fetch_chart',return_value=(b'fixture-json',rows,{'2026-01-01':2.})),patch('connectors.latest_closed',return_value={'date':'2026-01-01'}):
            MarketConnectors(ops,None).fetch('yahoo')
        ops.preserve.assert_called_once_with('yahoo-SOXL-2026-01-01.json',b'fixture-json','utf-8')
        metadata=ops.feed.call_args.args[1];self.assertEqual(metadata['provider'],'yahoo');self.assertEqual(metadata['splits'],{'2026-01-01':2.})
    def test_request_failure_has_no_fallback_and_does_not_store_prices(self):
        import urllib.error
        with patch('yahoo_market.urllib.request.urlopen',side_effect=urllib.error.HTTPError('https://example',429,'limited',{},None)) as request:
            with self.assertRaisesRegex(ValueError,'429'):fetch_chart('SOXL','2026-03-01')
            self.assertEqual(request.call_count,1)
