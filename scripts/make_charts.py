#!/usr/bin/env python3
"""Regenerate the README charts (PNG 1600x900 and SVG) and the README stats tables from data/log.csv.

    python scripts/make_charts.py            # charts into docs/charts/, tables into README.md

Nothing here is hardcoded: every number, range and time label is computed by scripts/stats.py.
Points are joined only across gaps of 30 minutes or less; there is no interpolation.
"""
from __future__ import annotations

import re
import sys
from datetime import timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stats  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "charts"
README = ROOT / "README.md"
PT = stats.PT

# ---- design tokens
BG, GRID, AXIS = "#FAFAF9", "#E7E5E4", "#57534E"
ROOM, BAND_C, HEAT, COOL = "#1C1917", "#10B981", "#F97316", "#3B82F6"
HEAT_SP, COOL_SP, OUTDOOR = "#EA580C", "#2563EB", "#A8A29E"
GAP = timedelta(minutes=30)
FIG = (8, 4.5)   # inches; at 200 dpi (2x) -> 1600 x 900 px
DPI = 200

plt.rcParams.update({
    "font.family": "sans-serif",
    # DejaVu Sans ships inside matplotlib, so every machine renders the same glyphs.
    "font.sans-serif": ["DejaVu Sans"], "font.weight": "normal",
    "font.size": 12, "axes.labelsize": 12, "xtick.labelsize": 12, "ytick.labelsize": 12,
    "axes.titlesize": 16, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "figure.facecolor": BG, "axes.facecolor": BG, "savefig.facecolor": BG,
    "axes.edgecolor": GRID, "axes.labelcolor": AXIS, "xtick.color": AXIS, "ytick.color": AXIS,
    "text.color": AXIS, "axes.grid": True, "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False, "legend.fontsize": 10,
    "svg.fonttype": "none", "svg.hashsalt": "minisplit-controller",
})


def f1(x):
    return f"{x:.1f}"


def pct(x):
    return f"{x * 100:.0f}%"


def segments(xs, ys):
    """Split a series wherever consecutive points are more than 30 min apart."""
    seg_x, seg_y = [], []
    for x, y in zip(xs, ys):
        if seg_x and x - seg_x[-1] > GAP:
            yield seg_x, seg_y
            seg_x, seg_y = [], []
        seg_x.append(x)
        seg_y.append(y)
    if seg_x:
        yield seg_x, seg_y


def plot_gapped(ax, xs, ys, **kw):
    first = True
    for sx, sy in segments(xs, ys):
        ax.plot(sx, sy, label=kw.pop("label", None) if first else None, **kw)
        first = False


def step_gapped(ax, xs, ys, **kw):
    first = True
    for sx, sy in segments(xs, ys):
        ax.step(sx, sy, where="post", label=kw.pop("label", None) if first else None, **kw)
        first = False


def mode_spans(ax, rs, end):
    """Shade heat and cool periods (each reading's mode holds until the next reading)."""
    for a, b in zip(rs, rs[1:] + [None]):
        stop = b.ts if b else end
        if a.mode in ("heat", "cool"):
            ax.axvspan(a.ts, stop, color=HEAT if a.mode == "heat" else COOL, alpha=0.10, lw=0, zorder=0)


def band(ax):
    ax.axhspan(*stats.BAND, color=BAND_C, alpha=0.12, lw=0, zorder=0.5)
    for y in stats.BAND:
        ax.axhline(y, color=BAND_C, lw=0.9, ls=(0, (4, 3)), zorder=0.6)


def time_axis(ax):
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3), tz=PT))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-I %p", tz=PT))
    ax.set_xlabel(f"Time ({stats.TZ_LABEL})")


def callout(ax, text, xy, xytext, **kw):
    ax.annotate(text, xy=xy, xytext=xytext, fontsize=10, color=ROOM, ha=kw.pop("ha", "left"),
                arrowprops=dict(arrowstyle="-", color=AXIS, lw=0.6, shrinkA=2, shrinkB=2), **kw)


def deploy_line(ax, split, label=True):
    ax.axvline(split, color=AXIS, lw=1, zorder=3)
    if label:
        ax.text(split, 0.985, f" v2 deployed {stats.clock(split)}", transform=ax.get_xaxis_transform(),
                fontsize=10, color=AXIS, va="top", ha="left")


def save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"{name}.png", dpi=DPI, metadata={"Software": None})
    fig.savefig(OUT / f"{name}.svg", metadata={"Date": None, "Creator": None})
    plt.close(fig)


