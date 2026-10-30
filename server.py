#!/usr/bin/env python3
"""Pattern Lab v1.3.3 Unified local API server for Korea and US stocks through KIS Open API.
Uses Python standard library only. It does not place orders.
"""
from __future__ import annotations
import csv, json, os, re, time, threading, secrets, base64, urllib.error, urllib.parse, urllib.request, webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from league import League,utcnow
from ai_league import AILeague, MeetingError, Config
from providers import GeminiAdapter, ProviderError
from settings_store import SETTINGS
from operations import Operations
from backups import Backups
from connectors import MarketConnectors,PROVIDERS
from web_config import WebConfig

WEB=WebConfig()
ROOT = Path(__file__).resolve().parent
DATA_DIR = SETTINGS.root
DATA_DIR.mkdir(parents=True,exist_ok=True)
CREDS_PATH = ROOT / 'kis_credentials.json'
HOST, PORT = ('0.0.0.0',int(os.environ.get('PORT','8770'))) if WEB.enabled else ('127.0.0.1',8770)
LEAGUE = League(DATA_DIR / 'league.sqlite3')
AI_LEAGUE = AILeague(DATA_DIR / 'ai_league.sqlite3', LEAGUE)
OPS=Operations(DATA_DIR/'operations.sqlite3',LEAGUE,AI_LEAGUE)
AI_LEAGUE.gate_checker=OPS.gates
AI_LEAGUE.profile_loader=OPS.profiles
AI_LEAGUE.leader_loader=OPS.leaders
AI_LEAGUE.safety_checker=lambda:OPS.emergency()['enabled']
BACKUPS=Backups(DATA_DIR)
CSRF_TOKEN=secrets.token_urlsafe(32)
SCHEDULER_STOP=threading.Event()
BASE_URL = 'https://openapi.koreainvestment.com:9443'
TOKEN_URL = BASE_URL + '/oauth2/tokenP'
QUOTE_URL = BASE_URL + '/uapi/overseas-price/v1/quotations/price'
HISTORY_URL = BASE_URL + '/uapi/overseas-price/v1/quotations/dailyprice'
EXCHANGES = {'AMS': 'AMEX / NYSE Arca', 'NAS': 'NASDAQ', 'NYS': 'NYSE'}
DOMESTIC_QUOTE_URL = BASE_URL + '/uapi/domestic-stock/v1/quotations/inquire-price'
DOMESTIC_HISTORY_URL = BASE_URL + '/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice'

_token = None
_token_expires = 0.0
_token_lock = threading.Lock()
_quote_cache = {}
_quote_lock = threading.Lock()


def load_credentials():
    env=SETTINGS.effective_env()
    if env.get('PL_KIS_APPKEY_API_KEY') and env.get('PL_KIS_APPSECRET_API_KEY'):
        return {'appkey':env['PL_KIS_APPKEY_API_KEY'],'appsecret':env['PL_KIS_APPSECRET_API_KEY']}
    if not CREDS_PATH.exists():
        return {}
    try:
        data = json.loads(CREDS_PATH.read_text(encoding='utf-8'))
        if data.get('appkey') and data.get('appsecret'):
            return data
    except Exception:
        pass
    return {}


def save_credentials(appkey, appsecret):
    SETTINGS.save({'keys':{'KIS_APPKEY':appkey,'KIS_APPSECRET':appsecret}})
    global _token, _token_expires
    _token, _token_expires = None, 0.0


def save_app_settings(body):
    global _token, _token_expires
    result = SETTINGS.save(body)
    if any(role in body.get('keys', {}) or role in body.get('delete_keys', [])
           for role in ('KIS_APPKEY', 'KIS_APPSECRET')):
        with _token_lock:
            _token, _token_expires = None, 0.0
        with _quote_lock:
            _quote_cache.clear()
    return result


def check_kis_connection():
    if OPS.emergency()['enabled']:
        raise ValueError('긴급 중지를 해제한 후 연결을 확인하세요.')
    if not load_credentials():
        raise ValueError('App Key와 App Secret을 둘 다 저장한 후 확인하세요.')
    try:
        _get_token()
    except Exception:
        raise ValueError('한국투자 인증에 실패했습니다. 실전투자용 키, Open API 신청 상태, 인터넷 연결을 확인하세요. 키는 삭제되지 않았습니다.') from None
    return {'authenticated': True, 'message': '한국투자 인증 성공. 저장된 키로 토큰을 발급받았습니다. 시세 수신은 ② 시장 데이터에서 별도로 갱신하세요. 실제 주문은 실행하지 않습니다.'}


def _request_json_with_headers(url, method='GET', headers=None, body=None, timeout=25):
    data = None if body is None else json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode('utf-8-sig')
            return json.loads(raw), {str(k).lower(): str(v) for k, v in response.headers.items()}
    except urllib.error.HTTPError as e:
        code=e.code;e.close()
        raise RuntimeError(f'시세 제공처 HTTP 오류 {code}. 연동 설정과 공급원 상태를 확인하세요.') from None
    except urllib.error.URLError as e:
        raise RuntimeError(f'시세 제공처에 연결하지 못했습니다: {e.reason}') from e
    except json.JSONDecodeError as e:
        raise RuntimeError('시세 제공처 응답을 JSON으로 해석하지 못했습니다.') from e


def _request_json(url, method='GET', headers=None, body=None, timeout=25):
    return _request_json_with_headers(url, method, headers, body, timeout)[0]


