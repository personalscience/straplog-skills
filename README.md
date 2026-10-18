# StrapLog skills for Claude Code

Four [Claude Code](https://claude.com/claude-code) skills that analyze Polar H10 recordings exported from the StrapLog iOS app (one SQLite file per recording, RR intervals plus markers).

| Skill | Ask Claude… | You get |
|---|---|---|
| `h10-session` | "analyze this morning's ride" | HR, Karvonen %HRR zones, DFA α1, cardiac drift, per-set peaks and HRR for lifts, optional GPX/FIT overlay |
| `hrv-overnight` | "how was my HRV last night" | overnight RMSSD/SDNN trajectory in blocks, Bluetooth coverage gaps, reconciliation against Apple Watch sleep staging |
| `hrv-ramp-test` | "analyze my ramp test" | DFA α1 vs HR, AeT/VT1 (α1 = 0.75) and VT2 (α1 = 0.50) crossings, training zones |
| `hrv-resting` | "log today's resting HRV" | RMSSD, LnRMSSD, SDNN, pNN50, Poincaré plot, and a longitudinal CSV log with a rolling baseline |

## Install

```
/plugin marketplace add personalscience/straplog-skills
/plugin install straplog-skills@personalscience
```

Requires [`uv`](https://docs.astral.sh/uv/) (`brew install uv`). Each script declares its dependencies inline (PEP 723), including the [`straplog`](https://pypi.org/project/straplog/) package, so `uv run` installs them on first use.

## Data

StrapLog exports `<YYMMDD-HHMM>-<name>.sqlite` to its iCloud Drive folder (`~/Library/Mobile Documents/iCloud~com~personalscience~straplog/Documents/`). Every script takes `--db` as a path or a bare export name in that folder; without it, the newest downloaded export is used. `uvx straplog ls` lists what's there.

## License

MIT
