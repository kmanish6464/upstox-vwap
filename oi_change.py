"""
Nifty Futures — OI / Volume Live Tracker
==========================================
• Fetches 3-min intraday candles from Upstox every 3 min
• Stores OI, ΔOI, Volume, Price in MongoDB (NSE_DAILY.oi_tracker)
• Serves a live dashboard at http://localhost:5050

Run:
    python nifty_oi_tracker.py

Open browser:
    http://localhost:5050
"""

import os, sys, time, threading, logging, configparser, urllib.parse
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests
import pytz
from flask import Flask, jsonify, render_template_string
from pymongo import MongoClient, ASCENDING, UpdateOne
from pymongo.errors import BulkWriteError

# ─────────────────────────────────────────────────────────────────
# 0.  LOGGING
# ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler("oi_tracker.log", encoding="utf-8")]
)
log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────────────────────────
# 1.  CONFIG
# ─────────────────────────────────────────────────────────────────
cfg = configparser.ConfigParser(inline_comment_prefixes=(";","#"))
if os.path.exists("config.ini"):
    cfg.read("config.ini")

TOKEN_FILE   = cfg.get("UPSTOX","token_file",       fallback="token.txt").strip()
FUTURES_KEY  = cfg.get("UPSTOX","Nifty_futures_key",fallback="NSE_FO|57960").strip()
FETCH_EVERY  = 180          # seconds (3 min)
TIMEFRAME    = "3minute"    # Upstox API interval string

MONGO_URI    = "mongodb://localhost:27017/"
DB_NAME      = "NSE_DAILY"
COL_NAME     = "oi_tracker"
HIST_URL     = "https://api.upstox.com/v2/historical-candle"

# ─────────────────────────────────────────────────────────────────
# 2.  MONGODB
# ─────────────────────────────────────────────────────────────────
_client = None
_col    = None

def get_col():
    global _client, _col
    if _col is None:
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=4000)
        _col    = _client[DB_NAME][COL_NAME]
        _col.create_index([("instrument_key",ASCENDING),("ts",ASCENDING)],
                          unique=True, name="ix_key_ts", background=True)
    return _col

# ─────────────────────────────────────────────────────────────────
# 3.  UPSTOX FETCH
# ─────────────────────────────────────────────────────────────────
def load_token() -> Optional[str]:
    if not os.path.exists(TOKEN_FILE):
        log.error("token.txt not found")
        return None
    return open(TOKEN_FILE).read().strip()

