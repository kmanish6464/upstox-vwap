"""
NSE OI Change — Live Plotter
==============================
Reads from MongoDB (nse_oi_db.oi_summary) and renders a live-updating
OI Change chart (CE vs PE) for the nearest expiry, auto-refreshing every 10 sec.

Requirements:
    pip install pymongo matplotlib pandas

Run fetcher.py first, then run this in a separate terminal:
    python nse_oi_plotter.py
"""

import time
import logging
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from datetime import datetime, timezone, timedelta
from pymongo import MongoClient, DESCENDING

# ──────────────────────────────────────────────
# CONFIG  (must match fetcher config)
# ──────────────────────────────────────────────
SYMBOL          = "NIFTY"
MONGO_URI       = "mongodb://localhost:27017/"
DB_NAME         = "nse_oi_db"
REFRESH_SEC     = 10           # chart refresh interval (seconds)

# Number of strikes to show on each side of ATM
STRIKES_EACH_SIDE = 10         # shows 21 strikes total (ATM ± 10)

# OI threshold bar — shade strikes with net OI change > this
OI_HIGHLIGHT_THRESHOLD = 500_000
# ──────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

client = MongoClient(MONGO_URI)
col    = client[DB_NAME]["oi_summary"]


def get_latest_snapshot() -> pd.DataFrame | None:
    """Fetch the most recent timestamp's data for SYMBOL."""
    latest_doc = col.find_one(
        {"symbol": SYMBOL},
        sort=[("timestamp", DESCENDING)],
    )
    if not latest_doc:
        log.warning("No data in MongoDB yet — is the fetcher running?")
        return None

    latest_ts = latest_doc["timestamp"]
    # Fetch all rows from the same timestamp
    rows = list(col.find(
        {"symbol": SYMBOL, "timestamp": latest_ts},
        {"_id": 0, "strike": 1, "ce_oi_change": 1, "pe_oi_change": 1,
         "ce_oi": 1, "pe_oi": 1, "underlying_value": 1, "expiry": 1}
    ))
    if not rows:
        return None

    df = pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)
    df["net_oi_change"] = df["ce_oi_change"] - df["pe_oi_change"]
    return df, latest_ts


