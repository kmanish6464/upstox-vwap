"""
Nifty Options Trading Bot — Backtest + Live
============================================
Strategy:
  • Runs on CLOSED 5-min candles of Nifty Futures (configurable timeframe)
  • Computes EMA-7, EMA-21, VWAP, OI-Action per candle
  • OI Actions: LB=Long Buildup | SC=Short Covering | LU=Long Unwinding | SB=Short Buildup

BUY_CALL Rules:
  • Last 2 closed candles must be LB or SC
  • Price must NOT be at/above R1 or R2 (within buffer)
  • Price must NOT be inside the No-Trade Zone
  • Price must NOT be above Upper Limit
  • Strong entry  : price > EMA7, EMA21, VWAP → EXIT when close < prev-candle close
  • Pullback entry: price > EMA7 but < EMA21 or VWAP → EXIT at VWAP OR close < prev-candle close

BUY_PUT Rules:
  • Last 2 closed candles must be LU or SB
  • Price must NOT be at/below S1 or S2 (within buffer)
  • Price must NOT be inside the No-Trade Zone
  • Price must NOT be below Lower Limit
  • Strong entry  : price < EMA7, EMA21, VWAP → EXIT when close > prev-candle close
  • Pullback entry: price < EMA7 but > EMA21 or VWAP → EXIT at VWAP OR close > prev-candle close

Usage:
  python nifty_trading_bot.py backtest      # Fetch from Upstox API + run backtest
  python nifty_trading_bot.py backtest_db   # Read from MongoDB + run backtest (FAST)
  python nifty_trading_bot.py live          # Live market (polls Upstox every N sec)

MongoDB (for backtest_db):
  Run populate_nifty_mongo.py first to download candle history.
  DB: NSE_DAILY  |  Collection: candles  |  URI: mongodb://localhost:27017/
"""

import os
import sys
import csv
import json
import time
import logging
import configparser
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests
import pytz

try:
    from pymongo import MongoClient, ASCENDING
    MONGO_AVAILABLE = True
except ImportError:
    MONGO_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────
# 0.  LOGGING
# ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("nifty_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────────────────────────
# 1.  CONFIG LOADER
# ─────────────────────────────────────────────────────────────────

def load_config(path: str = "config.ini") -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    if not os.path.exists(path):
        log.error("config.ini not found at '%s'", path)
        sys.exit(1)
    cfg.read(path)
    return cfg


cfg = load_config()

# — UPSTOX —
TOKEN_FILE    = cfg.get("UPSTOX", "token_file",          fallback="token.txt")
BUY_KEY       = cfg.get("UPSTOX", "Buy_instrument_key",  fallback="NSE_FO|54765").strip()
SELL_KEY      = cfg.get("UPSTOX", "Sell_instrument_key", fallback="NSE_FO|54766").strip()
FUTURES_KEY   = cfg.get("UPSTOX", "Nifty_futures_key",   fallback="NSE_FO|57960").strip()
VIX_KEY       = cfg.get("UPSTOX", "vix_key",             fallback="NSE_INDEX|India VIX").strip()

# — SETTINGS —
TIMEFRAME     = cfg.get("SETTINGS",  "timeframe",     fallback="5minute").strip()
LOOP_INTERVAL = cfg.getint("SETTINGS", "loop_interval", fallback=15)
LOT_SIZE      = cfg.getint("SETTINGS", "lot_size",      fallback=25)
SLIPPAGE_PCT  = cfg.getfloat("SETTINGS", "slippage_pct", fallback=0.0005)

# — ZONES —
NT_UPPER      = cfg.getfloat("ZONES", "no_trade_upper", fallback=24100)
NT_LOWER      = cfg.getfloat("ZONES", "no_trade_lower", fallback=23900)
UPPER_LIMIT   = cfg.getfloat("ZONES", "upper_limit",    fallback=24500)
LOWER_LIMIT   = cfg.getfloat("ZONES", "lower_limit",    fallback=23500)
R1            = cfg.getfloat("ZONES", "r1",             fallback=23992)
R2            = cfg.getfloat("ZONES", "r2",             fallback=24150)
S1            = cfg.getfloat("ZONES", "s1",             fallback=23885)
S2            = cfg.getfloat("ZONES", "s2",             fallback=23700)
LVL_BUFFER    = cfg.getfloat("ZONES", "level_buffer",   fallback=15)

# — BACKTEST —
BT_FROM       = cfg.get("BACKTEST", "from_date", fallback="2025-04-01").strip()
BT_TO         = cfg.get("BACKTEST", "to_date",   fallback="2025-04-30").strip()

# — TELEGRAM —
BOT_TOKEN       = cfg.get("TELEGRAM", "bot_token",       fallback="").strip()
CHANNEL_ID      = cfg.get("TELEGRAM", "channel_id",      fallback="").strip()
ENABLE_TELEGRAM = cfg.getboolean("TELEGRAM", "enable_telegram", fallback=False)

# Pandas resample alias
TF_MAP = {
    "1minute": "1min", "3minute": "3min", "5minute": "5min",
    "15minute": "15min", "30minute": "30min",
}
PANDAS_TF = TF_MAP.get(TIMEFRAME, "5min")

UPSTOX_QUOTE = "https://api.upstox.com/v2/market-quote/quotes"
UPSTOX_HIST  = "https://api.upstox.com/v2/historical-candle"
TRADE_LOG    = "nifty_trade_log.csv"

# — MongoDB (for backtest_db mode + trade log persistence) —
MONGO_URI   = "mongodb://localhost:27017/"
MONGO_DB    = "NSE_DAILY"
MONGO_COL   = "candles"
MONGO_TRADES= "trade_logs"


# ─────────────────────────────────────────────────────────────────
# 2.  UTILITIES
# ─────────────────────────────────────────────────────────────────

def load_token() -> Optional[str]:
    if not os.path.exists(TOKEN_FILE):
        log.error("Token file '%s' missing.", TOKEN_FILE)
        return None
    with open(TOKEN_FILE) as fh:
        return fh.read().strip()


