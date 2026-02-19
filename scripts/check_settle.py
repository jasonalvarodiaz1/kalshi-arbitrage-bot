import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sys, os, json, requests
from datetime import datetime, timezone
sys.stdout.reconfigure(encoding='utf-8')
from kalshi_bot import KalshiAPI
from config import Config

api = KalshiAPI(api_key=Config.KALSHI_API_KEY)
api._ensure_auth()

# Open positions
h2 = api._signed_headers('GET', '/trade-api/v2/portfolio/positions')
r2 = requests.get(api.BASE_URL + '/portfolio/positions', headers=h2)
positions = r2.json().get('market_positions', [])
open_pos = [p for p in positions if p.get('position', 0) != 0]

# Check settlement status for our tickers
print("=== POSITION SETTLEMENT STATUS ===")
for p in open_pos:
    ticker = p.get('ticker', '?')
    pos = p.get('position', 0)
    exp = p.get('market_exposure', 0)
    
    # Check if market settled
    path = '/trade-api/v2/markets/' + ticker
    h = api._signed_headers('GET', path)
    r = requests.get(api.BASE_URL + '/markets/' + ticker, headers=h)
    md = r.json().get('market', {})
    status = md.get('status', '?')
    result = md.get('result', '?')
    close_time = md.get('close_time', '')
    
    print("  %s: pos=%d exp=$%.2f status=%s result=%s close=%s" % (
        ticker, pos, exp/100, status, result, close_time[:19]))

# Balance
h = api._signed_headers('GET', '/trade-api/v2/portfolio/balance')
r = requests.get(api.BASE_URL + '/portfolio/balance', headers=h)
d = r.json()
cash = d.get('balance', 0) / 100
pv = d.get('portfolio_value', 0) / 100
total = cash + pv
pl = total - 250
print("\n=== BALANCE ===")
print("Cash: $%.2f  Portfolio: $%.2f  Total: $%.2f  P/L: %+.2f" % (cash, pv, total, pl))

# Check for next events
print("\n=== UPCOMING EVENTS ===")
now = datetime.now(timezone.utc)
for series in ['KXBTC', 'KXETH', 'KXDOGE', 'KXXRP']:
    markets = api.get_all_markets(status='open', series_ticker=series)
    events = {}
    for m in markets:
        et = m.get('event_ticker', '')
        events.setdefault(et, []).append(m)
    for ev in sorted(events.keys()):
        ct = events[ev][0].get('close_time', '')
        try:
            close = datetime.fromisoformat(ct.replace('Z', '+00:00'))
            mins = (close - now).total_seconds() / 60
        except:
            mins = -1
        if mins > 0 and mins < 120:
            print("  %s: %d brackets, %.0f min left" % (ev, len(events[ev]), mins))

# Fills from different endpoint
print("\n=== ORDERS (recent) ===")
path = '/trade-api/v2/portfolio/orders?limit=20'
h = api._signed_headers('GET', path)
r = requests.get(api.BASE_URL + '/portfolio/orders?limit=20', headers=h)
orders = r.json().get('orders', [])
print("Total orders returned:", len(orders))
for o in orders[:15]:
    ticker = o.get('ticker', '?')
    side = o.get('side', '?')
    action = o.get('action', '?')
    status = o.get('status', '?')
    price = o.get('yes_price', 0) if side == 'yes' else o.get('no_price', 0)
    count = o.get('count', 0) if o.get('count') else o.get('remaining_count', 0)
    created = o.get('created_time', '')[:19]
    print("  %s %s %s %dx %s %s @ %dc status=%s" % (created, action, side, count, ticker, side, price, status))
