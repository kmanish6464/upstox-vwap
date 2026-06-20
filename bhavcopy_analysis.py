import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from io import BytesIO, StringIO
from pymongo import MongoClient

# --- CONFIGURATION ---
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "NSE_DAILY"
COLLECTION_NAME = "bhavcopy"
START_DATE = datetime(2026, 1, 1)
END_DATE = datetime.now()
OUTPUT_CSV = "bullish_stocks_analysis.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
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


def download_and_sync_to_mongo():
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    collection.create_index([("SYMBOL", 1), ("DATE1", 1)])
    collection.create_index("extraction_date")

    session = get_session()
    current_date = START_DATE

    print(f"--- Starting Optimized Sync ---")

    while current_date <= END_DATE:
        if current_date.weekday() >= 5:
            current_date += timedelta(days=1)
            continue

        date_str = current_date.strftime("%d%m%Y")
        url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv"

        try:
            existing_count = collection.count_documents({"extraction_date": current_date})
            if existing_count > 1000:
                print(f"[{current_date.date()}] Skipping: {existing_count} records already exist.")
                current_date += timedelta(days=1)
                continue

            response = session.get(url, timeout=15)
            if response.status_code == 200:
                df = pd.read_csv(StringIO(response.text))
                df.columns = [c.strip().upper() for c in df.columns]

                date_val_in_csv = df['DATE1'].iloc[0] if not df.empty else ""
                existing_symbols = set(collection.distinct("SYMBOL", {"DATE1": date_val_in_csv}))

                records_to_add = []
                for _, row in df.iterrows():
                    symbol = str(row.get('SYMBOL', '')).strip()
                    if symbol in existing_symbols:
                        continue

                    records_to_add.append({
                        "SYMBOL": symbol,
                        "SERIES": str(row.get('SERIES', '')).strip(),
                        "DATE1": str(row.get('DATE1', '')).strip(),
                        "PREV_CLOSE": float(row.get('PREV_CLOSE', 0)),
                        "OPEN_PRICE": float(row.get('OPEN_PRICE', 0)),
                        "HIGH_PRICE": float(row.get('HIGH_PRICE', 0)),
                        "LOW_PRICE": float(row.get('LOW_PRICE', 0)),
                        "LAST_PRICE": float(row.get('LAST_PRICE', 0)),
                        "CLOSE_PRICE": float(row.get('CLOSE_PRICE', 0)),
                        "AVG_PRICE": float(row.get('AVG_PRICE', 0)),
                        "TTL_TRD_QNTY": int(row.get('TTL_TRD_QNTY', 0)),
                        "TURNOVER_LACS": float(row.get('TURNOVER_LACS', 0)),
                        "NO_OF_TRADES": int(row.get('NO_OF_TRADES', 0)),
                        "DELIV_QTY": str(row.get('DELIV_QTY', '0')).strip(),
                        "DELIV_PER": str(row.get('DELIV_PER', '0')).strip(),
                        "extraction_date": current_date
                    })

                if records_to_add:
                    collection.insert_many(records_to_add)
                    print(f"[{current_date.date()}] Sync complete: Added {len(records_to_add)} records.")
                else:
                    print(f"[{current_date.date()}] No new records to add.")
            elif response.status_code == 404:
                print(f"[{current_date.date()}] Holiday or Data Not Found.")
        except Exception as e:
            print(f"[{current_date.date()}] Error: {e}")

        current_date += timedelta(days=1)
        time.sleep(0.1)


def analyze_high_delivery_volume():
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    print("\n--- Running Bullish Analysis ---")
    projection = {"SYMBOL": 1, "CLOSE_PRICE": 1, "TTL_TRD_QNTY": 1, "DELIV_PER": 1, "extraction_date": 1, "SERIES": 1,
                  "DATE1": 1}
    data = list(collection.find({}, projection))

    if not data:
        print("No data found in MongoDB for analysis.")
        return

    df = pd.DataFrame(data)
    df['CLOSE_PRICE'] = pd.to_numeric(df['CLOSE_PRICE'])
    df['TTL_TRD_QNTY'] = pd.to_numeric(df['TTL_TRD_QNTY'])
    df['DELIV_PER'] = pd.to_numeric(df['DELIV_PER'], errors='coerce').fillna(0)
    df['SERIES'] = df['SERIES'].str.strip()

    df = df.sort_values(by=['SYMBOL', 'extraction_date'])

    df['prev_close'] = df.groupby('SYMBOL')['CLOSE_PRICE'].shift(1)
    df['avg_vol_5d'] = df.groupby('SYMBOL')['TTL_TRD_QNTY'].transform(lambda x: x.rolling(window=5).mean())

    latest_date = df['extraction_date'].max()
    print(f"Analyzing data for latest date: {latest_date.date()}")

    latest_df = df[(df['extraction_date'] == latest_date) & (df['SERIES'].isin(['EQ', 'BE']))].copy()

    if latest_df.empty:
        print(f"No EQ/BE series data found.")
        return

    # Try Strict Criteria First
    bullish = latest_df[
        (latest_df['CLOSE_PRICE'] > latest_df['prev_close']) &
        (latest_df['TTL_TRD_QNTY'] > (latest_df['avg_vol_5d'] * 1.5)) &
        (latest_df['DELIV_PER'] > 50)
        ].copy()

    # Fallback to Relaxed Criteria if empty
    if bullish.empty:
        print("No stocks met strict criteria. Using relaxed criteria (25% Deliv, 1.2x Vol)...")
        bullish = latest_df[
            (latest_df['CLOSE_PRICE'] > latest_df['prev_close']) &
            (latest_df['TTL_TRD_QNTY'] > (latest_df['avg_vol_5d'] * 1.2)) &
            (latest_df['DELIV_PER'] > 25)
            ].copy()

    if not bullish.empty:
        bullish['VOL_RATIO'] = (bullish['TTL_TRD_QNTY'] / bullish['avg_vol_5d']).round(2)
        bullish['PRICE_CHG_PCT'] = (
                    ((bullish['CLOSE_PRICE'] - bullish['prev_close']) / bullish['prev_close']) * 100).round(2)

        # Sort and Save to CSV
        output_df = bullish[['SYMBOL', 'DATE1', 'CLOSE_PRICE', 'PRICE_CHG_PCT', 'DELIV_PER', 'VOL_RATIO']].sort_values(
            'VOL_RATIO', ascending=False)
        output_df.to_csv(OUTPUT_CSV, index=False)

        print(f"\nFiltered Results (Saved to {OUTPUT_CSV}):")
        print(output_df.head(20).to_string(index=False))
    else:
        print("\nNo stocks met even the relaxed criteria today.")


if __name__ == "__main__":
    download_and_sync_to_mongo()
    analyze_high_delivery_volume()