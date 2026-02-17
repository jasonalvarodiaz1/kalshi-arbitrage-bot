"""Probability-based trading engine for Kalshi crypto interval markets."""

import re
import time
import math
import logging
import requests
from typing import Dict, List, Optional
from datetime import datetime, timezone

try:
    from scipy.stats import norm
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

from config import Config
from kelly import kelly_fraction, size_position


logger = logging.getLogger('kalshi_bot')


class ProbabilityTrader:
    """Probability-based trading strategy for crypto interval markets."""
    
    def __init__(self, api, config=None):
        """
        Initialize probability trader.
        
        Args:
            api: KalshiAPI instance
            config: Config class (optional, uses global Config if not provided)
        """
        self.api = api
        self.config = config or Config
        self.price_cache = {}  # {asset: (price, timestamp)}
        
        # Default realized volatility estimates (15-min timeframe)
        # These are starting values - can be updated dynamically
        self.vol_estimates = {
            'BTC': getattr(self.config, 'BTC_15MIN_VOL', 0.004),  # 0.4% for 15 min
            'ETH': getattr(self.config, 'ETH_15MIN_VOL', 0.005)   # 0.5% for 15 min
        }
        
        self.price_cache_ttl = getattr(self.config, 'PRICE_CACHE_SECONDS', 10)
        self.min_edge_percent = getattr(self.config, 'MIN_EDGE_PERCENT', 3.0)
        self.kelly_multiplier = getattr(self.config, 'KELLY_MULTIPLIER', 0.5)
        
        logger.info("ProbabilityTrader initialized with vol estimates: %s", self.vol_estimates)
    
    def get_current_price(self, asset: str) -> Optional[float]:
        """
        Fetch current price from CoinGecko with caching.
        
        Args:
            asset: 'BTC' or 'ETH'
            
        Returns:
            Current price in USD, or None if unavailable
        """
        # Check cache first
        if asset in self.price_cache:
            price, timestamp = self.price_cache[asset]
            if time.time() - timestamp < self.price_cache_ttl:
                return price
        
        # Map asset to CoinGecko ID
        asset_map = {
            'BTC': 'bitcoin',
            'ETH': 'ethereum'
        }
        
        coin_id = asset_map.get(asset)
        if not coin_id:
            logger.warning("Unknown asset: %s", asset)
            return None
        
        try:
            url = f"https://api.coingecko.com/api/v3/simple/price"
            params = {
                'ids': coin_id,
                'vs_currencies': 'usd'
            }
            
            response = requests.get(url, params=params, timeout=5)
            response.raise_for_status()
            
            data = response.json()
            price = data.get(coin_id, {}).get('usd')
            
            if price:
                self.price_cache[asset] = (price, time.time())
                logger.debug("Fetched %s price: $%.2f", asset, price)
                return price
            else:
                logger.warning("Price not found in CoinGecko response for %s", asset)
                return None
                
        except Exception as e:
            logger.error("Error fetching price for %s: %s", asset, e)
            return None
    
    def parse_strike_from_ticker(self, ticker: str) -> Optional[Dict]:
        """
        Parse asset, strike price, and expiry from Kalshi ticker.
        
        Kalshi crypto tickers follow patterns like:
        - KXBTC-26FEB16-T98000 (BTC above $98,000)
        - KXBTC-26FEB16-B98000 (BTC below $98,000)
        - KXETH-26FEB16-T3500 (ETH above $3,500)
        
        Args:
            ticker: Kalshi market ticker
            
        Returns:
            Dict with keys: asset, strike, direction, expiry_str
            Or None if parsing fails
        """
        try:
            # Pattern: KXBTC-<DATE>-<T|B><STRIKE>
            # T = above (ticker), B = below
            # Date can be 6+ chars like 26FEB16 or 26FEB1717 (includes hour)
            # Strike can have decimals like T78249.99
            pattern = r'KX(BTC|ETH)-(\d{2}[A-Z]{3}\d{2,6})-([TB])(\d+(?:\.\d+)?)'
            match = re.match(pattern, ticker)
            
            if not match:
                return None
            
            asset = match.group(1)
            expiry_str = match.group(2)
            direction_code = match.group(3)
            strike_raw = match.group(4)
            
            # Convert strike to float (e.g., "98000" -> 98000.0)
            strike = float(strike_raw)
            
            # T = above, B = below
            direction = 'above' if direction_code == 'T' else 'below'
            
            return {
                'asset': asset,
                'strike': strike,
                'direction': direction,
                'expiry_str': expiry_str
            }
            
        except Exception as e:
            logger.debug("Failed to parse ticker %s: %s", ticker, e)
            return None
    
    def _normal_cdf(self, x: float) -> float:
        """
        Calculate cumulative distribution function of standard normal.
        Uses scipy if available, otherwise pure Python approximation.
        """
        if SCIPY_AVAILABLE:
            return norm.cdf(x)
        else:
            # Pure Python approximation using error function
            # CDF(x) = 0.5 * (1 + erf(x / sqrt(2)))
            return 0.5 * (1 + math.erf(x / math.sqrt(2)))
    
    def estimate_probability(self, current_price: float, strike: float, 
                            minutes_remaining: float, asset: str,
                            direction: str = 'above') -> float:
        """
        Estimate P(price stays above/below strike) using normal distribution model.
        
        Uses a simple geometric Brownian motion model:
        P(above_strike) = norm.cdf((ln(current/strike)) / (vol * sqrt(time)))
        
        Args:
            current_price: Current asset price
            strike: Strike price
            minutes_remaining: Minutes until expiry
            asset: 'BTC' or 'ETH'
            direction: 'above' or 'below'
            
        Returns:
            Estimated probability (0 to 1)
        """
        if minutes_remaining <= 0 or current_price <= 0 or strike <= 0:
            return 0.5  # No information
        
        # Get volatility estimate for this asset
        vol = self.vol_estimates.get(asset, 0.005)
        
        # Convert minutes to years for annualized vol calculation
        # Assume 15-min vol is given, scale to time remaining
        time_in_years = minutes_remaining / (365.25 * 24 * 60)
        
        # For very short timeframes, use the base 15-min vol
        # Scale vol by sqrt(time) for longer periods (standard volatility scaling)
        # Note: We scale from 15-min base, so vol_scaled = vol * sqrt(minutes_remaining / 15)
        vol_scaled = vol * (minutes_remaining / 15.0) ** 0.5 if minutes_remaining > 15 else vol
        
        # Calculate z-score
        if current_price == strike:
            z = 0
        else:
            log_moneyness = math.log(current_price / strike)
            volatility_term = vol_scaled * math.sqrt(time_in_years) if time_in_years > 0 else vol_scaled * 0.01
            
            # Avoid division by zero
            if volatility_term == 0:
                volatility_term = 0.0001
                
            z = log_moneyness / volatility_term
        
        # P(price > strike) = CDF(z)
        prob_above = self._normal_cdf(z)
        
        if direction == 'above':
            return prob_above
        else:
            return 1 - prob_above
    
    def calculate_edge(self, market: Dict, orderbook: Dict) -> Optional[Dict]:
        """
        Calculate expected value edge for a crypto interval market.
        
        Args:
            market: Market data from API
            orderbook: Orderbook data
            
        Returns:
            Opportunity dict with: ticker, side, price, implied_prob, 
            estimated_prob, edge, kelly_fraction, or None if no edge
        """
        ticker = market.get('ticker')
        
        # Parse ticker to extract strike and asset
        parsed = self.parse_strike_from_ticker(ticker)
        if not parsed:
            return None
        
        asset = parsed['asset']
        strike = parsed['strike']
        direction = parsed['direction']
        
        # Get current price
        current_price = self.get_current_price(asset)
        if not current_price:
            logger.warning("Could not fetch current price for %s", asset)
            return None
        
        # Calculate time remaining
        close_time_str = market.get('close_time') or market.get('expiration_time')
        if not close_time_str:
            return None
        
        try:
            close_time = datetime.fromisoformat(close_time_str.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            minutes_remaining = (close_time - now).total_seconds() / 60
            
            if minutes_remaining <= 0:
                return None
                
        except (ValueError, TypeError):
            return None
        
        # Get orderbook prices
        yes_asks = orderbook.get('yes_asks', [])
        no_asks = orderbook.get('no_asks', [])
        
        if not yes_asks or not no_asks:
            return None
        
        # Extract best prices (validate orderbook format)
        try:
            best_yes_price = min([ask[0] for ask in yes_asks])
            best_no_price = min([ask[0] for ask in no_asks])
        except (IndexError, TypeError):
            logger.warning("Invalid orderbook format for %s", ticker)
            return None
        
        # Estimate actual probability
        # For "above" markets, YES pays if price > strike
        # For "below" markets, YES pays if price < strike
        if direction == 'above':
            estimated_prob_yes = self.estimate_probability(
                current_price, strike, minutes_remaining, asset, 'above'
            )
        else:  # below
            estimated_prob_yes = self.estimate_probability(
                current_price, strike, minutes_remaining, asset, 'below'
            )
        
        estimated_prob_no = 1 - estimated_prob_yes
        
        # Calculate implied probabilities from market prices
        implied_prob_yes = best_yes_price / 100.0
        implied_prob_no = best_no_price / 100.0
        
        # Calculate edge for each side
        edge_yes = estimated_prob_yes - implied_prob_yes
        edge_no = estimated_prob_no - implied_prob_no
        
        # Determine which side to trade (if any)
        best_side = None
        best_edge = 0
        best_price = 0
        best_estimated_prob = 0
        best_implied_prob = 0
        
        if edge_yes > edge_no and edge_yes * 100 > self.min_edge_percent:
            best_side = 'yes'
            best_edge = edge_yes
            best_price = best_yes_price
            best_estimated_prob = estimated_prob_yes
            best_implied_prob = implied_prob_yes
        elif edge_no * 100 > self.min_edge_percent:
            best_side = 'no'
            best_edge = edge_no
            best_price = best_no_price
            best_estimated_prob = estimated_prob_no
            best_implied_prob = implied_prob_no
        
        if not best_side:
            return None
        
        # Calculate Kelly fraction
        win_amount = 1.0 - (best_price / 100.0)  # Profit if win
        loss_amount = best_price / 100.0  # Loss if lose
        kelly = kelly_fraction(best_estimated_prob, win_amount, loss_amount)
        
        # Get max quantity from orderbook
        if best_side == 'yes':
            max_qty = next((ask[1] for ask in yes_asks if ask[0] == best_yes_price), 1)
        else:
            max_qty = next((ask[1] for ask in no_asks if ask[0] == best_no_price), 1)
        
        return {
            'ticker': ticker,
            'title': market.get('title', ''),
            'side': best_side,
            'price': best_price,
            'implied_prob': best_implied_prob,
            'estimated_prob': best_estimated_prob,
            'edge': best_edge,
            'edge_percent': best_edge * 100,
            'kelly_fraction': kelly,
            'max_executable_qty': max_qty,
            'current_price': current_price,
            'strike': strike,
            'direction': direction,
            'asset': asset,
            'minutes_remaining': minutes_remaining,
            'timestamp': datetime.now().isoformat(),
            'strategy': f'Buy {best_side.upper()} - {asset} ${current_price:.0f} vs strike ${strike:.0f}'
        }
    
    def scan_crypto_markets(self) -> List[Dict]:
        """
        Scan all open crypto interval markets for probability edge opportunities.
        
        Returns:
            List of opportunity dicts
        """
        logger.info("Scanning crypto markets for probability edge...")
        
        # Fetch crypto markets directly using series_ticker filter
        btc_markets = self.api.get_all_markets(status="open", series_ticker="KXBTC")
        eth_markets = self.api.get_all_markets(status="open", series_ticker="KXETH")
        
        crypto_markets = btc_markets + eth_markets
        
        logger.info("Found %d crypto markets to scan (%d BTC + %d ETH)", 
                   len(crypto_markets), len(btc_markets), len(eth_markets))
        
        opportunities = []
        
        for market in crypto_markets:
            ticker = market.get('ticker')
            try:
                # Get orderbook
                orderbook = self.api.get_orderbook(ticker)
                if not orderbook:
                    continue
                
                # Calculate edge
                opportunity = self.calculate_edge(market, orderbook)
                
                if opportunity:
                    opportunities.append(opportunity)
                    logger.info("Found opportunity: %s - %.2f%% edge on %s side", 
                               ticker, opportunity['edge_percent'], opportunity['side'])
                
                # Rate limit
                time.sleep(0.3)
                
            except Exception as e:
                logger.error("Error scanning %s: %s", ticker, e)
                continue
        
        return opportunities
    
    def auto_trade_loop(self, interval_seconds: int = 15):
        """
        Continuous scanning + auto-execution loop for probability trades.
        
        Args:
            interval_seconds: Seconds between scan cycles
        """
        logger.info("Starting auto-trade loop for probability strategy (interval=%ds)", interval_seconds)
        logger.info("Paper trading: %s | Live trading: %s", self.config.PAPER_TRADING, self.config.LIVE_TRADING_ENABLED)
        
        iteration = 0
        total_trades_attempted = 0
        total_trades_succeeded = 0
        
        try:
            while True:
                iteration += 1
                logger.info("=" * 60)
                logger.info("Scan iteration %d", iteration)
                
                # Get current balance for Kelly sizing
                try:
                    balance = self.api.get_balance()
                    logger.info("Current balance: $%.2f", balance)
                except Exception as e:
                    logger.warning("Could not fetch balance: %s (using $250 default)", e)
                    balance = 250.0
                
                opportunities = self.scan_crypto_markets()
                
                if opportunities:
                    logger.info("Found %d probability opportunities", len(opportunities))
                    # Sort by edge (best first)
                    opportunities.sort(key=lambda o: o.get('edge_percent', 0), reverse=True)
                    
                    for opp in opportunities:
                        logger.info("")
                        logger.info("📈 OPPORTUNITY: %s", opp['ticker'])
                        logger.info("   Side: %s at %d¢", opp['side'].upper(), opp['contract_price'])
                        logger.info("   Edge: %.2f%%", opp['edge_percent'])
                        logger.info("   Est prob: %.1f%% | Implied prob: %.1f%%", 
                                   opp['estimated_prob'] * 100,
                                   opp['contract_price'])
                        logger.info("   Kelly quantity: %d contracts ($%.2f)", 
                                   opp['kelly_quantity'],
                                   opp['kelly_quantity'] * opp['contract_price'] / 100)
                        logger.info("   Time remaining: %.0f min", opp['minutes_remaining'])
                        
                        # Execute trade
                        if opp['kelly_quantity'] > 0:
                            total_trades_attempted += 1
                            
                            if self.config.PAPER_TRADING:
                                logger.info("   🎯 PAPER TRADE: BUY %d contracts", opp['kelly_quantity'])
                                logger.info("   📝 Simulated cost: $%.2f", 
                                           opp['kelly_quantity'] * opp['contract_price'] / 100)
                                total_trades_succeeded += 1
                            else:
                                # Real trade execution
                                try:
                                    logger.info("   💰 LIVE TRADE: Placing order...")
                                    order = self.api.place_order(
                                        ticker=opp['ticker'],
                                        side=opp['side'],
                                        quantity=opp['kelly_quantity'],
                                        price=opp['contract_price']
                                    )
                                    
                                    if order:
                                        logger.info("   ✅ Order placed successfully: %s", order.get('order_id'))
                                        total_trades_succeeded += 1
                                    else:
                                        logger.error("   ❌ Order failed")
                                        
                                except Exception as e:
                                    logger.error("   ❌ Trade execution error: %s", e)
                        else:
                            logger.info("   ⏭️  Skipping (Kelly size = 0)")
                else:
                    logger.info("No probability opportunities found this scan")
                
                logger.info("")
                logger.info("Session stats: %d/%d trades succeeded", total_trades_succeeded, total_trades_attempted)
                logger.info("Waiting %d seconds until next scan...", interval_seconds)
                logger.info("=" * 60)
                time.sleep(interval_seconds)
                
        except KeyboardInterrupt:
            logger.info("")
            logger.info("🛑 Auto-trade loop stopped by user")
            logger.info("Final stats: %d/%d trades succeeded over %d iterations", 
                       total_trades_succeeded, total_trades_attempted, iteration)

