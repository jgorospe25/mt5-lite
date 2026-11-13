#!/usr/bin/env python
"""
mt5lite - headless backtester + incubator (paper trader). No MT5 GUI, terminal output only.

Backtest and incubation share ONE broker/engine, so incubation results are
comparable to backtests.

Data sources (pick one):
  --csv FILE   CSV with columns: time,open,high,low,close[,tick_volume]
  --demo       synthetic random-walk data (to test the pipeline)
  fetch        one-off pull from MT5 into a CSV (needs MetaTrader5 pkg + terminal
               running ONCE; after that everything is offline)

Usage:
  python mt5lite.py backtest --demo
  python mt5lite.py backtest --csv EURUSD_H1.csv --spread 0.00012 --lots 0.1
  # realistic costs: spread + slippage (price units, paid on every fill incl. SL/TP)
  # + commission (flat per lot per side, or --commission-pct of notional per side)
  python mt5lite.py backtest --csv XAUUSD_H1.csv --strategy xau_donchian \
      --spread 0.25 --slippage 0.05 --commission 3.5
  python mt5lite.py fetch --symbol EURUSD --tf H1 --bars 20000 --out EURUSD_H1.csv
  python mt5lite.py incubate --csv EURUSD_H1.csv --replay 0.2     # replay as if live
  python mt5lite.py incubate --symbol EURUSD --tf H1               # live via MT5 feed

Add your own strategy by subclassing Strategy (see SmaCross) and registering it
in STRATEGIES.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# Strategy interface
# ----------------------------------------------------------------------------


@dataclass
class Signal:
    direction: int | None  # +1 long, -1 short, 0 close, None = keep position (only move stop)
    sl: float = 0.0  # stop distance from entry fill (0 = none)
    tp: float = 0.0  # take-profit distance from entry fill (0 = none)
    sl_abs: float = 0.0  # absolute initial stop price (overrides sl)
    move_sl: float = 0.0  # ratchet open position's stop to this price (only toward profit)
    tp_abs: float = 0.0  # absolute take-profit price (overrides tp)
    risk_pct: float = 0.0  # >0: size so a stop-out loses this % of balance (needs a stop)
    max_spread_frac: float = 0.0  # >0: skip entry if spread > this * stop distance


class Strategy:
    """prepare() must be CAUSAL (row i only uses rows <= i). signal() is called
    once per closed bar; any order it asks for fills at the NEXT bar's open."""

    name = "base"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    def signal(self, df: pd.DataFrame, i: int, pos: "Trade | None" = None) -> Signal | None:
        return None  # None = no change

    warmup = 50  # bars needed before signals are valid
    min_window = 0  # bars incubation must keep in memory (e.g. several days of M15)
    demo_base = 1.10  # synthetic-data price level for --demo
    defaults: dict = {}  # broker defaults (contract, spread, commission, equity_pct) for this market


class SmaCross(Strategy):
    name = "sma_cross"

    def __init__(self, fast=20, slow=50, atr_n=14, sl_atr=2.0, tp_atr=3.0):
        self.fast, self.slow, self.atr_n = fast, slow, atr_n
        self.sl_atr, self.tp_atr = sl_atr, tp_atr
        self.warmup = max(slow, atr_n) + 2

    def prepare(self, df):
        df = df.copy()
        df["f"] = df["close"].rolling(self.fast).mean()
        df["s"] = df["close"].rolling(self.slow).mean()
        pc = df["close"].shift(1)
        tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
        df["atr"] = tr.rolling(self.atr_n).mean()
        return df

    def signal(self, df, i, pos=None):
        f0, s0, f1, s1 = df.f.iat[i], df.s.iat[i], df.f.iat[i - 1], df.s.iat[i - 1]
        a = df.atr.iat[i]
        if np.isnan([f0, s0, f1, s1, a]).any():
            return None
        if f1 <= s1 and f0 > s0:
            return Signal(+1, self.sl_atr * a, self.tp_atr * a)
        if f1 >= s1 and f0 < s0:
            return Signal(-1, self.sl_atr * a, self.tp_atr * a)
        return None


