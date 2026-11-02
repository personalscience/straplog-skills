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
  --db 260726-2245-sleep.sqlite --recording 24 \
  --health auto \
  --out ~/Downloads/hrv260726/overnight
```

**`summary.json` → `night`** holds the morning-read figures: artifact-cleaned 2-min min HR, min and max RMSSD with their times, limited to the sleep window (Watch onset to final wake, else the markers), plus HR and RMSSD for the 2 min either side of each marker. Check `window_source`: the marker window can run past the real wake, so a wake bout can set its trough (261102: 3.7 ms at 04:09 with markers, 8.0 ms at 21:47 with the Watch).

**`--health auto` fetches the Watch staging itself** from the Health Auto
Export app's server on the iPhone (endpoint and token in
`~/.config/straplog/health.toml`, or `HEALTH_AUTO_EXPORT_URL` / `_TOKEN`) and
writes `applehealth_<YYMMDD>.json` into `--out`. **Don't call the
health-auto-export MCP tools and don't build that JSON by hand**: it costs
a dozen turns and the script does it identically. If the fetch fails (app
closed, phone off the LAN), the run still completes and `health_warning`
says so. Ask Rik to open the app, then re-run. `--health <path-or-glob>` still
takes a saved JSON (re-runs, old nights).

Outputs to `--out`: `summary.md`, `summary.json`, `applehealth_*.json`,
`night_trajectory.png`, `tachogram.png`. **Slim JSON goes to stdout** (the
30-min block table is only in `summary.json` / `summary.md`). Read the stdout
JSON *or* `summary.md`, not both, and don't grep the script. The stdout keys:

| Key | Holds |
|---|---|
| `coverage_pct`, `gaps` | capture quality (step 3) |
| `whole_strip` | `mean_hr`, `rmssd`, `ln_rmssd`, `sdnn`, `pnn50`, `ln_rmssd_clean` |
| `extremes` | raw 2-min `peak_vagal_ln_rmssd`/`_at`, `hr_floor_bpm`/`_at`; `trough_vagal_ln_rmssd`/`_at` (full windows only); the same with `_clean` after the artifact filter; `artifact_beats` |
| `sleep` | Watch architecture, `staging_stable`, `rhr_trustworthy`, `hours_after_wake`, RHR, `watch_sdnn` |
| `awake_reconciliation` | per Watch awake window: H10 mean HR, range, bump vs baseline |
| `night` | morning-read figures over the sleep window, marker pre/post 2 min (above) |
| `h10_sleep` | H10-only onset, wake bouts, WASO, TST (`tst_min`, `tst_max_min` if dropouts were sleep), efficiency; always present |
| `h10_sleep_vs_watch` | with Watch data: Watch vs H10 TST over the same window, epoch agreement, % of Watch wake caught |

**Raw vs clean extremes.** Raw values continue the filed series. When the raw
peak vagal value is well above `_clean` (night 261018: 5.19 at 02:22 raw vs 4.24
at 00:38 clean), the raw one sits on ectopic or missed beats. Report the clean
one and say why. No ad-hoc RR re-analysis is needed.

### 3. Check capture quality BEFORE interpreting anything

- **`coverage_pct`** — captured time ÷ wall-clock span, gaps >15 s excluded.
- **`gaps`** — every dropout >15 s, with clock times.

A gap is a Bluetooth dropout: the H10 sent no RR for that stretch. **Any
metric spanning a gap is meaningless** — say so rather than reporting it. The
2-min extremes are already computed inside continuous segments only, so a
dropout cannot fabricate a peak-vagal or HR-floor value.

### 4. 🛑 Provenance gates — these decide what may be said

With Watch data, the script reads the pull time and sets:

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

### 5b. Read the stage reconciliation

`stage_reconciliation` (in `summary.json`, stdout and a `summary.md` table) has
H10 mean HR, RMSSD and LnRMSSD inside **every** Watch epoch (`epochs`), plus a
`by_stage` rollup pooling epochs ≥3 min. Beats are artifact-cleaned, and RMSSD
never bridges an epoch boundary or a dropout; shorter epochs report HR only
(`note: "short"`). Mean HR uses the awake-window convention above.

Use it to ask whether the Watch's labels hold up: N3 is expected to carry the
night's lowest HR and more vagal tone than REM. **A Deep rollup that matches
Core is weak support for the Watch's deep figure; it is not proof the staging
is wrong.** Compare within the night, never across nights. The late-night HR
trough lowers whatever stage sits in it, so check a Deep epoch against its
Core neighbours, not against the night mean. Across 261018–261020, REM separated
clearly (higher HR, lower LnRMSSD on 2 of 3 nights) and Deep never separated
from Core.

### 5c. H10-only sleep estimate (`h10_sleep`)

Every run estimates sleep from the H10 alone. The window runs from the first `lights_out` diamond to the last `up`/`awake` one, falling back to the strip edges. Wake is HR ≥8 bpm above a rolling ±30-min sleeping baseline (the 20th percentile) for at least 1 min. Onset is the first 10 minutes with no wake. Dropout epochs are unknown, never sleep: `tst_min` excludes them, and `tst_max_min` counts them as sleep.

**Use it only when the Watch has no staging** (dead battery, Watch off). When Watch data exists, the Watch's TST stays the number of record, and `h10_sleep_vs_watch` keeps scoring the detector each night. Calibration (6 nights, wake dates 2026-10-18 → 10-24): TST was within ±23 min of the Watch on average (range −36 to +55 min) and onset within 7 min. Bout-by-bout agreement was only ~45%, since quiet wake is invisible to HR and REM surges can look like wake. Quote it as "H10 estimate ~X h", never as a measured TST.

### 6. File it

Per the `Health2026/CLAUDE.md` router:

**Run `Health2026/tools/file_h10.py sleep <out_dir>` when it exists.** It does everything below except the narrative, and leaves TODO markers for you to fill.

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
