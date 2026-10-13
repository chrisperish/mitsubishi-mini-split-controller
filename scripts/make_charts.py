#!/usr/bin/env python3
"""Draw the README charts.

    python scripts/make_charts.py                     # illustrative charts -> docs/charts/illustrative-*.{png,svg}
    python scripts/make_charts.py --from-log var/log.csv [--out charts]   # chart your own controller log

The README charts come from a deterministic simulation (scripts/simulate.py, seed 42). They show how
the two approaches behave; they are not measured data. --from-log charts a real log.csv written by
the controller instead; nothing from your log is committed.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import simulate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "charts"
ILLUSTRATIVE = "Illustrative simulation, not measured data."

# ---- design tokens (unchanged palette)
BG, GRID, AXIS = "#FAFAF9", "#E7E5E4", "#57534E"
ROOM, BAND_C, HEAT, COOL = "#1C1917", "#10B981", "#F97316", "#3B82F6"
HEAT_SP, COOL_SP, OUTDOOR = "#EA580C", "#2563EB", "#A8A29E"
FIG, DPI = (8, 4.5), 200      # 1600 x 900 px
GAP = timedelta(minutes=30)

plt.rcParams.update({
    # DejaVu Sans ships inside matplotlib, so every machine renders the same glyphs.
    "font.family": "sans-serif", "font.sans-serif": ["DejaVu Sans"], "font.weight": "normal",
    "font.size": 12, "axes.labelsize": 12, "xtick.labelsize": 11, "ytick.labelsize": 11,
    "figure.facecolor": BG, "axes.facecolor": BG, "savefig.facecolor": BG,
    "axes.edgecolor": GRID, "axes.labelcolor": AXIS, "xtick.color": AXIS, "ytick.color": AXIS,
    "text.color": AXIS, "axes.grid": True, "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False, "legend.fontsize": 9,
    "svg.fonttype": "none", "svg.hashsalt": "minisplit-controller",
})


# ------------------------------------------------------------------ helpers
def title(fig, text, sub=ILLUSTRATIVE, x=0.08):
    fig.text(x, 0.965, text, fontsize=16, fontweight="bold", color=ROOM, ha="left", va="top")
    if sub:
        fig.text(x, 0.965 - 0.07 * (text.count("\n") + 1), sub, fontsize=11, color=AXIS, ha="left", va="top")
        fig.text(0.99, 0.012, sub, fontsize=7, color=OUTDOOR, ha="right", va="bottom")


def band(ax):
    ax.axhspan(*simulate.BAND, color=BAND_C, alpha=0.12, lw=0, zorder=0.5)
    for y in simulate.BAND:
        ax.axhline(y, color=BAND_C, lw=0.9, ls=(0, (4, 3)), zorder=0.6)


def spans(t, mode):
    """[(start, end, mode)] runs of a constant mode."""
    out, s = [], 0
    for i in range(1, len(t) + 1):
        if i == len(t) or mode[i] != mode[s]:
            out.append((t[s], t[min(i, len(t) - 1)], mode[s]))
            s = i
    return out


def shade(ax, t, mode, label_y=None):
    for a, b, m in spans(t, mode):
        if m not in ("heat", "cool"):
            continue
        ax.axvspan(a, b, color=HEAT if m == "heat" else COOL, alpha=0.10, lw=0, zorder=0)
        if label_y is not None and (b - a) >= timedelta(minutes=50):
            ax.text(a + (b - a) / 2, label_y, "Heating" if m == "heat" else "Cooling", fontsize=8.5,
                    ha="center", va="bottom", clip_on=False, color=HEAT_SP if m == "heat" else COOL_SP)


def time_axis(ax, every=3):
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, every)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-I %p"))


def callout(ax, text, xy, xytext, ha="left"):
    ax.annotate(text, xy=xy, xytext=xytext, fontsize=9.5, color=ROOM, ha=ha, va="center",
                arrowprops=dict(arrowstyle="-", color=AXIS, lw=0.6, shrinkA=2, shrinkB=2))


def plot_gapped(ax, xs, ys, **kw):
    """Join points only across gaps of 30 min or less (used for real logs)."""
    seg_x, seg_y, first = [], [], True
    for x, y in list(zip(xs, ys)) + [(None, None)]:
        if x is None or (seg_x and x - seg_x[-1] > GAP):
            ax.plot(seg_x, seg_y, label=kw.pop("label", None) if first else None, **kw)
            seg_x, seg_y, first = [], [], False
        if x is not None:
            seg_x.append(x)
            seg_y.append(y)


def save(fig, name, out):
    out.mkdir(parents=True, exist_ok=True)
    fig.savefig(out / f"{name}.png", dpi=DPI, metadata={"Software": None})
    fig.savefig(out / f"{name}.svg", metadata={"Date": None, "Creator": None})
    plt.close(fig)


# ------------------------------------------------------------------ illustrative charts
def find_points(auto, ctl):
    """Locate callout anchors in the simulated series (no hardcoded times)."""
    flips = [i for i in range(1, len(auto.t)) if auto.mode[i] != auto.mode[i - 1]]
    first_cool = next((i for i in flips if auto.mode[i] == "cool"), None)
    night = [i for i in range(len(auto.t)) if auto.t[i].hour in (1, 2, 3, 4)]
    low = night[len(night) // 2]                     # middle of the night, Auto parked at its heat setpoint
    drops = [i for i in range(1, len(ctl.t)) if ctl.heat_sp[i] < ctl.heat_sp[i - 1]
             and ctl.tp[i] is not None and ctl.tp[i] > ctl.room[i] and ctl.room[i] <= simulate.BAND[1]]
    drop = max(drops, key=lambda i: ctl.tp[i] - ctl.room[i])
    ctl_cool = next((k for k in range(1, len(ctl.t)) if ctl.mode[k] == "cool" and ctl.mode[k - 1] != "cool"), None)
    return {"ctl_cool": ctl_cool, "first_cool": first_cool, "low": low, "drop": drop, "drops": drops, "flips": flips}


def chart1(auto, ctl, out):
    P = find_points(auto, ctl)
    fig, (top, bot) = plt.subplots(2, 1, figsize=FIG, sharex=True, sharey=True)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.68, bottom=0.11, hspace=0.36)
    for ax, r, name in ((top, auto, f"Stock Auto {simulate.AUTO_HEAT}/{simulate.AUTO_COOL}"), (bot, ctl, "Controller")):
        shade(ax, r.t, r.mode)   # Heating/Cooling are named once, in the legend
        band(ax)
        ax.plot(r.t, r.room, color=ROOM, lw=2, zorder=4)
        ax.set_ylim(64, 75)
        ax.set_yticks([65, 67, 69, 71, 73, 75])
        ax.set_title(name, loc="left", fontsize=10.5, fontweight="bold", color=ROOM, pad=3)
    top.step(auto.t, auto.heat_sp, where="post", color=HEAT_SP, lw=1)
    top.step(auto.t, auto.cool_sp, where="post", color=COOL_SP, lw=1)
    bot.step(ctl.t, ctl.heat_sp, where="post", color=HEAT_SP, lw=1.6)
    # Chart 1 carries exactly 3 callouts: one on Stock Auto, two on the controller.
    sa, sc = simulate.summarize(auto), simulate.summarize(ctl)
    i = P["first_cool"]
    callout(top, f"Sun: flips to cooling at {auto.t[i]:%-I:%M %p}, cools {sa['cool_h']:.1f} h",
            (auto.t[i], auto.room[i]), (auto.t[i] - timedelta(hours=1.5), 66.0), ha="right")
    j = P["ctl_cool"]
    callout(bot, f"Stays in heat until 73F, cools {sc['cool_h']:.1f} h",
            (ctl.t[j], ctl.room[j]), (ctl.t[j] - timedelta(hours=1.0), 72.8), ha="right")
    i = P["drop"]
    callout(bot, "Setpoint steps down before the room overshoots", (ctl.t[i], ctl.heat_sp[i]),
            (ctl.t[i] + timedelta(hours=0.8), 65.0) if ctl.t[i] - ctl.t[0] < timedelta(hours=12)
            else (ctl.t[i] - timedelta(hours=0.8), 65.0), ha="left" if ctl.t[i] - ctl.t[0] < timedelta(hours=12) else "right")
    time_axis(bot)
    bot.set_xlabel("Time")
    fig.text(0.015, 0.43, "Room temp (°F)", rotation=90, va="center", fontsize=12, color=AXIS)
    handles = [Line2D([], [], color=ROOM, lw=2, label="Room temp"),
               Line2D([], [], color=HEAT_SP, lw=1.4, label="Setpoint"),
               Patch(color=HEAT, alpha=0.25, label="Heating"), Patch(color=COOL, alpha=0.25, label="Cooling")]
    top.legend(handles=handles, ncol=4, loc="lower right", bbox_to_anchor=(1.0, 1.13))
    title(fig, "In simulation, both hold the band about equally;\nthe controller cools less.")
    save(fig, "illustrative-chart1-timeline", out)
    return P


def chart2(ctl, P, out):
    i = P["drop"]
    t0, t1 = ctl.t[i] - timedelta(hours=1.25), ctl.t[i] + timedelta(hours=1.25)
    idx = [k for k in range(len(ctl.t)) if t0 <= ctl.t[k] <= t1]
    t = [ctl.t[k] for k in idx]
    fig, ax = plt.subplots(figsize=FIG)
    fig.subplots_adjust(left=0.09, right=0.94, top=0.76, bottom=0.13)
    band(ax)
    ax.set_xlim(t0, t1)
    ax.plot(t, [ctl.room[k] for k in idx], color=ROOM, lw=2, label="Room temp", zorder=4)
    ax.plot(t, [ctl.tp[k] for k in idx], color=ROOM, lw=0.9, ls=(0, (4, 2)),
            label="Predicted temp, 15 min ahead", zorder=3)
    ax.step(t, [ctl.heat_sp[k] for k in idx], where="post", color=HEAT_SP, lw=2, label="Heat setpoint")
    ax.set_ylim(64, 73)
    ax.set_ylabel("°F")
    callout(ax, "Predicted to rise, so setpoint drops 1F", (ctl.t[i], ctl.heat_sp[i]),
            (ctl.t[i] + timedelta(minutes=15), 65.0))
    ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0, 30]))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-I:%M %p"))
    ax.set_xlabel("Time")
    ax.legend(loc="upper left", ncol=3)
    title(fig, "How the controller steers (simulated).")
    save(fig, "illustrative-chart2-steering", out)
    return (t0, t1)


def bars(ax, rows):
    cols = [("below", "Below 69°F", "#93C5FD"), ("in_band", "69 to 71°F", BAND_C), ("above", "Above 71°F", "#FDBA74")]
    for i, (_, d) in enumerate(rows):
        left = 0
        for key, lab, c in cols:
            w = d[key] * 100
            ax.barh(i, w, left=left, color=c, height=0.55, label=lab if i == 0 else None)
            if key == "in_band" and w >= 8:
                ax.text(left + w / 2, i, f"{w:.0f}%", ha="center", va="center", fontsize=20,
                        fontweight="bold", color="white")
            elif w >= 7:
                ax.text(left + w / 2, i, f"{w:.0f}%", ha="center", va="center", fontsize=10, color=ROOM)
            left += w
    ax.set_yticks(range(len(rows)), [n for n, _ in rows])
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.set_xticks([])
    ax.grid(False)
    ax.legend(ncol=3, loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=10)


def chart3(rows, out):
    fig, ax = plt.subplots(figsize=FIG)
    fig.subplots_adjust(left=0.27, right=0.96, top=0.70, bottom=0.12)
    bars(ax, rows)
    title(fig, "Time in the comfort band (illustrative)", x=0.03)
    save(fig, "illustrative-chart3-time-in-band", out)


def illustrative(out=OUT):
    auto, ctl = simulate.stock_auto(), simulate.controller()
    ref = simulate.stock_auto(heat=simulate.RESTORE_HEAT, cool=simulate.RESTORE_COOL)
    sa, sc, sr = simulate.summarize(auto), simulate.summarize(ctl), simulate.summarize(ref)
    P = chart1(auto, ctl, out)
    win = chart2(ctl, P, out)
    chart3([(f"Stock Auto {simulate.AUTO_HEAT}/{simulate.AUTO_COOL}\ncools {sa['cool_h']:.1f} h", sa),
            (f"Controller\ncools {sc['cool_h']:.1f} h", sc),
            (f"Stock Auto {simulate.RESTORE_HEAT}/{simulate.RESTORE_COOL}\n(not aimed at band)", sr)], out)
    return {"auto": sa, "controller": sc, "auto_6773": sr, "points": P, "zoom": win, "auto_run": auto, "ctl_run": ctl}


# ------------------------------------------------------------------ your own log
def load_log(path):
    """Readings from a controller log.csv: (time, room, mode, heat_sp, cool_sp, Tp)."""
    rows = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("event") == "Error" or not r.get("room_f"):
                continue
            m = re.match(r"\s*(\d{4}-\d{2}-\d{2} \d{1,2}:\d{2} [AP]M)", r["timestamp_pt"])
            if not m:
                continue
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %I:%M %p")
            tp = re.search(r"\bTp (\d+\.\d+)", r.get("action", ""))
            num = lambda x: float(x) if x not in ("", None) else None  # noqa: E731
            rows[ts] = (ts, float(r["room_f"]), r.get("mode", ""), num(r.get("heat_sp")), num(r.get("cool_sp")),
                        float(tp.group(1)) if tp else None)
    return [rows[k] for k in sorted(rows)]


def log_shares(rs):
    """Time-weighted shares: each reading holds until the next one (gaps over 30 min are skipped)."""
    tot = below = inb = 0.0
    for a, b in zip(rs, rs[1:]):
        d = (b[0] - a[0]).total_seconds()
        if d <= 0 or d > GAP.total_seconds():
            continue
        tot += d
        below += d if a[1] < simulate.BAND[0] else 0
        inb += d if simulate.BAND[0] <= a[1] <= simulate.BAND[1] else 0
    tot = tot or 1.0
    return {"below": below / tot, "in_band": inb / tot, "above": 1 - (below + inb) / tot}


def from_log(path, out):
    rs = load_log(path)
    if len(rs) < 2:
        sys.exit(f"{path}: need at least two readings")
    t = [r[0] for r in rs]
    fig, ax = plt.subplots(figsize=FIG)
    fig.subplots_adjust(left=0.09, right=0.97, top=0.80, bottom=0.13)
    shade(ax, t, [r[2] for r in rs], label_y=None)
    band(ax)
    plot_gapped(ax, t, [r[1] for r in rs], color=ROOM, lw=2, label="Room temp")
    hs = [(r[0], r[3]) for r in rs if r[2] == "heat" and r[3] is not None]
    if hs:
        ax.step([a for a, _ in hs], [b for _, b in hs], where="post", color=HEAT_SP, lw=1.2, label="Heat setpoint")
    ax.set_ylabel("°F")
    ax.set_xlabel("Time")
    time_axis(ax, every=3 if (t[-1] - t[0]) <= timedelta(hours=36) else 12)
    ax.legend(loc="upper left", ncol=2)
    title(fig, "Room temperature from your log", sub=None, x=0.09)
    save(fig, "log-timeline", out)
    fig, ax = plt.subplots(figsize=FIG)
    fig.subplots_adjust(left=0.20, right=0.96, top=0.74, bottom=0.12)
    bars(ax, [("Your log", log_shares(rs))])
    title(fig, "Time in the comfort band (your log)", sub=None, x=0.03)
    save(fig, "log-time-in-band", out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from-log", metavar="PATH", help="chart a controller log.csv instead of the simulation")
    ap.add_argument("--out", help="output directory (default: docs/charts, or ./charts for --from-log)")
    a = ap.parse_args(argv)
    if a.from_log:
        out = Path(a.out or "charts")
        from_log(a.from_log, out)
        print(f"wrote {out}/log-timeline.* and {out}/log-time-in-band.*")
        return
    res = illustrative(Path(a.out) if a.out else OUT)
    for k in ("auto", "controller", "auto_6773"):
        d = res[k]
        print(f"{k}: below {d['below']:.0%} in band {d['in_band']:.0%} above {d['above']:.0%} "
              f"flips {d['flips']} cooling {d['cool_h']:.1f} h (outdoor < 50F: {d['cool_cold_h']:.1f} h) "
              f"range {d['min']:.1f} to {d['max']:.1f}F")


if __name__ == "__main__":
    main()
