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
                       independently corroborates it as genuine wake; and H10
                       HR / RMSSD inside every staged epoch, rolled up by stage

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
      --health auto \
      --out ~/Downloads/hrv260726/overnight
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

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
    continuous segments only so a dropout can't fabricate an extreme.

    The raw figures (physiological gate only) keep the filed series
    comparable; the `_clean` twins are recomputed after `hrv.artifact_mask`,
    so an ectopic burst can't pose as peak vagal tone. A large raw-vs-clean
    gap means the raw extreme is an artifact: report the clean one."""
    keep = hrv.artifact_mask([v for _, v in rows])
    clean_rows = [r for r, k in zip(rows, keep) if k]
    raw, clean = _extremes(rows, gaps_raw), _extremes(clean_rows, gaps_raw)
    return {**raw, **{f"{k}_clean": v for k, v in clean.items()},
            "artifact_beats": int(len(gate(rows)) - keep.sum())}


def _extremes(rows, gaps_raw) -> dict:
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
        "_sleep_aggregate": d.get("sleep_aggregate"),
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

    awake, epochs = [], []
    for e in sleep.get("epochs", []) or []:
        w = {
            "start": _parse_local(e["start_local"]),
            "end": _parse_local(e["end_local"]),
            "duration_min": round(float(e.get("duration_min", 0)), 1),
        }
        epochs.append({"stage": str(e.get("stage", "")), **w})
        if str(e.get("stage", "")).lower().startswith("awake"):
            awake.append(w)

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
        "stage_epochs": epochs,
        "resting_heart_rate_bpm": cardio.get("resting_heart_rate_bpm"),
        "resting_heart_rate_prior_day_bpm": cardio.get("resting_heart_rate_prior_day_bpm"),
        "rhr_baseline_14d_mean_bpm": cardio.get("rhr_baseline_14d_mean_bpm"),
        "overnight_hr_nadir_bpm": cardio.get("overnight_hr_nadir_bpm"),
        "wake_stage_at_arousal": sleep.get("wake_stage_at_arousal"),
        "watch_sdnn": cardio.get("hrv_sdnn", {}),
        "known_gaps": meta.get("known_gaps", []),
        "adapted_from": d.get("_adapted_from"),
        "anomaly_flags": d.get("_anomaly_flags"),
        "aggregate": d.get("_sleep_aggregate"),
        "activity_yesterday": d.get("_activity_yesterday"),
        "physiology": d.get("_physiology"),
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


# ---------- Apple Health, fetched live (--health auto) ----------------------
#
# The Health Auto Export iPhone app serves MCP over HTTP on the LAN while it is
# open. Fetching here replaces the old hand-off (Claude called the MCP tool,
# then transcribed the stage list into applehealth_*.json). The result is
# written in the flat morning-pull layout, so load_health() stays the only
# parser.

HAE_CONFIG = Path("~/.config/straplog/health.toml").expanduser()
HAE_STAGES = ("Core", "Deep", "REM", "Awake")
HAE_TIME = "%Y-%m-%d %H:%M:%S %z"


def hae_config() -> tuple[str, str]:
    """(url, token) from HEALTH_AUTO_EXPORT_URL/_TOKEN, else the toml file."""
    import os
    import tomllib
    url = os.environ.get("HEALTH_AUTO_EXPORT_URL")
    token = os.environ.get("HEALTH_AUTO_EXPORT_TOKEN")
    if not (url and token) and HAE_CONFIG.exists():
        cfg = tomllib.loads(HAE_CONFIG.read_text())
        url, token = url or cfg.get("url"), token or cfg.get("token")
    if not (url and token):
        raise RuntimeError(f"no Health Auto Export endpoint: set HEALTH_AUTO_EXPORT_URL "
                           f"and _TOKEN, or write url/token to {HAE_CONFIG}")
    return url, token


class HaeClient:
    """Minimal MCP streamable-HTTP client: initialize once, then tools/call."""

    def __init__(self, url: str, token: str, timeout: float = 20.0):
        self.url, self.timeout = url, timeout
        self.headers = {"Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream"}
        self._id = 0
        init = self._post("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "straplog-skills", "version": "1"}})
        if init.get("session_id"):
            self.headers["Mcp-Session-Id"] = init["session_id"]
        self._post("notifications/initialized", None, notify=True)

    def _post(self, method: str, params, notify: bool = False) -> dict:
        import urllib.request
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self._id += 1
            body["id"] = self._id
        req = urllib.request.Request(self.url, json.dumps(body).encode(), self.headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            sid = r.headers.get("Mcp-Session-Id")
            text = r.read().decode()
        if notify:
            return {}
        if text.lstrip().startswith("{"):
            msg = json.loads(text)
        else:   # SSE: the response is the last `data:` line
            msg = json.loads([ln[5:] for ln in text.splitlines() if ln.startswith("data:")][-1])
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return {"session_id": sid, **msg.get("result", {})}

    def call(self, tool: str, **arguments) -> dict:
        res = self._post("tools/call", {"name": tool, "arguments": arguments})
        text = "".join(c.get("text", "") for c in res.get("content", []))
        if res.get("isError"):
            raise RuntimeError(f"{tool}: {text[:200]}")
        return json.loads(text)


def _hae_metrics(resp: dict) -> dict[str, list[dict]]:
    return {m["name"]: m.get("data", []) for m in resp.get("data", {}).get("metrics", [])}


def _hae_daily(rows: list[dict], day) -> float | None:
    q = next((r["qty"] for r in rows if r["date"][:10] == day.isoformat()), None)
    return None if q is None else round(q, 1)


def _hae_aggregate(agg_resp: dict, strip_end: datetime) -> dict | None:
    """Apple's per-night totals. TST for the Sleep.md row comes from here; the
    epochs drive the awake-window reconciliation. (The per-epoch feed used to
    stop before the final wake on 261016-261018; that was the fetch window,
    see fetch_health().)"""
    nights = [r for r in _hae_metrics(agg_resp).get("sleep_analysis", [])
              if r.get("sleepEnd")]
    if not nights:
        return None
    end = lambda r: datetime.strptime(r["sleepEnd"], HAE_TIME)   # noqa: E731
    r = min(nights, key=lambda r: abs((end(r) - strip_end).total_seconds()))
    m = lambda k: round(60 * (r.get(k) or 0), 1)   # noqa: E731  hours -> min
    return {"sleep_start_local": datetime.strptime(r["sleepStart"], HAE_TIME).isoformat(),
            "sleep_end_local": end(r).isoformat(),
            "tst_min": m("totalSleep"), "core_min": m("core"), "deep_min": m("deep"),
            "rem_min": m("rem"), "awake_min": m("awake")}


def health_pull(sleep_resp: dict, daily_resp: dict, hrv_resp: dict, agg_resp: dict,
                strip_start: datetime, strip_end: datetime, now: datetime) -> dict:
    """Map Health Auto Export responses onto the flat morning-pull layout.

    `strip_start` / `strip_end` are the H10 strip's aware local datetimes;
    staged epochs overlapping [start - 1 h, end + 2 h] count as this night.
    """
    lo, hi = strip_start - timedelta(hours=1), strip_end + timedelta(hours=2)
    seen, epochs = set(), []
    for r in _hae_metrics(sleep_resp).get("sleep_analysis", []):
        if r.get("value") not in HAE_STAGES:
            continue
        a, b = datetime.strptime(r["start"], HAE_TIME), datetime.strptime(r["end"], HAE_TIME)
        if b <= lo or a >= hi or (a, b) in seen:
            continue
        seen.add((a, b))
        epochs.append((r["value"], a, b))
    if not epochs:
        raise RuntimeError("no Watch sleep staging overlaps the strip")
    epochs.sort(key=lambda e: e[1])

    mins = {s: 0.0 for s in HAE_STAGES}
    for s, a, b in epochs:
        mins[s] += (b - a).total_seconds() / 60
    first, last = epochs[0][1], epochs[-1][2]
    span = (last - first).total_seconds() / 60
    tst = mins["Core"] + mins["Deep"] + mins["REM"]
    awake = [(b - a).total_seconds() / 60 for s, a, b in epochs if s == "Awake"]
    pct = lambda m: round(100 * m / tst, 1) if tst else None   # noqa: E731

    wake_day = last.date()
    daily = _hae_metrics(daily_resp)
    yday = wake_day - timedelta(days=1)
    hrv_samples = []
    for r in _hae_metrics(hrv_resp).get("heart_rate_variability", []):
        t = datetime.strptime(r["date"], HAE_TIME)
        hrv_samples.append({"time_local": t.isoformat(), "sdnn_ms": round(r["qty"], 1),
                            "in_sleep_window": first <= t <= last})
    hours_after_wake = (now - last).total_seconds() / 3600

    return {
        "schema_version": "1.0",
        "date_label": wake_day.strftime("%y%m%d"),
        "captured_at_local": now.isoformat(timespec="seconds"),
        "sleep_window": {
            "staged_span_start_local": first.isoformat(),
            "staged_span_end_local": last.isoformat(),
            "staged_span_min": round(span, 1),
            "tst_min": round(tst, 1),
            "sleep_efficiency_pct_vs_staged_span": round(100 * tst / span, 1) if span else None,
        },
        "sleep_architecture": {
            "tst_min": round(tst, 1),
            "core_min": round(mins["Core"], 1), "core_pct_tst": pct(mins["Core"]),
            "n3_deep_min": round(mins["Deep"], 1), "n3_pct_tst": pct(mins["Deep"]),
            "rem_min": round(mins["REM"], 1), "rem_pct_tst": pct(mins["REM"]),
            "wake_stage_at_final_arousal": epochs[-1][0],
        },
        "waso": {
            "waso_total_min": round(sum(awake), 1),
            "episode_count": len(awake),
            "longest_episode_min": round(max(awake), 1) if awake else 0.0,
        },
        "sleep_aggregate": _hae_aggregate(agg_resp, strip_end),
        "sleep_epochs_staged": [
            {"stage": s, "start_local": a.isoformat(), "end_local": b.isoformat(),
             "min": round((b - a).total_seconds() / 60, 1)} for s, a, b in epochs],
        "autonomic": {
            "rhr_today_bpm": _hae_daily(daily.get("resting_heart_rate", []), wake_day),
            "rhr_yesterday_bpm": _hae_daily(daily.get("resting_heart_rate", []), yday),
            "hrv_samples": hrv_samples,
        },
        "activity_yesterday": {
            "steps": _hae_daily(daily.get("step_count", []), yday),
            "active_kcal": _hae_daily(daily.get("active_energy", []), yday),
            "exercise_min": _hae_daily(daily.get("apple_exercise_time", []), yday),
            "walking_hr_avg": _hae_daily(daily.get("walking_heart_rate_average", []), yday),
        },
        "physiology": {
            "resp_rate": _hae_daily(daily.get("respiratory_rate", []), wake_day),
            "spo2_pct": _hae_daily(daily.get("blood_oxygen_saturation", []), wake_day),
        },
        "provenance": {
            # Staging commits ~T+2 h after final wake; RHR is gated separately
            # (>= 18 h) in load_health(), whatever this says.
            "provisional": hours_after_wake < 2.0,
            "source": f"Health Auto Export MCP, fetched {now:%Y-%m-%d %H:%M} "
                      f"({hours_after_wake:.1f} h after final staged wake)",
        },
    }


def fetch_health(strip_start: datetime, strip_end: datetime, out_dir: Path) -> Path:
    """Fetch the night's Watch data and write applehealth_<YYMMDD>.json.

    Takes the strip's naive local datetimes; the system zone supplies the
    offset for that date (DST-correct for back-dated nights)."""
    a, b = strip_start.astimezone(), strip_end.astimezone()
    lo, hi = a - timedelta(hours=1), b + timedelta(hours=2)
    fmt = lambda t: t.strftime(HAE_TIME)   # noqa: E731
    day0 = (b.date() - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00 ") + b.strftime("%z")
    day1 = b.strftime("%Y-%m-%d 23:59:59 %z")

    hae = HaeClient(*hae_config())
    # Health Auto Export silently drops epochs that START in roughly the last
    # 6 h of the requested window (261019: end 06:45 -> feed stopped at 00:56;
    # end 05:00 -> 23:08; end 12:00 -> full night to 04:06). This was the
    # "per-epoch feed stops before final wake" seen 261016-261018. Over-request
    # by 10 h; health_pull() trims to the strip's own overlap window.
    sleep = hae.call("get_health_metrics", start=fmt(lo), end=fmt(hi + timedelta(hours=10)),
                     metrics="sleep_analysis", aggregate=False)
    daily = hae.call("get_health_metrics", start=day0, end=day1, interval="days",
                     metrics="resting_heart_rate,respiratory_rate,blood_oxygen_saturation,"
                             "step_count,active_energy,apple_exercise_time,"
                             "walking_heart_rate_average")
    hrv_ = hae.call("get_health_metrics", start=fmt(lo), end=fmt(hi),
                    metrics="heart_rate_variability", aggregate=False)
    agg = hae.call("get_health_metrics", start=fmt(b - timedelta(hours=18)),
                   end=fmt(b + timedelta(hours=6)), metrics="sleep_analysis",
                   aggregate=True, interval="days")
    pull = health_pull(sleep, daily, hrv_, agg, a, b, datetime.now().astimezone())
    path = out_dir / f"applehealth_{pull['date_label']}.json"
    path.write_text(json.dumps(pull, indent=1))
    return path


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


MIN_STAGE_SEC = 180.0   # below this an epoch's RMSSD is noise: HR only
MIN_PAIRS = 30          # successive differences needed before quoting RMSSD
STAGE_ORDER = ("Deep", "Core", "REM", "Awake")


def _stage_name(raw: str) -> str:
    s = raw.split()[0].capitalize() if raw else "?"
    return "REM" if s == "Rem" else s


def _rmssd(sq: list[float]) -> dict:
    if len(sq) < MIN_PAIRS:
        return {}
    rm = (sum(sq) / len(sq)) ** 0.5
    return {"rmssd": round(rm, 1), "ln_rmssd": round(math.log(rm), 3)}


def reconcile_stages(rows, epochs) -> dict:
    """H10 autonomics inside every Watch-staged epoch, plus a per-stage
    rollup. Within one night, N3 is expected to carry the lowest HR and more
    vagal tone than REM; a Deep epoch indistinguishable from its Core
    neighbours is weakly supported. This corroborates the Watch's labels; it
    cannot stage sleep from the H10 alone.

    Beats pass `hrv.artifact_mask` (the `_clean` convention). Successive
    differences are taken only between beats adjacent in the raw stream,
    inside one epoch and never across a dropout, so RMSSD can't bridge a stage
    boundary or a gap. Mean HR is 60000 / mean(RR), as in reconcile_awake.
    The rollup pools only epochs of at least MIN_STAGE_SEC."""
    keep = hrv.artifact_mask([v for _, v in rows])
    per, pool = [], {}
    for e in epochs:
        a = (e["start"] - REF).total_seconds()
        b = (e["end"] - REF).total_seconds()
        rrs, sq, prev = [], [], None   # prev = (raw index, ts, rr) of last kept beat
        for i, ((ts, v), k) in enumerate(zip(rows, keep)):
            if ts < a:
                continue
            if ts > b:
                break
            if not (k and gated(v)):
                prev = None
                continue
            rrs.append(v)
            if prev and prev[0] == i - 1 and ts - prev[1] <= GAP_SEC:
                sq.append((v - prev[2]) ** 2)
            prev = (i, ts, v)
        stage = _stage_name(e["stage"])
        long_enough = (b - a) >= MIN_STAGE_SEC
        row = {"stage": stage,
               "window": f"{e['start']:%H:%M}-{e['end']:%H:%M}",
               "duration_min": e["duration_min"],
               "n_beats": len(rrs)}
        if len(rrs) >= 5:
            row["h10_mean_hr"] = round(60000.0 / (sum(rrs) / len(rrs)), 1)
        if long_enough:
            row |= _rmssd(sq)
        if len(rrs) < 5:
            row["note"] = "no H10 coverage"
        elif not long_enough:
            row["note"] = "short"
        per.append(row)
        if long_enough and rrs:
            p = pool.setdefault(stage, {"min": 0.0, "rr": [], "sq": [], "n": 0})
            p["min"] += e["duration_min"]
            p["rr"] += rrs
            p["sq"] += sq
            p["n"] += 1

    rank = {s: i for i, s in enumerate(STAGE_ORDER)}
    by_stage = [{"stage": st, "n_epochs": p["n"], "minutes": round(p["min"], 1),
                 "h10_mean_hr": round(60000.0 / (sum(p["rr"]) / len(p["rr"])), 1),
                 **_rmssd(p["sq"])}
                for st, p in sorted(pool.items(), key=lambda kv: rank.get(kv[0], 9))]
    return {"epochs": per, "by_stage": by_stage}


# ---------- H10-only wake estimate -----------------------------------------
#
# A wake bout shows on the H10 as a heart-rate surge above the local sleeping
# baseline. That catches movement and real arousals; quiet wake lying still
# looks like light sleep, so TST from the H10 alone reads high. Gap epochs are
# unknown, never sleep: `tst_min` excludes them and `tst_max_min` counts them.

EPOCH_SEC = 30.0
WAKE_BASE_HALF_SEC = 1800.0   # baseline = rolling low percentile over ±30 min
WAKE_BASE_PCT = 20
WAKE_ENTER_BPM = 8.0          # hysteresis: a bout starts this far above baseline
WAKE_STAY_BPM = 4.0           # ...and lasts while HR stays this far above it
WAKE_MIN_SEC = 60.0           # shorter surges are arousals, not wake
ONSET_RUN_SEC = 600.0         # sleep onset = first 10 min with no wake epoch
IN_MARKERS = ("lights_out", "lights out", "bed")
OUT_MARKERS = ("up", "awake", "wake")


def _marker_window(markers, t_start: float, t_end: float, day0: datetime):
    """Time in bed from Rik's diamonds: the first lights-out to the last
    up/awake marker; the strip edges where a marker is missing. Marker clock
    times are resolved onto the strip's date span."""
    def ts_of(hhmm: str) -> float:
        h, m = map(int, hhmm.split(":"))
        t = datetime.combine(day0.date(), datetime.min.time()) + timedelta(hours=h, minutes=m)
        ts = (t - REF).total_seconds()
        return ts + 86400 if ts < t_start - 60 else ts
    ins = [ts_of(m["time"]) for m in markers if m["note"].lower() in IN_MARKERS]
    outs = [ts_of(m["time"]) for m in markers if m["note"].lower() in OUT_MARKERS]
    a = min(ins) if ins else t_start
    b = max(outs) if outs else t_end
    return max(a, t_start), min(b, t_end), bool(ins), bool(outs)


