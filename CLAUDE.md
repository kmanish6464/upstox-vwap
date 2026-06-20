# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A collection of standalone Python scripts for trading Nifty Options on Indian markets. Each script is self-contained — there is no package structure or shared module. The two primary bots are:

- **`upstox_bot_0905.py`** — Backtest + paper-live bot (no real orders). Entry point: `python upstox_bot_0905.py backtest` or `python upstox_bot_0905.py live`
- **`UPSTOX_LIVE1005.py`** — Full live trading bot with Upstox order placement, position sync, circuit breakers. Entry point: `python UPSTOX_LIVE1005.py [--paper | --simulate]`

Supporting scripts:
- `nse_oi_fetcher.py` — Polls NSE option chain every 10s → MongoDB (`nse_oi_db`)
- `nse_oi_plotter.py` — Live chart from MongoDB OI data
- `dashboard_server.py` — Flask REST API + HTML dashboard for bhavcopy data (`http://localhost:5050`)

## Setup

```bash
pip install -r requirements.txt
pip install pytz pymongo flask flask-cors  # additional deps not in requirements.txt

cp config.template.ini config.ini          # then fill in credentials
```

**`token.txt`** — Upstox Bearer token. Expires daily; regenerate from the Upstox developer portal. A `401` from any Upstox API call means the token is stale.

**Instrument keys** (`NSE_FO|XXXXX`) — find the correct key from the [Upstox instrument master](https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz). Update `Nifty_futures_key` in `config.ini` every expiry.

## Architecture and key invariants

**Strategy logic** (same in both bots): Runs on closed candles of Nifty Futures. Computes EMA-7, EMA-21, and daily-reset VWAP. Classifies each candle's OI action as LB/SC (bullish) or LU/SB (bearish). Requires 2 consecutive bullish OI candles for a CALL entry, 2 bearish for a PUT entry. Two entry modes:
- **STRONG**: price > EMA7, EMA21, and VWAP → exits when close < previous close
- **PULLBACK**: price > EMA7 only → exits at VWAP breach OR close < previous close

**`PositionManager` class** (`UPSTOX_LIVE1005.py`): Central abstraction that tracks one open position at a time, handles paper vs live order routing, position sync against the broker, circuit breaker checks, and P&L accounting. All entry/exit logic goes through `open_call()`, `open_put()`, and `close_position()`.

**Instrument key encoding**: Keys like `NSE_FO|66071` must be URL-encoded (`NSE_FO%7C66071`) in all Upstox API URLs. Upstox silently returns empty data without the encoding. Both bots have a `_encode_key()` / `_encode()` helper for this.

**DataFrame columns**: Candles are stored as `[ts, o, h, l, c, v, oi]`. The `ts` column is always timezone-aware IST (`Asia/Kolkata`). VWAP computation groups by `df["ts"].dt.date` and cumsum per day.

**Live candle guard**: In live mode, the most recent candle from Upstox intraday is always dropped (`raw.iloc[:-1]`) because it is still forming. After resampling, any candle whose close time hasn't passed yet is also filtered out.

**NSE session warm-up** (`nse_oi_fetcher.py`): NSE blocks headless scrapers. The fetcher warms up a `requests.Session` by hitting the NSE homepage first to obtain cookies, then calls the option-chain API. On repeated `401`s, add a longer sleep in the warm-up or rotate the User-Agent.

**MongoDB databases**:
- `nse_oi_db` — collections `oi_snapshots` and `oi_summary` (NSE option chain OI)
- `NSE_DAILY` — collections `bhavcopy` (EOD data) and `live_trades` (UPSTOX_LIVE1005 trade log)

**Trade logs**: All completed trades are appended to CSV files (`nifty_trade_log.csv`, `live_trade_log.csv`) and optionally to MongoDB.

**Live-orders safety**: `ENABLE_LIVE_ORDERS = False` in config means the bot runs in paper simulation even if `--paper` is not passed. When `True`, the bot prompts for `YES` confirmation before placing real orders. Orders go to `https://api-hft.upstox.com/v3/order/place` (Upstox HFT endpoint).
