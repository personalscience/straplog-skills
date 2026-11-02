#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pandas", "straplog[plot]>=0.3.1"]
# ///
"""
DFA alpha1 analysis of a graded ramp test recorded with StrapLog.

Reads a single-file StrapLog snapshot (via the `straplog` package), pulls raw
RR intervals for one recording, computes DFA α1 (`straplog.hrv`) in sliding
2-min windows, locates AeT / VT1 / MID / VT2 crossings, and writes
plots + a 5-zone HR table to an output directory.

Usage
-----
  analyze_ramp.py --list                      # JSON list of recordings
  analyze_ramp.py                             # newest export, most recent recording
  analyze_ramp.py --db 261018-0730-RAMP.sqlite  # bare name or a path
  analyze_ramp.py --recording 3 --out /tmp/x  # analyze specific recording
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import straplog.db as straplog_db
from straplog import catalog, hrv
from straplog import plot as chart_style


# ---------- constants ----------------------------------------------------

DEFAULT_OUT = Path.cwd() / "hrv_ramp_analysis"

MIN_BEATS_RECORDING = 300

AET_ALPHA = 1.00     # Z1/Z2 (aerobic threshold)
VT1_ALPHA = 0.75     # Z2/Z3 (1st ventilatory threshold)
MID_ALPHA = 0.60     # Z3/Z4
VT2_ALPHA = 0.50     # Z4/Z5 (2nd ventilatory threshold)


# ---------- load ----------------------------------------------------------

def list_recordings(db_path: Path) -> list[dict]:
    # date_utc keeps this skill's convention: local wall-clock labelled UTC
    return [{"pk": r["pk"], "name": r["name"],
             "date_utc": pd.Timestamp(r["start_local"]).tz_localize("UTC").isoformat(),
             "n_beats": r["n_beats"], "duration_min": r["duration_min"]}
            for r in straplog_db.list_recordings(db_path)]


def pick_recording(recs: list[dict]) -> int:
    """Auto-pick: most recent recording by date."""
    if not recs:
        raise SystemExit("No recordings found in the export.")
    return sorted(recs, key=lambda r: r["date_utc"])[-1]["pk"]


def load_recording(db_path: Path, pk: int):
    rr, ev, start_local, _ = straplog_db.load_recording(db_path, pk)
    _require_beats(rr, pk)
    return rr, ev, start_local.tz_localize("UTC")


def _require_beats(rr: pd.DataFrame, pk: int) -> None:
    if len(rr) < MIN_BEATS_RECORDING:
        raise SystemExit(
            f"Recording {pk} has only {len(rr)} RR intervals "
            f"(< {MIN_BEATS_RECORDING}); too short for DFA α1 analysis."
        )


# ---------- threshold detection ------------------------------------------

def crossing_hr(feat: pd.DataFrame, alpha_target: float):
    """
    First downward crossing of alpha1 through `alpha_target`, linearly
    interpolated on HR. If never crossed, extrapolate from the ascending
    ramp (HR >= 150) and mark the result 'extrapolated'.
    """
    a = feat["alpha1"].to_numpy()
    h = feat["hr_bpm"].to_numpy()
    for i in range(1, len(a)):
        if np.isnan(a[i - 1]) or np.isnan(a[i]):
            continue
        if a[i - 1] >= alpha_target and a[i] < alpha_target:
            frac = (a[i - 1] - alpha_target) / (a[i - 1] - a[i])
            return float(h[i - 1] + frac * (h[i] - h[i - 1])), "interpolated"

    if len(h) == 0:
        return None, "not reached"
    peak_idx = int(np.nanargmax(h))
    idx = np.arange(len(h))
    mask = (idx <= peak_idx) & (h >= 150) & np.isfinite(a)
    if mask.sum() >= 3:
        slope, intercept = np.polyfit(h[mask], a[mask], 1)
        if slope < 0:
            return float((alpha_target - intercept) / slope), "extrapolated"
    return None, "not reached"


# ---------- plots ---------------------------------------------------------

def plot_crossplot(feat: pd.DataFrame, vt1_hr, vt2_hr, path: Path):
    from plotnine import (
        aes, annotate, geom_hline, geom_line, geom_point, geom_vline, ggplot,
        ggtitle, labs, scale_color_cmap,
    )

    y_top = float(feat["alpha1"].max())

    p = (ggplot(feat, aes("hr_bpm", "alpha1"))
         + geom_line(color=chart_style.INK["baseline"], size=0.9)
         + geom_point(aes(color="t_min"), size=3)
         + scale_color_cmap(cmap_name="viridis",
                            name="time into\nrecording (min)")
         + geom_hline(yintercept=VT1_ALPHA, color=chart_style.SERIES["orange"],
                      linetype="dashed", size=1.2)
         + geom_hline(yintercept=VT2_ALPHA, color=chart_style.SERIES["red"],
                      linetype="dashed", size=1.2)
         + labs(x="Heart rate (bpm)", y="DFA α1")
         + ggtitle("Ramp test: HR vs DFA α1")
         + chart_style.theme_house())

    if vt1_hr is not None:
        p = (p + geom_vline(xintercept=vt1_hr, color=chart_style.SERIES["orange"],
                            linetype="dotted", size=1.0)
               + annotate("text", x=vt1_hr, y=y_top * 0.95,
                          label=f" VT1 ≈ {vt1_hr:.0f} bpm",
                          color=chart_style.SERIES["orange"], ha="left",
                          va="top", size=9))
    if vt2_hr is not None:
        p = (p + geom_vline(xintercept=vt2_hr, color=chart_style.SERIES["red"],
                            linetype="dotted", size=1.0)
               + annotate("text", x=vt2_hr, y=y_top * 0.85,
                          label=f" VT2 ≈ {vt2_hr:.0f} bpm",
                          color=chart_style.SERIES["red"], ha="left",
                          va="top", size=9))

    p.save(path, width=8, height=6, dpi=chart_style.DPI, verbose=False)


def plot_timeseries(feat: pd.DataFrame, ev: pd.DataFrame, path: Path):
    from plotnine import (
        aes, geom_hline, geom_line, geom_point, ggplot, ggtitle, labs,
        scale_x_continuous,
    )

    hr_color = chart_style.SERIES["red"]
    a1_color = chart_style.SERIES["blue"]
    y_top_hr = float(feat["hr_bpm"].max())

    ev = ev.assign(t_min=ev["t_rel"] / 60.0)   # ev["t_rel"] is raw seconds
    # One shared x-range across both stacked panels (union incl. events), so a
    # vertical event marker lands at the same horizontal position in each panel.
    x_lo = min(float(feat["t_min"].min()),
               float(ev["t_min"].min()) if len(ev) else np.inf)
    x_hi = max(float(feat["t_min"].max()),
               float(ev["t_min"].max()) if len(ev) else -np.inf)
    shared_x = scale_x_continuous(limits=(x_lo, x_hi))

    p_hr = (ggplot(feat, aes("t_min", "hr_bpm"))
            + geom_line(color=hr_color, size=1.6)
            + geom_point(color=hr_color, size=2)
            + chart_style.event_marker_layers(ev, x_col="t_min",
                                              label_col="note", y_top=y_top_hr)
            + shared_x
            + labs(x="", y="HR (bpm)")
            + ggtitle("Ramp test time series (HR + DFA α1)")
            + chart_style.theme_house())

    p_a1 = (ggplot(feat, aes("t_min", "alpha1"))
            + geom_line(color=a1_color, size=1.6)
            + geom_point(color=a1_color, size=2, shape="s")
            + geom_hline(yintercept=VT1_ALPHA, color=chart_style.SERIES["orange"],
                         linetype="dashed", size=1.0)
            + geom_hline(yintercept=VT2_ALPHA, color=chart_style.SERIES["violet"],
                         linetype="dashed", size=1.0)
            + shared_x
            + labs(x="Time into recording (min)", y="DFA α1")
            + chart_style.theme_house())

    combined = p_hr / p_a1   # plotnine >= 0.14 native vertical stack
    combined.save(path, width=10, height=5.6, dpi=chart_style.DPI, verbose=False)


# ---------- zone table ----------------------------------------------------

def zone_table(aet, vt1, mid, vt2) -> pd.DataFrame:
    def bpm(hr):
        return "?" if hr is None else round(hr)   # threshold not reached

    rows = [
        ("Z1 – Recovery",      f"< {bpm(aet)}",
         "α1 > 1.0 · sub-AeT · too easy for an aerobic stimulus"),
        ("Z2 – Aerobic base",  f"{bpm(aet)}–{bpm(vt1)}",
         "α1 ≈ 1.0→0.75 · true Seiler Z2 · fat-ox, mitochondrial"),
        ("Z3 – Tempo",         f"{bpm(vt1)}–{bpm(mid)}",
         "α1 ≈ 0.75→0.60 · moderate / 'grey zone'"),
        ("Z4 – Threshold",     f"{bpm(mid)}–{bpm(vt2)}",
         "α1 ≈ 0.60→0.50 · LT2 / VT2 approach"),
        ("Z5 – VO2max",        f"> {bpm(vt2)}",
         "α1 < 0.50 · above VT2, maximal aerobic / anaerobic"),
    ]
    return pd.DataFrame(rows, columns=["zone", "hr_bpm", "description"])


def write_zone_outputs(out_dir: Path, start_wall, aet, vt1, mid, vt2,
                       hrmax, feat, methods):
    zt = zone_table(aet, vt1, mid, vt2)
    zt.to_csv(out_dir / "zone_table.csv", index=False)

    def fmt(hr, method):
        if hr is None:
            return "not reached"
        tag = "" if method == "interpolated" else " _(extrapolated; outside observed HR)_"
        return f"{hr:.0f} bpm{tag}"

    md = [
        "# Personalized HR Zones — Graded Ramp Test",
        "",
        f"- **Date of test**: {start_wall:%Y-%m-%d %H:%M} UTC",
        f"- **HRmax observed (window-avg)**: {hrmax:.0f} bpm",
        f"- **AeT (α1 = 1.00)**: {fmt(aet, methods['aet'])}",
        f"- **VT1 (α1 = 0.75)**: {fmt(vt1, methods['vt1'])}",
        f"- **MID (α1 = 0.60)**: {fmt(mid, methods['mid'])}",
        f"- **VT2 (α1 = 0.50)**: {fmt(vt2, methods['vt2'])}",
        "",
        "## Zones",
        "",
        "| Zone | HR (bpm) | Notes |",
        "|---|---|---|",
    ]
    for _, r in zt.iterrows():
        md.append(f"| {r['zone']} | {r['hr_bpm']} | {r['description']} |")
    md += [
        "",
        "## Computation notes",
        "",
        f"- Sliding 2-min windows, 1-min stride ({len(feat)} points).",
        "- Artifact correction: physiological gate (300–2000 ms) plus "
        "iterative 5-beat median filter at 25% tolerance (applied per window).",
        "- DFA α1 computed over scales 4–16 beats, linear detrend per box.",
        "- VT1/VT2 HR found by linear interpolation of the first window "
        "where α1 crosses the threshold downward.",
    ]
    (out_dir / "zone_table.md").write_text("\n".join(md))


# ---------- CLI ----------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", type=catalog.resolve, default=None,
                   help="StrapLog export: a path, or a bare name in the iCloud "
                        "folder (default: the newest downloaded export there)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                   help=f"Output directory (default: {DEFAULT_OUT})")
    p.add_argument("--recording", type=int, default=None,
                   help="StrapLog recording.id to analyze (default: most recent)")
    p.add_argument("--list", action="store_true",
                   help="Print JSON list of recordings and exit")
    p.add_argument("--window", type=float, default=hrv.WINDOW_SEC_DEFAULT,
                   help=f"Window length in seconds (default: {hrv.WINDOW_SEC_DEFAULT})")
    p.add_argument("--stride", type=float, default=hrv.STRIDE_SEC_DEFAULT,
                   help=f"Window stride in seconds (default: {hrv.STRIDE_SEC_DEFAULT})")
    p.add_argument("--pct-thresh", type=float, default=hrv.PCT_THRESH_DEFAULT,
                   help=f"Artifact filter tolerance (default: {hrv.PCT_THRESH_DEFAULT})")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.db is None:
        newest = next((e for e in catalog.entries() if e["downloaded"]), None)
        if newest is None:
            raise SystemExit(f"No downloaded StrapLog exports in {catalog.DEFAULT_DIR}; "
                             "pass --db.")
        args.db = Path(newest["path"])

    if not args.db.exists():
        raise SystemExit(
            f"Export not found: {args.db}\n"
            "Export the recording from StrapLog (<YYMMDD-HHMM>-<name>.sqlite).")
    if not straplog_db.is_straplog(args.db):
        raise SystemExit(f"{args.db} is not a StrapLog export (no `beat` table).")

    if args.list:
        print(json.dumps(list_recordings(args.db), indent=2))
        return 0

    pk = args.recording
    if pk is None:
        pk = pick_recording(list_recordings(args.db))

    args.out.mkdir(parents=True, exist_ok=True)

    rr_df, ev_df, start_wall = load_recording(args.db, pk)
    print(f"Recording PK={pk}: {len(rr_df)} RR intervals · {len(ev_df)} events · "
          f"{rr_df['t_rel'].max()/60:.1f} min · start {start_wall}",
          file=sys.stderr)

    feat = hrv.rolling_features(rr_df, args.window, args.stride, args.pct_thresh)
    feat.to_csv(args.out / "windowed_features.csv", index=False)
    print(f"Features: {len(feat)} windows · "
          f"HR {feat['hr_bpm'].min():.0f}-{feat['hr_bpm'].max():.0f} bpm · "
          f"α1 {feat['alpha1'].min():.2f}-{feat['alpha1'].max():.2f}",
          file=sys.stderr)

    aet_hr, aet_method = crossing_hr(feat, AET_ALPHA)
    vt1_hr, vt1_method = crossing_hr(feat, VT1_ALPHA)
    mid_hr, mid_method = crossing_hr(feat, MID_ALPHA)
    vt2_hr, vt2_method = crossing_hr(feat, VT2_ALPHA)
    hrmax = float(feat["hr_bpm"].max())

    # clamp VT2/MID to <= HRmax so zone edges don't exceed observed physiology
    vt2_use = min(vt2_hr, hrmax) if vt2_hr is not None else hrmax
    if mid_hr is not None:
        mid_use = min(mid_hr, vt2_use - 1)
    else:
        mid_use = (vt1_hr + vt2_use) / 2 if vt1_hr is not None else vt2_use

    plot_crossplot(feat, vt1_hr, vt2_hr, args.out / "crossplot_hr_alpha1.png")
    plot_timeseries(feat, ev_df, args.out / "timeseries_hr_alpha1.png")
    write_zone_outputs(
        args.out, start_wall, aet_hr, vt1_hr, mid_use, vt2_use, hrmax, feat,
        methods={"aet": aet_method, "vt1": vt1_method,
                 "mid": mid_method, "vt2": vt2_method},
    )

    # machine-readable summary on stdout so the caller can parse it
    summary = {
        "recording_pk": pk,
        "start_utc": start_wall.isoformat(),
        "duration_min": float(rr_df["t_rel"].max() / 60),
        "n_windows": len(feat),
        "hr_max": hrmax,
        "aet_hr": aet_hr, "aet_method": aet_method,
        "vt1_hr": vt1_hr, "vt1_method": vt1_method,
        "mid_hr": mid_use, "mid_method": mid_method,
        "vt2_hr": vt2_use, "vt2_method": vt2_method,
        "out_dir": str(args.out),
        "files": sorted(str(p.name) for p in args.out.iterdir()),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
