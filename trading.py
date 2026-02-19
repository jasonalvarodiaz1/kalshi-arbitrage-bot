import logging
import time
from typing import Dict, List, Optional
from datetime import datetime, timezone
from config import Config
from notifications import NotificationManager
from kelly import size_position
from api import KalshiAPI

logger = logging.getLogger('kalshi_bot')

class KalshiTradingBot:
    """Paper trading bot for Kalshi (simulation mode)"""
    
    def __init__(self, api: KalshiAPI, initial_balance: float = 1000.0, 
                 paper_trading: bool = True, storage=None):
        self.api = api
        self.paper_trading = paper_trading
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.positions = []
        self.trade_history = []
        self.notifier = NotificationManager()
        self.storage = storage  # Optional storage backend
    
    def _wait_for_fill(self, order_id: str, expected_qty: int, timeout: int = None) -> Dict:
        """Poll order status until filled or timeout, then cancel remainder.

        Args:
            order_id: Exchange order ID to poll
            expected_qty: Number of contracts we intended to fill
            timeout: Seconds to wait before cancelling (default: Config.FILL_WAIT_TIMEOUT_SECONDS)

        Returns:
            Dict with 'filled_qty', 'unfilled_qty', 'status', 'cancelled'
        """
        if timeout is None:
            timeout = Config.FILL_WAIT_TIMEOUT_SECONDS

        poll_interval = 2  # seconds between polls
        elapsed = 0
        filled_qty = 0
        order_status = 'unknown'

        while elapsed < timeout:
            order_info = self.api.get_order(order_id)
            if order_info is None:
                pass  # No response yet, keep polling
            elif not isinstance(order_info, dict):
                # Non-standard response — treat as filled to avoid false cancellations
                logger.debug("Order %s: unexpected get_order response type, assuming filled", order_id)
                return {
                    'filled_qty': expected_qty,
                    'unfilled_qty': 0,
                    'status': 'filled',
                    'cancelled': False,
                }
            else:
                # Kalshi order response may be nested under 'order'
                order_data = order_info.get('order', order_info)
                if not isinstance(order_data, dict):
                    order_data = {}
                order_status = order_data.get('status', 'unknown')
                raw_filled = order_data.get('filled_count', 0) or order_data.get('quantity_filled', 0) or 0
                filled_qty = int(raw_filled) if isinstance(raw_filled, (int, float)) else 0

                logger.debug("Order %s: status=%s filled=%d/%d elapsed=%ds",
                             order_id, order_status, filled_qty, expected_qty, elapsed)

                if order_status in ('filled', 'closed') or filled_qty >= expected_qty:
                    logger.info("Order %s fully filled: %d/%d contracts", order_id, filled_qty, expected_qty)
                    return {
                        'filled_qty': filled_qty,
                        'unfilled_qty': max(0, expected_qty - filled_qty),
                        'status': order_status,
                        'cancelled': False,
                    }

            time.sleep(poll_interval)
            elapsed += poll_interval

        # Timeout reached — cancel unfilled remainder
        unfilled_qty = max(0, expected_qty - filled_qty)
        logger.warning("Order %s timed out after %ds: filled=%d/%d. Cancelling remainder...",
                       order_id, timeout, filled_qty, expected_qty)
        cancelled = False
        if unfilled_qty > 0:
            cancelled = bool(self.api.cancel_order(order_id))
            if cancelled:
                logger.info("Cancelled unfilled remainder of order %s (%d contracts)", order_id, unfilled_qty)
            else:
                logger.error("Failed to cancel order %s! %d unfilled contracts remain.", order_id, unfilled_qty)

        return {
            'filled_qty': filled_qty,
            'unfilled_qty': unfilled_qty,
            'status': order_status,
            'cancelled': cancelled,
        }

    def execute_arbitrage(self, opportunity: Dict, quantity: int = None, use_kelly: bool = False):
        """
        Execute arbitrage trade (buy both YES and NO).
        
        Args:
            opportunity: Opportunity dict with ticker, yes_price, no_price, etc.
            quantity: Number of contracts (if None and use_kelly=True, calculates from Kelly)
            use_kelly: Whether to use Kelly criterion for position sizing
        """ 
        
        ticker = opportunity['ticker']
        yes_price = opportunity['yes_price']
        no_price = opportunity['no_price']
        
        # Calculate quantity using Kelly if requested and not provided
        if quantity is None and use_kelly:
            # For arbitrage, win probability is effectively 1.0 (guaranteed profit)
            win_prob = 0.99  # Nearly certain
            avg_price = (yes_price + no_price) / 2
            quantity = size_position(
                bankroll=self.balance,
                win_prob=win_prob,
                contract_price_cents=int(avg_price),
                max_trade_usd=Config.MAX_TRADE_USD,
                kelly_multiplier=Config.KELLY_MULTIPLIER
            )
            logger.info("Kelly sizing calculated quantity: %d contracts", quantity)
        
        # Default to 1 if still None
        if quantity is None or quantity < 1:
            quantity = 1
        
        total_cost = (yes_price + no_price) * quantity / 100
        
        print(f"\n{'='*60}")
        print(f"🤖 EXECUTING ARBITRAGE")
        print(f"{'='*60}")
        print(f"Market: {opportunity['title']}")
        print(f"Quantity: {quantity} contracts")
        print(f"YES price: ${yes_price/100:.2f} x {quantity} = ${yes_price * quantity / 100:.2f}")
        print(f"NO price: ${no_price/100:.2f} x {quantity} = ${no_price * quantity / 100:.2f}")
        print(f"Total cost: ${total_cost:.2f}")
        print(f"Expected payout: ${quantity:.2f}")
        print(f"Expected profit: ${quantity - total_cost:.2f}")
        
        if total_cost > self.balance:
            print(f"❌ Insufficient balance (need ${total_cost:.2f}, have ${self.balance:.2f})")
            return False

        if self.paper_trading:
            print(f"\n📄 PAPER TRADE MODE - No real orders placed")

            trade = {
                'ticker': ticker,
                'type': 'arbitrage',
                'quantity': quantity,
                'yes_price': yes_price,
                'no_price': no_price,
                'cost': total_cost,
                'expected_profit': quantity - total_cost,
                'timestamp': datetime.now().isoformat(),
                'paper_trading': True
            }

            self.trade_history.append(trade)
            self.balance -= total_cost
            self.positions.append(trade)

            print(f"✅ Paper trade recorded")
            print(f"Remaining balance: ${self.balance:.2f}")
            self.notifier.notify_trade_executed(trade)
            
            # Save to storage if available
            if self.storage:
                self.storage.save_trade(trade)

        else:
            # Live trading path with safeguards
            if not Config.LIVE_TRADING_ENABLED:
                print("\n⚠️  LIVE TRADING DISABLED - enable by setting ENABLE_LIVE_TRADING=true in .env")
                return False
            
            # Verify live balance before trading
            live_balance = self.api.get_balance()
            if live_balance < total_cost:
                print(f"❌ Insufficient live balance (need ${total_cost:.2f}, have ${live_balance:.2f} on exchange)")
                return False
            
            # Check position limits
            if len(self.positions) >= Config.MAX_POSITIONS:
                print(f"⚠️ Position limit reached ({Config.MAX_POSITIONS} positions). Refusing new trade.")
                return False

            total_exposure = sum(t.get('cost', 0) for t in self.positions)
            if total_exposure + total_cost > Config.MAX_EXPOSURE_USD:
                print(f"⚠️ Exposure limit would be exceeded (current: ${total_exposure:.2f}, new: ${total_cost:.2f}, max: ${Config.MAX_EXPOSURE_USD:.2f})")
                return False

            if total_cost > Config.MAX_TRADE_USD:
                print(f"\n⚠️  Trade exceeds configured max (${Config.MAX_TRADE_USD:.2f}). Refusing to place live orders.")
                return False

            print(f"\n🚀 LIVE TRADING MODE - AUTO-EXECUTING ARBITRAGE")
            print(f"Market: {opportunity['title']}")
            print(f"YES price: ${yes_price/100:.2f} x {quantity}")
            print(f"NO price: ${no_price/100:.2f} x {quantity}")
            print(f"Total cost: ${total_cost:.2f}")
            print(f"Expected profit: ${quantity - total_cost:.2f}")
            
            # Stale price check: re-fetch orderbook and verify arbitrage is still valid
            logger.info("Re-fetching orderbook for %s before placing orders...", ticker)
            fresh_orderbook = self.api.get_orderbook(ticker)
            if fresh_orderbook and isinstance(fresh_orderbook, dict):
                fresh_yes_ask = None
                fresh_no_ask = None
                yes_asks = fresh_orderbook.get('yes', [])
                no_asks = fresh_orderbook.get('no', [])
                if yes_asks:
                    fresh_yes_ask = min(a[0] for a in yes_asks if len(a) >= 2)
                if no_asks:
                    fresh_no_ask = min(a[0] for a in no_asks if len(a) >= 2)

                if fresh_yes_ask is not None and fresh_no_ask is not None:
                    if fresh_yes_ask + fresh_no_ask >= 100:
                        logger.warning(
                            "Stale price on %s: original %d+%d=%.0f¢, now %d+%d=%d¢ — no longer profitable. Aborting.",
                            ticker, yes_price, no_price, yes_price + no_price,
                            fresh_yes_ask, fresh_no_ask, fresh_yes_ask + fresh_no_ask
                        )
                        print(f"⚠️ Prices moved — arbitrage no longer profitable. Aborting trade.")
                        return False
                    # Use freshest prices
                    yes_price = fresh_yes_ask
                    no_price = fresh_no_ask
                    logger.info("Using fresh prices: YES=%d¢ NO=%d¢", yes_price, no_price)

            # Place YES order first
            logger.info("Placing YES order: %s qty=%d price=%d¢", ticker, quantity, yes_price)
            yes_order = self.api.place_order(ticker, 'yes', quantity, yes_price, order_type='limit')

            if not yes_order:
                logger.error("YES order failed for %s. No orders placed.", ticker)
                return False

            yes_order_id = yes_order.get('order', {}).get('order_id') or yes_order.get('order_id')
            if not yes_order_id:
                logger.warning("YES order succeeded but order_id not found in response. Cannot poll fill status.")

            # Wait for YES order to fill
            yes_fill = {'filled_qty': quantity, 'unfilled_qty': 0, 'status': 'filled', 'cancelled': False}
            if yes_order_id:
                yes_fill = self._wait_for_fill(yes_order_id, quantity)

            # Place NO order
            logger.info("Placing NO order: %s qty=%d price=%d¢", ticker, quantity, no_price)
            no_order = self.api.place_order(ticker, 'no', quantity, no_price, order_type='limit')

            if not no_order:
                # CRITICAL: YES succeeded but NO failed — cancel YES to prevent naked exposure
                logger.critical("NO order failed for %s! Cancelling YES order %s to prevent naked exposure...", ticker, yes_order_id)
                if yes_order_id and self.api.cancel_order(yes_order_id):
                    logger.info("YES order %s cancelled successfully. No exposure.", yes_order_id)
                else:
                    logger.critical("FAILED to cancel YES order %s! MANUAL INTERVENTION REQUIRED. Check positions immediately.", yes_order_id)
                return False

            no_order_id = no_order.get('order', {}).get('order_id') or no_order.get('order_id')

            # Wait for NO order to fill
            no_fill = {'filled_qty': quantity, 'unfilled_qty': 0, 'status': 'filled', 'cancelled': False}
            if no_order_id:
                no_fill = self._wait_for_fill(no_order_id, quantity)

            yes_filled = yes_fill['filled_qty']
            no_filled = no_fill['filled_qty']

            if yes_filled != no_filled:
                logger.critical(
                    "FILL IMBALANCE on %s: YES filled=%d, NO filled=%d — %d naked contracts! "
                    "Check positions immediately.",
                    ticker, yes_filled, no_filled, abs(yes_filled - no_filled)
                )

            # Use actual filled quantities for position tracking
            quantity = min(yes_filled, no_filled)
            if quantity == 0:
                logger.error("No contracts filled for %s. Trade aborted.", ticker)
                return False

            total_cost = (yes_price + no_price) * quantity / 100

            logger.info("✅ Both orders filled for %s: YES=%d NO=%d contracts", ticker, yes_filled, no_filled)
            print(f"✅ Both orders filled: YES={yes_filled} NO={no_filled} contracts")

            trade = {
                'ticker': ticker,
                'type': 'arbitrage',
                'quantity': quantity,
                'yes_price': yes_price,
                'no_price': no_price,
                'cost': total_cost,
                'expected_profit': quantity - total_cost,
                'timestamp': datetime.now().isoformat(),
                'yes_order': yes_order,
                'no_order': no_order,
                'paper_trading': False
            }

            self.trade_history.append(trade)
            self.balance -= total_cost
            self.positions.append(trade)
            print(f"Remaining balance: ${self.balance:.2f}")
            self.notifier.notify_trade_executed(trade)
            
            # Save to storage if available
            if self.storage:
                self.storage.save_trade(trade)

        return True
    
    def get_stats(self) -> Dict:
        """Get trading statistics"""
        total_expected_profit = sum([t.get('expected_profit', 0) for t in self.positions])
        realized_profit = sum(t.get('realized_profit', 0) for t in self.trade_history if t.get('realized_profit') is not None)
        
        return {
            'paper_trading': self.paper_trading,
            'initial_balance': self.initial_balance,
            'current_balance': self.balance,
            'total_trades': len(self.trade_history),
            'open_positions': len(self.positions),
            'total_invested': self.initial_balance - self.balance,
            'expected_profit': total_expected_profit,
            'expected_roi_percent': (total_expected_profit / (self.initial_balance - self.balance) * 100) if self.balance != self.initial_balance else 0,
            'realized_profit': realized_profit
        }
    
    def print_stats(self):
        """Print formatted statistics"""
        stats = self.get_stats()
        
        print(f"\n{'='*60}")
        print(f"📊 TRADING STATISTICS")
        print(f"{'='*60}")
        print(f"Mode: {'PAPER TRADING' if stats['paper_trading'] else 'LIVE TRADING'}")
        print(f"Initial balance: ${stats['initial_balance']:.2f}")
        print(f"Current balance: ${stats['current_balance']:.2f}")
        print(f"Total invested: ${stats['total_invested']:.2f}")
        print(f"Total trades: {stats['total_trades']}")
        print(f"Open positions: {stats['open_positions']}")
        print(f"Expected profit: ${stats['expected_profit']:.2f}")
        print(f"Expected ROI: {stats['expected_roi_percent']:.2f}%")
        print(f"Realized profit: ${stats['realized_profit']:.2f}")
        print(f"{'='*60}")
    
    def reconcile_positions(self):
        """Check settled positions and calculate realized P&L."""
        if not self.positions:
            logger.debug("No open positions to reconcile")
            return []

        settled = []
        for position in self.positions:
            ticker = position.get('ticker')
            if not ticker:
                continue
                
            market = self.api.get_market(ticker)
            if market and market.get('status') in ('settled', 'closed'):
                result = market.get('result', '')
                # Each arb position bought both YES and NO, so one of them pays $1
                payout = position['quantity']  # $1 per contract pair
                profit = payout - position['cost']
                position['realized_profit'] = profit
                position['settlement_result'] = result
                position['settled_at'] = datetime.now().isoformat()
                settled.append(position)
                logger.info("Position settled: %s (%s) - P&L: $%.2f", ticker, result, profit)

        for s in settled:
            self.positions.remove(s)
            self.balance += s['quantity']  # Add payout back
            self.trade_history.append({**s, 'status': 'settled'})
            
            # Update storage if available
            if self.storage:
                # Save updated settled trade
                self.storage.save_trade({**s, 'status': 'settled'})

        if settled:
            total_realized = sum(s.get('realized_profit', 0) for s in settled)
            logger.info("Settled %d positions, total realized P&L: $%.2f", len(settled), total_realized)
        
        return settled
    
    def _capital_recycle(self):
        """
        Automatic capital recycling - check for settled positions and recycle capital.
        
        This method:
        1. Calls reconcile_positions() to check for settled positions
        2. Logs settled positions and realized P&L
        3. Updates balance (already done in reconcile_positions)
        4. Makes freed capital immediately available for next trade
        
        Returns:
            Number of positions that were settled and recycled
        """
        logger.debug("Running capital recycle...")
        settled = self.reconcile_positions()
        
        if settled:
            total_recycled = sum(s.get('quantity', 0) for s in settled)
            logger.info("Capital recycled: $%.2f now available for trading", total_recycled)
        
        return len(settled)
    
    def reconcile_with_exchange(self):
        """Compare local positions with exchange positions for consistency."""
        print("🔄 Reconciling with exchange...")

        exchange_positions = self.api.get_positions()
        local_tickers = set(p['ticker'] for p in self.positions)
        exchange_tickers = set(p.get('ticker') for p in exchange_positions)

        # Find discrepancies
        missing_on_exchange = local_tickers - exchange_tickers
        extra_on_exchange = exchange_tickers - local_tickers

        if missing_on_exchange:
            print(f"⚠️ Positions tracked locally but not on exchange: {missing_on_exchange}")
        if extra_on_exchange:
            print(f"⚠️ Positions on exchange but not tracked locally: {extra_on_exchange}")
        if not missing_on_exchange and not extra_on_exchange:
            print("✅ Local and exchange positions match")

        return {
            'local_count': len(local_tickers),
            'exchange_count': len(exchange_tickers),
            'missing_on_exchange': list(missing_on_exchange),
            'extra_on_exchange': list(extra_on_exchange)
        }


