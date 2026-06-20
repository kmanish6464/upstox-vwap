"""
1603_v2  (SR-Guard Edition)
=========================================================
ARCHITECTURE
  • GoCharting LIPI (MANISH0604) validates delta, EMA, VWAP, time, and S/R
    zone before firing — but the bot enforces ALL guards independently as a
    second line of defence so a stale/old LIPI script can never bypass them.

  • Bot guards (all apply to BUY_CALL and BUY_PUT only):
      1. Market-open cooldown  : no entry 09:15 – 09:22 (configurable).
      2. Session-close guard   : no entry after 15:15.
      3. S/R no-trade zone     : no entry when Nifty spot is between
                                  support and resistance levels.
                                  Entry allowed ONLY above resistance
                                  or below support.

  • EXIT signals are NEVER blocked by any guard — positions always close.

SETUP
-----
1. pip install flask requests pytz
2. Run ngrok: ngrok http 5000
3. GoCharting alert messages (all four alerts in MANISH0604):

     BUY_CALL  : BUY_CALL — Bull delta confirmed. Close: {{close}}
     BUY_PUT   : BUY_PUT — Bear delta confirmed. Close: {{close}}
     EXIT_CALL : EXIT_CALL — BUY exit triggered. Close: {{close}}
     EXIT_PUT  : EXIT_PUT — SELL exit triggered. Close: {{close}}

4. config.ini:
     [UPSTOX]
     token_file          = token.txt
     Buy_instrument_key  = NSE_FO|XXXXX    <- CE instrument key
     Sell_instrument_key = NSE_FO|XXXXX    <- PE instrument key
     Nifty_index_key     = NSE_INDEX|Nifty 50

     [TELEGRAM]
     bot_token       = <token>
     channel_id      = <id>
     enable_telegram = true

     [RISK]
     market_open_cooldown = 7       <- minutes after 09:15 before first entry
     resistance           = 23063   <- must match MANISH0604 Resistance Level
     support              = 22953   <- must match MANISH0604 Support Level

QUICK TEST
----------
   http://localhost:5000/test_signal?signal=BUY_CALL&price=24500
   http://localhost:5000/test_signal?signal=EXIT_CALL&price=24600
   http://localhost:5000/test_signal?signal=BUY_PUT&price=24400
   http://localhost:5000/test_signal?signal=EXIT_PUT&price=24500
   http://localhost:5000/status
   http://localhost:5000/health
   POST http://localhost:5000/reset
"""

import os
import re
import sys
import csv
import json
import logging
import configparser
import threading
from datetime import datetime, time as dtime

import pytz
import requests
from flask import Flask, request, jsonify


