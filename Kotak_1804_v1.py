"""
Kotak Neo Webhook Trading Bot  (SR-Guard Edition)  —  v2.0
===========================================================
Authentication  : Kotak REST  (TOTP prompt at startup -> MPIN)
Config file     : KConfig.ini
Order placement : REST  POST  {baseUrl}/orders/v1/place_orders
LTP quotes      : REST  POST  {baseUrl}/quotes/v2.1
Funds / balance : REST  GET   {baseUrl}/limits/v1

SETUP
-----
1. pip install flask requests pytz pyotp
2. Fill KConfig.ini  (consumer_key, mobile_number, client_code,
   mpin, call_symbol, call_token, put_symbol, put_token,
   nifty_futures_token, resistance, support ...)
3. Run: python Live_Kotak_Neo_Webhook_Trading_Bot.py
4. Start ngrok: ngrok http 5000

SIGNAL FORMAT  (GoCharting / TradingView alert body -- JSON)
------------------------------------------------------------
   {"signal": "BUY_CALL",  "close": 24200}
   {"signal": "BUY_PUT",   "close": 24200}
   {"signal": "EXIT_CALL", "close": 24300}
   {"signal": "EXIT_PUT",  "close": 24100}

ENDPOINTS
---------
   POST  /webhook        <- receive alerts
   GET   /status         <- portfolio snapshot
   GET   /health         <- guard status + Nifty LTP
   GET   /day_summary    <- today's trades
   GET   /test_signal    <- dry-run  (?signal=BUY_CALL&price=24200)
   POST  /reset          <- reset daily counters
"""

import sys
import logging
import datetime
import configparser

import pytz
import requests
from flask import Flask, request, jsonify

