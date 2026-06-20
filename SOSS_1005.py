"""
=============================================================
 SMART OPTIONS SIGNAL SYSTEM  —  SOSS_1005_v9
 Upstox Live / Paper Trade  |  reads config.ini

 FIXES vs v7:
   1. Protobuf import removed entirely.
      upstox_client.MarketDataStreamerV3 handles decoding internally.
      No more "ModuleNotFoundError: MarketDataFeed_pb2".
   2. Historical API uses "1minute" + resamples to 5-min in Python.
   3. Cumulative vtt → per-tick delta tracked correctly.

 Install:
   pip install upstox-python-sdk numpy requests
   (websocket-client already pulled in by the SDK)
=============================================================
"""

import configparser, json, logging, sys
import threading, time, datetime
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import quote as url_encode

import numpy as np
import requests

# ── Upstox SDK (v2.19.0+) ────────────────────────────────────
try:
    import upstox_client
    _streamer_cls = upstox_client.MarketDataStreamerV3
except (ImportError, AttributeError) as _e:
    print(f"CRITICAL  upstox-python-sdk missing or too old: {_e}")
    print("  Run:  pip install --upgrade upstox-python-sdk")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("soss.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("SOSS")


# =============================================================
#  SECTION 1 — CONFIG LOADER
# =============================================================

BASE_DIR = Path(__file__).parent


def _get(p, section, key, fallback=None, cast=None):
    try:
        v = p.get(section, key)
        return cast(v) if cast else v
    except (configparser.NoSectionError, configparser.NoOptionError):
        return fallback


def load_config(path: str = "config.ini") -> dict:
    p = configparser.ConfigParser(
        inline_comment_prefixes=(";", "#"),
        comment_prefixes=(";", "#"),
    )
    cfg_path = BASE_DIR / path
    if not cfg_path.exists():
        log.critical("config.ini not found at %s", cfg_path); sys.exit(1)
    p.read(cfg_path, encoding="utf-8")

    tok_file = BASE_DIR / _get(p, "UPSTOX", "token_file", "token.txt")
    if not tok_file.exists():
        log.critical("token.txt not found at %s", tok_file); sys.exit(1)
    token = tok_file.read_text(encoding="utf-8").strip()
    if not token:
        log.critical("token.txt is empty"); sys.exit(1)
    log.info("Token OK  len=%d  prefix=%s...", len(token), token[:12])

    lot = _get(p, "SETTINGS", "lot_size", 25, int)
    cfg = dict(
        access_token   = token,
        api_base       = "https://api.upstox.com/v2",
        call_key       = _get(p, "UPSTOX", "buy_instrument_key",  ""),
        put_key        = _get(p, "UPSTOX", "sell_instrument_key", ""),
        futures_key    = _get(p, "UPSTOX", "nifty_futures_key",   ""),
        timeframe      = _get(p, "SETTINGS", "timeframe",     "5minute"),
        loop_interval  = _get(p, "SETTINGS", "loop_interval", 15,   int),
        lot_size       = lot,
        ntz_upper      = _get(p, "ZONES", "no_trade_upper",  0.0, float),
        ntz_lower      = _get(p, "ZONES", "no_trade_lower",  0.0, float),
        upper_limit    = _get(p, "ZONES", "upper_limit",     0.0, float),
        lower_limit    = _get(p, "ZONES", "lower_limit",     0.0, float),
        r1             = _get(p, "ZONES", "r1",              0.0, float),
        r2             = _get(p, "ZONES", "r2",              0.0, float),
        s1             = _get(p, "ZONES", "s1",              0.0, float),
        s2             = _get(p, "ZONES", "s2",              0.0, float),
        level_buffer   = _get(p, "ZONES", "level_buffer",   15.0, float),
        enable_live    = _get(p, "LIVE_TRADING", "enable_live_orders",
                              "false").lower() in ("true", "1", "yes"),
        product_type   = _get(p, "LIVE_TRADING", "product_type",       "I"),
        qty_call       = _get(p, "LIVE_TRADING", "trade_qty_call", lot, int),
        qty_put        = _get(p, "LIVE_TRADING", "trade_qty_put",  lot, int),
        squareoff_hhmm = _get(p, "LIVE_TRADING", "auto_squareoff_time", "15:20"),
        max_daily_loss = _get(p, "LIVE_TRADING", "max_daily_loss_rs",   0, float),
        order_retry    = _get(p, "LIVE_TRADING", "order_retry",         2, int),
        tg_token       = _get(p, "TELEGRAM", "bot_token",   ""),
        tg_chat        = _get(p, "TELEGRAM", "chat_id",     ""),
        tg_enable      = _get(p, "TELEGRAM", "enable_telegram",
                              "false").lower() in ("true", "1", "yes"),
        delta_min      = 20_000.0,
        delta_ovrd     = 80_000.0,
    )
    log.info("Config  futures=%s  call=%s  put=%s",
             cfg["futures_key"], cfg["call_key"], cfg["put_key"])
    log.info("Live=%s  NTZ=%.0f-%.0f  Limits=%.0f-%.0f",
             cfg["enable_live"], cfg["ntz_lower"], cfg["ntz_upper"],
             cfg["lower_limit"], cfg["upper_limit"])
    return cfg


# =============================================================
#  SECTION 2 — TELEGRAM
# =============================================================

class Telegram:
    def __init__(self, cfg):
        self._t = cfg["tg_token"]; self._c = cfg["tg_chat"]
        self._on = cfg["tg_enable"]

    def send(self, text: str):
        if not self._on: return
        try:
            requests.post(f"https://api.telegram.org/bot{self._t}/sendMessage",
                          json={"chat_id": self._c, "text": text}, timeout=5)
        except Exception as e:
            log.warning("Telegram: %s", e)


# =============================================================
#  SECTION 3 — CANDLE STORE
# =============================================================

class Candle:
    __slots__ = ("ts", "open", "high", "low", "close", "volume", "oi", "delta")
    def __init__(self, ts, o, h, l, c, vol, oi=0.0, delta=0.0):
        self.ts=ts; self.open=o; self.high=h; self.low=l
        self.close=c; self.volume=vol; self.oi=oi; self.delta=delta


class CandleBuffer:
    def __init__(self, maxlen=300):
        self._bars: deque = deque(maxlen=maxlen)
        self._live: Optional[Candle] = None
        self._lock = threading.Lock()

    def on_tick(self, price: float, volume: float, oi: float,
                ts: datetime.datetime):
        bar_ts = ts.replace(second=0, microsecond=0,
                            minute=(ts.minute // 5) * 5)
        with self._lock:
            if self._live is None or self._live.ts != bar_ts:
                if self._live is not None:
                    self._bars.append(self._live)
                    log.debug("BAR %s  C=%.2f  vol=%.0f  oi=%.0f",
                              self._live.ts.strftime("%H:%M"),
                              self._live.close, self._live.volume, self._live.oi)
                self._live = Candle(bar_ts, price, price, price, price, volume, oi)
            else:
                b = self._live
                b.high   = max(b.high, price)
                b.low    = min(b.low,  price)
                b.close  = price
                b.volume += volume
                b.oi     = oi
                b.delta  += volume if price >= b.open else -volume

    def push_bar(self, c: Candle):
        with self._lock: self._bars.append(c)

    def snapshot(self) -> list:
        with self._lock: return list(self._bars)

    def bar_count(self) -> int:
        with self._lock: return len(self._bars)


# =============================================================
#  SECTION 4 — HISTORICAL CANDLE LOADER
#  Upstox valid intraday intervals: 1minute | 30minute
#  We fetch 1minute and resample → 5min in memory.
# =============================================================

def _agg(rows, ts) -> Candle:
    o   = float(rows[0][1])
    h   = max(float(r[2]) for r in rows)
    l   = min(float(r[3]) for r in rows)
    c   = float(rows[-1][4])
    vol = sum(float(r[5]) for r in rows)
    oi  = float(rows[-1][6]) if len(rows[-1]) > 6 else 0.0
    bar = Candle(ts, o, h, l, c, vol, oi)
    bar.delta = vol if c >= o else -vol
    return bar


def _resample(rows: list) -> list:
    bars, group, grp_ts = [], [], None
    for row in rows:
        try:
            ts = datetime.datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        except Exception:
            continue
        bar_ts = ts.replace(second=0, microsecond=0, minute=(ts.minute // 5) * 5)
        if grp_ts is None:
            grp_ts = bar_ts
        if bar_ts != grp_ts:
            if group: bars.append(_agg(group, grp_ts))
            group, grp_ts = [row], bar_ts
        else:
            group.append(row)
    if group and grp_ts is not None:
        bars.append(_agg(group, grp_ts))
    return bars


def load_historical(cfg: dict, buffer: CandleBuffer, n: int = 80):
    enc     = url_encode(cfg["futures_key"], safe="")
    to_dt   = datetime.date.today()
    from_dt = to_dt - datetime.timedelta(days=7)
    url     = (f"{cfg['api_base']}/historical-candle/{enc}/1minute/"
               f"{to_dt.isoformat()}/{from_dt.isoformat()}")
    hdrs    = {"Accept": "application/json",
               "Authorization": f"Bearer {cfg['access_token']}"}
    log.info("Historical URL: %s", url)
    try:
        r = requests.get(url, headers=hdrs, timeout=20)
        log.debug("HTTP %d  %.300s", r.status_code, r.text)
        data = r.json()
    except Exception as e:
        log.error("Historical fetch: %s", e); return
    if data.get("status") != "success":
        log.error("Historical error: %s", data.get("errors", data)); return
    raw = list(reversed(data.get("data", {}).get("candles", [])))
    log.info("1-min bars received: %d", len(raw))
    bars = _resample(raw)
    log.info("Resampled to 5-min bars: %d", len(bars))
    for b in bars[-n:]:
        buffer.push_bar(b)
    log.info("Buffer loaded: %d bars", buffer.bar_count())


# =============================================================
#  SECTION 5 — INDICATORS
# =============================================================

def _ema(s: np.ndarray, p: int) -> np.ndarray:
    k = 2.0 / (p + 1)
    o = np.empty_like(s, dtype=float); o[0] = s[0]
    for i in range(1, len(s)):
        o[i] = s[i] * k + o[i-1] * (1-k)
    return o


def _vwap(c, h, l, v) -> np.ndarray:
    hlc3 = (h + l + c) / 3
    cv   = np.cumsum(v)
    return np.cumsum(hlc3 * v) / np.where(cv == 0, 1.0, cv)


def compute_indicators(bars: list) -> Optional[dict]:
    if len(bars) < 22: return None
    c = np.array([b.close  for b in bars], float)
    h = np.array([b.high   for b in bars], float)
    l = np.array([b.low    for b in bars], float)
    v = np.array([b.volume for b in bars], float)
    o = np.array([b.oi     for b in bars], float)
    d = np.array([b.delta  for b in bars], float)
    return dict(
        close=c[-1], close_p1=c[-2],
        ema7=_ema(c,7)[-1], ema21=_ema(c,21)[-1],
        vwap=_vwap(c,h,l,v)[-1],
        d_sum=d[-1]+d[-2],
        price_chg=c[-1]-c[-2], oi_chg=o[-1]-o[-2],
    )


# =============================================================
#  SECTION 6 — SIGNAL ENGINE
# =============================================================

class SignalEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.in_call_c1=self.in_call_c2=self.in_put_c3=self.in_put_c4=False

    def evaluate(self, ind) -> dict:
        c, p1       = ind["close"], ind["close_p1"]
        e7,e21,vw   = ind["ema7"], ind["ema21"], ind["vwap"]
        ds, cfg     = ind["d_sum"], self.cfg
        bull = ind["price_chg"] > 0
        bear = ind["price_chg"] < 0

        hard_blk = (cfg["upper_limit"]>0 and c>=cfg["upper_limit"]) or \
                   (cfg["lower_limit"]>0 and c<=cfg["lower_limit"])
        ntz = cfg["ntz_upper"]>cfg["ntz_lower"]>0 and cfg["ntz_lower"]<=c<=cfg["ntz_upper"]

        buf = cfg["level_buffer"]
        near_res = any(lv>0 and (abs(c-lv)<=buf or c>=lv)
                       for lv in (cfg["r1"], cfg["r2"]))
        near_sup = any(lv>0 and (abs(c-lv)<=buf or c<=lv)
                       for lv in (cfg["s1"], cfg["s2"]))
        call_ok = (not near_res or ds>cfg["delta_ovrd"]) and not hard_blk
        put_ok  = (not near_sup or ds<-cfg["delta_ovrd"]) and not hard_blk

        dm = cfg["delta_min"]
        c1 = c>e7 and c>e21 and c>vw  and ds>dm  and bull
        c2 = c>e7 and c>e21 and c<vw  and ds>dm  and bull
        c3 = c<e7 and c<e21 and c<vw  and ds<-dm and bear
        c4 = c<e7 and c<e21 and c>vw  and ds<-dm and bear

        buy_call = (c1 or c2) and not ntz and call_ok
        buy_put  = (c3 or c4) and not ntz and put_ok

        # EXIT reads PREVIOUS flags first (core v5 ordering fix)
        ec1 = self.in_call_c1 and c < p1
        ec2 = self.in_call_c2 and (c < p1 or c <= vw)
        ec3 = self.in_put_c3  and c > p1
        ec4 = self.in_put_c4  and (c > p1 or c >= vw)
        exit_call = ec1 or ec2
        exit_put  = ec3 or ec4

        if exit_call: self.in_call_c1=self.in_call_c2=False
        if exit_put:  self.in_put_c3=self.in_put_c4=False
        if buy_call:
            self.in_put_c3=self.in_put_c4=False
            self.in_call_c1=c1; self.in_call_c2=c2
        if buy_put:
            self.in_call_c1=self.in_call_c2=False
            self.in_put_c3=c3; self.in_put_c4=c4

        return dict(buy_call=buy_call, buy_put=buy_put,
                    exit_call=exit_call, exit_put=exit_put,
                    in_call=self.in_call_c1 or self.in_call_c2,
                    in_put=self.in_put_c3 or self.in_put_c4,
                    ntz=ntz, hard_blk=hard_blk,
                    near_res=near_res, near_sup=near_sup, ds=ds)


# =============================================================
#  SECTION 7 — BROKER
# =============================================================

class UpstoxBroker:
    def __init__(self, cfg, tg: Telegram):
        self.cfg=cfg; self.tg=tg
        self._h = {"Accept":"application/json",
                   "Content-Type":"application/json",
                   "Authorization":f"Bearer {cfg['access_token']}"}
        self._side=None; self.pnl=0.0; self._blocked=False

    def _place(self, key, txn, qty, tag) -> bool:
        if not self.cfg["enable_live"]:
            log.info("PAPER  %s  %s  qty=%d", txn, tag, qty)
            self.tg.send(f"PAPER {txn} {tag} qty={qty}")
            return True
        payload = dict(quantity=qty, product=self.cfg["product_type"],
                       validity="DAY", price=0, tag=tag,
                       instrument_token=key, order_type="MARKET",
                       transaction_type=txn, disclosed_quantity=0,
                       trigger_price=0, is_amo=False)
        url = f"{self.cfg['api_base']}/order/place"
        for n in range(1, self.cfg["order_retry"]+1):
            try:
                r = requests.post(url, headers=self._h,
                                  data=json.dumps(payload), timeout=10)
                d = r.json()
                if r.status_code==200 and d.get("status")=="success":
                    msg = f"ORDER {txn} {tag} qty={qty} id={d['data']['order_id']}"
                    log.info(msg); self.tg.send(msg); return True
                log.error("Order attempt %d: %s", n, d)
            except Exception as e:
                log.error("Order attempt %d exc: %s", n, e)
            time.sleep(1)
        self.tg.send(f"ORDER FAILED {tag}"); return False

    def _loss_ok(self) -> bool:
        ml = self.cfg["max_daily_loss"]
        if ml>0 and self.pnl<=-ml and not self._blocked:
            log.warning("Max loss Rs%.0f hit", ml)
            self.tg.send(f"Max loss Rs{ml:.0f} hit - trading stopped")
            self._blocked = True
        return self._blocked

    def enter_call(self):
        if self._loss_ok() or self._side=="CALL": return
        if self._place(self.cfg["call_key"],"BUY",self.cfg["qty_call"],"SOSS_BUY_CALL"):
            self._side="CALL"

    def enter_put(self):
        if self._loss_ok() or self._side=="PUT": return
        if self._place(self.cfg["put_key"],"BUY",self.cfg["qty_put"],"SOSS_BUY_PUT"):
            self._side="PUT"

    def exit_call(self):
        if self._side!="CALL": return
        if self._place(self.cfg["call_key"],"SELL",self.cfg["qty_call"],"SOSS_EXIT_CALL"):
            self._side=None

    def exit_put(self):
        if self._side!="PUT": return
        if self._place(self.cfg["put_key"],"SELL",self.cfg["qty_put"],"SOSS_EXIT_PUT"):
            self._side=None

    def square_off_all(self, reason="EOD"):
        msg = f"Square-off reason={reason} PnL=Rs{self.pnl:.0f}"
        log.info(msg); self.tg.send(msg)
        self.exit_call(); self.exit_put()

    @property
    def side(self): return self._side


# =============================================================
#  SECTION 8 — MARKET FEED via MarketDataStreamerV3
#
#  The SDK handles:
#    • WS auth (v3 endpoint)
#    • Protobuf decoding
#    • Auto-reconnect
#  No manual pb2 import needed.
#
#  message dict structure (full mode):
#    feeds -> <instrument_key> -> ff -> marketFf -> ltpc/oi/vtt
# =============================================================

def _safe_float(v, default=0.0) -> float:
    try: return float(v or default)
    except Exception: return default


class MarketFeed:
    def __init__(self, cfg, buffer: CandleBuffer):
        self.cfg       = cfg
        self.buffer    = buffer
        self._streamer = None
        self._connected = threading.Event()
        self._prev_vtt = 0.0   # track cumulative volume to get per-tick delta

    def _on_open(self):
        self._connected.set()
        log.info("MarketDataStreamerV3 connected  key=%s", self.cfg["futures_key"])

    def _on_message(self, message):
        """
        message is already decoded by the SDK (dict from MessageToDict).
        Proto path (full mode):
          feeds -> key -> ff -> marketFf -> ltpc.ltp / oi / vtt
        """
        try:
            feeds = message.get("feeds", {}) if isinstance(message, dict) else {}
            feed  = feeds.get(self.cfg["futures_key"])
            if feed is None: return

            ff  = feed.get("ff", {})
            mff = ff.get("marketFf", {})       # camelCase of market_ff

            ltp  = _safe_float(mff.get("ltpc", {}).get("ltp"))
            oi   = _safe_float(mff.get("oi"))
            vtt  = _safe_float(mff.get("vtt")) # cumulative daily volume

            if ltp == 0: return

            # per-tick volume = change in cumulative vtt
            vol_tick = max(0.0, vtt - self._prev_vtt) if vtt > 0 else 0.0
            self._prev_vtt = vtt

            self.buffer.on_tick(ltp, vol_tick, oi, datetime.datetime.now())

        except Exception as exc:
            log.debug("on_message parse error: %s  msg=%s", exc,
                      str(message)[:120])

    def _on_error(self, error):
        log.error("Streamer error: %s", error)

    def _on_close(self, *args):
        # SDK calls close handler with (close_status_code, close_msg) args
        self._connected.clear()
        log.warning("Streamer disconnected  args=%s", args)

    def connect(self):
        config = upstox_client.Configuration()
        config.access_token = self.cfg["access_token"]

        self._streamer = upstox_client.MarketDataStreamerV3(
            upstox_client.ApiClient(config),
            [self.cfg["futures_key"]],
            "full",
        )
        self._streamer.on("open",    self._on_open)
        self._streamer.on("message", self._on_message)
        self._streamer.on("error",   self._on_error)
        self._streamer.on("close",   self._on_close)

        # run in daemon thread so it doesn't block the main loop
        t = threading.Thread(target=self._streamer.connect, daemon=True)
        t.start()
        log.info("MarketDataStreamerV3 thread started.")

    def wait_connected(self, timeout=25.0) -> bool:
        return self._connected.wait(timeout=timeout)

    def stop(self):
        if self._streamer:
            try: self._streamer.disconnect()
            except Exception: pass


# =============================================================
#  SECTION 9 — MAIN LOOP
# =============================================================

def squareoff_reached(hhmm: str) -> bool:
    h, m = map(int, hhmm.strip().split(":"))
    now  = datetime.datetime.now()
    return now >= now.replace(hour=h, minute=m, second=0, microsecond=0)


def market_open() -> bool:
    now   = datetime.datetime.now()
    start = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    end   = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return start <= now <= end and now.weekday() < 5


def main():
    cfg    = load_config("config.ini")
    tg     = Telegram(cfg)
    buffer = CandleBuffer()
    broker = UpstoxBroker(cfg, tg)
    engine = SignalEngine(cfg)

    log.info("=" * 60)
    log.info("  SOSS_1005_v9  —  %s",
             "LIVE ORDERS" if cfg["enable_live"] else "PAPER TRADE")
    log.info("=" * 60)
    tg.send("SOSS v8 started  mode=" + ("LIVE" if cfg["enable_live"] else "PAPER"))

    # ── Historical warm-up ────────────────────────────────────
    load_historical(cfg, buffer, n=80)
    if buffer.bar_count() < 5:
        log.warning("Only %d historical bars — EMA stabilises after 22 live bars.",
                    buffer.bar_count())

    # ── Live feed ─────────────────────────────────────────────
    feed = MarketFeed(cfg, buffer)
    feed.connect()

    log.info("Waiting for WebSocket connection ...")
    if not feed.wait_connected(timeout=25):
        log.critical("WebSocket did not connect in 25 s. "
                     "Check token freshness and network.")
        sys.exit(1)

    log.info("Signal loop running ...")
    last_bar_ts = None

    # ── After-hours guard: skip squareoff check until market opens ────
    _waited_for_market = False

    try:
        while True:
            if not market_open():
                if not _waited_for_market:
                    log.info("Market closed — waiting for next session (09:15). "
                             "Script will start trading automatically.")
                    _waited_for_market = True
                time.sleep(30); continue
            _waited_for_market = False   # reset once market is open

            if squareoff_reached(cfg["squareoff_hhmm"]):
                broker.square_off_all("Auto EOD"); break

            bars = buffer.snapshot()
            if len(bars) < 2:
                time.sleep(cfg["loop_interval"]); continue

            latest = bars[-1]
            if latest.ts == last_bar_ts:
                time.sleep(cfg["loop_interval"]); continue

            last_bar_ts = latest.ts
            ind = compute_indicators(bars)
            if ind is None:
                log.info("Warming up ... bars=%d / 22 needed", len(bars))
                time.sleep(cfg["loop_interval"]); continue

            sig = engine.evaluate(ind)
            log.info(
                "BAR %s | C=%.2f E7=%.2f E21=%.2f VWAP=%.2f dSum=%.0f"
                " | NTZ=%s BLK=%s NR=%s NS=%s"
                " | BC=%s BP=%s EC=%s EP=%s  IN=%s/%s",
                latest.ts.strftime("%H:%M"),
                ind["close"], ind["ema7"], ind["ema21"],
                ind["vwap"], ind["d_sum"],
                sig["ntz"], sig["hard_blk"], sig["near_res"], sig["near_sup"],
                sig["buy_call"], sig["buy_put"],
                sig["exit_call"], sig["exit_put"],
                sig["in_call"], sig["in_put"],
            )

            if sig["exit_call"]: broker.exit_call()
            if sig["exit_put"]:  broker.exit_put()
            if sig["buy_call"]:  broker.enter_call()
            if sig["buy_put"]:   broker.enter_put()

            time.sleep(cfg["loop_interval"])

    except KeyboardInterrupt:
        log.info("Stopped by user.")
        broker.square_off_all("Manual stop")
    finally:
        feed.stop()
        log.info("Session PnL: Rs%.0f", broker.pnl)
        tg.send(f"SOSS stopped  PnL=Rs{broker.pnl:.0f}")


if __name__ == "__main__":
    main()