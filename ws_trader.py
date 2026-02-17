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
        self.order_timeout_secs = 15             # cancel unfilled orders after 15s
        self._order_check_interval = 5           # poll pending orders every 5s
        self._last_order_check = 0.0

        # Exposure tracking
        self.total_exposure = 0.0                # dollars currently at risk (cost of open positions)
        self.max_total_exposure = float(getattr(Config, 'MAX_EXPOSURE_USD', 250.0))
        self.max_trades_per_event = 8            # cap trades per event to avoid order spam
        self.event_trade_count: Dict[str, int] = {}  # {event_ticker: count}

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
        self.min_confidence = 0.30
        self.min_edge_pct = 2.0
        self.max_trade_usd = float(getattr(self.config, 'MAX_TRADE_USD', 20.0))
        self.max_contracts = 50
        self.min_price_cents = 4       # Skip orders <= 3c (phantom liquidity)
        self.min_book_depth = 5        # Min contracts available at price level

        # Vol estimates
        self.base_vol = {'BTC': 0.0015, 'ETH': 0.0020, 'DOGE': 0.0030, 'XRP': 0.0025}
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

    _ASSETS = ['BTC', 'ETH', 'DOGE', 'XRP']
    _COIN_IDS = 'bitcoin,ethereum,dogecoin,ripple'
    _ASSET_MAP = [('BTC', 'bitcoin'), ('ETH', 'ethereum'), ('DOGE', 'dogecoin'), ('XRP', 'ripple')]

    def _fetch_prices(self) -> Dict[str, float]:
        now = time.time()
        all_fresh = all(
            a in self.price_cache and now - self.price_cache[a][1] < self.price_cache_ttl
            for a in self._ASSETS
        )
        if all_fresh:
            return {a: self.price_cache[a][0] for a in self._ASSETS if a in self.price_cache}
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={'ids': self._COIN_IDS, 'vs_currencies': 'usd'},
                timeout=5
            )
            if r.status_code == 429:
                return {a: self.price_cache[a][0] for a in self._ASSETS if a in self.price_cache}
            r.raise_for_status()
            data = r.json()
            result = {}
            for asset, cid in self._ASSET_MAP:
                p = data.get(cid, {}).get('usd')
                if p:
                    self.price_cache[asset] = (p, now)
                    result[asset] = p
            return result
        except Exception:
            return {a: self.price_cache[a][0] for a in self._ASSETS if a in self.price_cache}

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
        return None

    def _minutes_until(self, close_time: str) -> Optional[float]:
        try:
            close = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
            return (close - datetime.now(timezone.utc)).total_seconds() / 60
        except (ValueError, TypeError):
            return None

    def scan_markets(self) -> List[str]:
        """
        REST-scan for near-expiry crypto bracket markets.
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
                new_market_meta[ticker] = {
                    'floor_strike': b.get('floor_strike'),
                    'cap_strike': b.get('cap_strike'),
                    'event_ticker': event_ticker,
                    'close_time': ct,
                    'asset': asset,
                }
                tickers_to_sub.append(ticker)

        # Replace market_meta entirely so expired events are dropped
        self.market_meta = new_market_meta
        logger.info("Found %d tickers across qualifying events", len(tickers_to_sub))
        return tickers_to_sub

    # ─── Trade evaluation (called on each WS update) ─────────────────────

    def evaluate_opportunity(self, ticker: str) -> Optional[Dict]:
        """
        Evaluate a single market for convergence opportunity.
        Called whenever we get a WS update for this ticker.
        """
        meta = self.market_meta.get(ticker)
        if not meta:
            return None

        asset = meta['asset']
        current_price = self.get_price(asset)
        if not current_price:
            return None

        mins_left = self._minutes_until(meta['close_time'])
        if mins_left is None or mins_left < self.min_expiry_minutes or mins_left > self.max_expiry_minutes:
            return None

        floor_s = meta['floor_strike']
        cap_s = meta['cap_strike']
        event = meta['event_ticker']
        impl_vol = self.calibrated_vol.get(event)

        model_prob = self.bracket_probability(current_price, floor_s, cap_s, mins_left, asset, impl_vol)

        # Get best prices from WS orderbook or ticker data
        td = self.ticker_data.get(ticker, {})
        ob = self.orderbooks.get(ticker, {})

        # Prefer WS ticker data for bid/ask
        yes_bid = td.get('yes_bid', 0) or 0
        yes_ask = td.get('yes_ask', 0) or 0

        # Fall back to orderbook best levels
        if not yes_ask and ob.get('yes'):
            # yes orderbook: sorted asks (lowest first)
            yes_levels = ob['yes']
            if yes_levels:
                yes_ask = min(p for p, q in yes_levels)
        if not yes_bid and ob.get('yes'):
            yes_levels = ob['yes']
            if yes_levels:
                yes_bid = max(p for p, q in yes_levels)

        no_ask = 0
        if ob.get('no'):
            no_levels = ob['no']
            if no_levels:
                no_ask = min(p for p, q in no_levels)

        # Check YES opportunity
        yes_depth = self._book_depth_at(ticker, 'yes', yes_ask) if yes_ask else 0
        if yes_ask >= self.min_price_cents and yes_ask < 95 and yes_depth >= self.min_book_depth:
            implied_prob = yes_ask / 100.0
            edge = (model_prob - implied_prob) * 100
            min_edge = self.min_edge_pct
            if model_prob >= 0.85 and mins_left <= 15:
                min_edge = max(1.5, self.min_edge_pct - 1.5)

            if edge >= min_edge and model_prob >= self.min_confidence:
                return {
                    'ticker': ticker, 'event': event, 'side': 'yes',
                    'price': yes_ask, 'model_prob': model_prob,
                    'implied_prob': implied_prob, 'edge_pct': edge,
                    'minutes_left': mins_left, 'asset': asset,
                    'current_price': current_price,
                    'floor': floor_s, 'cap': cap_s, 'impl_vol': impl_vol,
                    'book_depth': yes_depth,
                }

        # Check NO opportunity
        model_prob_no = 1.0 - model_prob
        no_depth = self._book_depth_at(ticker, 'no', no_ask) if no_ask else 0
        if no_ask >= self.min_price_cents and no_ask < 95 and no_depth >= self.min_book_depth:
            implied_prob_no = no_ask / 100.0
            edge_no = (model_prob_no - implied_prob_no) * 100
            min_edge = self.min_edge_pct
            if model_prob_no >= 0.85 and mins_left <= 15:
                min_edge = max(1.5, self.min_edge_pct - 1.5)

            if edge_no >= min_edge and model_prob_no >= self.min_confidence:
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

    def _book_depth_at(self, ticker: str, side: str, price_cents: int) -> int:
        """Return quantity available at given price level in the orderbook."""
        ob = self.orderbooks.get(ticker, {})
        levels = ob.get(side, [])
        for p, q in levels:
            if p == price_cents:
                return q
        return 0

    def size_trade(self, opp: Dict, balance: float) -> int:
        """Half-Kelly position sizing."""
        price_cents = opp['price']
        model_prob = opp['model_prob']
        cost = price_cents / 100.0
        net_win = 1.0 - cost
        if net_win <= 0 or cost <= 0:
            return 0
        kelly_mult = getattr(self.config, 'KELLY_MULTIPLIER', 0.5)
        b = net_win / cost
        f = ((b * model_prob) - (1 - model_prob)) / b
        f = max(0, f * kelly_mult)
        risk = min(f * balance, self.max_trade_usd)
        contracts = int(risk / cost)
        contracts = min(max(0, contracts), self.max_contracts)
        # Cap to available book depth
        book_depth = opp.get('book_depth', contracts)
        if book_depth > 0:
            contracts = min(contracts, book_depth)
        return contracts

    def execute_trade(self, opp: Dict, balance: float) -> bool:
        """Execute a convergence trade (paper or live)."""
        trade_key = f"{opp['ticker']}_{opp['side']}"
        if trade_key in self.traded_tickers:
            return False

        # Prevent trading opposite side on same bracket (guaranteed loss)
        opposite = 'no' if opp['side'] == 'yes' else 'yes'
        opposite_key = f"{opp['ticker']}_{opposite}"
        if opposite_key in self.traded_tickers:
            return False

        # Check per-event trade cap
        event = opp.get('event', '')
        if self.event_trade_count.get(event, 0) >= self.max_trades_per_event:
            return False

        # Check total exposure cap
        qty = self.size_trade(opp, balance)
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

        floor_str = f"${opp['floor']:,.0f}" if opp['floor'] else "(-inf)"
        cap_str = f"${opp['cap']:,.0f}" if opp['cap'] else "(+inf)"

        logger.info("")
        logger.info(">> %s  %s x%d @ %dc", opp['ticker'], opp['side'].upper(), qty, opp['price'])
        logger.info("   Model: %.1f%%  Market: %.1f%%  Edge: +%.1f%%  Mins: %.0f",
                     opp['model_prob'] * 100, opp['implied_prob'] * 100,
                     opp['edge_pct'], opp['minutes_left'])
        logger.info("   Bracket: %s — %s  |  %s @ $%s  |  Depth: %d",
                     floor_str, cap_str, opp['asset'], f"{opp['current_price']:,.0f}",
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
            # Store for settlement tracking
            self.paper_trades.append({
                'ticker': opp['ticker'], 'event': event, 'side': opp['side'],
                'qty': qty, 'price': opp['price'], 'cost': cost,
                'floor': opp['floor'], 'cap': opp['cap'],
                'asset': opp['asset'], 'current_price': opp['current_price'],
                'model_prob': opp['model_prob'], 'edge_pct': opp['edge_pct'],
                'minutes_left': opp['minutes_left'],
                'placed_at': datetime.now(timezone.utc).isoformat(),
            })
            return True

        elif is_live:
            try:
                logger.info("   LIVE ORDER: %d %s @ %dc on %s",
                             qty, opp['side'].upper(), opp['price'], opp['ticker'])
                order = self.api.place_order(
                    ticker=opp['ticker'], side=opp['side'],
                    quantity=qty, price=opp['price']
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
                        # Order is resting (maker order) — track it for polling
                        logger.info("   RESTING: order_id=%s status=%s fill=%d/%d",
                                     order_id, status, fill_count, qty)
                        self.pending_orders[order_id] = {
                            'order_id': order_id, 'ticker': opp['ticker'],
                            'side': opp['side'], 'qty': qty, 'price': opp['price'],
                            'cost': cost, 'placed_at': time.time(),
                            'fill_count': fill_count,
                        }
                        # Reserve exposure for pending order
                        self.total_exposure += cost

                    self.traded_tickers[trade_key] = time.time()
                    self.event_trade_count[event] = self.event_trade_count.get(event, 0) + 1
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
                        # No fills at all — release exposure
                        self.total_exposure -= info['cost']

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
        """Settle all paper trades for an expired event using actual price."""
        if event_ticker in self._settled_events:
            return

        # Get trades for this event
        event_trades = [t for t in self.paper_trades if t['event'] == event_ticker]
        if not event_trades:
            return

        asset = event_trades[0]['asset']
        settle_price = self.get_price(asset)
        if not settle_price:
            logger.warning("Cannot settle %s — no price available for %s", event_ticker, asset)
            return

        self._settled_events.add(event_ticker)

        event_pnl = 0.0
        event_wins = 0
        event_losses = 0

        def _fmt_price(p):
            if p is None:
                return 'N/A'
            if abs(p) < 1:
                return f'{p:.6f}'
            elif abs(p) < 100:
                return f'{p:.4f}'
            else:
                return f'{p:,.0f}'

        logger.info("")
        logger.info("=" * 60)
        logger.info("SETTLEMENT: %s  |  %s @ $%s", event_ticker, asset, _fmt_price(settle_price))
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
        mode = "PAPER" if is_paper else ("LIVE" if is_live else "DRY-RUN")

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
            # NOTE: Do NOT clear event_trade_count here — counters must persist across reconnects
            logger.info("Balance: $%.2f | Max exposure: $%.2f", balance, self.max_total_exposure)

            # Process messages
            updates_processed = 0
            opps_found = 0
            last_status = time.time()

            async for raw_msg in ws:
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

                # Check pending orders for fills / timeouts
                self._check_pending_orders()

                # Periodic status log (every 30s)
                now = time.time()
                if now - last_status >= 30:
                    pending_count = len(self.pending_orders)
                    filled_count = len(self.filled_orders)
                    logger.info("WS status: %d updates | %d opps | %d/%d trades (%d pending, %d filled, %d cancelled) | exposure=$%.2f | pnl=$%.2f | settled=$%.2f (%d/%d wins)",
                                 updates_processed, opps_found,
                                 self.trades_succeeded, self.trades_attempted,
                                 pending_count, filled_count, self.trades_cancelled,
                                 self.total_exposure, self.paper_pnl,
                                 self.paper_settled_pnl, self.paper_wins, self.paper_wins + self.paper_losses)
                    last_status = now
                    updates_processed = 0
                    opps_found = 0

                    # Refresh balance
                    try:
                        balance = self.api.get_balance()
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

    # Fix Windows console encoding for emoji characters
    if sys.stdout.encoding != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

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
