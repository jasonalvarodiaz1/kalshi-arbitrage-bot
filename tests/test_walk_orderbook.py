"""Unit tests for _walk_orderbook() VWAP calculations.

Tests orderbook depth walking with synthetic orderbooks.
No live API connection required.
"""

import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_bot import KalshiArbitrageBot


class MockAPI:
    """Minimal mock — walk_orderbook doesn't touch the API."""
    pass


class TestWalkOrderbookBasic(unittest.TestCase):
    """Basic orderbook walking scenarios."""

    def setUp(self):
        self.bot = KalshiArbitrageBot(MockAPI(), min_profit_percent=2.0)

    # ---- empty / degenerate inputs ----

    def test_empty_asks_returns_none(self):
        self.assertIsNone(self.bot._walk_orderbook([], 5))

    def test_none_asks_returns_none(self):
        """None instead of a list should return None (or raise gracefully)."""
        result = self.bot._walk_orderbook(None, 5)
        self.assertIsNone(result)

    def test_zero_target_qty(self):
        """Requesting 0 contracts should fill 0."""
        result = self.bot._walk_orderbook([[50, 10]], 0)
        # Either None or dict with filled_qty == 0 is acceptable
        if result is not None:
            self.assertEqual(result['filled_qty'], 0)


class TestWalkOrderbookSingleLevel(unittest.TestCase):
    """All liquidity sits at one price level."""

    def setUp(self):
        self.bot = KalshiArbitrageBot(MockAPI(), min_profit_percent=2.0)

    def test_exact_fill(self):
        """Request exactly matches available depth."""
        asks = [[50, 10]]
        result = self.bot._walk_orderbook(asks, 10)

        self.assertIsNotNone(result)
        self.assertEqual(result['avg_price'], 50)
        self.assertEqual(result['total_cost'], 500)
        self.assertEqual(result['filled_qty'], 10)
        self.assertEqual(result['levels_used'], 1)
        self.assertTrue(result['fully_filled'])

    def test_partial_fill(self):
        """Request less than available depth."""
        asks = [[50, 10]]
        result = self.bot._walk_orderbook(asks, 3)

        self.assertEqual(result['avg_price'], 50)
        self.assertEqual(result['total_cost'], 150)
        self.assertEqual(result['filled_qty'], 3)
        self.assertTrue(result['fully_filled'])

    def test_insufficient_depth(self):
        """Request exceeds available depth."""
        asks = [[50, 5]]
        result = self.bot._walk_orderbook(asks, 10)

        self.assertIsNotNone(result)
        self.assertEqual(result['filled_qty'], 5)
        self.assertFalse(result['fully_filled'])
        self.assertEqual(result['avg_price'], 50)


class TestWalkOrderbookMultiLevel(unittest.TestCase):
    """Multiple price levels — tests VWAP accuracy."""

    def setUp(self):
        self.bot = KalshiArbitrageBot(MockAPI(), min_profit_percent=2.0)

    def test_two_levels_full_fill(self):
        """Fill across two levels, verify VWAP."""
        asks = [
            [45, 10],  # 10 @ 45¢  = 450¢
            [50, 10],  # 10 @ 50¢  = 500¢
        ]
        result = self.bot._walk_orderbook(asks, 20)

        self.assertTrue(result['fully_filled'])
        self.assertEqual(result['filled_qty'], 20)
        self.assertEqual(result['total_cost'], 950)
        self.assertAlmostEqual(result['avg_price'], 47.5, places=2)
        self.assertEqual(result['levels_used'], 2)

    def test_three_levels_partial_third(self):
        """Fill uses first two levels fully and part of third."""
        asks = [
            [40, 5],   # 5 @ 40¢  = 200¢
            [45, 5],   # 5 @ 45¢  = 225¢
            [50, 10],  # take 5 @ 50¢ = 250¢
        ]
        result = self.bot._walk_orderbook(asks, 15)

        self.assertTrue(result['fully_filled'])
        self.assertEqual(result['filled_qty'], 15)
        # total = 200 + 225 + 250 = 675
        self.assertEqual(result['total_cost'], 675)
        self.assertAlmostEqual(result['avg_price'], 45.0, places=2)
        self.assertEqual(result['levels_used'], 3)

    def test_unsorted_asks_are_handled(self):
        """Asks may not arrive pre-sorted; _walk_orderbook sorts internally."""
        asks = [
            [55, 5],   # worst price listed first
            [40, 5],   # best price listed last
            [48, 5],
        ]
        result = self.bot._walk_orderbook(asks, 10)

        self.assertTrue(result['fully_filled'])
        # Should fill: 5@40 + 5@48 = 200 + 240 = 440
        self.assertEqual(result['total_cost'], 440)
        self.assertAlmostEqual(result['avg_price'], 44.0, places=2)
        # Only 2 levels consumed
        self.assertEqual(result['levels_used'], 2)

    def test_fills_cheapest_first(self):
        """Verify that the walker always fills at the cheapest available level first."""
        asks = [
            [90, 100],
            [10, 1],
        ]
        result = self.bot._walk_orderbook(asks, 1)

        # Should take the single contract at 10¢, not 90¢
        self.assertEqual(result['avg_price'], 10)
        self.assertEqual(result['filled_qty'], 1)


