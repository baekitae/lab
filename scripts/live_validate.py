"""Explicit live API check. Uses synthetic market-only inputs, never account data."""
import argparse,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ai_league import Config
from providers import GeminiAdapter

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--allow-generations',action='store_true');parser.add_argument('--retry',action='store_true');args=parser.parse_args()
    config=Config.from_env()
    for role,item in config.roles.items():
        result=GeminiAdapter(item.key,item.model,config.input_rate,config.output_rate).health_check()
        print(json.dumps({'role':role,**result},ensure_ascii=False),flush=True)
        if not result['available']:raise SystemExit('해당 역할에서 모델이 제공되지 않습니다.')
    if not args.allow_generations:return
    import server
    from trading_calendar import sessions_between,latest_closed
    days=sessions_between('2026-01-02',latest_closed()['date'])[-100:]
    rows=[]
    for i,day in enumerate(days):
        close=round(40+i*.07,2);rows.append({'date':day,'open':round(close-.1,2),'high':round(close+1,2),'low':round(close-1,2),'close':close})
    snapshot=server.LEAGUE.import_snapshot({'symbol':'SOXL','source':'live API validation / synthetic OHLC v2','synthetic':True,'price_basis':'synthetic','rows':rows})
    run=server.AI_LEAGUE.start(snapshot['id'],retry=args.retry);print('LIVE_RUN',run['id'],flush=True)
    deadline=time.monotonic()+600;last=None
    while time.monotonic()<deadline:
        run=server.AI_LEAGUE.get_run(run['id']);progress=(run['state'],sum(s['state']=='completed' for s in run['steps']))
        if progress!=last:
            print(json.dumps({'state':progress[0],'completed_steps':progress[1],'audit_valid':run['audit_valid']},ensure_ascii=False),flush=True);last=progress
        if run['state']!='running':
            print(json.dumps({'run_id':run['id'],'result':run['result'],'usage':[e['payload'] for e in run['events'] if e['kind']=='provider_usage']},ensure_ascii=False),flush=True)
            server.AI_LEAGUE.shutdown()
            if run['state']!='completed':raise SystemExit(1)
            return
        time.sleep(2)
    server.AI_LEAGUE.cancel(run['id']);server.AI_LEAGUE.shutdown();raise SystemExit('실제 회의 검증 제한 시간을 초과하여 중단을 요청했습니다.')
if __name__=='__main__':main()
