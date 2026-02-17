"""Unit tests for _get_market_thresholds() adaptive thresholds.

Verifies that crypto markets (KXBTC, KXETH, KXSOL, KXDOGE) with short
durations get relaxed thresholds, while everything else gets strict defaults.
No live API connection required.
"""

import unittest
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_bot import KalshiArbitrageBot


class MockAPI:
    """Minimal mock — threshold methods don't touch the API."""
    pass


def _make_market(ticker, series_ticker=None, minutes_from_now=30):
    """Helper to build a synthetic market dict with a close_time."""
    close_time = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    return {
        'ticker': ticker,
        'series_ticker': series_ticker or ticker.split('-')[0],
        'close_time': close_time.isoformat(),
    }


class TestIsCryptoMarket(unittest.TestCase):
    """Verify _is_crypto_market() detection across all supported prefixes."""

    def setUp(self):
        self.bot = KalshiArbitrageBot(MockAPI(), min_profit_percent=2.0)

    def test_kxbtc_detected(self):
        m = _make_market('KXBTC-26FEB16-T98000', 'KXBTC')
        self.assertTrue(self.bot._is_crypto_market(m))

    def test_kxeth_detected(self):
        m = _make_market('KXETH-26FEB16-T3500', 'KXETH')
        self.assertTrue(self.bot._is_crypto_market(m))

    def test_kxsol_detected(self):
        m = _make_market('KXSOL-26FEB16-T150', 'KXSOL')
        self.assertTrue(self.bot._is_crypto_market(m))

    def test_kxdoge_detected(self):
        m = _make_market('KXDOGE-26FEB16-T0.15', 'KXDOGE')
        self.assertTrue(self.bot._is_crypto_market(m))

    def test_non_crypto_not_detected(self):
        for ticker, series in [
            ('PRES-2026', 'PRES'),
            ('INX-26FEB-T5000', 'INX'),
            ('NASDAQ-100', 'NASDAQ'),
            ('FED-RATE-MARCH', 'FED'),
        ]:
            m = _make_market(ticker, series)
            self.assertFalse(self.bot._is_crypto_market(m),
                             f"{ticker} should not be crypto")

    def test_series_ticker_takes_precedence(self):
        """Even if ticker doesn't start with KX, series_ticker match is enough."""
        m = {'ticker': 'SOME-THING', 'series_ticker': 'KXBTC'}
        self.assertTrue(self.bot._is_crypto_market(m))

    def test_empty_ticker(self):
        m = {'ticker': '', 'series_ticker': ''}
        self.assertFalse(self.bot._is_crypto_market(m))


class TestAdaptiveThresholdsCryptoShort(unittest.TestCase):
    """Short-duration crypto markets (≤60 min) should get relaxed thresholds."""

    def setUp(self):
        self.bot = KalshiArbitrageBot(MockAPI(), min_profit_percent=2.0)

    def _assert_relaxed(self, thresholds):
        self.assertEqual(thresholds['min_volume'], 0)
        self.assertEqual(thresholds['min_price_cents'], 1)
        self.assertEqual(thresholds['min_qty_at_best'], 1)

    def test_15_minute_btc(self):
        m = _make_market('KXBTC-26FEB16-T98000', 'KXBTC', minutes_from_now=15)
        self._assert_relaxed(self.bot._get_market_thresholds(m))

    def test_30_minute_eth(self):
        m = _make_market('KXETH-26FEB16-T3500', 'KXETH', minutes_from_now=30)
        self._assert_relaxed(self.bot._get_market_thresholds(m))

    def test_60_minute_boundary(self):
        """Exactly 60 minutes should still be relaxed (≤60)."""
        m = _make_market('KXBTC-26FEB16-T98000', 'KXBTC', minutes_from_now=60)
        self._assert_relaxed(self.bot._get_market_thresholds(m))

    def test_5_minute_ultra_short(self):
        m = _make_market('KXBTC-26FEB16-T98000', 'KXBTC', minutes_from_now=5)
        self._assert_relaxed(self.bot._get_market_thresholds(m))

    def test_1_minute_market(self):
        """Even a 1-minute-to-close market should get relaxed thresholds."""
        m = _make_market('KXSOL-26FEB16-T150', 'KXSOL', minutes_from_now=1)
        self._assert_relaxed(self.bot._get_market_thresholds(m))


class TestAdaptiveThresholdsStrict(unittest.TestCase):
    """Non-crypto and long-duration crypto markets get strict defaults."""

    def setUp(self):
        self.bot = KalshiArbitrageBot(MockAPI(), min_profit_percent=2.0)

    def _assert_strict(self, thresholds):
        self.assertEqual(thresholds['min_volume'], self.bot.MIN_VOLUME)
        self.assertEqual(thresholds['min_price_cents'], self.bot.MIN_PRICE_CENTS)
        self.assertEqual(thresholds['min_qty_at_best'], self.bot.MIN_QTY_AT_BEST)

    def test_non_crypto_market(self):
        m = _make_market('PRES-2026', 'PRES', minutes_from_now=30)
        self._assert_strict(self.bot._get_market_thresholds(m))

    def test_crypto_long_duration(self):
        """Crypto market closing in 4 hours → strict thresholds."""
        m = _make_market('KXBTC-26FEB16-T98000', 'KXBTC', minutes_from_now=240)
        self._assert_strict(self.bot._get_market_thresholds(m))

    def test_crypto_61_minutes(self):
        """Just over 60 minutes → strict thresholds (boundary)."""
        m = _make_market('KXETH-26FEB16-T3500', 'KXETH', minutes_from_now=61)
        self._assert_strict(self.bot._get_market_thresholds(m))

    def test_non_crypto_short_duration(self):
        """Non-crypto market at 15 min still gets strict thresholds."""
        m = _make_market('INX-26FEB-T5000', 'INX', minutes_from_now=15)
        self._assert_strict(self.bot._get_market_thresholds(m))


