"""
NSE Bhavcopy Dashboard - Flask API Server
Run: pip install flask flask-cors pymongo
Then: python dashboard_server.py
Dashboard opens at: http://localhost:5050
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
from pymongo import MongoClient
import pandas as pd
from datetime import datetime, timedelta
import math

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION ---
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "NSE_DAILY"
COLLECTION_NAME = "bhavcopy"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
collection = db[COLLECTION_NAME]


def clean_val(v):
    """Convert NaN/Inf to None for JSON serialization."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


@app.route("/api/dates")
def get_dates():
    """Return list of available trading dates, sorted newest first."""
    raw_dates = collection.distinct("DATE1")

    def parse_date(d):
        d = d.strip() if d else ""
        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(d, fmt)
            except ValueError:
                continue
        return datetime.min  # unparseable goes to bottom

    dated = [(d, parse_date(d)) for d in raw_dates if d and d.strip()]
    dated.sort(key=lambda x: x[1], reverse=True)
    sorted_dates = [d for d, _ in dated]
    return jsonify(sorted_dates[:90])  # last 90 trading days


@app.route("/api/debug")
def debug():
    """Show sample records to diagnose date/series format issues."""
    sample = list(collection.find({}, {"_id": 0, "SYMBOL": 1, "DATE1": 1, "SERIES": 1, "extraction_date": 1}).limit(5))
    latest_by_extract = list(collection.find({}, {"_id": 0, "DATE1": 1, "extraction_date": 1})
                             .sort("extraction_date", -1).limit(3))
    # Count distinct DATE1 values
    total_dates = len(collection.distinct("DATE1"))
    total_docs = collection.count_documents({})
    return jsonify({
        "sample_records": [{k: str(v) for k, v in r.items()} for r in sample],
        "latest_by_extraction_date": [{k: str(v) for k, v in r.items()} for r in latest_by_extract],
        "total_distinct_dates": total_dates,
        "total_documents": total_docs,
    })


@app.route("/api/summary")
def get_summary():
    """Return summary stats for a given date."""
    date = request.args.get("date")
    if not date:
        return jsonify({"error": "date required"}), 400

    pipeline = [
        {"$match": {"DATE1": date, "SERIES": {"$in": ["EQ", " EQ", "BE", " BE"]}}},
        {"$group": {
            "_id": None,
            "total_symbols": {"$sum": 1},
            "advances": {"$sum": {"$cond": [{"$gt": ["$CLOSE_PRICE", "$PREV_CLOSE"]}, 1, 0]}},
            "declines": {"$sum": {"$cond": [{"$lt": ["$CLOSE_PRICE", "$PREV_CLOSE"]}, 1, 0]}},
            "unchanged": {"$sum": {"$cond": [{"$eq": ["$CLOSE_PRICE", "$PREV_CLOSE"]}, 1, 0]}},
            "total_turnover": {"$sum": "$TURNOVER_LACS"},
            "total_volume": {"$sum": "$TTL_TRD_QNTY"},
        }}
    ]
    result = list(collection.aggregate(pipeline))
    if result:
        r = result[0]
        del r["_id"]
        return jsonify({k: clean_val(v) for k, v in r.items()})
    return jsonify({})


