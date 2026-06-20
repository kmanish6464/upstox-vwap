"""
NSE Pre-Open Market — Auto-Download + MongoDB Sync
===================================================
Fetches pre-open data from NSE India for:
  • ALL  (Capital Market – all stocks)
  • NIFTY 50
  • EMERGE (SME Platform)
  • FO    (Futures & Options)

Upserts into MongoDB:
  Database    : nse_market
  Collections : preopen_equity  |  preopen_futures

Usage:
  python nse_preopen_sync.py                        # fetch today, sync once
  python nse_preopen_sync.py --schedule             # auto-fetch at 09:07 & 09:15 IST (weekdays)
  python nse_preopen_sync.py --date 2026-05-01      # specific date
  python nse_preopen_sync.py --from-cache           # re-process cached JSON (no download)
  python nse_preopen_sync.py --no-mongo             # CSV export only

Dependencies:
  pip install curl_cffi pymongo schedule pandas
"""

# ── Standard library ──────────────────────────────────────────────────────────
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo   # Python 3.9+

# ── Third-party (required) ────────────────────────────────────────────────────
from curl_cffi import requests  # Chrome TLS fingerprint — bypasses Akamai WAF

# ── Third-party (optional) ────────────────────────────────────────────────────
try:
    from pymongo import MongoClient, UpdateOne
    from pymongo.errors import BulkWriteError
    HAS_MONGO = True
except ImportError:
    HAS_MONGO = False

try:
    import schedule
    HAS_SCHEDULE = True
except ImportError:
    HAS_SCHEDULE = False


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
MONGO_URI      = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME        = "nse_market"
COL_EQUITY     = "preopen_equity"
COL_FUTURES    = "preopen_futures"

SCHEDULE_TIMES = ["09:07", "09:15"]    # IST 24-hr; pre-open closes ~09:08
IST            = ZoneInfo("Asia/Kolkata")
CACHE_DIR      = Path("nse_cache")

BASE_URL       = "https://www.nseindia.com"
REFERER        = f"{BASE_URL}/market-data/pre-open-market-cm-and-emerge-market"

ENDPOINTS = {
    "ALL":    f"{BASE_URL}/api/market-data-pre-open?key=ALL",
    "NIFTY":  f"{BASE_URL}/api/market-data-pre-open?key=NIFTY",
    "EMERGE": f"{BASE_URL}/api/market-data-pre-open?key=EMERGE",
    "FO":     f"{BASE_URL}/api/market-data-pre-open-fo",
}


# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("nse_sync.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# 1. SESSION  —  curl_cffi impersonates Chrome to pass Akamai WAF
# ══════════════════════════════════════════════════════════════════════════════
def build_session() -> requests.Session:
    return requests.Session(impersonate="chrome124")


def warm_cookies(session: requests.Session) -> bool:
    """
    NSE requires valid browser cookies (nsit, nseappid) before the API responds.
    Hits homepage then the pre-open page to collect them.
    """
    warmup_pages = [BASE_URL, REFERER]

    for url in warmup_pages:
        try:
            log.info(f"  Warmup → {url}")
            r = session.get(url, timeout=20)
            log.info(f"  Status {r.status_code} | cookies: {list(session.cookies.keys())}")

            if r.status_code == 403:
                log.warning("  403 — try impersonate='chrome120' or 'chrome116' in build_session()")
                return False

            time.sleep(1.5)

        except Exception as e:
            log.error(f"  Warmup error: {e}")
            return False

    expected = {"nsit", "nseappid"}
    missing  = expected - set(session.cookies.keys())
    if missing:
        log.warning(f"  Missing cookies {missing} — API may still reject requests")

    return True


# ══════════════════════════════════════════════════════════════════════════════
# 2. DOWNLOAD
# ══════════════════════════════════════════════════════════════════════════════
def fetch_endpoint(session: requests.Session, key: str, url: str) -> dict | None:
    """Fetch one NSE pre-open endpoint. Returns parsed JSON or None on error."""
    try:
        log.info(f"  Fetching [{key}] …")
        r = session.get(url, headers={"Referer": REFERER, "Accept": "application/json"}, timeout=20)
        log.info(f"  [{key}] status={r.status_code}  size={len(r.content)} B")

        if r.status_code == 200:
            return r.json()

        log.error(f"  [{key}] HTTP {r.status_code}")
        return None

    except Exception as e:
        log.error(f"  [{key}] {e}")
        return None


