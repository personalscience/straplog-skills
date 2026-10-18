# CLAUDE.md — straplog-skills

Claude Code plugin (`personalscience/straplog-skills`) with four Polar H10 HRV skills for StrapLog exports: `h10-session`, `hrv-overnight`, `hrv-ramp-test`, `hrv-resting`. Each skill is a `SKILL.md` plus one PEP 723 `uv` script; loading and HRV algorithms come from the `straplog` package on PyPI (`~/dev/straplog-py`).

## Commands

```
uv run skills/<skill>/scripts/<script>.py --list      # smoke test against the newest export
uvx ruff check --select F skills/*/scripts/*.py
claude plugin validate .
```

## Rules that the tree does not tell you

- StrapLog input only. HRV Logger (`DB.sqlite`) support was dropped on 2026-10-18; don't reintroduce format branches.
- Algorithms (artifact filter, time-domain metrics, DFA α1, windowed features, chart style) live in `straplog.hrv` / `straplog.plot`. Fix them there and bump `straplog>=` here; never re-copy them into a script.
- Verify a script change against a real StrapLog export (old vs new outputs), not only `--list`. Never commit `*.sqlite`: they're personal health data.
- hrv-resting's CSV log columns are a contract: changing one breaks log continuity.

## Vault

- Plan: `~/.claude/plans/straplog-skills-consolidation.md`