def _get_token(force=False):
    global _token, _token_expires
    creds = load_credentials()
    if not creds:
        raise RuntimeError('한국투자 API 실전투자용 App Key와 App Secret을 먼저 저장해 주세요.')
    with _token_lock:
        if not force and _token and time.time() < _token_expires - 60:
            return _token, creds
        result = _request_json(TOKEN_URL, method='POST', headers={'content-type':'application/json; charset=utf-8'}, body={
            'grant_type':'client_credentials', 'appkey':creds['appkey'], 'appsecret':creds['appsecret']
        })
        token = result.get('access_token')
        if not token:
            raise RuntimeError(result.get('error_description') or result.get('msg1') or 'API 토큰 발급에 실패했습니다. 실전투자용 키와 API 신청 상태를 확인해 주세요.')
        _token = token
        _token_expires = time.time() + float(result.get('expires_in', 86400))
        return _token, creds


def _kis_get(url, tr_id, params, retry=True, tr_cont='', include_headers=False):
    token, creds = _get_token()
    query = urllib.parse.urlencode(params)
    headers = {
        'authorization': 'Bearer ' + token,
        'appkey': creds['appkey'],
        'appsecret': creds['appsecret'],
        'tr_id': tr_id,
        'custtype': 'P',
        'content-type': 'application/json; charset=utf-8',
        'tr_cont': tr_cont,
    }
    result, response_headers = _request_json_with_headers(url + '?' + query, headers=headers)
    if result.get('rt_cd') not in (None, '0', 0):
        message = str(result.get('msg1') or result.get('msg_cd') or 'API 요청이 거부되었습니다.')
        if retry and ('토큰' in message or 'token' in message.lower() or result.get('msg_cd') in ('EGW00123', 'EGW00121')):
            global _token, _token_expires
            _token, _token_expires = None, 0.0
            token, creds = _get_token(force=True)
            headers['authorization'] = 'Bearer ' + token
            result, response_headers = _request_json_with_headers(url + '?' + query, headers=headers)
            if result.get('rt_cd') in (None, '0', 0):
                return (result, response_headers) if include_headers else result
        raise RuntimeError(f'한국투자 API 오류 ({result.get("msg_cd", "코드 미상")}): {message}')
    return (result, response_headers) if include_headers else result


def get_ws_approval_key():
    creds = load_credentials()
    if not creds:
        raise RuntimeError('한국투자 API App Key와 App Secret을 먼저 저장해 주세요.')
    result = _request_json(BASE_URL + '/oauth2/Approval', method='POST', headers={'content-type':'application/json; charset=utf-8'}, body={
        'grant_type':'client_credentials', 'appkey':creds['appkey'], 'secretkey':creds['appsecret']
    })
    key = result.get('approval_key')
    if not key:
        raise RuntimeError(result.get('error_description') or result.get('msg1') or '실시간 시세 접속키를 발급하지 못했습니다.')
    return key


def validate_symbol(symbol):
    symbol = str(symbol or '').strip().upper()
    if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,11}', symbol):
        raise ValueError('미국 주식 티커를 입력해 주세요. 예: SOXL, SOXS, NVDA, AAPL')
    return symbol


def validate_exchange(exchange):
    exchange = str(exchange or 'AMS').strip().upper()
    if exchange not in EXCHANGES:
        raise ValueError('거래소는 AMS(AMEX/NYSE Arca), NAS(NASDAQ), NYS(NYSE) 중에서 선택해 주세요.')
    return exchange


def _to_float(value):
    try:
        if value is None or str(value).strip() == '': return 0.0
        return float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return 0.0


def _first_present(obj, names):
    for name in names:
        value = obj.get(name)
        if value is not None and str(value).strip() != '':
            return value
    return None


def _as_output_object(result):
    out = result.get('output') or {}
    if isinstance(out, list):
        return out[0] if out else {}
    return out if isinstance(out, dict) else {}


def _exchange_candidates(exchange):
    first = validate_exchange(exchange)
    return [first] + [x for x in ('AMS', 'NAS', 'NYS') if x != first]


def get_overseas_quote(symbol, exchange='AMS', force=False):
    symbol = validate_symbol(symbol)
    requested_exchange = validate_exchange(exchange)
    cache_key = f'{requested_exchange}:{symbol}'
    with _quote_lock:
        cached = _quote_cache.get(cache_key)
        if cached and not force and time.time() - cached['_epoch'] < 5:
            return {k:v for k,v in cached.items() if k != '_epoch'}

    errors = []
    for excd in _exchange_candidates(requested_exchange):
        try:
            result = _kis_get(QUOTE_URL, 'HHDFS00000300', {'AUTH':'', 'EXCD':excd, 'SYMB':symbol})
            out = _as_output_object(result)
            price = _to_float(_first_present(out, ('last','last_price','ovrs_nmix_prpr','stck_prpr','price','clos')))
            if price <= 0:
                errors.append(f'{excd}: 현재가 항목 없음')
                continue
            sign = str(_first_present(out, ('sign','prdy_vrss_sign')) or '')
            diff_abs = abs(_to_float(_first_present(out, ('diff','prdy_vrss'))))
            change = 0.0 if sign == '3' else (-diff_abs if sign in ('4','5') else diff_abs)
            pct_raw = _to_float(_first_present(out, ('rate','prdy_ctrt','changePct','change_rate')))
            if sign in ('4','5'):
                pct = -abs(pct_raw)
            elif sign == '3':
                pct = 0.0
            else:
                pct = pct_raw
            item = {
                'symbol': symbol, 'exchange': excd, 'exchangeName': EXCHANGES[excd],
                'price': price, 'change': change, 'changePct': pct,
                'volume': _to_float(_first_present(out, ('tvol','acml_vol','volume'))),
                'currency': 'USD',
                'fetchedAt': datetime.now().astimezone().isoformat(timespec='seconds'),
                'source': '한국투자 Open API 해외주식 현재체결가', '_epoch': time.time(),
            }
            with _quote_lock:
                _quote_cache[cache_key] = item
            return {k:v for k,v in item.items() if k != '_epoch'}
        except Exception as exc:
            errors.append(f'{excd}: {exc}')
    raise RuntimeError('해외주식 현재가 조회에 실패했습니다. 티커/거래소 및 실전투자 API 권한을 확인해 주세요. ' + ' | '.join(errors[:2]))