def download_all(trade_date: str) -> dict[str, dict]:
    """
    Download all endpoints for trade_date.
    Raw JSON is cached to  nse_cache/<date>/<key>.json
    so re-runs skip the network call.
    """
    cache_dir = CACHE_DIR / trade_date
    cache_dir.mkdir(parents=True, exist_ok=True)

    session = build_session()
    if not warm_cookies(session):
        log.error("Cookie warmup failed — aborting.")
        return {}

    results: dict[str, dict] = {}

    for key, url in ENDPOINTS.items():
        cache_file = cache_dir / f"{key}.json"

        if cache_file.exists():
            log.info(f"  [{key}] Loading from cache: {cache_file}")
            results[key] = json.loads(cache_file.read_text())
            continue

        data = fetch_endpoint(session, key, url)
        if data is None:
            continue

        cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        log.info(f"  [{key}] Cached → {cache_file}")

        results[key] = data
        time.sleep(1)

    return results


def load_from_cache(trade_date: str) -> dict[str, dict]:
    """Load previously cached JSON files without hitting the network."""
    cache_dir = CACHE_DIR / trade_date
    results: dict[str, dict] = {}

    for key in ENDPOINTS:
        fp = cache_dir / f"{key}.json"
        if fp.exists():
            results[key] = json.loads(fp.read_text())
            log.info(f"  [{key}] Loaded cache: {fp}")
        else:
            log.warning(f"  [{key}] Cache missing: {fp}")

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 3. PARSE  —  NSE JSON → flat Python dicts
# ══════════════════════════════════════════════════════════════════════════════
def _float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def parse_equity(raw: dict, trade_date: str, segment: str) -> list[dict]:
    """Parse CM / NIFTY / EMERGE response into flat documents."""
    now = datetime.now(IST).isoformat()
    ts  = raw.get("timestamp", "")
    docs = []

    for item in raw.get("data", []):
        m = item.get("metadata", {})
        symbol = str(m.get("symbol", "")).strip()
        if not symbol:
            continue

        docs.append({
            # ── Identity ──────────────────────────────────────────────────────
            "symbol":           symbol,
            "date":             trade_date,
            "market_type":      "equity",
            "market_segment":   segment,
            "nse_timestamp":    ts,
            "downloaded_at":    now,
            # ── Price ─────────────────────────────────────────────────────────
            "prev_close":       _float(m.get("pCls")),
            "iep":              _float(m.get("iep")),
            "change":           _float(m.get("chn")),
            "pct_change":       _float(m.get("perChn")),
            "final_flag":       m.get("finPriceFinalFlag", ""),
            # ── Volume / Value ────────────────────────────────────────────────
            "preopen_qty":      _float(m.get("trdQnty")),
            "preopen_value_cr": _float(m.get("iVal")),
            "total_qty":        _float(m.get("sumQnty")),
            "total_value_cr":   _float(m.get("sumVal")),
            "total_buy_qty":    _float(m.get("buyQnty")),
            "total_sell_qty":   _float(m.get("sellQnty")),
            # ── 52-Week Range ─────────────────────────────────────────────────
            "week52_high":      _float(m.get("yHigh")),
            "week52_low":       _float(m.get("yLow")),
            # ── Corporate Action ──────────────────────────────────────────────
            "ex_date":          m.get("xDt"),
            "corp_action":      m.get("caAct"),
        })

    log.info(f"  Parsed {len(docs)} equity records [{segment}]")
    return docs


