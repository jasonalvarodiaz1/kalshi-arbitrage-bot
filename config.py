import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    """Configuration management for Kalshi Arbitrage Bot"""
    
    # API Configuration
    KALSHI_EMAIL = os.getenv('KALSHI_EMAIL')
    KALSHI_PASSWORD = os.getenv('KALSHI_PASSWORD')
    
    # Bot Configuration
    MIN_PROFIT_PERCENT = float(os.getenv('MIN_PROFIT_PERCENT', 1.0))
    SCAN_INTERVAL_SECONDS = int(os.getenv('SCAN_INTERVAL_SECONDS', 60))
    PAPER_TRADING = os.getenv('PAPER_TRADING', 'true').lower() == 'true'
    
    # API Settings
    BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
    RATE_LIMIT_DELAY = 0.3  # Seconds between requests
    
    @classmethod
    def validate(cls):
        """Validate configuration"""
        if cls.MIN_PROFIT_PERCENT < 0:
            raise ValueError("MIN_PROFIT_PERCENT must be positive")
        if cls.SCAN_INTERVAL_SECONDS < 1:
            raise ValueError("SCAN_INTERVAL_SECONDS must be at least 1")
    
    @classmethod
    def print_config(cls):
        """Print current configuration (without sensitive data)"""
        print("Current Configuration:")
        print(f"  Min Profit: {cls.MIN_PROFIT_PERCENT}%")
        print(f"  Scan Interval: {cls.SCAN_INTERVAL_SECONDS}s")
        print(f"  Paper Trading: {cls.PAPER_TRADING}")
        print(f"  Authenticated: {bool(cls.KALSHI_EMAIL and cls.KALSHI_PASSWORD)}")
