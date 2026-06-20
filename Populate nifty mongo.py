"""
Nifty / NSE 1-Minute Candle Downloader → MongoDB
==================================================
Replaces the SQL Server version. Stores candles in:
  MongoDB  :  mongodb://localhost:27017/
  Database :  NSE_DAILY
  Collection: candles

Document Schema:
  {
    instrument_key : "NSE_FO|66071",   ← Upstox key
    ts             : datetime (UTC),    ← candle open time, UTC stored, IST display
    o, h, l, c     : float,
    v, oi          : int
  }
  Unique index on (instrument_key, ts) — safe to re-run, duplicates skipped.

Usage:
  python populate_nifty_mongo.py                   # process nse.csv
  python populate_nifty_mongo.py --key NSE_FO|66071 --days 365
  python populate_nifty_mongo.py --key NSE_FO|66071 --from 2024-01-01 --to 2024-12-31
  python populate_nifty_mongo.py --info             # show DB stats
  python populate_nifty_mongo.py --list             # list all stored instruments
"""

import os
import sys
import time
import urllib.parse
import datetime
import argparse

import requests
import pandas as pd
from pymongo import MongoClient, UpdateOne, ASCENDING
from pymongo.errors import BulkWriteError

# ─────────────────────────────────────────────────────────────────
# 1.  CONFIGURATION
# ─────────────────────────────────────────────────────────────────

TOKEN_FILE      = "token.txt"
INPUT_CSV       = "nse.csv"
HISTORICAL_URL  = "https://api.upstox.com/v2/historical-candle"

# — MongoDB —
MONGO_URI       = "mongodb://localhost:27017/"
DB_NAME         = "NSE_DAILY"
COLLECTION      = "candles"

# — Download settings —
HISTORY_DAYS    = 365          # default lookback (changeable via --days)
CHUNK_DAYS      = 30           # days per API call (Upstox 1-min limit per call)
REQUEST_DELAY   = 0.15         # seconds between API calls (rate-limit safety)


# ─────────────────────────────────────────────────────────────────
# 2.  SETUP
# ─────────────────────────────────────────────────────────────────

def load_token() -> str:
    if not os.path.exists(TOKEN_FILE):
        print(f"❌  {TOKEN_FILE} not found.")
        sys.exit(1)
    with open(TOKEN_FILE) as fh:
        return fh.read().strip()


def get_mongo_col():
    """Connect to MongoDB and return (client, collection) with index ensured."""
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    # Trigger connection check
    client.server_info()
    col = client[DB_NAME][COLLECTION]
    # Unique compound index — ensures no duplicates, enables fast range queries
    col.create_index(
        [("instrument_key", ASCENDING), ("ts", ASCENDING)],
        unique=True, name="ix_key_ts", background=True
    )
    return client, col


def fix_key(key: str) -> str:
    """Normalise instrument key — ensure exchange prefix exists."""
    key = str(key).strip()
    if '|' in key:
        return key
    if key.startswith(('INE', 'INF')):
        return f"NSE_EQ|{key}"
    return f"NSE_EQ|{key}"


# ─────────────────────────────────────────────────────────────────
# 3.  MONGODB HELPERS
# ─────────────────────────────────────────────────────────────────

def chunk_exists(col, key: str, from_dt: datetime.datetime, to_dt: datetime.datetime) -> bool:
    """Check if ANY candle exists for this key in the date range."""
    return col.count_documents(
        {"instrument_key": key, "ts": {"$gte": from_dt, "$lte": to_dt}},
        limit=1
    ) > 0


def save_to_mongo(col, df: pd.DataFrame) -> int:
    """
    Bulk-upsert candles into MongoDB.
    Returns number of new documents inserted.
    """
    if df.empty:
        return 0

    ops = []
    for _, row in df.iterrows():
        # Store timestamp as UTC datetime (timezone-naive UTC in MongoDB)
        ts_utc = row["ts"].to_pydatetime()
        if hasattr(ts_utc, "tzinfo") and ts_utc.tzinfo is not None:
            ts_utc = ts_utc.astimezone(datetime.timezone.utc).replace(tzinfo=None)

        ops.append(UpdateOne(
            {"instrument_key": row["instrument_key"], "ts": ts_utc},
            {"$setOnInsert": {
                "instrument_key": row["instrument_key"],
                "ts": ts_utc,
                "o":  float(row["o"]),
                "h":  float(row["h"]),
                "l":  float(row["l"]),
                "c":  float(row["c"]),
                "v":  int(row["v"]),
                "oi": int(row["oi"]),
            }},
            upsert=True
        ))

    try:
        result = col.bulk_write(ops, ordered=False)
        return result.upserted_count
    except BulkWriteError as bwe:
        # Count only real errors (not duplicate key E11000)
        real_errors = [e for e in bwe.details.get("writeErrors", [])
                       if e.get("code") != 11000]
        if real_errors:
            print(f"\n   ⚠️  Bulk write errors: {len(real_errors)}")
        return bwe.details.get("nUpserted", 0)


