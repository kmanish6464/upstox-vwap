"""
SOSS Dashboard — Flask route patches
Add these 4 routes to LIVE_UPSTOX_-805.py just before the startup_banner() function.
They power the Config editor, Log viewer, and Option Chain panel in the dashboard.
"""

import os, json, configparser
from flask import Flask, request, jsonify, send_from_directory


# ─────────────────────────────────────────────────────────────────
# ROUTE 1 — /config  GET: read config.ini + token.txt
#                    POST: write config.ini + token.txt
# ─────────────────────────────────────────────────────────────────
@app.route("/config", methods=["GET", "POST"])
def config_route():
    CONFIG_FILE = "config.ini"
    TOKEN_FILE_  = "token.txt"

    if request.method == "GET":
        cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        cfg.read(CONFIG_FILE)

        token_val = ""
        if os.path.exists(TOKEN_FILE_):
            with open(TOKEN_FILE_) as f:
                token_val = f.read().strip()

        return jsonify({
            "token": token_val,
            "config": {
                "upstox": dict(cfg["UPSTOX"])   if "UPSTOX"   in cfg else {},
                "telegram": dict(cfg["TELEGRAM"]) if "TELEGRAM" in cfg else {},
                "risk": dict(cfg["RISK"])        if "RISK"     in cfg else {},
                "settings": dict(cfg["SETTINGS"]) if "SETTINGS" in cfg else {},
            }
        })

    # POST — write back
    data = request.get_json(force=True)
    try:
        # 1. Write token.txt
        if data.get("token"):
            with open(TOKEN_FILE_, "w") as f:
                f.write(data["token"].strip())
            log.info("CONFIG: token.txt updated via dashboard")

        # 2. Write config.ini — read existing first, patch only supplied keys
        cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        cfg.read(CONFIG_FILE)

        c = data.get("config", {})
        section_map = {
            "UPSTOX":   c.get("upstox",   {}),
            "TELEGRAM": c.get("telegram", {}),
            "RISK":     c.get("risk",     {}),
        }
        for section, pairs in section_map.items():
            if not cfg.has_section(section):
                cfg.add_section(section)
            for k, v in pairs.items():
                if v != "":
                    cfg.set(section, k, str(v))

        with open(CONFIG_FILE, "w") as f:
            cfg.write(f)
        log.info("CONFIG: config.ini updated via dashboard")

        return jsonify({"status": "ok", "note": "Restart the bot for changes to take effect."})

    except Exception as exc:
        log.error("CONFIG write error: %s", exc)
        return jsonify({"status": "error", "reason": str(exc)}), 500


# ─────────────────────────────────────────────────────────────────
# ROUTE 2 — /logs?tail=200   Return last N lines of the log file
# ─────────────────────────────────────────────────────────────────
@app.route("/logs", methods=["GET"])
def logs_route():
    log_file = "webhook_live_trade.log"
    tail_n   = int(request.args.get("tail", 300))
    try:
        if not os.path.exists(log_file):
            return jsonify({"content": "(log file not found)"})
        with open(log_file, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if tail_n:
            lines = lines[-tail_n:]
        return jsonify({"content": "".join(lines), "total_lines": len(lines)})
    except Exception as exc:
        return jsonify({"content": f"Error reading log: {exc}"}), 500


# ─────────────────────────────────────────────────────────────────
# ROUTE 3 — /chain   Proxy the Upstox option chain
#           Returns ATM-centred 17-strike window as JSON
# ─────────────────────────────────────────────────────────────────
@app.route("/chain", methods=["GET"])
def chain_route():
    """
    Calls Upstox /v2/option/chain using the live token and returns
    the data in the format the dashboard expects.
    Requires: requests, token.txt
    """
    import requests as req_lib, datetime

    BASE_URL       = "https://api.upstox.com/v2"
    OPTION_IDX_KEY = "NSE_INDEX|Nifty 50"
    VERIFY_SSL     = False

    token = load_token()
    if not token:
        return jsonify({"error": "token missing"}), 401

    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    # Step 1: next option expiry
    try:
        inst_url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv.gz"
        import pandas as pd, io
        resp = req_lib.get(inst_url, verify=VERIFY_SSL, timeout=30)
        df   = pd.read_csv(io.BytesIO(resp.content), compression="gzip")
        if "tradingsymbol" in df.columns:
            df.rename(columns={"tradingsymbol": "trading_symbol"}, inplace=True)
        today    = datetime.datetime.now().date()
        nifty_opt= df[(df["name"]=="NIFTY") & (df["instrument_type"]=="OPTIDX")].copy()
        nifty_opt["expiry_date"] = pd.to_datetime(nifty_opt["expiry"]).dt.date
        expiries = sorted(nifty_opt[nifty_opt["expiry_date"] >= today]["expiry_date"].unique())
        if not expiries:
            return jsonify({"error": "no expiry found"}), 404
        expiry = expiries[0].strftime("%Y-%m-%d")
    except Exception as exc:
        return jsonify({"error": f"instrument fetch failed: {exc}"}), 500

    # Step 2: fetch chain
    try:
        r = req_lib.get(f"{BASE_URL}/option/chain",
                        headers=headers,
                        params={"instrument_key": OPTION_IDX_KEY, "expiry_date": expiry},
                        verify=VERIFY_SSL, timeout=10)
        data = r.json()
        if data.get("status") != "success":
            return jsonify({"error": data.get("errors", [{}])[0].get("message", "API error")}), 500
    except Exception as exc:
        return jsonify({"error": f"chain fetch failed: {exc}"}), 500

    # Step 3: build response
    chain_data = data.get("data", [])
    rows       = []
    spot_price = None
    for item in chain_data:
        ce = item.get("call_options", {})
        pe = item.get("put_options",  {})
        if spot_price is None:
            spot_price = (ce.get("market_data", {}).get("underlying_spot_price") or
                          pe.get("market_data", {}).get("underlying_spot_price"))
        rows.append({
            "strike": item["strike_price"],
            "ce_oi":  ce.get("market_data", {}).get("oi",  0),
            "ce_ltp": ce.get("market_data", {}).get("ltp", 0),
            "pe_ltp": pe.get("market_data", {}).get("ltp", 0),
            "pe_oi":  pe.get("market_data", {}).get("oi",  0),
            "ce_key": ce.get("instrument_key", ""),
            "pe_key": pe.get("instrument_key", ""),
        })

    rows.sort(key=lambda x: x["strike"])

    # ATM
    atm_strike = None
    if spot_price:
        atm_strike = min(rows, key=lambda x: abs(x["strike"] - spot_price))["strike"]
    atm_idx = next((i for i, r in enumerate(rows) if r["strike"] == atm_strike), len(rows) // 2)
    window  = rows[max(0, atm_idx - 8): atm_idx + 9]

    return jsonify({
        "expiry":      expiry,
        "spot_price":  spot_price,
        "atm_strike":  atm_strike,
        "chain":       window,
    })


# ─────────────────────────────────────────────────────────────────
# ROUTE 4 — /dashboard   Serve the HTML dashboard file
# ─────────────────────────────────────────────────────────────────
@app.route("/dashboard")
def dashboard():
    """Open http://localhost:5000/dashboard in any browser."""
    return send_from_directory(".", "soss_dashboard.html")
