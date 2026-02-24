"""Unit tests for PolymarketSportsArbitrage."""

import unittest
import sys
import os
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polymarket_sports_arb import PolymarketSportsArbitrage, POLY_PAYOUT_CENTS
from polymarket_api import PolymarketAPI


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_market(question='Finland vs Switzerland', condition_id='cond-1', tokens=None):
    if tokens is None:
        tokens = [
            {'outcome': 'Finland', 'token_id': 'tok-fin'},
            {'outcome': 'Switzerland', 'token_id': 'tok-swi'},
        ]
    return {'question': question, 'condition_id': condition_id, 'tokens': tokens}


def _make_orderbook(ask_price, bid_price, size=100):
    return {
        'asks': [{'price': str(ask_price), 'size': str(size)}],
        'bids': [{'price': str(bid_price), 'size': str(size)}],
    }


def _build_scanner():
    poly_api = Mock(spec=PolymarketAPI)
    scanner = PolymarketSportsArbitrage(
        poly_api=poly_api,
        min_profit_percent=0.5,
    )
    return scanner, poly_api


# ---------------------------------------------------------------------------
# _is_sports_market
# ---------------------------------------------------------------------------

class TestIsSportsMarket(unittest.TestCase):
    def setUp(self):
        self.scanner, _ = _build_scanner()

    def test_sports_keyword_in_title(self):
        market = {'question': 'Who wins the NBA championship?', 'tokens': []}
        self.assertTrue(self.scanner._is_sports_market(market))

    def test_soccer_keyword(self):
        market = {'question': 'Premier League top scorer 2025', 'tokens': []}
        self.assertTrue(self.scanner._is_sports_market(market))

    def test_non_sports_market(self):
        market = {'question': 'Will the Fed raise rates?', 'tokens': []}
        self.assertFalse(self.scanner._is_sports_market(market))

    def test_sports_tag(self):
        market = {
            'question': 'Some crypto question',
            'tags': [{'slug': 'nfl-playoffs'}],
            'tokens': [],
        }
        self.assertTrue(self.scanner._is_sports_market(market))

    def test_empty_market(self):
        self.assertFalse(self.scanner._is_sports_market({}))

    def test_title_field_fallback(self):
        market = {'title': 'UEFA Champions League final', 'tokens': []}
        self.assertTrue(self.scanner._is_sports_market(market))


# ---------------------------------------------------------------------------
# _is_binary_market / _get_binary_tokens
# ---------------------------------------------------------------------------

class TestBinaryMarket(unittest.TestCase):
    def setUp(self):
        self.scanner, _ = _build_scanner()

    def test_binary_market_two_tokens(self):
        market = _make_market()
        self.assertTrue(self.scanner._is_binary_market(market))

    def test_not_binary_single_token(self):
        market = {'tokens': [{'outcome': 'Yes', 'token_id': 'x'}]}
        self.assertFalse(self.scanner._is_binary_market(market))

    def test_not_binary_three_tokens(self):
        market = {
            'tokens': [
                {'outcome': 'A', 'token_id': '1'},
                {'outcome': 'B', 'token_id': '2'},
                {'outcome': 'C', 'token_id': '3'},
            ]
        }
        self.assertFalse(self.scanner._is_binary_market(market))

    def test_get_binary_tokens_returns_pair(self):
        market = _make_market()
        pair = self.scanner._get_binary_tokens(market)
        self.assertIsNotNone(pair)
        tok_a, tok_b = pair
        self.assertEqual(tok_a['outcome'], 'Finland')
        self.assertEqual(tok_b['outcome'], 'Switzerland')

    def test_get_binary_tokens_returns_none_for_non_binary(self):
        market = {'tokens': [{'outcome': 'A', 'token_id': '1'}]}
        self.assertIsNone(self.scanner._get_binary_tokens(market))


# ---------------------------------------------------------------------------
# _get_best_asks_cents
# ---------------------------------------------------------------------------

