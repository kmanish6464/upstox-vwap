"""
NSE Bhavcopy — Breakout Stock Finder
Syncs daily NSE bhavcopy data to MongoDB and scores stocks
for next-day breakout potential using technical indicators.
"""

import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from io import StringIO
from pymongo import MongoClient
from pymongo.errors import BulkWriteError

# ─── Configuration ────────────────────────────────────────────────────────────

MONGO_URI       = "mongodb://localhost:27017/"
DB_NAME         = "NSE_DAILY"
COLLECTION_NAME = "bhavcopy"
START_DATE      = datetime(2026, 1, 1)
END_DATE        = datetime.now()
OUTPUT_CSV      = "breakout_stocks_analysis.csv"

NSE_HEADERS = {
    "User-Agent"     : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer"        : "https://www.nseindia.com/all-reports",
}

NUMERIC_COLS = ["CLOSE_PRICE", "HIGH_PRICE", "LOW_PRICE", "OPEN_PRICE", "PREV_CLOSE", "TURNOVER_LACS"]

# ─── MongoDB Helpers ──────────────────────────────────────────────────────────

def get_collection():
    client = MongoClient(MONGO_URI)
    return client[DB_NAME][COLLECTION_NAME]


def setup_indexes(collection):
    """
    Ensures a unique compound index on (SYMBOL, DATE1).
    Deduplicates existing data first if the index cannot be built cleanly.
    """
    # Step 1 — Remove duplicate documents (keep the first _id per pair)
    pipeline = [
        {"$group": {"_id": {"SYMBOL": "$SYMBOL", "DATE1": "$DATE1"},
                    "count": {"$sum": 1}, "ids": {"$push": "$_id"}}},
        {"$match": {"count": {"$gt": 1}}},
    ]
    duplicates = list(collection.aggregate(pipeline, allowDiskUse=True))
    if duplicates:
        ids_to_delete = [oid for doc in duplicates for oid in doc["ids"][1:]]
        # Delete in batches of 500 to stay well under MongoDB's 16 MB BSON limit
        BATCH_SIZE = 500
        total_deleted = 0
        for i in range(0, len(ids_to_delete), BATCH_SIZE):
            batch = ids_to_delete[i : i + BATCH_SIZE]
            total_deleted += collection.delete_many({"_id": {"$in": batch}}).deleted_count
        print(f"[setup] Removed {total_deleted} duplicate records.")

    # Step 2 — Drop old non-unique index if present, then recreate as unique
    try:
        collection.drop_index("SYMBOL_1_DATE1_1")
    except Exception:
        pass

    collection.create_index([("SYMBOL", 1), ("DATE1", 1)], unique=True)
    collection.create_index("extraction_date")
    print("[setup] Indexes ready.")


# ─── Download & Sync ──────────────────────────────────────────────────────────

def _get_nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception:
        pass
    return session


def _build_record(row: pd.Series, extraction_date: datetime) -> dict:
    return {
        "SYMBOL"        : str(row.get("SYMBOL", "")).strip(),
        "SERIES"        : str(row.get("SERIES", "")).strip(),
        "DATE1"         : str(row.get("DATE1", "")).strip(),
        "PREV_CLOSE"    : float(row.get("PREV_CLOSE", 0)),
        "OPEN_PRICE"    : float(row.get("OPEN_PRICE", 0)),
        "HIGH_PRICE"    : float(row.get("HIGH_PRICE", 0)),
        "LOW_PRICE"     : float(row.get("LOW_PRICE", 0)),
        "LAST_PRICE"    : float(row.get("LAST_PRICE", 0)),
        "CLOSE_PRICE"   : float(row.get("CLOSE_PRICE", 0)),
        "AVG_PRICE"     : float(row.get("AVG_PRICE", 0)),
        "TTL_TRD_QNTY"  : int(row.get("TTL_TRD_QNTY", 0)),
        "TURNOVER_LACS" : float(row.get("TURNOVER_LACS", 0)),
        "NO_OF_TRADES"  : int(row.get("NO_OF_TRADES", 0)),
        "DELIV_QTY"     : str(row.get("DELIV_QTY", "0")).strip(),
        "DELIV_PER"     : str(row.get("DELIV_PER", "0")).strip(),
        "extraction_date": extraction_date,
    }


