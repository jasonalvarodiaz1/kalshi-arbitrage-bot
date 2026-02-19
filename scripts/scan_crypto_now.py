"""Quick crypto probability scan - runs option 9 directly."""
import sys
sys.path.insert(0, '.')

from config import Config
from kalshi_bot import KalshiAPI
from probability_trader import ProbabilityTrader

# Initialize
api = KalshiAPI(api_key=Config.KALSHI_API_KEY)
trader = ProbabilityTrader(api)

# Run scan
print("\n" + "="*60)
print("📊 CRYPTO PROBABILITY SCAN")
print("="*60)
print(f"Min edge: {trader.min_edge_percent}%")
print(f"BTC vol: {trader.vol_estimates['BTC']*100:.2f}%")
print(f"ETH vol: {trader.vol_estimates['ETH']*100:.2f}%")
print(f"Max trade: ${Config.MAX_TRADE_USD}")
print("="*60 + "\n")

opportunities = trader.scan_crypto_markets()

if opportunities:
    print(f"\n✅ Found {len(opportunities)} opportunities:\n")
    for opp in opportunities:
        print(f"  {opp['ticker']}")
        print(f"    Edge: {opp['edge_percent']:.2f}%")
        print(f"    Price: {opp['contract_price']}¢")
        print(f"    Est prob: {opp['estimated_prob']*100:.1f}%")
        print(f"    Kelly qty: {opp['kelly_quantity']} contracts (${opp['kelly_quantity'] * opp['contract_price'] / 100:.2f})")
        print(f"    Time left: {opp['minutes_remaining']:.0f} min")
        print()
else:
    print("\n❌ No opportunities found with edge >= 3.0%\n")
