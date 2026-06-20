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

## Running the bots

```bash
# Install dependencies
pip install -r requirements.txt
# Also needed for UPSTOX_LIVE1005.py: pip install pytz pymongo

# Backtest (reads historical candles from Upstox API)
python upstox_bot_0905.py backtest

# Paper live (polls Upstox intraday data, no real orders)
python upstox_bot_0905.py live

# Live trader — paper simulation (default when enable_live_orders=False)
python UPSTOX_LIVE1005.py

# Live trader — force paper mode
python UPSTOX_LIVE1005.py --paper

# Replay today's closed candles offline
python UPSTOX_LIVE1005.py --simulate

# NSE OI monitor (requires MongoDB running)
python nse_oi_fetcher.py   # Terminal 1
python nse_oi_plotter.py   # Terminal 2

# Bhavcopy dashboard
python dashboard_server.py
```

## Configuration

All trading parameters live in **`config.ini`**. Key sections:

- `[UPSTOX]` — instrument keys (`NSE_FO|XXXXX` format) and `token_file`
- `[SETTINGS]` — candle timeframe (`1minute`/`5minute`/etc.), poll interval, lot size
- `[ZONES]` — no-trade band, hard limits, support/resistance levels
- `[BACKTEST]` — date range for historical runs
- `[LIVE_TRADING]` — `enable_live_orders`, auto square-off time, daily loss circuit breaker
- `[TELEGRAM]` — bot token, channel, `enable_telegram = False/True`

**`token.txt`** — Upstox Bearer token. Expires daily; regenerate from the Upstox developer portal. A `401` response from any Upstox API call means the token is stale.

## Architecture and key invariants

**Strategy logic** (same in both bots): Runs on closed candles of Nifty Futures. Computes EMA-7, EMA-21, and daily-reset VWAP. Classifies each candle's OI action as LB/SC (bullish) or LU/SB (bearish). Requires 2 consecutive bullish OI candles for a CALL entry, 2 bearish for a PUT entry. Two entry modes:
- **STRONG**: price > EMA7, EMA21, and VWAP → exits when close < previous close
- **PULLBACK**: price > EMA7 only → exits at VWAP breach OR close < previous close

**Instrument key encoding**: Keys like `NSE_FO|66071` must be URL-encoded (`NSE_FO%7C66071`) in all Upstox API URLs. Upstox silently returns empty data without the encoding. Both bots have a `_encode_key()` / `_encode()` helper for this.

**DataFrame columns**: Candles are stored as `[ts, o, h, l, c, v, oi]`. The `ts` column is always timezone-aware IST (`Asia/Kolkata`). VWAP computation groups by `df["ts"].dt.date` and cumsum per day.

**Live candle guard**: In live mode, the most recent candle from Upstox intraday is always dropped (`raw.iloc[:-1]`) because it is still forming. After resampling, any candle whose close time hasn't passed yet is also filtered out.

**MongoDB databases**:
- `nse_oi_db` — collections `oi_snapshots` and `oi_summary` (NSE option chain OI)
- `NSE_DAILY` — collections `bhavcopy` (EOD data) and `live_trades` (UPSTOX_LIVE1005 trade log)

**Trade logs**: All completed trades are appended to CSV files (`nifty_trade_log.csv`, `live_trade_log.csv`) and optionally to MongoDB.

**Live-orders safety**: `ENABLE_LIVE_ORDERS = False` in config means the bot runs in paper simulation even if `--paper` is not passed. When `True`, the bot prompts for `YES` confirmation before placing real orders. Orders go to `https://api-hft.upstox.com/v3/order/place` (Upstox HFT endpoint).
