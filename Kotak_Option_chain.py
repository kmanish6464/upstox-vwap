"""
╔══════════════════════════════════════════════════════════════════╗
║   NIFTY_Chain_Selector.py  —  v1.1                              ║
║   Auto ATM ± N chain viewer                                      ║
║   Works with KOTAK_1704_V1.py  (no manual scrip lookup needed)  ║
╚══════════════════════════════════════════════════════════════════╝

WHAT IT DOES
────────────
1. Reads KConfig.ini for Kotak credentials (same file as your algo).
2. Authenticates via Kotak REST (TOTP entered manually at prompt).
3. Downloads today's nse_fo scrip master CSV.
4. Finds ALL live NIFTY weekly / monthly expiries automatically.
5. Shows a menu — you pick the expiry (default = nearest).
6. Fetches live Nifty Futures LTP → computes ATM (rounded to STEP).
7. Displays ATM ± WINDOW strikes with CE / PE symbols & tokens.
8. You pick a strike row → symbols/tokens printed to screen.

CHANGES in v1.1
───────────────
• Weekly expiries now parsed correctly (YY + M-char + DD format).
• TOTP entered manually at the prompt — no totp_secret needed in ini.
• KConfig.ini is never modified by this tool.

USAGE
─────
  python NIFTY_Chain_Selector.py

KConfig.ini must contain:
    [KOTAK]
    consumer_key        = <API access token>
    mobile_number       = +91XXXXXXXXXX
    client_code         = <UCC>
    mpin                = <6-digit MPIN>
    nifty_futures_token = <pSymbol of Nifty Fut>   ← e.g. 58662
"""

import sys
import csv
import io
import re
import datetime
import configparser

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
# ② KOTAK ENDPOINTS
# ─────────────────────────────────────────────────────────────
LOGIN_URL    = "https://mis.kotaksecurities.com/login/1.0/tradeApiLogin"
VALIDATE_URL = "https://mis.kotaksecurities.com/login/1.0/tradeApiValidate"
NEO_FIN_KEY  = "neotradeapi"

# ─────────────────────────────────────────────────────────────
# ③ LOAD CONFIG  (no totp_secret needed)
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
FUT_TOKEN      = _cfg("nifty_futures_token")

session = {"trading_token": None, "trading_sid": None, "base_url": None}

# ─────────────────────────────────────────────────────────────
# ④ TOTP — entered manually at runtime
# ─────────────────────────────────────────────────────────────
def get_totp() -> str:
    """Prompt user to type the 6-digit TOTP from their authenticator app."""
    while True:
        try:
            otp = input("🔢  Enter TOTP (6-digit from authenticator app): ").strip()
        except EOFError:
            sys.exit("\n❌  No input — exiting.")
        if otp.isdigit() and len(otp) == 6:
            return otp
        print("   ⚠️  Please enter exactly 6 digits.")

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
        _CSV_CACHE = [{k.strip(): v.strip() for k, v in row.items()} for row in rows]
        print(f"   ✔  {len(_CSV_CACHE):,} instruments loaded")
        return _CSV_CACHE
    except Exception as e:
        print(f"❌  CSV download error: {e}")
        _CSV_CACHE = []
        return []

# ─────────────────────────────────────────────────────────────
# ⑦ SYMBOL PARSING — TWO formats supported
#
#   MONTHLY  (last Thursday of month):
#       NIFTY  DD  MON  STRIKE  CE/PE
#       e.g.  NIFTY26APR24000CE
#
#   WEEKLY  (every Thursday):
#       NIFTY  YY  M  DD  STRIKE  CE/PE
#       YY = 2-digit year (25 = 2025)
#       M  = 1-9 (Jan–Sep), O (Oct), N (Nov), D (Dec)
#       e.g.  NIFTY2541724000CE  → 2025-Apr-17, strike 24000
# ─────────────────────────────────────────────────────────────

# Month-char → month-number mapping for weekly format
_WEEK_MONTH_CHAR = {
    '1': 1, '2': 2, '3': 3, '4': 4,
    '5': 5, '6': 6, '7': 7, '8': 8,
    '9': 9, 'O': 10, 'N': 11, 'D': 12,
}

# Regex for monthly expiry symbols
_MONTHLY_RE = re.compile(r"^NIFTY(\d{2})([A-Z]{3})(\d+)(CE|PE)$")

# Regex for weekly expiry symbols
# Group 1: YY  Group 2: M-char  Group 3: DD  Group 4: strike  Group 5: CE/PE
_WEEKLY_RE  = re.compile(r"^NIFTY(\d{2})([1-9OND])(\d{2})(\d+)(CE|PE)$")