class TestGetBestAsksCents(unittest.TestCase):
    def setUp(self):
        self.scanner, self.poly_api = _build_scanner()

    def test_returns_yes_and_no_asks(self):
        self.poly_api.get_orderbook.return_value = _make_orderbook(0.37, 0.61)
        result = self.scanner._get_best_asks_cents('tok-1')
        self.assertIsNotNone(result)
        yes_ask, no_ask = result
        self.assertEqual(yes_ask, 37)
        # NO ask = round((1.0 - 0.61) * 100) = 39
        self.assertEqual(no_ask, 39)

    def test_returns_none_on_empty_orderbook(self):
        self.poly_api.get_orderbook.return_value = {}
        self.assertIsNone(self.scanner._get_best_asks_cents('tok-1'))

    def test_returns_none_when_no_asks(self):
        self.poly_api.get_orderbook.return_value = {'bids': [{'price': '0.5', 'size': '10'}], 'asks': []}
        self.assertIsNone(self.scanner._get_best_asks_cents('tok-1'))

    def test_no_ask_fallback_when_no_bids(self):
        self.poly_api.get_orderbook.return_value = {
            'asks': [{'price': '0.40', 'size': '10'}],
            'bids': [],
        }
        result = self.scanner._get_best_asks_cents('tok-1')
        self.assertIsNotNone(result)
        yes_ask, no_ask = result
        self.assertEqual(yes_ask, 40)
        self.assertEqual(no_ask, 60)  # fallback: 100 - yes_ask


# ---------------------------------------------------------------------------
# _walk_poly_orderbook
# ---------------------------------------------------------------------------

class TestWalkPolyOrderbook(unittest.TestCase):
    def setUp(self):
        self.scanner, _ = _build_scanner()

    def test_walk_sufficient_depth(self):
        asks = [{'price': '0.45', 'size': '100'}]
        result = self.scanner._walk_poly_orderbook(asks, target_qty=50)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result['avg_price'], 0.45)
        self.assertEqual(result['filled_qty'], 50)
        self.assertTrue(result['fully_filled'])

    def test_walk_insufficient_depth(self):
        asks = [{'price': '0.45', 'size': '3'}]
        result = self.scanner._walk_poly_orderbook(asks, target_qty=10)
        self.assertIsNotNone(result)
        self.assertEqual(result['filled_qty'], 3)
        self.assertFalse(result['fully_filled'])

    def test_walk_empty_book_returns_none(self):
        self.assertIsNone(self.scanner._walk_poly_orderbook([], target_qty=5))

    def test_walk_multi_level(self):
        asks = [
            {'price': '0.40', 'size': '50'},
            {'price': '0.42', 'size': '50'},
        ]
        result = self.scanner._walk_poly_orderbook(asks, target_qty=100)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result['avg_price'], 0.41)
        self.assertEqual(result['filled_qty'], 100)
        self.assertEqual(result['levels_used'], 2)


# ---------------------------------------------------------------------------
# _check_yes_arb
# ---------------------------------------------------------------------------

