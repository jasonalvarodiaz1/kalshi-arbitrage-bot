"""Tests for partial fill monitoring in execute_arbitrage."""

import unittest
import sys
import os
from unittest.mock import Mock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_bot import KalshiAPI, KalshiTradingBot


class TestWaitForFill(unittest.TestCase):
    """Test _wait_for_fill helper method."""

    def setUp(self):
        # test_leg_risk_extended.py disables logging at module level; re-enable here
        import logging as _logging
        _logging.disable(_logging.NOTSET)
        self.api = Mock(spec=KalshiAPI)
        self.trader = KalshiTradingBot(self.api, initial_balance=1000.0, paper_trading=False)

    def test_immediate_full_fill(self):
        """Order fully fills on first poll."""
        self.api.get_order.return_value = {'order': {'status': 'filled', 'filled_count': 10}}
        result = self.trader._wait_for_fill('order-123', expected_qty=10, timeout=30)
        self.assertEqual(result['filled_qty'], 10)
        self.assertEqual(result['unfilled_qty'], 0)
        self.assertFalse(result['cancelled'])

    def test_partial_fill_timeout_cancels(self):
        """Partial fill after timeout triggers cancellation."""
        self.api.get_order.return_value = {'order': {'status': 'open', 'filled_count': 3}}
        self.api.cancel_order.return_value = True

        with patch('time.sleep'):
            result = self.trader._wait_for_fill('order-456', expected_qty=10, timeout=4)

        self.assertEqual(result['filled_qty'], 3)
        self.assertEqual(result['unfilled_qty'], 7)
        self.assertTrue(result['cancelled'])
        self.api.cancel_order.assert_called_once_with('order-456')

    def test_zero_fill_timeout_cancels(self):
        """No fill after timeout triggers cancellation."""
        self.api.get_order.return_value = {'order': {'status': 'open', 'filled_count': 0}}
        self.api.cancel_order.return_value = True

        with patch('time.sleep'):
            result = self.trader._wait_for_fill('order-789', expected_qty=5, timeout=2)

        self.assertEqual(result['filled_qty'], 0)
        self.assertEqual(result['unfilled_qty'], 5)
        self.assertTrue(result['cancelled'])

    def test_cancel_fails_on_timeout(self):
        """Records cancel=False when exchange cancel fails."""
        self.api.get_order.return_value = {'order': {'status': 'open', 'filled_count': 0}}
        self.api.cancel_order.return_value = False

        with patch('time.sleep'):
            result = self.trader._wait_for_fill('order-bad', expected_qty=5, timeout=2)

        self.assertFalse(result['cancelled'])

    def test_fill_imbalance_logged(self):
        """Fill imbalance between YES and NO logs CRITICAL warning."""
        yes_order_response = {'order': {'order_id': 'yes-123', 'status': 'open'}}
        no_order_response = {'order': {'order_id': 'no-456', 'status': 'open'}}

        self.api.place_order.side_effect = [yes_order_response, no_order_response]
        self.api.get_balance.return_value = 1000.0
        self.api.get_orderbook.return_value = None  # skip stale check

        # YES fills 10, NO fills 7
        self.api.get_order.side_effect = [
            {'order': {'status': 'filled', 'filled_count': 10}},  # YES poll
            {'order': {'status': 'filled', 'filled_count': 7}},   # NO poll
        ]

        opportunity = {
            'ticker': 'TEST-MARKET',
            'title': 'Test Market',
            'yes_price': 45,
            'no_price': 54,
        }

        with patch('kalshi_bot.Config.LIVE_TRADING_ENABLED', True):
            with self.assertLogs('kalshi_bot', level='CRITICAL') as cm:
                self.trader.execute_arbitrage(opportunity, quantity=10)

        self.assertTrue(any('FILL IMBALANCE' in msg or 'naked' in msg.lower() for msg in cm.output))


class TestStalePrice(unittest.TestCase):
    """Test stale price check in execute_arbitrage."""

    def setUp(self):
        self.api = Mock(spec=KalshiAPI)
        self.api.get_balance.return_value = 1000.0
        self.trader = KalshiTradingBot(self.api, initial_balance=1000.0, paper_trading=False)

    def test_aborts_when_prices_moved_above_100(self):
        """Aborts trade when fresh orderbook shows total >= 100¢."""
        # Fresh orderbook shows prices moved up — no longer profitable
        self.api.get_orderbook.return_value = {
            'yes_asks': [[52, 10]],
            'no_asks': [[52, 10]],
        }

        opportunity = {
            'ticker': 'TEST-MARKET',
            'title': 'Test Market',
            'yes_price': 45,
            'no_price': 50,
        }

        with patch('kalshi_bot.Config.LIVE_TRADING_ENABLED', True):
            result = self.trader.execute_arbitrage(opportunity, quantity=5)

        self.assertFalse(result)
        self.api.place_order.assert_not_called()

    def test_proceeds_when_prices_still_profitable(self):
        """Continues with trade when fresh prices still sum < 100¢."""
        self.api.get_orderbook.return_value = {
            'yes_asks': [[44, 10]],
            'no_asks': [[50, 10]],
        }
        self.api.place_order.side_effect = [
            {'order': {'order_id': 'y1', 'status': 'open'}},
            {'order': {'order_id': 'n1', 'status': 'open'}},
        ]
        self.api.get_order.return_value = {'order': {'status': 'filled', 'filled_count': 5}}

        opportunity = {
            'ticker': 'TEST-MARKET',
            'title': 'Test Market',
            'yes_price': 44,
            'no_price': 50,
        }

        with patch('kalshi_bot.Config.LIVE_TRADING_ENABLED', True):
            result = self.trader.execute_arbitrage(opportunity, quantity=5)

        self.assertTrue(result)
        self.assertEqual(self.api.place_order.call_count, 2)


if __name__ == '__main__':
    unittest.main()
