import pandas as pd
import requests
import os
import sys
import io
import time
import json
import configparser
from datetime import datetime, timedelta

# ================= 1. CONFIGURATION =================

# FILE PATHS
CONFIG_FILE = 'config.ini'
TOKEN_FILE = 'token.txt'
INPUT_FILE = 'nse.csv'  # Reading from CSV again

# DEFAULT CONFIG
DEFAULT_CONFIG = {
    'TELEGRAM': {'bot_token': '', 'chat_id': ''},
    'SETTINGS': {'vol_surge_threshold': '50', 'loop_interval': '60'}
}


# ================= 2. SETUP =================

def load_config():
    config = configparser.ConfigParser()
    if not os.path.exists(CONFIG_FILE):
        for section, content in DEFAULT_CONFIG.items():
            config[section] = content
        with open(CONFIG_FILE, 'w') as f:
            config.write(f)
    config.read(CONFIG_FILE)
    return config


def read_token():
    if not os.path.exists(TOKEN_FILE):
        print(f"❌ Error: {TOKEN_FILE} not found.")
        sys.exit(1)
    with open(TOKEN_FILE, 'r') as f:
        return f.read().strip()


# ================= 3. CORE LOGIC =================

def fetch_data(key, token, days_back=30):
    """Fetches 1-minute data for the last 'days_back' days."""
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}

    today = datetime.now()
    from_date = (today - timedelta(days=days_back)).strftime('%Y-%m-%d')
    today_str = today.strftime('%Y-%m-%d')

    # Historical API for extended range
    url = f"https://api.upstox.com/v2/historical-candle/{key}/1minute/{today_str}/{from_date}"

    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            # print(f"❌ API Error: {r.status_code} - {r.text}")
            return None

        data = r.json()
        if 'data' not in data or 'candles' not in data['data'] or not data['data']['candles']:
            # print("❌ No data returned.")
            return None

        cols = ['ts', 'o', 'h', 'l', 'c', 'v', 'oi']
        df = pd.DataFrame(data['data']['candles'], columns=cols)
        df['ts'] = pd.to_datetime(df['ts']).dt.tz_convert('Asia/Kolkata')
        df.sort_values('ts', inplace=True)
        df.set_index('ts', inplace=True)

        return df

    except Exception as e:
        # print(f"❌ Exception fetching data: {e}")
        return None


def calculate_vwap(df, period='W'):
    """Calculates VWAP anchored to the start of the period."""

    df = df.copy()
    df['tp'] = (df['h'] + df['l'] + df['c']) / 3

    # 1. Identify Group ID
    if period == 'W':  # Weekly
        df['group_id'] = df.index.year.astype(str) + '-' + df.index.isocalendar().week.astype(str)
    elif period == 'M':  # Monthly
        df['group_id'] = df.index.to_period('M').astype(str)
    elif period == 'Q':  # Quarterly
        df['group_id'] = df.index.to_period('Q').astype(str)
    elif period == 'Y':  # Yearly
        df['group_id'] = df.index.to_period('Y').astype(str)

    # 2. Cumulative Calculation
    df['pv'] = df['tp'] * df['v']
    df['cum_v'] = df.groupby('group_id')['v'].cumsum()
    df['cum_pv'] = df.groupby('group_id')['pv'].cumsum()

    df['vwap'] = df['cum_pv'] / df['cum_v']

    return df['vwap']


