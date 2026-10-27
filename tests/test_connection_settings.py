"""Connection settings regressions; no provider traffic or credentials."""
import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import server
from settings_store import Settings

class ConnectionSettingsTests(unittest.TestCase):
    def test_kis_save_does_not_require_gemini_and_invalidates_cached_token(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{},clear=True):
            settings=Settings(Path(d))
            with patch.object(server,'SETTINGS',settings),patch.object(server,'_token','old-token'),patch.object(server,'_token_expires',9999999999),patch.object(server,'_quote_cache',{'stale':{}}):
                result=server.save_app_settings({'keys':{'KIS_APPKEY':'fake-kis-appkey','KIS_APPSECRET':'fake-kis-secret'}})
                self.assertTrue(all(result['market_credentials'].values()))
                self.assertNotIn('fake-kis-secret',str(result))
                self.assertIsNone(server._token)
                self.assertEqual(server._quote_cache,{})
                server.save_app_settings({'keys':{}})
                self.assertEqual(server.load_credentials()['appkey'],'fake-kis-appkey')
    def test_check_requires_pair_and_respects_emergency(self):
        with patch.object(server.OPS,'emergency',return_value={'enabled':True}),patch.object(server,'_get_token') as token:
            with self.assertRaisesRegex(ValueError,'긴급 중지'):server.check_kis_connection()
            token.assert_not_called()
        with patch.object(server.OPS,'emergency',return_value={'enabled':False}),patch.object(server,'load_credentials',return_value={}):
            with self.assertRaisesRegex(ValueError,'둘 다'):server.check_kis_connection()
    def test_check_returns_safe_auth_result_or_safe_failure(self):
        with patch.object(server.OPS,'emergency',return_value={'enabled':False}),patch.object(server,'load_credentials',return_value={'appkey':'fake-key','appsecret':'fake-secret'}):
            with patch.object(server,'_get_token',return_value=('fake-token',{})):
                result=server.check_kis_connection()
                self.assertTrue(result['authenticated'])
                self.assertNotIn('fake-token',str(result))
            with patch.object(server,'_get_token',side_effect=RuntimeError('fake-secret')):
                with self.assertRaisesRegex(ValueError,'인증에 실패') as error:server.check_kis_connection()
                self.assertNotIn('fake-secret',str(error.exception))

    def test_kis_history_failure_exposes_safe_code_not_response_secrets(self):
        from connectors import MarketConnectors
        from unittest.mock import Mock
        ops=Mock();ops.emergency.return_value={'enabled':False}
        ops.last.return_value={'enabled':True,'license_confirmed':True,'symbol':'SOXL','exchange':'AMS'}
        connector=MarketConnectors(ops,Mock(side_effect=RuntimeError('HTTP 403 EGW00123 secret-provider-response')))
        with self.assertRaises(ValueError) as error:connector.fetch('kis')
        self.assertIn('HTTP 403',str(error.exception))
        self.assertIn('EGW00123',str(error.exception))
        self.assertNotIn('secret-provider-response',str(error.exception))
        ops.league.import_snapshot.assert_not_called()
