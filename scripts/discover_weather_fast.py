"""Quick weather market discovery — search by known series tickers."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from kalshi_bot import KalshiAPI
from config import Config
from datetime import datetime, timezone

bot = KalshiAPI(api_key=Config.KALSHI_API_KEY)
now = datetime.now(timezone.utc)

# Known Kalshi weather series tickers
weather_series = [
    'HIGHNY', 'LOWNY', 'HIGHCHI', 'LOWCHI',
    'HIGHDFW', 'LOWDFW', 'HIGHLAX', 'LOWLAX',
    'HIGHDEN', 'LOWDEN', 'HIGHATL', 'LOWATL',
    'HIGHDC', 'LOWDC', 'HIGHBOS', 'LOWBOS',
    'HIGHMIA', 'LOWMIA', 'HIGHSEA', 'LOWSEA',
    'HIGHHOU', 'LOWHOU', 'HIGHPHX', 'LOWPHX',
    'HIGHSFO', 'LOWSFO', 'HIGHORD', 'LOWORD',
    'TEMPNY', 'TEMPCHI', 'TEMPDFW', 'TEMPLAX',
    'RAINNY', 'RAINCHI', 'RAINDFW', 'RAINLAX',
    'SNOWNY', 'SNOWCHI', 'SNOWDFW',
    'KXHIGHNY', 'KXLOWNY', 'KXTEMPNY',
]

found = {}
for series in weather_series:
    try:
        markets = bot.get_all_markets(status='open', series_ticker=series)
        if markets:
            found[series] = markets
            sample = markets[0]
            print(f"[OK] {series}: {len(markets)} markets")
            print(f"     Title: {sample.get('title', '')[:80]}")
            ct = sample.get('close_time', '')
            if ct:
                try:
                    close = datetime.fromisoformat(ct.replace('Z', '+00:00'))
                    hrs = (close - now).total_seconds() / 3600
                    print(f"     Closes in: {hrs:.1f} hours")
                except:
                    pass
            print(f"     Floor: {sample.get('floor_strike')} Cap: {sample.get('cap_strike')}")
            print(f"     Bid/Ask: {sample.get('yes_bid')}/{sample.get('yes_ask')}")
            print()
    except Exception as e:
        pass  # not found

if not found:
    print("No weather series found with known tickers.")
    print("\nTrying broader search with event_ticker prefix...")
    # Try fetching a page of all markets and filter
    markets, _ = bot.get_markets(status='open', limit=200)
    weather = [m for m in markets if any(kw in m.get('series_ticker', '').upper() 
               for kw in ['HIGH', 'LOW', 'TEMP', 'RAIN', 'SNOW', 'WEATHER'])]
    if weather:
        seen = set()
        for m in weather:
            st = m.get('series_ticker', '')
            if st not in seen:
                seen.add(st)
                print(f"  {st}: {m.get('title', '')[:60]}")
    else:
        print("  No weather markets found in first 200 markets.")
        print("  Searching across all markets by title keywords...")
else:
    print(f"\nTotal: {len(found)} weather series found with {sum(len(v) for v in found.values())} total markets")
    
    # Show some orderbook depth for each
    print("\n=== LIQUIDITY CHECK ===")
    for series, ms in found.items():
        total_bid_depth = 0
        total_ask_gap = 0
        with_liq = 0
        for m in ms:
            yb = m.get('yes_bid', 0) or 0
            ya = m.get('yes_ask', 0) or 0
            if yb > 0 and ya > 0:
                with_liq += 1
                total_ask_gap += (ya - yb)
        print(f"  {series}: {with_liq}/{len(ms)} markets have bid+ask, avg spread: {total_ask_gap/max(1,with_liq):.0f}c")
