import requests
import time
import random
import logging
from typing import Dict, List, Optional
from datetime import datetime, timezone
from config import Config
from urllib.parse import urlparse
import base64
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.backends import default_backend
from concurrent.futures import ThreadPoolExecutor, as_completed
from notifications import NotificationManager
from kelly import size_position


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('kalshi_bot.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger('kalshi_bot')

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
        self.token_expires_at = None
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
                print("✅ Private key loaded from file")
            except Exception as e:
                print(f"❌ Failed to load private key: {e}")

        # Determine auth method
        if self.api_key and self.private_key:
            # Use Kalshi signed-header auth (per docs)
            # Test which base URL + salt length works
            if self._test_auth():
                print(f"✅ Authenticated with signed headers (base: {self.BASE_URL})")
            else:
                print("⚠️  Signed-header auth failed on both prod and demo URLs with both salt lengths")
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
                print(f"  Trying: {base_url} with salt={salt_name}...")

                path = urlparse(base_url).path.rstrip('/') + '/markets'
                headers = self._signed_headers('GET', path)
                if not headers:
                    print(f"    Skipped (no headers generated)")
                    continue

                try:
                    resp = self.session.get(f"{base_url}/markets", params={'limit': 1}, headers=headers)
                    print(f"    Response: {resp.status_code}")
                    if resp.status_code == 200:
                        return True
                    else:
                        # Show response body for debugging
                        body = resp.text[:200] if resp.text else '(empty)'
                        print(f"    Body: {body}")
                except Exception as e:
                    print(f"    Error: {e}")

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
        """Make HTTP request with exponential backoff retry on transient failures.
        
        Retries on:
        - 429 Too Many Requests (rate limit)
        - 500, 502, 503, 504 (server errors)
        - ConnectionError, Timeout (network issues)
        
        Backoff schedule: 2s, 5s, 9s (base * 2^attempt + jitter)
        
        Args:
            method: HTTP method ('GET', 'POST', 'DELETE', etc.)
            url: Full URL
            max_retries: Maximum retry attempts (default 3)
            **kwargs: Passed to requests (json, params, headers, etc.)
        
        Returns:
            Response object, or None if all retries exhausted
        """
        base_delay = 1.5
        
        for attempt in range(max_retries + 1):
            try:
                # Make the request
                response = self.session.request(method, url, **kwargs)
                
                # Check for success
                if response.status_code < 400:
                    return response
                
                # Don't retry on client errors (400, 401, 403, 404)
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    return response
                
                # Retry on 429 or 5xx
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < max_retries:
                        # Check for Retry-After header
                        retry_after = response.headers.get('Retry-After')
                        if retry_after and retry_after.isdigit():
                            delay = int(retry_after)
                        else:
                            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                        
                        logger.warning(
                            "Request failed with %d: %s %s (attempt %d/%d). Retrying in %.2fs...",
                            response.status_code, method, url, attempt + 1, max_retries + 1, delay
                        )
                        time.sleep(delay)
                        continue
                    else:
                        logger.error(
                            "Request failed after %d retries: %s %s (status: %d)",
                            max_retries + 1, method, url, response.status_code
                        )
                        return response
                
                # Return for any other status code
                return response
                
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                    logger.warning(
                        "Network error: %s %s (attempt %d/%d). Retrying in %.2fs...",
                        method, url, attempt + 1, max_retries + 1, delay
                    )
                    time.sleep(delay)
                    continue
                else:
                    logger.error(
                        "Request failed after %d retries: %s %s (error: %s)",
                        max_retries + 1, method, url, str(e)
                    )
                    return None
            except Exception as e:
                # Don't retry on unexpected errors
                logger.error("Unexpected error in request: %s %s - %s", method, url, str(e))
                return None
        
        return None
    
    def login(self) -> bool:
        """Authenticate with Kalshi"""
        try:
            endpoint = f"{self.BASE_URL}/login"
            payload = {
                "email": self.email,
                "password": self.password
            }
            response = self._request_with_retry('POST', endpoint, json=payload)
            
            if response is None:
                logger.error("Login request failed after retries: POST %s", endpoint)
                print("❌ Login error: Request failed after retries")
                return False
            
            if response.status_code == 200:
                data = response.json()
                self.token = data.get('token')
                # Kalshi tokens typically expire in ~24 hours; refresh proactively at 23 hours
                self.token_expires_at = time.time() + (23 * 3600)
                self.session.headers.update({
                    'Authorization': f'Bearer {self.token}'
                })
                print("✅ Successfully logged in to Kalshi")
                return True
            else:
                print(f"❌ Login failed: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ Login error: {e}")
            return False
    
    def _ensure_auth(self):
        """Check if auth token needs refresh and re-login if needed."""
        if self.token and self.token_expires_at:
            if time.time() > self.token_expires_at:
                print("🔄 Token expiring soon, refreshing...")
                self.login()
    
    def get_markets(self, status: str = "open", limit: int = 100, cursor: str = None, series_ticker: str = None) -> List[Dict]:
        """Fetch active markets (single page)"""
        self._ensure_auth()
        try:
            endpoint = f"{self.BASE_URL}/markets"
            params = {
                'status': status,
                'limit': limit
            }
            if cursor:
                params['cursor'] = cursor
            if series_ticker:
                params['series_ticker'] = series_ticker
            # construct path without query for signing
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/markets"
            headers = self._signed_headers('GET', path)
            response = self._request_with_retry('GET', endpoint, params=params, headers=headers if headers else None)
            
            if response is None:
                logger.error("Request failed after retries: GET %s", endpoint)
                return [], None
            
            if response.status_code == 200:
                data = response.json()
                return data.get('markets', []), data.get('cursor', None)
            else:
                print(f"Error fetching markets: {response.status_code}")
                if response.status_code == 401:
                    body = response.text[:200] if response.text else '(empty)'
                    print(f"  Response body: {body}")
                return [], None
        except Exception as e:
            print(f"Error: {e}")
            return [], None

    def get_all_markets(self, status: str = "open", series_ticker: str = None) -> List[Dict]:
        """Fetch ALL markets using pagination."""
        all_markets = []
        cursor = None
        page = 0
        while True:
            page += 1
            markets, cursor = self.get_markets(status=status, limit=200, cursor=cursor, series_ticker=series_ticker)
            all_markets.extend(markets)
            if not cursor or not markets:
                break
            time.sleep(Config.RATE_LIMIT_DELAY)  # Rate limit
        if series_ticker:
            print(f"📦 Fetched {len(all_markets)} {series_ticker} markets ({page} pages)")
        else:
            print(f"📦 Fetched {len(all_markets)} total markets ({page} pages)")
        return all_markets

    def get_event(self, event_ticker: str) -> Optional[Dict]:
        """Get event details and its markets."""
        self._ensure_auth()
        try:
            endpoint = f"{self.BASE_URL}/events/{event_ticker}"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/events/{event_ticker}"
            headers = self._signed_headers('GET', path)
            response = self._request_with_retry('GET', endpoint, headers=headers if headers else None)
            if response is None:
                logger.error("Request failed after retries: GET %s", endpoint)
                return None
            if response.status_code == 200:
                return response.json().get('event')
            return None
        except Exception as e:
            print(f"Error fetching event {event_ticker}: {e}")
            return None
    
    def get_market(self, ticker: str) -> Optional[Dict]:
        """Get specific market by ticker"""
        self._ensure_auth()
        try:
            endpoint = f"{self.BASE_URL}/markets/{ticker}"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/markets/{ticker}"
            headers = self._signed_headers('GET', path)
            response = self._request_with_retry('GET', endpoint, headers=headers if headers else None)
            
            if response is None:
                logger.error("Request failed after retries: GET %s", endpoint)
                return None
            
            if response.status_code == 200:
                return response.json().get('market')
            return None
        except Exception as e:
            print(f"Error fetching market {ticker}: {e}")
            return None
    
    def get_orderbook(self, ticker: str) -> Dict:
        """Get orderbook for a market.
        
        Kalshi API returns:
            {"orderbook": {"yes": [[price, qty], ...], "no": [[price, qty], ...]}}
        The yes/no arrays represent resting limit orders (asks) at each price level.
        """
        self._ensure_auth()
        try:
            endpoint = f"{self.BASE_URL}/markets/{ticker}/orderbook"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/markets/{ticker}/orderbook"
            headers = self._signed_headers('GET', path)
            response = self._request_with_retry('GET', endpoint, headers=headers if headers else None)
            
            if response is None:
                logger.error("Request failed after retries: GET %s", endpoint)
                return {}
            
            if response.status_code == 200:
                data = response.json()
                ob = data.get('orderbook', data)
                yes_levels = ob.get('yes') or []  # [[price, qty], ...]
                no_levels = ob.get('no') or []

                return {
                    'yes_asks': yes_levels,   # Each element is [price_cents, quantity]
                    'no_asks': no_levels,
                    'timestamp': time.time()
                }
            return {}
        except Exception as e:
            print(f"Error fetching orderbook: {e}")
            return {}
    
    def get_trades(self, ticker: str, limit: int = 100) -> List[Dict]:
        """Get recent trades for a market"""
        self._ensure_auth()
        try:
            endpoint = f"{self.BASE_URL}/markets/{ticker}/trades"
            params = {'limit': limit}
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/markets/{ticker}/trades"
            headers = self._signed_headers('GET', path)
            response = self._request_with_retry('GET', endpoint, params=params, headers=headers if headers else None)
            
            if response is None:
                logger.error("Request failed after retries: GET %s", endpoint)
                return []
            
            if response.status_code == 200:
                return response.json().get('trades', [])
            return []
        except Exception as e:
            print(f"Error fetching trades: {e}")
            return []

    # try_private_key_auth removed — replaced by _test_auth() + _signed_headers()
    
    def get_balance(self) -> float:
        """Get account balance (requires authentication)"""
        self._ensure_auth()
        try:
            endpoint = f"{self.BASE_URL}/portfolio/balance"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/portfolio/balance"
            headers = self._signed_headers('GET', path)
            response = self._request_with_retry('GET', endpoint, headers=headers if headers else None)
            
            if response is None:
                logger.error("Request failed after retries: GET %s", endpoint)
                return 0.0
            
            if response.status_code == 200:
                data = response.json()
                return float(data.get('balance', 0)) / 100  # Kalshi uses cents
            return 0.0
        except Exception as e:
            print(f"Error fetching balance: {e}")
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
        self._ensure_auth()
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
            response = self._request_with_retry('POST', endpoint, json=payload, headers=headers if headers else None)
            
            if response is None:
                logger.error("Request failed after retries: POST %s", endpoint)
                return None
            
            if response.status_code == 201:
                return response.json().get('order')
            else:
                print(f"Order failed: {response.status_code} - {response.text}")
                return None
        except Exception as e:
            print(f"Error placing order: {e}")
            return None
    
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order"""
        self._ensure_auth()
        try:
            endpoint = f"{self.BASE_URL}/portfolio/orders/{order_id}"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/portfolio/orders/{order_id}"
            headers = self._signed_headers('DELETE', path)
            response = self._request_with_retry('DELETE', endpoint, headers=headers if headers else None)
            
            if response is None:
                logger.error("Request failed after retries: DELETE %s", endpoint)
                return False
            
            if response.status_code == 200:
                return True
            else:
                print(f"Cancel order failed: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            print(f"Error cancelling order: {e}")
            return False
    
    def get_order(self, order_id: str) -> Optional[Dict]:
        """Get order details"""
        self._ensure_auth()
        try:
            endpoint = f"{self.BASE_URL}/portfolio/orders/{order_id}"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/portfolio/orders/{order_id}"
            headers = self._signed_headers('GET', path)
            response = self._request_with_retry('GET', endpoint, headers=headers if headers else None)
            
            if response is None:
                logger.error("Request failed after retries: GET %s", endpoint)
                return None
            
            if response.status_code == 200:
                return response.json().get('order')
            return None
        except Exception as e:
            print(f"Error fetching order: {e}")
            return None
    
    def get_positions(self) -> List[Dict]:
        """Get current positions"""
        self._ensure_auth()
        try:
            endpoint = f"{self.BASE_URL}/portfolio/positions"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/portfolio/positions"
            headers = self._signed_headers('GET', path)
            response = self._request_with_retry('GET', endpoint, headers=headers if headers else None)
            
            if response is None:
                logger.error("Request failed after retries: GET %s", endpoint)
                return []
            
            if response.status_code == 200:
                return response.json().get('positions', [])
            return []
        except Exception as e:
            print(f"Error fetching positions: {e}")
            return []


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


class KalshiTradingBot:
    """Paper trading bot for Kalshi (simulation mode)"""
    
    def __init__(self, api: KalshiAPI, initial_balance: float = 1000.0, 
                 paper_trading: bool = True, storage=None):
        self.api = api
        self.paper_trading = paper_trading
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.positions = []
        self.trade_history = []
        self.notifier = NotificationManager()
        self.storage = storage  # Optional storage backend
    
    def execute_arbitrage(self, opportunity: Dict, quantity: int = None, use_kelly: bool = False):
        """
        Execute arbitrage trade (buy both YES and NO).
        
        Args:
            opportunity: Opportunity dict with ticker, yes_price, no_price, etc.
            quantity: Number of contracts (if None and use_kelly=True, calculates from Kelly)
            use_kelly: Whether to use Kelly criterion for position sizing
        """ 
        
        ticker = opportunity['ticker']
        yes_price = opportunity['yes_price']
        no_price = opportunity['no_price']
        
        # Calculate quantity using Kelly if requested and not provided
        if quantity is None and use_kelly:
            # For arbitrage, win probability is effectively 1.0 (guaranteed profit)
            win_prob = 0.99  # Nearly certain
            avg_price = (yes_price + no_price) / 2
            quantity = size_position(
                bankroll=self.balance,
                win_prob=win_prob,
                contract_price_cents=int(avg_price),
                max_trade_usd=Config.MAX_TRADE_USD,
                kelly_multiplier=Config.KELLY_MULTIPLIER
            )
            logger.info("Kelly sizing calculated quantity: %d contracts", quantity)
        
        # Default to 1 if still None
        if quantity is None or quantity < 1:
            quantity = 1
        
        total_cost = (yes_price + no_price) * quantity / 100
        
        print(f"\n{'='*60}")
        print(f"🤖 EXECUTING ARBITRAGE")
        print(f"{'='*60}")
        print(f"Market: {opportunity['title']}")
        print(f"Quantity: {quantity} contracts")
        print(f"YES price: ${yes_price/100:.2f} x {quantity} = ${yes_price * quantity / 100:.2f}")
        print(f"NO price: ${no_price/100:.2f} x {quantity} = ${no_price * quantity / 100:.2f}")
        print(f"Total cost: ${total_cost:.2f}")
        print(f"Expected payout: ${quantity:.2f}")
        print(f"Expected profit: ${quantity - total_cost:.2f}")
        
        if total_cost > self.balance:
            print(f"❌ Insufficient balance (need ${total_cost:.2f}, have ${self.balance:.2f})")
            return False

        if self.paper_trading:
            print(f"\n📄 PAPER TRADE MODE - No real orders placed")

            trade = {
                'ticker': ticker,
                'type': 'arbitrage',
                'quantity': quantity,
                'yes_price': yes_price,
                'no_price': no_price,
                'cost': total_cost,
                'expected_profit': quantity - total_cost,
                'timestamp': datetime.now().isoformat(),
                'paper_trading': True
            }

            self.trade_history.append(trade)
            self.balance -= total_cost
            self.positions.append(trade)

            print(f"✅ Paper trade recorded")
            print(f"Remaining balance: ${self.balance:.2f}")
            self.notifier.notify_trade_executed(trade)
            
            # Save to storage if available
            if self.storage:
                self.storage.save_trade(trade)

        else:
            # Live trading path with safeguards
            if not Config.LIVE_TRADING_ENABLED:
                print("\n⚠️  LIVE TRADING DISABLED - enable by setting ENABLE_LIVE_TRADING=true in .env")
                return False
            
            # Verify live balance before trading
            live_balance = self.api.get_balance()
            if live_balance < total_cost:
                print(f"❌ Insufficient live balance (need ${total_cost:.2f}, have ${live_balance:.2f} on exchange)")
                return False
            
            # Check position limits
            if len(self.positions) >= Config.MAX_POSITIONS:
                print(f"⚠️ Position limit reached ({Config.MAX_POSITIONS} positions). Refusing new trade.")
                return False

            total_exposure = sum(t.get('cost', 0) for t in self.positions)
            if total_exposure + total_cost > Config.MAX_EXPOSURE_USD:
                print(f"⚠️ Exposure limit would be exceeded (current: ${total_exposure:.2f}, new: ${total_cost:.2f}, max: ${Config.MAX_EXPOSURE_USD:.2f})")
                return False

            if total_cost > Config.MAX_TRADE_USD:
                print(f"\n⚠️  Trade exceeds configured max (${Config.MAX_TRADE_USD:.2f}). Refusing to place live orders.")
                return False

            print(f"\n🚀 LIVE TRADING MODE - AUTO-EXECUTING ARBITRAGE")
            print(f"Market: {opportunity['title']}")
            print(f"YES price: ${yes_price/100:.2f} x {quantity}")
            print(f"NO price: ${no_price/100:.2f} x {quantity}")
            print(f"Total cost: ${total_cost:.2f}")
            print(f"Expected profit: ${quantity - total_cost:.2f}")
            
            # Place YES order first
            logger.info("Placing YES order: %s qty=%d price=%d¢", ticker, quantity, yes_price)
            yes_order = self.api.place_order(ticker, 'yes', quantity, yes_price, order_type='limit')

            if not yes_order:
                logger.error("YES order failed for %s. No orders placed.", ticker)
                return False

            yes_order_id = yes_order.get('order', {}).get('order_id') or yes_order.get('order_id')
            if not yes_order_id:
                logger.warning("YES order succeeded but order_id not found in response. Cannot cancel if NO order fails.")

            # Place NO order
            logger.info("Placing NO order: %s qty=%d price=%d¢", ticker, quantity, no_price)
            no_order = self.api.place_order(ticker, 'no', quantity, no_price, order_type='limit')

            if not no_order:
                # CRITICAL: YES succeeded but NO failed — cancel YES to prevent naked exposure
                logger.critical("NO order failed for %s! Cancelling YES order %s to prevent naked exposure...", ticker, yes_order_id)
                if yes_order_id and self.api.cancel_order(yes_order_id):
                    logger.info("YES order %s cancelled successfully. No exposure.", yes_order_id)
                else:
                    logger.critical("FAILED to cancel YES order %s! MANUAL INTERVENTION REQUIRED. Check positions immediately.", yes_order_id)
                return False

            logger.info("✅ Both orders placed successfully for %s", ticker)
            print("✅ Both orders placed successfully (check exchange for order IDs)")

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
                'paper_trading': False
            }

            self.trade_history.append(trade)
            self.balance -= total_cost
            self.positions.append(trade)
            print(f"Remaining balance: ${self.balance:.2f}")
            self.notifier.notify_trade_executed(trade)
            
            # Save to storage if available
            if self.storage:
                self.storage.save_trade(trade)

        return True
    
    def get_stats(self) -> Dict:
        """Get trading statistics"""
        total_expected_profit = sum([t.get('expected_profit', 0) for t in self.positions])
        realized_profit = sum(t.get('realized_profit', 0) for t in self.trade_history if t.get('realized_profit') is not None)
        
        return {
            'paper_trading': self.paper_trading,
            'initial_balance': self.initial_balance,
            'current_balance': self.balance,
            'total_trades': len(self.trade_history),
            'open_positions': len(self.positions),
            'total_invested': self.initial_balance - self.balance,
            'expected_profit': total_expected_profit,
            'expected_roi_percent': (total_expected_profit / (self.initial_balance - self.balance) * 100) if self.balance != self.initial_balance else 0,
            'realized_profit': realized_profit
        }
    
    def print_stats(self):
        """Print formatted statistics"""
        stats = self.get_stats()
        
        print(f"\n{'='*60}")
        print(f"📊 TRADING STATISTICS")
        print(f"{'='*60}")
        print(f"Mode: {'PAPER TRADING' if stats['paper_trading'] else 'LIVE TRADING'}")
        print(f"Initial balance: ${stats['initial_balance']:.2f}")
        print(f"Current balance: ${stats['current_balance']:.2f}")
        print(f"Total invested: ${stats['total_invested']:.2f}")
        print(f"Total trades: {stats['total_trades']}")
        print(f"Open positions: {stats['open_positions']}")
        print(f"Expected profit: ${stats['expected_profit']:.2f}")
        print(f"Expected ROI: {stats['expected_roi_percent']:.2f}%")
        print(f"Realized profit: ${stats['realized_profit']:.2f}")
        print(f"{'='*60}")
    
    def reconcile_positions(self):
        """Check settled positions and calculate realized P&L."""
        if not self.positions:
            logger.debug("No open positions to reconcile")
            return []

        settled = []
        for position in self.positions:
            ticker = position.get('ticker')
            if not ticker:
                continue
                
            market = self.api.get_market(ticker)
            if market and market.get('status') in ('settled', 'closed'):
                result = market.get('result', '')
                # Each arb position bought both YES and NO, so one of them pays $1
                payout = position['quantity']  # $1 per contract pair
                profit = payout - position['cost']
                position['realized_profit'] = profit
                position['settlement_result'] = result
                position['settled_at'] = datetime.now().isoformat()
                settled.append(position)
                logger.info("Position settled: %s (%s) - P&L: $%.2f", ticker, result, profit)

        for s in settled:
            self.positions.remove(s)
            self.balance += s['quantity']  # Add payout back
            self.trade_history.append({**s, 'status': 'settled'})
            
            # Update storage if available
            if self.storage:
                # Save updated settled trade
                self.storage.save_trade({**s, 'status': 'settled'})

        if settled:
            total_realized = sum(s.get('realized_profit', 0) for s in settled)
            logger.info("Settled %d positions, total realized P&L: $%.2f", len(settled), total_realized)
        
        return settled
    
    def _capital_recycle(self):
        """
        Automatic capital recycling - check for settled positions and recycle capital.
        
        This method:
        1. Calls reconcile_positions() to check for settled positions
        2. Logs settled positions and realized P&L
        3. Updates balance (already done in reconcile_positions)
        4. Makes freed capital immediately available for next trade
        
        Returns:
            Number of positions that were settled and recycled
        """
        logger.debug("Running capital recycle...")
        settled = self.reconcile_positions()
        
        if settled:
            total_recycled = sum(s.get('quantity', 0) for s in settled)
            logger.info("Capital recycled: $%.2f now available for trading", total_recycled)
        
        return len(settled)
    
    def reconcile_with_exchange(self):
        """Compare local positions with exchange positions for consistency."""
        print("🔄 Reconciling with exchange...")

        exchange_positions = self.api.get_positions()
        local_tickers = set(p['ticker'] for p in self.positions)
        exchange_tickers = set(p.get('ticker') for p in exchange_positions)

        # Find discrepancies
        missing_on_exchange = local_tickers - exchange_tickers
        extra_on_exchange = exchange_tickers - local_tickers

        if missing_on_exchange:
            print(f"⚠️ Positions tracked locally but not on exchange: {missing_on_exchange}")
        if extra_on_exchange:
            print(f"⚠️ Positions on exchange but not tracked locally: {extra_on_exchange}")
        if not missing_on_exchange and not extra_on_exchange:
            print("✅ Local and exchange positions match")

        return {
            'local_count': len(local_tickers),
            'exchange_count': len(exchange_tickers),
            'missing_on_exchange': list(missing_on_exchange),
            'extra_on_exchange': list(extra_on_exchange)
        }


def main():
    """Main function""" 
    
    print(f"\n{'='*60}")
    print(f"🤖 KALSHI ARBITRAGE BOT - Educational Version")
    print(f"{'='*60}\n")
    
    # Load configuration from config.py/.env
    try:
        Config.validate()
    except Exception as e:
        print(f"Configuration error: {e}")

    Config.print_config()

    api = KalshiAPI(email=Config.KALSHI_EMAIL, password=Config.KALSHI_PASSWORD, api_key=Config.KALSHI_API_KEY)
    
    # Initialize optional storage backend
    storage = None
    try:
        from storage import Storage
        storage = Storage("kalshi_bot.db")
        logger.info("Storage enabled - trades and opportunities will be persisted")
    except Exception as e:
        logger.warning("Storage not available: %s", e)
        print("⚠️  Note: Storage disabled - trades will not be persisted to database")
    
    bot = KalshiArbitrageBot(api, min_profit_percent=Config.MIN_PROFIT_PERCENT, storage=storage)
    
    print("Choose mode:")
    print("1. Scan all markets once (concurrent)")
    print("2. Scan specific category (e.g., BTC, NASDAQ)")
    print("3. Monitor specific tickers continuously")
    print("4. Demo paper trading")
    print("5. View historical stats")
    print("6. Reconcile settled positions & view P&L")
    print("7. Reconcile positions with exchange")
    print("8. Continuous auto-trading scanner 🤖 (finds & executes arbitrage)")
    print("9. Scan crypto markets for probability edge (BTC/ETH interval markets)")
    print("10. Auto-trade crypto probability strategy (continuous loop)")
    
    choice = input("\nEnter choice (1-10): ").strip()
    
    if choice == "1":
        opportunities = bot.scan_all_markets_concurrent()
        print(f"\n✅ Scan complete: Found {len(opportunities)} opportunities")
        
    elif choice == "2":
        category = input("Enter category keyword (BTC, NASDAQ, etc.): ").strip()
        opportunities = bot.scan_all_markets_concurrent(category_filter=category)
        print(f"\n✅ Scan complete: Found {len(opportunities)} opportunities")
        
    elif choice == "3":
        print("\nExample tickers: KXBTC-23DEC31-T50000, INX-23DEC-T4500")
        tickers_input = input("Enter tickers (comma-separated): ").strip()
        tickers = [t.strip() for t in tickers_input.split(",")]
        bot.monitor_specific_markets(tickers, interval=60)
        
    elif choice == "4":
        trader = KalshiTradingBot(api, initial_balance=1000.0, paper_trading=True, storage=storage)
        
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
        trader = KalshiTradingBot(api, initial_balance=1000.0, paper_trading=True, storage=storage)
        trader.print_stats()
    
    elif choice == "6":
        trader = KalshiTradingBot(api, initial_balance=1000.0, paper_trading=True, storage=storage)
        trader.reconcile_positions()
    
    elif choice == "7":
        trader = KalshiTradingBot(api, initial_balance=1000.0, paper_trading=True, storage=storage)
        trader.reconcile_with_exchange()
    
    elif choice == "8":
        # Continuous auto-trading scanner
        print("\n" + "="*60)
        print("🤖 CONTINUOUS AUTO-TRADING SCANNER")
        print("="*60)
        print(f"Scan interval: {Config.SCAN_INTERVAL_SECONDS}s")
        print(f"Min profit: {Config.MIN_PROFIT_PERCENT}%")
        print(f"Live trading: {'ENABLED ⚠️' if Config.LIVE_TRADING_ENABLED else 'DISABLED (paper only)'}")
        print(f"Max trade: ${Config.MAX_TRADE_USD}")
        print(f"Max exposure: ${Config.MAX_EXPOSURE_USD}")
        print(f"Press Ctrl+C to stop")
        print("="*60 + "\n")
        
        # Initialize trader with live balance
        starting_balance = api.get_balance() if Config.LIVE_TRADING_ENABLED else 250.0
        trader = KalshiTradingBot(
            api, 
            initial_balance=starting_balance,
            paper_trading=not Config.LIVE_TRADING_ENABLED,
            storage=storage
        )
        
        iteration = 0
        try:
            while True:
                iteration += 1
                print(f"\n{'='*60}")
                print(f"Scan #{iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*60}")
                
                # Scan all markets (single-market + multi-outcome event arbitrage)
                opportunities = bot.scan_all_markets_concurrent()
                
                if opportunities:
                    # Sort by profit % descending — execute best opportunities first
                    opportunities.sort(key=lambda o: o.get('profit_percent', 0), reverse=True)
                    print(f"\n✅ Found {len(opportunities)} arbitrage opportunities (sorted by profit %)")
                    
                    for opp in opportunities:
                        opp_type = opp.get('type', 'single')
                        ticker_label = opp.get('event_ticker') if opp_type == 'multi_leg' else opp.get('ticker')
                        
                        # Calculate maximum affordable quantity
                        max_from_book = opp.get('max_executable_qty', 1)
                        cost_per_contract = opp.get('total_cost', 100)  # cents
                        max_from_balance = int((trader.balance * 100) / cost_per_contract) if cost_per_contract > 0 else 0
                        max_from_trade_limit = int((Config.MAX_TRADE_USD * 100) / cost_per_contract) if cost_per_contract > 0 else 0
                        
                        quantity = max(1, min(max_from_book, max_from_balance, max_from_trade_limit))
                        
                        print(f"\n🎯 {'EVENT' if opp_type == 'multi_leg' else 'MARKET'}: {ticker_label} ({opp['profit_percent']:.2f}% profit, qty={quantity})")
                        
                        if opp_type == 'multi_leg':
                            # Multi-leg: place YES orders on each leg
                            print(f"   Multi-leg trade: {opp.get('num_legs')} outcomes")
                            if not Config.LIVE_TRADING_ENABLED:
                                # Paper trade
                                total_cost_usd = (cost_per_contract * quantity) / 100
                                trade = {
                                    'ticker': opp.get('event_ticker'),
                                    'type': 'multi_leg_arbitrage',
                                    'quantity': quantity,
                                    'legs': opp.get('legs', []),
                                    'cost': total_cost_usd,
                                    'expected_profit': (quantity * 100 - cost_per_contract * quantity) / 100,
                                    'timestamp': datetime.now().isoformat()
                                }
                                trader.trade_history.append(trade)
                                trader.balance -= total_cost_usd
                                trader.positions.append(trade)
                                print(f"   📄 Paper trade recorded: ${total_cost_usd:.2f}")
                            else:
                                # Live multi-leg execution
                                total_cost_usd = (cost_per_contract * quantity) / 100
                                if total_cost_usd > Config.MAX_TRADE_USD:
                                    print(f"   ⚠️ Exceeds max trade (${total_cost_usd:.2f} > ${Config.MAX_TRADE_USD})")
                                    continue
                                
                                all_orders = []
                                failed = False
                                for leg in opp.get('legs', []):
                                    order = api.place_order(leg['ticker'], 'yes', quantity, leg['yes_price'], order_type='limit')
                                    if order:
                                        all_orders.append(order)
                                    else:
                                        print(f"   ❌ Failed on leg {leg['ticker']} — attempting to cancel previous legs")
                                        for prev_order in all_orders:
                                            api.cancel_order(prev_order.get('order_id', ''))
                                        failed = True
                                        break
                                    time.sleep(Config.RATE_LIMIT_DELAY)
                                
                                if not failed:
                                    trade = {
                                        'ticker': opp.get('event_ticker'),
                                        'type': 'multi_leg_arbitrage',
                                        'quantity': quantity,
                                        'cost': total_cost_usd,
                                        'expected_profit': (quantity * 100 - cost_per_contract * quantity) / 100,
                                        'timestamp': datetime.now().isoformat(),
                                        'orders': all_orders
                                    }
                                    trader.trade_history.append(trade)
                                    trader.balance -= total_cost_usd
                                    trader.positions.append(trade)
                                    print(f"   ✅ All {len(all_orders)} legs executed!")
                        else:
                            # Single-market arbitrage
                            success = trader.execute_arbitrage(opp, quantity=quantity)
                            if success:
                                print(f"   ✅ Trade executed")
                            else:
                                print(f"   ⚠️ Trade not executed (safety limits)")
                        
                        time.sleep(Config.RATE_LIMIT_DELAY)
                else:
                    print(f"\n📊 No arbitrage opportunities found this scan")
                
                # Print current stats
                trader.print_stats()
                
                # Wait for next scan
                print(f"\n⏳ Waiting {Config.SCAN_INTERVAL_SECONDS}s until next scan...")
                print(f"Total scans: {iteration} | Total trades: {len(trader.trade_history)}")
                
                # Capital recycling - check for settled positions
                recycled = trader._capital_recycle()
                if recycled > 0:
                    print(f"♻️  Recycled capital from {recycled} settled positions")
                
                time.sleep(Config.SCAN_INTERVAL_SECONDS)
                
        except KeyboardInterrupt:
            print(f"\n\n{'='*60}")
            print("🛑 Scanner stopped by user")
            print(f"{'='*60}")
            trader.print_stats()
            print("\nFinal summary:")
            print(f"- Total scans: {iteration}")
            print(f"- Total opportunities found: {len(bot.opportunities_found)}")
            print(f"- Total trades executed: {len(trader.trade_history)}")
            print(f"{'='*60}")
    
    elif choice == "9":
        # Scan crypto markets for probability edge
        from probability_trader import ProbabilityTrader
        
        print("\n" + "="*60)
        print("📊 SCANNING CRYPTO MARKETS FOR PROBABILITY EDGE")
        print("="*60)
        print(f"Min edge required: {Config.MIN_EDGE_PERCENT}%")
        print(f"BTC vol estimate: {Config.BTC_15MIN_VOL*100:.2f}%")
        print(f"ETH vol estimate: {Config.ETH_15MIN_VOL*100:.2f}%")
        print("="*60 + "\n")
        
        prob_trader = ProbabilityTrader(api, Config)
        opportunities = prob_trader.scan_crypto_markets()
        
        if opportunities:
            print(f"\n✅ Found {len(opportunities)} probability opportunities:\n")
            for i, opp in enumerate(opportunities, 1):
                print(f"{i}. {opp['ticker']} - {opp['strategy']}")
                print(f"   Side: {opp['side'].upper()} at {opp['price']}¢")
                print(f"   Edge: {opp['edge_percent']:.2f}% (est. prob: {opp['estimated_prob']*100:.1f}%, implied: {opp['implied_prob']*100:.1f}%)")
                print(f"   Kelly fraction: {opp['kelly_fraction']:.3f}")
                print(f"   Max qty: {opp['max_executable_qty']} | Time left: {opp['minutes_remaining']:.1f} min")
                print()
        else:
            print("\n📊 No probability edge opportunities found")
    
    elif choice == "10":
        # Continuous auto-trading for probability strategy
        from probability_trader import ProbabilityTrader
        from kelly import size_position
        
        print("\n" + "="*60)
        print("🤖 CONTINUOUS PROBABILITY AUTO-TRADING")
        print("="*60)
        print(f"Scan interval: 15 seconds (fast for crypto)")
        print(f"Min edge: {Config.MIN_EDGE_PERCENT}%")
        print(f"Kelly multiplier: {Config.KELLY_MULTIPLIER} (half-Kelly)")
        print(f"Live trading: {'ENABLED ⚠️' if Config.LIVE_TRADING_ENABLED else 'DISABLED (paper only)'}")
        print(f"Max trade: ${Config.MAX_TRADE_USD}")
        print(f"Press Ctrl+C to stop")
        print("="*60 + "\n")
        
        # Initialize trader
        starting_balance = api.get_balance() if Config.LIVE_TRADING_ENABLED else 250.0
        trader = KalshiTradingBot(
            api,
            initial_balance=starting_balance,
            paper_trading=not Config.LIVE_TRADING_ENABLED,
            storage=storage
        )
        
        prob_trader = ProbabilityTrader(api, Config)
        
        iteration = 0
        try:
            while True:
                iteration += 1
                print(f"\n{'='*60}")
                print(f"Probability Scan #{iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*60}")
                
                # Scan for probability opportunities
                opportunities = prob_trader.scan_crypto_markets()
                
                if opportunities:
                    # Sort by edge descending
                    opportunities.sort(key=lambda o: o.get('edge_percent', 0), reverse=True)
                    print(f"\n✅ Found {len(opportunities)} probability opportunities")
                    
                    for opp in opportunities:
                        ticker = opp['ticker']
                        side = opp['side']
                        price = opp['price']
                        edge_pct = opp['edge_percent']
                        est_prob = opp['estimated_prob']
                        
                        # Calculate position size using Kelly
                        quantity = size_position(
                            bankroll=trader.balance,
                            win_prob=est_prob,
                            contract_price_cents=price,
                            max_trade_usd=Config.MAX_TRADE_USD,
                            kelly_multiplier=Config.KELLY_MULTIPLIER
                        )
                        
                        # Limit to orderbook depth
                        quantity = min(quantity, opp.get('max_executable_qty', 1))
                        
                        if quantity < 1:
                            print(f"\n⚠️ {ticker}: Insufficient balance for trade")
                            continue
                        
                        cost_usd = (price * quantity) / 100.0
                        expected_profit = ((1.0 - price/100.0) * est_prob - (price/100.0) * (1-est_prob)) * quantity
                        
                        print(f"\n🎯 {ticker} - {opp['strategy']}")
                        print(f"   {side.upper()} @ {price}¢ x {quantity} = ${cost_usd:.2f}")
                        print(f"   Edge: {edge_pct:.2f}% | Expected profit: ${expected_profit:.2f}")
                        
                        # Execute trade (paper or live)
                        if trader.paper_trading:
                            trade = {
                                'ticker': ticker,
                                'type': 'probability',
                                'side': side,
                                'quantity': quantity,
                                'price': price,
                                'cost': cost_usd,
                                'expected_profit': expected_profit,
                                'edge_percent': edge_pct,
                                'timestamp': datetime.now().isoformat()
                            }
                            trader.trade_history.append(trade)
                            trader.balance -= cost_usd
                            trader.positions.append(trade)
                            print(f"   📄 Paper trade recorded")
                        else:
                            # Live trading
                            if cost_usd > Config.MAX_TRADE_USD:
                                print(f"   ⚠️ Exceeds max trade size")
                                continue
                            
                            if len(trader.positions) >= Config.MAX_POSITIONS:
                                print(f"   ⚠️ Position limit reached")
                                continue
                            
                            order = api.place_order(ticker, side, quantity, price, order_type='limit')
                            if order:
                                trade = {
                                    'ticker': ticker,
                                    'type': 'probability',
                                    'side': side,
                                    'quantity': quantity,
                                    'price': price,
                                    'cost': cost_usd,
                                    'expected_profit': expected_profit,
                                    'edge_percent': edge_pct,
                                    'timestamp': datetime.now().isoformat(),
                                    'order': order
                                }
                                trader.trade_history.append(trade)
                                trader.balance -= cost_usd
                                trader.positions.append(trade)
                                print(f"   ✅ Live order placed")
                            else:
                                print(f"   ❌ Order failed")
                        
                        time.sleep(Config.RATE_LIMIT_DELAY)
                else:
                    print(f"\n📊 No probability opportunities found this scan")
                
                # Print stats
                trader.print_stats()
                
                # Capital recycling
                recycled = trader._capital_recycle()
                if recycled > 0:
                    print(f"♻️  Recycled capital from {recycled} settled positions")
                
                # Wait for next scan (15 seconds for fast crypto markets)
                print(f"\n⏳ Waiting 15 seconds until next scan...")
                time.sleep(15)
                
        except KeyboardInterrupt:
            print(f"\n\n{'='*60}")
            print("🛑 Probability scanner stopped by user")
            print(f"{'='*60}")
            trader.print_stats()
            print(f"\nTotal probability scans: {iteration}")
            print(f"Total trades: {len(trader.trade_history)}")
            print(f"{'='*60}")
    
    else:
        print("Invalid choice")


if __name__ == "__main__":
    main()