def analyze_multi_timeframe(df_1min):
    """Calculates 188min EMA and Multi-Timeframe VWAP."""

    # Ensure standard typical price
    df = df_1min.copy()

    # 1. Calculate VWAPs on 1-min Data
    df['vwap_weekly'] = calculate_vwap(df, 'W')
    df['vwap_monthly'] = calculate_vwap(df, 'M')
    df['vwap_quarterly'] = calculate_vwap(df, 'Q')
    df['vwap_yearly'] = calculate_vwap(df, 'Y')

    # 2. Resample to 188min
    # Align to 09:15 AM.
    df_res = df.resample('188min', origin='start_day', offset='9h15min').agg({
        'o': 'first', 'h': 'max', 'l': 'min', 'c': 'last', 'v': 'sum',
        'vwap_weekly': 'last',
        'vwap_monthly': 'last',
        'vwap_quarterly': 'last',
        'vwap_yearly': 'last'
    }).dropna()

    # Filter Market Hours Only (09:15 onwards)
    df_res = df_res[df_res.index.time >= pd.to_datetime("09:15").time()]

    # 3. Calculate EMA 7 and 21
    df_res['ema7'] = df_res['c'].ewm(span=7, adjust=False).mean()
    df_res['ema21'] = df_res['c'].ewm(span=21, adjust=False).mean()

    # 4. Identify Crossovers
    # Standard EMA Crossover Logic
    df_res['bull_cross'] = (df_res['ema7'] > df_res['ema21']) & (df_res['ema7'].shift(1) <= df_res['ema21'].shift(1))
    df_res['bear_cross'] = (df_res['ema7'] < df_res['ema21']) & (df_res['ema7'].shift(1) >= df_res['ema21'].shift(1))

    # 5. Advanced Signal: EMA Cross + Price > All VWAPs
    df_res['above_all_vwaps'] = (
            (df_res['c'] > df_res['vwap_weekly']) &
            (df_res['c'] > df_res['vwap_monthly']) &
            (df_res['c'] > df_res['vwap_yearly'])
    )

    df_res['below_all_vwaps'] = (
            (df_res['c'] < df_res['vwap_weekly']) &
            (df_res['c'] < df_res['vwap_monthly']) &
            (df_res['c'] < df_res['vwap_yearly'])
    )

    # Combined Signal logic
    df_res['strong_bull_cross'] = df_res['bull_cross'] & df_res['above_all_vwaps']

    # Fix FutureWarning by using infer_objects(copy=False)
    df_res['vwap_breakout_bull'] = (
            df_res['above_all_vwaps'] &
            (~df_res['above_all_vwaps'].shift(1).fillna(False).infer_objects(copy=False)) &
            (df_res['ema7'] > df_res['ema21'])
    )

    df_res['strong_bear_cross'] = df_res['bear_cross'] & df_res['below_all_vwaps']
    df_res['vwap_breakout_bear'] = (
            df_res['below_all_vwaps'] &
            (~df_res['below_all_vwaps'].shift(1).fillna(False).infer_objects(copy=False)) &
            (df_res['ema7'] < df_res['ema21'])
    )

    return df_res


def print_debug_table(df_res, symbol_name):
    """Prints a detailed table of recent candles."""

    # Show last 20 candles
    recent = df_res.tail(20)

    # Track latest detected signal info
    last_signal_type = "NONE"
    last_signal_time = "-"

    found_signals = False

    # Buffer print content to only show if signal exists
    output_buffer = []
    output_buffer.append(f"\n📊 --- DETAILED ANALYSIS: {symbol_name} (Last 20 Candles) ---")
    output_buffer.append(f"{'Time':<20} | {'Close':<10} | {'EMA Signal':<15} | {'VWAP Status':<15} | {'STRONG SIGNAL'}")
    output_buffer.append("-" * 100)

    for i in range(len(recent)):
        row = recent.iloc[i]
        ts = row.name.strftime('%Y-%m-%d %H:%M')
        close = f"{row['c']:.2f}"

        # EMA Signal
        ema_sig = "-"
        if row['bull_cross']:
            ema_sig = "EMA BULL 🟢"
            last_signal_type = "BULL CROSS"
            last_signal_time = ts
        elif row['bear_cross']:
            ema_sig = "EMA BEAR 🔴"
            last_signal_type = "BEAR CROSS"
            last_signal_time = ts

        # VWAP Status
        vwap_stat = "MIXED"
        if row['above_all_vwaps']:
            vwap_stat = "ALL BULL 🟢"
        elif row['below_all_vwaps']:
            vwap_stat = "ALL BEAR 🔴"

        # Combined Signal
        strong_sig = "-"
        if row['strong_bull_cross']:
            strong_sig = "🚀 STRONG BUY (Cross)"
            last_signal_type = "STRONG BUY"
            last_signal_time = ts
        elif row['vwap_breakout_bull']:
            strong_sig = "🚀 STRONG BUY (VWAP)"
            last_signal_type = "STRONG BUY"
            last_signal_time = ts
        elif row['strong_bear_cross']:
            strong_sig = "🩸 STRONG SELL (Cross)"
            last_signal_type = "STRONG SELL"
            last_signal_time = ts
        elif row['vwap_breakout_bear']:
            strong_sig = "🩸 STRONG SELL (VWAP)"
            last_signal_type = "STRONG SELL"
            last_signal_time = ts

        # Logic: Print if signal exists OR it's one of the last 3 candles (to show activity)
        is_recent = i >= (len(recent) - 3)

        if ema_sig != "-" or strong_sig != "-" or is_recent:
            output_buffer.append(f"{ts:<20} | {close:<10} | {ema_sig:<15} | {vwap_stat:<15} | {strong_sig}")
            # If we found a real signal (not just recent candle), mark as found
            if ema_sig != "-" or strong_sig != "-":
                found_signals = True

    output_buffer.append("-" * 100)

    # Only print full block if signals found or requested (optional)
    # For loop scanning, we usually only print if signal is found
    if found_signals:
        for line in output_buffer:
            print(line)

        # Current Status
        latest = df_res.iloc[-1]
        close_val = latest['c']

        print("\n🔎 CURRENT VWAP LEVELS:")
        print(
            f"   Weekly    : {latest['vwap_weekly']:.2f} ({'Above' if close_val > latest['vwap_weekly'] else 'Below'})")
        print(
            f"   Monthly   : {latest['vwap_monthly']:.2f} ({'Above' if close_val > latest['vwap_monthly'] else 'Below'})")
        print(
            f"   Yearly    : {latest['vwap_yearly']:.2f} ({'Above' if close_val > latest['vwap_yearly'] else 'Below'})")

        # Determine Action based on recent history + current state
        trend = "BULLISH" if latest['ema7'] > latest['ema21'] else "BEARISH"

        print(f"\n📢 MARKET STATE: {trend}")

        if last_signal_type != "NONE":
            print(f"ℹ️  Last Signal: {last_signal_type} at {last_signal_time}")

            # Recommendation logic
            if "BUY" in last_signal_type:
                if trend == "BULLISH":
                    print("✅ ACTION: HOLD LONG / BUY DIPS (Trend confirms Signal)")
                else:
                    print("⚠️ ACTION: CAUTION (Signal was Buy, but Short-term Trend is Bearish)")
            elif "SELL" in last_signal_type:
                if trend == "BEARISH":
                    print("✅ ACTION: HOLD SHORT / SELL RALLIES (Trend confirms Signal)")
                else:
                    print("⚠️ ACTION: CAUTION (Signal was Sell, but Short-term Trend is Bullish)")

    return found_signals


