import os
import time
import requests
import pandas as pd
import re
from datetime import datetime, timedelta
from io import StringIO
from pymongo import MongoClient, UpdateOne

# --- CONFIGURATION ---
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "NSE_DAILY"
RAW_COLLECTION = "bhavcopy"
RESULTS_COLLECTION = "bullish_ema_results"
# Set START_DATE back to ensure we have enough data for EMA 200
START_DATE = datetime(2025, 4, 1)
END_DATE = datetime.now()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Referer": "https://www.nseindia.com/all-reports"
}


def get_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except:
        pass
    return session


def is_excluded(symbol, series):
    """Strict filtering to remove ETFs, GS, and non-equity."""
    symbol = str(symbol).upper().strip()
    series = str(series).upper().strip()
    if series != 'EQ': return True
    if "ETF" in symbol or symbol.endswith('BE') or symbol.endswith('EB'): return True
    if re.search(r'\d{2,4}GS\d{4}', symbol): return True
    junk = ['GOLDBE', 'NIFTYBE', 'LIQUID', 'JUNIOR', 'NETF', 'SETF', 'GILT', 'INVIT', 'REIT']
    if any(k in symbol for k in junk): return True
    return False


def download_and_sync_to_mongo():
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    collection = db[RAW_COLLECTION]
    collection.create_index([("SYMBOL", 1), ("extraction_date", -1)])

    session = get_session()
    current_date = START_DATE
    print(f"--- Syncing History from {START_DATE.date()} ---")

    while current_date <= END_DATE:
        if current_date.weekday() >= 5:
            current_date += timedelta(days=1);
            continue

        # Skip if already exists
        if collection.count_documents({"extraction_date": current_date}) > 500:
            current_date += timedelta(days=1);
            continue

        date_str = current_date.strftime("%d%m%Y")
        url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv"

        try:
            response = session.get(url, timeout=15)
            if response.status_code == 200:
                df = pd.read_csv(StringIO(response.text))
                df.columns = [c.strip().upper() for c in df.columns]
                records = []
                for _, row in df.iterrows():
                    if is_excluded(row.get('SYMBOL', ''), row.get('SERIES', '')): continue
                    records.append({
                        "SYMBOL": str(row.get('SYMBOL')).strip(),
                        "CLOSE": float(row.get('CLOSE_PRICE', 0)),
                        "VOLUME": int(row.get('TTL_TRD_QNTY', 0)),
                        "DELIVERY": pd.to_numeric(row.get('DELIV_PER'), errors='coerce') or 0,
                        "extraction_date": current_date
                    })
                if records:
                    collection.insert_many(records)
                    print(f"[{current_date.date()}] Inserted {len(records)} stocks.")
        except Exception as e:
            print(f"Error {current_date.date()}: {e}")

        current_date += timedelta(days=1)
        time.sleep(0.5)


def run_ema_analysis(target_date):
    """Analyzes stocks breaking/above EMA 50, 100, 200."""
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    raw_col = db[RAW_COLLECTION]
    res_col = db[RESULTS_COLLECTION]

    # Load 300 days of history to calculate 200 EMA accurately
    lookback = target_date - timedelta(days=365)
    data = list(raw_col.find({"extraction_date": {"$gte": lookback, "$lte": target_date}}))
    if not data: return

    df = pd.DataFrame(data)
    df = df.sort_values(by=['SYMBOL', 'extraction_date'])

    # Technical Calculations
    groups = df.groupby('SYMBOL')
    df['EMA50'] = groups['CLOSE'].transform(lambda x: x.ewm(span=50, adjust=False).mean())
    df['EMA100'] = groups['CLOSE'].transform(lambda x: x.ewm(span=100, adjust=False).mean())
    df['EMA200'] = groups['CLOSE'].transform(lambda x: x.ewm(span=200, adjust=False).mean())
    df['PREV_CLOSE'] = groups['CLOSE'].shift(1)

    # Filter for target day
    day_df = df[df['extraction_date'] == target_date].copy()
    if day_df.empty: return

    # --- FOCUS CRITERIA ---
    # 1. Price is above all 3 EMAs
    # 2. Price just broke (crossed above) at least one of the EMAs
    # 3. Delivery is decent (> 30%)

    above_all = (day_df['CLOSE'] > day_df['EMA50']) & \
                (day_df['CLOSE'] > day_df['EMA100']) & \
                (day_df['CLOSE'] > day_df['EMA200'])

    broke_50 = (day_df['PREV_CLOSE'] <= day_df['EMA50']) & (day_df['CLOSE'] > day_df['EMA50'])
    broke_100 = (day_df['PREV_CLOSE'] <= day_df['EMA100']) & (day_df['CLOSE'] > day_df['EMA100'])
    broke_200 = (day_df['PREV_CLOSE'] <= day_df['EMA200']) & (day_df['CLOSE'] > day_df['EMA200'])

    focused_df = day_df[above_all & (broke_50 | broke_100 | broke_200) & (day_df['DELIVERY'] > 30)].copy()

    if not focused_df.empty:
        # Add labels for which EMA was broken
        focused_df['BREAKOUT_TYPE'] = focused_df.apply(
            lambda x: "EMA200" if x['PREV_CLOSE'] <= x['EMA200'] < x['CLOSE'] else
            ("EMA100" if x['PREV_CLOSE'] <= x['EMA100'] < x['CLOSE'] else "EMA50"), axis=1
        )

        cols = ['SYMBOL', 'CLOSE', 'PREV_CLOSE', 'EMA50', 'EMA100', 'EMA200', 'DELIVERY', 'BREAKOUT_TYPE']
        result_df = focused_df[cols].round(2)

        # Save CSV
        fname = f"ema_breakout_{target_date.strftime('%Y-%m-%d')}.csv"
        result_df.to_csv(fname, index=False)

        # Save to Mongo
        ops = []
        for d in result_df.to_dict('records'):
            d['analysis_date'] = target_date
            ops.append(UpdateOne({"SYMBOL": d['SYMBOL'], "analysis_date": target_date}, {"$set": d}, upsert=True))
        res_col.bulk_write(ops)
        print(f"Analyzed {target_date.date()}: Found {len(result_df)} breakout stocks.")


def generate_master_summary():
    """Generates a summary of all breakout stocks found."""
    client = MongoClient(MONGO_URI)
    res_col = client[DB_NAME][RESULTS_COLLECTION]
    data = list(res_col.find().sort("analysis_date", -1))
    if data:
        df = pd.DataFrame(data)
        df.drop(columns=['_id'], inplace=True, errors='ignore')
        df.to_csv("ema_analysis_summary.csv", index=False)
        print("--- Master Summary: ema_analysis_summary.csv Created ---")


if __name__ == "__main__":
    download_and_sync_to_mongo()

    print("\n--- Running EMA Analysis ---")
    # Analyze today and yesterday
    for i in range(1, -1, -1):
        t_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=i)
        run_ema_analysis(t_date)

    generate_master_summary()