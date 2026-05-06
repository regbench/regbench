#!/usr/bin/env python3
"""
BM25 retrieval baseline for RegBench v7 questions.

Indexes all processed section markdown files. For each question, retrieves
top-K sections by BM25 score, concatenates their text, and feeds to the
answering LLM. Same judge as the oracle/full_context baselines.

Also computes retrieval recall@K against gold pages (at section level).
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# os.environ.setdefault("all_proxy", "socks5://127.0.0.1:1084")  # uncomment to route via local SOCKS bridge

sys.path.insert(0, str(Path(__file__).parent))
from llm_client import LLMClient
from rank_bm25 import BM25Okapi

PT1_DIR = Path("/workspace/ocx_explanation/DNV-RU-SHIP-Pt1")
PT3_DIR = Path("/workspace/ocx_explanation/DNV-RU-SHIP-Pt3")
PT5_DIR = Path("/workspace/ocx_explanation/DNV-RU-SHIP-Pt5")

MAX_CONTEXT_CHARS = 600_000


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_section(sec_id):
    m = re.match(r'Pt(\d+)\.Ch(\d+)\.Sec(\d+)', sec_id)
    if not m: return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def section_to_dir(sec_id):
    p = parse_section(sec_id)
    if not p: return None
    pt, ch, sec = p
    base_map = {1: PT1_DIR, 3: PT3_DIR, 5: PT5_DIR}
    base = base_map.get(pt)
    if not base: return None
    return base / f"DNV-RU-Pt{pt}-Chap{ch}-sec{sec}" / "pipeline"


def load_section_text(sec_id):
    pipeline = section_to_dir(sec_id)
    if not pipeline or not pipeline.exists(): return ""
    for f in pipeline.glob("*.md"):
        if "_raw" not in f.name and "_content" not in f.name:
            return f.read_text(errors="replace")
    return ""


def discover_all_sections() -> list[str]:
    """Find all section IDs across Pt1, Pt3, Pt5."""
    sections = []
    for base in [PT1_DIR, PT3_DIR, PT5_DIR]:
        if not base.exists(): continue
        for d in sorted(base.iterdir()):
            m = re.match(r'DNV-RU-Pt(\d+)-Chap(\d+)-sec(\d+)', d.name)
            if m:
                sections.append(f"Pt{m.group(1)}.Ch{m.group(2)}.Sec{m.group(3)}")
    return sections


def build_bm25_index(sections: list[str]) -> tuple:
    """Build BM25 index over section texts."""
    texts = []
    valid_sections = []
    for sid in sections:
        text = load_section_text(sid)
        if text:
            texts.append(text)
            valid_sections.append(sid)

    # Tokenize: simple whitespace + lowercase
    tokenized = [t.lower().split() for t in texts]
    bm25 = BM25Okapi(tokenized)
    return bm25, valid_sections, texts


def retrieve_topk(bm25, sections, texts, query: str, k: int = 10) -> list[tuple[str, str, float]]:
    """Retrieve top-K sections for a query. Returns [(section_id, text, score)]."""
    tokenized_query = query.lower().split()
    scores = bm25.get_scores(tokenized_query)

    ranked = sorted(zip(sections, texts, scores), key=lambda x: -x[2])
    return ranked[:k]


def compute_section_recall(retrieved_sections: list[str], gold_pages: list[dict]) -> dict:
    """Recall at section level: did we retrieve any section containing a gold page?"""
    gold_sections = set(gp["section"] for gp in (gold_pages or []))
    if not gold_sections:
        return {"recall": None, "gold_sections": 0, "hit": 0}
    hits = gold_sections & set(retrieved_sections)
    return {
        "recall": len(hits) / len(gold_sections),
        "gold_sections": len(gold_sections),
        "hit": len(hits),
        "retrieved": len(retrieved_sections),
    }


ANSWER_PROMPT = """\
You are a ship-design verification assistant. Below are excerpts from the DNV ship \
classification rules retrieved by a search system. Read them, then answer the question.

## RETRIEVED DOCUMENTS

{context}

## QUESTION

{question}

## INSTRUCTIONS
Find the relevant clauses in the retrieved documents and reason through them step by \
step. Cite the clause numbers you use. Answer with concrete numeric results where asked.
"""


JUDGE_PROMPT = """\
You are grading an AI system's answer to a DNV ship-design question.

## QUESTION
{question}

## EXPECTED CORRECT DERIVATION (ground truth)
{expected}

## REQUIRED FACTS
{required_facts}

## THE MODEL'S RESPONSE
{response}

## YOUR TASK
For each required fact, determine whether the model's response contains an equivalent \
statement. Output a single JSON object (no fences):

{{
  "fact_verdicts": [
    {{"fact": "<fact verbatim>", "present": true|false, "evidence": "<short quote>"}},
    ...
  ],
  "facts_present": <int>,
  "facts_total": <int>,
  "is_correct": true|false,
  "rationale": "<one-sentence rationale>"
}}