class XauDonchian(Strategy):
    """Port of XAU_H1_DonchianBreakout.mq5 (H1 gold).
    20-bar channel breakout + 200 EMA filter, exit on opposite 10-bar channel,
    ATR initial stop and ratcheting ATR stop. Channels use the N bars BEFORE the
    signal bar. ATR is the MT5 iATR flavour (simple MA of TR), as in the EA."""

    name = "xau_donchian"
    demo_base = 2000.0
    # Vantage Standard/Cent XAUUSD: no commission, ~0.25 spread (measured median 0.21, now 0.29)
    defaults = dict(contract=100.0, spread=0.25, commission=0.0, equity_pct=100.0)

    def __init__(self, entry=20, exit_=10, ema=200, atr_n=14, sl_atr=2.0, trail_atr=3.0):
        self.entry, self.exit_, self.ema, self.atr_n = entry, exit_, ema, atr_n
        self.sl_atr, self.trail_atr = sl_atr, trail_atr
        self.warmup = ema + 2

    def prepare(self, df):
        df = df.copy()
        df["up"] = df.high.rolling(self.entry).max().shift(1)
        df["dn"] = df.low.rolling(self.entry).min().shift(1)
        df["xup"] = df.high.rolling(self.exit_).max().shift(1)
        df["xdn"] = df.low.rolling(self.exit_).min().shift(1)
        df["ema"] = df.close.ewm(span=self.ema, adjust=False).mean()
        pc = df.close.shift(1)
        tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
        df["atr"] = tr.rolling(self.atr_n).mean()
        return df

    def signal(self, df, i, pos=None):
        c, up, dn, xup, xdn, ema, atr = (df[k].iat[i] for k in ("close", "up", "dn", "xup", "xdn", "ema", "atr"))
        if np.isnan([up, dn, xup, xdn, ema, atr]).any() or atr <= 0:
            return None
        was_long = pos is not None and pos.side > 0
        was_short = pos is not None and pos.side < 0
        long_e = c > up and c > ema and not was_long
        short_e = c < dn and c < ema and not was_short
        if long_e:
            return Signal(+1, sl_abs=c - atr * self.sl_atr)
        if short_e:
            return Signal(-1, sl_abs=c + atr * self.sl_atr)
        if was_long and c < xdn:
            return Signal(0)
        if was_short and c > xup:
            return Signal(0)
        if was_long:
            return Signal(None, move_sl=c - atr * self.trail_atr)
        if was_short:
            return Signal(None, move_sl=c + atr * self.trail_atr)
        return None


def _vantage_offset_hours(ts: pd.Series) -> np.ndarray:
    """Vantage server time = GMT+2, GMT+3 while US DST is on (2nd Sun Mar .. 1st Sun Nov, by server date)."""
    d = ts.dt.normalize()
    off = np.full(len(d), 2)
    for y in d.dt.year.unique():
        m1, n1 = pd.Timestamp(int(y), 3, 1), pd.Timestamp(int(y), 11, 1)
        start = m1 + pd.Timedelta(days=(6 - m1.day_of_week) % 7 + 7)
        end = n1 + pd.Timedelta(days=(6 - n1.day_of_week) % 7)
        off[((d >= start) & (d < end)).to_numpy()] = 3
    return off


class XauF7(Strategy):
    """Port of XAU_M15_VolBreakout.mq5 (F7 Williams volatility breakout, XAUUSD M15, UTC).
    O = open of the UTC day's first bar, PR = previous trading day's high-low (weekends skipped).
    Long on the first close above O+k*PR, short below O-k*PR, bars opening 02:00-18:00 UTC Mon-Fri,
    first trigger of the day only (even if skipped). SL = O, TP = entry +/- tp_r*R (R=|signal close-O|),
    skip if R > max_risk*PR or spread > 10% of the stop distance. Close at the bar opening 21:00 UTC.
    True risk sizing: 1% of balance lost if the stop is hit. Data times are Vantage server time."""

    name = "xau_f7"
    demo_base = 2000.0
    warmup = 300
    min_window = 1500
    defaults = dict(contract=100.0, spread=0.25, commission=0.0, equity_pct=0.0)

    def __init__(self, k=0.5, tp_r=1.5, start_min=120, end_min=1080, exit_min=1260,
                 max_risk_pr=1.0, risk_pct=1.0, max_spread_frac=0.10):
        self.k, self.tp_r, self.start_min, self.end_min, self.exit_min = k, tp_r, start_min, end_min, exit_min
        self.max_risk_pr, self.risk_pct, self.max_spread_frac = max_risk_pr, risk_pct, max_spread_frac
        self._cur_day, self._triggered, self._dmap = None, False, {}

    def prepare(self, df):
        d = df.copy()
        d["utc"] = d.time - pd.to_timedelta(_vantage_offset_hours(d.time), unit="h")
        d["day"] = d.utc.dt.normalize()
        d["mod"] = d.utc.dt.hour * 60 + d.utc.dt.minute
        d["dow"] = d.utc.dt.day_of_week  # Mon=0
        d["O"] = d.groupby("day")["open"].transform("first")
        g = d.groupby("day").agg(hi=("high", "max"), lo=("low", "min"))
        g = g[g.index.day_of_week < 5]
        g["pr"] = (g.hi - g.lo).shift(1)  # previous weekday that has bars
        d["PR"] = d.day.map(g.pr)
        self._dmap = dict(zip(d.time, d.day))
        return d

    def signal(self, df, i, pos=None):
        day, mod = df.day.iat[i], df["mod"].iat[i]
        if pos is not None:  # one position at a time; only the time exit applies
            eday = self._dmap.get(pos.entry_time, day)
            return Signal(0) if (day == eday and mod >= self.exit_min) or day > eday else None
        if day != self._cur_day:
            self._cur_day, self._triggered = day, False
        if self._triggered or i < 1:
            return None
        O, PR = df.O.iat[i], df.PR.iat[i]
        if np.isnan(PR) or df.dow.iat[i] > 4 or mod < self.start_min or mod > self.end_min:
            return None
        if df.day.iat[i - 1] != day:  # both bars must be in the current UTC day
            return None
        c1, c2 = df.close.iat[i], df.close.iat[i - 1]
        up, dn = O + self.k * PR, O - self.k * PR
        sig_l, sig_s = c1 > up and c2 <= up, c1 < dn and c2 >= dn
        if not (sig_l or sig_s):
            return None
        self._triggered = True  # first trigger only, even if skipped below
        R = abs(c1 - O)
        if R <= 0 or R > self.max_risk_pr * PR:
            return None
        d_ = +1 if sig_l else -1
        return Signal(d_, sl_abs=O, tp_abs=c1 + d_ * self.tp_r * R,
                      risk_pct=self.risk_pct, max_spread_frac=self.max_spread_frac)


