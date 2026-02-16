# Probability Trading Engine - Feature Documentation

This document describes the probability-based trading features added to the Kalshi Arbitrage Bot.

## Overview

The bot now supports **probability-based trading** for Kalshi crypto interval markets (15-min / hourly BTC/ETH), in addition to traditional arbitrage detection. This strategy is based on buying contracts where the implied probability (from market price) differs significantly from the estimated actual probability.

## Key Features

### 1. Probability Trading Engine (`probability_trader.py`)

**What it does:**
- Scans crypto interval markets (KXBTC, KXETH tickers)
- Fetches current BTC/ETH prices from CoinGecko
- Estimates actual probabilities using volatility models
- Identifies trades where estimated probability > implied probability

**Example:**
```
Market: KXBTC-26FEB16-T98000 (BTC above $98,000 in 15 minutes)
Current BTC price: $98,500
YES contract price: 85¢

Implied probability: 85% (from price)
Estimated probability: 92% (from volatility model)
Edge: 7% → profitable trade opportunity
```

**Key Methods:**
- `parse_strike_from_ticker()` - Extracts strike price from ticker
- `get_current_price()` - Fetches crypto prices (10s cache)
- `estimate_probability()` - Uses normal distribution CDF
- `calculate_edge()` - Computes expected value
- `scan_crypto_markets()` - Scans all crypto markets
- `auto_trade_loop()` - Continuous trading loop

### 2. Kelly Criterion Position Sizing (`kelly.py`)

**What it does:**
- Calculates optimal position size based on edge and bankroll
- Uses Kelly Criterion formula: `f = (bp - q) / b`
- Defaults to **half-Kelly** for conservative risk management

**Functions:**
- `kelly_fraction(win_prob, win_amount, loss_amount)` - Calculate Kelly fraction
- `size_position(bankroll, win_prob, price, max_trade)` - Size position in contracts

**Example:**
```python
# 96% win probability, 92¢ contract
kelly = kelly_fraction(0.96, 0.08, 0.92)  # Returns ~0.50
contracts = size_position(1000, 0.96, 92, 100, 0.5)  # Returns optimal quantity
```

### 3. Automatic Capital Recycling

**What it does:**
- Checks for settled positions after each scan cycle
- Automatically adds payout back to available balance
- Critical for 15-min markets where capital turns over frequently

**Implementation:**
```python
# In auto-trading loop
trader._capital_recycle()  # Check for settled positions
# Freed capital immediately available for next trade
```

### 4. SQLite Storage (`storage.py`)

**What it does:**
- Persists opportunities and trades to database
- Tracks trade history and P&L
- Optional integration (gracefully degrades if unavailable)

**Schema:**
- `opportunities` - Detected arbitrage/probability opportunities
- `trades` - Executed trades with status tracking

### 5. New Configuration Options

Added to `.env`:
```bash
# Probability Trading
MIN_EDGE_PERCENT=3.0          # Minimum edge to trade (%)
KELLY_MULTIPLIER=0.5          # Half-Kelly (conservative)
BTC_15MIN_VOL=0.004           # BTC 15-min realized volatility
ETH_15MIN_VOL=0.005           # ETH 15-min realized volatility
PRICE_CACHE_SECONDS=10        # Price feed cache TTL
```

### 6. New Menu Options

**Option 9: Scan crypto markets for probability edge**
- Scans all KXBTC/KXETH markets
- Shows edge, Kelly fraction, time remaining
- One-time scan

**Option 10: Auto-trade crypto probability strategy**
- Continuous scanning every 15 seconds
- Automatic position sizing via Kelly
- Capital recycling after each cycle
- Paper or live trading

## Usage Examples

### Scan for Probability Opportunities