# -----------------------------------------------------------------
# 0.  LOGGING
# -----------------------------------------------------------------
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("kotak_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# -----------------------------------------------------------------
# 1.  CONFIG  (KConfig.ini)
# -----------------------------------------------------------------
_cfg = configparser.ConfigParser()
_cfg.read("KConfig.ini")

def _get(section, key, fallback=""):
    try:    return _cfg.get(section, key).strip()
    except: return fallback

def _getf(section, key, fallback=0.0):
    try:    return float(_cfg.get(section, key).strip())
    except: return fallback

def _geti(section, key, fallback=0):
    try:    return int(_cfg.get(section, key).strip())
    except: return fallback

def _getb(section, key, fallback=False):
    try:    return _cfg.getboolean(section, key)
    except: return fallback

# -- Kotak --------------------------------------------------------
K_ACCESS_TOKEN   = _get("KOTAK", "consumer_key")
K_MOBILE         = _get("KOTAK", "mobile_number")
K_UCC            = _get("KOTAK", "client_code")
K_MPIN           = _get("KOTAK", "mpin")
K_TOTP_SECRET    = _get("KOTAK", "totp_secret").replace(" ", "")
CALL_SYMBOL      = _get("KOTAK", "call_symbol")
PUT_SYMBOL       = _get("KOTAK", "put_symbol")
CALL_TOKEN       = _get("KOTAK", "call_token")
PUT_TOKEN        = _get("KOTAK", "put_token")
NIFTY_TOKEN      = _get("KOTAK", "nifty_futures_token")
EXCHANGE_SEGMENT = _get("KOTAK", "exchange_segment", "nse_fo")
PRODUCT          = _get("KOTAK", "product", "MIS")
QUANTITY         = _geti("KOTAK", "quantity", 65)

# -- Telegram -----------------------------------------------------
TEL_TOKEN   = _get("TELEGRAM", "bot_token")
TEL_CHAT_ID = _get("TELEGRAM", "chat_id")
TEL_ENABLED = _getb("TELEGRAM", "enable_telegram", False)

# -- Risk ---------------------------------------------------------
INITIAL_CAPITAL = _getf("RISK", "initial_capital", 500000)
COOLDOWN_MINS   = _geti("RISK", "market_open_cooldown_mins", 7)
MAX_TRADES      = _geti("RISK", "max_trades_per_day", 6)
RESISTANCE      = _getf("RISK", "resistance", 0.0)
SUPPORT         = _getf("RISK", "support", 0.0)

# -----------------------------------------------------------------
# 2.  KOTAK REST CONSTANTS
# -----------------------------------------------------------------
_LOGIN_URL    = "https://mis.kotaksecurities.com/login/1.0/tradeApiLogin"
_VALIDATE_URL = "https://mis.kotaksecurities.com/login/1.0/tradeApiValidate"
_NEO_FIN_KEY  = "neotradeapi"
_DEFAULT_BASE = "https://cis.kotaksecurities.com"

# Session populated by authenticate()
_sess = {
    "trading_token": None,
    "trading_sid":   None,
    "base_url":      _DEFAULT_BASE,
}

# -----------------------------------------------------------------
# 3.  AUTHENTICATION  (same 2-step REST as Kotak_Option_chain.py)
# -----------------------------------------------------------------
def _get_totp() -> str:
    """
    Return 6-digit TOTP.
      - 6-digit number in KConfig.ini -> use directly.
      - Base32 seed                   -> auto-generate via pyotp.
      - Fallback                      -> prompt user.
    """
    if K_TOTP_SECRET.isdigit() and len(K_TOTP_SECRET) == 6:
        log.info("TOTP: using 6-digit value from KConfig.ini")
        return K_TOTP_SECRET

    if K_TOTP_SECRET:
        try:
            import pyotp
            otp = pyotp.TOTP(K_TOTP_SECRET).now()
            log.info("TOTP auto-generated: %s", otp)
            return otp
        except Exception as e:
            log.warning("pyotp failed (%s) -- falling back to manual entry.", e)

    while True:
        try:
            otp = input("Enter TOTP (6-digit from authenticator): ").strip()
        except EOFError:
            sys.exit("No TOTP input -- exiting.")
        if otp.isdigit() and len(otp) == 6:
            return otp
        print("Please enter exactly 6 digits.")


def authenticate() -> bool:
    """
    Step A: POST mobile + ucc + totp  ->  view_token + view_sid
    Step B: POST mpin                 ->  trading_token + sid + base_url
    """
    totp = _get_totp()

    log.info("Auth Step A: TOTP login  (mobile=%s  ucc=%s)", K_MOBILE, K_UCC)
    try:
        r = requests.post(
            _LOGIN_URL,
            headers={
                "Authorization": K_ACCESS_TOKEN,
                "neo-fin-key":   _NEO_FIN_KEY,
                "Content-Type":  "application/json",
            },
            json={"mobileNumber": K_MOBILE, "ucc": K_UCC, "totp": totp},
            timeout=10,
        )
        resp = r.json()
        data = resp.get("data", {})
        if data.get("status") != "success":
            log.error("Auth Step A failed: %s", resp)
            return False
        view_token = data["token"]
        view_sid   = data["sid"]
        log.info("Auth Step A OK")
    except Exception as e:
        log.error("Auth Step A error: %s", e)
        return False

    log.info("Auth Step B: MPIN validation ...")
    try:
        r2 = requests.post(
            _VALIDATE_URL,
            headers={
                "Authorization": K_ACCESS_TOKEN,
                "neo-fin-key":   _NEO_FIN_KEY,
                "Content-Type":  "application/json",
                "sid":           view_sid,
                "Auth":          view_token,
            },
            json={"mpin": K_MPIN},
            timeout=10,
        )
        resp2 = r2.json()
        data2 = resp2.get("data", {})
        if data2.get("status") != "success":
            log.error("Auth Step B failed: %s", resp2)
            return False
        _sess["trading_token"] = data2["token"]
        _sess["trading_sid"]   = data2["sid"]
        _sess["base_url"]      = data2.get("baseUrl", _DEFAULT_BASE).rstrip("/")
        log.info("Authenticated OK  base_url=%s", _sess["base_url"])
        return True
    except Exception as e:
        log.error("Auth Step B error: %s", e)
        return False


def _auth_headers() -> dict:
    return {
        "Auth":          _sess["trading_token"],
        "Sid":           _sess["trading_sid"],
        "neo-fin-key":   _NEO_FIN_KEY,
        "Authorization": K_ACCESS_TOKEN,
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

# -----------------------------------------------------------------
# 4.  LIVE BALANCE  (GET {baseUrl}/limits/v1)
# -----------------------------------------------------------------
def fetch_balance() -> dict:
    """Fetch live funds/margin from Kotak account."""
    if not _sess["trading_token"]:
        return {}
    url = f"{_sess['base_url']}/limits/v1"
    try:
        r = requests.get(url, headers=_auth_headers(), timeout=10)
        if r.status_code != 200:
            log.warning("Balance HTTP %s: %s", r.status_code, r.text[:200])
            return {}
        resp = r.json()
        data = resp.get("data", resp)
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict):
            return {}

        def _fv(*keys):
            for k in keys:
                v = data.get(k)
                if v is not None:
                    try: return float(v)
                    except: pass
            return None

        return {
            "available_cash": _fv("Net", "net", "availableCash",
                                  "available_cash", "AvailableMargin"),
            "used_margin":    _fv("utilisedAmount", "used_margin",
                                  "UsedMargin", "Debit"),
            "net_balance":    _fv("grossAvailableMargin",
                                  "gross_available_margin", "Balance", "balance"),
        }
    except Exception as e:
        log.error("fetch_balance error: %s", e)
        return {}

# -----------------------------------------------------------------
# 5.  LTP  (POST {baseUrl}/quotes/v2.1)
# -----------------------------------------------------------------
def fetch_ltp(instrument_token: str,
              exchange_segment: str = "nse_fo") -> float | None:
    """Fetch LTP via Kotak Quotes v2.1."""
    if not _sess["trading_token"]:
        return None
    url     = f"{_sess['base_url']}/quotes/v2.1"
    payload = {
        "instrument_tokens": [
            {"instrument_token": instrument_token,
             "exchange_segment": exchange_segment}
        ]
    }
    try:
        r     = requests.post(url, headers=_auth_headers(), json=payload, timeout=5)
        if r.status_code != 200:
            return None
        items = r.json().get("data", {})
        entry = (items[0] if isinstance(items, list) and items
                 else next(iter(items.values()), {})
                 if isinstance(items, dict) else {})
        for field in ("ltp", "84ltp", "last_price", "lp", "lastPrice"):
            val = entry.get(field)
            if val is not None:
                try: return float(val)
                except: pass
    except Exception as e:
        log.error("fetch_ltp error: %s", e)
    return None


def fetch_nifty_ltp() -> float | None:
    return fetch_ltp(NIFTY_TOKEN, EXCHANGE_SEGMENT)

# -----------------------------------------------------------------
# 6.  PLACE ORDER  (POST {baseUrl}/orders/v1/place_orders)
# -----------------------------------------------------------------
def place_order(symbol: str, token: str, side: str) -> dict | None:
    """Place a live MKT order. side: 'BUY' or 'SELL'"""
    if not _sess["trading_token"]:
        log.error("place_order: not authenticated.")
        return None
    url     = f"{_sess['base_url']}/orders/v1/place_orders"
    payload = {
        "exchange_segment":   EXCHANGE_SEGMENT,
        "product":            PRODUCT,
        "price":              "0",
        "order_type":         "MKT",
        "quantity":           str(QUANTITY),
        "validity":           "DAY",
        "trading_symbol":     symbol,
        "transaction_type":   "B" if side.upper() == "BUY" else "S",
        "amo":                "NO",
        "disclosed_quantity": "0",
        "market_protection":  "0",
        "pf":                 "N",
        "trigger_price":      "0",
        "tag":                "algo_webhook",
        "instrument_token":   token,
    }
    try:
        r        = requests.post(url, headers=_auth_headers(),
                                 json=payload, timeout=10)
        res      = r.json()
        order_id = (res.get("nOrdNo") or res.get("orderId")
                    or (res.get("data") or {}).get("nOrdNo", ""))
        if order_id:
            log.info("ORDER: %s %s  qty=%s  id=%s", side, symbol, QUANTITY, order_id)
        else:
            log.warning("Order response (no id): %s", res)
        return res
    except Exception as e:
        log.error("place_order exception: %s", e)
        return None

# -----------------------------------------------------------------
# 7.  TELEGRAM
# -----------------------------------------------------------------
def _html_escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram(message: str) -> None:
    if not TEL_ENABLED or not TEL_TOKEN or not TEL_CHAT_ID:
        return
    url     = f"https://api.telegram.org/bot{TEL_TOKEN}/sendMessage"
    payload = {"chat_id": TEL_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code == 200:
            log.info("Telegram sent OK")
        else:
            log.warning("Telegram HTTP %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        log.error("Telegram error: %s", e)

# -----------------------------------------------------------------
# 8.  TIME HELPERS
# -----------------------------------------------------------------
_IST         = pytz.timezone("Asia/Kolkata")
_SESSION_END = datetime.time(15, 15, 0)


def now_ist() -> datetime.datetime:
    return datetime.datetime.now(_IST)


def _ts() -> str:
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")


def _after_session_end() -> bool:
    return now_ist().time() >= _SESSION_END

# -----------------------------------------------------------------
# 9.  DAILY STATE
# -----------------------------------------------------------------
state = {
    "entry_count":      0,
    "total_signals":    0,
    "trades_executed":  0,
    "blocked_by_guard": 0,
    "call_active":      False,
    "call_entry_price": 0.0,
    "call_entry_time":  None,
    "put_active":       False,
    "put_entry_price":  0.0,
    "put_entry_time":   None,
    "realized_pnl":     0.0,
}

# -----------------------------------------------------------------
# 10.  FLASK APP + SIGNAL ROUTING
# -----------------------------------------------------------------
app = Flask(__name__)

_SIGNAL_MAP = {
    "buy call":  "BUY_CALL",
    "buy_call":  "BUY_CALL",
    "buycall":   "BUY_CALL",
    "buy put":   "BUY_PUT",
    "buy_put":   "BUY_PUT",
    "buyput":    "BUY_PUT",
    "exit call": "EXIT_CALL",
    "exit_call": "EXIT_CALL",
    "exitcall":  "EXIT_CALL",
    "exit put":  "EXIT_PUT",
    "exit_put":  "EXIT_PUT",
    "exitput":   "EXIT_PUT",
}


def _parse_signal(raw: str) -> str | None:
    txt = raw.strip().lower()
    for key, canonical in _SIGNAL_MAP.items():
        if txt.startswith(key):
            return canonical
    return None


def route_signal(raw_signal: str,
                 price: float | None,
                 sig_time: str) -> dict:
    state["total_signals"] += 1
    sig      = _parse_signal(raw_signal)
    is_entry = sig in ("BUY_CALL", "BUY_PUT")
    is_exit  = sig in ("EXIT_CALL", "EXIT_PUT")

    if not sig:
        return {"status": "ignored", "reason": "unrecognised signal",
                "raw": raw_signal}

    now      = now_ist()
    mkt_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    mkt_cls  = now.replace(hour=15, minute=15, second=0, microsecond=0)

    # Guard 1: session close
    if now >= mkt_cls:
        state["blocked_by_guard"] += 1
        send_telegram(f"BLOCKED (after 15:15) | {sig} | {_ts()}")
        log.warning("BLOCKED (session close): %s", sig)
        return {"status": "blocked", "reason": "session_close"}

    if is_entry:
        # Guard 2: cooldown
        if now < mkt_open + datetime.timedelta(minutes=COOLDOWN_MINS):
            state["blocked_by_guard"] += 1
            send_telegram(f"BLOCKED (cooldown) | {sig} | {_ts()}")
            log.warning("BLOCKED (cooldown): %s", sig)
            return {"status": "blocked", "reason": "cooldown"}

        # Guard 3: max trades per day
        if state["entry_count"] >= MAX_TRADES:
            state["blocked_by_guard"] += 1
            send_telegram(
                f"BLOCKED (max trades {state['entry_count']}/{MAX_TRADES})"
                f" | {sig} | {_ts()}")
            log.warning("BLOCKED (max_trades %d/%d): %s",
                        state["entry_count"], MAX_TRADES, sig)
            return {"status": "blocked", "reason": "max_trades_reached",
                    "count": state["entry_count"], "max": MAX_TRADES}

        # Guard 4: S/R no-trade zone
        if SUPPORT > 0 and RESISTANCE > 0:
            ltp = fetch_nifty_ltp() or price or 0.0
            if SUPPORT <= ltp <= RESISTANCE:
                state["blocked_by_guard"] += 1
                send_telegram(
                    f"<b>NO-TRADE ZONE</b>\nLTP: {ltp:,.2f}\n"
                    f"Zone: {SUPPORT:,.0f} - {RESISTANCE:,.0f}\n"
                    f"Signal: {sig}")
                log.warning("BLOCKED (NTZ %.2f): %s", ltp, sig)
                return {"status": "blocked", "reason": "ntz", "ltp": ltp}

        # All guards passed -- EXECUTE ENTRY
        if sig == "BUY_CALL":
            symbol, token = CALL_SYMBOL, CALL_TOKEN
            ltp_e = fetch_ltp(CALL_TOKEN, EXCHANGE_SEGMENT) or 0.0
            state.update(call_active=True, call_entry_price=ltp_e,
                         call_entry_time=_ts())
        else:
            symbol, token = PUT_SYMBOL, PUT_TOKEN
            ltp_e = fetch_ltp(PUT_TOKEN, EXCHANGE_SEGMENT) or 0.0
            state.update(put_active=True, put_entry_price=ltp_e,
                         put_entry_time=_ts())

        res      = place_order(symbol, token, "BUY")
        order_id = (res or {}).get("nOrdNo") or (res or {}).get("orderId", "—")
        state["entry_count"]     += 1
        state["trades_executed"] += 1

        send_telegram(
            f"<b>ENTRY EXECUTED</b>\n"
            f"Signal : {sig}\n"
            f"Symbol : <code>{_html_escape(symbol)}</code>\n"
            f"LTP    : {ltp_e:,.2f}\n"
            f"Qty    : {QUANTITY}\n"
            f"OrdID  : {order_id}\n"
            f"Time   : {_ts()}")
        log.info("ENTRY: %s  sym=%s  ltp=%.2f  ord=%s", sig, symbol, ltp_e, order_id)
        return {"status": "success", "action": "entry", "signal": sig,
                "order_id": order_id, "ltp": ltp_e}

    elif is_exit:
        # Exits are NEVER blocked
        if sig == "EXIT_CALL":
            symbol, token = CALL_SYMBOL, CALL_TOKEN
            entry_px = state["call_entry_price"]
            state["call_active"] = False
        else:
            symbol, token = PUT_SYMBOL, PUT_TOKEN
            entry_px = state["put_entry_price"]
            state["put_active"] = False

        ltp_x     = fetch_ltp(token, EXCHANGE_SEGMENT) or 0.0
        trade_pnl = (ltp_x - entry_px) * QUANTITY
        state["realized_pnl"]    += trade_pnl
        state["trades_executed"] += 1

        res      = place_order(symbol, token, "SELL")
        order_id = (res or {}).get("nOrdNo") or (res or {}).get("orderId", "—")

        send_telegram(
            f"<b>EXIT EXECUTED</b>\n"
            f"Signal   : {sig}\n"
            f"Symbol   : <code>{_html_escape(symbol)}</code>\n"
            f"LTP      : {ltp_x:,.2f}\n"
            f"Trade PnL: Rs {trade_pnl:,.2f}\n"
            f"OrdID    : {order_id}\n"
            f"Time     : {_ts()}")
        log.info("EXIT: %s  sym=%s  ltp=%.2f  pnl=%.2f  ord=%s",
                 sig, symbol, ltp_x, trade_pnl, order_id)
        return {"status": "success", "action": "exit", "signal": sig,
                "order_id": order_id, "trade_pnl": round(trade_pnl, 2)}

    return {"status": "ignored"}


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data       = request.get_json(silent=True) or {}
        raw_signal = str(data.get("signal", "")).strip()
        price      = float(data.get("close", 0) or 0) or None
        if not raw_signal:
            return jsonify({"status": "error",
                            "reason": "missing 'signal' field"}), 400
        log.info("Webhook: signal='%s' price=%s", raw_signal, price)
        return jsonify(route_signal(raw_signal, price, _ts())), 200
    except Exception as e:
        log.exception("Webhook error: %s", e)
        return jsonify({"status": "error", "reason": str(e)}), 500


@app.route("/status", methods=["GET"])
def status():
    call_ur = put_ur = 0.0
    if state["call_active"]:
        ltp = fetch_ltp(CALL_TOKEN, EXCHANGE_SEGMENT)
        if ltp: call_ur = (ltp - state["call_entry_price"]) * QUANTITY
    if state["put_active"]:
        ltp = fetch_ltp(PUT_TOKEN, EXCHANGE_SEGMENT)
        if ltp: put_ur = (ltp - state["put_entry_price"]) * QUANTITY

    return jsonify({
        "timestamp":        _ts(),
        "realized_pnl":     round(state["realized_pnl"], 2),
        "unrealized_pnl":   round(call_ur + put_ur, 2),
        "net_pnl":          round(state["realized_pnl"] + call_ur + put_ur, 2),
        "entry_count":      state["entry_count"],
        "max_trades":       MAX_TRADES,
        "total_signals":    state["total_signals"],
        "trades_executed":  state["trades_executed"],
        "blocked_by_guard": state["blocked_by_guard"],
        "call_trade": {
            "active":      state["call_active"],
            "symbol":      CALL_SYMBOL,
            "token":       CALL_TOKEN,
            "entry_price": round(state["call_entry_price"], 2),
            "entry_time":  state["call_entry_time"],
            "unrealized":  round(call_ur, 2),
        },
        "put_trade": {
            "active":      state["put_active"],
            "symbol":      PUT_SYMBOL,
            "token":       PUT_TOKEN,
            "entry_price": round(state["put_entry_price"], 2),
            "entry_time":  state["put_entry_time"],
            "unrealized":  round(put_ur, 2),
        },
    })


@app.route("/health", methods=["GET"])
def health():
    nifty  = fetch_nifty_ltp()
    in_ntz = (nifty is not None and RESISTANCE > 0
              and SUPPORT <= nifty <= RESISTANCE)
    return jsonify({
        "status":           "ok",
        "version":          "Kotak-Neo-v2.0-SR-Guard",
        "time_ist":         _ts(),
        "telegram":         TEL_ENABLED,
        "cooldown_mins":    COOLDOWN_MINS,
        "max_trades":       MAX_TRADES,
        "resistance":       RESISTANCE,
        "support":          SUPPORT,
        "nifty_fut_ltp":    nifty,
        "in_no_trade_zone": in_ntz,
        "session_ended":    _after_session_end(),
        "entry_count":      state["entry_count"],
    })


@app.route("/day_summary", methods=["GET"])
def day_summary():
    return jsonify({
        "date":             now_ist().strftime("%Y-%m-%d"),
        "total_signals":    state["total_signals"],
        "trades_executed":  state["trades_executed"],
        "entry_count":      state["entry_count"],
        "blocked_by_guard": state["blocked_by_guard"],
        "realized_pnl":     round(state["realized_pnl"], 2),
        "call_active":      state["call_active"],
        "put_active":       state["put_active"],
    })


@app.route("/test_signal", methods=["GET"])
def test_signal():
    raw   = request.args.get("signal", "BUY_CALL")
    price = float(request.args.get("price", 0) or 0) or None
    return jsonify(route_signal(raw, price, _ts()))


@app.route("/reset", methods=["POST"])
def reset():
    state.update({
        "entry_count": 0, "total_signals": 0, "trades_executed": 0,
        "blocked_by_guard": 0, "call_active": False,
        "call_entry_price": 0.0, "call_entry_time": None,
        "put_active": False, "put_entry_price": 0.0,
        "put_entry_time": None, "realized_pnl": 0.0,
    })
    log.info("State RESET.")
    send_telegram(f"<b>Bot RESET</b> | All daily counters cleared | {_ts()}")
    return jsonify({"status": "reset", "message": "Daily counters cleared."})

# -----------------------------------------------------------------
# 11.  STARTUP BANNER  (NTZ style)
# -----------------------------------------------------------------
def startup_banner() -> None:
    tel_ok = TEL_ENABLED and bool(TEL_TOKEN)
    sr_ok  = RESISTANCE > 0 and SUPPORT > 0
    ts     = _ts()

    # Live balance from Kotak account
    bal   = fetch_balance()
    avail = bal.get("available_cash")
    used  = bal.get("used_margin")
    net   = bal.get("net_balance")

    def _fmt(v):
        return f"Rs {v:>12,.2f}" if v is not None else "N/A"

    banner = (
        "\n"
        "+----------------------------------------------------------+\n"
        "|  Kotak Neo Webhook Bot  (SR-Guard Edition)  -- LIVE  v2  |\n"
        "+----------------------------------------------------------+\n"
        f"|  Call Symbol   : {CALL_SYMBOL:<42}|\n"
        f"|  Call Token    : {CALL_TOKEN:<42}|\n"
        f"|  Put Symbol    : {PUT_SYMBOL:<42}|\n"
        f"|  Put Token     : {PUT_TOKEN:<42}|\n"
        f"|  Nifty Fut Tok : {NIFTY_TOKEN:<42}|\n"
        f"|  Exchange      : {EXCHANGE_SEGMENT:<42}|\n"
        f"|  Product       : {PRODUCT:<42}|\n"
        f"|  Quantity      : {str(QUANTITY):<42}|\n"
        "+----------------------------------------------------------+\n"
        f"|  Avail Cash    : {_fmt(avail):<42}|\n"
        f"|  Used Margin   : {_fmt(used):<42}|\n"
        f"|  Net Balance   : {_fmt(net):<42}|\n"
        "+----------------------------------------------------------+\n"
        f"|  MktOpen Guard : {str(COOLDOWN_MINS) + ' min after 09:15':<42}|\n"
        f"|  Session End   : {'15:15 IST':<42}|\n"
        f"|  Max Trades/Day: {str(MAX_TRADES):<42}|\n"
        f"|  Resistance    : {str(RESISTANCE):<42}|\n"
        f"|  Support       : {str(SUPPORT):<42}|\n"
        f"|  SR Guard      : {'ACTIVE' if sr_ok else 'DISABLED -- set R/S in KConfig.ini':<42}|\n"
        f"|  Telegram      : {'ENABLED (HTML mode)' if tel_ok else 'DISABLED':<42}|\n"
        f"|  Time IST      : {ts:<42}|\n"
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
        f"<b>Kotak Neo SR-Guard Bot STARTED  v2.0</b>\n"
        f"Mode          : LIVE TRADING\n"
        f"Call Symbol   : <code>{_html_escape(CALL_SYMBOL)}</code>\n"
        f"Put Symbol    : <code>{_html_escape(PUT_SYMBOL)}</code>\n"
        f"Avail Cash    : {_fmt(avail)}\n"
        f"Net Balance   : {_fmt(net)}\n"
        f"MktOpen Guard : {COOLDOWN_MINS} min after 09:15\n"
        f"Session End   : 15:15 IST\n"
        f"Max Trades    : {MAX_TRADES}/day\n"
        f"Resistance    : {RESISTANCE:.0f}\n"
        f"Support       : {SUPPORT:.0f}\n"
        f"Time          : {ts}"
    )
    log.info("Server ready. Waiting for webhook signals...")

# -----------------------------------------------------------------
# 12.  ENTRY POINT
# -----------------------------------------------------------------
if __name__ == "__main__":
    if not authenticate():
        log.error("Authentication failed -- cannot start bot. "
                  "Check KConfig.ini credentials.")
        sys.exit(1)

    startup_banner()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
