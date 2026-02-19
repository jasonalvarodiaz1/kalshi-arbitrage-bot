"""Quick weather market discovery — known series from Kalshi Climate category."""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from kalshi_bot import KalshiAPI
from config import Config
from datetime import datetime, timezone

bot = KalshiAPI(api_key=Config.KALSHI_API_KEY)
now = datetime.now(timezone.utc)

# Series found from Kalshi Climate page
weather_series = [
    'KXHIGHLAX', 'KXHIGHCHI', 'KXHIGHNYC', 'KXHIGHDFW', 'KXHIGHDEN',
    'KXHIGHATL', 'KXHIGHDC', 'KXHIGHTLV', 'KXHIGHPHX', 'KXHIGHPHIL',
    'KXLOWTNYC', 'KXLOWTCHI', 'KXLOWTLAX',
    'KXRAINLAXM',
]

found = {}
for series in weather_series:
    try:
        markets = bot.get_all_markets(status='open', series_ticker=series)
        if markets:
            found[series] = markets
    except Exception:
        pass

print(f"Found {len(found)} weather series\n")

for series in sorted(found.keys()):
    ms = found[series]
    # Group by event
    events = {}
    for m in ms:
        et = m.get('event_ticker', '')
        events.setdefault(et, []).append(m)
    
    print(f"=== {series} ({len(ms)} markets, {len(events)} events) ===")
    
    for et in sorted(events.keys()):
        brackets = events[et]
        sample = brackets[0]
        ct = sample.get('close_time', '')
        hrs = -1
        if ct:
            try:
                close = datetime.fromisoformat(ct.replace('Z', '+00:00'))
                hrs = (close - now).total_seconds() / 3600
            except:
                pass
        
        print(f"  Event: {et} ({len(brackets)} brackets, {hrs:.1f}h left)")
        print(f"  Title: {sample.get('title', '')[:70]}")
        
        # Show each bracket with liquidity
        for b in sorted(brackets, key=lambda x: x.get('floor_strike') or 0):
            fs = b.get('floor_strike')
            cs = b.get('cap_strike')
            yb = b.get('yes_bid', 0) or 0
            ya = b.get('yes_ask', 0) or 0
            tk = b.get('ticker', '')
            strike_type = b.get('strike_type', '')
            
            bracket_str = f"{fs}-{cs}" if fs and cs else f">{fs}" if fs else f"<{cs}"
            liq = "HAS LIQ" if yb > 0 and ya > 0 else "NO LIQ"
            print(f"    {tk:40s} [{bracket_str:12s}] bid={yb:3d} ask={ya:3d} {liq} type={strike_type}")
        print()

# Detailed orderbook for one event
if found:
    first_series = list(found.keys())[0]
    first_event_markets = list(found.values())[0]
    sample_ticker = first_event_markets[0].get('ticker', '')
    if sample_ticker:
        print(f"\n=== ORDERBOOK SAMPLE: {sample_ticker} ===")
        ob = bot.get_orderbook(sample_ticker)
        print(json.dumps(ob, indent=2)[:500])
