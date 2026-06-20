"""
Swing Trade Scanner — Upstox v2  (scanner_server.py)
=====================================================
Universe   : nse.csv  (2434 NSE EQ symbols + instrument keys)
Timeframe  : Daily candles  (swing trading — hold 2-15 days)
Indicators : EMA 20/50/200 · ADX(14)+DI · RSI(14) · ATR(14)
             Volume MA(20) · Supertrend(10,3) · 52-Week Hi/Lo
Signal     : Industry-standard swing scoring (0-100)

Signal Types
────────────
  STRONG_BUY   ADX>25 +DI>−DI price>EMA20>EMA50  RSI 50-68  Vol↑  Score 80-100
  BUY          ADX>18 +DI>−DI price>EMA20          RSI 45-68  Score 60-79
  PULLBACK_BUY price>EMA50 RSI pulled back 35-50   ADX>18     Score 45-59
  STRONG_SELL  ADX>25 −DI>+DI price<EMA20<EMA50   RSI 32-50  Vol↑  Score 80-100
  SELL         ADX>18 −DI>+DI price<EMA20          RSI 32-55  Score 60-79
  PULLBACK_SELL price<EMA50 RSI elevated 50-65     ADX>18     Score 45-59
  OVERSOLD     RSI<30  (watch for reversal)
  OVERBOUGHT   RSI>70  (watch for fade)
  NEUTRAL      No clear bias

Run
────
  pip install flask flask-cors requests pandas pytz
  python scanner_server.py
  # Open scanner.html in browser

API
────
  GET  /api/scan              all results (cached)
  GET  /api/scan?filter=BUY   filter by signal
  GET  /api/status
  POST /api/refresh           force immediate re-scan
  GET  /api/watchlist
"""

import os, sys, time, logging, threading, configparser, math
from datetime import datetime, timedelta, date
from typing import Optional, List, Dict

import pandas as pd
import numpy as np
import requests
import pytz
from flask import Flask, jsonify, request
from flask_cors import CORS

# ─────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────
def load_config(path="config.ini"):
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    if os.path.exists(path):
        cfg.read(path)
    return cfg

cfg = load_config()
TOKEN_FILE   = cfg.get("UPSTOX", "token_file", fallback="token.txt").strip()

# Scanner settings (add these to config.ini [SCANNER] section if desired)
SCAN_PORT      = int(cfg.get("SCANNER", "port",             fallback="5051"))
BATCH_SIZE     = int(cfg.get("SCANNER", "batch_size",       fallback="50"))   # symbols per scan batch
REFRESH_SECS   = int(cfg.get("SCANNER", "refresh_secs",     fallback="300"))  # 5-min background refresh
CANDLE_DAYS    = int(cfg.get("SCANNER", "candle_days",      fallback="300"))  # trading days of history
NSE_CSV        = cfg.get("SCANNER", "nse_csv",              fallback="nse.csv")
TOP_N          = int(cfg.get("SCANNER", "top_n",            fallback="200"))  # scan top N from csv

# Upstox endpoints
UPSTOX_HIST    = "https://api.upstox.com/v2/historical-candle"
UPSTOX_LTP     = "https://api.upstox.com/v2/market-quote/ltp"

# ─────────────────────────────────────────────────────────────────
# LOAD UNIVERSE FROM nse.csv
# ─────────────────────────────────────────────────────────────────
def load_universe(csv_path: str, top_n: int) -> Dict[str, str]:
    """Returns {SYMBOL: instrument_key} dict, top_n rows."""
    if not os.path.exists(csv_path):
        log.warning("nse.csv not found at %s — using empty universe", csv_path)
        return {}
    df = pd.read_csv(csv_path)
    # normalise column names
    df.columns = [c.strip().upper() for c in df.columns]
    sym_col = next((c for c in df.columns if "SYMBOL" in c), None)
    key_col = next((c for c in df.columns if "INSTRUMENT" in c or "KEY" in c), None)
    if not sym_col or not key_col:
        log.error("nse.csv must have SYMBOL and instrument_key columns")
        return {}
    df = df[[sym_col, key_col]].dropna().head(top_n)
    return dict(zip(df[sym_col].str.strip(), df[key_col].str.strip()))