def auth_headers() -> dict:
    tok = load_token()
    if not tok:
        return {}
    return {"Authorization": f"Bearer {tok}", "Accept": "application/json"}


def now_ist_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def send_telegram(msg: str) -> None:
    if not ENABLE_TELEGRAM or not BOT_TOKEN:
        return
    url     = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHANNEL_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=5)
        if r.status_code != 200:
            log.warning("Telegram HTTP %s: %s", r.status_code, r.text[:150])
    except Exception as exc:
        log.error("Telegram error: %s", exc)


def log_trade_csv(row: dict) -> None:
    new_file = not os.path.isfile(TRADE_LOG)
    with open(TRADE_LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "mode", "entry_time", "exit_time", "side", "instrument",
            "entry_price", "exit_price", "qty", "pnl_pts", "pnl_rs",
            "exit_reason", "cumulative_pnl_rs",
        ])
        if new_file:
            w.writeheader()
        w.writerow(row)


# ─────────────────────────────────────────────────────────────────
# 3.  OI + INDICATORS
# ─────────────────────────────────────────────────────────────────

def oi_action(price_chg: float, oi_chg: float) -> str:
    """Classify each candle's OI action."""
    if   price_chg > 0 and oi_chg > 0: return "LB"   # Long Buildup
    elif price_chg < 0 and oi_chg > 0: return "SB"   # Short Buildup
    elif price_chg < 0 and oi_chg < 0: return "LU"   # Long Unwinding
    elif price_chg > 0 and oi_chg < 0: return "SC"   # Short Covering
    else:                               return "NE"   # Neutral


OI_LABEL = {"LB": "🟢 LongBuild", "SC": "🔵 ShrtCover",
            "LU": "🟡 LngUnwind", "SB": "🔴 ShrtBuild", "NE": "⚪ Neutral"}


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)

    df["ema7"]  = df["c"].ewm(span=7,  adjust=False).mean()
    df["ema21"] = df["c"].ewm(span=21, adjust=False).mean()

    # VWAP resets daily
    df["date"] = df["ts"].dt.date
    df["tp"]   = (df["h"] + df["l"] + df["c"]) / 3
    df["tpv"]  = df["tp"] * df["v"]
    df["cum_tpv"] = df.groupby("date")["tpv"].cumsum()
    df["cum_v"]   = df.groupby("date")["v"].cumsum()
    df["vwap"]    = df["cum_tpv"] / df["cum_v"]
    df.drop(columns=["tpv", "cum_tpv", "cum_v"], inplace=True)

    # OI action per candle
    df["oi_chg"]    = df["oi"].diff()
    df["price_chg"] = df["c"].diff()
    df["oi_act"]    = df.apply(
        lambda r: oi_action(r["price_chg"], r["oi_chg"]), axis=1
    )
    return df


# ─────────────────────────────────────────────────────────────────
# 4.  ZONE / LEVEL CHECKS
# ─────────────────────────────────────────────────────────────────

def in_no_trade_zone(price: float) -> bool:
    return NT_LOWER <= price <= NT_UPPER


def at_resistance(price: float) -> bool:
    return (abs(price - R1) <= LVL_BUFFER or
            abs(price - R2) <= LVL_BUFFER or
            price >= R2 + LVL_BUFFER)      # above R2 also blocked for calls


def at_support(price: float) -> bool:
    return (abs(price - S1) <= LVL_BUFFER or
            abs(price - S2) <= LVL_BUFFER or
            price <= S2 - LVL_BUFFER)      # below S2 also blocked for puts


def beyond_limits(price: float) -> bool:
    return price >= UPPER_LIMIT or price <= LOWER_LIMIT


def call_entry_allowed(price: float) -> tuple[bool, str]:
    """Returns (allowed, reason_if_blocked)."""
    if beyond_limits(price):
        return False, f"Beyond hard limits ({LOWER_LIMIT}–{UPPER_LIMIT})"
    if in_no_trade_zone(price):
        return False, f"No-Trade Zone ({NT_LOWER}–{NT_UPPER})"
    if at_resistance(price):
        return False, f"At/Above Resistance (R1={R1}, R2={R2})"
    return True, ""


def put_entry_allowed(price: float) -> tuple[bool, str]:
    if beyond_limits(price):
        return False, f"Beyond hard limits ({LOWER_LIMIT}–{UPPER_LIMIT})"
    if in_no_trade_zone(price):
        return False, f"No-Trade Zone ({NT_LOWER}–{NT_UPPER})"
    if at_support(price):
        return False, f"At/Below Support (S1={S1}, S2={S2})"
    return True, ""


# ─────────────────────────────────────────────────────────────────
# 5.  SIGNAL LOGIC
# ─────────────────────────────────────────────────────────────────

BULLISH_OI = {"LB", "SC"}
BEARISH_OI = {"LU", "SB"}


def check_call_signal(curr: pd.Series, prev: pd.Series,
                      prev2: pd.Series) -> tuple[str, str]:
    """
    Returns (signal_type, entry_mode)
    signal_type : 'STRONG' | 'PULLBACK' | ''
    entry_mode  : 'STRONG' | 'PULLBACK' | ''
    """
    # Last 2 closed candles must be bullish OI
    if curr["oi_act"]  not in BULLISH_OI: return "", ""
    if prev["oi_act"]  not in BULLISH_OI: return "", ""

    price = curr["c"]
    ok, _ = call_entry_allowed(price)
    if not ok:
        return "", ""

    above_ema7  = price > curr["ema7"]
    above_ema21 = price > curr["ema21"]
    above_vwap  = price > curr["vwap"]

    if above_ema7 and above_ema21 and above_vwap:
        return "BUY_CALL", "STRONG"
    if above_ema7:                              # price > ema7 but below ema21 or vwap
        return "BUY_CALL", "PULLBACK"
    return "", ""