class TestCheckYesArb(unittest.TestCase):
    def setUp(self):
        self.scanner, self.poly_api = _build_scanner()

    def test_yes_arb_detected(self):
        """ask_a(37¢) + ask_b(59¢) = 96¢ < 98¢ → YES arb (profit 2¢)."""
        market = _make_market()
        ob_a = _make_orderbook(ask_price=0.37, bid_price=0.35)
        ob_b = _make_orderbook(ask_price=0.59, bid_price=0.57)

        result = self.scanner._check_yes_arb(market, ob_a=ob_a, ob_b=ob_b)

        self.assertIsNotNone(result)
        self.assertEqual(result['arb_side'], 'yes')
        self.assertEqual(result['ask_a_cents'], 37)
        self.assertEqual(result['ask_b_cents'], 59)
        self.assertEqual(result['total_cost_cents'], 96)
        self.assertEqual(result['effective_payout_cents'], POLY_PAYOUT_CENTS)
        self.assertEqual(result['profit_cents'], 2)
        self.assertGreater(result['profit_percent'], 0)
        self.assertEqual(result['type'], 'polymarket_sports_arb')
        self.assertIn('Finland', result['outcome_a'])

    def test_no_arb_when_total_equals_payout(self):
        """ask_a + ask_b == 98¢ → no arb (profit is 0)."""
        market = _make_market()
        ob_a = _make_orderbook(ask_price=0.49, bid_price=0.47)
        ob_b = _make_orderbook(ask_price=0.49, bid_price=0.47)

        result = self.scanner._check_yes_arb(market, ob_a=ob_a, ob_b=ob_b)
        self.assertIsNone(result)

    def test_no_arb_when_total_exceeds_payout(self):
        """ask_a + ask_b > 98¢ → no arb."""
        market = _make_market()
        ob_a = _make_orderbook(ask_price=0.50, bid_price=0.48)
        ob_b = _make_orderbook(ask_price=0.50, bid_price=0.48)

        result = self.scanner._check_yes_arb(market, ob_a=ob_a, ob_b=ob_b)
        self.assertIsNone(result)

    def test_below_min_profit_percent_ignored(self):
        """Profit below min_profit_percent threshold is rejected."""
        self.scanner.min_profit_percent = 5.0
        market = _make_market()
        # 37 + 59 = 96 → profit 2¢ = 2.08% < 5%
        ob_a = _make_orderbook(ask_price=0.37, bid_price=0.35)
        ob_b = _make_orderbook(ask_price=0.59, bid_price=0.57)

        result = self.scanner._check_yes_arb(market, ob_a=ob_a, ob_b=ob_b)
        self.assertIsNone(result)

    def test_missing_asks_returns_none(self):
        market = _make_market()
        ob_a = {'asks': [], 'bids': [{'price': '0.35', 'size': '10'}]}
        ob_b = _make_orderbook(ask_price=0.59, bid_price=0.57)

        result = self.scanner._check_yes_arb(market, ob_a=ob_a, ob_b=ob_b)
        self.assertIsNone(result)

    def test_non_binary_market_returns_none(self):
        market = {'question': 'Three-way match', 'tokens': [
            {'outcome': 'A', 'token_id': '1'},
            {'outcome': 'B', 'token_id': '2'},
            {'outcome': 'C', 'token_id': '3'},
        ]}
        result = self.scanner._check_yes_arb(market)
        self.assertIsNone(result)

    def test_strategy_string_format(self):
        market = _make_market()
        ob_a = _make_orderbook(ask_price=0.37, bid_price=0.35)
        ob_b = _make_orderbook(ask_price=0.59, bid_price=0.57)

        result = self.scanner._check_yes_arb(market, ob_a=ob_a, ob_b=ob_b)
        self.assertIsNotNone(result)
        self.assertIn('YES(Finland)', result['strategy'])
        self.assertIn('YES(Switzerland)', result['strategy'])
        self.assertIn('96¢', result['strategy'])
        self.assertIn('98¢', result['strategy'])


# ---------------------------------------------------------------------------
# _check_no_arb
# ---------------------------------------------------------------------------

class TestCheckNoArb(unittest.TestCase):
    def setUp(self):
        self.scanner, self.poly_api = _build_scanner()

    def test_no_arb_detected(self):
        """bid_a(0.65) + bid_b(0.65) = 1.30 → no_ask_a=35¢, no_ask_b=35¢, total=70¢ < 98¢."""
        market = _make_market()
        ob_a = _make_orderbook(ask_price=0.37, bid_price=0.65)
        ob_b = _make_orderbook(ask_price=0.37, bid_price=0.65)

        result = self.scanner._check_no_arb(market, ob_a=ob_a, ob_b=ob_b)
        self.assertIsNotNone(result)
        self.assertEqual(result['arb_side'], 'no')
        self.assertEqual(result['ask_a_cents'], 35)   # 1 - 0.65 = 0.35
        self.assertEqual(result['ask_b_cents'], 35)
        self.assertEqual(result['total_cost_cents'], 70)
        self.assertEqual(result['profit_cents'], 28)
        self.assertEqual(result['type'], 'polymarket_sports_arb')

    def test_no_arb_when_bids_normal(self):
        """Normal bids (bid_a + bid_b ≈ 1.0) → no_ask total ≈ 100¢ → no arb."""
        market = _make_market()
        # bid_a=0.63, bid_b=0.35 → no_ask_a=37¢, no_ask_b=65¢, total=102¢ > 98¢
        ob_a = _make_orderbook(ask_price=0.37, bid_price=0.63)
        ob_b = _make_orderbook(ask_price=0.67, bid_price=0.35)

        result = self.scanner._check_no_arb(market, ob_a=ob_a, ob_b=ob_b)
        self.assertIsNone(result)

    def test_missing_bids_returns_none(self):
        market = _make_market()
        ob_a = {'asks': [{'price': '0.37', 'size': '10'}], 'bids': []}
        ob_b = _make_orderbook(ask_price=0.59, bid_price=0.57)

        result = self.scanner._check_no_arb(market, ob_a=ob_a, ob_b=ob_b)
        self.assertIsNone(result)

    def test_strategy_string_includes_no(self):
        market = _make_market()
        ob_a = _make_orderbook(ask_price=0.37, bid_price=0.65)
        ob_b = _make_orderbook(ask_price=0.37, bid_price=0.65)

        result = self.scanner._check_no_arb(market, ob_a=ob_a, ob_b=ob_b)
        self.assertIsNotNone(result)
        self.assertIn('NO(Finland)', result['strategy'])
        self.assertIn('NO(Switzerland)', result['strategy'])