def fetch_candles() -> Optional[pd.DataFrame]:
    tok = load_token()
    if not tok:
        return None
    hdrs = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
    enc  = urllib.parse.quote(FUTURES_KEY, safe="")
    url  = f"{HIST_URL}/intraday/{enc}/{TIMEFRAME}"
    try:
        r = requests.get(url, headers=hdrs, timeout=12)
        if r.status_code == 401:
            log.error("Token expired — regenerate token.txt"); return None
        if r.status_code != 200:
            log.warning("Upstox HTTP %s: %s", r.status_code, r.text[:150]); return None
        candles = r.json().get("data",{}).get("candles",[])
        if not candles:
            log.warning("No candles returned"); return None

        df = pd.DataFrame(candles, columns=["ts","o","h","l","c","v","oi"])
        df["ts"] = pd.to_datetime(df["ts"])
        if df["ts"].dt.tz is None:
            df["ts"] = df["ts"].dt.tz_localize("UTC").dt.tz_convert(IST)
        else:
            df["ts"] = df["ts"].dt.tz_convert(IST)
        df.sort_values("ts", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df
    except Exception as exc:
        log.error("Fetch error: %s", exc)
        return None

# ─────────────────────────────────────────────────────────────────
# 4.  CALCULATE + STORE
# ─────────────────────────────────────────────────────────────────
OI_ACTIONS = {
    ("up","up"):   ("LB","Long Buildup","#00ff88"),
    ("up","down"): ("SC","Short Covering","#00d4ff"),
    ("dn","up"):   ("SB","Short Buildup","#ff4444"),
    ("dn","dn"):   ("LU","Long Unwinding","#ffaa00"),
}

def oi_action_tag(price_chg: float, oi_chg: float) -> tuple:
    pk = "up" if price_chg >= 0 else "dn"
    ok = "up" if oi_chg    >= 0 else "dn"
    return OI_ACTIONS.get((pk,ok), ("NE","Neutral","#888888"))

# running state
_last_fetch_ts   = None
_fetch_count     = 0
_fetch_status    = "Waiting for first fetch..."

def fetch_and_store() -> int:
    global _last_fetch_ts, _fetch_count, _fetch_status
    log.info("Fetching 3-min candles for %s …", FUTURES_KEY)
    df = fetch_candles()
    if df is None or df.empty:
        _fetch_status = f"⚠ Fetch failed at {datetime.now(IST).strftime('%H:%M:%S')}"
        return 0

    # Drop last (forming) candle
    df = df.iloc[:-1].copy()

    # Derived columns
    df["oi_change"]    = df["oi"].diff().fillna(0).astype(int)
    df["price_change"] = df["c"].diff().fillna(0).round(2)
    df["oi_act"]       = df.apply(
        lambda r: oi_action_tag(r["price_change"], r["oi_change"])[0], axis=1)
    df["oi_label"]     = df.apply(
        lambda r: oi_action_tag(r["price_change"], r["oi_change"])[1], axis=1)
    df["oi_color"]     = df.apply(
        lambda r: oi_action_tag(r["price_change"], r["oi_change"])[2], axis=1)
    df["session_date"] = df["ts"].dt.strftime("%Y-%m-%d")

    # Upsert into MongoDB
    col  = get_col()
    ops  = []
    for _, row in df.iterrows():
        ts_utc = row["ts"].astimezone(timezone.utc).replace(tzinfo=None)
        ops.append(UpdateOne(
            {"instrument_key": FUTURES_KEY, "ts": ts_utc},
            {"$set": {
                "instrument_key": FUTURES_KEY,
                "ts":   ts_utc,
                "o":    float(row["o"]),  "h": float(row["h"]),
                "l":    float(row["l"]),  "c": float(row["c"]),
                "v":    int(row["v"]),    "oi": int(row["oi"]),
                "oi_change":    int(row["oi_change"]),
                "price_change": float(row["price_change"]),
                "oi_act":   row["oi_act"],
                "oi_label": row["oi_label"],
                "oi_color": row["oi_color"],
                "session_date": row["session_date"],
            }},
            upsert=True
        ))

    saved = 0
    try:
        res   = col.bulk_write(ops, ordered=False)
        saved = res.upserted_count + res.modified_count
    except BulkWriteError as bwe:
        saved = bwe.details.get("nUpserted",0) + bwe.details.get("nModified",0)

    _last_fetch_ts = datetime.now(IST)
    _fetch_count  += 1
    _fetch_status  = f"✓ {_last_fetch_ts.strftime('%H:%M:%S')}  ({len(df)} candles, {saved} new)"
    log.info("Stored %d candles (%d new/updated)", len(df), saved)
    return saved

# ─────────────────────────────────────────────────────────────────
# 5.  BACKGROUND THREAD
# ─────────────────────────────────────────────────────────────────
def _bg_loop():
    fetch_and_store()          # immediate first fetch
    while True:
        time.sleep(FETCH_EVERY)
        fetch_and_store()

threading.Thread(target=_bg_loop, daemon=True).start()

# ─────────────────────────────────────────────────────────────────
# 6.  FLASK API
# ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

def _today_str():
    return datetime.now(IST).strftime("%Y-%m-%d")

@app.route("/api/data")
def api_data():
    """All candles for today from MongoDB."""
    try:
        col  = get_col()
        docs = list(col.find(
            {"instrument_key": FUTURES_KEY, "session_date": _today_str()},
            {"_id":0},
            sort=[("ts", ASCENDING)]
        ))
        for d in docs:
            d["ts"] = (d["ts"].replace(tzinfo=timezone.utc)
                       .astimezone(IST).strftime("%H:%M"))
        return jsonify({"ok": True, "data": docs,
                        "status": _fetch_status, "count": len(docs)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/api/latest")
def api_latest():
    """Most recent single candle."""
    try:
        col = get_col()
        doc = col.find_one(
            {"instrument_key": FUTURES_KEY, "session_date": _today_str()},
            {"_id":0}, sort=[("ts",-1)]
        )
        if doc:
            doc["ts"] = (doc["ts"].replace(tzinfo=timezone.utc)
                         .astimezone(IST).strftime("%H:%M"))
        return jsonify({"ok": True, "data": doc, "status": _fetch_status})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/api/trigger")
def api_trigger():
    """Manually trigger a fetch."""
    threading.Thread(target=fetch_and_store, daemon=True).start()
    return jsonify({"ok": True, "msg": "Fetch triggered"})

@app.route("/api/stats")
def api_stats():
    """Session summary stats."""
    try:
        col  = get_col()
        docs = list(col.find(
            {"instrument_key": FUTURES_KEY, "session_date": _today_str()},
            {"_id":0,"c":1,"oi":1,"oi_change":1,"v":1,"oi_act":1},
            sort=[("ts", ASCENDING)]
        ))
        if not docs:
            return jsonify({"ok":True,"data":{}})
        first, last = docs[0], docs[-1]
        total_vol   = sum(d["v"] for d in docs)
        total_oi_ch = sum(d["oi_change"] for d in docs)
        act_counts  = {}
        for d in docs:
            act_counts[d["oi_act"]] = act_counts.get(d["oi_act"],0) + 1
        return jsonify({"ok":True,"data":{
            "open":  first["c"],
            "ltp":   last["c"],
            "oi":    last["oi"],
            "oi_change_last": last["oi_change"],
            "total_oi_change": total_oi_ch,
            "total_volume": total_vol,
            "oi_act": last["oi_act"],
            "oi_label": last.get("oi_label",""),
            "oi_color": last.get("oi_color","#888"),
            "candles": len(docs),
            "act_counts": act_counts,
        }})
    except Exception as exc:
        return jsonify({"ok":False,"error":str(exc)}), 500

# ─────────────────────────────────────────────────────────────────
# 7.  DASHBOARD HTML
# ─────────────────────────────────────────────────────────────────
DASHBOARD = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nifty OI Tracker</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root{
    --bg:       #060a12;
    --bg2:      #0c1220;
    --bg3:      #111b2e;
    --border:   #1a2840;
    --teal:     #00e5c8;
    --teal2:    #00b5a0;
    --green:    #00ff88;
    --red:      #ff3355;
    --amber:    #ffb300;
    --blue:     #4488ff;
    --purple:   #a855f7;
    --text:     #d8e4f0;
    --muted:    #4a6080;
    --mono:     'Share Tech Mono', monospace;
    --display:  'Orbitron', sans-serif;
    --body:     'Inter', sans-serif;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%;background:var(--bg);color:var(--text);font-family:var(--body);font-size:14px;overflow-x:hidden}

  /* ── SCANLINE EFFECT ── */
  body::before{
    content:'';position:fixed;inset:0;
    background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.08) 2px,rgba(0,0,0,.08) 4px);
    pointer-events:none;z-index:0;
  }

  /* ── HEADER ── */
  .header{
    display:flex;align-items:center;justify-content:space-between;
    padding:14px 24px;
    background:linear-gradient(90deg,#060a12 0%,#0c1726 50%,#060a12 100%);
    border-bottom:1px solid var(--border);
    position:relative;z-index:10;
  }
  .header-left{display:flex;align-items:center;gap:16px}
  .logo{font-family:var(--display);font-size:18px;font-weight:900;letter-spacing:2px;
        color:var(--teal);text-shadow:0 0 20px rgba(0,229,200,.5)}
  .sub{font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:1px}
  .badge{padding:3px 10px;border-radius:3px;font-family:var(--mono);font-size:11px;letter-spacing:1px}
  .badge-live{background:rgba(0,255,136,.12);border:1px solid var(--green);color:var(--green)}
  .badge-paper{background:rgba(255,179,0,.12);border:1px solid var(--amber);color:var(--amber)}

  .header-right{display:flex;align-items:center;gap:20px}
  .fetch-status{font-family:var(--mono);font-size:11px;color:var(--muted);max-width:260px;
                white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .countdown-wrap{display:flex;align-items:center;gap:8px}
  .countdown-label{font-family:var(--mono);font-size:11px;color:var(--muted)}
  .countdown-val{font-family:var(--display);font-size:22px;font-weight:700;
                 color:var(--teal);min-width:36px;text-align:center;
                 text-shadow:0 0 12px rgba(0,229,200,.6)}
  .refresh-btn{
    padding:6px 14px;border-radius:4px;border:1px solid var(--teal);
    background:rgba(0,229,200,.07);color:var(--teal);
    font-family:var(--mono);font-size:12px;cursor:pointer;letter-spacing:1px;
    transition:all .2s;
  }
  .refresh-btn:hover{background:rgba(0,229,200,.18);box-shadow:0 0 12px rgba(0,229,200,.3)}

  /* ── STATS ROW ── */
  .stats{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;
         background:var(--border);border-bottom:1px solid var(--border);position:relative;z-index:5}
  .stat-card{
    background:var(--bg2);padding:16px 20px;
    position:relative;overflow:hidden;
  }
  .stat-card::before{
    content:'';position:absolute;top:0;left:0;right:0;height:2px;
    background:linear-gradient(90deg,transparent,var(--teal),transparent);
    opacity:.4;
  }
  .stat-label{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:1.5px;margin-bottom:6px}
  .stat-val{font-family:var(--display);font-size:20px;font-weight:700;letter-spacing:1px}
  .stat-sub{font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:4px}
  .col-teal{color:var(--teal)}
  .col-green{color:var(--green)}
  .col-red{color:var(--red)}
  .col-amber{color:var(--amber)}
  .col-blue{color:var(--blue)}
  .col-purple{color:var(--purple)}

  /* ── OI ACTION PILL ── */
  .oi-pill{
    display:inline-block;padding:4px 12px;border-radius:3px;
    font-family:var(--display);font-size:13px;font-weight:700;letter-spacing:2px;
  }

  /* ── MAIN CONTENT ── */
  .content{padding:16px 20px;display:flex;flex-direction:column;gap:16px;
           position:relative;z-index:5}

  /* ── CHART PANEL ── */
  .panel{
    background:var(--bg2);border:1px solid var(--border);border-radius:6px;
    overflow:hidden;
  }
  .panel-header{
    display:flex;align-items:center;justify-content:space-between;
    padding:12px 18px;border-bottom:1px solid var(--border);
    background:linear-gradient(90deg,var(--bg3),var(--bg2));
  }
  .panel-title{font-family:var(--display);font-size:11px;font-weight:700;
               letter-spacing:2px;color:var(--teal)}
  .panel-meta{font-family:var(--mono);font-size:10px;color:var(--muted)}
  .chart-wrap{padding:14px 16px 10px}

  /* ── LEGEND ROW ── */
  .legend-row{display:flex;gap:16px;padding:6px 18px 10px;flex-wrap:wrap}
  .legend-item{display:flex;align-items:center;gap:6px;
               font-family:var(--mono);font-size:10px;color:var(--muted)}
  .legend-dot{width:10px;height:10px;border-radius:50%}
  .legend-line{width:20px;height:2px}

  /* ── OI ACTION BREAKDOWN ── */
  .act-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;
            padding:10px 18px 14px}
  .act-item{text-align:center;padding:8px 4px;border-radius:4px;border:1px solid var(--border)}
  .act-name{font-family:var(--display);font-size:10px;letter-spacing:1px;margin-bottom:4px}
  .act-count{font-family:var(--display);font-size:18px;font-weight:700}

  /* ── FOOTER ── */
  .footer{padding:8px 20px;border-top:1px solid var(--border);
          display:flex;justify-content:space-between;align-items:center;
          font-family:var(--mono);font-size:10px;color:var(--muted);
          position:relative;z-index:5}

  /* ── PULSE DOT ── */
  .pulse{display:inline-block;width:7px;height:7px;border-radius:50%;
         background:var(--green);box-shadow:0 0 0 0 rgba(0,255,136,.5);
         animation:pulse 2s infinite}
  @keyframes pulse{
    0%{box-shadow:0 0 0 0 rgba(0,255,136,.5)}
    70%{box-shadow:0 0 0 8px rgba(0,255,136,0)}
    100%{box-shadow:0 0 0 0 rgba(0,255,136,0)}
  }
  .no-data{text-align:center;padding:40px;font-family:var(--mono);color:var(--muted);font-size:13px}
  .spinner{display:inline-block;width:16px;height:16px;border:2px solid var(--border);
           border-top-color:var(--teal);border-radius:50%;animation:spin .8s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>

<!-- HEADER -->
<div class="header">
  <div class="header-left">
    <div>
      <div class="logo">NIFTY OI SCANNER</div>
      <div class="sub" id="futKey">loading…</div>
    </div>
    <span class="badge badge-live" id="modeBadge">● LIVE</span>
  </div>
  <div class="header-right">
    <div class="fetch-status" id="fetchStatus">connecting…</div>
    <div class="countdown-wrap">
      <span class="countdown-label">NEXT</span>
      <span class="countdown-val" id="countdown">—</span>
      <span class="countdown-label">s</span>
    </div>
    <button class="refresh-btn" onclick="triggerFetch()">⟳ FETCH</button>
  </div>
</div>

<!-- STATS ROW -->
<div class="stats">
  <div class="stat-card">
    <div class="stat-label">LTP (FUTURES)</div>
    <div class="stat-val col-teal" id="sLtp">—</div>
    <div class="stat-sub" id="sChg">—</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">OPEN INTEREST</div>
    <div class="stat-val col-purple" id="sOi">—</div>
    <div class="stat-sub" id="sOiSub">contracts</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">OI CHANGE (3m)</div>
    <div class="stat-val" id="sOiCh">—</div>
    <div class="stat-sub" id="sOiChSub">last candle</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">SESSION OI Δ</div>
    <div class="stat-val" id="sTotalOi">—</div>
    <div class="stat-sub">total change today</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">VOLUME (session)</div>
    <div class="stat-val col-blue" id="sVol">—</div>
    <div class="stat-sub" id="sVolSub">shares traded</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">OI ACTION</div>
    <div id="sAct" style="margin-top:4px"><span class="oi-pill" style="background:rgba(136,136,136,.15);color:#888">—</span></div>
    <div class="stat-sub" id="sActSub" style="margin-top:6px">—</div>
  </div>
</div>

<!-- MAIN -->
<div class="content">

  <!-- Chart 1: Price + VWAP -->
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">PRICE</span>
      <span class="panel-meta" id="priceMeta">—</span>
    </div>
    <div class="legend-row">
      <div class="legend-item"><div class="legend-line" style="background:var(--teal)"></div>Close</div>
      <div class="legend-item"><div class="legend-line" style="background:var(--amber)"></div>VWAP</div>
    </div>
    <div class="chart-wrap"><canvas id="chartPrice" height="110"></canvas></div>
  </div>

  <!-- Chart 2: OI + ΔOI -->
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">OPEN INTEREST  &amp;  OI CHANGE</span>
      <span class="panel-meta" id="oiMeta">—</span>
    </div>
    <div class="legend-row">
      <div class="legend-item"><div class="legend-dot" style="background:var(--purple)"></div>OI (bars, left)</div>
      <div class="legend-item"><div class="legend-line" style="background:var(--teal)"></div>ΔOI (line, right)</div>
    </div>
    <div class="chart-wrap"><canvas id="chartOI" height="130"></canvas></div>
  </div>

  <!-- Chart 3: Volume bars -->
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">VOLUME  (3-MIN)</span>
      <span class="panel-meta" id="volMeta">—</span>
    </div>
    <div class="chart-wrap"><canvas id="chartVol" height="100"></canvas></div>
  </div>

  <!-- OI action breakdown -->
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">OI ACTION BREAKDOWN  (SESSION)</span>
      <span class="panel-meta">candle count</span>
    </div>
    <div class="act-grid" id="actGrid">
      <div class="act-item" style="border-color:#00ff8840">
        <div class="act-name col-green">LB</div>
        <div class="act-count col-green" id="acLB">—</div>
        <div class="legend-label" style="font-size:9px;color:var(--muted);font-family:var(--mono)">Long Buildup</div>
      </div>
      <div class="act-item" style="border-color:#4488ff40">
        <div class="act-name col-blue">SC</div>
        <div class="act-count col-blue" id="acSC">—</div>
        <div class="legend-label" style="font-size:9px;color:var(--muted);font-family:var(--mono)">Short Covering</div>
      </div>
      <div class="act-item" style="border-color:#ff335540">
        <div class="act-name col-red">SB</div>
        <div class="act-count col-red" id="acSB">—</div>
        <div class="legend-label" style="font-size:9px;color:var(--muted);font-family:var(--mono)">Short Buildup</div>
      </div>
      <div class="act-item" style="border-color:#ffb30040">
        <div class="act-name col-amber">LU</div>
        <div class="act-count col-amber" id="acLU">—</div>
        <div class="legend-label" style="font-size:9px;color:var(--muted);font-family:var(--mono)">Long Unwinding</div>
      </div>
    </div>
  </div>

</div>

<!-- FOOTER -->
<div class="footer">
  <span><span class="pulse"></span>&nbsp; NIFTY OI TRACKER — 3-MIN REALTIME</span>
  <span id="lastUpdated">—</span>
  <span>NSE_DAILY.oi_tracker · MongoDB</span>
</div>

<script>
// ── Chart.js defaults ──────────────────────────────────────────
Chart.defaults.color = '#4a6080';
Chart.defaults.borderColor = '#1a2840';
Chart.defaults.font.family = "'Share Tech Mono', monospace";
Chart.defaults.font.size   = 10;

const BASE_OPTS = {
  responsive:true, animation:{duration:400},
  plugins:{legend:{display:false},tooltip:{
    backgroundColor:'#0c1220',borderColor:'#1a2840',borderWidth:1,
    titleColor:'#00e5c8',bodyColor:'#d8e4f0',
    titleFont:{family:"'Orbitron',sans-serif",size:10},
    bodyFont:{family:"'Share Tech Mono',monospace",size:11},
    callbacks:{label: ctx => `  ${ctx.dataset.label||''}: ${Number(ctx.parsed.y||0).toLocaleString('en-IN')}`}
  }},
  scales:{
    x:{grid:{color:'rgba(26,40,64,.8)'},ticks:{maxRotation:0,maxTicksLimit:10}},
    y:{grid:{color:'rgba(26,40,64,.8)'},position:'left'},
  }
};

// ── chart instances ────────────────────────────────────────────
let cPrice, cOI, cVol;

function mkPrice(labels, closes, vwaps){
  const ctx = document.getElementById('chartPrice').getContext('2d');
  const gradT = ctx.createLinearGradient(0,0,0,160);
  gradT.addColorStop(0,'rgba(0,229,200,.25)'); gradT.addColorStop(1,'rgba(0,229,200,0)');
  if(cPrice) cPrice.destroy();
  cPrice = new Chart(ctx, {
    type:'line',
    data:{labels, datasets:[
      {label:'Close',data:closes,borderColor:'#00e5c8',borderWidth:2,
       backgroundColor:gradT,fill:true,pointRadius:0,tension:.35},
      {label:'VWAP', data:vwaps, borderColor:'#ffb300',borderWidth:1.5,
       borderDash:[5,3],fill:false,pointRadius:0,tension:.35},
    ]},
    options:{...JSON.parse(JSON.stringify(BASE_OPTS)),
      plugins:{...BASE_OPTS.plugins,
        tooltip:{...BASE_OPTS.plugins.tooltip,
          callbacks:{label:ctx=>`  ${ctx.dataset.label}: ${ctx.parsed.y.toFixed(2)}`}
        }
      }
    }
  });
}

function mkOI(labels, ois, oiChanges, colors){
  const ctx = document.getElementById('chartOI').getContext('2d');
  if(cOI) cOI.destroy();
  cOI = new Chart(ctx,{
    type:'bar',
    data:{labels, datasets:[
      {label:'OI',data:ois,backgroundColor: colors.map(c=>c+'44'),
       borderColor:colors,borderWidth:1,order:2,yAxisID:'yL'},
      {label:'ΔOI',data:oiChanges,type:'line',borderColor:'#00e5c8',borderWidth:1.5,
       pointRadius:2,pointBackgroundColor:'#00e5c8',fill:false,tension:.3,order:1,yAxisID:'yR'},
    ]},
    options:{
      responsive:true,animation:{duration:400},
      plugins:BASE_OPTS.plugins,
      scales:{
        x:{grid:{color:'rgba(26,40,64,.8)'},ticks:{maxRotation:0,maxTicksLimit:10}},
        yL:{grid:{color:'rgba(26,40,64,.8)'},position:'left',
            ticks:{callback:v=>(v/1e5).toFixed(1)+'L'}},
        yR:{grid:{display:false},position:'right',
            ticks:{callback:v=>v>=0?'+'+v.toLocaleString('en-IN'):v.toLocaleString('en-IN')},
            title:{display:true,text:'ΔOI',color:'#00e5c8',font:{size:9}}},
      }
    }
  });
}

function mkVol(labels, vols){
  const ctx = document.getElementById('chartVol').getContext('2d');
  const gradV = ctx.createLinearGradient(0,0,0,120);
  gradV.addColorStop(0,'rgba(68,136,255,.7)'); gradV.addColorStop(1,'rgba(68,136,255,.1)');
  if(cVol) cVol.destroy();
  cVol = new Chart(ctx,{
    type:'bar',
    data:{labels, datasets:[
      {label:'Volume',data:vols,backgroundColor:gradV,borderColor:'#4488ff',borderWidth:1},
    ]},
    options:{...JSON.parse(JSON.stringify(BASE_OPTS)),
      scales:{...BASE_OPTS.scales,
        y:{...BASE_OPTS.scales.y,ticks:{callback:v=>v>=1e5?(v/1e5).toFixed(1)+'L':v.toLocaleString('en-IN')}},
      }
    }
  });
}

// ── helpers ────────────────────────────────────────────────────
function fmt(n,dec=2){return n==null?'—':Number(n).toLocaleString('en-IN',{minimumFractionDigits:dec,maximumFractionDigits:dec})}
function fmtInt(n){return n==null?'—':Number(n).toLocaleString('en-IN')}

function calcVwap(data){
  let cumTV=0, cumV=0;
  return data.map(d=>{cumTV+=((d.h+d.l+d.c)/3)*d.v; cumV+=d.v; return cumV?+(cumTV/cumV).toFixed(2):d.c});
}

// ── main data load ─────────────────────────────────────────────
async function loadData(){
  try{
    const [dRes, sRes] = await Promise.all([
      fetch('/api/data').then(r=>r.json()),
      fetch('/api/stats').then(r=>r.json()),
    ]);

    document.getElementById('fetchStatus').textContent = dRes.status || '—';

    if(!dRes.ok || !dRes.data || !dRes.data.length){
      ['chartPrice','chartOI','chartVol'].forEach(id=>{
        const c = document.getElementById(id);
        const ctx = c.getContext('2d');
        ctx.clearRect(0,0,c.width,c.height);
      });
      document.getElementById('priceMeta').textContent = 'No data for today yet';
      return;
    }

    const data   = dRes.data;
    const labels = data.map(d=>d.ts);
    const closes = data.map(d=>+d.c);
    const vwaps  = calcVwap(data);
    const ois    = data.map(d=>+d.oi);
    const oiChs  = data.map(d=>+d.oi_change);
    const vols   = data.map(d=>+d.v);
    const colors = data.map(d=>d.oi_color||'#888888');

    mkPrice(labels, closes, vwaps);
    mkOI(labels, ois, oiChs, colors);
    mkVol(labels, vols);

    // meta labels
    document.getElementById('priceMeta').textContent =
      `O: ${fmt(closes[0])}  H: ${fmt(Math.max(...closes))}  L: ${fmt(Math.min(...closes))}  C: ${fmt(closes.at(-1))}`;
    document.getElementById('oiMeta').textContent =
      `Max ΔOI: ${fmtInt(Math.max(...oiChs))}  Min ΔOI: ${fmtInt(Math.min(...oiChs))}`;
    document.getElementById('volMeta').textContent =
      `Total: ${fmtInt(vols.reduce((a,b)=>a+b,0))}  Peak: ${fmtInt(Math.max(...vols))}`;

    // stats row
    if(sRes.ok && sRes.data){
      const s = sRes.data;
      const chg = s.ltp - s.open;
      const chgPct = s.open ? (chg/s.open*100) : 0;
      document.getElementById('sLtp').textContent = fmt(s.ltp);
      document.getElementById('sChg').innerHTML =
        `<span class="${chg>=0?'col-green':'col-red'}">${chg>=0?'+':''}${fmt(chg)} (${chgPct>=0?'+':''}${chgPct.toFixed(2)}%)</span>`;
      document.getElementById('sOi').textContent = fmtInt(s.oi);
      const oiLacs = s.oi ? (s.oi/100000).toFixed(2)+' L' : '—';
      document.getElementById('sOiSub').textContent = oiLacs+' contracts';
      const och = s.oi_change_last;
      document.getElementById('sOiCh').innerHTML =
        `<span class="${och>=0?'col-green':'col-red'}">${och>=0?'+':''}${fmtInt(och)}</span>`;
      document.getElementById('sOiChSub').textContent = 'last 3-min candle';
      const toch = s.total_oi_change;
      document.getElementById('sTotalOi').innerHTML =
        `<span class="${toch>=0?'col-green':'col-red'}">${toch>=0?'+':''}${fmtInt(toch)}</span>`;
      document.getElementById('sVol').textContent = fmtInt(s.total_volume);
      document.getElementById('sVolSub').textContent =
        s.total_volume ? (s.total_volume/1e5).toFixed(2)+' L shares' : '—';

      // OI action pill
      const actEl = document.getElementById('sAct');
      const color = s.oi_color || '#888';
      actEl.innerHTML = `<span class="oi-pill" style="background:${color}22;color:${color};border:1px solid ${color}55">${s.oi_act||'—'}</span>`;
      document.getElementById('sActSub').textContent = s.oi_label || '—';

      // action breakdown
      const ac = s.act_counts || {};
      document.getElementById('acLB').textContent = ac.LB || 0;
      document.getElementById('acSC').textContent = ac.SC || 0;
      document.getElementById('acSB').textContent = ac.SB || 0;
      document.getElementById('acLU').textContent = ac.LU || 0;
    }

    document.getElementById('futKey').textContent = '{{ futures_key }}  ·  3-MIN';
    document.getElementById('lastUpdated').textContent =
      'Updated ' + new Date().toLocaleTimeString('en-IN');

  } catch(e){
    console.error(e);
    document.getElementById('fetchStatus').textContent = '⚠ Connection error';
  }
}

// ── countdown timer ────────────────────────────────────────────
let secsLeft = {{ fetch_every }};
function tick(){
  document.getElementById('countdown').textContent = secsLeft;
  if(secsLeft <= 0){ secsLeft = {{ fetch_every }}; loadData(); }
  secsLeft--;
}
setInterval(tick, 1000);
tick();

// ── manual fetch trigger ───────────────────────────────────────
async function triggerFetch(){
  document.getElementById('fetchStatus').textContent = '⏳ Fetching…';
  await fetch('/api/trigger');
  setTimeout(()=>{ secsLeft = 3; }, 500);   // reload in 3s
}

// ── initial load ───────────────────────────────────────────────
loadData();
</script>
</body>
</html>
"""

@app.route("/")
def dashboard():
    return render_template_string(
        DASHBOARD,
        futures_key=FUTURES_KEY,
        fetch_every=FETCH_EVERY,
    )

# ─────────────────────────────────────────────────────────────────
# 8.  STARTUP
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════════════╗
║        NIFTY OI TRACKER — LIVE DASHBOARD             ║
╠══════════════════════════════════════════════════════╣
║  Futures key : {FUTURES_KEY:<37}║
║  Timeframe   : {TIMEFRAME:<37}║
║  MongoDB     : {MONGO_URI:<37}║
║  Database    : {DB_NAME}.{COL_NAME:<29}║
╠══════════════════════════════════════════════════════╣
║  Open browser → http://localhost:5050                ║
╚══════════════════════════════════════════════════════╝
""")
    try:
        get_col()
        log.info("MongoDB connected → %s.%s", DB_NAME, COL_NAME)
    except Exception as e:
        log.warning("MongoDB not yet reachable: %s — will retry on first fetch.", e)

    app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False)
