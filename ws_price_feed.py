"""Real-time BTC/ETH/SOL price feed via Coinbase Exchange public WebSocket."""

import json
import logging
import threading
import time
from typing import Optional

import websocket

logger = logging.getLogger('kalshi_bot')

_COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"

_SUBSCRIBE_MSG = json.dumps({
    "type": "subscribe",
    "product_ids": ["BTC-USD", "ETH-USD", "SOL-USD"],
    "channels": ["ticker"],
})

_PRODUCT_MAP = {
    "BTC-USD": "BTC",
    "ETH-USD": "ETH",
    "SOL-USD": "SOL",
}


class CryptoPriceFeed:
    """
    Real-time BTC and ETH price feed via the Coinbase Exchange public WebSocket.

    No API key is required.  Prices are updated sub-second as Coinbase pushes
    ticker events.  The feed runs in a background daemon thread and reconnects
    automatically (exponential back-off, capped at ``max_reconnect_delay`` s).
    """

    def __init__(self, max_reconnect_delay: int = 30) -> None:
        self._max_reconnect_delay = max_reconnect_delay
        self._prices: dict[str, float] = {}          # {"BTC": 97000.0, ...}
        self._timestamps: dict[str, float] = {}      # {"BTC": time.time(), ...}
        self._lock = threading.Lock()
        self._connected = False
        self._stop_event = threading.Event()
        self._ws: Optional[websocket.WebSocketApp] = None

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.debug("CryptoPriceFeed started (Coinbase Exchange WebSocket)")

    # ── Public API ────────────────────────────────────────────────────────

    def get_btc_price(self) -> Optional[float]:
        """Return the latest BTC/USD price, or None if not yet received."""
        with self._lock:
            return self._prices.get("BTC")

    def get_eth_price(self) -> Optional[float]:
        """Return the latest ETH/USD price, or None if not yet received."""
        with self._lock:
            return self._prices.get("ETH")

    def get_sol_price(self) -> Optional[float]:
        """Return the latest SOL/USD price, or None if not yet received."""
        with self._lock:
            return self._prices.get("SOL")

    def get_price_age_seconds(self, symbol: str) -> float:
        """
        Return how many seconds ago the cached price for *symbol* was updated.

        Returns ``float('inf')`` if the price has never been received.
        """
        with self._lock:
            ts = self._timestamps.get(symbol.upper())
        if ts is None:
            return float("inf")
        return time.time() - ts

    def is_connected(self) -> bool:
        """Return True if the WebSocket is open and data is flowing."""
        return self._connected and not self._stop_event.is_set()

    def stop(self) -> None:
        """Cleanly disconnect and stop the background thread."""
        logger.debug("CryptoPriceFeed stopping")
        self._stop_event.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        self._thread.join(timeout=5)
        logger.debug("CryptoPriceFeed stopped")

    # ── Internal ──────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Reconnect loop with exponential back-off."""
        delay = 1
        while not self._stop_event.is_set():
            try:
                self._connect()
            except Exception as exc:
                logger.debug("CryptoPriceFeed connection error: %s", exc)

            if self._stop_event.is_set():
                break

            logger.debug(
                "CryptoPriceFeed disconnected; reconnecting in %d s", delay
            )
            self._stop_event.wait(delay)
            delay = min(delay * 2, self._max_reconnect_delay)

    def _connect(self) -> None:
        self._ws = websocket.WebSocketApp(
            _COINBASE_WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws.run_forever(ping_interval=20, ping_timeout=10)

    def _on_open(self, websocket_app) -> None:  # noqa: ARG002
        self._connected = True
        websocket_app.send(_SUBSCRIBE_MSG)
        logger.debug("CryptoPriceFeed WebSocket connected")

    def _on_message(self, websocket_app, raw: str) -> None:  # noqa: ARG002
        try:
            data = json.loads(raw)
            if data.get("type") != "ticker":
                return
            product_id = data.get("product_id", "")
            asset = _PRODUCT_MAP.get(product_id)
            if asset is None:
                return
            price_str = data.get("price")
            if price_str is None:
                return
            price = float(price_str)
            with self._lock:
                self._prices[asset] = price
                self._timestamps[asset] = time.time()
            logger.debug("CryptoPriceFeed %s price updated: $%.2f", asset, price)
        except Exception as exc:
            logger.debug("CryptoPriceFeed message parse error: %s", exc)

    def _on_error(self, websocket_app, error) -> None:  # noqa: ARG002
        logger.debug("CryptoPriceFeed WebSocket error: %s", error)
        self._connected = False

    def _on_close(self, websocket_app, close_status_code, close_msg) -> None:  # noqa: ARG002
        self._connected = False
        logger.debug(
            "CryptoPriceFeed WebSocket closed (code=%s msg=%s)",
            close_status_code,
            close_msg,
        )
