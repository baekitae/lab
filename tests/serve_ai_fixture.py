"""Browser test server with explicit fixture transport; never used by production."""
import json
import os
import sys
import tempfile
from http.server import ThreadingHTTPServer
from pathlib import Path

if __name__ == '__main__':
    if sys.argv[1:] != ['--test-only']:
        raise SystemExit('Test-only fixture server. Pass --test-only; no live Gemini requests will be made.')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from test_ai_league import FakeTransport, configured_env
    from test_league import sample, extended
    from ai_league import AILeague, Config
    from league import League
    import server
    os.environ.update(configured_env())
    with tempfile.TemporaryDirectory(prefix='pattern-lab-browser-test-') as folder:
        root = Path(folder)
        server.AI_LEAGUE.shutdown()
        server.LEAGUE = League(root/'research.sqlite3')
        base = server.LEAGUE.import_snapshot(sample())['id']
        future = server.LEAGUE.import_snapshot(extended(sample()))['id']
        transport = FakeTransport()
        server.AI_LEAGUE = AILeague(root/'ai.sqlite3', server.LEAGUE, Config.from_env, transport.factory)
        server.PORT = 18771
        httpd = ThreadingHTTPServer(('127.0.0.1', server.PORT), server.Handler)
        print(json.dumps({'fixture_only': True, 'base': base, 'future': future, 'port': server.PORT}), flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.AI_LEAGUE.shutdown()
            httpd.server_close()
