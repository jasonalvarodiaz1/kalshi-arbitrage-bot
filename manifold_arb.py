import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from config import Config
from api import KalshiAPI
from manifold_api import ManifoldAPI
from notifications import NotificationManager
from cross_platform_arb import _normalize_title, _similarity

logger = logging.getLogger('kalshi_bot')


class ManifoldArbitrage:
    """Cross-platform arbitrage between Kalshi and Manifold Markets.

    Strategy:
    - Match Kalshi and Manifold markets by title similarity (reuses _normalize_title
      and _similarity from cross_platform_arb.py)
    - Compare prices: Kalshi YES ask vs Manifold YES probability (and vice versa)

    Arb conditions:
    1. Kalshi YES cheap + Manifold YES expensive:
       Buy YES on Kalshi, sell YES (buy NO) on Manifold
       Profit if: kalshi_yes_ask + manifold_no_price < 100

    2. Manifold YES cheap + Kalshi YES expensive:
       Buy YES on Manifold, sell YES (buy NO) on Kalshi
       Profit if: manifold_yes_price + kalshi_no_ask < 100

    Since Manifold uses AMM, the "price" is just the probability.
    YES price = probability, NO price = 1 - probability.
    Both are converted to cents for comparison with Kalshi prices.
    """

    # Keywords used to identify politics-related markets (case-insensitive)
    POLITICS_KEYWORDS = [
        'president', 'election', 'senate', 'congress', 'governor', 'vote',
        'party', 'democrat', 'republican', 'trump', 'biden',
    ]

    # Keywords used to identify sports-related markets (case-insensitive)
    SPORTS_KEYWORDS = [
        'nfl', 'nba', 'mlb', 'nhl', 'soccer', 'football', 'basketball',
        'baseball', 'hockey', 'super bowl', 'world series', 'championship',
        'game', 'match', 'team', 'win', 'score',
    ]

    def __init__(
        self,
        kalshi_api: KalshiAPI,
        manifold_api: Optional[ManifoldAPI] = None,
        min_profit_percent: Optional[float] = None,
        similarity_threshold: Optional[float] = None,
        storage=None,
    ):
        self.kalshi_api = kalshi_api
        self.manifold_api = manifold_api or ManifoldAPI(api_key=Config.MANIFOLD_API_KEY)
        self.min_profit_percent = (
            min_profit_percent
            if min_profit_percent is not None
            else Config.MANIFOLD_MIN_PROFIT_PERCENT
        )
        self.similarity_threshold = (
            similarity_threshold
            if similarity_threshold is not None
            else Config.MATCH_SIMILARITY_THRESHOLD
        )
        self.notifier = NotificationManager()
        self.storage = storage
        # Cache of matched pairs: list of (kalshi_market, manifold_market, score) tuples
        self._matched_pairs: Optional[List[Tuple[Dict, Dict, float]]] = None

    # ------------------------------------------------------------------
    # Market type helpers
    # ------------------------------------------------------------------

    def _is_sweepstakes(self, market: Dict) -> bool:
        """Check if a Manifold market uses Sweepcash (real money).
        market['token'] == 'CASH'"""
        return market.get('token') == 'CASH'

    def _is_binary(self, market: Dict) -> bool:
        """Check if market is binary (YES/NO).
        market['outcomeType'] == 'BINARY'"""
        return market.get('outcomeType') == 'BINARY'

    # ------------------------------------------------------------------
    # Category filtering
    # ------------------------------------------------------------------

    def _is_eligible_category(self, market: Dict) -> bool:
        """Return True if *market* belongs to an allowed Manifold category.

        When ``Config.MANIFOLD_CATEGORIES`` is ``'all'`` every market passes.
        Otherwise the market question is checked case-insensitively against
        the keyword lists for each configured category.
        """
        categories_cfg = Config.MANIFOLD_CATEGORIES.strip().lower()
        if categories_cfg == 'all':
            return True

        allowed = [c.strip() for c in categories_cfg.split(',') if c.strip()]
        question = (market.get('question', '') or '').lower()

        if 'politics' in allowed:
            if any(kw in question for kw in self.POLITICS_KEYWORDS):
                return True
        if 'sports' in allowed:
            if any(kw in question for kw in self.SPORTS_KEYWORDS):
                return True
        return False

    # ------------------------------------------------------------------
    # Market matching
    # ------------------------------------------------------------------

    def match_markets(
        self, force_refresh: bool = False, sweepstakes_only: bool = True
    ) -> List[Tuple[Dict, Dict, float]]:
        """Match Kalshi and Manifold markets by title similarity.

        1. Fetch Kalshi markets (open, page-capped)
        2. Fetch Manifold markets (token='CASH' for sweepstakes, filter binary open)
        3. Fuzzy match titles using _normalize_title and _similarity
        4. Return list of (kalshi_market, manifold_market, match_score) tuples

        Results are cached; pass ``force_refresh=True`` to re-fetch.
        """
        if self._matched_pairs is not None and not force_refresh:
            return self._matched_pairs

        logger.info("Fetching Kalshi markets for Manifold matching...")
        kalshi_max_pages = int(getattr(Config, 'CROSS_PLATFORM_KALSHI_MAX_PAGES', 10))
        kalshi_markets = self.kalshi_api.get_all_markets(status="open", max_pages=kalshi_max_pages)
        logger.info("Kalshi: %d markets fetched (%d pages cap)", len(kalshi_markets), kalshi_max_pages)

        logger.info("Fetching Manifold markets for cross-platform matching...")
        token_filter = 'CASH' if (sweepstakes_only and Config.MANIFOLD_SWEEPSTAKES_ONLY) else None
        manifold_markets = self.manifold_api.get_markets(
            limit=1000,
            sort='liquidity',
            filter_='open',
        )
        if token_filter:
            # Supplement with an explicit CASH search if the base endpoint doesn't filter by token
            cash_markets = self.manifold_api.search_markets(
                term='',
                limit=1000,
                filter_='open',
                sort='liquidity',
                token=token_filter,
            )
            # Merge and de-duplicate by id
            seen_ids = {m.get('id') for m in manifold_markets}
            for m in cash_markets:
                if m.get('id') not in seen_ids:
                    manifold_markets.append(m)
                    seen_ids.add(m.get('id'))

        # Filter to binary, open, non-resolved markets
        eligible_manifold = []
        for m in manifold_markets:
            if not self._is_binary(m):
                continue
            if m.get('isResolved'):
                continue
            if sweepstakes_only and Config.MANIFOLD_SWEEPSTAKES_ONLY and not self._is_sweepstakes(m):
                continue
            if not self._is_eligible_category(m):
                continue
            eligible_manifold.append(m)

        logger.info(
            "Manifold: %d eligible binary%s markets after filtering",
            len(eligible_manifold),
            ' sweepstakes' if (sweepstakes_only and Config.MANIFOLD_SWEEPSTAKES_ONLY) else '',
        )

        # Category pre-filter for Kalshi side
        categories_cfg = Config.MANIFOLD_CATEGORIES.strip().lower()
        if categories_cfg == 'all':
            eligible_kalshi = kalshi_markets
        else:
            eligible_kalshi = [m for m in kalshi_markets if self._is_eligible_category(m)]
        logger.info(
            "Kalshi category filter (%s): %d eligible markets",
            Config.MANIFOLD_CATEGORIES, len(eligible_kalshi),
        )

        logger.info(
            "Matching %d Kalshi vs %d Manifold markets (threshold=%.2f)...",
            len(eligible_kalshi), len(eligible_manifold), self.similarity_threshold,
        )

        # Pre-compute normalized Manifold titles + word sets for fast pre-filtering
        _STOPWORDS = {'a', 'an', 'the', 'in', 'on', 'at', 'to', 'of', 'for',
                      'by', 'or', 'and', 'be', 'is', 'are', 'will', 'does'}
        manifold_normalized = []
        for m in eligible_manifold:
            title = _normalize_title(m.get('question', '') or '')
            words = {w for w in title.split() if len(w) > 2 and w not in _STOPWORDS}
            manifold_normalized.append((m, title, words))

        pairs: List[Tuple[Dict, Dict, float]] = []
        for km in eligible_kalshi:
            k_title = _normalize_title(km.get('title', '') or '')
            if not k_title:
                continue
            k_words = {w for w in k_title.split() if len(w) > 2 and w not in _STOPWORDS}
            best_score = 0.0
            best_mm = None
            for mm, m_title, m_words in manifold_normalized:
                if not m_title:
                    continue
                # Fast word-overlap pre-filter: skip pairs with no shared keywords
                if k_words and m_words and not (k_words & m_words):
                    continue
                score = _similarity(k_title, m_title)
                if score > best_score:
                    best_score = score
                    best_mm = mm
            if best_score >= self.similarity_threshold and best_mm is not None:
                pairs.append((km, best_mm, best_score))

        logger.info("Found %d matched Kalshi ↔ Manifold market pairs", len(pairs))
        self._matched_pairs = pairs
        return pairs

    # ------------------------------------------------------------------
    # Price helpers
    # ------------------------------------------------------------------

    def _get_kalshi_best_asks(self, ticker: str) -> Tuple[Optional[int], Optional[int]]:
        """Return ``(yes_ask_cents, no_ask_cents)`` for a Kalshi market."""
        ob = self.kalshi_api.get_orderbook(ticker)
        if not ob:
            return None, None
        yes_asks = ob.get('yes_asks', [])
        no_asks = ob.get('no_asks', [])
        yes_ask = min((a[0] for a in yes_asks), default=None) if yes_asks else None
        no_ask = min((a[0] for a in no_asks), default=None) if no_asks else None
        return yes_ask, no_ask

    # ------------------------------------------------------------------
    # Opportunity detection
    # ------------------------------------------------------------------

    def _check_pair(
        self,
        kalshi_market: Dict,
        manifold_market: Dict,
        match_confidence: float,
    ) -> Optional[Dict]:
        """Check a matched pair for cross-platform arbitrage.

        Get Kalshi orderbook (yes_ask, no_ask in cents).
        Get Manifold probability → yes_price_cents, no_price_cents.

        Check both directions:
        A) kalshi_yes_ask + manifold_no_cents < 100 → buy YES Kalshi + NO Manifold
        B) manifold_yes_cents + kalshi_no_ask < 100 → buy YES Manifold + NO Kalshi

        Returns an opportunity dict or None.
        """
        kalshi_ticker = kalshi_market.get('ticker', '')
        manifold_id = manifold_market.get('id', '')

        probability = manifold_market.get('probability')
        if probability is None:
            logger.debug("Skipping %s: Manifold market has no probability", kalshi_ticker)
            return None

        manifold_yes_cents = ManifoldAPI.to_cents(probability)
        manifold_no_cents = ManifoldAPI.to_cents(1.0 - probability)

        kalshi_yes_ask, kalshi_no_ask = self._get_kalshi_best_asks(kalshi_ticker)

        if None in (kalshi_yes_ask, kalshi_no_ask):
            logger.debug(
                "Skipping %s: incomplete Kalshi prices (yes=%s no=%s)",
                kalshi_ticker, kalshi_yes_ask, kalshi_no_ask,
            )
            return None

        # Strategy A: buy YES on Kalshi + NO on Manifold
        cost_a = kalshi_yes_ask + manifold_no_cents
        profit_a = 100 - cost_a

        # Strategy B: buy YES on Manifold + NO on Kalshi
        cost_b = manifold_yes_cents + kalshi_no_ask
        profit_b = 100 - cost_b

        if profit_a <= 0 and profit_b <= 0:
            return None

        if profit_a >= profit_b:
            total_cost = cost_a
            profit_cents = profit_a
            kalshi_side = 'yes'
            kalshi_price = kalshi_yes_ask
            manifold_side = 'no'
            manifold_price = manifold_no_cents
        else:
            total_cost = cost_b
            profit_cents = profit_b
            kalshi_side = 'no'
            kalshi_price = kalshi_no_ask
            manifold_side = 'yes'
            manifold_price = manifold_yes_cents

        if total_cost <= 0:
            return None

        profit_percent = (profit_cents / total_cost) * 100

        if profit_percent < self.min_profit_percent:
            return None

        strategy = (
            f"Buy {kalshi_side.upper()}@Kalshi({kalshi_price}¢)"
            f" + {manifold_side.upper()}@Manifold({manifold_price}¢)"
        )

        return {
            'type': 'kalshi_manifold',
            'kalshi_ticker': kalshi_ticker,
            'manifold_id': manifold_id,
            'manifold_slug': manifold_market.get('slug', ''),
            'kalshi_title': kalshi_market.get('title', ''),
            'manifold_title': manifold_market.get('question', ''),
            'match_confidence': round(match_confidence, 4),
            'kalshi_side': kalshi_side,
            'kalshi_price': kalshi_price,
            'manifold_side': manifold_side,
            'manifold_price': manifold_price,
            'total_cost': total_cost,
            'profit_cents': profit_cents,
            'profit_percent': round(profit_percent, 2),
            'manifold_is_sweepstakes': self._is_sweepstakes(manifold_market),
            'manifold_liquidity': manifold_market.get('totalLiquidity', 0),
            'strategy': strategy,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def scan_opportunities(self, force_refresh: bool = False) -> List[Dict]:
        """Scan all matched pairs and return Kalshi ↔ Manifold arb opportunities."""
        if not Config.MANIFOLD_ENABLED:
            logger.info("Manifold scanning disabled (MANIFOLD_ENABLED=false)")
            return []

        pairs = self.match_markets(force_refresh=force_refresh)
        if not pairs:
            logger.info("No matched Kalshi ↔ Manifold market pairs found")
            return []

        logger.info("Scanning %d matched pairs for Kalshi ↔ Manifold arbitrage...", len(pairs))
        opportunities: List[Dict] = []

        for km, mm, confidence in pairs:
            try:
                opp = self._check_pair(km, mm, confidence)
            except Exception as exc:
                logger.error(
                    "Error checking pair %s / %s: %s",
                    km.get('ticker', '?'), mm.get('id', '?'), exc,
                )
                opp = None

            if opp:
                opportunities.append(opp)
                logger.info(
                    "✅ MANIFOLD ARB: %s ↔ Manifold(%s) — %s¢ (%.2f%%)",
                    opp['kalshi_ticker'], opp['manifold_id'],
                    opp['profit_cents'], opp['profit_percent'],
                )
                self.notifier.notify_opportunity(opp)
                if self.storage:
                    try:
                        self.storage.save_opportunity(opp)
                    except Exception as exc:
                        logger.warning("Failed to save Manifold opportunity: %s", exc)

            time.sleep(Config.RATE_LIMIT_DELAY)

        logger.info(
            "Manifold scan complete: %d opportunities found across %d pairs",
            len(opportunities), len(pairs),
        )
        return opportunities

    def scan_continuous(self, interval: Optional[int] = None) -> None:
        """Continuously scan for Kalshi ↔ Manifold arbitrage opportunities.

        Args:
            interval: Seconds to wait between scans. Defaults to Config.MANIFOLD_SCAN_INTERVAL.
        """
        scan_interval = interval if interval is not None else Config.MANIFOLD_SCAN_INTERVAL
        logger.info(
            "Starting continuous Kalshi ↔ Manifold arbitrage scanner (interval=%ds)", scan_interval
        )
        iteration = 0
        try:
            while True:
                iteration += 1
                print(f"\n{'='*60}")
                print(f"Manifold Scan #{iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*60}")

                opportunities = self.scan_opportunities(force_refresh=True)

                if opportunities:
                    print(f"\n✅ Found {len(opportunities)} Kalshi ↔ Manifold opportunities:")
                    for opp in opportunities:
                        sweeps = '(Sweepcash 💰)' if opp['manifold_is_sweepstakes'] else '(Mana)'
                        print(
                            f"  {opp['kalshi_ticker']} ↔ Manifold {sweeps} | "
                            f"{opp['strategy']} | "
                            f"Profit: {opp['profit_cents']}¢ ({opp['profit_percent']:.2f}%)"
                        )
                else:
                    print("\n📊 No Kalshi ↔ Manifold arbitrage opportunities found this scan")

                print(f"\n⏳ Waiting {scan_interval}s until next scan...")
                time.sleep(scan_interval)

        except KeyboardInterrupt:
            print(f"\n\n{'='*60}")
            print("🛑 Manifold scanner stopped by user")
            print(f"Total scans: {iteration}")
            print(f"{'='*60}")
            logger.info("Manifold scanner stopped after %d iterations", iteration)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Cross-platform Kalshi/Manifold arbitrage scanner')
    parser.add_argument('--once', action='store_true', help='Run a single scan and exit')
    parser.add_argument('--interval', type=int, default=None, help='Seconds between scans')
    args = parser.parse_args()

    Config.print_config()
    from kalshi_bot import KalshiAPI
    kalshi = KalshiAPI(api_key=Config.KALSHI_API_KEY)
    scanner = ManifoldArbitrage(kalshi_api=kalshi)

    if args.once:
        opps = scanner.scan_opportunities(force_refresh=True)
        if not opps:
            print('\nNo Kalshi ↔ Manifold arbitrage opportunities found.')
        else:
            print(f'\nFound {len(opps)} opportunit{"y" if len(opps)==1 else "ies"}:')
            for o in opps:
                print(
                    f"  {o['kalshi_ticker']} ↔ Manifold({o['manifold_id']}) | "
                    f"{o['strategy']} | profit={o['profit_cents']}c ({o['profit_percent']:.2f}%)"
                )
    else:
        scanner.scan_continuous(interval=args.interval)
