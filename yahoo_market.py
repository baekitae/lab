"""Explicit Yahoo chart reader. No keys, browser-challenge bypass or fallback."""
import json,math,re,urllib.request,urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

MAX_BYTES=3_000_000

def parse_chart(raw,symbol,cutoff):
    if len(raw)>MAX_BYTES:raise ValueError('Yahoo 시세 응답 크기가 허용 한도를 초과했습니다.')
    try:data=json.loads(raw)
    except (ValueError,UnicodeError):raise ValueError('Yahoo가 JSON 시세 대신 다른 응답을 반환했습니다. 제공처 상태를 확인하세요.') from None
    try:
        chart=data['chart']
        if chart.get('error'):raise ValueError('Yahoo가 종목 조회를 거부했습니다. 종목 또는 제공처 상태를 확인하세요.')
        results=chart['result']
        if not isinstance(results,list) or len(results)!=1:raise KeyError()
        result=results[0];meta=result['meta']
        if meta.get('symbol','').upper()!=symbol:raise ValueError('Yahoo 응답의 종목이 요청과 다릅니다.')
        if meta.get('currency')!='USD' or meta.get('exchangeTimezoneName')!='America/New_York':raise ValueError('이 연결은 미국 거래소의 USD 일봉만 지원합니다.')
        timestamps=result['timestamp'];quotes=result['indicators']['quote']
        if not isinstance(timestamps,list) or len(quotes)!=1:raise KeyError()
        quote=quotes[0];fields=('open','high','low','close')
        if any(not isinstance(quote.get(k),list) or len(quote[k])!=len(timestamps) for k in fields):raise KeyError()
        rows=[];seen=set()
        for i,stamp in enumerate(timestamps):
            if type(stamp) not in (int,float) or not math.isfinite(stamp):raise KeyError()
            day=datetime.fromtimestamp(stamp,ZoneInfo('America/New_York')).date().isoformat()
            if day>cutoff:continue
            values=[quote[k][i] for k in fields]
            if all(v is None for v in values):continue
            if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in values):raise ValueError('Yahoo 일봉에 누락되거나 유효하지 않은 가격이 있습니다. 저장하지 않았습니다.')
            row={'date':day,**dict(zip(fields,values))}
            if day in seen or row['low']>min(row['open'],row['close']) or row['high']<max(row['open'],row['close']):raise ValueError('Yahoo 일봉 날짜 또는 OHLC 범위가 올바르지 않습니다.')
            seen.add(day);rows.append(row)
        splits={}
        for event in result.get('events',{}).get('splits',{}).values():
            day=datetime.fromtimestamp(event['date'],ZoneInfo('America/New_York')).date().isoformat()
            if day>cutoff:continue
            numerator,denominator=event['numerator'],event['denominator']
            if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in (numerator,denominator)):raise KeyError()
            if day in splits:raise KeyError()
            ratio=numerator/denominator
            if not math.isfinite(ratio) or ratio<=0:raise KeyError()
            splits[day]=ratio
        if len(rows)<60:raise ValueError('Yahoo에서 종료된 거래일의 일봉을 60개 이상 확보하지 못했습니다.')
        return sorted(rows,key=lambda r:r['date']),splits
    except (KeyError,TypeError,AttributeError,OverflowError,OSError):raise ValueError('Yahoo 일봉 응답 구조가 올바르지 않습니다. 해당 응답은 시세로 저장하지 않았습니다.') from None

def fetch_chart(symbol,cutoff):
    if not re.fullmatch('[A-Z][A-Z0-9.-]{0,11}',symbol):raise ValueError('미국 주식 종목을 확인하세요.')
    url='https://query1.finance.yahoo.com/v8/finance/chart/'+symbol+'?range=1y&interval=1d&events=splits'
    req=urllib.request.Request(url,headers={'User-Agent':'PatternLabResearch/2.0','Accept':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=30) as response:raw=response.read(MAX_BYTES+1)
    except urllib.error.HTTPError as exc:
        code=exc.code;exc.close()
        raise ValueError(f'Yahoo 시세 요청이 거부되었습니다 (HTTP {code}). 반복 호출하지 말고 제공처 상태·이용 조건을 확인하세요. 다른 공급원으로 자동 전환하지 않습니다.') from None
    except (urllib.error.URLError,TimeoutError,OSError):raise ValueError('Yahoo 시세 서버에 연결하지 못했습니다. 학교망·인터넷·제공처 상태를 확인하세요.') from None
    rows,splits=parse_chart(raw,symbol,cutoff)
    return raw,rows,splits
