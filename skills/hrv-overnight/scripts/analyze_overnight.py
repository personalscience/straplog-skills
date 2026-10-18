#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pandas", "straplog[plot]>=0.2"]
# ///
"""
Overnight HRV analyzer for StrapLog SQLite exports, with optional Apple
Health / Apple Watch staging reconciliation.

Replaces the per-night one-off scripts that used to be hand-written into
~/Downloads/hrv<YYMMDD>/ for every sleep strip.

Pipeline
--------
  1. H10 strip      -> coverage, dropout gaps, whole-strip time-domain HRV,
                       30-min block trajectory, 2-min rolling extremes
                       (peak vagal LnRMSSD, HR floor)
  2. Apple Health   -> sleep architecture (already computed upstream), cycles,
     (optional)        epochs, provenance flag
  3. Reconciliation -> per Watch-staged awake window, the H10 HR bump that
                       independently corroborates it as genuine wake

Timestamps
----------
StrapLog stores UTC plus tz_offset_s; `straplog.db` converts it to local
wall-clock (Apple-epoch seconds), so no further tz math happens here. Apple
Health JSON carries explicit ISO offsets and is parsed as-is; both end up on
the same local clock.

Usage
-----
  analyze_overnight.py --list                    # newest exports in iCloud Drive
  analyze_overnight.py --db 260726-2245-sleep.sqlite \
      --health ~/Downloads/hrv260726/applehealth_*.json \
      --out ~/Downloads/hrv260726/overnight
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import straplog.db as straplog_db
from straplog import catalog, hrv
from straplog import plot as chart_style


REF = datetime(2001, 1, 1)

GAP_SEC = 15.0         # a gap longer than this is a dropout, not a long beat
BLOCK_SEC = 1800.0     # 30-min trajectory blocks
ROLL_WIN_SEC = 120.0   # 2-min rolling window for extremes
MIN_BEATS_ROLL = 40

# A recording is a plausible overnight strip if it is at least this long.
MIN_OVERNIGHT_MIN = 180.0
SLEEP_KEYWORDS = ("sleep", "night", "overnight", "nap")


# ---------- helpers -------------------------------------------------------

def ts_to_local(ts: float) -> datetime:
    """Apple-epoch seconds -> LOCAL wall-clock. No tz math, by design."""
    return REF + timedelta(seconds=float(ts))


def hm(ts: float) -> str:
    return ts_to_local(ts).strftime("%H:%M")


def hms(ts: float) -> str:
    return ts_to_local(ts).strftime("%H:%M:%S")


def td_stats(vals: list[float]) -> dict:
    """Time-domain HRV over already-gated RR intervals (ms), under this
    skill's established key names. Empty below 2 beats."""
    if len(vals) < 2:
        return {}
    t = hrv.time_domain(vals)
    return {
        "n": t["n"],
        "mean_hr": t["hr_mean"],
        "min_hr": t["hr_min"],
        "max_hr": t["hr_max"],
        "rmssd": t["rmssd_ms"],
        "ln_rmssd": t["ln_rmssd"],
        "pnn50": t["pnn50_pct"],
        "sdnn": t["sdnn_ms"],
    }


def gated(v: float) -> bool:
    """Physiological gate only; overnight strips get no median filter."""
    return hrv.RR_MIN_MS < v < hrv.RR_MAX_MS


def gate(rows: list[tuple[float, float]]) -> list[float]:
    return [v for _, v in rows if gated(v)]


# ---------- load ----------------------------------------------------------

def list_recordings(db_path: Path) -> list[dict]:
    """StrapLog recordings, each flagged if it looks overnight."""
    rows = straplog_db.list_recordings(db_path)
    for r in rows:
        r["looks_overnight"] = looks_overnight(
            r["name"], datetime.fromisoformat(r["start_local"]), r["duration_min"])
    return rows


def looks_overnight(name: str, start: datetime, duration_min: float) -> bool:
    """A strip is overnight if the name says so, or if it is long AND starts
    in the evening / small hours. Duration alone is not enough -- a 4-hour
    bike ride is long too, which is exactly why the shape test needs the
    clock-hour condition."""
    if any(k in (name or "").lower() for k in SLEEP_KEYWORDS):
        return True
    if duration_min < MIN_OVERNIGHT_MIN:
        return False
    return start.hour >= 20 or start.hour <= 3


