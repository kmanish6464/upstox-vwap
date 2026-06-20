"""
KOTAK_NTZ_OPTIONS.py
================================================
Merged from  : KOTAK_1704.py  (Kotak Neo REST auth + order execution)
Strategy from: 0804_NiftyFuture_NTZ.py (NTZ guards, CALL/PUT state, CSV log)

WHAT CHANGED vs KOTAK_1704.py
──────────────────────────────
• Trades NIFTY CE and PE OPTIONS, NOT futures.
  - call_symbol / put_symbol are read from KConfig.ini  [KOTAK] section.
  - LTP for the S/R no-trade-zone guard fetched via Kotak Quotes API
    (client.quotes) using nifty_futures_token instead of Upstox.
• Dual position tracking (call_active / put_active) copied from NTZ strategy —
  opposite-leg auto-close before a new entry.
• All three entry guards from NTZ:
    1. Market-open cooldown (configurable, default 7 min after 09:15).
    2. Session-close guard  (no new entries after 15:15).
    3. S/R no-trade-zone    (blocked when Nifty futures LTP is between
                             support and resistance).
• EXIT signals NEVER blocked.
• CSV trade log (paper_trades_log.csv) carried over from NTZ strategy.
• /day_summary, /health, /test_signal, /reset endpoints retained.
• max_trades_per_day guard from KOTAK_1704 retained as Guard 4.

KConfig.ini additions required under [KOTAK]:
    call_symbol         = NIFTY25APR24000CE   <- pTrdSymbol from ScripMaster
    put_symbol          = NIFTY25APR24000PE   <- pTrdSymbol from ScripMaster
    call_token          = 54905               <- pSymbol  from ScripMaster
    put_token           = 54904               <- pSymbol  from ScripMaster
    nifty_futures_token = 58662               <- pSymbol of current Nifty Fut

SETUP
─────
1.  pip install flask requests pyotp pytz
2.  Fill KConfig.ini (see fields above).
3.  Run:  python KOTAK_NTZ_OPTIONS.py
4.  Expose port 5000 via ngrok or VPS reverse proxy.

GoCharting / TradingView alert messages:
    BUY_CALL  → {"signal":"BUY_CALL","price":{{close}}}
    BUY_PUT   → {"signal":"BUY_PUT","price":{{close}}}
    EXIT_CALL → {"signal":"EXIT_CALL","price":{{close}}}
    EXIT_PUT  → {"signal":"EXIT_PUT","price":{{close}}}

Quick test (GET):
    http://localhost:5000/test_signal?signal=BUY_CALL&price=24500
    http://localhost:5000/status
    http://localhost:5000/health
    http://localhost:5000/day_summary
"""

import os
import re
import sys
import csv
import json
import logging
import threading
import configparser
from datetime import datetime, time as dtime

import pytz
import requests
from flask import Flask, request, jsonify

try:
    import pyotp
except ImportError:
    sys.exit("CRITICAL: pyotp not installed.  pip install pyotp")


# ─────────────────────────────────────────────────────────────────────────────
# 0.  LOGGING
# ─────────────────────────────────────────────────────────────────────────────

def _utf8_stream(stream):
    try:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