UNIVERSE: Dict[str, str] = load_universe(NSE_CSV, TOP_N)
log.info("Universe loaded: %d symbols", len(UNIVERSE))

# ─────────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────────
def load_token() -> Optional[str]:
    if not os.path.exists(TOKEN_FILE):
        log.error("Token file '%s' missing.", TOKEN_FILE)
        return None
    return open(TOKEN_FILE).read().strip()

def auth_headers() -> dict:
    tok = load_token()
    return {"Authorization": f"Bearer {tok}", "Accept": "application/json"} if tok else {}

# ─────────────────────────────────────────────────────────────────
# DATA FETCH
# ─────────────────────────────────────────────────────────────────
def fetch_daily_candles(instrument_key: str, days: int = CANDLE_DAYS) -> pd.DataFrame:
    """Fetch daily OHLCV from Upstox historical candle API."""
    hdrs = auth_headers()
    if not hdrs:
        return pd.DataFrame()
    to_dt   = date.today()
    # Add calendar buffer for weekends/holidays
    from_dt = to_dt - timedelta(days=int(days * 1.6))
    url = (f"{UPSTOX_HIST}/{instrument_key}/day"
           f"/{to_dt.strftime('%Y-%m-%d')}/{from_dt.strftime('%Y-%m-%d')}")
    try:
        r = requests.get(url, headers=hdrs, timeout=12)
        if r.status_code != 200:
            return pd.DataFrame()
        candles = r.json().get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame()
        df = pd.DataFrame(candles, columns=["ts","open","high","low","close","volume","oi"])
        df["ts"] = pd.to_datetime(df["ts"])
        df = df.sort_values("ts").reset_index(drop=True)
        for c in ["open","high","low","close","volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["open","high","low","close"])
        return df.tail(days)
    except Exception as exc:
        log.debug("fetch_daily_candles(%s): %s", instrument_key, exc)
        return pd.DataFrame()

def fetch_ltp_batch(keys: List[str]) -> dict:
    hdrs = auth_headers()
    if not hdrs:
        return {}
    try:
        r = requests.get(UPSTOX_LTP, params={"instrument_key": ",".join(keys)},
                         headers=hdrs, timeout=10)
        return r.json().get("data", {}) if r.status_code == 200 else {}
    except Exception:
        return {}

# ─────────────────────────────────────────────────────────────────
# INDICATOR ENGINE
# ─────────────────────────────────────────────────────────────────

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing (RMA) — used in RSI, ADX, ATR."""
    result = [float("nan")] * len(s)
    idx = s.first_valid_index()
    if idx is None:
        return pd.Series(result, index=s.index)
    pos = s.index.get_loc(idx)
    # seed with SMA of first n values
    seed_end = pos + n
    if seed_end > len(s):
        return pd.Series(result, index=s.index)
    result[seed_end - 1] = s.iloc[pos:seed_end].mean()
    alpha = 1.0 / n
    for i in range(seed_end, len(s)):
        result[i] = result[i-1] * (1 - alpha) + s.iloc[i] * alpha
    return pd.Series(result, index=s.index)

def compute_atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return rma(tr, n)

def compute_rsi(s: pd.Series, n: int = 14) -> pd.Series:
    delta = s.diff()
    gain  = rma(delta.clip(lower=0), n)
    loss  = rma((-delta).clip(lower=0), n)
    rs    = gain / loss.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).round(2)

def compute_adx(df: pd.DataFrame, n: int = 14):
    """Returns (adx_series, plus_di_series, minus_di_series)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    up   = high - prev_high
    down = prev_low - low

    plus_dm  = np.where((up > down) & (up > 0), up,   0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    tr_s    = rma(tr, n)
    pdm_s   = rma(pd.Series(plus_dm,  index=df.index), n)
    mdm_s   = rma(pd.Series(minus_dm, index=df.index), n)

    plus_di  = (100 * pdm_s / tr_s.replace(0, float("nan"))).round(2)
    minus_di = (100 * mdm_s / tr_s.replace(0, float("nan"))).round(2)

    dx = (100 * (plus_di - minus_di).abs() /
          (plus_di + minus_di).replace(0, float("nan")))
    adx = rma(dx, n).round(2)
    return adx, plus_di, minus_di

def compute_supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0):
    """Returns supertrend direction: +1 = bullish, -1 = bearish."""
    atr  = compute_atr(df, period)
    mid  = (df["high"] + df["low"]) / 2
    upper = mid + mult * atr
    lower = mid - mult * atr

    st    = [float("nan")] * len(df)
    trend = [0] * len(df)

    for i in range(1, len(df)):
        if math.isnan(atr.iloc[i]):
            continue
        # finalUpperBand
        ub = upper.iloc[i] if (upper.iloc[i] < (st[i-1] if not math.isnan(st[i-1]) else upper.iloc[i])
                               or df["close"].iloc[i-1] > (st[i-1] if not math.isnan(st[i-1]) else upper.iloc[i])) \
             else (st[i-1] if not math.isnan(st[i-1]) else upper.iloc[i])
        lb = lower.iloc[i] if (lower.iloc[i] > (st[i-1] if not math.isnan(st[i-1]) else lower.iloc[i])
                               or df["close"].iloc[i-1] < (st[i-1] if not math.isnan(st[i-1]) else lower.iloc[i])) \
             else (st[i-1] if not math.isnan(st[i-1]) else lower.iloc[i])

        prev_st = st[i-1] if not math.isnan(st[i-1]) else ub
        if prev_st == ub:
            st[i] = lb if df["close"].iloc[i] <= ub else ub
        else:
            st[i] = ub if df["close"].iloc[i] >= lb else lb

        trend[i] = 1 if df["close"].iloc[i] > st[i] else -1

    return pd.Series(trend, index=df.index), pd.Series(st, index=df.index)

# ─────────────────────────────────────────────────────────────────
# SWING SIGNAL ENGINE
# ─────────────────────────────────────────────────────────────────

def compute_score(row: dict) -> int:
    """
    Composite score 0-100 for the signal.
    Component weights (total 100):
      ADX strength          20 pts
      DI alignment          15 pts
      EMA alignment         20 pts
      RSI zone              20 pts
      Volume confirmation   10 pts
      Supertrend            10 pts
      Price vs EMA200        5 pts
    """
    score = 0
    sig = row["signal_type"]
    is_long = "BUY" in sig
    is_short = "SELL" in sig

    adx, pdi, mdi = row.get("adx"), row.get("plus_di"), row.get("minus_di")
    rsi   = row.get("rsi")
    price = row.get("close")
    e20, e50, e200 = row.get("ema20"), row.get("ema50"), row.get("ema200")
    vol, vol_ma = row.get("volume"), row.get("vol_ma20")
    st_dir = row.get("supertrend_dir", 0)

    # ADX strength (20 pts)
    if adx:
        if adx >= 40:   score += 20
        elif adx >= 30: score += 15
        elif adx >= 25: score += 12
        elif adx >= 20: score += 8
        elif adx >= 15: score += 4

    # DI alignment (15 pts)
    if pdi and mdi:
        gap = abs(pdi - mdi)
        if is_long  and pdi > mdi: score += min(15, int(gap / 2))
        if is_short and mdi > pdi: score += min(15, int(gap / 2))

    # EMA alignment (20 pts)
    if price and e20 and e50 and e200:
        if is_long:
            if price > e20:             score += 7
            if e20   > e50:             score += 7
            if price > e200:            score += 6
        if is_short:
            if price < e20:             score += 7
            if e20   < e50:             score += 7
            if price < e200:            score += 6

    # RSI zone (20 pts)
    if rsi:
        if is_long:
            if   50 <= rsi <= 65:       score += 20
            elif 45 <= rsi <  50:       score += 14
            elif 65 <  rsi <= 70:       score += 10
            elif 40 <= rsi <  45:       score += 8
        if is_short:
            if   35 <= rsi <= 50:       score += 20
            elif 30 <= rsi <  35:       score += 14
            elif 50 <  rsi <= 55:       score += 10
            elif 55 <  rsi <= 60:       score += 8

    # Volume (10 pts)
    if vol and vol_ma and vol_ma > 0:
        ratio = vol / vol_ma
        if   ratio >= 2.0: score += 10
        elif ratio >= 1.5: score += 8
        elif ratio >= 1.2: score += 6
        elif ratio >= 1.0: score += 4

    # Supertrend (10 pts)
    if (is_long  and st_dir ==  1): score += 10
    if (is_short and st_dir == -1): score += 10

    # EMA200 (5 pts — regime filter)
    if price and e200:
        if is_long  and price > e200: score += 5
        if is_short and price < e200: score += 5

    return min(100, score)


def classify_signal(row: dict) -> str:
    adx = row.get("adx") or 0
    pdi = row.get("plus_di") or 0
    mdi = row.get("minus_di") or 0
    rsi = row.get("rsi") or 50
    price = row.get("close") or 0
    e20  = row.get("ema20")  or price
    e50  = row.get("ema50")  or price
    e200 = row.get("ema200") or price
    vol  = row.get("volume") or 0
    vma  = row.get("vol_ma20") or 1
    st   = row.get("supertrend_dir", 0)

    bull_ema    = price > e20 > e50
    bull_long   = price > e20 and pdi > mdi
    bear_ema    = price < e20 < e50
    bear_short  = price < e20 and mdi > pdi
    vol_confirm = vol >= vma * 1.2
    above_200   = price > e200
    below_200   = price < e200

    # STRONG BUY: full alignment, trending
    if (adx >= 25 and pdi > mdi and bull_ema and
            45 <= rsi <= 70 and vol_confirm and st == 1 and above_200):
        return "STRONG_BUY"

    # BUY: most conditions met
    if (adx >= 18 and bull_long and 42 <= rsi <= 70 and st == 1):
        return "BUY"

    # PULLBACK BUY: price above EMA50 but pulled back, RSI dipped
    if (adx >= 15 and price > e50 and pdi > mdi and
            30 <= rsi <= 50 and above_200):
        return "PULLBACK_BUY"

    # STRONG SELL
    if (adx >= 25 and mdi > pdi and bear_ema and
            30 <= rsi <= 55 and vol_confirm and st == -1 and below_200):
        return "STRONG_SELL"

    # SELL
    if (adx >= 18 and bear_short and 30 <= rsi <= 58 and st == -1):
        return "SELL"

    # PULLBACK SELL
    if (adx >= 15 and price < e50 and mdi > pdi and
            50 <= rsi <= 70 and below_200):
        return "PULLBACK_SELL"

    # OVERSOLD (reversal watch)
    if rsi < 30 and price > e200 * 0.85:
        return "OVERSOLD"

    # OVERBOUGHT (fade watch)
    if rsi > 78 and price < e200 * 1.20:
        return "OVERBOUGHT"

    return "NEUTRAL"


SIGNAL_LABELS = {
    "STRONG_BUY":   "🚀 STRONG BUY",
    "BUY":          "🟢 BUY",
    "PULLBACK_BUY": "🌿 PB BUY",
    "STRONG_SELL":  "💣 STRONG SELL",
    "SELL":         "🔴 SELL",
    "PULLBACK_SELL":"🍂 PB SELL",
    "OVERSOLD":     "⚡ OVERSOLD",
    "OVERBOUGHT":   "🔥 OVERBOUGHT",
    "NEUTRAL":      "⬜ NEUTRAL",
}

# ─────────────────────────────────────────────────────────────────
# FULL STOCK ANALYSIS
# ─────────────────────────────────────────────────────────────────

def analyze(symbol: str, instrument_key: str) -> dict:
    base = {
        "symbol": symbol, "instrument": instrument_key,
        "signal_type": "NEUTRAL", "signal": "⬜ NEUTRAL",
        "score": 0, "error": None,
    }

    df = fetch_daily_candles(instrument_key, CANDLE_DAYS)
    MIN_BARS = 60
    if df.empty or len(df) < MIN_BARS:
        base["error"] = f"only {len(df)} candles"
        return base

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]

    # ── Indicators ──
    df["ema20"]  = ema(close, 20)
    df["ema50"]  = ema(close, 50)
    df["ema200"] = ema(close, 200)
    df["rsi"]    = compute_rsi(close, 14)
    df["atr"]    = compute_atr(df, 14)
    df["vol_ma20"] = volume.rolling(20).mean()
    adx_s, pdi_s, mdi_s = compute_adx(df, 14)
    df["adx"]    = adx_s
    df["pdi"]    = pdi_s
    df["mdi"]    = mdi_s
    st_dir, st_line = compute_supertrend(df, 10, 3.0)
    df["st_dir"] = st_dir

    last = df.iloc[-1]

    def fv(x): return round(float(x), 2) if not (math.isnan(float(x)) if x is not None else True) else None

    price   = fv(last["close"])
    prev_close = fv(df["close"].iloc[-2]) if len(df) >= 2 else price
    change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close and prev_close != 0 else None

    # 52-week high/low
    window_252 = df.tail(252)
    w52_high = fv(window_252["high"].max())
    w52_low  = fv(window_252["low"].min())
    dist_52h = round((price / w52_high - 1) * 100, 1) if w52_high and price else None
    dist_52l = round((price / w52_low  - 1) * 100, 1) if w52_low  and price else None

    # ATR stop-loss
    atr_val    = fv(last["atr"])
    stop_loss_long  = round(price - 2.0 * atr_val, 2) if price and atr_val else None
    stop_loss_short = round(price + 2.0 * atr_val, 2) if price and atr_val else None

    row = {
        "symbol":        symbol,
        "instrument":    instrument_key,
        "close":         price,
        "change_pct":    change_pct,
        "volume":        int(last["volume"]) if not math.isnan(float(last["volume"])) else None,
        "vol_ma20":      fv(last["vol_ma20"]),
        "ema20":         fv(last["ema20"]),
        "ema50":         fv(last["ema50"]),
        "ema200":        fv(last["ema200"]),
        "rsi":           fv(last["rsi"]),
        "adx":           fv(last["adx"]),
        "plus_di":       fv(last["pdi"]),
        "minus_di":      fv(last["mdi"]),
        "atr":           atr_val,
        "supertrend_dir": int(last["st_dir"]),
        "w52_high":      w52_high,
        "w52_low":       w52_low,
        "dist_52h_pct":  dist_52h,
        "dist_52l_pct":  dist_52l,
        "stop_long":     stop_loss_long,
        "stop_short":    stop_loss_short,
        "candles":       len(df),
        "error":         None,
    }

    sig_type = classify_signal(row)
    row["signal_type"] = sig_type
    row["signal"]      = SIGNAL_LABELS.get(sig_type, "⬜ NEUTRAL")
    row["score"]       = compute_score(row)
    return row

