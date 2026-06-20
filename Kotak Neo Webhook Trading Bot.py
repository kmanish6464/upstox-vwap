"""
Kotak Neo Webhook Trading Bot (SR-Guard Edition)
=========================================================
Migrated from Upstox to Kotak Neo API v2.

REQUIREMENTS:
    pip install "git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git#egg=neo_api_client" flask requests pytz

HOW TO RUN:
    1. Update config.ini with your Kotak parameters.
    2. Place your daily JWT token in token.txt.
    3. Run: python kotak_algo.py
    4. Start ngrok: ngrok http 5000
"""

import os
import time
import logging
import datetime
import configparser
import requests
import pytz
from flask import Flask, request, jsonify
from neo_api_client import NeoAPI

# --- LOGGING CONFIG ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

# --- LOAD CONFIG ---
config = configparser.ConfigParser()
config.read('config.ini')

# Kotak Neo Settings
KOTAK_CONSUMER_KEY = config.get('KOTAK', 'consumer_key', fallback='')
TOKEN_FILE = config.get('KOTAK', 'token_file', fallback='token.txt')
BUY_SYMBOL = config.get('KOTAK', 'buy_trading_symbol', fallback='NIFTY25APRFUT')
SELL_SYMBOL = config.get('KOTAK', 'sell_trading_symbol', fallback='NIFTY25APRFUT')
NIFTY_TOKEN = config.get('KOTAK', 'nifty_futures_token', fallback='54765')
EXCHANGE_SEGMENT = config.get('KOTAK', 'exchange_segment', fallback='nse_fo')
PRODUCT = config.get('KOTAK', 'product', fallback='MIS')
QUANTITY = config.getint('KOTAK', 'quantity', fallback=50)

# Telegram Settings
TEL_TOKEN = config.get('TELEGRAM', 'bot_token', fallback='')
TEL_CHAT_ID = config.get('TELEGRAM', 'chat_id', fallback='')
TEL_ENABLED = config.getboolean('TELEGRAM', 'enable_telegram', fallback=False)

# Strategy Settings
RESISTANCE = config.getfloat('SETTINGS', 'resistance', fallback=22200)
SUPPORT = config.getfloat('SETTINGS', 'support', fallback=22000)
COOLDOWN_MINS = config.getint('SETTINGS', 'market_open_cooldown_mins', fallback=7)
INITIAL_CAPITAL = config.getfloat('SETTINGS', 'initial_capital', fallback=100000)

# Runtime State
daily_stats = {
    'total_signals': 0,
    'trades_executed': 0,
    'blocked_by_guard': 0,
    'pnl_estimate': 0.0
}

# --- INITIALIZE KOTAK NEO CLIENT ---
access_token = ""
if os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, 'r') as f:
        access_token = f.read().strip()
else:
    logger.warning(f"{TOKEN_FILE} not found. Ensure token is provided.")

try:
    kotak_client = NeoAPI(consumer_key=KOTAK_CONSUMER_KEY, environment='prod', access_token=access_token)
    logger.info("Kotak Neo Client initialized successfully.")
except Exception as e:
    logger.error(f"Failed to initialize Kotak Neo Client: {e}")


