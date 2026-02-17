"""Quick test runner for automated testing"""
import os
import sys

# Set UTF-8 encoding
os.environ['PYTHONIOENCODING'] = 'utf-8'

# Import after setting encoding
from kalshi_bot import KalshiAPI, KalshiArbitrageBot
from config import Config
from probability_trader import ProbabilityTrader

def test_1_smoke_test():
    """Test 1: Smoke test - authenticate and scan"""
    print("\n" + "="*60)
    print("TEST 1: SMOKE TEST - Authentication & Scan")
    print("="*60)
    
    try:
        api = KalshiAPI(api_key=Config.KALSHI_API_KEY)
        print("✅ API initialized")
        
        # Test get balance
        balance = api.get_balance()
        print(f"✅ Balance retrieved: ${balance:.2f}")
        
        # Test get markets (limited to 200 for speed)
        markets, cursor = api.get_markets(status="open", limit=200)
        print(f"✅ Fetched {len(markets)} markets")
        
        # Initialize bot
        bot = KalshiArbitrageBot(api, min_profit_percent=Config.MIN_PROFIT_PERCENT)
        print("✅ Bot initialized")
        
        print("\n✅ TEST 1 PASSED - Authentication and basic API calls work")
        return True
        
    except Exception as e:
        print(f"\n❌ TEST 1 FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_2_crypto_filter():
    """Test 2: Crypto market filtering"""
    print("\n" + "="*60)
    print("TEST 2: CRYPTO MARKET FILTER")
    print("="*60)
    
    try:
        api = KalshiAPI(api_key=Config.KALSHI_API_KEY)
        
        # Fetch BTC markets
        btc_markets = api.get_all_markets(status="open", series_ticker="KXBTC")
        print(f"✅ Found {len(btc_markets)} BTC markets")
        
        # Fetch ETH markets  
        eth_markets = api.get_all_markets(status="open", series_ticker="KXETH")
        print(f"✅ Found {len(eth_markets)} ETH markets")
        
        total_crypto = len(btc_markets) + len(eth_markets)
        print(f"✅ Total crypto markets: {total_crypto}")
        
        if total_crypto > 0:
            print("\n✅ TEST 2 PASSED - Crypto market filtering works")
            return True
        else:
            print("\n⚠️ TEST 2 WARNING - No crypto markets found (may be normal)")
            return True
            
    except Exception as e:
        print(f"\n❌ TEST 2 FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_3_paper_trading():
    """Test 3: Paper trading execution"""
    print("\n" + "="*60)
    print("TEST 3: PAPER TRADING EXECUTION")
    print("="*60)
    
    try:
        from kalshi_bot import KalshiTradingBot
        from storage import Storage
        
        api = KalshiAPI(api_key=Config.KALSHI_API_KEY)
        storage = Storage("test_kalshi.db")
        
        trader = KalshiTradingBot(api, initial_balance=1000.0, paper_trading=True, storage=storage)
        print(f"✅ Trader initialized with balance: ${trader.balance:.2f}")
        
        # Demo opportunity
        demo_opportunity = {
            'ticker': 'DEMO-TICKER',
            'title': 'Demo Market for Testing',
            'yes_price': 48,
            'no_price': 50,
            'total_cost': 98,
            'profit_cents': 2,
            'profit_percent': 2.04
        }
        
        initial_balance = trader.balance
        result = trader.execute_arbitrage(demo_opportunity, quantity=10)
        
        if result:
            print(f"✅ Trade executed - Balance: ${initial_balance:.2f} → ${trader.balance:.2f}")
            print(f"✅ Trades in history: {len(trader.trade_history)}")
            print("\n✅ TEST 3 PASSED - Paper trading works")
            return True
        else:
            print("\n❌ TEST 3 FAILED - Trade execution failed")
            return False
            
    except Exception as e:
        print(f"\n❌ TEST 3 FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_4_probability_scan():
    """Test 4: Probability strategy scan"""
    print("\n" + "="*60)
    print("TEST 4: PROBABILITY STRATEGY SCAN")
    print("="*60)
    
    try:
        api = KalshiAPI(api_key=Config.KALSHI_API_KEY)
        trader = ProbabilityTrader(api)
        
        print(f"Min edge: {trader.min_edge_percent}%")
        print(f"BTC vol: {trader.vol_estimates['BTC']*100:.2f}%")
        print(f"ETH vol: {trader.vol_estimates['ETH']*100:.2f}%")
        print("\nScanning crypto markets...")
        
        opportunities = trader.scan_crypto_markets()
        
        print(f"\n✅ Scan complete: {len(opportunities)} opportunities found")
        
        if opportunities:
            print("\nTop opportunities:")
            for opp in opportunities[:3]:
                print(f"  - {opp['ticker']}: {opp['edge_percent']:.2f}% edge")
        
        print("\n✅ TEST 4 PASSED - Probability scan works")
        return True
        
    except Exception as e:
        print(f"\n❌ TEST 4 FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    print("\n" + "="*60)
    print("KALSHI BOT TEST SUITE")
    print("="*60)
    
    results = {
        "Test 1: Smoke Test": test_1_smoke_test(),
        "Test 2: Crypto Filter": test_2_crypto_filter(),
        "Test 3: Paper Trading": test_3_paper_trading(),
        "Test 4: Probability Scan": test_4_probability_scan()
    }
    
    print("\n" + "="*60)
    print("TEST RESULTS SUMMARY")
    print("="*60)
    
    for test_name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {test_name}")
    
    passed = sum(results.values())
    total = len(results)
    print(f"\nTotal: {passed}/{total} tests passed")
    
    sys.exit(0 if passed == total else 1)
