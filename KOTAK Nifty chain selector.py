"""
╔══════════════════════════════════════════════════════════════════╗
║   NIFTY_Chain_Selector.py  —  v1.0                              ║
║   Auto ATM ± N chain viewer + KConfig.ini writer                ║
║   Works with KOTAK_1704_V1.py  (no manual scrip lookup needed)  ║
╚══════════════════════════════════════════════════════════════════╝

WHAT IT DOES
────────────
1. Reads KConfig.ini for Kotak credentials (same file as your algo).
2. Authenticates via Kotak REST (TOTP auto-generated from Base32 secret).
3. Downloads today's nse_fo scrip master CSV.
4. Finds ALL live NIFTY weekly / monthly expiries automatically.
5. Shows a menu — you pick the expiry (default = nearest).
6. Fetches live Nifty Futures LTP → computes ATM (rounded to STEP).
7. Displays ATM ± WINDOW strikes with CE / PE symbols & tokens.
8. You pick a strike row → writes call_symbol / call_token /
   put_symbol / put_token into KConfig.ini instantly.

USAGE
─────
  python NIFTY_Chain_Selector.py

No arguments needed.  Run it each morning before starting the algo.

KConfig.ini must contain (same as KOTAK_1704_V1.py):
    [KOTAK]
    consumer_key        = <API access token>
    mobile_number       = +91XXXXXXXXXX
    client_code         = <UCC>
    mpin                = <6-digit MPIN>
    totp_secret         = <Base32 TOTP seed>       ← NOT a 6-digit OTP
    nifty_futures_token = <pSymbol of Nifty Fut>   ← e.g. 58662
"""

import sys
import csv
import io
import re
import datetime
import configparser

try:
    import pyotp
except ImportError:
    sys.exit("❌  Missing: pip install pyotp")

try:
    import requests
except ImportError:
    sys.exit("❌  Missing: pip install requests")

# ─────────────────────────────────────────────────────────────
# ① SETTINGS  (change STEP / WINDOW if needed)
# ─────────────────────────────────────────────────────────────
SYMBOL_NAME  = "NIFTY"     # prefix to match in pTrdSymbol
STEP         = 50          # strike step size
WINDOW       = 16          # strikes above AND below ATM

CONFIG_FILE  = "KConfig.ini"

# ─────────────────────────────────────────────────────────────
# ② KOTAK ENDPOINTS  (identical to KOTAK_1704_V1.py)
# ─────────────────────────────────────────────────────────────
LOGIN_URL    = "https://mis.kotaksecurities.com/login/1.0/tradeApiLogin"
VALIDATE_URL = "https://mis.kotaksecurities.com/login/1.0/tradeApiValidate"
NEO_FIN_KEY  = "neotradeapi"

# ─────────────────────────────────────────────────────────────
# ③ LOAD CONFIG
# ─────────────────────────────────────────────────────────────
cfg = configparser.ConfigParser()
cfg.read(CONFIG_FILE)

def _cfg(key, fallback=""):
    try:    return cfg.get("KOTAK", key).strip()
    except: return fallback

K_ACCESS_TOKEN = _cfg("consumer_key") or _cfg("access_token")
K_MOBILE       = _cfg("mobile_number")
K_UCC          = _cfg("client_code")
K_MPIN         = _cfg("mpin")
K_TOTP_SECRET  = _cfg("totp_secret").replace(" ", "")
FUT_TOKEN      = _cfg("nifty_futures_token")

session = {"trading_token": None, "trading_sid": None, "base_url": None}

# ─────────────────────────────────────────────────────────────
# ④ TOTP  (auto-generated — no manual entry)
# ─────────────────────────────────────────────────────────────
def get_totp() -> str:
    if not K_TOTP_SECRET:
        print("❌  totp_secret missing from KConfig.ini")
        print("   Add:  totp_secret = <Base32 seed from Kotak API Dashboard>")
        sys.exit(1)
    if K_TOTP_SECRET.isdigit() and len(K_TOTP_SECRET) == 6:
        print("⚠️   totp_secret looks like a 6-digit OTP, not a Base32 seed.")
        print("   Go to Kotak API Dashboard → TOTP Registration → copy the Base32 key.")
        return K_TOTP_SECRET          # fallback: use as-is
    try:
        otp = pyotp.TOTP(K_TOTP_SECRET).now()
        print(f"✅  TOTP auto-generated: {otp}")
        return otp
    except Exception as e:
        print(f"❌  TOTP generation failed: {e}")
        sys.exit(1)