# ------------------------------------------------------------------ chart 1
def chart1(S):
    rs, split = S["readings"], S["split"]
    v1n, v2 = S["v1_night"], S["v2"]
    end = rs[-1].ts
    steady = S["v2_steady"]
    lo, hi = min(r.room for r in steady), max(r.room for r in steady)
    fig, ax = plt.subplots(figsize=FIG)
    fig.subplots_adjust(left=0.08, right=0.91, top=0.74, bottom=0.13)
    mode_spans(ax, rs, end)
    band(ax)
    xs, ys = [r.ts for r in rs], [r.room for r in rs]
    plot_gapped(ax, xs, ys, color=ROOM, lw=2, label="Room temp", zorder=4)
    ax.scatter(xs, ys, s=4, color=ROOM, zorder=5)
    deploy_line(ax, split)
    ax.set_ylabel("Room temp (°F)")
    ax.set_ylim(66.5, 75.5)
    time_axis(ax)

    ax2 = ax.twinx()
    od = [(r.ts, r.outdoor) for r in rs if r.outdoor is not None]
    plot_gapped(ax2, [a for a, _ in od], [b for _, b in od], color=OUTDOOR, lw=1.5, ls=":", label="Outdoor")
    ax2.set_ylabel("Outdoor (°F)", color=OUTDOOR)
    ax2.tick_params(axis="y", colors=OUTDOOR)
    ax2.grid(False)
    ax2.spines["right"].set_visible(True)
    ax2.spines["right"].set_color(GRID)
    ax2.set_ylim(20, 80)

    peak, trough = S["overshoot"]
    callout(ax, f"Overshoot: {f1(peak.room)}°F, then down to {f1(trough.room)}°F",
            (peak.ts, peak.room), (rs[0].ts, 74.6))
    flip_ts = [b.ts for a, b in zip(rs, rs[1:]) if v1n["start"] <= b.ts < split
               and {a.mode, b.mode} == {"heat", "cool"}]
    mid = flip_ts[len(flip_ts) // 2]
    callout(ax, f"{v1n['flips']} heat/cool flips overnight,\n{f1(v1n['cool_h'])} h of cooling",
            (mid, 70.0), (mid - timedelta(hours=3.2), 67.2))
    callout(ax, f"v2 heat regime: {f1(lo)} to {f1(hi)}°F\n({stats.clock(steady[0].ts)} to {stats.clock(steady[-1].ts)})",
            (steady[len(steady) // 2].ts, hi), (split + timedelta(hours=1.5), 67.4))
    seg = S["cool_seg"]

    handles = [plt.Line2D([], [], color=ROOM, lw=2, label="Room temp"),
               Patch(color=BAND_C, alpha=0.3, label="Comfort band 69 to 71°F"),
               Patch(color=HEAT, alpha=0.25, label="Heat mode"), Patch(color=COOL, alpha=0.25, label=f"Cool mode (v2 after {stats.clock(seg[0].ts)}: solar gain)"),
               plt.Line2D([], [], color=OUTDOOR, lw=1.5, ls=":", label="Outdoor (logged by v2 only)")]
    ax.legend(handles=handles, ncol=3, loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=8.5,
              handlelength=1.4, columnspacing=1.0)
    v1 = S["v1"]
    fig.suptitle(f"v1 flipped heat and cool {v1n['flips']} times in one night.\n"
                 f"v2 stays in heat until afternoon sun forces cool.",
                 x=0.08, ha="left", fontsize=16, fontweight="bold",
                 color=ROOM, y=0.97)
    save(fig, "chart1-timeline")
    return {"steady": (lo, hi), "flip_count": len(flip_ts)}


# ------------------------------------------------------------------ chart 2
def chart2(S):
    rs, split, events = S["readings"], S["split"], S["events"]
    v2 = [r for r in rs if r.ts >= split]
    fig, (top, bot) = plt.subplots(2, 1, figsize=FIG, sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    fig.subplots_adjust(left=0.09, right=0.97, top=0.78, bottom=0.14, hspace=0.12)
    end = v2[-1].ts
    for ax in (top, bot):
        mode_spans(ax, v2, end)
    band(top)
    plot_gapped(top, [r.ts for r in v2], [r.room for r in v2], color=ROOM, lw=2, label="Room temp", zorder=4)
    tp = [(r.ts, r.tp) for r in v2 if r.tp is not None]
    plot_gapped(top, [a for a, _ in tp], [b for _, b in tp], color=ROOM, lw=0.9, ls=(0, (4, 2)),
                label="Predicted temp Tp (15 min ahead)", zorder=3)
    top.set_ylabel("°F")
    top.legend(loc="upper left", ncol=2, fontsize=9)
    top.set_ylim(69, 75.5)

    heat = [(r.ts, r.heat_sp) for r in v2 if r.mode == "heat" and r.heat_sp is not None]
    cool = [(r.ts, r.cool_sp) for r in v2 if r.mode == "cool" and r.cool_sp is not None]
    step_gapped(bot, [a for a, _ in heat], [b for _, b in heat], color=HEAT_SP, lw=2, label="Heat setpoint")
    step_gapped(bot, [a for a, _ in cool], [b for _, b in cool], color=COOL_SP, lw=2, label="Cool setpoint")
    trims = []
    for e in events:
        m = re.search(r"trim change: ([+-]\d) -> ([+-]\d)", e["action"])
        if m:
            ts = stats.parse_ts(e["timestamp_pt"])
            d = int(m.group(2)) - int(m.group(1))
            trims.append((ts, d))
            bot.axvline(ts, ymin=0, ymax=0.12, color=AXIS, lw=1.2)
            bot.text(ts, 64.9, f" trim {d:+d}", fontsize=9, color=AXIS, va="bottom")
    bot.set_ylim(64.5, 78.5)
    bot.set_ylabel("Setpoint °F")
    bot.legend(loc="upper left", ncol=2, fontsize=9)
    time_axis(bot)
    bot.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 2), tz=PT))

    # Callout: the first setpoint cut made while the room was still in band but predicted to rise.
    drop = None
    for a, b in zip(v2, v2[1:]):
        if a.mode == b.mode == "heat" and a.heat_sp and b.heat_sp and b.heat_sp < a.heat_sp \
                and b.room <= stats.BAND[1] and b.tp is not None and b.tp > b.room:
            drop = b
            break
    if drop:
        callout(bot, "Setpoint drops before the room overshoots.", (drop.ts, drop.heat_sp),
                (drop.ts + timedelta(hours=1.0), 69.5))
    lo_tp = min(b for _, b in tp)
    fig.suptitle("v2 steers the heat setpoint from predicted temp,\nnot current temp",
                 x=0.09, ha="left", fontsize=16, fontweight="bold", color=ROOM, y=0.97)
    save(fig, "chart2-setpoint-steering")
    return {"drop": drop, "trims": trims, "tp_min": lo_tp}


# ------------------------------------------------------------------ chart 3
def chart3(S):
    v1, v2 = S["v1"], S["v2"]
    fig, (ax, axc) = plt.subplots(2, 1, figsize=FIG, gridspec_kw={"height_ratios": [2.2, 1.3]})
    fig.subplots_adjust(left=0.20, right=0.96, top=0.78, bottom=0.06, hspace=0.75)
    labels = [f"v1 ({f1(v1['hours'])} h)", f"v2 ({f1(v2['hours'])} h)"]
    rows = [v1, v2]
    cols = [("below", "Below 69°F", "#93C5FD"), ("in_band", "69 to 71°F", BAND_C), ("above", "Above 71°F", "#FDBA74")]
    for i, d in enumerate(rows):
        left = 0
        for key, lab, c in cols:
            w = d[key] * 100
            ax.barh(i, w, left=left, color=c, height=0.6, label=lab if i == 0 else None)
            if key == "in_band":
                ax.text(left + w / 2, i, pct(d[key]), ha="center", va="center", fontsize=20,
                        fontweight="bold", color="white")
            elif w >= 6:
                ax.text(left + w / 2, i, pct(d[key]), ha="center", va="center", fontsize=10, color=ROOM)
            left += w
    ax.set_yticks([0, 1], labels)
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.grid(False)
    ax.set_xticks([])
    ax.legend(ncol=3, loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=10)
    ax.text(0, 1.32, "Share of time by room temp", transform=ax.transAxes, fontsize=12, color=AXIS)
    for i, d in enumerate(rows):
        h = d["cool_share"] * 100
        axc.barh(i, h, color=COOL, alpha=0.85, height=0.6, label="Cool mode" if i == 0 else None)
        axc.barh(i, 100 - h, left=h, color=HEAT, alpha=0.6, height=0.6, label="Heat mode" if i == 0 else None)
        axc.text(h / 2 if h >= 8 else h + 1, i, f"cool {pct(d['cool_share'])}", va="center",
                 ha="center" if h >= 8 else "left", fontsize=10, color="white" if h >= 8 else ROOM)
    axc.set_yticks([0, 1], labels)
    axc.invert_yaxis()
    axc.set_xlim(0, 100)
    axc.grid(False)
    axc.set_xticks([])
    axc.legend(ncol=2, loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=10)
    axc.text(0, 1.55, "Share of time by mode", transform=axc.transAxes, fontsize=12, color=AXIS)
    seg = S["cool_seg"]
    fig.suptitle(f"v1 spent {pct(v1['cool_share'])} of its time cooling; v2 {pct(v2['cool_share'])}, "
                 f"all after {stats.clock(seg[0].ts)}", x=0.03, ha="left", fontsize=16, fontweight="bold",
                 color=ROOM, y=0.97)
    save(fig, "chart3-time-in-band")


# ------------------------------------------------------------------ README tables
def tables(S, c1):
    v1, n, v2 = S["v1"], S["v1_night"], S["v2"]
    peak, trough = S["overshoot"]
    fo = S["first_outdoor"]
    problem = "\n".join([
        f"| v1 night ({stats.clock(n['start'])} to {stats.clock(n['end'])}, {f1(n['hours'])} h) | From log.csv |",
        "|---|---|",
        f"| Heat/cool mode flips in one night | {n['flips']} |",
        f"| Time in 69 to 71°F | {pct(n['in_band'])} |",
        f"| Cooling hours that night | {f1(n['cool_h'])} h |",
    ])
    problem_note = (f"Peak {f1(peak.room)}°F at {stats.clock(peak.ts)}, then down to {f1(trough.room)}°F at "
                    f"{stats.clock(trough.ts)} before heat came back on. The night low was {f1(n['min'])}°F. "
                    f"Outdoor temperature was not logged during v1; the first logged reading, at "
                    f"{stats.clock(fo.ts)} the next morning, was {f1(fo.outdoor)}°F.")
    lo, hi = c1["steady"]
    results = "\n".join([
        "| Metric | v1 | v2 |",
        "|---|---|---|",
        f"| Period | {v1['start']:%b %-d} {stats.clock(v1['start'])} to {stats.clock(v1['end'])} "
        f"({f1(v1['hours'])} h) | {stats.clock(v2['start'])} to {stats.clock(S['readings'][-1].ts)} "
        f"({f1(v2['hours'])} h) |",
        f"| Time in 69 to 71°F | {pct(v1['in_band'])} | {pct(v2['in_band'])} |",
        f"| Heat/cool mode flips | {v1['flips']} | {v2['flips']} |",
        f"| Cooling hours | {f1(v1['cool_h'])} h | {f1(v2['cool_h'])} h |",
        f"| Min room temp | {f1(v1['min'])}°F | {f1(v2['min'])}°F |",
        f"| Max room temp | {f1(v1['max'])}°F | {f1(v2['max'])}°F |",
    ])
    seg = S["cool_seg"]
    results_note = (f"v2 held {f1(lo)} to {f1(hi)}°F in heat from {stats.clock(S['v2_steady'][0].ts)} to "
                    f"{stats.clock(S['v2_steady'][-1].ts)}. Its full range was {f1(v2['min'])} to "
                    f"{f1(v2['max'])}°F: from midday, solar gain pushed the room above the band "
                    f"with the heat setpoint already at its floor, and the 73°F rule switched to cool at "
                    f"{stats.clock(seg[0].ts)} ({f1(seg[0].room)}°F); the last reading was "
                    f"{f1(seg[-1].room)}°F at {stats.clock(seg[-1].ts)}. v2 has no overnight data yet, so "
                    f"the two columns cover different times of day.")
    a = S["afternoon"]
    a1, a2 = a["v1"], a["v2"]
    w1, w2 = a["window1"], a["window2"]
    verdict = ("v2 has not yet beaten v1 on time in band." if v2["in_band"] <= v1["in_band"]
               else "v2 spent more of its time in band than v1.")
    afternoon_txt = "\n".join([
        f"**{verdict}** The two runs cover different hours, so the one time of day both cover, the afternoon, "
        f"is the fairest comparison, and it favours {'v1' if a1['in_band'] >= a2['in_band'] else 'v2'}:",
        "",
        "| Afternoon | Window | In 69 to 71°F | Above 71°F |",
        "|---|---|---|---|",
        f"| v1 | {w1[0]:%b %-d} {stats.clock(w1[0])} to {stats.clock(w1[1])} | {pct(a1['in_band'])} | {pct(a1['above'])} |",
        f"| v2 | {w2[0]:%b %-d} {stats.clock(w2[0])} to {stats.clock(w2[1])} | {pct(a2['in_band'])} | {pct(a2['above'])} |",
        "",
        f"Both rows use the same clock window: {stats.clock(w1[0])}, when the log starts, to "
        f"{stats.clock(w2[1])}, the last v2 reading.",
    ])
    return {"problem": problem + "\n\n" + problem_note,
            "results": results + "\n\n" + results_note + "\n\n" + afternoon_txt}


def update_readme(blocks):
    if not README.exists():
        return
    text = README.read_text()
    for name, body in blocks.items():
        text = re.sub(rf"(<!-- stats:{name} -->).*?(<!-- /stats:{name} -->)",
                      lambda m: m.group(1) + "\n" + body + "\n" + m.group(2), text, flags=re.S)
    README.write_text(text)


def main():
    S = stats.compute()
    c1 = chart1(S)
    c2 = chart2(S)
    chart3(S)
    blocks = tables(S, c1)
    update_readme(blocks)
    for b in blocks.values():
        print(b, "\n")
    print("v2 deployed:", S["split"], "| first setpoint drop in band:", c2["drop"].ts if c2["drop"] else None,
          "| trims:", c2["trims"])


if __name__ == "__main__":
    main()
