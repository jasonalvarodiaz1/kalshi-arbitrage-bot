"""Unit tests for arbitrage improvements (adaptive thresholds, orderbook walking, etc.)."""

import unittest
import sys
import os
from datetime import datetime, timezone, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_bot import KalshiArbitrageBot, KalshiAPI
from kelly import size_position


class MockAPI:
    """Mock API for testing"""
    def __init__(self):
        pass


class TestCryptoMarketDetection(unittest.TestCase):
    """Test crypto market detection and adaptive thresholds."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.api = MockAPI()
        self.bot = KalshiArbitrageBot(self.api, min_profit_percent=2.0)
    
    def test_is_crypto_market_kxbtc(self):
        """Test detection of KXBTC crypto market."""
        market = {'ticker': 'KXBTC-24FEB16-T4750', 'series_ticker': 'KXBTC'}
        self.assertTrue(self.bot._is_crypto_market(market))
    
    def test_is_crypto_market_kxeth(self):
        """Test detection of KXETH crypto market."""
        market = {'ticker': 'KXETH-24FEB16-T2800', 'series_ticker': 'KXETH'}
        self.assertTrue(self.bot._is_crypto_market(market))
    
    def test_is_crypto_market_kxsol(self):
        """Test detection of KXSOL crypto market."""
        market = {'ticker': 'KXSOL-24FEB16-T150', 'series_ticker': 'KXSOL'}
        self.assertTrue(self.bot._is_crypto_market(market))
    
    def test_is_not_crypto_market(self):
        """Test that non-crypto markets are not detected as crypto."""
        market = {'ticker': 'PRES-2024', 'series_ticker': 'PRES'}
        self.assertFalse(self.bot._is_crypto_market(market))
    
    def test_adaptive_thresholds_crypto_short_duration(self):
        """Test adaptive thresholds for short-duration crypto markets."""
        # Create a crypto market closing in 30 minutes
        close_time = datetime.now(timezone.utc) + timedelta(minutes=30)
        market = {
            'ticker': 'KXBTC-24FEB16-T4750',
            'series_ticker': 'KXBTC',
            'close_time': close_time.isoformat()
        }
        
        thresholds = self.bot._get_market_thresholds(market)
        
        # Should have relaxed thresholds
        self.assertEqual(thresholds['min_volume'], 0)
        self.assertEqual(thresholds['min_price_cents'], 1)
        self.assertEqual(thresholds['min_qty_at_best'], 1)
    
    def test_adaptive_thresholds_non_crypto(self):
        """Test adaptive thresholds for non-crypto markets."""
        close_time = datetime.now(timezone.utc) + timedelta(hours=6)
        market = {
            'ticker': 'PRES-2024',
            'series_ticker': 'PRES',
            'close_time': close_time.isoformat()
        }
        
        thresholds = self.bot._get_market_thresholds(market)
        
        # Should have default strict thresholds
        self.assertEqual(thresholds['min_volume'], self.bot.MIN_VOLUME)
        self.assertEqual(thresholds['min_price_cents'], self.bot.MIN_PRICE_CENTS)
        self.assertEqual(thresholds['min_qty_at_best'], self.bot.MIN_QTY_AT_BEST)
    
    def test_adaptive_thresholds_crypto_long_duration(self):
        """Test that long-duration crypto markets use default thresholds."""
        # Create a crypto market closing in 4 hours (> 60 minutes)
        close_time = datetime.now(timezone.utc) + timedelta(hours=4)
        market = {
            'ticker': 'KXBTC-24FEB16-T4750',
            'series_ticker': 'KXBTC',
            'close_time': close_time.isoformat()
        }
        
        thresholds = self.bot._get_market_thresholds(market)
        
        # Should use default thresholds for longer duration
        self.assertEqual(thresholds['min_volume'], self.bot.MIN_VOLUME)
        self.assertEqual(thresholds['min_price_cents'], self.bot.MIN_PRICE_CENTS)
        self.assertEqual(thresholds['min_qty_at_best'], self.bot.MIN_QTY_AT_BEST)


class TestOrderbookWalking(unittest.TestCase):
    """Test orderbook depth walking functionality."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.api = MockAPI()
        self.bot = KalshiArbitrageBot(self.api, min_profit_percent=2.0)
    
    def test_walk_orderbook_single_level(self):
        """Test walking orderbook with single price level."""
        asks = [[50, 10]]  # 10 contracts at 50¢
        result = self.bot._walk_orderbook(asks, 5)
        
        self.assertIsNotNone(result)
        self.assertEqual(result['avg_price'], 50)
        self.assertEqual(result['filled_qty'], 5)
        self.assertEqual(result['levels_used'], 1)
        self.assertTrue(result['fully_filled'])
    
    def test_walk_orderbook_multiple_levels(self):
        """Test walking orderbook across multiple price levels."""
        asks = [
            [48, 5],   # 5 contracts at 48¢
            [49, 10],  # 10 contracts at 49¢
            [50, 5],   # 5 contracts at 50¢
        ]
        result = self.bot._walk_orderbook(asks, 12)
        
        self.assertIsNotNone(result)
        # Should fill: 5@48¢ + 7@49¢ = 240 + 343 = 583¢ total / 12 = 48.58¢ avg
        self.assertAlmostEqual(result['avg_price'], 48.58, places=1)
        self.assertEqual(result['filled_qty'], 12)
        self.assertEqual(result['levels_used'], 2)
        self.assertTrue(result['fully_filled'])
    
    def test_walk_orderbook_insufficient_depth(self):
        """Test walking orderbook with insufficient depth."""
        asks = [[50, 5]]  # Only 5 contracts available
        result = self.bot._walk_orderbook(asks, 10)
        
        self.assertIsNotNone(result)
        self.assertEqual(result['filled_qty'], 5)
        self.assertFalse(result['fully_filled'])
    
    def test_walk_orderbook_empty(self):
        """Test walking empty orderbook."""
        asks = []
        result = self.bot._walk_orderbook(asks, 10)
        
        self.assertIsNone(result)
    
    def test_walk_orderbook_vwap_calculation(self):
        """Test VWAP calculation accuracy."""
        asks = [
            [45, 10],  # 10 @ 45¢ = 450¢
            [50, 10],  # 10 @ 50¢ = 500¢
        ]
        result = self.bot._walk_orderbook(asks, 20)
        
        # Total: 950¢ / 20 contracts = 47.5¢ average
        self.assertEqual(result['avg_price'], 47.5)
        self.assertEqual(result['total_cost'], 950)


