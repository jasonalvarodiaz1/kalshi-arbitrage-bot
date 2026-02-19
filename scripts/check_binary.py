"""Check if hourly KXBTC fetch returns 15M markets, and debug binary detection."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kalshi_bot import KalshiAPI
from config import Config

bot = KalshiAPI(api_key=Config.KALSHI_API_KEY)

# Check if KXBTC hourly fetch returns any 15M tickers
btc = bot.get_all_markets(status='open', series_ticker='KXBTC')
btc15 = [m for m in btc if '15M' in m.get('ticker','')]
print(f'KXBTC returned {len(btc)} markets, {len(btc15)} have 15M in ticker')
for m in btc15[:5]:
    print(f"  {m.get('ticker')}: floor={m.get('floor_strike')}, cap={m.get('cap_strike')}, type={m.get('strike_type')}")

# Check hourly brackets for terminal markets that look like binaries
binlike = [m for m in btc if m.get('cap_strike') is None and m.get('floor_strike')]
print(f'\nHourly with no cap_strike (bin-like): {len(binlike)}')
for m in binlike[:3]:
    print(f"  {m.get('ticker')}: floor={m.get('floor_strike')}, type={m.get('strike_type')}")

# Check KXBTC markets that have NO floor AND no cap
nostrikes = [m for m in btc if not m.get('floor_strike') and not m.get('cap_strike')]
print(f'\nHourly with NEITHER strike: {len(nostrikes)}')
for m in nostrikes[:3]:
    print(f"  {m.get('ticker')}: floor={m.get('floor_strike')!r}, cap={m.get('cap_strike')!r}, type={m.get('strike_type')!r}")

# Check 15M binary markets detection
print('\n--- 15M Binary Detection ---')
for series in ['KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXXRP15M']:
    markets = bot.get_all_markets(status='open', series_ticker=series)
    for m in markets[:1]:
        cap = m.get('cap_strike')
        floor = m.get('floor_strike')
        st = m.get('strike_type', '')
        is_bin = (cap is None and floor is not None and st.startswith('greater'))
        print(f"  {m.get('ticker')}: floor={floor}, cap={cap!r}, strike_type={st!r}")
        print(f"    cap is None: {cap is None}, floor is not None: {floor is not None}, startswith greater: {st.startswith('greater')}")
        print(f"    => is_binary_updown: {is_bin}")