# ================= 4. MAIN RUNNER =================

def main():
    print(f"🔍 SCANNER START | Timeframe: 188min")

    token = read_token()

    # 1. Load Keys from CSV
    if not os.path.exists(INPUT_FILE):
        print(f"❌ {INPUT_FILE} missing.");
        return

    try:
        df_csv = pd.read_csv(INPUT_FILE)
        # Find key column
        key_col = next((c for c in df_csv.columns if 'instrument' in c.lower() or 'key' in c.lower()),
                       df_csv.columns[0])
        # Find symbol name column
        sym_col = next((c for c in df_csv.columns if 'symbol' in c.lower() or 'name' in c.lower() and c != key_col),
                       key_col)

        keys_to_scan = list(zip(df_csv[key_col], df_csv[sym_col]))
        print(f"✅ Loaded {len(keys_to_scan)} symbols from {INPUT_FILE}")

    except Exception as e:
        print(f"❌ Error reading CSV: {e}");
        return

    # 2. Scanning Loop
    print("\n🚀 Starting Scan...")

    for i, (key, symbol) in enumerate(keys_to_scan, 1):
        # Progress on same line
        sys.stdout.write(f"\r[{i}/{len(keys_to_scan)}] Scanning: {symbol:<15} ({key})")
        sys.stdout.flush()

        # Fix Key if needed (add NSE_EQ| if missing)
        clean_key = str(key).strip()
        if '|' not in clean_key and (clean_key.startswith('INE') or clean_key.startswith('INF')):
            clean_key = f"NSE_EQ|{clean_key}"

        # Fetch & Analyze
        df_1min = fetch_data(clean_key, token, days_back=30)

        if df_1min is not None and not df_1min.empty:
            try:
                df_188 = analyze_multi_timeframe(df_1min)

                # Print ONLY if signal found
                if print_debug_table(df_188, symbol):
                    print("\n" + "=" * 50 + "\n")  # Separator between stocks
            except Exception as e:
                # print(f" Analysis Error: {e}") # Silent error to keep console clean
                pass

        time.sleep(0.1)  # Rate limit safe

    print("\n✅ Scan Complete.")


if __name__ == "__main__":
    main()