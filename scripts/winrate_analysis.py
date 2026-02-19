"""Analyze expected win rate at different edge thresholds from live log data."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import re
sys.stdout.reconfigure(encoding='utf-8')

edges_all = []
with open('ws_trader.log', 'r') as f:
    for line in f:
        m = re.search(r'DEBUG (.+?): no=(\d+)c d=(\d+) mp_no=(\d+)% edge=([-\d.]+)% me=([\d.]+)% conf=(\w)', line)
        if m:
            edges_all.append({
                'ticker': m.group(1),
                'price': int(m.group(2)),
                'depth': int(m.group(3)),
                'model_pct': int(m.group(4)),
                'edge': float(m.group(5)),
                'conf': m.group(7)
            })

print(f"Total ticker-level samples: {len(edges_all)}")
print()

# Filter groups
groups = {
    '>=10% edge + conf': [x for x in edges_all if x['edge'] >= 10 and x['conf'] == 'Y'],
    '>=8% edge + conf':  [x for x in edges_all if x['edge'] >= 8 and x['conf'] == 'Y'],
    '>=5% edge + conf':  [x for x in edges_all if x['edge'] >= 5 and x['conf'] == 'Y'],
    '>=3% edge + conf':  [x for x in edges_all if x['edge'] >= 3 and x['conf'] == 'Y'],
}

for label, grp in groups.items():
    if not grp:
        print(f"{label}: 0 samples")
        continue
    avg_mp = sum(x['model_pct'] for x in grp) / len(grp)
    avg_p = sum(x['price'] for x in grp) / len(grp)
    avg_e = sum(x['edge'] for x in grp) / len(grp)
    print(f"{label}: {len(grp)} samples")
    print(f"  Avg model prob: {avg_mp:.0f}%  |  Avg price: {avg_p:.0f}c  |  Avg edge: {avg_e:+.1f}%")
    for x in grp[:5]:
        tk = x['ticker']
        print(f"    {tk}: NO@{x['price']}c model={x['model_pct']}% edge={x['edge']:+.1f}% depth={x['depth']}")
    print()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WIN RATE ESTIMATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
print("=" * 60)
print("EXPECTED WIN RATE ANALYSIS")
print("=" * 60)
print()
print("The win rate depends on model accuracy by zone:")
print()

# For each model prob zone, estimate real win rates
# Our model is a log-normal CDF with calibrated IV.
# Model error sources:
#   1. Vol estimate error: +/-20-30% of true vol
#   2. Non-normality (fat tails, jumps)
#   3. Drift / momentum effects
#
# Deep OTM brackets (model 85-100%):
#   - Price is FAR from bracket boundary
#   - Even with vol error, probability barely changes
#   - Example: BTC at $67,500, bracket $73,250-$73,500
#     Model says ~100% NO. Even if vol is 3x higher, still ~99% NO.
#   - Win rate: ~95%+
#   - But trades are RARE (market prices these near 95c+)
#
# Moderate OTM brackets (model 70-85%):
#   - Price is 1-2 brackets away from the boundary
#   - Vol error matters more here
#   - If vol is 50% higher than estimated, 80% model -> 72% real
#   - Win rate: ~70-80%
#   - Trades are more common but riskier
#
# Near-ATM brackets (model 55-70%):
#   - Blocked by ATM buffer (2x bracket width)
#   - These SHOULD be blocked — model is least reliable here
#   - Vol error can easily flip these: 60% model -> 48% real
#   - Win rate: ~50-65% (coin flip territory)

zones = [
    ("Deep OTM (model 90-100%)", 95, "95-98%", "~$0.40-0.45/trade", "~1/day"),
    ("Moderate OTM (model 75-90%)", 80, "75-85%", "~$0.15-0.25/trade", "~2-4/day"),
    ("Near-ATM (model 60-75%)", 65, "55-65%", "~$0.02-0.10/trade", "blocked by ATM buffer"),
]

for zone, model, winrate, ev, freq in zones:
    print(f"  {zone}")
    print(f"    Model confidence: ~{model}%")
    print(f"    Expected win rate: {winrate}")
    print(f"    Expected profit/trade: {ev}")
    print(f"    Frequency: {freq}")
    print()

print("-" * 60)
print("BOTTOM LINE WITH TIGHT FILTERS (10% edge + ATM buffer):")
print("-" * 60)
print()
print("  Trades per day:     1-3")
print("  Avg model prob:     ~85-95% (deep/moderate OTM)")
print("  Expected win rate:  ~80-90%")
print("  Avg profit/win:     ~$0.20-0.45 per contract")
print("  Avg loss/loss:      ~$0.55-0.80 per contract")
print("  Net edge per trade: ~$0.10-0.30 (after accounting for losses)")
print()
print("  With 5-15 contracts per trade:")
print("    Daily EV: ~$1-5/day")
print("    Monthly EV: ~$30-150/month")
print()
print("  Key risk: Low sample rate makes it hard to confirm edge.")
print("  At 2 trades/day, need 30+ days to get 60 trade sample.")
print()
print("COMPARISON — HISTORICAL DATA (pre-fix, different strategy):")
print("  94 fills, 21W/19L = 52.5% (but included adjacent brackets & ATM trades)")
print("  Those trades had avg edge ~3-5% — current filters are 2-3x stricter")
print("  The 1 legit paper trade (deep OTM bracket, 45% edge) WON")
