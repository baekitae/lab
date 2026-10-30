import io,json,unittest,urllib.error
from providers import GeminiAdapter,ProviderError

class DiagnosticTests(unittest.TestCase):
    def request_failure(self,body,status=400):
        calls=[]
        def opener(req,timeout):
            calls.append(req)
            raise urllib.error.HTTPError(req.full_url,status,'provider error',{},io.BytesIO(json.dumps(body).encode()))
        adapter=GeminiAdapter('secret-diagnostic-fixture','gemini-test',0,0,opener)
        with self.assertRaises(ProviderError) as error:adapter.generate_structured_report('private system',{}, {'type':'OBJECT'})
        self.assertEqual(len(calls),1);self.assertFalse(error.exception.retryable)
        self.assertNotIn('secret-diagnostic-fixture',str(error.exception));self.assertNotIn('private system',str(error.exception))
        return error.exception
    def test_invalid_key_reason_without_echoing_metadata(self):
        error=self.request_failure({'error':{'message':'private system secret-diagnostic-fixture','details':[{'reason':'API_KEY_INVALID','metadata':{'key':'secret-diagnostic-fixture'}}]}})
        self.assertEqual(error.code,'api_key_invalid')
    def test_unsupported_generation_options_are_distinguished(self):
        for message,code in [('thinking_config.thinking_level unsupported','thinking_unsupported'),('generation_config.response_schema invalid','schema_unsupported'),('temperature is not supported','temperature_unsupported')]:
            error=self.request_failure({'error':{'message':message+' private system secret-diagnostic-fixture'}})
            self.assertEqual(error.code,code)
    def test_unknown_error_discards_message_and_reports_model_and_http(self):
        error=self.request_failure({'error':{'message':'private system secret-diagnostic-fixture'}})
        self.assertEqual(error.code,'invalid_request');self.assertIn('HTTP 400',str(error));self.assertIn('gemini-test',str(error))
    def test_malformed_body_remains_safe(self):
        error=self.request_failure(['secret-diagnostic-fixture']);self.assertEqual(error.code,'invalid_request')
    def test_auth_and_rate_status_are_not_reclassified_by_arbitrary_text(self):
        error=self.request_failure({'error':{'message':'temperature secret-diagnostic-fixture'}},403);self.assertEqual(error.code,'access_denied')
