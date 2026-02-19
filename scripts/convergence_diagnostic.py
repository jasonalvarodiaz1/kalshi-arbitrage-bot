"""Diagnostic: What does the convergence model see right now?"""
import sys, os, time, math
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.path.insert(0, '.')

from config import Config
from kalshi_bot import KalshiAPI
from convergence_trader import ConvergenceTrader
from datetime import datetime, timezone

api = KalshiAPI(api_key=Config.KALSHI_API_KEY)
trader = ConvergenceTrader(api)

# Widen parameters for diagnostic purposes
trader.max_expiry_minutes = 120   # Look at everything within 2 hours  
trader.min_edge_pct = 0.0         # Show ALL edges, not just tradeable ones
trader.min_confidence = 0.0       # Show everything

print(f"\n{'='*80}")
print(f"CONVERGENCE MODEL DIAGNOSTIC")
print(f"Time: {datetime.now().strftime('%H:%M:%S')}")
print(f"{'='*80}\n")

opps = trader.scan_for_convergence()

# Also show the full event breakdown
btc_markets = api.get_all_markets(status="open", series_ticker="KXBTC")
eth_markets = api.get_all_markets(status="open", series_ticker="KXETH")
all_m = btc_markets + eth_markets

# Group by event
events = {}
for m in all_m:
    et = m.get('event_ticker', '?')
    events.setdefault(et, []).append(m)

btc_price = trader.get_price('BTC')
eth_price = trader.get_price('ETH')
now = datetime.now(timezone.utc)

print(f"\nBTC: ${btc_price:,.2f}" if btc_price else "BTC: N/A")
print(f"ETH: ${eth_price:,.2f}" if eth_price else "ETH: N/A")
print()

for et, brackets in sorted(events.items()):
    ct = brackets[0].get('close_time', '')
    if not ct: continue
    close = datetime.fromisoformat(ct.replace('Z','+00:00'))
    mins = (close - now).total_seconds() / 60
    if mins <= 0 or mins > 120: continue
    
    asset = 'BTC' if 'KXBTC' in et else 'ETH'
    current = btc_price if asset == 'BTC' else eth_price
    if not current: continue
    
    sorted_b = sorted(brackets, key=lambda b: b.get('floor_strike') or 0)
    
    # Find ATM and calibrate
    atm = trader._find_atm_bracket(sorted_b, current)
    impl_vol = trader.implied_vol_from_atm(current, atm, mins) if atm else None
    
    print(f"\n{'='*80}")
    print(f"EVENT: {et}  |  {len(brackets)} brackets  |  {mins:.0f} min to close")
    print(f"Asset: {asset} = ${current:,.2f}")
    if impl_vol:
        print(f"Implied Vol: {impl_vol*100:.3f}%/15min")
    else:
        print(f"Implied Vol: N/A (using default {trader.base_vol.get(asset,0)*100:.2f}%)")
    vol = impl_vol or trader.base_vol.get(asset, 0.002)
    print(f"{'='*80}")
    
    print(f"{'Floor':>10} {'Cap':>10} {'Model%':>8} {'YBid':>5} {'YAsk':>5} {'NBid':>5} {'NAsk':>5} {'EdgeY':>7} {'EdgeN':>7} {'ATM':>4}")
    print("-" * 85)
    
    for b in sorted_b:
        fs = b.get('floor_strike')
        cs = b.get('cap_strike')
        yb = b.get('yes_bid', 0) or 0
        ya = b.get('yes_ask', 0) or 0
        nb = b.get('no_bid', 0) or 0
        na = b.get('no_ask', 0) or 0
        
        mp = trader.bracket_probability(current, fs, cs, mins, asset, impl_vol)
        
        edge_y = (mp - ya/100.0) * 100 if ya > 0 else 0
        edge_n = ((1-mp) - na/100.0) * 100 if na > 0 else 0
        
        is_atm = " <<<" if atm and b.get('ticker') == atm.get('ticker') else ""
        
        fs_str = f"${fs:,.0f}" if fs else "(-inf)"
        cs_str = f"${cs:,.0f}" if cs else "(+inf)"
        
        flag = ""
        if edge_y >= 5: flag = " ** YES"
        elif edge_n >= 5 and (1-mp) >= 0.8: flag = " ** NO"
        
        print(f"{fs_str:>10} {cs_str:>10} {mp*100:>7.1f}% {yb:>5} {ya:>5} {nb:>5} {na:>5} {edge_y:>+6.1f}% {edge_n:>+6.1f}%{is_atm}{flag}")

# Summary of actual tradeable opportunities
print(f"\n{'='*80}")
print(f"TRADEABLE OPPORTUNITIES (edge >= 5%, confidence >= 80%):")
print(f"{'='*80}")
trader.min_edge_pct = 5.0
trader.min_confidence = 0.80
real_opps = [o for o in opps if o['edge_pct'] >= 5.0 and o['model_prob'] >= 0.80]
if real_opps:
    for o in real_opps[:10]:
        fs = f"${o['floor']:,.0f}" if o['floor'] else "(-inf)"
        cs = f"${o['cap']:,.0f}" if o['cap'] else "(+inf)"
        print(f"  {o['side'].upper()} {o['ticker']}  @ {o['price']}c  edge={o['edge_pct']:+.1f}%  model={o['model_prob']*100:.1f}%  bracket={fs}-{cs}")
else:
    print("  None currently")
print()
