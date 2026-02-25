import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

# Ensure UTF-8 output on Windows (cp1252 can't handle emoji in log messages)
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

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
            else Config.MANIFOLD_MATCH_THRESHOLD
        )
        self.notifier = NotificationManager()
        self.storage = storage
        # Cache of matched pairs: list of (kalshi_market, manifold_market, score) tuples
        self._matched_pairs: Optional[List[Tuple[Dict, Dict, float]]] = None
        # Paper trade tracking
        self._paper_trades: List[Dict] = []

    # ------------------------------------------------------------------
    # Market type helpers
    # ------------------------------------------------------------------

    def _is_sweepstakes(self, market: Dict) -> bool:
        """Check if a Manifold market is sweepstakes-eligible (real money).

        Manifold's API reports sweepstakes markets via the search endpoint
        with ``token='CASH_AND_MANA'``.  The returned objects still show
        ``token='MANA'`` but include a ``siblingContractId`` field that
        links the Mana pool to the Cash pool.  Markets fetched from the
        plain ``/v0/markets`` endpoint may lack the ``token`` field entirely.
        """
        tok = market.get('token', '')
        if tok == 'CASH':
            return True
        # siblingContractId is present only on sweepstakes-eligible markets
        if market.get('siblingContractId'):
            return True
        # Tag applied when fetched via CASH_AND_MANA search
        if market.get('_sweepstakes'):
            return True
        return False

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
    # Settlement time helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_kalshi_close(market: Dict) -> Optional[datetime]:
        """Parse the close/expiration time from a Kalshi market dict.

        Prefers ``close_time`` (when trading ends and the bracket settles)
        over ``expected_expiration_time`` (which can be years away for
        early-close-eligible markets).
        """
        for field in ('close_time', 'expected_expiration_time'):
            raw = market.get(field, '')
            if not raw or raw.startswith('0001') or raw.startswith('2099'):
                continue
            try:
                return datetime.fromisoformat(raw.replace('Z', '+00:00'))
            except (ValueError, TypeError):
                continue
        return None

    @staticmethod
    def _within_settle_window(market: Dict, cutoff: datetime) -> bool:
        """Return True if Kalshi market settles before *cutoff*."""
        ct = ManifoldArbitrage._parse_kalshi_close(market)
        if ct is None:
            return False  # Unknown close = skip (conservative)
        return ct <= cutoff

    @staticmethod
    def _manifold_within_settle_window(market: Dict, cutoff_ms: int) -> bool:
        """Return True if Manifold market closes before *cutoff_ms* (epoch ms)."""
        close_ms = market.get('closeTime')
        if close_ms is None:
            return False
        return close_ms <= cutoff_ms

    # ------------------------------------------------------------------
    # Market matching
    # ------------------------------------------------------------------

    def match_markets(
        self, force_refresh: bool = False, sweepstakes_only: bool = True
    ) -> List[Tuple[Dict, Dict, float]]:
        """Match Kalshi and Manifold markets by title similarity.

        1. Fetch Kalshi markets (open, page-capped)
        2. Fetch Manifold markets (sweepstakes or all binary)
        3. Fuzzy match titles using _normalize_title and _similarity
        4. Return list of (kalshi_market, manifold_market, match_score) tuples

        Results are cached; pass ``force_refresh=True`` to re-fetch.
        """
        if self._matched_pairs is not None and not force_refresh:
            return self._matched_pairs

        # ----------------------------------------------------------
        # Kalshi side: use events endpoint to bypass sports parlay flood
        # The default /markets listing is dominated by KXMVESPORTSMULTIGAME-*
        # parlays (4000+ of the first results).  Fetching via /events lets us
        # skip the Sports category and reach politics/economics/tech markets.
        # ----------------------------------------------------------
        logger.info("Fetching Kalshi markets via events (skipping Sports)...")
        kalshi_max_pages = int(getattr(Config, 'CROSS_PLATFORM_KALSHI_MAX_PAGES', 10))
        kalshi_markets = self.kalshi_api.get_events_with_markets(
            status="open",
            max_pages=kalshi_max_pages,
            skip_categories={'Sports'},
        )
        logger.info("Kalshi: %d non-sports markets fetched", len(kalshi_markets))

        logger.info("Fetching Manifold markets for cross-platform matching...")
        sweeps_filter = sweepstakes_only and Config.MANIFOLD_SWEEPSTAKES_ONLY

        # Primary fetch: broad binary markets sorted by liquidity
        manifold_markets = self.manifold_api.get_markets(
            limit=1000,
            sort='liquidity',
            filter_='open',
        )

        if sweeps_filter:
            # Manifold changed: sweepstakes markets use token='CASH_AND_MANA'
            # in the search API (not 'CASH'). Tag them so _is_sweepstakes works.
            cash_markets = self.manifold_api.search_markets(
                term='',
                limit=1000,
                filter_='open',
                sort='liquidity',
                token='CASH_AND_MANA',
            )
            # Mark these as sweepstakes-eligible
            for m in cash_markets:
                m['_sweepstakes'] = True
            # Merge and de-duplicate by id
            seen_ids = {m.get('id') for m in manifold_markets}
            for m in cash_markets:
                if m.get('id') not in seen_ids:
                    manifold_markets.append(m)
                    seen_ids.add(m.get('id'))
                else:
                    # Tag the existing copy
                    for existing in manifold_markets:
                        if existing.get('id') == m.get('id'):
                            existing['_sweepstakes'] = True
                            break

        # Filter to binary, open, non-resolved markets
        eligible_manifold = []
        for m in manifold_markets:
            if not self._is_binary(m):
                continue
            if m.get('isResolved'):
                continue
            if sweeps_filter and not self._is_sweepstakes(m):
                continue
            if not self._is_eligible_category(m):
                continue
            eligible_manifold.append(m)

        logger.info(
            "Manifold: %d eligible binary%s markets after filtering",
            len(eligible_manifold),
            ' sweepstakes' if sweeps_filter else '',
        )

        # ---- Settlement time filter ----
        # Skip markets that close too far in the future (user wants quick turnaround)
        max_settle_days = Config.MANIFOLD_MAX_SETTLE_DAYS
        now = datetime.now(timezone.utc)

        if max_settle_days > 0:
            cutoff = now + timedelta(days=max_settle_days)
            cutoff_ms = int(cutoff.timestamp() * 1000)  # Manifold uses epoch ms

            pre_kalshi = len(kalshi_markets)
            kalshi_markets = [m for m in kalshi_markets if self._within_settle_window(m, cutoff)]

            pre_manifold = len(eligible_manifold)
            eligible_manifold = [
                m for m in eligible_manifold
                if self._manifold_within_settle_window(m, cutoff_ms)
            ]

            logger.info(
                "Settlement filter (max %.1f days): Kalshi %d->%d, Manifold %d->%d",
                max_settle_days, pre_kalshi, len(kalshi_markets),
                pre_manifold, len(eligible_manifold),
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

        logger.info("Found %d matched Kalshi<->Manifold market pairs", len(pairs))
        self._matched_pairs = pairs
        return pairs

    # ------------------------------------------------------------------
    # Price helpers
    # ------------------------------------------------------------------

    def _get_kalshi_best_asks(self, ticker: str, market: Optional[Dict] = None) -> Tuple[Optional[int], Optional[int]]:
        """Return ``(yes_ask_cents, no_ask_cents)`` for a Kalshi market.

        Prefers the ``yes_ask`` / ``no_ask`` fields from the market metadata
        (already populated from the events fetch).  These represent the true
        best ask — the raw orderbook without a ``depth`` parameter includes
        dust limit-orders at 1-2¢ that distort the real spread.
        """
        # Try market metadata first (from events fetch)
        if market:
            ya = market.get('yes_ask')
            na = market.get('no_ask')
            if ya is not None and na is not None:
                return ya, na

        # Fallback: fetch market metadata via API
        m = self.kalshi_api.get_market(ticker)
        if m:
            ya = m.get('yes_ask')
            na = m.get('no_ask')
            if ya is not None and na is not None:
                return ya, na

        return None, None

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

        kalshi_yes_ask, kalshi_no_ask = self._get_kalshi_best_asks(kalshi_ticker, market=kalshi_market)

        if None in (kalshi_yes_ask, kalshi_no_ask):
            logger.debug(
                "Skipping %s: incomplete Kalshi prices (yes=%s no=%s)",
                kalshi_ticker, kalshi_yes_ask, kalshi_no_ask,
            )
            return None

        # Skip extremely illiquid Kalshi markets (1-2c asks = no real depth)
        min_kalshi_price = 3
        if kalshi_yes_ask < min_kalshi_price and kalshi_no_ask < min_kalshi_price:
            logger.debug(
                "Skipping %s: Kalshi prices too low (yes=%s no=%s) - likely illiquid",
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
            f"Buy {kalshi_side.upper()}@Kalshi({kalshi_price}c)"
            f" + {manifold_side.upper()}@Manifold({manifold_price}c)"
        )

        # Compute days to settlement for tracking
        kalshi_close = self._parse_kalshi_close(kalshi_market)
        manifold_close_ms = manifold_market.get('closeTime')
        now_utc = datetime.now(timezone.utc)
        if kalshi_close:
            days_to_settle = max(0, (kalshi_close - now_utc).total_seconds() / 86400)
        elif manifold_close_ms:
            days_to_settle = max(0, (manifold_close_ms / 1000 - now_utc.timestamp()) / 86400)
        else:
            days_to_settle = -1

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
            'days_to_settle': round(days_to_settle, 1),
            'manifold_is_sweepstakes': self._is_sweepstakes(manifold_market),
            'manifold_liquidity': manifold_market.get('totalLiquidity', 0),
            'strategy': strategy,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def scan_opportunities(self, force_refresh: bool = False) -> List[Dict]:
        """Scan all matched pairs and return Kalshi<->Manifold arb opportunities."""
        if not Config.MANIFOLD_ENABLED:
            logger.info("Manifold scanning disabled (MANIFOLD_ENABLED=false)")
            return []

        pairs = self.match_markets(force_refresh=force_refresh)
        if not pairs:
            logger.info("No matched Kalshi<->Manifold market pairs found")
            return []

        logger.info("Scanning %d matched pairs for Kalshi<->Manifold arbitrage...", len(pairs))
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
                    "[OK] MANIFOLD ARB: %s <-> Manifold(%s) -- %sc (%.2f%%) settles in %.1fd",
                    opp['kalshi_ticker'], opp['manifold_id'],
                    opp['profit_cents'], opp['profit_percent'],
                    opp.get('days_to_settle', -1),
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

    # ------------------------------------------------------------------
    # Paper trade execution & tracking
    # ------------------------------------------------------------------

    PAPER_TRADES_CSV = 'manifold_paper_trades.csv'

    def _init_paper_csv(self) -> None:
        """Create the paper trades CSV if it doesn't exist."""
        import csv, os
        if os.path.exists(self.PAPER_TRADES_CSV):
            return
        with open(self.PAPER_TRADES_CSV, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'trade_id', 'timestamp', 'kalshi_ticker', 'manifold_id',
                'kalshi_title', 'manifold_title', 'match_confidence',
                'kalshi_side', 'kalshi_price', 'manifold_side', 'manifold_price',
                'total_cost', 'profit_cents', 'profit_percent', 'days_to_settle',
                'strategy', 'bet_size_usd', 'status', 'settled_profit',
            ])

    def execute_paper_trade(self, opportunity: Dict) -> Optional[Dict]:
        """Execute a paper trade for a cross-platform opportunity.

        Records both legs (Kalshi side + Manifold side) in a CSV for later
        performance tracking.  No real orders are placed.

        Returns the paper trade record or None on failure.
        """
        import csv, uuid

        self._init_paper_csv()

        bet_size = min(Config.MANIFOLD_MAX_BET_USD, Config.MAX_TRADE_USD)
        trade_id = str(uuid.uuid4())[:8]

        record = {
            'trade_id': trade_id,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'kalshi_ticker': opportunity['kalshi_ticker'],
            'manifold_id': opportunity['manifold_id'],
            'kalshi_title': opportunity.get('kalshi_title', ''),
            'manifold_title': opportunity.get('manifold_title', ''),
            'match_confidence': opportunity['match_confidence'],
            'kalshi_side': opportunity['kalshi_side'],
            'kalshi_price': opportunity['kalshi_price'],
            'manifold_side': opportunity['manifold_side'],
            'manifold_price': opportunity['manifold_price'],
            'total_cost': opportunity['total_cost'],
            'profit_cents': opportunity['profit_cents'],
            'profit_percent': opportunity['profit_percent'],
            'days_to_settle': opportunity.get('days_to_settle', -1),
            'strategy': opportunity['strategy'],
            'bet_size_usd': bet_size,
            'status': 'open',
            'settled_profit': '',
        }

        # Log the paper trade
        logger.info(
            "PAPER TRADE [%s]: %s | %s | bet=$%.2f | expected profit=%.2f%%",
            trade_id, opportunity['strategy'],
            opportunity['kalshi_title'][:50], bet_size, opportunity['profit_percent'],
        )

        # Also simulate the Manifold bet via the API (uses paper mode)
        self.manifold_api.place_bet(
            market_id=opportunity['manifold_id'],
            amount=bet_size,
            outcome=opportunity['manifold_side'].upper(),
        )

        # Write to CSV
        try:
            with open(self.PAPER_TRADES_CSV, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([record[k] for k in [
                    'trade_id', 'timestamp', 'kalshi_ticker', 'manifold_id',
                    'kalshi_title', 'manifold_title', 'match_confidence',
                    'kalshi_side', 'kalshi_price', 'manifold_side', 'manifold_price',
                    'total_cost', 'profit_cents', 'profit_percent', 'days_to_settle',
                    'strategy', 'bet_size_usd', 'status', 'settled_profit',
                ]])
        except Exception as exc:
            logger.warning("Failed to write paper trade CSV: %s", exc)

        self._paper_trades.append(record)
        return record

    def get_paper_summary(self) -> Dict:
        """Return summary stats for paper trades in this session."""
        total = len(self._paper_trades)
        if total == 0:
            return {'total': 0, 'avg_profit_pct': 0, 'total_bet': 0}
        avg_profit = sum(t['profit_percent'] for t in self._paper_trades) / total
        total_bet = sum(t['bet_size_usd'] for t in self._paper_trades)
        return {
            'total': total,
            'avg_profit_pct': round(avg_profit, 2),
            'total_bet': round(total_bet, 2),
        }

    def scan_and_trade(self, force_refresh: bool = False) -> List[Dict]:
        """Scan for opportunities and execute paper trades on any found.

        This is the main entry point for the continuous paper trading loop.
        """
        opportunities = self.scan_opportunities(force_refresh=force_refresh)
        trades = []
        for opp in opportunities:
            trade = self.execute_paper_trade(opp)
            if trade:
                trades.append(trade)
        return trades

    def scan_continuous(self, interval: Optional[int] = None) -> None:
        """Continuously scan for Kalshi <-> Manifold arbitrage opportunities.

        When PAPER_TRADING is True, automatically executes paper trades for
        every opportunity found and tracks results in a CSV.

        Args:
            interval: Seconds to wait between scans. Defaults to Config.MANIFOLD_SCAN_INTERVAL.
        """
        scan_interval = interval if interval is not None else Config.MANIFOLD_SCAN_INTERVAL
        paper_mode = Config.PAPER_TRADING
        logger.info(
            "Starting continuous Kalshi <-> Manifold scanner (interval=%ds, paper=%s)",
            scan_interval, paper_mode,
        )
        iteration = 0
        try:
            while True:
                iteration += 1
                print(f"\n{'='*60}")
                print(f"Manifold Scan #{iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                if paper_mode:
                    print(f"  Mode: PAPER TRADING")
                print(f"{'='*60}")

                if paper_mode:
                    trades = self.scan_and_trade(force_refresh=True)
                    if trades:
                        print(f"\nPAPER TRADED {len(trades)} opportunities:")
                        for t in trades:
                            days = t.get('days_to_settle', -1)
                        days_str = f"{days:.0f}d" if days >= 0 else "?"
                        print(
                                f"  [{t['trade_id']}] {t['strategy']} | "
                                f"cost={t['total_cost']}c profit={t['profit_cents']}c "
                                f"({t['profit_percent']:.2f}%) settle={days_str} bet=${t['bet_size_usd']:.2f}"
                            )
                        summary = self.get_paper_summary()
                        print(f"\n  Session totals: {summary['total']} trades, "
                              f"avg profit {summary['avg_profit_pct']:.2f}%, "
                              f"total bet ${summary['total_bet']:.2f}")
                    else:
                        print("\nNo opportunities found this scan")
                else:
                    opportunities = self.scan_opportunities(force_refresh=True)
                    if opportunities:
                        print(f"\nFound {len(opportunities)} Kalshi <-> Manifold opportunities:")
                        for opp in opportunities:
                            sweeps = '(Sweepcash)' if opp['manifold_is_sweepstakes'] else '(Mana)'
                            print(
                                f"  {opp['kalshi_ticker']} <-> Manifold {sweeps} | "
                                f"{opp['strategy']} | "
                                f"Profit: {opp['profit_cents']}c ({opp['profit_percent']:.2f}%)"
                            )
                    else:
                        print("\nNo Kalshi <-> Manifold arbitrage opportunities found this scan")

                print(f"\nWaiting {scan_interval}s until next scan...")
                time.sleep(scan_interval)

        except KeyboardInterrupt:
            print(f"\n\n{'='*60}")
            print("Manifold scanner stopped by user")
            print(f"Total scans: {iteration}")
            if paper_mode:
                summary = self.get_paper_summary()
                print(f"Paper trades: {summary['total']}, "
                      f"avg profit: {summary['avg_profit_pct']:.2f}%, "
                      f"total bet: ${summary['total_bet']:.2f}")
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
        if Config.PAPER_TRADING:
            trades = scanner.scan_and_trade(force_refresh=True)
            if not trades:
                print('\nNo Kalshi <-> Manifold arbitrage opportunities found.')
            else:
                print(f'\nPaper traded {len(trades)} opportunit{"y" if len(trades)==1 else "ies"}:')
                for t in trades:
                    print(
                        f"  [{t['trade_id']}] {t['strategy']} | "
                        f"cost={t['total_cost']}c profit={t['profit_cents']}c ({t['profit_percent']:.2f}%)"
                    )
        else:
            opps = scanner.scan_opportunities(force_refresh=True)
            if not opps:
                print('\nNo Kalshi <-> Manifold arbitrage opportunities found.')
            else:
                print(f'\nFound {len(opps)} opportunit{"y" if len(opps)==1 else "ies"}:')
                for o in opps:
                    print(
                        f"  {o['kalshi_ticker']} <-> Manifold({o['manifold_id']}) | "
                        f"{o['strategy']} | profit={o['profit_cents']}c ({o['profit_percent']:.2f}%)"
                    )
    else:
        scanner.scan_continuous(interval=args.interval)
