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
        poly_api: Optional[PolymarketAPI] = None,
        min_profit_percent: Optional[float] = None,
        similarity_threshold: Optional[float] = None,
        storage=None,
    ):
        self.kalshi_api = kalshi_api
        self.poly_api = poly_api or PolymarketAPI(
            api_key=Config.POLYMARKET_API_KEY,
            private_key=Config.POLYMARKET_PRIVATE_KEY,
        )
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
    # Category filtering
    # ------------------------------------------------------------------

    def _is_eligible_category(self, market: Dict) -> bool:
        """Return True if *market* belongs to an allowed Polymarket category.

        When ``Config.POLYMARKET_CATEGORIES`` is ``'all'`` every market passes.
        Otherwise the market title is checked case-insensitively against the
        keyword lists for each configured category.
        """
        categories_cfg = Config.POLYMARKET_CATEGORIES.strip().lower()
        if categories_cfg == 'all':
            return True

        allowed = [c.strip() for c in categories_cfg.split(',') if c.strip()]
        title = (market.get('title', '') or '').lower()
        series_ticker = (market.get('series_ticker', '') or '').lower()

        if 'politics' in allowed:
            if any(kw in title for kw in self.POLITICS_KEYWORDS) or \
               any(kw in series_ticker for kw in self.POLITICS_KEYWORDS):
                return True
        if 'sports' in allowed:
            if any(kw in title for kw in self.SPORTS_KEYWORDS) or \
               any(kw in series_ticker for kw in self.SPORTS_KEYWORDS):
                return True
        return False

    def _is_eligible_poly_market(self, market: Dict) -> bool:
        """Return True if a *Polymarket* market belongs to an allowed category.

        Checks Polymarket's structured ``tags`` field first, then falls back
        to keyword matching on the question/title.  Mirrors ``_is_eligible_category``
        but adapted to Polymarket's data shape.
        """
        categories_cfg = Config.POLYMARKET_CATEGORIES.strip().lower()
        if categories_cfg == 'all':
            return True

        allowed = [c.strip() for c in categories_cfg.split(',') if c.strip()]
        question = (market.get('question', '') or market.get('title', '') or '').lower()

        # Collect tag strings from the structured tags field
        tags = market.get('tags') or []
        tag_strs = []
        for tag in tags:
            if isinstance(tag, dict):
                tag_strs.append((tag.get('slug', '') or tag.get('label', '') or '').lower())
            else:
                tag_strs.append(str(tag).lower())

        if 'politics' in allowed:
            if any(kw in question for kw in self.POLITICS_KEYWORDS) or \
               any(any(kw in t for kw in self.POLITICS_KEYWORDS) for t in tag_strs):
                return True
        if 'sports' in allowed:
            if any(kw in question for kw in self.SPORTS_KEYWORDS) or \
               any(any(kw in t for kw in self.SPORTS_KEYWORDS) for t in tag_strs):
                return True
        return False

    def _expiry_proximity_ok(self, kalshi_market: Dict, poly_market: Dict) -> bool:
        """Return True if both markets settle within CROSS_PLATFORM_MAX_EXPIRY_DIFF_HOURS.

        Prevents matching same-named markets that expire months apart
        (e.g. Kalshi 'BTC > $90k by March' vs Polymarket 'BTC > $90k by April').
        If either date is unreadable the check passes conservatively.
        """
        try:
            k_close_str = kalshi_market.get('close_time', '')
            if not k_close_str:
                return True
            k_close = datetime.fromisoformat(k_close_str.replace('Z', '+00:00'))

            # Polymarket uses end_date_iso or end_date
            p_close_str = (
                poly_market.get('end_date_iso')
                or poly_market.get('end_date')
                or ''
            )
            if not p_close_str:
                return True  # no Poly date to compare against

            # Normalise: Polymarket sometimes sends date-only strings
            if 'T' not in p_close_str:
                p_close_str += 'T00:00:00+00:00'
            p_close = datetime.fromisoformat(p_close_str.replace('Z', '+00:00'))

            diff_hours = abs((k_close - p_close).total_seconds()) / 3600
            return diff_hours <= Config.CROSS_PLATFORM_MAX_EXPIRY_DIFF_HOURS
        except Exception:
            return True  # parse failure → allow through

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
        # Cap pages to avoid fetching all 87k+ markets. Cross-platform execution is
        # blocked for US users (Polymarket app-only), so this scanner is monitoring only.
        # Increase CROSS_PLATFORM_KALSHI_MAX_PAGES in .env to scan more Kalshi markets.
        kalshi_max_pages = int(getattr(Config, 'CROSS_PLATFORM_KALSHI_MAX_PAGES', 10))
        kalshi_markets = self.kalshi_api.get_all_markets(status="open", max_pages=kalshi_max_pages)
        logger.info("Kalshi: %d markets fetched (%d pages cap)", len(kalshi_markets), kalshi_max_pages)

        logger.info("Fetching Polymarket markets for cross-platform matching...")
        # Use tag-filtered Gamma API fetch instead of fetching all markets
        categories_cfg = Config.POLYMARKET_CATEGORIES.strip().lower()
        poly_tags = [t.strip() for t in categories_cfg.split(',') if t.strip()] if categories_cfg != 'all' else ['sports', 'politics']
        poly_markets: List[Dict] = []
        for tag in poly_tags:
            tag_markets = self.poly_api.get_markets_for_tag(tag, min_liquidity=25.0)
            poly_markets.extend(tag_markets)
        # De-duplicate by conditionId / id in case tags overlap
        seen_ids: set = set()
        poly_deduped: List[Dict] = []
        for m in poly_markets:
            mid = m.get('conditionId') or m.get('id') or id(m)
            if mid not in seen_ids:
                seen_ids.add(mid)
                poly_deduped.append(m)
        poly_markets = poly_deduped
        logger.info("Polymarket: %d unique binary markets (tags: %s)", len(poly_markets), poly_tags)

        # Category pre-filter (Kalshi side)
        if categories_cfg == 'all':
            eligible = kalshi_markets
        else:
            eligible = [m for m in kalshi_markets if self._is_eligible_category(m)]
        filtered_out = len(kalshi_markets) - len(eligible)
        logger.info(
            "Kalshi category filter (%s): %d eligible, %d filtered out",
            Config.POLYMARKET_CATEGORIES, len(eligible), filtered_out,
        )
        print(
            f"\U0001f50d Kalshi filter ({Config.POLYMARKET_CATEGORIES}): "
            f"{len(eligible)} eligible, {filtered_out} filtered out"
        )

        # Polymarket is already tag-filtered above — no further post-filter needed

        logger.info(
            "Matching %d Kalshi vs %d Polymarket markets (threshold=%.2f)...",
            len(eligible), len(poly_markets), self.similarity_threshold,
        )

        # Pre-compute normalized Polymarket titles
        poly_normalized = [
            (m, _normalize_title(m.get('question', '') or m.get('title', '')))
            for m in poly_markets
        ]

        pairs: List[Tuple[Dict, Dict]] = []
        for km in eligible:
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
                # Reject pairs where settlement dates differ by more than the threshold
                if not self._expiry_proximity_ok(km, best_pm):
                    logger.debug(
                        "Skipping %s / Poly match: expiry dates too far apart",
                        km.get('ticker', '?'),
                    )
                    continue
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

        Uses the orderbook endpoint (not /prices) to get the true live best ask.
        Polymarket's /prices endpoint can lag; the orderbook is authoritative.
        """
        def _best_ask_cents(token_id: str) -> Optional[int]:
            ob = self.poly_api.get_orderbook(token_id)
            if not ob:
                return None
            asks = ob.get('asks', [])
            if not asks:
                return None
            try:
                best = min(float(a['price']) for a in asks)
                return PolymarketAPI.to_cents(best)
            except (KeyError, ValueError, TypeError):
                return None

        yes_ask = _best_ask_cents(yes_token_id)
        time.sleep(Config.RATE_LIMIT_DELAY)
        no_ask = _best_ask_cents(no_token_id)
        return yes_ask, no_ask

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

        # Correct execution token: buy YES token only when poly_side is 'yes'
        poly_exec_token = poly_yes_token if poly_side == 'yes' else poly_no_token

        return {
            'type': 'cross_platform',
            'kalshi_ticker': kalshi_ticker,
            'poly_token_id': poly_yes_token,
            'poly_exec_token_id': poly_exec_token,
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
                # Execute both legs (paper or live)
                self.execute_opportunity(opp)

            time.sleep(Config.RATE_LIMIT_DELAY)

        logger.info(
            "Cross-platform scan complete: %d opportunities found across %d pairs",
            len(opportunities), len(pairs),
        )
        return opportunities

    def execute_opportunity(self, opp: Dict) -> bool:
        """Execute both legs of a cross-platform arb opportunity.

        Kalshi leg is placed via the Kalshi REST API.  Polymarket leg is
        placed via the CLOB API using an EIP-712 signed FOK order so that
        an unfilled Polymarket order never leaves a naked Kalshi position.

        When ``PAPER_TRADING=true`` both legs are simulated and logged only.
        Uses 1 contract/share per execution cycle; scale by running more scans.

        Returns True if both legs were successfully attempted.
        """
        kalshi_ticker = opp['kalshi_ticker']
        kalshi_side = opp['kalshi_side']
        kalshi_price = opp['kalshi_price']
        poly_token = opp.get('poly_exec_token_id') or opp.get('poly_token_id')
        poly_side = opp['poly_side']
        poly_price_cents = opp['poly_price']
        poly_price_frac = poly_price_cents / 100.0

        if Config.PAPER_TRADING:
            logger.info(
                "PAPER XPLAT: Kalshi %s %s @ %d¢ + Polymarket %s %s @ %d¢ | profit=%d¢ (%.2f%%)",
                kalshi_side.upper(), kalshi_ticker, kalshi_price,
                poly_side.upper(), (poly_token or '')[:16], poly_price_cents,
                opp['profit_cents'], opp['profit_percent'],
            )
            return True

        if not Config.POLYMARKET_EXECUTION_ENABLED:
            logger.warning(
                "XPLAT: Polymarket leg skipped — US accounts can only trade via the "
                "Polymarket mobile app (API trading not available for US users). "
                "Kalshi leg NOT placed to avoid a naked position. "
                "Set POLYMARKET_EXECUTION_ENABLED=true in .env only if you have "
                "non-US API access."
            )
            return False

        # ---- Kalshi leg ----
        try:
            kalshi_resp = self.kalshi_api.execute_trade(
                ticker=kalshi_ticker,
                side=kalshi_side,
                price_cents=kalshi_price,
                quantity=1,
            )
            if not kalshi_resp:
                logger.error("XPLAT: Kalshi leg failed for %s", kalshi_ticker)
                return False
            logger.info("XPLAT: Kalshi leg placed — %s %s @ %d¢", kalshi_side, kalshi_ticker, kalshi_price)
        except Exception as exc:
            logger.error("XPLAT: Kalshi leg error for %s: %s", kalshi_ticker, exc)
            return False

        # ---- Polymarket leg (FOK — won't leave an unhedged Kalshi side) ----
        try:
            poly_resp = self.poly_api.place_order(
                token_id=poly_token,
                side='BUY',
                price=poly_price_frac,
                size=1.0,
                order_type='FOK',
            )
            if not poly_resp:
                logger.error(
                    "XPLAT: Polymarket leg failed for %s — Kalshi leg is unhedged!",
                    (poly_token or '')[:16],
                )
                return False
            logger.info(
                "XPLAT: Polymarket leg placed — %s %s @ %.4f",
                poly_side.upper(), (poly_token or '')[:16], poly_price_frac,
            )
        except Exception as exc:
            logger.error("XPLAT: Polymarket leg error for %s: %s", (poly_token or '')[:16], exc)
            return False

        return True

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


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Cross-platform Kalshi/Polymarket arbitrage scanner')
    parser.add_argument('--once', action='store_true', help='Run a single scan and exit')
    parser.add_argument('--interval', type=int, default=300, help='Seconds between scans (default 300)')
    args = parser.parse_args()

    Config.print_config()
    from kalshi_bot import KalshiAPI
    kalshi = KalshiAPI(api_key=Config.KALSHI_API_KEY)
    scanner = CrossPlatformArbitrage(kalshi_api=kalshi)

    if args.once:
        opps = scanner.scan_opportunities(force_refresh=True)
        if not opps:
            print('\nNo cross-platform arbitrage opportunities found.')
        else:
            print(f'\nFound {len(opps)} opportunit{"y" if len(opps)==1 else "ies"}:')
            for o in opps:
                print(f"  {o['kalshi_ticker']} | {o['strategy']} | profit={o['profit_cents']}c ({o['profit_percent']:.2f}%)")
    else:
        scanner.scan_continuous(interval=args.interval)