def _parse_row(sym: str):
    """
    Parse a NIFTY option symbol.
    Returns (expiry_date, strike, opt_type) or None.
    Handles both monthly (NIFTY26APR24000CE) and
    weekly (NIFTY2541724000CE) formats.
    """
    today = datetime.date.today()

    # ── Try WEEKLY format first (more specific regex) ──────────
    m = _WEEKLY_RE.match(sym)
    if m:
        yy, mchar, dd, strike_s, opt = m.groups()
        mon_num = _WEEK_MONTH_CHAR.get(mchar)
        if mon_num is None:
            return None
        year = 2000 + int(yy)
        try:
            exp = datetime.date(year, mon_num, int(dd))
        except ValueError:
            return None
        # Reject if year is too far in the past or future (sanity)
        if abs(exp.year - today.year) > 1:
            return None
        return exp, int(strike_s), opt

    # ── Try MONTHLY format ─────────────────────────────────────
    m = _MONTHLY_RE.match(sym)
    if m:
        dd, mon, strike_s, opt = m.groups()
        # Resolve year: find next calendar occurrence of DD-MON
        for yr in (today.year, today.year + 1):
            try:
                exp = datetime.datetime.strptime(
                    f"{dd}{mon}{yr}", "%d%b%Y"
                ).date()
                if exp >= today:
                    return exp, int(strike_s), opt
            except ValueError:
                pass
        return None

    return None


def build_option_map() -> dict:
    """
    Returns:
        { expiry_date: { strike: { 'CE': row, 'PE': row } } }
    """
    rows  = load_nse_fo()
    omap  = {}
    today = datetime.date.today()
    for row in rows:
        sym    = row.get("pTrdSymbol", "")
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
    print(f"   {'#':>3}  {'DATE':<14}  {'DAYS':>5}  {'TYPE':<8}  {'STRIKES':>8}")
    print("   " + "─" * 46)
    for i, d in enumerate(expiries, 1):
        label     = d.strftime("%d %b %Y").upper()
        days_away = (d - today).days
        count     = len(omap[d])
        # Simple heuristic: monthly expiry = last Thursday of the month
        exp_type  = _expiry_type_label(d)
        marker    = "  ← nearest" if i == 1 else ""
        print(f"   {i:>3}.  {label:<14}  {days_away:>5}d  {exp_type:<8}  {count:>8} strikes{marker}")

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


def _expiry_type_label(d: datetime.date) -> str:
    """Return 'MONTHLY' if d is the last Thursday of its month, else 'WEEKLY'."""
    # Last Thursday of the month
    import calendar
    last_thu = max(
        datetime.date(d.year, d.month, day)
        for day in range(1, calendar.monthrange(d.year, d.month)[1] + 1)
        if datetime.date(d.year, d.month, day).weekday() == 3  # Thursday
    )
    return "MONTHLY" if d == last_thu else "WEEKLY"

# ─────────────────────────────────────────────────────────────
# ⑨ LIVE NIFTY FUTURES LTP  →  ATM
# ─────────────────────────────────────────────────────────────
def get_ltp(psymbol: str, exchange_segment: str = "nse_fo") -> float | None:
    """Fetch LTP using the Kotak Quotes v2.1 endpoint."""
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


def print_selected(row: dict) -> None:
    """Print the selected strike's tokens to screen (no file write)."""
    print("\n" + "─" * 60)
    print("   ✅  Selected Tokens")
    print("─" * 60)
    print(f"   call_symbol  =  {row['ce_symbol']}")
    print(f"   call_token   =  {row['ce_token']}")
    print(f"   put_symbol   =  {row['pe_symbol']}")
    print(f"   put_token    =  {row['pe_token']}")
    print("─" * 60)
    print("   Copy these values into KConfig.ini manually, then run the algo.\n")

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    print("╔══════════════════════════════════════════════════╗")
    print("║  NIFTY Chain Selector  v1.1  —  Kotak Neo REST   ║")
    print("╚══════════════════════════════════════════════════╝")

    # 1. Authenticate (TOTP entered at prompt)
    if not authenticate():
        print("\n❌  Authentication failed. Exiting.")
        sys.exit(1)

    # 2. Load scrip master and build option map (weekly + monthly)
    omap = build_option_map()
    if not omap:
        print("❌  No NIFTY options found in scrip master.")
        sys.exit(1)

    # 3. Choose expiry
    expiry     = choose_expiry(omap)
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

    # 6. User selects a row → tokens printed to screen only
    atm_default = next((i + 1 for i, r in enumerate(chain) if r["strike"] == atm), 1)
    try:
        raw = input(
            f"\nEnter row # to view tokens  "
            f"(Enter = row {atm_default} ATM {atm:,},  0 = skip): "
        ).strip()
        if raw == "0":
            print("Skipped.")
            return
        n = int(raw) if raw else atm_default
        if 1 <= n <= len(chain):
            print_selected(chain[n - 1])
        else:
            print("Invalid row — nothing selected.")
    except (ValueError, EOFError):
        print("Skipped.")


if __name__ == "__main__":
    main()