def get_oi_change_over_time(strike: float) -> pd.DataFrame:
    """Return time-series OI change for a specific strike (for sparklines)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    rows = list(col.find(
        {"symbol": SYMBOL, "strike": strike, "timestamp": {"$gte": cutoff}},
        {"_id": 0, "timestamp": 1, "ce_oi_change": 1, "pe_oi_change": 1}
    ).sort("timestamp", 1))
    return pd.DataFrame(rows)


def atm_strike(df: pd.DataFrame) -> float:
    """Find ATM (closest strike to underlying)."""
    spot = df["underlying_value"].iloc[0]
    return df.loc[(df["strike"] - spot).abs().idxmin(), "strike"]


# ── Plot setup ─────────────────────────────────
plt.style.use("dark_background")
fig = plt.figure(figsize=(18, 10), facecolor="#0d0d0d")
fig.suptitle(
    f"NSE {SYMBOL}  —  OI Change Monitor",
    fontsize=16, fontweight="bold", color="#e0e0e0", y=0.98,
)

gs       = fig.add_gridspec(2, 2, hspace=0.45, wspace=0.35)
ax_bar   = fig.add_subplot(gs[0, :])   # OI Change bar chart (full width)
ax_ce_ts = fig.add_subplot(gs[1, 0])   # CE OI time-series
ax_pe_ts = fig.add_subplot(gs[1, 1])   # PE OI time-series

COLORS = {
    "ce":       "#00c8ff",   # cyan-blue for calls
    "pe":       "#ff4c6a",   # pink-red for puts
    "atm":      "#ffd700",   # gold ATM line
    "bg":       "#0d0d0d",
    "grid":     "#2a2a2a",
    "text":     "#e0e0e0",
    "positive": "#00e676",
    "negative": "#ff5252",
}

info_text = fig.text(
    0.5, 0.935,
    "Waiting for data …",
    ha="center", va="center", fontsize=10, color="#aaaaaa",
)


def style_ax(ax, title: str):
    ax.set_facecolor(COLORS["bg"])
    ax.set_title(title, color=COLORS["text"], fontsize=11, pad=8)
    ax.tick_params(colors=COLORS["text"], labelsize=8)
    ax.spines[:].set_color(COLORS["grid"])
    ax.grid(axis="y", color=COLORS["grid"], linewidth=0.5, linestyle="--")
    ax.xaxis.label.set_color(COLORS["text"])
    ax.yaxis.label.set_color(COLORS["text"])


def format_oi(val, _):
    """Format OI axis labels as K / L / Cr."""
    if abs(val) >= 1_00_00_000:
        return f"{val/1_00_00_000:.1f}Cr"
    if abs(val) >= 1_00_000:
        return f"{val/1_00_000:.1f}L"
    if abs(val) >= 1_000:
        return f"{val/1_000:.0f}K"
    return str(int(val))


def draw(frame):
    result = get_latest_snapshot()
    if result is None:
        return

    df, ts = result
    spot   = df["underlying_value"].iloc[0]
    atm    = atm_strike(df)
    expiry = df["expiry"].iloc[0] if "expiry" in df.columns else "N/A"

    # Slice ± N strikes around ATM
    atm_idx  = df.index[df["strike"] == atm][0]
    lo       = max(0, atm_idx - STRIKES_EACH_SIDE)
    hi       = min(len(df) - 1, atm_idx + STRIKES_EACH_SIDE)
    df_slice = df.iloc[lo: hi + 1].copy()
    strikes  = df_slice["strike"].astype(int).tolist()

    # ── Bar chart: CE vs PE OI Change ──────────
    ax_bar.clear()
    style_ax(ax_bar, f"OI Change — CE (Blue) vs PE (Red)  |  Expiry: {expiry}  |  Spot: {spot:,.0f}")

    x      = range(len(strikes))
    width  = 0.38
    bars_ce = ax_bar.bar(
        [i - width / 2 for i in x],
        df_slice["ce_oi_change"],
        width=width,
        color=COLORS["ce"],
        label="CE OI Δ",
        alpha=0.85,
        zorder=3,
    )
    bars_pe = ax_bar.bar(
        [i + width / 2 for i in x],
        df_slice["pe_oi_change"],
        width=width,
        color=COLORS["pe"],
        label="PE OI Δ",
        alpha=0.85,
        zorder=3,
    )

    # Highlight ATM strike
    atm_x = strikes.index(int(atm))
    ax_bar.axvline(atm_x, color=COLORS["atm"], linewidth=1.5, linestyle="--", zorder=4, label=f"ATM {int(atm)}")

    # Highlight high OI change strikes
    for i, row in enumerate(df_slice.itertuples()):
        if abs(row.ce_oi_change) > OI_HIGHLIGHT_THRESHOLD or abs(row.pe_oi_change) > OI_HIGHLIGHT_THRESHOLD:
            ax_bar.axvspan(i - 0.5, i + 0.5, color="#ffffff", alpha=0.05, zorder=1)

    ax_bar.set_xticks(list(x))
    ax_bar.set_xticklabels(strikes, rotation=45, ha="right", fontsize=7.5)
    ax_bar.yaxis.set_major_formatter(mticker.FuncFormatter(format_oi))
    ax_bar.axhline(0, color=COLORS["grid"], linewidth=0.8)
    ax_bar.legend(loc="upper right", fontsize=9, framealpha=0.3)

    # PCR annotation
    total_ce_oi = df_slice["ce_oi"].sum()
    total_pe_oi = df_slice["pe_oi"].sum()
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi else 0
    ax_bar.text(
        0.01, 0.95,
        f"PCR (OI): {pcr}  |  CE OI: {format_oi(total_ce_oi, None)}  |  PE OI: {format_oi(total_pe_oi, None)}",
        transform=ax_bar.transAxes,
        fontsize=9, color=COLORS["text"],
        va="top", bbox=dict(facecolor="#1a1a1a", alpha=0.7, boxstyle="round,pad=0.3"),
    )

    # ── Time-series: CE OI change over time ────
    atm_ts = get_oi_change_over_time(atm)
    ax_ce_ts.clear()
    style_ax(ax_ce_ts, f"CE OI Change Over Time  |  ATM {int(atm)}")
    if not atm_ts.empty:
        ax_ce_ts.plot(
            atm_ts["timestamp"], atm_ts["ce_oi_change"],
            color=COLORS["ce"], linewidth=1.8, marker="o", markersize=3,
        )
        ax_ce_ts.fill_between(
            atm_ts["timestamp"], atm_ts["ce_oi_change"],
            alpha=0.15, color=COLORS["ce"],
        )
        ax_ce_ts.yaxis.set_major_formatter(mticker.FuncFormatter(format_oi))
        ax_ce_ts.tick_params(axis="x", rotation=30)
    else:
        ax_ce_ts.text(0.5, 0.5, "Accumulating …", ha="center", va="center",
                      transform=ax_ce_ts.transAxes, color="#666666")

    # ── Time-series: PE OI change over time ────
    ax_pe_ts.clear()
    style_ax(ax_pe_ts, f"PE OI Change Over Time  |  ATM {int(atm)}")
    if not atm_ts.empty:
        ax_pe_ts.plot(
            atm_ts["timestamp"], atm_ts["pe_oi_change"],
            color=COLORS["pe"], linewidth=1.8, marker="o", markersize=3,
        )
        ax_pe_ts.fill_between(
            atm_ts["timestamp"], atm_ts["pe_oi_change"],
            alpha=0.15, color=COLORS["pe"],
        )
        ax_pe_ts.yaxis.set_major_formatter(mticker.FuncFormatter(format_oi))
        ax_pe_ts.tick_params(axis="x", rotation=30)
    else:
        ax_pe_ts.text(0.5, 0.5, "Accumulating …", ha="center", va="center",
                      transform=ax_pe_ts.transAxes, color="#666666")

    info_text.set_text(
        f"Last fetch: {ts.strftime('%d-%b-%Y %H:%M:%S UTC')}  |  "
        f"Auto-refresh every {REFRESH_SEC}s  |  Strikes shown: ±{STRIKES_EACH_SIDE} from ATM"
    )

    fig.canvas.draw_idle()
    log.info("📊  Chart updated  |  ATM: %s  |  PCR: %s", int(atm), pcr)


# ── Run ─────────────────────────────────────────
if __name__ == "__main__":
    log.info("🖥️   OI Plotter starting  |  Symbol: %s  |  Refresh: %ss", SYMBOL, REFRESH_SEC)
    log.info("   Make sure nse_oi_fetcher.py is running in a separate terminal!")

    from matplotlib.animation import FuncAnimation
    ani = FuncAnimation(fig, draw, interval=REFRESH_SEC * 1000, cache_frame_data=False)

    plt.show()
    client.close()
