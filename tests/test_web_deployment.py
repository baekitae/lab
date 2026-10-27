"""Web authentication, same-origin guards and encrypted restart persistence."""
import base64,json,os,tempfile,threading,unittest,urllib.request,urllib.error
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from web_config import WebConfig
from settings_store import Settings,CloudVault
import server

ENV={'PL_WEB_MODE':'1','PL_PUBLIC_ORIGIN':'https://lab.example.test','PL_WEB_USER':'owner','PL_WEB_PASSWORD':'test-only-password-123456789','PL_VAULT_MASTER_KEY':'test-only-master-key-1234567890123456789','PL_DATA_DIR':'/tmp/test-only'}
class WebConfigTests(unittest.TestCase):
    def test_fail_closed_without_required_configuration(self):
        for field in ('PL_PUBLIC_ORIGIN','PL_WEB_PASSWORD','PL_VAULT_MASTER_KEY','PL_DATA_DIR'):
            e=dict(ENV);e.pop(field)
            with self.assertRaises(ValueError):WebConfig(e)
        with self.assertRaises(ValueError):WebConfig({**ENV,'PL_PUBLIC_ORIGIN':'http://unsafe.example'})
        self.assertFalse(WebConfig({}).enabled)
    def test_encrypted_keys_survive_restart_and_wrong_master_cannot_open(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,ENV,clear=True):
            first=Settings(Path(d));first.save({'keys':{'KIS_APPKEY':'fixture-appkey','KIS_APPSECRET':'fixture-appsecret'}})
            self.assertNotIn(b'fixture-appsecret',first.vault.read_bytes())
            second=Settings(Path(d));self.assertEqual(second.secrets()['KIS_APPKEY'],'fixture-appkey')
            self.assertEqual(second.view()['storage'],'cloud-encrypted')
            with self.assertRaises(ValueError):Settings(Path(d),codec=CloudVault('different-test-master-key-1234567890123456')).secrets()

class WebHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,ENV,clear=True);self.env.start()
        self.patches=[patch.object(server,'WEB',WebConfig(ENV)),patch.object(server,'SETTINGS',Settings(Path(self.temp.name)))]
        for p in self.patches:p.start()
        self.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join()
        for p in reversed(self.patches):p.stop()
        self.env.stop();self.temp.cleanup()
    def request(self,path,auth=True,body=None,**headers):
        h={'Host':'lab.example.test',**headers}
        if auth:h['Authorization']='Basic '+base64.b64encode(('owner:'+ENV['PL_WEB_PASSWORD']).encode()).decode()
        if body is not None:h['Content-Type']='application/json'
        req=urllib.request.Request(f'http://127.0.0.1:{self.http.server_port}'+path,data=json.dumps(body).encode() if body is not None else None,headers=h)
        try:r=urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=5)
        except urllib.error.HTTPError as e:r=e
        with r:return r.status,r.headers,r.read()
    def test_all_app_routes_require_login_health_is_minimal(self):
        for path in ('/app','/ai','/api/app/state','/api/app/backup/download?name=x','/api/status'):
            code,h,raw=self.request(path,auth=False);self.assertEqual(code,401);self.assertEqual(raw,b'');self.assertIn('Basic',h['WWW-Authenticate'])
        self.assertEqual(self.request('/api/app/settings',auth=False,body={'keys':{'TEAM_A':'fixture-only-key'}})[0],401)
        code,h,raw=self.request('/healthz',auth=False);self.assertEqual(code,200);self.assertEqual(json.loads(raw),{'ok':True})
        code,h,raw=self.request('/app');self.assertEqual(code,200);self.assertIn(b'globalModel',raw);self.assertEqual(h['X-Frame-Options'],'DENY')
        self.assertEqual(self.request('/app',Host='evil.example')[0],421)
    def test_public_origin_and_csrf_required_for_writes(self):
        code,_,_=self.request('/api/app/settings',body={},Origin='https://evil.example',**{'X-Pattern-Lab-Token':server.CSRF_TOKEN});self.assertEqual(code,403)
        code,_,_=self.request('/api/app/settings',body={},Origin=ENV['PL_PUBLIC_ORIGIN']);self.assertEqual(code,403)
        code,_,raw=self.request('/api/app/settings',body={'keys':{'KIS_APPKEY':'fixture-only-key'}},Origin=ENV['PL_PUBLIC_ORIGIN'],**{'X-Pattern-Lab-Token':server.CSRF_TOKEN});self.assertEqual(code,200);self.assertNotIn(b'fixture-only-key',raw)