class XauF7K08(XauF7):
    """Preset f7_k08_tp2_incubation.set: k 0.8, TP 2R."""

    name = "xau_f7_k08"

    def __init__(self):
        super().__init__(k=0.8, tp_r=2.0)


class XauM5EmaPullback(Strategy):
    """Research cycle #1 (2026-11-13). XAUUSD M5 EMA-pullback continuation,
    filtered by H1 trend bias (EMA50 vs EMA200). Combines 2 concepts: EMA-pullback
    trend continuation + higher-timeframe (H1) bias, per the IDEA SOURCES rotation.

    Hypothesis: in a live London/NY session, when H1 trend bias is up (EMA50>EMA200)
    and M5 price pulls back to touch the M5 EMA21 band then closes back above it in
    the trend direction, momentum resumes (short-term liquidity re-entry near a fast
    MA before the dominant HTF trend continues). Mirrored for downtrends.

    No lookahead: the H1 bias used for M5 bar in hour H is from H1 bar H-1 (the last
    FULLY CLOSED H1 bar as of any M5 bar inside the still-forming hour H) -- i.e. the
    H1 bias is always known one calendar hour before being used. Entries fill at the
    next M5 bar's open (engine-enforced)."""

    name = "xau_m5_ema_pullback"
    demo_base = 2000.0
    # Vantage XAUUSD.sc: no commission, ~0.25 spread (same defaults as xau_donchian)
    defaults = dict(contract=100.0, spread=0.25, commission=0.0, equity_pct=0.0)

    def __init__(self, ema_fast=21, ema_slow=50, atr_n=14, h1_fast=50, h1_slow=200,
                 sl_atr=1.2, tp_atr=2.0, be_trigger_atr=1.0, be_lock_atr=0.1,
                 time_stop_bars=48, start_min=420, end_min=960, exit_min=960,
                 max_trades_day=6, risk_pct=0.75, max_spread_frac=0.20,
                 touch_band_atr=0.10):
        self.ema_fast, self.ema_slow, self.atr_n = ema_fast, ema_slow, atr_n
        self.h1_fast, self.h1_slow = h1_fast, h1_slow
        self.sl_atr, self.tp_atr = sl_atr, tp_atr
        self.be_trigger_atr, self.be_lock_atr = be_trigger_atr, be_lock_atr
        self.time_stop_bars = time_stop_bars
        self.start_min, self.end_min, self.exit_min = start_min, end_min, exit_min
        self.max_trades_day, self.risk_pct = max_trades_day, risk_pct
        self.max_spread_frac, self.touch_band_atr = max_spread_frac, touch_band_atr
        # H1 EMA200 needs ~200 H1 bars = ~2400 M5 bars to converge; be generous.
        self.warmup = max(h1_slow * 12 + 50, ema_slow + atr_n + 10)
        self._cur_day, self._trades_today = None, 0
        self._entry_i, self._entry_atr, self._be_done = None, 0.0, False

    def prepare(self, df):
        d = df.copy()
        # --- M5 indicators (causal: rolling/ewm use rows <= i only) ---
        d["ema_f"] = d.close.ewm(span=self.ema_fast, adjust=False).mean()
        d["ema_s"] = d.close.ewm(span=self.ema_slow, adjust=False).mean()
        pc = d.close.shift(1)
        tr = pd.concat([d.high - d.low, (d.high - pc).abs(), (d.low - pc).abs()], axis=1).max(axis=1)
        d["atr"] = tr.rolling(self.atr_n).mean()
        # --- session / day, UTC (Vantage server time offset, same helper as xau_f7) ---
        d["utc"] = d.time - pd.to_timedelta(_vantage_offset_hours(d.time), unit="h")
        d["utcday"] = d.utc.dt.normalize()
        d["mod"] = d.utc.dt.hour * 60 + d.utc.dt.minute
        d["dow"] = d.utc.dt.day_of_week
        # --- H1 bias from the M5 bars, using server-time hour buckets (matches MT5
        # iMA(H1) which aligns to server-time hours, not UTC) ---
        d["hour"] = d.time.dt.floor("h")
        h1 = d.groupby("hour").agg(c=("close", "last")).reset_index().set_index("hour")
        h1["ema_f"] = h1.c.ewm(span=self.h1_fast, adjust=False).mean()
        h1["ema_s"] = h1.c.ewm(span=self.h1_slow, adjust=False).mean()
        # shift(1): the bar at hour H is still forming for any M5 bar inside hour H,
        # so the last CLOSED H1 bar for hour H is H-1's row.
        h1s = h1[["ema_f", "ema_s"]].shift(1)
        d["h1up"] = d.hour.map(h1s.ema_f > h1s.ema_s)
        d["h1dn"] = d.hour.map(h1s.ema_f < h1s.ema_s)
        return d

    def signal(self, df, i, pos=None):
        c, o, hi, lo = df.close.iat[i], df.open.iat[i], df.high.iat[i], df.low.iat[i]
        ema_f, ema_s, atr = df.ema_f.iat[i], df.ema_s.iat[i], df.atr.iat[i]
        h1up, h1dn = df.h1up.iat[i], df.h1dn.iat[i]
        day, mod, dow = df.utcday.iat[i], df["mod"].iat[i], df.dow.iat[i]
        if day != self._cur_day:
            self._cur_day, self._trades_today = day, 0
        if np.isnan([ema_f, ema_s, atr]).any() or atr <= 0 or pd.isna(h1up) or i < 2:
            return None

        # --- manage an open position: time stop, session-end close, breakeven ratchet ---
        if pos is not None:
            bars_held = (i - self._entry_i) if self._entry_i is not None else 0
            if bars_held >= self.time_stop_bars or mod >= self.exit_min or dow >= 5:
                return Signal(0)
            if not self._be_done and bars_held >= 1 and self._entry_atr > 0:
                entry = pos.entry
                if pos.side > 0 and (c - entry) >= self.be_trigger_atr * self._entry_atr:
                    self._be_done = True
                    return Signal(None, move_sl=entry + self.be_lock_atr * self._entry_atr)
                if pos.side < 0 and (entry - c) >= self.be_trigger_atr * self._entry_atr:
                    self._be_done = True
                    return Signal(None, move_sl=entry - self.be_lock_atr * self._entry_atr)
            return None

        # --- entries: session + day-trade-cap gate, then H1-bias + M5 pullback ---
        if dow >= 5 or mod < self.start_min or mod >= self.end_min:
            return None
        if self._trades_today >= self.max_trades_day:
            return None
        lows3 = df.low.iloc[i - 2:i + 1]
        highs3 = df.high.iloc[i - 2:i + 1]
        band = self.touch_band_atr * atr
        touched_up = lows3.min() <= ema_f + band  # dipped to/through the fast EMA band
        touched_dn = highs3.max() >= ema_f - band
        resume_long = c > ema_f and c > ema_s and c > o and c > df.close.iat[i - 1]
        resume_short = c < ema_f and c < ema_s and c < o and c < df.close.iat[i - 1]

        if h1up and touched_up and resume_long:
            self._entry_i, self._entry_atr, self._be_done = i + 1, atr, False
            self._trades_today += 1
            return Signal(+1, sl=self.sl_atr * atr, tp=self.tp_atr * atr,
                          risk_pct=self.risk_pct, max_spread_frac=self.max_spread_frac)
        if h1dn and touched_dn and resume_short:
            self._entry_i, self._entry_atr, self._be_done = i + 1, atr, False
            self._trades_today += 1
            return Signal(-1, sl=self.sl_atr * atr, tp=self.tp_atr * atr,
                          risk_pct=self.risk_pct, max_spread_frac=self.max_spread_frac)
        return None


