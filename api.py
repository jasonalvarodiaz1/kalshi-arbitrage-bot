import requests
import time
import random
import threading
import logging
from typing import Dict, List, Optional
from datetime import datetime, timezone
from config import Config
from urllib.parse import urlparse
import base64
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.backends import default_backend


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
        self._local = threading.local()
        self._local.session = self.session  # Main thread reuses shared session

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

    def _get_session(self) -> requests.Session:
        """Return a thread-local session with the same default headers.
        
        Each thread gets its own session to prevent concurrent request
        corruption in ThreadPoolExecutor workers.
        """
        if not hasattr(self._local, 'session'):
            s = requests.Session()
            s.headers.update({
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            })
            self._local.session = s
        return self._local.session

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
                response = self._get_session().request(method, url, **kwargs)
                
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

    def get_all_markets(self, status: str = "open", series_ticker: str = None, max_pages: int = 0) -> List[Dict]:
        """Fetch markets using pagination.

        Args:
            status: Market status filter (default 'open').
            series_ticker: Optional series ticker filter.
            max_pages: Maximum pages to fetch (0 = no limit, fetches all pages).
        """
        all_markets = []
        cursor = None
        page = 0
        while True:
            page += 1
            if max_pages > 0 and page > max_pages:
                break
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
                    price: int, order_type: str = "limit",
                    expiration_ts: int = None) -> Optional[Dict]:
        """
        Place an order (REAL MONEY - BE CAREFUL!)
        
        Args:
            ticker: Market ticker (e.g., "KXBTC-23DEC31-T50000")
            side: "yes" or "no"
            quantity: Number of contracts
            price: Price in cents (e.g., 50 = $0.50)
            order_type: "limit" or "market"
            expiration_ts: Unix timestamp for order expiration (auto-cancel)
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
                "no_price": price if side == "no" else None,
                "expiration_ts": expiration_ts,
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


