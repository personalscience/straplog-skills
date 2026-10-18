# StrapLog export schema

The script never queries SQLite itself: `straplog.db` (PyPI package `straplog`)
loads the recording and converts times. The tables it reads:

- `recording` — one row per recording (`id` = the `--recording` PK, `name`,
  `started_at_ms` UTC, `tz_offset_s`).
- `beat` — raw RR intervals in beat order (`rr_ms`; `t_end_ms_est` anchored on
  phone receipt). Its presence is how an export is recognised.
- `event` — `kind='marker'` rows are the user's markers (`label` and/or `note`).

StrapLog stores no α1 or other app-computed features, so this skill computes
α1 from the beats. Times are UTC plus `tz_offset_s`; `straplog.db` converts
them to local wall-clock. The format contract is `docs/SCHEMA.md` in the
StrapLog app repo.
