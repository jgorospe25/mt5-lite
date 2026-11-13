#!/usr/bin/env python
"""
mt5gui - small desktop GUI to run and watch strategy backtests and forward tests (incubations).

  * pick a researcher strategy folder from the dropdown (folders next to this file, or add your own)
  * start new backtests / incubations, pause and resume incubations
  * equity curve: backtest vs forward (timeline view, or aligned view vs the backtest's own range)
  * how long each incubation has been running, and a backtest-vs-forward data table (exportable)

Every instance is a separate background process (mt5runner.py) with its files in instances/<id>/,
so closing this window does not stop an incubation.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import matplotlib

matplotlib.use("TkAgg")
import matplotlib.dates as mdates  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

ROOT = Path(__file__).resolve().parent
INST = ROOT / "instances"
SETTINGS = ROOT / "gui_settings.json"
TFS = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]

# Which python port (mt5lite.py) belongs to which researcher folder, and how .set inputs map to its parameters.
STRAT_MAP = {
    "xau_donchian": dict(
        match=("donchian",), tf="H1", symbol="XAUUSD.sc",
        params={"InpEntryChannel": ("entry", int), "InpExitChannel": ("exit_", int), "InpTrendEMA": ("ema", int),
                "InpAtrPeriod": ("atr_n", int), "InpSlAtrMult": ("sl_atr", float), "InpTrailAtrMult": ("trail_atr", float)},
        broker={"InpEquityPercent": "equity_pct"}),
    "xau_f7": dict(
        match=("volbreakout", "f7_", "f7 "), tf="M15", symbol="XAUUSD.sc",
        params={"InpK": ("k", float), "InpTpR": ("tp_r", float), "InpStartMin": ("start_min", int),
                "InpEndMin": ("end_min", int), "InpExitMin": ("exit_min", int), "InpMaxRiskPR": ("max_risk_pr", float),
                "InpRiskPercent": ("risk_pct", float), "InpMaxSpreadFracR": ("max_spread_frac", float)},
        broker={}),
}


# ------------------------------------------------------------------ helpers


def jload(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def fmt_dur(sec):
    sec = max(0, int(sec))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return (f"{d}d " if d else "") + f"{h}h {m:02d}m" if (d or h) else f"{m}m {sec % 60:02d}s"


def read_set(path: Path) -> dict:
    raw = path.read_bytes()
    txt = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in raw[:20] else raw.decode("utf-8-sig", "ignore")
    out = {}
    for ln in txt.splitlines():
        if "=" in ln and not ln.lstrip().startswith(";"):
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip().split("||")[0]
    return out


def detect(folder: Path):
    """-> (strategy key or None, info). Looks at file names and README text."""
    text = folder.name.lower() + " " + " ".join(f.name.lower() for f in folder.iterdir())
    rd = folder / "README.md"
    if rd.exists():
        text += " " + rd.read_text(encoding="utf-8", errors="ignore")[:600].lower()
    for key, info in STRAT_MAP.items():
        if any(s in text for s in info["match"]):
            return key, info
    return None, None


def presets_for(folder: Path, key: str) -> dict:
    info = STRAT_MAP[key]
    out = {"EA defaults": {"params": {}, "equity_pct": None}}
    for sf in sorted(folder.glob("*.set")):
        try:
            s = read_set(sf)
        except Exception:  # noqa: BLE001
            continue
        params = {pn: cast(s[inp]) for inp, (pn, cast) in info["params"].items() if inp in s}
        eq = {v: float(s[k]) for k, v in info["broker"].items() if k in s}
        out[sf.stem] = {"params": params, "equity_pct": eq.get("equity_pct")}
    return out


def scan_folders(extra) -> dict:
    cands = [p for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name not in ("instances", "__pycache__")]
    cands += [Path(e) for e in extra if Path(e).is_dir()]
    out = {}
    for p in cands:
        if any(p.glob("*.mq5")) or any(p.glob("*.pine")) or any(p.glob("*.set")):
            out[p.name] = p
    return out


_cache: dict = {}


def read_csv_cached(path: Path, **kw):
    try:
        key, mt = str(path), path.stat().st_mtime
    except OSError:
        return None
    if _cache.get(key, (None,))[0] != mt:
        try:
            _cache[key] = (mt, pd.read_csv(path, **kw))
        except Exception:  # noqa: BLE001
            return None
    return _cache[key][1]


def load_eq(path):
    return read_csv_cached(Path(path), parse_dates=["time"])


def load_trades(path):
    df = read_csv_cached(Path(path), parse_dates=["entry_time", "exit_time"])
    return df if df is not None else pd.DataFrame(columns=["pnl", "exit_time"])


def metrics(eq, trades, base=None):
    """Comparable stats for an equity curve (+ trades). eq: time,equity."""
    if eq is None or len(eq) < 2:
        return {}
    e = eq.equity.astype(float)
    base = e.iloc[0] if base is None else base
    days = (eq.time.iloc[-1] - eq.time.iloc[0]).total_seconds() / 86400
    n = len(trades)
    out = {"Period (days)": f"{days:.1f}", "Return %": f"{(e.iloc[-1] / base - 1) * 100:+.2f}",
           "Max drawdown %": f"{((e / e.cummax()) - 1).min() * 100:.2f}", "Trades": str(n),
           "Trades / month": f"{n / days * 30.44:.1f}" if days > 0 else "-"}
    if n:
        p = trades.pnl.astype(float)
        w, lo = p[p > 0], p[p <= 0]
        out["Win rate %"] = f"{100 * len(w) / n:.1f}"
        out["Profit factor"] = f"{w.sum() / abs(lo.sum()):.2f}" if lo.sum() else "inf"
        out["Avg trade"] = f"{p.mean():+.2f}"
        out["Best / worst"] = f"{p.max():+.1f} / {p.min():+.1f}"
    else:
        out.update({"Win rate %": "-", "Profit factor": "-", "Avg trade": "-", "Best / worst": "-"})
    return out


def aligned_band(be_daily: pd.Series, days: int):
    """Percentile band of what the backtest did over any window of 0..days days (calendar days)."""
    a = be_daily.to_numpy(float)
    n = len(a)
    days = max(2, min(days, n - 2))
    xs, lo, mid, hi = np.arange(days + 1), [], [], []
    for d in xs:
        r = (a[d:] / a[: n - d] - 1) * 100
        lo.append(np.percentile(r, 10)); mid.append(np.percentile(r, 50)); hi.append(np.percentile(r, 90))
    return xs, np.array(lo), np.array(mid), np.array(hi)


# ---------------------------------------------------------------------- app


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MT5 lite - backtest vs forward")
        self.geometry("1380x860")
        self.settings = jload(SETTINGS, {"extra_folders": []})
        self.procs: dict[str, subprocess.Popen] = {}
        self.folders: dict[str, Path] = {}
        self.presets: dict = {}
        self.sel_sig = None
        INST.mkdir(exist_ok=True)
        self._build()
        self.reload_folders()
        self.refresh_list()
        self.after(2000, self.tick)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- layout
    def _build(self):
        ttk.Style(self).theme_use("clam")
        f = ttk.LabelFrame(self, text="New instance", padding=6)
        f.pack(fill="x", padx=8, pady=(8, 4))
        self.v = {k: tk.StringVar(value=d) for k, d in dict(
            folder="", preset="", symbol="XAUUSD.sc", tf="H1", balance="10000", spread="", start="", feed="live",
            rbars="300", speed="0.5").items()}
        self.refresh = tk.BooleanVar(value=True)

        def row(r, items):
            for c, (label, widget) in enumerate(items):
                ttk.Label(f, text=label).grid(row=r, column=2 * c, sticky="e", padx=(8, 2), pady=2)
                widget.grid(row=r, column=2 * c + 1, sticky="w", pady=2)

        self.cb_folder = ttk.Combobox(f, textvariable=self.v["folder"], state="readonly", width=24)
        self.cb_folder.bind("<<ComboboxSelected>>", lambda e: self.on_folder())
        self.cb_preset = ttk.Combobox(f, textvariable=self.v["preset"], state="readonly", width=28)
        self.lbl_port = ttk.Label(f, text="", foreground="#a00")
        row(0, [("Strategy folder", self.cb_folder), ("Preset (.set)", self.cb_preset)])
        ttk.Button(f, text="Add folder…", command=self.add_folder).grid(row=0, column=4, padx=6)
        ttk.Button(f, text="Rescan", command=self.reload_folders).grid(row=0, column=5)
        self.lbl_port.grid(row=0, column=6, columnspan=4, sticky="w", padx=8)
        row(1, [("Symbol", ttk.Entry(f, textvariable=self.v["symbol"], width=12)),
                ("Timeframe", ttk.Combobox(f, textvariable=self.v["tf"], values=TFS, state="readonly", width=6)),
                ("Balance", ttk.Entry(f, textvariable=self.v["balance"], width=10)),
                ("Spread (blank = default)", ttk.Entry(f, textvariable=self.v["spread"], width=8))])
        row(2, [("Backtest from (YYYY-MM-DD)", ttk.Entry(f, textvariable=self.v["start"], width=12)),
                ("Incubation feed", ttk.Combobox(f, textvariable=self.v["feed"], values=["live", "replay"], state="readonly", width=8)),
                ("Replay bars", ttk.Entry(f, textvariable=self.v["rbars"], width=7)),
                ("Replay s/bar", ttk.Entry(f, textvariable=self.v["speed"], width=6))])
        ttk.Checkbutton(f, text="Refresh data from MT5 first", variable=self.refresh).grid(row=2, column=8, padx=8)
        bf = ttk.Frame(f)
        bf.grid(row=3, column=0, columnspan=10, sticky="w", pady=(6, 0))
        ttk.Button(bf, text="▶  New backtest", command=lambda: self.create("backtest")).pack(side="left", padx=4)
        ttk.Button(bf, text="▶  New incubation (forward test)", command=lambda: self.create("incubation")).pack(side="left", padx=4)
        ttk.Label(bf, text="  Live feed = real MT5 bars (terminal must be running). Replay = fast pipeline test on old bars.",
                  foreground="#666").pack(side="left")

        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=8, pady=4)
        left = ttk.Frame(pw, width=470)
        right = ttk.Frame(pw)
        pw.add(left, weight=0)
        pw.add(right, weight=1)

        cols = ("name", "kind", "status", "running", "trades", "ret")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=11, selectmode="browse")
        for c, t, w in zip(cols, ("Instance", "Type", "Status", "Running for", "Trades", "Return %"), (190, 70, 70, 85, 50, 65)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c == "name" else "center")
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.redraw(force=True))
        bb = ttk.Frame(left)
        bb.pack(fill="x", pady=4)
        for t, cmd in (("⏸ Pause", self.pause), ("⏵ Resume", self.resume), ("✕ Remove", self.remove), ("⇩ Export data", self.export)):
            ttk.Button(bb, text=t, command=cmd).pack(side="left", padx=2)
        ttk.Label(left, text="Runner log (terminal output)").pack(anchor="w")
        self.log = tk.Text(left, height=18, font=("Consolas", 9), bg="#111", fg="#cfd", state="disabled")
        self.log.pack(fill="both", expand=True)

        self.info = ttk.Label(right, text="Select or create an instance.", font=("Segoe UI", 10, "bold"), wraplength=820, justify="left")
        self.info.pack(anchor="w")
        vf = ttk.Frame(right)
        vf.pack(fill="x")
        self.view = tk.StringVar(value="timeline")
        self.zoom = tk.StringVar(value="all")
        for t, val in (("Timeline", "timeline"), ("Aligned: forward vs backtest range", "aligned")):
            ttk.Radiobutton(vf, text=t, value=val, variable=self.view, command=lambda: self.redraw(force=True)).pack(side="left", padx=6)
        ttk.Label(vf, text="   Backtest shown:").pack(side="left")
        zc = ttk.Combobox(vf, textvariable=self.zoom, values=["all", "365d", "180d", "90d", "30d"], state="readonly", width=6)
        zc.pack(side="left")
        zc.bind("<<ComboboxSelected>>", lambda e: self.redraw(force=True))
        self.fig = Figure(figsize=(7, 4.6), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.stats = ttk.Treeview(right, columns=("m", "a", "b", "c"), show="headings", height=11)
        for c, t, w in zip(("m", "a", "b", "c"), ("Metric", "Backtest (all)", "Backtest (same length)", "Forward"), (170, 150, 170, 150)):
            self.stats.heading(c, text=t)
            self.stats.column(c, width=w, anchor="w" if c == "m" else "center")
        self.stats.pack(fill="x")

    # ---------- folders / presets
    def reload_folders(self):
        self.folders = scan_folders(self.settings.get("extra_folders", []))
        self.cb_folder["values"] = list(self.folders)
        if self.folders and self.v["folder"].get() not in self.folders:
            self.v["folder"].set(next(iter(self.folders)))
        self.on_folder()

    def add_folder(self):
        p = filedialog.askdirectory(title="Strategy folder (with .mq5 / .set files)")
        if p:
            self.settings.setdefault("extra_folders", []).append(p)
            SETTINGS.write_text(json.dumps(self.settings), encoding="utf-8")
            self.reload_folders()
            self.v["folder"].set(Path(p).name)
            self.on_folder()

    def on_folder(self):
        name = self.v["folder"].get()
        self.key = None
        self.cb_preset["values"] = []
        self.v["preset"].set("")
        if name not in self.folders:
            return
        key, info = detect(self.folders[name])
        if key is None:
            self.lbl_port.config(text="⚠ no Python port of this strategy yet - port the .mq5 into mt5lite.py (see README)")
            return
        self.key = key
        self.lbl_port.config(text=f"python port: {key}", foreground="#060")
        self.presets = presets_for(self.folders[name], key)
        self.cb_preset["values"] = list(self.presets)
        self.v["preset"].set(next((p for p in self.presets if p != "EA defaults"), "EA defaults"))
        self.v["tf"].set(info["tf"])
        self.v["symbol"].set(info["symbol"])

    # ---------- instances
    def create(self, kind):
        if not self.key:
            messagebox.showwarning("No port", "This folder has no Python port yet, so it cannot be run here.")
            return
        try:
            bal = float(self.v["balance"].get())
            sp = float(self.v["spread"].get()) if self.v["spread"].get().strip() else None
            start = self.v["start"].get().strip() or None
            if start:
                pd.Timestamp(start)
        except ValueError:
            messagebox.showerror("Bad input", "Balance/spread must be numbers and the date YYYY-MM-DD.")
            return
        pr = self.presets[self.v["preset"].get()]
        sym, tf = self.v["symbol"].get().strip(), self.v["tf"].get()
        iid = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{kind[:3]}_{self.v['folder'].get()}_{self.v['preset'].get()}_{time.strftime('%m%d_%H%M%S')}")
        meta = dict(id=iid, kind=kind, folder=self.v["folder"].get(), preset=self.v["preset"].get(), strategy=self.key,
                    params=pr["params"], equity_pct=pr["equity_pct"], symbol=sym, tf=tf, balance=bal, spread=sp,
                    start=start, csv=str(ROOT / f"{sym.replace('.', '_')}_{tf}.csv"), refresh=self.refresh.get(),
                    feed=self.v["feed"].get(), replay_bars=int(self.v["rbars"].get() or 300),
                    speed=float(self.v["speed"].get() or 0.5), poll=15, status="starting", created_at=time.time())
        d = INST / iid
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        self.spawn(iid)
        self.refresh_list(select=iid)

    def spawn(self, iid):
        d = INST / iid
        py = Path(sys.executable)
        if py.name.lower() == "pythonw.exe":
            py = py.with_name("python.exe")
        log = open(d / "log.txt", "a", encoding="utf-8", buffering=1)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.procs[iid] = subprocess.Popen([str(py), "-u", str(ROOT / "mt5runner.py"), "--dir", str(d)], cwd=ROOT,
                                           stdout=log, stderr=subprocess.STDOUT, creationflags=flags)

    def alive(self, iid, meta):
        p = self.procs.get(iid)
        if p is not None and p.poll() is None:
            return True
        return meta.get("status") in ("running", "starting") and time.time() - meta.get("heartbeat", 0) < 60

    def selected(self):
        s = self.tree.selection()
        return s[0] if s else None

    def pause(self):
        iid = self.selected()
        m = iid and jload(INST / iid / "meta.json")
        if not m or m["kind"] != "incubation" or not self.alive(iid, m):
            return
        (INST / iid / "stop").write_text("pause")
        self.after(25000, lambda: self.procs.get(iid) and self.procs[iid].poll() is None and self.procs[iid].terminate())

    def resume(self):
        iid = self.selected()
        m = iid and jload(INST / iid / "meta.json")
        if not m or m["kind"] != "incubation" or self.alive(iid, m):
            return
        if not (INST / iid / "state.pkl").exists() and m.get("status") != "paused":
            messagebox.showinfo("Resume", "Nothing to resume yet (no saved state).")
            return
        self.spawn(iid)

    def remove(self):
        iid = self.selected()
        if not iid or not messagebox.askyesno("Remove", f"Delete {iid} and all its data?"):
            return
        p = self.procs.pop(iid, None)
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except Exception:  # noqa: BLE001
                pass
        (INST / iid / "stop").write_text("x") if (INST / iid).exists() else None
        time.sleep(0.3)
        shutil.rmtree(INST / iid, ignore_errors=True)
        self.refresh_list()
        self.redraw(force=True)

    def export(self):
        iid = self.selected()
        if not iid:
            return
        dest = filedialog.askdirectory(title="Export instance data to…")
        if not dest:
            return
        out = Path(dest) / iid
        out.mkdir(parents=True, exist_ok=True)
        for f in (INST / iid).glob("*.csv"):
            shutil.copy(f, out / f.name)
        shutil.copy(INST / iid / "meta.json", out / "meta.json")
        pd.DataFrame(self.last_table, columns=["metric", "backtest_all", "backtest_same_length", "forward"]).to_csv(out / "comparison.csv", index=False)
        messagebox.showinfo("Exported", f"Saved equity curves, trades and comparison.csv to\n{out}")

    # ---------- refresh
    def refresh_list(self, select=None):
        seen = set()
        for d in sorted(INST.glob("*/meta.json"), key=lambda p: p.stat().st_mtime):
            m = jload(d)
            if not m:
                continue
            iid = m["id"]
            seen.add(iid)
            alive = self.alive(iid, m)
            st = m.get("status", "?")
            if st == "running" and not alive:
                st = "stopped"
            if st == "running" and (INST / iid / "stop").exists():
                st = "pausing"
            if st == "running" and m.get("phase") == "baseline backtest":
                st = f"baseline {m.get('progress', 0) * 100:.0f}%"
            elif st == "running" and m["kind"] == "backtest":
                st = f"running {m.get('progress', 0) * 100:.0f}%"
            run = ""
            if m["kind"] == "incubation" and m.get("started_at"):
                end = time.time() if m.get("status") in ("running", "starting", "paused") else m.get("finished_at", time.time())
                run = fmt_dur(end - m["started_at"])
            elif m.get("started_at") and m.get("finished_at"):
                run = fmt_dur(m["finished_at"] - m["started_at"])
            if m["kind"] == "incubation":
                ret = f"{(m['equity'] / m['balance'] - 1) * 100:+.2f}" if m.get("equity") else ""
                n = m.get("n_trades", "")
            else:
                s = m.get("stats", {})
                ret = f"{s.get('net', 0) / m['balance'] * 100:+.2f}" if s else ""
                n = s.get("trades", "")
            vals = (f"{m['folder']} / {m['preset']}", "backtest" if m["kind"] == "backtest" else "forward", st, run, n, ret)
            if self.tree.exists(iid):
                self.tree.item(iid, values=vals)
            else:
                self.tree.insert("", "end", iid=iid, values=vals)
        for iid in list(self.tree.get_children()):
            if iid not in seen:
                self.tree.delete(iid)
        if select:
            self.tree.selection_set(select)

    def tick(self):
        try:
            self.refresh_list()
            self.redraw()
        finally:
            self.after(2000, self.tick)

    # ---------- drawing
    def redraw(self, force=False):
        iid = self.selected()
        if not iid:
            if force:
                self.fig.clear(); self.canvas.draw_idle(); self.info.config(text="Select or create an instance.")
            return
        d = INST / iid
        files = [d / n for n in ("meta.json", "equity.csv", "backtest_equity.csv", "log.txt")]
        sig = (iid, self.view.get(), self.zoom.get(), tuple(f.stat().st_mtime if f.exists() else 0 for f in files))
        if not force and sig == self.sel_sig:
            if jload(d / "meta.json", {}).get("status") != "running":
                return
        self.sel_sig = sig
        m = jload(d / "meta.json")
        if not m:
            return
        self.show_log(d / "log.txt")
        self.show_info(m)
        try:
            self.draw_charts(m, d)
        except Exception as e:  # noqa: BLE001
            self.fig.clear()
            self.fig.text(0.5, 0.5, f"chart error: {e}", ha="center")
        self.canvas.draw_idle()

    def show_log(self, p):
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()[-14:]
        except OSError:
            lines = []
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", "\n".join(l[-90:] for l in lines))
        self.log.config(state="disabled")
        self.log.see("end")

    def show_info(self, m):
        st = m.get("status")
        txt = f"{m['folder']} / {m['preset']}   |   {m['symbol']} {m['tf']}   |   status: {st}"
        if m.get("error"):
            txt += f"\nERROR: {m['error']}"
        if m["kind"] == "incubation" and m.get("started_at"):
            now = time.time() if st in ("running", "starting", "paused") else m.get("finished_at", time.time())
            total = now - m["started_at"]
            act = m.get("active_seconds", 0)
            txt += (f"\nIncubating since {time.strftime('%Y-%m-%d %H:%M', time.localtime(m['started_at']))}  -  "
                    f"elapsed {fmt_dur(total)}  (active {fmt_dur(act)}, paused/off {fmt_dur(max(0, total - act))})"
                    f"   |   forward starts after bar {m.get('forward_start_bar', '?')}   |   last bar {m.get('last_bar', '-')}")
        self.info.config(text=txt)

    def draw_charts(self, m, d):
        self.fig.clear()
        for r in self.stats.get_children():
            self.stats.delete(r)
        self.last_table = []
        bal = m["balance"]
        if m["kind"] == "backtest":
            eq = load_eq(d / "equity.csv")
            if eq is None or len(eq) < 2:
                self.fig.text(0.5, 0.5, "backtest running…" if m.get("status") != "error" else "no data", ha="center")
                return
            ax1 = self.fig.add_subplot(211)
            ax2 = self.fig.add_subplot(212, sharex=ax1)
            e = eq.set_index("time").equity
            ax1.plot(e.index, (e / bal - 1) * 100, color="#1f6fb5", lw=1.1, label="backtest")
            ax2.fill_between(e.index, (e / e.cummax() - 1) * 100, 0, color="#c0392b", alpha=.5)
            ax1.set_ylabel("Return %"); ax2.set_ylabel("Drawdown %"); ax1.legend(loc="upper left"); ax1.grid(alpha=.3); ax2.grid(alpha=.3)
            self.fmt_dates(ax2)
            rows = metrics(eq, load_trades(d / "trades.csv"), bal)
            self.fill_table({k: (v, "", "") for k, v in rows.items()})
            return
        # incubation: baseline (backtest up to start) vs forward
        be = load_eq(d / "backtest_equity.csv")
        fe = load_eq(d / "equity.csv")
        if be is None or len(be) < 2:
            self.fig.text(0.5, 0.5, "running baseline backtest…", ha="center")
            return
        bd = be.set_index("time").equity.resample("D").last().ffill().dropna()
        have_f = fe is not None and len(fe) >= 1
        f = fe.set_index("time").equity if have_f else None
        if self.view.get() == "timeline":
            z = self.zoom.get()
            if z != "all":
                bd = bd[bd.index >= bd.index[-1] - pd.Timedelta(days=int(z[:-1]))]
            ax1 = self.fig.add_subplot(211)
            ax2 = self.fig.add_subplot(212, sharex=ax1)
            ax1.plot(bd.index, (bd / bal - 1) * 100, color="#1f6fb5", lw=1.1, label="backtest (up to forward start)")
            stitched = bd.copy()
            if have_f and len(f) > 1:
                base_end = be.equity.iloc[-1]
                fr = base_end * f / f.iloc[0]
                ax1.plot(fr.index, (fr / bal - 1) * 100, color="#e67e22", lw=1.6, label="forward (rebased to backtest end)")
                stitched = pd.concat([bd[bd.index < fr.index[0]], fr])
            ax1.axvline(pd.Timestamp(m.get("forward_start_bar", bd.index[-1])), color="#888", ls="--", lw=1)
            ax2.fill_between(stitched.index, (stitched / stitched.cummax() - 1) * 100, 0, color="#c0392b", alpha=.5)
            ax1.set_ylabel("Return % (of start balance)"); ax2.set_ylabel("Drawdown %")
            ax1.legend(loc="upper left"); ax1.grid(alpha=.3); ax2.grid(alpha=.3)
            self.fmt_dates(ax2)
        else:
            ax = self.fig.add_subplot(111)
            fdays = (f.index[-1] - f.index[0]).total_seconds() / 86400 if have_f and len(f) > 1 else 0
            xs, lo, mid, hi = aligned_band(bd, int(fdays * 1.5) + 14 if fdays < 200 else int(fdays + 30))
            ax.fill_between(xs, lo, hi, color="#1f6fb5", alpha=.18, label="backtest range (10-90th pct of all start dates)")
            ax.plot(xs, mid, color="#1f6fb5", lw=1.2, label="backtest median")
            if have_f and len(f) > 1:
                fx = (f.index - f.index[0]).total_seconds() / 86400
                ax.plot(fx, (f / f.iloc[0] - 1) * 100, color="#e67e22", lw=1.8, label="forward")
            ax.axhline(0, color="#888", lw=.8)
            ax.set_xlabel("days since forward start"); ax.set_ylabel("Return %"); ax.legend(loc="upper left"); ax.grid(alpha=.3)
        self.fig.tight_layout()
        # comparison table
        bt = load_trades(d / "backtest_trades.csv")
        ft = load_trades(d / "trades.csv")
        full = metrics(be, bt)
        fwd = metrics(fe, ft) if have_f and len(fe) > 1 else {}
        same = {}
        if fwd:
            fdays = (fe.time.iloc[-1] - fe.time.iloc[0]).total_seconds() / 86400
            cut = be.time.iloc[-1] - pd.Timedelta(days=max(fdays, 0.01))
            bes = be[be.time >= cut]
            same = metrics(bes, bt[bt.exit_time >= cut]) if len(bes) > 1 else {}
        self.fill_table({k: (full.get(k, ""), same.get(k, ""), fwd.get(k, "")) for k in (full or fwd)})

    def fill_table(self, rows):
        self.last_table = [(k, *v) for k, v in rows.items()]
        for k, v in rows.items():
            self.stats.insert("", "end", values=(k, *v))

    @staticmethod
    def fmt_dates(ax):
        loc = mdates.AutoDateLocator()
        ax.xaxis.set_major_locator(loc)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))

    def on_close(self):
        running = [i for i, p in self.procs.items() if p.poll() is None]
        if running:
            a = messagebox.askyesnocancel("Close", f"{len(running)} instance(s) are still running.\n\nYes = pause them and close\nNo = leave them running in the background and close")
            if a is None:
                return
            if a:
                for i in running:
                    (INST / i / "stop").write_text("pause")
                for i in running:
                    try:
                        self.procs[i].wait(20)
                    except Exception:  # noqa: BLE001
                        self.procs[i].terminate()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
