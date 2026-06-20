"""
Nifty Options — LIVE Market Trader
====================================
Signal source : Nifty Futures 5-min closed candles (EMA7/21, VWAP, OI)
Orders placed : CE option  (Buy_instrument_key)
                PE option  (Sell_instrument_key)
Order API     : Upstox v3 HFT  POST https://api-hft.upstox.com/v3/order/place

Safety features
───────────────
  • ENABLE_LIVE_ORDERS = False  in config  →  paper-simulation only
  • Market-hours guard  (no orders before 09:15 or after AUTO_SQUAREOFF_TIME)
  • Auto square-off all open positions at AUTO_SQUAREOFF_TIME (default 15:20)
  • Order-fill verification via Upstox order-status API
  • Position sync via Upstox portfolio API (every N candles)
  • Max daily-loss circuit breaker (stops new entries, exits open trades)
  • Duplicate signal guard (one trade at a time)
  • All trades saved: CSV  +  MongoDB (NSE_DAILY.live_trades)  +  Telegram

Usage
─────
  python nifty_live_trader.py              # live orders if enabled, else simulation
  python nifty_live_trader.py --paper      # force paper mode even if config says live
  python nifty_live_trader.py --simulate   # replay logic on today's completed candles

config.ini additions required
──────────────────────────────
  [LIVE_TRADING]
  enable_live_orders   = False
  product_type         = I          ; I=Intraday  D=Delivery/Carryforward
  trade_qty_call       = 25         ; CE lot quantity
  trade_qty_put        = 25         ; PE lot quantity
  auto_squareoff_time  = 15:20      ; HH:MM  IST
  max_daily_loss_rs    = 5000       ; circuit-breaker in ₹  (0 = disabled)
  order_retry          = 2          ; max order placement retries
  position_sync_every  = 3         ; re-sync position every N candles
"""

