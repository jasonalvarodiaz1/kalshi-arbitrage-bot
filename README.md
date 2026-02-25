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
11. Scan for cross-platform arbitrage 🌐    — One-shot Kalshi ↔ Polymarket scan
12. Continuous cross-platform scanner 🌐    — Continuous Kalshi ↔ Polymarket loop
13. Scan Polymarket sports for same-event arb ⚽ — One-shot same-event binary sports arb
14. Continuous Polymarket sports scanner ⚽  — Continuous same-event sports arb loop
15. Scan Kalshi ↔ Manifold cross-platform arb 🎯 — One-shot Kalshi ↔ Manifold scan
16. Continuous Kalshi ↔ Manifold scanner 🎯  — Continuous Kalshi ↔ Manifold loop
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
| `polymarket_api.py` | `PolymarketAPI` — Polymarket CLOB API client (read-only) |
| `cross_platform_arb.py` | `CrossPlatformArbitrage` — Kalshi ↔ Polymarket arbitrage scanner |
| `polymarket_sports_arb.py` | `PolymarketSportsArbitrage` — same-event multi-outcome arb within Polymarket |
| `manifold_api.py` | `ManifoldAPI` — Manifold Markets API client (reads + order placement) |
| `manifold_arb.py` | `ManifoldArbitrage` — Kalshi ↔ Manifold cross-platform arbitrage scanner |

## Cross-platform Arbitrage (Kalshi ↔ Polymarket)

Price divergences of 2–7¢ are common on overlapping events between Kalshi
and Polymarket.  The scanner detects two strategies:

- **Buy YES on Kalshi + NO on Polymarket** when `kalshi_yes_ask + poly_no_ask < 100¢`
- **Buy YES on Polymarket + NO on Kalshi** when `poly_yes_ask + kalshi_no_ask < 100¢`

### Configuration

Add these to your `.env`:

```env
# Optional API key for Polymarket CLOB (read-only works without it)
POLYMARKET_API_KEY=

# Disable Polymarket scanning entirely
POLYMARKET_ENABLED=true

# Minimum profit % to report a cross-platform opportunity (default 2.0)
CROSS_PLATFORM_MIN_PROFIT_PERCENT=2.0

# Title similarity threshold for market matching (0–1, default 0.85)
MATCH_SIMILARITY_THRESHOLD=0.85

# Categories to match for cross-platform arb (US users limited to sports,politics)
# Set to 'all' to disable filtering (non-US users)
POLYMARKET_CATEGORIES=sports,politics
```

> **US users:** Polymarket restricts US accounts to **sports and politics** markets only.
> The default `POLYMARKET_CATEGORIES=sports,politics` setting reflects this.
> Non-US users can set `POLYMARKET_CATEGORIES=all` to scan all categories.

> **Note:** Trading on Polymarket is not yet implemented.  Options 11/12 are
> read-only scanners that detect and log opportunities.

## Polymarket Sports Arbitrage (Same-Event Multi-Outcome)

Inspired by a Polymarket sports bot that earned $619K/year, this strategy runs
same-event multi-outcome arbitrage on binary sports markets within Polymarket
itself.

### Strategy

For each binary sports market (exactly 2 outcome tokens — e.g. Finland vs Switzerland):

- **YES arb**: If `ask(Finland) + ask(Switzerland) < 98¢`, buy YES on both.
  Exactly one outcome pays $1; after Polymarket's 2% fee that's 98¢, guaranteed profit.
- **NO arb**: If `no_ask(Finland) + no_ask(Switzerland) < 98¢`, buy NO on both.
  `no_ask(token) = 1 − best_bid(token)` — the cost of a synthetic NO position.

The 98¢ threshold (not 100¢) accounts for the 2% fee Polymarket charges on
winning payouts.

### Configuration

```env
POLYMARKET_FEE_PERCENT=2.0              # Polymarket charges 2% on winning payouts
POLYMARKET_SPORTS_MIN_PROFIT_PERCENT=0.5 # Min profit after fees (low margin, high volume)
POLYMARKET_SPORTS_SCAN_INTERVAL=45       # Seconds between scans (30-60s recommended)
POLYMARKET_SPORTS_MAX_POSITION_USD=5000  # Max USD per side ($3K-$9K is what the $619K bot used)
```

Use menu option **13** for a one-shot scan or option **14** for a continuous loop.

## Manifold Markets Arbitrage (Kalshi ↔ Manifold)

[Manifold Markets](https://manifold.markets) is a prediction market platform that is **fully
accessible for US users** with no trading restrictions.  Unlike Polymarket (iOS-only for US),
Manifold's API supports both reading market data and placing bets with just an API key.

### Why Manifold?

- **No US restrictions** — full API access for US accounts, including order placement
- **0% trading fees** — no fee drag on arbitrage profits
- **Sweepstakes markets** — real-money markets using Sweepcash (redeemable for USD)
- **AMM pricing** — prices lag Kalshi by 5–15¢ on the same events due to different
  user bases and AMM-based pricing, creating cross-platform opportunities

### Currencies

- **Mana (M$)** — play money used in most Manifold markets
- **Sweepcash (S$)** — real money (Sweepstakes markets), redeemable for USD

The scanner focuses on **Sweepstakes markets** (`MANIFOLD_SWEEPSTAKES_ONLY=true`) for
real-money arbitrage.

### Strategy

- **Buy YES on Kalshi + NO on Manifold** when `kalshi_yes_ask + manifold_no_price < 100¢`
- **Buy YES on Manifold + NO on Kalshi** when `manifold_yes_price + kalshi_no_ask < 100¢`

Since Manifold uses an AMM, the YES price is simply the market's `probability` field and
the NO price is `1 − probability`.  Both are compared to Kalshi prices in cents.

### Configuration

```env
# Get your API key: https://manifold.markets/profile (API tab)
MANIFOLD_API_KEY=
MANIFOLD_ENABLED=true
MANIFOLD_SWEEPSTAKES_ONLY=true       # Only scan Sweepcash markets (real money)
MANIFOLD_MIN_PROFIT_PERCENT=3.0      # Min cross-platform edge (no fees, but AMM slippage)
MANIFOLD_SCAN_INTERVAL=90            # Seconds between scans (AMM prices move slowly)
MANIFOLD_MAX_BET_USD=50              # Max per bet (liquidity is thin — start small)
MANIFOLD_CATEGORIES=all              # 'all', 'politics', 'sports', 'crypto', etc.
```

Use menu option **15** for a one-shot scan or option **16** for a continuous loop.

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
