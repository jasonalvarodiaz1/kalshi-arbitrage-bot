"""SQLite storage for Kalshi trading bot."""

import sqlite3
import json
import logging
from typing import Dict, List, Optional
from datetime import datetime
from pathlib import Path


logger = logging.getLogger('kalshi_bot')


class Storage:
    """SQLite storage for opportunities and trades."""
    
    def __init__(self, db_path: str = "kalshi_bot.db"):
        """
        Initialize storage.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self.conn = None
        self._init_db()
    
    def _init_db(self):
        """Initialize database tables."""
        try:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            
            cursor = self.conn.cursor()
            
            # Opportunities table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS opportunities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    title TEXT,
                    strategy_type TEXT,
                    profit_percent REAL,
                    profit_cents INTEGER,
                    yes_price INTEGER,
                    no_price INTEGER,
                    total_cost INTEGER,
                    max_qty INTEGER,
                    timestamp TEXT NOT NULL,
                    metadata TEXT
                )
            """)
            
            # Trades table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    trade_type TEXT,
                    side TEXT,
                    quantity INTEGER,
                    price INTEGER,
                    cost REAL,
                    expected_profit REAL,
                    realized_profit REAL,
                    status TEXT DEFAULT 'open',
                    paper_trading BOOLEAN,
                    timestamp TEXT NOT NULL,
                    settled_at TEXT,
                    metadata TEXT
                )
            """)
            
            # Create indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_opportunities_timestamp ON opportunities(timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_opportunities_ticker ON opportunities(ticker)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
            
            self.conn.commit()
            logger.info("Storage initialized at %s", self.db_path)
            
        except Exception as e:
            logger.error("Failed to initialize storage: %s", e)
            raise
    
    def save_opportunity(self, opportunity: Dict) -> Optional[int]:
        """
        Save an arbitrage opportunity.
        
        Args:
            opportunity: Opportunity dict
            
        Returns:
            Row ID if successful, None otherwise
        """
        try:
            cursor = self.conn.cursor()
            
            # Extract common fields
            ticker = opportunity.get('ticker')
            title = opportunity.get('title')
            strategy_type = opportunity.get('type', 'single')
            profit_percent = opportunity.get('profit_percent')
            profit_cents = opportunity.get('profit_cents')
            yes_price = opportunity.get('yes_price')
            no_price = opportunity.get('no_price')
            total_cost = opportunity.get('total_cost')
            max_qty = opportunity.get('max_executable_qty')
            timestamp = opportunity.get('timestamp', datetime.now().isoformat())
            
            # Store full opportunity as JSON metadata
            metadata = json.dumps(opportunity)
            
            cursor.execute("""
                INSERT INTO opportunities (
                    ticker, title, strategy_type, profit_percent, profit_cents,
                    yes_price, no_price, total_cost, max_qty, timestamp, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ticker, title, strategy_type, profit_percent, profit_cents,
                  yes_price, no_price, total_cost, max_qty, timestamp, metadata))
            
            self.conn.commit()
            return cursor.lastrowid
            
        except Exception as e:
            logger.error("Failed to save opportunity: %s", e)
            return None
    
    def save_trade(self, trade: Dict) -> Optional[int]:
        """
        Save a trade.
        
        Args:
            trade: Trade dict
            
        Returns:
            Row ID if successful, None otherwise
        """
        try:
            cursor = self.conn.cursor()
            
            ticker = trade.get('ticker')
            trade_type = trade.get('type', 'arbitrage')
            side = trade.get('side')
            quantity = trade.get('quantity')
            # Extract price from trade dict, handling multiple field names
            # Check explicitly for None to avoid issues with 0 cent prices
            if trade.get('yes_price') is not None:
                price = trade.get('yes_price')
            elif trade.get('no_price') is not None:
                price = trade.get('no_price')
            else:
                price = trade.get('price')
            cost = trade.get('cost')
            expected_profit = trade.get('expected_profit')
            realized_profit = trade.get('realized_profit')
            status = trade.get('status', 'open')
            paper_trading = trade.get('paper_trading', True)
            timestamp = trade.get('timestamp', datetime.now().isoformat())
            settled_at = trade.get('settled_at')
            
            # Store full trade as JSON metadata
            metadata = json.dumps(trade)
            
            cursor.execute("""
                INSERT INTO trades (
                    ticker, trade_type, side, quantity, price, cost,
                    expected_profit, realized_profit, status, paper_trading,
                    timestamp, settled_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ticker, trade_type, side, quantity, price, cost,
                  expected_profit, realized_profit, status, paper_trading,
                  timestamp, settled_at, metadata))
            
            self.conn.commit()
            return cursor.lastrowid
            
        except Exception as e:
            logger.error("Failed to save trade: %s", e)
            return None
    
    def update_trade_status(self, trade_id: int, status: str, 
                           realized_profit: Optional[float] = None,
                           settled_at: Optional[str] = None) -> bool:
        """
        Update trade status.
        
        Args:
            trade_id: Trade ID
            status: New status
            realized_profit: Realized profit (optional)
            settled_at: Settlement timestamp (optional)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            cursor = self.conn.cursor()
            
            if realized_profit is not None and settled_at is not None:
                cursor.execute("""
                    UPDATE trades
                    SET status = ?, realized_profit = ?, settled_at = ?
                    WHERE id = ?
                """, (status, realized_profit, settled_at, trade_id))
            else:
                cursor.execute("""
                    UPDATE trades
                    SET status = ?
                    WHERE id = ?
                """, (status, trade_id))
            
            self.conn.commit()
            return True
            
        except Exception as e:
            logger.error("Failed to update trade status: %s", e)
            return False
    
    def get_recent_opportunities(self, limit: int = 100) -> List[Dict]:
        """
        Get recent opportunities.
        
        Args:
            limit: Maximum number to return
            
        Returns:
            List of opportunity dicts
        """
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM opportunities
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,))
            
            rows = cursor.fetchall()
            return [json.loads(row['metadata']) for row in rows]
            
        except Exception as e:
            logger.error("Failed to get recent opportunities: %s", e)
            return []
    
    def get_recent_trades(self, limit: int = 100, status: Optional[str] = None) -> List[Dict]:
        """
        Get recent trades.
        
        Args:
            limit: Maximum number to return
            status: Filter by status (optional)
            
        Returns:
            List of trade dicts
        """
        try:
            cursor = self.conn.cursor()
            
            if status:
                cursor.execute("""
                    SELECT * FROM trades
                    WHERE status = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (status, limit))
            else:
                cursor.execute("""
                    SELECT * FROM trades
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (limit,))
            
            rows = cursor.fetchall()
            return [json.loads(row['metadata']) for row in rows]
            
        except Exception as e:
            logger.error("Failed to get recent trades: %s", e)
            return []
    
    def get_stats(self) -> Dict:
        """
        Get storage statistics.
        
        Returns:
            Dict with stats
        """
        try:
            cursor = self.conn.cursor()
            
            # Count opportunities
            cursor.execute("SELECT COUNT(*) as count FROM opportunities")
            opp_count = cursor.fetchone()['count']
            
            # Count trades by status
            cursor.execute("SELECT status, COUNT(*) as count FROM trades GROUP BY status")
            trade_counts = {row['status']: row['count'] for row in cursor.fetchall()}
            
            # Sum realized profits
            cursor.execute("SELECT SUM(realized_profit) as total FROM trades WHERE realized_profit IS NOT NULL")
            total_realized = cursor.fetchone()['total'] or 0.0
            
            return {
                'opportunities_count': opp_count,
                'trades_by_status': trade_counts,
                'total_realized_profit': total_realized
            }
            
        except Exception as e:
            logger.error("Failed to get stats: %s", e)
            return {}
    
    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            logger.info("Storage connection closed")
