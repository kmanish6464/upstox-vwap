"""
NSE F&O — Participant-wise Open Interest → MongoDB
==================================================
Downloads the daily "Participant wise Open Interest" report from
https://www.nseindia.com/all-reports-derivatives and stores each day's
five participant rows (Client, DII, FII, Pro, TOTAL) into a local MongoDB.

Mirrors the storage pattern used in bhavcopy_2905.py:
  - requests.Session primed with NSE headers / cookies
  - day-by-day backfill loop (START_DATE -> END_DATE), weekends skipped
  - unique compound index, automatic de-duplication
  - idempotent: re-running only inserts missing days

Source file per day (the exact file behind the report's download icon):
  https://archives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv

Requirements:
  pip install requests pandas pymongo
  A MongoDB instance running locally (mongodb://localhost:27017/)
"""

import time
from io import StringIO
from datetime import datetime, timedelta

import requests
import pandas as pd
from pymongo import MongoClient
from pymongo.errors import BulkWriteError

# --- Configuration ------------------------------------------------------------

MONGO_URI       = "mongodb://localhost:27017/"
DB_NAME         = "NSE_DAILY"
COLLECTION_NAME = "participant_oi"

START_DATE      = datetime(2026, 1, 1)        # backfill start
END_DATE        = datetime.now()              # up to today

BASE_URL = "https://archives.nseindia.com/content/nsccl/fao_participant_oi_{date}.csv"

NSE_HEADERS = {
    "User-Agent"     : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer"        : "https://www.nseindia.com/all-reports-derivatives",
}

# CSV column header  ->  MongoDB field name
COLUMN_MAP = {
    "Client Type"            : "client_type",
    "Future Index Long"      : "future_index_long",
    "Future Index Short"     : "future_index_short",
    "Future Stock Long"      : "future_stock_long",
    "Future Stock Short"     : "future_stock_short",
    "Option Index Call Long" : "option_index_call_long",
    "Option Index Put Long"  : "option_index_put_long",
    "Option Index Call Short": "option_index_call_short",
    "Option Index Put Short" : "option_index_put_short",
    "Option Stock Call Long" : "option_stock_call_long",
    "Option Stock Put Long"  : "option_stock_put_long",
    "Option Stock Call Short": "option_stock_call_short",
    "Option Stock Put Short" : "option_stock_put_short",
    "Total Long Contracts"   : "total_long_contracts",
    "Total Short Contracts"  : "total_short_contracts",
}

NUMERIC_FIELDS = [v for v in COLUMN_MAP.values() if v != "client_type"]


# --- MongoDB Helpers ----------------------------------------------------------

def get_collection():
    client = MongoClient(MONGO_URI)
    return client[DB_NAME][COLLECTION_NAME]


def setup_indexes(collection):
    """Ensure a unique compound index on (trade_date, client_type)."""
    pipeline = [
        {"$group": {"_id": {"trade_date": "$trade_date", "client_type": "$client_type"},
                    "count": {"$sum": 1}, "ids": {"$push": "$_id"}}},
        {"$match": {"count": {"$gt": 1}}},
    ]
    duplicates = list(collection.aggregate(pipeline, allowDiskUse=True))
    if duplicates:
        ids_to_delete = [oid for doc in duplicates for oid in doc["ids"][1:]]
        deleted = 0
        for i in range(0, len(ids_to_delete), 500):
            batch = ids_to_delete[i:i + 500]
            deleted += collection.delete_many({"_id": {"$in": batch}}).deleted_count
        print(f"[setup] Removed {deleted} duplicate records.")

    try:
        collection.drop_index("trade_date_1_client_type_1")
    except Exception:
        pass

    collection.create_index([("trade_date", 1), ("client_type", 1)], unique=True)
    collection.create_index("trade_date")
    print("[setup] Indexes ready.")


# --- Download & Parse ---------------------------------------------------------

def _get_nse_session():
    """A session primed with a homepage hit so the archives server returns data."""
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception:
        pass
    return session


def _parse_trade_date(title_line):
    """Extract the trade date from the title row ('... as on Jun 11, 2026')."""
    marker = "as on"
    if marker not in title_line:
        return None
    date_text = title_line.split(marker, 1)[1].strip().strip('"').strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_text, fmt)
        except ValueError:
            continue
    return None


