"""features.py -- chain history, 10-signal reports, MTG1 10s heads-up, weak-asset filter."""
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import pandas as pd

from .logger import log


def chains(store) -> List[Dict]:
    """One row per BASE signal. result: WIN | MTG1 WIN | LOSS | DRAW. Chains still open are skipped."""
    with store.lock:
        rows = store.conn.execute(
            "SELECT id, ts, asset, step, parent_id, status, pnl FROM signals "
            "WHERE status IN ('WIN','LOSS','DRAW','PENDING') ORDER BY ts ASC").fetchall()
    base, order = {}, []
    for id_, ts, asset, step, parent, status, pnl in rows:
        if step == 0:
            base[id_] = {"id": id_, "ts": ts, "asset": asset, "pnl": float(pnl or 0), "legs": [status]}
            order.append(id_)
    for id_, ts, asset, step, parent, status, pnl in rows:
        if step > 0 and parent in base:
            base[parent]["pnl"] += float(pnl or 0)
            base[parent]["legs"].append(status)
    out = []
    for i in order:
        c = base[i]
        legs = c["legs"]
        if "PENDING" in legs:
            continue
        if legs[0] == "WIN":
            c["result"] = "WIN"
        elif legs[0] == "DRAW":
            c["result"] = "DRAW"
        elif len(legs) > 1 and legs[-1] == "WIN":
            c["result"] = "MTG1 WIN"
        else:
            c["result"] = "LOSS"
        c["pnl"] = round(c["pnl"], 2)
        out.append(c)
    return out


def _agg(cs: List[Dict]) -> Dict:
    w = sum(c["result"] == "WIN" for c in cs)
    m = sum(c["result"] == "MTG1 WIN" for c in cs)
    l = sum(c["result"] == "LOSS" for c in cs)
    return {"count": len(cs), "wins": w, "mtg_wins": m, "losses": l,
            "pnl": round(sum(c["pnl"] for c in cs), 2),
            "win_rate": round(100 * (w + m) / (w + m + l), 1) if (w + m + l) else None}


def history_blocks(store, size: int = 10) -> Dict:
    cs = chains(store)
    blocks = []
    for i in range(0, len(cs), size):
        part = cs[i:i + size]
        b = _agg(part)
        b.update(n=i // size + 1, complete=len(part) == size)
        blocks.append(b)
    return {"total": _agg(cs), "blocks": blocks[::-1]}


def maybe_history_report(bot) -> None:
    """After every 10th finished signal: Telegram summary (block + running total)."""
    cs = chains(bot.store)
    n = len(cs)
    if n == 0 or n % 10 or bot.store.kv_get("hist_reported", 0) >= n:
        return
    bot.store.kv_set("hist_reported", n)
    b, t = _agg(cs[-10:]), _agg(cs)
    bot.tg.message(
        f"📊 <b>History · signals {n - 9}–{n}</b>\n"
        f"✅ {b['wins']} win · 🔁 {b['mtg_wins']} MTG1 win · ❌ {b['losses']} loss\n"
        f"Block P/L: <b>{b['pnl']:+.2f}$</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"Total {t['count']} signals · WR {t['win_rate']}% · P/L <b>{t['pnl']:+.2f}$</b>")


def bad_assets(bot) -> set:
    """Assets whose recent finished signals (last 48h) are below the win-rate floor are skipped.
    Old results age out after 48h, so a skipped asset gets another chance."""
    win = int(os.getenv("ASSET_WINDOW", "10"))
    need = int(os.getenv("ASSET_MIN_SAMPLE", "6"))
    floor = float(os.getenv("ASSET_MIN_WINRATE", "55"))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    by: Dict[str, List[Dict]] = {}
    for c in chains(bot.store):
        try:
            t = datetime.fromisoformat(c["ts"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if t < cutoff:
                continue
        except Exception:
            continue
        by.setdefault(c["asset"], []).append(c)
    bad = set()
    for asset, cs in by.items():
        cs = cs[-win:]
        if len(cs) >= need:
            wr = _agg(cs)["win_rate"]
            if wr is not None and wr < floor:
                bad.add(asset)
    return bad


def pre_mtg_alert(bot, now: datetime) -> None:
    """~10s before a pending signal expires: tell whether MTG1 is likely and whether it would be allowed."""
    cfg = bot.cfg
    if not cfg.mtg_enabled or bot.source is None:
        return
    alerted = getattr(bot, "_mtg_alerted", None)
    if alerted is None:
        alerted = bot._mtg_alerted = set()
    for sig in list(bot.tracker.pending):
        if sig.step >= cfg.mtg_max_steps or sig.id in alerted:
            continue
        left = (pd.Timestamp(sig.expiry_time) - pd.Timestamp(now)).total_seconds()
        if left > int(os.getenv("MTG_ALERT_SECONDS", "10")) + 5:
            continue
        try:
            m5 = bot.source.get_candles_peek(sig.asset, cfg.entry_tf_seconds, 300, now)
        except Exception as e:
            log("mtg_alert_fetch_failed", error=str(e)[:200])
            continue
        if len(m5) == 0:
            continue
        alerted.add(sig.id)
        px = float(m5["close"].iloc[-1])
        against = px < sig.entry_price if sig.direction == "BUY" else px > sig.entry_price
        if not against:
            bot.tg.message(f"✅ <b>{sig.asset}</b> is in profit right now · MTG1 not expected")
            continue
        pre = getattr(bot.strategy, "mtg_precheck", None)
        ok, why = (True, "")
        if pre:
            m5c, h1, h4 = bot._frames(sig.asset, now)
            ok, why = pre(sig.asset, sig.direction, m5c, h1, h4, now)
        stake = bot.risk.mtg_stake(sig.base_stake or sig.stake, sig.payout_pct)
        if ok and stake:
            bot.tg.message(f"⚠️ <b>MTG1 heads-up · {sig.asset}</b>\n"
                           f"Price is against the signal. If it closes as LOSS, MTG1 will be sent at once.\n"
                           f"Prepare stake: <b>${stake:.2f}</b>")
        else:
            bot.tg.message(f"ℹ️ <b>{sig.asset}</b>: price is against the signal, but MTG1 will be "
                           f"<b>SKIPPED</b> ({why or 'risk limit'})")
