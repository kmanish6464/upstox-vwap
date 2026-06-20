import requests
import io
import pandas as pd
from datetime import datetime, timedelta
from pymongo import MongoClient, DESCENDING
import time

# Configuration
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "NSE_DAILY"
COLLECTION_NAME = "bhavcopy"
DEFAULT_START_DATE = datetime(2026, 1, 1)
END_DATE = datetime.now()

# NSE Headers to avoid 403 Forbidden
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/all-reports"
}


def get_mongo_collection():
    """Connects to the local MongoDB instance and returns the collection."""
    client = MongoClient(MONGO_URI)
    return client[DB_NAME][COLLECTION_NAME]


def get_last_update_date(collection):
    """
    Finds the latest extraction_date in the MongoDB collection.
    Returns the date if found, otherwise returns None.
    """
    last_record = collection.find_one(sort=[("extraction_date", DESCENDING)])
    if last_record and "extraction_date" in last_record:
        return last_record["extraction_date"]
    return None


def download_and_process(date):
    """
    Downloads Full Bhavcopy and Deliverable data for a specific date.
    NSE Bhavcopy URL pattern: https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv
    """
    date_str = date.strftime("%d%m%Y")
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv"

    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            # Load CSV data into a Pandas DataFrame
            df = pd.read_csv(io.StringIO(response.text))

            # Basic cleaning: remove leading/trailing spaces from column names/strings
            df.columns = df.columns.str.strip()
            df = df.apply(lambda x: x.str.strip() if isinstance(x, str) else x)

            # Add metadata field for the date
            df['extraction_date'] = date

            # Convert to dictionary for MongoDB
            records = df.to_dict('records')
            return records
        elif response.status_code == 404:
            # Likely a holiday or weekend
            return None
        else:
            print(f"Failed to download for {date_str}: Status {response.status_code}")
            return None
    except Exception as e:
        print(f"Error processing {date_str}: {e}")
        return None


def main():
    collection = get_mongo_collection()

    # Determine the start date
    last_date = get_last_update_date(collection)

    if last_date:
        # Start from the day after the last update
        start_date = last_date + timedelta(days=1)
        print(f"Last update found: {last_date.date()}. Resuming from: {start_date.date()}")
    else:
        start_date = DEFAULT_START_DATE
        print(f"No existing data found. Starting fresh from: {start_date.date()}")

    current_date = start_date

    if current_date > END_DATE:
        print("Data is already up to date.")
        return

    while current_date <= END_DATE:
        # Skip Saturdays (5) and Sundays (6)
        if current_date.weekday() < 5:
            print(f"Checking date: {current_date.strftime('%Y-%m-%d')}...", end="\r")

            # Double-check to ensure we don't insert duplicate records for the same day
            if collection.count_documents({"extraction_date": current_date}, limit=1) == 0:
                data = download_and_process(current_date)

                if data:
                    collection.insert_many(data)
                    print(f"Successfully stored {len(data)} records for {current_date.strftime('%Y-%m-%d')}")
                    # Ethical delay to prevent rate limiting
                    time.sleep(1.5)
            else:
                pass  # Already exists in DB

        current_date += timedelta(days=1)

    print("\nData sync completed.")


if __name__ == "__main__":
    main()