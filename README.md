# 🤖 Kalshi Arbitrage Bot

An AI-powered arbitrage detection bot for Kalshi prediction markets. Scan markets, detect pricing inefficiencies, and execute profitable trades automatically.

![Python](https://img.shields.io/badge/python-3.8+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Status](https://img.shields.io/badge/status-educational-yellow.svg)

## ⚠️ Important Disclaimers

- **Educational Purpose**: This bot is designed for learning about algorithmic trading and arbitrage strategies
- **Paper Trading Default**: Bot starts in paper trading mode - no real money at risk
- **No Guarantees**: Past performance doesn't guarantee future results
- **Risk Warning**: Trading involves risk of loss. Only trade with money you can afford to lose
- **Realistic Expectations**: Professional arbitrage opportunities are rare and competitive

## 🎯 What This Bot Does

This bot detects **arbitrage opportunities** on Kalshi, a CFTC-regulated prediction market exchange. 

**How Arbitrage Works:**
- In prediction markets, YES + NO outcomes should total $1.00 (100 cents)
- Sometimes due to market inefficiencies, YES + NO < $1.00
- You can buy BOTH outcomes and guarantee profit when the market settles
- Example: YES at 48¢ + NO at 50¢ = 98¢ spent, $1.00 payout = 2¢ profit (2% return)

**Key Features:**
- ✅ Scan all Kalshi markets for arbitrage opportunities
- ✅ Real-time monitoring of specific tickers
- ✅ Paper trading mode for risk-free testing
- ✅ Orderbook analysis and profit calculations
- ✅ Category filtering (BTC, NASDAQ, etc.)
- ✅ Automated trade execution (when enabled)

## 🏆 Why Kalshi?

| Feature | Benefit |
|---------|---------|
| **CFTC Regulated** | Legal and safe for US residents |
| **USD-Based** | No cryptocurrency complexity |
| **Bank Integration** | Easy deposits and withdrawals |
| **Tax Reporting** | Receives 1099 forms automatically |
| **Customer Support** | Real support for regulated exchange |

## 🚀 Quick Start

### Prerequisites

- Python 3.8 or higher
- Kalshi account (optional for scanning, required for trading)
- Basic command line knowledge

### Installation

1. **Clone the repository:**
```bash
git clone https://github.com/jasonalvarodiaz1/kalshi-arbitrage-bot.git
cd kalshi-arbitrage-bot
```

2. **Create a virtual environment (recommended):**
```bash
# On macOS/Linux:
python3 -m venv venv
source venv/bin/activate

# On Windows:
python -m venv venv
venv\Scripts\activate
```

3. **Install dependencies:**
```bash
pip install -r requirements.txt
```

4. **Configure the bot (optional):**
```bash
cp .env.example .env
# Edit .env with your credentials (only if you want authenticated features)
```

5. **Run the bot:**
```bash
python kalshi_bot.py
```

## 📖 Usage Examples

### Scan All Markets Once

```bash
python kalshi_bot.py
# Choose option 1: Scan all markets once
```

This scans all open Kalshi markets and reports any arbitrage opportunities found.

### Scan Specific Category

```bash
python kalshi_bot.py
# Choose option 2: Scan specific category
# Enter: BTC (or NASDAQ, INX, etc.)
```

Filters markets by keyword to focus on specific asset classes.

### Monitor Specific Tickers

```bash
python kalshi_bot.py
# Choose option 3: Monitor specific tickers continuously
# Enter tickers: KXBTC-24JAN15-T50000, INX-24JAN-T4500
```

Continuously monitors specific markets and alerts when opportunities appear.

### Demo Paper Trading

```bash
python kalshi_bot.py
# Choose option 4: Demo paper trading
```

Simulates executing an arbitrage trade with fake money to see how it works.

### Use Example Scripts

```bash
# Simple market scan
python examples/scan_markets.py

# Continuous monitoring
python examples/monitor_ticker.py
```

## ⚙️ Configuration

Create a `.env` file from the example:

```bash
cp .env.example .env
```

Configuration options:

```bash
# Kalshi API Credentials (optional - only for authenticated features)
KALSHI_EMAIL=your-email@example.com
KALSHI_PASSWORD=your-password

# Bot Settings
MIN_PROFIT_PERCENT=1.0          # Minimum profit threshold
SCAN_INTERVAL_SECONDS=60        # Seconds between scans
PAPER_TRADING=true              # Safe mode (no real trades)
```

## 🧠 How It Works

### 1. Market Scanning
```python
# Bot fetches all open markets from Kalshi API
markets = api.get_markets(status="open")
```

### 2. Orderbook Analysis
```python
# Gets best bid/ask prices for YES and NO
orderbook = api.get_orderbook(ticker)
best_yes_ask = min(orderbook['yes_asks'])
best_no_ask = min(orderbook['no_asks'])
```

### 3. Arbitrage Detection
```python
# Calculates if buying both outcomes is profitable
total_cost = best_yes_ask + best_no_ask  # In cents
guaranteed_payout = 100  # Always pays 100 cents
profit = guaranteed_payout - total_cost

if profit / total_cost > min_profit_percent:
    # Opportunity found!
```

### 4. Trade Execution (Paper Trading)
```python
# Simulates buying both YES and NO
# Real trading requires uncommenting actual API calls
trader.execute_arbitrage(opportunity, quantity=10)
```

## 💰 Realistic Expectations

### What the Hype Says:
- "$500-$800 daily profits"
- "$75k in a month"
- "Hundreds of opportunities per day"

### Reality Check:

**Actual Experience:**
- ⚠️ True arbitrage opportunities are **rare** (maybe 1-5 per week)
- ⚠️ Profits are **small** (0.5-3% per trade typically)
- ⚠️ You need **significant capital** ($10k+) to make meaningful money
- ⚠️ **Professional market makers** capture most opportunities instantly
- ⚠️ **Fees and slippage** eat into thin margins

**More Realistic Goals:**
- Start with $100-500 learning capital
- Expect $5-25/day if successful (1-5% returns)
- Paper trade for weeks before risking real money
- Focus on learning, not getting rich quick

**Why It's Hard:**
- Markets are efficient - pros already run these bots
- Speed matters - opportunities disappear in seconds
- Liquidity issues - can't always fill large orders
- Competition - you're trading against algorithms and market makers

## 🔒 Safety Features

### Built-in Protections:
- ✅ **Paper trading default** - No real money at risk
- ✅ **Rate limiting** - Respects API limits
- ✅ **Balance checks** - Won't overtrade your capital
- ✅ **Error handling** - Graceful failure modes
- ✅ **Clear logging** - See what the bot is doing

### Before Live Trading:
1. ✅ Paper trade for at least 2-4 weeks
2. ✅ Verify arbitrage logic with small amounts
3. ✅ Understand Kalshi fees structure
4. ✅ Set strict position size limits
5. ✅ Have stop-loss rules in place
6. ✅ Never risk more than 1-2% per trade

## 🐛 Troubleshooting

### "No opportunities found"
This is **normal**! True arbitrage is rare. Try:
- Lower your `MIN_PROFIT_PERCENT` to 0.5%
- Scan during high volatility (market open, news events)
- Monitor more markets simultaneously

### "Login failed" / "401 Unauthorized"
- Check your credentials in `.env`
- Ensure your Kalshi account is verified
- Public market scanning doesn't require login

### "Rate limit exceeded"
- Increase `RATE_LIMIT_DELAY` in config
- Reduce scan frequency
- Monitor fewer markets simultaneously

### Bot runs but shows errors
- Check Python version (3.8+ required)
- Reinstall dependencies: `pip install -r requirements.txt --force-reinstall`
- Check Kalshi API status: https://kalshi.com

## 📚 Project Structure

```
kalshi-arbitrage-bot/
├── README.md              # This file
├── requirements.txt       # Python dependencies
├── .env.example          # Configuration template
├── .gitignore            # Git ignore rules
├── kalshi_bot.py         # Main bot code
├── config.py             # Configuration management
└── examples/             # Example scripts
    ├── scan_markets.py   # Simple market scan
    └── monitor_ticker.py # Continuous monitoring
```

## 🤝 Contributing

Contributions welcome! Areas for improvement:

- Cross-market correlation analysis
- Machine learning price prediction
- Discord/Telegram notifications
- Web dashboard for monitoring
- Historical performance tracking
- Advanced risk management

**To contribute:**
1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📜 Legal & Compliance

- **Trading Risk**: You can lose money trading. Only risk capital you can afford to lose.
- **No Investment Advice**: This bot is educational software, not financial advice.
- **Regulatory Compliance**: Ensure prediction market trading is legal in your jurisdiction.
- **Tax Obligations**: Trading profits may be taxable. Consult a tax professional.
- **API Terms**: Respect Kalshi's API terms of service and rate limits.

## 📄 License

MIT License - see LICENSE file for details.

## 🙏 Acknowledgments

- Built with Claude AI assistance
- Inspired by the prediction market arbitrage community
- Powered by Kalshi's public API

## 📞 Support

- **Issues**: Open a GitHub issue
- **Discussions**: Use GitHub Discussions for questions
- **Kalshi Support**: https://kalshi.com/support

---

**Remember**: Start with paper trading, learn the markets, and never risk more than you can afford to lose. Good luck! 🚀