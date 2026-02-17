"""Unit tests for leg-risk handling in execute_arbitrage().

Mocks the KalshiAPI to simulate all combinations of YES/NO order
success and failure, and verifies that cancellation is triggered
correctly to prevent naked exposure.  No live API connection required.
"""

import unittest
import sys
import os
import logging
from unittest.mock import Mock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_bot import KalshiAPI, KalshiTradingBot

# Suppress noisy log output during tests
logging.disable(logging.CRITICAL)


def _opportunity(ticker='TEST-MARKET', yes=45, no=54):
    """Build a minimal opportunity dict."""
    return {
        'ticker': ticker,
        'title': f'Test Market {ticker}',
        'yes_price': yes,
        'no_price': no,
        'total_cost': yes + no,
        'profit_cents': 100 - (yes + no),
        'profit_percent': ((100 - (yes + no)) / (yes + no)) * 100,
    }


class TestLegRiskBothSucceed(unittest.TestCase):
    """Happy path: both legs fill successfully."""

    def setUp(self):
        self.api = Mock(spec=KalshiAPI)
        self.api.get_balance.return_value = 1000.0
        self.trader = KalshiTradingBot(self.api, initial_balance=1000.0, paper_trading=False)

    def test_both_orders_placed(self):
        """Both YES and NO orders succeed → no cancellation, returns True."""
        self.api.place_order.side_effect = [
            {'order_id': 'yes-001', 'status': 'open'},
            {'order_id': 'no-002', 'status': 'open'},
        ]

        with patch('kalshi_bot.Config.LIVE_TRADING_ENABLED', True):
            result = self.trader.execute_arbitrage(_opportunity(), quantity=10)

        self.assertTrue(result)
        self.assertEqual(self.api.place_order.call_count, 2)
        self.api.cancel_order.assert_not_called()

    def test_correct_order_arguments(self):
        """Verify that YES and NO orders use the right side and price."""
        opp = _opportunity(yes=45, no=54)
        self.api.place_order.side_effect = [
            {'order_id': 'y1'},
            {'order_id': 'n1'},
        ]

        with patch('kalshi_bot.Config.LIVE_TRADING_ENABLED', True):
            self.trader.execute_arbitrage(opp, quantity=5)

        calls = self.api.place_order.call_args_list
        # First call: YES order
        self.assertEqual(calls[0], call('TEST-MARKET', 'yes', 5, 45, order_type='limit'))
        # Second call: NO order
        self.assertEqual(calls[1], call('TEST-MARKET', 'no', 5, 54, order_type='limit'))


class TestLegRiskYesSuccessNoFails(unittest.TestCase):
    """YES fills but NO fails → must cancel YES to avoid naked exposure."""

    def setUp(self):
        self.api = Mock(spec=KalshiAPI)
        self.api.get_balance.return_value = 1000.0
        self.trader = KalshiTradingBot(self.api, initial_balance=1000.0, paper_trading=False)

    def test_yes_cancelled_on_no_failure(self):
        """YES order should be cancelled when NO order returns None."""
        self.api.place_order.side_effect = [
            {'order_id': 'yes-123', 'status': 'open'},
            None,  # NO order fails
        ]
        self.api.cancel_order.return_value = True

        with patch('kalshi_bot.Config.LIVE_TRADING_ENABLED', True):
            result = self.trader.execute_arbitrage(_opportunity(), quantity=10)

        self.assertFalse(result)
        self.api.cancel_order.assert_called_once_with('yes-123')

    def test_cancel_failure_is_critical(self):
        """When cancel also fails, function still returns False (manual intervention needed)."""
        self.api.place_order.side_effect = [
            {'order_id': 'yes-456'},
            None,
        ]
        self.api.cancel_order.return_value = False  # Cancel fails!

        with patch('kalshi_bot.Config.LIVE_TRADING_ENABLED', True):
            result = self.trader.execute_arbitrage(_opportunity(), quantity=10)

        self.assertFalse(result)
        self.api.cancel_order.assert_called_once_with('yes-456')

    def test_nested_order_id_extraction(self):
        """Some API responses nest order_id inside {'order': {'order_id': ...}}."""
        self.api.place_order.side_effect = [
            {'order': {'order_id': 'nested-789'}},
            None,
        ]
        self.api.cancel_order.return_value = True

        with patch('kalshi_bot.Config.LIVE_TRADING_ENABLED', True):
            self.trader.execute_arbitrage(_opportunity(), quantity=5)

        self.api.cancel_order.assert_called_once_with('nested-789')