def parse_futures(raw: dict, trade_date: str) -> list[dict]:
    """Parse F&O pre-open response into flat documents."""
    now = datetime.now(IST).isoformat()
    ts  = raw.get("timestamp", "")
    docs = []

    for item in raw.get("data", []):
        symbol = str(item.get("pSymbol", "")).strip()
        if not symbol:
            continue

        docs.append({
            # ── Identity ──────────────────────────────────────────────────────
            "symbol":            symbol,
            "date":              trade_date,
            "market_type":       "futures",
            "market_segment":    "FO",
            "instrument_type":   item.get("instType", ""),
            "expiry_date":       item.get("expDt", ""),
            "nse_timestamp":     ts,
            "downloaded_at":     now,
            # ── Price ─────────────────────────────────────────────────────────
            "prev_close":        _float(item.get("pCls")),
            "iep":               _float(item.get("iEP")),
            "change":            _float(item.get("chn")),
            "pct_change":        _float(item.get("perChn")),
            "final_flag":        item.get("finPriceFinalFlag", ""),
            # ── Volume / Value ────────────────────────────────────────────────
            "preopen_contracts": _float(item.get("trdQnty")),
            "preopen_value_cr":  _float(item.get("iVal")),
            "total_contracts":   _float(item.get("sumQnty")),
            "total_value_cr":    _float(item.get("sumVal")),
        })

    log.info(f"  Parsed {len(docs)} futures records [FO]")
    return docs


def parse_all(raw_data: dict[str, dict], trade_date: str) -> tuple[list[dict], list[dict]]:
    """
    Parse all endpoints.
    NIFTY/EMERGE are processed first; duplicates from ALL are skipped.
    Returns (equity_docs, futures_docs).
    """
    equity_docs  = []
    futures_docs = []
    seen_symbols: set[str] = set()

    for segment in ("NIFTY", "EMERGE", "ALL"):
        if segment not in raw_data:
            continue
        for doc in parse_equity(raw_data[segment], trade_date, segment):
            if segment == "ALL" and doc["symbol"] in seen_symbols:
                continue
            seen_symbols.add(doc["symbol"])
            equity_docs.append(doc)

    if "FO" in raw_data:
        futures_docs = parse_futures(raw_data["FO"], trade_date)

    return equity_docs, futures_docs


# ══════════════════════════════════════════════════════════════════════════════
# 4. MONGODB SYNC
# ══════════════════════════════════════════════════════════════════════════════
def _create_indexes(db) -> None:
    db[COL_EQUITY].create_index(
        [("symbol", 1), ("date", 1), ("market_segment", 1)],
        unique=True, name="sym_date_seg",
    )
    db[COL_EQUITY].create_index([("date",       1)],  name="by_date")
    db[COL_EQUITY].create_index([("pct_change", -1)], name="by_pct")
    db[COL_EQUITY].create_index([("week52_high",-1)], name="by_52h")

    db[COL_FUTURES].create_index(
        [("symbol", 1), ("date", 1), ("instrument_type", 1), ("expiry_date", 1)],
        unique=True, name="sym_date_inst_exp",
    )
    db[COL_FUTURES].create_index([("date", 1)], name="by_date")
    log.info("  Indexes ensured")


def _bulk_upsert(collection, docs: list[dict], filter_keys: list[str]) -> tuple[int, int]:
    """Upsert docs into collection using filter_keys as the unique key. Returns (inserted, modified)."""
    ops = [
        UpdateOne({k: doc[k] for k in filter_keys}, {"$set": doc}, upsert=True)
        for doc in docs
    ]
    try:
        res = collection.bulk_write(ops, ordered=False)
        return res.upserted_count, res.modified_count
    except BulkWriteError as e:
        log.error(f"  Bulk write error: {e.details}")
        return 0, 0


def sync_to_mongo(equity_docs: list[dict], futures_docs: list[dict]) -> None:
    if not HAS_MONGO:
        log.error("pymongo not installed — run: pip install pymongo")
        return

    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5_000)
        client.server_info()
    except Exception as e:
        log.error(f"MongoDB unreachable at {MONGO_URI}: {e}")
        return

    db = client[DB_NAME]
    _create_indexes(db)

    if equity_docs:
        ins, mod = _bulk_upsert(
            db[COL_EQUITY], equity_docs,
            ["symbol", "date", "market_segment"],
        )
        log.info(f"  Equity  → inserted: {ins}  updated: {mod}  total: {db[COL_EQUITY].count_documents({})}")

    if futures_docs:
        ins, mod = _bulk_upsert(
            db[COL_FUTURES], futures_docs,
            ["symbol", "date", "instrument_type", "expiry_date"],
        )
        log.info(f"  Futures → inserted: {ins}  updated: {mod}  total: {db[COL_FUTURES].count_documents({})}")

    client.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5. CSV EXPORT
