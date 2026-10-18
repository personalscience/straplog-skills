---
name: hrv-resting
description: Time-domain HRV (SDNN, RMSSD, LnRMSSD, pNN50) from a short seated resting recording (StrapLog export); logs to a longitudinal CSV and plots the 7-day rolling LnRMSSD baseline.
---

# Resting HRV Tracker

Turns a short resting StrapLog recording (2–5 min, on waking or seated) into time-domain HRV metrics plus a longitudinal trend. Every reading is appended to `~/Documents/hrv_resting_log.csv` and each run refreshes a rolling-baseline LnRMSSD plot.

Classic time-domain metrics over a single window — at rest the sliding-window/DFA framing isn't useful. It also handles **marked intervention sessions** (breathwork, a relaxation device, a nap) bracketed by start/end diamonds, reported as pre → session → post (§3a).

## Workflow

### 1. Locate the export

StrapLog writes one SQLite snapshot per recording (`<YYMMDD-HHMM>-<name>.sqlite`) to iCloud Drive (`~/Library/Mobile Documents/iCloud~com~personalscience~straplog/Documents/`; older ones may be in `~/Downloads/`). `--db` takes a path or a bare export name (looked up in the iCloud folder); without it, the newest downloaded export there is used. `uvx straplog ls --json` lists exports with their activity. Anything that is not a StrapLog snapshot (no `beat` table) is rejected. Current exports hold one recording, older ones every recording on the phone; pick one with `--list` (`pk` is StrapLog's `recording.id`). Markers (preset label `a`/`b`/`c`/`end`/`resting_start`/…, free note, or both as `label · note`) become events. Loading, the artifact filter and the metrics come from the `straplog` package on PyPI, declared in the script's inline metadata.

### 2. Run it

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/analyze_resting.py --db <export>
```

Add `--list` only if the auto-pick (most recent recording) is wrong, or to batch-import historical recordings one at a time.

With no other flags it cleans the RR intervals (physiological gate + 5-beat median filter at 20%), resolves the analysis window from event markers, computes metrics, auto-tags by wall-clock hour, appends to the log, and writes `reading_<pk>_poincare.png`, `reading_<pk>_tachogram.png`, `trend_lnrmssd.png` to `~/Documents/hrv_resting_readings/`.

**Parse the JSON on stdout to report results — don't re-open the CSV.** It carries `recording_pk`, `tag`, `tag_source`, `window_source`, `session_window_min`, `rec_duration_min`, `duration_min`, `hr_mean_bpm`, `sdnn_ms`, `rmssd_ms`, `lnrmssd`, `pnn50_pct`, the `pre`/`post` blocks, the raw `events` list, and plot paths.

Times are already **local wall-clock** (`straplog` applies the offset StrapLog stored with the recording); never tz-convert them again. The `date_utc` / `start_wall` stamps carry a cosmetic UTC label: read them as local.

### 3. Event markers ("diamonds") define the reading

When a recording brackets a protocol with a **start** and an **end** diamond, that marked span *is* the reading. The strap usually runs for minutes either side while you settle in and pack up; folding that in corrupts the result — the trailing tail is where HR climbs as you stand, which inflates SDNN badly while leaving RMSSD alone.

- Start = first marker whose text (label, then note) starts with `start`, `session start`, `begin`; end = last for `end`, `session end`, `stop`, `finish`, and it must fall after the start.
- Both must be present and the span ≥ 1 min, or it falls back to the whole recording.
- **StrapLog overnight recordings** (activity `sleep`) with a wake time end with an automatic 5-min lying reading labelled `resting_start` / `resting_end`; those labels count as start/end, and the reading is dated and tagged by its own start (so `morning`, not the previous evening). Its `pre` block is the whole night.
- `window_source` records which happened: `markers`, `manual`, or `full`. **Always report it** — the same recording yields very different SDNN under `markers` vs `full`, so an unlabelled number is not interpretable.
- **The default is the whole recording; when the markers are ambiguous, ask.** If `events` holds markers but `window_source` is `full` (only one end of the pair, a start inside a `label · note` such as `a · start`, an end before the start, or notes that read like a protocol but match no prefix), don't guess a window: show Richard the markers with their times and ask whether to bound the reading (`--session-window`) or keep the whole recording. Since the run has already appended the row, re-run with the answer; the log de-duplicates on `(recording_pk, timestamp_wall)`.

Leading/trailing segments become **pre/post baselines** (HR, RMSSD, LnRMSSD each); segments under 30 clean beats are `null`. The pre → session → post triple is the point for intervention sessions: a rise that decays back to pre is a within-session response, while no change against an already-high pre baseline is more likely a ceiling effect than a null result. **Always report the pre baseline alongside the session number.**

Overrides: `--markers ignore` (force whole-recording, e.g. to reproduce pre-marker log rows) · `--session-window START_MIN END_MIN` (bound manually, in minutes into the recording, when diamonds are missing or mistimed).

### 4. Tags

Auto-assigned by wall-clock hour: 00–04 `night` · 05–09 `morning` · 10–13 `midday` · 14–17 `afternoon` · 18–23 `evening`. Override with `--tag <choice>` if the user says otherwise.

### 5. Report

Headline metrics inline:

> **Reading PK 12 · 2026-04-15 07:10 morning**
> HR 58 bpm · RMSSD 42 ms · **LnRMSSD 3.74** · SDNN 51 ms · pNN50 18.3%

When `window_source` is `markers` or `manual`, say so and give the triple as a table instead — the deltas are the point:

> **Reading PK 32 · 2026-07-29 13:03 midday · marked window 3.98–19.23 min (15.2 of 25.9 min recorded)**
>
> | | dur | HR | SDNN | RMSSD | LnRMSSD |
> |---|---|---|---|---|---|
> | pre | 4.0 | 59.5 | — | 42.8 | 3.756 |
> | **session** | **15.2** | **62.2** | **72.0** | **38.0** | **3.639** |
> | post | 6.5 | 69.7 | — | 42.2 | 3.742 |

Then contextualize: compare against the trailing 7-day mean **of the same tag** and report the delta. Within the 7-day ±1 SD band = "normal day"; below = stress/fatigue signal; above = good recovery signal. Don't overclaim from one reading — day-to-day HRV is noisy and the signal is in the trend. For N-of-1 comparisons, push for ≥7 readings per period before concluding anything.

Norms and what each metric reflects: `reference/interpretation.md`.

### 6. The plots

- `reading_<pk>_poincare.png` — SD1 (width, beat-to-beat ≈ vagal tone) and SD2 (length, longer-term drift) ellipse metrics inline.
- `reading_<pk>_tachogram.png` — RR over the *whole* recording with the analyzed window shaded, bounds dashed, every diamond labelled. The mean line describes the analyzed window only. Good for spotting movement artifacts, ectopics, non-steady state, mistimed diamonds.
- `trend_lnrmssd.png` — all logged readings, 7-day rolling mean + ±1 SD band, points colored by tag.

## Special modes

- `--no-append` — analyze without writing to the log (exploratory work on historical recordings). It still **overwrites** `reading_<pk>_*.png`, so re-run the canonical analysis afterwards if the plots must match the logged row.
- `--trend-only` — skip per-recording analysis, just replot `trend_lnrmssd.png` from the existing log.
- `--window N` — rolling baseline window, default 7 days. Try `--window 28` once there's a month of data.
- `--markers ignore` / `--session-window A B` — see §3.

## Runtime

`uv` (`brew install uv`) — PEP 723 inline deps, so `uv run` auto-installs numpy/pandas and `straplog[plot]` (which brings plotnine) on first use. Python 3.11+ (uv fetches one if needed).

## Reference

- `reference/interpretation.md` — metric norms and what each one reflects.
- `reference/log-format.md` — the CSV's columns, the pre-marker legacy rows, and why `recording_pk` is not a safe join key.
- `reference/troubleshooting.md` — not a StrapLog export, too few beats, wrong tag, SDNN-high/RMSSD-normal, marked-window failures.
