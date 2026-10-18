# h10-session — filing an analysis

The canonical checklist lives in `Health2026/CLAUDE.md`. This file is the h10-specific detail. SKILL.md carries the one rule you must not get wrong (the naming rule); everything below is the rest of it.

## Naming (the rule that bites)

**The note's basename always repeats its folder name; never `analysis.md`.** `Ravine Walk 260724/Ravine Walk 260724.md`, not `Ravine Walk 260724/analysis.md`. See `history.md` for the 2026-07-27 rename that established this.

## Endurance sessions

- Note in `Fitness/Cycling/` as `Cycling Ride YYYY-MM-DD.md`, or the relevant activity folder, following the naming rule above.
- Copy `session.png` and `hr_alpha1.png` alongside the note.
- Cross-link note ↔ index in **both** directions.

## Strength sessions

- Note: `Strength Session YYYY-MM-DD` in `Fitness/Strength/`.
- Update **both** `Fitness.md` tables — the 30-day workout log AND the recovery markers.
- Update the H10-instrumented session count in the narrative.
- Add a row to `~/Documents/polar_strength_log.csv`.
- Confirm the set table with Richard before filing (mapping is greedy first-N — see SKILL.md § 5).

## Times

Times are **local** — label them `HH:MM PDT` (or PST in winter). Never tz-convert the RR timestamps again; see SKILL.md § Timestamps.