# -----------------------------------------------------------------
# 0.  LOGGING
# -----------------------------------------------------------------

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
        logging.FileHandler("webhook_paper_trade.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# -----------------------------------------------------------------
# 1.  CONFIG
# -----------------------------------------------------------------

config = configparser.ConfigParser()
config.read("config.ini")

TOKEN_FILE  = config.get("UPSTOX", "token_file",           fallback="token.txt")
CALL_KEY    = config.get("UPSTOX", "Buy_instrument_key",   fallback="NSE_FO|54905")
PUT_KEY     = config.get("UPSTOX", "Sell_instrument_key",  fallback="NSE_FO|54904")
NIFTY_KEY   = config.get("UPSTOX", "Nifty_index_key",      fallback="NSE_INDEX|Nifty 50")

BOT_TOKEN       = config.get("TELEGRAM", "bot_token",            fallback="")
CHANNEL_ID      = config.get("TELEGRAM", "channel_id",           fallback="")
ENABLE_TELEGRAM = config.getboolean("TELEGRAM", "enable_telegram", fallback=False)

MARKET_OPEN_COOLDOWN = config.getint(  "RISK", "market_open_cooldown", fallback=7)
RESISTANCE           = config.getfloat("RISK", "resistance",           fallback=0.0)
SUPPORT              = config.getfloat("RISK", "support",              fallback=0.0)

INITIAL_CAPITAL = 100_000.0
TRADE_QTY       = 65
SLIPPAGE_PCT    = 0.0005

IST           = pytz.timezone("Asia/Kolkata")
UPSTOX_QUOTE  = "https://api.upstox.com/v2/market-quote/quotes"
LOG_FILE      = "paper_trades_log.csv"

_MARKET_OPEN  = dtime(9, 15, 0)
_SESSION_END  = dtime(15, 15, 0)


# -----------------------------------------------------------------
# 2.  SHARED STATE
# -----------------------------------------------------------------

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


# -----------------------------------------------------------------
# 3.  UTILITIES
# -----------------------------------------------------------------

def now_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def load_token() -> str | None:
    if not os.path.exists(TOKEN_FILE):
        log.error("Token file '%s' not found.", TOKEN_FILE)
        return None
    with open(TOKEN_FILE) as fh:
        return fh.read().strip()


def upstox_headers() -> dict:
    tok = load_token()
    return {"Authorization": f"Bearer {tok}", "Accept": "application/json"} if tok else {}


def get_ltp(instrument_key: str) -> float | None:
    hdrs = upstox_headers()
    if not hdrs:
        log.error("get_ltp: no auth headers — token missing.")
        return None
    try:
        resp = requests.get(
            UPSTOX_QUOTE,
            headers=hdrs,
            params={"instrument_key": instrument_key},
            timeout=5,
        )
        log.info("LTP API HTTP %s for %s | body: %s",
                 resp.status_code, instrument_key, resp.text[:200])

        if resp.status_code == 200:
            data  = resp.json().get("data", {})
            entry = next(iter(data.values()), {})
            ltp   = (entry.get("last_price")
                     or entry.get("ltp")
                     or entry.get("close_price"))
            if ltp:
                return float(ltp)
            log.warning("get_ltp: no price field in response for %s", instrument_key)
        elif resp.status_code == 401:
            log.error("get_ltp: 401 Unauthorized — token expired. Refresh token.txt.")
        else:
            log.warning("Upstox quote API HTTP %s for %s", resp.status_code, instrument_key)

    except Exception as exc:
        log.error("get_ltp error: %s", exc)
    return None


def get_nifty_spot() -> float | None:
    """Fetch current Nifty 50 spot price for the S/R zone check."""
    return get_ltp(NIFTY_KEY)


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram(message: str) -> None:
    if not ENABLE_TELEGRAM or not BOT_TOKEN:
        return
    url     = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHANNEL_ID, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code == 200:
            log.info("Telegram sent OK")
        else:
            log.warning("Telegram HTTP %s: %s", resp.status_code, resp.text[:300])
    except Exception as exc:
        log.error("Telegram send error: %s", exc)


def append_trade_csv(entry_time, exit_time, instrument, side, qty,
                     entry_price, exit_price, pnl, cum_pnl, reason):
    new_file = not os.path.isfile(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow([
                "Entry Time", "Exit Time", "Instrument", "Side", "Qty",
                "Entry Price", "Exit Price", "Trade P&L", "Cumulative P&L", "Reason",
            ])
        w.writerow([
            entry_time, exit_time, instrument, side, qty,
            round(entry_price, 2), round(exit_price, 2),
            round(pnl, 2), round(cum_pnl, 2), reason,
        ])
    log.info("Trade CSV logged: %s %s | P&L=%.2f", instrument, side, pnl)


def portfolio_log():
    call_s = f"CALL=ACTIVE@{state['call_entry_price']:.2f}" if state["call_active"] else "CALL=IDLE"
    put_s  = f"PUT=ACTIVE@{state['put_entry_price']:.2f}"  if state["put_active"]  else "PUT=IDLE"
    log.info(
        "Portfolio | Capital=%.2f | Realized_PNL=%.2f | Trades=%d | %s | %s",
        state["capital"], state["realized_pnl"], state["trade_count"], call_s, put_s,
    )


# -----------------------------------------------------------------
# 4.  SIGNAL PARSING
# -----------------------------------------------------------------

_SIGNAL_ALIASES: dict[str, str] = {
    "buy call":  "BUY_CALL",
    "buy_call":  "BUY_CALL",
    "buycall":   "BUY_CALL",
    "buy":       "BUY_CALL",

    "buy put":   "BUY_PUT",
    "buy_put":   "BUY_PUT",
    "buyput":    "BUY_PUT",
    "sell":      "BUY_PUT",

    "exit call": "EXIT_CALL",
    "exit_call": "EXIT_CALL",
    "exitcall":  "EXIT_CALL",
    "call exit": "EXIT_CALL",
    "call_exit": "EXIT_CALL",
    "buy exit":  "EXIT_CALL",
    "buy_exit":  "EXIT_CALL",

    "exit put":  "EXIT_PUT",
    "exit_put":  "EXIT_PUT",
    "exitput":   "EXIT_PUT",
    "put exit":  "EXIT_PUT",
    "put_exit":  "EXIT_PUT",
    "sell exit": "EXIT_PUT",
    "sell_exit": "EXIT_PUT",
}


def normalise_signal(raw: str) -> str | None:
    return _SIGNAL_ALIASES.get(raw.strip().lower())


def _safe_float(val, name: str) -> float | None:
    if val is None:
        return None
    s = str(val).strip()
    if re.fullmatch(r'\{\{[^}]+\}\}', s):
        log.warning("GoCharting did not substitute '%s' — treating as missing.", s)
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def parse_webhook_body(raw_body: str) -> tuple[str, float | None]:
    """Returns (raw_signal, price)."""
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

    # Attempt 3b: GoCharting em-dash format  e.g. "BUY_CALL — Bull delta..."
    # U+2014 em-dash and U+2013 en-dash are not covered by ASCII regex ranges
    if not raw_signal:
        emdash_m = re.match(
            r'^([A-Za-z][A-Za-z_ ]*?)\s*[\u2013\u2014]',
            raw_body.strip()
        )
        if emdash_m:
            candidate = emdash_m.group(1).strip()
            if normalise_signal(candidate):
                raw_signal = candidate

    # Attempt 4: hyphen / double-space plain text  e.g. "BUY_PUT - ..."
    if not raw_signal:
        plain_m = re.match(
            r'^([A-Za-z][A-Za-z_ ]*?)(?:\s*[-:|]|\s{2,}|$)',
            raw_body.strip()
        )
        if plain_m:
            candidate = plain_m.group(1).strip()
            if normalise_signal(candidate):
                raw_signal = candidate

    # Attempt 5: extract price from "Close: 24500" pattern
    if price is None:
        price_m = re.search(r'(?:close|price)\s*[:\s]\s*([\d.]+)', raw_body, re.IGNORECASE)
        if price_m:
            price = _safe_float(price_m.group(1), "price_inline")

    return raw_signal, price


# -----------------------------------------------------------------
# 5.  ENTRY GUARDS
# -----------------------------------------------------------------

def _within_market_open_cooldown() -> bool:
    """True if still inside the cooldown window after 09:15."""
    if MARKET_OPEN_COOLDOWN <= 0:
        return False
    now_time  = datetime.now(IST).time()
    open_secs = _MARKET_OPEN.hour * 3600 + _MARKET_OPEN.minute * 60
    now_secs  = now_time.hour * 3600 + now_time.minute * 60 + now_time.second
    elapsed   = now_secs - open_secs
    return 0 <= elapsed < (MARKET_OPEN_COOLDOWN * 60)


def _after_session_end() -> bool:
    """True if current IST time is at or after 15:15."""
    return datetime.now(IST).time() >= _SESSION_END


def _in_sr_no_trade_zone() -> tuple[bool, float | None]:
    """
    Fetches Nifty spot and checks if price is inside the S/R no-trade zone.
    Returns (blocked: bool, spot_price: float | None).
    If S/R levels are not configured (both 0.0), the guard is disabled.
    """
    if RESISTANCE == 0.0 and SUPPORT == 0.0:
        return False, None                          # guard disabled

    spot = get_nifty_spot()
    if spot is None:
        log.warning("SR-Guard: Nifty spot unavailable — skipping S/R check.")
        return False, None                          # fail-open

    return (SUPPORT <= spot <= RESISTANCE), spot


# -----------------------------------------------------------------
# 6.  TRADE ACTIONS
# -----------------------------------------------------------------

def open_call(signal_price: float | None, signal_time: str):
    ltp = get_ltp(CALL_KEY) or signal_price
    if ltp is None:
        log.error("open_call: LTP unavailable.")
        send_telegram(
            f"<b>BUY CALL FAILED - No Price</b>\n"
            f"Instrument : <code>{_html_escape(CALL_KEY)}</code>\n"
            f"Time       : {signal_time}"
        )
        return

    entry  = ltp * (1 + SLIPPAGE_PCT)
    margin = entry * TRADE_QTY

    if state["capital"] < margin:
        log.warning("open_call: insufficient capital %.2f < %.2f", state["capital"], margin)
        send_telegram(
            f"<b>Insufficient Capital for BUY CALL</b>\n"
            f"Need: {margin:,.2f} | Have: {state['capital']:,.2f}"
        )
        return

    state["call_active"]      = True
    state["call_entry_price"] = entry
    state["call_entry_time"]  = signal_time
    state["capital"]         -= margin

    log.info("BUY CALL ENTRY | key=%s ltp=%.2f entry=%.2f qty=%d margin=%.2f time=%s",
             CALL_KEY, ltp, entry, TRADE_QTY, margin, signal_time)
    send_telegram(
        f"<b>PAPER BUY CALL ENTRY</b>\n"
        f"Instrument : <code>{_html_escape(CALL_KEY)}</code>\n"
        f"LTP        : {ltp:.2f}\n"
        f"Entry Price: {entry:.2f}\n"
        f"Quantity   : {TRADE_QTY}\n"
        f"Margin Used: {margin:,.2f}\n"
        f"Time       : {signal_time}"
    )
    portfolio_log()


def close_call(signal_price: float | None, signal_time: str, reason: str):
    if not state["call_active"]:
        log.info("close_call: no active CALL trade. Skipping EXIT_CALL.")
        return

    ltp = get_ltp(CALL_KEY) or signal_price
    if ltp is None:
        log.error("close_call: LTP unavailable.")
        return

    exit_px = ltp * (1 - SLIPPAGE_PCT)
    pnl     = (exit_px - state["call_entry_price"]) * TRADE_QTY
    refund  = state["call_entry_price"] * TRADE_QTY

    state["capital"]      += refund + pnl
    state["realized_pnl"] += pnl
    state["trade_count"]  += 1

    append_trade_csv(
        state["call_entry_time"], signal_time, CALL_KEY, "CALL", TRADE_QTY,
        state["call_entry_price"], exit_px, pnl, state["realized_pnl"], reason,
    )

    tag = "PROFIT" if pnl >= 0 else "LOSS"
    log.info("CALL EXIT [%s] | ltp=%.2f exit=%.2f pnl=%.2f reason=%s",
             tag, ltp, exit_px, pnl, reason)
    send_telegram(
        f"<b>PAPER CALL EXIT [{tag}]</b>\n"
        f"Instrument : <code>{_html_escape(CALL_KEY)}</code>\n"
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
    ltp = get_ltp(PUT_KEY) or signal_price
    if ltp is None:
        log.error("open_put: LTP unavailable.")
        send_telegram(
            f"<b>BUY PUT FAILED - No Price</b>\n"
            f"Instrument : <code>{_html_escape(PUT_KEY)}</code>\n"
            f"Time       : {signal_time}"
        )
        return

    entry  = ltp * (1 + SLIPPAGE_PCT)
    margin = entry * TRADE_QTY

    if state["capital"] < margin:
        log.warning("open_put: insufficient capital %.2f < %.2f", state["capital"], margin)
        send_telegram(
            f"<b>Insufficient Capital for BUY PUT</b>\n"
            f"Need: {margin:,.2f} | Have: {state['capital']:,.2f}"
        )
        return

    state["put_active"]      = True
    state["put_entry_price"] = entry
    state["put_entry_time"]  = signal_time
    state["capital"]        -= margin

    log.info("BUY PUT ENTRY | key=%s ltp=%.2f entry=%.2f qty=%d margin=%.2f time=%s",
             PUT_KEY, ltp, entry, TRADE_QTY, margin, signal_time)
    send_telegram(
        f"<b>PAPER BUY PUT ENTRY</b>\n"
        f"Instrument : <code>{_html_escape(PUT_KEY)}</code>\n"
        f"LTP        : {ltp:.2f}\n"
        f"Entry Price: {entry:.2f}\n"
        f"Quantity   : {TRADE_QTY}\n"
        f"Margin Used: {margin:,.2f}\n"
        f"Time       : {signal_time}"
    )
    portfolio_log()


def close_put(signal_price: float | None, signal_time: str, reason: str):
    if not state["put_active"]:
        log.info("close_put: no active PUT trade. Skipping EXIT_PUT.")
        return

    ltp = get_ltp(PUT_KEY) or signal_price
    if ltp is None:
        log.error("close_put: LTP unavailable.")
        return

    exit_px = ltp * (1 - SLIPPAGE_PCT)
    pnl     = (exit_px - state["put_entry_price"]) * TRADE_QTY
    refund  = state["put_entry_price"] * TRADE_QTY

    state["capital"]      += refund + pnl
    state["realized_pnl"] += pnl
    state["trade_count"]  += 1

    append_trade_csv(
        state["put_entry_time"], signal_time, PUT_KEY, "PUT", TRADE_QTY,
        state["put_entry_price"], exit_px, pnl, state["realized_pnl"], reason,
    )

    tag = "PROFIT" if pnl >= 0 else "LOSS"
    log.info("PUT EXIT [%s] | ltp=%.2f exit=%.2f pnl=%.2f reason=%s",
             tag, ltp, exit_px, pnl, reason)
    send_telegram(
        f"<b>PAPER PUT EXIT [{tag}]</b>\n"
        f"Instrument : <code>{_html_escape(PUT_KEY)}</code>\n"
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


# -----------------------------------------------------------------
# 7.  SIGNAL ROUTER
# -----------------------------------------------------------------

def route_signal(raw_signal: str, price: float | None, sig_time: str) -> dict:
    action = normalise_signal(raw_signal)

    if not action:
        log.warning("Unknown signal received: '%s'", raw_signal)
        return {"status": "ignored", "reason": f"unknown signal: '{raw_signal}'"}

    log.info("Signal normalised: '%s' -> '%s'", raw_signal, action)

    with lock:

        # Guards apply ONLY to entry signals — exits are never blocked
        if action in ("BUY_CALL", "BUY_PUT"):

            # Guard 1: market-open cooldown (09:15 to 09:15+N min)
            if _within_market_open_cooldown():
                reason = (f"BLOCKED {action} — market-open cooldown active. "
                          f"No entries within {MARKET_OPEN_COOLDOWN} min of 09:15.")
                log.warning(reason)
                send_telegram(f"<b>Trade Blocked — Opening Cooldown</b>\n"
                              f"{reason}\nTime: {sig_time}")
                return {"status": "blocked", "reason": reason}

            # Guard 2: session-end (after 15:15)
            if _after_session_end():
                reason = f"BLOCKED {action} — no entries after 15:15."
                log.warning(reason)
                send_telegram(f"<b>Trade Blocked — Session Ended</b>\n"
                              f"{reason}\nTime: {sig_time}")
                return {"status": "blocked", "reason": reason}

            # Guard 3: S/R no-trade zone
            blocked, spot = _in_sr_no_trade_zone()
            if blocked:
                reason = (f"BLOCKED {action} — Nifty spot {spot:.2f} is inside "
                          f"no-trade zone ({SUPPORT:.0f}–{RESISTANCE:.0f}). "
                          f"Entry only above {RESISTANCE:.0f} or below {SUPPORT:.0f}.")
                log.warning(reason)
                send_telegram(
                    f"<b>Trade Blocked — S/R No-Trade Zone</b>\n"
                    f"Signal    : {action}\n"
                    f"Nifty Spot: {spot:.2f}\n"
                    f"Support   : {SUPPORT:.0f}\n"
                    f"Resistance: {RESISTANCE:.0f}\n"
                    f"Time      : {sig_time}"
                )
                return {"status": "blocked", "reason": reason}

        # Execute
        if action == "BUY_CALL":
            if state["call_active"]:
                log.info("Skipped BUY_CALL — CALL already active.")
                return {"status": "skipped", "reason": "CALL trade already active"}
            if state["put_active"]:
                close_put(price, sig_time, "Opposite BUY_CALL signal received")
            open_call(price, sig_time)

        elif action == "BUY_PUT":
            if state["put_active"]:
                log.info("Skipped BUY_PUT — PUT already active.")
                return {"status": "skipped", "reason": "PUT trade already active"}
            if state["call_active"]:
                close_call(price, sig_time, "Opposite BUY_PUT signal received")
            open_put(price, sig_time)

        elif action == "EXIT_CALL":
            close_call(price, sig_time, "EXIT_CALL signal")

        elif action == "EXIT_PUT":
            close_put(price, sig_time, "EXIT_PUT signal")

    return {"status": "ok", "action": action}


# -----------------------------------------------------------------
# 8.  FLASK APP
# -----------------------------------------------------------------

app = Flask(__name__)


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw_body = request.get_data(as_text=True)
        log.info("RAW BODY    : %r", raw_body)
        log.info("Content-Type: %s", request.content_type)

        raw_signal, price = parse_webhook_body(raw_body)
        sig_time = now_ist()

        if not raw_signal:
            log.warning("Could not extract signal from body: %r", raw_body)
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
        ltp = get_ltp(CALL_KEY)
        if ltp:
            call_unreal = (ltp * (1 - SLIPPAGE_PCT) - s["call_entry_price"]) * TRADE_QTY

    if s["put_active"]:
        ltp = get_ltp(PUT_KEY)
        if ltp:
            put_unreal = (ltp * (1 - SLIPPAGE_PCT) - s["put_entry_price"]) * TRADE_QTY

    return jsonify({
        "timestamp":           now_ist(),
        "initial_capital":     INITIAL_CAPITAL,
        "available_capital":   round(s["capital"], 2),
        "realized_pnl":        round(s["realized_pnl"], 2),
        "unrealized_pnl":      round(call_unreal + put_unreal, 2),
        "net_pnl":             round(s["realized_pnl"] + call_unreal + put_unreal, 2),
        "total_trades":        s["trade_count"],
        "market_open_cooldown_min": MARKET_OPEN_COOLDOWN,
        "resistance":          RESISTANCE,
        "support":             SUPPORT,
        "call_trade": {
            "active":      s["call_active"],
            "instrument":  CALL_KEY,
            "entry_price": round(s["call_entry_price"], 2),
            "entry_time":  s["call_entry_time"],
            "unrealized":  round(call_unreal, 2),
        },
        "put_trade": {
            "active":      s["put_active"],
            "instrument":  PUT_KEY,
            "entry_price": round(s["put_entry_price"], 2),
            "entry_time":  s["put_entry_time"],
            "unrealized":  round(put_unreal, 2),
        },
    })


@app.route("/health", methods=["GET"])
def health():
    spot    = get_nifty_spot()
    in_zone = (spot is not None and RESISTANCE > 0 and SUPPORT <= spot <= RESISTANCE)
    return jsonify({
        "status":               "ok",
        "version":              "1603_v2-sr-guard",
        "time_ist":             now_ist(),
        "telegram":             ENABLE_TELEGRAM,
        "market_open_cooldown": MARKET_OPEN_COOLDOWN,
        "resistance":           RESISTANCE,
        "support":              SUPPORT,
        "nifty_spot":           spot,
        "in_no_trade_zone":     in_zone,
        "session_ended":        _after_session_end(),
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
                    "instrument":  row.get("Instrument", row.get("Symbol", "")),
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
    log.info("DAY SUMMARY [%s] | Trades=%d | PnL=%.2f | W=%d L=%d",
             target_date, len(trades), total_pnl, winners, len(trades) - winners)
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
    send_telegram(f"<b>Paper Trader RESET</b>\nCapital restored to {INITIAL_CAPITAL:,.0f}")
    return jsonify({"status": "reset", "capital": INITIAL_CAPITAL})


# -----------------------------------------------------------------
# 9.  STARTUP
# -----------------------------------------------------------------

def startup_banner():
    tok_ok = bool(load_token())
    tel_ok = ENABLE_TELEGRAM and bool(BOT_TOKEN)
    sr_ok  = RESISTANCE > 0 and SUPPORT > 0
    banner = (
        "\n"
        "+----------------------------------------------------------+\n"
        "|  1603_v2  (SR-Guard Edition)                             |\n"
        "+----------------------------------------------------------+\n"
        f"|  CALL key      : {CALL_KEY:<42}|\n"
        f"|  PUT  key      : {PUT_KEY:<42}|\n"
        f"|  Nifty key     : {NIFTY_KEY:<42}|\n"
        f"|  Capital       : Rs {INITIAL_CAPITAL:<39,.0f}|\n"
        f"|  MktOpen Guard : {MARKET_OPEN_COOLDOWN} min after 09:15{'':<29}|\n"
        f"|  Session End   : 15:15 IST{'':<33}|\n"
        f"|  Resistance    : {RESISTANCE:<42}|\n"
        f"|  Support       : {SUPPORT:<42}|\n"
        f"|  SR Guard      : {'ACTIVE' if sr_ok else 'DISABLED — set resistance+support in config.ini':<42}|\n"
        f"|  Token         : {'OK' if tok_ok else 'MISSING':<42}|\n"
        f"|  Telegram      : {'ENABLED (HTML mode)' if tel_ok else 'DISABLED':<42}|\n"
        f"|  Time IST      : {now_ist():<42}|\n"
        "+----------------------------------------------------------+\n"
        "|  Webhook  -> POST  http://localhost:5000/webhook         |\n"
        "|  Status   -> GET   http://localhost:5000/status          |\n"
        "|  Summary  -> GET   http://localhost:5000/day_summary     |\n"
        "|  Test     -> GET   http://localhost:5000/test_signal     |\n"
        "|  Health   -> GET   http://localhost:5000/health          |\n"
        "|  Reset    -> POST  http://localhost:5000/reset           |\n"
        "+----------------------------------------------------------+\n"
    )
    print(banner)
    send_telegram(
        f"<b>1603_v2 SR-Guard STARTED</b>\n"
        f"Capital       : {INITIAL_CAPITAL:,.0f}\n"
        f"MktOpen Guard : {MARKET_OPEN_COOLDOWN} min after 09:15\n"
        f"Session End   : 15:15 IST\n"
        f"Resistance    : {RESISTANCE:.0f}\n"
        f"Support       : {SUPPORT:.0f}\n"
        f"CALL Key      : <code>{_html_escape(CALL_KEY)}</code>\n"
        f"PUT  Key      : <code>{_html_escape(PUT_KEY)}</code>\n"
        f"Time          : {now_ist()}"
    )
    log.info("Server ready. Waiting for webhook signals...")


if __name__ == "__main__":
    startup_banner()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)