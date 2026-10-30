---
name: h10
description: Route a Polar H10 / StrapLog recording to the right HRV skill by its activity — "analyse last night", "this morning's ruck", "my latest H10 recording". Lists exports with the straplog CLI, then dispatches to hrv-overnight, hrv-resting, h10-session or hrv-ramp-test.
---

# H10 router

Use this when the request names a recording by when or what ("last night", "this
morning's reading", "today's ride") rather than by file. It picks the export and
the skill; the analysis itself belongs to the target skill. **Never analyse here.**

## 1. Find the recording

```
straplog ls --json [--activity sleep] [--since 2d] [--limit 5]
```

(`uvx straplog …` works without installing; `uv tool install straplog` puts
it on PATH.) It reads `index.json` in the StrapLog iCloud folder and scans any
exports the index lacks, newest first. Each row has `file`, `path`, `id`,
`name`, `activity`, `started_local`, `duration_ms`, `beats`, `coverage`,
`labels` (marker labels in order) and `downloaded`. If `downloaded` is false,
the file is an evicted iCloud placeholder: ask Rik to open the StrapLog folder
in Files (or `brctl download <path>`) before going on.

Pick the row that matches the request; if two plausibly match, ask. Pass
`--db <path> --recording <id>` to the target skill (its `pk` is this `id`).

## 2. Dispatch on `activity`

| activity | skill | notes |
|---|---|---|
| `sleep` | **hrv-overnight**, then **hrv-resting** if `labels` has `resting_start` | One sleep export serves both: overnight for the night, resting for the 5-min morning reading the app marked after the wake time. Report the two separately. |
| `other` | decide from `name` and `labels` (below) | |

`other` covers exports from before schema v3 (no activity recorded, e.g.
`SLEEP 261015`) and anything without a preset. Route by name:

- name says sleep / night / overnight, or duration ≥ 4 h → **hrv-overnight**
- name says rest / resting / morning / HRV, or a seated read under ~15 min → **hrv-resting**
- name says ramp / test → **hrv-ramp-test**
- otherwise (ride, walk, run, ruck, lift, strength, ...) → **h10-session**

Say which rule decided, so a wrong guess is easy to correct.

**BaroShift, on top of any route:** if a marker or the name mentions BaroShift (or Rik says he did a breathing session), also run **baroshift** on the same export. It finds and scores the paced-breathing windows; the main skill still analyses the recording.

## 3. Report

Lead with the recording (`started_local`, name, duration, coverage), then the
target skill's own report format. Data questions that need no analysis
("how long did I sleep with the strap on?", "what markers did I add?") are
answered from `straplog ls --json` / `straplog events <file> --markers --json`
directly.

The CLI's other commands: `straplog rr <file>` (CSV `t_ms,rr_ms`, epoch ms
UTC), `straplog events <file>`, `straplog meta <file>` (schema version, app
version, preset with marker vocabulary). Schema: StrapLog `docs/SCHEMA.md`.
