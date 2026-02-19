"""Tests for _prefilter_markets() in KalshiArbitrageBot."""

import unittest
import sys
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_bot import KalshiArbitrageBot, KalshiAPI


def make_market(ticker='TEST', yes_ask=48, no_ask=50, volume=20,
                yes_bid=47, no_bid=49, minutes_from_now=120):
    close_time = (datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)).isoformat()
    return {
        'ticker': ticker,
        'title': f'Market {ticker}',
        'yes_ask': yes_ask,
        'no_ask': no_ask,
        'yes_bid': yes_bid,
        'no_bid': no_bid,
        'volume': volume,
        'close_time': close_time,
        'status': 'open',
    }


class TestPrefilterMarkets(unittest.TestCase):

    def setUp(self):
        api = Mock(spec=KalshiAPI)
        self.bot = KalshiArbitrageBot(api, min_profit_percent=1.0)

    def test_profitable_market_passes(self):
        """Market with YES+NO < 100 passes pre-filter."""
        markets = [make_market(yes_ask=45, no_ask=50, volume=20)]
        result = self.bot._prefilter_markets(markets)
        self.assertEqual(len(result), 1)

    def test_unprofitable_market_filtered(self):
        """Market with YES+NO > 105 (buffer) is filtered out."""
        markets = [make_market(yes_ask=57, no_ask=50, volume=20)]
        result = self.bot._prefilter_markets(markets)
        self.assertEqual(len(result), 0)

    def test_zero_yes_ask_filtered(self):
        """Market with zero YES ask is filtered out."""
        markets = [make_market(yes_ask=0, no_ask=50, volume=20)]
        result = self.bot._prefilter_markets(markets)
        self.assertEqual(len(result), 0)

    def test_zero_no_ask_filtered(self):
        """Market with zero NO ask is filtered out."""
        markets = [make_market(yes_ask=48, no_ask=0, volume=20)]
        result = self.bot._prefilter_markets(markets)
        self.assertEqual(len(result), 0)

    def test_expired_market_filtered(self):
        """Market closing in less than MIN_EXPIRY_MINUTES is filtered."""
        markets = [make_market(yes_ask=45, no_ask=50, minutes_from_now=5)]
        result = self.bot._prefilter_markets(markets)
        self.assertEqual(len(result), 0)

    def test_multiple_markets_mixed(self):
        """Only profitable markets within time window pass."""
        markets = [
            make_market('GOOD', yes_ask=45, no_ask=50, volume=20, minutes_from_now=120),
            make_market('BAD_PRICE', yes_ask=57, no_ask=50, volume=20, minutes_from_now=120),
            make_market('EXPIRED', yes_ask=45, no_ask=50, volume=20, minutes_from_now=5),
        ]
        result = self.bot._prefilter_markets(markets)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['ticker'], 'GOOD')


if __name__ == '__main__':
    unittest.main()