_utf8_stream(sys.stdout)
_utf8_stream(sys.stderr)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("kotak_ntz_options.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 1.  CONFIG
# ─────────────────────────────────────────────────────────────────────────────

CONFIG_FILE = "KConfig.ini"
cfg = configparser.ConfigParser()
if not os.path.exists(CONFIG_FILE):
    sys.exit(f"CRITICAL: '{CONFIG_FILE}' not found.")
cfg.read(CONFIG_FILE)

try:
    # ── Telegram ──────────────────────────────────────────────────────────────
    TG_TOKEN       = cfg.get("TELEGRAM", "bot_token",              fallback="")
    TG_CHAT_ID     = cfg.get("TELEGRAM", "chat_id",                fallback="")
    TG_ENABLED     = cfg.getboolean("TELEGRAM", "enable_telegram", fallback=True)

    # ── Kotak credentials ─────────────────────────────────────────────────────
    K_ACCESS_TOKEN = cfg.get("KOTAK", "consumer_key")
    K_MOBILE       = cfg.get("KOTAK", "mobile_number")
    K_UCC          = cfg.get("KOTAK", "client_code")
    K_MPIN         = cfg.get("KOTAK", "mpin")
    K_TOTP_SECRET  = cfg.get("KOTAK", "totp_secret").replace(" ", "")

    # ── Options instrument details (must be added to KConfig.ini) ─────────────
    # pTrdSymbol from nse_fo ScripMaster — used in place_order trading_symbol
    K_CALL_SYMBOL  = cfg.get("KOTAK", "call_symbol",  fallback="NIFTY25APR24000CE")
    K_PUT_SYMBOL   = cfg.get("KOTAK", "put_symbol",   fallback="NIFTY25APR24000PE")
    # pSymbol (instrument token) — used for live quotes
    K_CALL_TOKEN   = cfg.get("KOTAK", "call_token",   fallback="54905")
    K_PUT_TOKEN    = cfg.get("KOTAK", "put_token",    fallback="54904")
    # pSymbol of current-month Nifty futures — used for S/R zone LTP check
    K_NIFTY_FUT_TOKEN = cfg.get("KOTAK", "nifty_futures_token", fallback="58662")

    K_EXCHANGE     = cfg.get("KOTAK", "exchange_segment", fallback="nse_fo").lower()
    K_PRODUCT      = cfg.get("KOTAK", "product",          fallback="MIS").upper()
    K_QTY          = cfg.getint("KOTAK", "quantity",      fallback=50)

    # ── Risk / guards ─────────────────────────────────────────────────────────
    INITIAL_CAPITAL  = cfg.getfloat("RISK", "initial_capital",           fallback=500_000.0)
    COOLDOWN_MINS    = cfg.getint("RISK",   "market_open_cooldown_mins",  fallback=7)
    MAX_TRADES       = cfg.getint("RISK",   "max_trades_per_day",         fallback=6)
    RESISTANCE       = cfg.getfloat("RISK", "resistance",                 fallback=0.0)
    SUPPORT          = cfg.getfloat("RISK", "support",                    fallback=0.0)

except Exception as exc:
    sys.exit(f"CRITICAL: Error reading {CONFIG_FILE}: {exc}")

SLIPPAGE_PCT = 0.0005
IST          = pytz.timezone("Asia/Kolkata")
LOG_FILE     = "paper_trades_log.csv"
_MARKET_OPEN = dtime(9, 15, 0)
_SESSION_END = dtime(15, 15, 0)

# ── Kotak REST endpoints ──────────────────────────────────────────────────────
LOGIN_URL    = "https://mis.kotaksecurities.com/login/1.0/tradeApiLogin"
VALIDATE_URL = "https://mis.kotaksecurities.com/login/1.0/tradeApiValidate"
NEO_FIN_KEY  = "neotradeapi"


# ─────────────────────────────────────────────────────────────────────────────
# 2.  SHARED STATE
# ─────────────────────────────────────────────────────────────────────────────

# Kotak session (populated after login)
session = {"trading_token": None, "trading_sid": None, "base_url": None}

# Trade state (mirrored from NTZ strategy — dual CALL / PUT tracking)
lock  = threading.Lock()
state = {
    "capital":          INITIAL_CAPITAL,
    "realized_pnl":     0.0,
    "trade_count":      0,
    "call_active":      False,
    "call_entry_price": 0.0,
    "call_entry_time":  None,
    "put_active":       False,
    "put_entry_price":  0.0,
    "put_entry_time":   None,
}


# ─────────────────────────────────────────────────────────────────────────────
# 3.  UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def now_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram(message: str) -> None:
    if not TG_ENABLED or not TG_TOKEN or not TG_CHAT_ID:
        return
    url     = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code != 200:
            log.warning("Telegram HTTP %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("Telegram send error: %s", exc)


def append_trade_csv(entry_time, exit_time, symbol, side, qty,
                     entry_price, exit_price, pnl, cum_pnl, reason):
    new_file = not os.path.isfile(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow([
                "Entry Time", "Exit Time", "Symbol", "Side", "Qty",
                "Entry Price", "Exit Price", "Trade P&L", "Cumulative P&L", "Reason",
            ])
        w.writerow([
            entry_time, exit_time, symbol, side, qty,
            round(entry_price, 2), round(exit_price, 2),
            round(pnl, 2), round(cum_pnl, 2), reason,
        ])
    log.info("CSV logged: %s %s | P&L=%.2f", symbol, side, pnl)


def portfolio_log():
    call_s = (f"CALL=ACTIVE@{state['call_entry_price']:.2f}"
              if state["call_active"] else "CALL=IDLE")
    put_s  = (f"PUT=ACTIVE@{state['put_entry_price']:.2f}"
              if state["put_active"] else "PUT=IDLE")
    log.info(
        "Portfolio | Capital=%.2f | RealizedPNL=%.2f | Trades=%d | %s | %s",
        state["capital"], state["realized_pnl"], state["trade_count"], call_s, put_s,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4.  TOTP HELPER
# ─────────────────────────────────────────────────────────────────────────────

def get_totp() -> str | None:
    if K_TOTP_SECRET.isdigit() and len(K_TOTP_SECRET) == 6:
        log.warning(
            "totp_secret looks like a plain 6-digit PIN, not a Base32 seed. "
            "Re-register TOTP in the Kotak API Dashboard and paste the Base32 secret."
        )
        return K_TOTP_SECRET
    try:
        return pyotp.TOTP(K_TOTP_SECRET).now()
    except Exception as exc:
        log.error("TOTP generation failed: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 5.  AUTHENTICATION  (Kotak Neo 2-step REST)
# ─────────────────────────────────────────────────────────────────────────────

def init_kotak() -> bool:
    """
    Step 2a: POST /login/1.0/tradeApiLogin   (TOTP)
    Step 2b: POST /login/1.0/tradeApiValidate (MPIN)
    Stores trading_token, trading_sid, base_url in the global `session` dict.
    """
    global session
    log.info("Authenticating with Kotak Neo REST API…")

    totp = get_totp()
    if not totp:
        return False

    # Step 2a ─────────────────────────────────────────────────────────────────
    try:
        log.info("Step 2a: TOTP login  mobile=%s  ucc=%s  totp=%s",
                 K_MOBILE, K_UCC, totp)
        r = requests.post(
            LOGIN_URL,
            headers={
                "Authorization": K_ACCESS_TOKEN,
                "neo-fin-key":   NEO_FIN_KEY,
                "Content-Type":  "application/json",
            },
            json={"mobileNumber": K_MOBILE, "ucc": K_UCC, "totp": totp},
            timeout=10,
        )
        resp = r.json()
        log.info("Step 2a response: %s", resp)

        data = resp.get("data", {})
        if data.get("status") != "success":
            log.error("TOTP login failed: %s", resp)
            return False

        view_token = data.get("token")
        view_sid   = data.get("sid")
        if not view_token or not view_sid:
            log.error("Missing token/sid in Step 2a response.")
            return False

        log.info("Step 2a OK — view token and sid received.")

    except Exception as exc:
        log.error("Step 2a exception: %s", exc)
        return False

    # Step 2b ─────────────────────────────────────────────────────────────────
    try:
        log.info("Step 2b: Validating MPIN…")
        r2 = requests.post(
            VALIDATE_URL,
            headers={
                "Authorization": K_ACCESS_TOKEN,
                "neo-fin-key":   NEO_FIN_KEY,
                "Content-Type":  "application/json",
                "sid":           view_sid,
                "Auth":          view_token,
            },
            json={"mpin": K_MPIN},
            timeout=10,
        )
        resp2 = r2.json()
        log.info("Step 2b response: %s", resp2)

        data2 = resp2.get("data", {})
        if data2.get("status") != "success":
            log.error("MPIN validation failed: %s", resp2)
            return False

        session["trading_token"] = data2.get("token")
        session["trading_sid"]   = data2.get("sid")
        session["base_url"]      = data2.get(
            "baseUrl", "https://cis.kotaksecurities.com"
        )

        if not session["trading_token"]:
            log.error("Missing trading token in Step 2b response.")
            return False

        log.info("Authenticated! base_url=%s", session["base_url"])
        send_telegram("✅ <b>KOTAK NTZ Options Bot</b> authenticated and started.")
        return True

    except Exception as exc:
        log.error("Step 2b exception: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 6.  KOTAK QUOTES — LTP via REST
#     Uses the Kotak Quotes API (see Quotes.md) with instrument_token + exchange
# ─────────────────────────────────────────────────────────────────────────────

def _kotak_quote_ltp(instrument_token: str, exchange_segment: str) -> float | None:
    """
    Fetch LTP for a single instrument using Kotak Quotes API.
    instrument_token : pSymbol value from ScripMaster (e.g. "54905")
    exchange_segment : e.g. "nse_fo", "nse_cm"
    """
    if not session["trading_token"]:
        log.warning("_kotak_quote_ltp: not authenticated.")
        return None

    url = f"{session['base_url']}/quotes/v2.1"
    payload = {
        "instrument_tokens": [
            {"instrument_token": instrument_token, "exchange_segment": exchange_segment}
        ]
    }
    try:
        r = requests.post(
            url,
            headers={
                "Auth":         session["trading_token"],
                "Sid":          session["trading_sid"],
                "neo-fin-key":  NEO_FIN_KEY,
                "Content-Type": "application/json",
                "Accept":       "application/json",
            },
            json=payload,
            timeout=5,
        )
        if r.status_code != 200:
            log.warning("Quotes API HTTP %s for token=%s", r.status_code, instrument_token)
            return None

        data = r.json()
        # Kotak Quotes API returns data.data list; ltp field is '84ltp' or 'ltp'
        items = data.get("data", {})
        if isinstance(items, list) and items:
            entry = items[0]
        elif isinstance(items, dict):
            entry = next(iter(items.values()), {})
        else:
            log.warning("Quotes: unexpected response shape: %s", str(data)[:200])
            return None

        # Field names per webSocket.md: ltp for stocks/derivatives
        for field in ("ltp", "84ltp", "last_price", "lp"):
            val = entry.get(field)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass

        log.warning("Quotes: no LTP field found for token=%s in %s",
                    instrument_token, entry)
        return None

    except Exception as exc:
        log.error("_kotak_quote_ltp error: %s", exc)
        return None


def get_call_ltp() -> float | None:
    return _kotak_quote_ltp(K_CALL_TOKEN, K_EXCHANGE)


def get_put_ltp() -> float | None:
    return _kotak_quote_ltp(K_PUT_TOKEN, K_EXCHANGE)


def get_nifty_futures_ltp() -> float | None:
    """Fetch Nifty current-month futures LTP for S/R guard."""
    return _kotak_quote_ltp(K_NIFTY_FUT_TOKEN, K_EXCHANGE)


# ─────────────────────────────────────────────────────────────────────────────
# 7.  ORDER EXECUTION  (Kotak Neo place_order — see Place_Order.md)
# ─────────────────────────────────────────────────────────────────────────────

def _place_kotak_order(trading_symbol: str, trans_type: str, qty: int) -> bool:
    """
    Place a MARKET order via Kotak Neo quick order endpoint.
    trading_symbol : pTrdSymbol from ScripMaster (e.g. "NIFTY25APR24000CE")
    trans_type     : "B" (buy) or "S" (sell)
    qty            : number of lots * lot_size  OR  just lots depending on product
    """
    if not session["trading_token"]:
        log.error("_place_kotak_order: not authenticated.")
        return False

    url   = f"{session['base_url']}/quick/order/rule/ms/place"
    jdata = json.dumps({
        "am":  "NO",        # after market order: NO
        "dq":  "0",         # disclosed quantity
        "es":  K_EXCHANGE,  # exchange segment
        "mp":  "0",         # market protection
        "pc":  K_PRODUCT,   # product (MIS / NRML)
        "pf":  "N",
        "pr":  "0",         # price = 0 for MARKET
        "pt":  "MKT",       # price type: Market
        "qt":  str(qty),    # quantity
        "rt":  "DAY",       # validity
        "tp":  "0",         # trigger price
        "ts":  trading_symbol,
        "tt":  trans_type,  # B or S
    })

    try:
        r = requests.post(
            url,
            headers={
                "Auth":         session["trading_token"],
                "Sid":          session["trading_sid"],
                "neo-fin-key":  NEO_FIN_KEY,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=f"jData={jdata}",
            timeout=10,
        )
        resp    = r.json()
        success = resp.get("stat") == "Ok"
        if success:
            log.info("Order OK: %s %s %s | nOrdNo=%s",
                     trans_type, qty, trading_symbol, resp.get("nOrdNo"))
        else:
            log.error("Order FAILED: %s", resp)
        return success
    except Exception as exc:
        log.error("_place_kotak_order exception: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 8.  SIGNAL PARSING  (from NTZ strategy)
# ─────────────────────────────────────────────────────────────────────────────

_SIGNAL_ALIASES: dict[str, str] = {
    "buy call":  "BUY_CALL",  "buy_call":  "BUY_CALL",
    "buycall":   "BUY_CALL",  "buy":       "BUY_CALL",
    "buy put":   "BUY_PUT",   "buy_put":   "BUY_PUT",
    "buyput":    "BUY_PUT",   "sell":      "BUY_PUT",
    "exit call": "EXIT_CALL", "exit_call": "EXIT_CALL",
    "exitcall":  "EXIT_CALL", "call exit": "EXIT_CALL",
    "call_exit": "EXIT_CALL", "buy exit":  "EXIT_CALL",
    "buy_exit":  "EXIT_CALL",
    "exit put":  "EXIT_PUT",  "exit_put":  "EXIT_PUT",
    "exitput":   "EXIT_PUT",  "put exit":  "EXIT_PUT",
    "put_exit":  "EXIT_PUT",  "sell exit": "EXIT_PUT",
    "sell_exit": "EXIT_PUT",
}


def normalise_signal(raw: str) -> str | None:
    return _SIGNAL_ALIASES.get(raw.strip().lower())


def _safe_float(val, name: str) -> float | None:
    if val is None:
        return None
    s = str(val).strip()
    if re.fullmatch(r'\{\{[^}]+\}\}', s):
        log.warning("Placeholder not substituted for '%s' — treating as missing.", name)
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def parse_webhook_body(raw_body: str) -> tuple[str, float | None]:
    """Returns (raw_signal_str, price_or_None)."""
    raw_signal = ""
    price: float | None = None

    # Attempt 1: clean JSON
    try:
        payload    = json.loads(raw_body)
        raw_signal = str(payload.get("signal", "")).strip()
        price      = _safe_float(payload.get("price"), "price")
        if raw_signal:
            return raw_signal, price
    except (json.JSONDecodeError, ValueError):
        pass

    # Attempt 2: JSON with unsubstituted {{...}} placeholders
    cleaned = re.sub(r'\{\{[^}]+\}\}', 'null', raw_body)
    m       = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if m:
        try:
            payload    = json.loads(m.group())
            raw_signal = str(payload.get("signal", "")).strip()
            price      = _safe_float(payload.get("price"), "price")
            if raw_signal:
                return raw_signal, price
        except (json.JSONDecodeError, ValueError):
            pass

    # Attempt 3: pipe-separator plain text  e.g. "BUY_CALL | ..."
    prefix = re.match(r'^([A-Za-z_ ]+?)\s*\|', raw_body.strip())
    if prefix:
        raw_signal = prefix.group(1).strip()

    # Attempt 3b: em-dash / en-dash format  e.g. "BUY_CALL — Bull delta..."
    if not raw_signal:
        emdash_m = re.match(
            r'^([A-Za-z][A-Za-z_ ]*?)\s*[\u2013\u2014]',
            raw_body.strip()
        )
        if emdash_m:
            candidate = emdash_m.group(1).strip()
            if normalise_signal(candidate):
                raw_signal = candidate

    # Attempt 4: hyphen / plain text  e.g. "BUY_PUT - ..."
    if not raw_signal:
        plain_m = re.match(
            r'^([A-Za-z][A-Za-z_ ]*?)(?:\s*[-:|]|\s{2,}|$)',
            raw_body.strip()
        )
        if plain_m:
            candidate = plain_m.group(1).strip()
            if normalise_signal(candidate):
                raw_signal = candidate

    # Attempt 5: extract price from "Close: 24500" inline
    if price is None:
        price_m = re.search(
            r'(?:close|price)\s*[:\s]\s*([\d.]+)', raw_body, re.IGNORECASE
        )
        if price_m:
            price = _safe_float(price_m.group(1), "price_inline")

    return raw_signal, price


# ─────────────────────────────────────────────────────────────────────────────
# 9.  ENTRY GUARDS  (NTZ guards + KOTAK_1704 max-trades guard)
# ─────────────────────────────────────────────────────────────────────────────

def _within_market_open_cooldown() -> bool:
    if COOLDOWN_MINS <= 0:
        return False
    now_t     = datetime.now(IST).time()
    open_secs = _MARKET_OPEN.hour * 3600 + _MARKET_OPEN.minute * 60
    now_secs  = now_t.hour * 3600 + now_t.minute * 60 + now_t.second
    elapsed   = now_secs - open_secs
    return 0 <= elapsed < (COOLDOWN_MINS * 60)


def _after_session_end() -> bool:
    return datetime.now(IST).time() >= _SESSION_END


def _in_sr_no_trade_zone() -> tuple[bool, float | None]:
    """
    Returns (blocked, nifty_futures_ltp).
    Guard disabled when both SUPPORT and RESISTANCE are 0.0.
    Fails open (returns False) if LTP fetch fails — never blocks on error.
    """
    if RESISTANCE == 0.0 and SUPPORT == 0.0:
        return False, None          # guard disabled

    spot = get_nifty_futures_ltp()
    if spot is None:
        log.warning("SR-Guard: Nifty futures LTP unavailable — skipping S/R check.")
        return False, None          # fail-open

    return (SUPPORT <= spot <= RESISTANCE), spot


# ─────────────────────────────────────────────────────────────────────────────
# 10.  TRADE ACTIONS  (NTZ pattern — BUY = buy option, EXIT = sell same option)
# ─────────────────────────────────────────────────────────────────────────────

def open_call(signal_price: float | None, signal_time: str):
    """Buy CALL option (CE) — place BUY MKT order."""
    ltp = get_call_ltp() or signal_price
    if ltp is None:
        log.error("open_call: LTP unavailable.")
        send_telegram(
            f"<b>BUY CALL FAILED — No LTP</b>\n"
            f"Symbol : <code>{_html_escape(K_CALL_SYMBOL)}</code>\n"
            f"Time   : {signal_time}"
        )
        return

    entry  = ltp * (1 + SLIPPAGE_PCT)
    margin = entry * K_QTY

    if state["capital"] < margin:
        msg = (f"Insufficient capital for BUY CALL | "
               f"Need: {margin:,.2f} | Have: {state['capital']:,.2f}")
        log.warning(msg)
        send_telegram(f"<b>Insufficient Capital — BUY CALL</b>\n{msg}\nTime: {signal_time}")
        return

    # Place real order
    ok = _place_kotak_order(K_CALL_SYMBOL, "B", K_QTY)
    if not ok:
        send_telegram(
            f"<b>BUY CALL ORDER FAILED</b>\n"
            f"Symbol : <code>{_html_escape(K_CALL_SYMBOL)}</code>\n"
            f"Time   : {signal_time}"
        )
        return

    state["call_active"]      = True
    state["call_entry_price"] = entry
    state["call_entry_time"]  = signal_time
    state["capital"]         -= margin

    log.info("BUY CALL ENTRY | sym=%s ltp=%.2f entry=%.2f qty=%d time=%s",
             K_CALL_SYMBOL, ltp, entry, K_QTY, signal_time)
    send_telegram(
        f"<b>✅ BUY CALL ENTRY</b>\n"
        f"Symbol     : <code>{_html_escape(K_CALL_SYMBOL)}</code>\n"
        f"LTP        : {ltp:.2f}\n"
        f"Entry Price: {entry:.2f}\n"
        f"Quantity   : {K_QTY}\n"
        f"Margin Used: {margin:,.2f}\n"
        f"Time       : {signal_time}"
    )
    portfolio_log()


def close_call(signal_price: float | None, signal_time: str, reason: str):
    """Sell CALL option (CE) to close position — place SELL MKT order."""
    if not state["call_active"]:
        log.info("close_call: no active CALL position. Skipping.")
        return

    ltp = get_call_ltp() or signal_price
    if ltp is None:
        log.error("close_call: LTP unavailable.")
        return

    ok = _place_kotak_order(K_CALL_SYMBOL, "S", K_QTY)
    if not ok:
        send_telegram(
            f"<b>EXIT CALL ORDER FAILED</b>\n"
            f"Symbol : <code>{_html_escape(K_CALL_SYMBOL)}</code>\n"
            f"Time   : {signal_time}"
        )
        return

    exit_px = ltp * (1 - SLIPPAGE_PCT)
    pnl     = (exit_px - state["call_entry_price"]) * K_QTY
    refund  = state["call_entry_price"] * K_QTY

    state["capital"]      += refund + pnl
    state["realized_pnl"] += pnl
    state["trade_count"]  += 1

    append_trade_csv(
        state["call_entry_time"], signal_time, K_CALL_SYMBOL, "CALL", K_QTY,
        state["call_entry_price"], exit_px, pnl, state["realized_pnl"], reason,
    )

    tag = "PROFIT" if pnl >= 0 else "LOSS"
    log.info("CALL EXIT [%s] | ltp=%.2f exit=%.2f pnl=%.2f reason=%s",
             tag, ltp, exit_px, pnl, reason)
    send_telegram(
        f"<b>📊 CALL EXIT [{tag}]</b>\n"
        f"Symbol     : <code>{_html_escape(K_CALL_SYMBOL)}</code>\n"
        f"Exit Price : {exit_px:.2f}\n"
        f"Trade P&amp;L  : {pnl:+.2f}\n"
        f"Cum. P&amp;L   : {state['realized_pnl']:+.2f}\n"
        f"Capital    : {state['capital']:,.2f}\n"
        f"Reason     : {_html_escape(reason)}\n"
        f"Time       : {signal_time}"
    )
    state["call_active"]      = False
    state["call_entry_price"] = 0.0
    state["call_entry_time"]  = None
    portfolio_log()


def open_put(signal_price: float | None, signal_time: str):
    """Buy PUT option (PE) — place BUY MKT order."""
    ltp = get_put_ltp() or signal_price
    if ltp is None:
        log.error("open_put: LTP unavailable.")
        send_telegram(
            f"<b>BUY PUT FAILED — No LTP</b>\n"
            f"Symbol : <code>{_html_escape(K_PUT_SYMBOL)}</code>\n"
            f"Time   : {signal_time}"
        )
        return

    entry  = ltp * (1 + SLIPPAGE_PCT)
    margin = entry * K_QTY

    if state["capital"] < margin:
        msg = (f"Insufficient capital for BUY PUT | "
               f"Need: {margin:,.2f} | Have: {state['capital']:,.2f}")
        log.warning(msg)
        send_telegram(f"<b>Insufficient Capital — BUY PUT</b>\n{msg}\nTime: {signal_time}")
        return

    ok = _place_kotak_order(K_PUT_SYMBOL, "B", K_QTY)
    if not ok:
        send_telegram(
            f"<b>BUY PUT ORDER FAILED</b>\n"
            f"Symbol : <code>{_html_escape(K_PUT_SYMBOL)}</code>\n"
            f"Time   : {signal_time}"
        )
        return

    state["put_active"]      = True
    state["put_entry_price"] = entry
    state["put_entry_time"]  = signal_time
    state["capital"]        -= margin

    log.info("BUY PUT ENTRY | sym=%s ltp=%.2f entry=%.2f qty=%d time=%s",
             K_PUT_SYMBOL, ltp, entry, K_QTY, signal_time)
    send_telegram(
        f"<b>✅ BUY PUT ENTRY</b>\n"
        f"Symbol     : <code>{_html_escape(K_PUT_SYMBOL)}</code>\n"
        f"LTP        : {ltp:.2f}\n"
        f"Entry Price: {entry:.2f}\n"
        f"Quantity   : {K_QTY}\n"
        f"Margin Used: {margin:,.2f}\n"
        f"Time       : {signal_time}"
    )
    portfolio_log()


def close_put(signal_price: float | None, signal_time: str, reason: str):
    """Sell PUT option (PE) to close position — place SELL MKT order."""
    if not state["put_active"]:
        log.info("close_put: no active PUT position. Skipping.")
        return

    ltp = get_put_ltp() or signal_price
    if ltp is None:
        log.error("close_put: LTP unavailable.")
        return

    ok = _place_kotak_order(K_PUT_SYMBOL, "S", K_QTY)
    if not ok:
        send_telegram(
            f"<b>EXIT PUT ORDER FAILED</b>\n"
            f"Symbol : <code>{_html_escape(K_PUT_SYMBOL)}</code>\n"
            f"Time   : {signal_time}"
        )
        return

    exit_px = ltp * (1 - SLIPPAGE_PCT)
    pnl     = (exit_px - state["put_entry_price"]) * K_QTY
    refund  = state["put_entry_price"] * K_QTY

    state["capital"]      += refund + pnl
    state["realized_pnl"] += pnl
    state["trade_count"]  += 1

    append_trade_csv(
        state["put_entry_time"], signal_time, K_PUT_SYMBOL, "PUT", K_QTY,
        state["put_entry_price"], exit_px, pnl, state["realized_pnl"], reason,
    )

    tag = "PROFIT" if pnl >= 0 else "LOSS"
    log.info("PUT EXIT [%s] | ltp=%.2f exit=%.2f pnl=%.2f reason=%s",
             tag, ltp, exit_px, pnl, reason)
    send_telegram(
        f"<b>📊 PUT EXIT [{tag}]</b>\n"
        f"Symbol     : <code>{_html_escape(K_PUT_SYMBOL)}</code>\n"
        f"Exit Price : {exit_px:.2f}\n"
        f"Trade P&amp;L  : {pnl:+.2f}\n"
        f"Cum. P&amp;L   : {state['realized_pnl']:+.2f}\n"
        f"Capital    : {state['capital']:,.2f}\n"
        f"Reason     : {_html_escape(reason)}\n"
        f"Time       : {signal_time}"
    )
    state["put_active"]      = False
    state["put_entry_price"] = 0.0
    state["put_entry_time"]  = None
    portfolio_log()


# ─────────────────────────────────────────────────────────────────────────────
# 11.  SIGNAL ROUTER
# ─────────────────────────────────────────────────────────────────────────────

def route_signal(raw_signal: str, price: float | None, sig_time: str) -> dict:
    action = normalise_signal(raw_signal)

    if not action:
        log.warning("Unknown signal: '%s'", raw_signal)
        return {"status": "ignored", "reason": f"unknown signal: '{raw_signal}'"}

    log.info("Signal: '%s' → '%s'", raw_signal, action)

    with lock:

        # ── Guards apply ONLY to entry signals; exits are NEVER blocked ────────
        if action in ("BUY_CALL", "BUY_PUT"):

            # Guard 1: market-open cooldown
            if _within_market_open_cooldown():
                reason = (f"BLOCKED {action} — market-open cooldown active "
                          f"({COOLDOWN_MINS} min after 09:15).")
                log.warning(reason)
                send_telegram(
                    f"<b>Trade Blocked — Opening Cooldown</b>\n{reason}\nTime: {sig_time}"
                )
                return {"status": "blocked", "reason": reason}

            # Guard 2: session-end (after 15:15)
            if _after_session_end():
                reason = f"BLOCKED {action} — no new entries after 15:15."
                log.warning(reason)
                send_telegram(
                    f"<b>Trade Blocked — Session Ended</b>\n{reason}\nTime: {sig_time}"
                )
                return {"status": "blocked", "reason": reason}

            # Guard 3: S/R no-trade zone
            blocked, spot = _in_sr_no_trade_zone()
            if blocked:
                reason = (
                    f"BLOCKED {action} — Nifty Fut LTP {spot:.2f} inside "
                    f"no-trade zone ({SUPPORT:.0f}–{RESISTANCE:.0f}). "
                    f"Entry only above {RESISTANCE:.0f} or below {SUPPORT:.0f}."
                )
                log.warning(reason)
                send_telegram(
                    f"<b>Trade Blocked — S/R No-Trade Zone</b>\n"
                    f"Signal    : {action}\n"
                    f"Nifty Fut : {spot:.2f}\n"
                    f"Support   : {SUPPORT:.0f}\n"
                    f"Resistance: {RESISTANCE:.0f}\n"
                    f"Time      : {sig_time}"
                )
                return {"status": "blocked", "reason": reason}

            # Guard 4: max trades per day
            if state["trade_count"] >= MAX_TRADES:
                reason = (f"BLOCKED {action} — max trades per day reached "
                          f"({MAX_TRADES}).")
                log.warning(reason)
                send_telegram(
                    f"<b>Trade Blocked — Max Trades Reached</b>\n"
                    f"{reason}\nTime: {sig_time}"
                )
                return {"status": "blocked", "reason": reason}

        # ── Execute ────────────────────────────────────────────────────────────
        if action == "BUY_CALL":
            if state["call_active"]:
                return {"status": "skipped", "reason": "CALL already active"}
            if state["put_active"]:
                # Auto-close opposite leg before entering new direction
                close_put(price, sig_time, "Auto-close — opposite BUY_CALL received")
            open_call(price, sig_time)

        elif action == "BUY_PUT":
            if state["put_active"]:
                return {"status": "skipped", "reason": "PUT already active"}
            if state["call_active"]:
                close_call(price, sig_time, "Auto-close — opposite BUY_PUT received")
            open_put(price, sig_time)

        elif action == "EXIT_CALL":
            close_call(price, sig_time, "EXIT_CALL signal")

        elif action == "EXIT_PUT":
            close_put(price, sig_time, "EXIT_PUT signal")

    return {"status": "ok", "action": action}


# ─────────────────────────────────────────────────────────────────────────────
# 12.  FLASK APP
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw_body = request.get_data(as_text=True)
        log.info("RAW BODY    : %r", raw_body)
        log.info("Content-Type: %s", request.content_type)

        raw_signal, price = parse_webhook_body(raw_body)
        sig_time          = now_ist()

        if not raw_signal:
            log.warning("Could not extract signal from: %r", raw_body)
            return jsonify({"status": "error", "reason": "missing 'signal' field"}), 400

        log.info("Parsed signal='%s' price=%s", raw_signal, price)
        result = route_signal(raw_signal, price, sig_time)
        return jsonify(result), 200

    except Exception as exc:
        log.exception("Webhook error: %s", exc)
        return jsonify({"status": "error", "reason": str(exc)}), 500


@app.route("/status", methods=["GET"])
def status():
    with lock:
        s = dict(state)

    call_unreal = put_unreal = 0.0
    if s["call_active"]:
        ltp = get_call_ltp()
        if ltp:
            call_unreal = (ltp * (1 - SLIPPAGE_PCT) - s["call_entry_price"]) * K_QTY
    if s["put_active"]:
        ltp = get_put_ltp()
        if ltp:
            put_unreal = (ltp * (1 - SLIPPAGE_PCT) - s["put_entry_price"]) * K_QTY

    return jsonify({
        "timestamp":         now_ist(),
        "authenticated":     bool(session["trading_token"]),
        "initial_capital":   INITIAL_CAPITAL,
        "available_capital": round(s["capital"], 2),
        "realized_pnl":      round(s["realized_pnl"], 2),
        "unrealized_pnl":    round(call_unreal + put_unreal, 2),
        "net_pnl":           round(s["realized_pnl"] + call_unreal + put_unreal, 2),
        "total_trades":      s["trade_count"],
        "max_trades":        MAX_TRADES,
        "resistance":        RESISTANCE,
        "support":           SUPPORT,
        "exchange":          K_EXCHANGE,
        "product":           K_PRODUCT,
        "quantity":          K_QTY,
        "call_trade": {
            "active":      s["call_active"],
            "symbol":      K_CALL_SYMBOL,
            "entry_price": round(s["call_entry_price"], 2),
            "entry_time":  s["call_entry_time"],
            "unrealized":  round(call_unreal, 2),
        },
        "put_trade": {
            "active":      s["put_active"],
            "symbol":      K_PUT_SYMBOL,
            "entry_price": round(s["put_entry_price"], 2),
            "entry_time":  s["put_entry_time"],
            "unrealized":  round(put_unreal, 2),
        },
    })


@app.route("/health", methods=["GET"])
def health():
    spot    = get_nifty_futures_ltp()
    in_zone = (spot is not None and RESISTANCE > 0 and SUPPORT <= spot <= RESISTANCE)
    return jsonify({
        "status":               "ok",
        "version":              "KOTAK_NTZ_OPTIONS",
        "time_ist":             now_ist(),
        "authenticated":        bool(session["trading_token"]),
        "telegram":             TG_ENABLED,
        "market_open_cooldown": COOLDOWN_MINS,
        "resistance":           RESISTANCE,
        "support":              SUPPORT,
        "nifty_fut_ltp":        spot,
        "in_no_trade_zone":     in_zone,
        "session_ended":        _after_session_end(),
        "call_symbol":          K_CALL_SYMBOL,
        "put_symbol":           K_PUT_SYMBOL,
    })


@app.route("/test_signal", methods=["GET"])
def test_signal():
    raw = request.args.get("signal", "BUY_CALL")
    px  = float(request.args.get("price", 0) or 0) or None
    res = route_signal(raw, px, now_ist())
    return jsonify(res)


@app.route("/day_summary", methods=["GET"])
def day_summary():
    req_date = request.args.get("date", "").strip()
    if req_date:
        try:
            target_date = datetime.strptime(req_date, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"status": "error", "reason": "date must be YYYY-MM-DD"}), 400
    else:
        target_date = datetime.now(IST).date()

    if not os.path.isfile(LOG_FILE):
        return jsonify({"status": "ok", "date": str(target_date), "total_trades": 0,
                        "total_pnl": 0.0, "winners": 0, "losers": 0, "trades": []})

    trades = []
    try:
        with open(LOG_FILE, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if not row.get("Entry Time", "").startswith(str(target_date)):
                    continue
                pnl = float(row.get("Trade P&L", 0) or 0)
                trades.append({
                    "entry_time":  row.get("Entry Time", ""),
                    "exit_time":   row.get("Exit Time", ""),
                    "symbol":      row.get("Symbol", ""),
                    "side":        row.get("Side", ""),
                    "qty":         int(row.get("Qty", 0) or 0),
                    "entry_price": float(row.get("Entry Price", 0) or 0),
                    "exit_price":  float(row.get("Exit Price", 0) or 0),
                    "pnl":         round(pnl, 2),
                    "result":      "WIN" if pnl >= 0 else "LOSS",
                    "reason":      row.get("Reason", ""),
                })
    except Exception as exc:
        return jsonify({"status": "error", "reason": str(exc)}), 500

    total_pnl = round(sum(t["pnl"] for t in trades), 2)
    winners   = sum(1 for t in trades if t["pnl"] >= 0)
    return jsonify({
        "status":       "ok",
        "date":         str(target_date),
        "total_trades": len(trades),
        "total_pnl":    total_pnl,
        "winners":      winners,
        "losers":       len(trades) - winners,
        "trades":       trades,
    })


@app.route("/reset", methods=["POST"])
def reset():
    with lock:
        state.update({
            "capital":          INITIAL_CAPITAL,
            "realized_pnl":     0.0,
            "trade_count":      0,
            "call_active":      False,
            "call_entry_price": 0.0,
            "call_entry_time":  None,
            "put_active":       False,
            "put_entry_price":  0.0,
            "put_entry_time":   None,
        })
    log.info("State RESET. Capital restored to Rs %.2f", INITIAL_CAPITAL)
    send_telegram(f"<b>Bot RESET</b>\nCapital restored to {INITIAL_CAPITAL:,.0f}")
    return jsonify({"status": "reset", "capital": INITIAL_CAPITAL})


# ─────────────────────────────────────────────────────────────────────────────
# 13.  STARTUP BANNER
# ─────────────────────────────────────────────────────────────────────────────

def startup_banner():
    sr_ok = RESISTANCE > 0 and SUPPORT > 0
    banner = (
        "\n"
        "+─────────────────────────────────────────────────────────+\n"
        "|  KOTAK_NTZ_OPTIONS  — Nifty CE/PE Options Bot           |\n"
        "+─────────────────────────────────────────────────────────+\n"
        f"|  CALL symbol   : {K_CALL_SYMBOL:<40}|\n"
        f"|  PUT  symbol   : {K_PUT_SYMBOL:<40}|\n"
        f"|  Exchange      : {K_EXCHANGE:<40}|\n"
        f"|  Product       : {K_PRODUCT:<40}|\n"
        f"|  Quantity      : {K_QTY:<40}|\n"
        f"|  Capital       : Rs {INITIAL_CAPITAL:<36,.0f}|\n"
        f"|  MktOpen Guard : {COOLDOWN_MINS} min after 09:15{'':<27}|\n"
        f"|  Session End   : 15:15 IST{'':<31}|\n"
        f"|  Max Trades    : {MAX_TRADES:<40}|\n"
        f"|  Resistance    : {RESISTANCE:<40}|\n"
        f"|  Support       : {SUPPORT:<40}|\n"
        f"|  SR Guard      : {'ACTIVE' if sr_ok else 'DISABLED (set resistance+support)':<40}|\n"
        f"|  Telegram      : {'ENABLED' if TG_ENABLED else 'DISABLED':<40}|\n"
        f"|  Time IST      : {now_ist():<40}|\n"
        "+─────────────────────────────────────────────────────────+\n"
        "|  POST  http://localhost:5000/webhook                    |\n"
        "|  GET   http://localhost:5000/status                     |\n"
        "|  GET   http://localhost:5000/health                     |\n"
        "|  GET   http://localhost:5000/test_signal?signal=BUY_CALL|\n"
        "|  GET   http://localhost:5000/day_summary                |\n"
        "|  POST  http://localhost:5000/reset                      |\n"
        "+─────────────────────────────────────────────────────────+\n"
    )
    print(banner)
    send_telegram(
        f"<b>KOTAK NTZ Options Bot STARTED</b>\n"
        f"CALL : <code>{_html_escape(K_CALL_SYMBOL)}</code>\n"
        f"PUT  : <code>{_html_escape(K_PUT_SYMBOL)}</code>\n"
        f"Qty  : {K_QTY} | Product: {K_PRODUCT}\n"
        f"Capital      : {INITIAL_CAPITAL:,.0f}\n"
        f"MktOpen Guard: {COOLDOWN_MINS} min | Session End: 15:15\n"
        f"Max Trades   : {MAX_TRADES}\n"
        f"Resistance   : {RESISTANCE:.0f} | Support: {SUPPORT:.0f}\n"
        f"Time         : {now_ist()}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 14.  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not init_kotak():
        log.error("Authentication failed. Exiting.")
        sys.exit(1)
    startup_banner()
    log.info("Flask webhook server starting on port 5000…")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)