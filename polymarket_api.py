import requests
import time
import random
import threading
import logging
from typing import Dict, List, Optional

logger = logging.getLogger('kalshi_bot')

BASE_URL = "https://clob.polymarket.com"
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

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
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

    def get_all_markets(self) -> List[Dict]:
        """Fetch ALL active Polymarket markets (handles pagination)."""
        all_markets: List[Dict] = []
        cursor = None
        page = 0
        while True:
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
