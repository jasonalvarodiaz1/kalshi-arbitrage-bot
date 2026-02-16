import sqlite3
import json
from datetime import datetime
from typing import List, Dict, Optional

class Storage:
    """SQLite storage for trade history and opportunity tracking."""

    def __init__(self, db_path: str = "kalshi_bot.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def _create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL DEFAULT 'single',
                ticker TEXT,
                event_ticker TEXT,
                title TEXT,
                yes_price INTEGER,
                no_price INTEGER,
                total_cost INTEGER,
                profit_cents INTEGER,
                profit_percent REAL,
                max_executable_qty INTEGER,
                strategy TEXT,
                legs_json TEXT,
                detected_at TEXT NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                trade_type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                yes_price INTEGER,
                no_price INTEGER,
                cost_usd REAL NOT NULL,
                expected_profit_usd REAL,
                paper_trade BOOLEAN NOT NULL DEFAULT 1,
                yes_order_id TEXT,
                no_order_id TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                executed_at TEXT NOT NULL
            )
        ''')
        self.conn.commit()

    def log_opportunity(self, opportunity: Dict):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO opportunities (type, ticker, event_ticker, title, yes_price, no_price,
                total_cost, profit_cents, profit_percent, max_executable_qty, strategy, legs_json, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            opportunity.get('type', 'single'),
            opportunity.get('ticker'),
            opportunity.get('event_ticker'),
            opportunity.get('title'),
            opportunity.get('yes_price'),
            opportunity.get('no_price'),
            opportunity.get('total_cost'),
            opportunity.get('profit_cents'),
            opportunity.get('profit_percent'),
            opportunity.get('max_executable_qty'),
            opportunity.get('strategy'),
            json.dumps(opportunity.get('legs')) if opportunity.get('legs') else None,
            opportunity.get('timestamp', datetime.now().isoformat())
        ))
        self.conn.commit()

    def log_trade(self, trade: Dict):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO trades (ticker, trade_type, quantity, yes_price, no_price, cost_usd,
                expected_profit_usd, paper_trade, yes_order_id, no_order_id, status, executed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade.get('ticker'),
            trade.get('type', 'arbitrage'),
            trade.get('quantity'),
            trade.get('yes_price'),
            trade.get('no_price'),
            trade.get('cost'),
            trade.get('expected_profit'),
            trade.get('paper_trade', True),
            trade.get('yes_order', {}).get('order_id') if isinstance(trade.get('yes_order'), dict) else None,
            trade.get('no_order', {}).get('order_id') if isinstance(trade.get('no_order'), dict) else None,
            'open',
            trade.get('timestamp', datetime.now().isoformat())
        ))
        self.conn.commit()

    def get_all_opportunities(self) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM opportunities ORDER BY detected_at DESC')
        return [dict(row) for row in cursor.fetchall()]

    def get_all_trades(self) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM trades ORDER BY executed_at DESC')
        return [dict(row) for row in cursor.fetchall()]

    def get_opportunity_stats(self) -> Dict:
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) as total, AVG(profit_percent) as avg_profit, MAX(profit_percent) as max_profit FROM opportunities')
        row = cursor.fetchone()
        return dict(row) if row else {}

    def close(self):
        self.conn.close()