class XauM5VwapReversion(Strategy):
    """Research cycle #2 (2026-11-13). XAUUSD M5 anchored-VWAP mean-reversion scalp,
    gated by an ATR volatility-regime filter. Genuinely different mechanism from
    cycle #1 (mean reversion, not trend continuation): combines 2 concepts --
    session-anchored VWAP std-dev bands (mean reversion) + ATR14/ATR100 regime
    filter (to sit out abnormal-volatility "trend days", per web research showing
    VWAP-band fades are the "biggest mistake" on trend days: NexusFi VWAP write-up,
    DEV Community VWAP+volatility-filter strategy).

    Hypothesis: during normal (non-trending) volatility regimes, gold M5 price that
    wicks through the -2sigma/+2sigma band of the day's anchored VWAP and closes back
    inside it is exhausted and reverts toward VWAP before the session ends.

    No lookahead: VWAP/std are cumulative-sum-to-bar-i only (reset each UTC day);
    ATR14/ATR100 are trailing rolling means; the regime ratio and bands at bar i use
    only bars <= i. Entries fill at the next M5 bar's open (engine-enforced)."""

    name = "xau_m5_vwap_reversion"
    demo_base = 2000.0
    defaults = dict(contract=100.0, spread=0.25, commission=0.0, equity_pct=0.0)

    def __init__(self, atr_n=14, atr_base_n=100, regime_max=1.3, band_sigma=2.0,
                 be_sigma=1.0, sl_atr_buffer=0.3, min_rr=1.2, time_stop_bars=24,
                 start_min=480, end_min=1200, max_trades_day=8, risk_pct=0.5,
                 max_spread_frac=0.15):
        self.atr_n, self.atr_base_n, self.regime_max = atr_n, atr_base_n, regime_max
        self.band_sigma, self.be_sigma = band_sigma, be_sigma
        self.sl_atr_buffer, self.min_rr = sl_atr_buffer, min_rr
        self.time_stop_bars = time_stop_bars
        self.start_min, self.end_min = start_min, end_min
        self.max_trades_day, self.risk_pct, self.max_spread_frac = max_trades_day, risk_pct, max_spread_frac
        self.warmup = atr_base_n + 20
        self._cur_day, self._trades_today = None, 0
        self._entry_i, self._entry_1sig, self._be_done = None, 0.0, False

    def prepare(self, df):
        d = df.copy()
        d["utc"] = d.time - pd.to_timedelta(_vantage_offset_hours(d.time), unit="h")
        d["utcday"] = d.utc.dt.normalize()
        d["mod"] = d.utc.dt.hour * 60 + d.utc.dt.minute
        d["dow"] = d.utc.dt.day_of_week
        vol = d.get("tick_volume", pd.Series(1.0, index=d.index)).astype(float).clip(lower=1.0)
        tp = (d.high + d.low + d.close) / 3.0
        cum_v = vol.groupby(d.utcday).cumsum()
        cum_vtp = (vol * tp).groupby(d.utcday).cumsum()
        cum_vtp2 = (vol * tp * tp).groupby(d.utcday).cumsum()
        d["vwap"] = cum_vtp / cum_v
        var = (cum_vtp2 / cum_v - d["vwap"] ** 2).clip(lower=0.0)
        d["vwap_std"] = np.sqrt(var)
        d["up2"] = d.vwap + self.band_sigma * d.vwap_std
        d["dn2"] = d.vwap - self.band_sigma * d.vwap_std
        d["up1"] = d.vwap + self.be_sigma * d.vwap_std
        d["dn1"] = d.vwap - self.be_sigma * d.vwap_std
        pc = d.close.shift(1)
        tr = pd.concat([d.high - d.low, (d.high - pc).abs(), (d.low - pc).abs()], axis=1).max(axis=1)
        d["atr"] = tr.rolling(self.atr_n).mean()
        d["atr_base"] = tr.rolling(self.atr_base_n).mean()
        d["regime"] = d.atr / d.atr_base
        return d

    def signal(self, df, i, pos=None):
        c, o, hi, lo = df.close.iat[i], df.open.iat[i], df.high.iat[i], df.low.iat[i]
        vwap, up1, dn1, up2, dn2 = (df[k].iat[i] for k in ("vwap", "up1", "dn1", "up2", "dn2"))
        atr, regime = df.atr.iat[i], df.regime.iat[i]
        day, mod, dow = df.utcday.iat[i], df["mod"].iat[i], df.dow.iat[i]
        if day != self._cur_day:
            self._cur_day, self._trades_today = day, 0
        if np.isnan([vwap, up2, dn2, atr, regime]).any() or atr <= 0:
            return None

        if pos is not None:
            bars_held = (i - self._entry_i) if self._entry_i is not None else 0
            if bars_held >= self.time_stop_bars or mod >= self.end_min or dow >= 5:
                return Signal(0)
            if not self._be_done and bars_held >= 1:
                if pos.side > 0 and c >= self._entry_1sig:
                    self._be_done = True
                    return Signal(None, move_sl=pos.entry + 0.05 * atr)
                if pos.side < 0 and c <= self._entry_1sig:
                    self._be_done = True
                    return Signal(None, move_sl=pos.entry - 0.05 * atr)
            return None

        if dow >= 5 or mod < self.start_min or mod >= self.end_min:
            return None
        if self._trades_today >= self.max_trades_day:
            return None
        if regime > self.regime_max:  # abnormal-volatility "trend day" -- sit out
            return None

        long_sig = lo <= dn2 and c > dn2 and c > o
        short_sig = hi >= up2 and c < up2 and c < o
        if long_sig:
            sl_dist = (c - lo) + self.sl_atr_buffer * atr
            tp_dist = vwap - c
            if sl_dist > 0 and tp_dist >= self.min_rr * sl_dist:
                self._entry_i, self._entry_1sig, self._be_done = i + 1, up1, False
                self._trades_today += 1
                return Signal(+1, sl=sl_dist, tp=tp_dist, risk_pct=self.risk_pct,
                              max_spread_frac=self.max_spread_frac)
        if short_sig:
            sl_dist = (hi - c) + self.sl_atr_buffer * atr
            tp_dist = c - vwap
            if sl_dist > 0 and tp_dist >= self.min_rr * sl_dist:
                self._entry_i, self._entry_1sig, self._be_done = i + 1, dn1, False
                self._trades_today += 1
                return Signal(-1, sl=sl_dist, tp=tp_dist, risk_pct=self.risk_pct,
                              max_spread_frac=self.max_spread_frac)
        return None


