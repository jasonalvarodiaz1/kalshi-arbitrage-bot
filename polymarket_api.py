import requests
import time
import random
import threading
import logging
from typing import Dict, List, Optional

logger = logging.getLogger('kalshi_bot')

BASE_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"
_RATE_LIMIT_DELAY = 0.6  # ~100 req/min → 1 request per 0.6 s


class PolymarketAPI:
    """Read-only client for the Polymarket CLOB API.

    Follows the same patterns as KalshiAPI: thread-local sessions,
    _request_with_retry with exponential backoff, and a simple public interface.

    Authentication
    --------------
    For read-only market-data endpoints the API key is optional but can be
    provided via the ``api_key`` constructor argument (sent as the
    ``POLY_API_KEY`` header).  Order signing (EIP-712) is out of scope for
    this module and left for a future PR.

    Price format
    ------------
    Polymarket prices are decimals in the range 0.00–1.00 (USDC).
    The helper :meth:`to_cents` converts them to integer cents for
    comparison with Kalshi prices.
    """

    BASE_URL = BASE_URL

    def __init__(self, api_key: Optional[str] = None, private_key: Optional[str] = None):
        self.api_key = api_key
        self.private_key = private_key  # Ethereum private key for EIP-712 order signing
        self._local = threading.local()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_session(self) -> requests.Session:
        """Return a thread-local requests.Session with default headers."""
        if not hasattr(self._local, 'session'):
            s = requests.Session()
            headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            }
            if self.api_key:
                headers['POLY_API_KEY'] = self.api_key
            s.headers.update(headers)
            self._local.session = s
        return self._local.session

    def _request_with_retry(
        self,
        method: str,
        url: str,
        max_retries: int = 3,
        **kwargs,
    ) -> Optional[requests.Response]:
        """HTTP request with exponential backoff (mirrors KalshiAPI pattern).

        Retries on 429 / 5xx / network errors.  Client errors (4xx except 429)
        are returned immediately without retry.
        """
        base_delay = 1.5

        for attempt in range(max_retries + 1):
            try:
                response = self._get_session().request(method, url, **kwargs)

                if response.status_code < 400:
                    return response

                if 400 <= response.status_code < 500 and response.status_code != 429:
                    return response

                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < max_retries:
                        retry_after = response.headers.get('Retry-After')
                        if retry_after and retry_after.isdigit():
                            delay = int(retry_after)
                        else:
                            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                        logger.warning(
                            "Polymarket request failed %d: %s %s (attempt %d/%d). Retrying in %.2fs...",
                            response.status_code, method, url, attempt + 1, max_retries + 1, delay,
                        )
                        time.sleep(delay)
                        continue
                    else:
                        logger.error(
                            "Polymarket request failed after %d retries: %s %s (status: %d)",
                            max_retries + 1, method, url, response.status_code,
                        )
                        return response

                return response

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                    logger.warning(
                        "Polymarket network error: %s %s (attempt %d/%d). Retrying in %.2fs...",
                        method, url, attempt + 1, max_retries + 1, delay,
                    )
                    time.sleep(delay)
                    continue
                else:
                    logger.error(
                        "Polymarket request failed after %d retries: %s %s (error: %s)",
                        max_retries + 1, method, url, str(exc),
                    )
                    return None
            except Exception as exc:
                logger.error("Unexpected error in Polymarket request: %s %s - %s", method, url, str(exc))
                return None

        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_markets(self, next_cursor: Optional[str] = None) -> tuple:
        """Fetch one page of active Polymarket markets.

        Returns
        -------
        (markets, next_cursor)
            *markets* is a list of market dicts; *next_cursor* is the
            pagination cursor for the next page (or ``None`` when exhausted).
        """
        try:
            params: Dict = {}
            if next_cursor:
                params['next_cursor'] = next_cursor
            response = self._request_with_retry('GET', f"{self.BASE_URL}/markets", params=params)
            if response is None:
                logger.error("Polymarket GET /markets failed after retries")
                return [], None
            if response.status_code == 200:
                data = response.json()
                markets = data.get('data', [])
                cursor = data.get('next_cursor')
                # Polymarket returns the literal string "LTE=" to signal the last page
                if cursor == 'LTE=':
                    cursor = None
                return markets, cursor
            logger.error("Polymarket GET /markets returned %d", response.status_code)
            return [], None
        except Exception as exc:
            logger.error("Error fetching Polymarket markets: %s", exc)
            return [], None

    def get_events_by_tag(self, tag: str, limit: int = 100, max_pages: int = 20) -> List[Dict]:
        """Fetch active Polymarket events filtered by tag using the Gamma API.

        The ``/events?tag=<tag>`` endpoint actually filters properly.
        Returns raw event dicts (each with a ``markets`` list inside).
        Common tags: ``'sports'``, ``'politics'``, ``'crypto'``.
        """
        all_events: List[Dict] = []
        offset = 0
        page = 0
        while page < max_pages:
            page += 1
            try:
                params = {
                    'tag': tag,
                    'active': 'true',
                    'closed': 'false',
                    'limit': limit,
                    'offset': offset,
                }
                resp = self._request_with_retry('GET', f"{GAMMA_URL}/events", params=params)
                if resp is None or resp.status_code != 200:
                    logger.error(
                        "Gamma /events?tag=%s failed (status=%s)",
                        tag, resp.status_code if resp else 'None',
                    )
                    break
                data = resp.json()
                page_events = data if isinstance(data, list) else data.get('data', [])
                if not page_events:
                    break
                all_events.extend(page_events)
                if len(page_events) < limit:
                    break  # last page
                offset += limit
                time.sleep(_RATE_LIMIT_DELAY)
            except Exception as exc:
                logger.error("Error fetching Gamma events tag=%s: %s", tag, exc)
                break
        logger.info("Gamma API: fetched %d events for tag '%s'", len(all_events), tag)
        return all_events

    def get_markets_for_tag(self, tag: str, min_liquidity: float = 25.0, max_pages: int = 20) -> List[Dict]:
        """Fetch active binary markets for a Gamma tag, normalized for the scanner.

        Extracts markets from events (``/events?tag=<tag>``), normalises the
        Gamma-format ``clobTokenIds`` into the CLOB-style ``tokens`` list, and
        pre-filters by ``min_liquidity``.  The returned dicts also carry the
        convenience fields ``bestBid``, ``bestAsk``, and ``liquidity`` so
        the caller can do a cheap pre-filter before calling ``get_orderbook()``.
        """
        import json as _json
        events = self.get_events_by_tag(tag, max_pages=max_pages)
        markets: List[Dict] = []
        for event in events:
            for m in event.get('markets', []):
                if not m.get('active') or m.get('closed'):
                    continue
                # clobTokenIds may arrive as a JSON-encoded string or a Python list
                raw_ids = m.get('clobTokenIds', [])
                if isinstance(raw_ids, str):
                    try:
                        token_ids = _json.loads(raw_ids)
                    except Exception:
                        continue
                else:
                    token_ids = raw_ids
                if not isinstance(token_ids, list) or len(token_ids) != 2:
                    continue  # only binary markets
                # outcomes may also be a JSON string
                outcomes_raw = m.get('outcomes', '["Yes","No"]')
                try:
                    outcomes = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                except Exception:
                    outcomes = ['Yes', 'No']
                # Attach normalised tokens list
                m['tokens'] = [
                    {'token_id': token_ids[i], 'outcome': outcomes[i] if i < len(outcomes) else str(i)}
                    for i in range(2)
                ]
                # Convert price strings → floats if needed
                for field in ('bestBid', 'bestAsk', 'lastTradePrice'):
                    val = m.get(field)
                    if isinstance(val, str):
                        try:
                            m[field] = float(val)
                        except ValueError:
                            m[field] = None
                # liquidity can be a string like "1234.56" or a float
                liq_raw = m.get('liquidityNum') or m.get('liquidity', 0)
                try:
                    liq = float(liq_raw) if liq_raw else 0.0
                except (ValueError, TypeError):
                    liq = 0.0
                if liq < min_liquidity:
                    continue
                markets.append(m)
        logger.info(
            "get_markets_for_tag('%s'): %d binary markets (liq>$%.0f) from %d events",
            tag, len(markets), min_liquidity, len(events),
        )
        return markets

    def get_markets_by_tag(self, tag_slug: str, limit: int = 100) -> List[Dict]:
        """Alias for ``get_markets_for_tag`` (kept for backwards compatibility)."""
        return self.get_markets_for_tag(tag_slug)

    def get_all_markets(self, tag_slug: Optional[str] = None, max_pages: int = 50) -> List[Dict]:
        """Fetch active Polymarket markets (handles pagination).

        If ``tag_slug`` is provided, uses the faster Gamma API tag filter.
        Otherwise falls back to the CLOB API (capped at ``max_pages``).
        """
        if tag_slug:
            return self.get_markets_by_tag(tag_slug)
        all_markets: List[Dict] = []
        cursor = None
        page = 0
        while page < max_pages:
            page += 1
            markets, cursor = self.get_markets(next_cursor=cursor)
            all_markets.extend(markets)
            if not cursor or not markets:
                break
            time.sleep(_RATE_LIMIT_DELAY)
        logger.info("Polymarket: fetched %d markets (%d pages)", len(all_markets), page)
        return all_markets

    def get_orderbook(self, token_id: str) -> Dict:
        """Get the orderbook for a Polymarket token.

        Returns a dict with ``bids`` and ``asks`` lists, each element being
        ``{"price": <float 0-1>, "size": <float>}``.
        """
        try:
            params = {'token_id': token_id}
            response = self._request_with_retry('GET', f"{self.BASE_URL}/book", params=params)
            if response is None:
                logger.error("Polymarket GET /book failed after retries for token %s", token_id)
                return {}
            if response.status_code == 200:
                data = response.json()
                return {
                    'bids': data.get('bids', []),
                    'asks': data.get('asks', []),
                    'timestamp': time.time(),
                }
            logger.error("Polymarket GET /book returned %d for token %s", response.status_code, token_id)
            return {}
        except Exception as exc:
            logger.error("Error fetching Polymarket orderbook for %s: %s", token_id, exc)
            return {}

    def get_price(self, token_id: str) -> Dict:
        """Get current best bid/ask prices for a Polymarket token.

        Returns ``{"bid": <float>, "ask": <float>}`` (USDC, 0–1 range).
        """
        try:
            params = {'token_id': token_id}
            response = self._request_with_retry('GET', f"{self.BASE_URL}/prices", params=params)
            if response is None:
                logger.error("Polymarket GET /prices failed after retries for token %s", token_id)
                return {}
            if response.status_code == 200:
                return response.json()
            logger.error("Polymarket GET /prices returned %d for token %s", response.status_code, token_id)
            return {}
        except Exception as exc:
            logger.error("Error fetching Polymarket price for %s: %s", token_id, exc)
            return {}

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get the midpoint price for a Polymarket token (USDC, 0–1 range).

        Returns ``None`` on failure.
        """
        try:
            params = {'token_id': token_id}
            response = self._request_with_retry('GET', f"{self.BASE_URL}/midpoint", params=params)
            if response is None:
                logger.error("Polymarket GET /midpoint failed after retries for token %s", token_id)
                return None
            if response.status_code == 200:
                data = response.json()
                mid = data.get('mid')
                return float(mid) if mid is not None else None
            logger.error("Polymarket GET /midpoint returned %d for token %s", response.status_code, token_id)
            return None
        except Exception as exc:
            logger.error("Error fetching Polymarket midpoint for %s: %s", token_id, exc)
            return None

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def to_cents(price: float) -> int:
        """Convert Polymarket USDC price (0.00–1.00) to integer cents (0–100)."""
        return round(price * 100)

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def place_order(
        self,
        token_id: str,
        side: str,        # 'BUY'
        price: float,     # limit price in USDC (0.0–1.0)
        size: float,      # number of contracts
        order_type: str = 'GTC',
    ) -> Optional[Dict]:
        """Place a Polymarket order via the CLOB API (EIP-712 signed).

        Requires ``POLYMARKET_PRIVATE_KEY`` to be set in ``.env``.
        When ``PAPER_TRADING=true`` the order is logged but NOT submitted.

        Args:
            token_id:   Polymarket outcome token ID.
            side:       'BUY' (only buy-side is currently supported).
            price:      Limit price in USDC (0.0–1.0).
            size:       Number of contracts.
            order_type: 'GTC' (Good-Till-Cancelled) or 'FOK' (Fill-Or-Kill).

        Returns:
            Response dict from the CLOB API, or ``None`` on failure.
            In paper mode returns a simulated response dict.
        """
        from config import Config  # local import to avoid circular dependency

        if Config.PAPER_TRADING:
            logger.info(
                "PAPER Polymarket order: %s %s @ %.4f x %.1f [%s]",
                side, token_id[:16], price, size, order_type,
            )
            return {
                'status': 'paper',
                'token_id': token_id,
                'side': side,
                'price': price,
                'size': size,
                'order_type': order_type,
            }

        if not self.private_key:
            logger.error(
                "Polymarket place_order: execution unavailable. "
                "US accounts can only trade via the Polymarket mobile app — "
                "API order signing is not supported for US users. "
                "Set POLYMARKET_EXECUTION_ENABLED=false in .env (already the default)."
            )
            return None

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs, OrderType

            client = ClobClient(
                host=self.BASE_URL,
                chain_id=137,      # Polygon mainnet
                key=self.private_key,
            )

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
            )

            signed_order = client.create_order(order_args)
            ot = OrderType.FOK if order_type == 'FOK' else OrderType.GTC
            resp = client.post_order(signed_order, ot)

            logger.info(
                "Polymarket order placed: %s %s @ %.4f x %.1f — resp: %s",
                side, token_id[:16], price, size, resp,
            )
            return resp

        except ImportError:
            logger.error("py-clob-client not installed. Run: pip install py-clob-client")
            return None
        except Exception as exc:
            logger.error("Polymarket order error for %s: %s", token_id[:16], exc)
            return None
