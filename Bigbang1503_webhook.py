"""
================================================================
  VWAP + EMA (7/21/100/200) STRICT Breakout Scanner
  ================================
  Trigger : Volume > VOL_SURGE_PCT % above prior N-bar average
  Filter  : ONLY reports if Close > EMA100/200 AND > W/M VWAP (Long)
            or Close < EMA100/200 AND < W/M VWAP (Short)
================================================================
"""

import sys, os, io, time, csv, configparser, warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

import pandas as pd
import requests

# ================================================================
# SETTINGS
# ================================================================
SCAN_TF_MIN = 30
VOL_SURGE_PCT = 200
VOL_AVG_BARS = 20
LOOKBACK_DAYS = 45
TARGET_DATE = "today"
SESSION_START = "09:15"
SESSION_END = "15:30"
DELAY_SEC = 0.3

if len(sys.argv) >= 2:
    try:
        SCAN_TF_MIN = int(sys.argv[1])
    except:
        pass
if len(sys.argv) >= 3:
    try:
        VOL_SURGE_PCT = float(sys.argv[2])
    except:
        pass
if len(sys.argv) >= 4:
    TARGET_DATE = sys.argv[3]

SCAN_TF_MIN = max(1, min(SCAN_TF_MIN, 188))
TF_STR = f"{SCAN_TF_MIN}min"

# ================================================================
# PATHS & CONFIG
# ================================================================
BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, "config.ini")
TOKEN_FILE = os.path.join(BASE, "token.txt")
NSE_CSV = os.path.join(BASE, "nse.csv")
OUT_CSV = os.path.join(BASE, "signals.csv")
MASTER_CACHE = os.path.join(BASE, "upstox_master.pkl")

cfg = configparser.ConfigParser()
if os.path.exists(CONFIG_FILE):
    cfg.read(CONFIG_FILE)

BOT_TOKEN = cfg.get("TELEGRAM", "bot_token", fallback="")
CHANNEL_ID = cfg.get("TELEGRAM", "channel_id", fallback="")
TG_ON = cfg.getboolean("TELEGRAM", "enable_telegram", fallback=False)


def read_token() -> str:
    for p in [TOKEN_FILE, os.path.join(BASE, "..", "token.txt")]:
        if os.path.exists(p):
            t = open(p).read().strip()
            if t: return t
    print("[ERROR] token.txt not found");
    sys.exit(1)


TOKEN = read_token()
HEADERS = {"Accept": "application/json", "Authorization": f"Bearer {TOKEN}"}


def tg(msg: str):
    if not TG_ON or not BOT_TOKEN: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHANNEL_ID, "text": msg, "parse_mode": "HTML"},
            timeout=6,
        )
    except Exception:
        pass


# ================================================================
# INSTRUMENT KEY LOOKUP
# ================================================================
_master_map: dict = {}


def get_master_map() -> dict:
    global _master_map
    if _master_map: return _master_map
    if os.path.exists(MASTER_CACHE):
        try:
            cached = pd.read_pickle(MASTER_CACHE)
            if cached.get("date") == datetime.now().strftime("%Y-%m-%d"):
                _master_map = cached["map"]
                return _master_map
        except Exception:
            pass

    print("  Downloading Upstox instrument master...", end="", flush=True)
    url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
    r = requests.get(url, timeout=30)
    df = pd.read_json(io.BytesIO(r.content), compression="gzip")
    nse = df[(df["exchange"] == "NSE") & (df["instrument_type"].isin(["EQ", "EQUITY", "BE"]))].copy()
    _master_map = dict(zip(nse["trading_symbol"].str.upper().str.strip(), nse["instrument_key"]))
    try:
        pd.to_pickle({"date": datetime.now().strftime("%Y-%m-%d"), "map": _master_map}, MASTER_CACHE)
    except Exception:
        pass
    print(f" OK ({len(_master_map):,} equities)")
    return _master_map


def resolve_key(symbol: str, stored_key: str) -> str | None:
    m = get_master_map()
    ikey = m.get(symbol.upper().strip())
    if ikey: return ikey
    if stored_key: return stored_key
    return None


# ================================================================
# FETCH CANDLES & RESAMPLE
# ================================================================
COLS = ["timestamp", "open", "high", "low", "close", "volume", "oi"]


