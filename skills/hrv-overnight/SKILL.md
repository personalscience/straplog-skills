---
name: hrv-overnight
description: Analyze an overnight sleep strip from a StrapLog SQLite export against Apple Watch staging — coverage, time-domain HRV, block trajectory, sleep architecture.
---

# Overnight HRV Analyzer

Turns one overnight StrapLog strip — plus, when available, the morning
Apple Health pull — into a filed per-night analysis. This replaces the
per-night one-off `analyze_overnight.py` that used to be hand-written into
`~/Downloads/hrv<YYMMDD>/` every time. **Do not write a new script; run this
one.** If it genuinely cannot answer something, extend it here so the next
night inherits the fix.

## Workflow

### 1. Find the strip

StrapLog exports one SQLite snapshot per recording (`<YYMMDD-HHMM>-<name>.sqlite`) to iCloud Drive (`~/Library/Mobile Documents/iCloud~com~personalscience~straplog/Documents/`; older ones may be in `~/Downloads/`). `--db` takes a path or a bare export name (looked up in that folder); without it, the newest downloaded export there is used. Anything that is not a StrapLog snapshot (no `beat` table) is rejected. Current exports hold one recording, older ones every recording on the phone; pick one with `--list` (`pk` is StrapLog's `recording.id`). Loading and time-domain HRV come from the `straplog` package on PyPI, declared in the script's inline metadata; `uvx straplog ls --json` lists exports with their activity.

```bash
${CLAUDE_SKILL_DIR}/scripts/analyze_overnight.py --db 260726-2245-sleep.sqlite --list
```

Newest first, capped at 8 (`-n N`, `-n 0` for all). Each row carries
`looks_overnight`: true when the name contains a sleep keyword, **or** the
strip is ≥180 min *and* starts between 20:00 and 03:00. The clock-hour
condition is load-bearing — a 4-hour bike ride is long too. With no
`--recording`, the most recent `looks_overnight` strip is used.

⚠️ **An older export can hold several recordings, sometimes two consecutive
nights.** Check `--list` before assuming the newest is the one you want.

### 2. Run it

```bash
${CLAUDE_SKILL_DIR}/scripts/analyze_overnight.py \
  --db 260726-2245-sleep.sqlite \
  --health ~/Downloads/hrv260726/applehealth_*.json \
  --out ~/Downloads/hrv260726/overnight
```

`--health` is optional and the run degrades cleanly without it (you lose
staging and the awake-window reconciliation, keep all H10 autonomics). A glob
is accepted. A missing file is reported in `health_warning`, not fatal.

Outputs to `--out`: `summary.md`, `summary.json`, `night_trajectory.png`,
`tachogram.png`. **Slim JSON goes to stdout** — the 30-min block table is
omitted there (it is in `summary.json` and `summary.md`) to keep stdout small.
Read the stdout JSON *or* `summary.md`, not both.

### 3. Check capture quality BEFORE interpreting anything

- **`coverage_pct`** — captured time ÷ wall-clock span, gaps >15 s excluded.
- **`gaps`** — every dropout >15 s, with clock times.

A gap is a Bluetooth dropout: the H10 sent no RR for that stretch. **Any
metric spanning a gap is meaningless** — say so rather than reporting it. The
2-min extremes are already computed inside continuous segments only, so a
dropout cannot fabricate a peak-vagal or HR-floor value.

### 4. 🛑 Provenance gates — these decide what may be said

When `--health` is passed, the script reads the pull time and sets:

- **`staging_stable`** — false when the pull ran <2 h after final wake. Sleep
  *staging* has been stable on every re-pull past T+2 h, so this is usually a
  soft flag; mark the note 🟡 PROVISIONAL and re-pull to clear it.
- **`rhr_trustworthy`** — false unless the export is `complete_day` or ≥18 h
  after wake. 🛑 **Never build an interpretation on a same-day resting HR.**
  Apple's RHR window stays open until end of day and drifts substantially —
  confirmed three times (Jul 18, Jul 22, Jul 25 2026; Jul 22 went 59 → 64 →
  **55 final**, and the 64 had to be withdrawn from `Sleep.md`). Mark
  same-day values `*` and `PROVISIONAL`. The same applies to steps /
  active_kcal / exercise_min on a partial-day export.

### 5. Read the awake-window reconciliation

For each Watch-staged awake window the script reports H10 mean HR and range
over the same clock minutes. **An HR bump above sleeping baseline is
independent evidence the WASO is genuine wake, not staging noise.** A window
with no bump is worth calling out — either the staging is wrong or the strap
lost contact.

The awake-window mean HR is `60000 / mean(RR)`, **not** `mean(60000 / RR)`.
The two differ by 1.5–3 bpm on a wake-window bump, and every previously filed
night used the former — don't "fix" this into incomparability with the series.
(Whole-strip and 30-min-block `mean_hr` come from `straplog.hrv.time_domain`
and *are* the mean of beat HRs, 0.2–1.7 bpm above the old figure; SDNN is the
sample SD, ddof=1. Both changed when this skill moved to the shared package,
2026-10-18 — mind the step when comparing with earlier filed nights.)

### 6. File it

Per the `Health2026/CLAUDE.md` router:

- Folder `Sleep/HRV Reference/SleepHRV-YYMMDD/` — **YYMMDD is the morning
  wakeup date**, the day Rik rose, not the night he went to bed. This is
  universal for every overnight artifact.
- `SleepHRV-YYMMDD.md` inside it — **the note's basename repeats the folder name; never `analysis.md`** (duplicate `analysis.md` basenames broke plain wikilinks; all renamed folder-wise 2026-07-27). The narrative: architecture, phase-by-phase autonomics, dietary/caffeine/alcohol context, and the reconciliation table.
- Copy the StrapLog `.sqlite` export and the Apple Health JSON alongside, so
  the folder is self-contained.
- Copy the PNGs alongside.
- One row at the top of the `Sleep/Sleep.md` 30-day table.
- Times are **local** — label them `HH:MM PDT`.

A daytime supine/resting reading is **not** an overnight: it uses the date it
was taken with a `-waking` suffix, and `hrv-resting` is the right skill.

## Timestamps — the one thing that is easy to get wrong

StrapLog stores UTC plus the phone's `tz_offset_s` at recording time;
`straplog.db` applies that offset, so every RR time is **local wall-clock**.
Never apply a second offset. Apple Health JSON carries explicit ISO offsets; the script keeps the wall-clock face value
so both land on the same clock. Sanity check: a decoded strip should start in
the evening and end in the morning, and the Watch awake windows should fall
inside the H10 HR bumps.

## What the metrics mean

| Metric | Read it as |
|---|---|
| **RMSSD / LnRMSSD** | vagal (parasympathetic) tone. Trend LnRMSSD; raw RMSSD is skewed |
| **SDNN** over a whole strip | dominated by circadian and respiratory swing, so it runs far above a short-reading SDNN — **not comparable across durations** |
| **HR floor (2-min)** | the night's deepest cardiac quiescence; a cleaner recovery marker than mean HR because it ignores wake bouts |
| **Peak vagal LnRMSSD (2-min)** | the strongest parasympathetic moment, usually inside an SWS-dominant cycle |
| **30-min blocks** | the trajectory — look for a sustained low-vagal stretch and check whether it coincides with a staged awakening or with N3 |

Watch **SDNN is not interchangeable** with Polar H10 / Kubios RMSSD. Direction only, never the same quantity. Apple's SDNN is also thin-sampled (n=3–4/night) — see the `reference_apple_sdnn_thin_sampling` memory.

## Runtime

`uv` (`brew install uv`) — PEP 723 inline dependencies (numpy, pandas,
`straplog[plot]`, which brings plotnine), auto-installed on first use. The
chart style is `straplog.plot`, shared with the other StrapLog skills.

## Troubleshooting

Strip not detected, not a StrapLog export, low coverage, awake windows with no HR bump: `reference/troubleshooting.md`.