def download_and_sync_to_mongo():
    """Downloads NSE Bhavcopy CSVs day-by-day and upserts records into MongoDB."""
    collection = get_collection()
    setup_indexes(collection)

    session = get_session()
    current_date = START_DATE
    print("─── Starting Sync ───────────────────────────────────────────────────────")

    while current_date <= END_DATE:
        if current_date.weekday() >= 5:          # skip weekends
            current_date += timedelta(days=1)
            continue

        date_str = current_date.strftime("%d%m%Y")
        url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv"

        try:
            if collection.count_documents({"extraction_date": current_date}) > 1000:
                print(f"  [{current_date.date()}] Already synced — skipping.")
                current_date += timedelta(days=1)
                continue

            response = session.get(url, timeout=15)

            if response.status_code == 404:
                print(f"  [{current_date.date()}] Holiday / data not found.")
            elif response.status_code == 200:
                df = pd.read_csv(StringIO(response.text))
                df.columns = [c.strip().upper() for c in df.columns]

                date_in_csv = df["DATE1"].iloc[0] if not df.empty else ""
                existing    = set(collection.distinct("SYMBOL", {"DATE1": date_in_csv}))

                records = [
                    _build_record(row, current_date)
                    for _, row in df.iterrows()
                    if str(row.get("SYMBOL", "")).strip() not in existing
                ]

                if records:
                    try:
                        collection.insert_many(records, ordered=False)
                    except BulkWriteError as bwe:
                        # Ignore duplicate-key errors from race conditions
                        inserted = bwe.details.get("nInserted", 0)
                        print(f"  [{current_date.date()}] Partial insert: {inserted} added (rest were duplicates).")
                    else:
                        print(f"  [{current_date.date()}] ✓ {len(records)} records added.")
                else:
                    print(f"  [{current_date.date()}] Nothing new to add.")

        except Exception as exc:
            print(f"  [{current_date.date()}] Error: {exc}")

        current_date += timedelta(days=1)
        time.sleep(0.1)

    print("─── Sync complete ───────────────────────────────────────────────────────\n")


# ─── Technical Indicators ─────────────────────────────────────────────────────