def check_put_signal(curr: pd.Series, prev: pd.Series,
                     prev2: pd.Series) -> tuple[str, str]:
    if curr["oi_act"] not in BEARISH_OI: return "", ""
    if prev["oi_act"] not in BEARISH_OI: return "", ""

    price = curr["c"]
    ok, _ = put_entry_allowed(price)
    if not ok:
        return "", ""

    below_ema7  = price < curr["ema7"]
    below_ema21 = price < curr["ema21"]
    below_vwap  = price < curr["vwap"]

    if below_ema7 and below_ema21 and below_vwap:
        return "BUY_PUT", "STRONG"
    if below_ema7:
        return "BUY_PUT", "PULLBACK"
    return "", ""


def check_call_exit(curr: pd.Series, prev: pd.Series, mode: str) -> tuple[bool, str]:
    """CALL exit: price < prev candle close (both modes) OR price < VWAP (pullback only)."""
    if curr["c"] < prev["c"]:
        return True, "Close < Prev Close"
    if mode == "PULLBACK" and curr["c"] < curr["vwap"]:
        return True, "Close < VWAP (pullback exit)"
    return False, ""


def check_put_exit(curr: pd.Series, prev: pd.Series, mode: str) -> tuple[bool, str]:
    """PUT exit: price > prev candle close (both modes) OR price > VWAP (pullback only)."""
    if curr["c"] > prev["c"]:
        return True, "Close > Prev Close"
    if mode == "PULLBACK" and curr["c"] > curr["vwap"]:
        return True, "Close > VWAP (pullback exit)"
    return False, ""


# ─────────────────────────────────────────────────────────────────
# 6.  DATA FETCHERS
# ─────────────────────────────────────────────────────────────────

def _encode_key(key: str) -> str:
    """
    URL-encode the instrument key so the pipe | becomes %7C.
    NSE_FO|66071  ->  NSE_FO%7C66071
    Upstox silently returns empty data when the pipe is not encoded.
    """
    from urllib.parse import quote
    return quote(key, safe="")


def fetch_intraday(key: str, headers: dict) -> Optional[pd.DataFrame]:
    """Fetch today's intraday 1-min candles (live mode)."""
    enc = _encode_key(key)
    url = f"{UPSTOX_HIST}/intraday/{enc}/1minute"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        log.debug("Intraday HTTP %s | URL: %s", r.status_code, url)
        if r.status_code != 200:
            log.error("Intraday fetch HTTP %s: %s", r.status_code, r.text[:300])
            return None
        payload = r.json()
        candles = payload.get("data", {}).get("candles", [])
        if not candles:
            log.warning("Intraday: empty candles. Response: %s", r.text[:200])
            return None
        return _build_df(candles)
    except Exception as exc:
        log.error("fetch_intraday error: %s", exc)
        return None


def fetch_historical(key: str, headers: dict,
                     from_date: str, to_date: str) -> Optional[pd.DataFrame]:
    """
    Fetch historical 1-min candles day-by-day and merge.

    Upstox endpoint (v2):
      GET /v2/historical-candle/{encoded_key}/1minute/{to_date}/{from_date}

    IMPORTANT:
      - Pipe in key MUST be URL-encoded (%7C) — without it API returns empty data.
      - to_date and from_date can be the same for a single day.
      - Upstox stores ~1 year of intraday history.
    """
    all_candles = []
    start = datetime.strptime(from_date, "%Y-%m-%d")
    end   = datetime.strptime(to_date,   "%Y-%m-%d")
    cur   = start
    enc   = _encode_key(key)

    log.info("Encoded futures key: %s", enc)

    while cur <= end:
        if cur.weekday() < 5:          # Mon-Fri only
            ds  = cur.strftime("%Y-%m-%d")
            # Format: /{key}/{interval}/{to}/{from}  (same date = single day)
            url = f"{UPSTOX_HIST}/{enc}/1minute/{ds}/{ds}"
            try:
                r = requests.get(url, headers=headers, timeout=10)
                if r.status_code == 200:
                    candles = r.json().get("data", {}).get("candles", [])
                    all_candles.extend(candles)
                    log.info("  %s -> %d candles  [URL: ...%s]",
                             ds, len(candles), url[-40:])
                    if len(candles) == 0:
                        log.debug("  Raw: %s", r.text[:300])
                elif r.status_code == 401:
                    log.error("  401 Unauthorised — token expired. Regenerate token.txt.")
                    break
                elif r.status_code == 400:
                    log.warning("  400 Bad Request for %s: %s", ds, r.text[:200])
                else:
                    log.warning("  HTTP %s for %s: %s", r.status_code, ds, r.text[:100])
            except Exception as exc:
                log.error("  Error for %s: %s", ds, exc)
            time.sleep(0.35)
        cur += timedelta(days=1)

    if not all_candles:
        log.error(
            "Zero candles collected. Common causes:\n"
            "  1. Token expired -> regenerate from Upstox developer portal.\n"
            "  2. Wrong futures key in config.ini [UPSTOX] Nifty_futures_key.\n"
            "     Check https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz\n"
            "  3. Date range has no data (holidays / wrong contract month).\n"
            "  4. Upstox historical API stores ~1 year of 1-min data only."
        )
        return None
    return _build_df(all_candles)


def _build_df(candles: list) -> pd.DataFrame:
    df = pd.DataFrame(candles, columns=["ts", "o", "h", "l", "c", "v", "oi"])
    df["ts"] = pd.to_datetime(df["ts"])
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize("UTC").dt.tz_convert(IST)
    else:
        df["ts"] = df["ts"].dt.tz_convert(IST)
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def resample_to_tf(df: pd.DataFrame) -> pd.DataFrame:
    """Resample 1-min candles to target timeframe."""
    if PANDAS_TF == "1min":
        return df
    df2 = df.set_index("ts").resample(PANDAS_TF).agg(
        {"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum", "oi": "last"}
    ).dropna().reset_index()
    return df2


def get_ltp(instrument_key: str, headers: dict) -> Optional[float]:
    try:
        r = requests.get(UPSTOX_QUOTE, headers=headers,
                         params={"instrument_key": instrument_key}, timeout=5)
        if r.status_code == 200:
            data  = r.json().get("data", {})
            entry = next(iter(data.values()), {})
            ltp   = (entry.get("last_price") or entry.get("ltp") or entry.get("close_price"))
            return float(ltp) if ltp else None
    except Exception as exc:
        log.error("get_ltp error: %s", exc)
    return None


