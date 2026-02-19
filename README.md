# Kalshi Arbitrage Bot

An automated trading bot for [Kalshi](https://kalshi.com) prediction markets. Detects and executes arbitrage opportunities, probability mispricing, and convergence trades.

## Features

- **Market Arbitrage**: Buy YES + NO on the same market when YES + NO < 100¢ (guaranteed profit)
- **Multi-leg Event Arbitrage**: Buy YES on all outcomes of a multi-outcome event when total < 100¢
- **Probability Trading**: Detect mispriced crypto interval markets using normal-distribution pricing
- **Convergence Trading**: Trade markets that are converging toward certainty
- **Kelly Criterion Sizing**: Optimal position sizing using half-Kelly by default
- **Partial Fill Monitoring**: Polls order fill status; cancels unfilled remainders to prevent naked exposure
- **Thread-safe Concurrent Scanning**: Scans markets in parallel with thread-local HTTP sessions
- **SQLite Persistence**: Opportunities and trades saved to `kalshi_bot.db`
- **Retry Logic**: Exponential backoff on 429/5xx errors
- **WebSocket Real-time Trader**: Live orderbook streaming via `ws_trader.py`
- **Notifications**: Email/webhook alerts on opportunities and trade execution

## Installation

```bash
git clone https://github.com/jasonalvarodiaz1/kalshi-arbitrage-bot.git
cd kalshi-arbitrage-bot
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
```

### Dependencies

```
requests>=2.31.0
python-dotenv>=1.0.0
PyJWT>=2.8.0
cryptography>=40.0.0
scipy>=1.10.0
websocket-client>=1.6.0
```

## Configuration

Copy `.env.example` to `.env` and configure:

```env
# API Authentication
KALSHI_API_KEY=your-key-id-uuid
KALSHI_PRIVATE_KEY_PATH=/path/to/private_key.pem

# Trading Settings
PAPER_TRADING=true
ENABLE_LIVE_TRADING=false
MAX_TRADE_USD=100
MAX_EXPOSURE_USD=500
MAX_POSITIONS=10

# Strategy Parameters
MIN_PROFIT_PERCENT=1.0
MIN_EDGE_PERCENT=3.0
KELLY_MULTIPLIER=0.5
SCAN_INTERVAL_SECONDS=60

# Crypto Volatility (for probability trading)
BTC_15MIN_VOL=0.004
ETH_15MIN_VOL=0.005

# Fill Monitoring
FILL_WAIT_TIMEOUT_SECONDS=30

# Market Filters
MIN_EXPIRY_MINUTES=30
MAX_EXPIRY_HOURS=24
```

## Usage

```bash
python kalshi_bot.py
```

### Menu Options

```
1. Scan all markets once (concurrent)       — Full market scan, finds all arb opportunities
2. Scan specific category                   — Filter by keyword (BTC, NASDAQ, etc.)
3. Monitor specific tickers continuously    — Watch a set of tickers on a loop
4. Demo paper trading                       — Run a simulated arbitrage trade
5. View historical stats                    — Show trading statistics
6. Reconcile settled positions & view P&L  — Check which positions have settled
7. Reconcile positions with exchange        — Compare local vs. exchange positions
8. Continuous auto-trading scanner 🤖       — Scans and executes arbitrage automatically
9. Scan crypto markets for probability edge — BTC/ETH interval market analysis
10. Auto-trade crypto probability strategy  — Continuous probability-based trading loop
```

## Architecture

After refactoring, the codebase is split into focused modules:

| Module | Description |
|--------|-------------|
| `api.py` | `KalshiAPI` — authentication, all API methods, retry logic |
| `arbitrage.py` | `KalshiArbitrageBot` — market scanning, opportunity detection, event arb |
| `trading.py` | `KalshiTradingBot` — trade execution, fill monitoring, position management |
| `cli.py` | `main()` — interactive menu system |
| `kalshi_bot.py` | Thin backwards-compatible shim (re-exports from above modules) |
| `probability_trader.py` | `ProbabilityTrader` — crypto interval market pricing |
| `convergence_trader.py` | `ConvergenceTrader` — convergence strategy |
| `kelly.py` | `size_position()` — Kelly criterion position sizing |
| `storage.py` | `Storage` — SQLite persistence |
| `ws_trader.py` | `WSTrader` — WebSocket real-time trading |
| `notifications.py` | `NotificationManager` — email/webhook alerts |

## Scripts

Diagnostic and analysis scripts are in `scripts/`. Run from the repo root:

```bash
python scripts/check_pnl.py
python scripts/scan_crypto_now.py
python scripts/diagnostic_scan.py
```

See [`scripts/README.md`](scripts/README.md) for full list.

## Testing

```bash
python -m pytest tests/
```

Tests cover: Kelly sizing, leg-risk protection, retry logic, adaptive thresholds, probability trading, orderbook walking, partial fill monitoring, storage operations, and market pre-filtering.

## Safety

- **Paper trading by default** — set `ENABLE_LIVE_TRADING=true` to enable live orders
- **Leg-risk protection** — if the NO order fails after YES fills, YES is auto-cancelled
- **Partial fill monitoring** — unfilled order remainders are cancelled after timeout
- **Stale price check** — orderbook is re-fetched before live orders; aborts if no longer profitable
- **Exposure limits** — `MAX_TRADE_USD`, `MAX_EXPOSURE_USD`, `MAX_POSITIONS` enforced

## License

Educational/research use only. See [LICENSE](LICENSE).
