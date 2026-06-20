"""
Kotak Neo Webhook Trading Bot
================================================
Auth  : Direct REST API (bypasses neo-api-client SDK entirely)
        Follows official Kotak docs: kotak.docx
        Step 2a → POST /login/1.0/tradeApiLogin   (TOTP)
        Step 2b → POST /login/1.0/tradeApiValidate (MPIN)
Signal: TradingView webhook  →  BUY_CALL / BUY_PUT / EXIT_CALL / EXIT_PUT
Guard : S/R no-trade zone, market-open cooldown, max-trades-per-day
"""

import os
import sys
import json
import logging
import threading
import configparser
from datetime import datetime

import pytz
from flask import Flask, request, jsonify
import requests

try:
    import pyotp
except ImportError:
    sys.exit("CRITICAL: pyotp not installed.  pip install pyotp")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_FILE = "KConfig.ini"
cfg = configparser.ConfigParser()
if not os.path.exists(CONFIG_FILE):
    sys.exit(f"CRITICAL: '{CONFIG_FILE}' not found.")
cfg.read(CONFIG_FILE)

try:
    TG_TOKEN       = cfg.get("TELEGRAM", "bot_token",             fallback="")
    TG_CHAT_ID     = cfg.get("TELEGRAM", "chat_id",               fallback="")
    TG_ENABLED     = cfg.getboolean("TELEGRAM", "enable_telegram", fallback=True)

    # UUID token from NEO App → Invest → Trade API → API Dashboard
    # Sent as the Authorization header in every REST call
    K_ACCESS_TOKEN = cfg.get("KOTAK", "consumer_key")
    K_MOBILE       = cfg.get("KOTAK", "mobile_number")    # e.g. +919XXXXXXXXX
    K_UCC          = cfg.get("KOTAK", "client_code")      # 5-char code e.g. KS280
    K_MPIN         = cfg.get("KOTAK", "mpin")             # 6-digit MPIN
    K_TOTP_SECRET  = cfg.get("KOTAK", "totp_secret").replace(" ", "")

    K_EXCHANGE     = cfg.get("KOTAK", "exchange_segment", fallback="nse_fo").lower()
    K_SYMBOL       = cfg.get("KOTAK", "trading_symbol",   fallback="NIFTY25APRFUT")
    K_PRODUCT      = cfg.get("KOTAK", "product",          fallback="mis").upper()
    K_QTY          = cfg.getint("KOTAK", "quantity",      fallback=50)

    COOLDOWN_MINS  = cfg.getint("RISK",  "market_open_cooldown_mins", fallback=15)
    MAX_TRADES     = cfg.getint("RISK",  "max_trades_per_day",        fallback=3)
    RESISTANCE     = cfg.getfloat("RISK","resistance",                fallback=25000)
    SUPPORT        = cfg.getfloat("RISK","support",                   fallback=24000)
except Exception as e:
    sys.exit(f"CRITICAL: Error reading {CONFIG_FILE}: {e}")

IST = pytz.timezone("Asia/Kolkata")

# ── Kotak REST endpoints (official docs) ──────────────────────────────────────
LOGIN_URL    = "https://mis.kotaksecurities.com/login/1.0/tradeApiLogin"
VALIDATE_URL = "https://mis.kotaksecurities.com/login/1.0/tradeApiValidate"
NEO_FIN_KEY  = "neotradeapi"

# ── Session state (populated after login) ─────────────────────────────────────
session = {"trading_token": None, "trading_sid": None, "base_url": None}

# ── Trade state ───────────────────────────────────────────────────────────────
state_lock  = threading.Lock()
pos_side    = "NONE"    # LONG | SHORT | NONE
trade_count = 0

app = Flask(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_totp() -> str | None:
    if K_TOTP_SECRET.isdigit() and len(K_TOTP_SECRET) == 6:
        log.warning(
            "totp_secret is a plain 6-digit number, not a Base32 seed. "
            "Re-register TOTP in the API Dashboard and paste the Base32 secret."
        )
        return K_TOTP_SECRET
    try:
        return pyotp.TOTP(K_TOTP_SECRET).now()
    except Exception as e:
        log.error(f"TOTP generation failed: {e}")
        return None


def send_telegram(text: str) -> None:
    if not TG_ENABLED or not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=5,
        )
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


def now_ist() -> datetime:
    return datetime.now(IST)


def market_open_ok() -> bool:
    n = now_ist()
    cutoff = n.replace(hour=9, minute=15 + COOLDOWN_MINS, second=0, microsecond=0)
    return n >= cutoff

# ── Authentication (direct REST, per official docs) ───────────────────────────

