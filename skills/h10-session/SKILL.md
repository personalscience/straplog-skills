---
name: h10-session
description: Analyze a workout (ride, walk, run, hike, lift) from a StrapLog SQLite export — HR, %HRR zones, DFA α1, drift, per-set peaks, optional GPX/FIT overlay.
---

# Polar H10 Session Analyzer

Turns one StrapLog recording into a filed analysis. Richard wears a Polar H10 and records with StrapLog, which exports one SQLite snapshot per recording (`<YYMMDD-HHMM>-<name>.sqlite`) to iCloud Drive (`~/Library/Mobile Documents/iCloud~com~personalscience~straplog/Documents/`; older ones may be in `~/Downloads/`). For rides he often also exports a **Strava GPX** of the same session. It reads the SQLite directly in every mode — no conversion step. Loading and the HRV algorithms (artifact filter, DFA α1, windowed features) come from the `straplog` package on PyPI, declared in the script's inline metadata.

## Modes

Auto-detected from the recording name; override with `--mode`. Detection order is name keyword → folder name (only if the name is blank) → duration/HR shape; unresolved stays `generic`. Check `guessed_mode` and pass `--mode` if it looks wrong.

| Mode | Name contains | Adds |
|---|---|---|
| `endurance` | bike, cycl, ride, run, walk, hike, row, swim | cardiac drift, HR/α1 decoupling, longest-continuous-time-in-band, GPS overlay |
| `strength` | strength, lift, weights, gym, rt | per-set HR peaks, set mapping, HRR60/120/180 |
| `generic` | anything else | HR summary, zones, α1 only |

## Workflow

### 1. Find the recording

