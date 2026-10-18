---
name: hrv-ramp-test
description: Derive personalized HR training zones from a graded treadmill or bike ramp test in a StrapLog SQLite export — sliding-window DFA α1, AeT/VT1/VT2 crossings.
---

# HRV Ramp Test Analyzer

Turns a graded ramp test (treadmill incline or bike power) recorded with StrapLog into personalized HR training zones grounded in DFA α1 — **AeT, VT1, VT2** plus a 5-zone table. StrapLog exports raw RR only, so the script computes α1 from the beats (`straplog.hrv`: artifact filter, DFA α1, sliding windows).

⚠️ **Do not run this on cycling data without saying so.** Richard's cycling α1 sits ~1.5 and never reaches the textbook thresholds it hits on walks — this is UNRESOLVED (see `h10-session` §4). Zones derived from a bike ramp are not trustworthy on the α1 side; anchor them to breathing/RPE instead.

## Workflow

### 1. Locate the export

StrapLog writes one SQLite snapshot per recording (`<YYMMDD-HHMM>-<name>.sqlite`) to iCloud Drive (`~/Library/Mobile Documents/iCloud~com~personalscience~straplog/Documents/`; older ones may be in `~/Downloads/`). `--db` takes a path or a bare export name looked up in that folder; without it, the newest downloaded export is used. `uvx straplog ls --json` lists exports with their activity. Anything that is not a StrapLog snapshot (no `beat` table) is rejected. Current exports hold one recording, older ones every recording on the phone; pick one with `--list` (`pk` is StrapLog's `recording.id`). Markers (`a`/`b`/`c`/`end`/free text) become events, and times are local wall-clock.

### 2. Run it

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/analyze_ramp.py \
  --db <YYMMDD-HHMM>-<name>.sqlite \
  --out ~/Documents/hrv_ramp_analysis/<YYYY-MM-DD>
```

Use a dated output directory so successive tests don't overwrite each other. `--list` prints all recordings (PK, name, start, beats, duration); the script auto-picks the most recent by date, so only reach for `--recording <PK>` when the auto-pick clearly isn't the ramp test (under 10 min, or the user says otherwise).

**Parse the stdout JSON to present results** — don't re-read the markdown. It carries `aet_hr`, `vt1_hr`, `mid_hr`, `vt2_hr`, each with a `_method` field (`interpolated` / `extrapolated` / `not reached`), plus `hr_max`, `n_windows`, and the output file list.

### 3. Report the thresholds

Give the four as a small table, and call out any whose method is `extrapolated` — α1 never actually crossed the target in the 2-min windowed series and the HR was inferred from a linear fit on the ascending ramp:

> **VT2 method = extrapolated**: the α1=0.50 crossing was not cleanly observed in the 2-min sliding windows. Usually the peak effort was too brief to produce a full sub-0.50 window, even though the real-time display (shorter effective window) may have dipped below 0.50. The value is clamped to HRmax.

Then render the zone table from `zone_table.md`:

| Zone | α1 band | What it is |
|---|---|---|
| Z1 – Recovery | > 1.0 | below AeT, no aerobic stimulus |
| Z2 – Aerobic base | 1.0 → 0.75 | true Seiler Z2 (fat-ox / mitochondrial) |
| Z3 – Tempo | 0.75 → 0.60 | moderate / "grey zone" |
| Z4 – Threshold | 0.60 → 0.50 | LT2 / VT2 approach |
| Z5 – VO2max | < 0.50 | above VT2 |

Point at the plots: `crossplot_hr_alpha1.png` (HR vs α1 scatter, colored by time, VT1/VT2 lines — use it to eyeball the crossings) and `timeseries_hr_alpha1.png` (dual-axis HR + α1 with diamond event markers).

### 4. Interpret

- **AeT > 145 bpm** — casual walks under ~140 bpm are below the aerobic threshold and give no training stimulus. Recommend enough hills/pace to sit in the Z2 band (AeT → VT1) for the target duration.
- **VT2 extrapolated and above observed HRmax** — the test didn't push hard enough at the top. Suggest a steeper ramp above HR 150 with a longer held peak.
- **VT1 unexpectedly high (≥90% HRmax)** — either the ramp sat too long in sub-threshold stages (delayed α1 drop) or the user is genuinely very fit. Flag it and suggest a shorter-stage follow-up.
- **A prior test was mentioned** — diff the thresholds and report which moved.

## Runtime

`uv` (`brew install uv`) — PEP 723 inline deps, so `uv run` auto-installs numpy/pandas and `straplog[plot]` (which brings plotnine) on first use. Python 3.11+ (uv fetches one if needed).

## Reference

- `reference/schema.md` — where the StrapLog tables the script reads (`recording`, `beat`, `event`) are defined, and how they're loaded.
- `reference/troubleshooting.md` — not a StrapLog export, short recording, wrong auto-pick, VT1 == VT2, and what each output file contains.