def to_iso(raw):
    s = str(raw or '').strip()
    if re.fullmatch(r'\d{8}', s): return f'{s[:4]}-{s[4:6]}-{s[6:8]}'
    return s[:10]


def _parse_overseas_bars(result):
    values = result.get('output2') or result.get('output') or []
    if isinstance(values, dict): values = [values]
    rows = []
    if not isinstance(values, list): return rows
    for item in values:
        if not isinstance(item, dict): continue
        raw_date = _first_present(item, ('xymd','stck_bsop_date','bsop_date','date','bymd','tymd'))
        close = _to_float(_first_present(item, ('clos','close','stck_clpr','last','last_price')))
        if not raw_date or close <= 0: continue
        rows.append({
            'date': to_iso(raw_date),
            'open': _to_float(_first_present(item, ('open','stck_oprc','oprc'))) or close,
            'high': _to_float(_first_present(item, ('high','stck_hgpr','hgpr'))) or close,
            'low': _to_float(_first_present(item, ('low','stck_lwpr','lwpr'))) or close,
            'close': close,
            'volume': _to_float(_first_present(item, ('tvol','acml_vol','volume'))),
        })
    # Deduplicate any overlapping dates returned by page boundaries.
    return sorted({r['date']:r for r in rows}.values(), key=lambda r:r['date'])


def cache_path(symbol, exchange):
    safe_symbol = validate_symbol(symbol)
    safe_exchange = validate_exchange(exchange)
    return DATA_DIR / f'{safe_exchange}_{safe_symbol}_daily.csv'


def read_cache(symbol, exchange):
    path = cache_path(symbol, exchange)
    if not path.exists(): return []
    rows = []
    try:
        with path.open('r', newline='', encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                if r.get('date') and _to_float(r.get('close')) > 0:
                    rows.append({'date':r['date'][:10], 'open':_to_float(r.get('open')), 'high':_to_float(r.get('high')), 'low':_to_float(r.get('low')), 'close':_to_float(r.get('close')), 'volume':_to_float(r.get('volume'))})
    except Exception:
        return []
    return sorted({r['date']:r for r in rows}.values(), key=lambda r:r['date'])


def write_cache(symbol, exchange, rows):
    path = cache_path(symbol, exchange)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=['date','open','high','low','close','volume'])
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def _fetch_history_page(symbol, exchange, bymd, tr_cont=''):
    params = {'AUTH':'', 'EXCD':exchange, 'SYMB':symbol, 'GUBN':'0', 'BYMD':bymd.strftime('%Y%m%d'), 'MODP':'1'}
    result, response_headers = _kis_get(HISTORY_URL, 'HHDFS76240000', params, tr_cont=tr_cont, include_headers=True)
    return _parse_overseas_bars(result), response_headers.get('tr_cont', '').upper()


def _collect_history(symbol, exchange, start, today, cached_rows, refresh):
    rows_by_date = {r['date']:r for r in cached_rows}
    oldest_cached = date.fromisoformat(cached_rows[0]['date']) if cached_rows else None
    newest_cached = date.fromisoformat(cached_rows[-1]['date']) if cached_rows else None
    full_fetch_needed = oldest_cached is None or oldest_cached > start
    stale_cutoff = today - timedelta(days=45)
    # If cache already covers the requested period, refresh recent prices only.
    target_date = start if full_fetch_needed else stale_cutoff
    if not refresh and cached_rows and newest_cached and newest_cached >= today - timedelta(days=5) and oldest_cached <= start:
        return [r for r in cached_rows if start.isoformat() <= r['date'] <= today.isoformat()], True, None

    cursor = today
    warning = None
    seen_pages = set()
    max_pages = 30
    tr_cont = ''
    for page_no in range(max_pages):
        try:
            page, next_cont = _fetch_history_page(symbol, exchange, cursor, tr_cont)
        except Exception as exc:
            if not cached_rows and not rows_by_date:
                raise
            warning = str(exc)
            break
        if not page:
            break
        new_count = 0
        for row in page:
            if row['date'] not in rows_by_date:
                new_count += 1
            rows_by_date[row['date']] = row
        oldest_text = min(r['date'] for r in page)
        page_signature = f'{oldest_text}:{max(r["date"] for r in page)}:{len(page)}'
        if page_signature in seen_pages or new_count == 0:
            # The API may have reached its oldest bar or repeated a page.
            break
        seen_pages.add(page_signature)
        oldest_date = date.fromisoformat(oldest_text)
        if oldest_date <= target_date:
            break
        # KIS uses tr_cont=N to request the next continuation page. Keep the
        # same BYMD while the response header indicates more pages.
        if next_cont in ('M', 'F'):
            tr_cont = 'N'
        else:
            # Defensive fallback for gateways that omit continuation metadata:
            # advance the query date to the oldest bar seen so far.
            next_cursor = oldest_date - timedelta(days=1)
            if next_cursor >= cursor:
                break
            cursor = next_cursor
            tr_cont = ''
        time.sleep(0.12)

    merged = sorted(rows_by_date.values(), key=lambda r:r['date'])
    if merged:
        write_cache(symbol, exchange, merged)
    filtered = [r for r in merged if start.isoformat() <= r['date'] <= today.isoformat()]
    return filtered, bool(cached_rows), warning


