#!/usr/bin/env python3
"""Live plot of voltage, current and internal resistance from a cycling_/monitor_/transport_*.csv log.

The file is followed like `tail -f`: every --interval seconds only newly appended
lines are read and the new points are added to the graph.

Usage:
  ./plotlog.py                     newest cycling_/monitor_/transport_*.csv in the current directory
  ./plotlog.py cycling_X.csv       given file
  ./plotlog.py -i 5 --last 3       refresh every 5 s, show only the last 3 hours
"""
import argparse
import csv
import glob
import os
import sys
from datetime import datetime, timedelta

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.animation import FuncAnimation

COLORS = {0: "tab:blue", 1: "tab:orange", 2: "tab:green", 3: "tab:red"}


class LogFollower:
    """Reads only complete new lines appended to a CSV file."""

    def __init__(self, path):
        self.path = path
        self.pos = 0
        self.header = None
        self.rest = ""

    def new_rows(self):
        try:
            if os.path.getsize(self.path) < self.pos:     # file truncated / replaced
                self.pos, self.header, self.rest = 0, None, ""
            with open(self.path, newline="") as f:
                f.seek(self.pos)
                chunk = f.read()
                self.pos = f.tell()
        except OSError:
            return []
        text = self.rest + chunk
        lines = text.split("\n")
        self.rest = lines.pop()                            # incomplete last line, wait for the rest
        rows = []
        for r in csv.reader(lines):
            if not r:
                continue
            if self.header is None:
                self.header = r
                continue
            rows.append(dict(zip(self.header, r)))
        return rows


def newest_log():
    files = glob.glob("cycling_*.csv") + glob.glob("monitor_*.csv") + glob.glob("transport_*.csv")
    return max(files, key=os.path.getmtime) if files else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="CSV log (default: newest in current directory)")
    ap.add_argument("-i", "--interval", type=float, default=2.0, help="refresh period [s] (default 2)")
    ap.add_argument("--last", type=float, metavar="H", help="show only the last H hours")
    args = ap.parse_args()

    path = args.file or newest_log()
    if not path:
        sys.exit("no cycling_/monitor_/transport_*.csv found")
    print(f"following {path}  (close the window to quit)")

    log = LogFollower(path)
    data = {}       # ch -> {"t": [], "U": [], "I": [], "rt": [], "R": [], "state": str}

    fig, (axu, axi, axr) = plt.subplots(3, 1, sharex=True, figsize=(11, 9),
                                        gridspec_kw={"height_ratios": [3, 3, 2]})
    fig.canvas.manager.set_window_title(os.path.basename(path))
    axu.set_ylabel("U [V]")
    axi.set_ylabel("I [A]")
    axr.set_ylabel("R [mOhm]")
    axi.axhline(0, color="gray", lw=0.6)
    for ax in (axu, axi, axr):
        ax.grid(True, alpha=0.3)
    axr.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    lines = {}
    title = fig.suptitle(path)

    def update(_):
        rows = log.new_rows()
        for r in rows:
            try:
                ch = int(r["ch"])
                t = datetime.fromisoformat(r["time"])
                u = int(r["U_mV"]) / 1000
                i = int(r["I_mA"]) / 1000
            except (KeyError, ValueError):
                continue
            d = data.setdefault(ch, {"t": [], "U": [], "I": [], "rt": [], "R": [], "state": ""})
            d["state"] = r.get("state", "")
            if r.get("R_mOhm"):
                d["rt"].append(t); d["R"].append(float(r["R_mOhm"]))
            if d["state"].startswith("DISCHARGE") and r.get("load") == "0":
                continue        # load briefly OFF for the R measurement - keep U/I curves clean
            d["t"].append(t); d["U"].append(u); d["I"].append(i)
        if not rows and lines:
            return []
        for ch in sorted(data):
            d = data[ch]
            if ch not in lines:
                c = COLORS.get(ch)
                lines[ch] = (axu.plot([], [], color=c, lw=1.2)[0], axi.plot([], [], color=c, lw=1.2)[0],
                             axr.plot([], [], "o-", color=c, lw=0.8, ms=3)[0])
            lu, li, lr = lines[ch]
            if not d["t"]:
                continue
            lu.set_data(d["t"], d["U"])
            li.set_data(d["t"], d["I"])
            lr.set_data(d["rt"], d["R"])
            lr.set_label(f"ch{ch}  {d['R'][-1]:.0f} mOhm" if d["R"] else f"ch{ch}")
            st = f" {d['state']}" if d["state"] else ""
            lu.set_label(f"ch{ch}{st}  {d['U'][-1]:.3f} V")
            li.set_label(f"ch{ch}  {d['I'][-1] * 1000:+.0f} mA")
        if any(d["t"] for d in data.values()):
            tmax = max(d["t"][-1] for d in data.values() if d["t"])
            tmin = min(d["t"][0] for d in data.values() if d["t"])
            if args.last:
                tmin = max(tmin, tmax - timedelta(hours=args.last))
            if tmax <= tmin:
                tmax = tmin + timedelta(seconds=10)
            axi.set_xlim(tmin, tmax)
            for ax in (axu, axi, axr):
                ax.relim(visible_only=True)
                ax.autoscale_view(scalex=False)
                ax.legend(loc="upper left", fontsize=8)
            title.set_text(f"{os.path.basename(path)}   last {tmax:%H:%M:%S}")
            fig.canvas.draw_idle()
        return []

    update(None)
    anim = FuncAnimation(fig, update, interval=args.interval * 1000, cache_frame_data=False)  # noqa: F841
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
