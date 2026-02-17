"""Diagnostic scan — shows what the probability model actually sees for each market.
Reveals whether edges are close to threshold or nowhere near."""

import sys, os
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.path.insert(0, '.')

from config import Config
from kalshi_bot import KalshiAPI
from probability_trader import ProbabilityTrader

api = KalshiAPI(api_key=Config.KALSHI_API_KEY)
trader = ProbabilityTrader(api)

# Fetch crypto markets
btc_markets = api.get_all_markets(status="open", series_ticker="KXBTC")
eth_markets = api.get_all_markets(status="open", series_ticker="KXETH")
markets = btc_markets + eth_markets

print(f"\n{'='*80}")
print(f"DIAGNOSTIC SCAN — {len(markets)} crypto markets")
print(f"Min edge threshold: {trader.min_edge_percent}%")
print(f"{'='*80}\n")

# Get current prices
btc_price = trader.get_current_price('BTC')
eth_price = trader.get_current_price('ETH')
print(f"Current BTC: ${btc_price:,.2f}" if btc_price else "BTC price: FAILED")
print(f"Current ETH: ${eth_price:,.2f}" if eth_price else "ETH price: FAILED")
print()

results = []
skipped_no_parse = 0
skipped_no_orderbook = 0
skipped_no_asks = 0
skipped_expired = 0

import time
from datetime import datetime, timezone

for i, m in enumerate(markets[:80]):  # Sample up to 80
    ticker = m.get('ticker', '')
    
    parsed = trader.parse_strike_from_ticker(ticker)
    if not parsed:
        skipped_no_parse += 1
        continue
    
    # Check time remaining
    close_time_str = m.get('close_time') or m.get('expiration_time')
    if close_time_str:
        try:
            close_time = datetime.fromisoformat(close_time_str.replace('Z', '+00:00'))
            mins_left = (close_time - datetime.now(timezone.utc)).total_seconds() / 60
            if mins_left <= 0:
                skipped_expired += 1
                continue
        except:
            continue
    else:
        continue
    
    # Get orderbook
    ob = api.get_orderbook(ticker)
    time.sleep(0.15)
    
    if not ob:
        skipped_no_orderbook += 1
        continue
    
    yes_asks = ob.get('yes_asks', [])
    no_asks = ob.get('no_asks', [])
    
    # Allow one-sided books — use 99 as placeholder for missing side
    if not yes_asks and not no_asks:
        skipped_no_asks += 1
        continue
    
    try:
        best_yes = min([a[0] for a in yes_asks]) if yes_asks else 99
        best_no = min([a[0] for a in no_asks]) if no_asks else 99
    except:
        continue
    
    asset = parsed['asset']
    strike = parsed['strike']
    direction = parsed['direction']
    current = btc_price if asset == 'BTC' else eth_price
    
    if not current:
        continue
    
    # Estimate probability
    est_prob_yes = trader.estimate_probability(current, strike, mins_left, asset, 
                                                'above' if direction == 'above' else 'below')
    est_prob_no = 1 - est_prob_yes
    
    implied_yes = best_yes / 100.0
    implied_no = best_no / 100.0
    
    edge_yes = (est_prob_yes - implied_yes) * 100 if yes_asks else -999
    edge_no = (est_prob_no - implied_no) * 100 if no_asks else -999
    best_edge = max(edge_yes, edge_no)
    best_side = 'YES' if edge_yes > edge_no else 'NO'
    
    results.append({
        'ticker': ticker,
        'asset': asset,
        'strike': strike,
        'direction': direction,
        'current': current,
        'mins_left': mins_left,
        'est_prob_yes': est_prob_yes,
        'implied_yes': implied_yes,
        'est_prob_no': est_prob_no,
        'implied_no': implied_no,
        'edge_yes': edge_yes,
        'edge_no': edge_no,
        'best_edge': best_edge,
        'best_side': best_side,
        'best_yes': best_yes,
        'best_no': best_no,
        'has_yes': bool(yes_asks),
        'has_no': bool(no_asks),
    })
    
    if (i+1) % 20 == 0:
        print(f"  ...scanned {i+1} markets so far")

# Sort by edge (highest first)
results.sort(key=lambda r: r['best_edge'], reverse=True)

print(f"\nSkipped: {skipped_no_parse} unparseable, {skipped_no_orderbook} no orderbook, "
      f"{skipped_no_asks} no asks, {skipped_expired} expired")
print(f"Analyzed: {len(results)} markets\n")

print(f"{'Ticker':<28} {'Strike':>8} {'Curr':>9} {'Dir':>6} {'Min':>5} "
      f"{'EstYes':>7} {'MktYes':>7} {'EdgeY':>7} {'EdgeN':>7} {'Best':>7}")
print("-" * 110)

for r in results[:30]:
    flag = " <--" if r['best_edge'] >= 2.0 else ""
    print(f"{r['ticker']:<28} {r['strike']:>8.0f} {r['current']:>9.0f} {r['direction']:>6} "
          f"{r['mins_left']:>5.0f} {r['est_prob_yes']:>7.1%} {r['implied_yes']:>7.1%} "
          f"{r['edge_yes']:>+7.1f}% {r['edge_no']:>+7.1f}% {r['best_edge']:>+6.1f}%{flag}")

print(f"\n{'='*80}")
print(f"Top edge: {results[0]['best_edge']:+.2f}% on {results[0]['ticker']}" if results else "No results")
if results:
    above_2 = sum(1 for r in results if r['best_edge'] >= 2.0)
    above_1 = sum(1 for r in results if r['best_edge'] >= 1.0)
    above_0 = sum(1 for r in results if r['best_edge'] >= 0.0)
    print(f"Markets with edge >= 2%: {above_2}")
    print(f"Markets with edge >= 1%: {above_1}")
    print(f"Markets with edge >= 0%: {above_0}")
    print(f"Markets with negative edge: {len(results) - above_0}")
print(f"{'='*80}")