# ─────────────────────────────────────────────────────────────
# ⑤ AUTHENTICATION  (2-step: TOTP → MPIN)
# ─────────────────────────────────────────────────────────────
def authenticate() -> bool:
    totp = get_totp()

    print(f"\n🔑  Step 2a: TOTP login  (mobile={K_MOBILE}  ucc={K_UCC}) ...")
    try:
        r = requests.post(
            LOGIN_URL,
            headers={"Authorization": K_ACCESS_TOKEN,
                     "neo-fin-key":   NEO_FIN_KEY,
                     "Content-Type":  "application/json"},
            json={"mobileNumber": K_MOBILE, "ucc": K_UCC, "totp": totp},
            timeout=10,
        )
        resp = r.json()
        data = resp.get("data", {})
        if data.get("status") != "success":
            print(f"❌  Login failed: {resp}")
            return False
        view_token = data["token"]
        view_sid   = data["sid"]
        print("   ✔  Step 2a OK")
    except Exception as e:
        print(f"❌  Step 2a error: {e}")
        return False

    print("🔐  Step 2b: Validating MPIN ...")
    try:
        r2 = requests.post(
            VALIDATE_URL,
            headers={"Authorization": K_ACCESS_TOKEN,
                     "neo-fin-key":   NEO_FIN_KEY,
                     "Content-Type":  "application/json",
                     "sid":           view_sid,
                     "Auth":          view_token},
            json={"mpin": K_MPIN},
            timeout=10,
        )
        resp2 = r2.json()
        data2 = resp2.get("data", {})
        if data2.get("status") != "success":
            print(f"❌  MPIN failed: {resp2}")
            return False
        session["trading_token"] = data2["token"]
        session["trading_sid"]   = data2["sid"]
        session["base_url"]      = data2.get("baseUrl", "https://cis.kotaksecurities.com")
        print(f"✅  Authenticated!  base_url={session['base_url']}")
        return True
    except Exception as e:
        print(f"❌  Step 2b error: {e}")
        return False

# ─────────────────────────────────────────────────────────────
# ⑥ SCRIP MASTER — download nse_fo CSV
# ─────────────────────────────────────────────────────────────
_CSV_CACHE: list | None = None

def load_nse_fo() -> list:
    global _CSV_CACHE
    if _CSV_CACHE is not None:
        return _CSV_CACHE

    url = f"{session['base_url']}/script-details/1.0/masterscrip/file-paths"
    try:
        r    = requests.get(url, headers={"Authorization": K_ACCESS_TOKEN}, timeout=10)
        urls = r.json().get("data", {}).get("filesPaths", [])
    except Exception as e:
        print(f"❌  Scrip master file-paths error: {e}")
        _CSV_CACHE = []
        return []

    nse_fo_url = next((u for u in urls if "nse_fo" in u.lower()), None)
    if not nse_fo_url:
        print(f"❌  nse_fo URL not found in: {urls}")
        _CSV_CACHE = []
        return []

    print(f"⬇️   Downloading scrip master  ({nse_fo_url.split('/')[-1]}) ...")
    try:
        r2   = requests.get(nse_fo_url, timeout=60)
        r2.encoding = "utf-8"
        rows = list(csv.DictReader(io.StringIO(r2.text)))
        # strip whitespace from all keys
        _CSV_CACHE = [{k.strip(): v.strip() for k, v in row.items()} for row in rows]
        print(f"   ✔  {len(_CSV_CACHE):,} instruments loaded")
        return _CSV_CACHE
    except Exception as e:
        print(f"❌  CSV download error: {e}")
        _CSV_CACHE = []
        return []

# ─────────────────────────────────────────────────────────────
# ⑦ SYMBOL PARSING
#   Format:  NIFTY  +  DDMON  +  STRIKE  +  CE/PE
#   Example: NIFTY26APR24000CE  → expiry 26-Apr-2026, strike 24000
#
#   NOTE: The CSV contains a bare DDMON tag (no year digit).
#   Year is resolved as the next calendar occurrence of that date.
# ─────────────────────────────────────────────────────────────
def _resolve_year(day: int, month_str: str) -> int | None:
    """Return the nearest future (or today) year for a given DDMON."""
    today = datetime.date.today()
    for yr in (today.year, today.year + 1):
        try:
            d = datetime.datetime.strptime(f"{day:02d}{month_str}{yr}", "%d%b%Y").date()
            if d >= today:
                return yr
        except ValueError:
            pass
    return None

_EXP_RE = re.compile(r"^NIFTY(\d{2})([A-Z]{3})(\d+)(CE|PE)$")