# ---------------------------------------------------------------------------
# scan_opportunities
# ---------------------------------------------------------------------------

class TestScanOpportunities(unittest.TestCase):
    def setUp(self):
        self.scanner, self.poly_api = _build_scanner()

    def _sports_binary_market(self, question='Finland vs Switzerland: Soccer Match', cond='c1'):
        return {
            'question': question,
            'condition_id': cond,
            'tokens': [
                {'outcome': 'Finland', 'token_id': 'tok-fin'},
                {'outcome': 'Switzerland', 'token_id': 'tok-swi'},
            ],
        }

    def test_yes_arb_found_in_scan(self):
        self.poly_api.get_all_markets.return_value = [self._sports_binary_market()]
        # ask 0.37 and 0.59 → total 96¢ < 98¢
        self.poly_api.get_orderbook.return_value = _make_orderbook(
            ask_price=0.37, bid_price=0.35
        )

        with patch('time.sleep'), patch.object(self.scanner.notifier, 'notify_opportunity'):
            opps = self.scanner.scan_opportunities()

        # YES arb: 37 + 37 = 74 < 98 → found
        self.assertGreater(len(opps), 0)
        yes_opps = [o for o in opps if o['arb_side'] == 'yes']
        self.assertGreater(len(yes_opps), 0)

    def test_no_arb_found_in_scan(self):
        self.poly_api.get_all_markets.return_value = [self._sports_binary_market()]
        # High bids → no_ask is low → NO arb
        self.poly_api.get_orderbook.return_value = _make_orderbook(
            ask_price=0.37, bid_price=0.65
        )

        with patch('time.sleep'), patch.object(self.scanner.notifier, 'notify_opportunity'):
            opps = self.scanner.scan_opportunities()

        no_opps = [o for o in opps if o['arb_side'] == 'no']
        self.assertGreater(len(no_opps), 0)

    def test_non_sports_market_skipped(self):
        self.poly_api.get_all_markets.return_value = [{
            'question': 'Will the Fed raise rates?',
            'condition_id': 'c1',
            'tokens': [
                {'outcome': 'Yes', 'token_id': 'y'},
                {'outcome': 'No', 'token_id': 'n'},
            ],
        }]

        with patch('time.sleep'), patch.object(self.scanner.notifier, 'notify_opportunity'):
            opps = self.scanner.scan_opportunities()

        self.assertEqual(opps, [])
        self.poly_api.get_orderbook.assert_not_called()

    def test_non_binary_market_skipped(self):
        self.poly_api.get_all_markets.return_value = [{
            'question': 'NFL championship winner',
            'condition_id': 'c1',
            'tokens': [
                {'outcome': 'Team A', 'token_id': '1'},
                {'outcome': 'Team B', 'token_id': '2'},
                {'outcome': 'Team C', 'token_id': '3'},
            ],
        }]

        with patch('time.sleep'), patch.object(self.scanner.notifier, 'notify_opportunity'):
            opps = self.scanner.scan_opportunities()

        self.assertEqual(opps, [])

    def test_empty_markets_returns_empty(self):
        self.poly_api.get_all_markets.return_value = []

        with patch('time.sleep'):
            opps = self.scanner.scan_opportunities()

        self.assertEqual(opps, [])

    def test_failed_orderbook_skips_market(self):
        self.poly_api.get_all_markets.return_value = [self._sports_binary_market()]
        self.poly_api.get_orderbook.return_value = {}

        with patch('time.sleep'), patch.object(self.scanner.notifier, 'notify_opportunity'):
            opps = self.scanner.scan_opportunities()

        self.assertEqual(opps, [])

    def test_opportunity_saved_to_storage(self):
        storage = Mock()
        self.scanner.storage = storage
        self.poly_api.get_all_markets.return_value = [self._sports_binary_market()]
        # Trigger YES arb
        self.poly_api.get_orderbook.return_value = _make_orderbook(
            ask_price=0.37, bid_price=0.35
        )

        with patch('time.sleep'), patch.object(self.scanner.notifier, 'notify_opportunity'):
            opps = self.scanner.scan_opportunities()

        if opps:
            storage.save_opportunity.assert_called()


