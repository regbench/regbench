# `data/` — released JSONL splits

| File | Items | Tiers | Domain |
|---|---:|---|---|
| `pilot.jsonl` | 47 | T0–T4 | DNV-RU-SHIP (early pilot, carries `gold_pages`) |
| `dnv.jsonl` | 499 | T0–T4 | DNV-RU-SHIP (main pool) |
| `basel.jsonl` | 281 | T0–T3 | 12 CFR Part 217 (Basel III) |

Total: **827 questions / 4,766 atomic-fact propositions**.

All three files are post-repair under release **v1.0.1** (2026-05-04) and bit-identical to the Hugging Face mirror at `regbench/regbench-release`. Selection is deterministic — items are sorted by id and emitted in order.

## `repair_audit/`

Stage-A integrity-patch trail for v1.0.1:

- `dnv_stageA_repairs_2026-05-04.csv` — every fact correction applied to a previously-released DNV item, with SME-confirmed defective text and the corrected text.
- `dnv_repair_diff_2026-05-04.csv` — id-level before/after diff for the v1.0.1 patch.

Pilot and Basel JSONLs are bit-identical to v1.0.0 (only DNV changed in v1.0.1).

## Schema

See [`../docs/SCHEMA.md`](../docs/SCHEMA.md).
