import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from config import Config
from polymarket_api import PolymarketAPI
from notifications import NotificationManager

logger = logging.getLogger('kalshi_bot')

# Expanded sports keywords for filtering (superset of cross_platform_arb.py list)
SPORTS_KEYWORDS = [
    'nfl', 'nba', 'mlb', 'nhl', 'mls', 'ufc', 'boxing', 'tennis', 'golf',
    'soccer', 'football', 'basketball', 'baseball', 'hockey',
    'premier league', 'champions league', 'ncaa',
    'super bowl', 'world series', 'championship', 'match', 'game',
    'win', 'score', 'team', 'playoff', 'tournament',
]

# After Polymarket's 2% fee on winning payouts, a $1 payout nets 98¢
POLY_PAYOUT_CENTS = 98


class PolymarketSportsArbitrage:
    """Scans Polymarket sports markets for same-event multi-outcome arbitrage.

    For each binary sports market (exactly 2 outcome tokens A and B):

    - **YES arb**: ask(A) + ask(B) < 98¢  →  buy YES on both sides.
      Exactly one outcome pays $1 (98¢ after 2% fee), guaranteed profit.
    - **NO arb**: no_ask(A) + no_ask(B) < 98¢  →  buy NO on both sides.
      no_ask(token) = 1 - best_bid(token) (synthetic NO via selling YES).

    This mirrors the strategy used by a Polymarket sports bot that earned
    $619K/year running same-event multi-outcome arb on binary sports markets.
    """

    def __init__(self, poly_api=None, min_profit_percent=None, storage=None):
        self.poly_api = poly_api or PolymarketAPI(api_key=Config.POLYMARKET_API_KEY)
        self.min_profit_percent = (
            min_profit_percent
            if min_profit_percent is not None
            else Config.POLYMARKET_SPORTS_MIN_PROFIT_PERCENT
        )
        self.notifier = NotificationManager()
        self.storage = storage

    # ------------------------------------------------------------------
    # Market filtering
    # ------------------------------------------------------------------

    def _is_sports_market(self, market: Dict) -> bool:
        """Check if a market is a sports market using tags or title keywords."""
        # Check structured tags field first
        tags = market.get('tags') or []
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, dict):
                    tag_str = (tag.get('slug', '') or tag.get('label', '') or '').lower()
                else:
                    tag_str = str(tag).lower()
                if any(kw in tag_str for kw in SPORTS_KEYWORDS):
                    return True

        # Fall back to title/question keyword matching
        title = (market.get('question', '') or market.get('title', '') or '').lower()
        return any(kw in title for kw in SPORTS_KEYWORDS)

    def _is_binary_market(self, market: Dict) -> bool:
        """Check if a market has exactly 2 outcome tokens."""
        return len(market.get('tokens', [])) == 2

    def _get_binary_tokens(self, market: Dict) -> Optional[Tuple[Dict, Dict]]:
        """Extract the two outcome tokens from a binary market.

        Returns ``(token_a, token_b)`` or ``None`` if the market isn't binary.
        """
        tokens = market.get('tokens', [])
        if len(tokens) != 2:
            return None
        return tokens[0], tokens[1]

    # ------------------------------------------------------------------
    # Price helpers
    # ------------------------------------------------------------------

    def _get_best_asks_cents(self, token_id: str) -> Optional[Tuple[int, int]]:
        """Get best YES ask and best NO ask in cents for a token.

        YES ask = lowest ask price in the orderbook (converted to cents).
        NO ask  = ``(1 - best_bid) * 100`` — cost of a synthetic NO position
                  obtained by selling YES at the market bid.

        Returns ``(yes_ask_cents, no_ask_cents)`` or ``None`` on failure.
        """
        ob = self.poly_api.get_orderbook(token_id)
        if not ob:
            return None

        asks = ob.get('asks', [])
        bids = ob.get('bids', [])

        if not asks:
            return None

        try:
            best_ask = min(float(a['price']) for a in asks)
        except (KeyError, ValueError, TypeError):
            return None

        yes_ask_cents = PolymarketAPI.to_cents(best_ask)

        # Derive NO ask from best bid; fall back to complement of YES ask
        if bids:
            try:
                best_bid = max(float(b['price']) for b in bids)
                no_ask_cents = PolymarketAPI.to_cents(1.0 - best_bid)
            except (KeyError, ValueError, TypeError):
                no_ask_cents = 100 - yes_ask_cents
        else:
            no_ask_cents = 100 - yes_ask_cents

        return yes_ask_cents, no_ask_cents

    # ------------------------------------------------------------------
    # Orderbook depth
    # ------------------------------------------------------------------

    def _walk_poly_orderbook(self, asks: List[Dict], target_qty: int) -> Optional[Dict]:
        """Walk a Polymarket orderbook to get VWAP for ``target_qty`` contracts.

        Similar to ``arbitrage.py``'s ``_walk_orderbook`` but works with
        Polymarket's ``{'price': str, 'size': str}`` entry format.

        Returns a dict with ``avg_price``, ``filled_qty``, ``fully_filled``, etc.,
        or ``None`` if there is no liquidity.
        """
        if not asks:
            return None

        try:
            sorted_asks = sorted(asks, key=lambda a: float(a['price']))
        except (KeyError, TypeError, ValueError):
            return None

        filled = 0.0
        total_cost = 0.0
        levels_used = 0

        for entry in sorted_asks:
            try:
                price = float(entry['price'])
                qty = float(entry['size'])
            except (KeyError, TypeError, ValueError):
                continue

            can_fill = min(qty, target_qty - filled)
            total_cost += price * can_fill
            filled += can_fill
            levels_used += 1

            if filled >= target_qty:
                break

        if filled == 0:
            return None

        return {
            'avg_price': total_cost / filled,
            'total_cost': total_cost,
            'filled_qty': filled,
            'levels_used': levels_used,
            'fully_filled': filled >= target_qty,
        }

    # ------------------------------------------------------------------
    # Arb detection
    # ------------------------------------------------------------------

    def _check_yes_arb(
        self,
        market: Dict,
        ob_a: Optional[Dict] = None,
        ob_b: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Check if YES(A) + YES(B) < 98¢ for a binary sports market.

        ``ob_a`` and ``ob_b`` are pre-fetched orderbooks for tokens A and B.
        If not provided, they are fetched via the Polymarket API.
        """
        tokens_pair = self._get_binary_tokens(market)
        if not tokens_pair:
            return None

        token_a, token_b = tokens_pair
        token_a_id = token_a.get('token_id')
        token_b_id = token_b.get('token_id')

        if not token_a_id or not token_b_id:
            return None

        if ob_a is None:
            ob_a = self.poly_api.get_orderbook(token_a_id)
            time.sleep(Config.RATE_LIMIT_DELAY)
        if ob_b is None:
            ob_b = self.poly_api.get_orderbook(token_b_id)

        if not ob_a or not ob_b:
            return None

        asks_a = ob_a.get('asks', [])
        asks_b = ob_b.get('asks', [])

        if not asks_a or not asks_b:
            return None

        try:
            best_ask_a = min(float(a['price']) for a in asks_a)
            best_ask_b = min(float(b['price']) for b in asks_b)
        except (KeyError, ValueError, TypeError):
            return None

        ask_a_cents = PolymarketAPI.to_cents(best_ask_a)
        ask_b_cents = PolymarketAPI.to_cents(best_ask_b)
        total = ask_a_cents + ask_b_cents

        effective_payout = POLY_PAYOUT_CENTS
        profit = effective_payout - total

        if total <= 0 or profit <= 0:
            return None

        profit_percent = (profit / total) * 100

        if profit_percent < self.min_profit_percent:
            return None

        # Orderbook depth check — ensure MIN_ORDER_QUANTITY can be filled
        min_qty = Config.MIN_ORDER_QUANTITY
        walk_a = self._walk_poly_orderbook(asks_a, min_qty)
        walk_b = self._walk_poly_orderbook(asks_b, min_qty)

        if not walk_a or not walk_b:
            return None

        max_exec_qty = int(min(
            sum(float(a.get('size', 0)) for a in asks_a),
            sum(float(b.get('size', 0)) for b in asks_b),
        ))

        recommended_qty = min(
            max_exec_qty,
            int((Config.POLYMARKET_SPORTS_MAX_POSITION_USD * 100) / max(total, 1)),
        )

        outcome_a = token_a.get('outcome', 'Outcome A')
        outcome_b = token_b.get('outcome', 'Outcome B')
        market_title = market.get('question', '') or market.get('title', 'Unknown')

        return {
            'type': 'polymarket_sports_arb',
            'arb_side': 'yes',
            'condition_id': market.get('condition_id', ''),
            'market_title': market_title,
            'outcome_a': outcome_a,
            'outcome_b': outcome_b,
            'outcome_a_token_id': token_a_id,
            'outcome_b_token_id': token_b_id,
            'ask_a_cents': ask_a_cents,
            'ask_b_cents': ask_b_cents,
            'total_cost_cents': total,
            'effective_payout_cents': effective_payout,
            'profit_cents': profit,
            'profit_percent': round(profit_percent, 2),
            'max_executable_qty': max_exec_qty,
            'recommended_qty': max(1, recommended_qty),
            'strategy': (
                f"Buy YES({outcome_a})@{ask_a_cents}¢ + "
                f"YES({outcome_b})@{ask_b_cents}¢ = {total}¢ → payout {effective_payout}¢"
            ),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }

    def _check_no_arb(
        self,
        market: Dict,
        ob_a: Optional[Dict] = None,
        ob_b: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Check if NO(A) + NO(B) < 98¢ for a binary sports market.

        NO ask for a token is computed as ``1 - best_bid`` (synthetic NO
        obtained by selling YES at the highest available bid price).

        ``ob_a`` and ``ob_b`` are pre-fetched orderbooks for tokens A and B.
        If not provided, they are fetched via the Polymarket API.
        """
        tokens_pair = self._get_binary_tokens(market)
        if not tokens_pair:
            return None

        token_a, token_b = tokens_pair
        token_a_id = token_a.get('token_id')
        token_b_id = token_b.get('token_id')

        if not token_a_id or not token_b_id:
            return None

        if ob_a is None:
            ob_a = self.poly_api.get_orderbook(token_a_id)
            time.sleep(Config.RATE_LIMIT_DELAY)
        if ob_b is None:
            ob_b = self.poly_api.get_orderbook(token_b_id)

        if not ob_a or not ob_b:
            return None

        bids_a = ob_a.get('bids', [])
        bids_b = ob_b.get('bids', [])

        if not bids_a or not bids_b:
            return None

        try:
            best_bid_a = max(float(b['price']) for b in bids_a)
            best_bid_b = max(float(b['price']) for b in bids_b)
        except (KeyError, ValueError, TypeError):
            return None

        # NO ask = 1 - best_bid (cost of synthetic NO via selling YES)
        no_ask_a_cents = PolymarketAPI.to_cents(1.0 - best_bid_a)
        no_ask_b_cents = PolymarketAPI.to_cents(1.0 - best_bid_b)
        total = no_ask_a_cents + no_ask_b_cents

        effective_payout = POLY_PAYOUT_CENTS
        profit = effective_payout - total

        if total <= 0 or profit <= 0:
            return None

        profit_percent = (profit / total) * 100

        if profit_percent < self.min_profit_percent:
            return None

        # Depth check: convert bids to synthetic NO asks for walk calculation
        # (selling YES at bid = buying NO synthetically)
        min_qty = Config.MIN_ORDER_QUANTITY
        no_asks_a = [
            {'price': str(1.0 - float(b['price'])), 'size': b.get('size', '0')}
            for b in bids_a
            if 'price' in b
        ]
        no_asks_b = [
            {'price': str(1.0 - float(b['price'])), 'size': b.get('size', '0')}
            for b in bids_b
            if 'price' in b
        ]

        walk_a = self._walk_poly_orderbook(no_asks_a, min_qty)
        walk_b = self._walk_poly_orderbook(no_asks_b, min_qty)

        if not walk_a or not walk_b:
            return None

        max_exec_qty = int(min(
            sum(float(b.get('size', 0)) for b in bids_a),
            sum(float(b.get('size', 0)) for b in bids_b),
        ))

        recommended_qty = min(
            max_exec_qty,
            int((Config.POLYMARKET_SPORTS_MAX_POSITION_USD * 100) / max(total, 1)),
        )

        outcome_a = token_a.get('outcome', 'Outcome A')
        outcome_b = token_b.get('outcome', 'Outcome B')
        market_title = market.get('question', '') or market.get('title', 'Unknown')

        return {
            'type': 'polymarket_sports_arb',
            'arb_side': 'no',
            'condition_id': market.get('condition_id', ''),
            'market_title': market_title,
            'outcome_a': outcome_a,
            'outcome_b': outcome_b,
            'outcome_a_token_id': token_a_id,
            'outcome_b_token_id': token_b_id,
            'ask_a_cents': no_ask_a_cents,
            'ask_b_cents': no_ask_b_cents,
            'total_cost_cents': total,
            'effective_payout_cents': effective_payout,
            'profit_cents': profit,
            'profit_percent': round(profit_percent, 2),
            'max_executable_qty': max_exec_qty,
            'recommended_qty': max(1, recommended_qty),
            'strategy': (
                f"Buy NO({outcome_a})@{no_ask_a_cents}¢ + "
                f"NO({outcome_b})@{no_ask_b_cents}¢ = {total}¢ → payout {effective_payout}¢"
            ),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def scan_opportunities(self) -> List[Dict]:
        """Scan all Polymarket sports markets for arb opportunities."""
        logger.info("Fetching Polymarket markets for sports arb scan...")
        markets = self.poly_api.get_all_markets()
        logger.info("Polymarket: %d total markets fetched", len(markets))

        sports_markets = [
            m for m in markets
            if self._is_sports_market(m) and self._is_binary_market(m)
        ]
        logger.info(
            "Polymarket sports arb: %d binary sports markets to scan",
            len(sports_markets),
        )
        print(
            f"🏟️  Found {len(sports_markets)} binary sports markets to scan "
            f"(from {len(markets)} total)"
        )

        opportunities: List[Dict] = []

        for market in sports_markets:
            market_title = market.get('question', '') or market.get('title', 'Unknown')
            tokens_pair = self._get_binary_tokens(market)
            if not tokens_pair:
                continue

            token_a, token_b = tokens_pair
            token_a_id = token_a.get('token_id')
            token_b_id = token_b.get('token_id')

            if not token_a_id or not token_b_id:
                continue

            try:
                ob_a = self.poly_api.get_orderbook(token_a_id)
                time.sleep(Config.RATE_LIMIT_DELAY)
                ob_b = self.poly_api.get_orderbook(token_b_id)

                if not ob_a or not ob_b:
                    continue

                # YES arb check
                yes_opp = self._check_yes_arb(market, ob_a, ob_b)
                if yes_opp:
                    opportunities.append(yes_opp)
                    logger.info(
                        "✅ YES ARB: %s — %d¢ (%.2f%%)",
                        market_title, yes_opp['profit_cents'], yes_opp['profit_percent'],
                    )
                    self.notifier.notify_opportunity(yes_opp)
                    if self.storage:
                        try:
                            self.storage.save_opportunity(yes_opp)
                        except Exception as exc:
                            logger.warning("Failed to save opportunity: %s", exc)

                # NO arb check
                no_opp = self._check_no_arb(market, ob_a, ob_b)
                if no_opp:
                    opportunities.append(no_opp)
                    logger.info(
                        "✅ NO ARB: %s — %d¢ (%.2f%%)",
                        market_title, no_opp['profit_cents'], no_opp['profit_percent'],
                    )
                    self.notifier.notify_opportunity(no_opp)
                    if self.storage:
                        try:
                            self.storage.save_opportunity(no_opp)
                        except Exception as exc:
                            logger.warning("Failed to save opportunity: %s", exc)

            except Exception as exc:
                logger.error("Error scanning market %s: %s", market_title, exc)
                continue

            time.sleep(Config.RATE_LIMIT_DELAY)

        logger.info(
            "Polymarket sports arb scan complete: %d opportunities found across %d markets",
            len(opportunities), len(sports_markets),
        )
        return opportunities

    def scan_continuous(self, interval: int = 60) -> None:
        """Continuously scan for Polymarket sports arb opportunities.

        Args:
            interval: Seconds between scans (30–60s recommended per the $619K bot).
        """
        logger.info(
            "Starting continuous Polymarket sports arb scanner (interval=%ds)", interval
        )
        iteration = 0
        try:
            while True:
                iteration += 1
                print(f"\n{'='*60}")
                print(
                    f"⚽ Polymarket Sports Arb Scan #{iteration} - "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                print(f"{'='*60}")

                opportunities = self.scan_opportunities()

                if opportunities:
                    print(f"\n✅ Found {len(opportunities)} Polymarket sports arb opportunities:")
                    for opp in opportunities:
                        print(
                            f"  [{opp['arb_side'].upper()}] {opp['market_title']} | "
                            f"{opp['strategy']} | "
                            f"Profit: {opp['profit_cents']}¢ ({opp['profit_percent']:.2f}%)"
                        )
                else:
                    print("\n📊 No Polymarket sports arb opportunities found this scan")

                print(f"\n⏳ Waiting {interval}s until next scan...")
                time.sleep(interval)

        except KeyboardInterrupt:
            print(f"\n\n{'='*60}")
            print("🛑 Polymarket sports arb scanner stopped by user")
            print(f"Total scans: {iteration}")
            print(f"{'='*60}")
            logger.info(
                "Polymarket sports arb scanner stopped after %d iterations", iteration
            )
