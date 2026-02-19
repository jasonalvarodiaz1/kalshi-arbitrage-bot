"""Discover weather markets on Kalshi."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from kalshi_bot import KalshiAPI
from config import Config

bot = KalshiAPI(api_key=Config.KALSHI_API_KEY)

# Search for weather-related markets
markets = bot.get_all_markets(status='open')
weather_kw = ['weather', 'temp', 'temperature', 'high', 'HIGHNY', 'HIGHCHI',
              'HIGHDFW', 'HIGHLAX', 'LOWNY', 'RAIN', 'SNOW', 'COLD', 'WARM',
              'FREEZE', 'DEW', 'WIND', 'HUMID', 'HEAT', 'PRECIP']
weather_markets = []
series_seen = set()
event_seen = set()

for m in markets:
    ticker = m.get('ticker', '')
    title = m.get('title', '').lower()
    st = m.get('series_ticker', '')
    et = m.get('event_ticker', '')
    cat = m.get('category', '').lower()
    
    is_weather = any(kw.lower() in ticker.lower() or kw.lower() in title 
                     or kw.lower() in st.lower() or kw.lower() in cat
                     for kw in weather_kw)
    
    if is_weather and et not in event_seen:
        event_seen.add(et)
        weather_markets.append(m)

print(f"Found {len(weather_markets)} unique weather events out of {len(markets)} total markets")
print()

# Group by series
from collections import defaultdict
by_series = defaultdict(list)
for m in weather_markets:
    by_series[m.get('series_ticker', '')].append(m)

for series in sorted(by_series.keys()):
    ms = by_series[series]
    sample = ms[0]
    print(f"Series: {series} ({len(ms)} events)")
    print(f"  Title: {sample.get('title', '')[:80]}")
    print(f"  Category: {sample.get('category', '')}")
    print(f"  Sample ticker: {sample.get('ticker', '')}")
    print(f"  Sample event: {sample.get('event_ticker', '')}")
    print(f"  Close: {sample.get('close_time', '')}")
    print(f"  Floor: {sample.get('floor_strike')} | Cap: {sample.get('cap_strike')}")
    print(f"  Yes bid/ask: {sample.get('yes_bid')}/{sample.get('yes_ask')}")
    print(f"  Strike type: {sample.get('strike_type', '')}")
    print(f"  Custom strike: {sample.get('custom_strike', {})}")
    print()

# Also look for ALL series with "temp" or city names in them
print("=" * 60)
print("ALL SERIES IN DATASET:")
all_series = defaultdict(int)
for m in markets:
    all_series[m.get('series_ticker', '')] += 1

# Look for non-crypto, non-politics series that might be weather
for s in sorted(all_series.keys()):
    count = all_series[s]
    if any(kw in s.upper() for kw in ['HIGH', 'LOW', 'TEMP', 'RAIN', 'SNOW', 'DEW', 'WIND', 'COLD', 'WARM', 'HEAT', 'PRECIP', 'FREEZE']):
        print(f"  {s}: {count} markets")
