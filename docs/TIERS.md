# Tier (chain-depth) definitions

RegBench uses a fixed **context-scope tier schema** that is corpus-independent in its definition but realised per-corpus by which level of the corpus's native hierarchy fills the schema's *chapter* / *Part* slots:

| Tier | Definition |
|---|---|
| **T0** | Single-clause local derivation |
| **T1** | Single-section derivation requiring a table, formula, conditional branch, or multiple clauses |
| **T2** | Cross-section reasoning within the same chapter |
| **T3** | Cross-chapter reasoning within the same Part |
| **T4** | Cross-Part or cross-volume reasoning |

## Per-corpus realisation

| Corpus | Tiers used | Why |
|---|---|---|
| DNV-RU-SHIP | T0, T1, T2, T3, T4 | Full schema realisable within the corpus |
| Basel III §217 | T0, T1, T2, T3 | T4-style cross-Part chains leave the public §217 scope and are excluded |

The chapter slot is filled by *DNV Chapter* on DNV and by *Basel Subpart of Part 217* on Basel.

## Reading the tiers

- Tier labels are **context-scope controls**, not exact hop-count labels and not claims that same-numbered tiers across corpora carry identical substantive difficulty.
- Cross-corpus claims concern the **tier-degradation pattern** (monotonic accuracy drop across tiers within a corpus), not absolute level.
- Per-tier counts in the released pools are deliberately tier-balanced so degradation analyses are not driven by sample-size imbalance:

| Pool | Per-tier counts |
|---|---|
| DNV (R500_/F500_) | T0=100, T1=100, T2=100, T3=99, T4=100 (one item killed post-eval) |
| Basel | T0=69, T1=70, T2=71, T3=71 |
| DNV pilot (P50_) | 10 per tier; T4 has 7 |
