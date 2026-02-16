import requests
import time
import hmac
import hashlib
from typing import Dict, List, Optional
from datetime import datetime
import json
from config import Config
from urllib.parse import urlparse
import base64
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.backends import default_backend
import base64
import jwt
import time as _time
from datetime import timezone

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
    
    def login(self) -> bool:
        """Authenticate with Kalshi"""
        try:
            endpoint = f"{self.BASE_URL}/login"
            payload = {
                "email": self.email,
                "password": self.password
            }
            response = self.session.post(endpoint, json=payload)
            
            if response.status_code == 200:
                data = response.json()
                self.token = data.get('token')
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
    
    def get_markets(self, status: str = "open", limit: int = 100) -> List[Dict]:
        """Fetch active markets"""
        try:
            endpoint = f"{self.BASE_URL}/markets"
            params = {
                'status': status,
                'limit': limit
            }
            # construct path without query for signing
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/markets"
            headers = self._signed_headers('GET', path)
            response = self.session.get(endpoint, params=params, headers=headers if headers else None)
            
            if response.status_code == 200:
                data = response.json()
                return data.get('markets', [])
            else:
                print(f"Error fetching markets: {response.status_code}")
                if response.status_code == 401:
                    body = response.text[:200] if response.text else '(empty)'
                    print(f"  Response body: {body}")
                return []
        except Exception as e:
            print(f"Error: {e}")
            return []
    
    def get_market(self, ticker: str) -> Optional[Dict]:
        """Get specific market by ticker"""
        try:
            endpoint = f"{self.BASE_URL}/markets/{ticker}"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/markets/{ticker}"
            headers = self._signed_headers('GET', path)
            response = self.session.get(endpoint, headers=headers if headers else None)
            
            if response.status_code == 200:
                return response.json().get('market')
            return None
        except Exception as e:
            print(f"Error fetching market {ticker}: {e}")
            return None
    
    def get_orderbook(self, ticker: str) -> Dict:
        """Get orderbook for a market"""
        try:
            endpoint = f"{self.BASE_URL}/markets/{ticker}/orderbook"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/markets/{ticker}/orderbook"
            headers = self._signed_headers('GET', path)
            response = self.session.get(endpoint, headers=headers if headers else None)
            
            if response.status_code == 200:
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
            print(f"Error fetching orderbook: {e}")
            return {}
    
    def get_trades(self, ticker: str, limit: int = 100) -> List[Dict]:
        """Get recent trades for a market"""
        try:
            endpoint = f"{self.BASE_URL}/markets/{ticker}/trades"
            params = {'limit': limit}
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/markets/{ticker}/trades"
            headers = self._signed_headers('GET', path)
            response = self.session.get(endpoint, params=params, headers=headers if headers else None)
            
            if response.status_code == 200:
                return response.json().get('trades', [])
            return []
        except Exception as e:
            print(f"Error fetching trades: {e}")
            return []

    # try_private_key_auth removed — replaced by _test_auth() + _signed_headers()
    
    def get_balance(self) -> float:
        """Get account balance (requires authentication)"""
        try:
            endpoint = f"{self.BASE_URL}/portfolio/balance"
            path_prefix = urlparse(self.BASE_URL).path.rstrip('/')
            path = f"{path_prefix}/portfolio/balance"
            headers = self._signed_headers('GET', path)
            response = self.session.get(endpoint, headers=headers if headers else None)
            
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
            response = self.session.post(endpoint, json=payload, headers=headers if headers else None)
            
            if response.status_code == 201:
                return response.json().get('order')
            else:
                print(f"Order failed: {response.status_code} - {response.text}")
                return None
        except Exception as e:
            print(f"Error placing order: {e}")
            return None


