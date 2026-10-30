---
name: baroshift
description: BaroShift paced-breathing sessions — find them in a StrapLog H10 recording (overnight or daytime) by R-R spectral lock, measure pace, lock and HR/HRV before→after, and keep a longitudinal log that also takes app-only sessions and the app's scores (from screenshots). Triggers - baroshift, breathwork session, resonance breathing, "I did a BaroShift cycle".
---

# BaroShift sessions

BaroShift is a phone-guided slow-breathing device. This skill answers two questions per session: **did the heart follow the pace** (R-R spectrum), and **what changed before → after** (HR, RMSSD). It keeps one log, `Health2026/Data/baroshift.csv` (override with `BAROSHIFT_LOG`), with one row per session, H10 or not.

## 1. With an H10 recording

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/baroshift.py analyze --db <export> \
    [--program sleep] [--context "overnight, after waking"] [--log] [--out <scratch dir>]
```

- `--db` takes a path or a bare export name (iCloud StrapLog folder); the default is the newest export. It works on any recording: an overnight strip, a seated daytime session, or one inside a longer recording.
- **How windows are found** (`window_source`; always report it):
  - `manual`: `--window HH:MM-HH:MM` (repeatable) overrides everything.
  - `markers`: a `start …` marker followed by an `end …` marker.
  - `detected+end`: the sliding 3-min spectrum found a locked run, and an `end …` marker inside it (or up to 3 min after it) set the end. Several end markers split one run into sessions.
  - `detected`: scan only. It's kept only if the whole window's on-pace share is ≥ 0.5; weaker runs go in `rejected` (REM throws up brief slow peaks).
- The pace band defaults to 4.5–7.5 breaths/min (`--pace-min/--pace-max`). Widen it if a program paces outside that.
- Parse the stdout JSON; `--out` adds a plot (HR plus the on-pace share trace, sessions shaded, markers drawn) and a summary table. The out dir is scratch: no plots in the vault.

**Metrics per session:** `breath_rate_bpm` (spectral peak × 60) · `peak_share` (power within ±0.015 Hz of the peak ÷ 0.04–0.4 Hz power; how purely the heart followed one rhythm) · `lock_pct` (% of sliding windows locked) · `hr_osc_bpm` (HR swing at the breathing frequency, peak-to-trough) · HR and RMSSD for `pre` (5 min before), `session`, `post` (5 min after) and `post2` (5–15 min after), plus the first and last 2-min HR.

**Reading it:**
- 🛑 RMSSD/SDNN *during* paced breathing are produced by the breathing, so they're not a vagal-tone readout. Compare pre → post and post2.
- A falling HR after a session is uncontrolled. HR also falls after any wake bout or after sitting down, so state the confound and the n. Label every effect "hypothesis, not finding" until there are matched sessions with and without the device.
- Overnight, `pre` often contains the wake bout that prompted the session; say so.
- Mark `start BaroShift` / `end BaroShift` when you can. Detected starts are accurate to about ±30 s.

## 2. App-only sessions and app scores

The app's Session Summary (screenshots for now) gives Training Score and BaroCapacity (0–100) plus four bars: Session Focus, Breathing Mechanics, Reflex Response, Heart Dynamics. Read the bars as a 0–1 fraction of the track; they're approximate (`scores_source=screenshot`).

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/baroshift.py log --date 2026-10-30 --end 02:11 \
    --training-score 94 --barocapacity 71 --focus 0.99 --mechanics 0.87 --reflex 0.56 --heart-dynamics 0.87
```

`log` merges into an existing row with the same date and an end within 3 min (an H10 session already analysed); otherwise it adds an app-only row (`h10=n`). Use the screenshot's clock as the end time. Add `--program`, `--context`, `--start`, `--duration-min`, `--notes` when known.

## 3. Filing

- Vault: the CSV row is the record. Mention the session in that day's note: the night note `SleepHRV-YYMMDD` for overnight sessions, or the session note.
- `events.csv`: one `other` row per BaroShift occasion (the vocabulary has no breathwork type).
