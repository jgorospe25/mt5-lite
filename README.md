# MT5 lite

A light, UI-free way to backtest and forward-test (incubate) MetaTrader 5 strategies from Python,
plus a small desktop GUI to watch **backtest vs forward** equity curves.

* No MT5 chart/Strategy Tester UI is needed. The MT5 terminal only has to be **running and logged in**
  so Python can read price bars. The tool **never places orders** (paper trading only).
* One shared engine runs both backtests and incubations, so forward results are comparable to the backtest.
* Each backtest / incubation is its own background process; incubations can be paused and resumed.

```
mt5lite.py      engine, strategies, command-line backtest / incubate / fetch
mt5runner.py    runs one instance (backtest or incubation) from instances/<id>/meta.json
mt5gui.py       desktop GUI (tkinter + matplotlib)
mt5.cmd         launcher: command line using the project's virtual environment
mt5gui.cmd      launcher: opens the GUI
donchian_h1/    researcher folder: XAUUSD H1 Donchian breakout (.mq5, .pine, .set presets, report)
f7_m15/         researcher folder: XAUUSD M15 volatility breakout (.mq5, .set presets)
```

## 1. Setup (Windows)

Requirements: Python 3.10+ (3.12 tested), the MetaTrader 5 desktop terminal, a broker account (demo is fine).

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Then use the launchers so you never depend on `python` being on your PATH:

```bash
.\mt5gui.cmd                       # opens the GUI
.\mt5.cmd backtest --demo          # command line (any mt5lite.py command works)
```

## 2. MT5 terminal prerequisites

* MT5 is installed, **running, and logged in** to the account whose prices you want.
* The symbol must exist **with the exact name your broker uses** (e.g. `XAUUSD`, or `XAUUSD.sc` on a cent account).
  If a fetch fails with `Terminal: Call failed`, the symbol name or timeframe history is wrong or not downloaded yet:
  open that symbol/timeframe chart once in MT5, or check names with Market Watch.
* Bar timestamps are **broker server time**. The strategies that depend on the clock (F7) convert Vantage
  server time (GMT+2, GMT+3 during US daylight saving) to UTC themselves.
* Historical data is limited by what the terminal holds (the runner asks for up to 50,000 bars when catching up).

## 3. The GUI

Start with `.\mt5gui.cmd`.

**New instance (top panel)**

