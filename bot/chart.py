"""
chart.py  --  signal photo + result photo (Telegram). Matplotlib only (Agg backend, headless-safe).
Both functions take plain dicts so MTG signals and restored signals render identically.
"""
import os
from typing import Dict, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import Rectangle

BG, FG, GRID = "#0e1621", "#d7dde5", "#1e2a38"
UP, DOWN = "#26a69a", "#ef5350"
WINC, LOSSC, DRAWC = "#2ecc71", "#e74c3c", "#f1c40f"


def _ensure(path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


def _draw_candles(ax, df: pd.DataFrame):
    for i, (_, r) in enumerate(df.iterrows()):
        col = UP if r["close"] >= r["open"] else DOWN
        ax.vlines(i, r["low"], r["high"], color=col, linewidth=1.2, zorder=3)
        lo, hi = sorted((r["open"], r["close"]))
        ax.add_patch(Rectangle((i - 0.32, lo), 0.64, max(hi - lo, 1e-9), facecolor=col, edgecolor=col, zorder=4))


def _style(ax, df: pd.DataFrame, tz_hours: int):
    ax.set_facecolor(BG)
    ax.grid(color=GRID, linewidth=0.6)
    for sp in ax.spines.values():
        sp.set_color(GRID)
    ax.tick_params(colors=FG, labelsize=9)
    step = max(1, len(df) // 8)
    ticks = list(range(0, len(df), step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([(df.index[i] + pd.Timedelta(hours=tz_hours)).strftime("%H:%M") for i in ticks])
    ax.set_xlim(-1, len(df) + 6)
    lo, hi = df["low"].min(), df["high"].max()
    pad = (hi - lo) * 0.12
    ax.set_ylim(lo - pad, hi + pad)
    ax.yaxis.tick_right()


def _pos(df: pd.DataFrame, iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        t = pd.Timestamp(iso)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        i = df.index.get_indexer([t])[0]
        return int(i) if i >= 0 else None
    except Exception:
        return None


def render_signal_chart(m5: pd.DataFrame, info: Dict, path: str, tz_hours: int = 6, bars: int = 40) -> str:
    """info keys: asset, direction, score, step, expiry_text, entry_price, viz{zone,ob,ob_time,fvg,fvg_time,sweep_level,sweep_time}"""
    _ensure(path)
    df = m5.iloc[-bars:]
    fig, ax = plt.subplots(figsize=(10.8, 7.2), dpi=100, facecolor=BG)
    _draw_candles(ax, df)
    _style(ax, df, tz_hours)
    viz = info.get("viz") or {}
    right = len(df) + 5
    ob, fvg = viz.get("ob"), viz.get("fvg")
    if ob:
        x0 = _pos(df, viz.get("ob_time")) or 0
        ax.add_patch(Rectangle((x0, ob[0]), right - x0, ob[1] - ob[0], facecolor="#3b82f6", alpha=0.22, edgecolor="#3b82f6", zorder=1))
        ax.text(right - 0.3, ob[1], "OB", color="#7db3ff", fontsize=9, ha="right", va="bottom")
    if fvg:
        x0 = _pos(df, viz.get("fvg_time")) or 0
        ax.add_patch(Rectangle((x0, fvg[0]), right - x0, fvg[1] - fvg[0], facecolor="#a855f7", alpha=0.25, edgecolor="#a855f7", zorder=1))
        ax.text(right - 0.3, fvg[1], "FVG", color="#d2a8ff", fontsize=9, ha="right", va="bottom")
    if viz.get("sweep_level"):
        ax.axhline(viz["sweep_level"], color="#f59e0b", linestyle="--", linewidth=1.1, zorder=2)
        ax.text(0, viz["sweep_level"], " liquidity swept", color="#f59e0b", fontsize=9, va="bottom")
    buy = info["direction"] == "BUY"
    color = WINC if buy else LOSSC
    last = len(df) - 1
    y = df["low"].iloc[-1] if buy else df["high"].iloc[-1]
    off = (df["high"].max() - df["low"].min()) * 0.06
    ax.annotate("", xy=(last, y - off * 0.2 if buy else y + off * 0.2), xytext=(last, y - off * 1.6 if buy else y + off * 1.6),
                arrowprops=dict(arrowstyle="-|>", color=color, lw=3), zorder=6)
    ax.axhline(info["entry_price"], color=FG, linestyle=":", linewidth=0.9, zorder=2)
    tag = f"MTG{info['step']} " if info.get("step") else ""
    side = "CALL (BUY)" if buy else "PUT (SELL)"
    ax.set_title(f"{tag}{side}  |  {info['asset']}  M{info.get('tf_seconds', 300) // 60}  |  Score {info.get('score', '-')}  |  {info.get('expiry_text', '')}",
                 color=color, fontsize=15, fontweight="bold", loc="left", pad=14)
    fig.tight_layout()
    fig.savefig(path, facecolor=BG)
    plt.close(fig)
    return path


def render_result_chart(m5: pd.DataFrame, info: Dict, exit_price: float, outcome: str, pnl: float, path: str,
                        tz_hours: int = 6, bars: int = 40) -> str:
    """m5 must end with the expiry candle. info: asset, direction, entry_price, entry_time, step."""
    _ensure(path)
    df = m5.iloc[-bars:]
    fig, ax = plt.subplots(figsize=(10.8, 7.2), dpi=100, facecolor=BG)
    _draw_candles(ax, df)
    _style(ax, df, tz_hours)
    col = {"WIN": WINC, "LOSS": LOSSC}.get(outcome, DRAWC)
    ax.axhline(info["entry_price"], color=FG, linestyle="--", linewidth=1.2, zorder=2)
    ax.text(0, info["entry_price"], " ENTRY", color=FG, fontsize=9, va="bottom")
    last = len(df) - 1
    ax.scatter([last], [exit_price], s=140, color=col, zorder=7, edgecolors="white")
    ax.axhline(exit_price, color=col, linestyle=":", linewidth=1.2, zorder=2)
    ep = _pos(df, str(pd.Timestamp(info["entry_time"]) - pd.Timedelta(seconds=info.get("tf_seconds", 300))))
    if ep is not None:
        ax.axvline(ep + 0.5, color=FG, linewidth=0.8, linestyle=":")
    money = f"+${pnl:.2f}" if pnl > 0 else (f"-${abs(pnl):.2f}" if pnl < 0 else "$0.00")
    tag = f"MTG{info['step']} " if info.get("step") else ""
    ax.set_title(f"{tag}{outcome}   {money}", color=col, fontsize=26, fontweight="bold", loc="left", pad=16)
    ax.text(0.99, 0.97, f"{info['asset']}  {'CALL' if info['direction'] == 'BUY' else 'PUT'}\nentry {info['entry_price']:.5f}\nexit  {exit_price:.5f}",
            transform=ax.transAxes, color=FG, fontsize=11, ha="right", va="top", family="monospace")
    fig.tight_layout()
    fig.savefig(path, facecolor=BG)
    plt.close(fig)
    return path


def cleanup_old(directory: str, keep: int = 60):
    try:
        files = sorted((os.path.join(directory, f) for f in os.listdir(directory)), key=os.path.getmtime)
        for f in files[:-keep]:
            os.remove(f)
    except Exception:
        pass