# ─────────────────────────────────────────────────────────────────
# SCAN CACHE + BACKGROUND WORKER
# ─────────────────────────────────────────────────────────────────

_cache = {
    "results":    [],
    "updated_at": None,
    "scanning":   False,
    "scan_progress": 0,
    "error":      None,
}
_lock = threading.Lock()


def run_full_scan() -> List[dict]:
    symbols = list(UNIVERSE.items())
    total   = len(symbols)
    results = []
    for i, (sym, ikey) in enumerate(symbols):
        try:
            rec = analyze(sym, ikey)
        except Exception as exc:
            rec = {"symbol": sym, "instrument": ikey, "signal_type": "NEUTRAL",
                   "score": 0, "error": str(exc)}
        results.append(rec)
        with _lock:
            _cache["scan_progress"] = round((i + 1) / total * 100)
        time.sleep(0.25)   # ~4 req/s — well within Upstox rate limit

    # Sort: STRONG_BUY first → BUY → PB_BUY → STRONG_SELL → SELL → PB_SELL → others
    order = {
        "STRONG_BUY":   0, "BUY":          1, "PULLBACK_BUY":  2,
        "STRONG_SELL":  3, "SELL":          4, "PULLBACK_SELL": 5,
        "OVERSOLD":     6, "OVERBOUGHT":    7, "NEUTRAL":       8,
    }
    results.sort(key=lambda x: (order.get(x.get("signal_type","NEUTRAL"), 8), -x.get("score", 0)))
    return results


