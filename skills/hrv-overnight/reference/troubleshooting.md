# hrv-overnight — troubleshooting

Read when a run fails or a number looks wrong.

- **"No recording looks like an overnight strip"** — the strip was named
  something unusual and started outside 20:00–03:00, or is under 180 min. Run
  `--list` and pass `--recording <pk>`.
- **"is not a StrapLog export"** — an HRV Logger `DB.sqlite` or some other
  SQLite. Only StrapLog snapshots are supported; re-export from StrapLog.
- **Coverage well below 100%** — Bluetooth dropped. Check `gaps` for where,
  and do not report metrics spanning them.
- **Awake windows show no HR bump** — check the timestamps first (a tz
  double-shift moves everything by hours), then suspect the staging.