def fetch_candles(ikey: str, from_date: str, to_date: str) -> pd.DataFrame | None:
    chunks = []
    cur = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")
    while cur <= end:
        chunk_end = min(cur + timedelta(days=29), end)
        url = (f"https://api.upstox.com/v2/historical-candle/{ikey}/1minute/"
               f"{chunk_end.strftime('%Y-%m-%d')}/{cur.strftime('%Y-%m-%d')}")
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                data = r.json().get("data", {}).get("candles", [])
                if data: chunks.append(pd.DataFrame(data, columns=COLS))
        except Exception:
            pass
        cur = chunk_end + timedelta(days=1)
        time.sleep(DELAY_SEC)
    if not chunks: return None
    df = pd.concat(chunks, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata")
    df.sort_values("timestamp", inplace=True)
    df.drop_duplicates("timestamp", inplace=True)
    return df.set_index("timestamp").between_time(SESSION_START, SESSION_END).reset_index()


def resample(df1m: pd.DataFrame) -> pd.DataFrame:
    df = df1m.set_index("timestamp")
    origin = df.index[0].normalize().replace(hour=9, minute=15)
    return (df.resample(TF_STR, label="left", closed="left", origin=origin)
            .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "oi": "last"})
            .dropna(subset=["close"]).reset_index())


# ================================================================
# INDICATORS
# ================================================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["ema7"] = df["close"].ewm(span=7, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema100"] = df["close"].ewm(span=100, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["vol_avg"] = df["volume"].shift(1).rolling(VOL_AVG_BARS, min_periods=5).mean()
    df["date"] = df["timestamp"].dt.date
    df["tpv"] = tp * df["volume"]
    df["vwap_d"] = df.groupby("date")["tpv"].cumsum() / df.groupby("date")["volume"].cumsum()
    df["wk"] = df["timestamp"].dt.year.astype(str) + "_" + df["timestamp"].dt.isocalendar().week.astype(int).astype(str)
    df["vwap_w"] = df.groupby("wk")["tpv"].cumsum() / df.groupby("wk")["volume"].cumsum()
    df["mo"] = df["timestamp"].dt.to_period("M").astype(str)
    df["vwap_m"] = df.groupby("mo")["tpv"].cumsum() / df.groupby("mo")["volume"].cumsum()
    df.drop(columns=["tpv", "date", "wk", "mo"], inplace=True)
    return df


def get_trend(df: pd.DataFrame, scan_date: str) -> dict:
    up = df[df["timestamp"].dt.strftime("%Y-%m-%d") <= scan_date].reset_index(drop=True)
    if len(up) < 2: return {"trend": "UNKNOWN", "last_x": "NONE", "last_x_date": "N/A", "last_x_time": "N/A",
                            "bars_ago": "N/A"}
    c = up.iloc[-1];
    p = up.iloc[-2]
    if pd.isna(c["ema7"]) or pd.isna(c["ema21"]):
        trend = "UNKNOWN"
    else:
        gap = abs(c["ema7"] - c["ema21"]) / c["ema21"] * 100
        if gap < 0.1:
            trend = "NEUTRAL"
        elif c["ema7"] > c["ema21"]:
            trend = "STRONG BULL" if (c["close"] > c["ema7"] > p["ema7"] and c["ema21"] > p["ema21"]) else "BULL"
        else:
            trend = "STRONG BEAR" if (c["close"] < c["ema7"] < p["ema7"] and c["ema21"] < p["ema21"]) else "BEAR"

    lx = lxd = lxt = "N/A";
    bars = "N/A"
    for i in range(len(up) - 1, 0, -1):
        c2 = up.iloc[i];
        p2 = up.iloc[i - 1]
        if pd.isna(c2["ema7"]) or pd.isna(p2["ema7"]): continue
        if (p2["ema7"] <= p2["ema21"]) and (c2["ema7"] > c2["ema21"]):
            lx = "BULL_X";
            lxd = c2["timestamp"].strftime("%Y-%m-%d");
            lxt = c2["timestamp"].strftime("%H:%M");
            bars = len(up) - 1 - i;
            break
        if (p2["ema7"] >= p2["ema21"]) and (c2["ema7"] < c2["ema21"]):
            lx = "BEAR_X";
            lxd = c2["timestamp"].strftime("%Y-%m-%d");
            lxt = c2["timestamp"].strftime("%H:%M");
            bars = len(up) - 1 - i;
            break
    return {"trend": trend, "last_x": lx, "last_x_date": lxd, "last_x_time": lxt, "bars_ago": bars}


