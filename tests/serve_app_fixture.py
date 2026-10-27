"""Disposable full-app browser test server. No network provider or production data."""
import sys,os,tempfile,json
from pathlib import Path
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer
if __name__=='__main__':
    if sys.argv[1:]!=['--test-only']:raise SystemExit('Test-only: pass --test-only.')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    for name in list(os.environ):
        if name.startswith('PL_'):os.environ.pop(name)
    import server,ai_league,league,operations,trading_calendar
    from settings_store import Settings
    from league import League
    from ai_league import AILeague
    from operations import Operations
    from backups import Backups
    from connectors import MarketConnectors
    from test_ai_league import FakeTransport
    from test_operations import prices,market_csv
    class Clock(datetime):
        current=datetime(2026,10,26,21,tzinfo=timezone.utc)
        @classmethod
        def now(cls,tz=None):return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)
    for module in (league,operations,trading_calendar):module.datetime=Clock
    class Models:
        def __init__(self,*args,**kwargs):pass
        def list_models(self):return ['gemini-test-model']
    with tempfile.TemporaryDirectory(prefix='pattern-lab-app-test-') as folder:
        root=Path(folder);server.AI_LEAGUE.shutdown()
        store=Settings(root,codec=False);server.SETTINGS=ai_league.SETTINGS=store
        server.LEAGUE=League(root/'league.sqlite3');transport=FakeTransport()
        server.AI_LEAGUE=AILeague(root/'ai_league.sqlite3',server.LEAGUE,adapter_factory=transport.factory)
        server.OPS=Operations(root/'operations.sqlite3',server.LEAGUE,server.AI_LEAGUE)
        server.AI_LEAGUE.gate_checker=server.OPS.gates;server.AI_LEAGUE.profile_loader=server.OPS.profiles;server.AI_LEAGUE.leader_loader=server.OPS.leaders;server.AI_LEAGUE.safety_checker=lambda:server.OPS.emergency()['enabled']
        def failed_kis(*a,**kw):raise RuntimeError('fixture-only HTTP 403 EGW00123')
        server.BACKUPS=Backups(root);server.CONNECTORS=MarketConnectors(server.OPS,failed_kis)
        server.CREDS_PATH=root/'no-legacy-credentials.json';server.GeminiAdapter=Models;server.PORT=18772
        server._get_token=lambda: ('fixture-only-token',server.load_credentials())
        base=prices()
        future=base+[{'date':'2026-10-27','open':45,'high':47,'low':44,'close':46},{'date':'2026-10-28','open':46,'high':48,'low':45,'close':47}]
        class Handler(server.Handler):
            def do_GET(self):
                if self.path=='/__test__/fixtures':return self.send_json(200,{'market_csv':market_csv(base).decode(),'future_csv':market_csv(future).decode(),'fixture_only':True})
                return super().do_GET()
            def do_POST(self):
                if self.path=='/__test__/advance':Clock.current=datetime(2026,10,28,21,tzinfo=timezone.utc);return self.send_json(200,{'fixture_only':True})
                return super().do_POST()
        httpd=ThreadingHTTPServer(('127.0.0.1',18772),Handler)
        print('TEST ONLY full-app fixture on port 18772; no Google calls, disposable data.',flush=True)
        try:httpd.serve_forever()
        except KeyboardInterrupt:pass
        finally:server.AI_LEAGUE.shutdown();httpd.server_close()
