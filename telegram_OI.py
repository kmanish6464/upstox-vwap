import requests
import pandas as pd
import datetime
import os
import sys
import time
from collections import deque

# ================= CONFIGURATION =================

# Instrument Key for Nifty Futures
FUTURE_INSTRUMENT_KEY = "NSE_FO|49229"

# Polling Interval (in seconds)
POLL_INTERVAL = 60

# Signal Window (in minutes)
# Compares current data vs 3 minutes ago
SIGNAL_WINDOW_MINUTES = 3

# Set to True to see raw API responses if you get no output
DEBUG = True

# --- TELEGRAM ALERTS CONFIGURATION ---
ENABLE_TELEGRAM = True
TELEGRAM_BOT_TOKEN = "8493220272:AAF6oDD2CM8EgPBVlpSmvCtjLobsFdD-uKg"
TELEGRAM_CHAT_ID = "6316413399"

# Set to True if you want alerts for EVERY update (neutral included)
# Set to False for only BUY/SELL signals
SEND_ONLY_SIGNALS = True


def load_token():
    """Reads the Access Token from token.txt."""
    try:
        with open("token.txt", "r") as f:
            token = f.read().strip()
            if not token:
                print("Error: token.txt is empty.")
                sys.exit(1)
            return token
    except FileNotFoundError:
        print("Error: token.txt not found. Please ensure it exists.")
        sys.exit(1)


ACCESS_TOKEN = load_token()

# API Endpoints
QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"

HEADERS = {
    'Accept': 'application/json',
    'Authorization': f'Bearer {ACCESS_TOKEN}'
}


# ================= HELPER FUNCTIONS =================

def interpret_oi_action(price_change, oi_change):
    if price_change > 0 and oi_change > 0:
        return "Long Buildup"
    elif price_change < 0 and oi_change > 0:
        return "Short Buildup"
    elif price_change < 0 and oi_change < 0:
        return "Long Unwinding"
    elif price_change > 0 and oi_change < 0:
        return "Short Covering"
    else:
        return "Neutral"


def send_telegram_alert(message):
    """
    Sends a message to the configured Telegram Chat.
    """
    if not ENABLE_TELEGRAM:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        # Short timeout to prevent blocking the trading loop
        response = requests.post(url, json=payload, timeout=3)
        if response.status_code != 200:
            print(f"\n[Error] Telegram Failed ({response.status_code}): {response.text}")
    except Exception as e:
        print(f"\n[Warning] Telegram Connection Failed: {e}")


# ================= DATA FETCHING =================

def get_live_quote(instrument_key):
    """
    Fetches the current snapshot (LTP, OI).
    """
    params = {'instrument_key': instrument_key}

    try:
        response = requests.get(QUOTE_URL, headers=HEADERS, params=params)
        data = response.json()

        if data.get('status') == 'success' and data.get('data'):
            # Auto-detect the key returned by Upstox
            first_key = list(data['data'].keys())[0]
            quote = data['data'][first_key]

            ts_ms = int(quote['last_trade_time'])
            timestamp = pd.to_datetime(ts_ms, unit='ms').tz_localize('UTC').tz_convert('Asia/Kolkata')

            return {
                'timestamp': timestamp,
                'close': quote['last_price'],
                'oi': quote['oi']
            }
        else:
            if DEBUG:
                print(f"\n[DEBUG] API Error/Empty: {data}")
            return None
    except Exception as e:
        print(f"\n[Error] Exception fetching quote: {e}")
        return None


# ================= MAIN LOOP =================

def main():
    print(f"--- LIVE NIFTY FUTURES (Telegram Enabled: {ENABLE_TELEGRAM}) ---")
    print(f"Instrument: {FUTURE_INSTRUMENT_KEY}")
    print(f"Strategy: Rolling {SIGNAL_WINDOW_MINUTES}-Minute Change")

    # --- STARTUP TEST ---
    if ENABLE_TELEGRAM:
        print("Testing Telegram Connection...", end=" ")
        send_telegram_alert("🤖 *Bot Connected & Ready!* Monitoring Nifty Futures...")
        print("Test Message Sent.")

    print("Waiting for LIVE data updates...")
    print("\n" + "=" * 95)
    # Using specific spacing to match your requested format
    print(f"{'Time':<10} | {'LTP':<10} | {'OI':<10} | {'Price Chg':<10} | {'OI Chg':<10} | {'Signal':<15}")
    print("=" * 95)

    history = deque(maxlen=10)
    last_alert_time = None

    try:
        while True:
            # 1. Fetch Live Quote
            data_point = get_live_quote(FUTURE_INSTRUMENT_KEY)

            if data_point:
                # Add to history
                history.append(data_point)

                # Compare Current vs 3 minutes ago
                target_lookback = SIGNAL_WINDOW_MINUTES + 1

                if len(history) >= target_lookback:
                    prev_data = history[-target_lookback]
                    is_full_window = True
                else:
                    prev_data = history[0]
                    is_full_window = False

                curr_data = history[-1]

                # Calculate diffs
                price_chg = curr_data['close'] - prev_data['close']
                oi_chg = curr_data['oi'] - prev_data['oi']

                signal = interpret_oi_action(price_chg, oi_chg)

                # Format output strings
                time_str = curr_data['timestamp'].strftime('%H:%M')
                close_str = f"{curr_data['close']:.2f}"
                oi_str = f"{int(curr_data['oi'])}"

                # Formatting symbols for output
                # If 0, show 0.00 / 0 without + sign
                if price_chg == 0:
                    p_chg_str = "0.00"
                else:
                    p_sym = "+" if price_chg > 0 else ""
                    p_chg_str = f"{p_sym}{price_chg:.2f}"

                if oi_chg == 0:
                    o_chg_str = "0"
                else:
                    o_sym = "+" if oi_chg > 0 else ""
                    o_chg_str = f"{o_sym}{int(oi_chg)}"

                status_note = "" if is_full_window else "*"

                # Print to Console with aligned columns
                print(
                    f"{time_str:<10} | {close_str:<10} | {oi_str:<10} | {p_chg_str:<10} | {o_chg_str:<10} | {signal:<15} {status_note}")

                # --- TELEGRAM ALERT LOGIC ---
                if is_full_window:
                    should_send = False

                    if not SEND_ONLY_SIGNALS:
                        should_send = True
                    elif signal != "Neutral":
                        should_send = True

                    # Prevent duplicate alerts for the same timestamp
                    if should_send and (last_alert_time != time_str):

                        # Emoji mapping
                        emoji = "⚪"
                        if "Long Buildup" in signal: emoji = "🟢 🐂"
                        if "Short Buildup" in signal: emoji = "🔴 🐻"
                        if "Short Covering" in signal: emoji = "🔵 🚀"
                        if "Long Unwinding" in signal: emoji = "🟠 📉"

                        msg = (
                            f"{emoji} *NIFTY FUTURES ALERT*\n"
                            f"⏰ Time: {time_str}\n"
                            f"📊 Signal: *{signal}*\n"
                            f"💰 Price: {close_str} ({p_chg_str})\n"
                            f"📉 OI Change: {o_chg_str}"
                        )

                        send_telegram_alert(msg)
                        last_alert_time = time_str

            else:
                # Print a dot to show script is running but waiting for data
                print(".", end="", flush=True)

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()