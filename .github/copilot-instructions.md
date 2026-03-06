# Copilot Instructions for kalshi-arbitrage-bot

## Project Overview

This is a **Kalshi prediction market trading bot** that implements three core strategies:

1. **Arbitrage detection** – find mispriced markets where YES + NO prices sum to < 100¢
2. **Probability trading** – model the true probability of crypto outcomes (BTC/ETH price brackets) and trade when market prices diverge from model estimates
3. **Convergence trading** – near-expiry markets for crypto and weather where settlement becomes near-certain

Paper trading is the default. Live trading requires explicit opt-in via `ENABLE_LIVE_TRADING=true`.

---

## Architecture

### Key Files

| File | Purpose |
|------|---------|
| `kalshi_bot.py` | Backwards-compatible entry point; re-exports `KalshiAPI`, `KalshiArbitrageBot`, `KalshiTradingBot`, `main` |
| `api.py` | `KalshiAPI` class – all Kalshi REST API calls (auth, markets, orderbook, orders, balance) |
| `arbitrage.py` | `KalshiArbitrageBot` – detects and executes arbitrage opportunities |
| `trading.py` | `KalshiTradingBot` – position management, Kelly sizing, capital recycling |
| `cli.py` | `main()` – interactive CLI menu (options 1-15+) |
| `config.py` | `Config` class – all configuration values backed by environment variables |
| `storage.py` | SQLite persistence for trades, positions, and P&L via `TradeStorage` |
| `probability_trader.py` | `ProbabilityTrader` – GBM-based probability model for crypto interval markets |
| `convergence_trader.py` | `ConvergenceTrader` – near-expiry convergence strategy for crypto bracket markets |
| `kelly.py` | `kelly_fraction()` and `size_position()` – Kelly criterion position sizing |
| `notifications.py` | Email and webhook alert system |
| `ws_trader.py` | `WSConvergenceTrader` – WebSocket-based real-time convergence trader |
| `ws_price_feed.py` | `CryptoPriceFeed` – real-time BTC/ETH prices via Binance WebSocket |
| `cross_platform_arb.py` | Cross-platform arbitrage (Kalshi ↔ Polymarket) |
| `polymarket_api.py` | Polymarket CLOB API wrapper |
| `manifold_api.py` | Manifold Markets API wrapper |
| `manifold_arb.py` | Manifold ↔ Kalshi arbitrage |

---

## Conventions

### API Calls
- All Kalshi API methods go through `_request_with_retry()` in `api.py` — never call `requests.get/post` directly for Kalshi endpoints
- Always call `_ensure_auth()` before any authenticated request
- Rate limiting: respect `Config.RATE_LIMIT_DELAY` (default 0.3 s) between API calls

### Logging
- Use `logger = logging.getLogger('kalshi_bot')` at module level — **never use `print()` for operational messages**
- Use `logger.info()` for normal operational flow, `logger.debug()` for verbose detail, `logger.warning()` for recoverable issues, `logger.error()` for failures

### Configuration
- All config values come from the `Config` class in `config.py`
- Config values are backed by environment variables with safe defaults
- Access config via `Config.SOME_VALUE` or `self.config.SOME_VALUE` (when passed as constructor arg)
- **Never hardcode credentials, secrets, or URLs** that should be configurable

### Trading Values
- **Prices are in cents (int)**: a contract priced at 45¢ is represented as `45`
- **Balances are in USD (float)**: `$250.00` is `250.0`
- **Quantities are integers**: number of contracts

### Paper Trading
- `Config.PAPER_TRADING` defaults to `True` — paper mode is always the safe default
- `Config.LIVE_TRADING_ENABLED` defaults to `False` — must be explicitly enabled

### Price Caching
- HTTP price fetches (CoinGecko, etc.) are cached with TTL `Config.PRICE_CACHE_SECONDS` (default 10 s)
- WebSocket price feeds bypass this cache but expose `get_price_age_seconds()` to check staleness

---

## Safety Rules

> **These rules must never be violated. Do not remove or weaken them.**

1. **Never remove leg-risk protection in `execute_arbitrage()`** – if the NO order fails after a YES order is placed, the YES order must be cancelled immediately
2. **Never default `LIVE_TRADING_ENABLED` to `True`** – it must default to `False` in `Config` and `.env.example`
3. **Always check balance before placing orders** – confirm available funds cover the trade cost
4. **Always cancel YES order if NO order fails** – arbitrage is only safe when both legs execute

---

## Testing

Run the full test suite:

```bash
python -m pytest tests/
```

Run a specific test file:

```bash
python -m pytest tests/test_probability_trader.py -v
```

Tests use `unittest` style with `MockAPI` and `MockConfig` helper classes defined at the top of each test file. When adding new features, add corresponding tests in `tests/test_<module>.py`.
