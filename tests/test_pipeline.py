"""Integration smoke test: signal -> chart -> result -> MTG1 -> result, with fake data + fake Telegram (no network)."""
import dataclasses, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__))); sys.path.insert(0, os.path.dirname(__file__))
from datetime import timedelta
import pandas as pd
from scenario import bullish_scenario
from bot.config import settings
from bot.datafeed import CandleSource
from bot.strategy import Decision
from bot.smc import detect_setup
import main as M


class FakeTG:
    def __init__(self): self.log = []
    def message(self, t, pre=False): self.log.append(("msg", t)); return True
    def photo(self, p, c=""): self.log.append(("photo", p, c)); return os.path.exists(p)


class FakeSource(CandleSource):
    def __init__(self, df): self.df = df
    def get_candles(self, asset, tf, count, now=None): return self.df.iloc[-count:]
    def get_payout(self, asset): return 85.0


def add_candle(df, close_delta, o=None):
    last = df.iloc[-1]; t = df.index[-1] + pd.Timedelta(minutes=5)
    o = last.close if o is None else o; c = last.close + close_delta
    df.loc[t] = [o, max(o, c) + 0.0002, min(o, c) - 0.0002, c, 0.0]
    return df


def run():
    tmp = tempfile.mkdtemp()
    cfg = dataclasses.replace(settings, whitelist=("EURUSD_otc",), db_path=os.path.join(tmp, "t.db"), charts_dir=os.path.join(tmp, "c"),
                              news_enabled=False, mtg_enabled=True, signal_max_age_seconds=10**9, min_entry_candles=60)
    df = bullish_scenario()
    src, tg = FakeSource(df), FakeTG()
    bot = M.Bot(cfg, source=src, telegram=tg)
    setup, _ = detect_setup(df, "BUY", cfg)
    assert setup is not None

    def fake_eval(asset, m5, h1, h4, now):
        d = Decision(asset=asset, is_otc=True, tf_seconds=300, decision="BUY", score=91, htf_bias="BULLISH",
                     sweep="YES (SSL at 1.09900)", ob_fvg="YES", confirmation="Bullish Engulfing", rsi_text="27",
                     macd_text="Bullish Cross", bb_text="Lower Band", volume_ratio=6.2, notes="test", setup=setup)
        d.candle_close = (m5.index[-1] + pd.Timedelta(minutes=5)).to_pydatetime(); d.entry_price = float(m5.close.iloc[-1])
        return d
    bot.strategy.evaluate = fake_eval
    bot.strategy.mtg_precheck = lambda *a, **k: (True, "")

    now = (df.index[-1] + pd.Timedelta(minutes=5, seconds=1)).to_pydatetime()
    bot.scan(now)
    assert len(bot.tracker.pending) == 1, tg.log
    sig = bot.tracker.pending[0]; print("signal", sig.direction, sig.stake, sig.entry_price)

    # LOSS: next candle closes below entry
    add_candle(src.df, -0.0010)
    now2 = now + timedelta(minutes=5)
    bot.resolve_due(now2)
    mtg = bot.tracker.pending[0] if bot.tracker.pending else None
    assert mtg is not None and mtg.step == 1, [x[0] for x in tg.log]
    print("MTG stake", mtg.stake, "base", mtg.base_stake)

    # MTG WIN: next candle closes above mtg entry
    add_candle(src.df, +0.0010)
    bot.resolve_due(now2 + timedelta(minutes=5))
    assert not bot.tracker.pending
    print(bot.summary_numbers())
    print("risk", bot.risk.snapshot(now2))
    photos = [x for x in tg.log if x[0] == "photo"]
    print("photos:", len(photos), "msgs:", len([x for x in tg.log if x[0] == "msg"]))
    for p in photos: print(os.path.exists(p[1]), p[2].replace("\n", " | ")[:160])
    return photos


if __name__ == "__main__":
    ph = run()
    import shutil; os.makedirs("/tmp/chartcheck", exist_ok=True)
    for i, p in enumerate(ph): shutil.copy(p[1], f"/tmp/chartcheck/{i}.png")
