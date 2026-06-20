import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from io import StringIO
from pymongo import MongoClient

# --- CONFIGURATION ---
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "NSE_DAILY"
COLLECTION_NAME = "bhavcopy"
START_DATE = datetime(2026, 1, 1)
END_DATE = datetime.now()
OUTPUT_CSV = "5day_stocks_analysis.csv"

# ✅ Change this to any number of trading days you want (e.g. 3, 5, 7, 10, 15)
TREND_DAYS = 5

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

    print("--- Starting Optimized Sync ---")

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


# ---------------------------------------------------------------------------
# 5-DAY TREND SCORING ENGINE
# ---------------------------------------------------------------------------

def compute_volume_slope(volumes: pd.Series) -> float:
    """
    Returns the linear regression slope of volume over N days.
    Positive slope = volume is building up (bullish confirmation).
    Normalised by mean volume so it's comparable across stocks.
    """
    if volumes.isna().all() or volumes.mean() == 0:
        return 0.0
    x = np.arange(len(volumes))
    slope = np.polyfit(x, volumes.values, 1)[0]
    return round(slope / volumes.mean(), 4)  # normalised slope


def score_trend(group: pd.DataFrame) -> dict | None:
    """
    Score a stock's trend over available days in the window (min 3).
    Scoring breakdown (max 100):
      - Bullish days   : up to 40 pts  (proportional to days available)
      - Total return   : up to 20 pts  (capped at 10% gain)
      - Avg delivery % : up to 20 pts  (≥60% → full 20 pts)
      - Volume slope   : up to 20 pts  (positive slope = accumulation)
    """
    MIN_REQUIRED = max(3, TREND_DAYS - 1)
    if len(group) < MIN_REQUIRED:
        return None

    grp = group.tail(TREND_DAYS).copy()
    grp['prev_close_day'] = grp['CLOSE_PRICE'].shift(1)
    grp['day_chg']        = grp['CLOSE_PRICE'] - grp['prev_close_day']

    n_days       = len(grp)
    available_chg = grp['day_chg'].dropna()   # first row has no prev_close

    # --- Metric 1: Bullish day count (normalised to TREND_DAYS scale) ---
    bullish_days       = int((available_chg > 0).sum())
    bullish_day_score  = (bullish_days / max(n_days - 1, 1)) * 40  # max 40

    # --- Metric 2: Total return (first close → last close in window) ---
    first_close      = grp['CLOSE_PRICE'].iloc[0]
    last_close       = grp['CLOSE_PRICE'].iloc[-1]
    total_return_pct = round(((last_close - first_close) / first_close) * 100, 2) if first_close > 0 else 0
    return_score     = min(total_return_pct * 2, 20)   # 10% gain → 20 pts, capped

    # --- Metric 3: Average delivery % ---
    avg_deliv    = round(grp['DELIV_PER'].mean(), 2)
    deliv_score  = min((avg_deliv / 60) * 20, 20)      # 60% → 20 pts, capped

    # --- Metric 4: Volume accumulation slope ---
    vol_slope       = compute_volume_slope(grp['TTL_TRD_QNTY'])
    vol_slope_score = min(max(vol_slope * 200, 0), 20)

    # --- Composite Score ---
    trend_score = round(bullish_day_score + return_score + deliv_score + vol_slope_score, 1)

    avg_vol      = grp['TTL_TRD_QNTY'].mean()
    latest_vol   = grp['TTL_TRD_QNTY'].iloc[-1]
    vol_ratio    = round(latest_vol / avg_vol, 2) if avg_vol > 0 else 0

    return {
        "SYMBOL":        grp['SYMBOL'].iloc[-1],
        "DATE1":         grp['DATE1'].iloc[-1],
        "CLOSE_PRICE":   last_close,
        "SERIES":        grp['SERIES'].iloc[-1],
        "ND_RETURN_PCT": total_return_pct,          # N-day total return
        "BULLISH_DAYS":  bullish_days,              # green days in window
        "AVG_DELIV_PER": avg_deliv,
        "VOL_SLOPE":     vol_slope,
        "VOL_RATIO_LAST":vol_ratio,
        "TREND_SCORE":   trend_score,
    }


def get_last_n_trading_dates(collection, n: int) -> list:
    """
    Query MongoDB for distinct extraction_dates (actual trading days in DB),
    sort them and return the last N. This avoids any calendar-day guessing
    and is immune to holidays, weekends, or missing NSE data.
    """
    pipeline = [
        {"$group": {"_id": "$extraction_date"}},
        {"$sort": {"_id": -1}},
        {"$limit": n}
    ]
    dates = [doc["_id"] for doc in collection.aggregate(pipeline)]
    return sorted(dates)  # oldest → newest