# ================================================================
# SIGNAL DETECTION (STRICT FILTER APPLIED)
# ================================================================
def detect(df: pd.DataFrame, symbol: str, scan_date: str, trend: dict) -> list[dict]:
    mask = df["timestamp"].dt.strftime("%Y-%m-%d") == scan_date
    indices = df[mask].index.tolist()
    signals = []

    for idx in indices:
        if idx == 0: continue
        cur = df.iloc[idx]
        prv = df.iloc[idx - 1]

        if pd.isna(cur["ema21"]) or pd.isna(cur["vol_avg"]) or cur["vol_avg"] <= 0: continue
        vol_surge = (cur["volume"] - cur["vol_avg"]) / cur["vol_avg"] * 100

        if vol_surge < VOL_SURGE_PCT: continue

        # ---------------------------------------------------------
        # 1. VWAP & EMA Criteria
        # ---------------------------------------------------------
        above_vwaps = (cur["close"] > cur["vwap_w"]) and (cur["close"] > cur["vwap_m"])
        below_vwaps = (cur["close"] < cur["vwap_w"]) and (cur["close"] < cur["vwap_m"])

        above_emas = (cur["close"] > cur["ema100"]) or (cur["close"] > cur["ema200"])
        below_emas = (cur["close"] < cur["ema100"]) or (cur["close"] < cur["ema200"])

        # ---------------------------------------------------------
        # 2. 🚨 STRICT DAY TRADE FILTER 🚨
        # ---------------------------------------------------------
        # A valid long setup requires price to be ABOVE W/M VWAP and ABOVE EMA 100/200
        is_valid_long = above_vwaps and above_emas
        # A valid short setup requires price to be BELOW W/M VWAP and BELOW EMA 100/200
        is_valid_short = below_vwaps and below_emas

        # If it doesn't strictly match a breakout structural setup, throw it away!
        if not (is_valid_long or is_valid_short):
            continue

        # ---------------------------------------------------------
        # 3. Label Specific Breakout Crossovers if they happened exactly on this candle
        # ---------------------------------------------------------
        def x_up(pa, pb, ca, cb):
            return (not pd.isna(pa)) and (pa <= pb) and (ca > cb)

        def x_down(pa, pb, ca, cb):
            return (not pd.isna(pa)) and (pa >= pb) and (ca < cb)

        xlist = []
        if x_up(prv["close"], prv["ema100"], cur["close"], cur["ema100"]): xlist.append(("EMA100", "BULL Breakout"))
        if x_down(prv["close"], prv["ema100"], cur["close"], cur["ema100"]): xlist.append(("EMA100", "BEAR Breakout"))
        if x_up(prv["close"], prv["ema200"], cur["close"], cur["ema200"]): xlist.append(("EMA200", "BULL Breakout"))
        if x_down(prv["close"], prv["ema200"], cur["close"], cur["ema200"]): xlist.append(("EMA200", "BEAR Breakout"))

        xstr = " | ".join(f"{a}:{b}" for a, b in xlist) if xlist else "STRONG TREND (Valid Setup)"

        def ab(a, b):
            return "ABOVE" if a > b else "BELOW"

        candle_body = (cur["close"] - cur["open"]) / cur["open"] * 100

        signals.append({
            "Symbol": symbol,
            "Date": scan_date,
            "Time": cur["timestamp"].strftime("%H:%M"),
            "TF": f"{SCAN_TF_MIN}min",
            "Close": round(cur["close"], 2),
            "Open": round(cur["open"], 2),
            "High": round(cur["high"], 2),
            "Low": round(cur["low"], 2),
            "Body_pct": round(candle_body, 2),
            "Candle": "GREEN" if cur["close"] >= cur["open"] else "RED",
            "EMA7": round(cur["ema7"], 2),
            "EMA21": round(cur["ema21"], 2),
            "EMA100": round(cur["ema100"], 2),
            "EMA200": round(cur["ema200"], 2),
            "EMA7_vs_EMA21": ab(cur["ema7"], cur["ema21"]),
            "Price_vs_EMA7": ab(cur["close"], cur["ema7"]),
            "Price_vs_EMA21": ab(cur["close"], cur["ema21"]),
            "VWAP_D": round(cur["vwap_d"], 2),
            "VWAP_W": round(cur["vwap_w"], 2),
            "VWAP_M": round(cur["vwap_m"], 2),
            "Price_vs_DVWAP": ab(cur["close"], cur["vwap_d"]),
            "Price_vs_WVWAP": ab(cur["close"], cur["vwap_w"]),
            "Price_vs_MVWAP": ab(cur["close"], cur["vwap_m"]),
            "Volume": int(cur["volume"]),
            "VolAvg_prior": int(cur["vol_avg"]),
            "VolSurge_pct": round(vol_surge, 1),
            "Setup_Type": "BULLISH" if is_valid_long else "BEARISH",
            "Crossovers": xstr,
            "Trend": trend["trend"],
            "Last_EMA_X": trend["last_x"],
            "Last_X_Date": trend["last_x_date"],
            "Last_X_Time": trend["last_x_time"],
            "Bars_Since_X": trend["bars_ago"],
        })
    return signals


