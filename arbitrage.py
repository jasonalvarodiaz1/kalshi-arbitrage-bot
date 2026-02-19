import logging
import time
from typing import Dict, List, Optional
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import Config
from notifications import NotificationManager
from api import KalshiAPI

logger = logging.getLogger('kalshi_bot')

class KalshiArbitrageBot:
    """Arbitrage detection bot for Kalshi"""
    
    def __init__(self, api: KalshiAPI, min_profit_percent: float = 2.0, storage=None):
        self.api = api
        self.min_profit_percent = min_profit_percent
        self.opportunities_found = []
        self.notifier = NotificationManager()
        self.storage = storage  # Optional storage backend
    
    def _check_expiry(self, market: Dict) -> bool:
        """Return True if market settles within the allowed time window.
        
        Rejects markets that:
          - Close too soon (< MIN_EXPIRY_MINUTES)
          - Close too far out (> MAX_EXPIRY_HOURS) — we want same-day settlement
        """
        close_time_str = market.get('close_time') or market.get('expiration_time')
        if not close_time_str:
            return False  # No close time = can't verify settlement window
        try:
            close_time = datetime.fromisoformat(close_time_str.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            minutes_remaining = (close_time - now).total_seconds() / 60
            if minutes_remaining < Config.MIN_EXPIRY_MINUTES:
                return False
            max_minutes = Config.MAX_EXPIRY_HOURS * 60
            if minutes_remaining > max_minutes:
                return False  # Settles too far in the future
        except (ValueError, TypeError):
            return False
        return True

    def _is_crypto_market(self, market: Dict) -> bool:
        """Check if market is a short-duration crypto interval market."""
        ticker = market.get('ticker', '')
        series = market.get('series_ticker', '')
        return any(prefix in (ticker, series) for prefix in ('KXBTC', 'KXETH', 'KXSOL', 'KXDOGE'))

    def _get_market_thresholds(self, market: Dict) -> Dict:
        """Return adaptive filter thresholds based on market type.
        
        Crypto 15-min markets need relaxed volume/price floors because they
        launch fresh every 15 minutes with 0 volume. The orderbook depth
        check (MIN_QTY_AT_BEST) downstream still protects against empty books.
        """
        if self._is_crypto_market(market):
            # Check duration — only relax for short-duration markets
            close_time_str = market.get('close_time') or market.get('expiration_time')
            if close_time_str:
                try:
                    close_time = datetime.fromisoformat(close_time_str.replace('Z', '+00:00'))
                    minutes_remaining = (close_time - datetime.now(timezone.utc)).total_seconds() / 60
                    if minutes_remaining <= 60:  # Short-duration crypto
                        return {
                            'min_volume': 0,
                            'min_price_cents': 1,  # Allow 1¢ orders on crypto
                            'min_qty_at_best': 1,   # Less depth required
                        }
                except (ValueError, TypeError):
                    pass
        
        # Default thresholds for non-crypto / longer-duration markets
        return {
            'min_volume': self.MIN_VOLUME,        # 10
            'min_price_cents': self.MIN_PRICE_CENTS,  # 3
            'min_qty_at_best': self.MIN_QTY_AT_BEST,  # 2
        }

    def _walk_orderbook(self, asks: List[List], target_qty: int) -> Optional[Dict]:
        """Walk an orderbook to find the volume-weighted average fill price for target_qty.
        
        Args:
            asks: List of [price, quantity] pairs, sorted by price ascending
            target_qty: Number of contracts to fill
        
        Returns:
            Dict with 'avg_price', 'total_cost', 'filled_qty', 'levels_used',
            or None if not enough depth.
        """
        if not asks:
            return None
        
        # Sort asks by price ascending (best first)
        sorted_asks = sorted(asks, key=lambda a: a[0])
        
        filled = 0
        total_cost = 0
        levels_used = 0
        
        for price, qty in sorted_asks:
            can_fill = min(qty, target_qty - filled)
            total_cost += price * can_fill
            filled += can_fill
            levels_used += 1
            
            if filled >= target_qty:
                break
        
        if filled == 0:
            return None
        
        return {
            'avg_price': total_cost / filled,
            'total_cost': total_cost,
            'filled_qty': filled,
            'levels_used': levels_used,
            'fully_filled': filled >= target_qty,
        }

    def _max_executable_qty(self, orderbook: Dict, side: str = 'both') -> int:
        """Calculate maximum executable quantity from orderbook depth.
        
        For arbitrage we need to buy both YES and NO, so the max quantity
        is limited by the thinnest side's available volume at the best price.
        """
        try:
            if side in ('both', 'yes'):
                yes_asks = orderbook.get('yes_asks', [])
                if not yes_asks:
                    return 0
                best_yes = min(yes_asks, key=lambda x: x[0])
                yes_qty = best_yes[1] if len(best_yes) > 1 else 1
            else:
                yes_qty = float('inf')

            if side in ('both', 'no'):
                no_asks = orderbook.get('no_asks', [])
                if not no_asks:
                    return 0
                best_no = min(no_asks, key=lambda x: x[0])
                no_qty = best_no[1] if len(best_no) > 1 else 1
            else:
                no_qty = float('inf')

            return int(min(yes_qty, no_qty))
        except Exception:
            return 1

    def analyze_market_mispricing(self, market: Dict, orderbook: Dict) -> Optional[Dict]:
        """
        Detect mispricing in a single market
        YES + NO should equal 100 cents, look for deviations.
        Applies strict filters to avoid illiquid traps.
        """
        try:
            thresholds = self._get_market_thresholds(market)
            
            yes_asks = orderbook.get('yes_asks', [])
            no_asks = orderbook.get('no_asks', [])
            
            if not yes_asks or not no_asks:
                return None
            
            best_yes_ask = min([ask[0] for ask in yes_asks])
            best_no_ask = min([ask[0] for ask in no_asks])

            # Filter: ignore penny orders (adaptive threshold)
            if best_yes_ask < thresholds['min_price_cents'] or best_no_ask < thresholds['min_price_cents']:
                return None
            
            total_cost = best_yes_ask + best_no_ask
            guaranteed_payout = 100
            
            profit = guaranteed_payout - total_cost
            if total_cost <= 0:
                return None
            profit_percent = (profit / total_cost) * 100
            
            # Must be profitable but also realistic (not a 4900% illiquid trap)
            if profit_percent > self.MAX_PROFIT_PERCENT:
                return None  # Too good to be true — illiquid market

            if not self._check_expiry(market):
                return None

            max_qty = self._max_executable_qty(orderbook)

            # Must have enough depth to actually execute (adaptive threshold)
            if max_qty < thresholds['min_qty_at_best']:
                return None
            
            # If profitable at top of book, return immediately
            if profit_percent > self.min_profit_percent:
                return {
                    'ticker': market.get('ticker'),
                    'title': market.get('title'),
                    'yes_price': best_yes_ask,
                    'no_price': best_no_ask,
                    'total_cost': total_cost,
                    'profit_cents': profit,
                    'profit_percent': profit_percent,
                    'max_executable_qty': max_qty,
                    'strategy': 'Buy both YES and NO, guaranteed profit on settlement',
                    'timestamp': datetime.now().isoformat()
                }
            
            # --- Depth-based arb check ---
            # If top-of-book isn't profitable, check if walking the book reveals arb
            if total_cost >= 100 and total_cost <= 105:
                walk_qty = max(Config.MIN_ORDER_QUANTITY, thresholds.get('min_qty_at_best', self.MIN_QTY_AT_BEST))
                yes_walk = self._walk_orderbook(yes_asks, walk_qty)
                no_walk = self._walk_orderbook(no_asks, walk_qty)
                
                if yes_walk and no_walk and yes_walk['fully_filled'] and no_walk['fully_filled']:
                    walked_total = yes_walk['avg_price'] + no_walk['avg_price']
                    walked_profit = 100 - walked_total
                    if walked_total > 0 and walked_profit > 0:
                        walked_profit_pct = (walked_profit / walked_total) * 100
                        if walked_profit_pct > self.min_profit_percent and walked_profit_pct <= self.MAX_PROFIT_PERCENT:
                            return {
                                'ticker': market.get('ticker'),
                                'title': market.get('title'),
                                'yes_price': round(yes_walk['avg_price'], 2),
                                'no_price': round(no_walk['avg_price'], 2),
                                'total_cost': round(walked_total, 2),
                                'profit_cents': round(walked_profit, 2),
                                'profit_percent': walked_profit_pct,
                                'max_executable_qty': min(yes_walk['filled_qty'], no_walk['filled_qty']),
                                'strategy': f'Depth arb: VWAP YES={yes_walk["avg_price"]:.1f}¢ + NO={no_walk["avg_price"]:.1f}¢ across {yes_walk["levels_used"]+no_walk["levels_used"]} levels',
                                'timestamp': datetime.now().isoformat(),
                                'depth_based': True,
                            }
            
            return None
            
        except Exception as e:
            print(f"Error analyzing market: {e}")
            return None

    def analyze_event_arbitrage(self, event_markets: List[Dict]) -> Optional[Dict]:
        """Detect multi-outcome event arbitrage.
        
        In an event with N mutually exclusive outcomes, exactly one resolves YES.
        If the sum of best YES ask prices across all outcomes < 100¢,
        buying YES on every outcome guarantees profit.
        """
        if len(event_markets) < 2:
            return None

        try:
            legs = []
            total_cost = 0
            min_qty = float('inf')

            for market in event_markets:
                thresholds = self._get_market_thresholds(market)
                
                ticker = market.get('ticker')
                orderbook = self.api.get_orderbook(ticker)
                if not orderbook:
                    return None

                yes_asks = orderbook.get('yes_asks', [])
                if not yes_asks:
                    return None

                best_ask = min([a[0] for a in yes_asks])

                # Filter: ignore penny orders (adaptive threshold)
                if best_ask < thresholds['min_price_cents']:
                    return None

                # Volume available at best ask
                best_ask_qty = next((a[1] for a in yes_asks if a[0] == best_ask), 1)
                if best_ask_qty < thresholds['min_qty_at_best']:
                    return None

                min_qty = min(min_qty, best_ask_qty)

                total_cost += best_ask
                legs.append({
                    'ticker': ticker,
                    'title': market.get('title', ''),
                    'yes_price': best_ask,
                    'available_qty': best_ask_qty
                })

                if not self._check_expiry(market):
                    return None

                time.sleep(Config.RATE_LIMIT_DELAY)  # Rate limit between orderbook calls

            guaranteed_payout = 100  # Exactly one outcome pays $1
            profit = guaranteed_payout - total_cost
            if total_cost <= 0:
                return None
            profit_percent = (profit / total_cost) * 100

            # Must be profitable but realistic
            if profit_percent <= self.min_profit_percent:
                return None
            if profit_percent > self.MAX_PROFIT_PERCENT:
                return None  # Illiquid trap

            if profit_percent > self.min_profit_percent:
                return {
                    'type': 'multi_leg',
                    'event_ticker': event_markets[0].get('event_ticker', 'unknown'),
                    'num_legs': len(legs),
                    'legs': legs,
                    'total_cost': total_cost,
                    'profit_cents': profit,
                    'profit_percent': profit_percent,
                    'max_executable_qty': int(min_qty),
                    'strategy': f'Buy YES on all {len(legs)} outcomes — exactly one pays $1',
                    'timestamp': datetime.now().isoformat()
                }
            return None

        except Exception as e:
            print(f"Error analyzing event arbitrage: {e}")
            return None
    
    def scan_all_markets(self, category_filter: Optional[str] = None) -> List[Dict]:
        """Scan all markets for arbitrage opportunities"""
        print(f"🔍 Scanning Kalshi markets...")
        
        markets = self.api.get_all_markets(status="open")
        print(f"Found {len(markets)} open markets")
        
        opportunities = []
        scanned = 0
        
        for market in markets:
            try:
                ticker = market.get('ticker')
                title = market.get('title', '')
                
                if category_filter and category_filter.upper() not in title.upper():
                    continue
                
                scanned += 1
                print(f"Scanning {scanned}: {ticker} - {title[:50]}...")
                
                orderbook = self.api.get_orderbook(ticker)
                
                if not orderbook:
                    continue
                
                opportunity = self.analyze_market_mispricing(market, orderbook)
                
                if opportunity:
                    opportunities.append(opportunity)
                    print(f"  ✅ OPPORTUNITY FOUND!")
                    print(f"     Profit: {opportunity['profit_cents']} cents ({opportunity['profit_percent']:.2f}%)")
                    self.notifier.notify_opportunity(opportunity)
                    
                    # Save to storage if available
                    if self.storage:
                        self.storage.save_opportunity(opportunity)
                
                time.sleep(Config.RATE_LIMIT_DELAY)
                
            except Exception as e:
                print(f"  Error scanning {ticker}: {e}")
                continue
        
        return opportunities
    
    # Minimum thresholds to filter out illiquid trap markets
    MIN_PRICE_CENTS = 3       # Ignore asks below 3¢ (stale penny orders)
    MIN_VOLUME = 10           # Market must have at least this many trades
    MAX_PROFIT_PERCENT = 15.0 # Cap: real arb is 0.5-10%, not 4900%
    MIN_QTY_AT_BEST = 2       # Must have ≥2 contracts at best ask

    def _prefilter_markets(self, markets: List[Dict]) -> List[Dict]:
        """Pre-filter markets using listing data to avoid fetching orderbooks for dead markets.
        
        The /markets response includes yes_ask, no_ask, volume, liquidity fields.
        We only deep-scan markets that:
         1. Settle within our time window (MAX_EXPIRY_HOURS)
         2. Have both yes_ask and no_ask above MIN_PRICE_CENTS (adaptive for crypto)
         3. Have meaningful volume or open interest (adaptive for crypto)
         4. Quick-check: yes_ask + no_ask < 100 (potential arb)
        """
        candidates = []
        now = datetime.now(timezone.utc)
        min_minutes = Config.MIN_EXPIRY_MINUTES
        max_minutes = Config.MAX_EXPIRY_HOURS * 60

        for m in markets:
            # --- Time filter first (cheapest check, eliminates most markets) ---
            close_time_str = m.get('close_time') or m.get('expiration_time')
            if not close_time_str:
                continue
            try:
                close_time = datetime.fromisoformat(close_time_str.replace('Z', '+00:00'))
                minutes_remaining = (close_time - now).total_seconds() / 60
                if minutes_remaining < min_minutes or minutes_remaining > max_minutes:
                    continue
            except (ValueError, TypeError):
                continue

            thresholds = self._get_market_thresholds(m)
            
            yes_ask = m.get('yes_ask') or 0
            no_ask = m.get('no_ask') or 0
            volume = m.get('volume') or 0
            open_interest = m.get('open_interest') or 0

            # Must have both sides quoted above minimum price (adaptive)
            if yes_ask < thresholds['min_price_cents'] or no_ask < thresholds['min_price_cents']:
                continue

            # Must have real trading activity (adaptive — relaxed for crypto)
            if volume < thresholds['min_volume'] and open_interest < thresholds['min_volume']:
                continue

            # Quick arb pre-screen: total cost must be below payout
            # Allow 5¢ buffer since orderbook depth might have better prices
            if yes_ask + no_ask <= 105:
                candidates.append(m)

        return candidates

    def scan_all_markets_concurrent(self, category_filter: Optional[str] = None, max_workers: int = 10) -> List[Dict]:
        """Scan all markets using concurrent threads for speed."""
        print(f"🔍 Scanning Kalshi markets (concurrent, {max_workers} workers)...")

        markets = self.api.get_all_markets(status="open")
        print(f"Found {len(markets)} total open markets")

        if category_filter:
            markets = [m for m in markets if category_filter.upper() in m.get('title', '').upper()]
            print(f"Filtered to {len(markets)} markets matching '{category_filter}'")

        # Pre-filter to liquid, potentially profitable markets
        candidates = self._prefilter_markets(markets)
        print(f"🎯 {len(candidates)} markets pass pre-filter (both sides quoted, liquid, near arb)")

        opportunities = []

        def _scan_one(market):
            ticker = market.get('ticker')
            try:
                orderbook = self.api.get_orderbook(ticker)
                if not orderbook:
                    return None
                return self.analyze_market_mispricing(market, orderbook)
            except Exception as e:
                print(f"Error scanning {ticker}: {e}")
                return None

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_scan_one, m): m for m in candidates}
            scanned = 0
            for future in as_completed(futures):
                scanned += 1
                market = futures[future]
                ticker = market.get('ticker')
                if scanned % 100 == 0 or scanned == len(candidates):
                    print(f"Progress: {scanned}/{len(candidates)} markets scanned...")
                try:
                    result = future.result()
                    if result:
                        opportunities.append(result)
                        print(f"  ✅ OPPORTUNITY: {ticker} — {result['profit_cents']}¢ ({result['profit_percent']:.2f}%)")
                        self.notifier.notify_opportunity(result)
                except Exception as e:
                    print(f"  Error processing {ticker}: {e}")

        # --- Multi-outcome event arbitrage ---
        # Group markets by event_ticker, but only those within our time window
        # Only check events where at least 2 markets have nonzero yes_ask
        events = {}
        now_utc = datetime.now(timezone.utc)
        min_mins = Config.MIN_EXPIRY_MINUTES
        max_mins = Config.MAX_EXPIRY_HOURS * 60
        for m in markets:
            et = m.get('event_ticker')
            if not et or (m.get('yes_ask') or 0) <= 0:
                continue
            # Apply same time window filter
            ct = m.get('close_time') or m.get('expiration_time')
            if not ct:
                continue
            try:
                ct_dt = datetime.fromisoformat(ct.replace('Z', '+00:00'))
                mins_left = (ct_dt - now_utc).total_seconds() / 60
                if mins_left < min_mins or mins_left > max_mins:
                    continue
            except (ValueError, TypeError):
                continue
            events.setdefault(et, []).append(m)

        multi_events = {k: v for k, v in events.items() if len(v) >= 2}
        
        # Quick pre-filter: sum of yes_asks across legs should be < 105 to be worth checking
        promising_events = {}
        for et, ev_markets in multi_events.items():
            total_yes = sum(m.get('yes_ask', 0) for m in ev_markets)
            if total_yes < 105:
                promising_events[et] = ev_markets

        if promising_events:
            print(f"\n🔗 Scanning {len(promising_events)} promising multi-outcome events...")
            for event_ticker, event_markets in promising_events.items():
                try:
                    result = self.analyze_event_arbitrage(event_markets)
                    if result:
                        opportunities.append(result)
                        print(f"  ✅ EVENT ARB: {event_ticker} — {result['num_legs']} legs, {result['profit_cents']}¢ ({result['profit_percent']:.2f}%)")
                        self.notifier.notify_opportunity(result)
                except Exception as e:
                    print(f"  Error scanning event {event_ticker}: {e}")

        return opportunities
    
    def monitor_specific_markets(self, tickers: List[str], interval: int = 60):
        """Monitor specific markets continuously"""
        print(f"👀 Monitoring {len(tickers)} markets every {interval}s")
        print(f"Markets: {', '.join(tickers)}")
        
        iteration = 0
        
        try:
            while True:
                iteration += 1
                print(f"\n{'='*60}")
                print(f"Iteration {iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*60}")
                
                for ticker in tickers:
                    try:
                        market = self.api.get_market(ticker)
                        if not market:
                            continue
                        
                        orderbook = self.api.get_orderbook(ticker)
                        opportunity = self.analyze_market_mispricing(market, orderbook)
                        
                        if opportunity:
                            print(f"\n🎯 ARBITRAGE OPPORTUNITY!")
                            print(f"Market: {opportunity['title']}")
                            print(f"YES price: {opportunity['yes_price']} cents")
                            print(f"NO price: {opportunity['no_price']} cents")
                            print(f"Total cost: {opportunity['total_cost']} cents")
                            print(f"Profit: {opportunity['profit_cents']} cents ({opportunity['profit_percent']:.2f}%)")
                            print(f"Strategy: {opportunity['strategy']}")
                             
                            self.opportunities_found.append(opportunity)
                            self.notifier.notify_opportunity(opportunity)
                        else:
                            print(f"✓ {ticker}: No opportunity (spread too small)")
                        
                        time.sleep(Config.RATE_LIMIT_DELAY)
                        
                    except Exception as e:
                        print(f"Error monitoring {ticker}: {e}")
                
                print(f"\nWaiting {interval}s until next scan...")
                print(f"Total opportunities found: {len(self.opportunities_found)}")
                time.sleep(interval)
                
        except KeyboardInterrupt:
            print(f"\n\n{'='*60}")
            print(f"Monitoring stopped by user")
            print(f"Total opportunities found: {len(self.opportunities_found)}")
            print(f"{'='*60}")


