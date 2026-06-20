import requests
import pandas as pd
from datetime import datetime, timedelta
from pymongo import MongoClient
import io
import time

# --- CONFIGURATION ---
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "NSE_DAILY"
COLLECTION_NAME = "bhavcopy"
START_DATE = datetime(2026, 1, 1)

# NSE URL pattern for Full Bhavcopy (including delivery)
# URL: https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv
BASE_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{}.csv"

# Request headers to avoid 403 Forbidden
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/all-reports"
}


def get_session():
    """Establish a session with cookies from the main site first."""
    session = requests.Session()
    session.get("https://www.nseindia.com", headers=HEADERS, timeout=10)
    return session


def download_and_store():
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    session = get_session()
    current_date = START_DATE
    end_date = datetime.now()

    print(f"Starting extraction from {current_date.date()} to {end_date.date()}...")

    while current_date <= end_date:
        # Skip weekends
        if current_date.weekday() >= 5:
            current_date += timedelta(days=1)
            continue

        date_str = current_date.strftime("%d%m%Y")
        url = BASE_URL.format(date_str)

        try:
            response = session.get(url, headers=HEADERS, timeout=15)

            if response.status_code == 200:
                df = pd.read_csv(io.StringIO(response.text))
                # Standardize column names (remove whitespace)
                df.columns = [c.strip() for c in df.columns]

                # Format data to match requested structure
                records = []
                for _, row in df.iterrows():
                    record = {
                        "SYMBOL": str(row.get('SYMBOL', '')).strip(),
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
                    }
                    records.append(record)

                if records:
                    # Update or Insert to prevent duplicates if script runs twice
                    for rec in records:
                        collection.update_one(
                            {"SYMBOL": rec["SYMBOL"], "DATE1": rec["DATE1"], "SERIES": rec["SERIES"]},
                            {"$set": rec},
                            upsert=True
                        )
                    print(f"✅ Processed: {current_date.date()}")
            else:
                print(f"⚠️ Data not available for {current_date.date()} (Status: {response.status_code})")

        except Exception as e:
            print(f"❌ Error on {current_date.date()}: {str(e)}")

        # Throttling to avoid IP ban
        time.sleep(1)
        current_date += timedelta(days=1)

    print("Extraction Complete.")


if __name__ == "__main__":
    download_and_store()