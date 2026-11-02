#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pandas", "scipy", "matplotlib", "straplog[plot]>=0.3.1"]
# ///
"""
BaroShift paced-breathing sessions: find them in a StrapLog H10 recording,
measure how well the heart followed the pace, and keep one longitudinal log
that also takes app-only sessions (no H10) and the app's own scores.

    baroshift.py analyze --db <export> [--recording PK] [--window HH:MM-HH:MM ...]
                         [--program sleep] [--context "..."] [--log] [--out DIR]
    baroshift.py log --date YYYY-MM-DD --end HH:MM [--start HH:MM] [--program sleep]
                     [--training-score N] [--barocapacity N] [--focus F]
                     [--mechanics F] [--reflex F] [--heart-dynamics F] ...

How a session is found (window_source):
  manual           --window given
  pacer            the app's pacer_start/pacer_stop events (StrapLog 1.2+, or
                   a legacy "6.0/min" note); the stamped rate is the pace band
                   centre (+-1.5/min). An unstopped pacer ends with the recording.
  markers          a start marker and an end marker bracket it
  detected+end     the spectral scan found a locked run; end markers inside it
                   split it into sessions and set their ends
  detected         spectral scan only, no usable markers; kept only if the
                   whole window's on-peak share is >= 0.5 (else "rejected")

"Locked" = in a sliding 3-min R-R spectrum, the dominant 0.04-0.4 Hz peak
sits inside the pace band (default 4.5-7.5 breaths/min) and the power within
±0.015 Hz of it is at least half the total. Spontaneous breathing at rest or
asleep rarely concentrates that much power in one narrow slow peak.

Beat times are rebuilt from cumsum(rr) per gap-free segment: StrapLog's
per-beat stamps are BLE arrival times, batched, and unusable for resampling.
Times are local wall-clock; never tz-convert them.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from scipy.signal import welch

import straplog.db as straplog_db
from straplog import catalog, export, hrv

APPLE_REF = datetime(2001, 1, 1)
GAP_SEC = 15.0
FS = 4.0               # resample rate, Hz
WIN_SEC = 180.0        # sliding spectral window
STEP_SEC = 30.0
NPERSEG = 480          # 120 s Welch segments -> 0.0083 Hz resolution
HALF_BW = 0.015        # Hz either side of the peak counted as "on pace"
LOCK_SHARE = 0.5
MERGE_GAP_SEC = 120.0  # unlocked stretches this short don't break a run
MIN_RUN_SEC = 180.0
PRE_SEC = 300.0
POST_SEC = 300.0
POST2_SEC = 600.0      # second post window: end+5 -> end+15 min
END_SNAP_SEC = 180.0   # an end marker this close after a run ends it
PACER_BAND_HALF = 1.5  # breaths/min either side of the stamped rate (matches the 4.5-7.5 default at 6/min)
MIN_PACER_SEC = 60.0   # a spectrum needs a minute

LOG_PATH = Path(os.environ.get(
    "BAROSHIFT_LOG",
    Path.home() / "Documents/Notes/Sprague2026/Health2026/Data/baroshift.csv"))
LOG_COLUMNS = [
    "date", "start_local", "end_local", "duration_min", "program", "context",
    "h10", "window_source", "recording", "breath_rate_bpm", "peak_share",
    "lock_pct", "hr_osc_bpm", "hr_session", "hr_pre", "hr_post", "hr_post2",
    "rmssd_pre", "rmssd_post", "rmssd_post2",
    "training_score", "barocapacity", "focus", "mechanics", "reflex",
    "heart_dynamics", "scores_source", "notes",
]
SCORE_FIELDS = ["training_score", "barocapacity", "focus", "mechanics",
                "reflex", "heart_dynamics"]

START_RX = re.compile(r"\b(start|begin)\b", re.I)
END_RX = re.compile(r"\b(end|stop|finish)\b", re.I)


# ---------- load ----------------------------------------------------------

def local(ts: float) -> datetime:
    """Apple-epoch local seconds -> naive local datetime. No tz math."""
    return APPLE_REF + timedelta(seconds=float(ts))


def hms(ts: float) -> str:
    return local(ts).strftime("%H:%M:%S")


def load(db: Path, pk: int):
    """Beats with rebuilt times. Returns (t_beat, rr, keep, events)."""
    df = straplog_db.load_rr(db, pk)
    t_rx = df["t"].to_numpy(float)
    rr = df["rr_ms"].to_numpy(float)
    # New segment wherever arrival time jumps by more than a gap: a dropout.
    seg = np.r_[0, np.cumsum(np.diff(t_rx) > GAP_SEC)]
    t = np.empty_like(rr)
    for s in np.unique(seg):
        i = np.flatnonzero(seg == s)
        c = np.cumsum(rr[i]) / 1000.0
        t[i] = t_rx[i[0]] + (c - c[0])
    keep = hrv.artifact_mask(rr)
    ev = straplog_db.load_events(db, pk)
    events = [(float(r.t), str(r.note)) for r in ev.itertuples()]
    return t, rr, keep, seg, events


def resampled(t, rr, keep, seg, a, b, hr=False):
    """Clean R-R (or beat HR) on a 4 Hz grid inside [a, b), per segment;
    returns the longest gap-free piece (a spectrum must not span a dropout)."""
    best = (np.array([]), np.array([]))
    for s in np.unique(seg):
        m = (seg == s) & keep & (t >= a) & (t < b)
        if m.sum() < 30:
            continue
        tt, vv = t[m], rr[m]
        if hr:
            vv = 60000.0 / vv
        g = np.arange(tt[0], tt[-1], 1.0 / FS)
        if len(g) > len(best[0]):
            best = (g, np.interp(g, tt, vv))
    return best


def spectrum(x):
    x = x - x.mean()
    f, p = welch(x, fs=FS, nperseg=min(len(x), NPERSEG))
    return f, p


def peak_stats(f, p, band):
    """Dominant 0.04-0.4 Hz peak, its narrow-band share, and whether it is
    on pace."""
    df = f[1] - f[0]
    tot_m = (f >= 0.04) & (f <= 0.4)
    fpk = float(f[tot_m][np.argmax(p[tot_m])])
    near = tot_m & (f >= fpk - HALF_BW) & (f <= fpk + HALF_BW)
    pw = float(p[near].sum() * df)
    share = pw / float(p[tot_m].sum() * df)
    on_pace = band[0] <= fpk <= band[1]
    return fpk, share, pw, on_pace


# ---------- detection -----------------------------------------------------

def scan(t, rr, keep, seg, band):
    """Sliding-window lock trace: list of (centre, fpk, share, locked).
    share is the on-peak share when the peak is on pace, else 0."""
    out = []
    c = t[0] + WIN_SEC / 2
    while c < t[-1] - WIN_SEC / 2:
        g, x = resampled(t, rr, keep, seg, c - WIN_SEC / 2, c + WIN_SEC / 2)
        if len(x) >= 0.8 * WIN_SEC * FS:
            fpk, share, _, on = peak_stats(*spectrum(x), band)
            out.append((c, fpk, share if on else 0.0, on and share >= LOCK_SHARE))
        else:
            out.append((c, float("nan"), float("nan"), False))
        c += STEP_SEC
    return out


def runs(trace):
    """Locked runs as (start, end), merged across short unlocked stretches."""
    locked = [c for c, *_, lk in trace if lk]
    if not locked:
        return []
    rs, a, b = [], locked[0], locked[0]
    for c in locked[1:]:
        if c - b <= MERGE_GAP_SEC:
            b = c
        else:
            rs.append((a, b))
            a = b = c
    rs.append((a, b))
    return [(a, b) for a, b in rs if b - a >= MIN_RUN_SEC]


def pacer_windows(db: Path, pk: int, t_last: float):
    """Pacer sessions as (start, end, "pacer", info) in Apple-epoch local
    seconds (the same clock as `load`). A pacer with no stop ends at the
    recording's last beat; a window is clipped to the recording's beats."""
    ps = export.pacer_sessions(db, pk)
    if not ps:
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tz = con.execute("SELECT tz_offset_s FROM recording WHERE id=?", (pk,)).fetchone()[0]
    finally:
        con.close()
    to_apple = lambda ms: ms / 1000.0 + tz - straplog_db.APPLE_EPOCH_OFFSET
    out = []
    for p in ps:
        a = to_apple(p["start_ms"])
        b = t_last if p["stop_ms"] is None else min(to_apple(p["stop_ms"]), t_last)
        if b - a >= MIN_PACER_SEC:
            out.append((a, b, "pacer", {
                "rate": p["rate"], "inhale_s": p["inhale_s"], "exhale_s": p["exhale_s"],
                "stopped": p["stop_ms"] is not None}))
    return out


def sessions_from(trace, events, windows, pacer=()):
    """Resolve session windows and how each was found. Order of authority:
    manual, pacer, markers, spectral. Marker/spectral windows that overlap a
    pacer window are dropped; the rest still apply."""
    if windows:
        return [(a, b, "manual", None) for a, b in windows]
    starts = [t for t, n in events if START_RX.search(n)]
    ends = [t for t, n in events if END_RX.search(n)]
    out = list(pacer)
    # Marker pairs next: each start with the first end after it.
    used = set()
    for s in starts:
        e = next((e for e in ends if e > s and e not in used), None)
        if e is not None and e - s >= 60 and not any(p[0] < e and p[1] > s for p in pacer):
            out.append((s, e, "markers", None))
            used.add(e)
    free_ends = [e for e in ends if e not in used]
    for a, b in runs(trace):
        if any(o[0] < b and o[1] > a for o in out):
            continue
        inside = [e for e in free_ends if a <= e <= b + END_SNAP_SEC]
        if not inside:
            out.append((a, b, "detected", None))
            continue
        cur = a
        for e in inside:
            out.append((cur, e, "detected+end", None))
            cur = e
        if b - cur >= MIN_RUN_SEC:
            out.append((cur, b, "detected", None))
    return sorted(out, key=lambda o: o[:2])


# ---------- metrics -------------------------------------------------------

def td(t, rr, keep, a, b):
    m = keep & (t >= a) & (t < b)
    if m.sum() < 30:
        return {}
    d = hrv.time_domain(rr[m])
    return {"n": d["n"], "hr": round(d["hr_mean"], 1),
            "rmssd": round(d["rmssd_ms"], 1), "ln_rmssd": round(d["ln_rmssd"], 3),
            "sdnn": round(d["sdnn_ms"], 1)}


def session_metrics(t, rr, keep, seg, trace, a, b, band):
    g, x = resampled(t, rr, keep, seg, a, b)
    res = {"start": hms(a), "end": hms(b), "duration_min": round((b - a) / 60, 1)}
    if len(x) >= 60 * FS:
        f, p = spectrum(x)
        fpk, share, pw, on = peak_stats(f, p, band)
        res |= {"breath_rate_bpm": round(fpk * 60, 1), "peak_hz": round(fpk, 4),
                "peak_share": round(share, 2), "peak_power_ms2": round(pw),
                "on_pace": on}
        # HR oscillation: beat HR band-passed around the peak; peak-to-trough
        # of an equivalent sinusoid (2·√2·SD).
        gh, h = resampled(t, rr, keep, seg, a, b, hr=True)
        H = np.fft.rfft(h - h.mean())
        fr = np.fft.rfftfreq(len(h), 1 / FS)
        H[(fr < fpk - 0.03) | (fr > fpk + 0.03)] = 0
        res["hr_osc_bpm"] = round(float(2 * np.sqrt(2) * np.fft.irfft(H, len(h)).std()), 1)
    inside = [lk for c, *_, lk in trace if a <= c <= b]
    res["lock_pct"] = round(100 * sum(inside) / len(inside)) if inside else None
    res["session"] = td(t, rr, keep, a, b)
    res["first_2min_hr"] = td(t, rr, keep, a, a + 120).get("hr")
    res["last_2min_hr"] = td(t, rr, keep, b - 120, b).get("hr")
    res["pre"] = td(t, rr, keep, a - PRE_SEC, a)
    res["post"] = td(t, rr, keep, b, b + POST_SEC)
    res["post2"] = td(t, rr, keep, b + POST_SEC, b + POST_SEC + POST2_SEC)
    return res


# ---------- outputs -------------------------------------------------------

def plot(t, rr, keep, trace, sess, events, path: Path, title: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    if sess:
        lo = min(s[0] for s in sess) - 1200
        hi = max(s[1] for s in sess) + 1200
    else:
        lo, hi = t[0], t[-1]
    m = keep & (t >= lo) & (t <= hi)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 5.5), sharex=True,
                                   height_ratios=[2, 1])
    x = [local(v) for v in t[m]]
    ax1.plot(x, 60000 / rr[m], lw=0.6, color="#33658a")
    ax1.set_ylabel("HR (bpm)")
    tr = [(c, fpk, sh) for c, fpk, sh, _ in trace if lo <= c <= hi]
    ax2.plot([local(c) for c, *_ in tr], [sh for *_, sh in tr], color="#2f4858")
    ax2.axhline(LOCK_SHARE, ls=":", color="grey")
    ax2.set_ylabel("on-pace share")
    import matplotlib.dates as mdates
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax2.set_ylim(0, 1)
    for a, b, _ in sess:
        for ax in (ax1, ax2):
            ax.axvspan(local(a), local(b), color="#86bbd8", alpha=0.35)
    for te, n in events:
        if lo <= te <= hi:
            ax1.axvline(local(te), color="#f26419", lw=0.8)
            ax1.text(local(te), ax1.get_ylim()[1], n, rotation=90, va="top",
                     fontsize=7, color="#f26419")
    ax1.set_title(title)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def write_md(s: dict, path: Path):
    L = [f"# BaroShift: {s['name']} ({s['date']})", "",
         f"Recording pk {s['recording_pk']}, pace band {s['pace_band_bpm'][0]}–"
         f"{s['pace_band_bpm'][1]} breaths/min. Markers: "
         + (" · ".join(f"`{n}` {hms(te)}" for te, n in s["_events"]) or "none"), "",
         "| # | window | source | min | rate /min | share | lock % | HR osc | HR pre → session → post → post2 | RMSSD pre → post → post2 |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for i, x in enumerate(s["sessions"], 1):
        g = lambda k, f: x.get(k, {}).get(f, "–")
        L.append(f"| {i} | {x['start'][:5]}–{x['end'][:5]} | {x['window_source']} | "
                 f"{x['duration_min']} | {x.get('breath_rate_bpm', '–')} | "
                 f"{x.get('peak_share', '–')} | {x.get('lock_pct', '–')} | "
                 f"{x.get('hr_osc_bpm', '–')} | {g('pre','hr')} → {g('session','hr')} → "
                 f"{g('post','hr')} → {g('post2','hr')} | {g('pre','rmssd')} → "
                 f"{g('post','rmssd')} → {g('post2','rmssd')} |")
    L += ["", "RMSSD/SDNN *during* paced breathing are driven by the breathing; "
          "read pre → post, not the session value. Pre/post are 5 min either side; "
          "post2 is end+5 → end+15 min."]
    path.write_text("\n".join(L) + "\n")


def read_log() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    with LOG_PATH.open(newline="") as fh:
        return list(csv.DictReader(fh))


def write_log(rows: list[dict]):
    rows.sort(key=lambda r: (r["date"], r["end_local"]))
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("w", newline="") as fh:
        w = csv.DictWriter(fh, LOG_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")[:2]
    return int(h) * 60 + int(m)


def match(rows, date, end, tol=3):
    for r in rows:
        if r["date"] == date and r["end_local"] and abs(_mins(r["end_local"]) - _mins(end)) <= tol:
            return r
    return None


# ---------- commands ------------------------------------------------------

def parse_windows(specs, day0: datetime):
    """HH:MM-HH:MM -> Apple-epoch local seconds, on or after the recording start."""
    out = []
    for spec in specs or []:
        a, b = spec.split("-")
        ts = []
        for hm in (a, b):
            h, m = map(int, hm.split(":")[:2])
            d = day0.replace(hour=h, minute=m, second=0, microsecond=0)
            if d < day0 - timedelta(hours=1):
                d += timedelta(days=1)
            ts.append((d - APPLE_REF).total_seconds())
        out.append(tuple(ts))
    return out


def cmd_analyze(a) -> int:
    db = a.db
    if db is None:
        newest = next((e for e in catalog.entries() if e["downloaded"]), None)
        if newest is None:
            raise SystemExit(f"No downloaded StrapLog exports in {catalog.DEFAULT_DIR}")
        db = Path(newest["path"])
    recs = straplog_db.list_recordings(db)
    if a.recording is not None:
        rec = next((r for r in recs if r["pk"] == a.recording), None)
    else:
        rec = recs[-1] if recs else None
    if rec is None:
        raise SystemExit("recording not found")
    pk = rec["pk"]
    t, rr, keep, seg, events = load(db, pk)
    band = (a.pace_min / 60.0, a.pace_max / 60.0)
    trace = scan(t, rr, keep, seg, band)
    windows = parse_windows(a.window, local(t[0]))
    pacer = [] if windows else pacer_windows(db, pk, float(t[-1]))
    sess = sessions_from(trace, events, windows, pacer)
    traces = {band: trace}   # pacer sessions scan around their own rate
    out = {"db": str(db), "recording_pk": pk, "name": rec["name"],
           "date": local(t[0]).date().isoformat(),
           "pace_band_bpm": [a.pace_min, a.pace_max],
           "markers": [{"time": hms(te), "note": n} for te, n in events],
           "sessions": []}
    kept = []
    out["rejected"] = []
    for s0, s1, src, info in sess:
        sband = band
        if info:
            sband = (max(info["rate"] - PACER_BAND_HALF, 2.4) / 60.0,
                     (info["rate"] + PACER_BAND_HALF) / 60.0)
            if sband not in traces:
                traces[sband] = scan(t, rr, keep, seg, sband)
        m = session_metrics(t, rr, keep, seg, traces[sband], s0, s1, sband)
        m["window_source"] = src
        if info:
            m["pacer"] = {"stamped_rate_bpm": info["rate"], "inhale_s": info["inhale_s"],
                          "exhale_s": info["exhale_s"], "stopped": info["stopped"]}
            m["pace_band_bpm"] = [round(sband[0] * 60, 1), round(sband[1] * 60, 1)]
        m["date"] = local(s1).date().isoformat()
        # A scan-only run must hold the lock as a whole, not just in patches:
        # REM and pre-wake stretches throw up brief slow peaks.
        if src == "detected" and m.get("peak_share", 0) < LOCK_SHARE:
            out["rejected"].append({k: m.get(k) for k in
                                    ("start", "end", "breath_rate_bpm", "peak_share", "lock_pct")})
            continue
        out["sessions"].append(m)
        kept.append((s0, s1, src))
    sess = kept
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        png = a.out / f"baroshift_{pk}.png"
        plot(t, rr, keep, trace, [s[:3] for s in sess], events, png, f"{rec['name']} · BaroShift")
        out["plot"] = str(png)
        write_md(out | {"_events": events}, a.out / f"baroshift_{pk}.md")
        out["summary_md"] = str(a.out / f"baroshift_{pk}.md")
    if a.log:
        rows = read_log()
        for m in out["sessions"]:
            row = match(rows, m["date"], m["end"][:5])
            if row is None:
                row = dict.fromkeys(LOG_COLUMNS, "")
                rows.append(row)
            row |= {
                "date": m["date"], "start_local": m["start"][:5],
                "end_local": m["end"][:5], "duration_min": m["duration_min"],
                "program": a.program or row.get("program", ""),
                "context": a.context or row.get("context", ""),
                "h10": "y", "window_source": m["window_source"],
                "recording": Path(db).name,
                "breath_rate_bpm": m.get("breath_rate_bpm", ""),
                "peak_share": m.get("peak_share", ""), "lock_pct": m.get("lock_pct", ""),
                "hr_osc_bpm": m.get("hr_osc_bpm", ""),
                "hr_session": m["session"].get("hr", ""),
                "hr_pre": m["pre"].get("hr", ""), "hr_post": m["post"].get("hr", ""),
                "hr_post2": m["post2"].get("hr", ""),
                "rmssd_pre": m["pre"].get("rmssd", ""),
                "rmssd_post": m["post"].get("rmssd", ""),
                "rmssd_post2": m["post2"].get("rmssd", ""),
            }
        write_log(rows)
        out["log"] = str(LOG_PATH)
    print(json.dumps(out, indent=1, default=str))
    return 0


def cmd_log(a) -> int:
    """App-only session, or the app's scores for an H10 session already logged
    (matched on date + end time within 3 min)."""
    rows = read_log()
    row = match(rows, a.date, a.end)
    action = "updated"
    if row is None:
        row = dict.fromkeys(LOG_COLUMNS, "") | {
            "date": a.date, "end_local": a.end, "h10": "n"}
        rows.append(row)
        action = "added"
    for k in ["start", "duration_min", "program", "context", "notes", "scores_source"]:
        v = getattr(a, k)
        if v not in (None, ""):
            row["start_local" if k == "start" else k] = v
    for k in SCORE_FIELDS:
        v = getattr(a, k)
        if v is not None:
            row[k] = int(v) if float(v).is_integer() else v
    if any(getattr(a, k) is not None for k in SCORE_FIELDS) and not row["scores_source"]:
        row["scores_source"] = "screenshot"
    write_log(rows)
    print(json.dumps({"action": action, "log": str(LOG_PATH),
                      "row": {k: row[k] for k in LOG_COLUMNS if row[k] != ""}}, indent=1))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    an = sub.add_parser("analyze", help="find and measure sessions in an H10 recording")
    an.add_argument("--db", type=catalog.resolve, default=None,
                    help="StrapLog export path or bare name (default: newest)")
    an.add_argument("--recording", type=int, default=None, help="recording pk")
    an.add_argument("--window", action="append",
                    help="HH:MM-HH:MM session window (repeatable); overrides detection")
    an.add_argument("--pace-min", type=float, default=4.5, help="breaths/min")
    an.add_argument("--pace-max", type=float, default=7.5, help="breaths/min")
    an.add_argument("--program", default="", help="BaroShift program, e.g. sleep")
    an.add_argument("--context", default="", help="e.g. 'overnight, after waking'")
    an.add_argument("--log", action="store_true", help=f"upsert rows into {LOG_PATH}")
    an.add_argument("--out", type=Path, default=None, help="scratch dir for plot + md")
    an.set_defaults(func=cmd_analyze)

    lg = sub.add_parser("log", help="log an app-only session or add app scores")
    lg.add_argument("--date", required=True, help="YYYY-MM-DD (local)")
    lg.add_argument("--end", required=True, help="HH:MM local session end")
    lg.add_argument("--start", default=None, help="HH:MM")
    lg.add_argument("--duration-min", default=None)
    lg.add_argument("--program", default=None)
    lg.add_argument("--context", default=None)
    lg.add_argument("--notes", default=None)
    lg.add_argument("--scores-source", default=None,
                    help="screenshot (default when scores given) or export")
    lg.add_argument("--training-score", type=float, default=None, help="0-100")
    lg.add_argument("--barocapacity", type=float, default=None, help="0-100")
    for k in ["focus", "mechanics", "reflex", "heart-dynamics"]:
        lg.add_argument(f"--{k}", type=float, default=None,
                        help="bar position 0-1 (read off the screenshot; approximate)")
    lg.set_defaults(func=cmd_log)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
