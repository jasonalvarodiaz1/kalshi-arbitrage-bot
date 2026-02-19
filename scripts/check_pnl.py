"""Full P&L analysis of all trades."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

from kalshi_bot import KalshiAPI
from config import Config
from datetime import datetime, timezone

bot = KalshiAPI(api_key=Config.KALSHI_API_KEY)

# Balance
bal = bot.get_balance()
print(f"=== CURRENT BALANCE: ${bal/100:.2f} ===")
print(f"=== STARTED WITH: $250.00 ===")
print(f"=== TOTAL P&L: ${(bal/100 - 250):.2f} ===")
print()

# Get all fills
fills = bot.get_fills(limit=200)
print(f"Total fills: {len(fills)}")
print()

# Get settlements
try:
    settlements = bot.get_settlements(limit=200)
except:
    settlements = []
print(f"Total settlements: {len(settlements)}")
print()

# Show each fill
print("=== ALL FILLS (chronological) ===")
fills_sorted = sorted(fills, key=lambda f: f.get('created_time', ''))
for f in fills_sorted:
    t = f.get('ticker', '')
    side = f['side']
    price = f['yes_price'] if side == 'yes' else f['no_price']
    cnt = f['count']
    ct = f.get('created_time', '')[:19]
    fee = f.get('fee', 0)
    is_binary = '15M' in t or '-T' in t
    mtype = "BINARY" if is_binary else "BRACKET"
    print(f"  {ct} [{mtype}] {t} {side.upper()} x{cnt} @{price}c fee={fee}c")

print()

# Show settlements
print("=== ALL SETTLEMENTS ===")
if settlements:
    for s in sorted(settlements, key=lambda x: x.get('settled_time', x.get('created_time', ''))):
        ticker = s.get('market_ticker', s.get('ticker', ''))
        result = s.get('result', s.get('settlement_result', '?'))
        revenue = s.get('revenue', 0)
        st = s.get('settled_time', s.get('created_time', ''))[:19]
        print(f"  {st} {ticker} result={result} revenue=${revenue/100:.2f}")
else:
    print("  (No settlements endpoint or empty)")

# P&L by type
print()
print("=== P&L SUMMARY BY TYPE ===")
binary_cost = 0
binary_count = 0
bracket_cost = 0
bracket_count = 0
total_fees = 0

for f in fills:
    t = f.get('ticker', '')
    side = f['side']
    price = f['yes_price'] if side == 'yes' else f['no_price']
    cnt = f['count']
    fee = f.get('fee', 0)
    cost = price * cnt + fee
    total_fees += fee
    
    is_binary = '15M' in t or '-T' in t
    if is_binary:
        binary_cost += cost
        binary_count += cnt
    else:
        bracket_cost += cost
        bracket_count += cnt

print(f"Binary trades: {binary_count} contracts, cost=${binary_cost/100:.2f}")
print(f"Bracket trades: {bracket_count} contracts, cost=${bracket_cost/100:.2f}")
print(f"Total fees: ${total_fees/100:.2f}")
print(f"Total spent: ${(binary_cost + bracket_cost)/100:.2f}")

# Check open positions
print()
print("=== OPEN POSITIONS ===")
try:
    positions = bot.get_positions()
    if positions:
        for p in positions:
            ticker = p.get('market_ticker', p.get('ticker', ''))
            side = 'YES' if p.get('position', 0) > 0 else 'NO'
            qty = abs(p.get('position', 0))
            if qty > 0:
                print(f"  {ticker} {side} x{qty}")
    else:
        print("  No open positions")
except Exception as e:
    print(f"  Error getting positions: {e}")

# Check for resting orders
print()
print("=== RESTING ORDERS ===")
try:
    orders = bot.get_orders(status='resting')
    if orders:
        for o in orders:
            print(f"  {o.get('ticker','')} {o.get('side','')} x{o.get('count',0)} @{o.get('yes_price', o.get('no_price','?'))}c")
    else:
        print("  No resting orders")
except Exception as e:
    print(f"  Error: {e}")
