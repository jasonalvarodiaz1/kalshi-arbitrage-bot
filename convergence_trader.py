"""
Near-Expiry Convergence Trader for Kalshi Crypto Bracket Markets.

Strategy: Focus on bracket markets expiring within 60 minutes where the 
outcome is becoming clear. When BTC/ETH is deeply inside or outside a bracket 
with little time remaining, the market price should converge to ~100 or ~0.
If it hasn't caught up, trade the convergence.

Key insight: These are BRACKET markets (e.g. "BTC between $68,500-$68,749.99"),
NOT simple binary above/below. Each event has many brackets that sum to 100%.
We use floor_strike and cap_strike from the API, not parsed ticker direction.

Model: P(floor < price_at_expiry < cap) = CDF(z_cap) - CDF(z_floor)
where z = ln(price/strike) / (vol * sqrt(T))
We derive implied vol from the ATM bracket to calibrate the model.
"""

import math
import time
import logging
import requests
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

try:
    from scipy.stats import norm
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

from config import Config
from kelly import kelly_fraction

logger = logging.getLogger('kalshi_bot')


class ConvergenceTrader:
    """Near-expiry convergence strategy for crypto bracket markets."""

    def __init__(self, api, config=None):
        self.api = api
        self.config = config or Config
        self.price_cache = {}  # {asset: (price, timestamp)}
        self.price_cache_ttl = 15  # Refresh price every 15 sec to avoid rate limits
        
        # Convergence parameters
        self.max_expiry_minutes = 60      # Only trade markets expiring within this window
        self.min_expiry_minutes = 2       # Don't trade if <2 min (settlement risk)
        self.min_confidence = 0.30        # Min model probability to trade
        self.min_edge_pct = 2.0           # Min edge % to trade
        self.max_trade_usd = float(getattr(self.config, 'MAX_TRADE_USD', 20.0))
        self.max_contracts_per_trade = 50 # Cap contracts per trade
        
        # Stale order detection
        self.last_prices = {}  # Track {asset: (price, timestamp)} between scans
        self.price_move_threshold = 0.002  # 0.2% move triggers stale check
        
        # Vol estimates (will be overridden by implied vol when possible)
        self.base_vol = {
            'BTC': 0.0015,  # 0.15% per 15-min (calibrated from market data)
            'ETH': 0.0020,  # 0.20% per 15-min
        }
        
        logger.info("ConvergenceTrader initialized | max_expiry=%dmin | min_edge=%.1f%% | min_conf=%.0f%%",
                     self.max_expiry_minutes, self.min_edge_pct, self.min_confidence * 100)

    # ── Price feeds ──────────────────────────────────────────────────────

    def get_price(self, asset: str) -> Optional[float]:
        """Fetch current BTC/ETH price from CoinGecko with caching."""
        if asset in self.price_cache:
            price, ts = self.price_cache[asset]
            if time.time() - ts < self.price_cache_ttl:
                return price
        # Try batch fetch
        return self._fetch_prices_batch().get(asset)

    def _fetch_prices_batch(self) -> Dict[str, float]:
        """Fetch BTC + ETH prices in a single API call. Caches both."""
        # Check if either cache is still fresh
        now = time.time()
        all_fresh = all(
            asset in self.price_cache and now - self.price_cache[asset][1] < self.price_cache_ttl
            for asset in ['BTC', 'ETH']
        )
        if all_fresh:
            return {a: self.price_cache[a][0] for a in ['BTC', 'ETH'] if a in self.price_cache}

        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={'ids': 'bitcoin,ethereum', 'vs_currencies': 'usd'},
                timeout=5
            )
            if r.status_code == 429:
                logger.debug("CoinGecko rate limited, using cached prices")
                return {a: self.price_cache[a][0] for a in ['BTC', 'ETH'] if a in self.price_cache}
            r.raise_for_status()
            data = r.json()
            result = {}
            for asset, coin_id in [('BTC', 'bitcoin'), ('ETH', 'ethereum')]:
                price = data.get(coin_id, {}).get('usd')
                if price:
                    self.price_cache[asset] = (price, now)
                    result[asset] = price
            return result
        except Exception as e:
            logger.debug("Price fetch error: %s, using cache", e)
            return {a: self.price_cache[a][0] for a in ['BTC', 'ETH'] if a in self.price_cache}

    # ── Probability model ────────────────────────────────────────────────

    @staticmethod
    def _cdf(x: float) -> float:
        """Standard normal CDF."""
        if SCIPY_AVAILABLE:
            return float(norm.cdf(x))
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    def bracket_probability(self, current_price: float, floor_strike: float,
                            cap_strike: float, minutes_left: float,
                            asset: str, implied_vol: Optional[float] = None) -> float:
        """
        Estimate P(floor < price_at_expiry < cap) using log-normal model.
        
        Args:
            current_price: Current BTC/ETH price
            floor_strike: Lower bound of bracket (None = unbounded below)
            cap_strike: Upper bound of bracket (None = unbounded above)
            minutes_left: Minutes until market close
            asset: 'BTC' or 'ETH'
            implied_vol: Override vol estimate (per-15-min basis)
            
        Returns:
            Probability between 0 and 1
        """
        if minutes_left <= 0 or current_price <= 0:
            return 0.0

        # Get volatility and scale to time remaining
        vol_15m = implied_vol or self.base_vol.get(asset, 0.002)
        # Scale vol from 15-min base to actual time: sigma = vol_15m * sqrt(min/15)
        sigma = vol_15m * math.sqrt(minutes_left / 15.0)
        
        if sigma <= 0:
            sigma = 0.0001

        # CDF for upper bound
        if cap_strike and cap_strike > 0:
            z_cap = math.log(cap_strike / current_price) / sigma
            p_below_cap = self._cdf(z_cap)
        else:
            p_below_cap = 1.0  # No upper bound

        # CDF for lower bound
        if floor_strike and floor_strike > 0:
            z_floor = math.log(floor_strike / current_price) / sigma
            p_below_floor = self._cdf(z_floor)
        else:
            p_below_floor = 0.0  # No lower bound

        return max(0.0, min(1.0, p_below_cap - p_below_floor))

    def implied_vol_from_atm(self, current_price: float, atm_bracket: Dict,
                              minutes_left: float) -> Optional[float]:
        """
        Back out implied volatility from the ATM bracket's market price.
        Uses bisection search.
        
        Args:
            current_price: Current BTC/ETH price
            atm_bracket: Market dict for the bracket containing current price
            minutes_left: Minutes until expiry
            
        Returns:
            Implied vol (15-min basis), or None if can't calibrate
        """
        floor_s = atm_bracket.get('floor_strike')
        cap_s = atm_bracket.get('cap_strike')
        
        # Use mid-market or last price as reference
        yes_bid = atm_bracket.get('yes_bid', 0) or 0
        yes_ask = atm_bracket.get('yes_ask', 0) or 0
        
        if yes_bid > 0 and yes_ask > 0:
            market_prob = (yes_bid + yes_ask) / 200.0  # Average bid/ask in probability
        elif yes_ask > 0:
            market_prob = yes_ask / 100.0
        else:
            return None

        if market_prob <= 0.01 or market_prob >= 0.99:
            return None  # Can't calibrate from extreme prices

        # Bisection: find vol such that bracket_probability ≈ market_prob
        lo, hi = 0.0001, 0.05  # 0.01% to 5% per 15-min
        for _ in range(50):
            mid = (lo + hi) / 2
            p = self.bracket_probability(current_price, floor_s, cap_s, minutes_left, 'BTC', mid)
            if p < market_prob:
                hi = mid  # Lower vol concentrates probability → need lower vol to raise P? No.
                # Actually: lower vol → narrower distribution → if price is IN bracket, higher P
                # So if P is too low, vol is too high (distribution too wide). Lower hi.
                # Wait let me think again.
                # If current price is inside [floor, cap]:
                #   Lower vol → tighter distribution → MORE probability in bracket → higher P
                #   If P < market_prob, we need higher P → lower vol → decrease hi
                # That's correct only if price is inside bracket.
                # If price is outside bracket, lower vol → less tail → lower P
                # For ATM calibration, price IS inside bracket (that's the definition of ATM)
                hi = mid
            else:
                lo = mid
        
        return (lo + hi) / 2

    # ── Market scanning ──────────────────────────────────────────────────

    def _parse_asset(self, ticker: str) -> Optional[str]:
        """Extract asset (BTC/ETH) from ticker."""
        if 'KXBTC' in ticker:
            return 'BTC'
        elif 'KXETH' in ticker:
            return 'ETH'
        return None

    def _group_by_event(self, markets: List[Dict]) -> Dict[str, List[Dict]]:
        """Group markets by event_ticker."""
        events = {}
        for m in markets:
            et = m.get('event_ticker', 'unknown')
            events.setdefault(et, []).append(m)
        return events

    def _minutes_until_close(self, market: Dict) -> Optional[float]:
        """Calculate minutes until market close."""
        ct = market.get('close_time', '')
        if not ct:
            return None
        try:
            close = datetime.fromisoformat(ct.replace('Z', '+00:00'))
            return (close - datetime.now(timezone.utc)).total_seconds() / 60
        except (ValueError, TypeError):
            return None

    def detect_price_move(self, asset: str, current_price: float) -> Optional[float]:
        """
        Detect if the underlying price moved significantly since last scan.
        Returns the move as a fraction, or None if first scan.
        
        Large moves mean bracket market prices may be stale.
        """
        if asset in self.last_prices:
            old_price, _ = self.last_prices[asset]
            move = abs(current_price - old_price) / old_price
            self.last_prices[asset] = (current_price, time.time())
            return move
        self.last_prices[asset] = (current_price, time.time())
        return None

    def _find_atm_bracket(self, brackets: List[Dict], current_price: float) -> Optional[Dict]:
        """Find the bracket that contains the current price."""
        for b in brackets:
            floor_s = b.get('floor_strike')
            cap_s = b.get('cap_strike')
            # Handle edge brackets (no floor or no cap)
            if floor_s is None and cap_s is not None:
                if current_price < cap_s:
                    return b
            elif cap_s is None and floor_s is not None:
                if current_price >= floor_s:
                    return b
            elif floor_s is not None and cap_s is not None:
                if floor_s <= current_price < cap_s:
                    return b
        return None

    def scan_for_convergence(self) -> List[Dict]:
        """
        Scan crypto bracket markets for near-expiry convergence trades.
        
        Returns:
            List of opportunity dicts, sorted by edge (best first)
        """
        # Fetch crypto markets
        try:
            btc_markets = self.api.get_all_markets(status="open", series_ticker="KXBTC")
            eth_markets = self.api.get_all_markets(status="open", series_ticker="KXETH")
        except Exception as e:
            logger.error("Failed to fetch markets: %s", e)
            return []

        all_markets = btc_markets + eth_markets
        events = self._group_by_event(all_markets)

        # Get current prices
        btc_price = self.get_price('BTC')
        eth_price = self.get_price('ETH')
        
        if not btc_price and not eth_price:
            logger.error("Could not fetch any prices")
            return []

        prices = {'BTC': btc_price, 'ETH': eth_price}
        logger.info("Prices: BTC=$%s  ETH=$%s", 
                     f"{btc_price:,.0f}" if btc_price else "N/A",
                     f"{eth_price:,.0f}" if eth_price else "N/A")

        opportunities = []
        events_scanned = 0
        brackets_analyzed = 0

        for event_ticker, brackets in events.items():
            # Check time to expiry of first bracket (all share same close_time)
            mins_left = self._minutes_until_close(brackets[0])
            if mins_left is None:
                continue
            if mins_left > self.max_expiry_minutes or mins_left < self.min_expiry_minutes:
                continue

            events_scanned += 1
            asset = self._parse_asset(event_ticker)
            if not asset:
                continue

            current_price = prices.get(asset)
            if not current_price:
                continue

            # Sort brackets by floor_strike
            sorted_brackets = sorted(brackets, key=lambda b: b.get('floor_strike') or 0)

            # Find ATM bracket and calibrate implied vol
            atm = self._find_atm_bracket(sorted_brackets, current_price)
            impl_vol = None
            if atm:
                impl_vol = self.implied_vol_from_atm(current_price, atm, mins_left)
                if impl_vol:
                    logger.info("Event %s: implied vol = %.3f%% (15-min) from ATM bracket",
                                event_ticker, impl_vol * 100)
                else:
                    logger.debug("Could not calibrate implied vol for %s, using default", event_ticker)

            # Detect price moves that might create stale orders
            price_move = self.detect_price_move(asset, current_price)
            stale_boost = 0
            if price_move and price_move > self.price_move_threshold:
                stale_boost = min(price_move * 500, 3.0)  # Up to 3% edge boost
                logger.info("Event %s: %s moved %.2f%% since last scan → stale boost +%.1f%%", 
                            event_ticker, asset, price_move * 100, stale_boost)

            # Evaluate each bracket
            for bracket in sorted_brackets:
                brackets_analyzed += 1
                ticker = bracket.get('ticker', '')
                floor_s = bracket.get('floor_strike')
                cap_s = bracket.get('cap_strike')

                # Get market prices
                yes_ask = bracket.get('yes_ask', 0) or 0
                yes_bid = bracket.get('yes_bid', 0) or 0
                no_ask = bracket.get('no_ask', 0) or 0
                no_bid = bracket.get('no_bid', 0) or 0

                # Calculate model probability
                model_prob = self.bracket_probability(
                    current_price, floor_s, cap_s, mins_left, asset, impl_vol
                )

                # ── Check YES opportunity ──
                # Buy YES if model prob >> ask price
                if yes_ask > 0 and yes_ask < 95:
                    implied_prob = yes_ask / 100.0
                    edge = (model_prob - implied_prob) * 100 + stale_boost
                    min_edge = self.min_edge_pct
                    
                    # Tighter edge requirement for higher conviction
                    # If confidence >= 85%, accept smaller edge
                    if model_prob >= 0.85 and mins_left <= 15:
                        min_edge = max(1.5, self.min_edge_pct - 1.5)
                    
                    if edge >= min_edge and model_prob >= self.min_confidence:
                        opportunities.append({
                            'ticker': ticker,
                            'event': event_ticker,
                            'side': 'yes',
                            'price': yes_ask,
                            'model_prob': model_prob,
                            'implied_prob': implied_prob,
                            'edge_pct': edge,
                            'minutes_left': mins_left,
                            'asset': asset,
                            'current_price': current_price,
                            'floor': floor_s,
                            'cap': cap_s,
                            'impl_vol': impl_vol,
                            'liquidity': bracket.get('liquidity', 0),
                        })

                # ── Check NO opportunity ──
                # Buy NO if model says bracket is very unlikely
                model_prob_no = 1.0 - model_prob
                if no_ask > 0 and no_ask < 95:
                    implied_prob_no = no_ask / 100.0
                    edge_no = (model_prob_no - implied_prob_no) * 100 + stale_boost
                    min_edge = self.min_edge_pct
                    
                    if model_prob_no >= 0.85 and mins_left <= 15:
                        min_edge = max(1.5, self.min_edge_pct - 1.5)
                    
                    if edge_no >= self.min_edge_pct and model_prob_no >= self.min_confidence:
                        opportunities.append({
                            'ticker': ticker,
                            'event': event_ticker,
                            'side': 'no',
                            'price': no_ask,
                            'model_prob': model_prob_no,
                            'implied_prob': implied_prob_no,
                            'edge_pct': edge_no,
                            'minutes_left': mins_left,
                            'asset': asset,
                            'current_price': current_price,
                            'floor': floor_s,
                            'cap': cap_s,
                            'impl_vol': impl_vol,
                            'liquidity': bracket.get('liquidity', 0),
                        })

        logger.info("Scanned %d near-expiry events, %d brackets | Found %d opportunities",
                     events_scanned, brackets_analyzed, len(opportunities))

        # Sort by edge
        opportunities.sort(key=lambda o: o['edge_pct'], reverse=True)
        return opportunities

    # ── Position sizing ──────────────────────────────────────────────────

    def size_trade(self, opp: Dict, balance: float) -> int:
        """
        Calculate number of contracts to buy using half-Kelly.
        
        Returns:
            Number of contracts (0 if too small)
        """
        price_cents = opp['price']
        model_prob = opp['model_prob']
        
        # Kelly: f = (bp - q) / b where b = net_win/cost, p = win_prob, q = 1-p
        cost = price_cents / 100.0  # Dollar cost per contract
        net_win = 1.0 - cost        # Dollar profit if win
        
        if net_win <= 0 or cost <= 0:
            return 0
        
        # Use half-Kelly
        kelly_mult = getattr(self.config, 'KELLY_MULTIPLIER', 0.5)
        b = net_win / cost
        f = ((b * model_prob) - (1 - model_prob)) / b
        f = max(0, f * kelly_mult)
        
        # Dollar amount to risk
        risk_dollars = f * balance
        risk_dollars = min(risk_dollars, self.max_trade_usd)
        
        # Number of contracts
        contracts = int(risk_dollars / cost)
        contracts = min(contracts, self.max_contracts_per_trade)
        
        return max(0, contracts)

    # ── Auto-trade loop ──────────────────────────────────────────────────

    def auto_trade_loop(self, interval_seconds: int = 30):
        """
        Continuous scan + execute loop for convergence trades.
        
        Args:
            interval_seconds: Seconds between scan cycles
        """
        is_paper = self.config.PAPER_TRADING
        is_live = getattr(self.config, 'LIVE_TRADING_ENABLED', False)
        
        mode = "PAPER" if is_paper else ("LIVE" if is_live else "DRY-RUN")
        logger.info("=" * 70)
        logger.info("CONVERGENCE TRADER — %s MODE", mode)
        logger.info("Scanning for near-expiry bracket opportunities every %ds", interval_seconds)
        logger.info("Max expiry: %d min | Min edge: %.1f%% | Min confidence: %.0f%%",
                     self.max_expiry_minutes, self.min_edge_pct, self.min_confidence * 100)
        logger.info("Max trade: $%.2f | Kelly mult: %.1fx",
                     self.max_trade_usd, getattr(self.config, 'KELLY_MULTIPLIER', 0.5))
        logger.info("=" * 70)

        iteration = 0
        trades_attempted = 0
        trades_succeeded = 0
        paper_pnl = 0.0
        # Track traded tickers to avoid re-buying same bracket
        traded_tickers = {}  # {ticker: (side, timestamp)}

        try:
            while True:
                iteration += 1
                logger.info("-" * 50)
                logger.info("Scan #%d  |  %s", iteration, 
                           datetime.now().strftime('%H:%M:%S'))

                # Get balance
                try:
                    balance = self.api.get_balance()
                    logger.info("Balance: $%.2f", balance)
                except Exception:
                    balance = 250.0

                # Scan
                opps = self.scan_for_convergence()

                if opps:
                    for opp in opps[:3]:  # Trade up to 3 best per cycle
                        # Dedup: skip if we already traded this ticker+side
                        trade_key = (opp['ticker'], opp['side'])
                        if trade_key in traded_tickers:
                            prev_ts = traded_tickers[trade_key]
                            logger.debug("   Skip %s %s: already traded at %s",
                                         opp['ticker'], opp['side'],
                                         datetime.fromtimestamp(prev_ts).strftime('%H:%M:%S'))
                            continue

                        qty = self.size_trade(opp, balance)
                        cost = qty * opp['price'] / 100.0

                        logger.info("")
                        logger.info(">> %s  %s %s @ %dc",
                                     opp['ticker'], opp['side'].upper(), 
                                     f"x{qty}" if qty > 0 else "(skip)", opp['price'])
                        logger.info("   Model: %.1f%%  Market: %.1f%%  Edge: +%.1f%%  Mins: %.0f",
                                     opp['model_prob'] * 100, opp['implied_prob'] * 100,
                                     opp['edge_pct'], opp['minutes_left'])
                        floor_str = f"${opp['floor']:,.0f}" if opp['floor'] else "(-inf)"
                        cap_str = f"${opp['cap']:,.0f}" if opp['cap'] else "(+inf)"
                        logger.info("   Bracket: %s — %s  |  %s @ $%s",
                                     floor_str, cap_str, opp['asset'],
                                     f"{opp['current_price']:,.0f}")

                        if qty <= 0:
                            logger.info("   Skip: Kelly size = 0")
                            continue

                        trades_attempted += 1

                        if is_paper:
                            # Paper trade
                            logger.info("   PAPER BUY: %d contracts @ %dc = $%.2f",
                                         qty, opp['price'], cost)
                            trades_succeeded += 1
                            # Estimate P&L per contract:
                            # EV = model_prob * $1.00 (payout) - cost_per_contract
                            cost_per_contract = opp['price'] / 100.0
                            ev_per_contract = opp['model_prob'] * 1.0 - cost_per_contract
                            ev = qty * ev_per_contract
                            paper_pnl += ev
                            balance -= cost
                            # Mark as traded to prevent re-buying
                            traded_tickers[trade_key] = time.time()

                        elif is_live:
                            # Real trade
                            try:
                                logger.info("   LIVE ORDER: %d %s @ %dc on %s",
                                             qty, opp['side'].upper(), opp['price'], opp['ticker'])
                                order = self.api.place_order(
                                    ticker=opp['ticker'],
                                    side=opp['side'],
                                    quantity=qty,
                                    price=opp['price']
                                )
                                if order:
                                    logger.info("   FILLED: order_id=%s", order.get('order_id', '?'))
                                    trades_succeeded += 1
                                    traded_tickers[trade_key] = time.time()
                                else:
                                    logger.warning("   ORDER FAILED")
                            except Exception as e:
                                logger.error("   ORDER ERROR: %s", e)
                        else:
                            logger.info("   DRY-RUN (no execution)")
                else:
                    logger.info("No convergence opportunities this cycle")

                logger.info("")
                logger.info("Stats: %d/%d trades | paper_pnl=$%.2f | next scan in %ds",
                             trades_succeeded, trades_attempted, paper_pnl, interval_seconds)
                time.sleep(interval_seconds)

        except KeyboardInterrupt:
            logger.info("")
            logger.info("Convergence trader stopped")
            logger.info("Final: %d/%d trades | paper_pnl=$%.2f over %d iterations",
                         trades_succeeded, trades_attempted, paper_pnl, iteration)
