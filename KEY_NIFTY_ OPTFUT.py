import pandas as pd
import requests
import datetime
import sys
import io

# ✅ FIX: Suppress SSL warnings on Windows where CA bundle is often missing
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------
TOKEN_FILE = "token.txt"
INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv.gz"
BASE_URL = "https://api.upstox.com/v2"
OPTION_INDEX_KEY = "NSE_INDEX|Nifty 50"

# ✅ FIX: Single flag to control SSL verification across the entire script.
#         Set to True if you install certificates later (recommended for production).
VERIFY_SSL = False


def load_token():
    try:
        with open(TOKEN_FILE, "r") as f:
            return f.read().strip()
    except Exception:
        print(f"❌ Error: '{TOKEN_FILE}' not found.")
        return None


def get_api_headers(token):
    return {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}


# -----------------------------------------------------------------------------
# 1. FETCH INSTRUMENTS (FUTURES & OPTIONS EXPIRY)
# -----------------------------------------------------------------------------
def get_nifty_data():
    """Extracts BOTH Futures and the Next Option Expiry directly from the CSV"""
    print("⬇️  Downloading Master Instrument List... (Please wait)")
    try:
        # ✅ FIX: Download via requests (supports verify=False) then feed to pandas
        response = requests.get(INSTRUMENT_URL, verify=VERIFY_SSL, timeout=30)
        response.raise_for_status()
        df = pd.read_csv(io.BytesIO(response.content), compression='gzip')

        if 'tradingsymbol' in df.columns:
            df.rename(columns={'tradingsymbol': 'trading_symbol'}, inplace=True)

        today = datetime.datetime.now().date()

        # --- A. Get Top 3 Futures ---
        nifty_fut = df[(df['name'] == 'NIFTY') & (df['instrument_type'] == 'FUTIDX')].copy()
        nifty_fut['expiry_date'] = pd.to_datetime(nifty_fut['expiry']).dt.date
        nifty_fut = nifty_fut[nifty_fut['expiry_date'] >= today]
        top_futures = nifty_fut.sort_values(by='expiry_date').head(3)

        # --- B. Get Next Option Expiry ---
        nifty_opt = df[(df['name'] == 'NIFTY') & (df['instrument_type'] == 'OPTIDX')].copy()
        nifty_opt['expiry_date'] = pd.to_datetime(nifty_opt['expiry']).dt.date
        future_opt_expiries = sorted(
            nifty_opt[nifty_opt['expiry_date'] >= today]['expiry_date'].unique()
        )
        next_opt_expiry = future_opt_expiries[0].strftime("%Y-%m-%d") if future_opt_expiries else None

        return top_futures, next_opt_expiry

    except Exception as e:
        print(f"❌ Error processing instrument list: {e}")
        return pd.DataFrame(), None


# -----------------------------------------------------------------------------
# 2. FETCH FUTURES LTP
# -----------------------------------------------------------------------------
def normalize_key(k: str) -> str:
    """Upstox sometimes returns keys with '%7C' or ':' instead of '|'. Normalize all to '|'."""
    return k.replace('%7C', '|').replace(':', '|').upper()


def get_fut_ltp(token, instrument_keys):
    url = f"{BASE_URL}/market-quote/ltp"
    params = {'instrument_key': ",".join(instrument_keys)}
    try:
        # ✅ FIX: Added verify=VERIFY_SSL
        response = requests.get(url, headers=get_api_headers(token), params=params, verify=VERIFY_SSL)
        data = response.json()

        if data.get('status') == 'success':
            raw = data.get('data', {})
            normalized_prices = {normalize_key(k): v.get('last_price', 0.0) for k, v in raw.items()}
            result = {}
            for key in instrument_keys:
                result[key] = normalized_prices.get(normalize_key(key), "N/A")
            return result
        else:
            err = data.get('errors', [{}])[0].get('message', 'Unknown error')
            print(f"⚠️  API Warning (LTP): {err}")
            print(f"   Raw response: {data}")
    except Exception as e:
        print(f"⚠️  LTP Request Failed: {e}")
    return {}


# -----------------------------------------------------------------------------
# 3. FETCH OPTION CHAIN
# -----------------------------------------------------------------------------
def fetch_option_chain(token, expiry_date):
    url = f"{BASE_URL}/option/chain"
    params = {"instrument_key": OPTION_INDEX_KEY, "expiry_date": expiry_date}
    try:
        # ✅ FIX: Added verify=VERIFY_SSL
        response = requests.get(url, headers=get_api_headers(token), params=params, verify=VERIFY_SSL)
        data = response.json()
        if data.get('status') == 'success':
            return data.get('data', [])
        else:
            err = data.get('errors', [{}])[0].get('message', 'Unknown error')
            print(f"❌ API Error (Chain): {err}")
    except Exception as e:
        print(f"❌ API Request Failed: {e}")
    return None


