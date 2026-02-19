"""Backwards-compatible entry point. Imports from refactored modules."""
from api import KalshiAPI
from arbitrage import KalshiArbitrageBot
from trading import KalshiTradingBot
from cli import main
from config import Config

__all__ = ['KalshiAPI', 'KalshiArbitrageBot', 'KalshiTradingBot', 'main', 'Config']

if __name__ == "__main__":
    main()
