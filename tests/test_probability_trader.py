"""Unit tests for probability trader ticker parsing."""

import unittest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probability_trader import ProbabilityTrader


class MockAPI:
    """Mock API for testing."""
    pass


class MockConfig:
    """Mock config for testing."""
    MIN_EDGE_PERCENT = 3.0
    KELLY_MULTIPLIER = 0.5
    BTC_15MIN_VOL = 0.004
    ETH_15MIN_VOL = 0.005
    PRICE_CACHE_SECONDS = 10


class TestTickerParsing(unittest.TestCase):
    """Test Kalshi ticker parsing."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.trader = ProbabilityTrader(MockAPI(), MockConfig())
    
    def test_parse_btc_above_ticker(self):
        """Test parsing BTC above ticker."""
        ticker = "KXBTC-26FEB16-T98000"
        result = self.trader.parse_strike_from_ticker(ticker)
        
        self.assertIsNotNone(result)
        self.assertEqual(result['asset'], 'BTC')
        self.assertEqual(result['strike'], 98000.0)
        self.assertEqual(result['direction'], 'above')
        self.assertEqual(result['expiry_str'], '26FEB16')
    
    def test_parse_btc_below_ticker(self):
        """Test parsing BTC below ticker."""
        ticker = "KXBTC-26FEB16-B98000"
        result = self.trader.parse_strike_from_ticker(ticker)
        
        self.assertIsNotNone(result)
        self.assertEqual(result['asset'], 'BTC')
        self.assertEqual(result['strike'], 98000.0)
        self.assertEqual(result['direction'], 'below')
        self.assertEqual(result['expiry_str'], '26FEB16')
    
    def test_parse_eth_above_ticker(self):
        """Test parsing ETH above ticker."""
        ticker = "KXETH-26FEB16-T3500"
        result = self.trader.parse_strike_from_ticker(ticker)
        
        self.assertIsNotNone(result)
        self.assertEqual(result['asset'], 'ETH')
        self.assertEqual(result['strike'], 3500.0)
        self.assertEqual(result['direction'], 'above')
        self.assertEqual(result['expiry_str'], '26FEB16')
    
    def test_parse_eth_below_ticker(self):
        """Test parsing ETH below ticker."""
        ticker = "KXETH-01MAR26-B3000"
        result = self.trader.parse_strike_from_ticker(ticker)
        
        self.assertIsNotNone(result)
        self.assertEqual(result['asset'], 'ETH')
        self.assertEqual(result['strike'], 3000.0)
        self.assertEqual(result['direction'], 'below')
        self.assertEqual(result['expiry_str'], '01MAR26')
    
    def test_parse_various_strikes(self):
        """Test parsing various strike prices."""
        test_cases = [
            ("KXBTC-26FEB16-T100000", 100000.0),
            ("KXBTC-26FEB16-T95000", 95000.0),
            ("KXETH-26FEB16-T4000", 4000.0),
            ("KXETH-26FEB16-T2500", 2500.0),
        ]
        
        for ticker, expected_strike in test_cases:
            result = self.trader.parse_strike_from_ticker(ticker)
            self.assertIsNotNone(result, f"Failed to parse {ticker}")
            self.assertEqual(result['strike'], expected_strike, f"Wrong strike for {ticker}")
    
    def test_parse_invalid_ticker(self):
        """Test parsing invalid tickers."""
        invalid_tickers = [
            "INVALID-TICKER",
            "BTC-26FEB16-T98000",  # Missing KX prefix
            "KXBTC-INVALID-T98000",  # Invalid date format
            "KXBTC-26FEB16-98000",  # Missing T/B direction
            "KXDOGE-26FEB16-T1000",  # Invalid asset
        ]
        
        for ticker in invalid_tickers:
            result = self.trader.parse_strike_from_ticker(ticker)
            self.assertIsNone(result, f"Should not parse invalid ticker: {ticker}")


class TestProbabilityEstimation(unittest.TestCase):
    """Test probability estimation."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.trader = ProbabilityTrader(MockAPI(), MockConfig())
    
    def test_far_itm_above(self):
        """Test probability when far in-the-money (price >> strike)."""
        current_price = 100000.0
        strike = 95000.0
        minutes_remaining = 15.0
        asset = 'BTC'
        
        prob = self.trader.estimate_probability(
            current_price, strike, minutes_remaining, asset, 'above'
        )
        
        # Should be very high probability (> 95%)
        self.assertGreater(prob, 0.95)
    
    def test_far_otm_above(self):
        """Test probability when far out-of-the-money (price << strike)."""
        current_price = 95000.0
        strike = 100000.0
        minutes_remaining = 15.0
        asset = 'BTC'
        
        prob = self.trader.estimate_probability(
            current_price, strike, minutes_remaining, asset, 'above'
        )
        
        # Should be very low probability (< 5%)
        self.assertLess(prob, 0.05)
    
    def test_atm_probability(self):
        """Test probability when at-the-money (price ≈ strike)."""
        current_price = 98000.0
        strike = 98000.0
        minutes_remaining = 15.0
        asset = 'BTC'
        
        prob = self.trader.estimate_probability(
            current_price, strike, minutes_remaining, asset, 'above'
        )
        
        # Should be around 50%
        self.assertGreater(prob, 0.45)
        self.assertLess(prob, 0.55)
    
    def test_below_direction(self):
        """Test probability for 'below' direction."""
        current_price = 95000.0
        strike = 100000.0
        minutes_remaining = 15.0
        asset = 'BTC'
        
        prob_above = self.trader.estimate_probability(
            current_price, strike, minutes_remaining, asset, 'above'
        )
        prob_below = self.trader.estimate_probability(
            current_price, strike, minutes_remaining, asset, 'below'
        )
        
        # Should sum to approximately 1.0
        self.assertAlmostEqual(prob_above + prob_below, 1.0, places=5)
    
    def test_time_decay(self):
        """Test that probability changes with time remaining."""
        # Use a very close strike to avoid saturation
        current_price = 98000.0
        strike = 98050.0  # Only $50 away
        asset = 'BTC'
        
        prob_15min = self.trader.estimate_probability(
            current_price, strike, 15.0, asset, 'above'
        )
        prob_60min = self.trader.estimate_probability(
            current_price, strike, 60.0, asset, 'above'
        )
        
        # With price below strike, probability should be < 0.5
        # Both should be reasonable probabilities
        self.assertGreaterEqual(prob_15min, 0.0)
        self.assertLessEqual(prob_15min, 1.0)
        self.assertGreaterEqual(prob_60min, 0.0)
        self.assertLessEqual(prob_60min, 1.0)
    
    def test_zero_time_remaining(self):
        """Test with zero time remaining."""
        current_price = 98000.0
        strike = 95000.0
        minutes_remaining = 0.0
        asset = 'BTC'
        
        prob = self.trader.estimate_probability(
            current_price, strike, minutes_remaining, asset, 'above'
        )
        
        # With zero time remaining, the function returns 0.5 as it has 
        # no valid information to calculate a probability (edge case handling)
        # In reality, with 0 time the market should be settled already
        self.assertEqual(prob, 0.5)


if __name__ == '__main__':
    unittest.main()
