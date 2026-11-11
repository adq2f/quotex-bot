"""
backtest.py  --  Rulebook Section 12 validation.
Usage:
  python backtest.py --csv data/EURUSD_M5.csv --asset EURUSD --payout 85 [--mtg]
CSV columns: time,open,high,low,close[,volume]  (M5, UTC; TradingView/Binance/broker export)

What it does: replays the SAME strategy + risk manager on history, one closed candle at a time, then reports
win rate, profit factor, max drawdown, per-trade Sharpe, avg duration, longest losing streak, and the 60/30
walk-forward check (12.5). Acceptance criteria (12.4) are printed PASS/FAIL.

LIMITS (read them): no historical news calendar (news filter is OFF), real tick volume is usually absent
(range proxy is used), and OTC prices are broker-generated so a real-market CSV is only a rough proxy (12.7).
Passing here is NECESSARY, not sufficient: you still need 2 weeks of DEMO forward-testing (12.6).
"""
import argparse
import dataclasses
import math
from collections import Counter
from datetime import timedelta

import numpy as np
import pandas as pd

from bot.config import settings
from bot.datafeed import load_csv, resample_ohlc
from bot.risk import RiskManager
from bot.strategy import Strategy
from bot.tracker import outcome_for, pnl_for


def run_backtest(m5: pd.DataFrame, asset: str, cfg, payout: float = 85.0, mtg: bool = False, start_idx: int = 300):
    tf = cfg.entry_tf_seconds
    h1_all, h4_all = resample_ohlc(m5, "1h"), resample_ohlc(m5, "4h")
    h1_close, h4_close = (h1_all.index + pd.Timedelta(hours=1)), (h4_all.index + pd.Timedelta(hours=4))
    strat = Strategy(dataclasses.replace(cfg, whitelist=(asset,)), news=None)
    cfg = dataclasses.replace(cfg, whitelist=(asset,), mtg_enabled=mtg)
    risk = RiskManager(cfg, store=None)
    trades, diag = [], Counter()
    i, n, exp = start_idx, len(m5), cfg.expiry_candles

    def frames(i):
        now = (m5.index[i] + pd.Timedelta(seconds=tf))
        k1, k4 = h1_close.searchsorted(now, side="right"), h4_close.searchsorted(now, side="right")
        return m5.iloc[max(0, i - 299): i + 1], h1_all.iloc[max(0, k1 - 300):k1], h4_all.iloc[max(0, k4 - 300):k4], now.to_pydatetime()

    while i < n - exp - 1:
        m, h1, h4, now = frames(i)
        d = strat.evaluate(asset, m, h1, h4, now)
        diag[d.gate or d.decision] += 1
        if d.decision == "HOLD":
            i += 1
            continue
        ok, _ = risk.can_open(asset, d.decision, now, payout, 0)
        if not ok:
            diag["risk_block"] += 1
            i += 1
            continue
        stake = risk.base_stake(asset, payout)
        base = stake
        risk.register_signal(asset, d.decision, now)
        step, j = 0, i
        while True:
            entry, exit_ = float(m5["close"].iloc[j]), float(m5["close"].iloc[j + exp])
            out = outcome_for(d.decision, entry, exit_)
            pnl = pnl_for(out, stake, payout)
            t_exit = (m5.index[j + exp] + pd.Timedelta(seconds=tf)).to_pydatetime()
            bal_before = risk.balance
            risk.record_result(asset, out, pnl, t_exit)
            trades.append({"time": m5.index[i], "step": step, "outcome": out, "pnl": pnl, "ret": pnl / bal_before, "balance": risk.balance})
            j += exp
            if out == "LOSS" and mtg and step < cfg.mtg_max_steps and j + exp < n:
                mm, hh1, hh4, now2 = frames(j)
                okm, _ = strat.mtg_precheck(asset, d.decision, mm, hh1, hh4, now2)
                ok2, _ = risk.can_open(asset, d.decision, now2, payout, 0, is_mtg=True)
                ms = risk.mtg_stake(base, payout)
                if okm and ok2 and ms:
                    stake, step = ms, step + 1
                    continue
            break
        i = j + 1
    return trades, diag


def metrics(trades, start_balance):
    if not trades:
        return {"trades": 0}
    pnl = np.array([t["pnl"] for t in trades])
    outs = [t["outcome"] for t in trades]
    w, l = outs.count("WIN"), outs.count("LOSS")
    gp, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    bal = np.array([start_balance] + [t["balance"] for t in trades])
    peak = np.maximum.accumulate(bal)
    dd = float(((peak - bal) / peak).max() * 100)
    streak = cur = 0
    for o in outs:
        cur = cur + 1 if o == "LOSS" else 0
        streak = max(streak, cur)
    rets = np.array([t["ret"] for t in trades])
    sharpe = float(rets.mean() / rets.std() * math.sqrt(len(rets))) if rets.std() > 0 else 0.0
    return {"trades": len(trades), "win_rate": round(100 * w / (w + l), 1) if (w + l) else 0.0,
            "profit_factor": round(gp / gl, 2) if gl else float("inf"), "max_dd_pct": round(dd, 1),
            "sharpe_per_trade_scaled": round(sharpe, 2), "avg_duration_min": cfg_duration(), "longest_loss_streak": streak,
            "net_pnl": round(float(pnl.sum()), 2), "mtg_trades": sum(1 for t in trades if t["step"] > 0)}


def cfg_duration():
    return settings.expiry_candles * settings.entry_tf_seconds / 60


def verdict(m, min_trades=500):
    if m.get("trades", 0) == 0:
        return "NO TRADES"
    checks = {
        f"trades >= {min_trades} (12.2)": m["trades"] >= min_trades,
        "win rate >= 65% (12.4)": m["win_rate"] >= 65,
        "profit factor >= 1.5": m["profit_factor"] >= 1.5,
        "max drawdown <= 15%": m["max_dd_pct"] <= 15,
        "longest losing streak <= 5": m["longest_loss_streak"] <= 5,
    }
    return checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--asset", required=True)
    ap.add_argument("--payout", type=float, default=85.0)
    ap.add_argument("--mtg", action="store_true")
    ap.add_argument("--indicator-window", type=int, default=None, help="override INDICATOR_WINDOW for experiments")
    a = ap.parse_args()
    cfg = settings if a.indicator_window is None else dataclasses.replace(settings, indicator_window=a.indicator_window)
    m5 = load_csv(a.csv)
    span_days = (m5.index[-1] - m5.index[0]).days
    print(f"data: {len(m5)} candles, {span_days} days  ({m5.index[0]} -> {m5.index[-1]})")
    if span_days < 90:
        print("WARNING: <90 days of data. Section 12.1 requires 90 days of M5.")
    split = m5.index[0] + (m5.index[-1] - m5.index[0]) * (2 / 3)
    windows = {"FULL": m5, "TRAIN (first 2/3)": m5[m5.index < split], "TEST (last 1/3)": m5[m5.index >= split]}
    for name, df in windows.items():
        if len(df) < 400:
            print(f"\n[{name}] too short"); continue
        trades, diag = run_backtest(df, a.asset, cfg, a.payout, a.mtg)
        m = metrics(trades, cfg.virtual_balance)
        print(f"\n===== {name} =====\n{m}")
        print("top blockers:", diag.most_common(6))
        v = verdict(m)
        if isinstance(v, dict):
            for k, ok in v.items():
                print(("  PASS  " if ok else "  FAIL  ") + k)
        else:
            print("  ", v)
    print("\nREMINDER: news filter OFF in backtest; real volume usually absent; pass here != safe live (12.6, 13.1).")


if __name__ == "__main__":
    main()
