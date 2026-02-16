import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from kalshi_bot import KalshiAPI

api = KalshiAPI(api_key=Config.KALSHI_API_KEY)

markets, _ = api.get_markets(status='open', limit=200)
print(f"Fetched {len(markets)} markets")
found = 0
for m in markets:
    ticker = m['ticker']
    ob = api.get_orderbook(ticker)
    ya = ob.get('yes_asks', [])
    na = ob.get('no_asks', [])
    if ya and na:
        found += 1
        best_yes = min([a[0] for a in ya])
        best_no = min([a[0] for a in na])
        total = best_yes + best_no
        profit = 100 - total
        pct = (profit / total * 100) if total > 0 else 0
        print(f"{ticker}: YES={best_yes}c NO={best_no}c total={total}c profit={profit}c ({pct:.2f}%)")
        print(f"  yes depth: {ya[:3]}")
        print(f"  no depth:  {na[:3]}")
        if found >= 5:
            break

if found == 0:
    for m in markets[:10]:
        ticker = m['ticker']
        ob = api.get_orderbook(ticker)
        ya = ob.get('yes_asks', [])
        na = ob.get('no_asks', [])
        print(f"{ticker}: yes={len(ya)} levels, no={len(na)} levels")
