"""Deep dive into weather market structure and liquidity."""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from kalshi_bot import KalshiAPI
from config import Config
from datetime import datetime, timezone

bot = KalshiAPI(api_key=Config.KALSHI_API_KEY)
now = datetime.now(timezone.utc)

series_list = [
    'KXHIGHLAX', 'KXHIGHCHI', 'KXHIGHDEN', 'KXHIGHTLV', 
    'KXHIGHPHIL', 'KXLOWTNYC', 'KXLOWTCHI', 'KXRAINLAXM',
]

print(f"UTC: {now.strftime('%Y-%m-%d %H:%M')}\n")

for s in series_list:
    try:
        ms = bot.get_all_markets(status='open', series_ticker=s)
        if not ms:
            continue
    except:
        continue
    
    # Group by event
    events = {}
    for m in ms:
        et = m.get('event_ticker', '')
        events.setdefault(et, []).append(m)
    
    print(f"{'='*70}")
    print(f"SERIES: {s} ({len(ms)} markets, {len(events)} events)")
    print(f"{'='*70}")
    
    for et in sorted(events.keys()):
        brackets = events[et]
        ct = brackets[0].get('close_time', '')
        hrs = -1
        if ct:
            try:
                close = datetime.fromisoformat(ct.replace('Z', '+00:00'))
                hrs = (close - now).total_seconds() / 3600
            except:
                pass
        
        print(f"\n  Event: {et}  ({hrs:.1f}h left)")
        
        for b in sorted(brackets, key=lambda x: x.get('floor_strike') or 0):
            fs = b.get('floor_strike')
            cs = b.get('cap_strike')
            yb = b.get('yes_bid', 0) or 0
            ya = b.get('yes_ask', 0) or 0
            tk = b.get('ticker', '')
            st = b.get('strike_type', '')
            title = b.get('title', '')[:50]
            
            liq = "LIQ" if yb > 0 else "---"
            print(f"    {tk:50s} fs={fs} cs={cs} bid={yb:2d} ask={ya:2d} {liq}  {st}")
        
        # Get orderbook for first bracket with liquidity
        for b in brackets:
            if (b.get('yes_bid', 0) or 0) > 0:
                tk = b.get('ticker', '')
                ob = bot.get_orderbook(tk)
                yes_levels = ob.get('orderbook', {}).get('yes', [])
                no_levels = ob.get('orderbook', {}).get('no', [])
                total_yes_depth = sum(q for p, q in yes_levels) if yes_levels else 0
                total_no_depth = sum(q for p, q in no_levels) if no_levels else 0
                print(f"    >> Orderbook {tk}: YES depth={total_yes_depth} ({len(yes_levels)} levels) NO depth={total_no_depth} ({len(no_levels)} levels)")
                if yes_levels:
                    print(f"       YES: {yes_levels[:5]}")
                if no_levels:
                    print(f"       NO:  {no_levels[:5]}")
                break
    print()

print("\nDone!")
