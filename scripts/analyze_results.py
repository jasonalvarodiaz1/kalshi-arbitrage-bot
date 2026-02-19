"""Analyze paper trading results to identify winning/losing patterns."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import csv

wins = losses = 0
total_pnl = total_cost = 0.0

# By side
yes_w = yes_l = 0; yes_pnl = 0.0
no_w = no_l = 0; no_pnl = 0.0

# By price bucket
buckets = {
    'YES <=15c': {'w': 0, 'l': 0, 'pnl': 0.0, 'cost': 0.0},
    'YES 16-30c': {'w': 0, 'l': 0, 'pnl': 0.0, 'cost': 0.0},
    'YES 31-60c': {'w': 0, 'l': 0, 'pnl': 0.0, 'cost': 0.0},
    'YES 61-90c': {'w': 0, 'l': 0, 'pnl': 0.0, 'cost': 0.0},
    'NO  <=15c': {'w': 0, 'l': 0, 'pnl': 0.0, 'cost': 0.0},
    'NO  16-30c': {'w': 0, 'l': 0, 'pnl': 0.0, 'cost': 0.0},
    'NO  31-60c': {'w': 0, 'l': 0, 'pnl': 0.0, 'cost': 0.0},
    'NO  61-90c': {'w': 0, 'l': 0, 'pnl': 0.0, 'cost': 0.0},
}

# By asset
assets = {}

trades = []
with open('paper_results.csv') as f:
    reader = csv.DictReader(f)
    for r in reader:
        won = r['won'] == 'True'
        pnl = float(r['pnl'])
        cost = float(r['cost'])
        price = int(r['price_cents'])
        side = r['side']
        edge = float(r['edge_pct'])
        model = float(r['model_prob'])
        asset = r['asset']
        mins = float(r['minutes_left'])

        total_pnl += pnl
        total_cost += cost
        if won: wins += 1
        else: losses += 1

        if side == 'yes':
            yes_pnl += pnl
            if won: yes_w += 1
            else: yes_l += 1
        else:
            no_pnl += pnl
            if won: no_w += 1
            else: no_l += 1

        # Bucket
        if price <= 15: pb = f'{side.upper():3s} <=15c'
        elif price <= 30: pb = f'{side.upper():3s} 16-30c'
        elif price <= 60: pb = f'{side.upper():3s} 31-60c'
        else: pb = f'{side.upper():3s} 61-90c'
        if pb in buckets:
            b = buckets[pb]
            b['pnl'] += pnl
            b['cost'] += cost
            if won: b['w'] += 1
            else: b['l'] += 1

        # Asset
        if asset not in assets:
            assets[asset] = {'w': 0, 'l': 0, 'pnl': 0.0}
        assets[asset]['pnl'] += pnl
        if won: assets[asset]['w'] += 1
        else: assets[asset]['l'] += 1

        trades.append({'side': side, 'price': price, 'won': won, 'pnl': pnl,
                       'model': model, 'edge': edge, 'asset': asset, 'mins': mins,
                       'ticker': r['ticker'], 'cost': cost})

n = wins + losses
print(f"OVERALL: {wins}/{n} wins ({wins/n*100:.0f}%) | P&L: ${total_pnl:+.2f} | Cost: ${total_cost:.2f}")
print(f"  YES: {yes_w}/{yes_w+yes_l} wins | P&L: ${yes_pnl:+.2f}")
print(f"  NO:  {no_w}/{no_w+no_l} wins | P&L: ${no_pnl:+.2f}")
print()

print("BY PRICE BUCKET:")
for name, b in buckets.items():
    t = b['w'] + b['l']
    if t == 0: continue
    wr = b['w'] / t * 100
    print(f"  {name}: {b['w']}/{t} wins ({wr:.0f}%) | P&L: ${b['pnl']:+.2f} | Cost: ${b['cost']:.2f}")
print()

print("BY ASSET:")
for asset, a in sorted(assets.items()):
    t = a['w'] + a['l']
    wr = a['w'] / t * 100
    print(f"  {asset}: {a['w']}/{t} wins ({wr:.0f}%) | P&L: ${a['pnl']:+.2f}")
print()

print("TRADE DETAIL:")
for t in trades:
    result = "WIN " if t['won'] else "LOSS"
    print(f"  {result} {t['side']:3s} {t['price']:3d}c  model={t['model']:.1%}  edge={t['edge']:+.1f}%  {t['asset']:4s}  {t['mins']:.0f}min  pnl=${t['pnl']:+.2f}  {t['ticker']}")
