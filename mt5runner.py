#!/usr/bin/env python
"""
mt5runner - runs ONE instance (a backtest or an incubation) described by <dir>/meta.json.
Spawned by mt5gui.py, but fully usable on its own:

    python mt5runner.py --dir instances/<id>

Files it writes into the instance folder (the GUI only reads these):
  meta.json            config + live status/heartbeat
  equity.csv           backtest equity, or FORWARD equity for an incubation
  trades.csv           trades of the above
  backtest_equity.csv  (incubation) the backtest run on all data up to the incubation start
  backtest_trades.csv
  state.pkl            (incubation) broker + strategy state, so pause/resume continues seamlessly
  stop                 create this file to ask a running instance to pause
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

import mt5lite as M

TF_SEC = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}


def jload(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def jsave(p, d):
    tmp = Path(str(p) + ".tmp")
    num = lambda o: float(o) if isinstance(o, (np.floating, np.integer)) else str(o)
    tmp.write_text(json.dumps(d, indent=1, default=num), encoding="utf-8")
    for _ in range(5):  # the GUI may be reading the file at this very moment
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.1)


class Inst:
    def __init__(self, d):
        self.dir = Path(d)
        self.mp = self.dir / "meta.json"
        self.meta = jload(self.mp)

    def update(self, **kw):
        self.meta.update(kw)
        jsave(self.mp, self.meta)


def make_strategy(m):
    return M.STRATEGIES[m["strategy"]](**m.get("params", {}))


def make_broker(m):
    d = M.STRATEGIES[m["strategy"]].defaults
    pick = lambda k, fb: m[k] if m.get(k) is not None else d.get(k, fb)
    return M.Broker(balance=m["balance"], lots=0.1, contract=pick("contract", 100_000.0), spread=pick("spread", 0.0001),
                    commission=pick("commission", 0.0), equity_pct=pick("equity_pct", 0.0),
                    commission_pct=m.get("commission_pct") or 0.0)


def write_curves(b, d, prefix=""):
    pd.DataFrame(b.equity_curve, columns=["time", "equity"]).to_csv(Path(d) / f"{prefix}equity.csv", index=False)
    pd.DataFrame([t.__dict__ for t in b.trades]).to_csv(Path(d) / f"{prefix}trades.csv", index=False)


def run_bars(strat, b, d, first, progress=None):
    for i in range(len(d)):
        fn = (lambda pos, i=i: strat.signal(d, i, pos)) if i >= first else None
        b.on_bar(d.time.iat[i], d.open.iat[i], d.high.iat[i], d.low.iat[i], d.close.iat[i], fn)
        if progress and i % 2000 == 0:
            progress(i / len(d))


# ---------------------------------------------------------------- data feeds


def refresh_csv(csv, symbol, tf):
    """Bring the CSV up to date from the MT5 terminal (closed bars only)."""
    p = Path(csv)
    if p.exists():
        old = M.load_csv(str(p)).iloc[:-1]  # last stored bar may have been a still-forming one
        n = int((pd.Timestamp.now() - old.time.iat[-1]).total_seconds() / TF_SEC[tf]) + 60
        new = M.mt5_rates(symbol, tf, min(max(n, 100), 50000)).iloc[:-1]
        df = pd.concat([old, new]).drop_duplicates("time", keep="last").sort_values("time")
    else:
        df = None
        for n in (150000, 100000, 60000, 30000, 10000):
            try:
                df = M.mt5_rates(symbol, tf, n).iloc[:-1]
                break
            except SystemExit:
                continue
        if df is None:
            sys.exit(f"could not download {symbol} {tf} from MT5")
    df.to_csv(p, index=False)
    return M.load_csv(str(p))


class LiveFeed:
    def __init__(self, m):
        self.sym, self.tf = m["symbol"], m["tf"]
        self.csv = m["csv"]
        self.interval = m.get("poll", 15)

    def baseline(self):
        return refresh_csv(self.csv, self.sym, self.tf)

    def catch_up(self, last, window):
        missed = int((pd.Timestamp.now() - last).total_seconds() / TF_SEC[self.tf]) + 10
        bars = M.mt5_rates(self.sym, self.tf, min(window + max(missed, 0) + 10, 50000)).iloc[:-1].reset_index(drop=True)
        return bars[bars.time <= last].tail(window).reset_index(drop=True), bars[bars.time > last].reset_index(drop=True)

    def poll(self, last):
        bars = M.mt5_rates(self.sym, self.tf, 6).iloc[:-1]
        return bars[bars.time > last].reset_index(drop=True), False


class ReplayFeed:
    """Replays the last N bars of a CSV one at a time (to try the whole pipeline without waiting)."""

    def __init__(self, m):
        self.df = M.load_csv(m["csv"])
        self.cut = len(self.df) - int(m.get("replay_bars", 300))
        self.interval = m.get("speed", 0.5)

    def baseline(self):
        return self.df.iloc[: self.cut].reset_index(drop=True)

    def catch_up(self, last, window):
        return self.df[self.df.time <= last].tail(window).reset_index(drop=True), self.df.iloc[0:0]

    def poll(self, last):
        nxt = self.df[self.df.time > last].head(1).reset_index(drop=True)
        return nxt, len(nxt) == 0


# ------------------------------------------------------------------- backtest


def run_backtest_instance(inst):
    m = inst.meta
    inst.update(status="running", pid=os.getpid(), heartbeat=time.time(), progress=0.0, started_at=time.time())
    if m.get("refresh"):
        try:
            refresh_csv(m["csv"], m["symbol"], m["tf"])
        except (SystemExit, Exception) as e:  # fall back to the CSV we already have
            print("data refresh failed, using existing CSV:", e, flush=True)
    df = M.load_csv(m["csv"])
    strat, b = make_strategy(m), make_broker(m)
    d = strat.prepare(df)
    first = strat.warmup
    if m.get("start"):
        first = max(first, int((d.time < pd.Timestamp(m["start"])).sum()))
    print(f"backtest {m['strategy']} {m['symbol']} {m['tf']} {len(d)} bars from {d.time.iat[first]}", flush=True)
    run_bars(strat, b, d, first, lambda p: inst.update(progress=round(p, 3), heartbeat=time.time()))
    b.close_open(d.time.iat[-1], d.close.iat[-1])
    write_curves(b, inst.dir)
    inst.update(status="done", progress=1.0, finished_at=time.time(), heartbeat=time.time(),
                stats=M.stats(b, m["balance"]), data_end=str(d.time.iat[-1]), data_start=str(d.time.iat[first]))
    print("done", M.stats(b, m["balance"]), flush=True)


# ----------------------------------------------------------------- incubation


def run_incubation(inst):
    m, dr = inst.meta, inst.dir
    live = m.get("feed", "live") == "live"
    feed = LiveFeed(m) if live else ReplayFeed(m)
    strat0 = make_strategy(m)
    window = max(2000, strat0.min_window)
    state = dr / "state.pkl"
    stop = dr / "stop"
    if stop.exists():
        stop.unlink()

    if state.exists():
        with open(state, "rb") as f:
            S = pickle.load(f)
        b, strat, last = S["broker"], S["strat"], S["last"]
        hist, new = feed.catch_up(last, window)
        print(f"resuming after {last}; {len(new)} bars missed while paused are processed now", flush=True)
    else:
        df = feed.baseline()
        strat_bt, b_bt = make_strategy(m), make_broker(m)
        d = strat_bt.prepare(df)
        print(f"baseline backtest on {len(d)} bars up to {d.time.iat[-1]} ...", flush=True)
        inst.update(status="running", phase="baseline backtest", pid=os.getpid(), heartbeat=time.time())
        first = strat_bt.warmup
        if m.get("start"):
            first = max(first, int((d.time < pd.Timestamp(m["start"])).sum()))
        run_bars(strat_bt, b_bt, d, first, lambda p: inst.update(progress=round(p, 3), heartbeat=time.time()))
        write_curves(b_bt, dr, "backtest_")
        last = df.time.iat[-1]
        hist, new = df.tail(window).reset_index(drop=True), df.iloc[0:0]
        strat, b = make_strategy(m), make_broker(m)
        b.equity_curve.append((last, b.balance))
        inst.update(started_at=time.time(), forward_start_bar=str(last), active_seconds=0.0,
                    baseline_stats=M.stats(b_bt, m["balance"]), phase="forward")
        print(f"forward test starts after bar {last}", flush=True)

    inst.update(status="running", phase="forward", pid=os.getpid(), heartbeat=time.time())
    tick = last_meta = time.time()
    n_since_save = 0

    def save(force=False):
        nonlocal last_meta, n_since_save
        strat._dmap = {}
        with open(str(state) + ".tmp", "wb") as f:
            pickle.dump({"broker": b, "strat": strat, "last": last}, f)
        os.replace(str(state) + ".tmp", state)
        write_curves(b, dr)
        n_since_save = 0

    def heartbeat(**kw):
        nonlocal tick, last_meta
        now = time.time()
        inst.meta["active_seconds"] = inst.meta.get("active_seconds", 0.0) + (now - tick)
        tick = now
        inst.update(heartbeat=now, last_bar=str(last), n_trades=len(b.trades),
                    equity=b.equity_curve[-1][1], **kw)
        last_meta = now

    def step(row_df):
        nonlocal hist, last
        hist = pd.concat([hist, row_df], ignore_index=True).tail(window).reset_index(drop=True)
        d = strat.prepare(hist)
        i = len(d) - 1
        fn = (lambda pos: strat.signal(d, i, pos)) if i >= strat.warmup else None
        b.on_bar(d.time.iat[i], d.open.iat[i], d.high.iat[i], d.low.iat[i], d.close.iat[i], fn)
        last = d.time.iat[i]
        pos = "flat" if b.pos is None else ("LONG" if b.pos.side > 0 else "SHORT")
        print(f"{last} c={d.close.iat[i]:.2f} pos={pos} equity={b.equity_curve[-1][1]:,.2f} trades={len(b.trades)}", flush=True)

    finished = False
    while True:
        if len(new) == 0:
            new, finished = feed.poll(last)
        for k in range(len(new)):
            step(new.iloc[[k]])
            n_since_save += 1
            if live or n_since_save >= 25:
                save()
        new = new.iloc[0:0]
        if stop.exists():
            stop.unlink()
            save()
            heartbeat(status="paused", paused_at=time.time())
            print("paused", flush=True)
            return
        if finished:
            save()
            heartbeat(status="done", finished_at=time.time())
            print("replay finished", flush=True)
            return
        if time.time() - last_meta > 5:
            heartbeat()
        t_end = time.time() + feed.interval
        while time.time() < t_end and not stop.exists():
            time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True)
    a = ap.parse_args()
    inst = Inst(a.dir)
    try:
        (run_backtest_instance if inst.meta["kind"] == "backtest" else run_incubation)(inst)
    except SystemExit as e:
        inst.update(status="error", error=str(e.code), heartbeat=time.time())
        print("ERROR:", e.code, flush=True)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        inst.update(status="error", error=f"{type(e).__name__}: {e}", heartbeat=time.time())


if __name__ == "__main__":
    main()