@app.route("/api/stocks")
def get_stocks():
    """Return stock data with computed indicators for a given date."""
    date = request.args.get("date")
    series_filter = request.args.get("series", "EQ,BE")
    min_price = float(request.args.get("min_price", 0))
    min_volume = int(request.args.get("min_volume", 0))
    min_deliv = float(request.args.get("min_deliv", 0))

    if not date:
        return jsonify({"error": "date required"}), 400

    series_list = [s.strip() for s in series_filter.split(",")]
    # Handle leading/trailing spaces in stored series values
    series_query = series_list + [" " + s for s in series_list] + [s + " " for s in series_list]

    # Match DATE1 exactly and also with trimmed variants (e.g. " 30-Mar-2026")
    date_variants = [date, date.strip(), " " + date.strip()]

    # Fetch latest date data
    today_docs = list(collection.find(
        {"DATE1": {"$in": date_variants}, "SERIES": {"$in": series_query}},
        {"_id": 0, "SYMBOL": 1, "SERIES": 1, "DATE1": 1,
         "OPEN_PRICE": 1, "HIGH_PRICE": 1, "LOW_PRICE": 1,
         "CLOSE_PRICE": 1, "PREV_CLOSE": 1, "AVG_PRICE": 1,
         "TTL_TRD_QNTY": 1, "TURNOVER_LACS": 1,
         "NO_OF_TRADES": 1, "DELIV_QTY": 1, "DELIV_PER": 1,
         "extraction_date": 1}
    ))

    if not today_docs:
        return jsonify([])

    df_today = pd.DataFrame(today_docs)
    symbols = df_today["SYMBOL"].tolist()

    # Fetch last 20 days for rolling averages
    all_recent = list(collection.find(
        {"SYMBOL": {"$in": symbols}, "SERIES": {"$in": series_query}},
        {"_id": 0, "SYMBOL": 1, "CLOSE_PRICE": 1, "TTL_TRD_QNTY": 1,
         "HIGH_PRICE": 1, "LOW_PRICE": 1, "extraction_date": 1}
    ).sort("extraction_date", -1).limit(len(symbols) * 25))

    df_hist = pd.DataFrame(all_recent) if all_recent else pd.DataFrame()

    # Compute rolling indicators
    vol_ratio_map = {}
    avg_vol_map = {}
    week_high_map = {}
    week_low_map = {}

    if not df_hist.empty:
        df_hist = df_hist.sort_values(["SYMBOL", "extraction_date"])
        for sym, grp in df_hist.groupby("SYMBOL"):
            grp = grp.tail(20)
            avg5 = grp["TTL_TRD_QNTY"].rolling(5).mean().iloc[-1] if len(grp) >= 2 else None
            avg_vol_map[sym] = avg5
            today_vol_series = df_today.loc[df_today["SYMBOL"] == sym, "TTL_TRD_QNTY"]
            if avg5 and avg5 > 0 and not today_vol_series.empty:
                vol_ratio_map[sym] = round(today_vol_series.iloc[0] / avg5, 2)
            # 52-week high/low (using available history)
            week_high_map[sym] = grp["HIGH_PRICE"].max()
            week_low_map[sym] = grp["LOW_PRICE"].min()

    # Build response
    numeric_cols = ["OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE",
                    "PREV_CLOSE", "AVG_PRICE", "TURNOVER_LACS"]
    for col in numeric_cols:
        df_today[col] = pd.to_numeric(df_today[col], errors="coerce")

    df_today["TTL_TRD_QNTY"] = pd.to_numeric(df_today["TTL_TRD_QNTY"], errors="coerce").fillna(0).astype(int)
    df_today["DELIV_PER"] = pd.to_numeric(df_today["DELIV_PER"], errors="coerce").fillna(0)
    df_today["NO_OF_TRADES"] = pd.to_numeric(df_today["NO_OF_TRADES"], errors="coerce").fillna(0).astype(int)
    df_today["SERIES"] = df_today["SERIES"].str.strip()

    # Price change
    df_today["CHG"] = (df_today["CLOSE_PRICE"] - df_today["PREV_CLOSE"]).round(2)
    df_today["CHG_PCT"] = ((df_today["CHG"] / df_today["PREV_CLOSE"]) * 100).round(2)
    df_today["VOL_RATIO"] = df_today["SYMBOL"].map(vol_ratio_map)
    df_today["PERIOD_HIGH"] = df_today["SYMBOL"].map(week_high_map)
    df_today["PERIOD_LOW"] = df_today["SYMBOL"].map(week_low_map)

    # High-Low range %
    df_today["HL_RANGE_PCT"] = (((df_today["HIGH_PRICE"] - df_today["LOW_PRICE"]) / df_today["LOW_PRICE"]) * 100).round(2)

    # Filters
    df_today = df_today[df_today["CLOSE_PRICE"] >= min_price]
    df_today = df_today[df_today["TTL_TRD_QNTY"] >= min_volume]
    df_today = df_today[df_today["DELIV_PER"] >= min_deliv]

    records = df_today.to_dict("records")
    # Clean NaN/Inf for JSON
    clean_records = []
    for rec in records:
        clean_records.append({k: clean_val(v) for k, v in rec.items()
                               if k not in ["extraction_date", "_id"]})

    return jsonify(clean_records)


