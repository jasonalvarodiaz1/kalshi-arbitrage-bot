"""Unit tests for CrossPlatformArbitrage."""

import unittest
import sys
import os
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cross_platform_arb import CrossPlatformArbitrage, _normalize_title, _similarity
from polymarket_api import PolymarketAPI


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_kalshi_market(ticker='KX-TICKER', title='Will BTC exceed $100k by end of year?'):
    return {'ticker': ticker, 'title': title}


def _make_poly_market(condition_id='abc', question='BTC exceed $100k by end of year?'):
    return {
        'condition_id': condition_id,
        'question': question,
        'tokens': [
            {'outcome': 'Yes', 'token_id': 'yes-token'},
            {'outcome': 'No', 'token_id': 'no-token'},
        ],
    }


# ---------------------------------------------------------------------------
# Title normalization
# ---------------------------------------------------------------------------

class TestNormalizeTitle(unittest.TestCase):
    def test_lowercase_strip(self):
        self.assertEqual(_normalize_title('  Hello World  '), 'hello world')

    def test_strips_will_prefix(self):
        self.assertEqual(_normalize_title('Will BTC hit 100k?'), 'btc hit 100k?')

    def test_strips_is_prefix(self):
        self.assertEqual(_normalize_title('Is the market open?'), 'the market open?')

    def test_no_prefix(self):
        self.assertEqual(_normalize_title('BTC at 100k'), 'btc at 100k')


class TestSimilarity(unittest.TestCase):
    def test_identical_strings(self):
        self.assertAlmostEqual(_similarity('foo', 'foo'), 1.0)

    def test_different_strings(self):
        self.assertLess(_similarity('foo', 'bar'), 0.5)

    def test_similar_strings(self):
        s = _similarity('btc exceed 100k by year end', 'btc exceed 100k by end of year')
        self.assertGreater(s, 0.8)


# ---------------------------------------------------------------------------
# extract_poly_token_ids
# ---------------------------------------------------------------------------

class TestExtractPolyTokenIds(unittest.TestCase):
    def test_standard_tokens_list(self):
        pm = {
            'tokens': [
                {'outcome': 'Yes', 'token_id': 'yes-123'},
                {'outcome': 'No', 'token_id': 'no-456'},
            ]
        }
        yes_id, no_id = CrossPlatformArbitrage._extract_poly_token_ids(pm)
        self.assertEqual(yes_id, 'yes-123')
        self.assertEqual(no_id, 'no-456')

    def test_fallback_clob_token_ids(self):
        pm = {'clob_token_ids': ['yes-abc', 'no-def'], 'tokens': []}
        yes_id, no_id = CrossPlatformArbitrage._extract_poly_token_ids(pm)
        self.assertEqual(yes_id, 'yes-abc')
        self.assertEqual(no_id, 'no-def')

    def test_missing_tokens_returns_none(self):
        pm = {}
        yes_id, no_id = CrossPlatformArbitrage._extract_poly_token_ids(pm)
        self.assertIsNone(yes_id)
        self.assertIsNone(no_id)


# ---------------------------------------------------------------------------
# match_markets
# ---------------------------------------------------------------------------

class TestMatchMarkets(unittest.TestCase):
    def _build_scanner(self):
        kalshi_api = Mock()
        poly_api = Mock(spec=PolymarketAPI)
        scanner = CrossPlatformArbitrage(
            kalshi_api=kalshi_api,
            poly_api=poly_api,
            min_profit_percent=2.0,
            similarity_threshold=0.85,
        )
        return scanner, kalshi_api, poly_api

    def test_match_similar_titles(self):
        scanner, kalshi_api, poly_api = self._build_scanner()
        kalshi_api.get_all_markets.return_value = [
            _make_kalshi_market('KX-BTC', 'Will BTC exceed $100k by year end?')
        ]
        poly_api.get_all_markets.return_value = [
            _make_poly_market('abc', 'BTC exceed $100k by year end?')
        ]
        pairs = scanner.match_markets()
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0]['ticker'], 'KX-BTC')

    def test_no_match_dissimilar_titles(self):
        scanner, kalshi_api, poly_api = self._build_scanner()
        kalshi_api.get_all_markets.return_value = [
            _make_kalshi_market('KX-BTC', 'Will BTC exceed $100k?')
        ]
        poly_api.get_all_markets.return_value = [
            _make_poly_market('xyz', 'Will the Fed raise rates?')
        ]
        pairs = scanner.match_markets()
        self.assertEqual(len(pairs), 0)

    def test_cache_used_on_second_call(self):
        scanner, kalshi_api, poly_api = self._build_scanner()
        kalshi_api.get_all_markets.return_value = []
        poly_api.get_all_markets.return_value = []
        scanner.match_markets()
        scanner.match_markets()  # second call should use cache
        self.assertEqual(kalshi_api.get_all_markets.call_count, 1)

    def test_force_refresh_bypasses_cache(self):
        scanner, kalshi_api, poly_api = self._build_scanner()
        kalshi_api.get_all_markets.return_value = []
        poly_api.get_all_markets.return_value = []
        scanner.match_markets()
        scanner.match_markets(force_refresh=True)
        self.assertEqual(kalshi_api.get_all_markets.call_count, 2)


