import logging
import time
from difflib import SequenceMatcher
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from config import Config
from api import KalshiAPI
from polymarket_api import PolymarketAPI
from notifications import NotificationManager

logger = logging.getLogger('kalshi_bot')


def _normalize_title(title: str) -> str:
    """Normalize a market title for fuzzy matching.

    Lowercases, strips whitespace, and removes common leading prefixes so
    that titles like "Will BTC exceed $100k?" and "BTC exceed $100k?" are
    treated as equivalent.
    """
    t = title.lower().strip()
    for prefix in ('will ', 'does ', 'is ', 'can ', 'did '):
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    return t


def _similarity(a: str, b: str) -> float:
    """Return the SequenceMatcher similarity ratio between two strings."""
    return SequenceMatcher(None, a, b).ratio()


class CrossPlatformArbitrage:
    """Detects cross-platform arbitrage opportunities between Kalshi and Polymarket.

    For each pair of matched markets the scanner checks:
    - ``kalshi_yes_ask + poly_no_ask < 100`` → buy YES on Kalshi, NO on Polymarket
    - ``poly_yes_ask + kalshi_no_ask < 100`` → buy YES on Polymarket, NO on Kalshi

    All prices are compared in *cents* (0–100).  Polymarket prices (USDC,
    0.00–1.00) are converted via :meth:`PolymarketAPI.to_cents`.
    """

    def __init__(
        self,
        kalshi_api: KalshiAPI,
        poly_api: Optional[PolymarketAPI] = None,
        min_profit_percent: Optional[float] = None,
        similarity_threshold: Optional[float] = None,
        storage=None,
    ):
        self.kalshi_api = kalshi_api
        self.poly_api = poly_api or PolymarketAPI(api_key=Config.POLYMARKET_API_KEY)
        self.min_profit_percent = (
            min_profit_percent
            if min_profit_percent is not None
            else Config.CROSS_PLATFORM_MIN_PROFIT_PERCENT
        )
        self.similarity_threshold = (
            similarity_threshold
            if similarity_threshold is not None
            else Config.MATCH_SIMILARITY_THRESHOLD
        )
        self.notifier = NotificationManager()
        self.storage = storage
        # Cache of matched pairs: list of (kalshi_market, poly_market) dicts
        self._matched_pairs: Optional[List[Tuple[Dict, Dict]]] = None

    # ------------------------------------------------------------------
    # Market matching
    # ------------------------------------------------------------------

    def match_markets(self, force_refresh: bool = False) -> List[Tuple[Dict, Dict]]:
        """Match Kalshi and Polymarket markets by title similarity.

        Returns a list of ``(kalshi_market, poly_market)`` tuples where the
        normalized title similarity is above :attr:`similarity_threshold`.
        Results are cached; pass ``force_refresh=True`` to re-fetch.
        """
        if self._matched_pairs is not None and not force_refresh:
            return self._matched_pairs

        logger.info("Fetching Kalshi markets for cross-platform matching...")
        kalshi_markets = self.kalshi_api.get_all_markets(status="open")
        logger.info("Fetching Polymarket markets for cross-platform matching...")
        poly_markets = self.poly_api.get_all_markets()

        logger.info(
            "Matching %d Kalshi vs %d Polymarket markets (threshold=%.2f)...",
            len(kalshi_markets), len(poly_markets), self.similarity_threshold,
        )

        # Pre-compute normalized Polymarket titles
        poly_normalized = [
            (m, _normalize_title(m.get('question', '') or m.get('title', '')))
            for m in poly_markets
        ]

        pairs: List[Tuple[Dict, Dict]] = []
        for km in kalshi_markets:
            k_title = _normalize_title(km.get('title', ''))
            if not k_title:
                continue
            best_score = 0.0
            best_pm = None
            for pm, p_title in poly_normalized:
                if not p_title:
                    continue
                score = _similarity(k_title, p_title)
                if score > best_score:
                    best_score = score
                    best_pm = pm
            if best_score >= self.similarity_threshold and best_pm is not None:
                pairs.append((km, best_pm))

        logger.info("Found %d matched market pairs", len(pairs))
        self._matched_pairs = pairs
        return pairs

    # ------------------------------------------------------------------
    # Price helpers
    # ------------------------------------------------------------------

    def _get_kalshi_best_asks(self, ticker: str) -> Tuple[Optional[int], Optional[int]]:
        """Return ``(yes_ask_cents, no_ask_cents)`` for a Kalshi market.

        Returns ``(None, None)`` if the orderbook is unavailable.
        """
        ob = self.kalshi_api.get_orderbook(ticker)
        if not ob:
            return None, None
        yes_asks = ob.get('yes_asks', [])
        no_asks = ob.get('no_asks', [])
        yes_ask = min((a[0] for a in yes_asks), default=None) if yes_asks else None
        no_ask = min((a[0] for a in no_asks), default=None) if no_asks else None
        return yes_ask, no_ask

    def _get_poly_best_asks(self, yes_token_id: str, no_token_id: str) -> Tuple[Optional[int], Optional[int]]:
        """Return ``(yes_ask_cents, no_ask_cents)`` for a Polymarket market.

        Fetches best ask prices for both YES and NO tokens and converts from
        USDC (0–1) to cents (0–100).
        """
        yes_price = self.poly_api.get_price(yes_token_id)
        time.sleep(Config.RATE_LIMIT_DELAY)
        no_price = self.poly_api.get_price(no_token_id)

        def _ask_cents(price_data: Dict) -> Optional[int]:
            ask = price_data.get('ask') or price_data.get('best_ask')
            if ask is None:
                return None
            return PolymarketAPI.to_cents(float(ask))

        return _ask_cents(yes_price), _ask_cents(no_price)

    @staticmethod
    def _extract_poly_token_ids(poly_market: Dict) -> Tuple[Optional[str], Optional[str]]:
        """Extract YES and NO token IDs from a Polymarket market dict.

        Polymarket markets have a ``tokens`` list with ``outcome`` and
        ``token_id`` fields.  Falls back to ``clob_token_ids`` if present.
        """
        tokens = poly_market.get('tokens', [])
        yes_id: Optional[str] = None
        no_id: Optional[str] = None
        for tok in tokens:
            outcome = (tok.get('outcome') or '').lower()
            tid = tok.get('token_id')
            if outcome == 'yes':
                yes_id = tid
            elif outcome == 'no':
                no_id = tid
        # Fallback: some responses use clob_token_ids as a list [yes, no]
        if (yes_id is None or no_id is None):
            clob_ids = poly_market.get('clob_token_ids', [])
            if len(clob_ids) >= 2:
                yes_id = yes_id or clob_ids[0]
                no_id = no_id or clob_ids[1]
        return yes_id, no_id

    # ------------------------------------------------------------------
    # Opportunity detection
    # ------------------------------------------------------------------

    def _check_pair(
        self,
        kalshi_market: Dict,
        poly_market: Dict,
        match_confidence: float,
    ) -> Optional[Dict]:
        """Check a matched pair for cross-platform arbitrage.

        Returns an opportunity dict or ``None``.
        """
        kalshi_ticker = kalshi_market.get('ticker', '')
        poly_yes_token, poly_no_token = self._extract_poly_token_ids(poly_market)

        if not poly_yes_token or not poly_no_token:
            logger.debug("Skipping %s: could not extract Polymarket token IDs", kalshi_ticker)
            return None

        kalshi_yes_ask, kalshi_no_ask = self._get_kalshi_best_asks(kalshi_ticker)
        time.sleep(Config.RATE_LIMIT_DELAY)
        poly_yes_ask, poly_no_ask = self._get_poly_best_asks(poly_yes_token, poly_no_token)

        if None in (kalshi_yes_ask, kalshi_no_ask, poly_yes_ask, poly_no_ask):
            logger.debug(
                "Skipping %s: incomplete prices (K_yes=%s K_no=%s P_yes=%s P_no=%s)",
                kalshi_ticker, kalshi_yes_ask, kalshi_no_ask, poly_yes_ask, poly_no_ask,
            )
            return None

        # Strategy A: buy YES on Kalshi + NO on Polymarket
        cost_a = kalshi_yes_ask + poly_no_ask
        profit_a = 100 - cost_a

        # Strategy B: buy YES on Polymarket + NO on Kalshi
        cost_b = poly_yes_ask + kalshi_no_ask
        profit_b = 100 - cost_b

        if profit_a <= 0 and profit_b <= 0:
            return None

        if profit_a >= profit_b:
            total_cost = cost_a
            profit_cents = profit_a
            kalshi_side = 'yes'
            kalshi_price = kalshi_yes_ask
            poly_side = 'no'
            poly_price = poly_no_ask
        else:
            total_cost = cost_b
            profit_cents = profit_b
            kalshi_side = 'no'
            kalshi_price = kalshi_no_ask
            poly_side = 'yes'
            poly_price = poly_yes_ask

        if total_cost <= 0:
            return None

        profit_percent = (profit_cents / total_cost) * 100

        if profit_percent < self.min_profit_percent:
            return None

        strategy = (
            f"Cross-platform: Buy {kalshi_side.upper()}@Kalshi({kalshi_price}¢)"
            f" + {poly_side.upper()}@Polymarket({poly_price}¢)"
        )

        return {
            'type': 'cross_platform',
            'kalshi_ticker': kalshi_ticker,
            'poly_token_id': poly_yes_token,
            'kalshi_title': kalshi_market.get('title', ''),
            'poly_title': poly_market.get('question', '') or poly_market.get('title', ''),
            'match_confidence': round(match_confidence, 4),
            'kalshi_side': kalshi_side,
            'kalshi_price': kalshi_price,
            'poly_side': poly_side,
            'poly_price': poly_price,
            'total_cost': total_cost,
            'profit_cents': profit_cents,
            'profit_percent': round(profit_percent, 2),
            'strategy': strategy,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def scan_opportunities(self, force_refresh: bool = False) -> List[Dict]:
        """Scan all matched market pairs and return cross-platform arb opportunities."""
        if not Config.POLYMARKET_ENABLED:
            logger.info("Polymarket scanning disabled (POLYMARKET_ENABLED=false)")
            return []

        pairs = self.match_markets(force_refresh=force_refresh)
        if not pairs:
            logger.info("No matched market pairs found")
            return []

        logger.info("Scanning %d matched pairs for cross-platform arbitrage...", len(pairs))
        opportunities: List[Dict] = []

        for km, pm in pairs:
            # Compute match confidence for this pair
            k_title = _normalize_title(km.get('title', ''))
            p_title = _normalize_title(pm.get('question', '') or pm.get('title', ''))
            confidence = _similarity(k_title, p_title)

            try:
                opp = self._check_pair(km, pm, confidence)
            except Exception as exc:
                logger.error(
                    "Error checking pair %s / %s: %s",
                    km.get('ticker', '?'), pm.get('condition_id', '?'), exc,
                )
                opp = None

            if opp:
                opportunities.append(opp)
                logger.info(
                    "✅ CROSS-PLATFORM ARB: %s ↔ Polymarket — %s¢ (%.2f%%)",
                    opp['kalshi_ticker'], opp['profit_cents'], opp['profit_percent'],
                )
                self.notifier.notify_opportunity(opp)
                if self.storage:
                    try:
                        self.storage.save_opportunity(opp)
                    except Exception as exc:
                        logger.warning("Failed to save cross-platform opportunity: %s", exc)

            time.sleep(Config.RATE_LIMIT_DELAY)

        logger.info(
            "Cross-platform scan complete: %d opportunities found across %d pairs",
            len(opportunities), len(pairs),
        )
        return opportunities

    def scan_continuous(self, interval: int = 300) -> None:
        """Continuously scan for cross-platform arbitrage opportunities.

        Refreshes the market match cache on every iteration so that new
        markets are picked up automatically.

        Args:
            interval: Seconds to wait between scans (default 300).
        """
        logger.info(
            "Starting continuous cross-platform arbitrage scanner (interval=%ds)", interval
        )
        iteration = 0
        try:
            while True:
                iteration += 1
                print(f"\n{'='*60}")
                print(f"Cross-platform Scan #{iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*60}")

                opportunities = self.scan_opportunities(force_refresh=True)

                if opportunities:
                    print(f"\n✅ Found {len(opportunities)} cross-platform opportunities:")
                    for opp in opportunities:
                        print(
                            f"  {opp['kalshi_ticker']} ↔ Polymarket | "
                            f"{opp['strategy']} | "
                            f"Profit: {opp['profit_cents']}¢ ({opp['profit_percent']:.2f}%)"
                        )
                else:
                    print("\n📊 No cross-platform arbitrage opportunities found this scan")

                print(f"\n⏳ Waiting {interval}s until next scan...")
                time.sleep(interval)

        except KeyboardInterrupt:
            print(f"\n\n{'='*60}")
            print("🛑 Cross-platform scanner stopped by user")
            print(f"Total scans: {iteration}")
            print(f"{'='*60}")
            logger.info("Cross-platform scanner stopped after %d iterations", iteration)
