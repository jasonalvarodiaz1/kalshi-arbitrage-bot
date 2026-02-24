import logging
import time
from datetime import datetime
from config import Config
from api import KalshiAPI
from arbitrage import KalshiArbitrageBot
from trading import KalshiTradingBot

logger = logging.getLogger('kalshi_bot')

def main():
    """Main function""" 
    
    print(f"\n{'='*60}")
    print(f"🤖 KALSHI ARBITRAGE BOT - Educational Version")
    print(f"{'='*60}\n")
    
    # Load configuration from config.py/.env
    try:
        Config.validate()
    except Exception as e:
        print(f"Configuration error: {e}")

    Config.print_config()

    api = KalshiAPI(email=Config.KALSHI_EMAIL, password=Config.KALSHI_PASSWORD, api_key=Config.KALSHI_API_KEY)
    
    # Initialize optional storage backend
    storage = None
    try:
        from storage import Storage
        storage = Storage("kalshi_bot.db")
        logger.info("Storage enabled - trades and opportunities will be persisted")
    except Exception as e:
        logger.warning("Storage not available: %s", e)
        print("⚠️  Note: Storage disabled - trades will not be persisted to database")
    
    bot = KalshiArbitrageBot(api, min_profit_percent=Config.MIN_PROFIT_PERCENT, storage=storage)
    
    print("Choose mode:")
    print("1. Scan all markets once (concurrent)")
    print("2. Scan specific category (e.g., BTC, NASDAQ)")
    print("3. Monitor specific tickers continuously")
    print("4. Demo paper trading")
    print("5. View historical stats")
    print("6. Reconcile settled positions & view P&L")
    print("7. Reconcile positions with exchange")
    print("8. Continuous auto-trading scanner 🤖 (finds & executes arbitrage)")
    print("9. Scan crypto markets for probability edge (BTC/ETH interval markets)")
    print("10. Auto-trade crypto probability strategy (continuous loop)")
    print("11. Scan for cross-platform arbitrage (Kalshi ↔ Polymarket)")
    print("12. Continuous cross-platform arbitrage scanner")
    
    choice = input("\nEnter choice (1-12): ").strip()
    
    if choice == "1":
        opportunities = bot.scan_all_markets_concurrent()
        print(f"\n✅ Scan complete: Found {len(opportunities)} opportunities")
        
    elif choice == "2":
        category = input("Enter category keyword (BTC, NASDAQ, etc.): ").strip()
        opportunities = bot.scan_all_markets_concurrent(category_filter=category)
        print(f"\n✅ Scan complete: Found {len(opportunities)} opportunities")
        
    elif choice == "3":
        print("\nExample tickers: KXBTC-23DEC31-T50000, INX-23DEC-T4500")
        tickers_input = input("Enter tickers (comma-separated): ").strip()
        tickers = [t.strip() for t in tickers_input.split(",")]
        bot.monitor_specific_markets(tickers, interval=60)
        
    elif choice == "4":
        trader = KalshiTradingBot(api, initial_balance=1000.0, paper_trading=True, storage=storage)
        
        demo_opportunity = {
            'ticker': 'DEMO-TICKER',
            'title': 'Demo Market for Testing',
            'yes_price': 48,
            'no_price': 50,
            'total_cost': 98,
            'profit_cents': 2,
            'profit_percent': 2.04
        }
        
        trader.execute_arbitrage(demo_opportunity, quantity=10)
        trader.print_stats()
    
    elif choice == "5":
        trader = KalshiTradingBot(api, initial_balance=1000.0, paper_trading=True, storage=storage)
        trader.print_stats()
    
    elif choice == "6":
        trader = KalshiTradingBot(api, initial_balance=1000.0, paper_trading=True, storage=storage)
        trader.reconcile_positions()
    
    elif choice == "7":
        trader = KalshiTradingBot(api, initial_balance=1000.0, paper_trading=True, storage=storage)
        trader.reconcile_with_exchange()
    
    elif choice == "8":
        # Continuous auto-trading scanner
        print("\n" + "="*60)
        print("🤖 CONTINUOUS AUTO-TRADING SCANNER")
        print("="*60)
        print(f"Scan interval: {Config.SCAN_INTERVAL_SECONDS}s")
        print(f"Min profit: {Config.MIN_PROFIT_PERCENT}%")
        print(f"Live trading: {'ENABLED ⚠️' if Config.LIVE_TRADING_ENABLED else 'DISABLED (paper only)'}")
        print(f"Max trade: ${Config.MAX_TRADE_USD}")
        print(f"Max exposure: ${Config.MAX_EXPOSURE_USD}")
        print(f"Press Ctrl+C to stop")
        print("="*60 + "\n")
        
        # Initialize trader with live balance
        starting_balance = api.get_balance() if Config.LIVE_TRADING_ENABLED else 250.0
        trader = KalshiTradingBot(
            api, 
            initial_balance=starting_balance,
            paper_trading=not Config.LIVE_TRADING_ENABLED,
            storage=storage
        )
        
        iteration = 0
        try:
            while True:
                iteration += 1
                print(f"\n{'='*60}")
                print(f"Scan #{iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*60}")
                
                # Scan all markets (single-market + multi-outcome event arbitrage)
                opportunities = bot.scan_all_markets_concurrent()
                
                if opportunities:
                    # Sort by profit % descending — execute best opportunities first
                    opportunities.sort(key=lambda o: o.get('profit_percent', 0), reverse=True)
                    print(f"\n✅ Found {len(opportunities)} arbitrage opportunities (sorted by profit %)")
                    
                    for opp in opportunities:
                        opp_type = opp.get('type', 'single')
                        ticker_label = opp.get('event_ticker') if opp_type == 'multi_leg' else opp.get('ticker')
                        
                        # Calculate maximum affordable quantity
                        max_from_book = opp.get('max_executable_qty', 1)
                        cost_per_contract = opp.get('total_cost', 100)  # cents
                        max_from_balance = int((trader.balance * 100) / cost_per_contract) if cost_per_contract > 0 else 0
                        max_from_trade_limit = int((Config.MAX_TRADE_USD * 100) / cost_per_contract) if cost_per_contract > 0 else 0
                        
                        quantity = max(1, min(max_from_book, max_from_balance, max_from_trade_limit))
                        
                        print(f"\n🎯 {'EVENT' if opp_type == 'multi_leg' else 'MARKET'}: {ticker_label} ({opp['profit_percent']:.2f}% profit, qty={quantity})")
                        
                        if opp_type == 'multi_leg':
                            # Multi-leg: place YES orders on each leg
                            print(f"   Multi-leg trade: {opp.get('num_legs')} outcomes")
                            if not Config.LIVE_TRADING_ENABLED:
                                # Paper trade
                                total_cost_usd = (cost_per_contract * quantity) / 100
                                trade = {
                                    'ticker': opp.get('event_ticker'),
                                    'type': 'multi_leg_arbitrage',
                                    'quantity': quantity,
                                    'legs': opp.get('legs', []),
                                    'cost': total_cost_usd,
                                    'expected_profit': (quantity * 100 - cost_per_contract * quantity) / 100,
                                    'timestamp': datetime.now().isoformat()
                                }
                                trader.trade_history.append(trade)
                                trader.balance -= total_cost_usd
                                trader.positions.append(trade)
                                print(f"   📄 Paper trade recorded: ${total_cost_usd:.2f}")
                            else:
                                # Live multi-leg execution
                                total_cost_usd = (cost_per_contract * quantity) / 100
                                if total_cost_usd > Config.MAX_TRADE_USD:
                                    print(f"   ⚠️ Exceeds max trade (${total_cost_usd:.2f} > ${Config.MAX_TRADE_USD})")
                                    continue
                                
                                all_orders = []
                                failed = False
                                for leg in opp.get('legs', []):
                                    order = api.place_order(leg['ticker'], 'yes', quantity, leg['yes_price'], order_type='limit')
                                    if order:
                                        all_orders.append(order)
                                    else:
                                        print(f"   ❌ Failed on leg {leg['ticker']} — attempting to cancel previous legs")
                                        for prev_order in all_orders:
                                            api.cancel_order(prev_order.get('order_id', ''))
                                        failed = True
                                        break
                                    time.sleep(Config.RATE_LIMIT_DELAY)
                                
                                if not failed:
                                    trade = {
                                        'ticker': opp.get('event_ticker'),
                                        'type': 'multi_leg_arbitrage',
                                        'quantity': quantity,
                                        'cost': total_cost_usd,
                                        'expected_profit': (quantity * 100 - cost_per_contract * quantity) / 100,
                                        'timestamp': datetime.now().isoformat(),
                                        'orders': all_orders
                                    }
                                    trader.trade_history.append(trade)
                                    trader.balance -= total_cost_usd
                                    trader.positions.append(trade)
                                    print(f"   ✅ All {len(all_orders)} legs executed!")
                        else:
                            # Single-market arbitrage
                            success = trader.execute_arbitrage(opp, quantity=quantity)
                            if success:
                                print(f"   ✅ Trade executed")
                            else:
                                print(f"   ⚠️ Trade not executed (safety limits)")
                        
                        time.sleep(Config.RATE_LIMIT_DELAY)
                else:
                    print(f"\n📊 No arbitrage opportunities found this scan")
                
                # Print current stats
                trader.print_stats()
                
                # Wait for next scan
                print(f"\n⏳ Waiting {Config.SCAN_INTERVAL_SECONDS}s until next scan...")
                print(f"Total scans: {iteration} | Total trades: {len(trader.trade_history)}")
                
                # Capital recycling - check for settled positions
                recycled = trader._capital_recycle()
                if recycled > 0:
                    print(f"♻️  Recycled capital from {recycled} settled positions")
                
                time.sleep(Config.SCAN_INTERVAL_SECONDS)
                
        except KeyboardInterrupt:
            print(f"\n\n{'='*60}")
            print("🛑 Scanner stopped by user")
            print(f"{'='*60}")
            trader.print_stats()
            print("\nFinal summary:")
            print(f"- Total scans: {iteration}")
            print(f"- Total opportunities found: {len(bot.opportunities_found)}")
            print(f"- Total trades executed: {len(trader.trade_history)}")
            print(f"{'='*60}")
            logger.info("Session ended: %d scans, %d trades", iteration, len(trader.trade_history))
            if storage:
                storage.close()
                logger.info("Storage flushed and closed.")
    
    elif choice == "9":
        # Scan crypto markets for probability edge
        from probability_trader import ProbabilityTrader
        
        print("\n" + "="*60)
        print("📊 SCANNING CRYPTO MARKETS FOR PROBABILITY EDGE")
        print("="*60)
        print(f"Min edge required: {Config.MIN_EDGE_PERCENT}%")
        print(f"BTC vol estimate: {Config.BTC_15MIN_VOL*100:.2f}%")
        print(f"ETH vol estimate: {Config.ETH_15MIN_VOL*100:.2f}%")
        print("="*60 + "\n")
        
        prob_trader = ProbabilityTrader(api, Config)
        opportunities = prob_trader.scan_crypto_markets()
        
        if opportunities:
            print(f"\n✅ Found {len(opportunities)} probability opportunities:\n")
            for i, opp in enumerate(opportunities, 1):
                print(f"{i}. {opp['ticker']} - {opp['strategy']}")
                print(f"   Side: {opp['side'].upper()} at {opp['price']}¢")
                print(f"   Edge: {opp['edge_percent']:.2f}% (est. prob: {opp['estimated_prob']*100:.1f}%, implied: {opp['implied_prob']*100:.1f}%)")
                print(f"   Kelly fraction: {opp['kelly_fraction']:.3f}")
                print(f"   Max qty: {opp['max_executable_qty']} | Time left: {opp['minutes_remaining']:.1f} min")
                print()
        else:
            print("\n📊 No probability edge opportunities found")
    
    elif choice == "10":
        # Continuous auto-trading for probability strategy
        from probability_trader import ProbabilityTrader
        from kelly import size_position
        
        print("\n" + "="*60)
        print("🤖 CONTINUOUS PROBABILITY AUTO-TRADING")
        print("="*60)
        print(f"Scan interval: 15 seconds (fast for crypto)")
        print(f"Min edge: {Config.MIN_EDGE_PERCENT}%")
        print(f"Kelly multiplier: {Config.KELLY_MULTIPLIER} (half-Kelly)")
        print(f"Live trading: {'ENABLED ⚠️' if Config.LIVE_TRADING_ENABLED else 'DISABLED (paper only)'}")
        print(f"Max trade: ${Config.MAX_TRADE_USD}")
        print(f"Press Ctrl+C to stop")
        print("="*60 + "\n")
        
        # Initialize trader
        starting_balance = api.get_balance() if Config.LIVE_TRADING_ENABLED else 250.0
        trader = KalshiTradingBot(
            api,
            initial_balance=starting_balance,
            paper_trading=not Config.LIVE_TRADING_ENABLED,
            storage=storage
        )
        
        prob_trader = ProbabilityTrader(api, Config)
        
        iteration = 0
        try:
            while True:
                iteration += 1
                print(f"\n{'='*60}")
                print(f"Probability Scan #{iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*60}")
                
                # Scan for probability opportunities
                opportunities = prob_trader.scan_crypto_markets()
                
                if opportunities:
                    # Sort by edge descending
                    opportunities.sort(key=lambda o: o.get('edge_percent', 0), reverse=True)
                    print(f"\n✅ Found {len(opportunities)} probability opportunities")
                    
                    for opp in opportunities:
                        ticker = opp['ticker']
                        side = opp['side']
                        price = opp['price']
                        edge_pct = opp['edge_percent']
                        est_prob = opp['estimated_prob']
                        
                        # Calculate position size using Kelly
                        quantity = size_position(
                            bankroll=trader.balance,
                            win_prob=est_prob,
                            contract_price_cents=price,
                            max_trade_usd=Config.MAX_TRADE_USD,
                            kelly_multiplier=Config.KELLY_MULTIPLIER
                        )
                        
                        # Limit to orderbook depth
                        quantity = min(quantity, opp.get('max_executable_qty', 1))
                        
                        if quantity < 1:
                            print(f"\n⚠️ {ticker}: Insufficient balance for trade")
                            continue
                        
                        cost_usd = (price * quantity) / 100.0
                        expected_profit = ((1.0 - price/100.0) * est_prob - (price/100.0) * (1-est_prob)) * quantity
                        
                        print(f"\n🎯 {ticker} - {opp['strategy']}")
                        print(f"   {side.upper()} @ {price}¢ x {quantity} = ${cost_usd:.2f}")
                        print(f"   Edge: {edge_pct:.2f}% | Expected profit: ${expected_profit:.2f}")
                        
                        # Execute trade (paper or live)
                        if trader.paper_trading:
                            trade = {
                                'ticker': ticker,
                                'type': 'probability',
                                'side': side,
                                'quantity': quantity,
                                'price': price,
                                'cost': cost_usd,
                                'expected_profit': expected_profit,
                                'edge_percent': edge_pct,
                                'timestamp': datetime.now().isoformat()
                            }
                            trader.trade_history.append(trade)
                            trader.balance -= cost_usd
                            trader.positions.append(trade)
                            print(f"   📄 Paper trade recorded")
                        else:
                            # Live trading
                            if cost_usd > Config.MAX_TRADE_USD:
                                print(f"   ⚠️ Exceeds max trade size")
                                continue
                            
                            if len(trader.positions) >= Config.MAX_POSITIONS:
                                print(f"   ⚠️ Position limit reached")
                                continue
                            
                            order = api.place_order(ticker, side, quantity, price, order_type='limit')
                            if order:
                                trade = {
                                    'ticker': ticker,
                                    'type': 'probability',
                                    'side': side,
                                    'quantity': quantity,
                                    'price': price,
                                    'cost': cost_usd,
                                    'expected_profit': expected_profit,
                                    'edge_percent': edge_pct,
                                    'timestamp': datetime.now().isoformat(),
                                    'order': order
                                }
                                trader.trade_history.append(trade)
                                trader.balance -= cost_usd
                                trader.positions.append(trade)
                                print(f"   ✅ Live order placed")
                            else:
                                print(f"   ❌ Order failed")
                        
                        time.sleep(Config.RATE_LIMIT_DELAY)
                else:
                    print(f"\n📊 No probability opportunities found this scan")
                
                # Print stats
                trader.print_stats()
                
                # Capital recycling
                recycled = trader._capital_recycle()
                if recycled > 0:
                    print(f"♻️  Recycled capital from {recycled} settled positions")
                
                # Wait for next scan (15 seconds for fast crypto markets)
                print(f"\n⏳ Waiting 15 seconds until next scan...")
                time.sleep(15)
                
        except KeyboardInterrupt:
            print(f"\n\n{'='*60}")
            print("🛑 Probability scanner stopped by user")
            print(f"{'='*60}")
            trader.print_stats()
            print(f"\nTotal probability scans: {iteration}")
            print(f"Total trades: {len(trader.trade_history)}")
            print(f"{'='*60}")
            logger.info("Probability session ended: %d scans, %d trades", iteration, len(trader.trade_history))
            if storage:
                storage.close()
                logger.info("Storage flushed and closed.")
    
    elif choice == "11":
        # Scan for cross-platform arbitrage (Kalshi ↔ Polymarket)
        from cross_platform_arb import CrossPlatformArbitrage

        print("\n" + "="*60)
        print("🌐 CROSS-PLATFORM ARBITRAGE SCANNER (Kalshi ↔ Polymarket)")
        print("="*60)
        print(f"Min profit: {Config.CROSS_PLATFORM_MIN_PROFIT_PERCENT}%")
        print(f"Similarity threshold: {Config.MATCH_SIMILARITY_THRESHOLD}")
        _cats = Config.POLYMARKET_CATEGORIES
        print(f"🌐 Cross-platform categories: {_cats}")
        if _cats.strip().lower() != 'all':
            print("   (Set POLYMARKET_CATEGORIES=all for unrestricted scanning)")
        print("="*60 + "\n")

        scanner = CrossPlatformArbitrage(api, storage=storage)
        opportunities = scanner.scan_opportunities()

        if opportunities:
            print(f"\n✅ Found {len(opportunities)} cross-platform opportunities:\n")
            for i, opp in enumerate(opportunities, 1):
                print(f"{i}. {opp['kalshi_ticker']} ↔ Polymarket")
                print(f"   Kalshi: {opp['kalshi_title'][:60]}")
                print(f"   Polymarket: {opp['poly_title'][:60]}")
                print(f"   Match confidence: {opp['match_confidence']:.2%}")
                print(f"   Strategy: {opp['strategy']}")
                print(f"   Profit: {opp['profit_cents']}¢ ({opp['profit_percent']:.2f}%)")
                print()
        else:
            print("\n📊 No cross-platform arbitrage opportunities found")

    elif choice == "12":
        # Continuous cross-platform arbitrage scanner
        from cross_platform_arb import CrossPlatformArbitrage

        print("\n" + "="*60)
        print("🌐 CONTINUOUS CROSS-PLATFORM ARBITRAGE SCANNER")
        print("="*60)
        print(f"Scan interval: {Config.SCAN_INTERVAL_SECONDS}s")
        print(f"Min profit: {Config.CROSS_PLATFORM_MIN_PROFIT_PERCENT}%")
        print(f"Similarity threshold: {Config.MATCH_SIMILARITY_THRESHOLD}")
        _cats = Config.POLYMARKET_CATEGORIES
        print(f"🌐 Cross-platform categories: {_cats}")
        if _cats.strip().lower() != 'all':
            print("   (Set POLYMARKET_CATEGORIES=all for unrestricted scanning)")
        print(f"Press Ctrl+C to stop")
        print("="*60 + "\n")

        scanner = CrossPlatformArbitrage(api, storage=storage)
        scanner.scan_continuous(interval=Config.SCAN_INTERVAL_SECONDS)

    else:
        print("Invalid choice")