def _bg_scanner():
    while True:
        try:
            with _lock:
                _cache["scanning"] = True
                _cache["scan_progress"] = 0
            results = run_full_scan()
            with _lock:
                _cache["results"]    = results
                _cache["updated_at"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
                _cache["scanning"]   = False
                _cache["error"]      = None
            log.info("Scan done: %d symbols | STRONG_BUY=%d BUY=%d STRONG_SELL=%d SELL=%d",
                     len(results),
                     sum(1 for r in results if r.get("signal_type") == "STRONG_BUY"),
                     sum(1 for r in results if r.get("signal_type") == "BUY"),
                     sum(1 for r in results if r.get("signal_type") == "STRONG_SELL"),
                     sum(1 for r in results if r.get("signal_type") == "SELL"))
        except Exception as exc:
            log.error("Scanner error: %s", exc)
            with _lock:
                _cache["error"]   = str(exc)
                _cache["scanning"] = False
        time.sleep(REFRESH_SECS)

# ─────────────────────────────────────────────────────────────────
# FLASK
# ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

FILTER_MAP = {
    "STRONG_BUY":    ["STRONG_BUY"],
    "BUY":           ["BUY", "STRONG_BUY"],
    "ALL_LONGS":     ["STRONG_BUY", "BUY", "PULLBACK_BUY"],
    "PULLBACK_BUY":  ["PULLBACK_BUY"],
    "STRONG_SELL":   ["STRONG_SELL"],
    "SELL":          ["SELL", "STRONG_SELL"],
    "ALL_SHORTS":    ["STRONG_SELL", "SELL", "PULLBACK_SELL"],
    "PULLBACK_SELL": ["PULLBACK_SELL"],
    "OVERSOLD":      ["OVERSOLD"],
    "OVERBOUGHT":    ["OVERBOUGHT"],
    "NEUTRAL":       ["NEUTRAL"],
    "ALL":           None,
}

@app.route("/api/scan")
def api_scan():
    filt = request.args.get("filter", "ALL").upper()
    allowed = FILTER_MAP.get(filt)
    with _lock:
        results  = list(_cache["results"])
        updated  = _cache["updated_at"]
        scanning = _cache["scanning"]
        progress = _cache["scan_progress"]
        error    = _cache["error"]

    if allowed is not None:
        results = [r for r in results if r.get("signal_type") in allowed]

    # Summary counts
    counts = {}
    for sig in SIGNAL_LABELS:
        counts[sig] = sum(1 for r in _cache["results"] if r.get("signal_type") == sig)

    return jsonify({
        "ok":         True,
        "updated_at": updated,
        "scanning":   scanning,
        "progress":   progress,
        "error":      error,
        "total":      len(_cache["results"]),
        "filtered":   len(results),
        "counts":     counts,
        "results":    results,
    })

@app.route("/api/status")
def api_status():
    now = datetime.now(IST)
    mo  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    mc  = now.replace(hour=15, minute=30, second=0, microsecond=0)
    mkt = mo <= now <= mc and now.weekday() < 5
    with _lock:
        scanning = _cache["scanning"]
        updated  = _cache["updated_at"]
        progress = _cache["scan_progress"]
    return jsonify({
        "ok":             True,
        "server_time":    now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "market_open":    mkt,
        "scanning":       scanning,
        "progress":       progress,
        "last_scan":      updated,
        "universe_size":  len(UNIVERSE),
        "refresh_secs":   REFRESH_SECS,
        "token_ok":       os.path.exists(TOKEN_FILE),
    })

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    def _go():
        try:
            with _lock:
                _cache["scanning"] = True
                _cache["scan_progress"] = 0
            res = run_full_scan()
            with _lock:
                _cache["results"]    = res
                _cache["updated_at"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
                _cache["scanning"]   = False
                _cache["error"]      = None
        except Exception as e:
            with _lock:
                _cache["error"]   = str(e)
                _cache["scanning"] = False
    threading.Thread(target=_go, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan started"})

@app.route("/api/watchlist")
def api_watchlist():
    return jsonify({"count": len(UNIVERSE), "symbols": list(UNIVERSE.keys())})

@app.route("/")
def index():
    return ("<html><body style='font-family:monospace;background:#0a0c0f;"
            "color:#c9d1e0;padding:30px'><h2>📡 Swing Scanner API</h2>"
            "<p>Open <b>scanner.html</b> for the dashboard.</p></body></html>")

# ─────────────────────────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("Swing Scanner  |  port %d  |  %d symbols  |  daily candles", SCAN_PORT, len(UNIVERSE))
    log.info("Token: %s", "FOUND" if os.path.exists(TOKEN_FILE) else "MISSING ⚠")
    log.info("Refresh: every %ds", REFRESH_SECS)
    log.info("=" * 60)
    threading.Thread(target=_bg_scanner, daemon=True).start()
    app.run(host="0.0.0.0", port=SCAN_PORT, debug=False)
