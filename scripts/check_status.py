import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sys, os, json, requests
sys.stdout.reconfigure(encoding='utf-8')
from kalshi_bot import KalshiAPI
from config import Config

api = KalshiAPI(api_key=Config.KALSHI_API_KEY)
api._ensure_auth()

# Balance
h = api._signed_headers('GET', '/trade-api/v2/portfolio/balance')
r = requests.get(api.BASE_URL + '/portfolio/balance', headers=h)
d = r.json()
cash = d.get('balance', 0) / 100
pv = d.get('portfolio_value', 0) / 100
total = cash + pv
pl = total - 250
print("=== BALANCE ===")
print(f"Cash: ${cash:.2f}  Portfolio: ${pv:.2f}  Total: ${total:.2f}  P/L: {pl:+.2f}")

# Open positions
h2 = api._signed_headers('GET', '/trade-api/v2/portfolio/positions')
r2 = requests.get(api.BASE_URL + '/portfolio/positions', headers=h2)
positions = r2.json().get('market_positions', [])
open_pos = [p for p in positions if p.get('position', 0) != 0]
total_exp = sum(p.get('market_exposure', 0) for p in open_pos)
print(f"\n=== OPEN POSITIONS ({len(open_pos)}, exposure=${total_exp/100:.2f}) ===")
for p in open_pos:
    print(f"  {p.get('ticker','?')}  pos={p.get('position',0)}  exp=${p.get('market_exposure',0)/100:.2f}")

# Recent fills with pagination
print("\n=== RECENT FILLS ===")
all_fills = []
cursor = None
for page in range(10):
    path = '/trade-api/v2/portfolio/fills?limit=100'
    if cursor:
        path += '&cursor=' + cursor
    h3 = api._signed_headers('GET', path)
    r3 = requests.get(api.BASE_URL + path.split('/v2')[1], headers=h3)
    d3 = r3.json()
    fills = d3.get('fills', [])
    all_fills.extend(fills)
    cursor = d3.get('cursor', '')
    if not fills or not cursor:
        break

total_cost = 0
total_fees = 0
by_ticker = {}
for f in all_fills:
    ticker = f.get('ticker', '?')
    side = f.get('side', '?')
    count = f.get('count', 0)
    price = f.get('yes_price', 0) if side == 'yes' else f.get('no_price', 0)
    cost = count * price
    fee = f.get('fee', 0) if isinstance(f.get('fee'), (int, float)) else 0
    total_cost += cost
    total_fees += fee
    created = f.get('created_time', '')[:19]
    action = f.get('action', '?')
    
    key = ticker
    if key not in by_ticker:
        by_ticker[key] = {'count': 0, 'cost': 0, 'side': side, 'action': action}
    by_ticker[key]['count'] += count
    by_ticker[key]['cost'] += cost

print("Total fills:", len(all_fills), "cost: $%.2f" % (total_cost/100), "fees: $%.2f" % (total_fees/100))
if all_fills:
    first = all_fills[0].get('created_time', '')[:19]
    last = all_fills[-1].get('created_time', '')[:19]
    print("Date range:", last, "to", first)

print("\nBy ticker:")
for ticker, info in sorted(by_ticker.items()):
    avg = info['cost'] / info['count'] if info['count'] else 0
    print("  %s: %s %d %s @ avg %.0fc = $%.2f" % (ticker, info['action'], info['count'], info['side'], avg, info['cost']/100))

# Show individual recent fills
print("\n=== INDIVIDUAL FILLS (newest first) ===")
for f in all_fills[:40]:
    ticker = f.get('ticker', '?')
    side = f.get('side', '?')
    count = f.get('count', 0)
    price = f.get('yes_price', 0) if side == 'yes' else f.get('no_price', 0)
    created = f.get('created_time', '')[:19]
    action = f.get('action', '?')
    print("  %s %s %dx %s %s @ %dc" % (created, action, count, side, ticker, price))