def get_overseas_history(symbol, exchange='AMS', days=1095, refresh=False):
    symbol = validate_symbol(symbol)
    exchange = validate_exchange(exchange)
    days = max(60, min(int(days or 1095), 1825))
    today = date.today()
    start = today - timedelta(days=days)
    cached_rows = read_cache(symbol, exchange)
    errors = []
    used_exchange = exchange
    filtered = []
    was_cached = bool(cached_rows)
    warning = None
    for excd in _exchange_candidates(exchange):
        cached = read_cache(symbol, excd)
        try:
            result_rows, cached_used, update_warning = _collect_history(symbol, excd, start, today, cached, refresh)
            if len(result_rows) >= 25:
                filtered, was_cached, warning, used_exchange = result_rows, cached_used, update_warning, excd
                break
            errors.append(f'{excd}: 조회 가능한 데이터 {len(result_rows)}개')
        except Exception as exc:
            errors.append(f'{excd}: {exc}')
    if len(filtered) < 25:
        raise RuntimeError('해외주식 과거 일봉을 충분히 가져오지 못했습니다. 티커/거래소, API 신청 권한과 호출 제한을 확인해 주세요. ' + ' | '.join(errors[:3]))
    return {
        'symbol':symbol, 'exchange':used_exchange, 'exchangeName':EXCHANGES[used_exchange],
        'currency':'USD', 'rows':filtered, 'firstDate':filtered[0]['date'], 'lastDate':filtered[-1]['date'],
        'cached':was_cached, 'count':len(filtered), 'updatedAt':datetime.now().astimezone().isoformat(timespec='seconds'), 'warning':warning
    }


# -------------------------
# Domestic Korean stocks / ETFs
# -------------------------
def validate_domestic_symbol(symbol):
    symbol = str(symbol or '').strip().upper()
    if not re.fullmatch(r'\d{6}|Q\d{6}', symbol):
        raise ValueError('국내 종목은 6자리 종목코드(예: 005930)를 입력해 주세요. ETN은 Q로 시작하는 코드를 사용할 수 있습니다.')
    return symbol


def get_domestic_quote(symbol, force=False):
    symbol = validate_domestic_symbol(symbol)
    cache_key = f'KR:{symbol}'
    with _quote_lock:
        cached = _quote_cache.get(cache_key)
        if cached and not force and time.time() - cached['_epoch'] < 5:
            return {k:v for k,v in cached.items() if k != '_epoch'}
    result = _kis_get(DOMESTIC_QUOTE_URL, 'FHKST01010100', {
        'FID_COND_MRKT_DIV_CODE':'J', 'FID_INPUT_ISCD':symbol
    })
    out = _as_output_object(result)
    price = _to_float(_first_present(out, ('stck_prpr','prpr','price','last')))
    if price <= 0:
        raise RuntimeError('국내주식 현재가 항목을 찾지 못했습니다. 종목코드와 API 권한을 확인해 주세요.')
    sign = str(_first_present(out, ('prdy_vrss_sign','sign')) or '')
    change_abs = abs(_to_float(_first_present(out, ('prdy_vrss','diff'))))
    if sign in ('4','5'):
        change = -change_abs
    elif sign == '3':
        change = 0.0
    else:
        change = change_abs
    pct_raw = _to_float(_first_present(out, ('prdy_ctrt','rate','changePct')))
    pct = -abs(pct_raw) if sign in ('4','5') else (0.0 if sign == '3' else pct_raw)
    item = {
        'symbol':symbol, 'market':'KR', 'exchange':'J', 'exchangeName':'KRX',
        'price':price, 'change':change, 'changePct':pct,
        'volume':_to_float(_first_present(out, ('acml_vol','cntg_vol','volume'))),
        'currency':'KRW', 'fetchedAt':datetime.now().astimezone().isoformat(timespec='seconds'),
        'source':'한국투자 Open API 국내주식 현재가', '_epoch':time.time()
    }
    with _quote_lock:
        _quote_cache[cache_key] = item
    return {k:v for k,v in item.items() if k != '_epoch'}


def domestic_cache_path(symbol):
    return DATA_DIR / f'KR_{validate_domestic_symbol(symbol)}_daily.csv'


