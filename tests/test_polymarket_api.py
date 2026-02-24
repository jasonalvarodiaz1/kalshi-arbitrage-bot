"""Unit tests for PolymarketAPI."""

import unittest
import sys
import os
from unittest.mock import Mock, patch
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polymarket_api import PolymarketAPI


class TestPolymarketAPIRetry(unittest.TestCase):
    """Test retry logic in PolymarketAPI._request_with_retry."""

    def setUp(self):
        self.api = PolymarketAPI()

    def test_successful_request_no_retry(self):
        self.api._local.session = Mock()
        resp = Mock()
        resp.status_code = 200
        self.api._local.session.request.return_value = resp
        result = self.api._request_with_retry('GET', 'https://clob.polymarket.com/markets')
        self.assertEqual(self.api._local.session.request.call_count, 1)
        self.assertEqual(result.status_code, 200)

    def test_retry_on_429(self):
        self.api._local.session = Mock()
        resp_429 = Mock(status_code=429, headers={})
        resp_200 = Mock(status_code=200)
        self.api._local.session.request.side_effect = [resp_429, resp_200]
        with patch('time.sleep'):
            result = self.api._request_with_retry('GET', 'https://test.com/')
        self.assertEqual(self.api._local.session.request.call_count, 2)
        self.assertEqual(result.status_code, 200)

    def test_no_retry_on_404(self):
        self.api._local.session = Mock()
        resp = Mock(status_code=404)
        self.api._local.session.request.return_value = resp
        result = self.api._request_with_retry('GET', 'https://test.com/')
        self.assertEqual(self.api._local.session.request.call_count, 1)
        self.assertEqual(result.status_code, 404)

    def test_connection_error_returns_none_after_exhaustion(self):
        self.api._local.session = Mock()
        self.api._local.session.request.side_effect = requests.exceptions.ConnectionError("fail")
        with patch('time.sleep'):
            result = self.api._request_with_retry('GET', 'https://test.com/', max_retries=2)
        self.assertIsNone(result)
        self.assertEqual(self.api._local.session.request.call_count, 3)


class TestPolymarketAPIGetMarkets(unittest.TestCase):
    def setUp(self):
        self.api = PolymarketAPI(api_key='test-key')

    def _mock_response(self, data, status=200):
        resp = Mock()
        resp.status_code = status
        resp.json.return_value = data
        return resp

    def test_get_markets_returns_list_and_cursor(self):
        markets = [{'condition_id': 'abc', 'question': 'Will BTC hit 100k?'}]
        resp = self._mock_response({'data': markets, 'next_cursor': 'XYZ'})
        with patch.object(self.api, '_request_with_retry', return_value=resp):
            result, cursor = self.api.get_markets()
        self.assertEqual(result, markets)
        self.assertEqual(cursor, 'XYZ')

    def test_get_markets_terminal_cursor_returns_none(self):
        resp = self._mock_response({'data': [], 'next_cursor': 'LTE='})
        with patch.object(self.api, '_request_with_retry', return_value=resp):
            result, cursor = self.api.get_markets()
        self.assertIsNone(cursor)

    def test_get_markets_failure_returns_empty(self):
        with patch.object(self.api, '_request_with_retry', return_value=None):
            result, cursor = self.api.get_markets()
        self.assertEqual(result, [])
        self.assertIsNone(cursor)

    def test_get_orderbook_success(self):
        data = {'bids': [{'price': '0.45', 'size': '10'}], 'asks': [{'price': '0.55', 'size': '5'}]}
        resp = self._mock_response(data)
        with patch.object(self.api, '_request_with_retry', return_value=resp):
            ob = self.api.get_orderbook('token-123')
        self.assertIn('bids', ob)
        self.assertIn('asks', ob)
        self.assertIn('timestamp', ob)

    def test_get_orderbook_failure_returns_empty(self):
        with patch.object(self.api, '_request_with_retry', return_value=None):
            ob = self.api.get_orderbook('token-123')
        self.assertEqual(ob, {})

    def test_get_price_success(self):
        data = {'bid': '0.44', 'ask': '0.56'}
        resp = self._mock_response(data)
        with patch.object(self.api, '_request_with_retry', return_value=resp):
            price = self.api.get_price('token-123')
        self.assertEqual(price, data)

    def test_get_midpoint_success(self):
        resp = self._mock_response({'mid': '0.50'})
        with patch.object(self.api, '_request_with_retry', return_value=resp):
            mid = self.api.get_midpoint('token-123')
        self.assertAlmostEqual(mid, 0.50)

    def test_get_midpoint_failure_returns_none(self):
        with patch.object(self.api, '_request_with_retry', return_value=None):
            mid = self.api.get_midpoint('token-123')
        self.assertIsNone(mid)

    def test_to_cents_conversion(self):
        self.assertEqual(PolymarketAPI.to_cents(0.45), 45)
        self.assertEqual(PolymarketAPI.to_cents(0.0), 0)
        self.assertEqual(PolymarketAPI.to_cents(1.0), 100)
        self.assertEqual(PolymarketAPI.to_cents(0.555), 56)  # rounds


class TestPolymarketAPIKeyHeader(unittest.TestCase):
    def test_api_key_in_header(self):
        api = PolymarketAPI(api_key='my-secret-key')
        session = api._get_session()
        self.assertEqual(session.headers.get('POLY_API_KEY'), 'my-secret-key')

    def test_no_api_key_no_header(self):
        api = PolymarketAPI()
        session = api._get_session()
        self.assertNotIn('POLY_API_KEY', session.headers)


if __name__ == '__main__':
    unittest.main()
