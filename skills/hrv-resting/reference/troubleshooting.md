# hrv-resting — troubleshooting

Read when a run fails or a number looks wrong.

- **`Export not found`** — user exported to a different path, or the iCloud file isn't downloaded yet. Ask where, or open it in Files to download.
- **"is not a StrapLog export"** — an HRV Logger `DB.sqlite` or some other SQLite. Only StrapLog snapshots are supported; re-export the recording from StrapLog.
- **"Only N RR intervals (< 60)"** — recording is too short. Record a longer reading.
- **"After artifact correction only N beats remain"** — reading was noisy (movement, loose strap). Try a new reading; don't force-analyze this one.
- **Wrong tag** — either pass `--tag <correct>` or confirm the phone's clock matches the user's local time. StrapLog stores the phone's tz offset with each recording, so a wrong phone clock or time zone gives a wrong tag.
- **Log rows out of order** — the script sorts by `timestamp_wall` on every append, so this shouldn't happen; if it does, the CSV was hand-edited.
- **Trend plot flat or with wild outliers** — check `~/Documents/hrv_resting_log.csv` directly; `--no-append` any bad rows out with a text editor.
- **"Marked window … holds only N clean beats"** — diamonds are too close together or land outside the RR stream. Check the tachogram's diamond labels, then use `--session-window` or `--markers ignore`.
- **SDNN much larger than expected, RMSSD normal** — HR drift, not variability. Almost always an unmarked settle-in or stand-up tail; check `window_source` is `markers`.
- **`recording_pk` doesn't match the current export** — older log rows came from HRV Logger, whose `Z_PK` values were renumbered whenever its database was rebuilt, and StrapLog `recording.id`s are a separate numbering. Join rows on `timestamp_wall` + `recording_name`, never on PK alone.

