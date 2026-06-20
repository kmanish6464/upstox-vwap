"""
NSE Bhavcopy — Sync + Bullish Stock Analysis
=============================================
Downloads NSE full bhavcopy CSVs into MongoDB, then analyses
for high-delivery, high-volume bullish setups.

MongoDB schema:
  DB         : NSE_DAILY
  Collection : bhavcopy
  Key fields : SYMBOL, SERIES, DATE1 (str "DD-Mon-YYYY"),
               extraction_date (datetime), OPEN_PRICE, CLOSE_PRICE,
               HIGH_PRICE, LOW_PRICE, TTL_TRD_QNTY, DELIV_PER …

Usage:
  python bhavcopy_analysis.py            # sync + analyse
  python bhavcopy_analysis.py --sync     # sync only
  python bhavcopy_analysis.py --analyse  # analyse only
  python bhavcopy_analysis.py --top 30   # show top N results (default 20)

Dependencies:
  pip install requests pandas pymongo
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from pymongo import MongoClient, ASCENDING, DESCENDING

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
MONGO_URI       = "mongodb://localhost:27017/"
DB_NAME         = "NSE_DAILY"
COLLECTION_NAME = "bhavcopy"

START_DATE      = datetime(2026, 1, 1)
IST             = ZoneInfo("Asia/Kolkata")

OUTPUT_CSV      = Path("bullish_stocks_analysis.csv")
BHAV_URL        = "https://archives.nseindia.com/products/content/sec_bhavdata_full_{date}.csv"

# Bullish filter thresholds
STRICT  = dict(vol_multiplier=1.5, min_deliv_pct=50.0)
RELAXED = dict(vol_multiplier=1.2, min_deliv_pct=25.0)

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# 1. HTTP SESSION
# ══════════════════════════════════════════════════════════════════════════════
def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/119.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/all-reports",
    })
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception:
        pass
    return session


# ══════════════════════════════════════════════════════════════════════════════
# 2. MONGODB
# ══════════════════════════════════════════════════════════════════════════════
def connect() -> tuple[MongoClient, any]:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5_000)
    client.server_info()
    col = client[DB_NAME][COLLECTION_NAME]
    return client, col


def ensure_indexes(col) -> None:
    col.create_index([("SYMBOL", ASCENDING), ("DATE1", ASCENDING)], unique=True, name="sym_date")
    col.create_index([("extraction_date", DESCENDING)],                              name="by_ext_date")
    col.create_index([("SERIES", ASCENDING)],                                        name="by_series")


# ══════════════════════════════════════════════════════════════════════════════
# 3. SYNC
# ══════════════════════════════════════════════════════════════════════════════
def _parse_row(row: pd.Series, extraction_date: datetime) -> dict:
    def f(key, default=0.0): return float(str(row.get(key, default)).replace(",", "") or default)
    def i(key, default=0):   return int(str(row.get(key, default)).replace(",", "").split(".")[0] or default)
    def s(key):               return str(row.get(key, "")).strip()

    return {
        "SYMBOL":       s("SYMBOL"),
        "SERIES":       s("SERIES"),
        "DATE1":        s("DATE1"),
        "PREV_CLOSE":   f("PREV_CLOSE"),
        "OPEN_PRICE":   f("OPEN_PRICE"),
        "HIGH_PRICE":   f("HIGH_PRICE"),
        "LOW_PRICE":    f("LOW_PRICE"),
        "LAST_PRICE":   f("LAST_PRICE"),
        "CLOSE_PRICE":  f("CLOSE_PRICE"),
        "AVG_PRICE":    f("AVG_PRICE"),
        "TTL_TRD_QNTY": i("TTL_TRD_QNTY"),
        "TURNOVER_LACS":f("TURNOVER_LACS"),
        "NO_OF_TRADES": i("NO_OF_TRADES"),
        "DELIV_QTY":    s("DELIV_QTY"),
        "DELIV_PER":    f("DELIV_PER"),
        "extraction_date": extraction_date,
    }


def _already_synced(col, date: datetime) -> bool:
    return col.count_documents({"extraction_date": date}, limit=1) > 0


def sync_date(col, session: requests.Session, date: datetime) -> None:
    if _already_synced(col, date):
        log.info(f"  [{date.date()}] Already synced — skipping")
        return

    url = BHAV_URL.format(date=date.strftime("%d%m%Y"))
    try:
        r = session.get(url, timeout=15)

        if r.status_code == 404:
            log.info(f"  [{date.date()}] Holiday or no data")
            return
        if r.status_code != 200:
            log.warning(f"  [{date.date()}] HTTP {r.status_code}")
            return

        df = pd.read_csv(StringIO(r.text))
        df.columns = [c.strip().upper() for c in df.columns]

        if df.empty:
            log.warning(f"  [{date.date()}] Empty CSV")
            return

        # Get DATE1 from CSV to check for duplicates
        date1_val = str(df["DATE1"].iloc[0]).strip()
        existing  = set(col.distinct("SYMBOL", {"DATE1": date1_val}))

        docs = [
            _parse_row(row, date)
            for _, row in df.iterrows()
            if str(row.get("SYMBOL", "")).strip() not in existing
        ]

        if docs:
            col.insert_many(docs, ordered=False)
            log.info(f"  [{date.date()}] Inserted {len(docs)} records  (DATE1={date1_val})")
        else:
            log.info(f"  [{date.date()}] No new records")

    except Exception as e:
        log.error(f"  [{date.date()}] {e}")


def run_sync() -> None:
    log.info("═" * 60)
    log.info("  Bhavcopy Sync")
    log.info("═" * 60)

    client, col = connect()
    ensure_indexes(col)
    session = build_session()

    end  = datetime.now()
    date = START_DATE

    while date <= end:
        if date.weekday() < 5:          # Mon–Fri only
            sync_date(col, session, date)
            time.sleep(0.15)            # polite delay
        date += timedelta(days=1)

    client.close()
    log.info("  ✔  Sync complete")


# ══════════════════════════════════════════════════════════════════════════════
# 4. RESOLVE LAST TRADING DATE
# ══════════════════════════════════════════════════════════════════════════════
def last_trading_date(col) -> datetime | None:
    """Return the most recent extraction_date that has EQ/BE data."""
    doc = col.find_one(
        {"SERIES": {"$in": ["EQ", "BE"]}},
        {"extraction_date": 1},
        sort=[("extraction_date", DESCENDING)],
    )
    return doc["extraction_date"] if doc else None


# ══════════════════════════════════════════════════════════════════════════════
# 5. ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════
def load_data(col) -> pd.DataFrame:
    """Load all EQ/BE records needed for rolling window calculation."""
    docs = list(col.find(
        {"SERIES": {"$in": ["EQ", "BE"]}},
        {"_id": 0, "SYMBOL": 1, "SERIES": 1, "DATE1": 1,
         "CLOSE_PRICE": 1, "OPEN_PRICE": 1, "HIGH_PRICE": 1, "LOW_PRICE": 1,
         "TTL_TRD_QNTY": 1, "DELIV_PER": 1, "extraction_date": 1},
    ))
    if not docs:
        return pd.DataFrame()

    df = pd.DataFrame(docs)
    df["CLOSE_PRICE"]  = pd.to_numeric(df["CLOSE_PRICE"],  errors="coerce")
    df["OPEN_PRICE"]   = pd.to_numeric(df["OPEN_PRICE"],   errors="coerce")
    df["TTL_TRD_QNTY"] = pd.to_numeric(df["TTL_TRD_QNTY"], errors="coerce")
    df["DELIV_PER"]    = pd.to_numeric(df["DELIV_PER"],    errors="coerce").fillna(0)
    return df.sort_values(["SYMBOL", "extraction_date"]).reset_index(drop=True)


def add_rolling_features(df: pd.DataFrame) -> pd.DataFrame:
    grp = df.groupby("SYMBOL")
    df["prev_close"]  = grp["CLOSE_PRICE"].shift(1)
    df["avg_vol_5d"]  = grp["TTL_TRD_QNTY"].transform(lambda x: x.rolling(5, min_periods=1).mean())
    return df


def apply_filters(df: pd.DataFrame, thresholds: dict) -> pd.DataFrame:
    return df[
        (df["CLOSE_PRICE"] > df["prev_close"]) &
        (df["TTL_TRD_QNTY"] > df["avg_vol_5d"] * thresholds["vol_multiplier"]) &
        (df["DELIV_PER"] > thresholds["min_deliv_pct"])
    ].copy()


def run_analysis(top_n: int) -> None:
    log.info("═" * 60)
    log.info("  Bullish Stock Analysis")
    log.info("═" * 60)

    client, col = connect()

    trade_date = last_trading_date(col)
    if trade_date is None:
        log.error("  No EQ/BE data in MongoDB — run sync first")
        client.close()
        return

    log.info(f"  Last trading date : {trade_date.date()}")

    df = load_data(col)
    client.close()

    if df.empty:
        log.error("  No data loaded")
        return

    log.info(f"  Loaded {len(df)} rows across {df['SYMBOL'].nunique()} symbols")

    df = add_rolling_features(df)

    # Latest date slice
    latest = df[df["extraction_date"] == trade_date].copy()
    log.info(f"  Symbols on {trade_date.date()} : {len(latest)}")

    # Try strict → relaxed
    for label, thresholds in (("strict", STRICT), ("relaxed", RELAXED)):
        bullish = apply_filters(latest, thresholds)
        if not bullish.empty:
            log.info(f"  Filter : {label}  ({len(bullish)} stocks)")
            break
    else:
        log.warning("  No stocks met even relaxed criteria")
        return

    bullish["VOL_RATIO"]     = (bullish["TTL_TRD_QNTY"] / bullish["avg_vol_5d"]).round(2)
    bullish["PRICE_CHG_PCT"] = ((bullish["CLOSE_PRICE"] - bullish["prev_close"]) / bullish["prev_close"] * 100).round(2)

    out_cols = ["SYMBOL", "DATE1", "OPEN_PRICE", "CLOSE_PRICE",
                "PRICE_CHG_PCT", "DELIV_PER", "VOL_RATIO", "TTL_TRD_QNTY"]
    result = bullish[out_cols].sort_values("VOL_RATIO", ascending=False).reset_index(drop=True)

    result.to_csv(OUTPUT_CSV, index=False)

    sep = "─" * 60
    print(f"\n{'═'*60}")
    print(f"  Bullish Stocks — {trade_date.date()}  |  filter={label}  |  count={len(result)}")
    print(f"{'═'*60}")
    print(result.head(top_n).to_string(index=False))
    print(f"\n  Full list saved → {OUTPUT_CSV}")
    print(f"{'═'*60}\n")

    log.info("  ✔  Analysis complete")


# ══════════════════════════════════════════════════════════════════════════════
# 6. CLI
# ══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="NSE Bhavcopy sync + bullish stock screener",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python bhavcopy_analysis.py             # sync + analyse
  python bhavcopy_analysis.py --sync      # sync only
  python bhavcopy_analysis.py --analyse   # analyse only
  python bhavcopy_analysis.py --top 30    # top 30 results
        """,
    )
    parser.add_argument("--sync",    action="store_true", help="Download & sync bhavcopy to MongoDB")
    parser.add_argument("--analyse", action="store_true", help="Run bullish stock analysis")
    parser.add_argument("--top",     default=20, type=int, help="Top N stocks to display (default: 20)")

    args = parser.parse_args()

    # Default: run both if no flag given
    do_sync    = args.sync    or (not args.sync and not args.analyse)
    do_analyse = args.analyse or (not args.sync and not args.analyse)

    if do_sync:
        run_sync()
    if do_analyse:
        run_analysis(top_n=args.top)


if __name__ == "__main__":
    main()