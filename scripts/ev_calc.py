"""Show exact dollar math for tight-filter EV at current balance."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

balance = 171.18

# Current sizing constraints
max_event = 15.0
max_asset = balance * 0.20
cppi_floor = balance * 0.70
cppi_cushion = balance - cppi_floor
cppi_max = 3.0 * cppi_cushion
kelly_mult = 0.5

print(f"Balance: {balance:.2f}")
print(f"Sizing caps:")
print(f"  Per-event max:  {max_event:.2f}  (binding constraint)")
print(f"  Per-asset max:  {max_asset:.2f}")
print(f"  CPPI max:       {cppi_max:.2f}")
print()

print("=== TIGHT FILTER (10%+ edge) TRADE SCENARIOS ===")
print()

scenarios = [
    ("Deep OTM stale quote (like B73375)", 55, 100, 5),
    ("Far bracket, NO expensive", 92, 97, 5),
    ("Moderate OTM, cheap NO", 15, 85, 10),
]

for desc, price_c, model_pct, qty in scenarios:
    cost = qty * price_c / 100.0
    payout = qty * 1.0
    profit_if_win = payout - cost
    loss_if_lose = cost
    win_rate = min(model_pct / 100.0 * 0.95, 0.98)
    ev = win_rate * profit_if_win - (1 - win_rate) * loss_if_lose

    print(f"  {desc}:")
    print(f"    Buy {qty}x NO @ {price_c}c = {cost:.2f} risked")
    print(f"    Win: +{profit_if_win:.2f}  |  Lose: -{loss_if_lose:.2f}")
    print(f"    Win rate: ~{win_rate:.0%}")
    print(f"    EV per trade: {ev:+.2f}")
    print()

print("=" * 55)
print("REALISTIC DAILY P&L AT 171 BALANCE:")
print("=" * 55)
print()
print("Trades/day: 1-3 (mostly deep OTM stale quotes)")
print()

for trades_per_day in [1, 2, 3]:
    avg_qty = 7
    avg_price = 40
    avg_cost = avg_qty * avg_price / 100.0
    avg_win = avg_qty * 1.0 - avg_cost
    win_rate = 0.85
    daily_ev = trades_per_day * (win_rate * avg_win - (1 - win_rate) * avg_cost)
    monthly = daily_ev * 30
    annual = daily_ev * 365
    print(f"  {trades_per_day} trades/day: EV {daily_ev:+.2f}/day = {monthly:+.0f}/month = {annual:+.0f}/year")

print()
print("With 1,000 balance (same strategy, bigger positions):")
for trades_per_day in [1, 2, 3]:
    avg_qty = 25
    avg_price = 40
    avg_cost = avg_qty * avg_price / 100.0
    avg_win = avg_qty * 1.0 - avg_cost
    win_rate = 0.85
    daily_ev = trades_per_day * (win_rate * avg_win - (1 - win_rate) * avg_cost)
    monthly = daily_ev * 30
    print(f"  {trades_per_day} trades/day: EV {daily_ev:+.2f}/day = {monthly:+.0f}/month")

print()
print("With 5,000 balance:")
for trades_per_day in [1, 2, 3]:
    avg_qty = 50
    avg_price = 40
    avg_cost = avg_qty * avg_price / 100.0
    avg_win = avg_qty * 1.0 - avg_cost
    win_rate = 0.85
    daily_ev = trades_per_day * (win_rate * avg_win - (1 - win_rate) * avg_cost)
    monthly = daily_ev * 30
    print(f"  {trades_per_day} trades/day: EV {daily_ev:+.2f}/day = {monthly:+.0f}/month")

print()
print("BOTTOM LINE:")
print("  At 171, the per-event cap of 15 limits you to ~5-15 contracts.")
print("  Even with 85% win rate, daily EV is 1-5.")
print("  You need more capital OR more trades to make this meaningful.")
