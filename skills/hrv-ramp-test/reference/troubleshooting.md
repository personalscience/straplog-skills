# hrv-ramp-test — troubleshooting

- **`Export not found`** — a bare name is looked up in the StrapLog iCloud folder; the file may not have downloaded yet, or it lives elsewhere. Ask for the path.
- **"is not a StrapLog export"** — an HRV Logger `DB.sqlite` or some other SQLite. Only StrapLog snapshots are supported; re-export from StrapLog.
- **Tiny recording (< 300 beats)** — the wrong recording was picked, or the strap lost contact. Check `--list` for `n_beats`.
- **Auto-pick grabbed the wrong recording** — the user probably has a morning readiness reading newer than the ramp test. Re-run with `--recording <PK>` using the correct PK from the `--list` output.
- **VT1 and VT2 are identical** — the test was too short; α1 fell from >0.75 to <0.50 in fewer than two windows. Retry with a gentler ramp or longer stages so the descent is captured.

## Output files

| File | What it is |
|---|---|
| `zone_table.md` | Human-readable thresholds + zone table |
| `zone_table.csv` | Machine-readable zones (for importing to a training log) |
| `windowed_features.csv` | Per-window HR, α1, beat count — useful for custom plots or time-tracking |
| `crossplot_hr_alpha1.png` | HR vs α1 scatter with VT1/VT2 lines |
| `timeseries_hr_alpha1.png` | Dual-axis HR + α1 with event markers |