# ─────────────────────────────────────────────────────────────────
# 7.  BACKTEST ENGINE
# ─────────────────────────────────────────────────────────────────

def run_backtest():
    log.info("=" * 70)
    log.info("BACKTEST MODE  |  %s → %s  |  TF: %s", BT_FROM, BT_TO, TIMEFRAME)
    log.info("Futures Key : %s", FUTURES_KEY)
    log.info("Zones: NT=[%s–%s]  R1=%s R2=%s  S1=%s S2=%s",
             NT_LOWER, NT_UPPER, R1, R2, S1, S2)
    log.info("=" * 70)

    hdrs = auth_headers()
    if not hdrs:
        log.error("No auth token — cannot run backtest.")
        return

    # ── Pre-flight: validate token + key before slow date loop ──
    log.info("Pre-flight check: token + futures key...")
    from urllib.parse import quote
    enc_key = quote(FUTURES_KEY, safe="")
    test_url = f"{UPSTOX_HIST}/{enc_key}/1minute/{BT_FROM}/{BT_FROM}"
    try:
        tr = requests.get(test_url, headers=hdrs, timeout=10)
        if tr.status_code == 401:
            log.error("Token EXPIRED or INVALID (HTTP 401). "
                      "Please regenerate token.txt from Upstox developer portal.")
            return
        elif tr.status_code == 400:
            log.error("Bad Request (HTTP 400) — likely wrong futures key.\n"
                      "  Key used   : %s\n"
                      "  Encoded    : %s\n"
                      "  API says   : %s\n"
                      "  Fix: update Nifty_futures_key in config.ini with the correct "
                      "NSE_FO|XXXXX from Upstox instrument master.", FUTURES_KEY, enc_key, tr.text[:200])
            return
        elif tr.status_code == 200:
            sample = tr.json().get("data", {}).get("candles", [])
            log.info("Pre-flight OK. Sample candles on %s: %d", BT_FROM, len(sample))
        else:
            log.warning("Pre-flight HTTP %s: %s", tr.status_code, tr.text[:150])
    except Exception as e:
        log.error("Pre-flight request failed: %s", e)
        return

    log.info("Fetching historical data from Upstox...")
    raw = fetch_historical(FUTURES_KEY, hdrs, BT_FROM, BT_TO)
    if raw is None or raw.empty:
        log.error("No data returned. Check token, dates, and futures key.")
        return

    log.info("Total 1-min candles fetched: %d", len(raw))
    df = resample_to_tf(raw)
    log.info("Candles after resampling to %s: %d", TIMEFRAME, len(df))

    df = add_indicators(df)

    # ─── State ───
    trade_side   = None          # 'CALL' | 'PUT'
    entry_price  = 0.0
    entry_time   = None
    entry_mode   = ""            # 'STRONG' | 'PULLBACK'
    trade_log    = []
    cumulative   = 0.0

    HDR = (f"\n{'TIME':<6} {'PRICE':>8} {'EMA7':>8} {'EMA21':>8} "
           f"{'VWAP':>8} {'OI':>11} {'SIGNAL':<22} {'P&L':>9}")
    print(HDR)
    print("-" * 90)

    for i in range(2, len(df)):
        curr  = df.iloc[i]
        prev  = df.iloc[i - 1]
        prev2 = df.iloc[i - 2]

        ts_str = curr["ts"].strftime("%H:%M")
        oi_lbl = OI_LABEL.get(curr["oi_act"], curr["oi_act"])

        signal_txt = ""
        pnl_txt    = ""

        # ─── EXIT CHECK ─────────────────────────────────────────
        if trade_side == "CALL":
            exited, reason = check_call_exit(curr, prev, entry_mode)
            if exited:
                pnl_pts  = curr["c"] - entry_price
                pnl_rs   = pnl_pts * LOT_SIZE
                cumulative += pnl_rs
                signal_txt  = f"🛑 EXIT CALL ({reason[:18]})"
                pnl_txt     = f"{pnl_pts:+.1f}pts ₹{pnl_rs:+.0f}"
                trade_log.append({
                    "mode": "backtest",
                    "entry_time": entry_time, "exit_time": ts_str,
                    "side": "CALL",   "instrument": BUY_KEY,
                    "entry_price": round(entry_price, 2),
                    "exit_price":  round(curr["c"], 2),
                    "qty": LOT_SIZE,
                    "pnl_pts":    round(pnl_pts, 2),
                    "pnl_rs":     round(pnl_rs, 2),
                    "exit_reason": reason,
                    "cumulative_pnl_rs": round(cumulative, 2),
                })
                trade_side  = None
                entry_price = 0.0

        elif trade_side == "PUT":
            exited, reason = check_put_exit(curr, prev, entry_mode)
            if exited:
                pnl_pts  = entry_price - curr["c"]   # profit when futures falls
                pnl_rs   = pnl_pts * LOT_SIZE
                cumulative += pnl_rs
                signal_txt  = f"🛑 EXIT PUT  ({reason[:18]})"
                pnl_txt     = f"{pnl_pts:+.1f}pts ₹{pnl_rs:+.0f}"
                trade_log.append({
                    "mode": "backtest",
                    "entry_time": entry_time, "exit_time": ts_str,
                    "side": "PUT",    "instrument": SELL_KEY,
                    "entry_price": round(entry_price, 2),
                    "exit_price":  round(curr["c"], 2),
                    "qty": LOT_SIZE,
                    "pnl_pts":    round(pnl_pts, 2),
                    "pnl_rs":     round(pnl_rs, 2),
                    "exit_reason": reason,
                    "cumulative_pnl_rs": round(cumulative, 2),
                })
                trade_side  = None
                entry_price = 0.0

        # ─── ENTRY CHECK (only if flat) ──────────────────────────
        if trade_side is None:
            sig, mode = check_call_signal(curr, prev, prev2)
            if sig == "BUY_CALL":
                trade_side  = "CALL"
                entry_price = curr["c"]
                entry_time  = ts_str
                entry_mode  = mode
                signal_txt  = f"🟢 BUY CALL ({mode})"

            else:
                sig, mode = check_put_signal(curr, prev, prev2)
                if sig == "BUY_PUT":
                    trade_side  = "PUT"
                    entry_price = curr["c"]
                    entry_time  = ts_str
                    entry_mode  = mode
                    signal_txt  = f"🔴 BUY PUT  ({mode})"

        # ─── CONSOLE ROW ────────────────────────────────────────
        if signal_txt:
            print(
                f"{ts_str:<6} {curr['c']:>8.1f} {curr['ema7']:>8.1f} "
                f"{curr['ema21']:>8.1f} {curr['vwap']:>8.1f} "
                f"{oi_lbl:>11} {signal_txt:<22} {pnl_txt:>9}"
            )

    # ─── Write CSV ───────────────────────────────────────────────
    for row in trade_log:
        log_trade_csv(row)

    # ─── Performance Summary ─────────────────────────────────────
    if not trade_log:
        print("\nNo completed trades found.")
        return

    th     = pd.DataFrame(trade_log)
    total  = len(th)
    wins   = (th["pnl_rs"] > 0).sum()
    losses = total - wins
    net    = th["pnl_rs"].sum()
    best   = th["pnl_rs"].max()
    worst  = th["pnl_rs"].min()
    wr     = wins / total * 100
    avg    = th["pnl_rs"].mean()

    calls  = th[th["side"] == "CALL"]
    puts   = th[th["side"] == "PUT"]

    print("\n" + "=" * 55)
    print(f"  BACKTEST SUMMARY  ({BT_FROM} → {BT_TO})")
    print("=" * 55)
    print(f"  Timeframe      : {TIMEFRAME}")
    print(f"  Lot Size       : {LOT_SIZE}")
    print(f"  Total Trades   : {total}  (CALL={len(calls)} | PUT={len(puts)})")
    print(f"  Wins / Losses  : {wins} ✅  / {losses} ❌")
    print(f"  Win Rate       : {wr:.1f}%")
    print(f"  Net P&L (₹)   : ₹{net:+,.0f}")
    print(f"  Avg P&L (₹)   : ₹{avg:+,.0f}")
    print(f"  Best Trade (₹) : ₹{best:+,.0f}")
    print(f"  Worst Trade(₹) : ₹{worst:+,.0f}")
    print(f"  Log saved to   : {TRADE_LOG}")
    print("=" * 55)


