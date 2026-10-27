"""Read-only market connectors. User confirms source terms and adjustment policy."""
import csv,io,re,urllib.parse,urllib.request
from datetime import datetime,timezone
from trading_calendar import latest_closed

class MarketConnectors:
    def __init__(self,ops,kis_history):self.ops=ops;self.kis_history=kis_history
    def configure(self,body):
        provider=body.get('provider')
        if provider not in ('kis','stooq') or not isinstance(body.get('enabled'),bool):raise ValueError('공급원과 활성화 여부를 확인하세요.')
        if body['enabled'] and body.get('license_confirmed') is not True:raise ValueError('공급원의 이용 조건을 확인하고 동의해야 합니다.')
        if body.get('price_basis') not in ('split-adjusted','unadjusted'):raise ValueError('공급원의 실제 분할 조정 정책을 확인하세요.')
        symbol=str(body.get('symbol','SOXL')).upper()
        if not re.fullmatch('[A-Z][A-Z0-9.-]{0,11}',symbol):raise ValueError('미국 주식 종목을 확인하세요.')
        value={'provider':provider,'enabled':body['enabled'],'license_confirmed':body.get('license_confirmed') is True,
               'price_basis':body['price_basis'],'symbol':symbol,'exchange':body.get('exchange','AMS'),'splits':body.get('splits',{}),
               'configured_at':datetime.now(timezone.utc).isoformat()}
        return self.ops.add('connector_config',provider,value)
    def fetch(self,provider):
        if self.ops.emergency()['enabled']:raise ValueError('긴급 중지 중에는 공급원에 연결하지 않습니다.')
        config=self.ops.last('connector_config',provider)
        if not config or not config['enabled'] or not config['license_confirmed']:raise ValueError('사용 조건과 분할 조정 정책을 확인하고 공급원을 활성화하세요.')
        cutoff=latest_closed()['date'];symbol=config['symbol']
        if provider=='kis':
            try:
                result=self.kis_history(symbol,config['exchange'],1095,refresh=True)
                rows=result['rows']
            except RuntimeError as exc:
                # Return only recognized provider codes, never arbitrary response text.
                codes=list(dict.fromkeys(re.findall(r'\b(?:EGW|APBK|OPSQ|OPSP|AUTH)[A-Z0-9]{3,12}\b|HTTP\s+[1-5][0-9]{2}',str(exc))))
                detail=' 제공처 오류 코드: '+', '.join(codes[:5])+'.' if codes else ''
                raise ValueError('한국투자 과거 일봉 조회에 실패했습니다.'+detail+' ① API 연결 설정에서 한국투자 인증 확인을 먼저 누르세요. 인증이 성공하면 해외주식 시세 조회 권한·호출 제한·종목을 확인한 뒤 다시 갱신하세요. 원본과 저장된 키는 유지됩니다.') from None
        else:
            url='https://stooq.com/q/d/l/?'+urllib.parse.urlencode({'s':symbol.lower()+'.us','i':'d'})
            req=urllib.request.Request(url,headers={'User-Agent':'PatternLabResearch/2.0'})
            try:
                with urllib.request.urlopen(req,timeout=30) as response:raw=response.read(3_000_001)
                if len(raw)>3_000_000:raise ValueError('시세 파일 크기 한도 초과입니다.')
                text=raw.decode('utf-8-sig');rows=[]
                for r in csv.DictReader(io.StringIO(text)):
                    rows.append({'date':r['Date'],**{k:float(r[k.title()]) for k in ('open','high','low','close')}})
            except (KeyError,UnicodeError,ValueError):raise ValueError('공급원이 유효한 OHLC CSV를 반환하지 않았습니다. 이용 조건·종목·제한을 확인하세요.') from None
            except Exception:raise ValueError('Stooq 시세 연결에 실패했습니다. 네트워크·공급원 상태를 확인하세요.') from None
            self.ops.preserve(provider+'-'+symbol+'-'+cutoff+'.csv',raw,'utf-8-sig')
        rows=[r for r in rows if r['date']<=cutoff]
        snap=self.ops.league.import_snapshot({'symbol':symbol,'source':provider,'synthetic':False,'price_basis':config['price_basis'],'rows':rows})
        return self.ops.feed(snap['id'],{**config,'asof':datetime.now(timezone.utc).isoformat()})
    def refresh(self):
        today=datetime.now(timezone.utc).date().isoformat();results=[]
        for provider in ('kis','stooq'):
            config=self.ops.last('connector_config',provider)
            if not config or not config['enabled']:continue
            key=provider+':'+today
            if self.ops.last('automatic_refresh',key):continue
            try:result={'provider':provider,'status':'fetched','feed':self.fetch(provider)}
            except Exception:result={'provider':provider,'status':'held','reason':'자동 수집 실패. 설정·연결 상태를 확인하고 수동 갱신하세요.'}
            self.ops.add('automatic_refresh',key,result);results.append(result)
        feeds=self.ops.events('feed');fresh={}
        for f in feeds:
            if f['cutoff']==latest_closed()['date'] and not f['synthetic']:fresh[f['provider']]=f
        if len(fresh)>=2:
            fs=list(fresh.values())
            try:self.ops.compare(fs[-1]['id'],fs[-2]['id'])
            except ValueError:pass
        return results
