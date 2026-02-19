"""Auto-trade crypto probability strategy - Option 10 directly."""
import sys
sys.path.insert(0, '.')

from config import Config
from kalshi_bot import KalshiAPI
from probability_trader import ProbabilityTrader
from storage import Storage

# Initialize
print("\n" + "="*60)
print("🤖 CRYPTO AUTO-TRADER STARTING")
print("="*60)
print(f"Max trade: ${Config.MAX_TRADE_USD}")
print(f"Min edge: {Config.MIN_EDGE_PERCENT}%")
print(f"Kelly multiplier: {Config.KELLY_MULTIPLIER}")
print(f"Live trading: {Config.LIVE_TRADING_ENABLED}")
print(f"Paper trading: {Config.PAPER_TRADING}")
print("="*60 + "\n")

api = KalshiAPI(api_key=Config.KALSHI_API_KEY)
trader = ProbabilityTrader(api)

try:
    storage = Storage()
except Exception as e:
    print(f"Storage initialization failed: {e}")
    storage = None

# Run auto-trading loop
print("Starting continuous auto-trading loop...")
print("Press Ctrl+C to stop.\n")

try:
    trader.auto_trade_loop(interval_seconds=Config.SCAN_INTERVAL_SECONDS)
except KeyboardInterrupt:
    print("\n\n🛑 Auto-trader stopped by user")
    print("Final summary will be displayed if available...")
except Exception as e:
    print(f"\n\n❌ Error in auto-trader: {e}")
    import traceback
    traceback.print_exc()
