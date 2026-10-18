# h10-session — why the defaults are what they are

Read this only when a number here is disputed, or when an older note/command line contradicts the current defaults. The *rules* are already in SKILL.md; this file is the evidence behind them.

## HR bands: the corrected table (adopted 2026-07-18)

Defaults are **Z2 `120-145`, Z3 `145-158`**, anchored to the talk-test VT1 of ~145 adopted in `Cycling Ride 2026-07-18`.

The old inferred pair — `115-135` / `136-150` — was **retired there**. It sat 10–15 bpm too low, under-reported Z2, and split real continuous blocks in half. On the 2026-07-26 ride the same data read **34.9 min** longest continuous Z2 under the corrected bands versus **19.5 min** under the old ones. If a note or an older command line still says `115-135`, that is stale; do not copy it forward.

## A one-sample HR peak is an artifact, not a max effort

Always check how long a peak was *held* before reporting it.

- **2026-07-26**: `hr_max` read 183, but only **1 second** exceeded 170 and **8 seconds** exceeded 155. True peak ~155.
- **2026-07-18**: the H10's 167 was contradicted by Strava's independent trace at 157.

A genuine max leaves a plateau. Grep `hr_1hz.csv` for seconds-above-threshold before quoting `hr_max`. (See also the `reference_second_sensor_impeaches_peaks` memory: a second sensor impeaches peaks, it does not establish them.)

## Note naming: the 2026-07-27 rename

Duplicate `analysis.md` basenames across ~38 session folders broke plain wikilinks and forced full-subpath links. They were all renamed folder-wise on 2026-07-27, so the note's basename always repeats its folder name: `Ravine Walk 260724/Ravine Walk 260724.md`, never `Ravine Walk 260724/analysis.md`. Keep it that way so `[[Ravine Walk 260724]]` resolves without a subpath.

## Mode auto-detection: why the folder rule is gated on a blank name

The recording name is often **blank** when a recording was never named in the app (the 2026-07-26 HRV Logger ride was). Detection order: name keyword → enclosing folder name *only if the name is blank* → duration/HR shape (≥90 min **and** ≥105 bpm ⇒ endurance).

The blank-name gate on the folder rule is deliberate: the DB in `~/Downloads/cycling260726/` also holds `SLEEP260722-23`, which must not inherit "cycl" from the folder. Anything unresolved stays `generic` rather than guessing.

## Runtime: the fitparse dependency

`.fit` works out of the box as of **2026-08-19**, when `fitparse` was added to the PEP 723 inline dependency block. Before that it had to be supplied by hand with `uv run --with fitparse`; without it the script warned and skipped the track entirely.

## The |ΔRR| false-alarm that cost a dataset (2026-07-18)

Never flag over-smoothing from the *level* of |ΔRR| alone, and never compare |ΔRR| across sessions of different intensity. Low |ΔRR| is mostly just hard exercise (vagal withdrawal). What matters is the *slope* — `variability_tracks_intensity`. This exact error invalidated a real dataset's interpretation on 2026-07-18. Full argument in `alpha1-gate.md`.