class TestLegRiskYesFails(unittest.TestCase):
    """YES order fails → NO should never be attempted."""

    def setUp(self):
        self.api = Mock(spec=KalshiAPI)
        self.api.get_balance.return_value = 1000.0
        self.trader = KalshiTradingBot(self.api, initial_balance=1000.0, paper_trading=False)

    def test_no_not_placed_when_yes_fails(self):
        self.api.place_order.return_value = None  # YES fails

        with patch('kalshi_bot.Config.LIVE_TRADING_ENABLED', True):
            result = self.trader.execute_arbitrage(_opportunity(), quantity=10)

        self.assertFalse(result)
        self.assertEqual(self.api.place_order.call_count, 1)
        self.api.cancel_order.assert_not_called()


class TestLegRiskPaperTrading(unittest.TestCase):
    """Paper trading mode should never touch the real API."""

    def setUp(self):
        self.api = Mock(spec=KalshiAPI)
        self.trader = KalshiTradingBot(self.api, initial_balance=1000.0, paper_trading=True)

    def test_no_real_orders_in_paper_mode(self):
        result = self.trader.execute_arbitrage(_opportunity(), quantity=10)

        self.assertTrue(result)
        self.api.place_order.assert_not_called()
        self.api.cancel_order.assert_not_called()

    def test_balance_deducted(self):
        """Paper trade still deducts from local balance."""
        opp = _opportunity(yes=48, no=50)  # total cost = 98¢ × 10 = $9.80
        initial = self.trader.balance

        self.trader.execute_arbitrage(opp, quantity=10)

        expected = initial - (98 * 10 / 100.0)
        self.assertAlmostEqual(self.trader.balance, expected, places=2)

    def test_trade_recorded_in_history(self):
        self.trader.execute_arbitrage(_opportunity(), quantity=5)
        self.assertGreater(len(self.trader.trade_history), 0)

    def test_position_tracked(self):
        self.trader.execute_arbitrage(_opportunity(), quantity=5)
        self.assertGreater(len(self.trader.positions), 0)
        self.assertEqual(self.trader.positions[-1]['ticker'], 'TEST-MARKET')


class TestLegRiskBalanceChecks(unittest.TestCase):
    """Verify that insufficient balance prevents execution."""

    def setUp(self):
        self.api = Mock(spec=KalshiAPI)
        self.trader = KalshiTradingBot(self.api, initial_balance=5.0, paper_trading=True)

    def test_insufficient_balance_rejected(self):
        """$5 balance can't cover $9.90 cost → should fail."""
        opp = _opportunity(yes=45, no=54)  # total = 99¢ × 10 = $9.90
        result = self.trader.execute_arbitrage(opp, quantity=10)

        self.assertFalse(result)
        self.api.place_order.assert_not_called()


class TestLegRiskMultipleExecutions(unittest.TestCase):
    """Verify state correctness across multiple sequential trades."""

    def setUp(self):
        self.api = Mock(spec=KalshiAPI)
        self.trader = KalshiTradingBot(self.api, initial_balance=100.0, paper_trading=True)

    def test_sequential_trades_deduct_correctly(self):
        """Two trades should deduct from running balance."""
        opp = _opportunity(yes=48, no=50)  # 98¢ per pair

        self.trader.execute_arbitrage(opp, quantity=5)  # cost = $4.90
        balance_after_1 = self.trader.balance

        self.trader.execute_arbitrage(opp, quantity=5)  # cost = $4.90
        balance_after_2 = self.trader.balance

        self.assertAlmostEqual(balance_after_1, 100.0 - 4.90, places=2)
        self.assertAlmostEqual(balance_after_2, 100.0 - 9.80, places=2)
        self.assertEqual(len(self.trader.trade_history), 2)

    def test_third_trade_rejected_if_broke(self):
        """After exhausting balance, should reject further trades."""
        opp = _opportunity(yes=48, no=50)

        # Use up most of the balance
        self.trader.execute_arbitrage(opp, quantity=50)  # cost = $49.00
        self.trader.execute_arbitrage(opp, quantity=50)  # cost = $49.00
        # Balance now ~$2.00

        result = self.trader.execute_arbitrage(opp, quantity=50)  # cost = $49.00
        self.assertFalse(result)  # Should be rejected


if __name__ == '__main__':
    logging.disable(logging.NOTSET)  # Re-enable after tests
    unittest.main()