STRATEGIES = {"sma_cross": SmaCross, "xau_donchian": XauDonchian, "xau_f7": XauF7, "xau_f7_k08": XauF7K08,
              "xau_m5_ema_pullback": XauM5EmaPullback, "xau_m5_vwap_reversion": XauM5VwapReversion}

# ----------------------------------------------------------------------------
# Broker / engine (shared by backtest and incubation)
# ----------------------------------------------------------------------------


@dataclass
class Trade:
    side: int
    entry_time: pd.Timestamp
    entry: float
    lots: float
    sl: float
    tp: float
    exit_time: pd.Timestamp | None = None
    exit: float = 0.0
    pnl: float = 0.0
    reason: str = ""


@dataclass
class Broker:
    balance: float = 10_000.0
    lots: float = 0.1
    contract: float = 100_000.0  # units per lot (FX = 100k)
    spread: float = 0.0  # full spread in price units, paid on entry (ask = mid+spread)
    slippage: float = 0.0  # price units lost per fill (entry, exit, SL/TP) vs the requested price
    commission: float = 0.0  # per lot, per side, account currency
    commission_pct: float = 0.0  # % of notional per side (TradingView style), e.g. 0.02
    equity_pct: float = 0.0  # >0: size = equity*pct% / (price*contract), 0.01 step; else fixed lots
    pos: Trade | None = None
    pending: Signal | None = None
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple] = field(default_factory=list)

    def _size(self, price):
        if self.equity_pct <= 0:
            return self.lots
        lots = np.floor(self.balance * self.equity_pct / 100 / (price * self.contract) / 0.01) * 0.01
        return round(max(lots, 0.01), 2)

    def _close(self, t, price, time_, reason):
        # longs exit at bid (= price), shorts exit at ask (= price + spread); slippage
        # always costs the trader, on top of the spread (SL/TP fills included)
        px = price - self.slippage if t.side > 0 else price + self.spread + self.slippage
        t.exit, t.exit_time, t.reason = px, time_, reason
        t.pnl = ((px - t.entry) * t.side * t.lots * self.contract - 2 * self.commission * t.lots
                 - self.commission_pct / 100 * (t.entry + px) * t.lots * self.contract)
        self.balance += t.pnl
        self.trades.append(t)
        self.pos = None

    def on_bar(self, ts, o, h, l, c, sig: Signal | None):
        """Process one CLOSED bar: fill pending order at open, check SL/TP, then
        record any new signal to fill at the next open."""
        # 1) fill pending order at this bar's open
        if self.pending is not None:
            p, self.pending = self.pending, None
            if p.direction is None:  # keep position, ratchet stop toward profit only
                t = self.pos
                if t is not None and p.move_sl:
                    t.sl = max(t.sl, p.move_sl) if t.side > 0 else (min(t.sl, p.move_sl) if t.sl else p.move_sl)
            else:
                if self.pos is not None and self.pos.side != p.direction:
                    self._close(self.pos, o, ts, "exit" if p.direction == 0 else "reverse")
                if p.direction != 0 and self.pos is None:
                    # buy fills pay spread+slippage, sell fills lose slippage
                    entry = o + self.spread + self.slippage if p.direction > 0 else o - self.slippage
                    sl = p.sl_abs or (entry - p.direction * p.sl if p.sl else 0.0)
                    tp = p.tp_abs or (entry + p.direction * p.tp if p.tp else 0.0)
                    dist = abs(entry - sl) if sl else 0.0
                    lots = self._size(o)
                    ok = True
                    if p.risk_pct:  # true risk sizing off the stop distance at the actual fill
                        ok = dist > 0 and (sl - entry) * p.direction < 0
                        if ok:
                            lots = np.floor(self.balance * p.risk_pct / 100 / (dist * self.contract) / 0.01) * 0.01
                            ok = lots >= 0.01
                    if ok and p.max_spread_frac and dist and self.spread > p.max_spread_frac * dist:
                        ok = False
                    if ok:
                        self.pos = Trade(p.direction, ts, entry, round(float(lots), 2), sl, tp)
        # 2) intrabar SL/TP (SL wins ties: conservative)
        t = self.pos
        if t is not None:
            if t.side > 0:
                if t.sl and l <= t.sl:
                    self._close(t, t.sl, ts, "sl")
                elif t.tp and h >= t.tp:
                    self._close(t, t.tp, ts, "tp")
            else:  # short: bid hits levels on ask side
                if t.sl and h + self.spread >= t.sl:
                    self._close(t, t.sl - self.spread, ts, "sl")
                elif t.tp and l + self.spread <= t.tp:
                    self._close(t, t.tp - self.spread, ts, "tp")
        # 3) mark-to-market equity
        eq = self.balance
        if self.pos is not None:
            mark = c if self.pos.side > 0 else c + self.spread
            eq += (mark - self.pos.entry) * self.pos.side * self.pos.lots * self.contract
        self.equity_curve.append((ts, eq))
        # 4) queue new signal for next open. `sig` may be a callable taking the
        #    position AFTER this bar was processed (what the EA sees at bar close).
        if callable(sig):
            sig = sig(self.pos)
        if sig is not None:
            self.pending = sig

    def close_open(self, ts, price):
        if self.pos is not None:
            self._close(self.pos, price, ts, "end")


