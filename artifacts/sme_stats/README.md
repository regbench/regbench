# RegBench — aggregated SME disagreement statistics (release package)

Generated 2026-07-24. Shipped in the RegBench release at `artifacts/sme_stats/`.

## Purpose
Supports independent verification of the v5d audit-calibration and judge-quality
claims (paper Table 1 and §4.1) as committed in the NeurIPS 2026 author response
(Reviewer zT9c, W2/Q2).

## Redaction policy
Aggregate statistics only. This package contains **no SME free-text comments and
no per-item SME labels**; raw annotation records remain internal to preserve
audit-slate integrity (paper §release). Every number is either a frozen paper
value (source-annotated to its paper location) or a documented recomputation
(provenance below).

## Files
- `v5d_calibration_slices.{json,csv}` — per-slice v5d-vs-SME agreement,
  recall/precision on SME-flagged items, pass-set miss rates, IAA overlap
  statistics, with Wilson 95% CIs and per-slice annotation-visibility status.
- `disposition_ledger.{json,csv}` — v5d flag -> SME disposition
  (repair / kill / spurious) per audited slice, and the 837 -> 827 kill ledger.
- `judge_calibration_aggregate.json` — §4.1 pairwise aggregates
  (SME-1 x judge, SME-2 x judge, SME-1 x SME-2) with raw agreement, n,
  Wilson 95% CIs, and annotation-visibility notes for each annotator.
  Chance-corrected (Gwet AC1) values are reported in the paper §4.1 and
  are omitted here; see `chance_corrected_note` in that file.
- `judge_calibration_per_pairing.csv` — SME-1 vs primary-judge per-fact
  agreement per model-condition pairing, with disagreement direction
  (judge-lenient = judge present / SME absent; judge-strict = reverse).

## Provenance note for `judge_calibration_per_pairing.csv`
Computed as: SME-1 fact-presence labels (2,453; annotation campaign, April 2026)
intersected with the **released-state** judge verdicts (post-release-repair,
47-question pilot pool). Labels on subsequently-killed items have no released
counterpart and are counted in `labels_without_release_counterpart` rather than
dropped silently. The §4.1 aggregate (2,444/2,453 = 99.6%) was computed during
the annotation campaign against the then-current verdicts; the per-pairing table
here is the released-state view. Both computations are reproducible from the
released pool + this package.
