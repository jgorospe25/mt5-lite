# F7 Williams volatility breakout, XAUUSD M15: saved for incubation

Status: **Testing** (saved for incubation at your request on 2026-11-13). It did NOT pass the preset backtest gate: the default setting had a long-window profit factor of 1.04 (gate 1.05) and lost money in-sample. It is incubated because its edge held across neighbouring settings, the best of any M15 idea. Treat the demo as the real test.

## Rules (what the EA does)
All times UTC, M15 bars, Monday-Friday.
- O = open of the day's 00:00 bar; PR = previous trading day's high - low (weekends skipped).
- Trigger levels: O + k x PR and O - k x PR.
- Long on the first M15 close above the upper level, short on the first close below the lower level, on bars opening 02:00-18:00. Only the first trigger of the day counts.
- Stop loss: the day's open O (fixed at entry). R = entry - O. Skip if R > 1 x PR.
- Take profit: entry +/- TP x R (fixed at entry).
- Time exit: close at the bar opening 21:00 UTC (a day trade).
- Risk: a true 1% of equity per trade (no-leverage cap off); skip if the spread is more than 10% of R. One trade per day.

## Backtest evidence (demo-server XAUUSD, 10,000 USD = $100 cent account x100, 1 minute OHLC model)
| Setting | Long window 2022-06 to 2026-11 | Notes |
|---|---|---|
| k 0.5, TP 1.5R (default) | +7.9%, PF 1.04, 719 trades, DD 20.1% | recent 14 months +7.9%, PF 1.17 |
| k 0.3, TP 1.5R | +30.9%, PF 1.07, DD 22.6% | |
| k 0.8, TP 1.5R | +14.5%, PF 1.18, DD 8.9% | |

Sweep (in-sample 2022-06..2025-06 / holdout 2025-07..2026-11), 12 settings: 7 profitable in both windows.
- k 0.8 with TP 2R: in-sample +8.4% PF 1.14 DD 8.7%; holdout +12.7% PF 1.62 DD 3.9% (best on both windows).
- The default k 0.5 / TP 1.5R lost in-sample (-3.1%), one of the two weakest settings.
- k 1.0 never trades (the risk rule blocks every entry).

## Which preset to run
- `f7_k08_tp2_incubation.set` (recommended): k 0.8, TP 2R. Best on both windows, BUT chosen after seeing the sweep, so the demo is its only honest out-of-sample test. Expect fewer trades (about 60 a year).
- `f7_default_k05_tp15.set`: the original default, for comparison if you run two copies on separate demo accounts or magic numbers.
Both presets keep the magic number 26111307; if you run both at once on one account, change the magic number of one copy.

## Files
- `XAU_M15_VolBreakout.mq5` / `.ex5`: the EA (compiled with 0 errors); also in MQL5\Experts\XAU_Research.
- The two `.set` presets (also copied to MQL5\Presets).
- `gen_f7.py`: the generator that built the EA.

## Incubation plan
1. Vantage demo account (hedging), XAUUSD **M15** chart, Algo Trading on, attach the EA, load the preset.
2. It can share the account with Donchian (H1, magic 26111202): magic numbers keep their trades apart. Combined open risk can reach about 2%.
3. Run at least 3 months (about 15 trades with k 0.8; too few to judge alone, so 6 months is better).
4. Compare monthly: win rate (expect 45-50%), profit factor, drawdown, and how often entries are skipped by the spread filter on the live feed.
5. Stop and review if the drawdown passes 15% or the profit factor is below 0.9 after 40 trades (my suggestions).

## Not done yet
- Real-tick re-run (only the 1 minute OHLC model so far).
- Any test of the k 0.8 / 2R setting on data the sweep has not already seen; the demo is that test.