def parse_participant_csv(text, fallback_date):
    """
    Parse the participant-wise OI CSV into a list of MongoDB documents.

    Layout:
      line 0 : title row  (contains the trade date)
      line 1 : column header
      line 2+: Client / DII / FII / Pro / TOTAL data rows
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        return []

    trade_date = _parse_trade_date(lines[0]) or fallback_date

    df = pd.read_csv(StringIO("\n".join(lines[1:])))
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns=COLUMN_MAP)
    df["client_type"] = df["client_type"].astype(str).str.strip()

    records = []
    for _, row in df.iterrows():
        ctype = row.get("client_type", "")
        if not ctype or ctype.lower() == "nan":
            continue
        doc = {
            "trade_date"     : trade_date,
            "client_type"    : ctype,
            "report"         : "participant_oi",
            "extraction_date": datetime.now(),
        }
        for field in NUMERIC_FIELDS:
            raw = str(row.get(field, "0")).replace(",", "").strip()
            try:
                doc[field] = int(float(raw))
            except (ValueError, TypeError):
                doc[field] = 0
        records.append(doc)
    return records


# --- Sync ---------------------------------------------------------------------

def sync_to_mongo():
    """Backfill + daily sync of participant-wise OI into MongoDB."""
    collection = get_collection()
    setup_indexes(collection)

    session = _get_nse_session()
    current = START_DATE
    print("--- Starting Participant-OI Sync --------------------------------------")

    while current <= END_DATE:
        if current.weekday() >= 5:                       # skip Sat/Sun
            current += timedelta(days=1)
            continue

        date_str = current.strftime("%d%m%Y")            # DDMMYYYY
        url = BASE_URL.format(date=date_str)

        try:
            if collection.count_documents({"trade_date": current}) >= 5:
                print(f"  [{current.date()}] Already synced - skipping.")
                current += timedelta(days=1)
                continue

            resp = session.get(url, timeout=15)

            if resp.status_code == 404:
                print(f"  [{current.date()}] Holiday / not published.")
            elif resp.status_code == 200 and resp.text.strip():
                records = parse_participant_csv(resp.text, current)
                if records:
                    file_date = records[0]["trade_date"]
                    if collection.count_documents({"trade_date": file_date}) >= 5:
                        print(f"  [{current.date()}] Already synced (file date {file_date.date()}).")
                    else:
                        try:
                            collection.insert_many(records, ordered=False)
                            print(f"  [{current.date()}] OK {len(records)} rows added "
                                  f"(trade date {file_date.date()}).")
                        except BulkWriteError as bwe:
                            inserted = bwe.details.get("nInserted", 0)
                            print(f"  [{current.date()}] Partial insert: {inserted} added "
                                  f"(rest duplicates).")
                else:
                    print(f"  [{current.date()}] Empty / unparseable response.")
            else:
                print(f"  [{current.date()}] HTTP {resp.status_code}.")

        except Exception as exc:
            print(f"  [{current.date()}] Error: {exc}")

        current += timedelta(days=1)
        time.sleep(0.3)

    print("--- Sync complete -----------------------------------------------------\n")


# --- Read-back helpers --------------------------------------------------------

# (mongo field, short header, width, alignment) - all 15 columns, report order.
DISPLAY_COLS = [
    ("client_type",             "Client",    8, "<"),
    ("future_index_long",       "FutIdxL",   9, ">"),
    ("future_index_short",      "FutIdxS",   9, ">"),
    ("future_stock_long",       "FutStkL",  10, ">"),
    ("future_stock_short",      "FutStkS",  10, ">"),
    ("option_index_call_long",  "OptIdxCL", 11, ">"),
    ("option_index_put_long",   "OptIdxPL", 11, ">"),
    ("option_index_call_short", "OptIdxCS", 11, ">"),
    ("option_index_put_short",  "OptIdxPS", 11, ">"),
    ("option_stock_call_long",  "OptStkCL", 11, ">"),
    ("option_stock_put_long",   "OptStkPL", 11, ">"),
    ("option_stock_call_short", "OptStkCS", 11, ">"),
    ("option_stock_put_short",  "OptStkPS", 11, ">"),
    ("total_long_contracts",    "TotLong",  12, ">"),
    ("total_short_contracts",   "TotShort", 12, ">"),
]


def _print_day(rows, day):
    order = {"Client": 0, "DII": 1, "FII": 2, "Pro": 3, "TOTAL": 4}
    rows = sorted(rows, key=lambda r: order.get(r["client_type"], 9))

    print(f"\nTrade date: {day.date()}  ({len(rows)} participant rows) - ALL columns\n")
    header = "".join(format(name, align + str(w)) for _, name, w, align in DISPLAY_COLS)
    print(header)
    print("-" * len(header))
    for r in rows:
        line = ""
        for field, _, w, align in DISPLAY_COLS:
            cell = r[field] if field == "client_type" else f"{r[field]:,}"
            line += format(cell, align + str(w))
        print(line)
    print()


def show_latest():
    """Print ALL 15 columns of the most recent day's participant OI in MongoDB."""
    collection = get_collection()
    latest = collection.find_one(sort=[("trade_date", -1)])
    if not latest:
        print("No data in MongoDB yet.")
        return
    day = latest["trade_date"]
    _print_day(list(collection.find({"trade_date": day})), day)


def export_csv(out_path="participant_oi_export.csv"):
    """Dump every stored day x participant row (all columns) to a flat CSV."""
    collection = get_collection()
    df = pd.DataFrame(list(collection.find({}, {"_id": 0, "extraction_date": 0})))
    if df.empty:
        print("No data to export.")
        return
    ordered = ["trade_date", "client_type", "report"] + NUMERIC_FIELDS
    df = df[[c for c in ordered if c in df.columns]]
    df = df.sort_values(["trade_date", "client_type"])
    df.to_csv(out_path, index=False)
    print(f"Exported {len(df)} rows -> {out_path}")


# --- Entry Point --------------------------------------------------------------

if __name__ == "__main__":
    sync_to_mongo()
    show_latest()
    # export_csv()      # uncomment to dump the whole collection to CSV