# ══════════════════════════════════════════════════════════════════════════════
def save_csv(equity_docs: list[dict], futures_docs: list[dict], trade_date: str) -> None:
    try:
        import pandas as pd
    except ImportError:
        log.warning("pandas not installed — CSV export skipped")
        return

    out = CACHE_DIR / trade_date
    for label, docs in (("equity", equity_docs), ("futures", futures_docs)):
        if docs:
            fp = out / f"{label}_{trade_date}.csv"
            pd.DataFrame(docs).to_csv(fp, index=False)
            log.info(f"  CSV saved: {fp}  ({len(docs)} rows)")


# ══════════════════════════════════════════════════════════════════════════════
# 6. PIPELINE ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════
def run_sync(trade_date: str | None = None, from_cache: bool = False) -> None:
    if trade_date is None:
        trade_date = datetime.now(IST).strftime("%Y-%m-%d")

    log.info("═" * 60)
    log.info(f"  NSE Pre-Open Sync  |  date={trade_date}")
    log.info("═" * 60)

    raw_data = load_from_cache(trade_date) if from_cache else download_all(trade_date)

    if not raw_data:
        log.error("No data available — aborting.")
        return

    equity_docs, futures_docs = parse_all(raw_data, trade_date)
    log.info(f"  Parsed: {len(equity_docs)} equity  |  {len(futures_docs)} futures")

    if not equity_docs and not futures_docs:
        log.warning("Parsing produced 0 documents — check cached JSON.")
        return

    if HAS_MONGO:
        sync_to_mongo(equity_docs, futures_docs)
    else:
        log.warning("MongoDB unavailable — CSV only")

    save_csv(equity_docs, futures_docs, trade_date)
    log.info("  ✔  Sync complete")


# ══════════════════════════════════════════════════════════════════════════════
# 7. SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════
def _is_weekday() -> bool:
    return datetime.now(IST).weekday() < 5  # Mon–Fri


def start_scheduler() -> None:
    if not HAS_SCHEDULE:
        log.error("schedule not installed — run: pip install schedule")
        sys.exit(1)

    for t in SCHEDULE_TIMES:
        schedule.every().day.at(t).do(lambda: _is_weekday() and run_sync())
        log.info(f"  Scheduled at {t} IST (weekdays only)")

    log.info("  Scheduler running — press Ctrl+C to stop")
    while True:
        schedule.run_pending()
        time.sleep(30)


# ══════════════════════════════════════════════════════════════════════════════
# 8. CLI
# ══════════════════════════════════════════════════════════════════════════════
def main():
    global MONGO_URI, HAS_MONGO

    parser = argparse.ArgumentParser(
        description="NSE Pre-Open downloader + MongoDB sync",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python nse_preopen_sync.py
  python nse_preopen_sync.py --schedule
  python nse_preopen_sync.py --date 2026-04-30
  python nse_preopen_sync.py --from-cache
  python nse_preopen_sync.py --no-mongo
  python nse_preopen_sync.py --mongo mongodb://user:pass@host:27017
        """,
    )
    parser.add_argument("--schedule",   action="store_true", help="Auto-fetch at 09:07 & 09:15 IST (weekdays)")
    parser.add_argument("--date",       default=None,        help="Trade date YYYY-MM-DD (default: today IST)")
    parser.add_argument("--from-cache", action="store_true", help="Re-process cached JSON, skip download")
    parser.add_argument("--mongo",      default=MONGO_URI,   help="MongoDB URI")
    parser.add_argument("--no-mongo",   action="store_true", help="Disable MongoDB; CSV output only")

    args = parser.parse_args()
    MONGO_URI = args.mongo
    if args.no_mongo:
        HAS_MONGO = False

    if args.schedule:
        start_scheduler()
    else:
        run_sync(trade_date=args.date, from_cache=args.from_cache)


if __name__ == "__main__":
    main()