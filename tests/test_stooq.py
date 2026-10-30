import unittest
from unittest.mock import Mock,patch
from connectors import parse_stooq_csv,MarketConnectors

class StooqTests(unittest.TestCase):
    def test_valid_csv_with_bom_and_extra_volume(self):
        rows=parse_stooq_csv(b'\xef\xbb\xbfDate,Open,High,Low,Close,Volume\n2026-10-29,40,42,39,41,100\n','text/plain')
        self.assertEqual(rows,[{'date':'2026-10-29','open':40.,'high':42.,'low':39.,'close':41.}])
    def test_browser_verification_is_not_misreported_as_bad_prices(self):
        with self.assertRaisesRegex(ValueError,'브라우저 확인'):parse_stooq_csv(b'<!DOCTYPE html><html>This site requires JavaScript to verify your browser</html>','text/html')
    def test_nodata_empty_and_bad_header(self):
        for raw in (b'No data',b'',b'Date,Close\n2026-10-29,40'):
            with self.assertRaises(ValueError):parse_stooq_csv(raw)
    def test_rejects_nonfinite_negative_invalid_ohlc_and_bad_date(self):
        for line in ('2026-10-29,nan,42,39,41','2026-10-29,-1,42,39,41','2026-10-29,40,39,38,41','2026-99-29,40,42,39,41'):
            with self.assertRaises(ValueError):parse_stooq_csv(('Date,Open,High,Low,Close\n'+line).encode())
    def test_html_never_creates_snapshot_or_raw_price_record(self):
        ops=Mock();ops.emergency.return_value={'enabled':False};ops.last.return_value={'enabled':True,'license_confirmed':True,'symbol':'SOXL'}
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=b'<html>JavaScript verification</html>';response.headers={'Content-Type':'text/html'}
        with patch('connectors.urllib.request.urlopen',return_value=response):
            with self.assertRaisesRegex(ValueError,'브라우저 확인'):MarketConnectors(ops,None).fetch('stooq')
        ops.preserve.assert_not_called();ops.league.import_snapshot.assert_not_called();ops.feed.assert_not_called()
