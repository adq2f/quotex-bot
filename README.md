# Quotex Signal Engine v2.1

Signal bot that implements the MASTERMIND rulebook (v2.0 + v2.1 addendum): HTF bias → SMC/ICT → RSI/MACD/BB confluence →
fakeout filter → score → hard risk rules. Sends **signal chart photo** + **result photo (WIN/LOSS/DRAW + P/L)** to Telegram.
Optional **MTG1** after a loss (default OFF). Default mode is **signal-only**: nothing is ever traded.

## Layout
```
main.py                 Flask /health (main thread) + trading loop (daemon thread)
backtest.py             Section 12 validation (metrics + 2/3-1/3 walk-forward)
bot/config.py           all settings; hard rules clamped (risk<=2%, daily loss cannot be disabled, MTG<=2 steps)
bot/datafeed.py         Synthetic / CSV / Quotex candle sources (closed candles only)
bot/indicators.py       RSI, MACD, EMA, BB, ATR, volume proxy, triple confluence
bot/bias.py             H4+H1 bias (EMA200 + HH/HL, LH/LL)
bot/smc.py              sweep, impulse, order block, FVG, BOS/CHoCH
bot/patterns.py         candle patterns, 4.4 context checks, 4.3 fakeout filter
bot/scoring.py          0-100 score
bot/filters.py          sessions, rollover, ATR/gap locks
bot/news.py             high-impact news block (+-15 min)
bot/risk.py             stateful risk manager + MTG sizing
bot/strategy.py         orchestrator + Section 9 output
bot/tracker.py          signal lifecycle, WIN/LOSS/DRAW
bot/chart.py            signal + result images
bot/telegram_out.py     Telegram delivery
bot/executor.py         execution gate (signal_only by default)
bot/store.py            SQLite audit log + state
tests/                  scenario + pipeline smoke test
```

## Run locally (safe smoke test, no network needed)
```
pip install -r requirements.txt
cp .env.example .env        # fill TELEGRAM_* ; keep DATA_SOURCE=synthetic, NEWS_ENABLED=false
python tests/test_pipeline.py   # signal -> chart -> loss -> MTG1 -> win, with fake Telegram
python main.py                  # /health on :10000
```
Synthetic data is random: it proves the plumbing, not profitability.

## Go-live order (Section 12 / 14.8)
1. Get 90+ days of M5 CSV per asset (TradingView/broker export): `data/EURUSD_M5.csv`.
2. `python backtest.py --csv data/EURUSD_M5.csv --asset EURUSD --payout 85` (add `--mtg` to compare).
3. Deploy on Render with `DATA_SOURCE=quotex`, `ACCOUNT_TYPE=DEMO`, `EXECUTION_MODE=signal_only`.
4. Watch `/diag` (which gate blocks most) and the Telegram results for 2+ weeks.
5. Only then consider anything else. Never enable real execution before 1-4 pass.

## Render
- Web Service, **Starter** plan, start `python main.py`, health path `/health`, `PYTHON_VERSION=3.11.9`.
- Add a **Disk** mounted at `/data` and set `DB_PATH=/data/quotex_bot.db` (otherwise cooldown/read-only state resets on deploy).
- Set secrets in the Environment tab (never in GitHub). UptimeRobot: HTTP monitor on `/health`, 5 min.
- `/stats` = state + win rates (base vs MTG separately), `/diag` = gate counters, `/admin/reset_readonly?key=ADMIN_KEY` = manual reset of the 15% drawdown guard.

## Interpretation decisions (rulebook conflicts) — change only with your approval
1. **4.2 vs pin bars**: "body < 30% banned" is applied to engulfing/multi-candle patterns; hammer/shooting-star are exempt (they are small-body by definition).
2. **4.3 fakeout tests** apply to the impulse (breakout) candle, not the sweep candle (which must have a long wick) or the pin-bar confirmation.
3. **OB + FVG "overlap"** = overlap *or touch* (the OB candle is candle 1 of the FVG, so they share a boundary). Entry zone = OB+FVG span.
4. **Zone freshness**: untouched from creation until the confirmation candle; max age 20 candles.
5. **Volume** on OTC has no real data: candle range is used as a proxy (15.5).
6. **6.6 stake formula** `balance x risk / payout` makes real risk ~1.18% at 85% payout (inside the 2% hard cap). Sequence worst case with MTG1 is capped at 2%.
7. **Score 75 threshold** never binds: passing all mandatory gates already gives >= 85.
8. **4.4 Three Line Strike "prior move >= 5% in 10 candles"** is literally unreachable on forex M5. Configurable via `TLS_MIN_MOVE_PCT`.
9. **Indicator timing**: `INDICATOR_WINDOW=1` (strict) = RSI, MACD cross and BB touch must all happen on the confirmation candle.

## Expect very few signals
The literal rules stack mean-reversion conditions (RSI<30, BB touch, MACD cross, bottom-30% context) on top of a
trend-pullback structure (sweep -> impulse -> retrace). They rarely coincide. That is by design ("NO-TRADE is valid"),
but check `/diag` and the backtest before loosening anything (`INDICATOR_WINDOW`, `SMC_REQUIRE_FVG`).

## What is NOT verified
- `QuotexSource` (unofficial client): written against that library's documented style, **never run here**. Verify method names for the version you install. Only that class needs editing.
- News feed URL is a public weekly calendar JSON; if it is unreachable the bot **blocks** trading (`NEWS_FAIL_POLICY=block`).
- Unofficial Quotex clients can break or put your account at risk. Signal-only mode avoids placing trades.

## MTG1 honesty note
MTG1 raises the chance that a *sequence* ends in profit, but a double loss costs ~3.2x the base stake. It does not create edge.
Stats report base and MTG win rates separately so you can judge it with data.
