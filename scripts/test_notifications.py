import os
import sys
from pathlib import Path

# Ensure digest mode for the test
os.environ['NOTIFICATION_MODE'] = 'digest'
os.environ['NOTIFICATION_SUMMARY_INTERVAL_MIN'] = '1'

# Ensure project root is on sys.path so imports work when running from scripts/
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notifications import NotificationManager
import time

mgr = NotificationManager()

# Create sample opportunities
op1 = {
    'ticker': 'TEST-1',
    'title': 'Test Market 1',
    'yes_price': 48,
    'no_price': 50,
    'total_cost': 98,
    'profit_cents': 2,
    'profit_percent': 2.04,
    'strategy': 'Buy both',
    'timestamp': '2026-02-16T12:00:00Z'
}
op2 = {
    'ticker': 'TEST-2',
    'title': 'Test Market 2',
    'yes_price': 45,
    'no_price': 54,
    'total_cost': 99,
    'profit_cents': 1,
    'profit_percent': 1.01,
    'strategy': 'Buy both',
    'timestamp': '2026-02-16T12:01:00Z'
}

trade = {
    'ticker': 'TEST-TRADE',
    'type': 'arbitrage',
    'quantity': 2,
    'yes_price': 48,
    'no_price': 50,
    'cost': 1.96,
    'expected_profit': 0.04,
    'timestamp': '2026-02-16T12:02:00Z'
}

# Buffer items
mgr.notify_opportunity(op1)
mgr.notify_opportunity(op2)
mgr.notify_trade_executed(trade)

# Show buffered counts
print("Buffered opportunities:", len(mgr._opportunities))
print("Buffered trades:", len(mgr._trades))

# Manually build and print the digest preview (so we can see output without sending email/webhook)
with mgr._lock:
    opps = list(mgr._opportunities)
    trades = list(mgr._trades)

parts = []
if opps:
    parts.append(f"Opportunities: {len(opps)}")
    for o in opps:
        parts.append(f"- {o.get('ticker')} | {o.get('profit_cents', 0)}¢ ({o.get('profit_percent', 0):.2f}%) | {o.get('title','')}")

if trades:
    parts.append(f"Trades executed: {len(trades)}")
    for t in trades:
        parts.append(f"- {t.get('ticker')} | ${t.get('cost',0):.2f} | qty={t.get('quantity')}")

subject = f"Kalshi Digest: {len(opps)} opps, {len(trades)} trades"
body = "\n".join(parts)

print("\n--- DIGEST PREVIEW ---")
print(subject)
print(body)
print("--- END DIGEST PREVIEW ---\n")

# Clean up background thread and flush buffers
mgr.stop()
print("Test complete.")
