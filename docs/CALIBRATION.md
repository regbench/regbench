# v5d audit calibration

The v5d selective audit (§3.3 of the paper) is a single locked LLM prompt with three failure-mode guardrails (E1: chart/table row-column mismatch; E2: formula integrity; E3: default-vs-special branch error). The prompt was **locked on a 148-Q DNV TRAIN audit** and applied unchanged to held-out DNV TEST and to Basel III §217 without retuning.

## Calibration table (paper Table 2, condensed)

| Audit design | Pool | Items audited | Facts audited | v5d routed | Routed confirmed | v5d missed | Item agreement | Fact agreement |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Full audit (pass + routed) | DNV pilot | 49 | 277 | 9 | 77.8 % | 1 | 93.9 % | 98.2 % |
| Full audit (pass + routed) | DNV TRAIN+TEST | 200 | 1,117 | 38 | 100 % | 1 | **99.0 %** | **99.1 %** |
| Routed + 20-pass sample | Basel §217 audited slice | 79 | 478 | 59 | 94.9 % | 0 | 96.2 % | 94.77 % |
| Selective routing only | DNV selective audit split | 46 | 271 | 46 | 100 % | — | 100 % | 94.5 % |
| **Aggregate** | union of audit slices | 374 | 2,143 | 152 | 96.7 % | 2 | 97.9 % | 97.4 % |

## Headline figures

- **Held-out DNV TEST primary miss-rate:** 0/446 fact-level misses on v5d-passed items (rule-of-three 95 % upper bound ≤ 0.67 %); 98.94 % fact-level agreement with SME source-grounded re-review.
- **Combined 200-item DNV TRAIN+TEST audit pool:** 198/200 = 99.0 % item-level agreement; 1,107/1,117 = 99.1 % fact-level agreement; 97.4 % recall, 100 % precision on SME-flagged items.
- **Basel transfer:** locked v5d prompt applied unchanged. v5d flags 59/288 items at a tier-monotonic rate; SME re-review confirms 56/59 = 94.9 % as fact-level defective; 76/79 = 96.2 % item / 453/478 = 94.77 % fact agreement on the audited slice; 0/122 fact-level defects on the 20-item v5d-pass sample.

## Cross-vendor robustness (pilot-scope)

A vendor-swap on the 49-question dense pilot replays the locked v5d prompt with Claude Sonnet against the GPT-5.4-xhigh primary. Sonnet runs at `medium` reasoning effort (deterministic, temperature = 0). Replay agrees on 43/49 dispositions (Cohen's κ = 0.45); all 6 disagreements are GPT-5.4-flag / Sonnet-pass, so Sonnet's flag-set is a *subset* of the primary's — consistent with compute-dependent sensitivity, not vendor idiosyncrasy.

## Why we report two registers

- **Primary register (fact-level-blind miss rate on v5d-passed items)** is the scientifically-interesting quantity and is independent of audit flagging at the fact level — it bounds how much defect remains in the released pool after v5d gating.
- **Anchored register (item-level / fact-level agreement on the full audit pool)** is computed with v5d's item-level disposition visible to SMEs as an advisory hint and so carries less evidential weight on its own. We keep them separate.

The locked v5d prompt is shipped in `prompts/v5d_audit.md`. The audit driver is `pipeline/audit/auto_gt_audit.py` and the per-module prompt templates are in `pipeline/audit/gt_audit_prompts.py`.