Mark `is_correct: true` only if ALL required facts are present.
"""


def strip_to_json(raw):
    raw = raw.strip()
    if "<think>" in raw:
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```\w*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    return raw


def judge_response(question, response, client, judge_model, cache_key):
    required = question.get("required_facts") or []
    if not required:
        return {"is_correct": False, "error": "no_required_facts"}
    grounding = question.get("annotator_grounding", {}) or {}
    expected = grounding.get("expected_derivation") or grounding.get("chain_summary") or ""
    req_str = "\n".join(f"- {f}" for f in required)
    prompt = JUDGE_PROMPT.format(
        question=question.get("question_text", ""),
        expected=expected, required_facts=req_str,
        response=response,
    )
    raw = client.call_with_retry(
        messages=[{"role": "user", "content": prompt}],
        model=judge_model, max_tokens=16384, temperature=0.0,
        cache_key=cache_key,
    )
    raw = strip_to_json(raw)
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        return {"is_correct": False, "error": "judge_parse_fail"}
    try:
        return json.loads(raw[i:j + 1])
    except json.JSONDecodeError:
        return {"is_correct": False, "error": "judge_json_fail"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="input", default="/workspace/regbench_pilot/pilot_49_v7_with_gold_pages.json")
    parser.add_argument("--out", default="/workspace/regbench_pilot/pilot_49_v7_bm25_results.json")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--judge", default="claude-sonnet-4-6")
    parser.add_argument("--topk", type=int, default=10)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    questions = json.loads(Path(args.input).read_text())
    if args.limit:
        questions = questions[:args.limit]

    log(f"Loaded {len(questions)} questions")

    # Build BM25 index
    log("Building BM25 index over all sections...")
    all_sections = discover_all_sections()
    log(f"  Found {len(all_sections)} sections")
    bm25, indexed_sections, indexed_texts = build_bm25_index(all_sections)
    log(f"  Indexed {len(indexed_sections)} sections with text")

    client = LLMClient(
        cache_dir="/workspace/regbench_pilot/bm25_cache",
        token_log_path="/workspace/regbench_pilot/bm25_token_log.jsonl",
    )

    results = []
    for i, q in enumerate(questions):
        # Retrieve
        topk = retrieve_topk(bm25, indexed_sections, indexed_texts, q["question_text"], k=args.topk)
        retrieved_sids = [sid for sid, _, _ in topk]

        # Build context
        context_parts = []
        total = 0
        for sid, text, score in topk:
            if total + len(text) > MAX_CONTEXT_CHARS:
                break
            context_parts.append(f"## {sid} (score: {score:.1f})\n\n{text}")
            total += len(text)
        context = "\n\n".join(context_parts)

        # Answer
        prompt = ANSWER_PROMPT.format(context=context, question=q["question_text"])
        cache_key = f"bm25_mt16k_k{args.topk}_{q['id']}_{args.model}"
        try:
            response = client.call_with_retry(
                messages=[{"role": "user", "content": prompt}],
                model=args.model, max_tokens=16384, temperature=0.0,
                cache_key=cache_key,
            )
        except Exception as e:
            response = f"[ERROR: {type(e).__name__}: {e}]"

        # Judge
        verdict = judge_response(
            q, response, client, args.judge,
            cache_key=f"judge_bm25_mt16k_k{args.topk}_{q['id']}_{args.model}_{args.judge}",
        )

        # Recall
        recall = compute_section_recall(retrieved_sids, q.get("gold_pages", []))

        r = {
            "id": q["id"],
            "tier": q["tier"],
            "condition": f"bm25_k{args.topk}",
            "retrieved_sections": retrieved_sids,
            "context_chars": len(context),
            "recall": recall,
            "response": response,
            "verdict": verdict,
            "is_correct": bool(verdict.get("is_correct", False)),
        }
        results.append(r)
        mark = "✓" if r["is_correct"] else "✗"
        rec = f"{recall['recall']:.2f}" if recall["recall"] is not None else "—"
        log(f"  [{i+1}/{len(questions)}] {mark} T{q['tier']} {q['id']} "
            f"recall={rec} facts={verdict.get('facts_present','?')}/{verdict.get('facts_total','?')}")

    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))

    # Aggregate
    log(f"\n{'='*60}")
    log(f"BM25 top-{args.topk} → {args.model}")
    log(f"{'='*60}")
    by_tier = defaultdict(lambda: {"correct": 0, "total": 0, "recall_sum": 0, "recall_n": 0})
    for r in results:
        t = r["tier"]
        by_tier[t]["total"] += 1
        if r["is_correct"]:
            by_tier[t]["correct"] += 1
        rec = r["recall"].get("recall")
        if rec is not None:
            by_tier[t]["recall_sum"] += rec
            by_tier[t]["recall_n"] += 1

    log(f"{'Tier':<6} {'acc':<15} {'section_recall'}")
    for t in sorted(by_tier):
        s = by_tier[t]
        acc = 100 * s["correct"] / max(s["total"], 1)
        rec = 100 * s["recall_sum"] / max(s["recall_n"], 1)
        log(f"T{t:<5} {s['correct']:>2}/{s['total']:<2} ({acc:4.1f}%)    {rec:5.1f}%")

    tot_c = sum(s["correct"] for s in by_tier.values())
    tot_n = sum(s["total"] for s in by_tier.values())
    log(f"TOTAL: {tot_c}/{tot_n} ({100*tot_c/tot_n:.1f}%)")
    log(f"\nToken usage: {client.get_token_usage()}")


if __name__ == "__main__":
    main()
