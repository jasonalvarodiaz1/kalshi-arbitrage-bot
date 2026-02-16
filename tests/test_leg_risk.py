"""Integration test for leg-risk handling in execute_arbitrage."""

import unittest
import sys
import os
from unittest.mock import Mock, patch, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_bot import KalshiAPI, KalshiTradingBot


class TestExecuteArbitrageLegRisk(unittest.TestCase):
    """Test leg-risk handling in execute_arbitrage."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.api = Mock(spec=KalshiAPI)
        self.api.get_balance.return_value = 1000.0
        self.trader = KalshiTradingBot(self.api, initial_balance=1000.0, paper_trading=False)
    
    def test_both_orders_succeed(self):
        """Test successful arbitrage with both orders placed."""
        # Mock successful YES and NO orders
        yes_order_response = {'order_id': 'yes-order-123', 'status': 'open'}
        no_order_response = {'order_id': 'no-order-456', 'status': 'open'}
        
        self.api.place_order.side_effect = [yes_order_response, no_order_response]
        
        opportunity = {
            'ticker': 'TEST-MARKET',
            'title': 'Test Market',
            'yes_price': 45,
            'no_price': 54,
        }
        
        with patch('kalshi_bot.Config.LIVE_TRADING_ENABLED', True):
            result = self.trader.execute_arbitrage(opportunity, quantity=10)
        
        # Should succeed
        self.assertTrue(result)
        
        # Both orders should have been placed
        self.assertEqual(self.api.place_order.call_count, 2)
        
        # Cancel should NOT have been called
        self.api.cancel_order.assert_not_called()
    
    def test_yes_succeeds_no_fails_cancels_yes(self):
        """Test that YES order is cancelled when NO order fails."""
        # Mock YES success, NO failure
        yes_order_response = {'order_id': 'yes-order-123', 'status': 'open'}
        
        self.api.place_order.side_effect = [yes_order_response, None]  # NO order fails
        self.api.cancel_order.return_value = True  # Cancel succeeds
        
        opportunity = {
            'ticker': 'TEST-MARKET',
            'title': 'Test Market',
            'yes_price': 45,
            'no_price': 54,
        }
        
        with patch('kalshi_bot.Config.LIVE_TRADING_ENABLED', True):
            result = self.trader.execute_arbitrage(opportunity, quantity=10)
        
        # Should fail (return False)
        self.assertFalse(result)
        
        # YES and NO orders should have been attempted
        self.assertEqual(self.api.place_order.call_count, 2)
        
        # Cancel should have been called to cancel the YES order
        self.api.cancel_order.assert_called_once_with('yes-order-123')
    
    def test_yes_succeeds_no_fails_cancel_fails(self):
        """Test critical path when YES order cannot be cancelled."""
        # Mock YES success, NO failure, cancel failure
        yes_order_response = {'order_id': 'yes-order-123', 'status': 'open'}
        
        self.api.place_order.side_effect = [yes_order_response, None]  # NO order fails
        self.api.cancel_order.return_value = False  # Cancel fails!
        
        opportunity = {
            'ticker': 'TEST-MARKET',
            'title': 'Test Market',
            'yes_price': 45,
            'no_price': 54,
        }
        
        with patch('kalshi_bot.Config.LIVE_TRADING_ENABLED', True):
            result = self.trader.execute_arbitrage(opportunity, quantity=10)
        
        # Should fail
        self.assertFalse(result)
        
        # Cancel should have been attempted
        self.api.cancel_order.assert_called_once_with('yes-order-123')
    
    def test_yes_fails_no_not_placed(self):
        """Test that NO order is not placed when YES order fails."""
        # Mock YES failure
        self.api.place_order.return_value = None  # YES order fails
        
        opportunity = {
            'ticker': 'TEST-MARKET',
            'title': 'Test Market',
            'yes_price': 45,
            'no_price': 54,
        }
        
        with patch('kalshi_bot.Config.LIVE_TRADING_ENABLED', True):
            result = self.trader.execute_arbitrage(opportunity, quantity=10)
        
        # Should fail
        self.assertFalse(result)
        
        # Only YES order should have been attempted (not NO)
        self.assertEqual(self.api.place_order.call_count, 1)
        
        # Cancel should NOT have been called (nothing to cancel)
        self.api.cancel_order.assert_not_called()
    
    def test_paper_trading_mode(self):
        """Test that paper trading mode doesn't place real orders."""
        trader = KalshiTradingBot(self.api, initial_balance=1000.0, paper_trading=True)
        
        opportunity = {
            'ticker': 'TEST-MARKET',
            'title': 'Test Market',
            'yes_price': 45,
            'no_price': 54,
        }
        
        result = trader.execute_arbitrage(opportunity, quantity=10)
        
        # Should succeed
        self.assertTrue(result)
        
        # No real orders should have been placed
        self.api.place_order.assert_not_called()
        self.api.cancel_order.assert_not_called()


if __name__ == '__main__':
    unittest.main()
