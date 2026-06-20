# upstox-vwap

Nifty Options trading bots and NSE market-data tools built on the Upstox v2/v3 API.

## Bots

| Script | Purpose |
|--------|---------|
| `upstox_bot_0905.py` | Backtest + paper-live bot (no real orders) |
| `UPSTOX_LIVE1005.py` | Full live trader — real orders via Upstox HFT API |
| `nse_oi_fetcher.py` | Polls NSE option chain every 10s → MongoDB |
| `nse_oi_plotter.py` | Live OI chart from MongoDB |
| `dashboard_server.py` | Flask REST API + bhavcopy dashboard (`http://localhost:5050`) |

## Strategy

Runs on closed Nifty Futures candles (default 5-min). Entry requires two consecutive candles with bullish OI action (Long Buildup / Short Covering) for a CALL, or bearish OI action (Long Unwinding / Short Buildup) for a PUT.

Two entry modes:
- **STRONG** — price above EMA-7, EMA-21, and daily VWAP
- **PULLBACK** — price above EMA-7 only; exits at VWAP breach

Configurable no-trade zones, support/resistance levels, and hard price limits filter entries.

## Setup

```bash
pip install -r requirements.txt
# Additional for UPSTOX_LIVE1005.py
pip install pytz pymongo flask flask-cors
```

Copy the config template and fill in your credentials:

```bash
cp config.template.ini config.ini
```

Get an Upstox Bearer token from the [Upstox Developer Portal](https://developer.upstox.com/) and save it to `token.txt`. The token expires daily and must be regenerated each session.

Find instrument keys (`NSE_FO|XXXXX`) from the [Upstox instrument master](https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz). Update `Nifty_futures_key` every expiry.

## Usage

```bash
# Backtest over a date range (set from_date/to_date in config.ini)
python upstox_bot_0905.py backtest

# Paper live (polls every 15s, no real orders)
python upstox_bot_0905.py live

# Live trader — paper simulation (default when enable_live_orders=False)
python UPSTOX_LIVE1005.py

# Force paper mode
python UPSTOX_LIVE1005.py --paper

# Replay today's closed candles offline
python UPSTOX_LIVE1005.py --simulate

# NSE OI monitor (requires MongoDB)
python nse_oi_fetcher.py   # Terminal 1
python nse_oi_plotter.py   # Terminal 2

# Bhavcopy dashboard
python dashboard_server.py
```

## Configuration

All parameters live in `config.ini` (gitignored — copy from `config.template.ini`):

- `[UPSTOX]` — instrument keys and token file path
- `[SETTINGS]` — candle timeframe, poll interval, lot size
- `[ZONES]` — no-trade band, hard limits, S/R levels
- `[BACKTEST]` — date range
- `[LIVE_TRADING]` — `enable_live_orders`, auto square-off time, daily loss circuit breaker
- `[TELEGRAM]` — optional trade alerts

## Safety

`enable_live_orders = False` in config keeps the bot in paper simulation mode. When set to `True`, the bot requires typing `YES` at the console before placing any real orders. A daily loss circuit breaker (`max_daily_loss_rs`) stops new entries and exits open positions if the limit is hit.

## MongoDB

Required only for `nse_oi_fetcher.py`, `nse_oi_plotter.py`, `dashboard_server.py`, and trade logging in `UPSTOX_LIVE1005.py`.

```bash
mongod --dbpath /data/db
```

Databases used:
- `nse_oi_db` — option chain OI snapshots
- `NSE_DAILY` — bhavcopy EOD data and live trade log
