import io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from providers import valid_model,GeminiAdapter
from ai_league import Config
from settings_store import Settings
from test_ai_league import configured_env

class ReportModelTests(unittest.TestCase):
    def test_nontext_model_rejected_before_provider_call(self):
        for model in ('gemini-3.8-flash-tts','gemini-2.5-flash-preview-tts','gemini-live-test','gemini-test-image','gemini-test-native-audio'):
            with self.assertRaisesRegex(ValueError,'전용 모델'):GeminiAdapter('fixture-key',model,0,0)
        self.assertEqual(valid_model('gemini-3.8-flash'),'gemini-3.8-flash')
    def test_list_excludes_nontext_models_even_with_generatecontent(self):
        models=[{'name':'models/'+m,'supportedGenerationMethods':['generateContent']} for m in ('gemini-3.8-flash','gemini-3.8-flash-tts','gemini-test-image')]
        adapter=GeminiAdapter('fixture-key','gemini-probe',0,0,lambda *a,**kw:io.BytesIO(json.dumps({'models':models}).encode()))
        self.assertEqual(adapter.list_models(),['gemini-3.8-flash'])
    def test_role_override_checked_even_when_common_model_valid(self):
        env=configured_env(PL_GEMINI_MODEL='gemini-3.8-flash',PL_TEAM_A_MODEL='gemini-3.8-flash-tts')
        with self.assertRaisesRegex(ValueError,'개별 모델'):Config.from_env(env)
    def test_bad_model_not_saved_and_clearing_override_keeps_keys(self):
        with tempfile.TemporaryDirectory() as d,patch.dict('os.environ',{},clear=True):
            store=Settings(Path(d),codec=False);store.save({'keys':{'TEAM_A':'fixture-kept-key'}})
            with self.assertRaises(ValueError):store.save({'values':{'PL_GEMINI_MODEL':'gemini-3.8-flash-tts'}})
            store.save({'values':{'PL_GEMINI_MODEL':'gemini-3.8-flash','PL_TEAM_A_MODEL':''}})
            self.assertEqual(store.secrets()['TEAM_A'],'fixture-kept-key')
