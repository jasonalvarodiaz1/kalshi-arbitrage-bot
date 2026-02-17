"""
Reconstruct what overnight paper trades would have settled at.
Uses CoinGecko historical prices since Kalshi removes settled market data.

We know from the overnight log:
- Bot traded on events from KXBTC/KXETH-26FEB1623 through -26FEB1707
- Trade types: mostly NO bets on far-away brackets, some YES on ATM brackets
- Prices logged at each scan: BTC ~$68,400-68,800, ETH ~$1,985-1,998
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import requests
import time
from datetime import datetime, timezone

# CoinGecko hourly price history (last 48h gives us hourly candles)
def get_hourly_prices(coin_id):
    """Get hourly OHLC from CoinGecko for last 2 days."""
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {'vs_currency': 'usd', 'days': 2}
    r = requests.get(url, params=params, timeout=10)
    if r.status_code == 200:
        data = r.json()
        prices = data.get('prices', [])
        return [(datetime.fromtimestamp(p[0]/1000, tz=timezone.utc), p[1]) for p in prices]
    print(f"CoinGecko error for {coin_id}: {r.status_code}")
    return []

print("Fetching BTC hourly prices...")
btc_prices = get_hourly_prices('bitcoin')
time.sleep(1)
print("Fetching ETH hourly prices...")
eth_prices = get_hourly_prices('ethereum')

def find_price_at(prices, target_utc_hour):
    """Find the price closest to a specific UTC hour."""
    best = None
    best_diff = float('inf')
    for ts, price in prices:
        diff = abs((ts - target_utc_hour).total_seconds())
        if diff < best_diff:
            best_diff = diff
            best = price
    return best

# The events that ran overnight (UTC times)
# KXBTC-26FEB1623 = close at 2026-02-17 04:00 UTC (11 PM EST)
# KXBTC-26FEB1700 = close at 2026-02-17 05:00 UTC (midnight EST) 
# ... through KXBTC-26FEB1707 = close at 2026-02-17 12:00 UTC (7 AM EST)
events = []
for hour in range(4, 13):  # UTC 04:00 through 12:00
    utc_close = datetime(2026, 2, 17, hour, 0, 0, tzinfo=timezone.utc)
    est_hour = hour - 5  # EST = UTC-5
    if est_hour < 0:
        est_hour += 24
        est_date = "Feb 16"
    else:
        est_date = "Feb 17"
    
    # Format event ticker
    if est_hour >= 0 and est_date == "Feb 16":
        btc_event = f"KXBTC-26FEB16{est_hour:02d}"
        eth_event = f"KXETH-26FEB16{est_hour:02d}"
    else:
        btc_event = f"KXBTC-26FEB17{est_hour:02d}"
        eth_event = f"KXETH-26FEB17{est_hour:02d}"
    
    btc_price = find_price_at(btc_prices, utc_close)
    eth_price = find_price_at(eth_prices, utc_close)
    
    events.append({
        'btc_event': btc_event,
        'eth_event': eth_event,
        'utc_close': utc_close,
        'est_time': f"{est_date} {est_hour}:00",
        'btc_price': btc_price,
        'eth_price': eth_price,
    })

print(f"\n{'Event':<22} {'EST Close':<16} {'BTC Settle':>12} {'ETH Settle':>12}")
print("-" * 65)
for ev in events:
    btc_str = f"${ev['btc_price']:,.0f}" if ev['btc_price'] else "N/A"
    eth_str = f"${ev['eth_price']:,.0f}" if ev['eth_price'] else "N/A"
    print(f"{ev['btc_event']:<22} {ev['est_time']:<16} {btc_str:>12} {eth_str:>12}")

# Now simulate what the bot's NO trades on far-away brackets would have done
# From the log we saw the bot buying NO on brackets like:
# B68625 NO (68500-68750), B69625 NO (69500-69750), B69125 NO (69000-69250)
# B68375 NO (68250-68500), B69375 NO (69250-69500), B61375 NO (61250-61500)
# B2020 NO (2010-2030), B2000 YES (1990-2010), B1980 NO (1970-1990)
# B2000 NO (1990-2010), B68875 YES (68750-69000)

print(f"\n\n{'='*65}")
print("SIMULATED SETTLEMENT for trades we saw in the log")
print(f"{'='*65}")
print("\nNote: We can only simulate trades from the KXBTC/KXETH-26FEB1623 event")
print("because that's what we have detailed trade data for from the log.\n")

# Known trades from the log (first event only — KXBTC-26FEB1623)
# These were captured before the log was cleared
known_trades = [
    # (ticker, side, qty, price_cents, floor, cap, asset)
    ('KXBTC-26FEB1623-B68875', 'yes', 50, 6, 68750, 69000, 'BTC'),
    ('KXETH-26FEB1623-B1980', 'no', 33, 60, 1970, 1990, 'ETH'),
    ('KXBTC-26FEB1623-B68625', 'no', 12, 4, 68500, 68750, 'BTC'),
    ('KXBTC-26FEB1623-B69625', 'no', 12, 4, 69500, 69750, 'BTC'),
    ('KXETH-26FEB1623-B2000', 'yes', 50, 15, 1990, 2010, 'ETH'),
    ('KXBTC-26FEB1623-B68875', 'no', 12, 4, 68750, 69000, 'BTC'),
    ('KXBTC-26FEB1623-B69125', 'no', 12, 4, 69000, 69250, 'BTC'),
    ('KXBTC-26FEB1623-B68375', 'no', 12, 4, 68250, 68500, 'BTC'),
    ('KXBTC-26FEB1623-B69375', 'no', 12, 4, 69250, 69500, 'BTC'),
    ('KXBTC-26FEB1623-B61375', 'no', 12, 4, 61250, 61500, 'BTC'),
    ('KXETH-26FEB1623-B2020', 'no', 28, 70, 2010, 2030, 'ETH'),
    ('KXETH-26FEB1623-B2000', 'no', 50, 19, 1990, 2010, 'ETH'),
]

# Settlement price for the 2300 EST event (UTC 04:00)
settle_btc = events[0]['btc_price']
settle_eth = events[0]['eth_price']

total_cost = 0
total_payout = 0
wins = 0
losses = 0

print(f"Settlement prices: BTC=${settle_btc:,.0f}  ETH=${settle_eth:,.0f}\n")
print(f"{'Ticker':<28} {'Side':>4} {'Qty':>4} {'Price':>5} {'Cost':>7} {'Result':>6} {'Payout':>7} {'P&L':>7}")
print("-" * 80)

for ticker, side, qty, price, floor, cap, asset in known_trades:
    settle = settle_btc if asset == 'BTC' else settle_eth
    cost = qty * price / 100
    
    in_bracket = floor <= settle < cap
    if side == 'yes':
        won = in_bracket
    else:
        won = not in_bracket
    
    payout = qty * 1.0 if won else 0.0
    pnl = payout - cost
    
    total_cost += cost
    total_payout += payout
    if won:
        wins += 1
    else:
        losses += 1
    
    result = "WIN" if won else "LOSS"
    print(f"{ticker:<28} {side:>4} {qty:>4} {price:>4}c ${cost:>6.2f} {result:>6} ${payout:>6.2f} ${pnl:>+6.2f}")

total_pnl = total_payout - total_cost
win_rate = wins / (wins + losses) * 100
print("-" * 80)
print(f"{'TOTAL':<28} {'':>4} {'':>4} {'':>5} ${total_cost:>6.2f} {wins}/{wins+losses}  ${total_payout:>6.2f} ${total_pnl:>+6.2f}")
print(f"\nWin rate: {win_rate:.0f}%  |  P&L: ${total_pnl:+.2f}  |  ROI: {total_pnl/total_cost*100:+.1f}%")