```bash
python kalshi_bot.py
# Choose option 9

Output:
Found 3 probability opportunities:
1. KXBTC-26FEB16-T98000 - BTC $98500 vs strike $98000
   Side: YES at 85¢
   Edge: 7.2% (est. prob: 92.2%, implied: 85.0%)
   Kelly fraction: 0.389
   Max qty: 50 | Time left: 12.3 min
```

### Auto-Trade with Kelly Sizing

```bash
python kalshi_bot.py
# Choose option 10

# Bot will:
# 1. Scan every 15 seconds
# 2. Calculate Kelly size for each opportunity
# 3. Execute trades (paper or live)
# 4. Recycle capital from settled positions
# 5. Print stats after each cycle
```

## How Probability Estimation Works

The bot uses a **normal distribution model** to estimate probabilities:

1. **Get current price** from CoinGecko (cached 10 seconds)
2. **Calculate distance from strike**: `log(current / strike)`
3. **Scale by volatility**: `vol * sqrt(time_remaining)`
4. **Compute z-score**: `z = distance / vol_term`
5. **Get probability**: `P(above) = norm.cdf(z)`

**Volatility estimates:**
- BTC 15-min: 0.4% (default)
- ETH 15-min: 0.5% (default)
- Configurable via `.env`

## Risk Management

**Built-in safeguards:**
- ✅ Half-Kelly position sizing (conservative)
- ✅ Minimum edge threshold (3% default)
- ✅ Maximum trade size cap
- ✅ Maximum exposure limit
- ✅ Position count limit
- ✅ Price feed failover (graceful degradation)

**Safety recommendations:**
1. Start with paper trading
2. Use small MAX_TRADE_USD initially
3. Monitor first 10-20 trades manually
4. Adjust MIN_EDGE_PERCENT based on results
5. Keep KELLY_MULTIPLIER at 0.5 or lower

## Testing

**Unit tests included:**
- `tests/test_kelly.py` - Kelly fraction and position sizing
- `tests/test_probability_trader.py` - Ticker parsing, probability estimation

Run tests:
```bash
python -m unittest discover -s tests -v
```

All 23 tests passing ✅

## Differences from Traditional Arbitrage

| Feature | Traditional Arb | Probability Trading |
|---------|----------------|---------------------|
| **Strategy** | Buy YES + NO when total < $1 | Buy one side with +EV edge |
| **Risk** | Guaranteed profit | Probabilistic edge |
| **Frequency** | Rare opportunities | More frequent |
| **Position sizing** | Fixed or manual | Kelly Criterion |
| **Markets** | Any Kalshi market | Crypto interval markets |
| **Win rate** | ~100% | ~60-95% depending on edge |

## Technical Implementation Notes

**Dependencies:**
- `scipy` - For normal distribution CDF (falls back to pure Python)
- `requests` - For CoinGecko price feed
- `sqlite3` - For storage (optional)

**Performance:**
- Price feed: 10s cache to avoid rate limits
- Scan speed: ~15s for all crypto markets
- Concurrent orderbook fetching (thread-safe)

**Error handling:**
- Graceful price feed degradation
- Orderbook validation
- Zero-division guards
- Invalid ticker handling

## Future Enhancements (Not Included)

Potential improvements for future PRs:
- [ ] Dynamic volatility estimation from historical data
- [ ] Multi-asset correlation modeling
- [ ] Advanced probability models (Black-Scholes, GARCH)
- [ ] Backtesting framework
- [ ] Real-time price feeds (WebSocket)
- [ ] Multi-exchange arbitrage
- [ ] Machine learning for edge detection

## Disclaimer

This is an **educational tool**. Probability-based trading involves:
- Market risk
- Model risk (volatility estimates may be wrong)
- Execution risk (prices change between detection and execution)
- Price feed risk (CoinGecko outages, delays)

**No guarantees of profitability.** Trade at your own risk.

## Support

- GitHub Issues: https://github.com/jasonalvarodiaz1/kalshi-arbitrage-bot/issues
- Documentation: See README.md for installation and basic usage