# ─────────────────────────────────────────────────────────────────
# 8.  LIVE TRADING ENGINE
# ─────────────────────────────────────────────────────────────────

def run_live():
    log.info("=" * 70)
    log.info("LIVE MODE  |  TF: %s  |  Poll every %ss", TIMEFRAME, LOOP_INTERVAL)
    log.info("Futures Key : %s", FUTURES_KEY)
    log.info("BUY key     : %s  |  SELL key: %s", BUY_KEY, SELL_KEY)
    log.info("Zones: NT=[%s–%s]  R1=%s R2=%s  S1=%s S2=%s",
             NT_LOWER, NT_UPPER, R1, R2, S1, S2)
    log.info("=" * 70)

    hdrs = auth_headers()
    if not hdrs:
        log.error("No auth token — cannot run live mode.")
        return

    # — Live State —
    trade_side        = None
    entry_price       = 0.0
    entry_time        = None
    entry_mode        = ""
    last_candle_ts    = None
    cumulative_pnl_rs = 0.0
    trade_count       = 0

    print(f"\n{'TIME':<6} {'PRICE':>8} {'EMA7':>8} {'EMA21':>8} "
          f"{'VWAP':>8} {'OI':>11} {'SIGNAL':<22} {'P&L':>9}")
    print("-" * 90)

    send_telegram(
        f"🤖 *Nifty Bot STARTED (Live)*\n"
        f"TF: `{TIMEFRAME}` | Key: `{FUTURES_KEY}`\n"
        f"NT Zone: {NT_LOWER}–{NT_UPPER}\n"
        f"R1={R1} R2={R2} | S1={S1} S2={S2}\n"
        f"Time: {now_ist_str()}"
    )

    while True:
        loop_start = time.time()
        try:
            raw = fetch_intraday(FUTURES_KEY, hdrs)
            if raw is None or len(raw) < 3:
                log.warning("Insufficient data; retrying...")
                time.sleep(LOOP_INTERVAL)
                continue

            # Drop the live (incomplete) candle
            raw = raw.iloc[:-1].copy()

            df = resample_to_tf(raw)

            # Also drop last resampled candle if it may be forming
            now_ist_dt = datetime.now(IST)
            df = df[df["ts"] + pd.Timedelta(PANDAS_TF) <= now_ist_dt].copy()

            if len(df) < 3:
                log.info("Waiting for enough closed candles...")
                time.sleep(LOOP_INTERVAL)
                continue

            df = add_indicators(df)

            curr  = df.iloc[-1]
            prev  = df.iloc[-2]
            prev2 = df.iloc[-3]

            # Skip if already processed this candle
            if last_candle_ts is not None and curr["ts"] <= last_candle_ts:
                elapsed = time.time() - loop_start
                time.sleep(max(0, LOOP_INTERVAL - elapsed))
                continue

            last_candle_ts = curr["ts"]
            ts_str = curr["ts"].strftime("%H:%M")
            oi_lbl = OI_LABEL.get(curr["oi_act"], curr["oi_act"])

            signal_txt = ""
            pnl_txt    = ""

            # ─── EXIT CHECK ──────────────────────────────────────
            if trade_side == "CALL":
                exited, reason = check_call_exit(curr, prev, entry_mode)
                if exited:
                    ltp     = get_ltp(BUY_KEY, hdrs) or curr["c"]
                    exit_px = ltp * (1 - SLIPPAGE_PCT)
                    pnl_pts = exit_px - entry_price
                    pnl_rs  = pnl_pts * LOT_SIZE
                    cumulative_pnl_rs += pnl_rs
                    trade_count       += 1
                    signal_txt = f"🛑 EXIT CALL ({reason[:18]})"
                    pnl_txt    = f"{pnl_pts:+.1f}pts ₹{pnl_rs:+.0f}"

                    row = {
                        "mode": "live",
                        "entry_time": entry_time, "exit_time": ts_str,
                        "side": "CALL", "instrument": BUY_KEY,
                        "entry_price": round(entry_price, 2),
                        "exit_price":  round(exit_px, 2),
                        "qty": LOT_SIZE,
                        "pnl_pts":    round(pnl_pts, 2),
                        "pnl_rs":     round(pnl_rs, 2),
                        "exit_reason": reason,
                        "cumulative_pnl_rs": round(cumulative_pnl_rs, 2),
                    }
                    log_trade_csv(row)

                    send_telegram(
                        f"🛑 *EXIT CALL* {'✅' if pnl_rs >= 0 else '🔴'}\n"
                        f"Exit  : {exit_px:.2f}\n"
                        f"P&L   : ₹{pnl_rs:+.0f} ({pnl_pts:+.1f} pts)\n"
                        f"Cumul : ₹{cumulative_pnl_rs:+.0f}\n"
                        f"Reason: {reason}\n"
                        f"Time  : {ts_str}"
                    )
                    trade_side  = None
                    entry_price = 0.0

            elif trade_side == "PUT":
                exited, reason = check_put_exit(curr, prev, entry_mode)
                if exited:
                    ltp     = get_ltp(SELL_KEY, hdrs) or curr["c"]
                    exit_px = ltp * (1 + SLIPPAGE_PCT)
                    pnl_pts = entry_price - exit_px
                    pnl_rs  = pnl_pts * LOT_SIZE
                    cumulative_pnl_rs += pnl_rs
                    trade_count       += 1
                    signal_txt = f"🛑 EXIT PUT  ({reason[:18]})"
                    pnl_txt    = f"{pnl_pts:+.1f}pts ₹{pnl_rs:+.0f}"

                    row = {
                        "mode": "live",
                        "entry_time": entry_time, "exit_time": ts_str,
                        "side": "PUT", "instrument": SELL_KEY,
                        "entry_price": round(entry_price, 2),
                        "exit_price":  round(exit_px, 2),
                        "qty": LOT_SIZE,
                        "pnl_pts":    round(pnl_pts, 2),
                        "pnl_rs":     round(pnl_rs, 2),
                        "exit_reason": reason,
                        "cumulative_pnl_rs": round(cumulative_pnl_rs, 2),
                    }
                    log_trade_csv(row)

                    send_telegram(
                        f"🛑 *EXIT PUT* {'✅' if pnl_rs >= 0 else '🔴'}\n"
                        f"Exit  : {exit_px:.2f}\n"
                        f"P&L   : ₹{pnl_rs:+.0f} ({pnl_pts:+.1f} pts)\n"
                        f"Cumul : ₹{cumulative_pnl_rs:+.0f}\n"
                        f"Reason: {reason}\n"
                        f"Time  : {ts_str}"
                    )
                    trade_side  = None
                    entry_price = 0.0

            # ─── ENTRY CHECK ─────────────────────────────────────
            if trade_side is None:
                sig, mode = check_call_signal(curr, prev, prev2)
                if sig == "BUY_CALL":
                    ltp        = get_ltp(BUY_KEY, hdrs) or curr["c"]
                    entry_price = ltp * (1 + SLIPPAGE_PCT)
                    entry_time  = ts_str
                    entry_mode  = mode
                    trade_side  = "CALL"
                    signal_txt  = f"🟢 BUY CALL ({mode})"

                    send_telegram(
                        f"🟢 *BUY CALL* ({mode})\n"
                        f"Entry : ₹{entry_price:.2f}\n"
                        f"EMA7  : {curr['ema7']:.1f} | EMA21: {curr['ema21']:.1f}\n"
                        f"VWAP  : {curr['vwap']:.1f}\n"
                        f"OI    : {oi_lbl}\n"
                        f"Time  : {ts_str}"
                    )

                else:
                    sig, mode = check_put_signal(curr, prev, prev2)
                    if sig == "BUY_PUT":
                        ltp         = get_ltp(SELL_KEY, hdrs) or curr["c"]
                        entry_price = ltp * (1 - SLIPPAGE_PCT)
                        entry_time  = ts_str
                        entry_mode  = mode
                        trade_side  = "PUT"
                        signal_txt  = f"🔴 BUY PUT  ({mode})"

                        send_telegram(
                            f"🔴 *BUY PUT* ({mode})\n"
                            f"Entry : ₹{entry_price:.2f}\n"
                            f"EMA7  : {curr['ema7']:.1f} | EMA21: {curr['ema21']:.1f}\n"
                            f"VWAP  : {curr['vwap']:.1f}\n"
                            f"OI    : {oi_lbl}\n"
                            f"Time  : {ts_str}"
                        )

            # ─── BLOCKED SIGNAL (for visibility) ─────────────────
            if signal_txt == "":
                price = curr["c"]
                # check if a potential call was blocked
                if (curr["oi_act"] in BULLISH_OI and prev["oi_act"] in BULLISH_OI
                        and price > curr["ema7"] and trade_side is None):
                    ok, blk_reason = call_entry_allowed(price)
                    if not ok:
                        signal_txt = f"⛔ CALL BLOCKED"
                        pnl_txt    = blk_reason[:18]

                elif (curr["oi_act"] in BEARISH_OI and prev["oi_act"] in BEARISH_OI
                      and price < curr["ema7"] and trade_side is None):
                    ok, blk_reason = put_entry_allowed(price)
                    if not ok:
                        signal_txt = f"⛔ PUT  BLOCKED"
                        pnl_txt    = blk_reason[:18]

                elif trade_side:
                    signal_txt = f"{'📈' if trade_side=='CALL' else '📉'} HOLDING {trade_side}"
                    open_pnl = ((curr["c"] - entry_price) if trade_side == "CALL"
                                else (entry_price - curr["c"]))
                    pnl_txt  = f"{open_pnl:+.1f}pts"

            # ─── CONSOLE PRINT ───────────────────────────────────
            print(
                f"\r{ts_str:<6} {curr['c']:>8.1f} {curr['ema7']:>8.1f} "
                f"{curr['ema21']:>8.1f} {curr['vwap']:>8.1f} "
                f"{oi_lbl:>11} {signal_txt:<22} {pnl_txt:>9}",
                end="" if signal_txt.startswith("📈") or signal_txt.startswith("📉") else "\n"
            )
            sys.stdout.flush()

        except KeyboardInterrupt:
            print("\n\n⏹  Bot stopped by user.")
            log.info("Live bot stopped. Cumulative P&L: ₹%+.0f | Trades: %d",
                     cumulative_pnl_rs, trade_count)
            send_telegram(
                f"⏹ *Bot Stopped*\n"
                f"Cumul P&L : ₹{cumulative_pnl_rs:+.0f}\n"
                f"Trades    : {trade_count}\n"
                f"Time      : {now_ist_str()}"
            )
            break
        except Exception as exc:
            log.error("Live loop error: %s", exc)

        elapsed = time.time() - loop_start
        time.sleep(max(0, LOOP_INTERVAL - elapsed))


