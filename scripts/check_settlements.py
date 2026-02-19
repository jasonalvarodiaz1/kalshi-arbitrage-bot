"""Check settlement results for all positions."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sys
sys.stdout.reconfigure(encoding='utf-8')

from kalshi_bot import KalshiAPI
from config import Config
import requests

api = KalshiAPI(api_key=Config.KALSHI_API_KEY)
api._ensure_auth()

cursor = ''
all_s = []
while True:
    path = '/trade-api/v2/portfolio/settlements'
    qs = '?limit=200' + ('&cursor=' + cursor if cursor else '')
    h = api._signed_headers('GET', path + qs)
    r = requests.get(api.BASE_URL + '/portfolio/settlements' + qs, headers=h)
    d = r.json()
    settlements = d.get('settlements', [])
    all_s.extend(settlements)
    cursor = d.get('cursor', '')
    if not cursor:
        break

total_pnl = 0
total_cost = 0
total_rev = 0
wins = 0
losses = 0

print(f"{'Ticker':<40} {'Result':>6} {'NO':>4} {'YES':>4} {'Cost':>7} {'Rev':>7} {'P&L':>7}")
print("-" * 85)

for s in all_s:
    nc = s.get('no_count', 0)
    yc = s.get('yes_count', 0)
    if nc > 0 or yc > 0:
        rev = s.get('revenue', 0)
        cost = s.get('no_total_cost', 0) + s.get('yes_total_cost', 0)
        result = s.get('market_result', '?')
        ticker = s.get('ticker', '?')
        pnl = rev - cost
        total_pnl += pnl
        total_cost += cost
        total_rev += rev
        if pnl >= 0:
            wins += 1
        else:
            losses += 1
        print(f"{ticker:<40} {result:>6} {nc:>4} {yc:>4} {cost:>6}c {rev:>6}c {pnl:>+6}c")

print("-" * 85)
print(f"Total: {wins} wins, {losses} losses")
print(f"Total cost: {total_cost}c (${total_cost/100:.2f})")
print(f"Total revenue: {total_rev}c (${total_rev/100:.2f})")
print(f"Settlement P&L: {total_pnl:+}c (${total_pnl/100:+.2f})")
if wins + losses > 0:
    print(f"Win rate: {wins/(wins+losses)*100:.0f}%")

# Also check current balance
h2 = api._signed_headers('GET', '/trade-api/v2/portfolio/balance')
r2 = requests.get(api.BASE_URL + '/portfolio/balance', headers=h2)
d2 = r2.json()
cash = d2.get('balance', 0) / 100
pv = d2.get('portfolio_value', 0) / 100
print(f"\nCurrent: ${cash:.2f} cash + ${pv:.2f} portfolio = ${cash+pv:.2f}")
print(f"Overall P&L from $250: ${cash+pv-250:+.2f}")