def _parse_row(sym: str):
    """
    Returns (expiry_date, strike, opt_type) or None.
    """
    m = _EXP_RE.match(sym)
    if not m:
        return None
    dd, mon, strike_s, opt = m.groups()
    yr = _resolve_year(int(dd), mon)
    if yr is None:
        return None
    try:
        exp = datetime.datetime.strptime(f"{dd}{mon}{yr}", "%d%b%Y").date()
    except ValueError:
        return None
    return exp, int(strike_s), opt

def build_option_map() -> dict:
    """
    Returns:
        { expiry_date: { strike: { 'CE': row, 'PE': row } } }
    """
    rows   = load_nse_fo()
    omap   = {}
    today  = datetime.date.today()
    for row in rows:
        sym = row.get("pTrdSymbol", "")
        parsed = _parse_row(sym)
        if not parsed:
            continue
        exp, strike, opt = parsed
        if exp < today:
            continue
        omap.setdefault(exp, {}).setdefault(strike, {})[opt] = row
    return omap

# ─────────────────────────────────────────────────────────────
# ⑧ EXPIRY SELECTION MENU
# ─────────────────────────────────────────────────────────────
def choose_expiry(omap: dict) -> datetime.date:
    expiries = sorted(omap.keys())
    if not expiries:
        print("❌  No future NIFTY expiries found in scrip master.")
        sys.exit(1)

    today = datetime.date.today()
    print("\n📅  Available NIFTY expiries:")
    print(f"   {'#':>3}  {'DATE':<14}  {'DAYS':>5}  {'STRIKES':>8}")
    print("   " + "─" * 38)
    for i, d in enumerate(expiries, 1):
        label     = d.strftime("%d %b %Y").upper()
        days_away = (d - today).days
        count     = len(omap[d])
        marker    = "  ← nearest" if i == 1 else ""
        print(f"   {i:>3}.  {label:<14}  {days_away:>5}d  {count:>8} strikes{marker}")

    default_label = expiries[0].strftime("%d %b %Y").upper()
    try:
        raw = input(f"\nSelect expiry [1–{len(expiries)}]  (Enter = {default_label}): ").strip()
        n   = int(raw) if raw else 1
        if 1 <= n <= len(expiries):
            chosen = expiries[n - 1]
        else:
            chosen = expiries[0]
    except (ValueError, EOFError):
        chosen = expiries[0]

    print(f"   ✔  Expiry selected: {chosen.strftime('%d %b %Y').upper()}")
    return chosen

# ─────────────────────────────────────────────────────────────
# ⑨ LIVE NIFTY FUTURES LTP  →  ATM
# ─────────────────────────────────────────────────────────────
def get_ltp(psymbol: str, exchange_segment: str = "nse_fo") -> float | None:
    """Fetch LTP using the Kotak Quotes v2.1 endpoint (same as KOTAK_1704_V1)."""
    if not session["trading_token"]:
        return None
    url = f"{session['base_url']}/quotes/v2.1"
    payload = {
        "instrument_tokens": [
            {"instrument_token": psymbol, "exchange_segment": exchange_segment}
        ]
    }
    try:
        r = requests.post(
            url,
            headers={"Auth":         session["trading_token"],
                     "Sid":          session["trading_sid"],
                     "neo-fin-key":  NEO_FIN_KEY,
                     "Content-Type": "application/json",
                     "Accept":       "application/json"},
            json=payload,
            timeout=5,
        )
        if r.status_code != 200:
            return None
        data  = r.json()
        items = data.get("data", {})
        entry = items[0] if isinstance(items, list) and items \
                else next(iter(items.values()), {}) if isinstance(items, dict) else {}
        for field in ("ltp", "84ltp", "last_price", "lp"):
            val = entry.get(field)
            if val is not None:
                try:    return float(val)
                except: pass
    except Exception:
        pass
    return None

def detect_atm() -> int:
    if FUT_TOKEN:
        print(f"\n📈  Fetching Nifty Futures LTP (token={FUT_TOKEN}) ...")
        ltp = get_ltp(FUT_TOKEN, "nse_fo")
        if ltp:
            atm = round(ltp / STEP) * STEP
            print(f"   ✔  Futures LTP = {ltp:,.2f}  →  ATM = {atm:,}")
            return atm
        print("   ⚠️  LTP fetch failed — market may be closed or token wrong.")
    try:
        val = input(f"\nEnter Nifty level to compute ATM (e.g. 24150): ").strip()
        return round(int(val) / STEP) * STEP
    except Exception:
        print("   Using default ATM = 24000")
        return 24000