# ─────────────────────────────────────────────────────────────────
# 9.  MONGODB UTILITIES
# ─────────────────────────────────────────────────────────────────

def get_mongo_col(collection: str = MONGO_COL):
    """Return (client, collection). Caller must close client."""
    if not MONGO_AVAILABLE:
        raise RuntimeError("pymongo not installed. Run: pip install pymongo")
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=4000)
    client.server_info()   # raises if not reachable
    return client, client[MONGO_DB][collection]


def fetch_from_mongo(key: str, from_date: str, to_date: str) -> Optional[pd.DataFrame]:
    """
    Load 1-min candles from MongoDB for a given key + date range.
    Returns a DataFrame identical to what fetch_historical() returns,
    so the same resample/indicator pipeline applies.
    """
    try:
        client, col = get_mongo_col()
    except Exception as exc:
        log.error("MongoDB connect failed: %s", exc)
        return None

    from_dt = datetime.strptime(from_date, "%Y-%m-%d")
    to_dt   = datetime.strptime(to_date,   "%Y-%m-%d") + timedelta(hours=23, minutes=59)

    cursor = col.find(
        {"instrument_key": key,
         "ts": {"$gte": from_dt, "$lte": to_dt}},
        {"_id": 0, "ts": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "oi": 1},
        sort=[("ts", ASCENDING)]
    )
    docs = list(cursor)
    client.close()

    if not docs:
        log.error("MongoDB returned 0 documents for key=%s  %s→%s", key, from_date, to_date)
        return None

    df = pd.DataFrame(docs)
    # ts stored as UTC-naive datetime in MongoDB — localise to IST
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize("UTC").dt.tz_convert(IST)
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    log.info("Loaded %d candles from MongoDB for %s", len(df), key)
    return df