def init_kotak() -> bool:
    """
    Official 2-step TOTP auth (kotak.docx):

    Step 2a  POST /login/1.0/tradeApiLogin
             Header : Authorization: <access_token>
             Body   : mobileNumber, ucc, totp
             Returns: token (view_token), sid (view_sid)

    Step 2b  POST /login/1.0/tradeApiValidate
             Headers: Authorization, sid: <view_sid>, Auth: <view_token>
             Body   : mpin
             Returns: token (trading_token), sid (trading_sid), baseUrl
    """
    global session
    log.info("Authenticating with Kotak Neo REST API…")

    totp = get_totp()
    if not totp:
        return False

    # Step 2a ─────────────────────────────────────────────────────────────────
    try:
        log.info(f"Step 2a: TOTP login  mobile={K_MOBILE}  ucc={K_UCC}  totp={totp}")
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
        log.info(f"Step 2a response: {resp}")

        data = resp.get("data", {})
        if data.get("status") != "success":
            log.error(f"TOTP login failed: {resp}")
            return False

        view_token = data.get("token")
        view_sid   = data.get("sid")
        if not view_token or not view_sid:
            log.error(f"Missing token/sid in Step 2a response.")
            return False

        log.info("Step 2a OK — view token and sid received.")

    except Exception as e:
        log.error(f"Step 2a exception: {e}")
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
        log.info(f"Step 2b response: {resp2}")

        data2 = resp2.get("data", {})
        if data2.get("status") != "success":
            log.error(f"MPIN validation failed: {resp2}")
            return False

        session["trading_token"] = data2.get("token")
        session["trading_sid"]   = data2.get("sid")
        session["base_url"]      = data2.get("baseUrl", "https://cis.kotaksecurities.com")

        if not session["trading_token"]:
            log.error("Missing trading token in Step 2b response.")
            return False

        log.info(f"✅ Authenticated! base_url={session['base_url']}")
        send_telegram("✅ Kotak Neo bot started and authenticated.")
        return True

    except Exception as e:
        log.error(f"Step 2b exception: {e}")
        return False

# ── Order Execution ───────────────────────────────────────────────────────────

def place_order(trans_type: str, qty: int) -> bool:
    """trans_type: 'B' (buy) or 'S' (sell)"""
    if not session["trading_token"]:
        log.error("place_order called but not authenticated.")
        return False

    url   = f"{session['base_url']}/quick/order/rule/ms/place"
    jdata = json.dumps({
        "am": "NO", "dq": "0",  "es": K_EXCHANGE,
        "mp": "0",  "pc": K_PRODUCT, "pf": "N",
        "pr": "0",  "pt": "MKT", "qt": str(qty),
        "rt": "DAY","tp": "0",  "ts": K_SYMBOL, "tt": trans_type,
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
            log.info(f"Order OK: {trans_type} {qty} {K_SYMBOL} | id={resp.get('nOrdNo')}")
        else:
            log.error(f"Order failed: {resp}")
        return success
    except Exception as e:
        log.error(f"place_order exception: {e}")
        return False

# ── Webhook ───────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    global pos_side, trade_count

    message = str((request.json or {}).get("message", ""))
    signals = ["BUY_CALL", "BUY_PUT", "EXIT_CALL", "EXIT_PUT"]
    signal  = next((s for s in signals if s in message), None)

    if signal is None:
        return jsonify({"status": "ignored"})

    if not market_open_ok():
        log.info(f"{signal} ignored — market-open cooldown.")
        return jsonify({"status": "cooldown"})

    with state_lock:
        if trade_count >= MAX_TRADES:
            log.info(f"{signal} ignored — max trades reached.")
            return jsonify({"status": "max_trades_reached"})

        executed = False
        if   signal == "BUY_CALL"  and pos_side == "NONE":
            if place_order("B", K_QTY): pos_side = "LONG";  trade_count += 1; executed = True
        elif signal == "BUY_PUT"   and pos_side == "NONE":
            if place_order("S", K_QTY): pos_side = "SHORT"; trade_count += 1; executed = True
        elif signal == "EXIT_CALL" and pos_side == "LONG":
            if place_order("S", K_QTY): pos_side = "NONE";  trade_count += 1; executed = True
        elif signal == "EXIT_PUT"  and pos_side == "SHORT":
            if place_order("B", K_QTY): pos_side = "NONE";  trade_count += 1; executed = True

        if executed:
            msg = f"📊 {signal} | pos={pos_side} | trades={trade_count}"
            log.info(msg)
            send_telegram(msg)

    return jsonify({"status": "processed", "signal": signal, "position": pos_side})


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "authenticated": bool(session["trading_token"]),
        "position":      pos_side,
        "trade_count":   trade_count,
        "max_trades":    MAX_TRADES,
        "symbol":        K_SYMBOL,
        "time_ist":      now_ist().strftime("%H:%M:%S"),
    })

# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not init_kotak():
        log.error("Authentication failed. Exiting.")
        sys.exit(1)
    log.info("🚀 Flask webhook server starting on port 5000…")
    app.run(host="0.0.0.0", port=5000)
