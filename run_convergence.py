"""
Launch the convergence trader for near-expiry crypto bracket markets.

This strategy focuses on bracket markets expiring within 60 minutes where
the outcome is becoming clear. When BTC/ETH is deeply inside or outside
a bracket with little time remaining, stale orders can be exploited.
"""
import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['PYTHONIOENCODING'] = 'utf-8'

from config import Config
from kalshi_bot import KalshiAPI
from convergence_trader import ConvergenceTrader

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('convergence.log', encoding='utf-8')
    ]
)
logger = logging.getLogger('kalshi_bot')

def main():
    print("=" * 60)
    print("CONVERGENCE TRADER — Near-Expiry Bracket Strategy")
    print("=" * 60)
    
    # Show config
    mode = "PAPER" if Config.PAPER_TRADING else ("LIVE" if Config.LIVE_TRADING_ENABLED else "DRY-RUN")
    print(f"Mode: {mode}")
    print(f"Max trade: ${Config.MAX_TRADE_USD:.2f}")
    print(f"Kelly mult: {Config.KELLY_MULTIPLIER}x")
    print()
    
    # Auth
    api = KalshiAPI(api_key=Config.KALSHI_API_KEY)
    
    try:
        balance = api.get_balance()
        print(f"Balance: ${balance:.2f}")
    except Exception as e:
        print(f"Warning: could not fetch balance: {e}")
    
    print()
    
    # Start trader
    trader = ConvergenceTrader(api)
    
    scan_interval = int(os.getenv('SCAN_INTERVAL_SECONDS', '15'))
    trader.auto_trade_loop(interval_seconds=scan_interval)


if __name__ == '__main__':
    main()
