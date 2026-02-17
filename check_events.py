"""Quick check of available events."""
from kalshi_bot import KalshiAPI
from config import Config
from datetime import datetime, timezone

bot = KalshiAPI(api_key=Config.KALSHI_API_KEY)
now = datetime.now(timezone.utc)
print(f"UTC: {now.strftime('%H:%M')}")

for series in ['KXBTC', 'KXETH']:
    markets = bot.get_all_markets(status='open', series_ticker=series)
    events = {}
    for m in markets:
        et = m.get('event_ticker', '')
        events.setdefault(et, []).append(m)
    for ev in sorted(events.keys()):
        ct = events[ev][0].get('close_time', '')
        try:
            close = datetime.fromisoformat(ct.replace('Z', '+00:00'))
            mins = (close - now).total_seconds() / 60
        except:
            mins = -1
        print(f"  {ev}: {len(events[ev])} brackets, {mins:.0f} min left")
