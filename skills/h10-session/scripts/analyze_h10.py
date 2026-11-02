#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pandas", "fitparse", "straplog[plot]>=0.3"]
# ///
"""
Generic Polar H10 session analyzer for StrapLog SQLite exports.

One pipeline, three activity modes. Reads a single-file StrapLog snapshot
(via the `straplog` package), pulls raw RR intervals for one recording,
cleans artifacts (`straplog.hrv`), computes HR / DFA alpha1 / Karvonen time-in-zones, and then
branches on activity:

  endurance  (bike, run, long walk)  -> cardiac drift, HR/alpha1 decoupling,
                                        aerobic-durability split-half stats,
                                        optional GPS overlay (speed/grade)
  strength   (lifting)               -> per-set HR peaks, HRR60/120/180 from a
                                        rest anchor, mostly-Z1 expectation
  generic    (anything else)         -> HR summary + zones + alpha1 only

Timestamps
----------
StrapLog stores true UTC plus tz_offset_s; `straplog.db` converts it to
local wall-clock (Apple-epoch seconds), so everything below works in local
time. GPX/FIT tracks, by contrast, are UTC -- so track alignment converts the
track, never the RR data.

Usage
-----
  analyze_h10.py --list                      # newest exports in iCloud Drive
  analyze_h10.py --db 261018-0730-RIDE.sqlite  # bare name or a path
  analyze_h10.py --db ... --recording 14 --mode endurance --track ride.gpx
  analyze_h10.py --db ... --mode strength --sets workout.txt --rest-marker
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd

import straplog.db as straplog_db
from straplog import catalog, hrv
from straplog import plot as chart_style


# ---------- constants -----------------------------------------------------

# Richard's audited anchors (Fitness.md, 2026-07). Override via CLI.
HRMAX_DEFAULT = 182
HRREST_DEFAULT = 57

MIN_BEATS_RECORDING = 300

AET_ALPHA = 1.00
VT1_ALPHA = 0.75
VT2_ALPHA = 0.50

# Karvonen %HRR bands (shared with the polar-strength skill)
ZONES = [
    ("Z1 recovery",   0.00, 0.60),
    ("Z2 aerobic",    0.60, 0.70),
    ("Z3 tempo",      0.70, 0.80),
    ("Z4 threshold",  0.80, 0.90),
    ("Z5 VO2max",     0.90, 1.01),
]

MODE_KEYWORDS = {
    "strength": ("strength", "lift", "weights", "gym", "rt"),
    "endurance": ("bike", "cycl", "ride", "run", "walk", "hike", "row", "swim"),
}


# ---------- load ----------------------------------------------------------

def list_recordings(db_path: Path) -> list[dict]:
    """StrapLog recordings, each with a guessed analysis mode."""
    rows = straplog_db.list_recordings(db_path)
    # resolve() so a relative --db still yields a real folder name for the
    # mode heuristic rather than an empty string.
    folder = db_path.resolve().parent.name
    for r in rows:
        r["guessed_mode"] = guess_mode(r["name"], folder=folder,
                                       duration_min=r["duration_min"],
                                       avg_hr=r["avg_hr"])
    return rows


def _match_keywords(text: str) -> str | None:
    low = (text or "").lower()
    for mode, keys in MODE_KEYWORDS.items():
        if any(k in low for k in keys):
            return mode
    return None


def guess_mode(name: str, folder: str = "", duration_min: float | None = None,
               avg_hr: float | None = None) -> str:
    """Best-effort mode detection, in descending order of trustworthiness.

    1. The recording's own name.
    2. The enclosing folder name -- but ONLY when the name is blank. An
       unnamed recording is often filed in `~/Downloads/<activity>YYMMDD/`
       (e.g. `cycling260726`), carrying the same keyword. Gating this on a
       blank name matters: an older export holds many sessions, so a folder
       called `cycling260726` can also contain `SLEEP260722-23`, and an
       ungated folder match would call that sleep strip an endurance ride.
    3. A duration/HR shape test, deliberately conservative: only long *and*
       elevated sessions are called endurance, because a 60-min strength
       circuit at mean HR ~95 is otherwise indistinguishable by shape alone.
       (An overnight sleep strip is long but low-HR, so it fails this too and
       correctly stays `generic`.) Anything that fails stays `generic` rather
       than guessing.
    """
    hit = _match_keywords(name)
    if hit:
        return hit
    if not (name or "").strip():
        hit = _match_keywords(folder)
        if hit:
            return hit
    if duration_min is not None and avg_hr is not None:
        if duration_min >= 90 and avg_hr >= 105:
            return "endurance"
    return "generic"


def load_recording(db_path: Path, pk: int):
    rr, ev, start_local, name = straplog_db.load_recording(db_path, pk)
    if len(rr) < MIN_BEATS_RECORDING:
        raise SystemExit(
            f"Recording {pk} has only {len(rr)} RR intervals "
            f"(< {MIN_BEATS_RECORDING}); too short to analyze.")
    return rr, ev, start_local, name


# ---------- per-beat + windowed series ------------------------------------

def beat_series(rr_df: pd.DataFrame, pct_thresh: float) -> tuple[pd.DataFrame, np.ndarray]:
    """Whole-recording cleaned beat series -> instantaneous HR on a 1 Hz grid,
    plus the per-beat keep mask. Same filter as the windowed features, so both
    agree on which beats are artifacts."""
    rr = rr_df["rr_ms"].to_numpy(dtype=float)
    t = rr_df["t_rel"].to_numpy()
    keep = hrv.artifact_mask(rr, pct_thresh)
    grid = np.arange(0, float(t.max()) + 1, 1.0)
    hr_1hz = np.interp(grid, t[keep], 60000.0 / rr[keep])
    return pd.DataFrame({"t": grid, "hr": hr_1hz}), keep


NOISY_MINUTE_FRAC = 0.10   # clean minutes run 0-4 % dropped; contact loss 15-50 %


def noisy_spans(rr_df: pd.DataFrame, keep: np.ndarray, start_local) -> list[dict]:
    """Stretches where the strap lost contact: whole minutes in which the
    artifact filter dropped more than NOISY_MINUTE_FRAC of the beats, merged
    into runs. Beats keep arriving (coverage stays ~1.0), but in a long noisy
    run the 5-beat median filter has no clean neighbours to judge against, so
    junk survives into HR, zones and alpha1. Nothing inside a span is
    trustworthy; `--start` skips a bad lead-in."""
    minute = (rr_df["t_rel"].to_numpy() // 60).astype(int)
    frac = pd.Series(~keep).groupby(minute).mean()
    spans: list[dict] = []
    for m, f in frac.items():
        if f <= NOISY_MINUTE_FRAC:
            continue
        if spans and spans[-1]["_to"] == m - 1:
            spans[-1]["_to"] = m
            spans[-1]["_fr"].append(f)
        else:
            spans.append({"_from": m, "_to": m, "_fr": [f]})
    return [{"from": clock(start_local, sp["_from"] * 60),
             "to": clock(start_local, (sp["_to"] + 1) * 60),
             "minutes": sp["_to"] - sp["_from"] + 1,
             "artifact_pct": round(100 * float(np.mean(sp["_fr"])), 1)}
            for sp in spans]


def trim_start(rr_df: pd.DataFrame, ev_df: pd.DataFrame, start_local,
               hhmm: str):
    """Drop everything before local clock time `hhmm` (HH:MM[:SS]) and
    re-zero the clocks there, so every downstream stat sees only the kept
    part. Markers before the cut are dropped."""
    t0 = pd.Timestamp(start_local)
    cut = pd.Timestamp(f"{t0.date()} {hhmm}")
    if cut < t0 - pd.Timedelta(hours=12):      # recording crossed midnight
        cut += pd.Timedelta(days=1)
    skip = (cut - t0).total_seconds()
    if not 0 < skip < float(rr_df["t_rel"].max()) - 60:
        raise SystemExit(f"--start {hhmm} is outside the recording "
                         f"({t0:%H:%M:%S} + {rr_df['t_rel'].max() / 60:.0f} min).")
    rr_df = rr_df[rr_df["t_rel"] >= skip].copy()
    rr_df["t_rel"] -= skip
    ev_df = ev_df[ev_df["t_rel"] >= skip].copy()
    ev_df["t_rel"] -= skip
    return rr_df.reset_index(drop=True), ev_df.reset_index(drop=True), cut, skip


# ---------- zones ---------------------------------------------------------

def zone_breakdown(hr_1hz: pd.DataFrame, hr_max: float, hr_rest: float) -> dict:
    reserve = max(1.0, hr_max - hr_rest)
    frac = (hr_1hz["hr"] - hr_rest) / reserve
    out = {}
    for name, lo, hi in ZONES:
        out[name] = int(((frac >= lo) & (frac < hi)).sum())
    return out


def rr_quality(rr_df: pd.DataFrame, hr_mean: float, keep: np.ndarray) -> dict:
    """Decide whether DFA α1 is trustworthy for this recording.

    Two failure modes push α1 in OPPOSITE directions, so a bad α1 is not
    always an obviously bad number:

      * missed/extra beats (loose strap, dry electrodes) randomize the series
        -> α1 falsely LOW (toward 0.5), mimicking high intensity;
      * a genuinely over-smoothed / interpolated RR stream kills beat-to-beat
        variability -> α1 falsely HIGH (toward 1.5), mimicking an easy effort.

    CRITICAL — low |ΔRR| ALONE IS NOT EVIDENCE OF OVER-SMOOTHING.
    Beat-to-beat variability collapses with exercise intensity (vagal
    withdrawal). Measured on Richard: median |ΔRR| runs ~5 ms at HR 100-109
    but ~1 ms at HR 130-149 on a session whose capture was independently
    confirmed clean. A flat threshold like "<=1.5 ms is over-smoothed" simply
    re-detects hard efforts and defames the strap -- it produced exactly one
    such false alarm on the 2026-07-18 ride, and comparing a bike ride's
    |ΔRR| against an easier walk's compounds the error, because that
    comparison is confounded by intensity.

    The discriminating test is WITHIN-SESSION SLOPE, not level: real RR shows
    variability that DECLINES as HR rises. A truly filtered stream is flat --
    uniformly low at every intensity, because the filter does not care how
    hard you are working. So we bin |ΔRR| by instantaneous HR and flag only
    when variability fails to track intensity.
    """
    v = rr_df["rr_ms"].to_numpy(dtype=float)
    d = np.abs(np.diff(v))
    med_drr = float(np.median(d)) if d.size else np.nan
    rmssd = float(np.sqrt((d ** 2).mean())) if d.size else np.nan
    # Successive differences between beats the filter kept, both of a pair:
    # comparable to the app's RMSSD. The raw one above drives the flag.
    both = keep[:-1] & keep[1:]
    rmssd_clean = float(np.sqrt((d[both] ** 2).mean())) if both.any() else np.nan

    # |ΔRR| binned by the instantaneous HR of the *preceding* beat
    hr_inst = 60000.0 / v[:-1] if v.size > 1 else np.array([])
    bins: dict[int, float] = {}
    for lo in range(60, 200, 10):
        sel = d[(hr_inst >= lo) & (hr_inst < lo + 10)]
        if sel.size >= 200:
            bins[lo] = round(float(np.median(sel)), 2)

    # Physiology gives a LARGE spread: measured 5.0 -> 1.0 ms (5x) on the bike,
    # 7.0 -> 1.0 (7x) on a walk. A filtered stream sits near 1.0x. Require a
    # real ratio, not a bare inequality -- two noisy bin medians satisfy ">"
    # half the time by chance, which lets a flat synthetic stream pass.
    MIN_RATIO = 1.5
    MIN_HR_SPAN = 20

    flags = []
    slope_ok = None
    ks = sorted(bins)
    testable = len(bins) >= 2 and (ks[-1] - ks[0]) >= MIN_HR_SPAN

    if testable:
        lo_band, hi_band = bins[ks[0]], bins[ks[-1]]
        ratio = (lo_band / hi_band) if hi_band > 0 else float("inf")
        slope_ok = ratio >= MIN_RATIO
        if not slope_ok:
            flags.append(
                f"flat_drr_across_hr: med {med_drr:.1f} ms, HR {ks[0]}-"
                f"{ks[-1] + 9}, {lo_band}->{hi_band} ms, ratio {ratio:.1f}x "
                f"vs {MIN_RATIO}x expected")
    elif med_drr <= 1.5:
        flags.append(
            f"drr_untestable_low: med {med_drr:.1f} ms, HR range too narrow "
            "to test the intensity slope")

    if hr_mean > 100 and rmssd > 60:
        flags.append(
            f"implausible_rmssd: {rmssd:.0f} ms at mean HR {hr_mean:.0f}")

    return {
        "median_abs_drr_ms": round(med_drr, 2),
        "rmssd_ms": round(rmssd, 1),
        "rmssd_clean_ms": round(rmssd_clean, 1),
        "median_abs_drr_by_hr_band": bins,
        "variability_tracks_intensity": slope_ok,
        "alpha1_trustworthy": not flags,
        "flags": flags,
    }


# stdout is the expensive channel: summary.md keeps the full set table,
# stdout gets at most this many so a low-prominence circuit can't flood it.
SETS_STDOUT_CAP = 12

# stdout carries short codes; these expand them for the human-facing summary.md
ALPHA1_ZONE_LABELS = {
    ">1.00": "alpha1 > 1.00 (easy / sub-AeT)",
    "0.75-1.00": "0.75-1.00 (aerobic Z2)",
    "0.50-0.75": "0.50-0.75 (tempo, above VT1)",
    "<0.50": "alpha1 < 0.50 (above VT2)",
}
RECON_VERDICT_TEXT = {
    "agree": ("agree — the strap was detecting beats correctly, so HR-derived "
              "metrics are trustworthy"),
    "partial": ("partially agree — some divergence; check for dropouts before "
                "trusting drift or HRR"),
    "disagree": ("DISAGREE — the two recordings tell different stories; treat "
                 "HR metrics as unreliable, not just α1"),
}
FLAG_TEXT = {
    "flat_drr_across_hr": ("variability does not track intensity — the RR "
                           "stream looks over-smoothed / interpolated rather "
                           "than vagally withdrawn"),
    "drr_untestable_low": ("α1 level unverified; cross-check against an "
                           "independent HR source (pass a companion track)"),
    "implausible_rmssd": "implausibly high — suggests missed/extra beat artifacts",
}


def expand_flag(f: str) -> str:
    """`code: numbers` -> `numbers — explanation` for summary.md."""
    code, _, rest = f.partition(":")
    why = FLAG_TEXT.get(code.strip())
    return f"{rest.strip()} — {why}" if why else f


def round_floats(o, nd: int = 2):
    """Kill 73.20000000000002-style noise across the whole stdout payload."""
    if isinstance(o, float):
        return round(o, nd)
    if isinstance(o, dict):
        return {k: round_floats(v, nd) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [round_floats(v, nd) for v in o]
    return o


def alpha1_breakdown(feat: pd.DataFrame) -> dict:
    a = feat["alpha1"].dropna()
    if not len(a):
        return {}
    return {
        ">1.00": int((a > AET_ALPHA).sum()),
        "0.75-1.00": int(((a > VT1_ALPHA) & (a <= AET_ALPHA)).sum()),
        "0.50-0.75": int(((a > VT2_ALPHA) & (a <= VT1_ALPHA)).sum()),
        "<0.50": int((a <= VT2_ALPHA).sum()),
    }


# ---------- endurance analytics -------------------------------------------

def drift_stats(hr_1hz: pd.DataFrame, feat: pd.DataFrame,
                warmup_sec: float) -> dict:
    """Cardiac drift + HR/alpha1 decoupling over the steady portion."""
    main = hr_1hz[hr_1hz["t"] >= warmup_sec]
    if len(main) < 600:
        return {"note": "main block too short for drift stats"}

    half = len(main) // 2
    h1, h2 = main.iloc[:half], main.iloc[half:]
    hr1, hr2 = float(h1["hr"].mean()), float(h2["hr"].mean())

    fm = feat[feat["t_center"] >= warmup_sec]
    a1 = a2 = None
    if len(fm) >= 4:
        fh = len(fm) // 2
        a1 = float(fm.iloc[:fh]["alpha1"].mean())
        a2 = float(fm.iloc[fh:]["alpha1"].mean())

    return {
        "main_start_min": round(warmup_sec / 60.0, 1),
        "first_half_hr": round(hr1, 1),
        "second_half_hr": round(hr2, 1),
        "drift_bpm": round(hr2 - hr1, 1),
        "drift_pct": round(100.0 * (hr2 - hr1) / hr1, 1),
        "first_half_alpha1": round(a1, 3) if a1 is not None else None,
        "second_half_alpha1": round(a2, 3) if a2 is not None else None,
        "alpha1_drop": round(a2 - a1, 3) if a1 is not None else None,
    }


def longest_in_band(hr_1hz: pd.DataFrame, lo: float, hi: float) -> dict:
    """Longest continuous stretch inside an HR band (allows 15 s excursions)."""
    inside = ((hr_1hz["hr"] >= lo) & (hr_1hz["hr"] <= hi)).to_numpy()
    best = cur = 0
    best_end = 0
    grace = 0
    for i, v in enumerate(inside):
        if v:
            cur += 1 + grace
            grace = 0
        else:
            grace += 1
            if grace > 15:
                if cur > best:
                    best, best_end = cur, i
                cur = 0
                grace = 0
    if cur > best:
        best, best_end = cur, len(inside)
    return {
        "band": f"{lo:.0f}-{hi:.0f} bpm",
        "longest_sec": int(best),
        "longest_min": round(best / 60.0, 1),
        "start_min": round((best_end - best) / 60.0, 1),
        "total_min": round(float(inside.sum()) / 60.0, 1),
    }


# ---------- GPS track overlay ---------------------------------------------

def _local(tag: str) -> str:
    """Strip any XML namespace: '{http://...}hr' -> 'hr'."""
    return tag.rsplit("}", 1)[-1].lower()


# Local tag names various apps use for heart rate inside <extensions>.
# Strava/Garmin emit gpxtpx:hr (TrackPointExtension v1/v2); Cluetrust-style
# exporters emit gpxdata:hr; some apps emit a bare <hr> or <heartrate>.
# Matching on the LOCAL name makes this vendor-neutral.
HR_TAGS = {"hr", "heartrate", "heart_rate", "bpm"}
CAD_TAGS = {"cad", "cadence"}


def load_track(path: Path) -> pd.DataFrame | None:
    """Parse a GPX or FIT track into t_utc/lat/lon/ele (+hr, cadence if present).

    Namespace-agnostic by design: any app that writes GPX 1.0/1.1 with a
    heart-rate extension works, not just Strava.
    """
    suffix = path.suffix.lower()

    if suffix == ".gpx":
        root = ET.parse(path).getroot()
        rows = []
        for pt in root.iter():
            if _local(pt.tag) != "trkpt":
                continue
            rec = {"lat": pt.get("lat"), "lon": pt.get("lon"),
                   "ele_m": np.nan, "hr_track": np.nan, "cad": np.nan,
                   "t_utc": None}
            # descend the whole subtree so extension nesting depth/prefix
            # (TrackPointExtension, gpxdata, bare) does not matter
            for el in pt.iter():
                name = _local(el.tag)
                txt = (el.text or "").strip()
                if not txt:
                    continue
                try:
                    if name == "time" and rec["t_utc"] is None:
                        rec["t_utc"] = pd.to_datetime(txt)
                    elif name == "ele":
                        rec["ele_m"] = float(txt)
                    elif name in HR_TAGS:
                        rec["hr_track"] = float(txt)
                    elif name in CAD_TAGS:
                        rec["cad"] = float(txt)
                except (ValueError, TypeError):
                    continue
            if rec["t_utc"] is None or rec["lat"] is None:
                continue
            rec["lat"] = float(rec["lat"])
            rec["lon"] = float(rec["lon"])
            rows.append(rec)
        return pd.DataFrame(rows) if rows else None

    if suffix == ".fit":
        try:
            from fitparse import FitFile
        except ImportError:
            print("[warn] .fit given but fitparse not installed "
                  "(`uv run --with fitparse`); pass the .gpx instead — same "
                  "ride, no extra dependency.", file=sys.stderr)
            return None
        rows = []
        for msg in FitFile(str(path)).get_messages("record"):
            d = {f.name: f.value for f in msg}
            if "timestamp" not in d:
                continue
            # FIT stores lat/lon in semicircles
            lat, lon = d.get("position_lat"), d.get("position_long")
            scale = 180.0 / 2 ** 31
            rows.append({
                "t_utc": pd.to_datetime(d["timestamp"]),
                "lat": lat * scale if isinstance(lat, int) else lat,
                "lon": lon * scale if isinstance(lon, int) else lon,
                "ele_m": d.get("altitude", d.get("enhanced_altitude", np.nan)),
                "hr_track": d.get("heart_rate", np.nan),
                "cad": d.get("cadence", np.nan),
            })
        return pd.DataFrame(rows) if rows else None

    return None


def _haversine(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(a))


def merge_track(hr_1hz: pd.DataFrame, trk: pd.DataFrame,
                start_local: pd.Timestamp, utc_offset_hours: float) -> tuple:
    """Align a UTC track to the LOCAL-clock RR series and add speed/grade.

    RR times are local; the track is UTC. We shift the *track*."""
    # FIT interleaves record messages: some carry GPS only, others HR only.
    # Timestamp everything FIRST, then split -- dropping non-GPS rows up front
    # would silently discard the entire HR channel.
    all_rows = trk.dropna(subset=["t_utc"]).copy()
    if all_rows.empty:
        return hr_1hz, None
    t_all = pd.to_datetime(all_rows["t_utc"], utc=True).dt.tz_localize(None)
    all_rows["t_local"] = t_all + pd.Timedelta(hours=utc_offset_hours)
    all_rows = all_rows.sort_values("t_local")
    all_rows["t_rel"] = (all_rows["t_local"] - start_local).dt.total_seconds()

    hr_track_series = all_rows[["t_rel", "hr_track"]].dropna() \
        if "hr_track" in all_rows.columns else pd.DataFrame()

    trk = all_rows.dropna(subset=["lat", "lon"]).copy()
    if trk.empty:
        # Indoor session: a strength/trainer FIT carries HR but no GPS at all.
        # Do NOT bail -- that would throw away the whole HR channel and with it
        # the sensor reconciliation, which is the only reason to pass a track
        # for an indoor lift. Merge HR on its own timestamps and return.
        if len(hr_track_series) < 60:
            return hr_1hz, None
        merged = hr_1hz.copy()
        merged["hr_track"] = np.interp(
            merged["t"], hr_track_series["t_rel"], hr_track_series["hr_track"],
            left=np.nan, right=np.nan)
        return merged, {
            "track_start_local": str(all_rows["t_local"].iloc[0]),
            "track_end_local": str(all_rows["t_local"].iloc[-1]),
            "track_points": int(len(all_rows)),
            "gps": False,
            "overlap_min": round(
                float(merged["hr_track"].notna().sum()) / 60.0, 1),
            "has_track_hr": True,
        }

    d = _haversine(trk["lat"].shift(), trk["lon"].shift(),
                   trk["lat"], trk["lon"]).fillna(0.0)
    dt = trk["t_rel"].diff().fillna(1.0).clip(lower=1.0)
    trk["dist_m"] = d.cumsum()
    trk["speed_mph"] = (d / dt) * 2.23694
    trk["speed_mph"] = trk["speed_mph"].rolling(15, min_periods=1).median()
    trk["grade_pct"] = (trk["ele_m"].diff().rolling(15, min_periods=1).sum()
                        / d.rolling(15, min_periods=1).sum().replace(0, np.nan)) * 100

    merged = hr_1hz.copy()
    cols = ["speed_mph", "grade_pct", "dist_m", "ele_m"]
    if "cad" in trk.columns and trk["cad"].notna().any():
        cols.append("cad")
    for col in cols:
        merged[col] = np.interp(merged["t"], trk["t_rel"],
                                trk[col].ffill().bfill(),
                                left=np.nan, right=np.nan)
    # HR comes from the full (un-split) track, on its own timestamps
    if len(hr_track_series) >= 60:
        merged["hr_track"] = np.interp(
            merged["t"], hr_track_series["t_rel"], hr_track_series["hr_track"],
            left=np.nan, right=np.nan)

    overlap = merged["speed_mph"].notna()
    info = {
        "track_start_local": str(trk["t_local"].iloc[0]),
        "track_end_local": str(trk["t_local"].iloc[-1]),
        "track_points": int(len(trk)),
        "gps": True,
        "distance_mi": round(float(trk["dist_m"].iloc[-1]) / 1609.34, 2),
        # smooth before summing: raw barometric/GPS altitude noise otherwise
        # inflates gain several-fold (FIT is far noisier than a Strava GPX)
        "elev_gain_ft": round(float(
            trk["ele_m"].rolling(15, min_periods=1, center=True).median()
            .diff().clip(lower=0).sum()) * 3.28084, 0),
        "overlap_min": round(float(overlap.sum()) / 60.0, 1),
        "moving_avg_speed_mph": round(
            float(trk.loc[trk["speed_mph"] > 1, "speed_mph"].mean()), 1),
        "has_track_hr": bool(len(hr_track_series) >= 60),
    }
    return merged, info


def reconcile_hr(merged: pd.DataFrame) -> dict | None:
    """Compare the companion track's HR against the H10 RR-derived HR.

    The track is an INDEPENDENT recording of the same heartbeats (same strap,
    different app). Close agreement means the strap was detecting beats
    correctly, so HR-derived metrics are sound even when the DFA α1 quality
    gate fails -- the gate is about millisecond RR resolution, which a 1-Hz
    track never had in the first place. Divergence means real dropouts, and
    then HR is suspect too.
    """
    if "hr_track" not in merged.columns:
        return None
    ok = merged[["hr", "hr_track"]].dropna()
    ok = ok[(ok["hr_track"] > 30) & (ok["hr_track"] < 230)]
    if len(ok) < 120:
        return None

    diff = ok["hr_track"] - ok["hr"]
    corr = float(ok["hr"].corr(ok["hr_track"]))
    mae = float(diff.abs().mean())

    if corr >= 0.8 and mae <= 6:
        verdict = "agree"
    elif corr >= 0.6:
        verdict = "partial"
    else:
        verdict = "disagree"

    return {
        "n_samples": int(len(ok)),
        "corr": round(corr, 3),
        "mean_diff_bpm": round(float(diff.mean()), 2),
        "mae_bpm": round(mae, 2),
        "track_mean_hr": round(float(ok["hr_track"].mean()), 1),
        "rr_mean_hr": round(float(ok["hr"].mean()), 1),
        "track_max_hr": round(float(ok["hr_track"].max()), 0),
        "rr_max_hr": round(float(ok["hr"].max()), 0),
        "verdict": verdict,
    }


# ---------- strength analytics (from polar-strength) ----------------------

def find_peaks_simple(y: np.ndarray, prominence: float, distance: int):
    peaks = []
    for i in range(1, len(y) - 1):
        if y[i] >= y[i - 1] and y[i] > y[i + 1]:
            left = y[max(0, i - distance):i].min() if i else y[i]
            right = y[i + 1:i + 1 + distance].min() if i + 1 < len(y) else y[i]
            if y[i] - max(left, right) >= prominence:
                peaks.append(i)
    out = []
    for p in peaks:
        if out and p - out[-1] < distance:
            if y[p] > y[out[-1]]:
                out[-1] = p
        else:
            out.append(p)
    return out


def _hr_at(hr_1hz: pd.DataFrame, t_sec: float, half: float = 2.0) -> float:
    """Mean HR over t ± `half` s: one 1-Hz sample is one noisy beat."""
    w = hr_1hz[(hr_1hz["t"] >= t_sec - half) & (hr_1hz["t"] <= t_sec + half)]
    return float(w["hr"].mean())


def hrr_table(hr_1hz: pd.DataFrame, anchor_sec: float) -> dict:
    a = hr_1hz[hr_1hz["t"] >= anchor_sec]
    if a.empty:
        return {}
    hr0 = _hr_at(hr_1hz, anchor_sec)
    out = {"anchor_min": round(anchor_sec / 60.0, 2), "anchor_hr": round(hr0, 1)}
    for dt in (60, 120, 180):
        if a["t"].max() >= anchor_sec + dt:
            out[f"hrr{dt}_bpm"] = round(hr0 - _hr_at(hr_1hz, anchor_sec + dt), 1)
    tail = a[a["t"] <= anchor_sec + 300]
    if len(tail):
        out["recovery_floor_bpm"] = round(float(tail["hr"].min()), 1)
    return out


# ---------- marker-driven stations + rest anchor --------------------------
#
# Richard's diamonds name the station and its load ("Shoulder press 85"); a
# bare "-100" is a load step-up on the same machine; "end" closes the lifting
# block; anything after it ("Car ride", "Home") is not a station.

END_NOTE = re.compile(r"^\s*(end|rest|cool\s*-?\s*down|done|finish(ed)?)\b", re.I)
STEP_NOTE = re.compile(r"^\s*-\s*(\d+(?:\.\d+)?)\s*$")
LOAD_NOTE = re.compile(r"^(.*?)\s+(\d+(?:\.\d+)?)\s*$")

HOLD_BAND_BPM = 3.0       # "held" = seconds within this many bpm of the peak
FALL_DROP_BPM = 10.0      # a sustained fall: HR 60 s later is this much lower
FALL_SEARCH_SEC = 300.0   # look this far past the end marker for sitting onset


def parse_marker(note: str) -> dict:
    """One diamond -> {kind: station|step|end, name, load}."""
    text = (note or "").strip()
    if END_NOTE.match(text):
        return {"kind": "end", "name": text, "load": None}
    m = STEP_NOTE.match(text)
    if m:
        return {"kind": "step", "name": None, "load": m.group(1)}
    m = LOAD_NOTE.match(text)
    if m and m.group(1).strip():
        return {"kind": "station", "name": m.group(1).strip(), "load": m.group(2)}
    return {"kind": "station", "name": text, "load": None}


def clock(start_local, t_sec: float) -> str:
    return (pd.Timestamp(start_local) + pd.Timedelta(seconds=float(t_sec))) \
        .strftime("%H:%M:%S")


def marker_list(ev: pd.DataFrame, start_local) -> list[dict]:
    return [{"time": clock(start_local, r.t_rel), "t_min": r.t_rel / 60.0,
             "note": str(r.note or "").strip()}
            for r in ev.sort_values("t_rel").itertuples()]


def end_marker_sec(ev: pd.DataFrame) -> float | None:
    for r in ev.sort_values("t_rel").itertuples():
        if parse_marker(r.note)["kind"] == "end":
            return float(r.t_rel)
    return None


def station_table(hr_1hz: pd.DataFrame, ev: pd.DataFrame, start_local,
                  hr_max: float, hr_rest: float) -> list[dict]:
    """Marker-to-marker station windows up to the end marker (or the last
    marker when there is none). Windows include the rests between sets."""
    marks = ev.sort_values("t_rel")
    t_end = end_marker_sec(ev)
    if t_end is None and len(marks):
        t_end = float(marks["t_rel"].iloc[-1])
    rows, name, num, sub = [], None, 0, 0
    starts = [(float(r.t_rel), parse_marker(r.note)) for r in marks.itertuples()
              if r.t_rel < t_end]
    reserve = max(1.0, hr_max - hr_rest)
    t, hr = hr_1hz["t"].to_numpy(), hr_1hz["hr"].to_numpy()
    for k, (t0, m) in enumerate(starts):
        t1 = starts[k + 1][0] if k + 1 < len(starts) else t_end
        if m["kind"] == "step" and name is not None:
            sub += 1
        else:
            name, num, sub = m["name"], num + 1, 0
        idx = np.flatnonzero((t >= t0) & (t < t1))
        if not idx.size:
            continue
        i_pk = idx[np.argmax(hr[idx])]
        rows.append({
            "n": f"{num}{'abcdefghij'[sub] if sub or _has_steps(starts, k) else ''}",
            "station": name, "load": m["load"],
            "start": clock(start_local, t0), "t0_sec": t0, "t1_sec": t1,
            "dur_min": (t1 - t0) / 60.0,
            "mean_hr": float(hr[idx].mean()),
            "peak_hr": float(hr[i_pk]),
            "peak_time": clock(start_local, t[i_pk]),
            "held_sec": int((hr[idx] >= hr[i_pk] - HOLD_BAND_BPM).sum()),
            "pct_hrr_peak": 100 * (hr[i_pk] - hr_rest) / reserve,
        })
    return rows


def _has_steps(starts: list, k: int) -> bool:
    """True when station k is followed by one or more load steps."""
    return k + 1 < len(starts) and starts[k + 1][1]["kind"] == "step"


def sitting_onset(hr_1hz: pd.DataFrame, after_sec: float) -> float | None:
    """Start of the first sustained HR fall after `after_sec`.

    After an `end` diamond Richard often walks to the car with HR held near
    the working level, so HRR from the diamond itself is walking-confounded.
    On the 5-s smoothed HR, find the first second that sits FALL_DROP_BPM
    above EVERY second 45-75 s later (one sample 60 s on is fooled by a
    transient dip), walk forward to where HR leaves that level by 2 bpm, and
    take the highest point in the 15 s before it: the last beat of work
    before the fall. A fall that starts right at the diamond resolves to
    within a few seconds of it."""
    sm = hr_1hz["hr"].rolling(5, center=True, min_periods=1).mean().to_numpy()
    t = hr_1hz["t"].to_numpy()
    for i in np.flatnonzero((t >= after_sec) & (t <= after_sec + FALL_SEARCH_SEC)):
        later = sm[(t >= t[i] + 45) & (t <= t[i] + 75)]
        if not later.size:
            break
        if sm[i] - later.max() >= FALL_DROP_BPM:
            k = i
            while k + 1 < len(sm) and sm[k + 1] >= sm[i] - 2:
                k += 1
            knee = np.flatnonzero((t >= max(t[i], t[k] - 15)) & (t <= t[k]))
            # ties go to the latest second: a flat plateau ends at the fall
            return float(t[knee[len(knee) - 1 - np.argmax(sm[knee][::-1])]])
    return None


def rest_anchor(hr_1hz: pd.DataFrame, ev: pd.DataFrame,
                peaks: list[int] | None, use_markers: bool) -> tuple:
    """(anchor_sec, method). With markers: the sitting onset after the end
    diamond, else the end diamond, else the last diamond. Without: last peak."""
    if use_markers and len(ev):
        t_end = end_marker_sec(ev)
        if t_end is not None:
            onset = sitting_onset(hr_1hz, t_end)
            if onset is not None:
                return onset, "sitting onset after end marker"
            return t_end, "end marker"
        return float(ev["t_rel"].max()), "last marker (no end marker)"
    if peaks:
        return float(hr_1hz["t"].iloc[peaks[-1]]), "last peak"
    return None, None


def parse_spec(text: str) -> list[dict]:
    phases = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        low = line.lower()
        if low.startswith("warmup"):
            m = re.search(r"(\d+)", line)
            phases.append({"kind": "warmup",
                           "minutes": int(m.group(1)) if m else 5})
            continue
        if low.startswith(("seated recovery", "cooldown", "cool down", "rest")):
            phases.append({"kind": "recovery"})
            continue
        m = re.match(r"^(.*?)\s+([\d\s\-x×/]+)$", line)
        if m:
            loads = [s for s in re.split(r"[-x×/\s]+", m.group(2).strip()) if s]
            phases.append({"kind": "exercise", "name": m.group(1).strip(),
                           "loads": loads})
        else:
            phases.append({"kind": "note", "text": line})
    return phases


# ---------- plots ---------------------------------------------------------

def plot_session(hr_1hz, feat, ev, out_path, title, hr_max, hr_rest,
                 peaks=None, anchor=None, has_track=False):
    from plotnine import (
        aes, annotate, geom_area, geom_hline, geom_line, geom_point, geom_text,
        geom_vline, ggplot, ggtitle, labs, scale_x_continuous,
    )

    hr_df = hr_1hz.assign(
        t_min=hr_1hz["t"] / 60.0,
        hr_smooth=hr_1hz["hr"].rolling(15, min_periods=1).mean(),
    )

    # One shared x-range across all stacked panels so a vertical event/anchor
    # marker lands at the same horizontal position in every panel.
    x_lo = min(float(hr_df["t_min"].min()), float(feat["t_min"].min()))
    x_hi = max(float(hr_df["t_min"].max()), float(feat["t_min"].max()))
    shared_x = scale_x_continuous(limits=(x_lo, x_hi))

    y_top_hr = float(hr_df["hr_smooth"].max())
    hr_span = max(1.0, y_top_hr - float(hr_df["hr_smooth"].min()))

    # --- panel 1: HR + zone bands + peaks + anchor + events ---
    p1 = ggplot(hr_df, aes("t_min", "hr_smooth"))
    p1 = p1 + chart_style.zone_band_layers(ZONES, hr_max, hr_rest)
    p1 = p1 + geom_line(color=chart_style.SERIES["red"], size=1.3)

    if peaks:
        peak_df = pd.DataFrame({
            "t_min": [hr_1hz["t"].iloc[p] / 60 for p in peaks],
            "hr":    [hr_1hz["hr"].iloc[p] for p in peaks],
            "label": [str(i) for i in range(1, len(peaks) + 1)],
        })
        p1 = (p1 + geom_point(peak_df, aes("t_min", "hr"), shape="v",
                              color=chart_style.INK["primary"], size=3)
                 + geom_text(peak_df, aes("t_min", "hr", label="label"),
                             nudge_y=hr_span * 0.03, ha="center",
                             color=chart_style.INK["secondary"], size=7))

    if anchor is not None:
        p1 = (p1 + geom_vline(xintercept=anchor / 60,
                              color=chart_style.SERIES["blue"],
                              linetype="dashed", size=1.2)
                 + annotate("text", x=anchor / 60, y=y_top_hr,
                            label=" rest anchor", angle=90, ha="left", va="top",
                            size=7, color=chart_style.SERIES["blue"]))
    if len(ev):
        # Richard's diamonds carry the station name AND the load ("seated row
        # 100"), so plot the marker itself and print its note beside it — the
        # weight is the single most useful thing on the chart.
        ev_min = ev.assign(t_min=ev["t_rel"] / 60.0,
                           y=y_top_hr,
                           note=ev["note"].fillna("").astype(str).str.strip())
        p1 = p1 + geom_vline(ev_min, aes(xintercept="t_min"),
                             color=chart_style.INK["baseline"],
                             linetype="dotted", size=0.9)
        p1 = (p1 + geom_point(ev_min, aes("t_min", "y"), shape="D",
                              color=chart_style.INK["baseline"], size=2.6)
                 + geom_text(ev_min, aes("t_min", "y", label="note"),
                             angle=90, ha="right", va="bottom",
                             nudge_y=-hr_span * 0.04, size=6,
                             color=chart_style.INK["baseline"]))

    p1 = (p1 + shared_x + labs(x="", y="HR (bpm)") + ggtitle(title)
             + chart_style.theme_house())

    # --- panel 2: DFA alpha1 + thresholds ---
    p2 = (ggplot(feat, aes("t_min", "alpha1"))
          + geom_line(color=chart_style.SERIES["blue"], size=1.3)
          + geom_point(color=chart_style.SERIES["blue"], size=2, shape="s"))
    x_a1_min = float(feat["t_min"].min())
    for alpha, label, key in ((AET_ALPHA, " AeT 1.00", "green"),
                              (VT1_ALPHA, " VT1 0.75", "orange"),
                              (VT2_ALPHA, " VT2 0.50", "red")):
        p2 = (p2 + geom_hline(yintercept=alpha, color=chart_style.SERIES[key],
                              linetype="dashed", size=1.0)
                 + annotate("text", x=x_a1_min, y=alpha, label=label,
                            color=chart_style.SERIES[key], ha="left",
                            va="bottom", size=7))
    x2_label = "Time into recording (min)" if not has_track else ""
    p2 = p2 + shared_x + labs(x=x2_label, y="DFA α1") + chart_style.theme_house()

    panels = [p1, p2]
    total_height = 6.4   # 2 x 3.2

    # --- panels 3+4 (optional): speed + elevation (was a twin axis) ---
    if has_track:
        hr_df = hr_df.assign(ele_ft=hr_df["ele_m"] * 3.28084)
        p3 = (ggplot(hr_df, aes("t_min", "speed_mph"))
              + geom_line(color=chart_style.SERIES["violet"], size=1.1)
              + shared_x + labs(x="", y="Speed (mph)")
              + chart_style.theme_house())
        p4 = (ggplot(hr_df, aes("t_min", "ele_ft"))
              + geom_area(fill=chart_style.INK["baseline"], alpha=0.35)
              + shared_x + labs(x="Time into recording (min)", y="Elev (ft)")
              + chart_style.theme_house())
        panels += [p3, p4]
        total_height += 3.6   # 2 x 1.8

    combined = panels[0]
    for pnl in panels[1:]:
        combined = combined / pnl   # plotnine >= 0.14 native vertical stack
    combined.save(out_path, width=13, height=total_height,
                  dpi=chart_style.DPI, verbose=False)


def plot_hr_alpha1_cloud(feat, out_path, title):
    from plotnine import (
        aes, geom_hline, geom_point, ggplot, ggtitle, labs, scale_color_cmap,
    )

    p = (ggplot(feat, aes("hr_bpm", "alpha1"))
         + geom_point(aes(color="t_min"), size=3)
         + scale_color_cmap(cmap_name="viridis",
                            name="time into\nsession (min)"))
    for alpha, key in ((AET_ALPHA, "green"), (VT1_ALPHA, "orange"),
                       (VT2_ALPHA, "red")):
        p = p + geom_hline(yintercept=alpha, color=chart_style.SERIES[key],
                           linetype="dashed", size=1.0)
    p = (p + labs(x="HR (bpm)", y="DFA α1") + ggtitle(title)
         + chart_style.theme_house())
    p.save(out_path, width=7, height=5.5, dpi=chart_style.DPI, verbose=False)


# ---------- summary writer ------------------------------------------------

def fmt_hms(sec) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}" \
        if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def write_summary(out_dir: Path, s: dict) -> None:
    L = [f"# H10 Session — {s['name'] or 'unnamed'} ({s['mode']})", ""]
    L += [
        f"- **Start (local)**: {s['start_local']}",
        f"- **Duration**: {fmt_hms(s['duration_sec'])} "
        f"({s['duration_min']:.1f} min)",
        f"- **Beats**: {s['n_beats']:,} · artifacts removed "
        f"{s['artifact_pct']:.2f}% · coverage {s['coverage']:.3f}",
        f"- **HR**: mean {s['hr_mean']:.1f} · median {s['hr_median']:.1f} · "
        f"peak {s['hr_max']:.0f} (held 10 s {s['hr_max_held_10s']:.0f}) · "
        f"min {s['hr_min']:.0f} (held 10 s {s['hr_min_held_10s']:.0f}) bpm",
        f"- **Anchors used**: HRmax {s['hrmax_stated']} · "
        f"HRrest {s['hrrest_stated']} (Karvonen)",
        "",
    ]

    if s.get("trimmed"):
        t = s["trimmed"]
        L += [f"> ✂️ Analyzed from **{t['analyzed_from']}**; the first "
              f"{t['skipped_min']} min (recorded from {t['recorded_from']}) "
              "were skipped with `--start`.", ""]
    if s.get("noisy_spans"):
        L += ["> ⚠️ **Strap contact lost** — beats kept arriving, but these "
              "stretches are mostly artifact. HR, zones and α1 inside them "
              "are not real; re-run with `--start` past a bad lead-in.", ">"]
        L += [f"> - {n['from']}–{n['to']} ({n['minutes']} min, "
              f"{n['artifact_pct']}% of beats dropped)" for n in s["noisy_spans"]]
        L.append("")
    if s["coverage"] < 0.9:
        L += ["> ⚠️ **Coverage below 0.90** — likely a Bluetooth dropout "
              "mid-recording. Check for a gap before trusting drift/HRR.", ""]
    if not s.get("rr_quality", {}).get("alpha1_trustworthy", True):
        L += ["> 🚫 **Signal-quality gate failed — DFA α1 is unusable for this "
              "session.** HR-based metrics (zones, drift, HRR) remain valid. "
              "See the α1 section for the reason.", ""]

    L += ["## Time in zones (%HRR, Karvonen)", "",
          "| Zone | Time | % |", "|---|---|---|"]
    tot = max(1, sum(s["zones"].values()))
    for k, v in s["zones"].items():
        L.append(f"| {k} | {fmt_hms(v)} | {100*v/tot:.1f}% |")
    L.append("")

    q = s.get("rr_quality", {})
    if s.get("alpha1_zones"):
        L += ["## DFA α1 distribution (2-min windows)", ""]
        if not q.get("alpha1_trustworthy", True):
            L += ["> 🚫 **DFA α1 IS NOT TRUSTWORTHY FOR THIS RECORDING — "
                  "do not interpret the numbers below as intensity.**", ""]
            for f in q["flags"]:
                L.append(f"> - {expand_flag(f)}")
            L += ["> ",
                  "> Use the Karvonen %HRR zones above instead. To recover α1 "
                  "on future rides: wet the strap electrodes thoroughly, "
                  "tighten it, and re-seat it after the first 10 minutes.", ""]
        L += ["| Band | Windows | % |", "|---|---|---|"]
        at = max(1, sum(s["alpha1_zones"].values()))
        for k, v in s["alpha1_zones"].items():
            L.append(f"| {ALPHA1_ZONE_LABELS.get(k, k)} | {v} | "
                     f"{100*v/at:.1f}% |")
        L += ["", f"Session mean α1 **{s['alpha1_mean']:.3f}** "
              f"(median {s['alpha1_median']:.3f}) · "
              f"median |ΔRR| {q.get('median_abs_drr_ms')} ms · "
              f"RMSSD {q.get('rmssd_ms')} ms raw, "
              f"{q.get('rmssd_clean_ms')} ms after the artifact filter.", ""]

    if s.get("track"):
        t = s["track"]
        if t.get("gps", True):
            L += ["## GPS track", "",
                  f"- Distance **{t['distance_mi']} mi** · elevation gain "
                  f"{t['elev_gain_ft']:.0f} ft",
                  f"- Moving average speed **{t['moving_avg_speed_mph']} mph**",
                  f"- Track {t['track_start_local']} → {t['track_end_local']} "
                  f"({t['overlap_min']} min overlapping the HR recording)", ""]
            if s.get("speed_hr_corr") is not None:
                L.append(f"- Speed↔HR correlation r = {s['speed_hr_corr']:.2f}")
                L.append("")
        else:
            # Indoor track (strength / trainer): HR channel only, no GPS.
            L += ["## Companion track (indoor — HR only, no GPS)", "",
                  f"- {t['track_points']} samples, "
                  f"{t['track_start_local']} → {t['track_end_local']}",
                  f"- {t['overlap_min']} min overlapping the HR recording", ""]

    if s.get("hr_reconciliation"):
        r = s["hr_reconciliation"]
        L += ["## Sensor reconciliation (track HR vs H10 RR)", "",
              "The companion track carries its own HR — an **independent "
              "recording of the same heartbeats** through a different app.",
              "",
              f"- Agreement over {r['n_samples']:,} samples: **r = {r['corr']}**, "
              f"mean difference {r['mean_diff_bpm']:+.2f} bpm, "
              f"MAE {r['mae_bpm']:.2f} bpm",
              f"- Mean HR: track {r['track_mean_hr']} vs RR {r['rr_mean_hr']} · "
              f"peak: track {r['track_max_hr']:.0f} vs RR {r['rr_max_hr']:.0f}",
              f"- **Verdict:** "
              f"{RECON_VERDICT_TEXT.get(r['verdict'], r['verdict'])}", ""]
        if not q.get("alpha1_trustworthy", True):
            L += ["> Read this together with the α1 gate below: agreement here "
                  "localizes the problem to **millisecond RR resolution**, not "
                  "beat detection. A 1-Hz track never carried that resolution, "
                  "so it cannot rescue α1 — but it does confirm the HR-derived "
                  "numbers above are sound.", ""]

    if s.get("drift"):
        d = s["drift"]
        if "note" in d:
            L += [f"## Cardiac drift\n\n{d['note']}\n"]
        else:
            L += ["## Cardiac drift & decoupling "
                  f"(main block from {d['main_start_min']} min)", "",
                  f"- First half HR **{d['first_half_hr']}** → second half "
                  f"**{d['second_half_hr']}** = **{d['drift_bpm']:+.1f} bpm** "
                  f"({d['drift_pct']:+.1f}%)"]
            if d.get("alpha1_drop") is not None:
                L.append(f"- α1 **{d['first_half_alpha1']}** → "
                         f"**{d['second_half_alpha1']}** "
                         f"({d['alpha1_drop']:+.3f})")
            L += ["", "> Drift <5% at constant output = good aerobic "
                  "durability; >10% = glycogen/heat/hydration limited.", ""]

    if s.get("bands"):
        L += ["## Time in target bands", "", "| Band | Longest block | Total |",
              "|---|---|---|"]
        for b in s["bands"]:
            L.append(f"| {b['band']} | {b['longest_min']} min "
                     f"(from {b['start_min']} min) | {b['total_min']} min |")
        L.append("")

    if s.get("markers"):
        L += ["## Markers", "",
              " · ".join(f"`{m['note']}` {m['time']}" for m in s["markers"]), ""]

    if s.get("stations"):
        L += ["## Stations (marker to marker, rests included)", "",
              "| # | Station | Load | Start | Dur (min) | Mean HR | Peak (held) "
              "| %HRR pk |", "|---|---|---|---|---|---|---|---|"]
        for r in s["stations"]:
            L.append(f"| {r['n']} | {r['station']} | {r['load'] or '—'} | "
                     f"{r['start']} | {r['dur_min']:.1f} | {r['mean_hr']:.0f} | "
                     f"{r['peak_hr']:.0f} ({r['held_sec']} s) | "
                     f"{r['pct_hrr_peak']:.0f}% |")
        L += ["", f"Held = seconds in the window within {HOLD_BAND_BPM:.0f} bpm "
              "of its 1-Hz peak.", ""]

    if s.get("sets"):
        rows = s.get("_all_sets") or s["sets"]
        L += ["## Detected sets / peaks", "",
              "| # | Time | Peak HR | %HRR | Mapped |", "|---|---|---|---|---|"]
        for r in rows:
            L.append(f"| {r['n']} | {r['time']} | {r['hr']:.0f} | "
                     f"{r['pct_hrr']:.0f}% | {r['label']} |")
        L.append("")

    if s.get("hrr"):
        h = s["hrr"]
        L += ["## Heart-rate recovery", "",
              f"- Anchor at **{h.get('anchor_time', '')}** "
              f"({h['anchor_min']} min, {h.get('anchor_method', '')}), "
              f"HR **{h['anchor_hr']}**"]
        for dt in (60, 120, 180):
            if f"hrr{dt}_bpm" in h:
                mark = " ← **HRR60**" if dt == 60 else ""
                L.append(f"- HRR{dt}: **{h[f'hrr{dt}_bpm']} bpm**{mark}")
        if "recovery_floor_bpm" in h:
            L.append(f"- Recovery floor (5 min): {h['recovery_floor_bpm']} bpm")
        L += ["", "> HRR60 bands (Cole et al.): <12 high risk · 12–18 average · "
              "19–25 good · ≥25 well-trained. Track the trend, not one value.", ""]

    L += ["## Method", "",
          f"- Sliding {s['window_sec']:.0f}-s windows, {s['stride_sec']:.0f}-s "
          f"stride ({s['n_windows']} windows).",
          "- Artifacts: 300–2000 ms physiological gate + iterative 5-beat "
          f"median filter at {s['pct_thresh']:.0%} tolerance.",
          "- DFA α1 over scales 4–16 beats, linear detrend per box.",
          "- Zones are Karvonen %HRR from the stated HRmax/HRrest.",
          "- RR timestamps are local wall-clock (StrapLog's stored offset); "
          "GPS tracks are UTC and were shifted to match.",
          ""]
    (out_dir / "summary.md").write_text("\n".join(L))


# ---------- CLI -----------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", type=catalog.resolve, default=None,
                   help="StrapLog export: a path, or a bare name in the iCloud "
                        "folder (default: the newest downloaded export there)")
    p.add_argument("--out", type=Path, default=None,
                   help="Output dir (default: <db parent>/h10_analysis)")
    p.add_argument("--recording", type=int, default=None,
                   help="StrapLog recording.id (default: most recent)")
    p.add_argument("--list", action="store_true",
                   help="Print JSON list of recordings and exit")
    p.add_argument("-n", "--limit", type=int, default=8,
                   help="With --list, show only the N most recent recordings, "
                        "newest first (default 8; use 0 for all). One DB "
                        "accumulates dozens of sessions.")
    p.add_argument("--mode", choices=["auto", "endurance", "strength", "generic"],
                   default="auto", help="Activity mode (default: from the recording name)")
    p.add_argument("--track", type=Path, default=None,
                   help="GPX or FIT file to overlay (endurance)")
    p.add_argument("--utc-offset", type=float, default=-7.0,
                   help="Hours to add to track UTC times (default -7 = PDT)")
    p.add_argument("--hrmax", type=float, default=HRMAX_DEFAULT)
    p.add_argument("--hrrest", type=float, default=HRREST_DEFAULT)
    p.add_argument("--start", default=None, metavar="HH:MM[:SS]",
                   help="Analyze from this local clock time on (skip a noisy "
                        "lead-in; see noisy_spans)")
    p.add_argument("--warmup", type=float, default=15.0,
                   help="Warmup minutes excluded from drift stats")
    p.add_argument("--band", action="append", default=None,
                   help="Target HR band 'LO-HI' for time-in-band (repeatable)")
    p.add_argument("--sets", type=Path, default=None,
                   help="Workout spec file (strength mode)")
    p.add_argument("--rest-marker", action="store_true",
                   help="Anchor HRR to the markers instead of the last peak: "
                        "sitting onset after the end/rest/cool-down marker, "
                        "else that marker, else the last marker")
    p.add_argument("--prominence", type=float, default=8.0)
    p.add_argument("--min-gap", type=int, default=30)
    p.add_argument("--window", type=float, default=hrv.WINDOW_SEC_DEFAULT)
    p.add_argument("--stride", type=float, default=hrv.STRIDE_SEC_DEFAULT)
    p.add_argument("--pct-thresh", type=float, default=hrv.PCT_THRESH_DEFAULT)
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
        rows = list_recordings(args.db)
        rows = sorted(rows, key=lambda r: r["start_local"], reverse=True)
        if args.limit > 0 and len(rows) > args.limit:
            rows = rows[:args.limit]
        print(json.dumps(rows, indent=2))
        return 0

    recs = list_recordings(args.db)
    pk = args.recording
    if pk is None:
        if not recs:
            raise SystemExit("No recordings found.")
        pk = sorted(recs, key=lambda r: r["start_local"])[-1]["pk"]

    out_dir = args.out or (args.db.parent / "h10_analysis")
    out_dir.mkdir(parents=True, exist_ok=True)

    rr_df, ev_df, start_local, name = load_recording(args.db, pk)
    recorded_from = pd.Timestamp(start_local)
    skipped_sec = 0.0
    if args.start:
        rr_df, ev_df, start_local, skipped_sec = trim_start(
            rr_df, ev_df, start_local, args.start)

    hr_1hz, keep = beat_series(rr_df, args.pct_thresh)
    if args.mode != "auto":
        mode = args.mode
    else:
        mode = guess_mode(
            name,
            folder=args.db.resolve().parent.name,
            duration_min=float(rr_df["t_rel"].max()) / 60.0,
            avg_hr=float(hr_1hz["hr"].mean()),
        )

    feat = hrv.rolling_features(rr_df, args.window, args.stride, args.pct_thresh)
    feat.to_csv(out_dir / "windowed_features.csv", index=False)

    duration_sec = float(rr_df["t_rel"].max())
    # StrapLog knows when Bluetooth was silent: coverage counts notification gaps.
    coverage = straplog_db.coverage(args.db, pk) or 0

    s = {
        "recording_pk": pk, "name": name, "mode": mode,
        "db": str(args.db),
        "start_local": str(start_local),
        "duration_sec": duration_sec, "duration_min": duration_sec / 60.0,
        "n_beats": len(rr_df), "artifact_pct": float(1.0 - keep.mean()) * 100,
        "coverage": coverage,
        "hr_mean": float(hr_1hz["hr"].mean()),
        "hr_median": float(hr_1hz["hr"].median()),
        "hr_max": float(hr_1hz["hr"].max()),
        # A one-sample spike is an artifact, not a max effort: the highest HR
        # held for 10 s is the number to quote. A big gap to hr_max = spike.
        "hr_max_held_10s": float(hr_1hz["hr"].rolling(10).min().max()),
        "hr_min": float(hr_1hz["hr"].min()),
        "hr_min_held_10s": float(hr_1hz["hr"].rolling(10).max().min()),
        "hrmax_stated": args.hrmax, "hrrest_stated": args.hrrest,
        "rr_quality": rr_quality(rr_df, float(hr_1hz["hr"].mean()), keep),
        "zones": zone_breakdown(hr_1hz, args.hrmax, args.hrrest),
        "zones_units": "seconds",
        "alpha1_zones": alpha1_breakdown(feat),
        # NB: different unit from `zones` above -- these count 2-min windows,
        # not seconds. Easy to misread as time when skimming the JSON.
        "alpha1_zones_units": "windows",
        "alpha1_mean": float(feat["alpha1"].mean()) if len(feat) else float("nan"),
        "alpha1_median": float(feat["alpha1"].median()) if len(feat) else float("nan"),
        "n_windows": len(feat),
        "window_sec": args.window, "stride_sec": args.stride,
        "pct_thresh": args.pct_thresh,
    }
    if skipped_sec:
        s["trimmed"] = {"recorded_from": f"{recorded_from:%H:%M:%S}",
                        "analyzed_from": f"{pd.Timestamp(start_local):%H:%M:%S}",
                        "skipped_min": round(skipped_sec / 60.0, 1)}
    noisy = noisy_spans(rr_df, keep, start_local)
    if noisy:
        s["noisy_spans"] = noisy
    if len(ev_df):
        s["markers"] = marker_list(ev_df, start_local)

    has_track = False
    if args.track and args.track.exists():
        trk = load_track(args.track)
        if trk is not None:
            hr_1hz, tinfo = merge_track(hr_1hz, trk, start_local, args.utc_offset)
            if tinfo:
                s["track"] = tinfo
                has_track = ("speed_mph" in hr_1hz.columns
                             and hr_1hz["speed_mph"].notna().any())
                if has_track:
                    ok = hr_1hz[["hr", "speed_mph"]].dropna()
                    if len(ok) > 60:
                        s["speed_hr_corr"] = float(
                            ok["hr"].corr(ok["speed_mph"]))
                rec = reconcile_hr(hr_1hz)
                if rec:
                    s["hr_reconciliation"] = rec

    peaks = anchor = None
    if mode == "endurance":
        s["drift"] = drift_stats(hr_1hz, feat, args.warmup * 60)
        # Defaults are Richard's CORRECTED bike/walk bands, anchored at the
        # talk-test VT1 of ~145 adopted in `Cycling Ride 2026-07-18`. The old
        # inferred 115-135 / 136-150 pair was retired there: it sat ~10-15 bpm
        # too low, so it under-reported Z2 and split real continuous blocks in
        # half (34.9 min -> 19.5 min on the 2026-07-26 ride). Override with
        # --band for anyone else or for a different modality.
        bands = args.band or ["120-145", "145-158"]
        s["bands"] = []
        for b in bands:
            lo, hi = (float(x) for x in b.split("-"))
            s["bands"].append(longest_in_band(hr_1hz, lo, hi))

    elif mode == "strength":
        y = hr_1hz["hr"].rolling(5, min_periods=1).mean().to_numpy()
        peaks = find_peaks_simple(y, args.prominence, args.min_gap)
        spec = parse_spec(args.sets.read_text()) if args.sets and args.sets.exists() else []
        labels = []
        for ph in spec:
            if ph["kind"] == "exercise":
                for ld in ph["loads"]:
                    labels.append(f"{ph['name']} {ld}")
        reserve = max(1.0, args.hrmax - args.hrrest)
        stations = station_table(hr_1hz, ev_df, start_local,
                                 args.hrmax, args.hrrest) if len(ev_df) else []
        if stations:
            s["stations"] = stations

        def label(i: int, p: int) -> str:
            # Diamonds beat the spec: they say what was happening, not what
            # was planned, and a greedy first-N mapping drifts after one miss.
            if stations:
                tp = float(hr_1hz["t"].iloc[p])
                for st in stations:
                    if st["t0_sec"] <= tp < st["t1_sec"]:
                        return f"{st['station']} {st['load'] or ''}".strip()
                return "_outside stations_"
            return labels[i] if i < len(labels) else "_extra / transition_"

        # full precision: summary.md formats these itself, and rounding here
        # first would double-round (74.53 -> 74.5 -> "74" instead of "75")
        all_sets = [{
            "n": i + 1,
            "time": fmt_hms(hr_1hz["t"].iloc[p]),
            "hr": float(hr_1hz["hr"].iloc[p]),
            "pct_hrr": 100 * (hr_1hz["hr"].iloc[p] - args.hrrest) / reserve,
            "label": label(i, p),
        } for i, p in enumerate(peaks)]
        # summary.md always carries the full table; stdout is capped so a
        # low-prominence circuit (27 "sets") cannot flood the context.
        s["_all_sets"] = all_sets
        s["sets"] = [dict(r, hr=round(r["hr"], 1),
                          pct_hrr=round(r["pct_hrr"], 1))
                     for r in all_sets[:SETS_STDOUT_CAP]]
        if len(all_sets) > SETS_STDOUT_CAP:
            s["sets_truncated"] = len(all_sets) - SETS_STDOUT_CAP

        anchor, method = rest_anchor(hr_1hz, ev_df, peaks, args.rest_marker)
        if anchor is not None:
            s["hrr"] = dict(hrr_table(hr_1hz, anchor),
                            anchor_time=clock(start_local, anchor),
                            anchor_method=method)

    title = (f"{name or 'H10 session'} — {start_local:%Y-%m-%d %H:%M} local "
             f"({mode})")
    plot_session(hr_1hz, feat, ev_df, out_dir / "session.png", title,
                 args.hrmax, args.hrrest, peaks=peaks, anchor=anchor,
                 has_track=has_track)
    plot_hr_alpha1_cloud(feat, out_dir / "hr_alpha1.png",
                         f"HR vs DFA α1 — {name or 'session'}")
    hr_1hz.to_csv(out_dir / "hr_1hz.csv", index=False)
    write_summary(out_dir, s)

    full = dict(s, sets=s.pop("_all_sets", s.get("sets")))
    full.pop("sets_truncated", None)
    (out_dir / "summary.json").write_text(
        json.dumps(round_floats(full), indent=2, default=str))
    s["out_dir"] = str(out_dir)
    s["files"] = sorted(p.name for p in out_dir.iterdir())
    if s.get("stations"):
        # Diamonds give the station table; the peak list repeats it with
        # station labels. Full list in summary.json / summary.md.
        s["n_sets"] = len(full.get("sets") or [])
        s.pop("sets", None)
        s.pop("sets_truncated", None)
    print(json.dumps(round_floats(s), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
