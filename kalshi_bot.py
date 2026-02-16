import requests
import time
from typing import Dict, List, Optional
from datetime import datetime
from config import Config
from urllib.parse import urlparse
import base64
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.backends import default_backend
import logging
from logging.handlers import RotatingFileHandler
from storage import Storage

def setup_logging():
    logger = logging.getLogger('kalshi_bot')
    logger.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter('%(message)s'))

    file_handler = RotatingFileHandler('kalshi_bot.log', maxBytes=5*1024*1024, backupCount=5)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger

logger = setup_logging()

class KalshiAPI:
    """Wrapper for Kalshi Exchange API"""
    
    PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"  # Updated Feb 2026
    PROD_BASE_URL_OLD = "https://trading-api.kalshi.com/trade-api/v2"  # Deprecated
    DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
    
    def __init__(self, email: Optional[str] = None, password: Optional[str] = None, api_key: Optional[str] = None):
        self.email = email
        self.password = password
        self.api_key = api_key  # This is the KEY ID (UUID), not a bearer token
        self.token = None
        self.private_key = None  # Loaded RSA key object
        self.salt_length = asym_padding.PSS.DIGEST_LENGTH  # Will try MAX_LENGTH as fallback
        self.BASE_URL = self.PROD_BASE_URL
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        })

        # Load private key from file if available
        pk_pem = Config.load_private_key()
        if pk_pem:
            try:
                self.private_key = serialization.load_pem_private_key(
                    pk_pem.encode('utf-8'),
                    password=None,
                    backend=default_backend()
                )
                logger.info("✅ Private key loaded from file")
            except Exception as e:
                logger.error(f"❌ Failed to load private key: {e}")

        # Determine auth method
        if self.api_key and self.private_key:
            # Use Kalshi signed-header auth (per docs)
            # Test which base URL + salt length works
            if self._test_auth():
                logger.info(f"✅ Authenticated with signed headers (base: {self.BASE_URL})")
            else:
                logger.warning("⚠️  Signed-header auth failed on both prod and demo URLs with both salt lengths")
        elif email and password:
            self.login()

    def _test_auth(self) -> bool:
        """Try prod/demo URLs and both salt lengths to find working auth config."""
        urls = [self.PROD_BASE_URL, self.PROD_BASE_URL_OLD, self.DEMO_BASE_URL]
        salts = [asym_padding.PSS.DIGEST_LENGTH, asym_padding.PSS.MAX_LENGTH]

        for base_url in urls:
            for salt in salts:
                self.BASE_URL = base_url
                self.salt_length = salt
                salt_name = 'DIGEST_LENGTH' if salt == asym_padding.PSS.DIGEST_LENGTH else 'MAX_LENGTH'
                logger.debug(f"  Trying: {base_url} with salt={salt_name}...")

                path = urlparse(base_url).path.rstrip('/') + '/markets'
                headers = self._signed_headers('GET', path)
                if not headers:
                    logger.debug(f"    Skipped (no headers generated)")
                    continue

                try:
                    resp = self.session.get(f"{base_url}/markets", params={'limit': 1}, headers=headers)
                    logger.debug(f"    Response: {resp.status_code}")
                    if resp.status_code == 200:
                        return True
                    else:
                        # Show response body for debugging
                        body = resp.text[:200] if resp.text else '(empty)'
                        logger.debug(f"    Body: {body}")
                except Exception as e:
                    logger.debug(f"    Error: {e}")

        return False

    def _signed_headers(self, method: str, path: str) -> Dict[str, str]:
        """Generate Kalshi signed headers using RSA-PSS per docs.

        Args:
            method: HTTP method (GET/POST)
            path: request path without query (e.g., '/trade-api/v2/markets')
        """
        if not self.private_key or not self.api_key:
            return {}

        timestamp = str(int(time.time() * 1000))
        msg_string = timestamp + method + path
        msg = msg_string.encode('utf-8')

        signature = self.private_key.sign(
            msg,
            asym_padding.PSS(
                mgf=asym_padding.MGF1(hashes.SHA256()),
                salt_length=self.salt_length
            ),
            hashes.SHA256()
        )

        sig_b64 = base64.b64encode(signature).decode('ascii')

        return {
            'KALSHI-ACCESS-KEY': self.api_key,
            'KALSHI-ACCESS-TIMESTAMP': timestamp,
            'KALSHI-ACCESS-SIGNATURE': sig_b64
        }
    
    def _request_with_retry(self, method: str, url: str, max_retries: int = 3, **kwargs) -> Optional[requests.Response]:
        """Make an HTTP request with exponential backoff retry."""
        for attempt in range(max_retries):
            try:
                response = getattr(self.session, method.lower())(url, **kwargs)
                if response.status_code == 429:
                    wait = (2 ** attempt) + 1
                    logger.warning(f"Rate limited (429). Retrying in {wait}s (attempt {attempt + 1}/{max_retries})...")
                    time.sleep(wait)
                    continue
                if response.status_code >= 500:
                    wait = (2 ** attempt) + 1
                    logger.warning(f"Server error ({response.status_code}). Retrying in {wait}s (attempt {attempt + 1}/{max_retries})...")
                    time.sleep(wait)
                    continue
                return response
            except requests.exceptions.RequestException as e:
                wait = (2 ** attempt) + 1
                logger.warning(f"Request failed: {e}. Retrying in {wait}s (attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait)
        logger.error(f"All {max_retries} retry attempts failed for {method} {url}")
        return None
    
    def login(self) -> bool:
        """Authenticate with Kalshi"""
        try:
            endpoint = f"{self.BASE_URL}/login"
            payload = {
                "email": self.email,
                "password": self.password
            }
            response = self._request_with_retry('post', endpoint, json=payload)
            
            if response and response.status_code == 200:
                data = response.json()
                self.token = data.get('token')
                self.session.headers.update({
                    'Authorization': f'Bearer {self.token}'
                })
                logger.info("✅ Successfully logged in to Kalshi")
                return True
            else:
                logger.error(f"❌ Login failed: {response.status_code if response else 'No response'}")
                return False
        except Exception as e:
            logger.error(f"❌ Login error: {e}")
            return False
    
    def get_markets(self, status: str = "open", limit: int = 100, max_markets: int = 1000) -> List[Dict]:
        """Fetch active markets with automatic pagination."""
        all_markets = []
        cursor = None

        while len(all_markets) < max_markets:
            remaining = max_markets - len(all_markets)
            params = {'status': status, 'limit': min(limit, remaining)}
            if cursor:
                params['cursor'] = cursor

            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/markets"
            headers = self._signed_headers('GET', path)
            
            try:
                response = self._request_with_retry('get', f"{self.BASE_URL}/markets", params=params, headers=headers if headers else None)
                if response and response.status_code == 200:
                    data = response.json()
                    markets = data.get('markets', [])
                    all_markets.extend(markets)
                    cursor = data.get('cursor')
                    if not cursor or not markets:
                        break
                    time.sleep(Config.RATE_LIMIT_DELAY)
                else:
                    logger.error(f"Error fetching markets: {response.status_code if response else 'No response'}")
                    break
            except Exception as e:
                logger.error(f"Error: {e}")
                break

        return all_markets
    
    def get_market(self, ticker: str) -> Optional[Dict]:
        """Get specific market by ticker"""
        try:
            endpoint = f"{self.BASE_URL}/markets/{ticker}"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/markets/{ticker}"
            headers = self._signed_headers('GET', path)
            response = self._request_with_retry('get', endpoint, headers=headers if headers else None)
            
            if response and response.status_code == 200:
                return response.json().get('market')
            return None
        except Exception as e:
            logger.error(f"Error fetching market {ticker}: {e}")
            return None
    
    def get_orderbook(self, ticker: str) -> Dict:
        """Get orderbook for a market"""
        try:
            endpoint = f"{self.BASE_URL}/markets/{ticker}/orderbook"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/markets/{ticker}/orderbook"
            headers = self._signed_headers('GET', path)
            response = self._request_with_retry('get', endpoint, headers=headers if headers else None)
            
            if response and response.status_code == 200:
                data = response.json()
                return {
                    'yes_bids': data.get('yes', {}).get('bids', []),
                    'yes_asks': data.get('yes', {}).get('asks', []),
                    'no_bids': data.get('no', {}).get('bids', []),
                    'no_asks': data.get('no', {}).get('asks', []),
                    'timestamp': time.time()
                }
            return {}
        except Exception as e:
            logger.error(f"Error fetching orderbook: {e}")
            return {}
    
    def get_trades(self, ticker: str, limit: int = 100) -> List[Dict]:
        """Get recent trades for a market"""
        try:
            endpoint = f"{self.BASE_URL}/markets/{ticker}/trades"
            params = {'limit': limit}
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/markets/{ticker}/trades"
            headers = self._signed_headers('GET', path)
            response = self._request_with_retry('get', endpoint, params=params, headers=headers if headers else None)
            
            if response and response.status_code == 200:
                return response.json().get('trades', [])
            return []
        except Exception as e:
            logger.error(f"Error fetching trades: {e}")
            return []

    # try_private_key_auth removed — replaced by _test_auth() + _signed_headers()
    
    def get_balance(self) -> float:
        """Get account balance (requires authentication)"""
        try:
            endpoint = f"{self.BASE_URL}/portfolio/balance"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/portfolio/balance"
            headers = self._signed_headers('GET', path)
            response = self._request_with_retry('get', endpoint, headers=headers if headers else None)
            
            if response and response.status_code == 200:
                data = response.json()
                return float(data.get('balance', 0)) / 100  # Kalshi uses cents
            return 0.0
        except Exception as e:
            logger.error(f"Error fetching balance: {e}")
            return 0.0
    
    def place_order(self, ticker: str, side: str, quantity: int, 
                    price: int, order_type: str = "limit") -> Optional[Dict]:
        """
        Place an order (REAL MONEY - BE CAREFUL!)
        
        Args:
            ticker: Market ticker (e.g., "KXBTC-23DEC31-T50000")
            side: "yes" or "no"
            quantity: Number of contracts
            price: Price in cents (e.g., 50 = $0.50)
            order_type: "limit" or "market"
        """
        try:
            endpoint = f"{self.BASE_URL}/portfolio/orders"
            payload = {
                "ticker": ticker,
                "action": "buy",
                "side": side,
                "count": quantity,
                "type": order_type,
                "yes_price": price if side == "yes" else None,
                "no_price": price if side == "no" else None
            }
            
            payload = {k: v for k, v in payload.items() if v is not None}
            
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/portfolio/orders"
            headers = self._signed_headers('POST', path)
            response = self._request_with_retry('post', endpoint, json=payload, headers=headers if headers else None)
            
            if response and response.status_code == 201:
                return response.json().get('order')
            else:
                logger.error(f"Order failed: {response.status_code if response else 'No response'} - {response.text if response else ''}")
                return None
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by order_id."""
        try:
            endpoint = f"{self.BASE_URL}/portfolio/orders/{order_id}"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/portfolio/orders/{order_id}"
            headers = self._signed_headers('DELETE', path)
            response = self._request_with_retry('delete', endpoint, headers=headers if headers else None)
            if response and response.status_code in (200, 204):
                return True
            else:
                logger.error(f"Cancel order failed: {response.status_code if response else 'No response'} - {response.text if response else ''}")
                return False
        except Exception as e:
            logger.error(f"Error cancelling order: {e}")
            return False

    def get_order(self, order_id: str) -> Optional[Dict]:
        """Get order status by order_id."""
        try:
            endpoint = f"{self.BASE_URL}/portfolio/orders/{order_id}"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/portfolio/orders/{order_id}"
            headers = self._signed_headers('GET', path)
            response = self._request_with_retry('get', endpoint, headers=headers if headers else None)
            if response and response.status_code == 200:
                return response.json().get('order')
            return None
        except Exception as e:
            logger.error(f"Error fetching order: {e}")
            return None

    def get_positions(self) -> List[Dict]:
        """Get current portfolio positions for reconciliation."""
        try:
            endpoint = f"{self.BASE_URL}/portfolio/positions"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/portfolio/positions"
            headers = self._signed_headers('GET', path)
            response = self._request_with_retry('get', endpoint, headers=headers if headers else None)
            if response and response.status_code == 200:
                return response.json().get('market_positions', [])
            return []
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            return []

    def get_event(self, event_ticker: str) -> Optional[Dict]:
        """Get event details."""
        try:
            endpoint = f"{self.BASE_URL}/events/{event_ticker}"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/events/{event_ticker}"
            headers = self._signed_headers('GET', path)
            response = self._request_with_retry('get', endpoint, headers=headers if headers else None)
            if response and response.status_code == 200:
                return response.json().get('event')
            return None
        except Exception as e:
            logger.error(f"Error fetching event {event_ticker}: {e}")
            return None


class KalshiArbitrageBot:
    """Arbitrage detection bot for Kalshi"""
    
    def __init__(self, api: KalshiAPI, min_profit_percent: float = 2.0):
        self.api = api
        self.min_profit_percent = min_profit_percent
        self.opportunities_found = []
        self.storage = Storage()
    
    def analyze_market_mispricing(self, market: Dict, orderbook: Dict) -> Optional[Dict]:
        """Detect mispricing in a single market with depth/liquidity checks."""
        try:
            yes_asks = orderbook.get('yes_asks', [])
            no_asks = orderbook.get('no_asks', [])

            if not yes_asks or not no_asks:
                return None

            min_qty = Config.MIN_ORDER_QUANTITY

            # Filter asks with sufficient depth
            yes_asks_filtered = [(price, qty) for price, qty in yes_asks if qty >= min_qty]
            no_asks_filtered = [(price, qty) for price, qty in no_asks if qty >= min_qty]

            if not yes_asks_filtered or not no_asks_filtered:
                return None

            best_yes = min(yes_asks_filtered, key=lambda x: x[0])
            best_no = min(no_asks_filtered, key=lambda x: x[0])

            best_yes_ask = best_yes[0]
            best_no_ask = best_no[0]
            max_executable_qty = min(best_yes[1], best_no[1])

            total_cost = best_yes_ask + best_no_ask
            guaranteed_payout = 100

            profit = guaranteed_payout - total_cost
            profit_percent = (profit / total_cost) * 100

            if profit_percent > self.min_profit_percent:
                return {
                    'ticker': market.get('ticker'),
                    'title': market.get('title'),
                    'yes_price': best_yes_ask,
                    'no_price': best_no_ask,
                    'total_cost': total_cost,
                    'profit_cents': profit,
                    'profit_percent': profit_percent,
                    'max_executable_qty': max_executable_qty,
                    'strategy': 'Buy both YES and NO, guaranteed profit on settlement',
                    'timestamp': datetime.now().isoformat()
                }

            return None

        except Exception as e:
            logger.error(f"Error analyzing market: {e}")
            return None
    
    def scan_all_markets(self, category_filter: Optional[str] = None) -> List[Dict]:
        """Scan all markets for arbitrage opportunities"""
        logger.info(f"🔍 Scanning Kalshi markets...")
        
        markets = self.api.get_markets(status="open")
        logger.info(f"Found {len(markets)} open markets")
        
        opportunities = []
        scanned = 0
        
        for market in markets:
            try:
                ticker = market.get('ticker')
                title = market.get('title', '')
                
                if category_filter and category_filter.upper() not in title.upper():
                    continue
                
                scanned += 1
                logger.info(f"Scanning {scanned}: {ticker} - {title[:50]}...")
                
                orderbook = self.api.get_orderbook(ticker)
                
                if not orderbook:
                    continue
                
                opportunity = self.analyze_market_mispricing(market, orderbook)
                
                if opportunity:
                    opportunities.append(opportunity)
                    self.storage.log_opportunity(opportunity)
                    logger.info(f"  ✅ OPPORTUNITY FOUND!")
                    logger.info(f"     Profit: {opportunity['profit_cents']} cents ({opportunity['profit_percent']:.2f}%)")
                
                time.sleep(Config.RATE_LIMIT_DELAY)
                
            except Exception as e:
                logger.error(f"  Error scanning {ticker}: {e}")
                continue
        
        return opportunities

    def scan_multi_leg_arbitrage(self) -> List[Dict]:
        """Scan for cross-market arbitrage within events.
        
        Groups markets by event_ticker, checks if sum of best YES asks
        across all outcomes < 100 cents (guaranteed profit by buying all).
        """
        logger.info("🔍 Scanning for multi-leg arbitrage opportunities...")

        markets = self.api.get_markets(status="open")
        logger.info(f"Found {len(markets)} open markets")

        # Group by event_ticker
        events = {}
        for market in markets:
            event_ticker = market.get('event_ticker')
            if event_ticker:
                events.setdefault(event_ticker, []).append(market)

        opportunities = []
        scanned_events = 0

        for event_ticker, event_markets in events.items():
            if len(event_markets) < 2:
                continue

            scanned_events += 1
            logger.info(f"Scanning event {scanned_events}: {event_ticker} ({len(event_markets)} markets)...")

            total_cost = 0
            legs = []
            valid = True

            for market in event_markets:
                orderbook = self.api.get_orderbook(market['ticker'])
                yes_asks = orderbook.get('yes_asks', [])
                if not yes_asks:
                    valid = False
                    break

                # Filter by minimum quantity
                filtered = [(p, q) for p, q in yes_asks if q >= Config.MIN_ORDER_QUANTITY]
                if not filtered:
                    valid = False
                    break

                best = min(filtered, key=lambda x: x[0])
                total_cost += best[0]
                legs.append({
                    'ticker': market['ticker'],
                    'title': market.get('title'),
                    'yes_price': best[0],
                    'quantity_available': best[1]
                })
                time.sleep(Config.RATE_LIMIT_DELAY)

            if not valid:
                continue

            profit = 100 - total_cost
            if total_cost < 100 and profit > 0:
                profit_pct = (profit / total_cost) * 100
                if profit_pct > self.min_profit_percent:
                    opp = {
                        'type': 'multi_leg',
                        'event_ticker': event_ticker,
                        'num_legs': len(legs),
                        'legs': legs,
                        'total_cost': total_cost,
                        'profit_cents': profit,
                        'profit_percent': profit_pct,
                        'max_executable_qty': min(leg['quantity_available'] for leg in legs),
                        'strategy': f'Buy YES on all {len(legs)} outcomes — one must settle YES',
                        'timestamp': datetime.now().isoformat()
                    }
                    opportunities.append(opp)
                    self.storage.log_opportunity(opp)
                    logger.info(f"  ✅ MULTI-LEG OPPORTUNITY! {len(legs)} legs, profit {profit}¢ ({profit_pct:.2f}%)")

        logger.info(f"\nScanned {scanned_events} multi-market events")
        return opportunities
    
    def monitor_specific_markets(self, tickers: List[str], interval: int = 60):
        """Monitor specific markets continuously"""
        logger.info(f"👀 Monitoring {len(tickers)} markets every {interval}s")
        logger.info(f"Markets: {', '.join(tickers)}")
        
        iteration = 0
        
        try:
            while True:
                iteration += 1
                logger.info(f"\n{'='*60}")
                logger.info(f"Iteration {iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                logger.info(f"{'='*60}")
                
                for ticker in tickers:
                    try:
                        market = self.api.get_market(ticker)
                        if not market:
                            continue
                        
                        orderbook = self.api.get_orderbook(ticker)
                        opportunity = self.analyze_market_mispricing(market, orderbook)
                        
                        if opportunity:
                            logger.info(f"\n🎯 ARBITRAGE OPPORTUNITY!")
                            logger.info(f"Market: {opportunity['title']}")
                            logger.info(f"YES price: {opportunity['yes_price']} cents")
                            logger.info(f"NO price: {opportunity['no_price']} cents")
                            logger.info(f"Total cost: {opportunity['total_cost']} cents")
                            logger.info(f"Profit: {opportunity['profit_cents']} cents ({opportunity['profit_percent']:.2f}%)")
                            logger.info(f"Strategy: {opportunity['strategy']}")
                             
                            self.opportunities_found.append(opportunity)
                        else:
                            logger.info(f"✓ {ticker}: No opportunity (spread too small)")
                        
                        time.sleep(Config.RATE_LIMIT_DELAY)
                        
                    except Exception as e:
                        logger.error(f"Error monitoring {ticker}: {e}")
                
                logger.info(f"\nWaiting {interval}s until next scan...")
                logger.info(f"Total opportunities found: {len(self.opportunities_found)}")
                time.sleep(interval)
                
        except KeyboardInterrupt:
            logger.info(f"\n\n{'='*60}")
            logger.info(f"Monitoring stopped by user")
            logger.info(f"Total opportunities found: {len(self.opportunities_found)}")
            logger.info(f"{'='*60}")


class KalshiTradingBot:
    """Paper trading bot for Kalshi (simulation mode)"""
    
    def __init__(self, api: KalshiAPI, initial_balance: float = 1000.0, 
                 paper_trading: bool = True):
        self.api = api
        self.paper_trading = paper_trading
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.positions = []
        self.trade_history = []
        self.storage = Storage()
    
    def execute_arbitrage(self, opportunity: Dict, quantity: int = 1):
        """Execute arbitrage trade (buy both YES and NO)""" 
        
        ticker = opportunity['ticker']
        yes_price = opportunity['yes_price']
        no_price = opportunity['no_price']
        total_cost = (yes_price + no_price) * quantity / 100
        
        logger.info(f"\n{'='*60}")
        logger.info(f"🤖 EXECUTING ARBITRAGE")
        logger.info(f"{'='*60}")
        logger.info(f"Market: {opportunity['title']}")
        logger.info(f"Quantity: {quantity} contracts")
        logger.info(f"YES price: ${yes_price/100:.2f} x {quantity} = ${yes_price * quantity / 100:.2f}")
        logger.info(f"NO price: ${no_price/100:.2f} x {quantity} = ${no_price * quantity / 100:.2f}")
        logger.info(f"Total cost: ${total_cost:.2f}")
        logger.info(f"Expected payout: ${quantity:.2f}")
        logger.info(f"Expected profit: ${quantity - total_cost:.2f}")
        
        if total_cost > self.balance:
            logger.error(f"❌ Insufficient balance (need ${total_cost:.2f}, have ${self.balance:.2f})")
            return False

        if self.paper_trading:
            logger.info(f"\n📄 PAPER TRADE MODE - No real orders placed")

            trade = {
                'ticker': ticker,
                'type': 'arbitrage',
                'quantity': quantity,
                'yes_price': yes_price,
                'no_price': no_price,
                'cost': total_cost,
                'expected_profit': quantity - total_cost,
                'timestamp': datetime.now().isoformat(),
                'paper_trade': True
            }

            self.trade_history.append(trade)
            self.balance -= total_cost
            self.positions.append(trade)
            self.storage.log_trade(trade)

            logger.info(f"✅ Paper trade recorded")
            logger.info(f"Remaining balance: ${self.balance:.2f}")

        else:
            # Live trading path with safeguards
            if not Config.LIVE_TRADING_ENABLED:
                logger.warning("\n⚠️  LIVE TRADING DISABLED - enable by setting ENABLE_LIVE_TRADING=true in .env")
                return False

            if total_cost > Config.MAX_TRADE_USD:
                logger.warning(f"\n⚠️  Trade exceeds configured max (${Config.MAX_TRADE_USD:.2f}). Refusing to place live orders.")
                return False

            logger.warning(f"\n⚠️  LIVE TRADING MODE - About to place real orders (SAFE MODE)")
            logger.warning(f"Market: {opportunity['title']}")
            logger.warning(f"YES price: ${yes_price/100:.2f} x {quantity}")
            logger.warning(f"NO price: ${no_price/100:.2f} x {quantity}")
            logger.warning(f"Total cost: ${total_cost:.2f}")

            confirmation = input("Type 'YES' to confirm and place live orders: ").strip().upper()
            if confirmation != 'YES':
                logger.info("Aborted by user. No orders placed.")
                return False

            logger.info("Placing YES order...")
            yes_order = self.api.place_order(ticker, 'yes', quantity, yes_price, order_type='limit')

            if not yes_order:
                logger.error("❌ YES order failed. No orders placed.")
                return False

            logger.info("Placing NO order...")
            no_order = self.api.place_order(ticker, 'no', quantity, no_price, order_type='limit')

            if not no_order:
                # YES succeeded but NO failed — cancel YES to avoid naked exposure
                yes_order_id = yes_order.get('order_id')
                logger.error(f"❌ NO order failed! Cancelling YES order {yes_order_id} to prevent naked exposure...")
                if self.api.cancel_order(yes_order_id):
                    logger.info("✅ YES order cancelled successfully. No exposure.")
                else:
                    logger.error("⚠️  CRITICAL: Failed to cancel YES order! Manual intervention required!")
                    logger.error(f"   Order ID: {yes_order_id} | Ticker: {ticker}")
                return False

            # Both orders succeeded
            logger.info("✅ Both orders placed successfully")
            trade = {
                'ticker': ticker,
                'type': 'arbitrage',
                'quantity': quantity,
                'yes_price': yes_price,
                'no_price': no_price,
                'cost': total_cost,
                'expected_profit': quantity - total_cost,
                'timestamp': datetime.now().isoformat(),
                'yes_order': yes_order,
                'no_order': no_order,
                'paper_trade': False
            }
            self.trade_history.append(trade)
            self.balance -= total_cost
            self.positions.append(trade)
            self.storage.log_trade(trade)
            logger.info(f"Remaining balance: ${self.balance:.2f}")

        return True
    
    def get_stats(self) -> Dict:
        """Get trading statistics"""
        total_expected_profit = sum([t.get('expected_profit', 0) for t in self.positions])
        
        return {
            'paper_trading': self.paper_trading,
            'initial_balance': self.initial_balance,
            'current_balance': self.balance,
            'total_trades': len(self.trade_history),
            'open_positions': len(self.positions),
            'total_invested': self.initial_balance - self.balance,
            'expected_profit': total_expected_profit,
            'expected_roi_percent': (total_expected_profit / (self.initial_balance - self.balance) * 100) if self.balance != self.initial_balance else 0
        }
    
    def print_stats(self):
        """Print formatted statistics"""
        stats = self.get_stats()
        
        logger.info(f"\n{'='*60}")
        logger.info(f"📊 TRADING STATISTICS")
        logger.info(f"{'='*60}")
        logger.info(f"Mode: {'PAPER TRADING' if stats['paper_trading'] else 'LIVE TRADING'}")
        logger.info(f"Initial balance: ${stats['initial_balance']:.2f}")
        logger.info(f"Current balance: ${stats['current_balance']:.2f}")
        logger.info(f"Total invested: ${stats['total_invested']:.2f}")
        logger.info(f"Total trades: {stats['total_trades']}")
        logger.info(f"Open positions: {stats['open_positions']}")
        logger.info(f"Expected profit: ${stats['expected_profit']:.2f}")
        logger.info(f"Expected ROI: {stats['expected_roi_percent']:.2f}%")
        logger.info(f"{'='*60}")


def main():
    """Main function - requires interactive terminal for user input""" 
    
    logger.info(f"\n{'='*60}")
    logger.info(f"🤖 KALSHI ARBITRAGE BOT - Educational Version")
    logger.info(f"{'='*60}\n")
    
    # Load configuration from config.py/.env
    try:
        Config.validate()
    except Exception as e:
        logger.error(f"Configuration error: {e}")

    Config.print_config()

    api = KalshiAPI(email=Config.KALSHI_EMAIL, password=Config.KALSHI_PASSWORD, api_key=Config.KALSHI_API_KEY)
    bot = KalshiArbitrageBot(api, min_profit_percent=Config.MIN_PROFIT_PERCENT)
    
    logger.info("Choose mode:")
    logger.info("1. Scan all markets once")
    logger.info("2. Scan specific category (e.g., BTC, NASDAQ)")
    logger.info("3. Monitor specific tickers continuously")
    logger.info("4. Demo paper trading")
    logger.info("5. Scan multi-leg arbitrage across events")
    logger.info("6. View historical stats")
    
    choice = input("\nEnter choice (1-6): ").strip()
    
    if choice == "1":
        opportunities = bot.scan_all_markets()
        logger.info(f"\n✅ Scan complete: Found {len(opportunities)} opportunities")
        
    elif choice == "2":
        category = input("Enter category keyword (BTC, NASDAQ, etc.): ").strip()
        opportunities = bot.scan_all_markets(category_filter=category)
        logger.info(f"\n✅ Scan complete: Found {len(opportunities)} opportunities")
        
    elif choice == "3":
        logger.info("\nExample tickers: KXBTC-23DEC31-T50000, INX-23DEC-T4500")
        tickers_input = input("Enter tickers (comma-separated): ").strip()
        tickers = [t.strip() for t in tickers_input.split(",")]
        bot.monitor_specific_markets(tickers, interval=60)
        
    elif choice == "4":
        trader = KalshiTradingBot(api, initial_balance=1000.0, paper_trading=True)
        
        demo_opportunity = {
            'ticker': 'DEMO-TICKER',
            'title': 'Demo Market for Testing',
            'yes_price': 48,
            'no_price': 50,
            'total_cost': 98,
            'profit_cents': 2,
            'profit_percent': 2.04
        }
        
        trader.execute_arbitrage(demo_opportunity, quantity=10)
        trader.print_stats()

    elif choice == "5":
        opportunities = bot.scan_multi_leg_arbitrage()
        logger.info(f"\n✅ Multi-leg scan complete: Found {len(opportunities)} opportunities")
        for i, opp in enumerate(opportunities, 1):
            logger.info(f"\n{i}. Event: {opp['event_ticker']}")
            logger.info(f"   Legs: {opp['num_legs']} | Cost: {opp['total_cost']}¢ | Profit: {opp['profit_cents']}¢ ({opp['profit_percent']:.2f}%)")
            for leg in opp['legs']:
                logger.info(f"     - {leg['ticker']}: YES @ {leg['yes_price']}¢ (qty: {leg['quantity_available']})")

    elif choice == "6":
        storage = Storage()
        logger.info(f"\n{'='*60}")
        logger.info(f"📊 HISTORICAL STATISTICS")
        logger.info(f"{'='*60}")
        
        # Opportunity stats
        stats = storage.get_opportunity_stats()
        logger.info(f"\n🎯 Opportunity Statistics:")
        logger.info(f"  Total opportunities found: {stats.get('total', 0)}")
        if stats.get('avg_profit'):
            logger.info(f"  Average profit: {stats['avg_profit']:.2f}%")
        if stats.get('max_profit'):
            logger.info(f"  Max profit: {stats['max_profit']:.2f}%")
        
        # Recent opportunities
        opportunities = storage.get_all_opportunities()
        if opportunities:
            logger.info(f"\n📈 Recent Opportunities (last 10):")
            for i, opp in enumerate(opportunities[:10], 1):
                opp_type = opp.get('type', 'single')
                if opp_type == 'multi_leg':
                    logger.info(f"  {i}. [{opp['detected_at'][:10]}] Multi-leg: {opp['event_ticker']} | Profit: {opp['profit_cents']}¢ ({opp['profit_percent']:.2f}%)")
                else:
                    logger.info(f"  {i}. [{opp['detected_at'][:10]}] {opp['ticker']}: {opp['title'][:40]} | Profit: {opp['profit_cents']}¢ ({opp['profit_percent']:.2f}%)")
        
        # Trade history
        trades = storage.get_all_trades()
        logger.info(f"\n💼 Trade History:")
        logger.info(f"  Total trades executed: {len(trades)}")
        if trades:
            logger.info(f"\n  Recent Trades (last 10):")
            for i, trade in enumerate(trades[:10], 1):
                mode = "Paper" if trade['paper_trade'] else "Live"
                logger.info(f"  {i}. [{trade['executed_at'][:10]}] {mode}: {trade['ticker']} | Qty: {trade['quantity']} | Cost: ${trade['cost_usd']:.2f} | Expected profit: ${trade['expected_profit_usd']:.2f}")
        
        storage.close()
        logger.info(f"{'='*60}")
    
    else:
        logger.error("Invalid choice")


if __name__ == "__main__":
    main()