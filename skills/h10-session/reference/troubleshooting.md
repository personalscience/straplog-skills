# h10-session — troubleshooting

Read when a run fails or a number looks wrong.

- **"is not a StrapLog export"** — an HRV Logger `DB.sqlite` or some other SQLite. Only StrapLog snapshots are supported; re-export the recording from StrapLog.
- **`coverage` well below 1.0** — Bluetooth dropped. Check for a time gap; drift and HRR spanning the gap are meaningless.
- **α1 gate fails on every ride but not on walks** — that's the cycling artifact pattern. Wet the electrodes thoroughly, tighten the strap, re-seat it ~10 min in.
- **Track and RR don't overlap** — wrong `--utc-offset` (PDT `-7`, PST `-8`). The JSON reports `overlap_min`; near zero means misalignment, not a bad ride.
- **27 "sets" detected in a strength session** — prominence too low for a circuit with constant transitions. Raise `--prominence` to 12–15.
