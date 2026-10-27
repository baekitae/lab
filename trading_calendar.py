"""NYSE session boundaries from the pinned exchange-calendars package."""
from datetime import datetime,timezone,timedelta,date
from functools import lru_cache

@lru_cache(maxsize=1)
def calendar():
    try:import exchange_calendars as xc
    except ImportError:raise ValueError('거래일 캘린더가 필요합니다. 설치 후 실행 파일을 이용하거나 pip install -r requirements.txt를 실행하세요.') from None
    return xc.get_calendar('XNYS',start='2000-01-01',end='2035-12-31')

def session_info(day):
    cal=calendar()
    if not cal.is_session(day):return None
    close=cal.session_close(day).to_pydatetime()
    return {'date':str(day)[:10],'open':cal.session_open(day).to_pydatetime().isoformat(),
            'close':close.isoformat(),'calendar':'XNYS','early_close':close.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).hour<16}

def latest_closed(now=None):
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:raise ValueError('시각에는 타임존이 필요합니다.')
    day=now.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date()
    for _ in range(12):
        info=session_info(day.isoformat())
        if info and datetime.fromisoformat(info['close'])<=now:return info
        day-=timedelta(days=1)
    raise ValueError('최근 종료 거래일을 찾을 수 없습니다.')

def next_session(day):
    return calendar().date_to_session((date.fromisoformat(str(day)[:10])+timedelta(days=1)).isoformat(),direction='next').date().isoformat()

def sessions_between(start,end):
    return [t.date().isoformat() for t in calendar().sessions_in_range(start,end)]