# ---------------------------------------------------------------------------
# NO-side arb in KalshiArbitrageBot.analyze_event_arbitrage
# ---------------------------------------------------------------------------

class TestKalshiNoSideEventArb(unittest.TestCase):
    """Test that analyze_event_arbitrage now detects NO-side arb."""

    def _build_bot(self):
        from arbitrage import KalshiArbitrageBot
        api = Mock()
        bot = KalshiArbitrageBot(api, min_profit_percent=1.0)
        return bot, api

    def _make_kalshi_market(self, ticker, close_offset_hours=2):
        from datetime import datetime, timezone, timedelta
        ct = (datetime.now(timezone.utc) + timedelta(hours=close_offset_hours)).isoformat()
        return {'ticker': ticker, 'title': f'Market {ticker}', 'close_time': ct, 'event_ticker': 'EVT-1'}

    def test_yes_arb_detected(self):
        bot, api = self._build_bot()
        # YES asks sum: 46+46=92 < 100 → profit 8¢ (8.7%), within MAX_PROFIT_PERCENT=15%
        api.get_orderbook.return_value = {
            'yes_asks': [[46, 10]],
            'no_asks': [[60, 10]],
        }
        markets = [
            self._make_kalshi_market('EVT-1-A'),
            self._make_kalshi_market('EVT-1-B'),
        ]
        with patch('time.sleep'):
            result = bot.analyze_event_arbitrage(markets)
        self.assertIsNotNone(result)
        self.assertEqual(result['arb_side'], 'yes')

    def test_no_arb_detected(self):
        bot, api = self._build_bot()
        # NO asks sum: 44+44=88 < 100 → profit 12¢ (13.6%), YES total=110+110>100 → no YES arb
        api.get_orderbook.return_value = {
            'yes_asks': [[55, 10]],
            'no_asks': [[44, 10]],
        }
        markets = [
            self._make_kalshi_market('EVT-1-A'),
            self._make_kalshi_market('EVT-1-B'),
        ]
        with patch('time.sleep'):
            result = bot.analyze_event_arbitrage(markets)
        self.assertIsNotNone(result)
        self.assertEqual(result['arb_side'], 'no')

    def test_returns_more_profitable_side(self):
        """When both YES and NO arb exist, returns the one with higher profit."""
        bot, api = self._build_bot()
        # YES total = 46+46=92 → profit 8¢ (8.7%)
        # NO total = 44+44=88 → profit 12¢ (13.6%) → NO wins
        market_a = {'yes_asks': [[46, 10]], 'no_asks': [[44, 10]]}
        market_b = {'yes_asks': [[46, 10]], 'no_asks': [[44, 10]]}
        api.get_orderbook.side_effect = [market_a, market_b]
        markets = [
            self._make_kalshi_market('EVT-1-A'),
            self._make_kalshi_market('EVT-1-B'),
        ]
        with patch('time.sleep'):
            result = bot.analyze_event_arbitrage(markets)
        self.assertIsNotNone(result)
        self.assertEqual(result['arb_side'], 'no')

    def test_no_arb_when_both_totals_above_threshold(self):
        bot, api = self._build_bot()
        # YES total = 55+55=110, NO total = 55+55=110 → no arb on either side
        api.get_orderbook.return_value = {
            'yes_asks': [[55, 10]],
            'no_asks': [[55, 10]],
        }
        markets = [
            self._make_kalshi_market('EVT-1-A'),
            self._make_kalshi_market('EVT-1-B'),
        ]
        with patch('time.sleep'):
            result = bot.analyze_event_arbitrage(markets)
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()
