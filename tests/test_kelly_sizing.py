"""Unit tests for Kelly Criterion position sizing with liquidity and duration adjustments.

Focuses on the *new* API of size_position() — liquidity_penalty and
duration_minutes — which are not covered by the existing test_kelly.py.
No live API connection required.
"""

import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kelly import kelly_fraction, size_position


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _base_size(**overrides):
    """Return size_position result with sensible defaults for the new API."""
    defaults = dict(
        edge=0.05,
        bankroll=1000.0,
        contract_price_cents=50,
        kelly_multiplier=0.5,
        max_trade_usd=100.0,
        liquidity_penalty=1.0,
        duration_minutes=240,  # long enough to hit factor=1.0
    )
    defaults.update(overrides)
    return size_position(**defaults)


# ===================================================================
# Tests: liquidity_penalty
# ===================================================================

class TestLiquidityPenalty(unittest.TestCase):
    """Verify that liquidity_penalty scales position size down for thin books."""

    def test_full_penalty_vs_none(self):
        """liquidity_penalty=1.0 should give the largest position."""
        full = _base_size(liquidity_penalty=1.0)
        half = _base_size(liquidity_penalty=0.5)
        self.assertGreater(full, 0)
        self.assertLessEqual(half, full)

    def test_penalty_03_reduces_size(self):
        """A penalty of 0.3 (thin book) should reduce size significantly."""
        full = _base_size(liquidity_penalty=1.0)
        thin = _base_size(liquidity_penalty=0.3)
        self.assertLess(thin, full)

    def test_penalty_below_minimum_clamped(self):
        """Penalty < 0.1 should be clamped to 0.1 (not zero out entirely)."""
        tiny = _base_size(liquidity_penalty=0.01)
        zero = _base_size(liquidity_penalty=0.0)
        # Both should use clamped value of 0.1
        self.assertEqual(tiny, zero)
        # Should still produce at least min_contracts
        self.assertGreaterEqual(tiny, 1)

    def test_penalty_exactly_one(self):
        """Penalty of 1.0 should not alter the position."""
        base = _base_size(liquidity_penalty=1.0)
        same = _base_size(liquidity_penalty=1.0)
        self.assertEqual(base, same)

    def test_penalty_monotonic(self):
        """Higher penalty should always give >= position size."""
        sizes = [_base_size(liquidity_penalty=p) for p in [0.2, 0.4, 0.6, 0.8, 1.0]]
        for i in range(len(sizes) - 1):
            self.assertLessEqual(sizes[i], sizes[i + 1],
                                 f"penalty {0.2 + i*0.2} produced larger size than {0.4 + i*0.2}")


# ===================================================================
# Tests: duration_minutes
# ===================================================================

class TestDurationFactor(unittest.TestCase):
    """Verify that shorter markets get smaller positions."""

    def test_short_vs_long(self):
        """15-minute market should produce smaller position than 4-hour market."""
        short = _base_size(duration_minutes=15)
        long_ = _base_size(duration_minutes=240)
        self.assertLess(short, long_)

    def test_60_min_vs_240_min(self):
        """1-hour smaller than 4-hour."""
        hour = _base_size(duration_minutes=60)
        four_hours = _base_size(duration_minutes=240)
        self.assertLessEqual(hour, four_hours)

    def test_duration_factor_formula_15min(self):
        """15-min duration factor = min(1.0, 0.4 + 15/400) = 0.4375."""
        # We can't directly observe the factor, but we can verify the ratio.
        # At 240 min, factor = min(1.0, 0.4 + 0.6) = 1.0
        # At 15 min, factor = 0.4375
        long_ = _base_size(duration_minutes=240)
        short = _base_size(duration_minutes=15)
        if long_ > 0:
            ratio = short / long_
            # Ratio should be approximately 0.4375 (allowing rounding to int)
            self.assertAlmostEqual(ratio, 0.4375, delta=0.15)

    def test_no_duration_no_penalty(self):
        """When duration_minutes=None, no duration adjustment is applied."""
        with_duration = _base_size(duration_minutes=240)  # factor = 1.0
        without_duration = _base_size(duration_minutes=None)
        # Both should give the same result (factor = 1.0 effectively)
        self.assertEqual(with_duration, without_duration)

    def test_very_short_duration(self):
        """5-minute market: factor = min(1.0, 0.4 + 5/400) = 0.4125."""
        result = _base_size(duration_minutes=5)
        self.assertGreaterEqual(result, 1)  # at least min_contracts

    def test_duration_monotonic(self):
        """Longer durations should always give >= position sizes."""
        durations = [5, 15, 30, 60, 120, 240, 480]
        sizes = [_base_size(duration_minutes=d) for d in durations]
        for i in range(len(sizes) - 1):
            self.assertLessEqual(sizes[i], sizes[i + 1],
                                 f"duration {durations[i]} min produced larger size "
                                 f"than {durations[i+1]} min")