# ─────────────────────────────────────────────────────────────────
# 4.  API FETCH
# ─────────────────────────────────────────────────────────────────

def fetch_chunk(key: str, from_str: str, to_str: str, headers: dict,
                retry: int = 3) -> pd.DataFrame:
    """
    Fetch 1-min candles from Upstox for a single date range.
    Endpoint: GET /v2/historical-candle/{encoded_key}/1minute/{to}/{from}
    """
    enc = urllib.parse.quote(key, safe="")
    url = f"{HISTORICAL_URL}/{enc}/1minute/{to_str}/{from_str}"

    for attempt in range(retry):
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code == 200:
                data = r.json()
                if data.get("status") == "success":
                    candles = data.get("data", {}).get("candles", [])
                    if candles:
                        df = pd.DataFrame(
                            candles,
                            columns=["ts", "o", "h", "l", "c", "v", "oi"]
                        )
                        df["ts"] = pd.to_datetime(df["ts"]).dt.tz_convert("Asia/Kolkata")
                        df["instrument_key"] = key
                        return df
                return pd.DataFrame()

            elif r.status_code == 429:
                wait = 2 * (attempt + 1)
                sys.stdout.write(f"\r   ⏳ Rate limit — waiting {wait}s...  ")
                sys.stdout.flush()
                time.sleep(wait)

            elif r.status_code == 401:
                print("\n❌  Token expired (HTTP 401). Regenerate token.txt.")
                sys.exit(1)

            else:
                return pd.DataFrame()

        except Exception as exc:
            if attempt == retry - 1:
                print(f"\n   ❌  Network error: {exc}")
    return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────
# 5.  SYMBOL PROCESSOR
# ─────────────────────────────────────────────────────────────────

def process_symbol(key: str, col, headers: dict,
                   from_dt: datetime.datetime, to_dt: datetime.datetime) -> int:
    """Download and store all missing candle chunks for one instrument."""
    current_to  = to_dt
    total_saved = 0

    while current_to > from_dt:
        current_from = current_to - datetime.timedelta(days=CHUNK_DAYS)
        if current_from < from_dt:
            current_from = from_dt

        to_str   = current_to.strftime("%Y-%m-%d")
        from_str = current_from.strftime("%Y-%m-%d")
        status   = f"{from_str} → {to_str}"

        # Convert to UTC-naive for MongoDB comparison
        from_utc = current_from.replace(tzinfo=None)
        to_utc   = current_to.replace(tzinfo=None)

        if chunk_exists(col, key, from_utc, to_utc):
            sys.stdout.write(f"\r   ✅ {status}  (cached)          ")
            sys.stdout.flush()
        else:
            sys.stdout.write(f"\r   ⏳ {status}  fetching...       ")
            sys.stdout.flush()

            df = fetch_chunk(key, from_str, to_str, headers)
            if not df.empty:
                saved = save_to_mongo(col, df)
                total_saved += saved
                sys.stdout.write(f"\r   💾 {status}  +{len(df)} candles ({saved} new)   ")
                sys.stdout.flush()
            else:
                sys.stdout.write(f"\r   ⚠️  {status}  no data          ")
                sys.stdout.flush()

            time.sleep(REQUEST_DELAY)

        current_to = current_from - datetime.timedelta(days=1)

    sys.stdout.write("\r" + " " * 85 + "\r")
    return total_saved


# ─────────────────────────────────────────────────────────────────
# 6.  INFO / LIST UTILITIES
# ─────────────────────────────────────────────────────────────────

def show_info(col):
    """Print DB statistics."""
    total = col.count_documents({})
    keys  = col.distinct("instrument_key")
    print(f"\n{'─'*55}")
    print(f"  MongoDB DB   : {DB_NAME}")
    print(f"  Collection   : {COLLECTION}")
    print(f"  Total candles: {total:,}")
    print(f"  Instruments  : {len(keys)}")
    if keys:
        print(f"\n  Sample keys  :")
        for k in keys[:10]:
            cnt  = col.count_documents({"instrument_key": k})
            first = col.find_one({"instrument_key": k}, sort=[("ts", 1)])
            last  = col.find_one({"instrument_key": k}, sort=[("ts", -1)])
            ts_f  = first["ts"].strftime("%Y-%m-%d") if first else "?"
            ts_l  = last["ts"].strftime("%Y-%m-%d")  if last  else "?"
            print(f"    {k:<30} {cnt:>8,} candles  ({ts_f} → {ts_l})")
        if len(keys) > 10:
            print(f"    ... and {len(keys)-10} more")
    print(f"{'─'*55}\n")