# ================================================================
# OUTPUT
# ================================================================
def print_signal(s: dict):
    ab = lambda v: "▲" if v == "ABOVE" else "▼"
    xline = f"\n  ★ {s['Crossovers']}"
    print(
        f"\n  {'─' * 65}\n"
        f"  STRICT SETUP: {s['Setup_Type']}  |  {s['Symbol']}  [{s['TF']}]  {s['Date']} {s['Time']}\n"
        f"  {'─' * 65}\n"
        f"  Close : {s['Close']}  O:{s['Open']}  H:{s['High']}  L:{s['Low']}"
        f"   Body:{s['Body_pct']:+.2f}% ({s['Candle']})\n"
        f"  EMA7  : {s['EMA7']}   EMA21: {s['EMA21']}   EMA100: {s['EMA100']}   EMA200: {s['EMA200']}\n"
        f"  D-VWAP: {s['VWAP_D']}   W-VWAP: {s['VWAP_W']}   M-VWAP: {s['VWAP_M']}\n"
        f"  Price {ab(s['Price_vs_DVWAP'])} D-VWAP"
        f"   Price {ab(s['Price_vs_WVWAP'])} W-VWAP"
        f"   Price {ab(s['Price_vs_MVWAP'])} M-VWAP\n"
        f"  Volume: {s['Volume']:,}   Avg({VOL_AVG_BARS}-bar): {s['VolAvg_prior']:,}"
        f"   Surge: +{s['VolSurge_pct']:.0f}%\n"
        f"  Trend : {s['Trend']}"
        f"   Last EMA cross: {s['Last_EMA_X']} ({s['Bars_Since_X']} bars ago)"
        + xline + f"\n  {'─' * 65}"
    )


def send_signal(s: dict):
    ab = lambda v: "▲" if v == "ABOVE" else "▼"
    xline = f"\n<b>🚨 SETUP :</b>\n<code>{s['Crossovers']}</code>"
    tg(
        f"<b>🔥 {s['Setup_Type']} SETUP — {s['Symbol']}</b>  [{s['TF']}]\n"
        f"<b>─────────────────────</b>\n"
        f"🕐 {s['Date']}  {s['Time']}\n"
        f"💰 Close : <b>₹{s['Close']}</b>  ({s['Body_pct']:+.2f}% {s['Candle']})\n"
        f"<b>─────────────────────</b>\n"
        f"📊 EMA100: {s['EMA100']} | EMA200: {s['EMA200']}\n"
        f"<b>─────────────────────</b>\n"
        f"📍 D-VWAP: {s['VWAP_D']}   [{ab(s['Price_vs_DVWAP'])}]\n"
        f"📍 W-VWAP: {s['VWAP_W']}   [{ab(s['Price_vs_WVWAP'])}]\n"
        f"📍 M-VWAP: {s['VWAP_M']}   [{ab(s['Price_vs_MVWAP'])}]\n"
        f"<b>─────────────────────</b>\n"
        f"📦 Vol Surge: <b>+{s['VolSurge_pct']:.0f}%</b>\n"
        + xline
    )


