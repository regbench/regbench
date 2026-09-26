# Answer-key sensitivity (perturbed keys)

Supports the answer-key sensitivity study (paper Appendix, "Independence checks", Table `tab:keysens`).
Model answers and the judge (DeepSeek-v4-pro, temperature 0) are held fixed; only the answer key varies.

Run `python verify.py` to recompute every number in the table from these files.

## Files

| File | Contents |
|---|---|
| `dnv_perturbed_keys.json` | All 328 DNV key-revision proposals (DeepSeek-v4-pro, at most one change per item, sees source text): operation, targeted facts, replacement, proposer's reason, full perturbed `required_facts`, and the adjudicating SME's decision (pseudonyms A-D) with rationale where one was written. `in_paper_analysis` marks the 143 accepted merge/split/reword revisions analysed in the paper; accepted drops are excluded because deleting a requirement changes the task. |
| `dnv_rejudge.json` | Strict verdicts under the released and the perturbed key for 5 systems x 292 accepted items; 715 pairs (143 items) are in the paper analysis. The perturbed-key run records per-item fact counts, not per-fact flags. |
| `basel_perturbed_keys.json` | All 281 Basel items with one balanced operation each (GPT-5.4, seed 20260803): `recomp` (same requirements and fact count, new wording and boundaries; 95), `compress` (merge two adjacent facts; 94), `dilute` (split one fact; 92). Each key was certified interchangeable by one SME (pseudonyms R1-R3) in a randomised, unlabelled side-by-side comparison with fact counts hidden. |
| `basel_rejudge.json` | Per-fact and strict verdicts under the released and the perturbed key for 5 systems x 281 items. |

Released-key verdicts are the same judge's verdicts on the released keys (`iclr2027/cross_judge/frozen_systems_deepseek.json`).
Enumeration prefixes ("1. ", "2. ") that the Basel generator added to fact strings have been removed; fact text is otherwise verbatim.