# ─────────────────────────────────────────────────────────────
# ⑩ BUILD & DISPLAY CHAIN
# ─────────────────────────────────────────────────────────────
def build_chain(strike_map: dict, atm: int) -> list:
    """Return list of dicts for strikes ATM ± WINDOW."""
    lo = atm - WINDOW * STEP
    hi = atm + WINDOW * STEP
    chain = []
    for strike in sorted(strike_map):
        if lo <= strike <= hi:
            ce  = strike_map[strike].get("CE", {})
            pe  = strike_map[strike].get("PE", {})
            chain.append({
                "strike":    strike,
                "ce_symbol": ce.get("pTrdSymbol", "—"),
                "ce_token":  ce.get("pSymbol",    "—"),
                "pe_symbol": pe.get("pTrdSymbol", "—"),
                "pe_token":  pe.get("pSymbol",    "—"),
            })
    return chain

def display_chain(chain: list, atm: int, expiry: datetime.date) -> None:
    exp_label = expiry.strftime("%d %b %Y").upper()
    W = 112
    print("\n" + "═" * W)
    print(f"   🎯  NIFTY OPTION CHAIN  |  Expiry: {exp_label}  |  ATM: {atm:,}  |  ±{WINDOW} strikes  (step {STEP})")
    print("═" * W)
    print(f"   {'#':>3}  {'STRIKE':>8}  {'CE SYMBOL':<26} {'CE TOKEN':>9}  {'PE SYMBOL':<26} {'PE TOKEN':>9}")
    print("   " + "─" * (W - 3))
    for i, row in enumerate(chain, 1):
        mark = "  ◄ ATM" if row["strike"] == atm else ""
        print(
            f"   {i:>3}  {row['strike']:>8,}  "
            f"{row['ce_symbol']:<26} {row['ce_token']:>9}  "
            f"{row['pe_symbol']:<26} {row['pe_token']:>9}"
            f"{mark}"
        )
    print("═" * W)

# ─────────────────────────────────────────────────────────────
# ⑪ WRITE KConfig.ini
# ─────────────────────────────────────────────────────────────
def update_kconfig(row: dict) -> None:
    """Write the 4 option fields into [KOTAK] section of KConfig.ini."""
    if not cfg.has_section("KOTAK"):
        cfg.add_section("KOTAK")
    cfg.set("KOTAK", "call_symbol", row["ce_symbol"])
    cfg.set("KOTAK", "call_token",  row["ce_token"])
    cfg.set("KOTAK", "put_symbol",  row["pe_symbol"])
    cfg.set("KOTAK", "put_token",   row["pe_token"])
    with open(CONFIG_FILE, "w") as fh:
        cfg.write(fh)
    print(f"\n✅  KConfig.ini updated:")
    print(f"   call_symbol = {row['ce_symbol']}   call_token = {row['ce_token']}")
    print(f"   put_symbol  = {row['pe_symbol']}   put_token  = {row['pe_token']}")
    print(f"\n   ▶  Now run: python KOTAK_1704_V1.py")

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    print("╔══════════════════════════════════════════════════╗")
    print("║  NIFTY Chain Selector  —  Kotak Neo REST         ║")
    print("╚══════════════════════════════════════════════════╝")

    # 1. Authenticate
    if not authenticate():
        print("\n❌  Authentication failed. Exiting.")
        sys.exit(1)

    # 2. Load scrip master and build option map
    omap = build_option_map()
    if not omap:
        print("❌  No NIFTY options found in scrip master.")
        sys.exit(1)

    # 3. Choose expiry
    expiry = choose_expiry(omap)
    strike_map = omap[expiry]

    # 4. Detect ATM from live futures LTP
    atm = detect_atm()

    # 5. Build and display chain
    chain = build_chain(strike_map, atm)
    if not chain:
        print(f"❌  No strikes found in range {atm - WINDOW*STEP}–{atm + WINDOW*STEP}.")
        print(f"   Available strikes for {expiry}: {sorted(strike_map.keys())[:10]} ...")
        sys.exit(1)

    display_chain(chain, atm, expiry)

    # 6. User selects a row → update KConfig.ini
    print(f"\nEnter row # to select strike & update KConfig.ini  (Enter = ATM row, 0 = skip):")
    atm_default = next((i + 1 for i, r in enumerate(chain) if r["strike"] == atm), 1)
    try:
        raw = input(f"Choice [1–{len(chain)}]  (Enter = row {atm_default}, ATM {atm:,}): ").strip()
        if raw == "0":
            print("Skipped — KConfig.ini unchanged.")
            return
        n = int(raw) if raw else atm_default
        if 1 <= n <= len(chain):
            update_kconfig(chain[n - 1])
        else:
            print("Invalid row — KConfig.ini unchanged.")
    except (ValueError, EOFError):
        print("Skipped — KConfig.ini unchanged.")

if __name__ == "__main__":
    main()