def _epoch_hr(rows, a: float, b: float) -> list[tuple[float, float | None]]:
    """(epoch start, HR) per EPOCH_SEC epoch; HR is None where under half the
    epoch has clean beats (a dropout). HR = 60000 / mean(RR), as elsewhere."""
    keep = hrv.artifact_mask([v for _, v in rows])
    n = int((b - a) // EPOCH_SEC)
    acc = [[] for _ in range(n)]
    for (ts, v), k in zip(rows, keep):
        i = int((ts - a) // EPOCH_SEC)
        if 0 <= i < n and k and gated(v):
            acc[i].append(v)
    return [(a + i * EPOCH_SEC,
             60000.0 / (sum(r) / len(r)) if sum(r) >= 500.0 * EPOCH_SEC else None)
            for i, r in enumerate(acc)]


def h10_wake_estimate(rows, markers, day0: datetime) -> dict:
    """Sleep onset, wake bouts and TST from the H10 alone (see section note)."""
    a, b, has_in, has_out = _marker_window(markers, rows[0][0], rows[-1][0], day0)
    ep = _epoch_hr(rows, a, b)
    hr = [h for _, h in ep]
    half = int(WAKE_BASE_HALF_SEC // EPOCH_SEC)
    base = []
    for i in range(len(ep)):
        w = sorted(h for h in hr[max(0, i - half):i + half + 1] if h is not None)
        base.append(w[int(len(w) * WAKE_BASE_PCT / 100)] if w else None)

    wake, on = [False] * len(ep), False
    for i, (h, bl) in enumerate(zip(hr, base)):
        if h is None or bl is None:
            on = False
            continue
        on = h >= bl + (WAKE_STAY_BPM if on else WAKE_ENTER_BPM)
        wake[i] = on
    # drop bouts too short to be wake
    min_ep = int(WAKE_MIN_SEC // EPOCH_SEC)
    i = 0
    while i < len(wake):
        if wake[i]:
            j = i
            while j < len(wake) and wake[j]:
                j += 1
            if j - i < min_ep:
                wake[i:j] = [False] * (j - i)
            i = j
        else:
            i += 1

    run = int(ONSET_RUN_SEC // EPOCH_SEC)
    onset = next((i for i in range(len(ep) - run + 1)
                  if all(hr[k] is not None and not wake[k] for k in range(i, i + run))),
                 None)
    m = EPOCH_SEC / 60.0
    out = {"window": f"{hm(a)}-{hm(b)}", "window_start_ts": a,
           "window_source": ("markers" if has_in and has_out else
                             "markers+strip edge" if has_in or has_out else "strip edges"),
           "time_in_bed_min": round((b - a) / 60.0, 1),
           "labels": ["gap" if h is None else "wake" if w else "sleep"
                      for h, w in zip(hr, wake)]}
    if onset is None:
        return out | {"note": "no 10-min sleep run found"}
    post = out["labels"][onset:]
    bouts, i = [], onset
    while i < len(ep):
        if wake[i]:
            j = i
            while j < len(ep) and wake[j]:
                j += 1
            bouts.append({"from": hm(ep[i][0]), "to": hm(ep[j - 1][0] + EPOCH_SEC),
                          "minutes": round((j - i) * m, 1),
                          "peak_hr": round(max(hr[i:j]), 1)})
            i = j
        else:
            i += 1
    tst = post.count("sleep") * m
    gap_after = post.count("gap") * m
    return out | {
        "sleep_onset": hm(ep[onset][0]),
        "onset_latency_min": round(onset * m, 1),
        "gap_min_before_onset": round(out["labels"][:onset].count("gap") * m, 1),
        "waso_min": round(post.count("wake") * m, 1),
        "gap_min_after_onset": round(gap_after, 1),
        "tst_min": round(tst, 1),
        "tst_max_min": round(tst + gap_after, 1),
        "efficiency_pct": round(100.0 * tst / ((b - a) / 60.0), 1),
        "wake_bouts": bouts,
    }


def validate_wake(est: dict, epochs) -> dict | None:
    """Epoch-by-epoch agreement of the H10 labels with Watch staging over the
    same window: Watch 'sleep' = inside a non-Awake epoch."""
    if "labels" not in est or not epochs:
        return None
    a = est["window_start_ts"]
    spans = [((e["start"] - REF).total_seconds(), (e["end"] - REF).total_seconds())
             for e in epochs if _stage_name(e["stage"]) != "Awake"]
    tp = fn = fp = tn = 0
    for i, lab in enumerate(est["labels"]):
        if lab == "gap":
            continue
        mid = a + (i + 0.5) * EPOCH_SEC
        w_sleep = any(s <= mid < e for s, e in spans)
        h_wake = lab == "wake" or (est.get("onset_latency_min") is not None
                                    and i < est["onset_latency_min"] * 60 / EPOCH_SEC)
        if not w_sleep:
            tp, fn = tp + h_wake, fn + (not h_wake)
        else:
            fp, tn = fp + h_wake, tn + (not h_wake)
    m = EPOCH_SEC / 60.0
    tot = tp + fn + fp + tn
    return {
        "watch_tst_min": round((fp + tn) * m, 1),
        "h10_tst_min": round((fn + tn) * m, 1),
        "agreement_pct": round(100.0 * (tp + tn) / tot, 1) if tot else None,
        "watch_wake_min": round((tp + fn) * m, 1),
        "wake_caught_pct": round(100.0 * tp / (tp + fn), 1) if tp + fn else None,
        "h10_wake_confirmed_pct": round(100.0 * tp / (tp + fp), 1) if tp + fp else None,
    }


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
          f"| Artifact-cleaned: LnRMSSD / peak vagal / HR floor | "
          f"{q['ln_rmssd_clean']:.3f} / {s['extremes']['peak_vagal_ln_rmssd_clean']} "
          f"@ {s['extremes']['peak_vagal_at_clean']} / {s['extremes']['hr_floor_bpm_clean']} "
          f"@ {s['extremes']['hr_floor_at_clean']} ({s['extremes']['artifact_beats']} beats dropped) |",
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

    sr = s.get("stage_reconciliation") or {}
    if sr.get("by_stage"):
        L += ["## H10 by Watch stage", "",
              "| Stage | Epochs ≥3 min | Minutes | H10 mean HR | RMSSD | LnRMSSD |",
              "|---|---|---|---|---|---|"]
        for r in sr["by_stage"]:
            L.append(f"| {r['stage']} | {r['n_epochs']} | {r['minutes']} | "
                     f"{r['h10_mean_hr']} | {r.get('rmssd', '—')} | "
                     f"{r.get('ln_rmssd', '—')} |")
        L += ["", "| Epoch | Stage | Min | H10 mean HR | RMSSD | LnRMSSD | Note |",
              "|---|---|---|---|---|---|---|"]
        for r in sr["epochs"]:
            L.append(f"| {r['window']} | {r['stage']} | {r['duration_min']} | "
                     f"{r.get('h10_mean_hr', '—')} | {r.get('rmssd', '—')} | "
                     f"{r.get('ln_rmssd', '—')} | {r.get('note', '')} |")
        L += ["", "> N3 is expected to show the night's lowest HR and more vagal "
                  "tone than REM; Deep that matches Core is weakly supported. Artifact-cleaned "
                  "beats; RMSSD never bridges an epoch boundary or a dropout; "
                  "epochs under 3 min report HR only.", ""]

    hs = s.get("h10_sleep") or {}
    if hs.get("tst_min") is not None:
        L += ["## H10-only sleep estimate", "",
              "| Metric | Value |", "|---|---|",
              f"| Window ({hs['window_source']}) | {hs['window']} "
              f"({hs['time_in_bed_min']} min) |",
              f"| Onset / latency | {hs['sleep_onset']} / {hs['onset_latency_min']} min |",
              f"| **TST** | **{hs['tst_min']} min ({hs['tst_min'] / 60:.2f} h)**"
              + (f", up to {hs['tst_max_min']} min if the dropouts were sleep"
                 if hs["gap_min_after_onset"] else "") + " |",
              f"| WASO (HR surges ≥1 min) | {hs['waso_min']} min, "
              f"{len(hs['wake_bouts'])} bouts |",
              f"| Efficiency | {hs['efficiency_pct']}% |", ""]
        v = s.get("h10_sleep_vs_watch")
        if v:
            L += [f"Against Watch staging: Watch TST {v['watch_tst_min']} vs H10 "
                  f"{v['h10_tst_min']} min; epoch agreement {v['agreement_pct']}%; "
                  f"the H10 caught {v['wake_caught_pct']}% of the Watch's "
                  f"{v['watch_wake_min']} wake min.", ""]
        L += ["> Wake = HR ≥8 bpm above the rolling ±30-min sleeping baseline "
              "for ≥1 min. Quiet wake lying still is invisible to HR, and REM "
              "surges can pose as wake, so bout-by-bout agreement with the Watch "
              "is only ~45%. On six nights with Watch staging (wake dates "
              "2026-10-18 → 10-24) TST landed within ±23 min of the Watch on "
              "average (range −36 to +55), with onset within 7 min.", ""]
    elif hs:
        L += ["## H10-only sleep estimate", "", f"- {hs.get('note')}", ""]

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
                   help="Apple Health JSON for the same night (path or glob), or "
                        "`auto` to fetch it from Health Auto Export on the iPhone")
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
        "whole_strip": {k: round(v, 3) for k, v in whole.items()}
                       | {"ln_rmssd_clean": round(td_stats(list(hrv.clean_rr(gate(rows))))["ln_rmssd"], 3)},
        "extremes": rolling_extremes(rows, gaps_raw),
        "blocks": block_trajectory(rows),
        "db": str(args.db),
        "end_date": ts_to_local(rows[-1][0]).strftime("%Y-%m-%d"),
        "markers": [{"time": hm(r.t), "note": str(r.note or "").strip()}
                    for r in straplog_db.load_events(args.db, pk).itertuples()],
    }

    wake_est = h10_wake_estimate(rows, s["markers"], start_dt)
    s["h10_sleep"] = {k: v for k, v in wake_est.items()
                      if k not in ("labels", "window_start_ts")}

    if str(args.health) == "auto":
        try:
            args.health = fetch_health(start_dt, ts_to_local(rows[-1][0]), out_dir)
        except Exception as e:   # phone off the LAN, app closed: run without it
            s["health_file"] = None
            s["health_warning"] = (f"Health Auto Export fetch failed ({type(e).__name__}: {e}); "
                                   "open the app on the iPhone and re-run")
            args.health = None
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
            s["stage_reconciliation"] = reconcile_stages(
                rows, s["sleep"]["stage_epochs"])
            s["h10_sleep_vs_watch"] = validate_wake(
                wake_est, s["sleep"]["stage_epochs"])
            # the epoch lists carry datetimes; drop them from the JSON payload
            s["sleep"] = {k: v for k, v in s["sleep"].items()
                          if k not in ("awake_windows", "stage_epochs")}
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
