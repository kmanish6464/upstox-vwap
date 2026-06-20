import pandas as pd
import numpy as np
from pymongo import MongoClient
from datetime import datetime, timedelta
import os

# --- CONFIGURATION ---
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "NSE_DAILY"
COLLECTION = "bhavcopy"  # Uses your existing collection
OUTPUT_FILE = "EMA_Breakout_Focus.csv"
SUMMARY_FILE = "EMA_Breakout_Summary.csv"

def load_clean_data():
    """Loads data from Mongo and fixes duplicate column issues."""
    print("📥 Connecting to MongoDB and fetching data...")
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    col = db[COLLECTION]
    
    # We load enough history for a 200 EMA calculation
    # Adjust the filter if you only want recent months to speed up loading
    data = list(col.find({}, {
        "_id": 0, "SYMBOL": 1, "SERIES": 1, "CLOSE_PRICE": 1, 
        "TTL_TRD_QNTY": 1, "DELIV_PER": 1, "extraction_date": 1
    }))
    
    df = pd.DataFrame(data)
    
    # Fix Column Names and Duplicates
    df.columns = [c.upper().strip() for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated()] # Removes duplicate columns to prevent TypeErrors
    
    # Rename for consistency
    rename_map = {
        "CLOSE_PRICE": "CLOSE",
        "TTL_TRD_QNTY": "VOLUME",
        "DELIV_PER": "DELIVERY",
        "EXTRACTION_DATE": "DATE"
    }
    df.rename(columns=rename_map, inplace=True)

    # Convert Types
    for col_name in ["CLOSE", "VOLUME", "DELIVERY"]:
        if col_name in df.columns:
            df[col_name] = pd.to_numeric(df[col_name], errors='coerce').fillna(0)
    
    df['DATE'] = pd.to_datetime(df['DATE'])
    
    # Filter only Equity
    if "SERIES" in df.columns:
        df = df[df["SERIES"] == "EQ"]
        
    return df.sort_values(['SYMBOL', 'DATE'])

def calculate_ema_breakouts(df):
    """Calculates EMAs and filters for breakouts."""
    print("📈 Calculating EMAs and searching for breakouts...")
    
    # Calculate Indicators per Symbol
    groups = df.groupby('SYMBOL')
    df['EMA_50']  = groups['CLOSE'].transform(lambda x: x.ewm(span=50, adjust=False).mean())
    df['EMA_100'] = groups['CLOSE'].transform(lambda x: x.ewm(span=100, adjust=False).mean())
    df['EMA_200'] = groups['CLOSE'].transform(lambda x: x.ewm(span=200, adjust=False).mean())
    
    # Volume Average
    df['VOL_AVG_5D'] = groups['VOLUME'].transform(lambda x: x.rolling(window=5).mean())
    
    # Previous day values for breakout detection
    df['PREV_CLOSE'] = groups['CLOSE'].shift(1)
    
    # Latest Data Only
    latest_date = df['DATE'].max()
    latest_df = df[df['DATE'] == latest_date].copy()
    
    # --- SCANNING CRITERIA ---
    # 1. Price above all 3 EMAs (Long term bullish)
    cond_above_emas = (latest_df['CLOSE'] > latest_df['EMA_50']) & \
                      (latest_df['CLOSE'] > latest_df['EMA_100']) & \
                      (latest_df['CLOSE'] > latest_df['EMA_200'])
    
    # 2. Today is a "Breakout" (Price crossed above at least one EMA today)
    cond_break_50  = (latest_df['PREV_CLOSE'] <= latest_df['EMA_50'])  & (latest_df['CLOSE'] > latest_df['EMA_50'])
    cond_break_100 = (latest_df['PREV_CLOSE'] <= latest_df['EMA_100']) & (latest_df['CLOSE'] > latest_df['EMA_100'])
    cond_break_200 = (latest_df['PREV_CLOSE'] <= latest_df['EMA_200']) & (latest_df['CLOSE'] > latest_df['EMA_200'])
    
    # 3. Volume Surge (> 20% above 5d avg) and Delivery (> 25%)
    cond_vol = latest_df['VOLUME'] > (latest_df['VOL_AVG_5D'] * 1.2)
    cond_del = latest_df['DELIVERY'] > 25
    
    # Apply Filters
    results = latest_df[cond_above_emas & (cond_break_50 | cond_break_100 | cond_break_200) & cond_vol & cond_del].copy()
    
    # Label the type of breakout
    def get_break_label(row):
        breaks = []
        if row['PREV_CLOSE'] <= row['EMA_50'] < row['CLOSE']: breaks.append("EMA50")
        if row['PREV_CLOSE'] <= row['EMA_100'] < row['CLOSE']: breaks.append("EMA100")
        if row['PREV_CLOSE'] <= row['EMA_200'] < row['CLOSE']: breaks.append("EMA200")
        return ", ".join(breaks)

    if not results.empty:
        results['BREAK_TYPE'] = results.apply(get_break_label, axis=1)
        results['VOL_RATIO'] = (results['VOLUME'] / results['VOL_AVG_5D']).round(2)
        
        # Select and reorder columns
        final_cols = ['DATE', 'SYMBOL', 'CLOSE', 'EMA_50', 'EMA_100', 'EMA_200', 'BREAK_TYPE', 'VOL_RATIO', 'DELIVERY']
        return results[final_cols].sort_values('VOL_RATIO', ascending=False)
    
    return pd.DataFrame()

if __name__ == "__main__":
    try:
        master_data = load_clean_data()
        final_picks = calculate_ema_breakouts(master_data)
        
        if not final_picks.empty:
            # 1. Save main breakout file
            final_picks.to_csv(OUTPUT_FILE, index=False)
            
            # 2. Save a simpler summary report
            summary = final_picks[['SYMBOL', 'CLOSE', 'BREAK_TYPE', 'VOL_RATIO']].copy()
            summary.to_csv(SUMMARY_FILE, index=False)
            
            print(f"\n✅ Success! Found {len(final_picks)} stocks breaking major EMAs.")
            print(f"📁 Files saved: {OUTPUT_FILE} and {SUMMARY_FILE}")
            
            # Show top 10 on console
            print("\n--- TOP EMA BREAKOUTS ---")
            print(final_picks.head(10).to_string(index=False))
        else:
            print("\nℹ️ No stocks met the EMA breakout criteria today.")
            
    except Exception as e:
        print(f"\n❌ Script failed: {e}")