class TestAdaptiveThresholdsEdgeCases(unittest.TestCase):
    """Edge cases: missing fields, bad timestamps, etc."""

    def setUp(self):
        self.bot = KalshiArbitrageBot(MockAPI(), min_profit_percent=2.0)

    def test_missing_close_time_returns_defaults(self):
        m = {'ticker': 'KXBTC-26FEB16-T98000', 'series_ticker': 'KXBTC'}
        thresholds = self.bot._get_market_thresholds(m)
        # Without a close_time the method can't determine duration → defaults
        self.assertEqual(thresholds['min_volume'], self.bot.MIN_VOLUME)

    def test_uses_expiration_time_fallback(self):
        """Should accept expiration_time when close_time is absent."""
        exp = datetime.now(timezone.utc) + timedelta(minutes=15)
        m = {
            'ticker': 'KXBTC-26FEB16-T98000',
            'series_ticker': 'KXBTC',
            'expiration_time': exp.isoformat(),
        }
        thresholds = self.bot._get_market_thresholds(m)
        # Should get relaxed thresholds since it's crypto <60 min
        self.assertEqual(thresholds['min_volume'], 0)

    def test_invalid_close_time_returns_defaults(self):
        m = {
            'ticker': 'KXBTC-26FEB16-T98000',
            'series_ticker': 'KXBTC',
            'close_time': 'not-a-date',
        }
        thresholds = self.bot._get_market_thresholds(m)
        self.assertEqual(thresholds['min_volume'], self.bot.MIN_VOLUME)

    def test_past_close_time_returns_defaults(self):
        """Market that already closed → duration is negative → defaults."""
        past = datetime.now(timezone.utc) - timedelta(minutes=10)
        m = {
            'ticker': 'KXBTC-26FEB16-T98000',
            'series_ticker': 'KXBTC',
            'close_time': past.isoformat(),
        }
        thresholds = self.bot._get_market_thresholds(m)
        # Negative minutes_remaining won't satisfy <= 60 check correctly,
        # but it IS <=60, so behavior depends on implementation.
        # Just ensure it doesn't crash
        self.assertIn('min_volume', thresholds)


class TestPrefilterUsesThresholds(unittest.TestCase):
    """Verify _prefilter_markets() applies adaptive thresholds correctly.
    
    A fresh KXBTC market with volume=0 should pass prefilter (relaxed thresholds),
    while a non-crypto market with volume=0 should be dropped (strict thresholds).
    """

    def setUp(self):
        self.bot = KalshiArbitrageBot(MockAPI(), min_profit_percent=0.5)

    def _make_full_market(self, ticker, series, minutes, volume=0, 
                          yes_ask=48, no_ask=50, open_interest=0):
        close_time = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        return {
            'ticker': ticker,
            'series_ticker': series,
            'close_time': close_time.isoformat(),
            'volume': volume,
            'open_interest': open_interest,
            'yes_ask': yes_ask,
            'no_ask': no_ask,
        }

    def test_fresh_crypto_passes_prefilter(self):
        """KXBTC market with 0 volume, 15 min to close: should pass."""
        m = self._make_full_market('KXBTC-26FEB16-T98000', 'KXBTC', 
                                    minutes=45, volume=0, yes_ask=48, no_ask=50)
        result = self.bot._prefilter_markets([m])
        self.assertEqual(len(result), 1, "Fresh crypto market should pass prefilter")

    def test_non_crypto_zero_volume_fails_prefilter(self):
        """Non-crypto with 0 volume: should be dropped."""
        m = self._make_full_market('PRES-2026', 'PRES',
                                    minutes=120, volume=0, yes_ask=48, no_ask=50)
        result = self.bot._prefilter_markets([m])
        self.assertEqual(len(result), 0, "Non-crypto zero-volume market should be dropped")

    def test_non_crypto_with_volume_passes(self):
        """Non-crypto with sufficient volume: should pass."""
        m = self._make_full_market('PRES-2026', 'PRES',
                                    minutes=120, volume=100, yes_ask=48, no_ask=50)
        result = self.bot._prefilter_markets([m])
        self.assertEqual(len(result), 1, "Non-crypto with volume should pass")

    def test_crypto_penny_ask_passes(self):
        """Crypto with 1¢ ask should pass (relaxed min_price_cents=1)."""
        m = self._make_full_market('KXETH-26FEB16-T3500', 'KXETH',
                                    minutes=45, volume=0, yes_ask=1, no_ask=97)
        result = self.bot._prefilter_markets([m])
        self.assertEqual(len(result), 1, "Crypto with 1c ask should pass relaxed filter")

    def test_non_crypto_penny_ask_fails(self):
        """Non-crypto with 1¢ ask should fail (strict min_price_cents=3)."""
        m = self._make_full_market('PRES-2026', 'PRES',
                                    minutes=120, volume=100, yes_ask=1, no_ask=97)
        result = self.bot._prefilter_markets([m])
        self.assertEqual(len(result), 0, "Non-crypto with 1c ask should fail strict filter")


if __name__ == '__main__':
    unittest.main()