# ----------------------------------------------------------------------------
# Stats / printing
# ----------------------------------------------------------------------------


def stats(b: Broker, start_balance: float) -> dict:
    tr = pd.DataFrame([t.__dict__ for t in b.trades])
    eq = pd.Series([e for _, e in b.equity_curve], dtype=float)
    out = {"trades": len(tr), "net": round(b.balance - start_balance, 2), "final_balance": round(b.balance, 2)}
    if len(tr):
        w, lo = tr[tr.pnl > 0].pnl, tr[tr.pnl <= 0].pnl
        out |= {
            "win_rate_%": round(100 * len(w) / len(tr), 1),
            "profit_factor": round(w.sum() / abs(lo.sum()), 2) if lo.sum() else float("inf"),
            "avg_trade": round(tr.pnl.mean(), 2),
            "best": round(tr.pnl.max(), 2),
            "worst": round(tr.pnl.min(), 2),
        }
    if len(eq):
        dd = (eq / eq.cummax() - 1).min()
        r = eq.pct_change().dropna()
        out["max_dd_%"] = round(100 * dd, 2)
        out["sharpe_per_bar"] = round(r.mean() / r.std(), 3) if r.std() > 0 else 0.0
    return out


def print_stats(title: str, s: dict):
    print(f"\n=== {title} ===")
    w = max(len(k) for k in s)
    for k, v in s.items():
        print(f"  {k:<{w}}  {v}")


