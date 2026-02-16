"""Unit tests for Kelly Criterion position sizing."""

import unittest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kelly import kelly_fraction, size_position


class TestKellyFraction(unittest.TestCase):
    """Test Kelly fraction calculations."""
    
    def test_positive_edge(self):
        """Test Kelly with positive edge."""
        # Example: 60% win rate, win $1, lose $1 (even money)
        # Kelly = (0.6 - 0.4) / 1 = 0.2 (bet 20% of bankroll)
        win_prob = 0.6
        win_amount = 1.0
        loss_amount = 1.0
        
        kelly = kelly_fraction(win_prob, win_amount, loss_amount)
        self.assertAlmostEqual(kelly, 0.2, places=5)
    
    def test_no_edge(self):
        """Test Kelly with no edge (fair bet)."""
        # 50% win rate, even money - should return 0
        win_prob = 0.5
        win_amount = 1.0
        loss_amount = 1.0
        
        kelly = kelly_fraction(win_prob, win_amount, loss_amount)
        self.assertEqual(kelly, 0.0)
    
    def test_negative_edge(self):
        """Test Kelly with negative edge."""
        # 40% win rate, even money - no bet
        win_prob = 0.4
        win_amount = 1.0
        loss_amount = 1.0
        
        kelly = kelly_fraction(win_prob, win_amount, loss_amount)
        self.assertEqual(kelly, 0.0)
    
    def test_extreme_edge(self):
        """Test Kelly with very high edge."""
        # 96% win rate, buying a 92 cent contract that pays $1
        # Win: $0.08, Lose: $0.92
        win_prob = 0.96
        win_amount = 0.08
        loss_amount = 0.92
        
        kelly = kelly_fraction(win_prob, win_amount, loss_amount)
        # Kelly should be around 0.5 (50% of bankroll)
        self.assertGreater(kelly, 0.4)
        self.assertLess(kelly, 0.6)
    
    def test_invalid_inputs(self):
        """Test Kelly with invalid inputs."""
        # Zero or negative loss amount
        self.assertEqual(kelly_fraction(0.6, 1.0, 0.0), 0.0)
        self.assertEqual(kelly_fraction(0.6, 1.0, -1.0), 0.0)
        
        # Invalid probabilities
        self.assertEqual(kelly_fraction(0.0, 1.0, 1.0), 0.0)
        self.assertEqual(kelly_fraction(1.0, 1.0, 1.0), 0.0)
        self.assertEqual(kelly_fraction(-0.1, 1.0, 1.0), 0.0)
        self.assertEqual(kelly_fraction(1.1, 1.0, 1.0), 0.0)


class TestSizePosition(unittest.TestCase):
    """Test position sizing calculations."""
    
    def test_basic_sizing(self):
        """Test basic position sizing."""
        bankroll = 1000.0
        win_prob = 0.6
        contract_price_cents = 50  # 50 cents
        max_trade_usd = 100.0
        
        quantity = size_position(bankroll, win_prob, contract_price_cents, max_trade_usd, kelly_multiplier=0.5)
        
        # Should return a positive integer
        self.assertIsInstance(quantity, int)
        self.assertGreater(quantity, 0)
        
        # Should not exceed maximum trade limit
        cost = (quantity * contract_price_cents) / 100.0
        self.assertLessEqual(cost, max_trade_usd)
    
    def test_insufficient_bankroll(self):
        """Test sizing with insufficient bankroll."""
        bankroll = 10.0
        win_prob = 0.6
        contract_price_cents = 95  # 95 cents per contract
        max_trade_usd = 100.0
        
        quantity = size_position(bankroll, win_prob, contract_price_cents, max_trade_usd)
        
        # With a small edge and small bankroll, Kelly may size to 0
        # This is correct behavior - Kelly says don't bet if edge isn't sufficient
        self.assertIsInstance(quantity, int)
        self.assertGreaterEqual(quantity, 0)
    
    def test_max_trade_limit(self):
        """Test that max trade limit is respected."""
        bankroll = 10000.0  # Large bankroll
        win_prob = 0.8  # High edge
        contract_price_cents = 50
        max_trade_usd = 100.0  # Small max trade
        
        quantity = size_position(bankroll, win_prob, contract_price_cents, max_trade_usd)
        
        cost = (quantity * contract_price_cents) / 100.0
        self.assertLessEqual(cost, max_trade_usd)
    
    def test_zero_bankroll(self):
        """Test sizing with zero bankroll."""
        bankroll = 0.0
        win_prob = 0.6
        contract_price_cents = 50
        max_trade_usd = 100.0
        
        quantity = size_position(bankroll, win_prob, contract_price_cents, max_trade_usd)
        self.assertEqual(quantity, 0)
    
    def test_zero_win_prob(self):
        """Test sizing with zero win probability."""
        bankroll = 1000.0
        win_prob = 0.0
        contract_price_cents = 50
        max_trade_usd = 100.0
        
        quantity = size_position(bankroll, win_prob, contract_price_cents, max_trade_usd)
        self.assertEqual(quantity, 0)
    
    def test_full_kelly_vs_half_kelly(self):
        """Test that half-Kelly is more conservative than full Kelly."""
        bankroll = 1000.0
        win_prob = 0.7
        contract_price_cents = 50
        max_trade_usd = 500.0
        
        full_kelly_qty = size_position(bankroll, win_prob, contract_price_cents, max_trade_usd, kelly_multiplier=1.0)
        half_kelly_qty = size_position(bankroll, win_prob, contract_price_cents, max_trade_usd, kelly_multiplier=0.5)
        
        # Half Kelly should bet less than or equal to full Kelly
        self.assertLessEqual(half_kelly_qty, full_kelly_qty)


if __name__ == '__main__':
    unittest.main()