def save_csv(rows: list[dict]):
    if not rows: return
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows: w.writerow({k: row.get(k, "") for k in fields})


# ================================================================
# MAIN
# ================================================================
def main():
    scan_date = datetime.now().strftime("%Y-%m-%d") if TARGET_DATE.lower() == "today" else TARGET_DATE
    from_date = (datetime.strptime(scan_date, "%Y-%m-%d") - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    print("=" * 65)
    print("  STRICT VWAP + EMA BREAKOUT SCANNER")
    print(f"  Scan date : {scan_date}   History: {from_date} →")
    print(f"  Timeframe : {SCAN_TF_MIN}-min   Trigger: Vol > {VOL_SURGE_PCT}%")
    print("=" * 65)

    if not os.path.exists(NSE_CSV):
        print(f"[ERROR] {NSE_CSV} not found");
        sys.exit(1)

    df_nse = pd.read_csv(NSE_CSV)
    sym_col = next((c for c in df_nse.columns if c.lower() in ("symbol", "trading_symbol")), None)
    key_col = next((c for c in df_nse.columns if "instrument_key" in c.lower()), None)

    symbols = []
    total_csv = len(df_nse)
    print(f"\n  Validating keys for {total_csv} stocks...")

    for i, row in df_nse.iterrows():
        sym = str(row[sym_col]).upper().strip()
        stored_key = str(row[key_col]).strip() if key_col else ""
        sys.stdout.write(f"\r  [Setup] Resolving {sym:<14} ({i + 1}/{total_csv})")
        sys.stdout.flush()
        ikey = resolve_key(sym, stored_key)
        if ikey: symbols.append((sym, ikey))

    print()
    total = len(symbols)
    all_sigs = []

    print(f"\n  Scanning {total} symbols...\n")
    tg(f"<b>Scanner started</b>\nDate: {scan_date}  TF: {SCAN_TF_MIN}-min\nStocks: {total}")

    for i, (sym, ikey) in enumerate(symbols, 1):
        sys.stdout.write(f"\r  [{i:>4}/{total}]  {sym:<14}  signals: {len(all_sigs)}")
        sys.stdout.flush()

        df1m = fetch_candles(ikey, from_date, scan_date)
        if df1m is None or len(df1m) < 30: continue

        dftf = resample(df1m)
        if len(dftf) < 10: continue

        avail = sorted(dftf["timestamp"].dt.strftime("%Y-%m-%d").unique())
        date = scan_date if scan_date in avail else avail[-1]

        dftf = add_indicators(dftf)
        trend = get_trend(dftf, date)
        sigs = detect(dftf, sym, date, trend)

        for s in sigs:
            all_sigs.append(s)
            print()
            print_signal(s)
            send_signal(s)

    print(f"\n{'─' * 65}")

    if all_sigs:
        save_csv(all_sigs)
        print(f"\n  {len(all_sigs)} valid signal(s) found on {scan_date}\n")
        print(f"  {'Symbol':<13} {'Time':<6} {'Close':>8} {'EMA100':>8} {'EMA200':>8} {'Setup'}")
        print("  " + "─" * 70)
        for s in all_sigs:
            print(f"  {s['Symbol']:<13} {s['Time']:<6} {s['Close']:>8.2f} "
                  f"{s['EMA100']:>8.2f} {s['EMA200']:>8.2f}  {s['Setup_Type']}")

        lines = [f"<b>Scan done — {scan_date}</b>", f"{len(all_sigs)} STRICT signal(s)\n"]
        for s in all_sigs:
            lines.append(f"<code>{s['Symbol']:<12}</code> {s['Time']}  ₹{s['Close']} ({s['Setup_Type']})")
        tg("\n".join(lines))
    else:
        print(f"\n  No valid setups found on {scan_date}")
        tg(f"<b>Scan done — {scan_date}</b>\nNo signals passed strict filters.")


if __name__ == "__main__":
    main()