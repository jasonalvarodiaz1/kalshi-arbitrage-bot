"""Deep P&L analysis - match every fill to its settlement."""
import sys, os, json
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(__file__))
from kalshi_bot import KalshiAPI
from config import Config
from urllib.parse import urlparse

bot = KalshiAPI(api_key=Config.KALSHI_API_KEY)

# Get balance (check raw value)
bal_raw = bot.get_balance()
print(f"Raw balance value from API: {bal_raw}")
print(f"If cents: ${bal_raw/100:.2f}")
print(f"If dollars: ${bal_raw:.2f}")
print()

# Get fills
path_prefix = urlparse(bot.BASE_URL).path.rstrip('/')
endpoint = f'{bot.BASE_URL}/portfolio/fills'
path = f'{path_prefix}/portfolio/fills'
headers = bot._signed_headers('GET', path)
resp = bot._request_with_retry('GET', endpoint, params={'limit': 200}, headers=headers)
fills = resp.json().get('fills', []) if resp and resp.status_code == 200 else []

# Get settlements
endpoint2 = f'{bot.BASE_URL}/portfolio/settlements'
path2 = f'{path_prefix}/portfolio/settlements'
headers2 = bot._signed_headers('GET', path2)
resp2 = bot._request_with_retry('GET', endpoint2, params={'limit': 200}, headers=headers2)
settlements = resp2.json().get('settlements', []) if resp2 and resp2.status_code == 200 else []

# Build settlement lookup: ticker -> revenue
sett_map = {}
for s in settlements:
    ticker = s.get('market_ticker', s.get('ticker', ''))
    rev = s.get('revenue', 0)
    sett_map[ticker] = rev

# Group fills by ticker
from collections import defaultdict
ticker_fills = defaultdict(list)
for f in fills:
    ticker_fills[f['ticker']].append(f)

# Calculate P&L per ticker
print("=" * 80)
print("TRADE-BY-TRADE P&L (all values in cents, display as dollars)")
print("=" * 80)

total_cost = 0
total_revenue = 0
total_fees = 0
total_wins = 0
total_losses = 0
total_pending = 0

# Categorize
bracket_pnl = 0
bracket_cost = 0
binary_pnl = 0
binary_cost = 0

results = []

for ticker in sorted(ticker_fills.keys()):
    ff = ticker_fills[ticker]
    side = ff[0]['side']
    
    cost = 0
    contracts = 0
    fees = 0
    for f in ff:
        price = f['yes_price'] if f['side'] == 'yes' else f['no_price']
        cnt = f['count']
        fee = f.get('fee', 0)
        cost += price * cnt
        fees += fee
        contracts += cnt
    
    total_cost += cost
    total_fees += fees
    
    revenue = sett_map.get(ticker)
    is_binary = '15M' in ticker or '-T' in ticker
    mtype = "BIN" if is_binary else "BKT"
    
    if revenue is not None:
        pnl = revenue - cost - fees
        total_revenue += revenue
        
        if pnl >= 0:
            total_wins += 1
            status = "WIN"
        else:
            total_losses += 1
            status = "LOSS"
        
        if is_binary:
            binary_pnl += pnl
            binary_cost += cost
        else:
            bracket_pnl += pnl
            bracket_cost += cost
            
        results.append((ticker, mtype, side.upper(), contracts, cost, fees, revenue, pnl, status))
    else:
        total_pending += 1
        results.append((ticker, mtype, side.upper(), contracts, cost, fees, None, None, "PENDING"))

# Print sorted by event time
for r in results:
    ticker, mtype, side, contracts, cost, fees, revenue, pnl, status = r
    if revenue is not None:
        print(f"  [{mtype}] {ticker}: {side} x{contracts} cost=${cost/100:.2f} fee=${fees/100:.2f} rev=${revenue/100:.2f} PnL=${pnl/100:+.2f} [{status}]")
    else:
        print(f"  [{mtype}] {ticker}: {side} x{contracts} cost=${cost/100:.2f} fee=${fees/100:.2f} [PENDING]")