def analyze_5day_trend():
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    print(f"\n--- Running {TREND_DAYS}-Day Bullish Trend Analysis ---")

    # ✅ Dynamically resolve the last N actual trading dates from DB
    trading_dates = get_last_n_trading_dates(collection, TREND_DAYS)

    if len(trading_dates) < TREND_DAYS:
        print(f"❌ Only {len(trading_dates)} trading date(s) in DB. Need {TREND_DAYS}. "
              f"Run the sync first or reduce TREND_DAYS.")
        return

    lookback_start = trading_dates[0]   # oldest of the N dates
    latest_dt      = trading_dates[-1]  # most recent trading date

    projection = {
        "SYMBOL": 1, "SERIES": 1, "DATE1": 1,
        "CLOSE_PRICE": 1, "TTL_TRD_QNTY": 1,
        "DELIV_PER": 1, "extraction_date": 1
    }
    # ✅ Fetch ALL series from DB — filter in pandas after stripping whitespace
    #    (avoids mismatches from spaces/casing stored in MongoDB)
    data = list(collection.find(
        {"extraction_date": {"$in": trading_dates}},
        projection
    ))

    if not data:
        print("No data found for the selected trading dates.")
        return

    df = pd.DataFrame(data)
    df['CLOSE_PRICE']  = pd.to_numeric(df['CLOSE_PRICE'], errors='coerce')
    df['TTL_TRD_QNTY'] = pd.to_numeric(df['TTL_TRD_QNTY'], errors='coerce')
    df['DELIV_PER']    = pd.to_numeric(df['DELIV_PER'], errors='coerce').fillna(0)
    df['SERIES']       = df['SERIES'].str.strip().str.upper()   # normalise here
    df['SYMBOL']       = df['SYMBOL'].str.strip().str.upper()
    df = df.dropna(subset=['CLOSE_PRICE', 'TTL_TRD_QNTY'])

    # Show what series exist in DB before filtering (useful for debugging)
    print(f"Series in DB           : {sorted(df['SERIES'].unique())}")

    # Filter to equity series only (after normalisation)
    df = df[df['SERIES'].isin(['EQ', 'BE'])]
    df = df.sort_values(by=['SYMBOL', 'extraction_date'])

    print(f"Trend window ({TREND_DAYS} days) : {[d.date() for d in trading_dates]}")
    print(f"Unique EQ/BE symbols   : {df['SYMBOL'].nunique()}")

    # ✅ Allow stocks present on at least MIN_DAYS out of TREND_DAYS
    #    (handles NSE holidays / circuit-locked stocks with no trades)
    MIN_DAYS = max(3, TREND_DAYS - 1)   # e.g. 5-day window → need at least 4 days
    symbol_day_counts = df.groupby('SYMBOL')['extraction_date'].nunique()
    complete_symbols  = symbol_day_counts[symbol_day_counts >= MIN_DAYS].index
    df_complete       = df[df['SYMBOL'].isin(complete_symbols)]

    print(f"Min days required      : {MIN_DAYS} of {TREND_DAYS}")
    print(f"Symbols qualifying     : {len(complete_symbols)}\n")

    # --- Score each symbol ---
    results = []
    for symbol, group in df_complete.groupby('SYMBOL'):
        result = score_trend(group.sort_values('extraction_date'))
        if result:
            results.append(result)

    if not results:
        print("No stocks qualified for scoring.")
        return

    results_df = pd.DataFrame(results)

    # -----------------------------------------------------------------
    # STRICT FILTER
    #   - Bullish on majority of available days
    #   - Positive total return
    #   - Avg delivery ≥ 40%
    #   - Volume accumulating (slope ≥ 0)
    # -----------------------------------------------------------------
    majority = max(2, (TREND_DAYS // 2))   # e.g. TREND_DAYS=5 → need ≥ 3 green days
    strict = results_df[
        (results_df['BULLISH_DAYS']  >= majority) &
        (results_df['ND_RETURN_PCT'] >  0) &
        (results_df['AVG_DELIV_PER'] >= 40) &
        (results_df['VOL_SLOPE']     >= 0)
    ].copy()

    if strict.empty:
        print(f"No stocks met strict criteria (bull_days≥{majority}, deliv≥40%, vol_slope≥0).")
        print("Applying relaxed filter (bull_days≥2, deliv≥25%, return>0)...")
        strict = results_df[
            (results_df['BULLISH_DAYS']  >= 2) &
            (results_df['ND_RETURN_PCT'] >  0) &
            (results_df['AVG_DELIV_PER'] >= 25)
        ].copy()

    ret_col = f"{TREND_DAYS}D_RET%"
    strict = strict.rename(columns={"ND_RETURN_PCT": ret_col})

    output_df = strict[[
        'SYMBOL', 'DATE1', 'SERIES', 'CLOSE_PRICE',
        ret_col, 'BULLISH_DAYS', 'AVG_DELIV_PER',
        'VOL_SLOPE', 'VOL_RATIO_LAST', 'TREND_SCORE'
    ]].sort_values('TREND_SCORE', ascending=False).reset_index(drop=True)

    output_df.to_csv(OUTPUT_CSV, index=False)

    # --- Console Summary ---
    W = 95
    print(f"{'='*W}")
    print(f"  {TREND_DAYS}-DAY BULLISH TREND RESULTS  |  {len(output_df)} stocks  |  Saved → {OUTPUT_CSV}")
    print(f"{'='*W}")
    print(f"{'#':<4} {'SYMBOL':<18} {'CLOSE':>8} {ret_col:>9} {'BULL_DAYS':>10} "
          f"{'DELIV%':>8} {'VOL_SLOPE':>10} {'SCORE':>7}")
    print(f"{'-'*W}")
    for i, row in output_df.head(25).iterrows():
        print(
            f"{i+1:<4} {row['SYMBOL']:<18} {row['CLOSE_PRICE']:>8.2f} "
            f"{row[ret_col]:>9.2f} {row['BULLISH_DAYS']:>10} "
            f"{row['AVG_DELIV_PER']:>8.2f} {row['VOL_SLOPE']:>10.4f} "
            f"{row['TREND_SCORE']:>7.1f}"
        )
    print(f"{'='*W}")
    print(f"\nColumn Guide:")
    print(f"  {ret_col:<12}: Total price return over last {TREND_DAYS} sessions")
    print(f"  BULL_DAYS   : Green-close days in the window (out of {TREND_DAYS})")
    print(f"  DELIV%      : Avg delivery % over {TREND_DAYS} days — high = institutional buying")
    print(f"  VOL_SLOPE   : Normalised volume trend (positive = accumulation)")
    print(f"  SCORE       : Composite 0-100 trend score (higher = stronger uptrend)")

if __name__ == "__main__":
    download_and_sync_to_mongo()
    analyze_5day_trend()