def _compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta    = close.diff()
    avg_gain = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    avg_loss = (-delta.clip(upper=0)).ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _compute_atr(grp: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = grp["CLOSE_PRICE"].shift(1)
    tr = pd.concat([
        grp["HIGH_PRICE"] - grp["LOW_PRICE"],
        (grp["HIGH_PRICE"] - prev_close).abs(),
        (grp["LOW_PRICE"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


def _compute_obv(close: pd.Series, vol: pd.Series) -> pd.Series:
    direction = np.where(close.diff() > 0, vol, np.where(close.diff() < 0, -vol, 0))
    return pd.Series(direction, index=close.index).cumsum()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Adds all technical indicator columns, grouped per symbol."""
    groups = []
    for _, grp in df.groupby("SYMBOL"):
        grp   = grp.sort_values("extraction_date").copy()
        close = grp["CLOSE_PRICE"]
        vol   = grp["TTL_TRD_QNTY"]

        grp["MA20"]                = close.rolling(20).mean()
        grp["MA50"]                = close.rolling(50).mean()
        grp["EMA9"]                = close.ewm(span=9, adjust=False).mean()
        grp["avg_vol_5d"]          = vol.rolling(5).mean()
        grp["vol_ratio_5d"]        = vol / grp["avg_vol_5d"]
        grp["RSI"]                 = _compute_rsi(close)
        grp["ATR14"]               = _compute_atr(grp)
        grp["high_52w"]            = grp["HIGH_PRICE"].rolling(252, min_periods=20).max()
        grp["consolidation_high"]  = grp["HIGH_PRICE"].rolling(20).max().shift(1)
        obv                        = _compute_obv(close, vol)
        grp["OBV"]                 = obv
        grp["OBV_MA20"]            = obv.rolling(20).mean()
        grp["prev_close"]          = close.shift(1)

        groups.append(grp)

    return pd.concat(groups).reset_index(drop=True)


# ─── Breakout Scoring ─────────────────────────────────────────────────────────

def score_breakout(row) -> tuple[float, list[str]]:
    """Returns (score, reasons). Higher score = stronger breakout signal."""
    score, reasons = 0.0, []

    close       = row["CLOSE_PRICE"]
    vol_ratio   = row.get("vol_ratio_5d", np.nan)
    deliv       = row.get("DELIV_PER", 0)
    rsi         = row.get("RSI", np.nan)
    ma20        = row.get("MA20", np.nan)
    ma50        = row.get("MA50", np.nan)
    high_52w    = row.get("high_52w", np.nan)
    consol_high = row.get("consolidation_high", np.nan)
    obv         = row.get("OBV", np.nan)
    obv_ma      = row.get("OBV_MA20", np.nan)
    chg_pct     = row.get("PRICE_CHG_PCT", 0)

    # 1. Breakout above 20-day consolidation range
    if pd.notna(consol_high) and close > consol_high:
        score += 3.0; reasons.append("✅ Broke 20d consolidation high")

    # 2. Near / at 52-week high
    if pd.notna(high_52w) and close >= high_52w * 0.97:
        score += 2.5; reasons.append("🚀 Near / at 52-week high")

    # 3. Volume surge
    if pd.notna(vol_ratio):
        if   vol_ratio >= 3.0: score += 3.0; reasons.append(f"📈 Volume {vol_ratio:.1f}x (strong)")
        elif vol_ratio >= 2.0: score += 2.0; reasons.append(f"📈 Volume {vol_ratio:.1f}x (moderate)")
        elif vol_ratio >= 1.5: score += 1.0; reasons.append(f"📈 Volume {vol_ratio:.1f}x (mild)")

    # 4. Delivery percentage
    if   deliv >= 70: score += 2.5; reasons.append(f"💼 Delivery {deliv:.1f}% — conviction buying")
    elif deliv >= 50: score += 1.5; reasons.append(f"💼 Delivery {deliv:.1f}% — solid")

    # 5. RSI zone
    if pd.notna(rsi):
        if   55 <= rsi <= 70: score += 2.0; reasons.append(f"⚡ RSI {rsi:.1f} — bullish momentum")
        elif 40 <= rsi <  55: score += 1.0; reasons.append(f"⚡ RSI {rsi:.1f} — building up")
        elif rsi > 70:        score -= 0.5; reasons.append(f"⚠️  RSI {rsi:.1f} — overbought, watch")

    # 6. Price vs moving averages
    above_ma20 = pd.notna(ma20) and close > ma20
    above_ma50 = pd.notna(ma50) and close > ma50
    if above_ma20 and above_ma50:
        score += 2.0; reasons.append("📊 Above MA20 & MA50 — uptrend confirmed")
    elif above_ma20:
        score += 1.0; reasons.append("📊 Above MA20")

    if pd.notna(ma20) and pd.notna(ma50) and ma20 > ma50:
        score += 1.0; reasons.append("✨ MA20 > MA50 — bullish alignment")

    if pd.notna(ma20) and pd.notna(ma50) and close < ma20 and close < ma50:
        score -= 2.0; reasons.append("❌ Below MA20 & MA50 — weak trend")

    # 7. OBV accumulation
    if pd.notna(obv) and pd.notna(obv_ma) and obv > obv_ma:
        score += 1.5; reasons.append("💰 OBV above MA — institutional accumulation")

    # 8. Today's price move
    if   chg_pct >= 5: score += 1.5; reasons.append(f"🔥 Strong day: +{chg_pct:.1f}%")
    elif chg_pct >= 2: score += 0.5; reasons.append(f"🔥 Good day: +{chg_pct:.1f}%")

    return round(score, 2), reasons


def _classify_tier(score: float) -> str:
    if   score >= 12: return "🔥 STRONG BREAKOUT"
    elif score >= 8:  return "✅ PROBABLE BREAKOUT"
    elif score >= 5:  return "👀 WATCH"
    return "—"


# ─── Main Analysis ────────────────────────────────────────────────────────────

def analyze_breakout_stocks():
    collection = get_collection()
    print("─── Running Breakout Analysis ───────────────────────────────────────────")

    projection = {
        "SYMBOL": 1, "SERIES": 1, "DATE1": 1,
        "CLOSE_PRICE": 1, "HIGH_PRICE": 1, "LOW_PRICE": 1,
        "OPEN_PRICE": 1, "PREV_CLOSE": 1,
        "TTL_TRD_QNTY": 1, "DELIV_PER": 1,
        "TURNOVER_LACS": 1, "extraction_date": 1,
    }
    df = pd.DataFrame(collection.find({}, projection))

    if df.empty:
        print("No data found in MongoDB."); return

    # Coerce types
    for col in NUMERIC_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    df["TTL_TRD_QNTY"] = pd.to_numeric(df["TTL_TRD_QNTY"], errors="coerce").fillna(0)
    df["DELIV_PER"]    = pd.to_numeric(df["DELIV_PER"],    errors="coerce").fillna(0)
    df["SERIES"]       = df["SERIES"].str.strip()

    # EQ / BE series only, sorted for rolling calculations
    df = (df[df["SERIES"].isin(["EQ", "BE"])]
            .sort_values(["SYMBOL", "extraction_date"])
            .copy())

    print("Computing indicators...")
    df = compute_indicators(df)

    latest_date = df["extraction_date"].max()
    print(f"Scoring for: {latest_date.date()}")

    today = df[df["extraction_date"] == latest_date].copy()
    if today.empty:
        print("No data for latest date."); return

    today["PRICE_CHG_PCT"] = (
        (today["CLOSE_PRICE"] - today["prev_close"]) / today["prev_close"] * 100
    ).round(2)

    # Drop illiquid stocks
    today = today[today["TURNOVER_LACS"] >= 10].copy()

    # Score and classify
    today[["BREAKOUT_SCORE", "REASONS"]] = today.apply(
        lambda r: pd.Series(score_breakout(r)), axis=1
    )
    today["TIER"] = today["BREAKOUT_SCORE"].apply(_classify_tier)

    candidates = (today[today["BREAKOUT_SCORE"] >= 5]
                  .sort_values("BREAKOUT_SCORE", ascending=False)
                  .copy())

    candidates["STOP_LOSS"] = (candidates["CLOSE_PRICE"] - 1.5 * candidates["ATR14"]).round(2)
    candidates["RISK_PCT"]  = (
        (candidates["CLOSE_PRICE"] - candidates["STOP_LOSS"]) / candidates["CLOSE_PRICE"] * 100
    ).round(2)

    # ── Output columns ───────────────────────────────────────────────────────
    out = candidates[[
        "SYMBOL", "DATE1", "TIER", "BREAKOUT_SCORE",
        "CLOSE_PRICE", "PRICE_CHG_PCT", "DELIV_PER",
        "vol_ratio_5d", "RSI", "MA20", "MA50", "high_52w",
        "STOP_LOSS", "RISK_PCT", "REASONS",
    ]].rename(columns={"vol_ratio_5d": "VOL_RATIO"})

    for col in ["VOL_RATIO", "MA20", "MA50", "high_52w"]:
        out[col] = out[col].round(2)
    out["RSI"] = out["RSI"].round(1)

    # Save CSV
    save = out.copy()
    save["REASONS"] = save["REASONS"].apply(" | ".join)
    save.to_csv(OUTPUT_CSV, index=False)

    # ── Print results ─────────────────────────────────────────────────────────
    print(f"\n{'═'*82}")
    print(f"  BREAKOUT CANDIDATES  —  {latest_date.date()}")
    print(f"{'═'*82}\n")

    for _, row in out.head(30).iterrows():
        print(
            f"  [{row['TIER']}]  {row['SYMBOL']:<14} "
            f"Score:{row['BREAKOUT_SCORE']:>5}  "
            f"₹{row['CLOSE_PRICE']:>9.2f}  {row['PRICE_CHG_PCT']:>+6.2f}%  "
            f"RSI:{row['RSI']:>5.1f}  Deliv:{row['DELIV_PER']:>5.1f}%  "
            f"Vol:{row['VOL_RATIO']:>4.2f}x  SL:₹{row['STOP_LOSS']:>9.2f}"
        )
        for reason in row["REASONS"]:
            print(f"      {reason}")
        print()

    print(f"  Total: {len(out)} candidates  —  saved to {OUTPUT_CSV}")
    print("\n  Summary by tier:")
    for label, count in out["TIER"].value_counts().items():
        print(f"    {label}: {count}")
    print()


# ─── Entry Point ──────────────────────────────────────────────────────────────

def get_session():
    """Alias kept for backward compatibility."""
    return _get_nse_session()


if __name__ == "__main__":
    download_and_sync_to_mongo()
    analyze_breakout_stocks()