"""Analyze all trades, settlements, and P&L."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sys, json
sys.stdout.reconfigure(encoding='utf-8')

from kalshi_bot import KalshiAPI
from config import Config
from urllib.parse import urlparse
import requests

bot = KalshiAPI(api_key=Config.KALSHI_API_KEY)
base = bot.BASE_URL
path_prefix = urlparse(base).path.rstrip('/')

balance = bot.get_balance()
print(f"Current balance: ${balance:.2f}")
print()

# ── Get all fills ──
all_fills = []
cursor = None
for _ in range(10):
    url = f'{base}/portfolio/fills?limit=100'
    if cursor:
        url += f'&cursor={cursor}'
    hdr = bot._signed_headers('GET', f'{path_prefix}/portfolio/fills')
    r = requests.get(url, headers=hdr)
    data = r.json()
    fills = data.get('fills', [])
    all_fills.extend(fills)
    cursor = data.get('cursor')
    if not cursor or not fills:
        break

# ── Get all settlements ──
all_settles = []
cursor = None
for _ in range(10):
    url = f'{base}/portfolio/settlements?limit=100'
    if cursor:
        url += f'&cursor={cursor}'
    hdr = bot._signed_headers('GET', f'{path_prefix}/portfolio/settlements')
    r = requests.get(url, headers=hdr)
    data = r.json()
    settles = data.get('settlements', [])
    all_settles.extend(settles)
    cursor = data.get('cursor')
    if not cursor or not settles:
        break

# ── Get open positions ──
positions = bot.get_positions()

# ── Analyze fills by type ──
binary_fills = [f for f in all_fills if '15M' in f.get('ticker','')]
bracket_fills = [f for f in all_fills if '15M' not in f.get('ticker','')]

print(f"=== FILL SUMMARY ===")
print(f"Total fills: {len(all_fills)}")
print(f"  Binary 15m: {len(binary_fills)}")
print(f"  Bracket:    {len(bracket_fills)}")
print()

# ── Binary 15m fills detail ──
print("=== BINARY 15M FILLS ===")
binary_cost = 0
for f in binary_fills:
    tk = f.get('ticker','')
    side = f.get('side','')
    count = f.get('count',0)
    yes_price = f.get('yes_price',0)
    no_price = f.get('no_price',0)
    price = yes_price if side == 'yes' else no_price
    cost_cents = count * price
    binary_cost += cost_cents
    created = f.get('created_time','')[:19]
    print(f"  {created}  {tk:45s}  {side:3s} x{count:3d} @{price:2d}c  cost=${cost_cents/100:.2f}")
print(f"  Total binary cost: ${binary_cost/100:.2f}")
print()

# ── Settlements with positions ──
print("=== SETTLEMENTS WITH POSITIONS ===")
total_pnl = 0
binary_pnl = 0
bracket_pnl = 0
total_fees = 0

for s in all_settles:
    tk = s.get('ticker','')
    no_count = s.get('no_count', 0)
    yes_count = s.get('yes_count', 0)
    if no_count == 0 and yes_count == 0:
        continue  # no position in this market
    
    no_cost = s.get('no_total_cost', 0)  # cents
    yes_cost = s.get('yes_total_cost', 0)  # cents
    revenue = s.get('revenue', 0)  # cents
    fee = float(s.get('fee_cost', '0'))  # dollars
    result = s.get('market_result', '')
    
    pnl_cents = revenue - no_cost - yes_cost
    pnl_dollars = pnl_cents / 100.0 - fee
    total_pnl += pnl_dollars
    total_fees += fee
    
    is_binary = '15M' in tk
    if is_binary:
        binary_pnl += pnl_dollars
    else:
        bracket_pnl += pnl_dollars
    
    side = 'NO' if no_count > 0 else 'YES'
    count = no_count if no_count > 0 else yes_count
    cost = no_cost if no_count > 0 else yes_cost
    won = (side == 'NO' and result == 'no') or (side == 'YES' and result == 'yes')
    prefix = '15M' if is_binary else 'BRK'
    symbol = 'WIN' if won else 'LOSS'
    
    print(f"  [{prefix}] {tk:45s}  {side:3s} x{count:3d}  cost=${cost/100:.2f}  rev=${revenue/100:.2f}  fee=${fee:.2f}  PnL=${pnl_dollars:+.2f}  {symbol}  result={result}")

print()
print(f"=== P&L SUMMARY ===")
print(f"  Bracket P&L:    ${bracket_pnl:+.2f}")
print(f"  Binary 15m P&L: ${binary_pnl:+.2f}")
print(f"  Total P&L:      ${total_pnl:+.2f}")
print(f"  Total fees:     ${total_fees:.2f}")
print()

# ── Open positions ──
print(f"=== OPEN POSITIONS ({len(positions)}) ===")
open_cost = 0
for p in positions:
    tk = p.get('ticker','') or p.get('market_ticker','')
    qty = p.get('total_traded',0) or p.get('position',0)
    side = ''
    yes_qty = p.get('yes_count', 0) or p.get('yes_sub_total',0)
    no_qty = p.get('no_count', 0) or p.get('no_sub_total',0)
    market_exposure = p.get('market_exposure', 0)
    realized = p.get('realized_pnl', 0)
    print(f"  {tk:45s}  yes={yes_qty} no={no_qty}  exposure={market_exposure}  realized_pnl={realized}")
    print(f"    raw: {json.dumps(p)}")

# ── Check log for today's trades ──
print()
print("=== LOG ANALYSIS ===")
try:
    with open('ws_trader.log', 'r', encoding='utf-8') as f:
        lines = f.readlines()
    trades = [l.strip() for l in lines if 'FILLED' in l or 'LIVE ORDER' in l or 'PAPER BUY' in l or 'CANCELLED' in l or 'RESTING' in l]
    for t in trades:
        print(f"  {t}")
except Exception as e:
    print(f"  Could not read log: {e}")