def load_rr(db_path: Path, pk: int) -> list[tuple[float, float]]:
    return list(straplog_db.load_rr(db_path, pk).itertuples(index=False, name=None))


# ---------- analysis ------------------------------------------------------

def coverage_gaps(rows) -> tuple[list[dict], float]:
    gaps, captured = [], 0.0
    for i in range(1, len(rows)):
        dt = rows[i][0] - rows[i - 1][0]
        if dt > GAP_SEC:
            gaps.append({
                "from": hm(rows[i - 1][0]), "to": hm(rows[i][0]),
                "minutes": round(dt / 60.0, 1),
            })
        else:
            captured += dt
    span = rows[-1][0] - rows[0][0]
    return gaps, (100.0 * captured / span if span > 0 else 0.0)


def block_trajectory(rows, block_sec=BLOCK_SEC) -> list[dict]:
    t0 = rows[0][0]
    buckets: dict[int, list] = {}
    for ts, v in rows:
        buckets.setdefault(int((ts - t0) // block_sec), []).append((ts, v))
    out = []
    for b in sorted(buckets):
        s = td_stats(gate(buckets[b]))
        if not s:
            continue
        out.append({
            "clock": hm(buckets[b][0][0]),
            # Full local datetime for plotting: "clock" alone drops the date,
            # so post-midnight blocks sort before the evening ones and the
            # line wraps across the empty daytime (fixed 2026-10-12).
            "start_local": ts_to_local(buckets[b][0][0]).isoformat(),
            "mean_hr": round(s["mean_hr"], 1),
            "rmssd": round(s["rmssd"], 1),
            "ln_rmssd": round(s["ln_rmssd"], 3),
            "sdnn": round(s["sdnn"], 1),
            "n": s["n"],
        })
    return out


def rolling_extremes(rows, gaps_raw) -> dict:
    """Peak vagal tone and HR floor over 2-min windows, computed inside
    continuous segments only so a dropout can't fabricate an extreme."""
    starts = [rows[0][0]] + [g[1] for g in gaps_raw]
    ends = [g[0] for g in gaps_raw] + [rows[-1][0]]
    best_ln, low_hr = (-9.0, None), (999.0, None)
    for s, e in zip(starts, ends):
        seg = [(ts, v) for ts, v in rows if s <= ts <= e]
        if len(seg) < 2 or (seg[-1][0] - seg[0][0]) < 300:
            continue
        i = 0
        while i < len(seg):
            a = seg[i][0]
            chunk, j = [], i
            while j < len(seg) and seg[j][0] - a < ROLL_WIN_SEC:
                chunk.append(seg[j])
                j += 1
            cs = td_stats(gate(chunk))
            if cs.get("n", 0) >= MIN_BEATS_ROLL:
                if cs["ln_rmssd"] > best_ln[0]:
                    best_ln = (cs["ln_rmssd"], a)
                if cs["mean_hr"] < low_hr[0]:
                    low_hr = (cs["mean_hr"], a)
            i = max(j, i + 1)
    return {
        "peak_vagal_ln_rmssd": round(best_ln[0], 3) if best_ln[1] else None,
        "peak_vagal_at": hm(best_ln[1]) if best_ln[1] else None,
        "hr_floor_bpm": round(low_hr[0], 1) if low_hr[1] else None,
        "hr_floor_at": hm(low_hr[1]) if low_hr[1] else None,
    }


# ---------- Apple Health --------------------------------------------------

def _parse_local(s: str) -> datetime:
    """ISO with offset -> naive LOCAL datetime (drop the offset, keep the
    wall-clock face value, so it lines up with the H10's local clock)."""
    return datetime.fromisoformat(s).replace(tzinfo=None)


def _adapt_flat_health(d: dict) -> dict:
    """Normalize the newer flat morning-pull layout onto the original nested
    one. Both self-declare schema_version 1.0, but the newer export hoists
    sleep_window / sleep_architecture / waso / sleep_epochs_staged / autonomic
    to the top level instead of nesting them under `sleep` + `cardiovascular`.
    Detected by the presence of a top-level `sleep_window`.

    Two deliberate choices here:
      * In-bed is taken from the STAGED span, not the envelope records. These
        exports carry an `envelope_note` saying the In Bed/Asleep envelope is
        internally inconsistent with the staged epochs and is excluded from all
        TST math; honoring that keeps efficiency comparable to prior nights.
      * Watch SDNN is recomputed over in_sleep_window samples only. The
        export's `hrv_sdnn_overnight_mean_ms` averages pre-sleep samples in
        too, which would not be comparable with earlier nights' in_sleep_mean.
    """
    win = d.get("sleep_window", {}) or {}
    arch_in = d.get("sleep_architecture", {}) or {}
    waso = d.get("waso", {}) or {}
    auto = d.get("autonomic", {}) or {}
    prov = d.get("provenance", {}) or {}

    epochs = [{
        "stage": e.get("stage"),
        "start_local": e.get("start_local"),
        "end_local": e.get("end_local"),
        "duration_min": e.get("min"),
    } for e in d.get("sleep_epochs_staged", []) or []]

    arch = {
        # TST lives on sleep_window in this layout, not sleep_architecture.
        "tst_min": arch_in.get("tst_min", win.get("tst_min")) or win.get("tst_min"),
        "core_min": arch_in.get("core_min"),
        "core_pct_tst": arch_in.get("core_pct_tst"),
        "n3_deep_min": arch_in.get("n3_deep_min"),
        "n3_pct_tst": arch_in.get("n3_pct_tst"),
        "rem_min": arch_in.get("rem_min"),
        "rem_pct_tst": arch_in.get("rem_pct_tst"),
        "waso_min": waso.get("waso_total_min", arch_in.get("awake_in_bed_min")),
        "awakenings_n": waso.get("episode_count"),
        "longest_waso_min": waso.get("longest_episode_min"),
        # Only present once the envelope records commit (a later re-pull);
        # the staged-span figure is the one comparable across the series.
        "efficiency_pct_vs_in_bed": win.get("sleep_efficiency_pct_vs_in_bed"),
        "efficiency_pct_vs_staged_span": win.get("sleep_efficiency_pct_vs_staged_span"),
    }

    in_sleep = [s for s in auto.get("hrv_samples", []) or []
                if s.get("in_sleep_window")]
    sdnn_vals = [float(s["sdnn_ms"]) for s in in_sleep if s.get("sdnn_ms") is not None]
    watch_sdnn = {"samples": auto.get("hrv_samples", [])}
    if sdnn_vals:
        watch_sdnn.update({
            "in_sleep_mean_ms": round(sum(sdnn_vals) / len(sdnn_vals), 2),
            "in_sleep_min_ms": round(min(sdnn_vals), 2),
            "in_sleep_max_ms": round(max(sdnn_vals), 2),
            "in_sleep_n": len(sdnn_vals),
        })

    # The morning pull often has no same-day RHR at all (the 14-day series
    # stops at the prior day); a later re-pull adds `rhr_today_bpm`. Either way
    # the standing vault rule governs: Apple's RHR window stays open until the
    # day closes, so the value is reportable but NOT interpretable until the
    # >=18h / complete_day gate below clears.
    rhr_series = auto.get("rhr_series_14d", []) or []
    rhr_today = auto.get("rhr_today_bpm")

    return {
        "sleep": {
            "session_date_wake": d.get("date_label"),
            "in_bed_local": win.get("staged_span_start_local"),
            "sleep_onset_local": win.get("staged_span_start_local"),
            "final_wake_local": win.get("staged_span_end_local"),
            "sleep_onset_latency_min": win.get("sleep_onset_latency_min"),
            "time_in_bed_min": win.get("staged_span_min"),
            "staged_span_min": win.get("staged_span_min"),
            "wake_stage_at_arousal": arch_in.get("wake_stage_at_final_arousal"),
            "architecture": arch,
            "epochs": epochs,
            "cycles": [],
        },
        "cardiovascular": {
            "resting_heart_rate_bpm": rhr_today,
            "resting_heart_rate_provisional": True,
            "resting_heart_rate_prior_day_bpm": auto.get("rhr_yesterday_bpm"),
            "rhr_baseline_14d_mean_bpm": auto.get("rhr_baseline_14d_mean_bpm"),
            "rhr_7day_context": {r["date"]: r["bpm"] for r in rhr_series[-7:]
                                 if "date" in r and "bpm" in r},
            "overnight_hr_nadir_bpm": auto.get("hr_sleep_window_min_bpm"),
            "hrv_sdnn": watch_sdnn,
            "hr_hourly_local": auto.get("hr_hourly_local", []),
        },
        "export_metadata": {
            "generated_local": d.get("captured_at_local"),
            "provenance_flag": prov.get("provisional_reason")
                               or prov.get("repull_delta")
                               or prov.get("source"),
            # 🛑 Do NOT map provenance.provisional -> complete_day. In this
            # layout `provisional` means "sleep STAGING may not have committed
            # yet"; a re-pull clears it at ~T+2h. Day-completeness is a
            # different question, and Apple's RHR / steps / active-kcal windows
            # stay open until the day closes regardless. Conflating them would
            # mark a 14:08 same-day RHR as trustworthy -- exactly the error the
            # standing rule exists to prevent (Jul 22: 59 -> 64 -> 55 final).
            # These exports carry no day-complete flag, so leave it False and
            # let the >=18h test in load_health() be the only gate.
            "complete_day": False,
            "staging_committed": not bool(prov.get("provisional", True)),
            "source": prov.get("source"),
            "known_gaps": [n for n in (
                (win.get("envelope_records", {}) or {}).get("envelope_note"),
                arch_in.get("first_cycle_n3_note"),
                arch_in.get("wake_stage_note"),
                win.get("onset_latency_note"),
            ) if n],
        },
        "_adapted_from": "flat_morning_pull",
        "_anomaly_flags": d.get("anomaly_flags"),
        "_activity_yesterday": d.get("activity_yesterday"),
        "_physiology": d.get("physiology"),
        "_body": d.get("body"),
    }


def load_health(path: Path) -> dict:
    d = json.loads(path.read_text())
    if "sleep_window" in d and "sleep" not in d:
        d = _adapt_flat_health(d)
    sleep = d.get("sleep", {}) or {}
    arch = sleep.get("architecture", {}) or {}
    meta = d.get("export_metadata", {}) or {}
    cardio = d.get("cardiovascular", {}) or {}

    awake = []
    for e in sleep.get("epochs", []) or []:
        if str(e.get("stage", "")).lower().startswith("awake"):
            awake.append({
                "start": _parse_local(e["start_local"]),
                "end": _parse_local(e["end_local"]),
                "duration_min": round(float(e.get("duration_min", 0)), 1),
            })

    out = {
        "provenance_flag": meta.get("provenance_flag"),
        "generated_local": meta.get("generated_local"),
        "session_date_wake": sleep.get("session_date_wake"),
        "in_bed_local": sleep.get("in_bed_local"),
        "sleep_onset_local": sleep.get("sleep_onset_local"),
        "final_wake_local": sleep.get("final_wake_local"),
        "sleep_onset_latency_min": sleep.get("sleep_onset_latency_min"),
        "time_in_bed_min": sleep.get("time_in_bed_min"),
        "architecture": arch,
        "cycles": sleep.get("cycles", []),
        "awake_windows": awake,
        "resting_heart_rate_bpm": cardio.get("resting_heart_rate_bpm"),
        "resting_heart_rate_prior_day_bpm": cardio.get("resting_heart_rate_prior_day_bpm"),
        "rhr_baseline_14d_mean_bpm": cardio.get("rhr_baseline_14d_mean_bpm"),
        "overnight_hr_nadir_bpm": cardio.get("overnight_hr_nadir_bpm"),
        "wake_stage_at_arousal": sleep.get("wake_stage_at_arousal"),
        "watch_sdnn": cardio.get("hrv_sdnn", {}),
        "known_gaps": meta.get("known_gaps", []),
        "adapted_from": d.get("_adapted_from"),
        "anomaly_flags": d.get("_anomaly_flags"),
    }

    # Provisional-pull check. The standing vault rule: a same-day Apple pull
    # keeps drifting (RHR especially) until the day closes. Staging is stable
    # past ~T+2h, RHR is not.
    try:
        gen = _parse_local(meta["generated_local"])
        wake = _parse_local(sleep["final_wake_local"])
        hours = (gen - wake).total_seconds() / 3600.0
        out["hours_after_wake"] = round(hours, 2)
        # An export that explicitly declares staging committed (e.g. a re-pull
        # that verified the epoch series is unchanged) beats the time heuristic.
        committed = meta.get("staging_committed")
        out["staging_stable"] = bool(committed) if committed is not None \
            else hours >= 2.0
        # RHR is gated separately and strictly: committed staging says nothing
        # about whether Apple's daily RHR window has closed.
        out["rhr_trustworthy"] = bool(meta.get("complete_day")) or hours >= 18.0
    except Exception:
        out["hours_after_wake"] = None
        out["staging_stable"] = None
        out["rhr_trustworthy"] = None
    return out


def reconcile_awake(rows, awake_windows, strip_start: datetime) -> list[dict]:
    """For each Watch-staged awake window, the H10 HR over the same clock
    minutes. A bump above sleeping baseline is independent evidence the WASO
    is genuine wake rather than staging noise."""
    out = []
    for w in awake_windows:
        a = (w["start"] - REF).total_seconds()
        b = (w["end"] - REF).total_seconds()
        vals = [v for ts, v in rows if a <= ts <= b and gated(v)]
        if len(vals) < 5:
            out.append({
                "window": f"{w['start']:%H:%M:%S}-{w['end']:%H:%M:%S}",
                "duration_min": w["duration_min"],
                "h10_mean_hr": None, "h10_min_hr": None, "h10_max_hr": None,
                "note": "no H10 coverage",
            })
            continue
        # Mean HR is 60000 / mean(RR), NOT mean(60000 / RR). The two differ by
        # Jensen's inequality (~1.5-3 bpm high on a wake-window bump, where RR
        # is most variable), and every prior filed night used the former --
        # so the latter would silently break comparability with the series.
        out.append({
            "window": f"{w['start']:%H:%M:%S}-{w['end']:%H:%M:%S}",
            "duration_min": w["duration_min"],
            "h10_mean_hr": round(60000.0 / (sum(vals) / len(vals)), 1),
            "h10_min_hr": round(60000.0 / max(vals)),
            "h10_max_hr": round(60000.0 / min(vals)),
            "n_beats": len(vals),
        })
    return out


# ---------- plots ---------------------------------------------------------

def plot_night(blocks, out_path: Path, title: str):
    from plotnine import (ggplot, aes, geom_line, geom_point, labs,
                          scale_x_datetime)
    if not blocks:
        return
    df = pd.DataFrame(blocks)
    df["t"] = pd.to_datetime(df["start_local"])
    long = pd.concat([
        df.assign(metric="mean HR (bpm)", value=df["mean_hr"]),
        df.assign(metric="LnRMSSD x10", value=df["ln_rmssd"] * 10),
    ])
    p = (ggplot(long, aes("t", "value", color="metric"))
         + geom_line(size=0.9) + geom_point(size=1.4)
         + scale_x_datetime(date_labels="%H:%M")
         + labs(title=title, x="clock (local)", y="value", color="")
         + chart_style.theme_house())
    p.save(out_path, dpi=chart_style.DPI, width=9, height=4.5, verbose=False)


def plot_tachogram(rows, out_path: Path, title: str):
    from plotnine import ggplot, aes, geom_point, labs, scale_x_datetime
    vals = [(ts, v) for ts, v in rows if gated(v)]
    if not vals:
        return
    step = max(1, len(vals) // 6000)          # keep the PNG light
    vals = vals[::step]
    df = pd.DataFrame({
        "t": [ts_to_local(ts) for ts, _ in vals],
        "rr": [v for _, v in vals],
    })
    p = (ggplot(df, aes("t", "rr"))
         + geom_point(size=0.25, alpha=0.35, color=chart_style.SERIES["blue"])
         + scale_x_datetime(date_labels="%H:%M")
         + labs(title=title, x="clock (local)", y="RR (ms)")
         + chart_style.theme_house())
    p.save(out_path, dpi=chart_style.DPI, width=9, height=4, verbose=False)


# ---------- summary -------------------------------------------------------

def write_summary(s: dict, path: Path):
    L = [f"# Overnight HRV — {s.get('date_label', 'unnamed')}", ""]
    L += [
        f"- **Recording**: pk {s['recording_pk']} "
        f"{('“' + s['name'] + '”') if s['name'] else '(unnamed)'}",
        f"- **Span**: {s['start_local']} → {s['end_local']} "
        f"({s['duration_h']:.2f} h)",
        f"- **RR intervals**: {s['n_beats']:,} · coverage "
        f"**{s['coverage_pct']:.1f}%** · gaps >15 s: {len(s['gaps'])}",
        "",
    ]
    q = s["whole_strip"]
    L += ["## Whole-strip autonomics", "",
          "| Metric | Value |", "|---|---|",
          f"| Mean HR | **{q['mean_hr']:.1f} bpm** |",
          f"| HR floor (2-min) | **{s['extremes']['hr_floor_bpm']} bpm** "
          f"@ {s['extremes']['hr_floor_at']} |",
          f"| RMSSD / LnRMSSD | {q['rmssd']:.1f} ms / {q['ln_rmssd']:.3f} |",
          f"| SDNN | {q['sdnn']:.1f} ms |",
          f"| pNN50 | {q['pnn50']:.1f}% |",
          f"| Peak vagal LnRMSSD (2-min) | **{s['extremes']['peak_vagal_ln_rmssd']}** "
          f"@ {s['extremes']['peak_vagal_at']} |",
          ""]

    if s.get("sleep"):
        sl = s["sleep"]
        a = sl.get("architecture", {})
        L += ["## Sleep architecture (Apple Watch staging)", "",
              "| Metric | Value |", "|---|---|",
              f"| In bed | {sl.get('in_bed_local')} |",
              f"| Onset / latency | {sl.get('sleep_onset_local')} / "
              f"{sl.get('sleep_onset_latency_min')} min |",
              f"| **TST** | **{a.get('tst_min')} min "
              f"({(a.get('tst_min') or 0)/60:.2f} h)** |",
              f"| Efficiency | {a.get('efficiency_pct_vs_in_bed')}% |",
              f"| Deep (N3) | {a.get('n3_deep_min')} min "
              f"({a.get('n3_pct_tst')}%) |",
              f"| REM | {a.get('rem_min')} min ({a.get('rem_pct_tst')}%) |",
              f"| Core | {a.get('core_min')} min ({a.get('core_pct_tst')}%) |",
              f"| WASO | {a.get('waso_min')} min, "
              f"{a.get('awakenings_n')} awakenings |",
              f"| Longest wake | {a.get('longest_waso_min')} min |",
              ""]
        if sl.get("staging_stable") is False:
            L += ["> ⚠️ **PROVISIONAL** — the Apple pull ran "
                  f"{sl.get('hours_after_wake')} h after wake. Staging is "
                  "usually stable past T+2 h; re-pull to confirm.", ""]
        if sl.get("rhr_trustworthy") is False:
            L += ["> 🛑 **Do not use the same-day resting HR** "
                  f"({sl.get('resting_heart_rate_bpm')}) as a recovery signal "
                  "— Apple's RHR window stays open until end of day and drifts "
                  "substantially. Re-pull after midnight.", ""]

    if s.get("awake_reconciliation"):
        L += ["## H10 ↔ Watch awake-window corroboration", "",
              "| Watch awake window | Dur (min) | H10 mean HR | H10 range |",
              "|---|---|---|---|"]
        for w in s["awake_reconciliation"]:
            if w.get("h10_mean_hr") is None:
                L.append(f"| {w['window']} | {w['duration_min']} | — | "
                         f"{w.get('note', 'n/a')} |")
            else:
                L.append(f"| {w['window']} | {w['duration_min']} | "
                         f"{w['h10_mean_hr']} | {w['h10_min_hr']}–"
                         f"{w['h10_max_hr']} |")
        L += ["", "> An HR bump above sleeping baseline in each window is "
                  "independent evidence the WASO is genuine wake, not staging "
                  "noise.", ""]

    if s["gaps"]:
        L += ["## ⚠️ Dropout gaps (>15 s)", "",
              "| From | To | Minutes |", "|---|---|---|"]
        for g in s["gaps"]:
            L.append(f"| {g['from']} | {g['to']} | {g['minutes']} |")
        L += ["", "> A Bluetooth dropout: the H10 sent no RR for the gap. "
                  "Metrics spanning a gap are not trustworthy.", ""]

    L += ["## 30-min block trajectory", "",
          "| Clock | Mean HR | RMSSD | LnRMSSD | SDNN | n |", "|---|---|---|---|---|---|"]
    for b in s["blocks"]:
        L.append(f"| {b['clock']} | {b['mean_hr']} | {b['rmssd']} | "
                 f"{b['ln_rmssd']} | {b['sdnn']} | {b['n']} |")
    L += ["", "## Method", "",
          "- RR gated to 300–2000 ms; RMSSD/SDNN (sample, ddof=1)/pNN50 on "
          "gated intervals; mean HR is the mean of beat HRs.",
          "- Coverage = captured time ÷ wall-clock span, gaps >15 s excluded.",
          "- 2-min rolling extremes computed inside continuous segments only, "
          "so a dropout cannot fabricate an extreme.",
          "- RR timestamps are local wall-clock (StrapLog's stored offset).",
          ""]
    path.write_text("\n".join(L))


# ---------- main ----------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=catalog.resolve, default=None,
                   help="StrapLog export: a path, or a bare name in the iCloud "
                        "folder (default: the newest downloaded export there)")
    p.add_argument("--list", action="store_true",
                   help="List recordings and exit")
    p.add_argument("-n", "--limit", type=int, default=8,
                   help="With --list, show the N most recent (default 8; "
                        "0 = all)")
    p.add_argument("--recording", type=int, default=None,
                   help="Recording pk. Default: the most recent strip that "
                        "looks like an overnight.")
    p.add_argument("--health", type=Path, default=None,
                   help="Apple Health JSON export for the same night")
    p.add_argument("--out", type=Path, default=None,
                   help="Output dir (default <db parent>/overnight)")
    p.add_argument("--no-plots", action="store_true")
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

    recs = list_recordings(args.db)
    if args.list:
        rows = sorted(recs, key=lambda r: r["start_local"], reverse=True)
        if args.limit > 0:
            rows = rows[:args.limit]
        print(json.dumps(rows, indent=2))
        return 0

    pk = args.recording
    if pk is None:
        cands = [r for r in recs if r["looks_overnight"]]
        if not cands:
            raise SystemExit(
                "No recording looks like an overnight strip. Pass --recording "
                "explicitly, or run --list to see what's in this export.")
        pk = sorted(cands, key=lambda r: r["start_local"])[-1]["pk"]

    rec = next((r for r in recs if r["pk"] == pk), None)
    if rec is None:
        raise SystemExit(f"Recording pk {pk} not found.")

    rows = load_rr(args.db, pk)
    if len(rows) < 100:
        raise SystemExit(f"Recording pk {pk} has only {len(rows)} RR intervals.")

    out_dir = args.out or (args.db.parent / "overnight")
    out_dir.mkdir(parents=True, exist_ok=True)

    gaps, cov = coverage_gaps(rows)
    gaps_raw = [(rows[i - 1][0], rows[i][0])
                for i in range(1, len(rows))
                if rows[i][0] - rows[i - 1][0] > GAP_SEC]
    whole = td_stats(gate(rows))
    start_dt = ts_to_local(rows[0][0])

    s = {
        "recording_pk": pk,
        "name": rec["name"],
        "date_label": start_dt.strftime("%Y-%m-%d"),
        "start_local": hms(rows[0][0]),
        "end_local": hms(rows[-1][0]),
        "duration_h": (rows[-1][0] - rows[0][0]) / 3600.0,
        "n_beats": len(rows),
        "coverage_pct": cov,
        "gaps": gaps,
        "whole_strip": {k: round(v, 3) for k, v in whole.items()},
        "extremes": rolling_extremes(rows, gaps_raw),
        "blocks": block_trajectory(rows),
    }

    if args.health:
        hp = args.health
        if not hp.exists():   # allow a glob
            matches = sorted(hp.parent.glob(hp.name))
            hp = matches[0] if matches else None
        if hp and hp.exists():
            s["sleep"] = load_health(hp)
            s["health_file"] = str(hp)
            s["awake_reconciliation"] = reconcile_awake(
                rows, s["sleep"]["awake_windows"], start_dt)
            # awake_windows carry datetimes; drop them from the JSON payload
            s["sleep"] = {k: v for k, v in s["sleep"].items()
                          if k != "awake_windows"}
        else:
            s["health_file"] = None
            s["health_warning"] = f"Apple Health file not found: {args.health}"

    if not args.no_plots:
        label = f"Overnight {s['date_label']}"
        try:
            plot_night(s["blocks"], out_dir / "night_trajectory.png",
                       f"{label} — HR & LnRMSSD by 30-min block")
            plot_tachogram(rows, out_dir / "tachogram.png",
                           f"{label} — RR tachogram")
        except Exception as e:                       # plots are not the point
            s["plot_warning"] = f"{type(e).__name__}: {e}"

    write_summary(s, out_dir / "summary.md")
    (out_dir / "summary.json").write_text(json.dumps(s, indent=2, default=str))
    s["out_dir"] = str(out_dir)
    s["files"] = sorted(p.name for p in out_dir.iterdir())

    # Keep stdout small: the block table and the full JSON live on disk.
    slim = {k: v for k, v in s.items() if k != "blocks"}
    slim["n_blocks"] = len(s["blocks"])
    print(json.dumps(slim, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