def read_domestic_cache(symbol):
    path = domestic_cache_path(symbol)
    if not path.exists(): return []
    rows=[]
    try:
        with path.open('r', newline='', encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                if r.get('date') and _to_float(r.get('close')) > 0:
                    rows.append({'date':r['date'][:10], 'open':_to_float(r.get('open')), 'high':_to_float(r.get('high')), 'low':_to_float(r.get('low')), 'close':_to_float(r.get('close')), 'volume':_to_float(r.get('volume'))})
    except Exception:
        return []
    return sorted({r['date']:r for r in rows}.values(), key=lambda r:r['date'])


def write_domestic_cache(symbol, rows):
    path=domestic_cache_path(symbol); tmp=path.with_suffix('.tmp')
    with tmp.open('w', newline='', encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f, fieldnames=['date','open','high','low','close','volume'])
        writer.writeheader(); writer.writerows(rows)
    tmp.replace(path)


def _parse_domestic_bars(result):
    values = result.get('output2') or []
    if isinstance(values, dict): values=[values]
    rows=[]
    if not isinstance(values, list): return rows
    for item in values:
        if not isinstance(item, dict): continue
        raw_date = _first_present(item, ('stck_bsop_date','date','xymd'))
        close = _to_float(_first_present(item, ('stck_clpr','clos','close')))
        if not raw_date or close <= 0: continue
        rows.append({'date':to_iso(raw_date), 'open':_to_float(_first_present(item, ('stck_oprc','open'))) or close,
                     'high':_to_float(_first_present(item, ('stck_hgpr','high'))) or close,
                     'low':_to_float(_first_present(item, ('stck_lwpr','low'))) or close,
                     'close':close, 'volume':_to_float(_first_present(item, ('acml_vol','tvol','volume')))})
    return sorted({r['date']:r for r in rows}.values(), key=lambda r:r['date'])


def _fetch_domestic_range(symbol, start_day, end_day):
    # KIS domestic daily item-chart endpoint returns at most 100 records per call.
    # 90-calendar-day chunks stay below the trading-day cap and avoid oversized responses.
    rows=[]; cur=start_day
    while cur <= end_day:
        chunk_end=min(end_day, cur+timedelta(days=89))
        result=_kis_get(DOMESTIC_HISTORY_URL, 'FHKST03010100', {
            'FID_COND_MRKT_DIV_CODE':'J', 'FID_INPUT_ISCD':symbol,
            'FID_INPUT_DATE_1':cur.strftime('%Y%m%d'), 'FID_INPUT_DATE_2':chunk_end.strftime('%Y%m%d'),
            'FID_PERIOD_DIV_CODE':'D', 'FID_ORG_ADJ_PRC':'0'
        })
        rows.extend(_parse_domestic_bars(result))
        cur=chunk_end+timedelta(days=1)
        if cur <= end_day: time.sleep(0.12)
    return sorted({r['date']:r for r in rows}.values(), key=lambda r:r['date'])


def get_domestic_history(symbol, days=1095, refresh=False):
    symbol=validate_domestic_symbol(symbol)
    days=max(60,min(int(days or 1095),1825))
    today=date.today(); start=today-timedelta(days=days)
    cached=read_domestic_cache(symbol); cache_map={r['date']:r for r in cached}
    if not cached:
        rows=_fetch_domestic_range(symbol,start,today)
    else:
        rows=cached
        # On refresh update the latest 90 days and fill any missing older beginning.
        if refresh:
            recent_start=max(start,today-timedelta(days=89))
            recent=_fetch_domestic_range(symbol,recent_start,today)
            cache_map.update({r['date']:r for r in recent})
            rows=sorted(cache_map.values(),key=lambda r:r['date'])
            if rows and rows[0]['date'] > start.isoformat():
                older_end=date.fromisoformat(rows[0]['date'])-timedelta(days=1)
                if older_end >= start:
                    older=_fetch_domestic_range(symbol,start,older_end)
                    cache_map.update({r['date']:r for r in older})
                    rows=sorted(cache_map.values(),key=lambda r:r['date'])
        elif len(cached)<25:
            rows=_fetch_domestic_range(symbol,start,today)
    rows=[r for r in rows if start.isoformat() <= r['date'] <= today.isoformat()]
    rows=sorted({r['date']:r for r in rows}.values(),key=lambda r:r['date'])
    if len(rows)<25:
        raise RuntimeError(f'국내주식 과거 일봉을 충분히 가져오지 못했습니다 ({len(rows)}행). 종목코드, 실전 API 권한과 호출 제한을 확인해 주세요.')
    write_domestic_cache(symbol,rows)
    return {'symbol':symbol,'market':'KR','exchange':'J','exchangeName':'KRX','currency':'KRW','rows':rows,
            'firstDate':rows[0]['date'],'lastDate':rows[-1]['date'],'cached':bool(cached), 'count':len(rows),
            'updatedAt':datetime.now().astimezone().isoformat(timespec='seconds'),'warning':None}


CONNECTORS=MarketConnectors(OPS,get_overseas_history)
OPS.refresh_sources_callback=CONNECTORS.refresh

class Handler(BaseHTTPRequestHandler):
    server_version = 'PatternLabLocal/2.0'
    def parse_request(self):
        if not super().parse_request():return False
        if WEB.enabled:
            if self.command=='GET' and self.path=='/healthz':return True
            if not WEB.authenticated(self.headers.get('Authorization','')):
                self.send_response(401)
                self.send_header('WWW-Authenticate','Basic realm="Pattern Lab", charset="UTF-8"')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length','0')
                self.end_headers()
                return False
        return True

    def origin_allowed(self):
        origin=self.headers.get('Origin','')
        allowed=(WEB.origin,) if WEB.enabled else (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}')
        return not origin or origin in allowed

    def end_headers(self):
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','same-origin')
        if WEB.enabled:self.send_header('Strict-Transport-Security','max-age=31536000')
        super().end_headers()
    def log_message(self, fmt, *args):
        # Never log credential bodies or secret headers.
        print('[PatternLab]',self.command,urllib.parse.urlsplit(self.path).path,str(args[1]) if len(args)>1 else '')

    def send_json(self, code, payload):
        try:env=SETTINGS.effective_env()
        except ValueError:env=dict(os.environ)
        private=[v for k,v in env.items() if k.startswith('PL_') and k.endswith('_API_KEY') and isinstance(v,str) and len(v)>=8]
        try:
            credentials=load_credentials()
            private.extend(credentials[k] for k in ('appkey','appsecret') if isinstance(credentials.get(k),str) and len(credentials[k])>=8)
        except ValueError:pass
        def redact(value):
            if isinstance(value,str):
                for secret in private:value=value.replace(secret,'[비밀 값 숨김]')
                return value
            if isinstance(value,list):return [redact(x) for x in value]
            if isinstance(value,dict):return {k:redact(v) for k,v in value.items()}
            return value
        payload=redact(payload)
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers(); self.wfile.write(raw)

    def local_host(self):
        if WEB.enabled:return self.headers.get('Host','')==WEB.authority
        port=self.server.server_port
        return self.headers.get('Host','') in (f'127.0.0.1:{port}',f'localhost:{port}')

    def do_GET(self):
        if WEB.enabled and self.path=='/healthz':return self.send_json(200,{'ok':True})
        if not self.local_host():return self.send_json(421,{'error':'로컬 호스트에서만 접근할 수 있습니다.'})
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == '/api/app/state':
                return self.send_json(200,{'release':'2.0.0','build':'2026-10-30.3','csrf':CSRF_TOKEN,'settings':SETTINGS.view(),'ai':AI_LEAGUE.overview(),
                    'operations':OPS.overview(),'snapshots':LEAGUE.overview()['snapshots'],'backups':BACKUPS.list(),
                    'connectors':[OPS.last('connector_config',p) for p in PROVIDERS],'legacy_kis_file_present':CREDS_PATH.exists()})
            if parsed.path=='/api/app/backup/download':
                name=(params.get('name') or [''])[0]
                if not re.fullmatch(r'backup-[0-9T]+Z-[a-f0-9]{8}\.zip',name):raise ValueError('백업 이름을 확인하세요.')
                raw=(BACKUPS.directory/name).read_bytes()
                self.send_response(200);self.send_header('Content-Type','application/zip');self.send_header('Content-Disposition',f'attachment; filename="{name}"');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
            if parsed.path=='/app':
                raw=(ROOT/'app.html').read_bytes();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
            if parsed.path == '/api/ai':
                return self.send_json(200, AI_LEAGUE.overview())
            if parsed.path == '/api/ai/run':
                return self.send_json(200, AI_LEAGUE.get_run((params.get('id') or [''])[0]))
            if parsed.path == '/ai':
                raw = (ROOT / 'ai_league.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(raw)
                return
            if parsed.path == '/api/league':
                return self.send_json(200, LEAGUE.overview())
            if parsed.path == '/league':
                raw = (ROOT / 'league.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(raw)
                return
            if parsed.path == '/api/status':
                configured = bool(load_credentials())
                symbols = len(list(DATA_DIR.glob('*_daily.csv')))
                return self.send_json(200, {'configured':configured,'cacheSymbols':symbols,'serverTime':datetime.now().astimezone().isoformat(timespec='seconds'),'provider':'한국투자 Open API 국내·해외주식'})
            if parsed.path == '/api/quote':
                market = (params.get('market') or ['US'])[0].upper()
                symbol = (params.get('symbol') or [''])[0]
                exchange = (params.get('exchange') or ['AMS'])[0]
                force = (params.get('force') or ['0'])[0] == '1'
                if market == 'KR':
                    result = get_domestic_quote(symbol, force=force)
                elif market == 'US':
                    result = get_overseas_quote(symbol, exchange, force=force)
                    result['market'] = 'US'
                else:
                    raise ValueError('market는 KR 또는 US여야 합니다.')
                return self.send_json(200, result)
            if parsed.path == '/api/history':
                market = (params.get('market') or ['US'])[0].upper()
                symbol = (params.get('symbol') or [''])[0]
                exchange = (params.get('exchange') or ['AMS'])[0]
                days = int((params.get('days') or ['1095'])[0])
                refresh = (params.get('refresh') or ['0'])[0] == '1'
                if market == 'KR':
                    result = get_domestic_history(symbol, days, refresh)
                elif market == 'US':
                    result = get_overseas_history(symbol, exchange, days, refresh)
                    result['market'] = 'US'
                else:
                    raise ValueError('market는 KR 또는 US여야 합니다.')
                return self.send_json(200, result)
            if parsed.path in ('/','/stock_pattern_lab.html'):
                path = ROOT / 'stock_pattern_lab.html'
                raw = path.read_bytes()
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw)
                return
            if parsed.path == '/favicon.ico':
                self.send_response(204);self.end_headers();return
            self.send_json(404, {'error':'요청한 경로를 찾을 수 없습니다.'})
        except ValueError as e:
            self.send_json(400, {'error':str(e)})
        except Exception as e:
            self.send_json(502, {'error':str(e) or '데이터를 불러오지 못했습니다.'})

    def post_league(self, path):
        origin = self.headers.get('Origin', '')
        if not self.origin_allowed():
            return self.send_json(403, {'error': '로컬 프로그램에서만 연구 기록을 추가할 수 있습니다.'})
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            return self.send_json(415, {'error': 'application/json 요청이 필요합니다.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 3000000:
                raise ValueError('요청 크기는 1~3,000,000바이트여야 합니다.')
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(payload, dict):
                raise ValueError('JSON 객체를 입력하세요.')
            if path.endswith('/snapshots'):
                result = LEAGUE.import_snapshot(payload)
            elif path.endswith('/evaluations'):
                result = LEAGUE.evaluate(payload.get('snapshot_id'), payload.get('evaluation_snapshot_id'))
            else:
                result = LEAGUE.run(payload.get('snapshot_id'))
            return self.send_json(200, result)
        except (ValueError, UnicodeError) as exc:
            return self.send_json(400, {'error': str(exc)})
        except Exception:
            return self.send_json(500, {'error': '연구 기록 처리에 실패했습니다. 서버 로그와 저장소 상태를 확인하세요.'})

    def post_ai(self, path):
        origin = self.headers.get('Origin', '')
        if not self.origin_allowed():
            return self.send_json(403, {'error': '로컬 프로그램에서만 AI 회의를 실행할 수 있습니다.'})
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            return self.send_json(415, {'error': 'application/json 요청이 필요합니다.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 10000:
                raise MeetingError('AI 요청 크기가 올바르지 않습니다.')
            data = json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(data, dict):
                raise MeetingError('JSON 객체를 입력하세요.')
            if path == '/api/ai/evaluations':
                return self.send_json(200, AI_LEAGUE.evaluate(data.get('run_id'), data.get('evaluation_snapshot_id')))
            if path == '/api/ai/market':
                if not load_credentials():
                    raise MeetingError('한국투자 연동 설정이 필요합니다. 기존 보유·거래 관리 화면에서 설정하세요.')
                result = get_overseas_history(data.get('symbol'), data.get('exchange', 'AMS'), 1095, refresh=False)
                return self.send_json(200, LEAGUE.import_snapshot({'symbol': result['symbol'],
                    'source': '한국투자 Open API 해외주식 일봉', 'synthetic': False,
                    'price_basis': 'provider-adjustment-unverified', 'rows': result['rows']}))
            if path == '/api/ai/check':
                role = data.get('role', 'TEAM_A')
                from ai_league import ROLES
                if role not in ROLES:
                    raise MeetingError('지원하지 않는 역할입니다.')
                env=SETTINGS.effective_env()
                key = env.get(f'PL_{role}_API_KEY', '')
                model = env.get(f'PL_{role}_MODEL') or env.get('PL_GEMINI_MODEL','')
                provider = GeminiAdapter(key, model, 0, 0)
                return self.send_json(200, provider.health_check())
            retry = data.get('retry', False)
            if not isinstance(retry, bool):
                raise MeetingError('retry는 boolean이어야 합니다.')
            with OPS.lock:result=AI_LEAGUE.start(data.get('snapshot_id'),retry=retry)
            return self.send_json(202,result)
        except (ValueError, UnicodeError) as exc:
            return self.send_json(400, {'error': str(exc)})
        except ProviderError as exc:
            return self.send_json(502, {'error': str(exc), 'code': exc.code})
        except Exception:
            return self.send_json(500, {'error': 'AI 회의 요청을 처리하지 못했습니다. 키 값은 기록하지 않습니다.'})

    def post_app(self,path):
        port=self.server.server_port
        if not self.origin_allowed():
            return self.send_json(403,{'error':'다른 사이트에서는 설정을 변경할 수 없습니다.'})
        if not secrets.compare_digest(self.headers.get('X-Pattern-Lab-Token',''),CSRF_TOKEN):
            return self.send_json(403,{'error':'화면을 새로고침한 뒤 다시 시도하세요.'})
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':return self.send_json(415,{'error':'JSON 요청이 필요합니다.'})
        try:
            size=int(self.headers.get('Content-Length','0'))
            limit=150000000 if path=='/api/app/backup/restore' else 16000000
            if not 0<size<=limit:raise ValueError('요청 크기 한도를 초과했습니다.')
            body=json.loads(self.rfile.read(size).decode('utf-8'))
            if not isinstance(body,dict):raise ValueError('JSON 객체가 필요합니다.')
            with OPS.lock:
                if path=='/api/app/emergency':result=OPS.emergency(body.get('enabled'),body.get('reason','operator_pause'))
                elif path=='/api/app/settings/recover':result=SETTINGS.recover(body.get('confirmed') is True)
                elif path=='/api/app/meeting/cancel':result=AI_LEAGUE.cancel(body.get('run_id'))
                elif path=='/api/app/settings':result=save_app_settings(body)
                elif path=='/api/app/kis/check':result=check_kis_connection()
                elif path=='/api/app/models':
                    role=body.get('role','TEAM_A')
                    if role not in __import__('ai_league').ROLES:raise ValueError('역할을 확인하세요.')
                    env=SETTINGS.effective_env();provider=GeminiAdapter(env.get(f'PL_{role}_API_KEY',''),'gemini-probe',0,0)
                    result={'models':provider.list_models(),'role':role,'generation_verified':False}
                elif path in ('/api/app/market/csv','/api/app/ledger/import','/api/app/migration/preview'):
                    raw=base64.b64decode(body.get('content',''),validate=True);name=body.get('filename','')
                    if path.endswith('/csv'):result=OPS.import_market_csv(name,raw,body.get('metadata',{}))
                    elif path.endswith('/import'):result=OPS.import_ledger(name,raw)
                    else:result=OPS.archive_json(name,raw)
                elif path=='/api/app/market/register':result=OPS.feed(body.get('snapshot_id'),body.get('metadata',{}))
                elif path=='/api/app/market/compare':result=OPS.compare(body.get('left'),body.get('right'))
                elif path=='/api/app/connector/config':result=CONNECTORS.configure(body)
                elif path=='/api/app/connector/fetch':result=CONNECTORS.fetch(body.get('provider'))
                elif path=='/api/app/ledger/confirm':result=OPS.confirm_ledger(body.get('ledger_id'),body)
                elif path=='/api/app/ledger/review':result=OPS.review_ledger(body.get('ledger_id'),body.get('resolutions',{}))
                elif path=='/api/app/migration/commit':result=OPS.commit_migration(body.get('source'))
                elif path=='/api/app/paper/queue':result=OPS.queue_orders(body.get('run_id'),body.get('conditions_checked') is True)
                elif path=='/api/app/paper/settle':result=OPS.settle(body.get('snapshot_id'))
                elif path=='/api/app/promotion/rules':result=OPS.rules(body)
                elif path=='/api/app/promotion/review':result=OPS.review(body.get('rule_id'))
                elif path=='/api/app/promotion/apply':result=OPS.apply_promotion(body.get('review_id'),body.get('leader_team'))
                elif path=='/api/app/schedule':result=OPS.schedule(body)
                elif path=='/api/app/schedule/tick':result=OPS.tick()
                elif path=='/api/app/replay':result=OPS.replay()
                elif path in ('/api/app/backup/create','/api/app/backup/restore'):
                    if any(r['state']=='running' for r in AI_LEAGUE.overview()['runs']):raise ValueError('AI 회의가 끝난 후 백업·복원하세요.')
                    if path.endswith('/create'):result=BACKUPS.create()
                    else:
                        result=BACKUPS.restore(base64.b64decode(body.get('content',''),validate=True),body.get('confirm') is True)
                        if result.get('restored'):
                            AI_LEAGUE.recover_interrupted()
                            OPS.add('restored',utcnow(),{'before_restore_backup':result['before_restore_backup']})
                else:return self.send_json(404,{'error':'지원하지 않는 작업입니다.'})
            return self.send_json(200,result)
        except ProviderError as exc:return self.send_json(502,{'error':str(exc),'code':exc.code})
        except (ValueError,TypeError,KeyError,OverflowError) as exc:return self.send_json(400,{'error':str(exc)})
        except Exception:return self.send_json(500,{'error':'작업을 완료하지 못했습니다. 원본과 백업은 보존됩니다.'})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not self.local_host():return self.send_json(421,{'error':'로컬 호스트에서만 접근할 수 있습니다.'})
        if parsed.path.startswith('/api/app/'):
            return self.post_app(parsed.path)
        if parsed.path in ('/api/ai/runs', '/api/ai/check', '/api/ai/market', '/api/ai/evaluations'):
            return self.post_ai(parsed.path)
        if parsed.path in ('/api/league/snapshots', '/api/league/runs', '/api/league/evaluations'):
            return self.post_league(parsed.path)
        if parsed.path not in ('/api/settings','/api/ws-approval'):
            return self.send_json(404, {'error':'요청한 경로를 찾을 수 없습니다.'})
        origin = self.headers.get('Origin','')
        if not self.origin_allowed():
            return self.send_json(403, {'error':'로컬 프로그램에서만 설정을 변경할 수 있습니다.'})
        try:
            n = int(self.headers.get('Content-Length','0'))
            if n > 10000: return self.send_json(400, {'error':'설정 데이터 크기가 올바르지 않습니다.'})
            raw = self.rfile.read(n).decode('utf-8') if n else '{}'
            if parsed.path == '/api/ws-approval':
                return self.send_json(200, {'approvalKey':get_ws_approval_key()})
            data = json.loads(raw)
            appkey = str(data.get('appkey','')).strip()
            appsecret = str(data.get('appsecret','')).strip()
            if not appkey or not appsecret: return self.send_json(400, {'error':'App Key와 App Secret을 모두 입력해 주세요.'})
            if len(appkey)>300 or len(appsecret)>300: return self.send_json(400, {'error':'API 키 길이가 올바르지 않습니다.'})
            save_credentials(appkey, appsecret)
            try:
                _get_token(force=True)
            except Exception as e:
                return self.send_json(400, {'error':'키를 저장했지만 인증 확인에 실패했습니다: '+str(e)})
            return self.send_json(200, {'ok':True,'message':'API 키를 이 PC에 저장하고 인증을 확인했습니다.'})
        except json.JSONDecodeError:
            return self.send_json(400, {'error':'설정 데이터를 JSON으로 읽지 못했습니다.'})
        except Exception as e:
            return self.send_json(502, {'error':str(e) or '설정을 처리하지 못했습니다.'})


def main():
    print('\nPattern Lab AI Team League 2.0 — 통합 운영 서버')
    print(f'브라우저 주소: http://{HOST}:{PORT}')
    print('종료하려면 이 창에서 Ctrl+C를 누르세요.')
    print('API 키는 연결 설정에서 등록하세요. Windows에서는 DPAPI로 암호화합니다.\n')
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print(f'⚠ 포트 {PORT}를 사용할 수 없습니다. 이전 패턴랩 서버가 실행 중일 수 있습니다.')
        print('기존 서버 창에서 Ctrl+C를 눌러 종료한 뒤 다시 실행해 주세요.')
        print(f'상세 오류: {e}')
        if WEB.enabled:raise
        input('종료하려면 Enter를 누르세요...')
        return
    AI_LEAGUE.recover_interrupted()
    def scheduler_loop():
        while not SCHEDULER_STOP.wait(30):
            try:
                with OPS.lock:OPS.tick()
            except Exception as exc:
                try:
                    key=datetime.now().date().isoformat();record={'code':type(exc).__name__,'reason':'자동 회의 작업을 보류했습니다. 캘린더·데이터·연결 설정을 확인하세요.'}
                    if OPS.last('scheduler_failure',key)!=record:OPS.add('scheduler_failure',key,record)
                except Exception:pass
    threading.Thread(target=scheduler_loop,daemon=True,name='pattern-lab-scheduler').start()
    if not WEB.enabled:threading.Timer(0.8, lambda: webbrowser.open(f'http://{HOST}:{PORT}/app')).start()
    try: server.serve_forever()
    except KeyboardInterrupt: print('\n서버를 종료합니다.')
    finally:
        SCHEDULER_STOP.set()
        AI_LEAGUE.shutdown()
        server.server_close()

if __name__ == '__main__': main()
