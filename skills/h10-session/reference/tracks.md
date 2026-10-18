# h10-session — companion GPX/FIT tracks

Read this only when a track fails to merge, `overlap_min` is near zero, `gps` is unexpectedly `false`, or an app writes an HR tag the parser misses. Otherwise: pass the track, read the JSON, move on.

## Vendor-neutral by design

Any app that writes GPX or FIT works — Strava, MyFitnessPal, BikeGPX, OsmAnd, Garmin, Wahoo, Komoot, Ride with GPS. The parser **ignores XML namespaces and matches on tag name**, so every heart-rate convention in the wild is handled:

- Garmin `gpxtpx:hr` (TrackPointExtension v1 and v2)
- Cluetrust `gpxdata:hr`
- a bare `<hr>`
- `<heartrate>`

…under either GPX 1.0 or 1.1. Verified against all four. If a future app uses some other spelling, add it to `HR_TAGS` in `scripts/analyze_h10.py` — that is a one-line change.

## FIT records are interleaved

Some FIT messages carry GPS only, others heart rate only. The script timestamps everything *before* splitting the channels, so HR survives the split.

**Prefer the GPX when you have both.** Its elevation is pre-smoothed by the exporter; raw FIT barometric altitude is noisy enough to inflate total climb several-fold if summed naively, so the script median-filters it.

## Indoor FIT files carry HR and NO GPS at all

A Strava strength-training export reports `total_distance: 0` and not one position record. That used to make `merge_track` bail and silently discard the entire HR channel — killing the sensor reconciliation that is the *only* reason to pass a track for an indoor lift. **Fixed 2026-08-19.** Such a track now reports `"gps": false` and still yields HR reconciliation, with no GPS section.

So: pass the `.fit` for gym sessions. Strava has no GPX to give you there.

## Alignment

GPX/FIT timestamps are genuinely UTC, unlike the RR times, which are already local (see SKILL.md § Timestamps). Alignment shifts the **track**, never the RR data — that is what `--utc-offset` controls (`-7` = PDT, `-8` = PST).

Sanity check: the track's local start should sit within a few minutes of the RR start. The JSON reports `overlap_min`; near zero means misalignment, not a bad ride.

## `hr_reconciliation` verdict codes

stdout carries the code; `summary.md` spells it out.

| Code | Threshold | Meaning |
|---|---|---|
| `agree` | r ≥ 0.8 and MAE ≤ 6 bpm | the strap was detecting beats correctly; HR-derived metrics are trustworthy |
| `partial` | r ≥ 0.6 | some divergence — check for dropouts before trusting drift or HRR |
| `disagree` | r < 0.6 | the two recordings tell different stories; treat HR metrics as unreliable, not just α1 |

Note what this can and cannot settle: means typically agree to ~2 bpm while **maxima agree on nothing**. A second sensor *impeaches* a peak, it does not establish one — see the `reference_second_sensor_impeaches_peaks` memory. Quote the average, not either max.

If there is no overlap at all (`overlap_min` near zero), the reconciliation block is absent — that is a `--utc-offset` problem, not a bad ride.