# -----------------------------------------------------------------------------
# MAIN EXECUTION
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    token = load_token()
    if not token:
        sys.exit(1)

    futures_df, option_expiry = get_nifty_data()

    # ── Step 1: Display Nifty Futures ────────────────────────────────────────
    if not futures_df.empty:
        fut_keys = futures_df['instrument_key'].tolist()
        prices = get_fut_ltp(token, fut_keys)

        print("\n" + "=" * 95)
        print("🚀  NIFTY FUTURES (Live Prices)")
        print("=" * 95)
        print(f"{'EXPIRY':<15} {'TRADING SYMBOL':<20} {'LTP':>12}  {'INSTRUMENT KEY'}")
        print("-" * 95)

        for _, fut in futures_df.iterrows():
            expiry_str = fut['expiry_date'].strftime("%Y-%m-%d")
            ltp = prices.get(fut['instrument_key'], "N/A")
            ltp_str = f"{ltp:,.2f}" if isinstance(ltp, float) else str(ltp)
            print(f"{expiry_str:<15} {fut['trading_symbol']:<20} {ltp_str:>12}  {fut['instrument_key']}")
    else:
        print("❌ Could not find Nifty Future contracts.")

    # ── Step 2: Display Option Chain ─────────────────────────────────────────
    if option_expiry:
        print(f"\n⏳  Fetching Nifty 50 Option Chain for expiry: {option_expiry} ...")
        chain_data = fetch_option_chain(token, option_expiry)

        if chain_data:
            data_list = []
            spot_price = None

            for item in chain_data:
                ce = item.get('call_options', {})
                pe = item.get('put_options', {})

                if spot_price is None:
                    spot_price = (
                        ce.get('market_data', {}).get('underlying_spot_price') or
                        pe.get('market_data', {}).get('underlying_spot_price')
                    )

                data_list.append({
                    "CE Key":   ce.get('instrument_key', "N/A"),
                    "CE OI":    ce.get('market_data', {}).get('oi', 0),
                    "CE LTP":   ce.get('market_data', {}).get('ltp', 0.0),
                    "Strike":   item['strike_price'],
                    "PE LTP":   pe.get('market_data', {}).get('ltp', 0.0),
                    "PE OI":    pe.get('market_data', {}).get('oi', 0),
                    "PE Key":   pe.get('instrument_key', "N/A"),
                })

            df = pd.DataFrame(data_list).sort_values(by="Strike").reset_index(drop=True)

            if spot_price:
                print(f"   Underlying Spot Price: ₹{spot_price:,.2f}")
                df['spot_diff'] = abs(df['Strike'] - spot_price)
                atm_idx = df['spot_diff'].idxmin()
            else:
                non_zero = df[(df['CE LTP'] > 0) & (df['PE LTP'] > 0)].copy()
                if not non_zero.empty:
                    non_zero['diff'] = abs(non_zero['CE LTP'] - non_zero['PE LTP'])
                    atm_idx = non_zero['diff'].idxmin()
                else:
                    atm_idx = len(df) // 2

            atm_strike = df.loc[atm_idx, 'Strike']
            print(f"   ATM Strike identified: ₹{atm_strike:,.0f}")

            window = df.iloc[max(0, atm_idx - 8): min(len(df), atm_idx + 9)].copy()
            window = window.drop(columns=['spot_diff'], errors='ignore')

            for col in ['CE LTP', 'PE LTP']:
                window[col] = window[col].apply(lambda x: f"{x:>8.2f}")
            for col in ['CE OI', 'PE OI']:
                window[col] = window[col].apply(lambda x: f"{int(x):>10,}")
            window['Strike'] = window['Strike'].apply(lambda x: f"{x:>8.0f}")

            print("\n" + "=" * 115)
            print(f"🎯  ATM CENTERED VIEW  |  Expiry: {option_expiry}  |  ATM: ₹{atm_strike:,.0f}")
            print("=" * 115)
            print(window[['CE Key', 'CE OI', 'CE LTP', 'Strike', 'PE LTP', 'PE OI', 'PE Key']].to_string(index=False))
            print("=" * 115)
        else:
            print("❌ Could not retrieve option chain data.")
    else:
        print("❌ Could not determine next option expiry.")