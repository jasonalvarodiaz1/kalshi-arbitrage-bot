import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from kalshi_bot import KalshiAPI
import time

api = KalshiAPI(api_key=Config.KALSHI_API_KEY)

all_markets = api.get_all_markets(status='open')
both = 0
arbs = 0
for m in all_markets:
    ob = api.get_orderbook(m['ticker'])
    ya = ob.get('yes_asks', [])
    na = ob.get('no_asks', [])
    if ya and na:
        both += 1
        best_yes = min(a[0] for a in ya)
        best_no = min(a[0] for a in na)
        total = best_yes + best_no
        if total < 100:
            profit = 100 - total
            pct = (profit / total * 100)
            ticker = m.get('ticker')
            title = m.get('title', '')[:60]
            print(f"ARB: {ticker} | {title} | YES={best_yes} NO={best_no} total={total} profit={profit}c ({pct:.2f}%)")
            arbs += 1
    if both >= 30:
        break
    time.sleep(0.1)

print(f"\nScanned until found {both} markets with both sides")
print(f"Arbitrage opportunities found: {arbs}")