`--db` takes a path or a bare export name (looked up in the iCloud folder); without it, the newest downloaded export there is used. `uvx straplog ls --json` lists exports with their activity. Anything that is not a StrapLog snapshot (no `beat` table) is rejected. Current exports hold one recording, older ones every recording on the phone; pick one with `--list` (`pk` is StrapLog's `recording.id`). Markers (`a`/`b`/`c`/`end`/free text) become events.

```bash
${CLAUDE_SKILL_DIR}/scripts/analyze_h10.py --db 261018-0730-RIDE.sqlite --list
```

Returns JSON: `pk`, `name`, `start_local`, `n_beats`, `duration_min`, `avg_hr`, `coverage`, `guessed_mode` — **newest first, capped at 8** (`-n N` to change, `-n 0` for all). The most recent recording is the default if you don't pass `--recording`.

**`coverage`** is the share of the recording not lost to Bluetooth silence (StrapLog counts notification gaps > 3 s). Below ~0.90 means a dropout mid-recording. Investigate the gap before trusting drift or HRR.

### 2. Run it

```bash
# Endurance ride with a Strava track
${CLAUDE_SKILL_DIR}/scripts/analyze_h10.py \
  --db 260718-0912-RIDE.sqlite \
  --track ~/Downloads/ride.gpx \
  --out ~/Downloads/cycling260718/h10_analysis

# Strength session, HRR anchored to sitting onset after the `end` diamond
${CLAUDE_SKILL_DIR}/scripts/analyze_h10.py \
  --db 260718-1730-STRENGTH.sqlite \
  --mode strength --sets workout.txt --rest-marker
```

Useful flags: `--hrmax` / `--hrrest` (default **182 / 57**, Richard's audited anchors), `--warmup N` (minutes excluded from drift, default 15), `--band LO-HI` (repeatable target band), `--utc-offset` (default `-7` = PDT; use `-8` in winter), `--prominence` / `--min-gap` for peak sensitivity.

**Bands default to the corrected table: Z2 `120-145`, Z3 `145-158`.** If a note or an older command line says `115-135` / `136-150`, that is stale — do not copy it. Why: `reference/history.md`.

Outputs to `--out`: `summary.md`, `session.png`, `hr_alpha1.png`, `hr_1hz.csv`, `windowed_features.csv`. Full JSON goes to stdout — **parse that first**, and read the JSON *or* `summary.md`, never both (~80% redundant). The two zone blocks use different units: `zones` is **seconds**, `alpha1_zones` counts **2-min windows**. Don't load the PNGs unless you need to look at the curve.

**A one-sample HR peak is an artifact, not a max effort.** Check how long the peak was *held* before reporting it — grep `hr_1hz.csv` for seconds-above-threshold before quoting `hr_max`. A genuine max leaves a plateau. Cases: `reference/history.md`.

### 3. Companion tracks — always pass one if it exists

Any app that writes GPX or FIT works (Strava, Garmin, Wahoo, Komoot, OsmAnd, …); the parser matches HR tags by name across namespaces. **Prefer the GPX outdoors** (pre-smoothed elevation), and **pass the `.fit` for gym sessions** — indoor FIT carries HR and no GPS, reports `"gps": false`, and still yields sensor reconciliation, which is the only reason to pass a track for a lift.

Read `reference/tracks.md` only if the track fails to merge, `overlap_min` is near zero, or you need the `hr_reconciliation` verdict codes.

### 4. Check the DFA α1 quality gate before interpreting α1 (CRITICAL)

The script decides this for you. Read `rr_quality` in the JSON:

- **`alpha1_trustworthy: true` and `flags: []`** → the capture is clean. Report α1's shape and range and move on. **Do not read the reference file.**
- **Any flag set, or someone disputes an α1 result** → read `reference/alpha1-gate.md` for the full argument (what discriminates a bad strap from hard exercise, the |ΔRR| false-alarm trap, the CSV cross-check).

Two rules that apply even when the gate passes, because they bite constantly:

1. **Never flag over-smoothing from the level of |ΔRR| alone, and never compare |ΔRR| across sessions of different intensity.** Low |ΔRR| is mostly just hard exercise (vagal withdrawal). What matters is the *slope* — `variability_tracks_intensity` — not the level.
2. ⚠️ **Richard's cycling α1 sits ~1.5 and this is UNRESOLVED.** On the bike α1 never reaches the textbook thresholds it hits on walks. So **do not apply the 0.75 = VT1 threshold to cycling data**, and never report "N% of windows sub-AeT" from a ride as a physiological claim. Use α1's shape and the decoupling number; anchor bike zones to breathing/RPE instead.

`alpha1_zones` keys are bare ranges. Labels: `>1.00` aerobic/below AeT · `0.75-1.00` around AeT/VT1 · `0.50-0.75` tempo, above VT1 · `<0.50` above VT2.

### 5. Reconcile strength set-mapping with Richard (CRITICAL)

**With diamonds, stations come from the markers.** JSON `markers` lists every diamond (time + note); `stations` (and the summary's Stations table) segments marker to marker up to the `end`/`rest`/`cool down` diamond: duration, mean, 1-Hz peak, seconds held within 3 bpm, %HRR. A bare `-NNN` is a load step on the previous station (rows `1a`/`1b`/`1c`); diamonds after `end` (`Car ride`, `Home`) are not stations. Windows include rests. Peaks are labelled by the station window they fall in. The analyzer does not fix typos in notes (`Abdominal 200` for 100): confirm loads with Richard.

**Without diamonds**, mapping is **greedy first-N** from `--sets`, so mid-session transitions create extra peaks and shift every later label; confirm the table with Richard before filing.

**HRR anchor.** `--rest-marker` anchors to the **sitting onset after the `end` diamond**: the last high point before the first sustained fall (HR ≥10 bpm lower throughout the next 45–75 s), because Richard usually walks to the car first. No fall within 5 min → the `end` diamond; no `end` → the last diamond (right when it is `car home`, wrong when it is `Home`). Without `--rest-marker`, the anchor is the last detected peak, usually wrong for machine circuits. `hrr.anchor_time` and `hrr.anchor_method` say which was used; anchor and endpoint HR are ±2 s means. Sanity-check the anchor on the plot. `sets` in the JSON is capped at 12; `summary.md` always holds the full table.

### 6. File it

**Naming — the note's basename always repeats its folder name; never `analysis.md`.** `Ravine Walk 260724/Ravine Walk 260724.md`, so `[[Ravine Walk 260724]]` resolves without a subpath. Times are **local** — label them `HH:MM PDT`.

Endurance → `Fitness/Cycling/` (or the activity folder) with the PNGs alongside. Strength → `Fitness/Strength/`, **both** `Fitness.md` tables, and a row in `~/Documents/polar_strength_log.csv`. Full checklist: `reference/filing.md` and `Health2026/CLAUDE.md`.

## Timestamps — the one thing that is easy to get wrong

StrapLog stores UTC plus the phone's `tz_offset_s` at recording time; `straplog.db` applies that offset, so every RR time the script reports is **local wall-clock**. Never apply a second offset.

GPX/FIT tracks are the opposite — genuinely UTC. So track alignment shifts the *track*, never the RR data; that's what `--utc-offset` controls. Sanity check: a decoded workout should land in waking hours, and the track's local start should sit within a few minutes of the RR start.

## What the endurance metrics mean

| Metric | Read it as |
|---|---|
| **Cardiac drift** (1st vs 2nd half HR, warmup excluded) | <5% at constant output = good aerobic durability; >10% = glycogen depletion, heat, or dehydration. Richard's strength series runs +3 to +16 bpm |
| **α1 decoupling** (1st vs 2nd half α1) | falling α1 at constant HR is the fatigue signature — only meaningful if the quality gate passed |
| **Longest continuous time in band** | the honest answer to "did I bank 45+ continuous min of true Z2?" — total time in band inflates easily with scattered seconds, so lead with the longest *block* |
| **Speed↔HR correlation** | on a flat rail-trail, low r means HR was driven by something other than output (heat, fatigue, drift) |

## Runtime

`uv` (`brew install uv`) — PEP 723 inline dependencies, so `uv run` auto-installs numpy/pandas/fitparse and `straplog[plot]` (which brings plotnine) on first use. The script is self-executable; `.fit` works out of the box.

## Troubleshooting

Failures and their causes (not a StrapLog export, low `coverage`, α1 gate failing on rides, track/RR non-overlap, 27 "sets"): `reference/troubleshooting.md`.
