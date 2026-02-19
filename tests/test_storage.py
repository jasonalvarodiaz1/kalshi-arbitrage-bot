"""Tests for SQLite storage operations."""

import unittest
import sys
import os
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage import Storage


class TestStorageSaveLoad(unittest.TestCase):
    """Test saving and loading opportunities and trades."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmpfile.close()
        self.storage = Storage(self.tmpfile.name)

    def tearDown(self):
        self.storage.close()
        os.unlink(self.tmpfile.name)

    def test_save_and_load_opportunity(self):
        opp = {
            'ticker': 'TEST-MARKET',
            'title': 'Test Market',
            'type': 'single',
            'profit_percent': 2.5,
            'profit_cents': 2,
            'yes_price': 48,
            'no_price': 50,
            'total_cost': 98,
            'max_executable_qty': 10,
            'timestamp': '2024-01-01T00:00:00',
        }
        row_id = self.storage.save_opportunity(opp)
        self.assertIsNotNone(row_id)
        self.assertGreater(row_id, 0)

        recent = self.storage.get_recent_opportunities(limit=10)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]['ticker'], 'TEST-MARKET')
        self.assertAlmostEqual(recent[0]['profit_percent'], 2.5)

    def test_save_and_load_trade(self):
        trade = {
            'ticker': 'TEST-MARKET',
            'type': 'arbitrage',
            'quantity': 5,
            'yes_price': 48,
            'no_price': 50,
            'cost': 4.9,
            'expected_profit': 0.1,
            'paper_trading': True,
            'timestamp': '2024-01-01T00:00:00',
        }
        row_id = self.storage.save_trade(trade)
        self.assertIsNotNone(row_id)
        self.assertGreater(row_id, 0)

        recent = self.storage.get_recent_trades(limit=10)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]['ticker'], 'TEST-MARKET')
        self.assertEqual(recent[0]['quantity'], 5)

    def test_get_stats_empty_db(self):
        stats = self.storage.get_stats()
        self.assertEqual(stats['opportunities_count'], 0)
        self.assertEqual(stats['total_realized_profit'], 0.0)

    def test_get_stats_with_data(self):
        self.storage.save_opportunity({'ticker': 'A', 'timestamp': '2024-01-01T00:00:00'})
        self.storage.save_opportunity({'ticker': 'B', 'timestamp': '2024-01-01T00:00:00'})
        self.storage.save_trade({'ticker': 'A', 'cost': 1.0, 'realized_profit': 0.05,
                                  'status': 'settled', 'timestamp': '2024-01-01T00:00:00'})
        stats = self.storage.get_stats()
        self.assertEqual(stats['opportunities_count'], 2)
        self.assertAlmostEqual(stats['total_realized_profit'], 0.05)

    def test_get_trades_by_status(self):
        self.storage.save_trade({'ticker': 'A', 'status': 'open', 'timestamp': '2024-01-01T00:00:00'})
        self.storage.save_trade({'ticker': 'B', 'status': 'settled', 'timestamp': '2024-01-01T00:00:00'})

        open_trades = self.storage.get_recent_trades(status='open')
        settled_trades = self.storage.get_recent_trades(status='settled')

        self.assertEqual(len(open_trades), 1)
        self.assertEqual(open_trades[0]['ticker'], 'A')
        self.assertEqual(len(settled_trades), 1)
        self.assertEqual(settled_trades[0]['ticker'], 'B')

    def test_multiple_opportunities_limit(self):
        for i in range(5):
            self.storage.save_opportunity({
                'ticker': f'MARKET-{i}',
                'timestamp': f'2024-01-0{i+1}T00:00:00',
            })
        recent = self.storage.get_recent_opportunities(limit=3)
        self.assertEqual(len(recent), 3)


if __name__ == '__main__':
    unittest.main()
