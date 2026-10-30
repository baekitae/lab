"""Read-only market connectors. User confirms source terms and adjustment policy."""
import csv,io,re,math,urllib.parse,urllib.request,urllib.error
from datetime import datetime,timezone
from trading_calendar import latest_closed

def parse_stooq_csv(raw,content_type=''):
    if len(raw)>3_000_000:raise ValueError('Stooq 시세 파일 크기가 허용 한도를 초과했습니다.')
    try:text=raw.decode('utf-8-sig').strip()
    except UnicodeError:raise ValueError('Stooq 응답이 UTF-8 CSV 형식이 아닙니다.') from None
    if not text or text.lower()=='no data':raise ValueError('Stooq에서 해당 종목의 데이터가 없다는 응답을 받았습니다. 종목을 확인하거나 다른 공급원의 CSV를 사용하세요.')
    if 'text/html' in content_type.lower() or re.match(r'(?is)^<(?:!doctype|html|head|body)',text):
        if re.search(r'(?i)javascript|verify your browser|captcha',text):
            raise ValueError('Stooq가 시세 CSV 대신 브라우저 확인 화면을 반환했습니다. 자동 다운로드가 제한된 상태입니다. API 키나 가격 기준의 문제가 아닙니다. 브라우저에서 정상적으로 내려받은 CSV가 있으면 오른쪽 CSV 가져오기를 사용하세요. 한국투자 데이터는 유지됩니다.')
        raise ValueError('Stooq가 시세 CSV 대신 웹페이지를 반환했습니다. 접속 제한 또는 제공처 상태를 확인하세요. 받은 내용을 주가로 저장하지 않았습니다.')
    reader=csv.DictReader(io.StringIO(text));fields=reader.fieldnames or []
    names={name.strip().lower():name for name in fields}
    required=('date','open','high','low','close')
    if not all(k in names for k in required):raise ValueError('Stooq 응답에 날짜·시가·고가·저가·종가 열이 없습니다. 정상적인 일봉 CSV 파일이 필요합니다.')
    rows=[]
    try:
        for r in reader:
            day=r[names['date']].strip()
            if datetime.strptime(day,'%Y-%m-%d').strftime('%Y-%m-%d')!=day:raise ValueError()
            row={'date':day,**{k:float(r[names[k]]) for k in required[1:]}}
            if any(not math.isfinite(row[k]) or row[k]<=0 for k in required[1:]):raise ValueError()
            if row['low']>min(row['open'],row['close']) or row['high']<max(row['open'],row['close']):raise ValueError()
            rows.append(row)
    except (ValueError,TypeError,KeyError,AttributeError):raise ValueError('Stooq CSV에 올바르지 않은 날짜 또는 가격이 있습니다. 해당 응답은 시세로 저장하지 않았습니다.') from None
    if not rows:raise ValueError('Stooq CSV에 가격 행이 없습니다. 해당 종목의 데이터를 확인하세요.')
    return rows

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
                with urllib.request.urlopen(req,timeout=30) as response:
                    raw=response.read(3_000_001);content_type=response.headers.get('Content-Type','')
            except urllib.error.HTTPError as exc:
                code=exc.code;exc.close()
                if code in (403,429):raise ValueError(f'Stooq가 자동 조회를 거부하거나 호출을 제한했습니다 (HTTP {code}). 반복 갱신을 멈추고 제공처 이용 조건을 확인하세요. 한국투자 데이터는 유지됩니다.') from None
                raise ValueError(f'Stooq 서버가 HTTP {code} 오류를 반환했습니다. 잠시 후 제공처 상태를 확인하세요.') from None
            except (urllib.error.URLError,TimeoutError,OSError):raise ValueError('Stooq에 연결하지 못했습니다. 인터넷·학교망·공급원 상태를 확인하세요.') from None
            rows=parse_stooq_csv(raw,content_type)
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