def list_keys(col):
    """Print all stored instrument keys with candle counts."""
    keys = col.distinct("instrument_key")
    print(f"\n  {'INSTRUMENT KEY':<35} {'CANDLES':>9}  {'FROM':<12} {'TO'}")
    print("  " + "─" * 75)
    for k in sorted(keys):
        cnt   = col.count_documents({"instrument_key": k})
        first = col.find_one({"instrument_key": k}, sort=[("ts",  1)])
        last  = col.find_one({"instrument_key": k}, sort=[("ts", -1)])
        ts_f  = first["ts"].strftime("%Y-%m-%d") if first else "N/A"
        ts_l  = last["ts"].strftime("%Y-%m-%d")  if last  else "N/A"
        print(f"  {k:<35} {cnt:>9,}  {ts_f:<12} {ts_l}")
    print()


# ─────────────────────────────────────────────────────────────────
# 7.  MAIN
# ─────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Download Upstox 1-min candles → MongoDB")
    p.add_argument("--key",   help="Single instrument key, e.g. NSE_FO|66071")
    p.add_argument("--days",  type=int, default=HISTORY_DAYS,
                   help=f"Lookback days (default {HISTORY_DAYS})")
    p.add_argument("--from",  dest="from_date",
                   help="Start date YYYY-MM-DD (overrides --days)")
    p.add_argument("--to",    dest="to_date",
                   help="End date YYYY-MM-DD (default today)")
    p.add_argument("--csv",   default=INPUT_CSV,
                   help=f"Input CSV file (default {INPUT_CSV})")
    p.add_argument("--info",  action="store_true", help="Show DB statistics and exit")
    p.add_argument("--list",  action="store_true", help="List stored instruments and exit")
    return p.parse_args()


def main():
    args    = parse_args()
    token   = load_token()
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    # — MongoDB connection —
    print("Connecting to MongoDB...")
    try:
        client, col = get_mongo_col()
        print(f"✅  MongoDB connected  →  {DB_NAME}.{COLLECTION}")
    except Exception as exc:
        print(f"❌  MongoDB connection failed: {exc}")
        print("    Is mongod running?  Start with:  mongod --dbpath /data/db")
        sys.exit(1)

    # — Info / List shortcuts —
    if args.info:
        show_info(col)
        client.close()
        return

    if args.list:
        list_keys(col)
        client.close()
        return

    # — Date range —
    to_dt   = (datetime.datetime.strptime(args.to_date, "%Y-%m-%d")
               if args.to_date else datetime.datetime.now())
    from_dt = (datetime.datetime.strptime(args.from_date, "%Y-%m-%d")
               if args.from_date
               else to_dt - datetime.timedelta(days=args.days))

    print(f"Date range : {from_dt.strftime('%Y-%m-%d')} → {to_dt.strftime('%Y-%m-%d')}")
    print(f"Chunk size : {CHUNK_DAYS} days per API call")
    print()

    # — Build symbol list —
    symbols: list[tuple[str, str]] = []

    if args.key:
        key = fix_key(args.key)
        symbols = [(key, key)]
        print(f"🎯 Single instrument: {key}\n")

    else:
        csv_path = args.csv
        if not os.path.exists(csv_path):
            print(f"❌  CSV file '{csv_path}' not found.")
            client.close()
            return
        try:
            df_csv   = pd.read_csv(csv_path)
            key_col  = next((c for c in df_csv.columns
                             if "instrument" in c.lower() or "key" in c.lower()),
                            df_csv.columns[0])
            name_col = next((c for c in df_csv.columns
                             if ("symbol" in c.lower() or "name" in c.lower())
                             and c != key_col),
                            key_col)
            symbols = [(fix_key(str(r[key_col])), str(r[name_col]))
                       for _, r in df_csv.iterrows()]
            print(f"✅  Loaded {len(symbols)} symbols from {csv_path}\n")
        except Exception as exc:
            print(f"❌  CSV read error: {exc}")
            client.close()
            return

    # — Process —
    grand_total = 0
    for i, (key, name) in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}]  {name}  ({key})")
        saved       = process_symbol(key, col, headers, from_dt, to_dt)
        grand_total += saved
        msg = f"   ✅ {saved:,} new candles saved." if saved else "   ✨ Already up-to-date."
        print(msg)

    client.close()
    print(f"\n🎉  Done.  Total new candles saved: {grand_total:,}")


if __name__ == "__main__":
    main()