| Field | Meaning |
|---|---|
| Strategy folder | Dropdown of folders next to the app that contain `.mq5` / `.pine` / `.set` files. **Add folder…** adds another location, **Rescan** refreshes. |
| Preset (.set) | Parameters read from the folder's `.set` files (or `EA defaults`). |
| Symbol / Timeframe | Defaults come from the strategy; change to match your broker's symbol name. |
| Balance | Starting balance for the simulation (sizing is a % of it). |
| Spread | Price units paid on entry (blank = the strategy's default, 0.25 for gold). |
| Backtest from | Optional `YYYY-MM-DD`; earlier bars only warm up indicators. |
| Incubation feed | `live` = real MT5 bars as they close. `replay` = replays the last N bars quickly to test the pipeline. |
| Refresh data from MT5 first | Downloads/updates the bar CSV before running (needs the terminal running). |

Buttons: **New backtest** and **New incubation (forward test)** each start a separate background process.

**Instance list (left)**

* Shows type, status, **how long it has been running**, trades and return %.
* **Pause** stops an incubation and saves its state. **Resume** continues from the saved bar.
* **Remove** deletes the instance and its data. **Export data** copies the equity curves, trades and a
  `comparison.csv` to a folder you choose.
* The log box shows the runner's terminal output.

**Charts and data (right)**

* The header shows when the incubation started, total elapsed time, active time vs paused/off time, and the last bar processed.
* **Timeline**: the backtest up to the forward start, with the forward curve continuing from its end (rebased), and a drawdown panel.
  The forward part is short at first; set **Backtest shown** to `30d` or `90d` to zoom in.
* **Aligned**: forward return vs the range of what the backtest did over any window of the same length
  (10th-90th percentile band and median). This is the most useful check that forward behaves like the backtest.
* The table compares **Backtest (all)**, **Backtest (same length as forward)** and **Forward**: return, max drawdown,
  trades, trades/month, win rate, profit factor, average and best/worst trade.

**Closing the GUI / shutting down**

* Closing the window offers to pause everything or leave incubations running in the background.
* If the PC is turned off, runners die, but a live incubation saves its full state after every bar. Open the GUI,
  select the instance (status `stopped`) and press **Resume**. The bars missed meanwhile are processed in order from
  the MT5 terminal (it must be running again), so the forward curve stays continuous. The off time is counted as
  paused/off in the elapsed-time display.

## 4. Command line

```bash
# pull bars from MT5 into a CSV (columns: time,open,high,low,close,tick_volume)
.\mt5.cmd fetch --symbol XAUUSD.sc --tf H1 --bars 30000 --out XAUUSD_sc_H1.csv

# backtest
.\mt5.cmd backtest --csv XAUUSD_sc_H1.csv --strategy xau_donchian --start 2022-06-01
.\mt5.cmd backtest --demo --strategy xau_f7_k08            # synthetic data, to try the pipeline

# forward test on live MT5 bars (Ctrl+C to stop; --log appends each closed trade to a CSV)
.\mt5.cmd incubate --symbol XAUUSD.sc --tf H1 --strategy xau_donchian --log donchian_incubation.csv

# replay the last 300 bars of a CSV as if live
.\mt5.cmd incubate --csv XAUUSD_sc_H1.csv --strategy xau_donchian --replay 0.1
```

Useful flags: `--balance`, `--spread`, `--commission` (per lot per side), `--commission-pct`, `--equity-pct`, `--trades N`.
The command-line `incubate` is simple and **cannot resume**; use the GUI for incubations you want to pause.

`mt5.cmd` runs `mt5lite.py`. To run a GUI-created instance without the GUI, use
`.venv\Scripts\python.exe mt5runner.py --dir instances\<id>`.

## 5. Strategies included

| Key | Folder | Rules (summary) |
|---|---|---|
| `xau_donchian` | `donchian_h1` | 20-bar channel breakout + 200 EMA filter, exit on opposite 10-bar channel, 2x ATR initial stop then ratcheting 3x ATR stop. H1. |
| `xau_f7` / `xau_f7_k08` | `f7_m15` | Williams-style volatility breakout from the UTC day open (k x prior-day range), SL = day open, TP in R, close at 21:00 UTC, 1% risk. M15. `xau_f7_k08` is k 0.8, TP 2R. |

Each folder's own `README.md` describes the rules and the research behind them.
On the same MT5 data the Python ports reproduce the EAs' trade counts to within a few trades
(Donchian 724 vs 718, F7 default 717 vs 719), with returns within a few percentage points.

## 6. Simulation assumptions

* Bar-based, not tick-based: signals use the **closed** bar and orders fill at the **next bar's open**.
* Spread is paid on entry (long at ask, short exits at ask). SL/TP are checked on bar high/low; **the stop wins** if both are hit in one bar.
* Sizing: Donchian uses a % of equity (0.01 lot steps); F7 sizes so a stop-out loses 1% and skips trades whose spread exceeds 10% of the stop distance.
* Commission defaults to 0 (Standard/Cent accounts). Swaps, slippage and margin are not modelled.
* Results can differ from the MT5 Strategy Tester, mostly on tight-stop or fast strategies.

## 7. Adding a strategy from a new researcher folder

A folder is listed in the dropdown automatically, but it only **runs** if there is a Python port.

1. Port the EA into `mt5lite.py` as a `Strategy` subclass:
   * `prepare(df)` adds **causal** indicator columns (row `i` uses only rows `<= i`).
   * `signal(df, i, pos)` is called once per closed bar and returns a `Signal` (direction `+1/-1/0`, or `None` to only move the stop;
     options: `sl`, `tp`, `sl_abs`, `tp_abs`, `move_sl`, `risk_pct`, `max_spread_frac`) or `None`.
   * Set `defaults` (contract size, spread, commission), `warmup` and, if it needs several days of bars, `min_window`.
   * Register it in `STRATEGIES`.
2. In `mt5gui.py`, add an entry to `STRAT_MAP`: words that identify the folder, default timeframe/symbol, and how `.set`
   input names map to your constructor parameters.
3. Check it against the EA's own MT5 Strategy Tester numbers before trusting forward results.

## 8. Files an instance writes (`instances/<id>/`)

| File | Content |
|---|---|
| `meta.json` | config, status, heartbeat, elapsed/active seconds, last bar |
| `equity.csv`, `trades.csv` | backtest result, or the **forward** curve/trades for an incubation |
| `backtest_equity.csv`, `backtest_trades.csv` | incubation baseline: backtest on all data up to the forward start |
| `state.pkl` | saved broker + strategy state for pause/resume |
| `log.txt` | runner terminal output |
| `stop` | create this file to ask a running instance to pause |

`instances/`, the bar CSVs and local settings are not tracked by git.

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| `python` is not recognized | Use `.\mt5.cmd ...` / `.\mt5gui.cmd`, or call `.venv\Scripts\python.exe` directly. |
| `Terminal: Call failed` / no data | Terminal not running or not logged in; wrong symbol name (e.g. `XAUUSD` vs `XAUUSD.sc`); open the chart once so history downloads. |
| GUI says "no Python port yet" | See section 7. |
| Instance status `stopped` | The process is gone (PC restart, crash). Select it and press **Resume**. |
| Forward curve invisible in Timeline | Choose a shorter **Backtest shown** window, or use the Aligned view. |
| Results differ from the MT5 tester | Check symbol, spread, data source and the tester's price model; see section 6. |

This is research tooling, not financial advice. Forward tests here are simulated and do not use your broker's execution.
