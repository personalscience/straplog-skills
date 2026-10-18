#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pandas", "straplog[plot]>=0.2"]
# ///
"""
Resting HRV analyzer for StrapLog recordings (via the `straplog` package).

Loads a short resting RR recording (morning/afternoon/evening/chair read),
computes HR + time-domain HRV (SDNN, RMSSD, LnRMSSD, pNN50), auto-tags by local
clock hour, appends a row to a longitudinal CSV log, and plots per-reading
(Poincaré + tachogram) and longitudinal trend.

If the recording carries StrapLog event markers ("diamonds") delimiting a
protocol window — e.g. a start diamond and an end diamond bracketing a 15-min
intervention — the headline metrics are computed over that marked window only,
and the leading/trailing segments are reported separately as pre/post baselines.
Without markers the whole recording is used, as before.

Usage
-----
  analyze_resting.py --list                        # JSON list of recordings
  analyze_resting.py                               # newest export, most recent recording
  analyze_resting.py --db 261015-1853-REST.sqlite  # bare name or a path
  analyze_resting.py --recording 5 --tag morning
  analyze_resting.py --recording 32 --markers ignore     # force whole-recording
  analyze_resting.py --recording 32 --session-window 4 19.2
  analyze_resting.py --trend-only                  # replot trend from existing log
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import straplog.db as straplog_db
from straplog import catalog, hrv
from straplog import plot as chart_style


# ---------- constants ----------------------------------------------------

DEFAULT_OUT = Path.home() / "Documents" / "hrv_resting_readings"
DEFAULT_LOG = Path.home() / "Documents" / "hrv_resting_log.csv"

PCT_THRESH_DEFAULT = 0.20            # stricter than ramp-test (rest is cleaner)
MIN_BEATS_RECORDING = 60             # ~1 min of data at 60 bpm
MIN_BEATS_SEGMENT = 30               # pre/post baselines below this are dropped

# Event-marker text that delimits a protocol window. A marker's free note can
# say anything, so match on prefix rather than exact equality.
START_NOTES = ("start", "session start", "begin", "start session", "resting_start")
END_NOTES = ("end", "session end", "stop", "finish", "resting_end")
# StrapLog's overnight recording brackets its automatic morning reading with
# these labels; that reading is dated (and tagged) by its own start, not the
# previous evening's recording start.
RESTING_START = "resting_start"

# Tag boundaries (local clock hour, lower inclusive)
TAG_BOUNDARIES = [
    (0,  "night"),      # 00:00-04:59
    (5,  "morning"),    # 05:00-09:59
    (10, "midday"),     # 10:00-13:59
    (14, "afternoon"),  # 14:00-17:59
    (18, "evening"),    # 18:00-23:59
]
VALID_TAGS = {t for _, t in TAG_BOUNDARIES}

LOG_COLUMNS = [
    "timestamp_wall", "tag", "recording_pk", "recording_name",
    "duration_min", "n_beats_raw", "n_beats_clean",
    "hr_mean", "hr_sd", "sdnn_ms", "rmssd_ms", "lnrmssd",
    "pnn50_pct", "sdsd_ms",
    # --- marked-window provenance (blank on pre-marker rows) ---
    "window_source", "session_start_min", "session_end_min", "rec_duration_min",
    "pre_min", "pre_hr", "pre_rmssd", "pre_lnrmssd",
    "post_min", "post_hr", "post_rmssd", "post_lnrmssd",
]


# ---------- load ----------------------------------------------------------

def list_recordings(db_path: Path) -> list[dict]:
    # date_utc keeps this skill's convention: local wall-clock labelled UTC
    return [{"pk": r["pk"], "name": r["name"],
             "date_utc": pd.Timestamp(r["start_local"]).tz_localize("UTC").isoformat(),
             "n_beats": r["n_beats"], "duration_min": r["duration_min"]}
            for r in straplog_db.list_recordings(db_path)]


def pick_recording(recs: list[dict]) -> int:
    if not recs:
        raise SystemExit("No recordings found in the database.")
    return sorted(recs, key=lambda r: r["date_utc"])[-1]["pk"]


def load_recording(db_path: Path, pk: int):
    rr, _, start_local, name = straplog_db.load_recording(db_path, pk)
    _require_beats(rr, pk)
    return rr, start_local.tz_localize("UTC"), name


def load_events(db_path: Path, pk: int, t0: float) -> list[dict]:
    """Event markers ("diamonds") for a recording, as minutes into the RR stream."""
    ev = straplog_db.load_events(db_path, pk)
    return [
        {"min": float((r["t"] - t0) / 60.0), "note": (r["note"] or "").strip()}
        for _, r in ev.iterrows()
    ]


def resolve_session_window(events: list[dict], rec_end_min: float):
    """
    Find a protocol window from start/end diamonds.

    Returns (start_min, end_min) or None. Takes the first start-ish marker and
    the last end-ish marker that follows it, so a session with intermediate
    diamonds still resolves to its outer bounds.
    """
    def matches(note: str, prefixes) -> bool:
        n = note.lower()
        return any(n == p or n.startswith(p) for p in prefixes)

    starts = [e["min"] for e in events if matches(e["note"], START_NOTES)]
    if not starts:
        return None
    s = min(starts)
    ends = [e["min"] for e in events
            if matches(e["note"], END_NOTES) and e["min"] > s]
    if not ends:
        return None
    e = min(max(ends), rec_end_min)
    if e - s < 1.0:            # implausibly short window; fall back to full
        return None
    return s, e


def _require_beats(rr: pd.DataFrame, pk: int) -> None:
    if len(rr) < MIN_BEATS_RECORDING:
        raise SystemExit(
            f"Recording {pk} has only {len(rr)} RR intervals "
            f"(< {MIN_BEATS_RECORDING}); too short for a useful resting read."
        )


# ---------- metrics ------------------------------------------------------

def compute_metrics(rr_ms: np.ndarray) -> dict:
    """`straplog.hrv.time_domain`, under the log's column names (which predate
    the package and must stay fixed for log continuity)."""
    td = hrv.time_domain(rr_ms)
    return {
        "n_beats_clean": td["n"],
        "hr_mean": td["hr_mean"],
        "hr_sd": td["hr_sd"],
        "sdnn_ms": td["sdnn_ms"],
        "rmssd_ms": td["rmssd_ms"],
        "lnrmssd": td["ln_rmssd"],
        "pnn50_pct": td["pnn50_pct"],
        "sdsd_ms": td["sdsd_ms"],
    }


# ---------- tagging ------------------------------------------------------

def auto_tag(start_stamp: pd.Timestamp) -> tuple[str, pd.Timestamp]:
    """
    Bucket the reading by hour-of-day. `straplog.db` has already applied the
    recording's stored tz offset, so the stamp is local wall-clock; its "UTC"
    label is cosmetic (this skill's convention), and we use the raw hour
    directly instead of converting via any tz.
    """
    naive = start_stamp.tz_convert("UTC").tz_localize(None).floor("us")
    hour = naive.hour
    tag = TAG_BOUNDARIES[0][1]
    for h, t in TAG_BOUNDARIES:
        if hour >= h:
            tag = t
    return tag, naive


# ---------- longitudinal log --------------------------------------------

def append_to_log(log_path: Path, row: dict) -> pd.DataFrame:
    """Read existing log (creating if absent), append row, write back.
    De-duplicates on (recording_pk, timestamp_utc)."""
    if log_path.exists():
        df = pd.read_csv(log_path)
    else:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(columns=LOG_COLUMNS)

    # drop any existing row with the same recording_pk + timestamp
    if "recording_pk" in df.columns and len(df):
        dup = (df["recording_pk"] == row["recording_pk"]) & \
              (df["timestamp_wall"] == row["timestamp_wall"])
        df = df[~dup]

    df = pd.concat([df, pd.DataFrame([row], columns=LOG_COLUMNS)],
                   ignore_index=True)
    df.sort_values("timestamp_wall", inplace=True, kind="stable")
    df.to_csv(log_path, index=False)
    return df


def load_log(log_path: Path) -> pd.DataFrame:
    if not log_path.exists():
        return pd.DataFrame(columns=LOG_COLUMNS)
    return pd.read_csv(log_path)


# ---------- plots --------------------------------------------------------

TAG_COLORS = {
    "night":     chart_style.SERIES["blue"],
    "morning":   chart_style.SERIES["green"],
    "midday":    chart_style.SERIES["magenta"],
    "afternoon": chart_style.SERIES["yellow"],
    "evening":   chart_style.SERIES["violet"],
}


def plot_poincare(rr: np.ndarray, metrics: dict, path: Path, title: str):
    from plotnine import (
        aes, coord_fixed, geom_label, geom_line, geom_point, ggplot,
        ggtitle, labs, xlim, ylim,
    )

    rr_n = rr[:-1]
    rr_n1 = rr[1:]
    sd1 = np.std(rr_n1 - rr_n, ddof=1) / np.sqrt(2)
    sd2 = np.std(rr_n + rr_n1, ddof=1) / np.sqrt(2)

    df = pd.DataFrame({"rr_n": rr_n, "rr_n1": rr_n1})
    lo, hi = float(rr.min() - 20), float(rr.max() + 20)
    diag = pd.DataFrame({"x": [lo, hi], "y": [lo, hi]})

    stats_text = (f"RMSSD = {metrics['rmssd_ms']:.1f} ms\n"
                  f"LnRMSSD = {metrics['lnrmssd']:.2f}\n"
                  f"SDNN = {metrics['sdnn_ms']:.1f} ms\n"
                  f"SD1 = {sd1:.1f} ms\nSD2 = {sd2:.1f} ms\n"
                  f"HR = {metrics['hr_mean']:.0f} bpm")
    # Anchor near top-left of the fixed [lo, hi] square — the data-coordinate
    # equivalent of the old (0.03, 0.97) axes-fraction stats box.
    label_x = lo + 0.05 * (hi - lo)
    label_y = hi - 0.03 * (hi - lo)
    label_df = pd.DataFrame({"x": [label_x], "y": [label_y], "label": [stats_text]})

    p = (ggplot(df, aes("rr_n", "rr_n1"))
         + geom_point(alpha=0.55, color=chart_style.SERIES["blue"], size=1.5,
                      stroke=0)
         + geom_line(diag, aes("x", "y"), color=chart_style.INK["baseline"],
                     linetype="dashed", size=0.9)
         + geom_label(label_df, aes("x", "y", label="label"),
                      fill=chart_style.INK["surface"],
                      color=chart_style.INK["secondary"],
                      size=8, family="monospace", ha="left", va="top")
         + xlim(lo, hi) + ylim(lo, hi) + coord_fixed()
         + labs(x="RR$_n$ (ms)", y="RR$_{n+1}$ (ms)")
         + ggtitle(f"Poincaré plot — {title}")
         + chart_style.theme_house())
    p.save(path, width=6, height=6, dpi=chart_style.DPI, verbose=False)


def plot_tachogram(rr: np.ndarray, t_rel: np.ndarray, path: Path, title: str,
                   window: tuple[float, float] | None = None,
                   events: list[dict] | None = None):
    from plotnine import (
        aes, annotate, geom_hline, geom_line, geom_rect, geom_vline, ggplot,
        ggtitle, labs,
    )

    df = pd.DataFrame({"t_min": t_rel / 60.0, "rr": rr})
    # Mean line describes the analyzed window, not the whole strip, so it agrees
    # with the reported HR.
    in_win = df["t_min"].between(*window) if window else pd.Series(True, index=df.index)
    mean_rr = float(df.loc[in_win, "rr"].mean())

    p = ggplot(df, aes("t_min", "rr"))

    if window:
        band = pd.DataFrame({"xmin": [window[0]], "xmax": [window[1]],
                             "ymin": [float(rr.min())], "ymax": [float(rr.max())]})
        p = (p + geom_rect(band, aes(xmin="xmin", xmax="xmax",
                                     ymin="ymin", ymax="ymax"),
                           inherit_aes=False,
                           fill=chart_style.SERIES["blue"], alpha=0.10)
               + geom_vline(xintercept=list(window),
                            color=chart_style.SERIES["blue"],
                            linetype="dashed", size=0.9))

    p = (p + geom_line(color=chart_style.SERIES["red"], size=1.1)
         + geom_hline(yintercept=mean_rr, color=chart_style.INK["muted"],
                      linetype="dashed", size=0.9)
         + annotate("text", x=float(df["t_min"].max()), y=mean_rr,
                    label=f"mean = {mean_rr:.0f} ms  ({60000/mean_rr:.0f} bpm)",
                    color=chart_style.INK["muted"], ha="right", va="bottom",
                    size=8)
         + ggtitle(f"RR tachogram — {title}")
         + labs(x="Time into reading (min)", y="RR (ms)")
         + chart_style.theme_house())

    for e in (events or []):
        if not e["note"]:
            continue
        p = p + annotate("text", x=e["min"], y=float(rr.max()), label=e["note"],
                         color=chart_style.SERIES["blue"], ha="center",
                         va="bottom", size=7)

    p.save(path, width=10, height=4, dpi=chart_style.DPI, verbose=False)


def plot_trend(log_df: pd.DataFrame, path: Path, window: int = 7):
    from plotnine import (
        aes, element_text, geom_line, geom_point, geom_ribbon, ggplot, ggtitle,
        labs, scale_color_manual, scale_x_datetime, theme,
    )

    if log_df.empty:
        return
    df = log_df.copy()
    df["ts"] = pd.to_datetime(df["timestamp_wall"])
    df.sort_values("ts", inplace=True)

    # 7-day rolling mean + ±1 SD band on lnrmssd (all tags combined)
    s = df.set_index("ts")["lnrmssd"]
    roll_mean = s.rolling(f"{window}D").mean()
    roll_std = s.rolling(f"{window}D").std()
    roll_df = pd.DataFrame({
        "ts": roll_mean.index, "mean": roll_mean.values, "std": roll_std.values,
    })
    roll_df["ymin"] = roll_df["mean"] - roll_df["std"]
    roll_df["ymax"] = roll_df["mean"] + roll_df["std"]

    # per-tag scatter — colour mapped to tag so plotnine builds the legend; a
    # surface-coloured ring (via stroke) keeps overlapping points readable.
    p = (ggplot()
         + geom_ribbon(roll_df, aes("ts", ymin="ymin", ymax="ymax"),
                       fill=chart_style.INK["baseline"], alpha=0.22)
         + geom_line(roll_df, aes("ts", "mean"),
                     color=chart_style.INK["primary"], size=1.6)
         + geom_point(df, aes("ts", "lnrmssd", color="tag"), size=3, stroke=0)
         + scale_color_manual(values=TAG_COLORS)
         + scale_x_datetime()
         + labs(x="Date", y="LnRMSSD")
         + ggtitle("Resting HRV trend — LnRMSSD")
         + chart_style.theme_house()
         + theme(axis_text_x=element_text(angle=45, ha="right")))
    p.save(path, width=10, height=5, dpi=chart_style.DPI, verbose=False)


# ---------- CLI ----------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", type=catalog.resolve, default=None,
                   help="StrapLog export: a path, or a bare name in the iCloud "
                        "folder (default: the newest downloaded export there)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                   help=f"Output directory for plots (default: {DEFAULT_OUT})")
    p.add_argument("--log", type=Path, default=DEFAULT_LOG,
                   help=f"Longitudinal CSV log (default: {DEFAULT_LOG})")
    p.add_argument("--recording", type=int, default=None,
                   help="StrapLog recording.id to analyze (default: most recent)")
    p.add_argument("--tag", type=str, default=None,
                   choices=sorted(VALID_TAGS),
                   help="Override auto-tag (morning/midday/afternoon/evening/night)")
    p.add_argument("--list", action="store_true",
                   help="Print JSON list of recordings and exit")
    p.add_argument("--no-append", action="store_true",
                   help="Do not update the longitudinal log")
    p.add_argument("--trend-only", action="store_true",
                   help="Replot trend from existing log; skip recording analysis")
    p.add_argument("--pct-thresh", type=float, default=PCT_THRESH_DEFAULT,
                   help=f"Artifact filter tolerance (default: {PCT_THRESH_DEFAULT})")
    p.add_argument("--window", type=int, default=7,
                   help="Rolling baseline window in days for trend plot (default: 7)")
    p.add_argument("--markers", choices=("auto", "ignore"), default="auto",
                   help="Use start/end event diamonds to bound the reading "
                        "(default: auto; 'ignore' forces whole-recording)")
    p.add_argument("--session-window", type=float, nargs=2,
                   metavar=("START_MIN", "END_MIN"), default=None,
                   help="Manually bound the reading, in minutes into the "
                        "recording; overrides event diamonds")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    # --- trend-only mode ---
    if args.trend_only:
        log_df = load_log(args.log)
        if log_df.empty:
            raise SystemExit(f"Log is empty or missing: {args.log}")
        args.out.mkdir(parents=True, exist_ok=True)
        trend_path = args.out / "trend_lnrmssd.png"
        plot_trend(log_df, trend_path, window=args.window)
        print(json.dumps({
            "trend_plot": str(trend_path),
            "n_readings": len(log_df),
            "log": str(args.log),
        }, indent=2))
        return 0

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

    pk = args.recording if args.recording is not None \
        else pick_recording(list_recordings(args.db))

    rr_df, start_stamp, rec_name = load_recording(args.db, pk)
    n_raw = len(rr_df)

    keep = hrv.artifact_mask(rr_df["rr_ms"].to_numpy(), pct_thresh=args.pct_thresh)
    if int(keep.sum()) < MIN_BEATS_RECORDING:
        raise SystemExit(
            f"After artifact correction only {int(keep.sum())} beats remain; "
            "reading too noisy for HRV analysis."
        )
    # Keep beats and their timestamps aligned through the filter so the analysis
    # window can be sliced by time.
    clean_df = rr_df.loc[keep, ["t_rel", "rr_ms"]].reset_index(drop=True)
    clean_df["t_min"] = clean_df["t_rel"] / 60.0
    clean_all = clean_df["rr_ms"].to_numpy()

    rec_duration_min = float(rr_df["t_rel"].max() / 60.0)

    # --- resolve the analysis window from diamonds (or manual override) ---
    events = load_events(args.db, pk, float(rr_df["t"].iloc[0]))
    if args.session_window is not None:
        sess = (float(args.session_window[0]), float(args.session_window[1]))
        window_source = "manual"
    elif args.markers == "auto":
        sess = resolve_session_window(events, rec_duration_min)
        window_source = "markers" if sess else "full"
    else:
        sess, window_source = None, "full"

    def segment(lo: float, hi: float) -> np.ndarray:
        m = (clean_df["t_min"] >= lo) & (clean_df["t_min"] < hi)
        return clean_df.loc[m, "rr_ms"].to_numpy()

    if sess:
        analyzed = segment(*sess)
        if len(analyzed) < MIN_BEATS_RECORDING:
            raise SystemExit(
                f"Marked window {sess[0]:.2f}-{sess[1]:.2f} min holds only "
                f"{len(analyzed)} clean beats; re-run with --markers ignore."
            )
        pre = segment(0.0, sess[0])
        post = segment(sess[1], rec_duration_min + 1.0)
        duration_min = sess[1] - sess[0]
    else:
        analyzed, pre, post = clean_all, np.array([]), np.array([])
        duration_min = rec_duration_min

    metrics = compute_metrics(analyzed)
    reading_stamp = start_stamp
    if sess and window_source == "markers" and any(
            e["note"].lower().startswith(RESTING_START) for e in events):
        reading_stamp = start_stamp + pd.Timedelta(minutes=sess[0])
    auto, wall_ts = auto_tag(reading_stamp)
    tag = args.tag or auto

    def baseline(seg: np.ndarray) -> dict:
        if len(seg) < MIN_BEATS_SEGMENT:
            return {"min": None, "hr": None, "rmssd": None, "lnrmssd": None}
        m = compute_metrics(seg)
        return {
            "min": round(float(seg.sum() / 60000.0), 2),
            "hr": round(m["hr_mean"], 1),
            "rmssd": round(m["rmssd_ms"], 1),
            "lnrmssd": round(m["lnrmssd"], 3),
        }

    pre_b, post_b = baseline(pre), baseline(post)

    row = {
        "timestamp_wall": wall_ts.isoformat(),
        "tag": tag,
        "recording_pk": pk,
        "recording_name": rec_name,
        "duration_min": round(duration_min, 2),
        "n_beats_raw": n_raw,
        **metrics,
        "window_source": window_source,
        "session_start_min": round(sess[0], 2) if sess else None,
        "session_end_min": round(sess[1], 2) if sess else None,
        "rec_duration_min": round(rec_duration_min, 2),
        "pre_min": pre_b["min"], "pre_hr": pre_b["hr"],
        "pre_rmssd": pre_b["rmssd"], "pre_lnrmssd": pre_b["lnrmssd"],
        "post_min": post_b["min"], "post_hr": post_b["hr"],
        "post_rmssd": post_b["rmssd"], "post_lnrmssd": post_b["lnrmssd"],
    }
    # round everything nicely for CSV
    for k in ("hr_mean", "hr_sd", "sdnn_ms", "rmssd_ms", "lnrmssd",
              "pnn50_pct", "sdsd_ms"):
        row[k] = round(row[k], 3)

    args.out.mkdir(parents=True, exist_ok=True)
    title = f"PK {pk} · {wall_ts:%Y-%m-%d %H:%M} {tag}"
    if sess:
        title += f" · marked {duration_min:.1f} min"
    plot_poincare(analyzed, metrics, args.out / f"reading_{pk}_poincare.png",
                  title)
    plot_tachogram(clean_all, clean_df["t_rel"].to_numpy(),
                   args.out / f"reading_{pk}_tachogram.png", title,
                   window=sess, events=events)

    if not args.no_append:
        log_df = append_to_log(args.log, row)
        trend_path = args.out / "trend_lnrmssd.png"
        plot_trend(log_df, trend_path, window=args.window)
    else:
        log_df = load_log(args.log)
        trend_path = None

    summary = {
        "recording_pk": pk,
        "recording_name": rec_name,
        "start_wall": wall_ts.isoformat(),
        "tag": tag,
        "tag_source": "user" if args.tag else "auto",
        "window_source": window_source,
        "session_window_min": [round(sess[0], 2), round(sess[1], 2)] if sess else None,
        "rec_duration_min": round(rec_duration_min, 2),
        "duration_min": round(duration_min, 2),
        "n_beats_raw": n_raw,
        "n_beats_clean": metrics["n_beats_clean"],
        "hr_mean_bpm": round(metrics["hr_mean"], 1),
        "sdnn_ms": round(metrics["sdnn_ms"], 1),
        "rmssd_ms": round(metrics["rmssd_ms"], 1),
        "lnrmssd": round(metrics["lnrmssd"], 2),
        "pnn50_pct": round(metrics["pnn50_pct"], 1),
        "pre": pre_b,
        "post": post_b,
        "events": events,
        "log": str(args.log) if not args.no_append else None,
        "log_rows": len(log_df),
        "files": {
            "poincare": str(args.out / f"reading_{pk}_poincare.png"),
            "tachogram": str(args.out / f"reading_{pk}_tachogram.png"),
            "trend": str(trend_path) if trend_path else None,
        },
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
