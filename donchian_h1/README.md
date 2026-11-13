# Donchian 20 breakout, XAUUSD H1: saved for incubation

Status: **Passed** the backtest gates. Not yet forward-tested on a demo account. Saved 2026-11-12.

## Rules (what the EA does)
All signals use the last closed H1 bar; orders go in at the next bar's open.
- Channels use the bars *before* the signal bar: entry channel = highest high / lowest low of the previous 20 bars; exit channel = previous 10 bars.
- Trend filter: 200 EMA.
- Long: close above the 20-bar high and above the 200 EMA, not already long. Short: close below the 20-bar low and below the 200 EMA, not already short. A signal in the other direction reverses the position.
- Initial stop: 2.0 x ATR(14) from the signal close. Then a ratcheting stop at 3.0 x ATR below (long) or above (short) the latest close; it only ever moves in the trade's favour.
- Exit early if the close breaks the 10-bar opposite channel.
- No take-profit, no averaging, no martingale. One position at a time.
- Sizing: InpEquityPercent of equity as position value (backtest used 100%, no leverage).

## Backtest summary ($100 account, Vantage XAUUSD.sc, MT5 tester, 1 minute OHLC)
| | Long window 2022-06-01 to 2026-11-12 | Recent 2025-09-21 to 2026-11-12 |
|---|---|---|
| Return on $100 | +24.8% | +24.4% |
| Profit factor | 1.13 | 1.39 |
| Max drawdown (equity) | 14.0% | 14.0% |
| Trades | 718 (about 160 a year) | 164 |
| Win rate | 34.1% | 39.6% |
- Real-tick re-run (only 19% / 74% real ticks): long +21.5% PF 1.11, recent +22.9% PF 1.36.
- Parameter sweep (entry channel 10/20/40 x trail 2/3/4 ATR, plus stop 1.5 and 2.5 ATR): 8 of 11 settings profitable in both the 2022-2025 in-sample window and the 2025-2026 holdout.
- **Caveat:** the three in-sample years were close to breakeven (-2% to +8% across settings, default -0.2%); most of the profit came from the last 14 months of strong gold trend. Expect long flat or losing stretches when gold is not trending.
- Iran-war period (28 Feb to 12 Nov 2026): 105 trades, 39% wins, +$14.25 on the $100 account; about 70% of trades exit at a stop (a third of those stops lock in profit).
- Full report: `backtest_report_100usd_long_window.htm`.

## Files
- `XAU_H1_DonchianBreakout.mq5` / `.ex5`: the EA (compiled with 0 errors). Copy both into `MQL5\Experts` in the MT5 data folder (already in `Experts\XAU_Research`).
- `XAU_H1_DonchianBreakout.pine`: the Pine v6 version (TradingKit).
- `donchian_backtest_match.set`: inputs identical to the backtest (100% of equity).
- `donchian_conservative.set`: same rules at 25% of equity.
- `backtest_report_100usd_long_window.htm`: MT5 tester report.

## Incubation plan (demo forward test)
1. Open a **Vantage demo** account (cent type, about $100 equivalent) and log in to it. Do not attach the EA to the live account.
2. Open an XAUUSD(.sc) **H1** chart, turn **Algo Trading** on, attach the EA, load `donchian_backtest_match.set` (Inputs tab -> Load), tick *Allow Algo Trading*.
3. Let it run for at least 3 months or 40 trades. The backtest averages about 13 trades a month.
4. Each month compare against the backtest: trade count, win rate (expect 30-40%), profit factor, max drawdown. Use Account History -> report.
5. Suggested stop-and-review triggers (my proposals, adjust as you like): equity drawdown beyond 25%, or profit factor below 0.9 after 100 trades.
6. Server time on Vantage is GMT+2/+3. This EA does not use clock time, so no offset input matters for it.

## Not done yet
- Real-tick test with full tick history for the whole window.
- Demo forward test and trade logging.
- RSI(2) and other strategies were rejected and are not part of this package.