def print_trade(t: Trade):
    side = "LONG " if t.side > 0 else "SHORT"
    print(f"  {t.exit_time:%Y-%m-%d %H:%M} {side} {t.entry:.5f} -> {t.exit:.5f} [{t.reason}] pnl {t.pnl:+.2f}")


# ----------------------------------------------------------------------------
# Data sources
# ----------------------------------------------------------------------------


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.lower().strip() for c in df.columns]
    df["time"] = pd.to_datetime(df["time"])
    return df.sort_values("time").reset_index(drop=True)


def demo_data(n=5000, seed=7, base=1.10) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    # trending/mean-reverting regime mix so strategies have something to find
    drift = np.repeat(rng.normal(0, 0.00004, n // 250 + 1), 250)[:n]
    ret = drift + rng.normal(0, 0.0007, n)
    close = base * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]]
    rng_ = (np.abs(rng.normal(0, 0.0005, n)) + 0.0002) * base / 1.10
    return pd.DataFrame({
        "time": pd.date_range("2023-01-02", periods=n, freq="h"),
        "open": open_, "high": np.maximum(open_, close) + rng_,
        "low": np.minimum(open_, close) - rng_, "close": close,
        "tick_volume": rng.integers(100, 2000, n),
    })


_TF = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15", "M30": "TIMEFRAME_M30",
       "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4", "D1": "TIMEFRAME_D1"}


def mt5_rates(symbol: str, tf: str, bars: int) -> pd.DataFrame:
    """Pull bars through the MT5 python API. Terminal must be running (it can be
    minimized / portable); we never touch its UI."""
    import MetaTrader5 as mt5  # lazy: only needed for fetch / live incubation

    if not mt5.initialize():
        sys.exit(f"MT5 initialize failed: {mt5.last_error()}  (is the terminal installed/running?)")
    try:
        mt5.symbol_select(symbol, True)
        r = mt5.copy_rates_from_pos(symbol, getattr(mt5, _TF[tf]), 0, bars)
        if r is None or len(r) == 0:
            sys.exit(f"No data for {symbol} {tf}: {mt5.last_error()}")
        df = pd.DataFrame(r)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df[["time", "open", "high", "low", "close", "tick_volume"]]
    finally:
        mt5.shutdown()


# ----------------------------------------------------------------------------
# Runners
# ----------------------------------------------------------------------------


def make_broker(a) -> Broker:
    d = STRATEGIES[a.strategy].defaults  # strategy's market defaults, CLI flags override
    g = lambda k, fallback: getattr(a, k) if getattr(a, k) is not None else d.get(k, fallback)
    return Broker(balance=a.balance, lots=a.lots, contract=g("contract", 100_000.0), spread=g("spread", 0.0001),
                  slippage=g("slippage", 0.0), commission=g("commission", 3.5), equity_pct=g("equity_pct", 0.0),
                  commission_pct=a.commission_pct or 0.0)


def run_backtest(a):
    strat = STRATEGIES[a.strategy]()
    df = demo_data(base=strat.demo_base) if a.demo else load_csv(a.csv)
    d = strat.prepare(df)
    first = strat.warmup
    if a.start:  # trade only from --start; earlier bars just warm up the indicators
        first = max(first, int((d.time < pd.Timestamp(a.start)).sum()))
    b = make_broker(a)
    t0 = time.perf_counter()
    for i in range(len(d)):
        fn = (lambda pos, i=i: strat.signal(d, i, pos)) if i >= first else None
        b.on_bar(d.time.iat[i], d.open.iat[i], d.high.iat[i], d.low.iat[i], d.close.iat[i], fn)
    b.close_open(d.time.iat[-1], d.close.iat[-1])
    dt = time.perf_counter() - t0
    print(f"{strat.name} | {len(d)} bars {d.time.iat[0]:%Y-%m-%d} -> {d.time.iat[-1]:%Y-%m-%d} | {dt:.2f}s")
    s = stats(b, a.balance)
    print_stats("BACKTEST", s)
    if a.trades:
        print("\nLast trades:")
        for t in b.trades[-a.trades:]:
            print_trade(t)
    if a.json:
        print(json.dumps(s))


