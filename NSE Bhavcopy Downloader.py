import requests
import pandas as pd
from pymongo import MongoClient
from datetime import datetime, timedelta
import time
import io
import logging

# Configure basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# ==========================================
# MongoDB Configuration
# ==========================================
MONGO_URI = 'mongodb://localhost:27017/'
DATABASE_NAME = 'NSE_DAILY'
COLLECTION_NAME = 'bhavcopy'

client = MongoClient(MONGO_URI)
db = client[DATABASE_NAME]
collection = db[COLLECTION_NAME]

# ==========================================
# NSE Scraping Configuration
# ==========================================
# NSE requires specific headers and session cookies to allow downloads
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
    "Connection": "keep-alive"
}


def get_nse_session():
    """Initializes a requests session and visits the main page to fetch cookies."""
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        # Pinging the main site to grab necessary cookies before hitting the archives
        session.get("https://www.nseindia.com", timeout=10)
        time.sleep(1)  # Be polite to the server
    except Exception as e:
        logging.error(f"Failed to initialize NSE session: {e}")
    return session


def process_and_insert_data(df, target_date):
    """Processes the dataframe into the specific dictionary structure and inserts to MongoDB."""
    # Strip whitespaces from column names just in case
    df.columns = df.columns.str.strip()

    records_to_insert = []

    for index, row in df.iterrows():
        # Handle potential empty or missing delivery data
        deliv_qty = str(row.get('DELIV_QTY', 0)).strip()
        deliv_per = str(row.get('DELIV_PER', 0.0)).strip()

        # Construct document matching the requested structure
        doc = {
            'SYMBOL': str(row['SYMBOL']).strip(),
            'SERIES': str(row['SERIES']).strip(),
            'DATE1': str(row['DATE1']).strip(),
            'PREV_CLOSE': float(row['PREV_CLOSE']),
            'OPEN_PRICE': float(row['OPEN_PRICE']),
            'HIGH_PRICE': float(row['HIGH_PRICE']),
            'LOW_PRICE': float(row['LOW_PRICE']),
            'LAST_PRICE': float(row['LAST_PRICE']),
            'CLOSE_PRICE': float(row['CLOSE_PRICE']),
            'AVG_PRICE': float(row['AVG_PRICE']),
            'TTL_TRD_QNTY': int(row['TTL_TRD_QNTY']),
            'TURNOVER_LACS': float(row['TURNOVER_LACS']),
            'NO_OF_TRADES': int(row['NO_OF_TRADES']),
            'DELIV_QTY': deliv_qty,
            'DELIV_PER': deliv_per,
            'extraction_date': target_date  # Stores as ISODate in MongoDB
        }
        records_to_insert.append(doc)

    if records_to_insert:
        collection.insert_many(records_to_insert)
        logging.info(f"Successfully inserted {len(records_to_insert)} records for {target_date.strftime('%Y-%m-%d')}")


def download_historical_data(start_date_str):
    """Loops through dates and downloads the sec_bhavdata_full file."""
    start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    end_date = datetime.today()

    session = get_nse_session()
    current_date = start_date

    while current_date <= end_date:
        # Skip weekends (Saturday=5, Sunday=6)
        if current_date.weekday() < 5:
            date_str = current_date.strftime("%d%m%Y")
            # The URL for the full bhavcopy (includes delivery data)
            url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv"

            logging.info(f"Attempting to download data for {current_date.strftime('%Y-%m-%d')}...")

            try:
                response = session.get(url, timeout=15)

                if response.status_code == 200:
                    # Read CSV into pandas DataFrame
                    csv_data = io.StringIO(response.text)
                    df = pd.read_csv(csv_data)

                    # Process and save to MongoDB
                    process_and_insert_data(df, current_date)

                elif response.status_code == 404:
                    logging.warning(
                        f"Data not available for {current_date.strftime('%Y-%m-%d')} (Likely a market holiday).")
                elif response.status_code == 403:
                    logging.error("Access forbidden. Refreshing session cookies...")
                    session = get_nse_session()  # Refresh session
                    time.sleep(2)
                    continue  # Retry the same date
                else:
                    logging.warning(f"Failed with status code {response.status_code} for {url}")

            except Exception as e:
                logging.error(f"Error fetching data for {current_date.strftime('%Y-%m-%d')}: {e}")

            # Sleep to prevent IP blocking
            time.sleep(2)

        current_date += timedelta(days=1)


if __name__ == "__main__":
    # Create an index on SYMBOL and DATE1 to ensure fast queries later
    logging.info("Ensuring database indexes...")
    collection.create_index([("SYMBOL", 1), ("extraction_date", -1)])

    # Start the download process
    logging.info("Starting NSE Bhavcopy extraction...")
    download_historical_data("2026-01-01")
    logging.info("Extraction complete.")