class TestWalkOrderbookVWAP(unittest.TestCase):
    """Focused VWAP (volume-weighted average price) correctness tests."""

    def setUp(self):
        self.bot = KalshiArbitrageBot(MockAPI(), min_profit_percent=2.0)

    def test_uniform_prices(self):
        """All levels at same price — VWAP equals that price."""
        asks = [[50, 5], [50, 5], [50, 5]]
        result = self.bot._walk_orderbook(asks, 15)

        self.assertEqual(result['avg_price'], 50)

    def test_weighted_average_precision(self):
        """Specific VWAP regression check."""
        asks = [
            [48, 5],   # 240
            [49, 10],  # 490
            [50, 5],   # never reached
        ]
        result = self.bot._walk_orderbook(asks, 12)

        # 5@48 + 7@49 = 240 + 343 = 583 / 12 ≈ 48.583
        expected_avg = 583 / 12
        self.assertAlmostEqual(result['avg_price'], expected_avg, places=2)
        self.assertEqual(result['filled_qty'], 12)
        self.assertTrue(result['fully_filled'])

    def test_large_spread_vwap(self):
        """Large spread between levels still calculates correctly."""
        asks = [
            [5, 1],    # 1 @ 5¢
            [95, 1],   # 1 @ 95¢
        ]
        result = self.bot._walk_orderbook(asks, 2)

        # (5 + 95) / 2 = 50
        self.assertEqual(result['avg_price'], 50)
        self.assertEqual(result['total_cost'], 100)


class TestWalkOrderbookEdgeCases(unittest.TestCase):
    """Edge cases and boundary conditions."""

    def setUp(self):
        self.bot = KalshiArbitrageBot(MockAPI(), min_profit_percent=2.0)

    def test_single_contract_at_penny(self):
        """Minimum possible values."""
        asks = [[1, 1]]
        result = self.bot._walk_orderbook(asks, 1)

        self.assertTrue(result['fully_filled'])
        self.assertEqual(result['avg_price'], 1)
        self.assertEqual(result['total_cost'], 1)

    def test_single_contract_at_99(self):
        """Maximum typical price."""
        asks = [[99, 1]]
        result = self.bot._walk_orderbook(asks, 1)

        self.assertTrue(result['fully_filled'])
        self.assertEqual(result['avg_price'], 99)

    def test_target_one_from_large_book(self):
        """Taking just 1 contract from a deep book."""
        asks = [
            [45, 100],
            [46, 200],
            [47, 500],
        ]
        result = self.bot._walk_orderbook(asks, 1)

        self.assertTrue(result['fully_filled'])
        self.assertEqual(result['avg_price'], 45)
        self.assertEqual(result['levels_used'], 1)

    def test_large_quantity_across_many_levels(self):
        """Walk through many small levels."""
        asks = [[40 + i, 1] for i in range(20)]  # 20 levels, 1 each
        result = self.bot._walk_orderbook(asks, 20)

        self.assertTrue(result['fully_filled'])
        self.assertEqual(result['filled_qty'], 20)
        self.assertEqual(result['levels_used'], 20)
        # avg of 40..59 = 49.5
        self.assertAlmostEqual(result['avg_price'], 49.5, places=2)


if __name__ == '__main__':
    unittest.main()