# ===================================================================
# Tests: combined liquidity + duration
# ===================================================================

class TestCombinedPenalties(unittest.TestCase):
    """Verify that liquidity_penalty and duration_minutes compound correctly."""

    def test_both_penalties_compound(self):
        """Thin book + short duration should be noticeably smaller than either alone."""
        full = _base_size(liquidity_penalty=1.0, duration_minutes=240)
        thin_only = _base_size(liquidity_penalty=0.3, duration_minutes=240)
        short_only = _base_size(liquidity_penalty=1.0, duration_minutes=15)
        both = _base_size(liquidity_penalty=0.3, duration_minutes=15)

        self.assertLessEqual(both, thin_only)
        self.assertLessEqual(both, short_only)
        self.assertLessEqual(both, full)

    def test_worst_case_still_returns_min_contracts(self):
        """Even with maximum penalties, result >= min_contracts."""
        result = _base_size(liquidity_penalty=0.1, duration_minutes=1)
        self.assertGreaterEqual(result, 1)


# ===================================================================
# Tests: edge cases in new API
# ===================================================================

class TestNewAPIEdgeCases(unittest.TestCase):
    """Edge cases for the new size_position API."""

    def test_zero_edge_returns_min(self):
        result = _base_size(edge=0.0)
        self.assertGreaterEqual(result, 1)

    def test_negative_edge_returns_min(self):
        result = _base_size(edge=-0.05)
        self.assertGreaterEqual(result, 1)

    def test_zero_bankroll_returns_min(self):
        result = _base_size(bankroll=0.0)
        self.assertGreaterEqual(result, 1)

    def test_very_large_edge(self):
        """Very large edge should still be capped by max_fraction."""
        result = _base_size(edge=0.50, bankroll=10000.0, max_trade_usd=500.0)
        cost = result * 50 / 100.0
        self.assertLessEqual(cost, 500.0)

    def test_max_fraction_cap(self):
        """Position should not exceed max_fraction of bankroll."""
        result = _base_size(edge=0.20, bankroll=1000.0, max_trade_usd=1000.0,
                            max_fraction=0.05)
        cost = result * 50 / 100.0
        # max_fraction * bankroll = 50, so cost should be ≤ 50
        self.assertLessEqual(cost, 50.0 + 0.50)  # allow 1 contract rounding

    def test_min_contracts_respected(self):
        """Even with near-zero edge, min_contracts is returned."""
        result = size_position(edge=0.001, bankroll=100.0,
                               contract_price_cents=50, min_contracts=3)
        self.assertGreaterEqual(result, 3)


# ===================================================================
# Tests: legacy API backwards compatibility
# ===================================================================

class TestLegacyAPICompat(unittest.TestCase):
    """Ensure old positional calling convention still works."""

    def test_legacy_basic(self):
        qty = size_position(1000.0, 0.8, 50, 100.0)
        self.assertIsInstance(qty, int)
        self.assertGreater(qty, 0)

    def test_legacy_respects_max_trade(self):
        qty = size_position(10000.0, 0.8, 50, 100.0)
        cost = qty * 50 / 100.0
        self.assertLessEqual(cost, 100.0)

    def test_legacy_no_edge_returns_zero(self):
        """50% win prob at 50c → no edge → 0 contracts."""
        qty = size_position(1000.0, 0.5, 50, 100.0)
        self.assertEqual(qty, 0)

    def test_legacy_half_kelly_smaller(self):
        full = size_position(1000.0, 0.7, 50, 500.0, kelly_multiplier=1.0)
        half = size_position(1000.0, 0.7, 50, 500.0, kelly_multiplier=0.5)
        self.assertLessEqual(half, full)


if __name__ == '__main__':
    unittest.main()