class TestKellySizingEnhancements(unittest.TestCase):
    """Test new Kelly sizing parameters."""
    
    def test_liquidity_penalty(self):
        """Test that liquidity penalty reduces position size."""
        # New API with liquidity penalty
        full_size = size_position(
            edge=0.05,
            bankroll=1000.0,
            contract_price_cents=50,
            liquidity_penalty=1.0,
        )
        
        penalized_size = size_position(
            edge=0.05,
            bankroll=1000.0,
            contract_price_cents=50,
            liquidity_penalty=0.5,  # Thin book - halve position
        )
        
        # Penalized size should be smaller
        self.assertLess(penalized_size, full_size)
    
    def test_duration_penalty_short_market(self):
        """Test that short duration reduces position size."""
        long_duration_size = size_position(
            edge=0.05,
            bankroll=1000.0,
            contract_price_cents=50,
            duration_minutes=240,  # 4 hours
        )
        
        short_duration_size = size_position(
            edge=0.05,
            bankroll=1000.0,
            contract_price_cents=50,
            duration_minutes=15,  # 15 minutes
        )
        
        # Short duration should result in smaller position
        self.assertLess(short_duration_size, long_duration_size)
    
    def test_legacy_api_still_works(self):
        """Test that legacy API signature still works."""
        # Old signature: size_position(bankroll, win_prob, contract_price_cents, max_trade_usd)
        quantity = size_position(1000.0, 0.8, 50, 100.0)
        
        self.assertIsInstance(quantity, int)
        self.assertGreater(quantity, 0)


if __name__ == '__main__':
    unittest.main()
