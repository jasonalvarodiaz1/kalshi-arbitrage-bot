"""
WebSocket-Powered Convergence Trader for Kalshi Crypto Bracket Markets.

Uses Kalshi's WebSocket API for real-time orderbook streaming instead of
polling every 15s. This catches stale orders within milliseconds of price
moves, dramatically improving execution quality.

Architecture:
  1. REST API: Initial market scan to find near-expiry events
  2. WebSocket: Stream orderbook_delta + ticker for those markets
  3. CoinGecko: BTC/ETH/DOGE/XRP price feed (still polled, 15s cache)
  4. On each orderbook/ticker update: re-evaluate model → trade if edge found

Connection: wss://api.elections.kalshi.com/trade-api/ws/v2
Auth: RSA-PSS signed headers (same as REST)
"""

import asyncio
import base64
import csv
import json
import math
import os
import time
import logging
import requests
import websockets
from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime, timezone
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.backends import default_backend

try:
    from scipy.stats import norm
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

from config import Config

logger = logging.getLogger('kalshi_bot')

# ─── WebSocket endpoint ──────────────────────────────────────────────────

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"
REST_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class WSConvergenceTrader:
    """Real-time convergence trader using WebSocket orderbook streaming."""

    def __init__(self, api=None, config=None):
        self.api = api  # KalshiBot instance for REST operations
        self.config = config or Config

        # Auth
        self.api_key = self.config.KALSHI_API_KEY
        self.private_key = None
        self._load_private_key()
        self.salt_length = asym_padding.PSS.MAX_LENGTH

        # Price feed
        self.price_cache: Dict[str, Tuple[float, float]] = {}  # {asset: (price, ts)}
        self.price_cache_ttl = 10  # seconds

        # Orderbook state (from WebSocket)
        self.orderbooks: Dict[str, Dict] = {}  # {market_ticker: {yes: [[price,qty]], no: [[price,qty]]}}
        self.ticker_data: Dict[str, Dict] = {}  # {market_ticker: {yes_bid, yes_ask, price, ...}}

        # Market metadata (from REST scan)
        self.market_meta: Dict[str, Dict] = {}  # {ticker: {floor_strike, cap_strike, event_ticker, close_time, asset}}
        self.subscribed_tickers: Set[str] = set()

        # Trading state
        self.traded_tickers: Dict[str, float] = {}  # {(ticker,side): timestamp}
        self.trades_attempted = 0
        self.trades_succeeded = 0
        self.trades_cancelled = 0
        self.paper_pnl = 0.0

        # Pending order tracking — maps order_id → order info
        # Each entry: {order_id, ticker, side, qty, price, placed_at, status}
        self.pending_orders: Dict[str, Dict] = {}
        self.filled_orders: List[Dict] = []     # completed fills for P&L tracking
        self.order_timeout_secs = 30             # cancel unfilled orders after 30s (orders also auto-expire at 60s)
        self._order_check_interval = 5           # poll pending orders every 5s
        self._last_order_check = 0.0

        # Exposure tracking
        self.total_exposure = 0.0                # dollars currently at risk (cost of open positions)
        self.max_total_exposure = float(getattr(Config, 'MAX_EXPOSURE_USD', 100.0))
        self.max_trades_per_event = 1            # STRICT: only 1 bracket per event — prevents adjacent losses
        self.max_exposure_per_event = 15.0        # reduced — limit per-event risk
        self.event_trade_count: Dict[str, int] = {}  # {event_ticker: count}
        self.event_exposure: Dict[str, float] = {}   # {event_ticker: dollars}

        # ── Risk Framework (5-rule system) ──────────────────────────────
        # Rule 1: Thesis (Asset) exposure — crypto assets ARE the thesis
        self.asset_exposure: Dict[str, float] = {}   # {asset: dollars}
        self.max_asset_pct = 0.20                    # 20% of bankroll per asset (sector cap)

        # Rule 4: Hard caps (% of bankroll, not fixed dollar amounts)
        self.max_event_pct = 0.05                    # 5% of bankroll per event
        self.stop_loss_pct = 0.15                    # 15% drawdown from starting equity → halt
        self.starting_balance = 171.18               # starting equity for drawdown tracking
        self.stop_loss_triggered = False             # True after stop-loss fires; clears on recovery

        # Rule 5: CPPI — dynamic floor protection
        # Allocation = multiplier * (equity - floor)
        # When equity drops, max allocation shrinks automatically
        self.cppi_multiplier = 3.0                   # aggressive=5, conservative=2
        self.cppi_floor_pct = 0.70                   # protect 70% of starting capital

        # ── Weather market configuration ────────────────────────────
        self.weather_series = {
            'KXHIGHLAX':  {'city': 'LAX',   'type': 'high', 'lat': 34.05,  'lon': -118.24, 'name': 'Los Angeles'},
            'KXHIGHCHI':  {'city': 'CHI',   'type': 'high', 'lat': 41.88,  'lon': -87.63,  'name': 'Chicago'},
            'KXHIGHDEN':  {'city': 'DEN',   'type': 'high', 'lat': 39.74,  'lon': -104.98, 'name': 'Denver'},
            'KXHIGHTLV':  {'city': 'TLV',   'type': 'high', 'lat': 36.17,  'lon': -115.14, 'name': 'Las Vegas'},
            'KXHIGHPHIL': {'city': 'PHIL',  'type': 'high', 'lat': 39.95,  'lon': -75.17,  'name': 'Philadelphia'},
            'KXLOWTNYC':  {'city': 'NYC_L', 'type': 'low',  'lat': 40.71,  'lon': -74.01,  'name': 'New York City'},
            'KXLOWTCHI':  {'city': 'CHI_L', 'type': 'low',  'lat': 41.88,  'lon': -87.63,  'name': 'Chicago'},
        }
        self.weather_forecast_cache: Dict[str, Dict] = {}  # {series: {temp, sigma, fetched_at, date}}
        self.weather_forecast_ttl = 900  # refresh forecast every 15 min
        self.weather_max_expiry_hours = 48  # allow weather markets up to 48h out

        # Paper settlement tracking
        self.paper_trades: List[Dict] = []       # all paper trades for settlement scoring
        self.paper_settled_pnl = 0.0              # actual P&L from settled paper trades
        self.paper_wins = 0
        self.paper_losses = 0
        self.paper_results_file = 'paper_results.csv'
        self._init_paper_results_file()
        self._settled_events: Set[str] = set()    # events already settled

        # Strategy params
        self.max_expiry_minutes = 60
        self.min_expiry_minutes = 2
        self.min_confidence = 0.60               # 60% model confidence minimum
        self.min_edge_pct = 5.0                   # 5% minimum edge — below this is noise
        self.max_trade_usd = float(getattr(self.config, 'MAX_TRADE_USD', 20.0))
        self.max_contracts = 15                      # Hard cap: 15 contracts per trade
        self.min_price_cents = 10                  # Minimum price to consider
        self.max_yes_price_cents = 30              # Max 30c for YES (risk/reward filter)
        self.min_book_depth = 5                    # Need 5+ real depth — no thin books
        self.atm_buffer_brackets = 1               # Skip the ATM bracket (model worst there)

        # Vol estimates — raised 50% to reduce overconfidence on ATM brackets
        self.base_vol = {'BTC': 0.0023, 'ETH': 0.0030, 'DOGE': 0.0045, 'XRP': 0.0038, 'SOL': 0.0035}
        # 15-minute binary up/down series
        self._15m_series = ['KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXXRP15M']
        # Weather forecast sigma: base_sigma * sqrt(hours_left / 6)
        self.weather_base_sigma = 1.8  # °F base uncertainty at 6h out
        self.calibrated_vol: Dict[str, float] = {}  # {event_ticker: implied_vol}

        # WebSocket state
        self.ws = None
        self.msg_id = 1
        self._reconnect_delay = 1
        self._max_reconnect_delay = 60

        # Control
        self._running = False
        self._last_scan_time = 0
        self._scan_interval = 60  # Re-scan REST markets every 60s

    def _load_private_key(self):
        pk_pem = Config.load_private_key()
        if pk_pem:
            try:
                self.private_key = serialization.load_pem_private_key(
                    pk_pem.encode('utf-8'), password=None, backend=default_backend()
                )
            except Exception as e:
                logger.error("Failed to load private key: %s", e)

    def _sign(self, method: str, path: str) -> Dict[str, str]:
        """Generate Kalshi RSA-PSS auth headers."""
        if not self.private_key or not self.api_key:
            return {}
        timestamp = str(int(time.time() * 1000))
        msg = (timestamp + method + path).encode('utf-8')
        signature = self.private_key.sign(
            msg,
            asym_padding.PSS(
                mgf=asym_padding.MGF1(hashes.SHA256()),
                salt_length=self.salt_length
            ),
            hashes.SHA256()
        )
        return {
            'KALSHI-ACCESS-KEY': self.api_key,
            'KALSHI-ACCESS-TIMESTAMP': timestamp,
            'KALSHI-ACCESS-SIGNATURE': base64.b64encode(signature).decode('ascii'),
        }

    # ─── CDF / probability model ─────────────────────────────────────────

    @staticmethod
    def _cdf(x: float) -> float:
        if SCIPY_AVAILABLE:
            return float(norm.cdf(x))
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    def bracket_probability(self, price: float, floor_s: Optional[float],
                            cap_s: Optional[float], mins_left: float,
                            asset: str, impl_vol: Optional[float] = None) -> float:
        """P(floor < price_at_expiry < cap) via log-normal model."""
        if mins_left <= 0 or price <= 0:
            return 0.0
        vol_15m = impl_vol or self.base_vol.get(asset, 0.002)
        sigma = vol_15m * math.sqrt(mins_left / 15.0)
        if sigma <= 0:
            sigma = 0.0001

        if cap_s and cap_s > 0:
            z_cap = math.log(cap_s / price) / sigma
            p_below_cap = self._cdf(z_cap)
        else:
            p_below_cap = 1.0

        if floor_s and floor_s > 0:
            z_floor = math.log(floor_s / price) / sigma
            p_below_floor = self._cdf(z_floor)
        else:
            p_below_floor = 0.0

        return max(0.0, min(1.0, p_below_cap - p_below_floor))

    def _implied_vol(self, price: float, floor_s: float, cap_s: float,
                     mins_left: float, market_prob: float) -> Optional[float]:
        """Bisection search for implied vol from ATM bracket."""
        if market_prob <= 0.01 or market_prob >= 0.99 or mins_left <= 0:
            return None
        lo, hi = 0.0001, 0.05
        for _ in range(50):
            mid = (lo + hi) / 2
            p = self.bracket_probability(price, floor_s, cap_s, mins_left, 'BTC', mid)
            if p < market_prob:
                hi = mid  # Price is in bracket → lower vol → higher prob
            else:
                lo = mid
        return (lo + hi) / 2

    # ─── Price feed ───────────────────────────────────────────────────────

    def get_price(self, asset: str) -> Optional[float]:
        if asset in self.price_cache:
            price, ts = self.price_cache[asset]
            if time.time() - ts < self.price_cache_ttl:
                return price
        return self._fetch_prices().get(asset)

    _ASSETS = ['BTC', 'ETH', 'DOGE', 'XRP', 'SOL']
    _COIN_IDS = 'bitcoin,ethereum,dogecoin,ripple,solana'
    _ASSET_MAP = [('BTC', 'bitcoin'), ('ETH', 'ethereum'), ('DOGE', 'dogecoin'), ('XRP', 'ripple'), ('SOL', 'solana')]

    def _fetch_prices(self) -> Dict[str, float]:
        now = time.time()
        all_fresh = all(
            a in self.price_cache and now - self.price_cache[a][1] < self.price_cache_ttl
            for a in self._ASSETS
        )
        if all_fresh:
            return {a: self.price_cache[a][0] for a in self._ASSETS if a in self.price_cache}

        # Try CoinGecko first
        result = self._try_coingecko(now)
        if result:
            return result

        # Fallback: CryptoCompare (no key needed)
        result = self._try_cryptocompare(now)
        if result:
            return result

        # Last resort: return cached prices
        return {a: self.price_cache[a][0] for a in self._ASSETS if a in self.price_cache}

    def _try_coingecko(self, now: float) -> Optional[Dict[str, float]]:
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={'ids': self._COIN_IDS, 'vs_currencies': 'usd'},
                timeout=5
            )
            if r.status_code == 429:
                return None
            r.raise_for_status()
            data = r.json()
            result = {}
            for asset, cid in self._ASSET_MAP:
                p = data.get(cid, {}).get('usd')
                if p:
                    self.price_cache[asset] = (p, now)
                    result[asset] = p
            return result if result else None
        except Exception:
            return None

    def _try_cryptocompare(self, now: float) -> Optional[Dict[str, float]]:
        try:
            r = requests.get(
                "https://min-api.cryptocompare.com/data/pricemulti",
                params={'fsyms': 'BTC,ETH,DOGE,XRP,SOL', 'tsyms': 'USD'},
                timeout=5
            )
            r.raise_for_status()
            data = r.json()
            cc_map = {'BTC': 'BTC', 'ETH': 'ETH', 'DOGE': 'DOGE', 'XRP': 'XRP', 'SOL': 'SOL'}
            result = {}
            for asset, sym in cc_map.items():
                p = data.get(sym, {}).get('USD')
                if p:
                    self.price_cache[asset] = (p, now)
                    result[asset] = p
            return result if result else None
        except Exception:
            return None

    # ─── Weather forecast feed ───────────────────────────────────────────

    def _fetch_weather_forecast(self, series_ticker: str) -> Optional[Dict]:
        """Fetch temperature forecast from Open-Meteo for a weather series.
        
        Returns {forecast_temp: float, date: str, hours_left: float} or None.
        Uses cache to avoid hitting the API too frequently.
        """
        now = time.time()
        cached = self.weather_forecast_cache.get(series_ticker)
        if cached and now - cached.get('fetched_at', 0) < self.weather_forecast_ttl:
            return cached

        ws = self.weather_series.get(series_ticker)
        if not ws:
            return None

        try:
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    'latitude': ws['lat'],
                    'longitude': ws['lon'],
                    'daily': 'temperature_2m_max,temperature_2m_min',
                    'temperature_unit': 'fahrenheit',
                    'timezone': 'America/New_York',
                    'forecast_days': 3,
                },
                timeout=8
            )
            r.raise_for_status()
            data = r.json()
            daily = data.get('daily', {})
            dates = daily.get('time', [])
            highs = daily.get('temperature_2m_max', [])
            lows = daily.get('temperature_2m_min', [])

            if not dates:
                return None

            # Build forecasts for each date
            forecasts = {}
            for i, d in enumerate(dates):
                forecasts[d] = {
                    'high': highs[i] if i < len(highs) else None,
                    'low': lows[i] if i < len(lows) else None,
                }

            result = {
                'forecasts': forecasts,
                'fetched_at': now,
                'city': ws['city'],
                'type': ws['type'],
                'name': ws['name'],
            }
            self.weather_forecast_cache[series_ticker] = result
            logger.info("Weather forecast for %s (%s): %s",
                        series_ticker, ws['name'],
                        {d: f"H={v['high']}F L={v['low']}F" for d, v in forecasts.items()})
            return result
        except Exception as e:
            logger.warning("Failed to fetch weather forecast for %s: %s", series_ticker, e)
            return cached  # return stale cache if available

    def _get_weather_temp_and_sigma(self, series_ticker: str, event_date: str,
                                     hours_left: float) -> Optional[tuple]:
        """Get forecast temperature and uncertainty for a weather event.
        
        Returns (forecast_temp, sigma) or None.
        """
        forecast = self._fetch_weather_forecast(series_ticker)
        if not forecast:
            return None

        ws = self.weather_series.get(series_ticker, {})
        temp_type = ws.get('type', 'high')
        forecasts = forecast.get('forecasts', {})

        # Find the matching date in forecasts
        temp = None
        for d, vals in forecasts.items():
            if event_date.startswith(d.replace('-', '')):
                # Date format mismatch: event uses 26FEB19, forecast uses 2026-02-19
                temp = vals.get(temp_type)
                break

        # Try matching by converting event_date format
        if temp is None:
            # event_date format: "26FEB19" → parse to yyyy-mm-dd
            try:
                from datetime import datetime as _dt
                # Extract just the date part from event ticker (e.g., KXHIGHLAX-26FEB18 → 26FEB18)
                parsed = _dt.strptime(event_date, '%y%b%d')
                iso_date = parsed.strftime('%Y-%m-%d')
                vals = forecasts.get(iso_date, {})
                temp = vals.get(temp_type)
            except (ValueError, TypeError):
                pass

        if temp is None:
            return None

        # Sigma scales with time: base_sigma * sqrt(hours_left / 6)
        # At 6h out: sigma = base (1.8°F)
        # At 24h out: sigma = 1.8 * 2 = 3.6°F
        # At 48h out: sigma = 1.8 * 2.83 = 5.1°F
        sigma = self.weather_base_sigma * math.sqrt(max(1, hours_left) / 6.0)
        # Floor at 1.5°F — forecasts always have some error
        sigma = max(1.5, sigma)

        return (temp, sigma)

    def weather_probability(self, forecast_temp: float, sigma: float,
                             floor_s: Optional[float], cap_s: Optional[float],
                             strike_type: str) -> float:
        """P(temp lands in bracket) using Normal distribution around forecast.
        
        strike_type: 'between', 'less', 'greater'
        """
        if strike_type == 'between' and floor_s is not None and cap_s is not None:
            z_cap = (cap_s - forecast_temp) / sigma
            z_floor = (floor_s - forecast_temp) / sigma
            return max(0.0, min(1.0, self._cdf(z_cap) - self._cdf(z_floor)))
        elif strike_type == 'less' and cap_s is not None:
            z = (cap_s - forecast_temp) / sigma
            return max(0.0, min(1.0, self._cdf(z)))
        elif strike_type.startswith('greater') and floor_s is not None:
            z = (floor_s - forecast_temp) / sigma
            return max(0.0, min(1.0, 1.0 - self._cdf(z)))
        return 0.0

    # ─── REST market scanning ────────────────────────────────────────────

    def _parse_asset(self, ticker: str) -> Optional[str]:
        if 'KXBTC' in ticker:
            return 'BTC'
        if 'KXETH' in ticker:
            return 'ETH'
        if 'KXDOGE' in ticker:
            return 'DOGE'
        if 'KXXRP' in ticker:
            return 'XRP'
        if 'KXSOL' in ticker:
            return 'SOL'
        return None

    @staticmethod
    def _fmt_price(p):
        """Format a price for display — handles small coins like DOGE/XRP."""
        if p is None:
            return 'N/A'
        if abs(p) < 1:
            return f'{p:.6f}'
        elif abs(p) < 100:
            return f'{p:.4f}'
        else:
            return f'{p:,.0f}'

    def _minutes_until(self, close_time: str) -> Optional[float]:
        try:
            close = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
            return (close - datetime.now(timezone.utc)).total_seconds() / 60
        except (ValueError, TypeError):
            return None

    def scan_markets(self) -> List[str]:
        """
        REST-scan for near-expiry crypto bracket markets AND 15-min binary up/down.
        Returns list of market tickers to subscribe to via WebSocket.
        Rebuilds market_meta from scratch so expired events are dropped.
        """
        tickers_to_sub = []
        new_market_meta = {}  # Rebuild from scratch — old events will disappear
        try:
            btc = self.api.get_all_markets(status="open", series_ticker="KXBTC")
            eth = self.api.get_all_markets(status="open", series_ticker="KXETH")
            doge = self.api.get_all_markets(status="open", series_ticker="KXDOGE")
            xrp = self.api.get_all_markets(status="open", series_ticker="KXXRP")
        except Exception as e:
            logger.error("REST scan failed: %s", e)
            return []

        all_markets = btc + eth + doge + xrp

        # Also fetch 15-minute binary up/down markets
        for series_15m in self._15m_series:
            try:
                m15 = self.api.get_all_markets(status="open", series_ticker=series_15m)
                all_markets.extend(m15)
            except Exception:
                pass
        events: Dict[str, List[Dict]] = {}
        for m in all_markets:
            et = m.get('event_ticker', '')
            events.setdefault(et, []).append(m)

        prices = {a: self.get_price(a) for a in self._ASSETS}
        price_parts = []
        for a in self._ASSETS:
            p = prices.get(a)
            if a in ('BTC', 'ETH'):
                price_parts.append(f"{a}=${p:,.0f}" if p else f"{a}=$N/A")
            else:
                price_parts.append(f"{a}=${p:.4f}" if p else f"{a}=$N/A")
        logger.info("Prices: %s", "  ".join(price_parts))

        for event_ticker, brackets in events.items():
            ct = brackets[0].get('close_time', '')
            mins = self._minutes_until(ct) if ct else None
            if mins is None or mins > self.max_expiry_minutes or mins < self.min_expiry_minutes:
                continue

            asset = self._parse_asset(event_ticker)
            if not asset or not prices.get(asset):
                continue

            current_price = prices[asset]

            # Extract floor/cap strikes — use custom_strike if standard fields are missing
            for b in brackets:
                if b.get('floor_strike') is None or b.get('cap_strike') is None:
                    cs = b.get('custom_strike', {})
                    if cs:
                        try:
                            if cs.get('floor_strike') and b.get('floor_strike') is None:
                                b['floor_strike'] = float(cs['floor_strike'])
                            if cs.get('cap_strike') and b.get('cap_strike') is None:
                                b['cap_strike'] = float(cs['cap_strike'])
                        except (ValueError, TypeError):
                            pass

            # Calibrate implied vol from ATM bracket
            sorted_b = sorted(brackets, key=lambda b: b.get('floor_strike') or 0)
            atm = None
            for b in sorted_b:
                fs, cs = b.get('floor_strike'), b.get('cap_strike')
                if fs is not None and cs is not None and fs <= current_price < cs:
                    atm = b
                    break

            if atm:
                yb = atm.get('yes_bid', 0) or 0
                ya = atm.get('yes_ask', 0) or 0
                if yb > 0 and ya > 0:
                    mkt_prob = (yb + ya) / 200.0
                    iv = self._implied_vol(current_price, atm.get('floor_strike'),
                                           atm.get('cap_strike'), mins, mkt_prob)
                    if iv:
                        self.calibrated_vol[event_ticker] = iv
                        logger.info("Event %s: IV=%.3f%% (15min), %.0f min left, %d brackets",
                                     event_ticker, iv * 100, mins, len(brackets))

            # Store metadata and collect tickers
            for b in brackets:
                ticker = b.get('ticker', '')
                if not ticker:
                    continue

                # GUARD: skip markets with BOTH strikes missing (API returns
                # incomplete data for newly-created markets — trading on
                # null strikes gives model_prob=1.0 which is always wrong)
                if b.get('floor_strike') is None and b.get('cap_strike') is None:
                    continue

                # Detect binary up/down markets (no cap_strike, strike_type is 'greater' or 'greater_or_equal')
                is_binary_updown = (b.get('cap_strike') is None
                                    and b.get('floor_strike') is not None
                                    and b.get('strike_type', '').startswith('greater'))
                new_market_meta[ticker] = {
                    'floor_strike': b.get('floor_strike'),
                    'cap_strike': b.get('cap_strike'),
                    'event_ticker': event_ticker,
                    'close_time': ct,
                    'asset': asset,
                    'market_type': 'binary_updown' if is_binary_updown else 'bracket',
                }
                tickers_to_sub.append(ticker)
                if is_binary_updown:
                    logger.info("15m binary: %s strike=%.2f, %s, %.0f min left",
                                ticker, b.get('floor_strike', 0), asset, mins)

        # Replace market_meta entirely so expired events are dropped
        self.market_meta = new_market_meta

        # ── Weather market scanning ─────────────────────────────────────
        weather_count = 0
        for ws_series, ws_info in self.weather_series.items():
            try:
                w_markets = self.api.get_all_markets(status="open", series_ticker=ws_series)
                if not w_markets:
                    continue
            except Exception:
                continue

            w_events: Dict[str, List[Dict]] = {}
            for m in w_markets:
                et = m.get('event_ticker', '')
                w_events.setdefault(et, []).append(m)

            for w_event, w_brackets in w_events.items():
                ct = w_brackets[0].get('close_time', '')
                mins = self._minutes_until(ct) if ct else None
                if mins is None or mins < 60 or mins > self.weather_max_expiry_hours * 60:
                    continue  # weather: skip < 1h or > 48h

                # Extract event date from event_ticker: KXHIGHLAX-26FEB18 → 26FEB18
                parts = w_event.split('-')
                event_date = parts[-1] if len(parts) >= 2 else ''

                for b in w_brackets:
                    ticker = b.get('ticker', '')
                    if not ticker:
                        continue

                    fs = b.get('floor_strike')
                    cs = b.get('cap_strike')
                    strike_type = b.get('strike_type', 'between')

                    self.market_meta[ticker] = {
                        'floor_strike': fs,
                        'cap_strike': cs,
                        'event_ticker': w_event,
                        'close_time': ct,
                        'asset': ws_info['city'],
                        'market_type': 'weather',
                        'weather_series': ws_series,
                        'strike_type': strike_type,
                        'event_date': event_date,
                    }
                    tickers_to_sub.append(ticker)
                    weather_count += 1

                    # Seed ticker_data from REST bid/ask (WS may not deliver updates for thin markets)
                    yb = b.get('yes_bid', 0) or 0
                    ya = b.get('yes_ask', 0) or 0
                    if yb > 0 or ya > 0:
                        if ticker not in self.ticker_data or not self.ticker_data[ticker].get('yes_bid'):
                            self.ticker_data[ticker] = {
                                'yes_bid': yb, 'yes_ask': ya,
                                'price': b.get('price', 0), 'volume': b.get('volume', 0),
                                'open_interest': b.get('open_interest', 0),
                            }

            # Pre-fetch forecast for this series
            self._fetch_weather_forecast(ws_series)

        if weather_count > 0:
            logger.info("Weather: %d tickers across %d series", weather_count, len(self.weather_series))

        logger.info("Found %d total tickers (crypto + weather)", len(tickers_to_sub))
        return tickers_to_sub

    # ─── Trade evaluation (called on each WS update) ─────────────────────

    # ─── Binary up/down (15-min) evaluation ──────────────────────────────

    def _evaluate_binary_updown(self, ticker: str, meta: dict,
                                 current_price: float, mins_left: float) -> Optional[Dict]:
        """Evaluate a 15-minute binary up/down market.

        YES = price >= strike at expiry (price goes UP from open).

        Strategy: Only trade when the CHEAP side (<35c) has a large model edge.
        This gives favorable risk/reward: risk 35c to win 65c+.
        We need a directional conviction + cheap price, not fair-value trades.

        Key insight: our log-normal model has NO inherent edge vs market makers.
        So we ONLY trade when:
          1. The option is cheap (<35c) — limits downside
          2. Model shows very high probability (>65%) — strong directional call
          3. This means we're buying far-from-ATM: e.g. price is well
             above strike → YES is expensive, NO is cheap → buy NO (contrarian)
             OR price is well below strike → YES is cheap → buy YES
          4. Implied vol reality check: skip if market vol >> our vol (market
             sees more risk than we do)
        """
        asset = meta['asset']
        strike = meta['floor_strike']
        event = meta['event_ticker']

        if not strike or strike <= 0:
            return None

        # Skip very short time — too noisy, mean reversion dominates
        if mins_left < 3:
            return None

        # Use base vol — but inflate slightly for safety (model uncertainty)
        vol_15m = self.base_vol.get(asset, 0.002) * 1.3  # 30% vol buffer
        sigma = vol_15m * math.sqrt(mins_left / 15.0)
        if sigma <= 0:
            sigma = 0.0001

        # P(price >= strike at expiry)
        z = math.log(strike / current_price) / sigma
        model_prob_yes = 1.0 - self._cdf(z)
        model_prob_no = 1.0 - model_prob_yes

        # ── Orderbook ──
        td = self.ticker_data.get(ticker, {})
        ob = self.orderbooks.get(ticker, {})

        yes_bid = td.get('yes_bid', 0) or 0
        yes_ask = td.get('yes_ask', 0) or 0
        yes_bids = ob.get('yes', [])
        no_bids = ob.get('no', [])

        if yes_bids:
            yes_bid = max(p for p, q in yes_bids)
        if not yes_ask and no_bids:
            best_no_bid = max(p for p, q in no_bids)
            yes_ask = 100 - best_no_bid
        no_ask = (100 - yes_bid) if yes_bid > 0 else 0

        # ── Implied vol reality check ──
        # If market mid-price diverges greatly from model, the market knows
        # something we don't (higher vol, news, etc.) — skip.
        if yes_ask > 0 and no_ask > 0:
            mkt_mid = yes_ask / 100.0  # market's implied P(YES)
            # If model and market disagree by >20% on the probability,
            # AND the market is pricing closer to 50/50 than we are,
            # the market likely has better vol info
            if abs(model_prob_yes - mkt_mid) > 0.20:
                # Check if market vol would be much higher than ours
                # (market closer to 50/50 = higher vol)
                if abs(mkt_mid - 0.5) < abs(model_prob_yes - 0.5):
                    return None  # market thinks vol is higher — defer

        # Depth
        yes_depth = 0
        if yes_ask > 0 and no_bids:
            matching = 100 - yes_ask
            for p, q in no_bids:
                if p == matching:
                    yes_depth = q
                    break
        no_depth = 0
        if yes_bid > 0 and yes_bids:
            for p, q in yes_bids:
                if p == yes_bid:
                    no_depth = q
                    break

        # ── Binary thresholds: MUCH stricter than bracket ──
        binary_max_price = 35        # only buy cheap options (≤35c)
        binary_min_edge = 10.0       # need 10%+ edge (model is noisy)
        binary_max_edge = 30.0       # >30% is stale-price artifact
        binary_min_conf = 0.65       # need 65%+ model confidence
        binary_min_depth = 5         # need 5+ contracts depth

        # ── Check YES opportunity (price going UP — only when cheap) ──
        if (yes_ask >= self.min_price_cents and yes_ask <= binary_max_price
                and yes_depth >= binary_min_depth):
            implied = yes_ask / 100.0
            edge = (model_prob_yes - implied) * 100
            if (edge >= binary_min_edge and edge <= binary_max_edge
                    and model_prob_yes >= binary_min_conf):
                return {
                    'ticker': ticker, 'event': event, 'side': 'yes',
                    'price': yes_ask, 'model_prob': model_prob_yes,
                    'implied_prob': implied, 'edge_pct': edge,
                    'minutes_left': mins_left, 'asset': asset,
                    'current_price': current_price,
                    'floor': strike, 'cap': None, 'impl_vol': vol_15m,
                    'book_depth': yes_depth,
                    'market_type': 'binary_updown',
                }

        # ── Check NO opportunity (price going DOWN — only when cheap) ──
        if (no_ask >= self.min_price_cents and no_ask <= binary_max_price
                and no_depth >= binary_min_depth):
            implied_no = no_ask / 100.0
            edge_no = (model_prob_no - implied_no) * 100
            if (edge_no >= binary_min_edge and edge_no <= binary_max_edge
                    and model_prob_no >= binary_min_conf):
                return {
                    'ticker': ticker, 'event': event, 'side': 'no',
                    'price': no_ask, 'model_prob': model_prob_no,
                    'implied_prob': implied_no, 'edge_pct': edge_no,
                    'minutes_left': mins_left, 'asset': asset,
                    'current_price': current_price,
                    'floor': strike, 'cap': None, 'impl_vol': vol_15m,
                    'book_depth': no_depth,
                    'market_type': 'binary_updown',
                }

        return None

    # ─── Weather bracket evaluation ─────────────────────────────────────

    def _evaluate_weather(self, ticker: str, meta: dict,
                           mins_left: float) -> Optional[Dict]:
        """Evaluate a weather temperature bracket market.
        
        Uses Normal CDF around Open-Meteo forecast to find mispricings.
        Weather markets have strike_type: 'between', 'less', 'greater'.
        """
        ws_series = meta.get('weather_series', '')
        event = meta['event_ticker']
        event_date = meta.get('event_date', '')
        strike_type = meta.get('strike_type', 'between')
        floor_s = meta['floor_strike']
        cap_s = meta['cap_strike']

        hours_left = mins_left / 60.0

        # Get forecast temperature and uncertainty
        ts_result = self._get_weather_temp_and_sigma(ws_series, event_date, hours_left)
        if not ts_result:
            return None
        forecast_temp, sigma = ts_result

        # Model probability
        model_prob = self.weather_probability(forecast_temp, sigma, floor_s, cap_s, strike_type)

        # Skip if model gives extreme probability (not useful for edge detection)
        if model_prob < 0.02 or model_prob > 0.98:
            return None

        # ── Orderbook data ──
        td = self.ticker_data.get(ticker, {})
        ob = self.orderbooks.get(ticker, {})

        yes_bid = td.get('yes_bid', 0) or 0
        yes_ask = td.get('yes_ask', 0) or 0

        # Enrich from orderbook if available
        yes_bids = ob.get('yes', [])
        no_bids = ob.get('no', [])

        if yes_bids:
            yes_bid = max(max(p for p, q in yes_bids), yes_bid)
        if not yes_ask and no_bids:
            best_no_bid = max(p for p, q in no_bids)
            yes_ask = 100 - best_no_bid

        no_ask = (100 - yes_bid) if yes_bid > 0 else 0

        # Depth
        no_depth = 0
        if yes_bid > 0 and yes_bids:
            for p, q in yes_bids:
                if p == yes_bid:
                    no_depth = q
                    break
        yes_depth = 0
        if yes_ask > 0 and no_bids:
            matching = 100 - yes_ask
            for p, q in no_bids:
                if p == matching:
                    yes_depth = q
                    break

        # ── ATM buffer: skip bracket containing the forecast temp ──
        is_atm = False
        if strike_type == 'between' and floor_s is not None and cap_s is not None:
            if floor_s <= forecast_temp < cap_s:
                is_atm = True
            # Also skip adjacent brackets (forecast ±1 bracket width)
            bracket_width = cap_s - floor_s
            if abs(forecast_temp - (floor_s + cap_s) / 2) < bracket_width * 1.5:
                is_atm = True

        if is_atm:
            return None

        model_prob_no = 1.0 - model_prob

        # Check YES opportunity — buy YES if model says bracket is more likely than market
        if (yes_ask >= 5 and yes_ask <= 40 and yes_depth >= 1):
            implied_prob = yes_ask / 100.0
            edge = (model_prob - implied_prob) * 100
            if edge >= self.min_edge_pct and model_prob >= 0.55:
                return {
                    'ticker': ticker, 'event': event, 'side': 'yes',
                    'price': yes_ask, 'model_prob': model_prob,
                    'implied_prob': implied_prob, 'edge_pct': edge,
                    'minutes_left': mins_left, 'asset': meta['asset'],
                    'current_price': forecast_temp,
                    'floor': floor_s, 'cap': cap_s, 'impl_vol': sigma,
                    'book_depth': max(yes_depth, 1),
                    'market_type': 'weather',
                }

        # Check NO opportunity — buy NO if model says bracket is unlikely
        if (no_ask >= 5 and no_ask <= 85 and no_depth >= 1):
            implied_no = no_ask / 100.0
            edge_no = (model_prob_no - implied_no) * 100
            min_edge = self.min_edge_pct
            # Tiered: expensive NO needs more edge (nearer to forecast)
            if no_ask > 60:
                min_edge = max(min_edge, 8.0)
            if edge_no >= min_edge and model_prob_no >= 0.55:
                return {
                    'ticker': ticker, 'event': event, 'side': 'no',
                    'price': no_ask, 'model_prob': model_prob_no,
                    'implied_prob': implied_no, 'edge_pct': edge_no,
                    'minutes_left': mins_left, 'asset': meta['asset'],
                    'current_price': forecast_temp,
                    'floor': floor_s, 'cap': cap_s, 'impl_vol': sigma,
                    'book_depth': max(no_depth, 1),
                    'market_type': 'weather',
                }

        return None

    # ─── Main evaluation dispatch ──────────────────────────────────────

    def evaluate_opportunity(self, ticker: str) -> Optional[Dict]:
        """
        Evaluate a single market for convergence opportunity.
        Called whenever we get a WS update for this ticker.
        """
        meta = self.market_meta.get(ticker)
        if not meta:
            return None

        market_type = meta.get('market_type', 'bracket')

        # ── Dispatch weather markets to dedicated handler ──
        if market_type == 'weather':
            mins_left = self._minutes_until(meta['close_time'])
            if mins_left is None or mins_left < 60 or mins_left > self.weather_max_expiry_hours * 60:
                return None
            return self._evaluate_weather(ticker, meta, mins_left)

        asset = meta['asset']
        current_price = self.get_price(asset)
        if not current_price:
            return None

        mins_left = self._minutes_until(meta['close_time'])
        if mins_left is None or mins_left < self.min_expiry_minutes or mins_left > self.max_expiry_minutes:
            return None

        # ── Dispatch binary up/down markets to dedicated handler ──
        if market_type == 'binary_updown':
            return self._evaluate_binary_updown(ticker, meta, current_price, mins_left)

        floor_s = meta['floor_strike']
        cap_s = meta['cap_strike']
        event = meta['event_ticker']

        # GUARD: reject markets with both strikes missing — API may return
        # incomplete data for newly-created markets. Without bounds,
        # bracket_probability returns 1.0 which is meaningless.
        if floor_s is None and cap_s is None:
            return None

        impl_vol = self.calibrated_vol.get(event)

        model_prob = self.bracket_probability(current_price, floor_s, cap_s, mins_left, asset, impl_vol)

        # ───────────────────────────────────────────────────────────────
        # Orderbook interpretation:
        #   WS orderbook 'yes' / 'no' levels are BIDS (buy orders).
        #   YES ask = 100 - max(NO bids)  (counterparty matching)
        #   NO  ask = 100 - max(YES bids)
        #   Depth at NO ask = depth at the matching YES bid level
        # ───────────────────────────────────────────────────────────────
        td = self.ticker_data.get(ticker, {})
        ob = self.orderbooks.get(ticker, {})

        # YES bid/ask from ticker data
        yes_bid_ticker = td.get('yes_bid', 0) or 0
        yes_ask = td.get('yes_ask', 0) or 0

        # Orderbook levels (these are BIDS)
        yes_bids = ob.get('yes', [])  # YES buy orders
        no_bids = ob.get('no', [])    # NO buy orders

        # Best YES bid from orderbook (most accurate for fill prices)
        yes_bid_ob = max(p for p, q in yes_bids) if yes_bids else 0

        # Use orderbook YES bid if available; fall back to ticker
        yes_bid = yes_bid_ob if yes_bid_ob > 0 else yes_bid_ticker

        # Derive YES ask from NO bids if ticker data unavailable
        if not yes_ask and no_bids:
            best_no_bid = max(p for p, q in no_bids)
            yes_ask = 100 - best_no_bid

        # NO ask = 100 - best YES bid (use orderbook for accuracy)
        no_ask = 0
        no_depth = 0
        if yes_bid > 0:
            no_ask = 100 - yes_bid
            # Depth at NO ask = depth at the matching YES bid
            for p, q in yes_bids:
                if p == yes_bid:
                    no_depth = q
                    break
            # NO DEPTH FALLBACK: if orderbook has no real depth, depth stays 0
            # Previously we faked depth=min_book_depth which caused phantom fills

        # YES depth from NO bids at matching level
        yes_depth = 0
        if yes_ask > 0 and no_bids:
            matching_no_bid = 100 - yes_ask
            for p, q in no_bids:
                if p == matching_no_bid:
                    yes_depth = q
                    break

        # Check YES opportunity — only if price is affordable
        if (yes_ask >= self.min_price_cents and yes_ask <= self.max_yes_price_cents
                and yes_depth >= self.min_book_depth):
            implied_prob = yes_ask / 100.0
            edge = (model_prob - implied_prob) * 100
            min_edge = self.min_edge_pct
            if model_prob >= 0.85 and mins_left <= 15:
                min_edge = max(3.0, self.min_edge_pct - 2.0)

            # Skip ATM bracket for YES — model is least accurate there
            is_atm = (floor_s is not None and cap_s is not None
                      and floor_s <= current_price < cap_s)
            if is_atm:
                pass  # skip ATM bracket entirely for YES
            elif edge >= min_edge and model_prob >= self.min_confidence:
                return {
                    'ticker': ticker, 'event': event, 'side': 'yes',
                    'price': yes_ask, 'model_prob': model_prob,
                    'implied_prob': implied_prob, 'edge_pct': edge,
                    'minutes_left': mins_left, 'asset': asset,
                    'current_price': current_price,
                    'floor': floor_s, 'cap': cap_s, 'impl_vol': impl_vol,
                    'book_depth': yes_depth,
                }

        # Check NO opportunity — prefer cheap NO on far-OTM brackets
        model_prob_no = 1.0 - model_prob

        # SKIP ATM and near-ATM brackets for NO too — model is unreliable there
        is_atm_no = False
        if floor_s is not None and cap_s is not None:
            bracket_width = cap_s - floor_s
            # Skip if current price is within 2 bracket widths of this bracket
            dist_to_bracket = min(abs(current_price - floor_s), abs(current_price - cap_s))
            if current_price >= floor_s and current_price < cap_s:
                dist_to_bracket = 0  # price is IN the bracket
            if dist_to_bracket < bracket_width * 2.0:
                is_atm_no = True  # ATM buffer (2x bracket width) — model unreliable near ATM

        if is_atm_no:
            pass  # Skip ATM/near-ATM NO trades entirely
        elif no_ask >= self.min_price_cents and no_ask < 95 and no_depth >= self.min_book_depth:
            implied_prob_no = no_ask / 100.0
            edge_no = (model_prob_no - implied_prob_no) * 100
            min_edge = self.min_edge_pct

            # Tiered edge requirements by NO price:
            # CHEAP NO (<25c) = very far OTM = model confident = lower edge OK
            # EXPENSIVE NO (>60c) = near ATM = model uncertain = need MORE edge
            if no_ask <= 25 and model_prob_no >= 0.88:
                min_edge = max(4.0, self.min_edge_pct)       # deep OTM: 4%+ edge if model is 88%+
            elif no_ask <= 40:
                min_edge = max(min_edge, 6.0)                  # moderate OTM: need 6%+ edge
            elif no_ask <= 60:
                min_edge = max(min_edge, 8.0)                  # near-ATM: need 8%+ edge
            else:
                min_edge = max(min_edge, 10.0)                 # expensive NO (60c+): need 10%+ edge

            # Also require high model confidence for NO bets
            min_conf_no = 0.60  # must be 60%+ confident in NO

            if edge_no >= min_edge and model_prob_no >= min_conf_no:
                return {
                    'ticker': ticker, 'event': event, 'side': 'no',
                    'price': no_ask, 'model_prob': model_prob_no,
                    'implied_prob': implied_prob_no, 'edge_pct': edge_no,
                    'minutes_left': mins_left, 'asset': asset,
                    'current_price': current_price,
                    'floor': floor_s, 'cap': cap_s, 'impl_vol': impl_vol,
                    'book_depth': no_depth,
                }

        return None

    def _book_depth_at(self, ticker: str, side: str, ask_price_cents: int) -> int:
        """Return quantity available at given ASK price.
        
        Since orderbook levels are BIDS, the depth at a YES ask comes from the
        NO bid at (100 - ask_price) and vice versa.
        """
        ob = self.orderbooks.get(ticker, {})
        counter_side = 'no' if side == 'yes' else 'yes'
        counter_price = 100 - ask_price_cents
        levels = ob.get(counter_side, [])
        for p, q in levels:
            if p == counter_price:
                return q
        return 0

    def size_trade(self, opp: Dict, balance: float) -> int:
        """Position sizing: Kelly/4 − liquidity penalty, with CPPI and hard caps.
        
        Framework:
          1. Base Kelly fraction (quarter for binary, half for bracket)
          2. Liquidity penalty: slash Kelly 50% if depth < 20
          3. CPPI: cap total allocation to multiplier * (equity - floor)
          4. Hard caps: 5% per event, 20% per asset
          5. Slippage guard: won't take > 50% of book depth
        """
        price_cents = opp['price']
        model_prob = opp['model_prob']
        cost = price_cents / 100.0
        net_win = 1.0 - cost
        if net_win <= 0 or cost <= 0:
            return 0
        kelly_mult = getattr(self.config, 'KELLY_MULTIPLIER', 0.5)

        # Use quarter-Kelly for binary (model is unreliable)
        is_binary = opp.get('market_type') == 'binary_updown'
        if is_binary:
            kelly_mult = kelly_mult * 0.5  # half of half-Kelly = quarter-Kelly

        # ── Rule 2: Liquidity-adjusted sizing ──
        # If book depth is thin, slash Kelly fraction to avoid slippage
        book_depth = opp.get('book_depth', 0)
        if book_depth < 20:
            kelly_mult *= 0.5  # thin book → half the Kelly fraction

        b = net_win / cost
        f = ((b * model_prob) - (1 - model_prob)) / b
        f = max(0, f * kelly_mult)

        # ── Rule 5: CPPI floor protection ──
        # Allocation = multiplier * (equity - floor)
        cppi_floor = self.starting_balance * self.cppi_floor_pct
        cushion = max(0, balance - cppi_floor)
        cppi_max_alloc = self.cppi_multiplier * cushion
        # CPPI caps the total dollars we can allocate to THIS trade
        # If equity is near floor, cppi_max_alloc shrinks toward 0

        max_trade = self.max_trade_usd
        max_qty = self.max_contracts
        if is_binary:
            max_trade = min(max_trade, 5.0)   # max $5 per binary trade
            max_qty = min(max_qty, 15)          # max 15 contracts per binary trade

        # ── Rule 4: Hard cap — 5% of bankroll per event ──
        event_cap = balance * self.max_event_pct
        event_exp = self.event_exposure.get(opp.get('event', ''), 0.0)
        event_remaining = max(0, event_cap - event_exp)

        # ── Rule 1: Thesis cap — 20% of bankroll per asset ──
        asset = opp.get('asset', '')
        asset_cap = balance * self.max_asset_pct
        asset_exp = self.asset_exposure.get(asset, 0.0)
        asset_remaining = max(0, asset_cap - asset_exp)

        # Apply all caps: Kelly, CPPI, max_trade, event cap, asset cap
        risk = min(f * balance, max_trade, cppi_max_alloc, event_remaining, asset_remaining)
        if risk <= 0:
            return 0

        contracts = int(risk / cost)
        contracts = min(max(0, contracts), max_qty)

        # ── Rule 2 continued: Slippage guard ──
        # Never take more than 50% of visible book depth
        if book_depth > 0:
            max_from_book = max(1, book_depth // 2)  # take at most half the book
            contracts = min(contracts, max_from_book)

        return contracts

    def _wait_for_fill(self, order_id: str, expected_qty: int, timeout: int = 30) -> Dict:
        """Poll order status until fully filled or timeout, then cancel remainder.

        Ported from the copilot/implement-partial-fill-monitoring PR.
        Returns dict: {filled_qty, unfilled_qty, status, cancelled}
        """
        poll_interval = 2  # seconds between polls
        elapsed = 0
        filled_qty = 0
        order_status = 'unknown'

        while elapsed < timeout:
            try:
                order_info = self.api.get_order(order_id)
            except Exception:
                order_info = None

            if order_info is None:
                pass  # no response yet, keep polling
            elif not isinstance(order_info, dict):
                # unexpected response — assume filled to avoid false cancellations
                logger.debug("_wait_for_fill %s: unexpected response type, assuming filled", order_id)
                return {'filled_qty': expected_qty, 'unfilled_qty': 0,
                        'status': 'filled', 'cancelled': False}
            else:
                order_data = order_info.get('order', order_info)
                if not isinstance(order_data, dict):
                    order_data = {}
                order_status = order_data.get('status', 'unknown')
                raw_filled = (order_data.get('filled_count') or
                              order_data.get('fill_count') or
                              order_data.get('quantity_filled') or 0)
                filled_qty = int(raw_filled) if isinstance(raw_filled, (int, float)) else 0

                logger.debug("_wait_for_fill %s: status=%s filled=%d/%d elapsed=%ds",
                             order_id, order_status, filled_qty, expected_qty, elapsed)

                if order_status in ('executed', 'filled', 'closed') or filled_qty >= expected_qty:
                    return {'filled_qty': filled_qty,
                            'unfilled_qty': max(0, expected_qty - filled_qty),
                            'status': order_status, 'cancelled': False}

            time.sleep(poll_interval)
            elapsed += poll_interval

        # Timeout — cancel unfilled remainder
        unfilled = max(0, expected_qty - filled_qty)
        logger.warning("_wait_for_fill %s: timed out after %ds, filled=%d/%d — cancelling remainder",
                       order_id, timeout, filled_qty, expected_qty)
        cancelled = False
        if unfilled > 0:
            try:
                cancelled = bool(self.api.cancel_order(order_id))
                if cancelled:
                    logger.info("   CANCELLED remainder of %s (%d contracts)", order_id[:8], unfilled)
                else:
                    logger.error("   CANCEL FAILED for %s — %d contracts may remain open", order_id[:8], unfilled)
            except Exception as ce:
                logger.error("   CANCEL ERROR for %s: %s", order_id[:8], ce)

        return {'filled_qty': filled_qty, 'unfilled_qty': unfilled,
                'status': order_status, 'cancelled': cancelled}

    def execute_trade(self, opp: Dict, balance: float) -> bool:
        """Execute a convergence trade (paper or live)."""
        trade_key = f"{opp['ticker']}_{opp['side']}"
        if trade_key in self.traded_tickers:
            return False

        # ── Rule 4: Stop-loss — halt if drawdown exceeds threshold ──
        if self.stop_loss_triggered:
            return False

        # Prevent trading opposite side on same bracket (guaranteed loss)
        opposite = 'no' if opp['side'] == 'yes' else 'yes'
        opposite_key = f"{opp['ticker']}_{opposite}"
        if opposite_key in self.traded_tickers:
            return False

        # ── Prevent adjacent/nearby bracket trades within same event ──
        # Trading NO on two nearby brackets guarantees one loss.
        # STRICT: only 1 trade per event (max_trades_per_event=1), but also
        # explicitly check bracket proximity as a safety net.
        event = opp.get('event', '')
        opp_floor = opp.get('floor')
        opp_cap = opp.get('cap')
        if opp_floor is not None and opp_cap is not None:
            bracket_width = opp_cap - opp_floor
            for existing_key in self.traded_tickers:
                existing_ticker = existing_key.rsplit('_', 1)[0]
                existing_meta = self.market_meta.get(existing_ticker, {})
                if existing_meta.get('event_ticker') != event:
                    continue
                ef = existing_meta.get('floor_strike')
                ec = existing_meta.get('cap_strike')
                if ef is None or ec is None:
                    continue
                # Block if brackets are within 4 bracket widths of each other
                center_new = (opp_floor + opp_cap) / 2
                center_existing = (ef + ec) / 2
                distance = abs(center_new - center_existing)
                if distance < bracket_width * 4:
                    return False  # too close — risk of adjacent loss

        # Check per-event trade cap
        if self.event_trade_count.get(event, 0) >= self.max_trades_per_event:
            return False

        # Check per-event exposure cap
        event_exp = self.event_exposure.get(event, 0.0)
        if event_exp >= self.max_exposure_per_event:
            return False

        # Check total exposure cap
        qty = self.size_trade(opp, balance)
        if qty <= 0:
            return False

        cost = qty * opp['price'] / 100.0

        # Trim to fit per-event exposure cap
        event_remaining = self.max_exposure_per_event - event_exp
        if cost > event_remaining:
            qty = int(event_remaining / (opp['price'] / 100.0))
            if qty <= 0:
                return False
            cost = qty * opp['price'] / 100.0

        if self.total_exposure + cost > self.max_total_exposure:
            remaining = self.max_total_exposure - self.total_exposure
            if remaining < 0.50:  # less than 50c headroom
                return False
            # Reduce qty to fit within exposure limit
            qty = int(remaining / (opp['price'] / 100.0))
            if qty <= 0:
                return False
            cost = qty * opp['price'] / 100.0

        is_paper = self.config.PAPER_TRADING
        is_live = getattr(self.config, 'LIVE_TRADING_ENABLED', False)
        weather_live_only = getattr(self.config, 'WEATHER_LIVE_ONLY', True)

        # Safety: if weather-only live mode, force paper for non-weather markets
        if is_live and weather_live_only and opp.get('market_type') != 'weather':
            is_paper = True
            is_live = False

        floor_str = f"${self._fmt_price(opp['floor'])}" if opp['floor'] else "(-inf)"
        cap_str = f"${self._fmt_price(opp['cap'])}" if opp['cap'] else "(+inf)"

        logger.info("")
        logger.info(">> %s  %s x%d @ %dc", opp['ticker'], opp['side'].upper(), qty, opp['price'])
        logger.info("   Model: %.1f%%  Market: %.1f%%  Edge: +%.1f%%  Mins: %.0f",
                     opp['model_prob'] * 100, opp['implied_prob'] * 100,
                     opp['edge_pct'], opp['minutes_left'])
        logger.info("   Bracket: %s — %s  |  %s @ $%s  |  Depth: %d",
                     floor_str, cap_str, opp['asset'], self._fmt_price(opp['current_price']),
                     opp.get('book_depth', 0))

        self.trades_attempted += 1

        if is_paper:
            logger.info("   PAPER BUY: %d contracts @ %dc = $%.2f", qty, opp['price'], cost)
            self.trades_succeeded += 1
            cost_per = opp['price'] / 100.0
            ev = qty * (opp['model_prob'] * 1.0 - cost_per)
            self.paper_pnl += ev
            self.traded_tickers[trade_key] = time.time()
            self.event_trade_count[event] = self.event_trade_count.get(event, 0) + 1
            self.event_exposure[event] = self.event_exposure.get(event, 0.0) + cost
            # Rule 1: Track thesis (asset) exposure
            asset = opp.get('asset', '')
            self.asset_exposure[asset] = self.asset_exposure.get(asset, 0.0) + cost
            # Store for settlement tracking
            self.paper_trades.append({
                'ticker': opp['ticker'], 'event': event, 'side': opp['side'],
                'qty': qty, 'price': opp['price'], 'cost': cost,
                'floor': opp['floor'], 'cap': opp['cap'],
                'asset': opp['asset'], 'current_price': opp['current_price'],
                'model_prob': opp['model_prob'], 'edge_pct': opp['edge_pct'],
                'minutes_left': opp['minutes_left'],
                'market_type': opp.get('market_type', 'bracket'),
                'placed_at': datetime.now(timezone.utc).isoformat(),
            })
            return True

        elif is_live:
            try:
                logger.info("   LIVE ORDER: %d %s @ %dc on %s",
                             qty, opp['side'].upper(), opp['price'], opp['ticker'])
                # Set order expiration to 60 seconds from now
                # This prevents orphan resting orders if the bot crashes
                import time as _time
                expiration_ts = int(_time.time()) + 60
                order = self.api.place_order(
                    ticker=opp['ticker'], side=opp['side'],
                    quantity=qty, price=opp['price'],
                    expiration_ts=expiration_ts
                )
                if order:
                    order_id = order.get('order_id', '')
                    status = order.get('status', 'unknown')
                    fill_count = order.get('fill_count', 0)

                    if status == 'executed' or fill_count >= qty:
                        # Fully filled immediately (taker fill)
                        logger.info("   FILLED: order_id=%s (immediate)", order_id)
                        self.trades_succeeded += 1
                        self.total_exposure += cost
                        self.filled_orders.append({
                            'order_id': order_id, 'ticker': opp['ticker'],
                            'side': opp['side'], 'qty': qty, 'price': opp['price'],
                            'cost': cost, 'filled_at': time.time(),
                        })
                    else:
                        # Order is resting — wait up to order_timeout_secs for a fill
                        logger.info("   RESTING: order_id=%s status=%s fill=%d/%d — polling for fill...",
                                     order_id, status, fill_count, qty)
                        self.total_exposure += cost  # reserve exposure while waiting
                        fill_result = self._wait_for_fill(order_id, expected_qty=qty,
                                                          timeout=self.order_timeout_secs)
                        actual_filled = fill_result['filled_qty']
                        actual_cost = actual_filled * opp['price'] / 100.0

                        if actual_filled >= qty:
                            # Fully filled during wait
                            logger.info("   FILLED (after wait): order_id=%s %d/%d contracts",
                                         order_id, actual_filled, qty)
                            self.trades_succeeded += 1
                            self.filled_orders.append({
                                'order_id': order_id, 'ticker': opp['ticker'],
                                'side': opp['side'], 'qty': actual_filled,
                                'price': opp['price'], 'cost': actual_cost,
                                'filled_at': time.time(),
                            })
                            # Adjust exposure to actual cost (may differ if price slipped)
                            self.total_exposure += actual_cost - cost
                        elif actual_filled > 0:
                            # Partial fill — record what we got, exposure already reserved
                            logger.warning("   PARTIAL FILL: %d/%d contracts filled on %s",
                                            actual_filled, qty, opp['ticker'])
                            self.trades_succeeded += 1
                            self.filled_orders.append({
                                'order_id': order_id, 'ticker': opp['ticker'],
                                'side': opp['side'], 'qty': actual_filled,
                                'price': opp['price'], 'cost': actual_cost,
                                'filled_at': time.time(),
                            })
                            # Release the unfilled portion of reserved exposure
                            self.total_exposure -= (cost - actual_cost)
                        else:
                            # Zero fill — cancel and undo everything
                            logger.warning("   NO FILL: order %s cancelled/timed out", order_id[:8])
                            self.total_exposure -= cost
                            self.traded_tickers.pop(trade_key, None)
                            self.event_trade_count[event] = max(0, self.event_trade_count.get(event, 1) - 1)
                            self.event_exposure[event] = max(0.0, self.event_exposure.get(event, 0.0) - cost)
                            return False  # no fill = no trade

                    self.traded_tickers[trade_key] = time.time()
                    self.event_trade_count[event] = self.event_trade_count.get(event, 0) + 1
                    self.event_exposure[event] = self.event_exposure.get(event, 0.0) + cost
                    # Rule 1: Track thesis (asset) exposure for live trades too
                    asset = opp.get('asset', '')
                    self.asset_exposure[asset] = self.asset_exposure.get(asset, 0.0) + cost
                    return True
                else:
                    logger.warning("   ORDER FAILED")
            except Exception as e:
                logger.error("   ORDER ERROR: %s", e)

        return False

    # ─── Pending order management ────────────────────────────────────────

    def _check_pending_orders(self):
        """Poll pending orders: confirm fills or cancel stale ones."""
        now = time.time()
        if now - self._last_order_check < self._order_check_interval:
            return
        self._last_order_check = now

        if not self.pending_orders:
            return

        to_remove = []
        for order_id, info in list(self.pending_orders.items()):
            age = now - info['placed_at']

            try:
                order = self.api.get_order(order_id)
                if not order:
                    # Can't fetch — cancel to be safe
                    if age > self.order_timeout_secs:
                        self._cancel_order(order_id, info, reason="timeout+fetch_fail")
                        to_remove.append(order_id)
                    continue

                status = order.get('status', '')
                fill_count = order.get('fill_count', 0)

                if status == 'executed' or fill_count >= info['qty']:
                    # Fully filled!
                    logger.info("   CONFIRMED FILL: %s %s x%d @ %dc (after %.1fs)",
                                 info['ticker'], info['side'].upper(), fill_count,
                                 info['price'], age)
                    self.trades_succeeded += 1
                    actual_cost = fill_count * info['price'] / 100.0
                    self.filled_orders.append({
                        'order_id': order_id, 'ticker': info['ticker'],
                        'side': info['side'], 'qty': fill_count,
                        'price': info['price'], 'cost': actual_cost,
                        'filled_at': now,
                    })
                    # Adjust exposure to actual fill amount
                    self.total_exposure += actual_cost - info['cost']
                    to_remove.append(order_id)

                elif status == 'canceled' or status == 'cancelled':
                    logger.info("   ORDER CANCELLED: %s (external)", order_id[:8])
                    self.total_exposure -= info['cost']
                    # Allow retry on this ticker
                    trade_key = f"{info['ticker']}_{info['side']}"
                    self.traded_tickers.pop(trade_key, None)
                    evt = info.get('event', info['ticker'].rsplit('-', 1)[0]) if '-B' in info['ticker'] else ''
                    if evt:
                        self.event_trade_count[evt] = max(0, self.event_trade_count.get(evt, 1) - 1)
                        self.event_exposure[evt] = max(0, self.event_exposure.get(evt, 0) - info['cost'])
                    to_remove.append(order_id)

                elif status == 'resting' and age > self.order_timeout_secs:
                    # Timed out — cancel it
                    partial = fill_count
                    if partial > 0:
                        # Partial fill: keep the filled portion
                        logger.info("   PARTIAL FILL+CANCEL: %s %d/%d filled, cancelling rest",
                                     info['ticker'], partial, info['qty'])
                        actual_cost = partial * info['price'] / 100.0
                        self.trades_succeeded += 1
                        self.filled_orders.append({
                            'order_id': order_id, 'ticker': info['ticker'],
                            'side': info['side'], 'qty': partial,
                            'price': info['price'], 'cost': actual_cost,
                            'filled_at': now,
                        })
                        self.total_exposure += actual_cost - info['cost']
                    else:
                        # No fills at all — release exposure and allow retry
                        self.total_exposure -= info['cost']
                        trade_key = f"{info['ticker']}_{info['side']}"
                        self.traded_tickers.pop(trade_key, None)
                        evt_ticker = info['ticker'].rsplit('-B', 1)[0] if '-B' in info['ticker'] else ''
                        if evt_ticker:
                            self.event_trade_count[evt_ticker] = max(0, self.event_trade_count.get(evt_ticker, 1) - 1)
                            self.event_exposure[evt_ticker] = max(0, self.event_exposure.get(evt_ticker, 0) - info['cost'])

                    self._cancel_order(order_id, info, reason="timeout")
                    to_remove.append(order_id)

            except Exception as e:
                logger.error("   Error checking order %s: %s", order_id[:8], e)
                if age > self.order_timeout_secs * 2:
                    self._cancel_order(order_id, info, reason="error+timeout")
                    to_remove.append(order_id)

        for oid in to_remove:
            self.pending_orders.pop(oid, None)

    def _cancel_order(self, order_id: str, info: Dict, reason: str = ""):
        """Cancel a single order via REST API."""
        try:
            success = self.api.cancel_order(order_id)
            if success:
                self.trades_cancelled += 1
                logger.info("   CANCELLED: %s %s x%d @ %dc (%s)",
                             info['ticker'], info['side'].upper(), info['qty'],
                             info['price'], reason)
            else:
                logger.warning("   Cancel failed for %s", order_id[:8])
        except Exception as e:
            logger.error("   Cancel error for %s: %s", order_id[:8], e)

    def _cancel_all_resting_orders(self):
        """Cancel ALL resting orders on shutdown/cleanup."""
        # Cancel tracked pending orders
        for order_id, info in list(self.pending_orders.items()):
            self._cancel_order(order_id, info, reason="shutdown")
            self.total_exposure -= info['cost']
        self.pending_orders.clear()

        # Also sweep for any resting orders via REST (safety net)
        try:
            headers = self._sign('GET', '/trade-api/v2/portfolio/orders')
            r = requests.get(f"{REST_BASE}/portfolio/orders",
                             params={'limit': 200, 'status': 'resting'},
                             headers=headers)
            if r.status_code == 200:
                orders = r.json().get('orders', [])
                for o in orders:
                    oid = o.get('order_id', '')
                    ticker = o.get('ticker', '')
                    self.api.cancel_order(oid)
                    logger.info("   SWEEP-CANCELLED: %s", ticker)
        except Exception as e:
            logger.error("   Sweep cancel error: %s", e)

    def _load_existing_positions(self):
        """Load existing positions from API to prevent duplicate trades after crash/restart.
        
        This is critical: without this, a bot crash clears traded_tickers and
        event_trade_count, allowing the bot to re-trade adjacent brackets in
        events where it already has positions.
        """
        try:
            positions = self.api.get_positions()
            loaded = 0
            for p in positions:
                ticker = p.get('market_ticker', p.get('ticker', ''))
                pos = p.get('position', 0)
                if pos == 0 or not ticker:
                    continue
                side = 'yes' if pos > 0 else 'no'
                trade_key = f"{ticker}_{side}"
                if trade_key not in self.traded_tickers:
                    self.traded_tickers[trade_key] = time.time()
                    loaded += 1
                    # Also update event tracking
                    # Extract event ticker from market ticker (e.g. KXBTC-26FEB1718-B67375 → KXBTC-26FEB1718)
                    parts = ticker.split('-')
                    if len(parts) >= 2:
                        event = '-'.join(parts[:2])
                        self.event_trade_count[event] = self.event_trade_count.get(event, 0) + 1
                        cost_est = abs(pos) * (p.get('average_price', 50) / 100.0)
                        self.event_exposure[event] = self.event_exposure.get(event, 0.0) + cost_est
                        self.total_exposure += cost_est
            if loaded > 0:
                logger.info("Loaded %d existing positions into traded_tickers (crash protection)", loaded)
            else:
                logger.info("No existing positions to load")
        except Exception as e:
            logger.warning("Failed to load existing positions: %s", e)

    # ─── Paper Settlement Tracking ───────────────────────────────────────

    def _init_paper_results_file(self):
        """Create CSV header if file doesn't exist."""
        if not os.path.exists(self.paper_results_file):
            with open(self.paper_results_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'event', 'ticker', 'asset', 'side', 'qty', 'price_cents',
                    'cost', 'floor', 'cap', 'settle_price', 'won', 'payout', 'pnl',
                    'model_prob', 'edge_pct', 'minutes_left',
                ])

    def _settle_paper_trades(self, event_ticker: str):
        """Settle all paper trades for an expired event using actual price/temperature."""
        if event_ticker in self._settled_events:
            return

        # Get trades for this event
        event_trades = [t for t in self.paper_trades if t['event'] == event_ticker]
        if not event_trades:
            return

        asset = event_trades[0]['asset']
        is_weather = event_trades[0].get('market_type') == 'weather'

        if is_weather:
            # For weather: use the forecast temp as settlement proxy
            # (actual observed temp not yet available — will be approximate)
            settle_price = event_trades[0].get('current_price')
            if not settle_price:
                logger.warning("Cannot settle weather %s — no forecast temp", event_ticker)
                return
            settle_label = f"{settle_price:.0f}°F (forecast)"
        else:
            settle_price = self.get_price(asset)
            if not settle_price:
                logger.warning("Cannot settle %s — no price available for %s", event_ticker, asset)
                return
            settle_label = f"${self._fmt_price(settle_price)}"

        self._settled_events.add(event_ticker)

        event_pnl = 0.0
        event_wins = 0
        event_losses = 0

        _fmt_price = self._fmt_price

        logger.info("")
        logger.info("=" * 60)
        logger.info("SETTLEMENT: %s  |  %s @ %s", event_ticker, asset, settle_label)
        logger.info("=" * 60)

        results_rows = []

        for trade in event_trades:
            floor_s = trade['floor']
            cap_s = trade['cap']
            side = trade['side']
            qty = trade['qty']
            cost = trade['cost']

            # Determine if price landed in this bracket
            in_bracket = True
            if floor_s is not None and settle_price < floor_s:
                in_bracket = False
            if cap_s is not None and settle_price >= cap_s:
                in_bracket = False

            # YES wins if price is in bracket, NO wins if price is NOT in bracket
            if side == 'yes':
                won = in_bracket
            else:
                won = not in_bracket

            payout = qty * 1.0 if won else 0.0
            pnl = payout - cost

            event_pnl += pnl
            if won:
                event_wins += 1
            else:
                event_losses += 1

            floor_str = f"${_fmt_price(floor_s)}" if floor_s else "(-inf)"
            cap_str = f"${_fmt_price(cap_s)}" if cap_s else "(+inf)"
            result_str = "WIN" if won else "LOSS"

            logger.info("  %s %s %s x%d @ %dc → %s  pnl=$%.2f  (bracket: %s—%s)",
                         trade['ticker'], side.upper(), result_str,
                         qty, trade['price'], f"${payout:.2f}", pnl,
                         floor_str, cap_str)

            results_rows.append([
                datetime.now(timezone.utc).isoformat(), event_ticker,
                trade['ticker'], asset, side, qty, trade['price'],
                f"{cost:.2f}", floor_s or '', cap_s or '', _fmt_price(settle_price),
                won, f"{payout:.2f}", f"{pnl:.2f}",
                f"{trade['model_prob']:.4f}", f"{trade['edge_pct']:.1f}",
                f"{trade['minutes_left']:.0f}",
            ])

        self.paper_settled_pnl += event_pnl
        self.paper_wins += event_wins
        self.paper_losses += event_losses

        total_trades = event_wins + event_losses
        win_rate = (event_wins / total_trades * 100) if total_trades > 0 else 0

        logger.info("-" * 60)
        logger.info("  Event P&L: $%.2f  |  %d/%d wins (%.0f%%)", event_pnl, event_wins, total_trades, win_rate)
        logger.info("  Cumulative settled P&L: $%.2f  |  %d/%d wins (%.0f%%)",
                     self.paper_settled_pnl, self.paper_wins, self.paper_wins + self.paper_losses,
                     (self.paper_wins / (self.paper_wins + self.paper_losses) * 100) if (self.paper_wins + self.paper_losses) > 0 else 0)
        logger.info("=" * 60)
        logger.info("")

        # Persist to CSV
        try:
            with open(self.paper_results_file, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerows(results_rows)
        except Exception as e:
            logger.error("Failed to write paper results: %s", e)

        # Remove settled trades from the list
        self.paper_trades = [t for t in self.paper_trades if t['event'] != event_ticker]

    # ─── WebSocket message handlers ──────────────────────────────────────

    def _handle_orderbook_snapshot(self, msg: Dict):
        """Process orderbook_snapshot from WS."""
        ticker = msg.get('market_ticker', '')
        self.orderbooks[ticker] = {
            'yes': msg.get('yes', []),
            'no': msg.get('no', []),
        }

    def _handle_orderbook_delta(self, msg: Dict):
        """Apply incremental orderbook update."""
        ticker = msg.get('market_ticker', '')
        side = msg.get('side', 'yes')
        price = msg.get('price', 0)
        delta = msg.get('delta', 0)

        if ticker not in self.orderbooks:
            self.orderbooks[ticker] = {'yes': [], 'no': []}

        levels = self.orderbooks[ticker].get(side, [])

        # Find existing level and update
        found = False
        for i, (p, q) in enumerate(levels):
            if p == price:
                new_q = q + delta
                if new_q <= 0:
                    levels.pop(i)
                else:
                    levels[i] = [p, new_q]
                found = True
                break

        if not found and delta > 0:
            levels.append([price, delta])
            levels.sort(key=lambda x: x[0])

        self.orderbooks[ticker][side] = levels

    def _handle_ticker(self, msg: Dict):
        """Process ticker update from WS."""
        ticker = msg.get('market_ticker', '')
        self.ticker_data[ticker] = {
            'yes_bid': msg.get('yes_bid', 0),
            'yes_ask': msg.get('yes_ask', 0),
            'price': msg.get('price', 0),
            'volume': msg.get('volume', 0),
            'open_interest': msg.get('open_interest', 0),
        }

    # ─── WebSocket connection ────────────────────────────────────────────

    async def _connect_ws(self) -> websockets.WebSocketClientProtocol:
        """Establish authenticated WebSocket connection."""
        headers = self._sign("GET", WS_PATH)
        ws = await websockets.connect(
            WS_URL,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        logger.info("WebSocket connected to %s", WS_URL)
        self._reconnect_delay = 1  # Reset backoff
        return ws

    async def _subscribe(self, ws, tickers: List[str]):
        """Subscribe to orderbook_delta + ticker for given markets."""
        if not tickers:
            return

        # Subscribe in batches of 50 to avoid message size limits
        batch_size = 50
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i + batch_size]
            msg = {
                "id": self.msg_id,
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta", "ticker"],
                    "market_tickers": batch,
                }
            }
            self.msg_id += 1
            await ws.send(json.dumps(msg))
            logger.info("Subscribed to %d markets (batch %d)", len(batch), i // batch_size + 1)

        self.subscribed_tickers.update(tickers)

    # ─── Main loop ───────────────────────────────────────────────────────

    async def run(self):
        """Main async event loop: connect WS, stream data, trade on updates."""
        is_paper = self.config.PAPER_TRADING
        is_live = getattr(self.config, 'LIVE_TRADING_ENABLED', False)
        weather_live_only = getattr(self.config, 'WEATHER_LIVE_ONLY', True)
        if is_live and weather_live_only:
            mode = "LIVE (weather only)"
        elif is_paper:
            mode = "PAPER"
        elif is_live:
            mode = "LIVE"
        else:
            mode = "DRY-RUN"

        logger.info("=" * 70)
        logger.info("WS CONVERGENCE TRADER — %s MODE", mode)
        logger.info("Real-time orderbook streaming via WebSocket")
        logger.info("Max expiry: %d min | Min edge: %.1f%% | Min confidence: %.0f%%",
                     self.max_expiry_minutes, self.min_edge_pct, self.min_confidence * 100)
        logger.info("Max trade: $%.2f | Kelly mult: %.1fx | Order timeout: %ds",
                     self.max_trade_usd, getattr(self.config, 'KELLY_MULTIPLIER', 0.5),
                     self.order_timeout_secs)
        logger.info("Max exposure: $%.2f | Max trades/event: %d",
                     self.max_total_exposure, self.max_trades_per_event)
        logger.info("=" * 70)

        self._running = True
        while self._running:
            try:
                await self._session()
                # Session ended normally (no-data timeout or no markets) — reconnect immediately
                logger.info("Session ended — re-scanning markets immediately")
                self._reconnect_delay = 1  # Reset backoff for normal exits
            except asyncio.CancelledError:
                logger.info("WS trader cancelled — cleaning up orders...")
                self._cancel_all_resting_orders()
                break
            except Exception as e:
                logger.error("WS session error: %s — reconnecting in %ds", e, self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

        logger.info("WS trader stopped | %d/%d trades (%d cancelled) | pnl=$%.2f | exposure=$%.2f",
                     self.trades_succeeded, self.trades_attempted, self.trades_cancelled,
                     self.paper_pnl, self.total_exposure)

    async def _session(self):
        """Single WS session: connect, subscribe, process messages."""
        # ── Cancel any orphaned resting orders from prior sessions ──
        logger.info("Cancelling any orphaned resting orders from prior sessions...")
        self._cancel_all_resting_orders()

        # ── Load existing positions to prevent duplicate trades after crash/restart ──
        self._load_existing_positions()

        # Settle any paper trades from events that expired before this session
        if self.config.PAPER_TRADING and self.paper_trades:
            # Get current active events from a fresh scan
            pre_scan_events = set(t['event'] for t in self.paper_trades)
            # We'll check after scan_markets rebuilds market_meta

        # Initial market scan
        tickers = self.scan_markets()

        # Settle paper trades for events that are no longer active after the scan
        if self.config.PAPER_TRADING and self.paper_trades:
            active_events = set(m.get('event_ticker', '') for m in self.market_meta.values())
            traded_events = set(t['event'] for t in self.paper_trades)
            for ev in traded_events:
                if ev not in active_events:
                    self._settle_paper_trades(ev)

        if not tickers:
            logger.warning("No qualifying markets found. Waiting 30s to retry...")
            await asyncio.sleep(30)
            return

        # Connect WebSocket
        ws = await self._connect_ws()
        self.ws = ws

        try:
            # Subscribe to markets
            await self._subscribe(ws, tickers)

            # Get initial balance
            try:
                balance = self.api.get_balance()
            except Exception:
                balance = 250.0
            self.total_exposure = 0.0  # Reset exposure each session
            # Set starting balance dynamically from current balance so stop-loss
            # is measured relative to THIS session's starting equity, not a stale
            # hardcoded value from a previous session.
            if balance > 0:
                self.starting_balance = balance
            # NOTE: Do NOT clear event_trade_count here — counters must persist across reconnects
            logger.info("Balance: $%.2f | Max exposure: $%.2f | Starting equity: $%.2f", balance, self.max_total_exposure, self.starting_balance)

            # Process messages
            updates_processed = 0
            opps_found = 0
            last_status = time.time()
            last_heartbeat = time.time()
            last_msg_time = time.time()
            HEARTBEAT_INTERVAL = 120  # log heartbeat every 2 min
            WS_RECV_TIMEOUT = 30     # seconds — run housekeeping even with no messages
            NO_DATA_TIMEOUT = 300    # 5 min with zero messages → force reconnect

            while True:
                # ── Receive next message with timeout ────────────────
                try:
                    raw_msg = await asyncio.wait_for(ws.recv(), timeout=WS_RECV_TIMEOUT)
                except asyncio.TimeoutError:
                    # No message in WS_RECV_TIMEOUT seconds — run housekeeping below
                    raw_msg = None
                except websockets.exceptions.ConnectionClosed:
                    logger.warning("WebSocket connection closed — will reconnect")
                    break

                # ── Process message if we got one ────────────────────
                if raw_msg is not None:
                    last_msg_time = time.time()
                    data = json.loads(raw_msg)
                    msg_type = data.get('type', '')

                    if msg_type == 'subscribed':
                        sid = data.get('msg', {}).get('sid', '?')
                        ch = data.get('msg', {}).get('channel', '?')
                        logger.info("Confirmed subscription: channel=%s sid=%s", ch, sid)

                    elif msg_type == 'orderbook_snapshot':
                        self._handle_orderbook_snapshot(data.get('msg', {}))
                        updates_processed += 1

                    elif msg_type == 'orderbook_delta':
                        self._handle_orderbook_delta(data.get('msg', {}))
                        updates_processed += 1
                        # Evaluate on every delta
                        ticker = data.get('msg', {}).get('market_ticker', '')
                        if ticker:
                            opp = self.evaluate_opportunity(ticker)
                            if opp:
                                opps_found += 1
                                self.execute_trade(opp, balance)

                    elif msg_type == 'ticker':
                        self._handle_ticker(data.get('msg', {}))
                        updates_processed += 1
                        # Also evaluate on ticker update
                        ticker = data.get('msg', {}).get('market_ticker', '')
                        if ticker:
                            opp = self.evaluate_opportunity(ticker)
                            if opp:
                                opps_found += 1
                                self.execute_trade(opp, balance)

                    elif msg_type == 'error':
                        err = data.get('msg', {})
                        logger.warning("WS error: code=%s msg=%s", err.get('code'), err.get('msg'))

                # ── No-data timeout → force reconnect to re-scan ─────
                if time.time() - last_msg_time >= NO_DATA_TIMEOUT:
                    logger.warning("No WS data for %ds — closing session to re-scan markets", NO_DATA_TIMEOUT)
                    break

                # Check pending orders for fills / timeouts
                self._check_pending_orders()

                # Heartbeat (every 2 min) — proves bot is alive even when no opps
                now = time.time()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                    logger.info("HEARTBEAT pid=%d | %d updates since last HB | exposure=$%.2f | filled=%d",
                                 os.getpid(), updates_processed, self.total_exposure, len(self.filled_orders))
                    last_heartbeat = now

                # Periodic status log (every 30s)
                now = time.time()
                if now - last_status >= 30:
                    pending_count = len(self.pending_orders)
                    filled_count = len(self.filled_orders)
                    # CPPI cushion info
                    cppi_floor = self.starting_balance * self.cppi_floor_pct
                    cppi_cushion = max(0, balance - cppi_floor)
                    cppi_max = self.cppi_multiplier * cppi_cushion
                    # Effective balance accounts for capital deployed in open live positions
                    eff_bal_status = balance + self.total_exposure
                    drawdown_pct = ((self.starting_balance - eff_bal_status) / self.starting_balance) * 100 if self.starting_balance > 0 else 0
                    logger.info("WS status: %d updates | %d opps | %d/%d trades (%d pending, %d filled, %d cancelled) | exposure=$%.2f | pnl=$%.2f | settled=$%.2f (%d/%d wins)",
                                 updates_processed, opps_found,
                                 self.trades_succeeded, self.trades_attempted,
                                 pending_count, filled_count, self.trades_cancelled,
                                 self.total_exposure, self.paper_pnl,
                                 self.paper_settled_pnl, self.paper_wins, self.paper_wins + self.paper_losses)
                    logger.info("  RISK: bal=$%.2f dd=%.1f%% cppi_max=$%.2f asset_exp=%s",
                                 balance, drawdown_pct, cppi_max,
                                 {k: round(v, 2) for k, v in self.asset_exposure.items()} if self.asset_exposure else '{}')

                    # Debug: sample a few tickers to see why no opps
                    if opps_found == 0 and self.market_meta:
                        no_liq_count = 0
                        no_pass_edge = 0
                        no_pass_conf = 0
                        sample_count = 0
                        for sample_tk in list(self.market_meta.keys()):
                            meta_s = self.market_meta[sample_tk]
                            asset_s = meta_s['asset']
                            price_s = self.get_price(asset_s)
                            if not price_s:
                                continue
                            mins_s = self._minutes_until(meta_s['close_time'])
                            if mins_s is None or mins_s < self.min_expiry_minutes or mins_s > self.max_expiry_minutes:
                                continue
                            ob_s = self.orderbooks.get(sample_tk, {})
                            # Derive NO ask from YES bids (orderbook levels are BIDS)
                            yes_bids_s = ob_s.get('yes', [])
                            td_s = self.ticker_data.get(sample_tk, {})
                            # Prefer orderbook YES bid, fall back to ticker
                            yb_s = max(p for p, q in yes_bids_s) if yes_bids_s else 0
                            if not yb_s:
                                yb_s = td_s.get('yes_bid', 0) or 0
                            no_ask_s = (100 - yb_s) if yb_s > 0 else 0
                            no_depth_s = 0
                            if no_ask_s and yes_bids_s:
                                for p, q in yes_bids_s:
                                    if p == yb_s:
                                        no_depth_s = q
                            if no_ask_s >= 4 and no_depth_s >= self.min_book_depth:
                                no_liq_count += 1
                                fs = meta_s['floor_strike']
                                cs = meta_s['cap_strike']
                                iv_s = self.calibrated_vol.get(meta_s['event_ticker'])
                                mp_s = self.bracket_probability(price_s, fs, cs, mins_s, asset_s, iv_s)
                                mp_no = 1.0 - mp_s
                                edge_s = (mp_no - no_ask_s / 100.0) * 100
                                me = self.min_edge_pct
                                if no_ask_s <= 25 and mp_no >= 0.88:
                                    me = max(4.0, self.min_edge_pct)
                                elif no_ask_s <= 40:
                                    me = max(me, 6.0)
                                elif no_ask_s <= 60:
                                    me = max(me, 8.0)
                                else:
                                    me = max(me, 10.0)
                                if edge_s >= me:
                                    no_pass_edge += 1
                                if mp_no >= self.min_confidence:
                                    no_pass_conf += 1
                                if sample_count < 5:
                                    logger.info("  DEBUG %s: no=%dc d=%d mp_no=%.0f%% edge=%.1f%% me=%.1f%% conf=%s",
                                                 sample_tk, no_ask_s, no_depth_s, mp_no*100, edge_s, me,
                                                 'Y' if mp_no >= self.min_confidence else 'N')
                                    sample_count += 1
                        logger.info("  DEBUG summary: %d tickers w/NO liq, %d pass edge, %d pass confidence",
                                     no_liq_count, no_pass_edge, no_pass_conf)

                    last_status = now
                    updates_processed = 0
                    opps_found = 0

                    # Refresh balance
                    try:
                        balance = self.api.get_balance()
                        # ── Rule 4: Stop-loss check ──
                        # Use effective_balance = balance + total_exposure so that
                        # capital deployed into open live positions is NOT counted as
                        # a loss (Kalshi debits premium immediately on fill).
                        effective_balance = balance + self.total_exposure
                        drawdown = (self.starting_balance - effective_balance) / self.starting_balance
                        if drawdown >= self.stop_loss_pct:
                            if not self.stop_loss_triggered:
                                logger.warning("STOP-LOSS TRIGGERED: balance=$%.2f, effective=$%.2f, drawdown=%.1f%% >= %.0f%% threshold. Halting trades.",
                                               balance, effective_balance, drawdown * 100, self.stop_loss_pct * 100)
                            self.stop_loss_triggered = True
                        else:
                            if self.stop_loss_triggered:
                                logger.info("STOP-LOSS CLEARED: effective_balance=$%.2f, drawdown=%.1f%% — resuming trades.",
                                            effective_balance, drawdown * 100)
                            self.stop_loss_triggered = False
                    except Exception:
                        pass

                # Periodic re-scan to pick up new near-expiry events
                if now - self._last_scan_time >= self._scan_interval:
                    self._last_scan_time = now
                    new_tickers = self.scan_markets()
                    # Subscribe to any new tickers
                    unsub = [t for t in new_tickers if t not in self.subscribed_tickers]
                    if unsub:
                        await self._subscribe(ws, unsub)
                        logger.info("Added %d new tickers from re-scan", len(unsub))

                    # ── Periodic weather evaluation ──
                    # Weather markets may get sparse WS updates, so evaluate
                    # all weather tickers on each re-scan using REST bid/ask data
                    weather_opps = 0
                    for w_ticker, w_meta in self.market_meta.items():
                        if w_meta.get('market_type') != 'weather':
                            continue
                        opp = self.evaluate_opportunity(w_ticker)
                        if opp:
                            weather_opps += 1
                            self.execute_trade(opp, balance)
                    if weather_opps > 0:
                        logger.info("Weather scan found %d opportunities", weather_opps)

                    # Clean up traded_tickers and event_trade_count for OLD events only
                    # Never re-trade a ticker within the same active event
                    active_events = set(m.get('event_ticker', '') for m in self.market_meta.values())

                    # Settle paper trades for events that just expired
                    if self.config.PAPER_TRADING:
                        # Check which events we had trades on that are no longer active
                        traded_events = set(t['event'] for t in self.paper_trades)
                        if traded_events:
                            gone = traded_events - active_events
                            if gone:
                                logger.info("Settlement check: %d traded events no longer active: %s", len(gone), gone)
                            for ev in traded_events:
                                if ev not in active_events:
                                    self._settle_paper_trades(ev)

                    # Remove traded_tickers entries for events that are no longer active
                    expired_keys = []
                    for k in self.traded_tickers:
                        # trade_key format: "KXBTC-26FEB1620-B68875_yes" — extract event part
                        ticker_part = k.rsplit('_', 1)[0]  # "KXBTC-26FEB1620-B68875"
                        event_part = '-'.join(ticker_part.split('-')[:2])  # "KXBTC-26FEB1620"
                        if event_part not in active_events:
                            expired_keys.append(k)
                    for k in expired_keys:
                        del self.traded_tickers[k]

                    # Remove event_trade_count for old events
                    old_events = [e for e in self.event_trade_count if e not in active_events]
                    for e in old_events:
                        del self.event_trade_count[e]

        finally:
            # Cancel any remaining pending orders before disconnecting
            if self.pending_orders:
                logger.info("Session ending — cancelling %d pending orders...", len(self.pending_orders))
                self._cancel_all_resting_orders()
            await ws.close()
            self.ws = None
            self.subscribed_tickers.clear()

    def stop(self):
        """Signal the trader to stop and clean up."""
        logger.info("Stopping trader — cancelling all resting orders...")
        self._cancel_all_resting_orders()
        self._running = False
        if self.ws:
            asyncio.ensure_future(self.ws.close())


def main():
    """Entry point for WS convergence trader."""
    import sys
    import io
    import traceback
    import atexit
    from pathlib import Path

    # Fix Windows console encoding for emoji characters
    if sys.stdout.encoding != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

    # ── PID lock file to prevent multiple instances ──
    LOCK_FILE = Path(__file__).parent / 'ws_trader.pid'
    import os
    import subprocess as _sp
    if LOCK_FILE.exists():
        old_pid = int(LOCK_FILE.read_text().strip())
        # Windows-compatible check: use tasklist to see if the PID is a python process
        try:
            result = _sp.run(['tasklist', '/FI', f'PID eq {old_pid}', '/FO', 'CSV', '/NH'],
                             capture_output=True, text=True, timeout=5)
            if 'python' in result.stdout.lower():
                print(f'FATAL: Another instance is already running (PID {old_pid}). Exiting.')
                sys.exit(1)
        except Exception:
            pass  # can't check — assume dead, proceed
    LOCK_FILE.write_text(str(os.getpid()))
    def _remove_lock():
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    atexit.register(_remove_lock)

    # Set up logging — use a custom FileHandler subclass that flushes every write
    class FlushFileHandler(logging.FileHandler):
        def emit(self, record):
            super().emit(record)
            self.flush()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            FlushFileHandler('ws_trader.log', mode='a', encoding='utf-8'),
        ]
    )

    # Catch ALL uncaught exceptions and log them (never die silently)
    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical("UNCAUGHT EXCEPTION — bot died!", exc_info=(exc_type, exc_value, exc_traceback))
    sys.excepthook = handle_exception

    from kalshi_bot import KalshiAPI

    logger.info("Initializing KalshiAPI for REST operations...")
    api = KalshiAPI(api_key=Config.KALSHI_API_KEY)

    trader = WSConvergenceTrader(api=api, config=Config)

    import signal

    def shutdown_handler(signum, frame):
        logger.info("Received signal %d — shutting down gracefully...", signum)
        trader.stop()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        trader.stop()
    except Exception:
        logger.critical("FATAL ERROR — bot crashed:\n%s", traceback.format_exc())
        raise


if __name__ == '__main__':
    main()
