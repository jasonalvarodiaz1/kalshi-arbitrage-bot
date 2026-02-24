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
    MIN_ORDER_QUANTITY = int(os.getenv('MIN_ORDER_QUANTITY', 5))
    SCAN_INTERVAL_SECONDS = int(os.getenv('SCAN_INTERVAL_SECONDS', 60))
    PAPER_TRADING = os.getenv('PAPER_TRADING', 'true').lower() == 'true'
    # Live trading safety
    LIVE_TRADING_ENABLED = os.getenv('ENABLE_LIVE_TRADING', 'false').lower() == 'true'
    WEATHER_LIVE_ONLY = os.getenv('WEATHER_LIVE_ONLY', 'true').lower() == 'true'  # Only live-trade weather, paper everything else
    DISABLE_CRYPTO = os.getenv('DISABLE_CRYPTO', 'false').lower() == 'true'  # Skip crypto scanning entirely
    MAX_TRADE_USD = float(os.getenv('MAX_TRADE_USD', 100.0))
    
    # Notification Configuration
    SMTP_HOST = os.getenv('SMTP_HOST')
    SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
    SMTP_USER = os.getenv('SMTP_USER')
    SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')
    NOTIFICATION_EMAIL = os.getenv('NOTIFICATION_EMAIL')
    WEBHOOK_URL = os.getenv('WEBHOOK_URL')
    
    # Filtering
    MIN_EXPIRY_MINUTES = int(os.getenv('MIN_EXPIRY_MINUTES', 30))
    MAX_EXPIRY_HOURS = int(os.getenv('MAX_EXPIRY_HOURS', 24))  # Only trade markets settling within this window
    
    # Position Limits
    MAX_POSITIONS = int(os.getenv('MAX_POSITIONS', 10))
    MAX_EXPOSURE_USD = float(os.getenv('MAX_EXPOSURE_USD', 500.0))
    
    # Exposure limits
    MAX_EVENT_EXPOSURE_PCT = float(os.getenv('MAX_EVENT_EXPOSURE_PCT', 20.0))  # Max % of bankroll in one event
    MAX_CATEGORY_EXPOSURE_PCT = float(os.getenv('MAX_CATEGORY_EXPOSURE_PCT', 40.0))  # Max % in one category (crypto, politics, etc.)
    
    # API Settings
    BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
    RATE_LIMIT_DELAY = 0.3  # Seconds between requests
    
    # Polymarket Configuration
    POLYMARKET_API_KEY = os.getenv('POLYMARKET_API_KEY')
    POLYMARKET_PRIVATE_KEY = os.getenv('POLYMARKET_PRIVATE_KEY')
    POLYMARKET_ENABLED = os.getenv('POLYMARKET_ENABLED', 'true').lower() == 'true'
    # US users can only trade Polymarket via the mobile app — API order signing
    # via private key is not supported for US-based accounts.  Scanners still run
    # in read-only mode (opportunity detection only) when this is False.
    POLYMARKET_EXECUTION_ENABLED = os.getenv('POLYMARKET_EXECUTION_ENABLED', 'false').lower() == 'true'

    # Cross-platform arbitrage
    CROSS_PLATFORM_MIN_PROFIT_PERCENT = float(os.getenv('CROSS_PLATFORM_MIN_PROFIT_PERCENT', 5.0))  # 5% min — covers fees+slippage on both legs
    MATCH_SIMILARITY_THRESHOLD = float(os.getenv('MATCH_SIMILARITY_THRESHOLD', 0.85))
    # Cross-platform category filter (US users: sports,politics only)
    POLYMARKET_CATEGORIES = os.getenv('POLYMARKET_CATEGORIES', 'sports,politics')  # Comma-separated categories
    # Max hours of difference in settlement dates between matched Kalshi/Poly markets
    CROSS_PLATFORM_MAX_EXPIRY_DIFF_HOURS = int(os.getenv('CROSS_PLATFORM_MAX_EXPIRY_DIFF_HOURS', 48))

    # Polymarket Sports Arb
    POLYMARKET_FEE_PERCENT = float(os.getenv('POLYMARKET_FEE_PERCENT', 2.0))  # Polymarket winner fee (2%)
    POLYMARKET_SPORTS_MIN_PROFIT_PERCENT = float(os.getenv('POLYMARKET_SPORTS_MIN_PROFIT_PERCENT', 2.0))  # 2% min after 2% fee — leaves room for execution slippage
    POLYMARKET_SPORTS_SCAN_INTERVAL = int(os.getenv('POLYMARKET_SPORTS_SCAN_INTERVAL', 45))  # Seconds between scans (30-60 recommended)
    POLYMARKET_SPORTS_MAX_POSITION_USD = float(os.getenv('POLYMARKET_SPORTS_MAX_POSITION_USD', 500.0))  # Conservative start — scale up after live validation

    # Probability Trading
    MIN_EDGE_PERCENT = float(os.getenv('MIN_EDGE_PERCENT', 3.0))  # Minimum edge to trade
    KELLY_MULTIPLIER = float(os.getenv('KELLY_MULTIPLIER', 0.5))  # Half-Kelly default (conservative)
    BTC_15MIN_VOL = float(os.getenv('BTC_15MIN_VOL', 0.004))  # BTC 15-min realized vol (0.4%)
    ETH_15MIN_VOL = float(os.getenv('ETH_15MIN_VOL', 0.005))  # ETH 15-min realized vol (0.5%)
    PRICE_CACHE_SECONDS = int(os.getenv('PRICE_CACHE_SECONDS', 10))  # Price feed cache TTL
    FILL_WAIT_TIMEOUT_SECONDS = int(os.getenv('FILL_WAIT_TIMEOUT_SECONDS', 30))
    
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
        print(f"  Max Event Exposure: {cls.MAX_EVENT_EXPOSURE_PCT}%")
        print(f"  Max Category Exposure: {cls.MAX_CATEGORY_EXPOSURE_PCT}%")
        # Do not print private key contents
        print(f"  Private Key Path: {cls.KALSHI_PRIVATE_KEY_PATH or 'not set'}")
        print(f"  Polymarket Enabled: {cls.POLYMARKET_ENABLED}")
        poly_auth = 'API key set' if cls.POLYMARKET_API_KEY else 'read-only (no key)'
        print(f"  Polymarket Auth: {poly_auth}")
        if not cls.POLYMARKET_EXECUTION_ENABLED:
            print(f"  Polymarket Execution: DISABLED (US accounts: app-only, no API trading)")
        else:
            pk_set = 'set' if cls.POLYMARKET_PRIVATE_KEY else 'NOT SET'
            print(f"  Polymarket Execution: ENABLED (private key: {pk_set})")
        print(f"  Cross-platform Min Profit: {cls.CROSS_PLATFORM_MIN_PROFIT_PERCENT}%")
        print(f"  Match Similarity Threshold: {cls.MATCH_SIMILARITY_THRESHOLD}")
        print(f"  Polymarket Categories: {cls.POLYMARKET_CATEGORIES}")
        print(f"  Cross-platform Max Expiry Diff: {cls.CROSS_PLATFORM_MAX_EXPIRY_DIFF_HOURS}h")
        print(f"  Polymarket Fee: {cls.POLYMARKET_FEE_PERCENT}%")
        print(f"  Sports Arb Min Profit: {cls.POLYMARKET_SPORTS_MIN_PROFIT_PERCENT}%")
        print(f"  Sports Arb Scan Interval: {cls.POLYMARKET_SPORTS_SCAN_INTERVAL}s")
        print(f"  Sports Arb Max Position: ${cls.POLYMARKET_SPORTS_MAX_POSITION_USD:.2f}")

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