import os
import sys
import csv
import time
import logging
import argparse
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
        logging.FileHandler("live_trader.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────────────────────────
# 1.  CONFIG
# ─────────────────────────────────────────────────────────────────

def load_config(path: str = "config.ini") -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    if not os.path.exists(path):
        log.error("config.ini not found.")
        sys.exit(1)
    cfg.read(path)
    return cfg

cfg = load_config()

# — Upstox keys —
TOKEN_FILE  = cfg.get("UPSTOX", "token_file",          fallback="token.txt").strip()
BUY_KEY     = cfg.get("UPSTOX", "Buy_instrument_key",  fallback="NSE_FO|54765").strip()   # CE
SELL_KEY    = cfg.get("UPSTOX", "Sell_instrument_key", fallback="NSE_FO|54766").strip()   # PE
FUTURES_KEY = cfg.get("UPSTOX", "Nifty_futures_key",   fallback="NSE_FO|57960").strip()
VIX_KEY     = cfg.get("UPSTOX", "vix_key",             fallback="NSE_INDEX|India VIX").strip()

# — Timeframe / indicators —
TIMEFRAME     = cfg.get("SETTINGS", "timeframe",      fallback="5minute").strip()
LOOP_INTERVAL = cfg.getint("SETTINGS", "loop_interval", fallback=15)
LOT_SIZE      = cfg.getint("SETTINGS", "lot_size",      fallback=25)

# — Zones —
NT_UPPER    = cfg.getfloat("ZONES", "no_trade_upper", fallback=24100)
NT_LOWER    = cfg.getfloat("ZONES", "no_trade_lower", fallback=23900)
UPPER_LIMIT = cfg.getfloat("ZONES", "upper_limit",    fallback=24500)
LOWER_LIMIT = cfg.getfloat("ZONES", "lower_limit",    fallback=23500)
R1          = cfg.getfloat("ZONES", "r1",             fallback=23992)
R2          = cfg.getfloat("ZONES", "r2",             fallback=24150)
S1          = cfg.getfloat("ZONES", "s1",             fallback=23885)
S2          = cfg.getfloat("ZONES", "s2",             fallback=23700)
LVL_BUFFER  = cfg.getfloat("ZONES", "level_buffer",   fallback=15)

# — Live trading —
_LT = "LIVE_TRADING"
ENABLE_LIVE_ORDERS  = cfg.getboolean(_LT, "enable_live_orders",  fallback=False)
PRODUCT_TYPE        = cfg.get(_LT,       "product_type",         fallback="I").strip().upper()
QTY_CALL            = cfg.getint(_LT,    "trade_qty_call",        fallback=LOT_SIZE)
QTY_PUT             = cfg.getint(_LT,    "trade_qty_put",         fallback=LOT_SIZE)
SQUAREOFF_TIME      = cfg.get(_LT,       "auto_squareoff_time",   fallback="15:20").strip()
MAX_DAILY_LOSS_RS   = cfg.getfloat(_LT,  "max_daily_loss_rs",     fallback=0)
ORDER_RETRY         = cfg.getint(_LT,    "order_retry",           fallback=2)
POSITION_SYNC_EVERY = cfg.getint(_LT,    "position_sync_every",   fallback=3)

# — Telegram —
BOT_TOKEN       = cfg.get("TELEGRAM", "bot_token",       fallback="").strip()
CHANNEL_ID      = cfg.get("TELEGRAM", "channel_id",      fallback="").strip()
ENABLE_TELEGRAM = cfg.getboolean("TELEGRAM", "enable_telegram", fallback=False)

# — MongoDB —
MONGO_URI    = "mongodb://localhost:27017/"
MONGO_DB     = "NSE_DAILY"
MONGO_TRADES = "live_trades"

# — API endpoints —
UPSTOX_QUOTE    = "https://api.upstox.com/v2/market-quote/quotes"
UPSTOX_HIST     = "https://api.upstox.com/v2/historical-candle"
UPSTOX_ORDER    = "https://api-hft.upstox.com/v3/order/place"
UPSTOX_ORDER_ST = "https://api.upstox.com/v2/order/details"
UPSTOX_POSITIONS= "https://api.upstox.com/v2/portfolio/short-term-positions"
UPSTOX_CANCEL   = "https://api.upstox.com/v2/order/cancel"

TRADE_LOG = "live_trade_log.csv"

TF_MAP = {
    "1minute": "1min",  "3minute": "3min",
    "5minute": "5min",  "15minute": "15min", "30minute": "30min",
}
PANDAS_TF = TF_MAP.get(TIMEFRAME, "5min")


# ─────────────────────────────────────────────────────────────────
# 2.  AUTH
# ─────────────────────────────────────────────────────────────────

def load_token() -> Optional[str]:
    if not os.path.exists(TOKEN_FILE):
        log.error("Token file '%s' missing.", TOKEN_FILE)
        return None
    with open(TOKEN_FILE) as fh:
        return fh.read().strip()

def auth_headers() -> dict:
    tok = load_token()
    return {"Authorization": f"Bearer {tok}", "Accept": "application/json"} if tok else {}

def now_ist() -> datetime:
    return datetime.now(IST)

def now_ist_str() -> str:
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")


# ─────────────────────────────────────────────────────────────────
# 3.  TELEGRAM
# ─────────────────────────────────────────────────────────────────

def send_telegram(msg: str) -> None:
    if not ENABLE_TELEGRAM or not BOT_TOKEN:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHANNEL_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
        if r.status_code != 200:
            log.warning("Telegram HTTP %s", r.status_code)
    except Exception as exc:
        log.error("Telegram: %s", exc)


# ─────────────────────────────────────────────────────────────────
# 4.  LOGGING — CSV + MONGODB
# ─────────────────────────────────────────────────────────────────

_CSV_FIELDS = [
    "mode", "date", "entry_time", "exit_time", "side",
    "instrument", "entry_price", "exit_price", "qty",
    "pnl_pts", "pnl_rs", "exit_reason",
    "order_id_entry", "order_id_exit", "cumulative_pnl_rs",
]

def log_trade_csv(row: dict) -> None:
    new_file = not os.path.isfile(TRADE_LOG)
    with open(TRADE_LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow(row)

def log_trade_mongo(row: dict) -> None:
    if not MONGO_AVAILABLE:
        return
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        doc = dict(row)
        doc["saved_at"] = datetime.utcnow()
        client[MONGO_DB][MONGO_TRADES].insert_one(doc)
        client.close()
    except Exception as exc:
        log.warning("MongoDB log failed: %s", exc)


# ─────────────────────────────────────────────────────────────────
# 5.  MARKET HOURS
# ─────────────────────────────────────────────────────────────────

MARKET_OPEN  = (9, 15)    # HH, MM
MARKET_CLOSE = (15, 30)

def parse_hhmm(hhmm: str) -> tuple:
    h, m = hhmm.split(":")
    return int(h), int(m)

SQUAREOFF_HH, SQUAREOFF_MM = parse_hhmm(SQUAREOFF_TIME)

def is_market_open() -> bool:
    n = now_ist()
    t = (n.hour, n.minute)
    return MARKET_OPEN <= t < MARKET_CLOSE and n.weekday() < 5

def is_squareoff_time() -> bool:
    n = now_ist()
    return (n.hour, n.minute) >= (SQUAREOFF_HH, SQUAREOFF_MM)

def minutes_to_open() -> int:
    n = now_ist()
    open_dt = n.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1],
                        second=0, microsecond=0)
    diff = (open_dt - n).total_seconds()
    return max(0, int(diff // 60))


# ─────────────────────────────────────────────────────────────────
# 6.  UPSTOX ORDER API
# ─────────────────────────────────────────────────────────────────

def place_order(instrument_key: str, transaction_type: str,
                qty: int, hdrs: dict,
                tag: str = "nifty_bot") -> Optional[str]:
    """
    Place a MARKET order via Upstox v3 HFT API.
    Returns order_id on success, None on failure.
    transaction_type: "BUY" | "SELL"
    """
    payload = {
        "quantity":           int(qty),
        "product":            PRODUCT_TYPE,
        "validity":           "DAY",
        "price":              0,
        "tag":                tag,
        "instrument_token":   instrument_key,
        "order_type":         "MARKET",
        "transaction_type":   transaction_type.upper(),
        "disclosed_quantity": 0,
        "trigger_price":      0,
        "is_amo":             False,
        "slice":              True,
    }
    post_hdrs = dict(hdrs)
    post_hdrs["Content-Type"] = "application/json"

    for attempt in range(1, ORDER_RETRY + 2):
        try:
            r = requests.post(UPSTOX_ORDER, json=payload, headers=post_hdrs, timeout=8)
            data = r.json()
            if r.status_code == 200 and data.get("status") == "success":
                oid = data.get("data", {}).get("order_id", "UNKNOWN")
                log.info("✅ ORDER PLACED | %s %s x%d | ID: %s",
                         transaction_type, instrument_key, qty, oid)
                return oid
            else:
                log.warning("Order attempt %d failed HTTP %s: %s",
                            attempt, r.status_code, str(data)[:200])
        except Exception as exc:
            log.error("Order API error (attempt %d): %s", attempt, exc)
        time.sleep(1)

    log.error("❌ ORDER FAILED after %d attempts | %s %s",
              ORDER_RETRY + 1, transaction_type, instrument_key)
    return None


def get_order_status(order_id: str, hdrs: dict) -> dict:
    """
    Fetch order detail from Upstox.
    Returns dict with keys: status, filled_qty, avg_price, message
    """
    if not order_id or order_id == "SIMULATED":
        return {"status": "complete", "filled_qty": 0, "avg_price": 0.0}
    try:
        r = requests.get(UPSTOX_ORDER_ST,
                         headers=hdrs, params={"order_id": order_id}, timeout=8)
        if r.status_code == 200:
            d    = r.json().get("data", {})
            return {
                "status":     d.get("status", "unknown").lower(),
                "filled_qty": d.get("filled_quantity", 0),
                "avg_price":  d.get("average_price", 0.0),
                "message":    d.get("status_message", ""),
            }
    except Exception as exc:
        log.error("Order status error: %s", exc)
    return {"status": "error", "filled_qty": 0, "avg_price": 0.0}


def wait_for_fill(order_id: str, hdrs: dict, max_wait: int = 15) -> dict:
    """Poll order status until filled or timeout."""
    for _ in range(max_wait):
        st = get_order_status(order_id, hdrs)
        if st["status"] in ("complete", "filled"):
            return st
        if st["status"] in ("rejected", "cancelled", "error"):
            log.error("Order %s %s: %s", order_id, st["status"], st.get("message"))
            return st
        time.sleep(1)
    log.warning("Order %s not filled within %ds", order_id, max_wait)
    return get_order_status(order_id, hdrs)


def get_upstox_positions(hdrs: dict) -> dict:
    """
    Fetch short-term positions from Upstox.
    Returns dict keyed by instrument_key → {qty, avg_price, pnl}
    """
    pos = {}
    try:
        r = requests.get(UPSTOX_POSITIONS, headers=hdrs, timeout=8)
        if r.status_code == 200:
            for item in r.json().get("data", []):
                key = item.get("instrument_token") or item.get("instrument_key", "")
                qty = item.get("quantity", 0)   # net qty (buy - sell)
                pos[key] = {
                    "qty":       qty,
                    "avg_price": item.get("average_price", 0.0),
                    "pnl":       item.get("pnl", 0.0),
                    "ltp":       item.get("last_price", 0.0),
                }
    except Exception as exc:
        log.error("Positions fetch error: %s", exc)
    return pos


def get_ltp(instrument_key: str, hdrs: dict) -> Optional[float]:
    try:
        r = requests.get(UPSTOX_QUOTE, headers=hdrs,
                         params={"instrument_key": instrument_key}, timeout=6)
        if r.status_code == 200:
            data  = r.json().get("data", {})
            entry = next(iter(data.values()), {})
            ltp   = entry.get("last_price") or entry.get("ltp") or entry.get("close_price")
            return float(ltp) if ltp else None
    except Exception as exc:
        log.error("LTP error: %s", exc)
    return None


def get_vix(hdrs: dict) -> float:
    try:
        r = requests.get(UPSTOX_QUOTE, headers=hdrs,
                         params={"instrument_key": VIX_KEY}, timeout=5)
        if r.status_code == 200:
            data  = r.json().get("data", {})
            entry = next(iter(data.values()), {})
            return float(entry.get("last_price", 0))
    except Exception:
        pass
    return 0.0


# ─────────────────────────────────────────────────────────────────
# 7.  DATA + INDICATORS
# ─────────────────────────────────────────────────────────────────

def _encode(key: str) -> str:
    from urllib.parse import quote
    return quote(key, safe="")


def fetch_intraday(key: str, hdrs: dict) -> Optional[pd.DataFrame]:
    url = f"{UPSTOX_HIST}/intraday/{_encode(key)}/1minute"
    try:
        r = requests.get(url, headers=hdrs, timeout=10)
        if r.status_code == 200:
            candles = r.json().get("data", {}).get("candles", [])
            if candles:
                return _build_df(candles)
    except Exception as exc:
        log.error("Intraday fetch: %s", exc)
    return None


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


def resample(df: pd.DataFrame) -> pd.DataFrame:
    if PANDAS_TF == "1min":
        return df
    return (
        df.set_index("ts")
        .resample(PANDAS_TF)
        .agg({"o": "first", "h": "max", "l": "min",
              "c": "last", "v": "sum", "oi": "last"})
        .dropna()
        .reset_index()
    )


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)

    df["ema7"]  = df["c"].ewm(span=7,  adjust=False).mean()
    df["ema21"] = df["c"].ewm(span=21, adjust=False).mean()

    df["date"]    = df["ts"].dt.date
    df["tp"]      = (df["h"] + df["l"] + df["c"]) / 3
    df["tpv"]     = df["tp"] * df["v"]
    df["cum_tpv"] = df.groupby("date")["tpv"].cumsum()
    df["cum_v"]   = df.groupby("date")["v"].cumsum()
    df["vwap"]    = df["cum_tpv"] / df["cum_v"]
    df.drop(columns=["tpv", "cum_tpv", "cum_v"], inplace=True)

    df["oi_chg"]    = df["oi"].diff()
    df["price_chg"] = df["c"].diff()
    df["oi_act"]    = df.apply(
        lambda r: _oi_action(r["price_chg"], r["oi_chg"]), axis=1
    )
    return df


def _oi_action(pc: float, oc: float) -> str:
    if   pc > 0 and oc > 0: return "LB"
    elif pc < 0 and oc > 0: return "SB"
    elif pc < 0 and oc < 0: return "LU"
    elif pc > 0 and oc < 0: return "SC"
    return "NE"

OI_LABEL = {"LB": "🟢 LB", "SC": "🔵 SC", "LU": "🟡 LU", "SB": "🔴 SB", "NE": "⚪ NE"}
BULLISH_OI = {"LB", "SC"}
BEARISH_OI = {"LU", "SB"}


# ─────────────────────────────────────────────────────────────────
# 8.  ZONE CHECKS
# ─────────────────────────────────────────────────────────────────

def call_allowed(price: float) -> tuple[bool, str]:
    if price >= UPPER_LIMIT or price <= LOWER_LIMIT:
        return False, f"Beyond limits"
    if NT_LOWER <= price <= NT_UPPER:
        return False, f"No-Trade Zone"
    if (abs(price - R1) <= LVL_BUFFER or abs(price - R2) <= LVL_BUFFER
            or price >= R2 + LVL_BUFFER):
        return False, f"At Resistance"
    return True, ""

def put_allowed(price: float) -> tuple[bool, str]:
    if price >= UPPER_LIMIT or price <= LOWER_LIMIT:
        return False, f"Beyond limits"
    if NT_LOWER <= price <= NT_UPPER:
        return False, f"No-Trade Zone"
    if (abs(price - S1) <= LVL_BUFFER or abs(price - S2) <= LVL_BUFFER
            or price <= S2 - LVL_BUFFER):
        return False, f"At Support"
    return True, ""


# ─────────────────────────────────────────────────────────────────
# 9.  SIGNAL LOGIC  (identical to backtest version)
# ─────────────────────────────────────────────────────────────────

def call_signal(curr, prev) -> tuple[str, str]:
    if curr["oi_act"] not in BULLISH_OI or prev["oi_act"] not in BULLISH_OI:
        return "", ""
    ok, _ = call_allowed(curr["c"])
    if not ok:
        return "", ""
    a7, a21, av = curr["c"] > curr["ema7"], curr["c"] > curr["ema21"], curr["c"] > curr["vwap"]
    if a7 and a21 and av: return "BUY_CALL", "STRONG"
    if a7:                return "BUY_CALL", "PULLBACK"
    return "", ""

def put_signal(curr, prev) -> tuple[str, str]:
    if curr["oi_act"] not in BEARISH_OI or prev["oi_act"] not in BEARISH_OI:
        return "", ""
    ok, _ = put_allowed(curr["c"])
    if not ok:
        return "", ""
    b7, b21, bv = curr["c"] < curr["ema7"], curr["c"] < curr["ema21"], curr["c"] < curr["vwap"]
    if b7 and b21 and bv: return "BUY_PUT", "STRONG"
    if b7:                return "BUY_PUT", "PULLBACK"
    return "", ""

def call_exit(curr, prev, mode: str) -> tuple[bool, str]:
    if curr["c"] < prev["c"]:          return True, "Close < Prev Close"
    if mode == "PULLBACK" and curr["c"] < curr["vwap"]: return True, "Close < VWAP"
    return False, ""

def put_exit(curr, prev, mode: str) -> tuple[bool, str]:
    if curr["c"] > prev["c"]:          return True, "Close > Prev Close"
    if mode == "PULLBACK" and curr["c"] > curr["vwap"]: return True, "Close > VWAP"
    return False, ""


# ─────────────────────────────────────────────────────────────────
# 10.  POSITION MANAGER
# ─────────────────────────────────────────────────────────────────

class PositionManager:
    """
    Tracks one open position at a time.
    Handles paper simulation when live orders are disabled.
    """

    def __init__(self, hdrs: dict, paper_mode: bool = False):
        self.hdrs         = hdrs
        self.paper_mode   = paper_mode  # True = no real orders placed
        self.side         = None        # "CALL" | "PUT"
        self.entry_price  = 0.0
        self.entry_time   = ""
        self.entry_mode   = ""          # "STRONG" | "PULLBACK"
        self.order_id_in  = ""
        self.qty          = 0
        self.instrument   = ""
        self.candle_count = 0           # for position sync cadence
        self.cum_pnl_rs   = 0.0
        self.daily_pnl_rs = 0.0
        self.trade_count  = 0

    # ── helpers ──────────────────────────────────────────────────

    def _ltp(self, key: str) -> float:
        ltp = get_ltp(key, self.hdrs)
        return ltp if ltp else 0.0

    def _mode_label(self) -> str:
        return "(PAPER)" if self.paper_mode else "(LIVE)"

    # ── entry ─────────────────────────────────────────────────────

    def open_call(self, futures_price: float, ts_str: str, mode: str) -> None:
        qty = QTY_CALL
        ltp = self._ltp(BUY_KEY) or futures_price

        order_id = "SIMULATED"
        fill_px  = ltp

        if not self.paper_mode and ENABLE_LIVE_ORDERS:
            order_id = place_order(BUY_KEY, "BUY", qty, self.hdrs) or "FAILED"
            if order_id == "FAILED":
                log.error("BUY CALL order failed — skipping entry.")
                send_telegram(f"❌ *BUY CALL order FAILED*\nTime: {ts_str}")
                return
            fill_info = wait_for_fill(order_id, self.hdrs)
            fill_px   = fill_info.get("avg_price") or ltp

        self.side        = "CALL"
        self.entry_price = fill_px
        self.entry_time  = ts_str
        self.entry_mode  = mode
        self.order_id_in = order_id
        self.qty         = qty
        self.instrument  = BUY_KEY

        log.info("📥 CALL ENTRY %s | price=%.2f qty=%d order=%s mode=%s",
                 self._mode_label(), fill_px, qty, order_id, mode)
        send_telegram(
            f"🟢 *BUY CALL* {self._mode_label()}\n"
            f"Instrument : `{BUY_KEY}`\n"
            f"Entry Price: ₹{fill_px:.2f}\n"
            f"Quantity   : {qty}\n"
            f"Mode       : {mode}\n"
            f"Futures    : {futures_price:.1f}\n"
            f"Order ID   : `{order_id}`\n"
            f"Time       : {ts_str}"
        )

    def open_put(self, futures_price: float, ts_str: str, mode: str) -> None:
        qty = QTY_PUT
        ltp = self._ltp(SELL_KEY) or futures_price

        order_id = "SIMULATED"
        fill_px  = ltp

        if not self.paper_mode and ENABLE_LIVE_ORDERS:
            order_id = place_order(SELL_KEY, "BUY", qty, self.hdrs) or "FAILED"
            if order_id == "FAILED":
                log.error("BUY PUT order failed — skipping entry.")
                send_telegram(f"❌ *BUY PUT order FAILED*\nTime: {ts_str}")
                return
            fill_info = wait_for_fill(order_id, self.hdrs)
            fill_px   = fill_info.get("avg_price") or ltp

        self.side        = "PUT"
        self.entry_price = fill_px
        self.entry_time  = ts_str
        self.entry_mode  = mode
        self.order_id_in = order_id
        self.qty         = qty
        self.instrument  = SELL_KEY

        log.info("📥 PUT ENTRY %s | price=%.2f qty=%d order=%s mode=%s",
                 self._mode_label(), fill_px, qty, order_id, mode)
        send_telegram(
            f"🔴 *BUY PUT* {self._mode_label()}\n"
            f"Instrument : `{SELL_KEY}`\n"
            f"Entry Price: ₹{fill_px:.2f}\n"
            f"Quantity   : {qty}\n"
            f"Mode       : {mode}\n"
            f"Futures    : {futures_price:.1f}\n"
            f"Order ID   : `{order_id}`\n"
            f"Time       : {ts_str}"
        )

    # ── exit ──────────────────────────────────────────────────────

    def close_position(self, ts_str: str, reason: str,
                       futures_ltp: float = 0.0) -> Optional[dict]:
        if not self.side:
            return None

        is_call = self.side == "CALL"
        key     = BUY_KEY if is_call else SELL_KEY
        ltp     = self._ltp(key) or futures_ltp

        order_id_out = "SIMULATED"
        exit_px      = ltp

        if not self.paper_mode and ENABLE_LIVE_ORDERS:
            order_id_out = place_order(key, "SELL", self.qty, self.hdrs) or "FAILED"
            if order_id_out != "FAILED":
                fill_info = wait_for_fill(order_id_out, self.hdrs)
                exit_px   = fill_info.get("avg_price") or ltp

        # P&L calculation
        if is_call:
            pnl_pts = exit_px - self.entry_price
        else:
            pnl_pts = self.entry_price - exit_px

        pnl_rs          = pnl_pts * self.qty
        self.cum_pnl_rs  += pnl_rs
        self.daily_pnl_rs += pnl_rs
        self.trade_count  += 1

        row = {
            "mode":              "paper" if self.paper_mode else "live",
            "date":              now_ist().strftime("%Y-%m-%d"),
            "entry_time":        self.entry_time,
            "exit_time":         ts_str,
            "side":              self.side,
            "instrument":        self.instrument,
            "entry_price":       round(self.entry_price, 2),
            "exit_price":        round(exit_px, 2),
            "qty":               self.qty,
            "pnl_pts":           round(pnl_pts, 2),
            "pnl_rs":            round(pnl_rs, 2),
            "exit_reason":       reason,
            "order_id_entry":    self.order_id_in,
            "order_id_exit":     order_id_out,
            "cumulative_pnl_rs": round(self.cum_pnl_rs, 2),
        }

        log_trade_csv(row)
        log_trade_mongo(row)

        pnl_icon = "✅" if pnl_rs >= 0 else "🔴"
        log.info("📤 %s EXIT %s | exit=%.2f pnl=₹%.0f (%+.1f pts) reason=%s order=%s",
                 self.side, self._mode_label(), exit_px, pnl_rs, pnl_pts,
                 reason, order_id_out)
        send_telegram(
            f"🛑 *EXIT {self.side}* {pnl_icon} {self._mode_label()}\n"
            f"Instrument : `{self.instrument}`\n"
            f"Exit Price : ₹{exit_px:.2f}\n"
            f"Trade P&L  : ₹{pnl_rs:+.0f} ({pnl_pts:+.1f} pts)\n"
            f"Cumul P&L  : ₹{self.cum_pnl_rs:+.0f}\n"
            f"Reason     : {reason}\n"
            f"Order ID   : `{order_id_out}`\n"
            f"Time       : {ts_str}"
        )

        # Reset state
        prev_side   = self.side
        self.side   = None
        self.entry_price = self.qty = 0
        self.order_id_in = self.instrument = ""

        return row

    # ── Upstox position sync ──────────────────────────────────────

    def sync_with_broker(self) -> None:
        """
        Cross-check local state against Upstox real positions.
        Corrects discrepancies (e.g. order got rejected without us knowing).
        """
        if self.paper_mode or not ENABLE_LIVE_ORDERS:
            return
        self.candle_count += 1
        if self.candle_count % POSITION_SYNC_EVERY != 0:
            return

        positions = get_upstox_positions(self.hdrs)

        if self.side == "CALL":
            pos = positions.get(BUY_KEY, {})
            if pos.get("qty", 0) == 0 and self.entry_price > 0:
                log.warning("⚠️  Broker shows no CALL position but local state is OPEN. Resetting.")
                send_telegram("⚠️ *Position Mismatch*: CALL closed by broker externally.")
                self.side = None; self.entry_price = 0; self.qty = 0

        elif self.side == "PUT":
            pos = positions.get(SELL_KEY, {})
            if pos.get("qty", 0) == 0 and self.entry_price > 0:
                log.warning("⚠️  Broker shows no PUT position but local state is OPEN. Resetting.")
                send_telegram("⚠️ *Position Mismatch*: PUT closed by broker externally.")
                self.side = None; self.entry_price = 0; self.qty = 0

    # ── circuit breaker ───────────────────────────────────────────

    def daily_loss_breached(self) -> bool:
        if MAX_DAILY_LOSS_RS <= 0:
            return False
        return self.daily_pnl_rs <= -abs(MAX_DAILY_LOSS_RS)

    # ── open P&L display ─────────────────────────────────────────

    def open_pnl(self) -> float:
        if not self.side:
            return 0.0
        key = BUY_KEY if self.side == "CALL" else SELL_KEY
        ltp = self._ltp(key)
        if not ltp:
            return 0.0
        if self.side == "CALL":
            return (ltp - self.entry_price) * self.qty
        return (self.entry_price - ltp) * self.qty


# ─────────────────────────────────────────────────────────────────
# 11.  MAIN LIVE LOOP
# ─────────────────────────────────────────────────────────────────

def run_live(paper_mode: bool = False):
    hdrs = auth_headers()
    if not hdrs:
        log.error("No auth token — aborting.")
        return

    mode_label = "PAPER SIMULATION" if paper_mode else \
                 ("LIVE ORDERS ✅"   if ENABLE_LIVE_ORDERS else "PAPER (live_orders=False)")

    log.info("=" * 70)
    log.info("NIFTY LIVE TRADER  |  %s", mode_label)
    log.info("Futures  : %s  |  TF: %s  |  Poll: %ss", FUTURES_KEY, TIMEFRAME, LOOP_INTERVAL)
    log.info("CE key   : %s  QTY=%d", BUY_KEY, QTY_CALL)
    log.info("PE key   : %s  QTY=%d", SELL_KEY, QTY_PUT)
    log.info("Sq-off   : %s IST  |  Max loss: ₹%s",
             SQUAREOFF_TIME, MAX_DAILY_LOSS_RS if MAX_DAILY_LOSS_RS else "disabled")
    log.info("Zones    : NT[%s–%s]  R1=%s R2=%s  S1=%s S2=%s",
             NT_LOWER, NT_UPPER, R1, R2, S1, S2)
    log.info("=" * 70)

    if not is_market_open():
        mins = minutes_to_open()
        log.info("Market closed. %d min to open (09:15 IST).", mins)

    pos = PositionManager(hdrs, paper_mode=paper_mode)
    last_ts = None
    squaredoff_today = False

    send_telegram(
        f"🚀 *Nifty Live Trader STARTED*\n"
        f"Mode     : {mode_label}\n"
        f"CE Key   : `{BUY_KEY}`\n"
        f"PE Key   : `{SELL_KEY}`\n"
        f"Futures  : `{FUTURES_KEY}`\n"
        f"TF       : {TIMEFRAME}\n"
        f"Sq-off   : {SQUAREOFF_TIME} IST\n"
        f"Time     : {now_ist_str()}"
    )

    print(f"\n{'TIME':<6} {'FUT':>8} {'EMA7':>8} {'EMA21':>8} {'VWAP':>8} "
          f"{'OI':>6} {'VIX':>5} {'SIGNAL':<24} {'OPEN P&L':>10}")
    print("─" * 100)

    while True:
        loop_start = time.time()
        try:
            n = now_ist()

            # ── Pre-market sleep ───────────────────────────────
            if not is_market_open():
                if n.weekday() >= 5:
                    log.info("Weekend — sleeping 30 min.")
                    time.sleep(1800)
                else:
                    log.info("Market closed. Sleeping %ds.", LOOP_INTERVAL * 4)
                    time.sleep(LOOP_INTERVAL * 4)
                squaredoff_today = False   # reset for next session
                pos.daily_pnl_rs = 0.0
                continue

            # ── Auto square-off ────────────────────────────────
            if is_squareoff_time() and not squaredoff_today:
                if pos.side:
                    log.warning("⏰ Auto square-off at %s", SQUAREOFF_TIME)
                    pos.close_position(n.strftime("%H:%M"), "Auto Square-off EOD")
                squaredoff_today = True
                log.info("Square-off done. No new entries today.")
                time.sleep(LOOP_INTERVAL)
                continue

            if squaredoff_today:
                time.sleep(LOOP_INTERVAL)
                continue

            # ── Circuit breaker ────────────────────────────────
            if pos.daily_loss_breached():
                if pos.side:
                    log.warning("🛑 Daily loss limit ₹%.0f breached — square-off.", MAX_DAILY_LOSS_RS)
                    send_telegram(f"🛑 *Daily Loss Limit Hit* ₹{pos.daily_pnl_rs:.0f}\nClosing position.")
                    pos.close_position(n.strftime("%H:%M"), "Daily Loss Limit")
                log.warning("No new entries today (daily loss limit).")
                time.sleep(LOOP_INTERVAL)
                continue

            # ── Fetch candles ──────────────────────────────────
            raw = fetch_intraday(FUTURES_KEY, hdrs)
            if raw is None or len(raw) < 5:
                log.warning("Insufficient candle data — retrying.")
                time.sleep(LOOP_INTERVAL)
                continue

            raw = raw.iloc[:-1].copy()      # drop live (incomplete) candle
            df  = resample(raw)

            # Confirm candle is fully closed
            now_dt = now_ist()
            df = df[df["ts"] + pd.Timedelta(PANDAS_TF) <= now_dt].copy()

            if len(df) < 3:
                log.info("Waiting for closed candles...")
                time.sleep(LOOP_INTERVAL)
                continue

            df = add_indicators(df)
            curr = df.iloc[-1]
            prev = df.iloc[-2]

            if last_ts is not None and curr["ts"] <= last_ts:
                elapsed = time.time() - loop_start
                time.sleep(max(0, LOOP_INTERVAL - elapsed))
                continue

            last_ts  = curr["ts"]
            ts_str   = curr["ts"].strftime("%H:%M")
            oi_lbl   = OI_LABEL.get(curr["oi_act"], curr["oi_act"])
            vix      = get_vix(hdrs)

            # Position sync
            pos.sync_with_broker()

            signal_txt = ""
            pnl_txt    = ""

            # ── EXIT ──────────────────────────────────────────
            if pos.side == "CALL":
                exited, reason = call_exit(curr, prev, pos.entry_mode)
                if exited:
                    pos.close_position(ts_str, reason, futures_ltp=curr["c"])
                    signal_txt = f"🛑 EXIT CALL"
                    pnl_txt    = ""

            elif pos.side == "PUT":
                exited, reason = put_exit(curr, prev, pos.entry_mode)
                if exited:
                    pos.close_position(ts_str, reason, futures_ltp=curr["c"])
                    signal_txt = f"🛑 EXIT PUT"
                    pnl_txt    = ""

            # ── ENTRY ─────────────────────────────────────────
            if pos.side is None and not squaredoff_today and not pos.daily_loss_breached():
                sig, mode = call_signal(curr, prev)
                if sig == "BUY_CALL":
                    pos.open_call(curr["c"], ts_str, mode)
                    signal_txt = f"🟢 BUY CALL ({mode})"
                else:
                    sig, mode = put_signal(curr, prev)
                    if sig == "BUY_PUT":
                        pos.open_put(curr["c"], ts_str, mode)
                        signal_txt = f"🔴 BUY PUT ({mode})"

            # ── Status display ─────────────────────────────────
            if not signal_txt:
                if pos.side:
                    opnl = pos.open_pnl()
                    signal_txt = f"{'📈' if pos.side=='CALL' else '📉'} HOLDING {pos.side}"
                    pnl_txt    = f"₹{opnl:+.0f}"
                else:
                    # Check if blocked
                    price = curr["c"]
                    if (curr["oi_act"] in BULLISH_OI and prev["oi_act"] in BULLISH_OI
                            and price > curr["ema7"]):
                        ok, blk = call_allowed(price)
                        if not ok:
                            signal_txt = f"⛔ CALL BLOCKED"
                            pnl_txt    = blk[:16]
                    elif (curr["oi_act"] in BEARISH_OI and prev["oi_act"] in BEARISH_OI
                            and price < curr["ema7"]):
                        ok, blk = put_allowed(price)
                        if not ok:
                            signal_txt = f"⛔ PUT BLOCKED"
                            pnl_txt    = blk[:16]
                    else:
                        signal_txt = "👀 WATCHING"

            end_char = "\n" if "HOLDING" not in signal_txt else "\r"
            print(
                f"{ts_str:<6} {curr['c']:>8.1f} {curr['ema7']:>8.1f} "
                f"{curr['ema21']:>8.1f} {curr['vwap']:>8.1f} "
                f"{oi_lbl:>6} {vix:>5.1f} {signal_txt:<24} {pnl_txt:>10}",
                end=end_char,
            )
            sys.stdout.flush()

        except KeyboardInterrupt:
            print("\n")
            log.info("Bot stopped by user.")
            if pos.side:
                log.info("Closing open %s position on exit.", pos.side)
                pos.close_position(now_ist().strftime("%H:%M"), "User exit (Ctrl-C)")

            log.info("Session summary | Trades=%d | Cumul P&L=₹%.0f",
                     pos.trade_count, pos.cum_pnl_rs)
            send_telegram(
                f"⏹ *Bot STOPPED*\n"
                f"Trades   : {pos.trade_count}\n"
                f"Daily P&L: ₹{pos.daily_pnl_rs:+.0f}\n"
                f"Cumul P&L: ₹{pos.cum_pnl_rs:+.0f}\n"
                f"Time     : {now_ist_str()}"
            )
            break

        except Exception as exc:
            log.error("Loop error: %s", exc, exc_info=True)

        elapsed = time.time() - loop_start
        time.sleep(max(0, LOOP_INTERVAL - elapsed))


# ─────────────────────────────────────────────────────────────────
# 12.  SIMULATE — replay today's closed candles
# ─────────────────────────────────────────────────────────────────

def run_simulate():
    """
    Replay today's already-closed candles through the full strategy.
    Useful for dry-run before market opens or post-session review.
    No orders placed. No live data streaming.
    """
    log.info("SIMULATE MODE — replaying today's closed candles")
    hdrs = auth_headers()
    if not hdrs:
        return

    raw = fetch_intraday(FUTURES_KEY, hdrs)
    if raw is None or raw.empty:
        log.error("No intraday data available.")
        return

    now_dt = now_ist()
    raw    = raw.iloc[:-1].copy()      # drop forming candle
    df     = resample(raw)
    df     = df[df["ts"] + pd.Timedelta(PANDAS_TF) <= now_dt].copy()
    df     = add_indicators(df)

    print(f"\n--- SIMULATE  {now_dt.strftime('%Y-%m-%d')}  ({TIMEFRAME}) ---")
    print(f"{'TIME':<6} {'PRICE':>8} {'EMA7':>8} {'EMA21':>8} {'VWAP':>8} "
          f"{'OI':>6} {'SIGNAL':<24} {'P&L':>10}")
    print("─" * 90)

    pos = PositionManager(hdrs, paper_mode=True)

    for i in range(2, len(df)):
        curr   = df.iloc[i]
        prev   = df.iloc[i - 1]
        ts_str = curr["ts"].strftime("%H:%M")
        oi_lbl = OI_LABEL.get(curr["oi_act"], curr["oi_act"])
        sig_t  = ""
        pnl_t  = ""

        if pos.side == "CALL":
            exited, reason = call_exit(curr, prev, pos.entry_mode)
            if exited:
                row = pos.close_position(ts_str, reason, curr["c"])
                if row:
                    pnl_t = f"₹{row['pnl_rs']:+.0f}"
                sig_t = "🛑 EXIT CALL"
        elif pos.side == "PUT":
            exited, reason = put_exit(curr, prev, pos.entry_mode)
            if exited:
                row = pos.close_position(ts_str, reason, curr["c"])
                if row:
                    pnl_t = f"₹{row['pnl_rs']:+.0f}"
                sig_t = "🛑 EXIT PUT"

        if pos.side is None:
            s, m = call_signal(curr, prev)
            if s == "BUY_CALL":
                pos.open_call(curr["c"], ts_str, m)
                sig_t = f"🟢 BUY CALL ({m})"
            else:
                s, m = put_signal(curr, prev)
                if s == "BUY_PUT":
                    pos.open_put(curr["c"], ts_str, m)
                    sig_t = f"🔴 BUY PUT ({m})"

        if not sig_t and pos.side:
            opnl  = (curr["c"] - pos.entry_price if pos.side == "CALL"
                     else pos.entry_price - curr["c"]) * pos.qty
            sig_t = f"{'📈' if pos.side=='CALL' else '📉'} HOLDING {pos.side}"
            pnl_t = f"₹{opnl:+.0f}"

        if sig_t:
            print(f"{ts_str:<6} {curr['c']:>8.1f} {curr['ema7']:>8.1f} "
                  f"{curr['ema21']:>8.1f} {curr['vwap']:>8.1f} "
                  f"{oi_lbl:>6} {sig_t:<24} {pnl_t:>10}")

    print("\n" + "─" * 90)
    print(f"Trades: {pos.trade_count}  |  "
          f"P&L Today: ₹{pos.daily_pnl_rs:+.0f}  |  "
          f"Log: {TRADE_LOG}")


# ─────────────────────────────────────────────────────────────────
# 13.  ENTRY POINT
# ─────────────────────────────────────────────────────────────────

def print_banner():
    live_str = "✅ LIVE ORDERS ENABLED" if ENABLE_LIVE_ORDERS else "⚠️  PAPER MODE (enable_live_orders=False)"
    print(f"""
╔══════════════════════════════════════════════════════════╗
║          NIFTY OPTIONS — LIVE MARKET TRADER              ║
╠══════════════════════════════════════════════════════════╣
║  {live_str:<56}║
║  CE Key    : {BUY_KEY:<44}║
║  PE Key    : {SELL_KEY:<44}║
║  Futures   : {FUTURES_KEY:<44}║
║  Product   : {PRODUCT_TYPE}  |  QTY_CALL={QTY_CALL}  QTY_PUT={QTY_PUT:<20}║
║  Sq-off at : {SQUAREOFF_TIME} IST{'':<44}║
╚══════════════════════════════════════════════════════════╝
""")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Nifty Options Live Trader")
    ap.add_argument("--paper",    action="store_true",
                    help="Force paper mode (no real orders regardless of config)")
    ap.add_argument("--simulate", action="store_true",
                    help="Replay today's closed candles offline")
    args = ap.parse_args()

    print_banner()

    if args.simulate:
        run_simulate()
    elif args.paper:
        log.info("FORCED PAPER MODE via --paper flag")
        run_live(paper_mode=True)
    else:
        if ENABLE_LIVE_ORDERS:
            confirm = input(
                "\n⚠️  LIVE ORDERS ARE ENABLED.\n"
                f"   CE: {BUY_KEY}  QTY={QTY_CALL}\n"
                f"   PE: {SELL_KEY}  QTY={QTY_PUT}\n"
                "   Type  YES  to confirm: "
            ).strip()
            if confirm != "YES":
                log.info("Confirmation not received — running in paper mode.")
                run_live(paper_mode=True)
            else:
                run_live(paper_mode=False)
        else:
            log.info("enable_live_orders=False in config — paper simulation.")
            run_live(paper_mode=True)
