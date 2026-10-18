# DFA α1 quality gate — the full argument

**Read this only when you need it:** the script's `rr_quality.flags` is
non-empty, `alpha1_trustworthy` is false, or someone is disputing an α1
result. When the gate passes clean, `SKILL.md`'s one-line summary is enough
and this file is wasted context.

Two failure modes push α1 in **opposite** directions, and neither looks
obviously broken:

- **Missed/extra beats** (loose or dry strap) randomize the series → α1
  falsely **low**, mimicking hard intensity.
- **A genuinely over-smoothed / interpolated stream** kills beat-to-beat
  variability → α1 falsely **high**, mimicking an easy effort.

## 🚫 Do NOT flag over-smoothing from the level of |ΔRR|

**Low |ΔRR| is mostly just hard exercise.** Beat-to-beat variability collapses
with intensity (vagal withdrawal). Measured on Richard, within a single
session whose capture was independently confirmed clean:

| HR band | median &#124;ΔRR&#124; | RMSSD |
|---|---|---|
| 100–109 | **5.0 ms** | 8.7 |
| 110–129 | 2.0 ms | 2.9–4.3 |
| 130–149 | **1.0 ms** | 1.9–2.5 |

So the old rule — *"3–8 ms is normal, ≤1.5 ms means filtered"* — simply
re-detects hard efforts and defames the strap. It fired a false alarm on the
2026-07-18 ride and cost a real α1 dataset.

**Never compare |ΔRR| across sessions of different intensity.** That was the
specific error: the bike (mean HR 134) was judged against a walk (115) and a
lift (106), and the intensity difference was attributed to strap contact.
Compare only at **matched HR**, or not at all.

## The tests that actually discriminate

1. **Within-session slope (the script does this).** Real RR shows variability
   that **declines as HR rises**; a filtered stream is *flat*, because a
   filter does not care how hard you are working. The summary reports
   `median_abs_drr_by_hr_band` and `variability_tracks_intensity`. A clean
   monotonic decline ⇒ the capture is fine, whatever the level. Worked
   example, 2026-07-26 ride: 16.0 ms @ HR 80 → 7.0 → 4.0 → 2.0 → 1.0 @ 140.
2. **The app's own artifact count — the strongest single witness.** HRV
   Logger's exported `Features` CSV has an **`artifacts_removed`** column from
   Altini's own detector. On the 2026-07-18 ride it totalled **6 beats out of
   21,356 (0.03%)** — an independent verdict that the strap was clean.
3. **Quantization is a non-issue.** RR arrives as whole milliseconds. Adding
   ±0.5 ms dither and recomputing moved α1 by 0.01. Do not spend time here.
4. **Companion-track HR reconciliation** tells a *beat-detection* problem from
   a physiology one. If track and RR agree (r ≥ 0.8, MAE ≤ 6 bpm), the strap
   detected beats correctly and every HR-derived metric is sound. If they
   disagree, that is real dropout and HR itself becomes suspect. Report which
   situation you are in — never let Richard think a flagged α1 means something
   is wrong with his heart.

## Cross-checking α1 against an independent computation

StrapLog stores raw RR only, with no app-computed α1, so the export alone can't
confirm a computed α1. The pipeline itself has been validated: on 2026-07-18
(recorded in HRV Logger) the app's mean α1 was **1.484** against this
pipeline's **1.493**, a difference of **0.009**. That killed the "our pipeline
has an offset" hypothesis outright. For a disputed session, reconcile HR
against a companion track (point 4 above) and, if needed, run the RR through
an independent tool (Kubios) before blaming the code.

## Interpreting the α1 *level* — a real open question for Richard

Even with clean capture, **Richard's cycling α1 sits ~1.5**, far above the
literature expectation. Observed three times now (2026-07-11, 07-18, 07-26;
the 07-26 ride ran mean 1.525 with 98.5% of windows above 1.00). On **walks**
α1 behaves textbook-like (dipping to 0.71 at a marked breathing breakpoint);
on the **bike** it never gets there, while still declining monotonically with
intensity (r ≈ −0.33). Candidate mechanism: cadence/respiratory entrainment
at a steady 80–90 rpm producing regular RR oscillation that inflates α1.
**Unresolved.**

Practical consequence: **do not apply the absolute 0.75 = VT1 threshold to his
cycling data** until a bike ramp test shows where α1 actually crosses 0.75 and
whether that HR matches the breathing breakpoint. Report α1's *shape and
range*; anchor bike zones to breathing/RPE. The %HRR (Karvonen) zones stay
valid regardless. Never report "N% of windows sub-AeT" on a bike ride as a
physiological claim — it is this offset showing up again.

This is the operational form of the `feedback_dfa_alpha1_gates` memory: never
trust an α1 threshold without checking coverage and sanity-checking against
RPE.

## `rr_quality.flags` codes

stdout carries `code: numbers`; `summary.md` expands the explanation.

| Code | Emitted when | Means |
|---|---|---|
| `flat_drr_across_hr` | ≥2 HR bands span ≥20 bpm and the low/high |ΔRR| ratio is < 1.5x | variability does not track intensity — the RR stream looks over-smoothed / interpolated rather than vagally withdrawn. Physiology gives a large spread (measured 5x on the bike, 7x on a walk); a filtered stream sits near 1.0x |
| `drr_untestable_low` | median |ΔRR| ≤ 1.5 ms and too little HR range to test the slope | α1 level unverified — cross-check against an independent HR source (companion track) before trusting it |
| `implausible_rmssd` | mean HR > 100 and RMSSD > 60 ms | missed or extra beat artifacts |

Remember rule 1 in SKILL.md: the *level* of |ΔRR| never justifies a flag on its own, and it is not comparable across sessions of different intensity. Only the slope is.