def run_incubate(a):
    """Forward-test: bars arrive one at a time, the same engine decides/fills.
    --csv/--demo + --replay S replays history at S seconds/bar (simulated live).
    --symbol polls MT5 for newly closed bars (live)."""
    strat = STRATEGIES[a.strategy]()
    a.window = max(a.window, strat.min_window)
    b = make_broker(a)
    live = bool(a.symbol) and not (a.csv or a.demo)
    if live:
        hist = mt5_rates(a.symbol, a.tf, a.window + 1).iloc[:-1]  # drop forming bar
        feed = None
    else:
        full = demo_data(base=strat.demo_base) if a.demo else load_csv(a.csv)
        cut = len(full) - a.replay_bars
        hist, feed = full.iloc[:cut].reset_index(drop=True), full.iloc[cut:].reset_index(drop=True)
    print(f"incubating {strat.name} | {'LIVE ' + a.symbol + ' ' + a.tf if live else 'REPLAY'} | "
          f"warm history {len(hist)} bars | Ctrl+C to stop")
    shown = 0

    def step(row):
        nonlocal hist, shown
        hist = pd.concat([hist, row.to_frame().T], ignore_index=True).tail(a.window)
        hist = hist.astype({c: float for c in ("open", "high", "low", "close")})
        hist["time"] = pd.to_datetime(hist["time"])
        d = strat.prepare(hist)
        i = len(d) - 1
        fn = (lambda pos: strat.signal(d, i, pos)) if i >= strat.warmup else None
        b.on_bar(d.time.iat[i], d.open.iat[i], d.high.iat[i], d.low.iat[i], d.close.iat[i], fn)
        eq = b.equity_curve[-1][1]
        pos = "flat" if b.pos is None else ("LONG" if b.pos.side > 0 else "SHORT")
        pend = b.pending
        note = ""
        if pend is not None:
            note = " next-open: " + ("move-stop" if pend.direction is None else {1: "BUY", -1: "SELL", 0: "CLOSE"}[pend.direction])
        print(f"{d.time.iat[i]:%Y-%m-%d %H:%M} c={d.close.iat[i]:.2f} pos={pos:<5} "
              f"equity={eq:,.2f} trades={len(b.trades)}{note}")
        while shown < len(b.trades):
            print_trade(b.trades[shown])
            if a.log:  # append closed trades so a restart does not lose the record
                t = b.trades[shown]
                new = not os.path.exists(a.log)
                with open(a.log, "a", encoding="utf-8") as f:
                    if new:
                        f.write("strategy,side,entry_time,entry,lots,exit_time,exit,pnl,reason\n")
                    f.write(f"{strat.name},{t.side},{t.entry_time},{t.entry},{t.lots},{t.exit_time},{t.exit},{t.pnl:.4f},{t.reason}\n")
            shown += 1

    try:
        if feed is not None:
            for _, row in feed.iterrows():
                step(row)
                time.sleep(a.replay)
        else:
            last = hist.time.iat[-1]
            while True:
                time.sleep(a.poll)
                new = mt5_rates(a.symbol, a.tf, 3).iloc[:-1]  # closed bars only
                for _, row in new[new.time > last].iterrows():
                    step(row); last = row.time
    except KeyboardInterrupt:
        print("\nstopped.")
    print_stats("INCUBATION", stats(b, a.balance))


def run_fetch(a):
    df = mt5_rates(a.symbol, a.tf, a.bars)
    df.to_csv(a.out, index=False)
    print(f"saved {len(df)} bars {df.time.iat[0]} -> {df.time.iat[-1]} to {a.out}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--csv"); sp.add_argument("--demo", action="store_true")
        sp.add_argument("--strategy", default="sma_cross", choices=STRATEGIES)
        sp.add_argument("--balance", type=float, default=10_000.0)
        sp.add_argument("--lots", type=float, default=0.1)
        sp.add_argument("--contract", type=float, help="units per lot (default: strategy's, else 100000)")
        sp.add_argument("--spread", type=float, help="price units (default: strategy's, else 0.0001)")
        sp.add_argument("--slippage", type=float,
                         help="price units lost on every fill incl. SL/TP (default: strategy's, else 0). "
                              "E.g. for XAUUSD.sc (point 0.01) 3 points of slippage is --slippage 0.03")
        sp.add_argument("--commission", type=float, help="per lot per side (default: strategy's, else 3.5)")
        sp.add_argument("--commission-pct", type=float, dest="commission_pct", help="%% of notional per side, e.g. 0.02")
        sp.add_argument("--start", help="only trade from this date (earlier bars used as warmup), e.g. 2025-09-22")
        sp.add_argument("--equity-pct", type=float, dest="equity_pct", help="size by %% of equity instead of --lots")

    bt = sub.add_parser("backtest"); common(bt)
    bt.add_argument("--trades", type=int, default=5, help="print last N trades"); bt.add_argument("--json", action="store_true")
    inc = sub.add_parser("incubate"); common(inc)
    inc.add_argument("--symbol"); inc.add_argument("--tf", default="H1", choices=_TF)
    inc.add_argument("--window", type=int, default=500, help="bars kept in memory")
    inc.add_argument("--poll", type=float, default=15, help="live poll seconds")
    inc.add_argument("--replay", type=float, default=0.05, help="seconds per replayed bar")
    inc.add_argument("--replay-bars", type=int, default=300, dest="replay_bars")
    inc.add_argument("--log", help="append every closed trade to this CSV")
    ft = sub.add_parser("fetch")
    ft.add_argument("--symbol", required=True); ft.add_argument("--tf", default="H1", choices=_TF)
    ft.add_argument("--bars", type=int, default=20000); ft.add_argument("--out", required=True)

    a = p.parse_args()
    if a.cmd == "backtest" and not (a.csv or a.demo):
        p.error("backtest needs --csv or --demo")
    if a.cmd == "incubate" and not (a.csv or a.demo or a.symbol):
        p.error("incubate needs --csv, --demo or --symbol")
    {"backtest": run_backtest, "incubate": run_incubate, "fetch": run_fetch}[a.cmd](a)


if __name__ == "__main__":
    main()