print()
print("=" * 80)
print("SUMMARY")
print("=" * 80)
print(f"Total fills: {len(fills)}")
print(f"Unique tickers traded: {len(ticker_fills)}")
print(f"Total cost: ${total_cost/100:.2f}")
print(f"Total fees: ${total_fees/100:.2f}")
print(f"Total revenue: ${total_revenue/100:.2f}")
print(f"Wins: {total_wins}, Losses: {total_losses}, Pending: {total_pending}")
print(f"Net P&L (settled): ${(total_revenue - total_cost - total_fees)/100:.2f}")
print()
print(f"Bracket: cost=${bracket_cost/100:.2f}, PnL=${bracket_pnl/100:+.2f}")
print(f"Binary:  cost=${binary_cost/100:.2f}, PnL=${binary_pnl/100:+.2f}")

# Find the problematic patterns
print()
print("=" * 80)
print("PROBLEM DETECTION")
print("=" * 80)

# 1. Adjacent brackets in same event
events = defaultdict(list)
for ticker in ticker_fills:
    parts = ticker.split('-')
    if len(parts) >= 3:
        event = '-'.join(parts[:2])
        events[event].append(ticker)

print("\n--- Adjacent Bracket Pairs (same event) ---")
for event, tickers in sorted(events.items()):
    brackets = [t for t in tickers if '-B' in t]
    if len(brackets) > 1:
        # Extract bracket values
        bracket_vals = []
        for b in brackets:
            val_str = b.split('-B')[-1]
            try:
                val = float(val_str)
                bracket_vals.append((val, b))
            except:
                pass
        bracket_vals.sort()
        for i in range(len(bracket_vals) - 1):
            v1, t1 = bracket_vals[i]
            v2, t2 = bracket_vals[i+1]
            gap = v2 - v1
            side1 = ticker_fills[t1][0]['side']
            side2 = ticker_fills[t2][0]['side']
            
            r1 = sett_map.get(t1)
            r2 = sett_map.get(t2)
            c1 = sum(f['no_price' if f['side']=='no' else 'yes_price'] * f['count'] for f in ticker_fills[t1])
            c2 = sum(f['no_price' if f['side']=='no' else 'yes_price'] * f['count'] for f in ticker_fills[t2])
            
            if side1 == side2 == 'no':
                if r1 is not None and r2 is not None:
                    pnl1 = r1 - c1
                    pnl2 = r2 - c2
                    net = pnl1 + pnl2
                    marker = " *** ADJACENT NO PAIR ***" if gap < 300 else ""
                    print(f"  {event}: {t1}({side1} c=${c1/100:.2f} r=${r1/100:.2f} pnl=${pnl1/100:+.2f}) + {t2}({side2} c=${c2/100:.2f} r=${r2/100:.2f} pnl=${pnl2/100:+.2f}) NET=${net/100:+.2f}{marker}")

# 2. Binary trade analysis  
print("\n--- Binary 15M Trades ---")
for ticker in sorted(ticker_fills.keys()):
    if '15M' not in ticker:
        continue
    ff = ticker_fills[ticker]
    side = ff[0]['side']
    contracts = sum(f['count'] for f in ff)
    cost = sum((f['yes_price'] if f['side']=='yes' else f['no_price']) * f['count'] for f in ff)
    avg = cost / contracts if contracts > 0 else 0
    rev = sett_map.get(ticker)
    pnl = (rev - cost) if rev is not None else None
    status = f"PnL=${pnl/100:+.2f}" if pnl is not None else "PENDING"
    print(f"  {ticker}: {side.upper()} x{contracts} avg@{avg:.0f}c cost=${cost/100:.2f} {status}")

# 3. Volume analysis
print("\n--- Trade Volume Per Hour ---")
hourly = defaultdict(int)
for f in fills:
    ct = f.get('created_time', '')[:13]  # YYYY-MM-DDTHH
    hourly[ct] += f['count']
for h in sorted(hourly.keys()):
    print(f"  {h}: {hourly[h]} contracts")