# --- HELPER FUNCTIONS ---
def send_telegram(message):
    """Send alert via Telegram if enabled."""
    if not TEL_ENABLED or not TEL_TOKEN or not TEL_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TEL_TOKEN}/sendMessage"
    payload = {
        "chat_id": TEL_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Telegram error: {e}")


def now_ist():
    """Returns current time in IST."""
    ist = pytz.timezone('Asia/Kolkata')
    return datetime.datetime.now(ist)


def fetch_ltp():
    """Fetch LTP from Kotak Neo for the SR-Guard S/R zone check."""
    try:
        res = kotak_client.quotes(
            instrument_tokens=[{"instrument_token": str(NIFTY_TOKEN), "exchange_segment": EXCHANGE_SEGMENT}],
            quote_type="ltp"
        )
        # Parse Kotak Neo quotes response safely
        if res and isinstance(res, dict) and 'message' in res and res['message'].lower() == 'success':
            data = res.get('data', [])
            if data:
                return float(data[0].get('lastPrice', 0.0))
    except Exception as e:
        logger.error(f"Error fetching LTP from Kotak: {e}")
    return 0.0


def place_order(symbol, side):
    """Place Market Order via Kotak Neo."""
    try:
        txn_type = "B" if side.upper() == "BUY" else "S"
        res = kotak_client.place_order(
            exchange_segment=EXCHANGE_SEGMENT,
            product=PRODUCT,
            price="0",
            order_type="MKT",
            quantity=str(QUANTITY),
            validity="DAY",
            trading_symbol=symbol,
            transaction_type=txn_type,
            amo="NO",
            disclosed_quantity="0",
            market_protection="0",
            pf="N",
            trigger_price="0",
            tag="algo_webhook"
        )
        logger.info(f"Order Placed: {side} {symbol} | Response: {res}")
        return res
    except Exception as e:
        logger.error(f"Exception during order placement: {e}")
        return None


# --- FLASK APP ---
app = Flask(__name__)


@app.route('/webhook', methods=['POST'])
def webhook():
    daily_stats['total_signals'] += 1
    data = request.json

    if not data:
        return jsonify({"status": "error", "message": "No JSON payload provided"}), 400

    # GoCharting Alert Parser
    signal_text = data.get('signal', '').upper()
    close_price = data.get('close', 0.0)

    logger.info(f"Received Signal: {signal_text} @ {close_price}")

    current_time = now_ist()
    market_open = current_time.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = current_time.replace(hour=15, minute=15, second=0, microsecond=0)

    # ---------------------------------------------------------
    # GUARD 1: Session Close Guard
    # ---------------------------------------------------------
    if current_time >= market_close:
        msg = f"⛔ <b>BLOCKED</b>: Signal after 15:15 IST.\\n{signal_text}"
        logger.warning("Blocked: After market close.")
        send_telegram(msg)
        daily_stats['blocked_by_guard'] += 1
        return jsonify({"status": "blocked", "reason": "session_close"})

    # Check for Entry vs Exit
    is_entry = "BUY_CALL" in signal_text or "BUY_PUT" in signal_text
    is_exit = "EXIT_CALL" in signal_text or "EXIT_PUT" in signal_text

    if is_entry:
        # ---------------------------------------------------------
        # GUARD 2: Market Open Cooldown
        # ---------------------------------------------------------
        cooldown_end = market_open + datetime.timedelta(minutes=COOLDOWN_MINS)
        if current_time < cooldown_end:
            msg = f"⏳ <b>COOLDOWN ACTIVE</b>: Ignored Entry.\\n{signal_text}"
            logger.warning("Blocked: Market Open Cooldown.")
            send_telegram(msg)
            daily_stats['blocked_by_guard'] += 1
            return jsonify({"status": "blocked", "reason": "cooldown"})

        # ---------------------------------------------------------
        # GUARD 3: S/R No-Trade Zone (NTZ)
        # ---------------------------------------------------------
        ltp = fetch_ltp()
        if not ltp:
            ltp = close_price  # fallback to webhook price if API fails

        if SUPPORT <= ltp <= RESISTANCE:
            msg = f"🛑 <b>NO-TRADE ZONE (NTZ)</b>\\nLTP: {ltp}\\nZone: {SUPPORT} - {RESISTANCE}\\nSignal: {signal_text}"
            logger.warning(f"Blocked: NTZ. LTP={ltp}")
            send_telegram(msg)
            daily_stats['blocked_by_guard'] += 1
            return jsonify({"status": "blocked", "reason": "ntz_guard", "ltp": ltp})

        # --- ALL GUARDS PASSED - EXECUTE ENTRY ---
        symbol_to_trade = BUY_SYMBOL if "BUY_CALL" in signal_text else SELL_SYMBOL
        res = place_order(symbol_to_trade, "BUY")

        daily_stats['trades_executed'] += 1
        msg = f"✅ <b>ENTRY EXECUTED (Kotak Neo)</b>\\nSignal: {signal_text}\\nLTP: {ltp}\\nResponse: {res}"
        send_telegram(msg)
        return jsonify({"status": "success", "action": "entry", "response": str(res)})

    elif is_exit:
        # --- EXITS ARE NEVER BLOCKED ---
        symbol_to_trade = BUY_SYMBOL if "EXIT_CALL" in signal_text else SELL_SYMBOL
        res = place_order(symbol_to_trade, "SELL")

        daily_stats['trades_executed'] += 1
        msg = f"❌ <b>EXIT EXECUTED (Kotak Neo)</b>\\nSignal: {signal_text}\\nResponse: {res}"
        send_telegram(msg)
        return jsonify({"status": "success", "action": "exit", "response": str(res)})

    return jsonify({"status": "ignored", "message": "Signal not recognized"})


@app.route('/status', methods=['GET'])
def get_status():
    """Health check and configuration status."""
    return jsonify({
        "status": "online",
        "broker": "Kotak Neo",
        "time_ist": str(now_ist()),
        "guards_active": {
            "ntz": f"Enabled ({SUPPORT} - {RESISTANCE})",
            "cooldown": f"{COOLDOWN_MINS} mins"
        }
    })


@app.route('/day_summary', methods=['GET'])
def get_day_summary():
    """Returns today's statistics."""
    return jsonify(daily_stats)


@app.route('/test_signal', methods=['GET'])
def test_signal():
    """Send a test payload to trigger an evaluation."""
    test_payload = {"signal": "BUY_CALL - Test Delta", "close": 22100}
    response = requests.post('http://127.0.0.1:5000/webhook', json=test_payload)
    return response.json()


if __name__ == '__main__':
    banner = (
        "==========================================================\\n"
        "   KOTAK NEO WEBHOOK TRADING BOT (SR-GUARD ENABLED)       \\n"
        "==========================================================\\n"
        f"|  Time IST      : {now_ist()}\\n"
        f"|  Token Length  : {len(access_token)} chars\\n"
        "+----------------------------------------------------------+\\n"
        "|  Webhook  -> POST  http://localhost:5000/webhook         |\\n"
        "|  Status   -> GET   http://localhost:5000/status          |\\n"
        "+----------------------------------------------------------+\\n"
    )
    print(banner)
    send_telegram("🚀 <b>Kotak Neo Bot Started!</b>\\nListening for TradingView/GoCharting Webhooks...")

    app.run(host='0.0.0.0', port=5000)