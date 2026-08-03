# RegBench — Source-Grounded Benchmarks for Regulatory Cross-Reference Reasoning

[![License: CC BY-NC 4.0](https://img.shields.io/badge/License-CC--BY--NC--4.0-blue.svg)](LICENSE)
[![Dataset on HF](https://img.shields.io/badge/%F0%9F%A4%97%20Dataset-RegBench-yellow)](https://huggingface.co/datasets/regbench/regbench-release)

RegBench is a benchmark for **source-grounded cross-reference traversal** in regulatory documents: given a scenario, a model must follow explicit cross-references through the corpus, apply the resulting chain, and produce an answer that contains every required atomic fact.

Items are scored at the **work-product level** (strict atomic-fact conjunction): an answer is correct only if every `required_facts` proposition is verifiably present and grounded in the source. This catches the "guessed verdict, broken derivation" failure mode that aggregate scoring hides.

> **Paper:** RegBench: Source-Grounded Benchmarks for Regulatory Cross-Reference Reasoning (NeurIPS 2026 Datasets and Benchmarks Track)
> **Project page:** https://regbench.github.io/
> **Dataset (Hugging Face):** https://huggingface.co/datasets/regbench/regbench-release

---

## Release contents

| Config | Items | Tiers | Domain | Notes |
|---|---:|---|---|---|
| `pilot` | 47 | T0–T4 | DNV-RU-SHIP (early pilot) | Carries `gold_pages` (human-verified evidence pages) for retrieval-baseline use |
| `dnv`   | 499 | T0–T4 | DNV-RU-SHIP (main pool) | Headline ship-design results |
| `basel` | 281 | T0–T3 | 12 CFR Part 217 (Basel III) | Cross-Part chains exiting §217 are out of scope |

Total: **827 questions / 4,766 atomic-fact propositions**, all post-repair (release v1.0.1, 2026-05-04). Tier (chain depth) definitions are within-domain. Cross-domain comparisons concern *pattern* (monotonic degradation across tiers), not absolute level.

```
.
├── data/                    # Released JSONL splits + repair audit trail
│   ├── pilot.jsonl
│   ├── dnv.jsonl
│   ├── basel.jsonl
│   └── repair_audit/        # v1.0.1 Stage-A integrity patch trail
├── examples/                # Minimal load + grade scripts
├── pipeline/                # Construction & evaluation code (research-grade)
│   ├── ingestion/           # DNV PDF → markdown (vendored MinerU + driver)
│   ├── graph/               # Cross-reference graph extraction
│   ├── generation/          # Chain-anchored scenario synthesis
│   ├── audit/               # v5d selective audit
│   ├── baselines/           # full_context, BM25, ChainRAG, closed-book runners
│   └── judge/               # Strict atomic-fact conjunction grading
├── artifacts/               # Croissant metadata + aggregated SME statistics
│   └── sme_stats/           # v5d/judge calibration slices, disposition ledger
├── prompts/                 # Locked v5d audit prompt
└── docs/                    # Schema, tier definitions, calibration notes
```

---

## Quickstart

### 1. Load the dataset

Either pull directly from the Hugging Face Hub:

```python
from datasets import load_dataset

dnv   = load_dataset("regbench/regbench-release", "dnv",   split="test")
basel = load_dataset("regbench/regbench-release", "basel", split="test")
pilot = load_dataset("regbench/regbench-release", "pilot", split="test")
```

…or read the JSONL files shipped in `data/` directly:

```python
import json
with open("data/dnv.jsonl") as f:
    dnv = [json.loads(line) for line in f]
print(dnv[0]["question_text"][:200])
print("required facts:", dnv[0]["required_facts"][:2])
```

### 2. Grade a model output (strict atomic-fact conjunction)

See `examples/grade_strict.py` for a minimal Sonnet-4.6 judge that returns a strict-correct verdict iff every `required_facts` proposition is verifiably present in the model output.

### 3. Run a baseline

`pipeline/baselines/` contains the runners used in the paper:

| Script | What it does |
|---|---|
| `run_codex_baseline.py` | Practitioner-scope (`full_context`) — anchor-Part expansion, capped at 600K chars (DNV); full §217 (Basel) |
| `run_bm25_baseline.py` | BM25 top-10 over the full corpus, fed to a Sonnet 4.6 reader |
| `run_chainrag_baseline.py` | Oracle-chain context (analysis, not main leaderboard) |
| `run_closedbook_baseline.py` | No-source closed-book floor |

> **Note.** Pipeline scripts in this release reflect the research-grade code used to produce the paper. They contain workspace-specific paths (`/workspace/...`) and depend on a corpus snapshot of DNV PDFs and Basel III §217 markdown. To run them on a fresh machine you will need to (i) provide a corpus mirror via the ingestion pipeline (see [`pipeline/ingestion/README.md`](pipeline/ingestion/README.md)), (ii) override the path constants at the top of each module, (iii) drop a `.env` next to `llm_client.py` with your gateway URL + API key, and (iv) optionally export `all_proxy` if you need to route outbound traffic through a proxy.

---

## Item schema

| Field | Type | Description |
|---|---|---|
| `id` | string | Stable item identifier (e.g. `R500_0201`, `B_T2_0000`, `P50_037`) |
| `domain` | string | `dnv_ru_ship`, `dnv_ru_ship_pilot`, or `basel_12cfr217` |
| `tier` | int | Chain depth, 0–4 (DNV) / 0–3 (Basel) |
| `source_section` | string | Anchor section identifier in the corpus where reasoning starts |
| `chain` | object/null | `{start, end, path, depth, ...}` — the cross-reference traversal the answer must apply (Basel only carries an explicit chain object; DNV chain metadata lives inside `annotator_grounding`) |
| `question_text` | string | Scenario-style question. Self-contained; chain identifiers are *not* leaked into the prompt |
| `format` | string | `mcq` or `explanation` |
| `options` | list/null | MCQ options (when `format == "mcq"`) |
| `correct` | string/null | MCQ correct option key (when `format == "mcq"`) |
| `required_facts` | list[string] | Atomic propositions the answer must contain. **Strict-conjunction graded** |
| `annotator_grounding` | object | Source-grounded rationale used by the audit (chain identifiers, target section quotes, derivation steps) |
| `scenario_parameters` | object | Numeric / categorical inputs that define the scenario |
| `tested_pattern` | string | Reasoning pattern category (e.g. `survey_check`, `applicability_filter`, `quantitative_apply`) |
| `leak_check` | object | Leak-filter trace (regex pass + leaked tokens, if any) |
| `gold_pages` | object | **(`pilot` only)** Human-verified evidence pages per chain step, used for retrieval baselines |

Concrete worked example: see Appendix A of the paper (`F500_0015`, DNV Pt3.Ch12.Sec10, T2) and `docs/SCHEMA.md`.

---

## Source corpora and licensing

- **DNV Ship Rules (DNV-RU-SHIP)** — accessible at <https://rules.dnv.com> after free DNV registration. Upstream rights-holders retain copyright; **we do not redistribute regulatory text**. To regenerate the markdown corpus the rest of the pipeline operates on, register with DNV, download the relevant section-level PDFs, drop them into `pipeline/ingestion/pdf_converter/metadata_files/`, and run the vendored MinerU converter — see [`pipeline/ingestion/README.md`](pipeline/ingestion/README.md) for the step-by-step.
- **Basel III §217 (12 CFR Part 217)** — public-domain US federal regulation, accessible at `ecfr.gov`. The pipeline consumes the published markdown directly; no PDF extraction needed.

Benchmark artifacts in this repository (scenarios, `required_facts`, chain metadata, code) are released under **CC-BY-NC 4.0** (see `LICENSE`). Source-corpus PDFs are subject to upstream licensing.

---

## v5d selective audit

The locked v5d audit prompt is shipped in `prompts/v5d_audit.md` and the Python audit driver in `pipeline/audit/auto_gt_audit.py`. Calibration on the held-out DNV TEST split: **0/446** fact-level miss rate on v5d-passed items (rule-of-three 95% upper bound ≤ 0.67%); **99.0%** item / **99.1%** fact agreement with SME source-grounded re-review on the combined 200-item DNV TRAIN+TEST audit pool. The prompt and rules are applied unchanged to Basel III §217.

See `docs/CALIBRATION.md` for the full calibration table and the cross-corpus port protocol.

---

## Intended use

- Evaluating LLMs and retrieval systems on source-grounded multi-hop regulatory reasoning.
- Studying chain-depth degradation and the gap between conclusion-level and strict-conjunction grading ("phantom credit").
- Auditing benchmark-construction pipelines for regulatory corpora.

## Out-of-scope use

- Training data for production-grade compliance systems.
- Broad regulatory-reasoning competence claims beyond explicit cross-reference traversal.
- Legal-precedent QA, open-textured interpretation, version-spanning analysis, adversarial framing — these are out of scope by construction.

## Limitations

- Two corpora (marine engineering + US banking); transfer to FDA / FAA / IRC / non-English regimes is not demonstrated.
- 827 Q is modest in scale; we traded scale for verified quality (every released item passes the v5d audit, every flagged item passes SME source-grounded re-review).
- Tier (chain-depth) definitions are within-domain; cross-domain claims concern *pattern* rather than absolute level.
- Closed-book performance is 0–4% across the evaluated panel — item-level memorization is precluded by construction (scenarios are LLM-synthesized after corpus snapshots), but subtler latent contamination cannot be fully ruled out.

---

## Citation

```bibtex
@inproceedings{regbench2026,
  title     = {RegBench: Source-Grounded Benchmarks for Regulatory Cross-Reference Reasoning},
  author    = {Anonymous},
  booktitle = {NeurIPS 2026 Datasets and Benchmarks Track},
  year      = {2026}
}
```

(Author block to be updated in the camera-ready version; anonymous during review.)

## Contact

See the `paper` field in the OpenReview submission for author contact (anonymous during review).
