"""Diagnostic: check 15-minute binary markets right now — prices, depth, model probs."""
import sys, os, math
sys.stdout.reconfigure(encoding='utf-8')

from kalshi_bot import KalshiAPI
from config import Config
from datetime import datetime, timezone

bot = KalshiAPI(api_key=Config.KALSHI_API_KEY)
now = datetime.now(timezone.utc)

# CDF helper
try:
    from scipy.stats import norm
    _cdf = norm.cdf
except ImportError:
    import math
    def _cdf(x):
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

# Base vols (same as ws_trader.py)
base_vol = {'BTC': 0.002, 'ETH': 0.003, 'DOGE': 0.005, 'XRP': 0.004, 'SOL': 0.004}

# Current prices
import requests
prices = {}
try:
    r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                     params={'ids': 'bitcoin,ethereum,dogecoin,ripple,solana', 'vs_currencies': 'usd'},
                     timeout=10)
    data = r.json()
    mapping = [('BTC','bitcoin'),('ETH','ethereum'),('DOGE','dogecoin'),('XRP','ripple'),('SOL','solana')]
    for asset, cid in mapping:
        if cid in data:
            prices[asset] = data[cid]['usd']
except Exception as e:
    print(f"Price fetch error: {e}")
    # Fallback
    try:
        r = requests.get("https://min-api.cryptocompare.com/data/pricemulti",
                         params={'fsyms': 'BTC,ETH,DOGE,XRP,SOL', 'tsyms': 'USD'}, timeout=10)
        data = r.json()
        for asset in ['BTC','ETH','DOGE','XRP','SOL']:
            if asset in data:
                prices[asset] = data[asset]['USD']
    except:
        pass

print(f"Current prices: {prices}")
print()

# Scan all crypto series
series_list = ['KXBTC', 'KXETH', 'KXDOGE', 'KXXRP',
               'KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXXRP15M']

for series in series_list:
    try:
        markets = bot.get_all_markets(status='open', series_ticker=series)
    except:
        continue
    if not markets:
        continue

    events = {}
    for m in markets:
        et = m.get('event_ticker', '')
        events.setdefault(et, []).append(m)

    for ev in sorted(events.keys()):
        mlist = events[ev]
        ct = mlist[0].get('close_time', '')
        try:
            close = datetime.fromisoformat(ct.replace('Z', '+00:00'))
            mins = (close - now).total_seconds() / 60
        except:
            mins = -1
        if mins <= 0 or mins > 60:
            continue

        # Find binary markets (cap_strike is None, floor_strike is not None)
        binaries = [m for m in mlist
                    if m.get('cap_strike') is None
                    and m.get('floor_strike') is not None
                    and m.get('strike_type', '').startswith('greater')]

        if not binaries:
            continue

        for b in binaries:
            ticker = b.get('ticker', '')
            strike = b.get('floor_strike', 0)
            # Figure out asset
            asset = None
            for a in ['BTC', 'ETH', 'DOGE', 'XRP', 'SOL']:
                if a in series.upper() or a in ev.upper():
                    asset = a
                    break
            if not asset or asset not in prices:
                continue
            price = prices[asset]

            # Model probability
            vol = base_vol.get(asset, 0.003) * 1.3
            sigma = vol * math.sqrt(mins / 15.0)
            if sigma > 0:
                z = math.log(strike / price) / sigma
                prob_yes = 1.0 - _cdf(z)
            else:
                prob_yes = 0.5
            prob_no = 1.0 - prob_yes

            # Get orderbook
            try:
                ob = bot.get_orderbook(ticker)
                yes_bids = [(l[0], l[1]) for l in ob.get('orderbook', {}).get('yes', [])]
                no_bids = [(l[0], l[1]) for l in ob.get('orderbook', {}).get('no', [])]
            except:
                yes_bids = []
                no_bids = []

            best_yes_bid = max((p for p, q in yes_bids), default=0)
            best_no_bid = max((p for p, q in no_bids), default=0)
            yes_ask = (100 - best_no_bid) if best_no_bid > 0 else 0
            no_ask = (100 - best_yes_bid) if best_yes_bid > 0 else 0

            # Depth
            yes_depth = 0
            for p, q in no_bids:
                if p == best_no_bid:
                    yes_depth = q
            no_depth = 0
            for p, q in yes_bids:
                if p == best_yes_bid:
                    no_depth = q

            # Calculate edges
            yes_edge = (prob_yes - yes_ask / 100.0) * 100 if yes_ask > 0 else 0
            no_edge = (prob_no - no_ask / 100.0) * 100 if no_ask > 0 else 0

            # Would it pass filters?
            MAX_PRICE = 35
            MIN_EDGE = 10.0
            MAX_EDGE = 30.0
            MIN_CONF = 0.65
            MIN_DEPTH = 5

            yes_pass = (yes_ask >= 4 and yes_ask <= MAX_PRICE
                        and yes_edge >= MIN_EDGE and yes_edge <= MAX_EDGE
                        and prob_yes >= MIN_CONF and yes_depth >= MIN_DEPTH)
            no_pass = (no_ask >= 4 and no_ask <= MAX_PRICE
                       and no_edge >= MIN_EDGE and no_edge <= MAX_EDGE
                       and prob_no >= MIN_CONF and no_depth >= MIN_DEPTH)

            status = ""
            if yes_pass:
                status = ">>> YES TRADEABLE <<<"
            elif no_pass:
                status = ">>> NO TRADEABLE <<<"
            else:
                # Why not?
                reasons = []
                for side, ask, edge, prob, depth in [
                    ('YES', yes_ask, yes_edge, prob_yes, yes_depth),
                    ('NO', no_ask, no_edge, prob_no, no_depth)
                ]:
                    if ask <= 0:
                        reasons.append(f"{side}: no price")
                    elif ask > MAX_PRICE:
                        reasons.append(f"{side}: price {ask}c > {MAX_PRICE}c")
                    elif depth < MIN_DEPTH:
                        reasons.append(f"{side}: depth {depth} < {MIN_DEPTH}")
                    elif edge < MIN_EDGE:
                        reasons.append(f"{side}: edge {edge:.1f}% < {MIN_EDGE}%")
                    elif edge > MAX_EDGE:
                        reasons.append(f"{side}: edge {edge:.1f}% > {MAX_EDGE}% (stale)")
                    elif prob < MIN_CONF:
                        reasons.append(f"{side}: conf {prob:.0%} < {MIN_CONF:.0%}")
                status = " | ".join(reasons)

            print(f"  {ticker}")
            print(f"    {asset} @ ${price:,.2f} | strike={strike:,.2f} | {mins:.0f}min left")
            print(f"    YES: ask={yes_ask}c depth={yes_depth} model={prob_yes:.1%} edge={yes_edge:+.1f}%")
            print(f"    NO:  ask={no_ask}c depth={no_depth} model={prob_no:.1%} edge={no_edge:+.1f}%")
            print(f"    {status}")
            print()
