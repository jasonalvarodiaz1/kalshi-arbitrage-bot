import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

class Config:
    """Configuration management for Kalshi Arbitrage Bot"""
    
    # API Configuration
    KALSHI_EMAIL = os.getenv('KALSHI_EMAIL')
    KALSHI_PASSWORD = os.getenv('KALSHI_PASSWORD')
    KALSHI_API_KEY = os.getenv('KALSHI_API_KEY')
    KALSHI_PRIVATE_KEY_PATH = os.getenv('KALSHI_PRIVATE_KEY_PATH')
    
    # Bot Configuration
    MIN_PROFIT_PERCENT = float(os.getenv('MIN_PROFIT_PERCENT', 1.0))
    SCAN_INTERVAL_SECONDS = int(os.getenv('SCAN_INTERVAL_SECONDS', 60))
    PAPER_TRADING = os.getenv('PAPER_TRADING', 'true').lower() == 'true'
    # Live trading safety
    LIVE_TRADING_ENABLED = os.getenv('ENABLE_LIVE_TRADING', 'false').lower() == 'true'
    MAX_TRADE_USD = float(os.getenv('MAX_TRADE_USD', 100.0))
    
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
        auth_state = 'API key' if cls.KALSHI_API_KEY else ('email/password' if (cls.KALSHI_EMAIL and cls.KALSHI_PASSWORD) else 'none')
        print(f"  Authenticated: {auth_state}")
        print(f"  Live Trading Enabled: {cls.LIVE_TRADING_ENABLED}")
        print(f"  Max Trade USD: ${cls.MAX_TRADE_USD:.2f}")
        # Do not print private key contents
        print(f"  Private Key Path: {cls.KALSHI_PRIVATE_KEY_PATH or 'not set'}")

    @classmethod
    def load_private_key(cls) -> Optional[str]:
        """Load private key PEM from file if provided."""
        path = cls.KALSHI_PRIVATE_KEY_PATH
        if not path:
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception:
            return None
