"""
Example: Monitor specific Kalshi tickers continuously
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_bot import KalshiAPI, KalshiArbitrageBot

def main():
    # Initialize
    api = KalshiAPI()
    bot = KalshiArbitrageBot(api, min_profit_percent=1.0)
    
    # Example tickers (replace with real ones from Kalshi)
    tickers = [
        "KXBTC-24JAN15-T50000",
        "INX-24JAN-T4500"
    ]
    
    print(f"Monitoring {len(tickers)} markets...")
    print("Press Ctrl+C to stop\n")
    
    # Monitor continuously (checks every 60 seconds)
    bot.monitor_specific_markets(tickers, interval=60)

if __name__ == "__main__":
    main()