class KalshiArbitrageBot:
    """Arbitrage detection bot for Kalshi"""
    
    def __init__(self, api: KalshiAPI, min_profit_percent: float = 2.0):
        self.api = api
        self.min_profit_percent = min_profit_percent
        self.opportunities_found = []
    
    def analyze_market_mispricing(self, market: Dict, orderbook: Dict) -> Optional[Dict]:
        """
        Detect mispricing in a single market
        YES + NO should equal 100 cents, look for deviations
        """
        try:
            yes_asks = orderbook.get('yes_asks', [])
            no_asks = orderbook.get('no_asks', [])
            
            if not yes_asks or not no_asks:
                return None
            
            best_yes_ask = min([ask[0] for ask in yes_asks])
            best_no_ask = min([ask[0] for ask in no_asks])
            
            total_cost = best_yes_ask + best_no_ask
            guaranteed_payout = 100;
            
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
                    'strategy': 'Buy both YES and NO, guaranteed profit on settlement',
                    'timestamp': datetime.now().isoformat()
                }
            
            return None
            
        except Exception as e:
            print(f"Error analyzing market: {e}")
            return None
    
    def scan_all_markets(self, category_filter: Optional[str] = None) -> List[Dict]:
        """Scan all markets for arbitrage opportunities"""
        print(f"🔍 Scanning Kalshi markets...")
        
        markets = self.api.get_markets(status="open")
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
                
                time.sleep(0.3)
                
            except Exception as e:
                print(f"  Error scanning {ticker}: {e}")
                continue
        
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
                        else:
                            print(f"✓ {ticker}: No opportunity (spread too small)")
                        
                        time.sleep(0.5)
                        
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
                 paper_trading: bool = True):
        self.api = api
        self.paper_trading = paper_trading
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.positions = []
        self.trade_history = []
    
    def execute_arbitrage(self, opportunity: Dict, quantity: int = 1):
        """Execute arbitrage trade (buy both YES and NO)""" 
        
        ticker = opportunity['ticker']
        yes_price = opportunity['yes_price']
        no_price = opportunity['no_price']
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
                'timestamp': datetime.now().isoformat()
            }

            self.trade_history.append(trade)
            self.balance -= total_cost
            self.positions.append(trade)

            print(f"✅ Paper trade recorded")
            print(f"Remaining balance: ${self.balance:.2f}")

        else:
            # Live trading path with safeguards
            if not Config.LIVE_TRADING_ENABLED:
                print("\n⚠️  LIVE TRADING DISABLED - enable by setting ENABLE_LIVE_TRADING=true in .env")
                return False

            if total_cost > Config.MAX_TRADE_USD:
                print(f"\n⚠️  Trade exceeds configured max (${Config.MAX_TRADE_USD:.2f}). Refusing to place live orders.")
                return False

            print(f"\n⚠️  LIVE TRADING MODE - About to place real orders (SAFE MODE)")
            print(f"Market: {opportunity['title']}")
            print(f"YES price: ${yes_price/100:.2f} x {quantity}")
            print(f"NO price: ${no_price/100:.2f} x {quantity}")
            print(f"Total cost: ${total_cost:.2f}")

            confirmation = input("Type 'YES' to confirm and place live orders: ").strip().upper()
            if confirmation != 'YES':
                print("Aborted by user. No orders placed.")
                return False

            print("Placing YES order...")
            yes_order = self.api.place_order(ticker, 'yes', quantity, yes_price, order_type='limit')
            print("Placing NO order...")
            no_order = self.api.place_order(ticker, 'no', quantity, no_price, order_type='limit')

            if yes_order and no_order:
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
                    'no_order': no_order
                }

                self.trade_history.append(trade)
                self.balance -= total_cost
                self.positions.append(trade)
                print(f"Remaining balance: ${self.balance:.2f}")
            else:
                print("❌ One or both orders failed. Check API responses and the exchange UI to reconcile." )

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
        print(f"{'='*60}")


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
    bot = KalshiArbitrageBot(api, min_profit_percent=Config.MIN_PROFIT_PERCENT)
    
    print("Choose mode:")
    print("1. Scan all markets once")
    print("2. Scan specific category (e.g., BTC, NASDAQ)")
    print("3. Monitor specific tickers continuously")
    print("4. Demo paper trading")
    
    choice = input("\nEnter choice (1-4): ").strip()
    
    if choice == "1":
        opportunities = bot.scan_all_markets()
        print(f"\n✅ Scan complete: Found {len(opportunities)} opportunities")
        
    elif choice == "2":
        category = input("Enter category keyword (BTC, NASDAQ, etc.): ").strip()
        opportunities = bot.scan_all_markets(category_filter=category)
        print(f"\n✅ Scan complete: Found {len(opportunities)} opportunities")
        
    elif choice == "3":
        print("\nExample tickers: KXBTC-23DEC31-T50000, INX-23DEC-T4500")
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
    
    else:
        print("Invalid choice")


if __name__ == "__main__":
    main()