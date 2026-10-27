"""Normalize supported broker layouts without guessing missing executions."""
import csv,io,re
from datetime import date

def day(value):
    value=str(value or '').strip();m=re.fullmatch(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})',value)
    if m:return date(*(int(x) for x in m.groups())).isoformat()
    return value

def compact(value):return re.sub(r'\s','',str(value or ''))
def numeric(value):return re.sub(r'[,\s$₩원]','',str(value or ''))

def normalize(text):
    sample='\n'.join(text.splitlines()[:8])
    try:dialect=csv.Sniffer().sniff(sample,delimiters=',;\t')
    except csv.Error:dialect=csv.excel
    rows=list(csv.reader(io.StringIO(text),dialect));rows=[r for r in rows if any(x.strip() for x in r)]
    if not rows:raise ValueError('거래 원본이 비어 있습니다.')
    def cell(r,c):return str(rows[r][c]).strip() if 0<=r<len(rows) and 0<=c<len(rows[r]) else ''
    def find(header,*names):return next((i for i,v in enumerate(header) if compact(v).lower() in [n.lower() for n in names]),-1)
    fields=['date','symbol','side','qty','price','fees','tax','trade_id','order_date','settlement_date','source_line'];records=[];warnings=[];skipped=0
    kiwoom=bool(re.match(r'^Version\s*=\s*1\.0',cell(0,0),re.I))
    kind='normalized-csv'
    if kiwoom and compact(cell(1,0))=='주문일자' and compact(cell(2,0))=='주문시간':
        kind='kiwoom-order-execution';h1,h2=rows[1],rows[2];sy=find(h1,'종목번호');qi=find(h1,'체결수량');si=find(h2,'매도수');pi=find(h2,'체결단가')
        if min(sy,qi,si,pi)<0:raise ValueError('키움 주문·체결 헤더가 불완전합니다.')
        warnings.append('주문·체결 파일에 수수료가 없습니다. 0은 잠정 계산값이며 별도 수수료 검산이 필요합니다.')
        r=3
        while r<len(rows):
            if not re.fullmatch(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}',cell(r,0)):r+=1;continue
            if not re.fullmatch(r'\d{1,2}:\d{2}:\d{2}',cell(r+1,0)):raise ValueError('주문·체결 상세행이 불완전합니다.')
            side={'매수':'BUY','매도':'SELL'}.get(compact(cell(r+1,si)),cell(r+1,si))
            records.append({'source_line':r+1,'date':day(cell(r,0)),'order_date':day(cell(r,0)),'symbol':cell(r,sy).lstrip("'\""),'side':side,'qty':numeric(cell(r,qi)),'price':numeric(cell(r+1,pi)),'fees':'0','tax':'0'})
            r+=2
    elif kiwoom and compact(cell(1,0))=='거래일자' and compact(cell(2,0))=='종목코드':
        kind='kiwoom-trade-settlement';h1,h2,h3=rows[1],rows[2],rows[3];qi=find(h1,'거래수량');pi=next((i for i,h in enumerate(h2) if '거래단가' in compact(h)),-1);fi=find(h3,'수수료(외)');ti=find(h3,'세금(외)','제세금(외)','세금');settle=find(h3,'정산일자','결제일자')
        if min(qi,pi,fi)<0:raise ValueError('키움 거래·정산 헤더가 불완전합니다.')
        r=4
        while r<len(rows):
            if not re.fullmatch(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}',cell(r,0)):r+=1;continue
            if r+2>=len(rows):raise ValueError('거래·정산 상세 3행이 불완전합니다.')
            if compact(cell(r,1))!='매매':skipped+=1;r+=3;continue
            records.append({'source_line':r+1,'date':day(cell(r,0)),'symbol':cell(r+1,0).lstrip("'\""),'side':{'매수':'BUY','매도':'SELL'}.get(compact(cell(r+1,1)),cell(r+1,1)),
                'qty':numeric(cell(r,qi)),'price':numeric(cell(r+1,pi)),'fees':numeric(cell(r+2,fi)) or '0','tax':numeric(cell(r+2,ti)) or '0','settlement_date':day(cell(r+2,settle))})
            r+=3
    else:
        aliases={'date':('date','거래일','거래일자','날짜'),'symbol':('symbol','종목코드','종목번호'),'side':('side','유형','구분','매매구분'),
                 'qty':('qty','수량','거래수량','체결수량'),'price':('price','체결가','체결단가'),'fees':('fees','수수료'),'tax':('tax','세금'),
                 'trade_id':('trade_id','거래id','체결번호'),'order_date':('order_date','주문일자'),'settlement_date':('settlement_date','정산일자','결제일자')}
        indices={k:find(rows[0],*names) for k,names in aliases.items()}
        if any(indices[k]<0 for k in ('date','symbol','side','qty','price')):raise ValueError('date,symbol,side,qty,price 또는 대응하는 한국어 열이 필요합니다.')
        for r in range(1,len(rows)):
            item={k:cell(r,i) for k,i in indices.items()};item['side']={'매수':'BUY','매도':'SELL'}.get(compact(item['side']),item['side'].upper())
            for k in ('date','order_date','settlement_date'):item[k]=day(item[k])
            for k in ('qty','price','fees','tax'):item[k]=numeric(item[k])
            item['source_line']=r+1;records.append(item)
    out=io.StringIO();writer=csv.DictWriter(out,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(records)
    return out.getvalue(),{'format':kind,'warnings':warnings,'non_trade_skipped':skipped}
