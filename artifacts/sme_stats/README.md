# RegBench — aggregated SME disagreement statistics (release package)

Generated 2026-07-24. Staged for review; final home: release repo `artifacts/sme_stats/`.

## Purpose
Supports independent verification of the v5d audit-calibration and judge-quality
claims (paper Table 1 and §4.1) as committed in the NeurIPS 2026 author response
(Reviewer zT9c, W2/Q2).

## Redaction policy
Two tiers, deliberately different.

**Top level — aggregate statistics only.** The files listed below contain no SME
free-text and no per-item SME labels. Every number is either a frozen paper value
(source-annotated to its paper location) or a documented recomputation.

**`disagreements/` — contested labels only.** This subfolder does ship per-item
SME labels and the reviewer's free-text on those items, because a 99.6% agreement
claim cannot be independently checked by inspecting the 99.6% that agreed. Only
disagreements are released: labels where the audit and the reviewer, or the two
reviewers, differ. Agreeing labels carry no verification value and are withheld,
which preserves audit-slate integrity — a reader learns which calls were contested,
not which items are clean.

## Files
- `v5d_calibration_slices.{json,csv}` — per-slice v5d-vs-SME agreement,
  recall/precision on SME-flagged items, pass-set miss rates, IAA overlap
  statistics, with Wilson 95% CIs (as `*_ci_lo` / `*_ci_hi`). The JSON
  additionally carries per-slice annotation-visibility status.
- `disposition_ledger.{json,csv}` — v5d flag -> SME disposition
  (repair / kill / spurious) per audited slice, and the 837 -> 827 kill ledger.
- `judge_calibration_aggregate.json` — §4.1 pairwise aggregates
  (SME-1 x judge, SME-2 x judge, SME-1 x SME-2) with AC1 and annotation-
  visibility notes for each annotator.
- `judge_calibration_per_pairing.csv` — SME-1 vs primary-judge per-fact
  agreement per model-condition pairing, with disagreement direction
  (judge-lenient = judge present / SME absent; judge-strict = reverse).

### `disagreements/` — the disagreement ledger
- `judge_disagreements.json` — every fact-level disagreement in the three §4.1
  pairings: judge x SME-1 (9 of 2,453), judge x SME-2 (60 of 2,350), SME-1 x SME-2
  (37 of 1,341). Each record carries the direction and the annotator's
  annotation-visibility status, because SME-1 reviewed anchored (judge verdict
  visible and pre-selected) while SME-2 reviewed unanchored — nine disagreements
  from an anchored reviewer and sixty from an unanchored one are not the same
  measurement. `merge_note` documents which SME-1 merge backs which published
  figure.
- `qa_audit_disagreements_dnv.json` — v5d-vs-reviewer conflicts on item quality
  for the DNV remaining-300 flagged set: 3 fact-level conflicts across 7 items.
- `qa_audit_disagreements_basel.json` — the same for Basel §217: 19 fact-level
  conflicts across 18 items, plus 5 disposition overrules. `scope_note` gives the
  bridge from these records to Table 2's 453/478.

Records referencing items the audit later KILLED are retained, not dropped —
removing them would hide the disagreements that caused the kill. Each file states
how many of its records fall in that category.

These are v5d-vs-SME on **item quality** (`qa_audit_*`) and judge-vs-SME on
**answer grading** (`judge_*`). They answer different questions and are kept apart
deliberately.

## Provenance note for `judge_calibration_per_pairing.csv`
Computed as: SME-1 fact-presence labels (2,453; annotation campaign, April 2026)
intersected with the **released-state** judge verdicts (post-release-repair,
47-question pilot pool). Labels on subsequently-killed items have no released
counterpart and are counted in `labels_without_release_counterpart` rather than
dropped silently. The §4.1 aggregate (2,444/2,453 = 99.6%) was computed during
the annotation campaign against the then-current verdicts; the per-pairing table
here is the released-state view. Both computations are reproducible from the
released pool + this package.
