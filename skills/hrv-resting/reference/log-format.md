# hrv-resting — the longitudinal log

The longitudinal CSV at `~/Documents/hrv_resting_log.csv` has columns:

```
timestamp_wall, tag, recording_pk, recording_name,
duration_min, n_beats_raw, n_beats_clean,
hr_mean, hr_sd, sdnn_ms, rmssd_ms, lnrmssd, pnn50_pct, sdsd_ms,
window_source, session_start_min, session_end_min, rec_duration_min,
pre_min, pre_hr, pre_rmssd, pre_lnrmssd,
post_min, post_hr, post_rmssd, post_lnrmssd
```

The headline metric columns (`hr_mean` … `sdsd_ms`) describe the **analyzed window**, and `duration_min` is that window's length — `rec_duration_min` is the full strip. On rows written before marker support the trailing twelve columns are blank, and `duration_min` there means the whole recording; treat blank `window_source` as `full`.

`timestamp_wall` is always the start of the *recording*, not of the marked window, so it remains a stable dedup key across re-analysis.

It's a plain CSV — safe to open in a spreadsheet, script against, or import into a dashboard tool. De-duplication is keyed on `(recording_pk, timestamp_wall)` — re-running the same recording won't create duplicate rows.
