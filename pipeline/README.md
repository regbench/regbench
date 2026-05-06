# `pipeline/` — RegBench construction & evaluation code

These are the research-grade scripts used to produce the released benchmark and the paper's results. They are shipped for transparency and reproducibility, not as a packaged library.

> **Heads-up.** The scripts contain workspace-specific paths (`/workspace/...`), corpus mount points (e.g. `/workspace/ocx_explanation/DNV-RU-SHIP-Pt3/...`), proxy settings, and an internal cache layer (`pipeline/shared/llm_client.py`). To run them on a fresh machine you will need to: (i) provide your own corpus mirror, (ii) override the path constants at the top of each module, (iii) set API credentials in your environment, and (iv) decide whether to keep or replace the proxy / cache plumbing in `llm_client.py`.

## Layout

| Subdir | Stage | Key script(s) |
|---|---|---|
| `ingestion/` | DNV PDF → markdown | `pdf_converter/pdf_to_latex.py` (driver), `pdf_converter/Magic-PDF/` (vendored MinerU). DNV-only; Basel uses public markdown directly. See `ingestion/README.md` for registration + run instructions |
| `graph/` | Cross-reference graph extraction | `build_xref_graph.py` (DNV), `build_xref_graph_basel.py` (Basel) |
| `generation/` | Chain-anchored scenario synthesis | `generate_500_v6.py` (DNV main), `generate_basel_v7.py` (Basel), `generate_from_chains.py` (chain-driven generator) |
| `audit/` | v5d selective audit | `auto_gt_audit.py` (driver), `gt_audit_prompts.py` (locked prompts) |
| `baselines/` | Evaluation baselines | `run_codex_baseline.py` (`full_context`), `run_bm25_baseline.py`, `run_chainrag_baseline.py`, `run_closedbook_baseline.py` |
| `judge/` | Strict atomic-fact-conjunction grading | `judge_rag_results.py` |

> **Note on `llm_client.py`.** The shipped runners use `sys.path.insert(0, Path(__file__).parent)` and `from llm_client import LLMClient`, so each subdir (`audit/`, `baselines/`, `generation/`, `judge/`) carries its own copy of `llm_client.py`. If you patch the wrapper, patch all four. The wrapper is an OpenAI-protocol caching + retry + token-logging client; it works against any vendor whose endpoint speaks the OpenAI protocol (Anthropic, GPT-5, Qwen, MiniMax, …) — point it at the right base URL via your `.env`.

## Reproducing the headline numbers

The paper reports strict accuracy on the **scale pool** (DNV N=499, Basel N=281) for five evaluation methods × both corpora, plus a 13-system pilot panel on the 47-Q pilot pool. The corresponding scripts are:

| Paper section | Pool | Method | Script |
|---|---|---|---|
| §4.2 leaderboard | DNV / Basel | full_context (Sonnet 4.6, GPT-5.4, Qwen3.6-Plus, MiniMax-M2.7) | `baselines/run_codex_baseline.py` |
| §4.2 leaderboard | DNV / Basel | BM25 + Sonnet 4.6 | `baselines/run_bm25_baseline.py` |
| §4.4 pilot panel | pilot | ChainRAG (oracle-chain analysis) | `baselines/run_chainrag_baseline.py` |
| Appendix (closed-book) | pilot / DNV | closed-book floor | `baselines/run_closedbook_baseline.py` |
| §3.3 v5d audit | candidate pool | v5d selective audit | `audit/auto_gt_audit.py` + `prompts/v5d_audit.md` |
| §4.1 grading | all pools | strict atomic-fact judge | `judge/judge_rag_results.py` (or the simplified `examples/grade_strict.py`) |

The judge in `judge/judge_rag_results.py` matches the production version used in the paper (Sonnet 4.6 primary). For a self-contained reference grader without the workspace-specific cache layer, use `examples/grade_strict.py` instead.