@app.route("/api/chart/<symbol>")
def get_chart(symbol):
    """Return OHLCV data for sparkline/mini chart."""
    docs = list(collection.find(
        {"SYMBOL": symbol.upper(), "SERIES": {"$in": ["EQ", " EQ", "BE", " BE"]}},
        {"_id": 0, "DATE1": 1, "OPEN_PRICE": 1, "HIGH_PRICE": 1,
         "LOW_PRICE": 1, "CLOSE_PRICE": 1, "TTL_TRD_QNTY": 1, "extraction_date": 1}
    ).sort("extraction_date", -1).limit(30))

    docs = sorted(docs, key=lambda x: x.get("extraction_date", ""))
    result = []
    for d in docs:
        result.append({
            "date": d["DATE1"],
            "open": clean_val(d.get("OPEN_PRICE")),
            "high": clean_val(d.get("HIGH_PRICE")),
            "low": clean_val(d.get("LOW_PRICE")),
            "close": clean_val(d.get("CLOSE_PRICE")),
            "volume": d.get("TTL_TRD_QNTY", 0)
        })
    return jsonify(result)


@app.route("/api/top_movers")
def top_movers():
    """Return top gainers and losers for a date."""
    date = request.args.get("date")
    if not date:
        return jsonify({"error": "date required"}), 400

    docs = list(collection.find(
        {"DATE1": date, "SERIES": {"$in": ["EQ", " EQ", "BE", " BE"]},
         "PREV_CLOSE": {"$gt": 10}, "CLOSE_PRICE": {"$gt": 10}},
        {"_id": 0, "SYMBOL": 1, "CLOSE_PRICE": 1, "PREV_CLOSE": 1,
         "TTL_TRD_QNTY": 1, "TURNOVER_LACS": 1}
    ))

    if not docs:
        return jsonify({"gainers": [], "losers": [], "volume_leaders": []})

    df = pd.DataFrame(docs)
    df["CLOSE_PRICE"] = pd.to_numeric(df["CLOSE_PRICE"], errors="coerce")
    df["PREV_CLOSE"] = pd.to_numeric(df["PREV_CLOSE"], errors="coerce")
    df["TTL_TRD_QNTY"] = pd.to_numeric(df["TTL_TRD_QNTY"], errors="coerce")
    df["TURNOVER_LACS"] = pd.to_numeric(df["TURNOVER_LACS"], errors="coerce")
    df["CHG_PCT"] = ((df["CLOSE_PRICE"] - df["PREV_CLOSE"]) / df["PREV_CLOSE"] * 100).round(2)
    df = df.dropna(subset=["CHG_PCT"])

    gainers = df.nlargest(10, "CHG_PCT")[["SYMBOL", "CLOSE_PRICE", "CHG_PCT", "TURNOVER_LACS"]].to_dict("records")
    losers = df.nsmallest(10, "CHG_PCT")[["SYMBOL", "CLOSE_PRICE", "CHG_PCT", "TURNOVER_LACS"]].to_dict("records")
    vol_leaders = df.nlargest(10, "TURNOVER_LACS")[["SYMBOL", "CLOSE_PRICE", "CHG_PCT", "TURNOVER_LACS"]].to_dict("records")

    return jsonify({
        "gainers": [{k: clean_val(v) for k, v in r.items()} for r in gainers],
        "losers": [{k: clean_val(v) for k, v in r.items()} for r in losers],
        "volume_leaders": [{k: clean_val(v) for k, v in r.items()} for r in vol_leaders],
    })


if __name__ == "__main__":
    print("=" * 50)
    print("  NSE Bhavcopy Dashboard Server")
    print("  API running at: http://localhost:5050")
    print("  Open dashboard.html in your browser")
    print("=" * 50)
    app.run(debug=False, port=5050, host="0.0.0.0")