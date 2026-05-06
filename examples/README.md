# Examples

Self-contained scripts that depend only on `datasets` and `anthropic` (or `openai`).

| Script | Purpose |
|---|---|
| `load_dataset.py` | Load RegBench from Hugging Face or from the local `data/*.jsonl` files |
| `grade_strict.py` | Reference strict atomic-fact-conjunction grader (Sonnet 4.6 judge) |

For full-pipeline reproductions (graph extraction → generation → v5d audit → baselines), see `pipeline/` — those scripts are research-grade and have workspace-specific paths you will need to override.

## Running the strict grader

1. Run any baseline (or your own model) and write predictions as JSONL where each line is one item:

   ```json
   {"id": "R500_0001", "tier": 0, "question_text": "...", "required_facts": ["...", "..."], "answer": "model's full response"}
   ```

2. Set your API key and run:

   ```bash
   export ANTHROPIC_API_KEY=...
   python examples/grade_strict.py --predictions out/sonnet_fc.jsonl --out out/sonnet_fc.graded.jsonl
   ```

3. The script prints a per-tier strict-accuracy table and writes the per-item judge verdicts back to disk.
