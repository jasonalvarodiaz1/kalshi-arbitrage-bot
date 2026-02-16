"""
Example: Scan all Kalshi markets once for arbitrage opportunities
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_bot import KalshiAPI, KalshiArbitrageBot

def main():
    print("Scanning Kalshi markets for arbitrage opportunities...\n")
    
    # Initialize API (no login needed for public data)
    api = KalshiAPI()
    
    # Create bot with 1% minimum profit threshold
    bot = KalshiArbitrageBot(api, min_profit_percent=1.0)
    
    # Scan all markets
    opportunities = bot.scan_all_markets()
    
    # Print results
    print(f"\n{'='*60}")
    print(f"Found {len(opportunities)} arbitrage opportunities")
    print(f"{'='*60}")
    
    for i, opp in enumerate(opportunities, 1):
        print(f"\n{i}. {opp['title']}")
        print(f"   Ticker: {opp['ticker']}")
        print(f"   YES: {opp['yes_price']}¢ | NO: {opp['no_price']}¢")
        print(f"   Profit: {opp['profit_cents']}¢ ({opp['profit_percent']:.2f}%)")

if __name__ == "__main__":
    main()