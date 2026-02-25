import requests
import time
import random
import threading
import logging
from typing import Dict, List, Optional

logger = logging.getLogger('kalshi_bot')


class ManifoldAPI:
    """Client for the Manifold Markets API.

    Manifold has two currencies:
    - Mana (M$) — play money, used in most markets
    - Sweepcash (S$) — real money (Sweepstakes markets), redeemable for USD

    The bot focuses on Sweepstakes markets for real-money arbitrage.
    All prices are probabilities (0.0–1.0). Use to_cents() for comparison with Kalshi.

    Follows the same patterns as PolymarketAPI: thread-local sessions,
    _request_with_retry with exponential backoff, and a simple public interface.
    """

    BASE_URL = "https://api.manifold.markets"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key  # Only needed for placing bets
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
                headers['Authorization'] = f'Key {self.api_key}'
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
        """HTTP request with exponential backoff (mirrors PolymarketAPI pattern).

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
                            "Manifold request failed %d: %s %s (attempt %d/%d). Retrying in %.2fs...",
                            response.status_code, method, url, attempt + 1, max_retries + 1, delay,
                        )
                        time.sleep(delay)
                        continue
                    else:
                        logger.error(
                            "Manifold request failed after %d retries: %s %s (status: %d)",
                            max_retries + 1, method, url, response.status_code,
                        )
                        return response

                return response

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                    logger.warning(
                        "Manifold network error: %s %s (attempt %d/%d). Retrying in %.2fs...",
                        method, url, attempt + 1, max_retries + 1, delay,
                    )
                    time.sleep(delay)
                    continue
                else:
                    logger.error(
                        "Manifold request failed after %d retries: %s %s (error: %s)",
                        max_retries + 1, method, url, str(exc),
                    )
                    return None
            except Exception as exc:
                logger.error("Unexpected error in Manifold request: %s %s - %s", method, url, str(exc))
                return None

        return None

    # ------------------------------------------------------------------
    # Market Data (no auth needed)
    # ------------------------------------------------------------------

    def get_markets(self, limit: int = 100, before: Optional[str] = None,
                    sort: str = 'liquidity', filter_: str = 'open') -> List[Dict]:
        """GET /v0/search-markets

        Params:
            limit: max markets per page (up to 1000)
            sort: 'liquidity', 'newest', 'score'
            filter_: 'open', 'closed', 'resolved', 'all'

        Returns list of market dicts. Key fields:
        - id: market ID (used for betting)
        - question: market title/question
        - probability: current probability (0.0-1.0) — this IS the price
        - pool: dict with YES/NO pool amounts
        - mechanism: 'cpmm-1' (AMM) or 'cpmm-multi-1'
        - outcomeType: 'BINARY', 'MULTIPLE_CHOICE', 'NUMERIC', etc.
        - isResolved: bool
        - closeTime: unix timestamp (ms) when market closes
        - volume: total volume traded
        - totalLiquidity: total liquidity in the pool
        - token: 'MANA' or 'CASH' (CASH = Sweepstakes/real money)
        """
        try:
            params: Dict = {
                'limit': limit,
                'sort': sort,
                'filter': filter_,
            }
            if before:
                params['before'] = before
            response = self._request_with_retry('GET', f"{self.BASE_URL}/v0/search-markets", params=params)
            if response is None:
                logger.error("Manifold GET /v0/search-markets failed after retries")
                return []
            if response.status_code == 200:
                return response.json()
            logger.error("Manifold GET /v0/search-markets returned %d", response.status_code)
            return []
        except Exception as exc:
            logger.error("Error fetching Manifold markets: %s", exc)
            return []

    def search_markets(self, term: str, limit: int = 100,
                       filter_: str = 'open', sort: str = 'liquidity',
                       token: Optional[str] = None) -> List[Dict]:
        """GET /v0/search-markets?term=...&token=CASH

        Search by keyword. The `token` param filters:
        - 'CASH' = Sweepstakes markets only (real money)
        - 'MANA' = play money only
        - None = all markets
        """
        try:
            params: Dict = {
                'term': term,
                'limit': limit,
                'filter': filter_,
                'sort': sort,
            }
            if token:
                params['token'] = token
            response = self._request_with_retry('GET', f"{self.BASE_URL}/v0/search-markets", params=params)
            if response is None:
                logger.error("Manifold search_markets failed after retries for term=%s", term)
                return []
            if response.status_code == 200:
                return response.json()
            logger.error("Manifold search_markets returned %d for term=%s", response.status_code, term)
            return []
        except Exception as exc:
            logger.error("Error searching Manifold markets for '%s': %s", term, exc)
            return []

    def get_market(self, market_id: str) -> Optional[Dict]:
        """GET /v0/market/{id}
        Returns full market details including bets history."""
        try:
            response = self._request_with_retry('GET', f"{self.BASE_URL}/v0/market/{market_id}")
            if response is None:
                logger.error("Manifold GET /v0/market/%s failed after retries", market_id)
                return None
            if response.status_code == 200:
                return response.json()
            logger.error("Manifold GET /v0/market/%s returned %d", market_id, response.status_code)
            return None
        except Exception as exc:
            logger.error("Error fetching Manifold market %s: %s", market_id, exc)
            return None

    def get_market_by_slug(self, slug: str) -> Optional[Dict]:
        """GET /v0/slug/{slug}
        Manifold markets have URL slugs like 'will-btc-hit-100k-by-2025'."""
        try:
            response = self._request_with_retry('GET', f"{self.BASE_URL}/v0/slug/{slug}")
            if response is None:
                logger.error("Manifold GET /v0/slug/%s failed after retries", slug)
                return None
            if response.status_code == 200:
                return response.json()
            logger.error("Manifold GET /v0/slug/%s returned %d", slug, response.status_code)
            return None
        except Exception as exc:
            logger.error("Error fetching Manifold market by slug %s: %s", slug, exc)
            return None

    def get_positions(self, market_id: str) -> List[Dict]:
        """GET /v0/market/{id}/positions
        Get all positions in a market (useful for seeing where money is)."""
        try:
            response = self._request_with_retry('GET', f"{self.BASE_URL}/v0/market/{market_id}/positions")
            if response is None:
                logger.error("Manifold GET /v0/market/%s/positions failed after retries", market_id)
                return []
            if response.status_code == 200:
                return response.json()
            logger.error("Manifold GET /v0/market/%s/positions returned %d", market_id, response.status_code)
            return []
        except Exception as exc:
            logger.error("Error fetching Manifold positions for %s: %s", market_id, exc)
            return []

    # ------------------------------------------------------------------
    # Price helpers
    # ------------------------------------------------------------------

    @staticmethod
    def to_cents(probability: float) -> int:
        """Convert Manifold probability (0.0-1.0) to cents (0-100).
        Same as PolymarketAPI.to_cents()."""
        return round(probability * 100)

    @staticmethod
    def from_cents(cents: int) -> float:
        """Convert cents back to probability."""
        return cents / 100.0

    def get_probability(self, market_id: str) -> Optional[float]:
        """Get current YES probability for a market."""
        market = self.get_market(market_id)
        if market:
            return market.get('probability')
        return None

    # ------------------------------------------------------------------
    # Order Placement (requires API key)
    # ------------------------------------------------------------------

    def place_bet(self, market_id: str, amount: float, outcome: str = 'YES',
                  limit_prob: Optional[float] = None) -> Optional[Dict]:
        """POST /v0/bet

        Place a bet on a Manifold market.

        Args:
            market_id: The contract/market ID
            amount: Amount in Mana or Sweepcash (depends on market type)
            outcome: 'YES' or 'NO'
            limit_prob: Optional limit order probability (0.0-1.0).
                       If set, creates a limit order that fills when
                       the market reaches this price.

        Returns the bet object or None on failure.
        """
        from config import Config  # local import to avoid circular dependency

        if Config.PAPER_TRADING:
            logger.info(
                "PAPER Manifold bet: %s %s amount=%.2f limit_prob=%s",
                outcome, market_id, amount, limit_prob,
            )
            return {
                'status': 'paper',
                'contractId': market_id,
                'outcome': outcome,
                'amount': amount,
                'limitProb': limit_prob,
            }

        if not self.api_key:
            logger.error(
                "Manifold place_bet: no API key set. "
                "Get your key at https://manifold.markets/profile (API tab) "
                "and set MANIFOLD_API_KEY in .env."
            )
            return None

        try:
            body: Dict = {
                'contractId': market_id,
                'amount': amount,
                'outcome': outcome,
            }
            if limit_prob is not None:
                body['limitProb'] = limit_prob

            response = self._request_with_retry('POST', f"{self.BASE_URL}/v0/bet", json=body)
            if response is None:
                logger.error("Manifold POST /v0/bet failed after retries for market %s", market_id)
                return None
            if response.status_code in (200, 201):
                resp_data = response.json()
                logger.info(
                    "Manifold bet placed: %s %s amount=%.2f — resp: %s",
                    outcome, market_id, amount, resp_data,
                )
                return resp_data
            logger.error(
                "Manifold POST /v0/bet returned %d for market %s: %s",
                response.status_code, market_id, response.text,
            )
            return None
        except Exception as exc:
            logger.error("Manifold bet error for %s: %s", market_id, exc)
            return None

    def cancel_bet(self, bet_id: str) -> bool:
        """POST /v0/bet/cancel/{betId}
        Cancel a pending limit order. Returns True on success."""
        try:
            response = self._request_with_retry('POST', f"{self.BASE_URL}/v0/bet/cancel/{bet_id}")
            if response is None:
                logger.error("Manifold cancel_bet failed after retries for bet %s", bet_id)
                return False
            if response.status_code in (200, 201):
                logger.info("Manifold bet %s cancelled successfully", bet_id)
                return True
            logger.error("Manifold cancel_bet returned %d for bet %s", response.status_code, bet_id)
            return False
        except Exception as exc:
            logger.error("Manifold cancel_bet error for %s: %s", bet_id, exc)
            return False

    def get_bets(self, market_id: Optional[str] = None,
                 user_id: Optional[str] = None,
                 limit: int = 100) -> List[Dict]:
        """GET /v0/bets
        Get bet history. Can filter by market and/or user."""
        try:
            params: Dict = {'limit': limit}
            if market_id:
                params['contractId'] = market_id
            if user_id:
                params['userId'] = user_id
            response = self._request_with_retry('GET', f"{self.BASE_URL}/v0/bets", params=params)
            if response is None:
                logger.error("Manifold GET /v0/bets failed after retries")
                return []
            if response.status_code == 200:
                return response.json()
            logger.error("Manifold GET /v0/bets returned %d", response.status_code)
            return []
        except Exception as exc:
            logger.error("Error fetching Manifold bets: %s", exc)
            return []

    def get_me(self) -> Optional[Dict]:
        """GET /v0/me
        Get authenticated user's profile (balance, etc).
        Returns dict with 'balance' (Mana) and 'cashBalance' (Sweepcash)."""
        try:
            response = self._request_with_retry('GET', f"{self.BASE_URL}/v0/me")
            if response is None:
                logger.error("Manifold GET /v0/me failed after retries")
                return None
            if response.status_code == 200:
                return response.json()
            logger.error("Manifold GET /v0/me returned %d", response.status_code)
            return None
        except Exception as exc:
            logger.error("Error fetching Manifold profile: %s", exc)
            return None
