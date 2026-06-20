"""
NSE Option Chain — OI Fetcher & MongoDB Writer
================================================
Fetches OI data from NSE every 10 seconds and stores it in MongoDB.

Requirements:
    pip install requests pymongo schedule

MongoDB must be running locally:
    mongod --dbpath /data/db

Collections created:
    nse_oi_db.oi_snapshots   ← raw snapshots every 10 sec
    nse_oi_db.oi_summary     ← per-strike CE/PE OI aggregated per fetch
"""

import requests
import schedule
import time
import logging
from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING
from pymongo.errors import ConnectionFailure

# ──────────────────────────────────────────────
# CONFIG  (edit these as needed)
# ──────────────────────────────────────────────
SYMBOL          = "NIFTY"          # NIFTY | BANKNIFTY | FINNIFTY | MIDCPNIFTY
MONGO_URI       = "mongodb://localhost:27017/"
DB_NAME         = "nse_oi_db"
FETCH_INTERVAL  = 10               # seconds
LOG_LEVEL       = logging.INFO
# ──────────────────────────────────────────────

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── NSE session (cookies are mandatory) ───────
NSE_BASE   = "https://www.nseindia.com"
NSE_API    = f"{NSE_BASE}/api/option-chain-indices?symbol={SYMBOL}"
HEADERS    = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         f"{NSE_BASE}/option-chain",
    "Connection":      "keep-alive",
}

session = requests.Session()
session.headers.update(HEADERS)

# ── MongoDB setup ──────────────────────────────
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=4000)
    client.admin.command("ping")
    db              = client[DB_NAME]
    col_snapshots   = db["oi_snapshots"]
    col_summary     = db["oi_summary"]
    # Indexes for fast time-range queries
    col_snapshots.create_index([("timestamp", ASCENDING)])
    col_summary.create_index([("timestamp", ASCENDING), ("strike", ASCENDING)])
    log.info("✅  MongoDB connected  →  %s / %s", MONGO_URI, DB_NAME)
except ConnectionFailure as exc:
    log.critical("❌  Cannot connect to MongoDB: %s", exc)
    raise SystemExit(1)


# ── Helpers ────────────────────────────────────
def warm_session() -> bool:
    """Hit the homepage once to get cookies — required by NSE."""
    try:
        r = session.get(NSE_BASE, timeout=10)
        r.raise_for_status()
        log.info("🍪  Session warmed (cookies obtained)")
        return True
    except Exception as exc:
        log.warning("⚠️  Session warm failed: %s", exc)
        return False


def fetch_option_chain() -> dict | None:
    """Fetch raw option-chain JSON from NSE API."""
    try:
        r = session.get(NSE_API, timeout=10)
        r.raise_for_status()
        data = r.json()
        log.info("📥  Fetched option chain for %s  |  status %s", SYMBOL, r.status_code)
        return data
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 401:
            log.warning("🔄  401 — re-warming session …")
            warm_session()
        else:
            log.error("HTTP error: %s", exc)
    except Exception as exc:
        log.error("Fetch error: %s", exc)
    return None


def parse_and_store(data: dict) -> None:
    """Parse OI fields and write both collections to MongoDB."""
    now = datetime.now(timezone.utc)
    records = data.get("records", {})
    underlying_value = records.get("underlyingValue", 0)
    expiry_dates     = records.get("expiryDates", [])
    nearest_expiry   = expiry_dates[0] if expiry_dates else "N/A"

    raw_data = records.get("data", [])
    if not raw_data:
        log.warning("No data rows in response")
        return

    # ── 1. Full raw snapshot ────────────────────
    col_snapshots.insert_one({
        "symbol":           SYMBOL,
        "timestamp":        now,
        "underlying_value": underlying_value,
        "nearest_expiry":   nearest_expiry,
        "raw":              raw_data,          # full payload stored for audit
    })

    # ── 2. Per-strike OI summary ────────────────
    summary_docs = []
    for row in raw_data:
        strike = row.get("strikePrice", 0)

        ce = row.get("CE", {})
        pe = row.get("PE", {})

        summary_docs.append({
            "symbol":            SYMBOL,
            "timestamp":         now,
            "expiry":            ce.get("expiryDate") or pe.get("expiryDate", nearest_expiry),
            "strike":            strike,
            "underlying_value":  underlying_value,

            # Call (CE)
            "ce_oi":             ce.get("openInterest", 0),
            "ce_oi_change":      ce.get("changeinOpenInterest", 0),
            "ce_volume":         ce.get("totalTradedVolume", 0),
            "ce_iv":             ce.get("impliedVolatility", 0),
            "ce_ltp":            ce.get("lastPrice", 0),

            # Put (PE)
            "pe_oi":             pe.get("openInterest", 0),
            "pe_oi_change":      pe.get("changeinOpenInterest", 0),
            "pe_volume":         pe.get("totalTradedVolume", 0),
            "pe_iv":             pe.get("impliedVolatility", 0),
            "pe_ltp":            pe.get("lastPrice", 0),

            # Derived
            "pcr_oi":            round(pe.get("openInterest", 0) / ce.get("openInterest", 1), 4)
                                  if ce.get("openInterest", 0) else 0,
        })

    if summary_docs:
        col_summary.insert_many(summary_docs)
        log.info(
            "💾  Stored %d strike rows  |  Underlying: %.2f  |  Expiry: %s",
            len(summary_docs), underlying_value, nearest_expiry,
        )


# ── Main job ────────────────────────────────────
def job():
    data = fetch_option_chain()
    if data:
        parse_and_store(data)
    else:
        log.warning("Skipping store — no data returned")


# ── Entry point ─────────────────────────────────
if __name__ == "__main__":
    log.info("🚀  NSE OI Fetcher starting  |  Symbol: %s  |  Interval: %ss", SYMBOL, FETCH_INTERVAL)
    warm_session()

    job()                                          # run immediately on start
    schedule.every(FETCH_INTERVAL).seconds.do(job)

    log.info("⏱️   Scheduler running every %s seconds. Press Ctrl+C to stop.", FETCH_INTERVAL)
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("🛑  Stopped by user.")
        client.close()