# ---------------------------------------------------------------------------
# scan_opportunities
# ---------------------------------------------------------------------------

class TestScanOpportunities(unittest.TestCase):
    def _build_scanner(self):
        kalshi_api = Mock()
        poly_api = Mock(spec=PolymarketAPI)
        scanner = CrossPlatformArbitrage(
            kalshi_api=kalshi_api,
            poly_api=poly_api,
            min_profit_percent=2.0,
            similarity_threshold=0.85,
        )
        return scanner, kalshi_api, poly_api

    def test_opportunity_detected_kalshi_yes_poly_no(self):
        """kalshi_yes_ask + poly_no_ask < 100 → opportunity."""
        scanner, kalshi_api, poly_api = self._build_scanner()

        # Pre-populate cache
        km = _make_kalshi_market()
        pm = _make_poly_market()
        scanner._matched_pairs = [(km, pm)]

        kalshi_api.get_orderbook.return_value = {
            'yes_asks': [[45, 10]],
            'no_asks': [[58, 10]],
        }
        poly_api.get_price.side_effect = [
            {'ask': '0.50'},   # YES ask
            {'ask': '0.48'},   # NO ask
        ]

        with patch('time.sleep'), patch.object(scanner.notifier, 'notify_opportunity'):
            opps = scanner.scan_opportunities()

        self.assertEqual(len(opps), 1)
        opp = opps[0]
        self.assertEqual(opp['type'], 'cross_platform')
        self.assertEqual(opp['kalshi_side'], 'yes')
        self.assertEqual(opp['kalshi_price'], 45)
        self.assertEqual(opp['poly_side'], 'no')
        self.assertEqual(opp['poly_price'], 48)
        self.assertEqual(opp['total_cost'], 93)
        self.assertEqual(opp['profit_cents'], 7)

    def test_opportunity_detected_poly_yes_kalshi_no(self):
        """poly_yes_ask + kalshi_no_ask < 100 → opportunity."""
        scanner, kalshi_api, poly_api = self._build_scanner()

        km = _make_kalshi_market()
        pm = _make_poly_market()
        scanner._matched_pairs = [(km, pm)]

        kalshi_api.get_orderbook.return_value = {
            'yes_asks': [[60, 10]],
            'no_asks': [[44, 10]],
        }
        poly_api.get_price.side_effect = [
            {'ask': '0.49'},   # YES ask
            {'ask': '0.55'},   # NO ask
        ]

        with patch('time.sleep'), patch.object(scanner.notifier, 'notify_opportunity'):
            opps = scanner.scan_opportunities()

        self.assertEqual(len(opps), 1)
        opp = opps[0]
        self.assertEqual(opp['kalshi_side'], 'no')
        self.assertEqual(opp['poly_side'], 'yes')

    def test_no_opportunity_when_cost_exceeds_100(self):
        scanner, kalshi_api, poly_api = self._build_scanner()
        km = _make_kalshi_market()
        pm = _make_poly_market()
        scanner._matched_pairs = [(km, pm)]

        kalshi_api.get_orderbook.return_value = {
            'yes_asks': [[55, 10]],
            'no_asks': [[55, 10]],
        }
        poly_api.get_price.side_effect = [
            {'ask': '0.55'},
            {'ask': '0.55'},
        ]

        with patch('time.sleep'), patch.object(scanner.notifier, 'notify_opportunity'):
            opps = scanner.scan_opportunities()

        self.assertEqual(len(opps), 0)

    def test_below_min_profit_percent_ignored(self):
        """Opportunity with profit % below threshold is excluded."""
        scanner, kalshi_api, poly_api = self._build_scanner()
        scanner.min_profit_percent = 5.0  # set high threshold
        km = _make_kalshi_market()
        pm = _make_poly_market()
        scanner._matched_pairs = [(km, pm)]

        # 45 + 53 = 98 → 2% profit (below 5%)
        kalshi_api.get_orderbook.return_value = {
            'yes_asks': [[45, 10]],
            'no_asks': [[58, 10]],
        }
        poly_api.get_price.side_effect = [
            {'ask': '0.55'},
            {'ask': '0.53'},
        ]

        with patch('time.sleep'), patch.object(scanner.notifier, 'notify_opportunity'):
            opps = scanner.scan_opportunities()

        self.assertEqual(len(opps), 0)

    def test_polymarket_disabled_returns_empty(self):
        scanner, _, _ = self._build_scanner()
        with patch('cross_platform_arb.Config') as mock_cfg:
            mock_cfg.POLYMARKET_ENABLED = False
            opps = scanner.scan_opportunities()
        self.assertEqual(opps, [])

    def test_incomplete_prices_skipped(self):
        scanner, kalshi_api, poly_api = self._build_scanner()
        km = _make_kalshi_market()
        pm = _make_poly_market()
        scanner._matched_pairs = [(km, pm)]

        kalshi_api.get_orderbook.return_value = {}  # no prices
        poly_api.get_price.return_value = {}

        with patch('time.sleep'), patch.object(scanner.notifier, 'notify_opportunity'):
            opps = scanner.scan_opportunities()

        self.assertEqual(len(opps), 0)


if __name__ == '__main__':
    unittest.main()