def save_trade_to_mongo(row: dict) -> None:
    """Append a completed trade record to MongoDB trade_logs collection."""
    try:
        client, col = get_mongo_col(MONGO_TRADES)
        doc = dict(row)
        doc["saved_at"] = datetime.utcnow()
        col.insert_one(doc)
        client.close()
    except Exception as exc:
        log.warning("MongoDB trade log save failed: %s", exc)


# ─────────────────────────────────────────────────────────────────
# 10.  BACKTEST_DB ENGINE  (reads from MongoDB instead of API)
# ─────────────────────────────────────────────────────────────────

def run_backtest_db():
    """
    Identical strategy to run_backtest() but sources candles from
    MongoDB (NSE_DAILY.candles) instead of fetching from Upstox API.

    Prerequisites:
      Run populate_nifty_mongo.py first to fill the database.
      The Nifty_futures_key in config.ini must match a stored instrument_key.
    """
    log.info("=" * 70)
    log.info("BACKTEST_DB MODE  |  %s → %s  |  TF: %s", BT_FROM, BT_TO, TIMEFRAME)
    log.info("Futures Key : %s", FUTURES_KEY)
    log.info("Source      : MongoDB  %s / %s.%s", MONGO_URI, MONGO_DB, MONGO_COL)
    log.info("Zones: NT=[%s–%s]  R1=%s R2=%s  S1=%s S2=%s",
             NT_LOWER, NT_UPPER, R1, R2, S1, S2)
    log.info("=" * 70)

    if not MONGO_AVAILABLE:
        log.error("pymongo is not installed. Run:  pip install pymongo")
        return

    raw = fetch_from_mongo(FUTURES_KEY, BT_FROM, BT_TO)
    if raw is None or raw.empty:
        log.error(
            "No data in MongoDB for key=%s  %s→%s\n"
            "  Run:  python populate_nifty_mongo.py --key %s --from %s --to %s",
            FUTURES_KEY, BT_FROM, BT_TO, FUTURES_KEY, BT_FROM, BT_TO
        )
        return

    log.info("1-min candles loaded : %d", len(raw))
    df = resample_to_tf(raw)
    log.info("Candles after %s resample: %d", TIMEFRAME, len(df))
    df = add_indicators(df)

    # ─── Identical engine to run_backtest() ───────────────────────
    trade_side  = None
    entry_price = 0.0
    entry_time  = None
    entry_mode  = ""
    trade_log   = []
    cumulative  = 0.0

    HDR = (f"\n{'DATE':<10} {'TIME':<6} {'PRICE':>8} {'EMA7':>8} {'EMA21':>8} "
           f"{'VWAP':>8} {'OI':>11} {'SIGNAL':<22} {'P&L':>9}")
    print(HDR)
    print("-" * 100)

    for i in range(2, len(df)):
        curr  = df.iloc[i]
        prev  = df.iloc[i - 1]
        prev2 = df.iloc[i - 2]

        date_str = curr["ts"].strftime("%Y-%m-%d")
        ts_str   = curr["ts"].strftime("%H:%M")
        oi_lbl   = OI_LABEL.get(curr["oi_act"], curr["oi_act"])
        signal_txt = ""
        pnl_txt    = ""

        # EXIT
        if trade_side == "CALL":
            exited, reason = check_call_exit(curr, prev, entry_mode)
            if exited:
                pnl_pts  = curr["c"] - entry_price
                pnl_rs   = pnl_pts * LOT_SIZE
                cumulative += pnl_rs
                signal_txt = f"🛑 EXIT CALL ({reason[:18]})"
                pnl_txt    = f"{pnl_pts:+.1f}pts ₹{pnl_rs:+.0f}"
                row = {
                    "mode": "backtest_db", "date": date_str,
                    "entry_time": entry_time, "exit_time": f"{date_str} {ts_str}",
                    "side": "CALL", "instrument": BUY_KEY,
                    "entry_price": round(entry_price, 2),
                    "exit_price":  round(curr["c"], 2),
                    "qty": LOT_SIZE, "pnl_pts": round(pnl_pts, 2),
                    "pnl_rs": round(pnl_rs, 2), "exit_reason": reason,
                    "cumulative_pnl_rs": round(cumulative, 2),
                }
                trade_log.append(row)
                log_trade_csv(row)
                save_trade_to_mongo(row)
                trade_side = None; entry_price = 0.0

        elif trade_side == "PUT":
            exited, reason = check_put_exit(curr, prev, entry_mode)
            if exited:
                pnl_pts  = entry_price - curr["c"]
                pnl_rs   = pnl_pts * LOT_SIZE
                cumulative += pnl_rs
                signal_txt = f"🛑 EXIT PUT  ({reason[:18]})"
                pnl_txt    = f"{pnl_pts:+.1f}pts ₹{pnl_rs:+.0f}"
                row = {
                    "mode": "backtest_db", "date": date_str,
                    "entry_time": entry_time, "exit_time": f"{date_str} {ts_str}",
                    "side": "PUT", "instrument": SELL_KEY,
                    "entry_price": round(entry_price, 2),
                    "exit_price":  round(curr["c"], 2),
                    "qty": LOT_SIZE, "pnl_pts": round(pnl_pts, 2),
                    "pnl_rs": round(pnl_rs, 2), "exit_reason": reason,
                    "cumulative_pnl_rs": round(cumulative, 2),
                }
                trade_log.append(row)
                log_trade_csv(row)
                save_trade_to_mongo(row)
                trade_side = None; entry_price = 0.0

        # ENTRY
        if trade_side is None:
            sig, mode = check_call_signal(curr, prev, prev2)
            if sig == "BUY_CALL":
                trade_side = "CALL"; entry_price = curr["c"]
                entry_time = f"{date_str} {ts_str}"; entry_mode = mode
                signal_txt = f"🟢 BUY CALL ({mode})"
            else:
                sig, mode = check_put_signal(curr, prev, prev2)
                if sig == "BUY_PUT":
                    trade_side = "PUT"; entry_price = curr["c"]
                    entry_time = f"{date_str} {ts_str}"; entry_mode = mode
                    signal_txt = f"🔴 BUY PUT  ({mode})"

        if signal_txt:
            print(
                f"{date_str:<10} {ts_str:<6} {curr['c']:>8.1f} {curr['ema7']:>8.1f} "
                f"{curr['ema21']:>8.1f} {curr['vwap']:>8.1f} "
                f"{oi_lbl:>11} {signal_txt:<22} {pnl_txt:>9}"
            )

    # ─── Summary ─────────────────────────────────────────────────
    if not trade_log:
        print("\nNo completed trades found.")
        return

    th     = pd.DataFrame(trade_log)
    total  = len(th)
    wins   = (th["pnl_rs"] > 0).sum()
    net    = th["pnl_rs"].sum()
    wr     = wins / total * 100

    # Per-day stats
    th["date_only"] = pd.to_datetime(th["date"])
    daily = th.groupby("date_only")["pnl_rs"].sum()
    best_day  = daily.max()
    worst_day = daily.min()

    print("\n" + "=" * 60)
    print(f"  BACKTEST_DB SUMMARY  ({BT_FROM} → {BT_TO})")
    print("=" * 60)
    print(f"  Timeframe      : {TIMEFRAME}")
    print(f"  Lot Size       : {LOT_SIZE}")
    print(f"  Total Trades   : {total}"
          f"  (CALL={len(th[th['side']=='CALL'])} | PUT={len(th[th['side']=='PUT'])})")
    print(f"  Wins / Losses  : {wins} ✅ / {total-wins} ❌")
    print(f"  Win Rate       : {wr:.1f}%")
    print(f"  Net P&L (₹)   : ₹{net:+,.0f}")
    print(f"  Avg Trade (₹)  : ₹{th['pnl_rs'].mean():+,.0f}")
    print(f"  Best Trade (₹) : ₹{th['pnl_rs'].max():+,.0f}")
    print(f"  Worst Trade(₹) : ₹{th['pnl_rs'].min():+,.0f}")
    print(f"  Best Day  (₹)  : ₹{best_day:+,.0f}")
    print(f"  Worst Day (₹)  : ₹{worst_day:+,.0f}")
    print(f"  CSV Log        : {TRADE_LOG}")
    print(f"  MongoDB Log    : {MONGO_DB}.{MONGO_TRADES}")
    print("=" * 60)


# ─────────────────────────────────────────────────────────────────
# 11.  ENTRY POINT
# ─────────────────────────────────────────────────────────────────

def print_help():
    print(
        "\nUsage:\n"
        "  python nifty_trading_bot.py backtest      # Fetch from Upstox API\n"
        "  python nifty_trading_bot.py backtest_db   # Read from MongoDB (FAST)\n"
        "  python nifty_trading_bot.py live          # Live market mode\n\n"
        "Config  : Edit config.ini (zones, levels, keys, timeframe, dates)\n"
        "MongoDB : Run populate_nifty_mongo.py first to fill the local DB\n"
    )


if __name__ == "__main__":
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if mode == "backtest":
        run_backtest()
    elif mode == "backtest_db":
        run_backtest_db()
    elif mode == "live":
        run_live()
    else:
        print_help()
